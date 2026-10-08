"""Tests for the global interface zoom, live in the running shell.

Scope:
    ===== ==============================================================
    Test  Behaviour
    ===== ==============================================================
    A     The commands: View menu, chrome buttons, steps, limits, states.
    B     What grows: font, stylesheet, chrome, ribbon, footer, widgets.
    C     What does not change: page, project, files, sheet/image zoom.
    D     Dialogs opened after a change, and startup restoring the zoom.
    E     Fitting: nothing clipped, nothing wider than a small display.
    ===== ==============================================================

Why these build a configured application:
    The application stylesheet is installed by
    :func:`~omr_scanner.gui.application.configure_application`, exactly as
    the real start-up does, because several of the behaviours under test -
    padding, minimum heights, dialogs inheriting the zoom - live in it.
    ``tests/gui/conftest.py`` returns the zoom to 100% after every test, so
    nothing here leaks into the rest of the suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from omr_scanner.config import AppConfig, load_app_config
from omr_scanner.gui.about_dialog import AboutDialog
from omr_scanner.gui.application import configure_application
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.gui.settings_dialog import SettingsDialog
from omr_scanner.gui.theme import (
    Chrome,
    Density,
    FontSize,
    IconSize,
    Navigator,
    UiScale,
    application_stylesheet,
)
from omr_scanner.gui.ui_scale import UiScaleManager, current_scale
from omr_scanner.gui.widgets import Card, EmptyState, PageHeader
from omr_scanner.gui.widgets.status_chips import StatusChip
from omr_scanner.gui.widgets.workflow_ribbon import RibbonMode

pytestmark = pytest.mark.gui

DISPLAY = (1366, 768)


def _settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


@pytest.fixture
def manager(qtbot) -> UiScaleManager:
    """The application's zoom manager, with the real stylesheet installed."""
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    configure_application(app)
    found = UiScaleManager.find()
    assert found is not None
    assert found.percent == 100
    return found


def _destroy(widget: QWidget) -> None:
    """Close and really delete ``widget``.

    pytest-qt calls ``deleteLater()``, but no event loop ever runs between
    tests, so without this every window a zoom test built would stay alive -
    and every later zoom change would re-polish all of them too.
    """
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def window(qtbot, tmp_path: Path, manager: UiScaleManager) -> Iterator[MainWindow]:
    main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    main_window.resize(*DISPLAY)
    main_window.show()
    _settle()
    yield main_window
    if main_window.session is not None:
        main_window.close_project()
    manager.set_percent(100)
    _destroy(main_window)


def _baseline_points(manager: UiScaleManager) -> float:
    return manager.baseline_font.pointSizeF()


