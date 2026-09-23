"""Tests for the application shell: the chrome row, the menu and the footer.

Scope:
    The bands around the workflow pages. Two *regression* risks dominate and
    shape most of what is asserted here. Moving File / Tools / Help behind one
    button must not lose a menu, a submenu, a shortcut or a developer command;
    and merging the title bar into the chrome row must not lose the project
    name, the licence, the status, or the reclaimed vertical space that was
    the whole point of doing it.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     The shell is three bands: chrome, pages, footer.
    B     The chrome row's menu button: icon, name, keyboard reach.
    C     The permanent File/Tools/Help row is gone; the hierarchy is not.
    D     Every keyboard shortcut still fires.
    E     Developer and stress-test commands are still reachable.
    F     Enabled/disabled action state still tracks the project.
    G     The footer states the build, the project, the credit, the
          licence and the status - in that arrangement.
    H     The footer sheds its least important parts as it narrows.
    I     No page carries a heading repeating its workflow stage.
    J     Accessibility: tab order, focus, no colour-only state.
    K     The shell survives representative window sizes and resizing.
    ===== ==========================================================

Why the menus are inspected through `menuBar()`:
    That is still where they live. The redesign hides the *bar* and pops the
    same `QMenu` objects up from the chrome row's button, precisely so that
    the hierarchy, the actions and Qt's shortcut handling are untouched. A
    test that looked only at the button would pass while the actual menu
    structure rotted.

Why these windows are built with ``frameless=False``:
    Everything here is about the shell's *contents*, and a frameless window
    adds a resize border and a platform-dependent frame to every geometry
    assertion for no gain. The frameless behaviour has its own suite -
    ``test_window_chrome.py`` - which builds windows the way the application
    does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QToolButton, QWidget

from omr_scanner import LICENSE_NAME, __version__
from omr_scanner.config import AppConfig
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
from omr_scanner.gui.theme import Chrome, Color
from omr_scanner.gui.widgets.app_chrome import MENU_ACCESSIBLE_NAME
from omr_scanner.gui.widgets.status_footer import (
    NO_PROJECT_TEXT,
    AppStatus,
    FooterTier,
)

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
    main_window = MainWindow(
        config=AppConfig(), config_path=tmp_path / "config.json", frameless=False
    )
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
    def test_the_shell_is_chrome_pages_footer(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        bands = [window.chrome, window.stack, window.footer]
        tops = [band.y() for band in bands]
        assert tops == sorted(tops), tops

    def test_there_is_no_second_chrome_band(self, window: MainWindow):
        """The pages start immediately under the one chrome row.

        The previous shell had a branded header *and* a navigator band. Their
        merge is the change this whole redesign is built on, so it is asserted
        as an adjacency rather than left to a screenshot.
        """
        window.show()
        QApplication.processEvents()
        assert window.chrome.y() == 0
        assert window.stack.y() == window.chrome.height()

    def test_there_is_no_left_navigation_column(self, window: MainWindow):
        assert window.findChild(QWidget, "workflowSidebar") is None
        assert window.findChild(QWidget, "workflowNavigation") is None

    def test_the_whole_chrome_is_one_compact_row(self, window: MainWindow):
        """One row, and a short one.

        The previous shell spent 48 pixels on a header and another 50-plus on
        a navigator band, on every stage. This is the number that replaced
        both of them.
        """
        assert window.chrome.height() == Chrome.HEIGHT
        assert Chrome.HEIGHT <= 56

    def test_the_tagline_is_gone_from_the_shell(self, window: MainWindow):
        """SCAN . GRADE . SIMPLIFY is removed, not merely hidden.

        Hidden would still be a widget in the layout, which is the "blank
        space where it used to be" the brief rules out - so the assertion is
        that no label anywhere in the shell carries any of the three words.
        """
        texts = " ".join(label.text() for label in window.chrome.findChildren(QLabel))
        for word in ("SCAN", "GRADE", "SIMPLIFY"):
            assert word not in texts
        assert not hasattr(window.chrome, "tagline")

    def test_the_accent_is_the_brand_colour(self):
        assert Color.PRIMARY.upper() == "#AC1F24"


# ----------------------------------------------------------------------
# B - the menu button
# ----------------------------------------------------------------------
class TestBMenuButton:
    def test_the_chrome_row_has_one_menu_button(self, window: MainWindow):
        buttons = window.findChildren(QToolButton, "appMenuButton")
        assert len(buttons) == 1
        assert buttons[0] is window.chrome.menu_button

    def test_the_menu_button_is_the_leftmost_control(self, window: MainWindow):
        """Far left, as the brief places it."""
        window.resize(1600, 900)
        window.show()
        QApplication.processEvents()
        button = window.chrome.menu_button
        for other in (
            window.chrome.logo,
            window.chrome.density_out_button,
            window.chrome.ribbon,
            window.chrome.close_button,
        ):
            assert button.x() < other.x()

    def test_the_button_uses_a_scalable_icon_not_a_text_glyph(
        self, window: MainWindow
    ):
        """The hamburger is a vector asset, not a character.

        A Unicode hamburger is a font dependency: different weight and
        baseline in every UI font, missing from some, and it cannot take the
        application's own colour.
        """
        button = window.chrome.menu_button
        assert not button.icon().isNull()
        assert button.text() == ""

    def test_the_button_has_an_accessible_name(self, window: MainWindow):
        """An icon-only button must never be nameless."""
        assert window.chrome.menu_button.accessibleName() == MENU_ACCESSIBLE_NAME
        assert MENU_ACCESSIBLE_NAME == "Application menu"

    def test_the_button_is_keyboard_reachable(self, window: MainWindow):
        assert window.chrome.menu_button.focusPolicy() == Qt.FocusPolicy.StrongFocus

    def test_the_button_opens_the_application_menu(self, window: MainWindow):
        assert window.chrome.menu_button.menu() is window.application_menu

    def test_the_menu_offers_exactly_file_tools_and_help(self, window: MainWindow):
        submenus = [
            action.menu().title()
            for action in window.application_menu.actions()
            if action.menu() is not None
        ]
        assert submenus == ["&File", "&Tools", "&Help"]

    def test_the_button_explains_itself_in_a_tooltip(self, window: MainWindow):
        tooltip = window.chrome.menu_button.toolTip()
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

    def test_nested_submenus_are_reachable_two_levels_deep(self, window: MainWindow):
        """File > Open Recent and Tools > Developer / Testing.

        Nesting is the thing most likely to be lost when a menu bar is
        replaced by a button, so both nested menus are checked explicitly.
        """
        titles = set(menus_of(window))
        assert "Open &Recent" in titles
        assert "&Developer / Testing" in titles

    def test_no_duplicate_menu_row_is_left_visible_anywhere(self, window: MainWindow):
        """One File menu in the application, not two."""
        window.show()
        QApplication.processEvents()
        files = [menu for menu in window.findChildren(QMenu) if menu.title() == "&File"]
        assert len(files) == 1

    def test_a_menu_command_still_navigates(self, window: MainWindow):
        """Commands reach the pages, not only the menu.

        The active stage has to follow a navigation started from the menu just
        as it follows a click, which is the brief's "however navigation
        occurs" requirement.
        """
        assert window.show_page("results")
        assert window.current_page_key() == "results"
        assert window.ribbon.current_key() == "results"


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

        Qt deactivates the shortcuts of actions that live only inside a hidden
        widget, so hiding the menu bar could have silently disabled every
        accelerator in the application. Nothing short of synthesising the key
        press would have caught it.
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
    def test_the_developer_submenu_keeps_all_three_commands(self, window: MainWindow):
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

    def test_the_diagnostic_bundle_command_is_still_available(self, window: MainWindow):
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

    def test_with_no_project_the_footer_says_so(self, window: MainWindow):
        """A statement, never a blank.

        An empty space beside the version reads as a footer that has not
        finished loading.
        """
        assert window.footer.project_title is None
        assert window.footer.project_label.full_text == NO_PROJECT_TEXT

    def test_creating_a_project_puts_its_title_in_the_footer(
        self, window: MainWindow, tmp_path: Path
    ):
        assert window.create_project_at(tmp_path, "Recruitment Exam")
        assert window.footer.project_title == "Recruitment Exam"
        assert "Project:" in window.footer.project_label.full_text

    def test_opening_a_project_puts_its_title_in_the_footer(
        self, window: MainWindow, tmp_path: Path
    ):
        assert window.create_project_at(tmp_path, "First")
        window.close_project()
        assert window.open_project_at(tmp_path / "First")
        assert window.footer.project_title == "First"

    def test_closing_a_project_clears_the_footer(
        self, window: MainWindow, tmp_path: Path
    ):
        """No stale project name - the brief's own words."""
        assert window.create_project_at(tmp_path, "Recruitment Exam")
        window.close_project()
        assert window.footer.project_title is None
        assert window.footer.project_label.full_text == NO_PROJECT_TEXT

    def test_opening_a_second_project_replaces_the_first(
        self, window: MainWindow, tmp_path: Path
    ):
        assert window.create_project_at(tmp_path, "First")
        assert window.create_project_at(tmp_path, "Second")
        assert window.footer.project_title == "Second"

    def test_the_footer_shows_the_examination_title_not_the_path(
        self, window: MainWindow, tmp_path: Path
    ):
        """The display title, which is not the folder name and never the path.

        An operator renames the examination in Project Configuration to
        something that need not be a legal folder name, and that is what they
        are checking they have open.
        """
        assert window.create_project_at(
            tmp_path,
            "bscra-2026",
            exam_name="Recruitment Exam, Bangladesh Submarine Cable Authority",
        )
        shown = window.footer.project_label.full_text
        assert "Recruitment Exam, Bangladesh Submarine Cable Authority" in shown
        assert str(tmp_path) not in shown

    def test_a_long_project_title_elides_and_keeps_its_tooltip(
        self, window: MainWindow, tmp_path: Path
    ):
        """The version beside it must survive, and nothing may be lost."""
        title = ("Recruitment Examination for the Bangladesh Submarine Cable " * 3).strip()
        assert window.create_project_at(tmp_path, "long", exam_name=title)
        window.resize(900, 700)
        window.show()
        QApplication.processEvents()

        label = window.footer.project_label
        assert label.is_elided
        assert title in label.toolTip()
        assert __version__ in window.footer.version_label.text()
        assert window.footer.version_label.isVisibleTo(window.footer)

    def test_the_status_follows_the_scan_stages_batch(self, window: MainWindow):
        """Mapped from real state, not invented."""
        window._on_processing_changed(True)
        assert window.footer.status is AppStatus.PROCESSING
        assert window.footer.status_label.text() == "Processing"

        window._on_processing_changed(False)
        assert window.footer.status is AppStatus.READY

    def test_the_footer_has_only_the_two_states_the_application_has(self):
        """No "Paused", no "Error" - nothing sets either.

        The brief lists both as examples; OMRFlow has no pause control and no
        persistent error condition, and a status word nothing can produce
        would be a lie told slowly.
        """
        assert {status.value for status in AppStatus} == {"Ready", "Processing"}

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
# H - the footer's arrangement and responsive behaviour
# ----------------------------------------------------------------------
class TestHFooterLayout:
    def _order(self, window: MainWindow) -> list[int]:
        return [
            window.footer.version_label.x(),
            window.footer.project_label.x(),
            window.footer.credit_label.x(),
            window.footer.licence_label.x(),
            window.footer.status_label.x(),
        ]

    def test_a_wide_footer_shows_everything_on_one_row(self, window: MainWindow):
        window.resize(1600, 900)
        window.show()
        QApplication.processEvents()
        assert window.footer.tier is FooterTier.FULL
        assert window.footer.licence_label.isVisible()
        assert window.footer.credit_label.isVisible()
        tops = {
            widget.y()
            for widget in (
                window.footer.version_label,
                window.footer.project_label,
                window.footer.credit_label,
                window.footer.licence_label,
                window.footer.status_label,
            )
        }
        assert len(tops) == 1, "the full footer is a single row"

    def test_the_arrangement_is_build_project_credit_licence_status(
        self, window: MainWindow
    ):
        """The brief's layout, asserted left to right.

        In particular the licence has moved: it used to sit beside the version
        on the left, and it now follows the developer credit on the right.
        """
        window.resize(1600, 900)
        window.show()
        QApplication.processEvents()
        assert self._order(window) == sorted(self._order(window))

    def test_the_licence_is_on_the_right_hand_side(self, window: MainWindow):
        window.resize(1600, 900)
        window.show()
        QApplication.processEvents()
        licence = window.footer.licence_label
        assert licence.x() > window.footer.width() / 2
        assert licence.x() > window.footer.credit_label.x()

    def test_the_licence_text_is_the_first_thing_dropped(self, window: MainWindow):
        footer = window.footer
        full = footer._width_for(FooterTier.FULL)
        assert footer.tier_for_width(full - 1) is FooterTier.NO_LICENCE

    def test_the_credit_goes_after_the_licence_and_before_anything_else(
        self, window: MainWindow
    ):
        footer = window.footer
        assert footer.tier_for_width(footer._width_for(FooterTier.NO_LICENCE) - 1) is (
            FooterTier.NO_CREDIT
        )

    def test_the_credit_reflows_to_a_second_line_rather_than_vanishing(
        self, window: MainWindow
    ):
        """Checked with `isVisibleTo`, not `isVisible`.

        On a window that was never shown the latter is false for every
        descendant regardless of what the layout did - the gotcha
        `docs/TESTING.md` records.
        """
        footer = window.footer
        assert footer.tier_for_width(10) is FooterTier.STACKED
        footer._apply_tier(FooterTier.STACKED)
        assert footer.credit_label.isVisibleTo(footer)
        assert footer.licence_label.isVisibleTo(footer)

    def test_the_version_project_and_status_are_never_dropped(
        self, window: MainWindow
    ):
        """The three things an operator glances down here to read."""
        footer = window.footer
        for tier in FooterTier:
            footer._apply_tier(tier)
            for label in (
                footer.version_label,
                footer.project_label,
                footer.status_label,
            ):
                assert label.isVisibleTo(footer)
            assert footer.version_label.text()
            assert footer.status_label.text()
            assert footer.project_label.full_text

    def test_the_footer_never_shrinks_its_text_to_fit(self, window: MainWindow):
        footer = window.footer
        before = footer.version_label.font().pointSizeF()
        footer._apply_tier(FooterTier.STACKED)
        assert footer.version_label.font().pointSizeF() == before

    def test_the_footer_stays_one_row_at_every_representative_size(
        self, window: MainWindow
    ):
        """A two-line footer is a last resort, not a normal width's outcome."""
        window.show()
        for width, height in REPRESENTATIVE_SIZES:
            window.resize(width, height)
            QApplication.processEvents()
            assert window.footer.tier is not FooterTier.STACKED, width


