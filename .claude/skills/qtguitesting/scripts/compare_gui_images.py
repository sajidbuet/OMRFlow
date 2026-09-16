"""Compare two GUI screenshots with tolerances, not byte equality.

Purpose:
    Tell a *real* layout regression from the noise two machines always produce.
    Fonts, anti-aliasing, Qt platform styles, DPI and even GPU text rendering
    differ, so two captures of an identical GUI are almost never byte-identical -
    and a comparison that demands they are reports a failure every single run and
    is therefore ignored.

    This reports magnitudes instead: how far apart the images are on average, what
    share of pixels changed meaningfully, and where. A moved panel shows up as a
    large changed-pixel percentage in a compact region; a font difference shows up
    as a tiny mean difference spread everywhere.

Usage:
    python .claude/skills/qtguitesting/scripts/compare_gui_images.py before.png after.png
    python .claude/skills/qtguitesting/scripts/compare_gui_images.py a.png b.png --threshold 12
    python .claude/skills/qtguitesting/scripts/compare_gui_images.py a.png b.png --diff out.png

Dependencies:
    OpenCV and NumPy only - both already runtime dependencies of OMRFlow. No new
    package is added for a diagnostic script.

Exit codes:
    0  the images are equivalent within tolerance
    1  they differ materially
    2  they could not be compared (missing file, mismatched dimensions)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

DEFAULT_PIXEL_THRESHOLD = 16
"""Per-channel difference, in grey levels, above which a pixel counts as
*changed*. Sub-pixel text positioning and anti-aliasing routinely move an edge
pixel by ten or so levels; a genuine layout change moves whole regions much
further."""

DEFAULT_CHANGED_FRACTION = 0.01
"""Share of changed pixels above which the images are reported as materially
different. One per cent of a 1600x1000 capture is 16,000 pixels - far more than
re-rendered text, far less than a moved panel."""


def compare(
    first: Path,
    second: Path,
    *,
    pixel_threshold: int = DEFAULT_PIXEL_THRESHOLD,
    changed_fraction: float = DEFAULT_CHANGED_FRACTION,
    diff_path: Path | None = None,
) -> dict[str, object]:
    """Compare two images and return a metrics dictionary.

    Args:
        first: Baseline image.
        second: Image to compare against it.
        pixel_threshold: Per-channel grey-level difference counting as changed.
        changed_fraction: Changed-pixel share above which the result is "differs".
        diff_path: When given, write a heat-map of the difference there.

    Returns:
        A dictionary with ``dimensions_equal``, ``mean_difference``,
        ``max_difference``, ``changed_pixel_fraction``, ``difference_bounds``
        (the bounding box containing every changed pixel, or ``None``) and
        ``equivalent``.

    Raises:
        FileNotFoundError: Either image could not be read.
    """
    left = cv2.imread(str(first), cv2.IMREAD_COLOR)
    right = cv2.imread(str(second), cv2.IMREAD_COLOR)
    if left is None:
        raise FileNotFoundError(f"Could not read {first}")
    if right is None:
        raise FileNotFoundError(f"Could not read {second}")

    if left.shape != right.shape:
        # A size change is itself the finding, and is reported rather than
        # papered over by rescaling - two differently sized captures mean the
        # window or the DPI changed, which is exactly what you want to know.
        return {
            "dimensions_equal": False,
            "first_shape": list(left.shape[:2]),
            "second_shape": list(right.shape[:2]),
            "equivalent": False,
            "note": "Images have different dimensions; nothing else was compared.",
        }

    difference = cv2.absdiff(left, right)
    per_pixel = difference.max(axis=2)
    changed = per_pixel > pixel_threshold
    changed_count = int(changed.sum())
    total = int(per_pixel.size)
    fraction = changed_count / total if total else 0.0

    bounds: list[int] | None = None
    if changed_count:
        rows = np.flatnonzero(changed.any(axis=1))
        columns = np.flatnonzero(changed.any(axis=0))
        bounds = [
            int(columns[0]),
            int(rows[0]),
            int(columns[-1] - columns[0] + 1),
            int(rows[-1] - rows[0] + 1),
        ]

    if diff_path is not None:
        heat = cv2.applyColorMap(
            cv2.convertScaleAbs(per_pixel, alpha=255.0 / max(1, per_pixel.max())),
            cv2.COLORMAP_INFERNO,
        )
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(diff_path), heat)

    return {
        "dimensions_equal": True,
        "shape": list(left.shape[:2]),
        "mean_difference": round(float(difference.mean()), 4),
        "max_difference": int(difference.max()),
        "changed_pixel_count": changed_count,
        "changed_pixel_fraction": round(fraction, 6),
        "difference_bounds": bounds,
        "pixel_threshold": pixel_threshold,
        "changed_fraction_threshold": changed_fraction,
        "equivalent": fraction <= changed_fraction,
        "diff_image": None if diff_path is None else str(diff_path),
    }


def main(argv: list[str] | None = None) -> int:
    """Compare two images and print the metrics as JSON."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_PIXEL_THRESHOLD,
        help="Per-channel grey-level difference counting as a changed pixel.",
    )
    parser.add_argument(
        "--changed-fraction",
        type=float,
        default=DEFAULT_CHANGED_FRACTION,
        help="Changed-pixel share above which the images are reported as differing.",
    )
    parser.add_argument(
        "--diff", type=Path, help="Write a difference heat-map to this path."
    )
    args = parser.parse_args(argv)

    try:
        metrics = compare(
            args.first,
            args.second,
            pixel_threshold=args.threshold,
            changed_fraction=args.changed_fraction,
            diff_path=args.diff,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(metrics, indent=2))
    if not metrics.get("dimensions_equal", False):
        return 2
    return 0 if metrics["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
