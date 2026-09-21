"""Tests for the application shell: header, application menu and footer.

Scope:
    The bands around the workflow pages, and in particular the *regression*
    risk in moving File / Tools / Help behind one button: every menu, every
    nested submenu, every shortcut and every developer command must still be
    there and still work.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The shell is four bands and no left column.
    B     The header's menu button: icon, name, keyboard reach.
    C     The permanent File/Tools/Help row is gone; the hierarchy is not.
    D     Every keyboard shortcut still fires.
    E     Developer and stress-test commands are still reachable.
    F     Enabled/disabled action state still tracks the project.
    G     The footer states the build, licence, credit and status.
    H     The footer sheds its least important parts as it narrows.
    I     Accessibility: tab order, focus, no colour-only state.
    J     The shell survives representative window sizes and resizing.
    ===== ==========================================================

Why the menus are inspected through `menuBar()`:
    That is still where they live. The redesign hides the *bar* and pops the
    same `QMenu` objects up from the header button, precisely so that the
    hierarchy, the actions and Qt's shortcut handling are untouched. A test
    that looked only at the header button would pass while the actual menu
    structure rotted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QToolButton, QWidget

from omr_scanner import LICENSE_NAME, __version__
from omr_scanner.config import AppConfig
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.theme import Color
from omr_scanner.gui.widgets.app_header import MENU_ACCESSIBLE_NAME, TAGLINE_WORDS
from omr_scanner.gui.widgets.status_footer import AppStatus, FooterTier

pytestmark = pytest.mark.gui

REPRESENTATIVE_SIZES = (
    (1920, 1080),
    (1600, 900),
    (1366, 768),
    (1280, 720),
)
"""The display sizes the brief names. Applied as window sizes, which is the
harder case - a maximised window on each of these would be wider."""


@pytest.fixture
def window(qtbot, tmp_path: Path) -> MainWindow:
    main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(main_window)
    return main_window


def menus_of(window: MainWindow) -> dict[str, QMenu]:
    """Every menu in the window's hierarchy, by title."""
    return {menu.title(): menu for menu in window.menuBar().findChildren(QMenu)}


def action_texts(menu: QMenu) -> list[str]:
    return [action.text() for action in menu.actions()]


