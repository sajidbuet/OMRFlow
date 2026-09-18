"""Score the recognition engine against a labelled dataset.

Purpose:
    Run Phase 3 over a dataset whose correct answers are known, report what it
    got right and - more usefully - classify everything it got wrong, so that a
    change to the engine can be judged by evidence rather than impression.

Usage::

    python -m omr_scanner.tools.benchmark_recognition out/dataset
        --template examples/templates/ece_0000_sample.omrt
        --report out/benchmark --workers auto

    python -m omr_scanner.tools.benchmark_recognition out/dataset
        --template sheet.omrt --report out/benchmark
        --baseline out/benchmark_before/summary.json

    python -m omr_scanner.tools.benchmark_recognition out/dataset
        --template sheet.omrt --sweep-fill 0.25 0.60 0.05

    (each example is one command; the options are wrapped for legibility)

Output::

    <report>/summary.json   dataset-level metrics
    <report>/errors.csv     one row per disagreement

Exit codes:
    ``0`` the benchmark ran, ``1`` it could not, ``2`` bad arguments. A poor
    *score* is not a non-zero exit: this tool measures, and a build that fails
    on a heuristic metric teaches a team to stop reading it.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.config.processing import ProcessingMode, ProcessingSettings
from omr_scanner.errors import OMRScannerError
from omr_scanner.evaluation.benchmark import (
    BenchmarkReport,
    compare_baseline,
    evaluate,
    load_summary,
    write_report,
)
from omr_scanner.evaluation.ground_truth import (
    load_ground_truth_directory,
    load_manifest,
)
from omr_scanner.evaluation.synthetic_dataset import (
    GROUND_TRUTH_DIRNAME,
    IMAGES_DIRNAME,
    MANIFEST_FILENAME,
)
from omr_scanner.services.batch_processor import BatchOptions, process_batch
from omr_scanner.services.recognition_settings import RecognitionOptions
from omr_scanner.services.scan_import import collect_scan_files
from omr_scanner.services.template_service import load_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.ground_truth import SheetGroundTruth
    from omr_scanner.services.recognition_models import ScanResult

EXIT_OK = 0
EXIT_FAILED = 1


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the tool."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.benchmark_recognition",
        description="Compare recognition output with a dataset's ground truth.",
    )
    parser.add_argument(
        "dataset",
        type=Path,
        help=f"Dataset folder containing {IMAGES_DIRNAME}/ and {GROUND_TRUTH_DIRNAME}/.",
    )
    parser.add_argument(
        "--template", type=Path, required=True, help="The .omrt template to read with."
    )
    parser.add_argument("--report", type=Path, help="Folder to write summary.json and errors.csv.")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="A previous summary.json to compare this run against.",
    )
    parser.add_argument("--workers", default="1", help="Worker processes, or 'auto'.")
    parser.add_argument(
        "--sweep-fill",
        nargs=3,
        type=float,
        metavar=("START", "STOP", "STEP"),
        help=(
            "Re-run the dataset at each fill-ratio threshold in this range and "
            "print the accuracy of each. Reports only; changes nothing."
        ),
    )
    parser.add_argument("--limit", type=int, help="Only read the first N scans.")
    return parser


def _resolve_workers(requested: str, item_count: int) -> int:
    """Turn ``--workers`` into a count, using the application's own rule."""
    if requested.strip().lower() in {"auto", "automatic"}:
        return ProcessingSettings(mode=ProcessingMode.AUTOMATIC).resolve_worker_count(item_count)
    count = int(requested)
    return ProcessingSettings(
        mode=ProcessingMode.CUSTOM, worker_count=max(count, 1)
    ).resolve_worker_count(item_count)


def _run(
    paths: list[Path],
    template: OmrTemplate,
    workers: int,
    options: RecognitionOptions,
) -> list[ScanResult]:
    """Recognise every scan and return the results."""
    report = process_batch(
        paths,
        template,
        options=BatchOptions(recognition=options),
        workers=workers,
    )
    return [item.result for item in report.processed]