# ----------------------------------------------------------------------
# A - the commands
# ----------------------------------------------------------------------
class TestACommands:
    def test_the_view_menu_sits_between_file_and_tools(self, window: MainWindow):
        titles = [
            action.menu().title()
            for action in window.application_menu.actions()
            if action.menu() is not None
        ]
        assert titles == ["&File", "&View", "&Tools", "&Help"]

    def test_the_view_menu_holds_exactly_the_three_zoom_commands(self, window: MainWindow):
        view = next(
            action.menu()
            for action in window.application_menu.actions()
            if action.menu() is not None and action.menu().title() == "&View"
        )
        texts = [action.text() for action in view.actions() if action.text()]
        assert texts[:3] == ["Zoom +", "Zoom -", "Zoom 100%"]
        zoom_texts = [text for text in texts if text.startswith("Zoom")]
        assert zoom_texts == ["Zoom +", "Zoom -", "Zoom 100%"]

    def test_the_zoom_commands_carry_no_keyboard_shortcut(self, window: MainWindow):
        """Ctrl++, Ctrl+- and Ctrl+0 belong to the sheet canvas."""
        for action in (window.zoom_in_action, window.zoom_out_action, window.zoom_reset_action):
            assert action.shortcut().isEmpty(), action.text()

    def test_no_window_action_claims_the_canvas_zoom_keys(self, window: MainWindow):
        canvas_keys = {
            QKeySequence(QKeySequence.StandardKey.ZoomIn).toString(),
            QKeySequence(QKeySequence.StandardKey.ZoomOut).toString(),
            QKeySequence("Ctrl+0").toString(),
        }
        for action in window.actions():
            for sequence in action.shortcuts():
                assert sequence.toString() not in canvas_keys, action.text()

    def test_plus_and_minus_move_exactly_one_step(self, window: MainWindow):
        window.zoom_in_action.trigger()
        assert window.interface_zoom == 110
        window.zoom_in_action.trigger()
        assert window.interface_zoom == 120
        window.zoom_out_action.trigger()
        assert window.interface_zoom == 110

    def test_reset_returns_exactly_to_100(self, window: MainWindow):
        window.set_interface_zoom(170)
        window.zoom_reset_action.trigger()
        assert window.interface_zoom == 100
        assert current_scale() == UiScale.of(100)

    def test_the_chrome_buttons_run_the_view_actions(self, window: MainWindow):
        chrome = window.chrome
        assert chrome.zoom_in_button.defaultAction() is window.zoom_in_action
        assert chrome.zoom_out_button.defaultAction() is window.zoom_out_action
        triggered: list[str] = []
        window.zoom_in_action.triggered.connect(lambda: triggered.append("in"))
        window.zoom_out_action.triggered.connect(lambda: triggered.append("out"))
        QTest.mouseClick(chrome.zoom_in_button, Qt.MouseButton.LeftButton)
        QTest.mouseClick(chrome.zoom_out_button, Qt.MouseButton.LeftButton)
        assert triggered == ["in", "out"]
        assert window.interface_zoom == 100

    def test_the_limits_disable_the_matching_command_and_button(self, window: MainWindow):
        window.set_interface_zoom(200)
        assert not window.zoom_in_action.isEnabled()
        assert not window.chrome.zoom_in_button.isEnabled()
        assert window.zoom_out_action.isEnabled() and window.chrome.zoom_out_button.isEnabled()
        assert window.zoom_in_interface() is False
        assert window.interface_zoom == 200

        window.set_interface_zoom(80)
        assert not window.zoom_out_action.isEnabled()
        assert not window.chrome.zoom_out_button.isEnabled()
        assert window.zoom_in_action.isEnabled() and window.chrome.zoom_in_button.isEnabled()
        assert window.zoom_out_interface() is False
        assert window.interface_zoom == 80

    def test_zoom_100_is_disabled_only_at_100(self, window: MainWindow):
        assert not window.zoom_reset_action.isEnabled()
        window.zoom_in_action.trigger()
        assert window.zoom_reset_action.isEnabled()
        window.zoom_reset_action.trigger()
        assert not window.zoom_reset_action.isEnabled()

    def test_the_buttons_name_themselves_and_report_the_zoom(self, window: MainWindow):
        chrome = window.chrome
        assert chrome.zoom_out_button.accessibleName() == "Decrease interface zoom"
        assert chrome.zoom_in_button.accessibleName() == "Increase interface zoom"
        window.set_interface_zoom(120)
        assert "currently 120%" in chrome.zoom_in_button.toolTip()

    def test_a_change_is_announced_in_the_status_bar(self, window: MainWindow):
        window.set_interface_zoom(120)
        assert window.statusBar().currentMessage() == "Interface zoom: 120%"

    def test_the_zoom_is_persisted_to_the_user_configuration(
        self, window: MainWindow, tmp_path: Path
    ):
        window.zoom_in_action.trigger()
        assert load_app_config(tmp_path / "config.json").ui_zoom_percent == 110

    def test_ribbon_density_stays_reachable_and_independent(self, window: MainWindow):
        density = window.ribbon.density
        window.density_roomier_action.trigger()
        assert window.ribbon.density == density + 1
        window.set_interface_zoom(150)
        assert window.ribbon.density == density + 1
        window.ribbon.set_density(Density.MAXIMUM)
        assert window.interface_zoom == 150