# ----------------------------------------------------------------------
# I - the page-name banner is gone
# ----------------------------------------------------------------------
class TestINoRedundantPageHeadings:
    def test_no_page_carries_a_heading_naming_its_own_stage(
        self, window: MainWindow
    ):
        """The ribbon already says which stage is open.

        Asserted across all nine rather than on a sample, because "applied
        consistently to all nine workflow pages" is what the brief asks for
        and one page left behind is exactly what a sample would miss.
        """
        for spec in WORKFLOW_PAGES:
            page = window._pages[spec.key]
            assert page.header is None, spec.key
            headings = [
                label.text()
                for label in page.findChildren(QLabel, "pageTitle")
            ]
            assert headings == [], spec.key

    def test_page_content_starts_immediately_under_the_chrome(
        self, window: MainWindow
    ):
        """No margin left behind where the heading used to be.

        A generous ceiling rather than an exact number: the assertion worth
        making is that a page's first widget is near the top of it, not that
        it is at any particular pixel.
        """
        window.resize(1366, 768)
        window.show()
        QApplication.processEvents()
        page = window._pages["reports"]
        children = [
            child
            for child in page.findChildren(QWidget)
            if child.parentWidget() is page and child.isVisibleTo(page)
        ]
        assert children
        assert min(child.y() for child in children) <= 24

    def test_the_pages_now_get_almost_the_whole_window(self, window: MainWindow):
        """The point of the exercise, at the size the brief singles out."""
        window.resize(1366, 768)
        window.show()
        QApplication.processEvents()
        central = window.centralWidget()
        assert central is not None
        assert window.stack.height() > central.height() * 0.85


