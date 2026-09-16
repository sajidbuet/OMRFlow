"""Capture deterministic screenshots of OMRFlow GUI states, using Qt itself.

Purpose:
    Produce the evidence a human or an agent looks at when a layout, an overlay
    or a rendered size needs checking - the things program state cannot show.

    Qt-native (`QWidget.grab()`), never an operating-system screenshot tool: no
    window has to be visible, focused, or on top, so this works headless and on a
    developer's desktop alike, and cannot capture somebody's other windows.

Usage:
    python .claude/skills/qtguitesting/scripts/capture_gui_states.py
    python .claude/skills/qtguitesting/scripts/capture_gui_states.py --only bubble
    python .claude/skills/qtguitesting/scripts/capture_gui_states.py --image other.png

Output:
    ``test-output/gui/*.png`` (git-ignored). These are *diagnostic evidence*, not
    baselines: fonts, anti-aliasing, Qt platform styles and DPI differ between
    machines, so no ordinary test may fail because two of them differ. Compare
    with ``compare_gui_images.py``, which uses tolerances.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (
    OUTPUT_ROOT,
    SAMPLE_SHEET,
    DesignerHarness,
    add_question_region,
    build_designer,
    build_empty_designer,
    ensure_application,
    orientation_search_rect,
)


def _save(widget: object, name: str) -> Path:
    """Grab ``widget`` into ``test-output/gui/<name>.png``."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT_ROOT / f"{name}.png"
    widget.grab().save(str(destination))
    return destination


def _capture_empty(_image: Path) -> list[Path]:
    """The Template page before anything is loaded - the compact header."""
    harness = build_empty_designer()
    return [_save(harness.page, "template_empty")]


def _capture_loaded(image: Path) -> list[Path]:
    """The sample sheet loaded and fitted, with its markers drawn."""
    harness = build_designer(image)
    harness.page.canvas.fit_to_window()
    harness.process_events()
    return [_save(harness.page, "template_ece0000_loaded")]


def _capture_question_region(image: Path) -> list[Path]:
    """The container invariant, at 1, 5 and the default 4 columns."""
    written: list[Path] = []
    for columns, name in (
        (5, "template_ece0000_question_region"),
        (1, "template_ece0000_question_1column"),
        (5, "template_ece0000_question_5columns"),
    ):
        harness = build_designer(image)
        add_question_region(harness, columns=columns)
        harness.page.canvas.fit_to_window()
        harness.process_events()
        written.append(_save(harness.page, name))
    return written


def _capture_bubble_radius(image: Path) -> list[Path]:
    """Two clearly different radii, for side-by-side comparison."""
    written: list[Path] = []
    for radius, name in (
        (4.0, "template_ece0000_bubble_radius_small"),
        (16.0, "template_ece0000_bubble_radius_large"),
    ):
        harness = build_designer(image)
        add_question_region(harness, columns=5)
        harness.page.bubble_radius_box.setValue(radius)
        harness.process_events()
        # Zoomed in on one column, or the difference is invisible at fit scale.
        _focus_on_first_column(harness)
        written.append(_save(harness.page, name))
    return written


def _focus_on_first_column(harness: DesignerHarness) -> None:
    """Zoom to the first question column so bubble size is actually legible."""
    zones = harness.page._designer_state.template.zones
    if not zones:
        return
    canvas = harness.page.canvas
    item = canvas._scene.region_items.get(zones[0].id)
    if item is None:
        return
    canvas.zoom_to_actual_size()
    canvas.centerOn(item.scene_rect().center())
    harness.process_events()


def _capture_selection_and_resize(image: Path) -> list[Path]:
    """A selected region, then the same region after a bottom-right resize.

    The two images must show the region's **top-left corner in the same place**;
    that is the regression these screenshots exist to make visible.
    """
    harness = build_designer(image)
    zones = add_question_region(harness, columns=5)
    page = harness.page
    zone_id = zones[0].id
    page.canvas.select_region(zone_id)
    page.canvas.fit_to_window()
    harness.process_events()
    written = [_save(page, "template_ece0000_region_selected")]
    written.append(_save(page, "template_ece0000_region_before_resize"))

    rect = page.canvas._scene.region_items[zone_id].scene_rect()
    page._on_canvas_geometry_committed(
        zone_id, "zone", rect.x(), rect.y(), rect.width() * 1.3, rect.height() * 1.15
    )
    page.canvas.select_region(zone_id)
    harness.process_events()
    written.append(_save(page, "template_ece0000_region_after_resize"))
    return written


