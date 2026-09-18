"""Read OMR sheets from the command line, with no GUI anywhere in sight.

Purpose:
    Make Phase 3 usable and debuggable without Qt: point it at one scan or a
    folder of them, get JSON results, an overlay showing what was decided, and
    - when asked - the full staged diagnostic dump.

    This is also the honest test of the architecture. If recognition can be
    driven from a terminal, then nothing in it depends on a window, and a future
    server, notebook or continuous-integration job can drive it too.

Usage::

    python -m omr_scanner.tools.recognise scan.png --template sheet.omrt

    python -m omr_scanner.tools.recognise scans/ --template sheet.omrt
        --json-dir out/results --overlay-dir out/overlays --workers auto

    python -m omr_scanner.tools.recognise scan.png --template sheet.omrt
        --diagnostics out/diagnostics --verbose

    (each example is one command; the options are wrapped for legibility)

Exit codes:
    ``0`` every sheet was read, ``1`` at least one failed, ``2`` the arguments
    were wrong (argparse's own code).

What does NOT belong here:
    * Any algorithm, any threshold, any decision. This module parses arguments,
      calls :class:`~omr_scanner.services.recognition_service.RecognitionEngine`
      through the batch processor, and prints. Nothing here may be the only
      implementation of anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from omr_scanner.config.processing import ProcessingMode, ProcessingSettings, detected_cpu_count
from omr_scanner.errors import OMRScannerError
from omr_scanner.services.batch_processor import BatchOptions, process_batch
from omr_scanner.services.recognition_models import RecognitionOutcome, ScanResult
from omr_scanner.services.recognition_settings import DiagnosticsOptions, RecognitionOptions
from omr_scanner.services.scan_import import collect_scan_files
from omr_scanner.services.template_service import load_template

EXIT_OK = 0
EXIT_FAILURES = 1

_LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the tool."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.recognise",
        description="Recognise one scan, or a folder of them, against a template.",
    )
    parser.add_argument(
        "inputs",
        type=Path,
        nargs="+",
        help="Scan files and/or folders of scans.",
    )
    parser.add_argument(
        "--template",
        type=Path,
        required=True,
        help="The .omrt template the sheets were printed from.",
    )
    parser.add_argument(
        "--json-dir",
        type=Path,
        help="Write one <scan>.json result document per sheet into this folder.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        help="Write every result as one JSON array to this file ('-' for stdout).",
    )
    parser.add_argument(
        "--overlay-dir",
        type=Path,
        help="Write an annotated overlay image per sheet into this folder.",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="Write the full staged diagnostic dump per sheet into this folder.",
    )
    parser.add_argument(
        "--failures-only",
        action="store_true",
        help="With --diagnostics, write nothing for a sheet that came back clean.",
    )
    parser.add_argument(
        "--workers",
        default="1",
        help=(
            "Worker processes: a number, or 'auto' to let OMRFlow choose. "
            f"This machine reports {detected_cpu_count()} logical CPUs."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the summary line.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log every stage's timing at DEBUG level.",
    )
    return parser


def resolve_workers(requested: str, item_count: int) -> int:
    """Turn the ``--workers`` argument into a worker count.

    Args:
        requested: ``"auto"`` or a positive integer, as typed.
        item_count: How many scans there are.

    Returns:
        The number of workers to use, never more than there are scans.

    Raises:
        ValueError: The argument was neither ``auto`` nor a positive integer.

    Uses the same :class:`~omr_scanner.config.processing.ProcessingSettings`
    the GUI does, rather than a second rule: "auto" must mean the same thing in
    both places, or a batch benchmarked here will not behave as measured when it
    is run from the application.
    """
    if requested.strip().lower() in {"auto", "automatic"}:
        return ProcessingSettings(mode=ProcessingMode.AUTOMATIC).resolve_worker_count(item_count)
    try:
        count = int(requested)
    except ValueError as exc:
        raise ValueError(f"--workers must be a number or 'auto', got '{requested}'") from exc
    if count < 1:
        raise ValueError(f"--workers must be at least 1, got {count}")
    return ProcessingSettings(
        mode=ProcessingMode.CUSTOM, worker_count=count
    ).resolve_worker_count(item_count)


def _describe(result: ScanResult) -> str:
    """One line per sheet, in the order a person reads it."""
    return (
        f"{result.source_path.name}: {result.outcome.value}"
        f"  roll={result.identifier_value or '-'}"
        f"  set={result.set_code_value or '-'}"
        f"  review={result.review_count}"
        f"  status={','.join(result.status_codes)}"
        f"  {result.elapsed_seconds:.2f}s"
    )


def _write_overlay(result: ScanResult, template_path: Path, directory: Path) -> None:
    """Re-read one sheet with a preview and save its annotated overlay.

    The batch deliberately discards page images - a hundred rectified pages is
    most of a gigabyte - so producing an overlay afterwards means reading that
    one sheet again. That is the right trade for a command line tool: the cost
    is paid only for the sheets a person asked to look at.
    """
    import cv2

    from omr_scanner.imaging.alignment import align_sheet
    from omr_scanner.services.alignment_service import (
        alignment_config_from_template,
        load_scan_image,
    )
    from omr_scanner.services.recognition_diagnostics import render_overlay

    template = load_template(template_path)
    image = load_scan_image(result.source_path, color=False)
    alignment = align_sheet(image, config=alignment_config_from_template(template))
    overlay = render_overlay(alignment.normalized_image, result, show_empty=True)

    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{result.source_path.stem}_overlay.png"
    success, buffer = cv2.imencode(".png", overlay)
    if success:
        destination.write_bytes(buffer.tobytes())


def main(argv: list[str] | None = None) -> int:
    """Run the tool and return a shell exit code."""
    parser = build_parser()
    arguments = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        template = load_template(arguments.template)
    except OMRScannerError as exc:
        print(f"Could not load the template: {exc.user_message}", file=sys.stderr)
        return EXIT_FAILURES

    paths = list(collect_scan_files(arguments.inputs))
    if not paths:
        print("No supported scans were found in the given paths.", file=sys.stderr)
        return EXIT_FAILURES

    try:
        workers = resolve_workers(arguments.workers, len(paths))
    except ValueError as exc:
        parser.error(str(exc))

    diagnostics = DiagnosticsOptions(
        enabled=arguments.diagnostics is not None,
        directory=arguments.diagnostics,
        failures_only=arguments.failures_only,
    )
    options = RecognitionOptions(
        with_preview=False, diagnostics=diagnostics
    )

    if not arguments.quiet:
        print(
            f"Reading {len(paths)} scan(s) with '{template.name}' "
            f"on {workers} worker(s)..."
        )

    report = process_batch(
        paths,
        template,
        options=BatchOptions(metrics_config=options.metrics, recognition=options),
        workers=workers,
    )

    results = [item.result for item in report.processed]
    failures = sum(
        result.outcome in (RecognitionOutcome.ERROR, RecognitionOutcome.REGISTRATION_FAILED)
        for result in results
    )

    if not arguments.quiet:
        for result in results:
            print(_describe(result))

    if arguments.json_dir is not None:
        arguments.json_dir.mkdir(parents=True, exist_ok=True)
        for result in results:
            destination = arguments.json_dir / f"{result.source_path.stem}.json"
            destination.write_text(
                json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
            )

    if arguments.json is not None:
        payload = json.dumps([result.to_dict() for result in results], indent=2, ensure_ascii=False)
        if str(arguments.json) == "-":
            print(payload)
        else:
            arguments.json.parent.mkdir(parents=True, exist_ok=True)
            arguments.json.write_text(payload, encoding="utf-8")

    if arguments.overlay_dir is not None:
        for result in results:
            if result.registration.value == "registration_failed":
                continue
            _write_overlay(result, arguments.template, arguments.overlay_dir)

    print(
        f"{report.total} processed - {report.complete_count} complete, "
        f"{report.review_count} for review, {report.failed_count} failed "
        f"({report.worker_count} worker(s), {report.elapsed_seconds:.2f}s)"
    )
    return EXIT_FAILURES if failures else EXIT_OK


if __name__ == "__main__":
    # Guarded because a worker pool re-imports this module under `spawn`; see
    # `omr_scanner.services.parallel_batch`.
    raise SystemExit(main())
