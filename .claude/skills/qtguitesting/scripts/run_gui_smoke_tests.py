"""A fast, non-destructive sanity check on OMRFlow's Qt GUI.

Purpose:
    Answer "is the GUI fundamentally intact?" in a couple of seconds, so a
    developer or an agent can tell a broken import from a failed assertion
    without waiting for, or reading, the full suite.

    Deliberately **not** a second copy of the pytest suite. It checks only the
    things whose failure makes every other test's output meaningless: the
    application initialises, the main window and the Template page construct, the
    sample image decodes and loads, the toolbars and their actions exist, the
    canvas and properties panel exist, and each region dialog can be built.

    Anything behavioural belongs in `tests/`, where it can be asserted properly.

Usage:
    python .claude/skills/qtguitesting/scripts/run_gui_smoke_tests.py
    python .claude/skills/qtguitesting/scripts/run_gui_smoke_tests.py --image other.png

Exit code 0 if every check passed, 1 otherwise. Nothing is written to disk.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (
    SAMPLE_SHEET,
    build_designer,
    build_empty_designer,
    ensure_application,
    question_region_bounds,
)

CheckResult = tuple[bool, str]


def _check_application() -> CheckResult:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    return app is not None, f"QApplication: {type(app).__name__ if app else 'missing'}"


def _check_main_window() -> CheckResult:
    from omr_scanner.gui.main_window import MainWindow

    window = MainWindow()
    pages = window._pages
    return len(pages) > 0, f"MainWindow constructed with {len(pages)} workflow page(s)"


def _check_empty_page() -> CheckResult:
    harness = build_empty_designer()
    page = harness.page
    ok = page._designer_state is None and not page.save_action.isEnabled()
    return ok, "Template page constructs with no document, save disabled"


def _check_sample_loads(image: Path) -> CheckResult:
    harness = build_designer(image)
    size = harness.page.canvas._image_size
    ok = size == (harness.image_width, harness.image_height)
    return ok, f"sample loaded at {size[0]}x{size[1]} px, zoom {harness.page.canvas.zoom:.0%}"


def _check_toolbars(image: Path) -> CheckResult:
    harness = build_designer(image)
    page = harness.page
    row_one = [a for a in page.toolbar.actions() if not a.isSeparator()]
    row_two = [a for a in page.toolbar_view.actions() if not a.isSeparator()]
    ok = len(row_one) >= 4 and len(row_two) >= 4
    return ok, f"toolbar rows carry {len(row_one)} and {len(row_two)} action(s)"


def _check_named_actions(image: Path) -> CheckResult:
    harness = build_designer(image)
    page = harness.page
    required = [
        "new_action", "open_action", "save_action", "save_as_action",
        "undo_action", "redo_action", "detect_action", "confirm_markers_action",
        "detect_orientation_action", "validate_action",
        "add_student_id_action", "add_question_set_action",
        "add_question_block_action", "add_custom_action", "add_ignored_action",
        "fine_tune_action", "clear_overrides_action",
        "zoom_in_action", "zoom_out_action", "fit_action", "actual_size_action",
        "grid_action",
    ]
    on_toolbar = page.toolbar_actions()
    missing = [
        name
        for name in required
        if not hasattr(page, name) or getattr(page, name) not in on_toolbar
    ]
    return not missing, (
        f"{len(required)} named actions present"
        if not missing
        else f"missing from the toolbar: {', '.join(missing)}"
    )


def _check_canvas_and_panels(image: Path) -> CheckResult:
    harness = build_designer(image)
    page = harness.page
    parts = {
        "canvas": page.canvas is not None,
        "properties panel": page.properties is not None,
        "region list": page.region_list is not None,
        "bubble radius box": page.bubble_radius_box is not None,
    }
    missing = [name for name, present in parts.items() if not present]
    return not missing, "canvas, properties panel, region list and radius box exist"


def _check_bubble_radius_reflects_the_document(image: Path) -> CheckResult:
    harness = build_designer(image)
    page = harness.page
    expected = (
        page._designer_state.template.default_bubble_radius * harness.image_width
    )
    shown = page.bubble_radius_box.value()
    ok = abs(shown - expected) < 0.5
    return ok, f"bubble radius shows {shown:.1f} px (document says {expected:.1f} px)"


def _check_dialogs_instantiate(image: Path) -> CheckResult:
    from omr_scanner.gui.template_designer.dialogs import (
        CustomBubbleDialog,
        IgnoredRegionDialog,
        QuestionBlockDialog,
        QuestionSetDialog,
        StudentIdDialog,
    )

    harness = build_designer(image)
    bounds = question_region_bounds()
    built: list[str] = []
    for dialog_cls in (
        StudentIdDialog, QuestionSetDialog, QuestionBlockDialog,
        CustomBubbleDialog, IgnoredRegionDialog,
    ):
        # Constructed, never `exec()`d - a modal has nothing to click offscreen
        # and would block forever (`docs/TESTING.md`).
        dialog_cls(
            bounds=bounds,
            existing_zone_ids=[],
            image_width=harness.image_width,
            image_height=harness.image_height,
        )
        built.append(dialog_cls.__name__)
    return True, f"{len(built)} region dialogs construct"


def _check_orientation_detection(image: Path) -> CheckResult:
    """The detector runs end to end and reports full-image coordinates."""
    from omr_scanner.services import detect_orientation_marker_in_region

    outcome = detect_orientation_marker_in_region(
        image, x=110.0, y=255.0, width=250.0, height=175.0
    )
    if not outcome.found:
        return False, f"orientation mark not found: {outcome.reason}"
    inside = (
        110.0 <= outcome.center_x <= 360.0 and 255.0 <= outcome.center_y <= 430.0
    )
    return inside, (
        f"orientation mark at ({outcome.center_x:.0f}, {outcome.center_y:.0f}) px, "
        f"score {outcome.score:.2f}"
    )


def _check_page_header_is_compact(image: Path) -> CheckResult:
    harness = build_designer(image)
    ok = harness.page.summary_widget is None
    return ok, "Template header spends no row on the summary sentence"


def main(argv: list[str] | None = None) -> int:
    """Run every check, print a report, and return a shell exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", type=Path, default=SAMPLE_SHEET)
    args = parser.parse_args(argv)

    if not args.image.is_file():
        print(f"Reference image not found: {args.image}", file=sys.stderr)
        return 2

    ensure_application()

    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("application initialises", _check_application),
        ("main window constructs", _check_main_window),
        ("template page constructs empty", _check_empty_page),
        ("sample image loads", lambda: _check_sample_loads(args.image)),
        ("both toolbar rows exist", lambda: _check_toolbars(args.image)),
        ("every named action is on a toolbar", lambda: _check_named_actions(args.image)),
        ("canvas and panels exist", lambda: _check_canvas_and_panels(args.image)),
        (
            "bubble radius reflects the document",
            lambda: _check_bubble_radius_reflects_the_document(args.image),
        ),
        ("region dialogs instantiate", lambda: _check_dialogs_instantiate(args.image)),
        ("orientation detection runs", lambda: _check_orientation_detection(args.image)),
        ("page header is compact", lambda: _check_page_header_is_compact(args.image)),
    ]

    failures = 0
    for name, check in checks:
        try:
            ok, detail = check()
        except Exception:
            # The traceback is the whole value of a smoke test failure; printing
            # it here is what saves a second run with more instrumentation.
            print(f"[ERROR] {name}")
            traceback.print_exc()
            failures += 1
            continue
        marker = "  ok  " if ok else "FAILED"
        print(f"[{marker}] {name}: {detail}")
        failures += 0 if ok else 1

    total = len(checks)
    print(f"\n{total - failures}/{total} checks passed")
    if failures:
        print("Run the full suite for detail: pytest -m gui", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