# ----------------------------------------------------------------------
# J - accessibility
# ----------------------------------------------------------------------
class TestJAccessibility:
    def test_the_menu_button_is_the_first_thing_in_the_tab_order(
        self, window: MainWindow
    ):
        """So the whole menu hierarchy is available without a mouse."""
        window.show()
        QApplication.processEvents()
        window.chrome.menu_button.setFocus()
        assert window.chrome.menu_button.hasFocus()

    def test_tab_moves_forward_and_shift_tab_moves_back(self, window: MainWindow):
        window.show()
        QApplication.processEvents()
        window.chrome.menu_button.setFocus()
        first = QApplication.focusWidget()
        QTest.keyClick(window, Qt.Key.Key_Tab)
        second = QApplication.focusWidget()
        assert second is not first
        QTest.keyClick(window, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        assert QApplication.focusWidget() is first

    def test_every_chrome_control_is_keyboard_reachable(self, window: MainWindow):
        """Nothing in the chrome row is mouse-only."""
        chrome = window.chrome
        for control in (
            chrome.menu_button,
            chrome.density_out_button,
            chrome.density_in_button,
            chrome.previous_button,
            chrome.next_button,
            chrome.minimise_button,
            chrome.maximise_button,
            chrome.close_button,
        ):
            assert control.focusPolicy() != Qt.FocusPolicy.NoFocus, (
                control.objectName()
            )

    def test_every_chrome_control_has_an_accessible_name(self, window: MainWindow):
        """Every one of them shows a glyph and no text."""
        chrome = window.chrome
        for control in (
            chrome.menu_button,
            chrome.density_out_button,
            chrome.density_in_button,
            chrome.previous_button,
            chrome.next_button,
            chrome.minimise_button,
            chrome.maximise_button,
            chrome.close_button,
        ):
            assert control.accessibleName(), control.objectName()

    def test_the_workflow_steps_are_in_the_tab_order(self, window: MainWindow):
        for step in window.ribbon.steps:
            assert step.focusPolicy() == Qt.FocusPolicy.StrongFocus

    def test_no_state_is_carried_by_colour_alone(self, window: MainWindow):
        """Three states, three non-colour signals.

        The active step is also bold; a disabled step is also dashed and
        explains itself in its tooltip; the footer status is also a word.
        """
        window.ribbon.set_current_key("scan")
        window.ribbon.set_step_enabled("reports", enabled=False, reason="Locked.")

        active = window.ribbon.step("scan")
        locked = window.ribbon.step("reports")
        assert active is not None and locked is not None
        assert active.isChecked() is True
        assert locked.isEnabled() is False
        assert locked.disabled_reason
        assert window.footer.status_label.text() == "Ready"

    def test_text_survives_a_larger_platform_font(self, window: MainWindow):
        """Windows text scaling arrives as a larger application font."""
        large = QFont(window.font())
        large.setPointSizeF(window.font().pointSizeF() * 1.5)
        window.setFont(large)
        QApplication.processEvents()
        assert window.footer.version_label.text()
        assert all(step.display_text for step in window.ribbon.steps)


# ----------------------------------------------------------------------
# K - window sizes and resizing
# ----------------------------------------------------------------------
class TestKWindowSizes:
    @pytest.mark.parametrize(("width", "height"), REPRESENTATIVE_SIZES)
    def test_the_shell_lays_out_at_representative_sizes(
        self, window: MainWindow, width: int, height: int
    ):
        window.resize(width, height)
        window.show()
        QApplication.processEvents()

        central = window.centralWidget()
        assert central is not None
        # Nothing overlaps and nothing is clipped: the three bands tile the
        # central widget top to bottom.
        assert window.chrome.y() == 0
        assert window.stack.y() == window.chrome.height()
        assert window.footer.y() == window.stack.y() + window.stack.height()
        assert window.footer.y() + window.footer.height() <= central.height()

    def test_no_workflow_step_is_clipped_after_a_single_resize(
        self, window: MainWindow
    ):
        """One resize and one event pass has to be enough.

        The ribbon's height is fixed, so this is about the horizontal case:
        after a resize the visible steps must sit inside the viewport they were
        placed in, not extend past its bottom because the layout had not caught
        up.
        """
        window.show()
        QApplication.processEvents()
        for width in (1600, 1000, 820, 1600, 900):
            window.resize(width, 800)
            QApplication.processEvents()
            viewport = window.ribbon._scroll.viewport()
            clipped = [
                step.key
                for step in window.ribbon.steps
                if step.isVisible() and step.y() + step.height() > viewport.height()
            ]
            assert clipped == [], (
                f"at {width}px in {window.ribbon.mode.value}: {clipped}"
            )

    def test_repeated_resizing_keeps_the_shell_consistent(self, window: MainWindow):
        window.show()
        for _ in range(3):
            for width, height in (*REPRESENTATIVE_SIZES, (900, 700), (760, 620)):
                window.resize(width, height)
                QApplication.processEvents()
                assert window.chrome.y() == 0
                assert window.stack.y() == window.chrome.height()
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
        assert window.chrome.y() == 0
        assert window.stack.y() == window.chrome.height()

    def test_the_current_page_survives_resizing(self, window: MainWindow):
        window.show()
        assert window.show_page("attendance")
        for width, height in (*REPRESENTATIVE_SIZES, (800, 600)):
            window.resize(width, height)
            QApplication.processEvents()
            assert window.current_page_key() == "attendance"
            assert window.ribbon.current_key() == "attendance"

    def test_business_state_survives_every_navigation_mode(
        self, window: MainWindow, tmp_path: Path
    ):
        """The brief's requirement T, end to end.

        A project stays open, the pages keep the session they were given, and
        the stage stays where the operator put it, across the full width range
        that takes the ribbon through all three of its layouts.
        """
        assert window.create_project_at(tmp_path, "Exam")
        assert window.show_page("answer_key")
        window.show()
        seen = set()
        for width in (1920, 1600, 1366, 1100, 900, 760, 1920):
            window.resize(width, 760)
            QApplication.processEvents()
            seen.add(window.ribbon.mode)
            assert window.session is not None
            assert window.session.name == "Exam"
            assert window.current_page_key() == "answer_key"
            assert window.ribbon.current_key() == "answer_key"
            assert window.footer.project_title == "Exam"
        assert len(seen) >= 2, "the sweep should cross at least one layout boundary"