# ----------------------------------------------------------------------
# B - what grows
# ----------------------------------------------------------------------
class TestBWhatGrows:
    def test_the_application_font_follows_the_zoom(
        self, window: MainWindow, manager: UiScaleManager
    ):
        base = _baseline_points(manager)
        window.set_interface_zoom(150)
        assert QApplication.font().pointSizeF() == pytest.approx(base * 1.5)
        window.set_interface_zoom(80)
        assert QApplication.font().pointSizeF() == pytest.approx(base * 0.8)

    def test_100_150_100_returns_to_the_baseline_exactly(
        self, window: MainWindow, manager: UiScaleManager
    ):
        base_font = QFont(QApplication.font())
        chrome_height = window.chrome.height()
        step_height = window.ribbon.steps[0].height()
        footer_hint = window.footer.sizeHint()
        window.set_interface_zoom(150)
        _settle()
        window.set_interface_zoom(100)
        _settle()
        assert QApplication.font().pointSizeF() == base_font.pointSizeF()
        assert QApplication.font().family() == base_font.family()
        assert manager.baseline_font.pointSizeF() == base_font.pointSizeF()
        assert QApplication.instance().styleSheet() == application_stylesheet()
        assert window.chrome.height() == chrome_height == Chrome.HEIGHT
        assert window.ribbon.steps[0].height() == step_height == Navigator.STEP_HEIGHT
        assert window.footer.sizeHint() == footer_hint

    def test_repeated_changes_do_not_compound(self, window: MainWindow, manager: UiScaleManager):
        base = _baseline_points(manager)
        for percent in (110, 120, 130, 120):
            window.set_interface_zoom(percent)
        assert QApplication.font().pointSizeF() == pytest.approx(base * 1.2)
        assert window.chrome.height() == UiScale.of(120).px(Chrome.HEIGHT)

    def test_the_application_stylesheet_is_recomposed(self, window: MainWindow):
        window.set_interface_zoom(150)
        assert QApplication.instance().styleSheet() == application_stylesheet(UiScale.of(150))

    def test_the_chrome_row_and_its_controls_scale(self, window: MainWindow):
        window.set_interface_zoom(150)
        _settle()
        scale = UiScale.of(150)
        chrome = window.chrome
        assert chrome.height() == scale.px(Chrome.HEIGHT)
        assert chrome.menu_button.width() == scale.px(Chrome.MENU_BUTTON_SIZE)
        assert chrome.menu_button.iconSize().width() == scale.px(IconSize.CHROME_MENU)
        for button in (chrome.zoom_in_button, chrome.zoom_out_button,
                       chrome.previous_button, chrome.next_button):
            assert button.width() == scale.px(Chrome.SMALL_BUTTON_SIZE)
            assert button.iconSize().width() == scale.px(IconSize.CHROME_CONTROL)
        assert chrome.logo.height() == scale.px(Chrome.LOGO_HEIGHT)
        for button in (chrome.minimise_button, chrome.maximise_button, chrome.close_button):
            assert button.width() == scale.px(Chrome.WINDOW_BUTTON_WIDTH)
            assert button.height() == scale.px(Chrome.HEIGHT)

    def test_the_ribbon_measures_steps_at_the_new_zoom(self, window: MainWindow):
        natural = window.ribbon.steps[0].natural_width()
        window.set_interface_zoom(150)
        _settle()
        ribbon = window.ribbon
        assert ribbon.height() == UiScale.of(150).px(Navigator.STEP_HEIGHT)
        assert all(step.step_geometry.scale == UiScale.of(150) for step in ribbon.steps)
        assert ribbon.steps[0].natural_width() > natural

    def test_the_ribbon_still_chooses_a_layout_from_the_width(self, window: MainWindow):
        """Wider steps need more room; the same rule picks the layout."""
        ribbon = window.ribbon
        width = ribbon.sizeHint().width()
        assert ribbon.plan_for_width(width).mode is RibbonMode.FULL
        window.set_interface_zoom(200)
        _settle()
        assert ribbon.plan_for_width(width).mode is not RibbonMode.FULL
        assert ribbon.plan_for_width(10_000).mode is RibbonMode.FULL
        assert ribbon.plan_for_width(10_000).rows == 1

    def test_the_footer_scales_its_text_and_spacing(
        self, window: MainWindow, manager: UiScaleManager
    ):
        base = _baseline_points(manager)
        footer = window.footer
        before = footer.sizeHint().height()
        window.set_interface_zoom(150)
        _settle()
        assert footer.version_label.font().pointSizeF() == pytest.approx(
            (base + FontSize.FOOTER) * 1.5
        )
        assert footer.sizeHint().height() > before
        assert footer.status_dot.width() == UiScale.of(150).px(8)

    def test_semantic_font_deltas_scale_proportionally(
        self, window: MainWindow, manager: UiScaleManager, qtbot
    ):
        base = _baseline_points(manager)
        header = PageHeader("Title", "Summary", "info")
        card = Card("Card title")
        qtbot.addWidget(header)
        qtbot.addWidget(card)
        window.set_interface_zoom(150)
        assert header.title_label.font().pointSizeF() == pytest.approx(
            (base + FontSize.PAGE_TITLE) * 1.5
        )
        assert card.title_label is not None
        assert card.title_label.font().pointSizeF() == pytest.approx(
            (base + FontSize.SECTION_TITLE) * 1.5
        )
        window.set_interface_zoom(100)
        assert header.title_label.font().pointSizeF() == pytest.approx(base + FontSize.PAGE_TITLE)

    def test_common_widgets_rescale_their_explicit_geometry(self, window: MainWindow, qtbot):
        empty = EmptyState("folder", "Nothing here")
        chip = StatusChip("4/4", tone="ok")
        qtbot.addWidget(empty)
        qtbot.addWidget(chip)
        def icon_width() -> float:
            return empty.icon_label.pixmap().deviceIndependentSize().width()

        assert icon_width() == IconSize.EMPTY_STATE
        margins_before = empty.layout().contentsMargins().left()
        window.set_interface_zoom(150)
        assert icon_width() == UiScale.of(150).px(IconSize.EMPTY_STATE)
        assert empty.layout().contentsMargins().left() == UiScale.of(150).px(margins_before)
        assert "padding: 3px 12px" in chip.styleSheet()

    def test_page_margins_scale(self, window: MainWindow):
        page = window._pages["reports"]
        before = page.layout().contentsMargins().left()
        window.set_interface_zoom(150)
        assert page.layout().contentsMargins().left() == UiScale.of(150).px(before)

    def test_qt_default_metrics_scale_through_the_style(self, window: MainWindow):
        """Icon sizes and layout margins nobody set explicitly follow too."""
        from PySide6.QtWidgets import QStyle

        style = QApplication.style()
        icon = style.pixelMetric(QStyle.PixelMetric.PM_SmallIconSize)
        margin = style.pixelMetric(QStyle.PixelMetric.PM_LayoutLeftMargin)
        window.set_interface_zoom(200)
        style = QApplication.style()
        assert style.pixelMetric(QStyle.PixelMetric.PM_SmallIconSize) == icon * 2
        assert style.pixelMetric(QStyle.PixelMetric.PM_LayoutLeftMargin) == margin * 2


