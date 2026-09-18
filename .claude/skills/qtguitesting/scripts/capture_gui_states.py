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
    SAMPLE_TEMPLATE,
    DesignerHarness,
    add_question_region,
    build_designer,
    build_empty_designer,
    build_scan_page,
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


def _duplicate_scan_copies(image: Path, count: int) -> list[Path]:
    """Byte-identical copies of ``image``, which therefore recognise alike.

    The point of copying rather than rendering variants: two sheets that read to
    the *same* roll number is exactly the case the duplicate-naming rule exists
    for, and identical bytes guarantee it without depending on the recogniser.
    """
    import shutil

    folder = OUTPUT_ROOT / "scan_inputs"
    folder.mkdir(parents=True, exist_ok=True)
    copies: list[Path] = []
    for index in range(count):
        destination = folder / f"IMG_{index + 1:03d}{image.suffix}"
        shutil.copyfile(image, destination)
        copies.append(destination)
    return copies


def _capture_scan_empty(_image: Path) -> list[Path]:
    """The Scan page before a template is loaded - the disabled state."""
    from _harness import WINDOW_HEIGHT, WINDOW_WIDTH, ScanHarness

    from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
    from omr_scanner.gui.scan.page import ScanPage

    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    page.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
    harness = ScanHarness(page=page, template_path=SAMPLE_TEMPLATE, output_dir=OUTPUT_ROOT)
    harness.settle()
    written = [_save(page, "scan_empty")]
    harness.shutdown()
    return written


def _capture_scan_template_loaded(image: Path) -> list[Path]:
    """A template loaded and the sample imported, before any processing."""
    harness = build_scan_page([image])
    harness.process_events()
    written = [_save(harness.page, "scan_template_loaded")]
    harness.shutdown()
    return written


def _capture_scan_processed(image: Path) -> list[Path]:
    """The sample recognised: results panel filled, overlay drawn, then zoomed.

    Three images, because they answer different questions: whether the values
    reached the right-hand panel, whether the overlay lands on the printed
    bubbles, and whether it still lands on them at 1:1.
    """
    harness = build_scan_page([image])
    harness.run_batch()
    harness.page.select_scan(0)
    if not harness.await_preview():
        raise RuntimeError("the preview never finished rendering")

    harness.page.preview.fit_to_window()
    harness.process_events()
    written = [_save(harness.page, "scan_processed")]

    # Zoomed onto the answer area: at fit scale an overlay that is a few pixels
    # out looks perfect, which is precisely the regression worth seeing. The
    # view must also be *centred on bubbles* - zooming in place lands on
    # whatever happened to be in the middle of the page, usually the rubric.
    harness.page.preview.zoom_to_actual_size()
    _centre_on_first_question_zone(harness)
    written.append(_save(harness.page, "scan_overlay_zoom"))

    # And the same view with every measured bubble outlined, not just the
    # selected ones - the check that the grid maps where the template says.
    harness.page.empty_checkbox.setChecked(True)
    harness.process_events()
    written.append(_save(harness.page, "scan_overlay_all_bubbles"))
    harness.shutdown()
    return written


def _centre_on_first_question_zone(harness: object) -> None:
    """Centre the preview on the first question block, in canonical pixels.

    The preview's scene is the canonical page, so a zone's normalised bounds
    scale straight onto it - no preview-scale correction, which is exactly why
    ``set_page`` rescales the pixmap rather than the overlay.
    """
    from PySide6.QtCore import QPointF

    from omr_scanner.domain.template import QuestionBlockFieldDefinition

    page = harness.page
    template = page.state.template
    zone = next(
        (
            item
            for item in template.zones
            if isinstance(item.field, QuestionBlockFieldDefinition)
        ),
        None,
    )
    if zone is None:
        return
    bounds = zone.bounds
    page.preview.centerOn(
        QPointF(
            (bounds.x + bounds.width / 2) * template.page.canonical_width_px,
            (bounds.y + bounds.height / 2) * template.page.canonical_height_px,
        )
    )
    harness.process_events()


def _capture_scan_duplicates(image: Path) -> list[Path]:
    """Three identical sheets renamed, so the _a/_b suffixes are visible."""
    output = OUTPUT_ROOT / "scan_duplicate_output"
    if output.is_dir():
        # A previous run's files would otherwise push this run to _c, _d, _e -
        # correct behaviour, but it would not show the sequence being captured.
        for item in output.iterdir():
            item.unlink()

    harness = build_scan_page(
        _duplicate_scan_copies(image, 3), output_dir=output, rename=True
    )
    harness.run_batch()
    harness.page.select_scan(0)
    # Without this the grab catches "Rendering preview..." and an empty canvas:
    # the batch discards previews, so selecting a row starts a worker that has
    # not finished by the time processEvents() returns.
    if not harness.await_preview():
        raise RuntimeError("the preview never finished rendering")
    harness.page.preview.fit_to_window()
    harness.process_events()

    names = sorted(item.name for item in output.iterdir())
    print(f"  output folder now holds: {', '.join(names)}")
    written = [_save(harness.page, "scan_duplicate_rolls")]
    harness.shutdown()
    return written