def _print_summary(report: BenchmarkReport) -> None:
    """Print the headline numbers a person reads first."""
    summary = report.summary
    print()
    print(f"Dataset:          {summary.dataset or '-'}")
    print(f"Engine:           {summary.engine_version or '-'}")
    print(f"Scans:            {summary.scans}"
          f"  (processed {summary.processed}, failed {summary.failed},"
          f" expected failures {summary.expected_failures})")
    print(
        f"Roll accuracy:    {summary.roll_accuracy:.4f}  "
        f"({summary.roll_correct}/{summary.roll_checked})"
    )
    print(
        f"Set accuracy:     {summary.set_accuracy:.4f}  "
        f"({summary.set_correct}/{summary.set_checked})"
    )
    print(
        f"Answer accuracy:  {summary.question_accuracy:.4f}  "
        f"({summary.questions_correct}/{summary.questions_checked})"
    )
    print(
        f"Blank accuracy:   {summary.blank_accuracy:.4f}  "
        f"({summary.blanks_correct}/{summary.blanks_expected})"
    )
    print(
        f"Multiple marks:   {summary.multiple_accuracy:.4f}  "
        f"({summary.multiples_correct}/{summary.multiples_expected})"
    )
    print(
        f"Borderline marks: {summary.ambiguous_handled_rate:.4f}  "
        f"({summary.ambiguous_handled}/{summary.ambiguous_expected} handled acceptably)"
    )
    print(f"Flagged for review: {summary.flagged_questions} question(s)")
    print(f"Time:             {summary.total_seconds:.2f}s total, "
          f"{summary.mean_seconds_per_scan:.3f}s per scan")

    if summary.error_counts:
        print("\nErrors by category:")
        for category, count in sorted(summary.error_counts.items()):
            print(f"  {category:<24} {count}")
    if summary.accuracy_by_confidence:
        print("\nAnswer accuracy by decision score (not a calibrated probability):")
        for band, values in sorted(summary.accuracy_by_confidence.items()):
            print(
                f"  {band:<12} {values['accuracy']:.4f}  "
                f"({int(values['correct'])}/{int(values['questions'])})"
            )


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and return a shell exit code."""
    arguments = build_parser().parse_args(argv)

    images = arguments.dataset / IMAGES_DIRNAME
    truth_dir = arguments.dataset / GROUND_TRUTH_DIRNAME
    manifest_path = arguments.dataset / MANIFEST_FILENAME

    try:
        template = load_template(arguments.template)
        truths = load_ground_truth_directory(truth_dir)
    except (OMRScannerError, FileNotFoundError, ValueError) as exc:
        print(f"Could not start the benchmark: {exc}", file=sys.stderr)
        return EXIT_FAILED

    paths = list(collect_scan_files([images if images.is_dir() else arguments.dataset]))
    if arguments.limit is not None:
        paths = paths[: arguments.limit]
    if not paths:
        print(f"No scans found in {images}", file=sys.stderr)
        return EXIT_FAILED

    dataset_name = arguments.dataset.name
    if manifest_path.is_file():
        # A damaged manifest is not a reason to refuse to measure the data.
        with contextlib.suppress(ValueError):
            dataset_name = load_manifest(manifest_path).name or dataset_name

    workers = _resolve_workers(arguments.workers, len(paths))
    options = RecognitionOptions(with_preview=False)

    print(f"Reading {len(paths)} scan(s) on {workers} worker(s)...")
    results = _run(paths, template, workers, options)
    report = evaluate(results, truths, dataset=dataset_name)
    _print_summary(report)

    if arguments.report is not None:
        summary_path, errors_path = write_report(report, arguments.report)
        print(f"\nWritten: {summary_path}\n         {errors_path}")

    if arguments.baseline is not None:
        try:
            baseline = load_summary(arguments.baseline)
        except (OSError, ValueError) as exc:
            print(f"Could not read the baseline: {exc}", file=sys.stderr)
            return EXIT_FAILED
        print("\nAgainst the baseline:")
        for comparison in compare_baseline(baseline, report.summary):
            print(
                f"  {comparison.metric:<20} {comparison.baseline:.4f} -> "
                f"{comparison.current:.4f}  {comparison.verdict}"
            )

    if arguments.sweep_fill is not None:
        _sweep_fill(paths, template, truths, dataset_name, workers, arguments.sweep_fill)

    return EXIT_OK


def _sweep_fill(
    paths: list[Path],
    template: OmrTemplate,
    truths: dict[str, SheetGroundTruth],
    dataset_name: str,
    workers: int,
    bounds: list[float],
) -> None:
    """Re-score the dataset at a range of fill thresholds and print the result.

    A calibration *hook*, not a calibration: it shows how accuracy moves with
    the threshold on this data, and stops there. Nothing is written back into
    the template or the defaults, because synthetic sheets are not evidence
    about real pencil - and because a tool that silently retunes an engine
    makes every later measurement meaningless.
    """
    start, stop, step = bounds
    if step <= 0:
        print("--sweep-fill needs a positive step", file=sys.stderr)
        return

    print("\nFill-threshold sweep (synthetic data only - not a calibration):")
    print(f"{'threshold':>10} {'answers':>10} {'blanks':>10} {'multiples':>10}")

    value = start
    while value <= stop + 1e-9:
        # The threshold lives in the template, which is immutable, so each step
        # reads with a copy. That is also the honest model: a threshold is a
        # property of a sheet design, and a sweep is asking "what if this design
        # had been tuned differently".
        settings = template.recognition.model_copy(
            update={"fill_ratio_threshold": value}
        )
        variant = template.model_copy(update={"recognition": settings})
        results = _run(paths, variant, workers, RecognitionOptions(with_preview=False))
        summary = evaluate(results, truths, dataset=dataset_name).summary
        print(
            f"{value:>10.3f} {summary.question_accuracy:>10.4f} "
            f"{summary.blank_accuracy:>10.4f} {summary.multiple_accuracy:>10.4f}"
        )
        value += step


if __name__ == "__main__":
    # Guarded: a worker pool re-imports this module under `spawn`.
    raise SystemExit(main())