def _capture_orientation(image: Path) -> list[Path]:
    """The orientation search rectangle, then the detected mark."""
    from omr_scanner.domain.geometry import NormalizedPoint, NormalizedSize
    from omr_scanner.domain.template import OrientationMarker
    from omr_scanner.gui.template_designer.state import MANUAL_CONFIRMED

    harness = build_designer(image)
    page = harness.page
    x, y, width, height = orientation_search_rect()

    # Put the orientation region where a user would have dragged it, then let
    # the real toolbar action search inside it.
    existing = page._designer_state.template.orientation_marker
    page._designer_state.set_orientation_marker(
        OrientationMarker(
            center=NormalizedPoint(
                x=(x + width / 2) / harness.image_width,
                y=(y + height / 2) / harness.image_height,
            ),
            size=NormalizedSize(
                width=width / harness.image_width, height=height / harness.image_height
            ),
            expected_near=existing.expected_near,
        ),
        status=MANUAL_CONFIRMED,
    )
    page._refresh_all(keep_selection="orientation")
    _zoom_to_top_left(harness)
    written = [_save(page, "template_ece0000_orientation_roi")]

    page.detect_orientation_marker()
    _zoom_to_top_left(harness)
    written.append(_save(page, "template_ece0000_orientation_detected"))

    marker = page._designer_state.template.orientation_marker
    print(
        "  orientation marker now at "
        f"({marker.center.x * harness.image_width:.0f}, "
        f"{marker.center.y * harness.image_height:.0f}) px, "
        f"{marker.size.width * harness.image_width:.0f}x"
        f"{marker.size.height * harness.image_height:.0f} px"
    )
    return written


def _zoom_to_top_left(harness: DesignerHarness) -> None:
    """Show the sheet's top-left corner at 1:1, where the orientation mark is."""
    from PySide6.QtCore import QPointF

    canvas = harness.page.canvas
    canvas.zoom_to_actual_size()
    canvas.centerOn(QPointF(250.0, 320.0))
    harness.process_events()


SCENARIOS: dict[str, Callable[[Path], list[Path]]] = {
    "empty": _capture_empty,
    "loaded": _capture_loaded,
    "question": _capture_question_region,
    "bubble": _capture_bubble_radius,
    "resize": _capture_selection_and_resize,
    "orientation": _capture_orientation,
}


def main(argv: list[str] | None = None) -> int:
    """Capture the requested scenarios and report where each file landed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only",
        choices=sorted(SCENARIOS),
        action="append",
        help="Capture only this scenario (repeatable). Default: all of them.",
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=SAMPLE_SHEET,
        help="Reference sheet to load (default: examples/ECE-0000.png).",
    )
    args = parser.parse_args(argv)

    if not args.image.is_file():
        print(f"Reference image not found: {args.image}", file=sys.stderr)
        return 2

    ensure_application()
    selected = args.only or sorted(SCENARIOS)
    written: list[Path] = []
    failed: list[str] = []

    for name in selected:
        print(f"[{name}]")
        try:
            written.extend(SCENARIOS[name](args.image))
        except Exception as exc:
            # Preserving which scenario failed and why is the point; one broken
            # capture must not cost the evidence from every other one.
            failed.append(f"{name}: {exc!r}")
            print(f"  FAILED: {exc!r}", file=sys.stderr)

    print(f"\n{len(written)} screenshot(s) written to {OUTPUT_ROOT}:")
    for path in written:
        print(f"  {path.name}")
    if failed:
        print(f"\n{len(failed)} scenario(s) failed:", file=sys.stderr)
        for note in failed:
            print(f"  {note}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