# ----------------------------------------------------------------------
# C - what does not change
# ----------------------------------------------------------------------
def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class TestCWhatStays:
    def test_the_current_page_is_kept(self, window: MainWindow):
        window.show_page("results")
        window.set_interface_zoom(150)
        window.set_interface_zoom(90)
        assert window.current_page_key() == "results"
        assert window.ribbon.current_key() == "results"

    def test_the_project_and_its_files_are_untouched(
        self, window: MainWindow, workspace: Path
    ):
        assert window.create_project_at(workspace, "Zoom Exam")
        session = window.session
        assert session is not None
        project_dir = session.root
        _settle()
        before = _snapshot(project_dir)
        window.set_interface_zoom(150)
        window.zoom_out_action.trigger()
        window.zoom_reset_action.trigger()
        _settle()
        assert window.session is session
        assert _snapshot(project_dir) == before
        window.close_project()

    def test_the_template_designers_document_zoom_is_unchanged(self, window: MainWindow):
        window.show_page("template")
        canvas = window._pages["template"].canvas
        canvas.set_blank_canvas(2000, 2800)
        canvas._apply_zoom(1.25)
        window.set_interface_zoom(120)
        _settle()
        assert canvas.zoom == pytest.approx(1.25)
        window.set_interface_zoom(100)
        _settle()
        assert canvas.zoom == pytest.approx(1.25)

    def test_the_canvas_keys_still_zoom_the_sheet(self, window: MainWindow):
        """Ctrl++, Ctrl+- and Ctrl+0 keep their Template Designer meaning."""
        window.show_page("template")
        page = window._pages["template"]
        canvas = page.canvas
        canvas.set_blank_canvas(2000, 2800)
        canvas.zoom_to_actual_size()
        window.set_interface_zoom(130)
        bindings = {shortcut.key().toString(): shortcut for shortcut in page._shortcuts}
        zoom_in = bindings[QKeySequence(QKeySequence.StandardKey.ZoomIn).toString()]
        zoom_out = bindings[QKeySequence(QKeySequence.StandardKey.ZoomOut).toString()]
        zoom_in.activated.emit()
        assert canvas.zoom > 1.0
        zoom_out.activated.emit()
        assert canvas.zoom == pytest.approx(1.0)
        bindings["Ctrl+0"].activated.emit()
        assert canvas.zoom != pytest.approx(1.0)
        assert window.interface_zoom == 130

    def test_the_scan_preview_zoom_is_unchanged_and_still_works(self, window: MainWindow):
        window.show_page("scan")
        preview = window._pages["scan"].preview
        preview.set_page(None, canonical_width=1000, canonical_height=1400)
        preview.set_zoom(1.5)
        window.set_interface_zoom(150)
        _settle()
        assert preview.zoom == pytest.approx(1.5)
        preview.zoom_in()
        assert preview.zoom > 1.5

    def test_the_calibration_fit_state_is_kept(self, window: MainWindow):
        window.show_page("calibration")
        page = window._pages["calibration"]
        page._actual_size_preview()
        window.set_interface_zoom(150)
        _settle()
        assert page._zoom_is_fit is False
        assert page.preview.zoom == pytest.approx(1.0)
        page._fit_preview()
        window.set_interface_zoom(100)
        _settle()
        assert page._zoom_is_fit is True

    def test_the_resolve_views_keep_their_zoom(self, window: MainWindow):
        window.show_page("resolve")
        page = window._pages["resolve"]
        views = page.findChildren(ScanPreviewView)
        assert views
        for view in views:
            view.set_page(None, canonical_width=1000, canonical_height=1400)
            view.set_zoom(0.75)
        window.set_interface_zoom(140)
        _settle()
        for view in views:
            assert view.zoom == pytest.approx(0.75), view.objectName()