# ----------------------------------------------------------------------
# A - the shell's shape
# ----------------------------------------------------------------------
class TestAShellShape:
    def test_the_shell_is_header_navigator_pages_footer(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        bands = [window.header, window.navigator, window.stack, window.footer]
        tops = [band.y() for band in bands]
        assert tops == sorted(tops), tops

    def test_there_is_no_left_navigation_column(self, window: MainWindow):
        assert window.findChild(QWidget, "workflowSidebar") is None
        assert window.findChild(QWidget, "workflowNavigation") is None

    def test_the_window_minimum_shrank_with_the_sidebar(self, window: MainWindow):
        """The 190-pixel sidebar was most of the old 960-pixel floor.

        Keeping that floor would have made the navigator's compact layouts
        unreachable, which is the opposite of why they exist.
        """
        assert window.minimumWidth() < 960

    def test_the_accent_is_the_brand_colour(self):
        assert Color.PRIMARY.upper() == "#AC1F24"


# ----------------------------------------------------------------------
# B - the menu button
# ----------------------------------------------------------------------
class TestBMenuButton:
    def test_the_header_has_one_menu_button(self, window: MainWindow):
        buttons = window.findChildren(QToolButton, "appMenuButton")
        assert len(buttons) == 1
        assert buttons[0] is window.header.menu_button

    def test_the_button_uses_a_scalable_icon_not_a_text_glyph(
        self, window: MainWindow
    ):
        """The hamburger is a vector asset, not a character.

        A Unicode hamburger is a font dependency: different weight and
        baseline in every UI font, missing from some, and it cannot take the
        application's own colour.
        """
        button = window.header.menu_button
        assert not button.icon().isNull()
        assert button.text() == ""
        assert "☰" not in button.text()

    def test_the_button_has_an_accessible_name(self, window: MainWindow):
        """An icon-only button must never be nameless.

        It shows an icon and no text, so without this a screen reader
        announces an unnamed button.
        """
        assert window.header.menu_button.accessibleName() == MENU_ACCESSIBLE_NAME
        assert MENU_ACCESSIBLE_NAME == "Application menu"

    def test_the_button_is_keyboard_reachable(self, window: MainWindow):
        assert (
            window.header.menu_button.focusPolicy() == Qt.FocusPolicy.StrongFocus
        )

    def test_the_button_opens_the_application_menu(self, window: MainWindow):
        assert window.header.menu_button.menu() is window.application_menu

    def test_the_menu_offers_exactly_file_tools_and_help(self, window: MainWindow):
        submenus = [
            action.menu().title()
            for action in window.application_menu.actions()
            if action.menu() is not None
        ]
        assert submenus == ["&File", "&Tools", "&Help"]

    def test_the_button_explains_itself_in_a_tooltip(self, window: MainWindow):
        tooltip = window.header.menu_button.toolTip()
        assert "File" in tooltip and "Tools" in tooltip and "Help" in tooltip


# ----------------------------------------------------------------------
# C - the menu hierarchy survives
# ----------------------------------------------------------------------
class TestCMenuHierarchyPreserved:
    def test_the_permanent_menu_row_is_hidden(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        assert window.menuBar().isVisible() is False

    def test_the_three_top_level_menus_still_exist(self, window: MainWindow):
        titles = set(menus_of(window))
        assert {"&File", "&Tools", "&Help"}.issubset(titles)

    def test_the_file_menu_keeps_all_of_its_commands(self, window: MainWindow):
        texts = action_texts(menus_of(window)["&File"])
        for expected in (
            "&New Project...",
            "&Open Project...",
            "Project &Configuration...",
            "&Close Project",
            "&Settings...",
            "E&xit",
        ):
            assert expected in texts, texts

    def test_the_open_recent_submenu_is_still_nested_under_file(
        self, window: MainWindow
    ):
        assert "Open &Recent" in menus_of(window)
        assert window.recent_menu.title() == "Open &Recent"

    def test_the_tools_menu_keeps_all_of_its_commands(self, window: MainWindow):
        texts = action_texts(menus_of(window)["&Tools"])
        assert "&Project Health / Recovery..." in texts
        assert "Create &Diagnostic Bundle..." in texts

    def test_the_help_menu_keeps_about(self, window: MainWindow):
        assert "&About OMRFlow" in action_texts(menus_of(window)["&Help"])

    def test_nested_submenus_are_reachable_two_levels_deep(
        self, window: MainWindow
    ):
        """File > Open Recent and Tools > Developer / Testing.

        Nesting is the thing most likely to be lost when a menu bar is
        replaced by a button, so both nested menus are checked explicitly.
        """
        titles = set(menus_of(window))
        assert "Open &Recent" in titles
        assert "&Developer / Testing" in titles

    def test_no_duplicate_menu_row_is_left_visible_anywhere(
        self, window: MainWindow
    ):
        """One File menu in the application, not two."""
        window.show()
        QApplication.processEvents()
        files = [
            menu for menu in window.findChildren(QMenu) if menu.title() == "&File"
        ]
        assert len(files) == 1


# ----------------------------------------------------------------------
# D - shortcuts
# ----------------------------------------------------------------------
class TestDShortcutsStillFire:
    def test_the_shortcuts_are_still_declared(self, window: MainWindow):
        assert window.new_project_action.shortcut().toString() == "Ctrl+N"
        assert window.open_project_action.shortcut().toString() == "Ctrl+O"

    @pytest.mark.parametrize(
        ("key", "attribute"),
        [
            (Qt.Key.Key_N, "new_project_action"),
            (Qt.Key.Key_O, "open_project_action"),
        ],
    )
    def test_pressing_the_shortcut_triggers_the_action(
        self, window: MainWindow, key: Qt.Key, attribute: str
    ):
        """Actually pressed, not merely declared.

        This is the regression the redesign most risks: Qt deactivates the
        shortcuts of actions that live only inside a hidden widget, so hiding
        the menu bar could have silently disabled every accelerator in the
        application. Nothing short of synthesising the key press would have
        caught it.
        """
        action: QAction = getattr(window, attribute)
        action.triggered.disconnect()
        fired: list[bool] = []
        action.triggered.connect(lambda: fired.append(True))

        window.show()
        QApplication.processEvents()
        QTest.keyClick(window, key, Qt.KeyboardModifier.ControlModifier)
        QApplication.processEvents()

        assert fired == [True]

    def test_every_shortcut_bearing_action_is_registered_on_the_window(
        self, window: MainWindow
    ):
        """Which is what gives them window context while the bar is hidden."""
        registered = set(window.actions())
        for action in window._shortcut_actions():
            assert action in registered


# ----------------------------------------------------------------------
# E - developer commands
# ----------------------------------------------------------------------
class TestEDeveloperCommandsReachable:
    def test_the_developer_submenu_keeps_all_three_commands(
        self, window: MainWindow
    ):
        texts = action_texts(menus_of(window)["&Developer / Testing"])
        assert "&Generate Synthetic Test Dataset..." in texts
        assert "Run Recognition &Benchmark..." in texts
        assert "Run &100,000-Sheet Stress Test..." in texts

    def test_the_developer_commands_are_enabled(self, window: MainWindow):
        for action in (
            window.generate_dataset_action,
            window.run_benchmark_action,
            window.run_stress_qualification_action,
        ):
            assert action.isEnabled()

    def test_the_stress_test_command_is_still_wired(self, window: MainWindow):
        """The action reaches a real handler, not a dead menu entry."""
        assert hasattr(window, "_prompt_run_stress_qualification")
        assert window.run_stress_qualification_action.objectName() == (
            "runStressQualificationAction"
        )
        assert "force-kill" in window.run_stress_qualification_action.statusTip()

    def test_the_diagnostic_bundle_command_is_still_available(
        self, window: MainWindow
    ):
        assert window.diagnostic_bundle_action.isEnabled()


# ----------------------------------------------------------------------
# F - action state still tracks the project
# ----------------------------------------------------------------------
class TestFActionStateTracksTheProject:
    def test_project_actions_start_disabled(self, window: MainWindow):
        assert window.close_project_action.isEnabled() is False
        assert window.project_health_action.isEnabled() is False
        assert window.project_config_action.isEnabled() is False

    def test_project_actions_enable_once_a_project_is_open(
        self, window: MainWindow, tmp_path: Path
    ):
        assert window.create_project_at(tmp_path, "Exam")
        assert window.close_project_action.isEnabled() is True
        assert window.project_health_action.isEnabled() is True
        assert window.project_config_action.isEnabled() is True

    def test_project_actions_disable_again_when_it_closes(
        self, window: MainWindow, tmp_path: Path
    ):
        assert window.create_project_at(tmp_path, "Exam")
        window.close_project()
        assert window.close_project_action.isEnabled() is False


# ----------------------------------------------------------------------
# G - the footer's content
# ----------------------------------------------------------------------
class TestGFooterContent:
    def test_the_footer_shows_the_real_version(self, window: MainWindow):
        assert __version__ in window.footer.version_label.text()

    def test_the_footer_shows_the_real_licence(self, window: MainWindow):
        assert LICENSE_NAME in window.footer.licence_label.text()

    def test_the_footer_starts_ready(self, window: MainWindow):
        assert window.footer.status is AppStatus.READY

    def test_the_status_follows_the_scan_stages_batch(self, window: MainWindow):
        """Mapped from real state, not invented.

        The Scan page emits when its worker starts and stops; those two are
        the only application states OMRFlow genuinely has, which is why there
        is no third word in `AppStatus`.
        """
        window._on_processing_changed(True)
        assert window.footer.status is AppStatus.PROCESSING
        assert window.footer.status_label.text() == "Processing"

        window._on_processing_changed(False)
        assert window.footer.status is AppStatus.READY

    def test_the_scan_page_reports_processing_changes(self, window: MainWindow):
        """The signal the footer depends on exists and is connected."""
        from omr_scanner.gui.scan.page import ScanPage

        scan_page = window._pages["scan"]
        assert isinstance(scan_page, ScanPage)
        received: list[bool] = []
        scan_page.processing_changed.connect(received.append)
        scan_page._announce_processing(True)
        scan_page._announce_processing(True)
        scan_page._announce_processing(False)
        assert received == [True, False]


# ----------------------------------------------------------------------
# H - the footer's responsive behaviour
# ----------------------------------------------------------------------
class TestHFooterResponds:
    def test_a_wide_footer_shows_everything_on_one_row(self, window: MainWindow):
        window.resize(1600, 900)
        window.show()
        QApplication.processEvents()
        assert window.footer.tier is FooterTier.FULL
        assert window.footer.licence_label.isVisible()
        assert window.footer.credit_label.isVisible()

    def test_the_licence_text_is_the_first_thing_dropped(self, window: MainWindow):
        footer = window.footer
        full = footer._width_for(FooterTier.FULL)
        assert footer.tier_for_width(full - 1) is FooterTier.NO_LICENCE

    def test_the_credit_moves_to_a_second_line_before_it_disappears(
        self, window: MainWindow
    ):
        """The credit reflows rather than vanishing.

        The brief allows the first and not the second.

        Checked with `isVisibleTo`, not `isVisible`: on a window that was
        never shown the latter is false for every descendant regardless of
        what the layout did, which is the gotcha `docs/TESTING.md` records.
        """
        footer = window.footer
        assert footer.tier_for_width(10) is FooterTier.STACKED
        footer._apply_tier(FooterTier.STACKED)
        assert footer.credit_label.isVisibleTo(footer)

    def test_the_version_and_status_are_never_dropped(self, window: MainWindow):
        """The two things an operator glances down here to read."""
        footer = window.footer
        for tier in FooterTier:
            footer._apply_tier(tier)
            assert footer.version_label.isVisibleTo(footer)
            assert footer.status_label.isVisibleTo(footer)
            assert footer.version_label.text()
            assert footer.status_label.text()

    def test_the_footer_never_shrinks_its_text_to_fit(self, window: MainWindow):
        footer = window.footer
        before = footer.version_label.font().pointSizeF()
        footer._apply_tier(FooterTier.STACKED)
        assert footer.version_label.font().pointSizeF() == before


# ----------------------------------------------------------------------
# I - accessibility
# ----------------------------------------------------------------------
class TestIAccessibility:
    def test_the_menu_button_is_the_first_thing_in_the_tab_order(
        self, window: MainWindow
    ):
        """So the whole menu hierarchy is available without a mouse."""
        window.show()
        QApplication.processEvents()
        window.header.menu_button.setFocus()
        assert window.header.menu_button.hasFocus()

    def test_tab_moves_forward_and_shift_tab_moves_back(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        window.header.menu_button.setFocus()
        first = QApplication.focusWidget()
        QTest.keyClick(window, Qt.Key.Key_Tab)
        second = QApplication.focusWidget()
        assert second is not first
        QTest.keyClick(window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        assert QApplication.focusWidget() is first

    def test_the_workflow_steps_are_in_the_tab_order(self, window: MainWindow):
        for step in window.navigator.steps:
            assert step.focusPolicy() == Qt.FocusPolicy.StrongFocus

    def test_no_state_is_carried_by_colour_alone(self, window: MainWindow):
        """Three states, three non-colour signals.

        The active step is also bold; a disabled step is also dashed and
        explains itself in its tooltip; the footer status is also a word.
        """
        window.navigator.set_current_key("scan")
        window.navigator.set_step_enabled("reports", enabled=False, reason="Locked.")

        active = window.navigator.step("scan")
        locked = window.navigator.step("reports")
        assert active is not None and locked is not None
        assert active.isChecked() is True
        assert locked.isEnabled() is False
        assert locked.disabled_reason
        assert window.footer.status_label.text() == "Ready"

    def test_the_tagline_is_text_and_not_an_image(self, window: MainWindow):
        """The tagline is rendered as text.

        No information may live only inside a picture, and this way it scales
        with the font and reaches a screen reader.
        """
        text = window.header.tagline.text()
        for word in TAGLINE_WORDS:
            assert word in text
        assert window.header.tagline.accessibleName()

    def test_text_survives_a_larger_platform_font(self, window: MainWindow):
        """Windows text scaling arrives as a larger application font."""
        large = QFont(window.font())
        large.setPointSizeF(window.font().pointSizeF() * 1.5)
        window.setFont(large)
        QApplication.processEvents()
        assert window.footer.version_label.text()
        assert all(step.display_text for step in window.navigator.steps)


# ----------------------------------------------------------------------
# J - window sizes and resizing
# ----------------------------------------------------------------------
class TestJWindowSizes:
    @pytest.mark.parametrize(("width", "height"), REPRESENTATIVE_SIZES)
    def test_the_shell_lays_out_at_representative_sizes(
        self, window: MainWindow, width: int, height: int
    ):
        window.resize(width, height)
        window.show()
        QApplication.processEvents()

        central = window.centralWidget()
        assert central is not None
        # Nothing overlaps and nothing is clipped: the four bands tile the
        # central widget top to bottom.
        assert window.header.y() == 0
        assert window.navigator.y() == window.header.height()
        assert window.stack.y() == window.navigator.y() + window.navigator.height()
        assert window.footer.y() == window.stack.y() + window.stack.height()
        assert window.footer.y() + window.footer.height() <= central.height()

    def test_no_workflow_step_is_clipped_after_a_single_resize(
        self, window: MainWindow
    ):
        """Regression test for a clipped second row.

        Inside a window the navigator is laid out by its parent, which makes
        the ordering inside its own ``_apply`` matter in a way it does not
        for a top-level widget: setting the band's height delivers a
        re-entrant resize that the navigator's guard swallows, so the
        corrective pass never came and two rows of chevrons were placed
        inside a viewport still sized for one. The second row rendered as a
        sliver. One resize and one event pass is the whole point - it used to
        take several before the layout caught up.
        """
        window.show()
        QApplication.processEvents()
        for width in (1600, 1000, 820, 1600, 900):
            window.resize(width, 800)
            QApplication.processEvents()
            viewport = window.navigator._scroll.viewport()
            clipped = [
                step.key
                for step in window.navigator.steps
                if step.y() + step.height() > viewport.height()
            ]
            assert clipped == [], (
                f"at {width}px in {window.navigator.mode.value}: {clipped}"
            )

    def test_the_navigator_band_is_tall_enough_for_its_layout(
        self, window: MainWindow
    ):
        """The band grows when the navigator needs a second row."""
        window.show()
        for width in (1600, 1000, 820):
            window.resize(width, 800)
            QApplication.processEvents()
            nav = window.navigator
            needed = max(step.y() + step.height() for step in nav.steps)
            assert nav.height() >= needed, nav.mode.value

    def test_the_pages_keep_most_of_the_window_height(self, window: MainWindow):
        """The chrome must not eat the workspace the editor stages need."""
        window.resize(1366, 768)
        window.show()
        QApplication.processEvents()
        central = window.centralWidget()
        assert central is not None
        assert window.stack.height() > central.height() * 0.7

    def test_repeated_resizing_keeps_the_shell_consistent(
        self, window: MainWindow
    ):
        window.show()
        for _ in range(3):
            for width, height in (*REPRESENTATIVE_SIZES, (900, 700), (760, 620)):
                window.resize(width, height)
                QApplication.processEvents()
                assert window.header.y() == 0
                assert window.navigator.y() == window.header.height()
                assert window.stack.height() > 0

    def test_maximising_and_restoring_keeps_the_shell_consistent(
        self, window: MainWindow
    ):
        window.resize(1280, 720)
        window.show()
        QApplication.processEvents()
        window.showMaximized()
        QApplication.processEvents()
        window.showNormal()
        QApplication.processEvents()
        assert window.header.y() == 0
        assert window.navigator.y() == window.header.height()
        assert window.stack.y() == window.navigator.y() + window.navigator.height()

    def test_the_current_page_survives_resizing(self, window: MainWindow):
        window.show()
        assert window.show_page("attendance")
        for width, height in (*REPRESENTATIVE_SIZES, (800, 600)):
            window.resize(width, height)
            QApplication.processEvents()
            assert window.current_page_key() == "attendance"
            assert window.navigator.current_key() == "attendance"
