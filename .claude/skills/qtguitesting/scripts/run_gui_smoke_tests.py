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
    SAMPLE_TEMPLATE,
    build_designer,
    build_empty_designer,
    build_scan_page,
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


def _check_scan_page_constructs() -> CheckResult:
    """The Scan page builds, and nothing that needs a template is enabled yet."""
    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.scan.page import ScanPage

    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    premature = [
        name
        for name, button in (
            ("Add Scan(s)", page.add_scans_button),
            ("Add Folder", page.add_folder_button),
            ("Process All", page.process_all_button),
            ("Export CSV", page.export_csv_button),
        )
        if button.isEnabled()
    ]
    return not premature, (
        "Scan page constructs with everything template-dependent disabled"
        if not premature
        else f"enabled before a template was loaded: {', '.join(premature)}"
    )


def _check_scan_object_names() -> CheckResult:
    """Every stable selector the scenario reference lists is present."""
    from PySide6.QtWidgets import QCheckBox, QProgressBar, QPushButton, QTableWidget

    from omr_scanner.gui.scan.preview import ScanPreviewView

    harness = build_scan_page()
    required = [
        ("loadTemplateButton", QPushButton),
        ("addScansButton", QPushButton),
        ("addFolderButton", QPushButton),
        ("clearScansButton", QPushButton),
        ("processAllButton", QPushButton),
        ("processSelectedButton", QPushButton),
        ("reprocessButton", QPushButton),
        ("cancelButton", QPushButton),
        ("outputFolderButton", QPushButton),
        ("exportCsvButton", QPushButton),
        ("renameScansCheckBox", QCheckBox),
        ("scanTable", QTableWidget),
        ("resultFieldsTable", QTableWidget),
        ("resultAnswersTable", QTableWidget),
        ("scanPreview", ScanPreviewView),
        ("progressBar", QProgressBar),
    ]
    missing = [
        name for name, widget_type in required if harness.page.findChild(widget_type, name) is None
    ]
    harness.shutdown()
    return not missing, (
        f"{len(required)} stable objectNames present"
        if not missing
        else f"missing objectName(s): {', '.join(missing)}"
    )


def _check_scan_template_and_import(image: Path) -> CheckResult:
    """The real template loads and the real sample reaches the scan list."""
    harness = build_scan_page([image])
    page = harness.page
    ok = (
        page.state.template is not None
        and page.scan_table.rowCount() == 1
        and page.process_all_button.isEnabled()
    )
    harness.shutdown()
    return ok, (
        f"template '{page.state.template.name}' loaded, "
        f"{page.scan_table.rowCount()} scan(s) listed, Process All enabled"
    )


def _check_scan_recognises_the_real_sample(image: Path) -> CheckResult:
    """The whole pipeline, end to end, on an actual scan of an actual sheet."""
    harness = build_scan_page([image])
    report = harness.run_batch()
    result = harness.page.state.entries[0].processed.result
    ok = (
        report.total == 1
        and result.registration.value != "failed"
        and bool(result.identifier_value)
        and len(result.answers) > 0
    )
    harness.shutdown()
    return ok, (
        f"roll '{result.identifier_value}', set '{result.set_code_value}', "
        f"{len(result.answers)} answer(s), registration {result.registration.value}"
    )


def _check_scan_preview_renders(image: Path) -> CheckResult:
    """Selecting a processed row produces a rectified page to look at."""
    harness = build_scan_page([image])
    harness.run_batch()
    harness.page.select_scan(0)
    if not harness.await_preview():
        harness.shutdown()
        return False, "the preview never finished rendering"
    detail = (
        f"preview rendered, zoom {harness.page.preview.zoom:.0%}, "
        f"status '{harness.page.preview_status_label.text()}'"
    )
    ok = harness.page.preview.has_page
    harness.shutdown()
    return ok, detail


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
        ("scan page constructs", _check_scan_page_constructs),
    ]

    # The Scan checks drive the real template that describes the real sample.
    # Without it there is nothing honest to check, so they are skipped aloud
    # rather than quietly passing on a substitute.
    if SAMPLE_TEMPLATE.is_file():
        checks.extend(
            [
                ("scan page object names", _check_scan_object_names),
                (
                    "scan template loads and imports",
                    lambda: _check_scan_template_and_import(args.image),
                ),
                (
                    "scan recognises the real sample",
                    lambda: _check_scan_recognises_the_real_sample(args.image),
                ),
                ("scan preview renders", lambda: _check_scan_preview_renders(args.image)),
            ]
        )
    else:
        print(f"[ skip ] scan checks: {SAMPLE_TEMPLATE} is not present", file=sys.stderr)

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