# ----------------------------------------------------------------------
# D - dialogs and start-up
# ----------------------------------------------------------------------
class TestDDialogsAndStartup:
    def test_a_dialog_opened_after_a_change_is_born_at_the_new_zoom(
        self, window: MainWindow, manager: UiScaleManager, qtbot
    ):
        at_100 = SettingsDialog(AppConfig(), window, cpu_count=4)
        # Parented to the window, so destroyed with it.
        button_100 = at_100.findChildren(QPushButton)[0].minimumSizeHint().height()
        window.set_interface_zoom(150)
        dialog = SettingsDialog(window.config, window, cpu_count=4)
        dialog.show()
        _settle()
        assert dialog.font().pointSizeF() == pytest.approx(_baseline_points(manager) * 1.5)
        button_150 = dialog.findChildren(QPushButton)[0].minimumSizeHint().height()
        assert button_150 > button_100
        dialog.close()

    def test_the_about_dialogs_title_is_scaled_from_its_delta(
        self, window: MainWindow, manager: UiScaleManager, qtbot
    ):
        window.set_interface_zoom(150)
        dialog = AboutDialog(window)
        name = dialog.findChild(QLabel, "aboutApplicationName")
        assert name is not None
        assert name.font().pointSizeF() == pytest.approx((_baseline_points(manager) + 8) * 1.5)

    def test_startup_restores_the_saved_zoom(self, qtbot, tmp_path: Path, manager):
        window = MainWindow(
            config=AppConfig().with_ui_zoom_percent(130), config_path=tmp_path / "c.json"
        )
        try:
            assert manager.percent == 130
            assert window.interface_zoom == 130
            assert window.chrome.height() == UiScale.of(130).px(Chrome.HEIGHT)
            assert window.zoom_reset_action.isEnabled()
        finally:
            manager.set_percent(100)
            _destroy(window)

    def test_applying_settings_keeps_the_zoom(self, window: MainWindow):
        window.set_interface_zoom(120)
        window.apply_reviewer_name("Examiner")
        assert window.interface_zoom == 120
        assert current_scale().percent == 120


# ----------------------------------------------------------------------
# E - fitting
# ----------------------------------------------------------------------
def _clipped(root: QWidget) -> list[str]:
    """Visible push buttons whose label does not fit the space they were given.

    The label's own extent - text advance plus icon, and one line of text -
    rather than ``minimumSizeHint``: several buttons deliberately carry an
    explicit minimum below the stylesheet's padded minimum (the Resolve
    action row, at 100% as at any zoom), and are then drawn with less padding
    but all of their text, which is not clipping.
    """
    problems = []
    for widget in root.findChildren(QPushButton):
        if not widget.isVisible() or not widget.text():
            continue
        metrics = widget.fontMetrics()
        text = widget.text().replace("&", "")
        icon = widget.iconSize().width() if not widget.icon().isNull() else 0
        needed_width = metrics.horizontalAdvance(text) + icon
        needed_height = metrics.height()
        if widget.width() < needed_width or widget.height() < needed_height:
            problems.append(
                f"{widget.objectName() or text!r}: {widget.size().toTuple()} "
                f"< {(needed_width, needed_height)}"
            )
    return problems


