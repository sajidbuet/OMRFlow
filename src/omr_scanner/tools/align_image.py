"""Align one scan from the command line and report what was measured.

Purpose:
    Give a developer a way to point the alignment engine at a single image, see
    the geometry it found and the numbers behind that decision, and write the
    rectified page and the diagnostic overlays somewhere for inspection.

Usage::

    python -m omr_scanner.tools.align_image scan.png --output aligned.png

    python -m omr_scanner.tools.align_image scan.png
        --template resources/templates/example_answer_sheet.omrt
        --output out/aligned.png
        --debug out/debug/

    (the second example is one command; the options are wrapped for legibility)

Exit codes:
    ``0`` aligned, ``1`` the sheet was rejected with a reason, ``2`` the
    arguments were wrong (argparse's own code).

What does NOT belong here:
    * Any algorithm. This is a front end onto
      :func:`omr_scanner.imaging.align_sheet` and
      :mod:`omr_scanner.services.alignment_service`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omr_scanner.errors import ImagingError, TemplateError
from omr_scanner.imaging import AlignmentConfig, align_sheet
from omr_scanner.imaging.diagnostics import (
    render_detection_overlay,
    render_normalized_preview,
    summarize,
)
from omr_scanner.services.alignment_service import (
    alignment_config_from_template,
    load_scan_image,
    save_image,
)
from omr_scanner.services.template_service import load_template

EXIT_OK = 0
EXIT_ALIGNMENT_FAILED = 1


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the tool."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.align_image",
        description="Normalise one OMR scan into canonical template coordinates.",
    )
    parser.add_argument("image", type=Path, help="Scan to align.")
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="An .omrt template supplying the canonical geometry. Without it the "
        "engine's built-in defaults are used, which describe the example sheet.",
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="Where to write the rectified page."
    )
    parser.add_argument(
        "--debug",
        type=Path,
        default=None,
        help="Directory to write diagnostic images into: the detection overlay on "
        "the source scan and the rectified page with its expected marker targets.",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace existing output files."
    )
    parser.add_argument(
        "--color",
        action="store_true",
        help="Read and rectify in colour instead of grayscale.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the tool.

    Args:
        argv: Command line arguments, excluding the program name. Defaults to
            :data:`sys.argv`.

    Returns:
        A process exit code.
    """
    arguments = build_parser().parse_args(argv)

    try:
        config = _configuration(arguments.template, diagnostics=arguments.debug is not None)
    except TemplateError as error:
        print(f"Template error: {error}", file=sys.stderr)
        return EXIT_ALIGNMENT_FAILED

    try:
        image = load_scan_image(arguments.image, color=arguments.color)
        result = align_sheet(image, config=config)
    except ImagingError as error:
        print(f"Alignment failed [{error.code}]: {error}", file=sys.stderr)
        return EXIT_ALIGNMENT_FAILED

    print(summarize(result))

    if arguments.output is not None:
        save_image(result.normalized_image, arguments.output, overwrite=arguments.overwrite)
        print(f"\nWrote rectified page to {arguments.output}")

    if arguments.debug is not None:
        arguments.debug.mkdir(parents=True, exist_ok=True)
        overlay = render_detection_overlay(image, result)
        preview = render_normalized_preview(result, config=config)
        save_image(overlay, arguments.debug / "detection.png", overwrite=True)
        save_image(preview, arguments.debug / "normalized.png", overwrite=True)
        (arguments.debug / "summary.txt").write_text(summarize(result), encoding="utf-8")
        print(f"Wrote diagnostics to {arguments.debug}")

    return EXIT_OK


def _configuration(template_path: Path | None, *, diagnostics: bool) -> AlignmentConfig:
    """Build the alignment configuration, from a template when one is supplied."""
    if template_path is None:
        return AlignmentConfig(diagnostics=diagnostics)
    return alignment_config_from_template(
        load_template(template_path), diagnostics=diagnostics
    )


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
