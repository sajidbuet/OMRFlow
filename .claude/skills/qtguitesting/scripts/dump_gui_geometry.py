"""Dump model, item and scene geometry side by side, as JSON.

Purpose:
    Make a scene/local/model inconsistency *obvious* instead of something you
    have to infer from a screenshot. Four numbers that should describe the same
    rectangle are printed next to each other; where they disagree is the bug.

    This is the standard first step for any OMRFlow graphics-item position or
    resize problem. Run it before the operation, perform the operation, run it
    again, and diff.

Usage:
    python .claude/skills/qtguitesting/scripts/dump_gui_geometry.py
    python .claude/skills/qtguitesting/scripts/dump_gui_geometry.py --scenario columns
    python .claude/skills/qtguitesting/scripts/dump_gui_geometry.py --scenario resize

Scenarios:
    markers          the four registration markers and the orientation mark
    question-region  a five-column Question Region over the sample's answer area
    resize           the same region, dumped before *and* after a bottom-right
                     resize, with the invariant checked for you
    columns          the same container at 4, 1, 5 and 2 columns, with the
                     container invariant checked for you

Output:
    Pretty-printed JSON on stdout, and to ``test-output/gui/geometry/<name>.json``
    unless ``--out`` says otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (
    OUTPUT_ROOT,
    SAMPLE_SHEET,
    DesignerHarness,
    add_question_region,
    build_designer,
    ensure_application,
)

GEOMETRY_ROOT = OUTPUT_ROOT / "geometry"


def describe_item(item: Any, zone: Any = None) -> dict[str, Any]:
    """Every frame's opinion of one graphics item's geometry.

    ``pos()`` is the item's origin in the scene; ``rect()`` and ``boundingRect()``
    are item-local (the latter including half the pen width); ``scene_rect()`` and
    ``sceneBoundingRect()`` are in the scene. For a correct `RegionHandleItem`:

    * ``pos`` == ``scene_rect``'s top-left;
    * ``rect`` starts at ``(0, 0)``;
    * ``scene_rect`` == ``model`` (converted to pixels).

    Any other arrangement is the bug.
    """
    return {
        "pos": [item.pos().x(), item.pos().y()],
        "rect": list(item.rect().getRect()),
        "bounding_rect": list(item.boundingRect().getRect()),
        "scene_bounding_rect": list(item.sceneBoundingRect().getRect()),
        "scene_rect": list(item.scene_rect().getRect()),
        "bubble_size": (
            None
            if item.bubble_size is None
            else [item.bubble_size.width(), item.bubble_size.height()]
        ),
        "model_normalised": (
            None
            if zone is None
            else [zone.bounds.x, zone.bounds.y, zone.bounds.width, zone.bounds.height]
        ),
    }


def snapshot(harness: DesignerHarness, *, only: str | None = None) -> dict[str, Any]:
    """Describe every region overlay currently on the canvas, plus the model."""
    page = harness.page
    template = page._designer_state.template
    items = page.canvas._scene.region_items

    regions: dict[str, Any] = {}
    for item_id, item in items.items():
        if only is not None and not item_id.startswith(only):
            continue
        regions[item_id] = describe_item(item, template.zone_by_id(item_id))

    return {
        "image": {"width": harness.image_width, "height": harness.image_height},
        "zoom": page.canvas.zoom,
        "viewport": [
            page.canvas.viewport().width(),
            page.canvas.viewport().height(),
        ],
        "template": {
            "default_bubble_radius": template.default_bubble_radius,
            "default_bubble_radius_px": (
                None
                if template.default_bubble_radius is None
                else template.default_bubble_radius * harness.image_width
            ),
            "zone_count": len(template.zones),
        },
        "regions": regions,
    }


def _outer_rect(snap: dict[str, Any], prefix: str) -> list[float] | None:
    """The union of every region whose id starts with ``prefix``, in scene pixels."""
    rects = [
        entry["scene_rect"]
        for item_id, entry in snap["regions"].items()
        if item_id.startswith(prefix)
    ]
    if not rects:
        return None
    left = min(rect[0] for rect in rects)
    top = min(rect[1] for rect in rects)
    right = max(rect[0] + rect[2] for rect in rects)
    bottom = max(rect[1] + rect[3] for rect in rects)
    return [left, top, right - left, bottom - top]


def _scenario_markers() -> dict[str, Any]:
    harness = build_designer()
    return snapshot(harness)


def _scenario_question_region() -> dict[str, Any]:
    harness = build_designer()
    add_question_region(harness, columns=5)
    snap = snapshot(harness, only="questions")
    snap["outer_rect"] = _outer_rect(snap, "questions")
    return snap


def _scenario_resize() -> dict[str, Any]:
    """Before/after a bottom-right resize, with the anchoring invariant checked."""
    harness = build_designer()
    zones = add_question_region(harness, columns=5)
    page = harness.page
    zone_id = zones[0].id
    page.canvas.select_region(zone_id)

    before = snapshot(harness, only=zone_id)
    rect = page.canvas._scene.region_items[zone_id].scene_rect()
    page._on_canvas_geometry_committed(
        zone_id, "zone", rect.x(), rect.y(), rect.width() * 1.3, rect.height() * 1.15
    )
    harness.process_events()
    after = snapshot(harness, only=zone_id)

    before_rect = before["regions"][zone_id]["scene_rect"]
    after_rect = after["regions"][zone_id]["scene_rect"]
    return {
        "operation": "resize from the bottom-right handle",
        "before": before,
        "after": after,
        "invariant": {
            "description": "the top-left anchor must not move",
            "x_before": before_rect[0],
            "x_after": after_rect[0],
            "y_before": before_rect[1],
            "y_after": after_rect[1],
            "holds": (
                abs(before_rect[0] - after_rect[0]) < 0.5
                and abs(before_rect[1] - after_rect[1]) < 0.5
            ),
        },
    }


def _scenario_columns() -> dict[str, Any]:
    """The container invariant across the brief's 4 -> 1 -> 5 -> 2 sequence."""
    results: dict[str, Any] = {"operation": "change the column count", "steps": {}}
    outer_rects: list[list[float]] = []
    for columns in (4, 1, 5, 2):
        harness = build_designer()
        add_question_region(harness, columns=columns)
        snap = snapshot(harness, only="questions")
        outer = _outer_rect(snap, "questions")
        outer_rects.append(outer or [])
        results["steps"][f"{columns}_columns"] = {
            "outer_rect": outer,
            "zone_count": len(snap["regions"]),
            "strip_widths": [
                entry["scene_rect"][2] for entry in snap["regions"].values()
            ],
        }
    first = outer_rects[0]
    results["invariant"] = {
        "description": "the outer rectangle must be identical at every column count",
        "outer_rects": outer_rects,
        "holds": all(
            all(abs(value - reference) < 0.5 for value, reference in zip(rect, first, strict=True))
            for rect in outer_rects
        ),
    }
    return results


SCENARIOS = {
    "markers": _scenario_markers,
    "question-region": _scenario_question_region,
    "resize": _scenario_resize,
    "columns": _scenario_columns,
}


def main(argv: list[str] | None = None) -> int:
    """Run one scenario and write its geometry dump."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scenario", choices=sorted(SCENARIOS), default="question-region"
    )
    parser.add_argument("--out", type=Path, help="Where to write the JSON.")
    parser.add_argument(
        "--quiet", action="store_true", help="Write the file without printing it."
    )
    args = parser.parse_args(argv)

    if not SAMPLE_SHEET.is_file():
        print(f"Sample sheet not found: {SAMPLE_SHEET}", file=sys.stderr)
        return 2

    ensure_application()
    payload = SCENARIOS[args.scenario]()
    text = json.dumps(payload, indent=2)

    destination = args.out or (GEOMETRY_ROOT / f"{args.scenario}.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")

    if not args.quiet:
        print(text)
    print(f"\nWritten to {destination}", file=sys.stderr)

    invariant = payload.get("invariant")
    if isinstance(invariant, dict) and invariant.get("holds") is False:
        print(f"INVARIANT VIOLATED: {invariant['description']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