class TestEFitting:
    @pytest.mark.parametrize("percent", [80, 120, 150, 200])
    def test_the_window_can_still_fit_a_small_display(self, window: MainWindow, percent: int):
        window.set_interface_zoom(percent)
        _settle()
        hint = window.minimumSizeHint()
        assert hint.width() <= DISPLAY[0]
        assert hint.height() <= DISPLAY[1]

    @pytest.mark.parametrize("percent", [80, 100, 120, 150, 200])
    @pytest.mark.parametrize("key", ["project", "attendance", "reports", "resolve"])
    def test_no_button_label_is_clipped(
        self, window: MainWindow, percent: int, key: str
    ):
        window.show_page(key)
        window.set_interface_zoom(percent)
        _settle()
        assert _clipped(window._pages[key]) == []

    @pytest.mark.parametrize("percent", [80, 120, 150, 200])
    def test_no_workflow_step_label_is_elided(self, window: MainWindow, percent: int):
        """The ribbon changes layout instead - it never shortens a label."""
        window.set_interface_zoom(percent)
        _settle()
        window.ribbon.repaint()
        for step in window.ribbon.steps:
            if step.isVisible() and window.ribbon.mode is not RibbonMode.SCROLL:
                assert not step.is_label_elided, step.display_text


class TestEWrappedRows:
    @pytest.mark.native_qt_layout(
        reason="Portable: test_a_row_given_its_height_for_width_shows_every_line."
    )
    @pytest.mark.parametrize("percent", [100, 150, 200])
    def test_an_action_row_is_tall_enough_for_its_wrapped_detail(
        self, window: MainWindow, percent: int
    ):
        """Regression: a third wrapped detail line drawn over the heading.

        At 150% "Project information" wrapped to a third line that a
        fixed-height row drew over its own heading.
        """
        from omr_scanner.gui.widgets import ActionRow

        window.show_page("project")
        window.set_interface_zoom(percent)
        _settle()
        rows = [row for row in window._pages["project"].findChildren(ActionRow) if row.isVisible()]
        assert rows
        for row in rows:
            layout = row.layout()
            assert layout is not None
            assert row.height() >= layout.totalHeightForWidth(row.width()), row.objectName()

    @pytest.mark.parametrize("percent", [100, 150, 200])
    def test_a_row_given_its_height_for_width_shows_every_line(
        self, qtbot, manager: UiScaleManager, percent: int
    ):
        """Portable: the contract the page's layout relies on, at explicit widths.

        The test above measures the real project page, which only means
        something in the desktop font: offscreen, the getting-started card's
        minimum width exceeds its 400 px column, so Qt asks the card for its
        height at that minimum width rather than the one it gets, and the rows
        come out short. What rules the regression out in any font is this:
        the row asks for enough height for every wrapped detail line, and
        given that height lays heading and detail out without overlap.
        """
        from omr_scanner.gui.widgets import ActionRow

        manager.set_percent(percent)
        holder = QWidget()
        qtbot.addWidget(holder)
        holder.resize(3000, 2000)
        row = ActionRow(
            "info",
            "Project information",
            "Open a project first - this manages the open project's details.",
            parent=holder,
        )
        holder.show()
        floor = row.minimumSizeHint().width()
        detail_heights = set()
        for width in (floor, floor + 60, floor + 160, floor + 600):
            height = row.heightForWidth(width)
            row.setGeometry(0, 0, width, height)
            layout = row.layout()
            assert layout is not None
            layout.activate()
            heading, detail = row.heading_label, row.detail_label
            needed = detail.heightForWidth(detail.width())
            assert detail.height() >= needed, (width, detail.height(), needed)
            assert heading.geometry().bottom() < detail.geometry().top(), width
            assert detail.geometry().bottom() < row.height(), width
            detail_heights.add(needed)
        # The widths really did change how the detail wraps.
        assert len(detail_heights) > 1
