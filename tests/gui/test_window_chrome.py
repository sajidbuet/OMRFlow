"""Tests for the custom title bar: drag areas, window controls, resizing.

Scope:
    What the application had to take over when it removed the native title
    bar, and the controls that share the row with the workflow ribbon.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The row's contents, in the order the brief fixes.
    B     Which parts of the row drag the window, and which do not.
    C     Minimise, maximise, restore, close, and Alt+F4.
    D     Resizing from the edges and corners.
    E     The previous/next workflow buttons.
    F     The density controls, and what they persist.
    G     The logo: aspect ratio, vector rendering, drag handle.
    ===== ==========================================================

Why the drag rules are asserted as a query and not as a drag:
    Moving a window is ``QWindow.startSystemMove()``, which hands the drag to
    the window manager. The offscreen platform used by a headless run has no
    window manager and declines, and on a developer's desktop a *successful*
    call would grab the pointer and move a real window across the screen in
    the middle of a test run. Neither is something to assert on. What can be
    asserted, and is what actually decides the behaviour, is the
    classification: :meth:`AppChrome.is_drag_area` answers "would a press here
    start a move?", and every interactive control in the row is checked
    against it.

Why some tests use ``frameless=False``:
    Only where the assertion is about the shell rather than the frame. The
    window-behaviour tests build the window exactly as the application does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from omr_scanner.config import DEFAULT_RIBBON_DENSITY, AppConfig, load_app_config
from omr_scanner.gui.branding import LOGO_ASPECT_RATIO, logo_svg_path
from omr_scanner.gui.main_window import WINDOW_RESIZE_BORDER, MainWindow
from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
from omr_scanner.gui.theme import Chrome, Density
from omr_scanner.gui.widgets.window_buttons import WindowButtonKind
from omr_scanner.gui.widgets.workflow_ribbon import RibbonMode

pytestmark = pytest.mark.gui

WIDE = 1600
TALL = 900


@pytest.fixture
def window(qtbot, tmp_path: Path) -> MainWindow:
    """A frameless window, built exactly as the application builds it."""
    main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(main_window)
    main_window.resize(WIDE, TALL)
    main_window.show()
    QApplication.processEvents()
    return main_window


def centre_of(widget: object) -> QPoint:
    return QPoint(widget.width() // 2, widget.height() // 2)


def resize_until_ribbon_mode(
    window: MainWindow, mode: RibbonMode, *, height: int = 760
) -> int:
    """Narrow ``window`` until its ribbon adopts ``mode``; return that width.

    Measured, never hard-coded. The ribbon picks its layout from the width
    nine labels need *in the font actually in use*, and that differs between a
    developer's Segoe UI desktop and a headless runner's fallback font - which
    is wider. "A 1000-pixel window scrolls" is true on one and false on the
    other, so a test asserting it either fails or, worse, skips itself on CI
    and silently stops covering anything.
    """
    for width in range(1920, window.minimumWidth() - 1, -20):
        window.resize(width, height)
        QApplication.processEvents()
        if window.ribbon.mode is mode:
            return width
    raise AssertionError(f"no window width between 1920 and the minimum produced {mode}")


# ----------------------------------------------------------------------
# A - the row's contents and their order
# ----------------------------------------------------------------------
class TestARowContents:
    def test_the_row_holds_everything_the_brief_lists(self, window: MainWindow):
        chrome = window.chrome
        for attribute in (
            "menu_button",
            "logo",
            "density_out_button",
            "density_in_button",
            "previous_button",
            "ribbon",
            "next_button",
            "minimise_button",
            "maximise_button",
            "close_button",
        ):
            assert getattr(chrome, attribute) is not None, attribute

    def test_the_controls_appear_in_the_specified_order(self, window: MainWindow):
        """Menu, logo, -, +, <, ribbon, >, then the window buttons."""
        chrome = window.chrome
        positions = [
            chrome.menu_button.x(),
            chrome.logo.x(),
            chrome.density_out_button.x(),
            chrome.density_in_button.x(),
            chrome.previous_button.x(),
            chrome.ribbon.x(),
            chrome.next_button.x(),
            chrome.minimise_button.x(),
            chrome.maximise_button.x(),
            chrome.close_button.x(),
        ]
        assert positions == sorted(positions), positions

    def test_the_window_controls_are_at_the_far_right(self, window: MainWindow):
        chrome = window.chrome
        assert chrome.close_button.x() + chrome.close_button.width() >= (
            chrome.width() - 1
        )
        assert chrome.close_button.x() > chrome.next_button.x()

    def test_the_workflow_begins_immediately_after_the_compact_controls(
        self, window: MainWindow
    ):
        """No gap where the tagline used to be.

        A tolerance rather than an exact number, because the assertion worth
        making is that nothing sits between the previous-stage arrow and the
        ribbon - not that the layout's spacing has any particular value.
        """
        chrome = window.chrome
        arrow_right = chrome.previous_button.x() + chrome.previous_button.width()
        assert 0 <= chrome.ribbon.x() - arrow_right <= 2 * Chrome.GROUP_GAP

    def test_every_part_of_the_row_shares_one_line(self, window: MainWindow):
        """It is a title bar; nothing in it may be on a second row."""
        chrome = window.chrome
        for widget in (
            chrome.menu_button,
            chrome.logo,
            chrome.density_out_button,
            chrome.previous_button,
            chrome.ribbon,
            chrome.next_button,
            chrome.close_button,
        ):
            assert widget.y() >= 0
            assert widget.y() + widget.height() <= Chrome.HEIGHT


# ----------------------------------------------------------------------
# B - dragging
# ----------------------------------------------------------------------
class TestBDragAreas:
    def test_the_empty_space_before_the_window_buttons_drags(
        self, window: MainWindow
    ):
        """The row's one guaranteed drag handle.

        Guaranteed because it is the only part that stays empty once the
        ribbon has filled its viewport, which is why it is a named width in
        the design tokens rather than an accident of the layout.
        """
        chrome = window.chrome
        handle_x = chrome.minimise_button.x() - Chrome.DRAG_HANDLE_WIDTH // 2
        assert chrome.is_drag_area(QPoint(handle_x, Chrome.HEIGHT // 2))

    def test_the_logo_area_drags(self, window: MainWindow):
        """The brief names the logo specifically."""
        chrome = window.chrome
        point = chrome.logo.geometry().center()
        assert chrome.is_drag_area(point)

    def test_the_logo_is_transparent_to_the_mouse(self, window: MainWindow):
        """Which is how a press on it reaches the row rather than stopping."""
        assert window.chrome.logo.testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        assert window.chrome.childAt(window.chrome.logo.geometry().center()) is None

    @pytest.mark.parametrize(
        "control",
        [
            "menu_button",
            "density_out_button",
            "density_in_button",
            "previous_button",
            "next_button",
            "minimise_button",
            "maximise_button",
            "close_button",
        ],
    )
    def test_no_interactive_control_drags_the_window(
        self, window: MainWindow, control: str
    ):
        """A row that dragged from anywhere would make every button unusable."""
        chrome = window.chrome
        widget = getattr(chrome, control)
        assert chrome.is_drag_area(widget.geometry().center()) is False, control

    def test_a_workflow_step_does_not_drag_the_window(self, window: MainWindow):
        chrome = window.chrome
        step = chrome.ribbon.step("project")
        assert step is not None
        point = step.mapTo(chrome, centre_of(step))
        assert chrome.is_drag_area(point) is False

    def test_clicking_a_workflow_step_still_navigates(self, window: MainWindow):
        """The other half of the same requirement.

        Not dragging is only correct if the press does what it was supposed
        to instead.
        """
        step = window.chrome.ribbon.step("calibration")
        assert step is not None
        QTest.mouseClick(step, Qt.MouseButton.LeftButton, pos=centre_of(step))
        QApplication.processEvents()
        assert window.current_page_key() == "calibration"

    def test_clicking_a_window_button_still_acts(self, window: MainWindow):
        received: list[bool] = []
        window.chrome.maximise_toggled.connect(lambda: received.append(True))
        QTest.mouseClick(window.chrome.maximise_button, Qt.MouseButton.LeftButton)
        assert received == [True]

    def test_double_clicking_a_drag_area_toggles_maximise(self, window: MainWindow):
        chrome = window.chrome
        received: list[bool] = []
        chrome.maximise_toggled.connect(lambda: received.append(True))
        handle_x = chrome.minimise_button.x() - Chrome.DRAG_HANDLE_WIDTH // 2
        QTest.mouseDClick(
            chrome, Qt.MouseButton.LeftButton, pos=QPoint(handle_x, Chrome.HEIGHT // 2)
        )
        assert received == [True]

    def test_double_clicking_a_control_does_not_toggle_maximise(
        self, window: MainWindow
    ):
        """Pressing ``+`` twice quickly must increase the density twice."""
        chrome = window.chrome
        received: list[bool] = []
        chrome.maximise_toggled.connect(lambda: received.append(True))
        QTest.mouseDClick(chrome.density_in_button, Qt.MouseButton.LeftButton)
        assert received == []


# ----------------------------------------------------------------------
# C - window state
# ----------------------------------------------------------------------
class TestCWindowState:
    def test_the_window_is_frameless_by_default(self, window: MainWindow):
        assert bool(window.windowFlags() & Qt.WindowType.FramelessWindowHint)

    def test_it_is_still_an_ordinary_top_level_window(self, window: MainWindow):
        """Which is what keeps the taskbar entry, Alt+Tab and activation.

        Frameless removes the frame, not the window: it still has a title for
        the taskbar to show, an icon, and no parent.
        """
        assert window.isWindow()
        assert window.parent() is None
        assert window.windowTitle()
        assert not window.windowIcon().isNull()

    def test_minimise_minimises(self, window: MainWindow):
        window.chrome.minimise_requested.emit()
        QApplication.processEvents()
        assert window.isMinimized()
        window.showNormal()
        QApplication.processEvents()

    def test_maximise_then_restore_returns_the_original_geometry(
        self, window: MainWindow
    ):
        before = window.geometry()
        window.toggle_maximised()
        QApplication.processEvents()
        assert window.isMaximized()
        window.toggle_maximised()
        QApplication.processEvents()
        assert window.isMaximized() is False
        assert window.geometry() == before

    def test_the_maximise_button_becomes_a_restore_button(self, window: MainWindow):
        assert window.chrome.maximise_button.kind is WindowButtonKind.MAXIMISE
        window.showMaximized()
        QApplication.processEvents()
        assert window.chrome.maximise_button.kind is WindowButtonKind.RESTORE
        assert "Restore" in window.chrome.maximise_button.accessibleName()
        window.showNormal()
        QApplication.processEvents()
        assert window.chrome.maximise_button.kind is WindowButtonKind.MAXIMISE

    def test_the_glyph_follows_a_state_change_the_application_did_not_start(
        self, window: MainWindow
    ):
        """Aero Snap, Win+Up and the taskbar menu all arrive this way.

        ``WindowStateChange`` is handled rather than the button's own click,
        so every route into maximised updates the glyph - not only the one
        route this application owns.
        """
        window.setWindowState(Qt.WindowState.WindowMaximized)
        QApplication.processEvents()
        assert window.chrome.maximise_button.kind is WindowButtonKind.RESTORE

    def test_close_closes(self, window: MainWindow):
        window.chrome.close_requested.emit()
        QApplication.processEvents()
        assert window.isVisible() is False

    def test_alt_f4_still_closes_the_window(self, window: MainWindow):
        """Qt maps it to a close event; nothing here may swallow it."""
        assert window.close() is True
        assert window.isVisible() is False

    def test_the_shell_survives_maximise_and_restore(self, window: MainWindow):
        window.showMaximized()
        QApplication.processEvents()
        assert window.chrome.y() == 0
        assert window.stack.y() == window.chrome.height()
        window.showNormal()
        QApplication.processEvents()
        assert window.chrome.y() == 0
        assert window.stack.y() == window.chrome.height()


# ----------------------------------------------------------------------
# D - resizing
# ----------------------------------------------------------------------
class TestDResizing:
    def test_a_resize_border_is_reserved_when_restored(self, window: MainWindow):
        """Something has to occupy the edge for a press there to reach the window."""
        margins = window.contentsMargins()
        assert margins.left() == WINDOW_RESIZE_BORDER
        assert margins.bottom() == WINDOW_RESIZE_BORDER

    def test_the_border_is_released_when_maximised(self, window: MainWindow):
        """There is no edge to drag, and the margin would show as a gap."""
        window.showMaximized()
        QApplication.processEvents()
        assert window.contentsMargins().left() == 0
        window.showNormal()
        QApplication.processEvents()
        assert window.contentsMargins().left() == WINDOW_RESIZE_BORDER

    @pytest.mark.parametrize(
        ("point", "expected"),
        [
            ((0, 200), Qt.Edge.LeftEdge),
            ((0, 0), Qt.Edge.LeftEdge | Qt.Edge.TopEdge),
            ((200, 0), Qt.Edge.TopEdge),
        ],
    )
    def test_the_edges_and_corners_are_grabbable(
        self, window: MainWindow, point: tuple[int, int], expected: Qt.Edge
    ):
        """A corner returns two edges, so it resizes in both axes."""
        assert window._edges_at(QPoint(*point)) == expected

    def test_the_far_edges_are_grabbable_too(self, window: MainWindow):
        assert window._edges_at(QPoint(window.width() - 1, 200)) == Qt.Edge.RightEdge
        assert window._edges_at(QPoint(200, window.height() - 1)) == Qt.Edge.BottomEdge
        assert window._edges_at(
            QPoint(window.width() - 1, window.height() - 1)
        ) == (Qt.Edge.RightEdge | Qt.Edge.BottomEdge)

    def test_the_middle_of_the_window_is_not_a_resize_grip(self, window: MainWindow):
        assert window._edges_at(QPoint(window.width() // 2, window.height() // 2)) == (
            Qt.Edge(0)
        )

    def test_a_maximised_window_has_no_resize_grips(self, window: MainWindow):
        window.showMaximized()
        QApplication.processEvents()
        assert window._edges_at(QPoint(0, 0)) == Qt.Edge(0)
        window.showNormal()

    @pytest.mark.parametrize(
        ("edges", "cursor"),
        [
            (Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor),
            (Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor),
            (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
            (Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
        ],
    )
    def test_each_edge_gets_the_conventional_cursor(
        self, window: MainWindow, edges: Qt.Edge, cursor: Qt.CursorShape
    ):
        assert MainWindow._cursor_for(edges) is cursor

    def test_the_window_can_still_be_resized_programmatically(
        self, window: MainWindow
    ):
        """The floor is the minimum size, not the frame."""
        window.resize(1000, 700)
        QApplication.processEvents()
        assert window.width() == 1000
        assert window.height() >= window.minimumHeight()

    def test_the_window_minimum_leaves_the_narrow_layout_reachable(
        self, window: MainWindow
    ):
        """A floor wider than the shell needs would hide its own narrow mode."""
        window.resize(window.minimumWidth(), window.minimumHeight())
        QApplication.processEvents()
        assert window.chrome.ribbon.mode is not RibbonMode.FULL

    def test_a_window_built_with_a_native_frame_reserves_no_border(
        self, qtbot, tmp_path: Path
    ):
        other = MainWindow(
            config=AppConfig(), config_path=tmp_path / "c.json", frameless=False
        )
        qtbot.addWidget(other)
        assert other.contentsMargins().left() == 0
        assert other._edges_at(QPoint(0, 0)) == Qt.Edge(0)


# ----------------------------------------------------------------------
# E - previous / next
# ----------------------------------------------------------------------
class TestEPreviousNext:
    def test_previous_is_disabled_at_the_first_stage(self, window: MainWindow):
        assert window.show_page("project")
        assert window.chrome.previous_button.isEnabled() is False
        assert window.chrome.next_button.isEnabled() is True

    def test_next_is_disabled_at_the_last_stage(self, window: MainWindow):
        assert window.show_page("reports")
        assert window.chrome.next_button.isEnabled() is False
        assert window.chrome.previous_button.isEnabled() is True

    def test_next_moves_one_stage_along(self, window: MainWindow):
        window.show_page("project")
        assert window.go_to_next_stage() is True
        assert window.current_page_key() == "template"
        assert window.ribbon.current_key() == "template"

    def test_previous_moves_one_stage_back(self, window: MainWindow):
        window.show_page("results")
        assert window.go_to_previous_stage() is True
        assert window.current_page_key() == "answer_key"

    def test_the_buttons_walk_the_whole_workflow_in_order(self, window: MainWindow):
        window.show_page("project")
        visited = [window.current_page_key()]
        while window.go_to_next_stage():
            visited.append(window.current_page_key())
        assert visited == [spec.key for spec in WORKFLOW_PAGES]

    def test_the_buttons_do_not_bypass_a_locked_stage(self, window: MainWindow):
        """Disabled, not redirected.

        Skipping a locked stage would quietly take an operator somewhere they
        did not ask to go; a disabled arrow is visible and says so.
        """
        window.show_page("project")
        window.ribbon.set_step_enabled("template", enabled=False, reason="Locked.")
        window.chrome.sync_navigation_buttons()
        assert window.chrome.next_button.isEnabled() is False
        assert window.go_to_next_stage() is False
        assert window.current_page_key() == "project"

    def test_the_buttons_carry_a_name_and_a_tooltip_naming_the_target(
        self, window: MainWindow
    ):
        window.show_page("scan")
        assert window.chrome.previous_button.accessibleName()
        assert "3. Calibrate" in window.chrome.previous_button.toolTip()
        assert "5. Resolve" in window.chrome.next_button.toolTip()

    def test_a_disabled_button_says_why_in_its_tooltip(self, window: MainWindow):
        window.show_page("project")
        assert "none available" in window.chrome.previous_button.toolTip()

    def test_arrow_navigation_scrolls_the_active_stage_into_view(
        self, window: MainWindow
    ):
        """Requirement 16, via the arrows rather than a click."""
        resize_until_ribbon_mode(window, RibbonMode.SCROLL)
        ribbon = window.ribbon
        assert ribbon.mode is RibbonMode.SCROLL
        window.show_page("project")
        for _ in range(8):
            window.go_to_next_stage()
        QApplication.processEvents()

        step = ribbon.step("reports")
        assert step is not None
        bar = ribbon._scroll.horizontalScrollBar()
        assert step.x() - bar.value() + step.width() <= (
            ribbon._scroll.viewport().width() + 1
        )


# ----------------------------------------------------------------------
# F - density controls
# ----------------------------------------------------------------------
class TestFDensityControls:
    def test_the_buttons_change_the_ribbons_density(self, window: MainWindow):
        start = window.ribbon.density
        QTest.mouseClick(window.chrome.density_in_button, Qt.MouseButton.LeftButton)
        assert window.ribbon.density == start + 1
        QTest.mouseClick(window.chrome.density_out_button, Qt.MouseButton.LeftButton)
        assert window.ribbon.density == start

    def test_the_buttons_disable_at_the_limits(self, window: MainWindow):
        window.ribbon.set_density(Density.MINIMUM)
        assert window.chrome.density_out_button.isEnabled() is False
        assert window.chrome.density_in_button.isEnabled() is True

        window.ribbon.set_density(Density.MAXIMUM)
        assert window.chrome.density_in_button.isEnabled() is False
        assert window.chrome.density_out_button.isEnabled() is True

    def test_the_density_is_persisted_to_the_application_configuration(
        self, qtbot, tmp_path: Path
    ):
        """A user preference, and not in any project file.

        Which is the backward-compatibility requirement stated as a test: a
        project saved before this feature existed has nothing to migrate,
        because nothing about it changed.
        """
        config_path = tmp_path / "config.json"
        first = MainWindow(config=AppConfig(), config_path=config_path)
        qtbot.addWidget(first)
        assert first.ribbon.density == DEFAULT_RIBBON_DENSITY
        first.ribbon.set_density(Density.MAXIMUM)
        QApplication.processEvents()

        stored = load_app_config(config_path)
        assert stored.ribbon_density == Density.MAXIMUM

        second = MainWindow(config=stored, config_path=config_path)
        qtbot.addWidget(second)
        assert second.ribbon.density == Density.MAXIMUM

    def test_the_density_does_not_scale_the_page_below(self, window: MainWindow):
        """It is not a zoom control, and the tooltip says so."""
        page = window._pages["reports"]
        before_font = page.font().pointSizeF()
        before_width = page.width()
        window.ribbon.set_density(Density.MAXIMUM)
        QApplication.processEvents()
        assert page.font().pointSizeF() == before_font
        assert page.width() == before_width
        assert "not scale the page" in window.chrome.density_in_button.toolTip()

    def test_the_density_buttons_are_named_and_keyboard_reachable(
        self, window: MainWindow
    ):
        for button in (
            window.chrome.density_out_button,
            window.chrome.density_in_button,
        ):
            assert button.accessibleName()
            assert button.focusPolicy() != Qt.FocusPolicy.NoFocus

    def test_a_stored_density_outside_the_range_is_clamped_not_rejected(self):
        """A preference that raised on the last click of ``+`` would be a crash."""
        assert AppConfig().with_ribbon_density(99).ribbon_density == Density.MAXIMUM
        assert AppConfig().with_ribbon_density(-5).ribbon_density == Density.MINIMUM


# ----------------------------------------------------------------------
# G - the logo
# ----------------------------------------------------------------------
class TestGLogo:
    def test_the_logo_is_in_the_chrome_row(self, window: MainWindow):
        assert isinstance(window.chrome.logo, QSvgWidget)
        assert window.chrome.logo.parentWidget() is window.chrome

    def test_there_is_exactly_one_logo(self, window: MainWindow):
        assert len(window.findChildren(QSvgWidget, "appLogo")) == 1

    def test_the_logo_is_a_vector_for_high_dpi(self, window: MainWindow):
        """A `QSvgWidget` re-renders at the device pixel ratio.

        The alternative - a `QPixmap` scaled to the row's height - looks
        identical at 100% scaling and visibly soft at 150%.
        """
        assert logo_svg_path().suffix == ".svg"
        assert window.chrome.logo.renderer() is not None

    def test_the_widget_matches_the_artworks_aspect_ratio(self, window: MainWindow):
        size = window.chrome.logo.size()
        assert size.width() / size.height() == pytest.approx(
            LOGO_ASPECT_RATIO, rel=0.02
        )

    def test_the_renderer_draws_the_artwork_and_not_the_empty_canvas(
        self, window: MainWindow
    ):
        """The fix for a wordmark that was being stretched by half again.

        ``logo.svg``'s `viewBox` is a square canvas with the ink occupying a
        1.55:1 rectangle inside it. Left alone, `QSvgWidget` maps that square
        onto a widget sized to the ink's ratio - which stretches the wordmark
        by exactly that factor and wastes about a third of the width on
        transparent padding. Narrowing the view box to the ink makes the
        mapping a pure scale.
        """
        view_box = window.chrome.logo.renderer().viewBoxF()
        assert view_box.width() / view_box.height() == pytest.approx(
            LOGO_ASPECT_RATIO, rel=0.001
        )
        assert view_box.width() < 1254, "the square canvas is not what gets drawn"

    def test_the_logo_is_compact_enough_not_to_crowd_the_ribbon(
        self, window: MainWindow
    ):
        assert window.chrome.logo.height() <= Chrome.HEIGHT - 2 * Chrome.V_PADDING
        assert window.chrome.logo.width() < window.chrome.ribbon.width() / 4

    def test_the_logo_names_itself_to_a_screen_reader(self, window: MainWindow):
        assert window.chrome.logo.accessibleName() == "OMRFlow"

    def test_the_row_survives_a_larger_platform_font(self, window: MainWindow):
        """Windows text scaling arrives as a larger application font.

        The row's height is fixed - it has to be, it is a title bar - so the
        thing to check is that nothing in it is pushed outside that height.
        """
        large = QFont(window.font())
        large.setPointSizeF(window.font().pointSizeF() * 1.5)
        window.setFont(large)
        QApplication.processEvents()
        assert window.chrome.height() == Chrome.HEIGHT
        for widget in (
            window.chrome.menu_button,
            window.chrome.logo,
            window.chrome.ribbon,
            window.chrome.close_button,
        ):
            assert widget.y() + widget.height() <= Chrome.HEIGHT