def _capture_scan_exported(image: Path) -> list[Path]:
    """The page after a CSV export, showing the confirmation in the status line."""
    harness = build_scan_page([image])
    harness.run_batch()
    # Finishing a batch re-selects the current row, which starts a preview
    # worker; let it land so the capture shows the sheet rather than a
    # half-rendered view (see _capture_scan_duplicates).
    harness.await_preview()
    destination = harness.page.export_csv_to(OUTPUT_ROOT / "scan_results.csv")
    harness.process_events()
    print(f"  CSV written to {destination}")
    written = [_save(harness.page, "scan_exported")]
    harness.shutdown()
    return written


def _capture_scan_progress(image: Path) -> list[Path]:
    """The progress panel mid-batch and at completion, at ten-thousand scale.

    Rendered from constructed snapshots rather than by processing ten thousand
    sheets: the panel is a pure function of a snapshot, and the point of the
    capture is to read the layout - do the numbers fit the column, is the
    remaining time legible, is "Failed 13" visible without relying on colour.
    """
    from omr_scanner.services.batch_progress import BatchState, ProgressSnapshot

    harness = build_scan_page([image])
    page = harness.page

    written: list[Path] = []
    mid_batch = ProgressSnapshot(
        state=BatchState.PROCESSING,
        total=10_000,
        successful=6_301,
        warnings=28,
        failed=13,
        elapsed_seconds=1_122.0,
        rate=5.65,
        eta_seconds=647.0,
        finish_wall_clock=None,
        workers=12,
    )
    page._render_progress(mid_batch)
    harness.process_events()
    print(
        f"  mid-batch: {page.progress_counts_label.text()} | "
        f"{page.progress_bar.format()} | {page.progress_timing_label.text()}"
    )
    written.append(_save(page, "scan_progress_large_batch"))

    page._render_progress(
        ProgressSnapshot(
            state=BatchState.CANCELLING,
            total=10_000,
            successful=6_301,
            warnings=28,
            failed=13,
            elapsed_seconds=1_122.0,
            rate=5.65,
            workers=12,
        )
    )
    harness.process_events()
    written.append(_save(page, "scan_progress_cancelling"))

    harness.shutdown()
    return written


def _capture_settings(_image: Path) -> list[Path]:
    """The Processing settings in each of its three modes.

    Three images, because the difference between them is exactly what a reader
    needs to check: whether the worker selector is enabled, and whether the
    "workers this setting uses" line agrees with the mode.
    """
    from omr_scanner.config import AppConfig
    from omr_scanner.config.processing import detected_cpu_count
    from omr_scanner.gui.settings_dialog import SettingsDialog

    written: list[Path] = []
    for label, name in (
        ("Automatic", "settings_processing_automatic"),
        ("Single core", "settings_processing_single_core"),
        ("Custom", "settings_processing_custom"),
    ):
        # The real dialog on the real machine: the CPU count shown is this
        # computer's, which is the number a reader is checking.
        dialog = SettingsDialog(AppConfig(), cpu_count=detected_cpu_count())
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findText(label))
        if label == "Custom":
            dialog.worker_spin.setValue(min(4, dialog.worker_spin.maximum()))
        dialog.adjustSize()
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        print(
            f"  {label}: {dialog.active_label.text()} worker(s), "
            f"selector {'enabled' if dialog.worker_spin.isEnabled() else 'disabled'}"
        )
        written.append(_save(dialog, name))
    return written


def _capture_scan_multicore(image: Path) -> list[Path]:
    """Four copies of the sample processed on four workers, then on one.

    What the two images are for: the progress line should report completions and
    the worker count, and the scan list should come out in the same order with
    the same values either way.
    """
    from omr_scanner.config.processing import ProcessingMode, ProcessingSettings

    written: list[Path] = []
    scans = _duplicate_scan_copies(image, 4)
    for processing, name in (
        (ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=4), "scan_multicore_four"),
        (ProcessingSettings(mode=ProcessingMode.SINGLE_CORE), "scan_multicore_single"),
    ):
        harness = build_scan_page(scans, processing=processing)
        report = harness.run_batch()
        harness.page.select_scan(0)
        harness.await_preview()
        harness.page.preview.fit_to_window()
        harness.process_events()
        print(
            f"  {report.worker_count} worker(s): {report.total} scans in "
            f"{report.elapsed_seconds:.2f}s - "
            f"{[item.result.identifier_value for item in report.processed]}"
        )
        written.append(_save(harness.page, name))
        harness.shutdown()
    return written


SCENARIOS: dict[str, Callable[[Path], list[Path]]] = {
    "empty": _capture_empty,
    "loaded": _capture_loaded,
    "question": _capture_question_region,
    "bubble": _capture_bubble_radius,
    "resize": _capture_selection_and_resize,
    "orientation": _capture_orientation,
    "scan-empty": _capture_scan_empty,
    "scan-template": _capture_scan_template_loaded,
    "scan-processed": _capture_scan_processed,
    "scan-duplicates": _capture_scan_duplicates,
    "scan-export": _capture_scan_exported,
    "scan-multicore": _capture_scan_multicore,
    "scan-progress": _capture_scan_progress,
    "settings": _capture_settings,
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
