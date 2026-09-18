"""Measure batch recognition throughput at several worker counts.

Purpose:
    Answer "is more workers actually faster on this machine, and where does it
    stop helping?" with measured numbers rather than an assumption - and give
    the automatic-mode cap in
    :mod:`omr_scanner.config.processing` an evidence base.

Why this is a script and not a test:
    A throughput assertion fails when the machine is busy, which teaches nobody
    anything. This prints what it measured and leaves the judgement to a person.

Usage::

    python scripts/benchmark_batch.py                      # real sample sheet
    python scripts/benchmark_batch.py --scans 48 --workers 1,2,4,8,12
    python scripts/benchmark_batch.py --source synthetic   # smaller, faster

The dataset is built by copying one sheet, so every run reads identical pages
and the only variable is the worker count.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT / "src") not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from omr_scanner.config.processing import (  # noqa: E402
    AUTOMATIC_WORKER_LIMIT,
    ProcessingSettings,
    detected_cpu_count,
)
from omr_scanner.services.batch_processor import process_batch  # noqa: E402
from omr_scanner.services.template_service import load_template  # noqa: E402

SAMPLE_IMAGE = REPOSITORY_ROOT / "examples" / "ECE-0000.png"
SAMPLE_TEMPLATE = REPOSITORY_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"
DEFAULT_SCANS = 24
DEFAULT_WORKERS = "1,2,4,8"


def build_dataset(directory: Path, source: Path, count: int) -> list[Path]:
    """Copy ``source`` ``count`` times into ``directory`` and return the paths."""
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index in range(count):
        destination = directory / f"scan{index:03d}{source.suffix}"
        if not destination.exists():
            shutil.copy2(source, destination)
        paths.append(destination)
    return paths


def synthetic_dataset(directory: Path, count: int) -> tuple[list[Path], object]:
    """Render a synthetic sheet ``count`` times, for a machine without the sample."""
    import cv2

    sys.path.insert(0, str(REPOSITORY_ROOT))
    from tests.conftest import build_answer_sheet_template, render_marked_sheet

    template = build_answer_sheet_template()
    marks = {
        "roll_number": dict(enumerate("120317")),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }
    directory.mkdir(parents=True, exist_ok=True)
    image = render_marked_sheet(template, marks)
    paths = []
    for index in range(count):
        path = directory / f"scan{index:03d}.png"
        if not path.exists():
            cv2.imwrite(str(path), image)
        paths.append(path)
    return paths, template


def run(paths: list[Path], template: object, workers: int) -> tuple[float, int]:
    """Process the dataset once and return ``(seconds, sheets read cleanly)``."""
    started = time.perf_counter()
    report = process_batch(paths, template, workers=workers)  # type: ignore[arg-type]
    elapsed = time.perf_counter() - started
    return elapsed, report.complete_count + report.review_count


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and print a table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scans", type=int, default=DEFAULT_SCANS)
    parser.add_argument("--workers", default=DEFAULT_WORKERS, help="comma separated")
    parser.add_argument("--source", choices=["real", "synthetic"], default="real")
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=REPOSITORY_ROOT / "test-output" / "benchmark",
        help="Where the copied scans are written (git-ignored by default).",
    )
    arguments = parser.parse_args(argv)

    counts = [int(value) for value in arguments.workers.split(",") if value.strip()]

    if arguments.source == "real":
        if not SAMPLE_IMAGE.is_file() or not SAMPLE_TEMPLATE.is_file():
            parser.error(f"Sample sheet or template missing: {SAMPLE_IMAGE}")
        template = load_template(SAMPLE_TEMPLATE)
        paths = build_dataset(arguments.dataset_dir, SAMPLE_IMAGE, arguments.scans)
    else:
        paths, template = synthetic_dataset(arguments.dataset_dir, arguments.scans)

    cpus = detected_cpu_count()
    automatic = ProcessingSettings().resolve_worker_count(len(paths), cpus)
    print(f"Dataset:   {len(paths)} scans from {arguments.source} sheet")
    print(f"Machine:   {cpus} logical CPUs")
    print(f"Automatic: {automatic} workers (cap {AUTOMATIC_WORKER_LIMIT})")
    print()
    print(f"{'workers':>8} {'seconds':>10} {'scans/s':>10} {'speed-up':>10} {'read':>6}")

    baseline: float | None = None
    for workers in counts:
        elapsed, read = run(paths, template, workers)
        if baseline is None:
            baseline = elapsed
        print(
            f"{workers:>8} {elapsed:>10.2f} {len(paths) / elapsed:>10.2f} "
            f"{baseline / elapsed:>9.2f}x {read:>6}"
        )
    return 0


if __name__ == "__main__":
    # Required before any worker pool starts: a spawned child re-imports this
    # module, and without the guard it would rerun the benchmark recursively.
    raise SystemExit(main())
