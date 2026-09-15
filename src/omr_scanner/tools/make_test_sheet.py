"""Render a synthetic OMR sheet, optionally distorted, from the command line.

Purpose:
    Produce something for :mod:`omr_scanner.tools.align_image` to work on
    without a real scan, and make the manual smoke test reproducible on a
    machine that holds no examination data.

Usage::

    python -m omr_scanner.tools.make_test_sheet sheet.png

    python -m omr_scanner.tools.make_test_sheet scan.png --rotate 6 --scale 0.9
        --perspective 0.03 --blur 3 --noise 4 --seed 7

    (the second example is one command; the options are wrapped for legibility)

The ground-truth marker and control-point positions are printed, so a rendered
file can be checked against what the alignment engine reports for it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    render_sheet,
)
from omr_scanner.services.alignment_service import save_image

EXIT_OK = 0


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the tool."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.make_test_sheet",
        description="Render a synthetic OMR sheet, optionally distorted.",
    )
    parser.add_argument("output", type=Path, help="Where to write the image.")
    parser.add_argument("--width", type=int, default=None, help="Canonical page width in px.")
    parser.add_argument("--height", type=int, default=None, help="Canonical page height in px.")
    parser.add_argument("--rotate", type=float, default=0.0, help="Clockwise degrees.")
    parser.add_argument("--scale", type=float, default=1.0, help="Uniform scale factor.")
    parser.add_argument(
        "--perspective",
        type=float,
        default=0.0,
        help="Corner displacement as a fraction of the shorter page side.",
    )
    parser.add_argument("--blur", type=int, default=0, help="Gaussian kernel side; odd or 0.")
    parser.add_argument("--noise", type=float, default=0.0, help="Noise sigma in grey levels.")
    parser.add_argument("--brightness", type=float, default=1.0, help="Exposure gain.")
    parser.add_argument("--margin", type=int, default=40, help="Blank border in px.")
    parser.add_argument("--seed", type=int, default=0, help="Seed for every random choice.")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing file.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Render the sheet and report its ground truth.

    Returns:
        A process exit code.
    """
    arguments = build_parser().parse_args(argv)

    defaults = SyntheticSheetSpec()
    sheet = render_sheet(
        SyntheticSheetSpec(
            width=arguments.width if arguments.width is not None else defaults.width,
            height=arguments.height if arguments.height is not None else defaults.height,
        )
    )

    distortion = DistortionSpec(
        rotation_degrees=arguments.rotate,
        scale_x=arguments.scale,
        scale_y=arguments.scale,
        perspective_strength=arguments.perspective,
        margin_px=arguments.margin,
        brightness_gain=arguments.brightness,
        blur_kernel_px=arguments.blur,
        noise_sigma=arguments.noise,
        seed=arguments.seed,
    )
    distorted = apply_distortion(sheet, distortion)

    save_image(distorted.image, arguments.output, overwrite=arguments.overwrite)

    height, width = distorted.image.shape[:2]
    print(f"Wrote {width} x {height} synthetic scan to {arguments.output}")
    print("Marker centres (TL, TR, BR, BL), in scan pixels:")
    for point in distorted.marker_centers:
        print(f"  ({point.x:8.2f}, {point.y:8.2f})")
    print("Control points, canonical -> scan pixels:")
    for canonical, scanned in zip(
        sheet.control_points, distorted.control_points, strict=True
    ):
        print(
            f"  ({canonical.x:7.1f}, {canonical.y:7.1f}) -> "
            f"({scanned.x:8.2f}, {scanned.y:8.2f})"
        )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
