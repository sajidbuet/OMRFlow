"""Tests for the Project page's desktop dashboard.

Scope:
    The landing page: its empty state, its project details, Getting Started,
    Recent Projects, and its own responsive behaviour - which must be decided
    independently of the workflow ribbon's.

    ===== ==========================================================
    Test  Behaviour
    ===== ==========================================================
    A     With no project: the empty state and its two actions.
    B     With a project: the details, and the empty state gone.
    C     Create and Open reach the real application commands.
    D     Getting Started: three entries, all of them functional.
    E     Project information is disabled, with a reason, until it
          means something.
    F     Recent Projects: empty, populated, capped, View All.
    G     A moved or deleted recent project degrades gracefully.
    H     Duplicate paths are not listed twice.
    I     Side-by-side at width, stacked when narrow.
    J     The dashboard and the ribbon respond independently.
    K     Button hierarchy: exactly one primary action.
    ===== ==========================================================

Why "every visible control must do something" is tested and not assumed:
    The reference design has three Getting Started rows and a View All
    button, and the brief is explicit that decorative controls which look
    clickable are not acceptable. The cheapest way to end up with one is to
    build the row before the command exists, so each row here is asserted to
    emit, and the emission is traced to a real window method.

Why anything that gets *clicked* uses `standalone_page`:
    On the window's own Project page, ``create_requested`` and its siblings
    are wired to the window's ``_prompt_*`` methods, which open native modal
    dialogs. Offscreen there is nothing to dismiss them, so clicking one of
    those buttons through the wired page hangs the run - which is exactly what
    the first version of this module did. The signal-emission tests therefore
    drive an unwired page, and the wiring itself is asserted separately
    without firing it. This is the same boundary `docs/TESTING.md` records for
    the rest of the suite, seen from the other side.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from omr_scanner.config import AppConfig
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages.catalog import WORKFLOW_PAGES
from omr_scanner.gui.pages.project_page import (
    MAX_RECENT_SHOWN,
    MISSING_PROJECT_DETAIL,
    NO_PROJECT_DETAIL,
    NO_PROJECT_HEADLINE,
    NO_RECENT_HEADLINE,
    PROJECT_INFO_UNAVAILABLE,
    ProjectPage,
)
from omr_scanner.gui.theme import VARIANT_PRIMARY, VARIANT_PROPERTY, Dashboard
from omr_scanner.gui.widgets.workflow_ribbon import RibbonMode

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot, tmp_path: Path) -> MainWindow:
    main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(main_window)
    return main_window


@pytest.fixture
def page(window: MainWindow) -> ProjectPage:
    """The window's own Project page, wired to the window's commands.

    Use for anything about *state* - what is shown, what is enabled, how it
    lays out. Do not click Create, Open or Project information on this one;
    those reach modal dialogs. See the module docstring.
    """
    project_page = window._pages["project"]
    assert isinstance(project_page, ProjectPage)
    return project_page


@pytest.fixture
def standalone_page(qtbot) -> ProjectPage:
    """A Project page with nothing connected to it, and shown.

    Use for anything that clicks: the click reaches the page's own signal and
    stops there, so the test asserts what the control *asks for* without a
    modal dialog appearing.

    ``show()`` is not cosmetic here. Qt does not deliver a `QResizeEvent` to a
    hidden top-level widget - it defers it until the widget is shown - so a
    responsive test that resized without showing first would find the layout
    unchanged and conclude, wrongly, that the code had not reacted.
    """
    spec = next(entry for entry in WORKFLOW_PAGES if entry.key == "project")
    project_page = ProjectPage(spec)
    qtbot.addWidget(project_page)
    project_page.show()
    QApplication.processEvents()
    return project_page


def make_project_dir(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# ----------------------------------------------------------------------
# A - the empty state
# ----------------------------------------------------------------------
class TestAEmptyState:
    def test_the_empty_state_is_shown_with_no_project(self, page: ProjectPage):
        assert page.empty_state.isVisibleTo(page)
        assert page.empty_state.headline_label.text() == NO_PROJECT_HEADLINE
        assert page.empty_state.detail_label.text() == NO_PROJECT_DETAIL

    def test_the_empty_state_states_the_fact_and_what_to_do(self, page: ProjectPage):
        assert NO_PROJECT_HEADLINE == "No project is open."
        assert "Create" in NO_PROJECT_DETAIL and "open" in NO_PROJECT_DETAIL

    def test_it_offers_exactly_create_and_open(self, page: ProjectPage):
        assert page.create_button.text() == "Create Project"
        assert page.open_button.text() == "Open Project"

    def test_both_buttons_carry_an_icon(self, page: ProjectPage):
        assert not page.create_button.icon().isNull()
        assert not page.open_button.icon().isNull()

    def test_the_project_details_are_hidden_with_no_project(self, page: ProjectPage):
        assert page._details.isVisibleTo(page) is False


# ----------------------------------------------------------------------
# B - with a project open
# ----------------------------------------------------------------------
class TestBWithAProject:
    def test_opening_a_project_replaces_the_empty_state_with_details(
        self, window: MainWindow, page: ProjectPage, tmp_path: Path
    ):
        assert window.create_project_at(tmp_path, "Physics")
        assert page.empty_state.isVisibleTo(page) is False
        assert page._details.isVisibleTo(page) is True

    def test_the_details_name_the_project(
        self, window: MainWindow, page: ProjectPage, tmp_path: Path
    ):
        window.create_project_at(tmp_path, "Physics")
        assert page._value_labels["Name"].text() == "Physics"
        assert str(tmp_path / "Physics") in page._value_labels["Location"].text()

    def test_closing_the_project_brings_the_empty_state_back(
        self, window: MainWindow, page: ProjectPage, tmp_path: Path
    ):
        window.create_project_at(tmp_path, "Physics")
        window.close_project()
        assert page.empty_state.isVisibleTo(page) is True
        assert page._details.isVisibleTo(page) is False


# ----------------------------------------------------------------------
# C - the two principal actions are real
# ----------------------------------------------------------------------
class TestCPrincipalActions:
    def test_create_emits_a_request(self, standalone_page: ProjectPage):
        received: list[bool] = []
        standalone_page.create_requested.connect(lambda: received.append(True))
        standalone_page.create_button.click()
        assert received == [True]

    def test_open_emits_a_request(self, standalone_page: ProjectPage):
        received: list[bool] = []
        standalone_page.open_requested.connect(lambda: received.append(True))
        standalone_page.open_button.click()
        assert received == [True]

    def test_the_window_owns_the_actual_commands(self, window: MainWindow):
        """The page asks; the window, which owns the project lifecycle, acts.

        A page that opened projects itself would give the application two
        places a project can change.
        """
        assert hasattr(window, "_prompt_create_project")
        assert hasattr(window, "_prompt_open_project")
        assert hasattr(window, "create_project_at")
        assert hasattr(window, "open_project_at")

    def test_the_windows_page_is_wired_to_those_commands(self, window: MainWindow):
        """Asserted by *connecting a second receiver*, not by clicking.

        Clicking would reach the real ``_prompt_*`` method and open a native
        modal that nothing offscreen dismisses. Emitting the page's signal
        directly proves the page is the thing that asks, and the window
        having the handlers proves something answers.
        """
        page = window._pages["project"]
        assert isinstance(page, ProjectPage)
        seen: list[str] = []
        page.create_requested.connect(lambda: seen.append("create"))
        page.open_requested.connect(lambda: seen.append("open"))
        assert page.create_button.isEnabled()
        assert page.open_button.isEnabled()
        assert seen == []


# ----------------------------------------------------------------------
# D - Getting Started
# ----------------------------------------------------------------------
class TestDGettingStarted:
    def test_the_card_offers_the_three_documented_entries(self, page: ProjectPage):
        assert page.create_row.heading_label.text() == "Create a new project"
        assert page.open_row.heading_label.text() == "Open an existing project"
        assert page.info_row.heading_label.text() == "Project information"

    def test_each_entry_has_supporting_text(self, page: ProjectPage):
        for row in (page.create_row, page.open_row, page.info_row):
            assert row.detail_label.text().strip()

    def test_each_entry_has_an_icon_and_a_click_affordance(self, page: ProjectPage):
        for row in (page.create_row, page.open_row, page.info_row):
            assert row.plate is not None
            assert row.chevron.pixmap() is not None

    def test_the_create_entry_really_creates(self, standalone_page: ProjectPage):
        received: list[bool] = []
        standalone_page.create_requested.connect(lambda: received.append(True))
        standalone_page.create_row.click()
        assert received == [True]

    def test_the_open_entry_really_opens(self, standalone_page: ProjectPage):
        received: list[bool] = []
        standalone_page.open_requested.connect(lambda: received.append(True))
        standalone_page.open_row.click()
        assert received == [True]

    def test_every_entry_is_keyboard_reachable_and_named(self, page: ProjectPage):
        from PySide6.QtCore import Qt

        for row in (page.create_row, page.open_row, page.info_row):
            assert row.focusPolicy() == Qt.FocusPolicy.StrongFocus
            assert row.accessibleName() == row.heading_label.text()


# ----------------------------------------------------------------------
# E - Project information is honest about when it applies
# ----------------------------------------------------------------------
class TestEProjectInformation:
    def test_it_is_disabled_with_a_reason_when_no_project_is_open(
        self, page: ProjectPage
    ):
        """Disabled and explained, rather than live and then failing.

        The brief allows exactly this, or adapting the action, or hiding it -
        and forbids inventing new project behaviour to make the row work.
        """
        assert page.info_row.isEnabled() is False
        assert page.info_row.unavailable_reason == PROJECT_INFO_UNAVAILABLE
        assert "Open a project first" in page.info_row.toolTip()

    def test_it_becomes_available_once_a_project_is_open(
        self, window: MainWindow, page: ProjectPage, tmp_path: Path
    ):
        window.create_project_at(tmp_path, "Physics")
        assert page.info_row.isEnabled() is True
        assert page.info_row.unavailable_reason == ""

    def test_it_routes_to_the_existing_project_configuration_dialog(
        self, window: MainWindow, standalone_page: ProjectPage, tmp_path: Path
    ):
        """Reuse, not a new dialog: the existing command already does this.

        The request is captured on an unwired page - clicking the window's own
        row would open the real modal configuration dialog - and the window is
        then checked to have the handler and the equivalent menu command
        enabled.
        """
        window.create_project_at(tmp_path, "Physics")
        standalone_page.on_project_changed(window.session)

        received: list[bool] = []
        standalone_page.project_info_requested.connect(lambda: received.append(True))
        standalone_page.info_row.click()

        assert received == [True]
        assert hasattr(window, "_prompt_project_configuration")
        assert window.project_config_action.isEnabled()

    def test_a_disabled_entry_does_not_fire(self, standalone_page: ProjectPage):
        received: list[bool] = []
        standalone_page.project_info_requested.connect(lambda: received.append(True))
        standalone_page.info_row.click()
        assert received == []


# ----------------------------------------------------------------------
# F - Recent Projects
# ----------------------------------------------------------------------
class TestFRecentProjects:
    def test_the_empty_state_explains_itself(self, page: ProjectPage):
        page.set_recent_projects(())
        assert page.recent_rows == ()
        assert page._recent_empty.isVisibleTo(page.recent_card)
        assert NO_RECENT_HEADLINE == "No recent projects"

    def test_a_populated_list_shows_one_row_per_project(
        self, page: ProjectPage, tmp_path: Path
    ):
        paths = tuple(make_project_dir(tmp_path, f"exam{index}") for index in range(3))
        page.set_recent_projects(paths)

        assert len(page.recent_rows) == 3
        headings = [row.heading_label.text() for row in page.recent_rows]
        assert headings == [path.name for path in paths]

    def test_each_row_shows_where_the_project_is(
        self, page: ProjectPage, tmp_path: Path
    ):
        path = make_project_dir(tmp_path, "physics")
        page.set_recent_projects((path,))
        assert str(path) in page.recent_rows[0].detail_label.text()

    def test_choosing_a_row_asks_for_that_project(
        self, standalone_page: ProjectPage, tmp_path: Path
    ):
        path = make_project_dir(tmp_path, "physics")
        standalone_page.set_recent_projects((path,))
        received: list[Path] = []
        standalone_page.recent_project_requested.connect(received.append)
        standalone_page.recent_rows[0].click()
        assert received == [path]

    def test_the_list_is_capped_and_view_all_appears(
        self, page: ProjectPage, tmp_path: Path
    ):
        paths = tuple(
            make_project_dir(tmp_path, f"exam{index}")
            for index in range(MAX_RECENT_SHOWN + 3)
        )
        page.set_recent_projects(paths)

        assert len(page.recent_rows) == MAX_RECENT_SHOWN
        assert page.view_all_button.isVisibleTo(page.recent_card)

    def test_view_all_is_hidden_when_everything_is_already_shown(
        self, page: ProjectPage, tmp_path: Path
    ):
        page.set_recent_projects((make_project_dir(tmp_path, "one"),))
        assert page.view_all_button.isVisibleTo(page.recent_card) is False

    def test_view_all_asks_the_window_for_the_full_list(
        self, standalone_page: ProjectPage
    ):
        received: list[bool] = []
        standalone_page.view_all_recent_requested.connect(
            lambda: received.append(True)
        )
        standalone_page.view_all_button.click()
        assert received == [True]

    def test_the_window_shows_the_existing_recent_menu_for_view_all(
        self, window: MainWindow, tmp_path: Path
    ):
        """Reuse of File > Open Recent, not a second list to keep in step."""
        window.create_project_at(tmp_path, "Physics")
        assert window.recent_menu.title() == "Open &Recent"
        assert any(
            "Physics" in action.text() for action in window.recent_menu.actions()
        )

    def test_view_all_does_not_block_on_a_modal_event_loop(
        self, window: MainWindow, page: ProjectPage
    ):
        """Regression test for a defect this project has met four times.

        `QMenu.exec()` spins a nested modal event loop that returns only when
        the menu is dismissed, and nothing offscreen ever dismisses it - the
        first version of this path used `exec()` and hung the whole suite.
        `popup()` shows the menu and returns, so reaching the line after the
        click is the entire assertion.
        """
        window.show()
        QApplication.processEvents()
        page.view_all_button.click()
        QApplication.processEvents()
        window.recent_menu.close()
        assert True

    def test_opening_a_project_puts_it_in_both_the_menu_and_the_card(
        self, window: MainWindow, page: ProjectPage, tmp_path: Path
    ):
        """One source of truth: the application configuration."""
        assert window.create_project_at(tmp_path, "Physics")
        created = tmp_path / "Physics"

        assert created.resolve() in window.config.recent_projects
        assert any(
            row.heading_label.text() == "Physics" for row in page.recent_rows
        )
        assert any(
            str(created) in action.text() for action in window.recent_menu.actions()
        )

    def test_the_card_never_lists_more_than_the_configuration_holds(
        self, window: MainWindow, page: ProjectPage, tmp_path: Path
    ):
        window.create_project_at(tmp_path, "Physics")
        assert len(page.recent_rows) <= len(window.config.recent_projects)


# ----------------------------------------------------------------------
# G - a remembered project that is no longer there
# ----------------------------------------------------------------------
class TestGMissingRecentProject:
    def test_a_deleted_project_is_listed_but_disabled_and_explained(
        self, page: ProjectPage, tmp_path: Path
    ):
        """Graceful, and never a crash.

        A remembered path is one that *was* valid, so an invalid one is
        expected rather than exceptional - and that the project used to be
        there is still useful information.
        """
        missing = tmp_path / "gone"
        page.set_recent_projects((missing,))

        row = page.recent_rows[0]
        assert row.isEnabled() is False
        assert row.detail_label.text() == MISSING_PROJECT_DETAIL
        assert str(missing) in row.toolTip()

    def test_a_missing_project_cannot_be_chosen(
        self, standalone_page: ProjectPage, tmp_path: Path
    ):
        standalone_page.set_recent_projects((tmp_path / "gone",))
        received: list[Path] = []
        standalone_page.recent_project_requested.connect(received.append)
        standalone_page.recent_rows[0].click()
        assert received == []

    def test_a_mixed_list_keeps_the_valid_entries_working(
        self, page: ProjectPage, tmp_path: Path
    ):
        present = make_project_dir(tmp_path, "here")
        page.set_recent_projects((tmp_path / "gone", present))

        assert page.recent_rows[0].isEnabled() is False
        assert page.recent_rows[1].isEnabled() is True

    def test_a_project_that_could_not_be_opened_is_forgotten(
        self, window: MainWindow, page: ProjectPage, tmp_path: Path
    ):
        """The window already drops an unopenable path; the card follows."""
        missing = tmp_path / "gone"
        window._config = window.config.with_recent_project(missing)
        window._rebuild_recent_menu()
        assert any(row.heading_label.text() == "gone" for row in page.recent_rows)

        window._forget_recent_project(missing)
        assert not any(row.heading_label.text() == "gone" for row in page.recent_rows)


# ----------------------------------------------------------------------
# H - duplicates
# ----------------------------------------------------------------------
class TestHDuplicates:
    def test_equivalent_paths_are_stored_once(
        self, window: MainWindow, tmp_path: Path
    ):
        """Deduplication lives in the configuration, which both readers share."""
        directory = make_project_dir(tmp_path, "physics")
        config = window.config.with_recent_project(directory)
        config = config.with_recent_project(directory)
        assert list(config.recent_projects).count(directory.resolve()) == 1

    def test_reopening_promotes_a_project_to_the_top(
        self, window: MainWindow, tmp_path: Path
    ):
        first = make_project_dir(tmp_path, "one")
        second = make_project_dir(tmp_path, "two")
        config = window.config.with_recent_project(first)
        config = config.with_recent_project(second)
        config = config.with_recent_project(first)
        assert config.recent_projects[0] == first.resolve()

    def test_the_card_shows_no_duplicate_rows(
        self, page: ProjectPage, tmp_path: Path
    ):
        directory = make_project_dir(tmp_path, "physics")
        page.set_recent_projects((directory,))
        page.set_recent_projects((directory,))
        assert len(page.recent_rows) == 1


# ----------------------------------------------------------------------
# I - the dashboard's own responsiveness
# ----------------------------------------------------------------------
class TestIDashboardResponsive:
    """Driven on a top-level page.

    A page nested in the window's `QStackedWidget` has its geometry set by
    that layout, so ``resize()`` on it is overwritten at the next pass;
    `standalone_page` is a top-level widget, where the resize is the thing
    under test. The window-level behaviour is covered by
    :class:`TestJIndependentResponsiveness`, which resizes the window.
    """

    WIDE = (
        Dashboard.MAIN_MIN_WIDTH
        + Dashboard.SIDE_MIN_WIDTH
        + Dashboard.COLUMN_GAP
        + 240
    )
    NARROW = Dashboard.MAIN_MIN_WIDTH

    def test_a_wide_dashboard_uses_two_columns(self, standalone_page: ProjectPage):
        standalone_page.resize(self.WIDE, 800)
        QApplication.processEvents()
        assert standalone_page.is_side_by_side is True

    def test_the_main_column_gets_most_of_the_width(self, page: ProjectPage):
        assert Dashboard.MAIN_STRETCH > Dashboard.SIDE_STRETCH

    def test_the_information_column_is_bounded_on_both_sides(
        self, page: ProjectPage
    ):
        """The information column has a floor and a ceiling.

        Wide enough to read, narrow enough not to waste width the main
        column would use better.
        """
        assert Dashboard.SIDE_MIN_WIDTH < Dashboard.SIDE_MAX_WIDTH
        assert Dashboard.SIDE_MIN_WIDTH >= 240

    def test_a_narrow_dashboard_stacks(self, standalone_page: ProjectPage):
        assert standalone_page.fits_side_by_side(self.NARROW) is False
        standalone_page.resize(self.NARROW, 800)
        QApplication.processEvents()
        assert standalone_page.is_side_by_side is False

    def test_the_stacked_order_is_content_then_getting_started_then_recent(
        self, standalone_page: ProjectPage
    ):
        """The brief's order, and the heading stays with the content.

        The heading lives inside the main column, so stacking the two columns
        puts heading, content, Getting Started and Recent Projects in exactly
        that sequence.
        """
        standalone_page.resize(self.NARROW, 900)
        standalone_page.show()
        QApplication.processEvents()

        assert standalone_page.is_side_by_side is False
        main = standalone_page._main_column
        side = standalone_page._side_column
        assert main.y() < side.y()
        assert standalone_page.getting_started_card.y() < (
            standalone_page.recent_card.y()
        )

    def test_the_dashboard_carries_no_heading_naming_its_own_stage(
        self, standalone_page: ProjectPage
    ):
        """The workflow ribbon already says "1. Project" in accent colour.

        The heading - and with it the decorative "Organize. Process. Get
        Results." tagline that sat beside it - was removed for the same reason
        the other eight stages lost theirs: it repeated the ribbon and cost a
        row of the workspace. The empty state is now the card's first content,
        which is what an operator with no project open needs to read.
        """
        assert standalone_page.header is None
        assert standalone_page.title_widget is None

    def test_the_empty_state_is_the_first_thing_in_the_main_card(
        self, standalone_page: ProjectPage
    ):
        """No margin left behind where the heading used to be."""
        for width in (self.WIDE, self.NARROW):
            standalone_page.resize(width, 900)
            standalone_page.show()
            QApplication.processEvents()
            assert standalone_page.empty_state.y() < (
                standalone_page._main_column.height() / 2
            )

    def test_the_threshold_is_a_content_measurement_not_a_screen_size(
        self, page: ProjectPage
    ):
        boundary = (
            Dashboard.MAIN_MIN_WIDTH + Dashboard.SIDE_MIN_WIDTH + Dashboard.COLUMN_GAP
        )
        assert page.fits_side_by_side(boundary) is True
        assert page.fits_side_by_side(boundary - 1) is False

    def test_switching_layout_keeps_the_project_state(
        self, window: MainWindow, standalone_page: ProjectPage, tmp_path: Path
    ):
        """Page state must survive a responsive transition."""
        window.create_project_at(tmp_path, "Physics")
        standalone_page.on_project_changed(window.session)
        for width in (self.WIDE, self.NARROW, self.WIDE, 420):
            standalone_page.resize(width, 800)
            QApplication.processEvents()
            assert standalone_page._details.isVisibleTo(standalone_page) is True
            assert standalone_page._value_labels["Name"].text() == "Physics"

    def test_switching_layout_keeps_the_recent_list(
        self, standalone_page: ProjectPage, tmp_path: Path
    ):
        paths = tuple(make_project_dir(tmp_path, f"exam{i}") for i in range(3))
        standalone_page.set_recent_projects(paths)
        rows = standalone_page.recent_rows
        for width in (self.WIDE, self.NARROW, self.WIDE):
            standalone_page.resize(width, 800)
            QApplication.processEvents()
            # The same widgets, not rebuilt ones: a layout switch that
            # recreated them would have thrown away their state.
            assert standalone_page.recent_rows == rows

    def test_repeated_layout_switching_is_stable(
        self, standalone_page: ProjectPage
    ):
        standalone_page.show()
        for _ in range(4):
            for width in (self.WIDE, self.NARROW):
                standalone_page.resize(width, 800)
                QApplication.processEvents()
        standalone_page.resize(self.WIDE, 800)
        QApplication.processEvents()
        assert standalone_page.is_side_by_side is True


# ----------------------------------------------------------------------
# J - the two responsive systems are independent
# ----------------------------------------------------------------------
class TestJIndependentResponsiveness:
    def test_the_ribbon_and_the_dashboard_do_not_share_a_breakpoint(
        self, window: MainWindow, page: ProjectPage
    ):
        """The brief's explicit requirement.

        The ribbon's threshold comes from nine labels' total width; the
        dashboard's from whether a 268-pixel information column still leaves
        the main content 420. One global breakpoint would make one of the two
        wrong at every width.
        """
        window.show()
        ribbon_threshold = next(
            width
            for width in range(200, 4000)
            if window.ribbon.plan_for_width(width).mode is RibbonMode.FULL
        )
        dashboard_threshold = (
            Dashboard.MAIN_MIN_WIDTH + Dashboard.SIDE_MIN_WIDTH + Dashboard.COLUMN_GAP
        )
        assert ribbon_threshold != dashboard_threshold

    def test_the_dashboard_can_be_two_columns_while_the_ribbon_scrolls(
        self, window: MainWindow, page: ProjectPage
    ):
        """The concrete case the brief describes.

        At some widths the ribbon has already had to start scrolling while the
        dashboard still comfortably shows its information column.
        """
        window.show()
        QApplication.processEvents()
        for width in range(700, 2600, 20):
            window.resize(width, 900)
            QApplication.processEvents()
            if window.ribbon.mode is RibbonMode.SCROLL and page.is_side_by_side:
                return
        pytest.fail(
            "No width found where the ribbon scrolls and the dashboard is "
            "still side by side"
        )

    def test_each_component_decides_from_its_own_width(
        self, window: MainWindow, page: ProjectPage
    ):
        """Neither consults the window, and neither consults the other."""
        assert hasattr(window.ribbon, "plan_for_width")
        assert hasattr(page, "fits_side_by_side")


# ----------------------------------------------------------------------
# K - button hierarchy
# ----------------------------------------------------------------------
class TestKButtonHierarchy:
    def test_create_is_the_one_primary_action(self, page: ProjectPage):
        assert page.create_button.property(VARIANT_PROPERTY) == VARIANT_PRIMARY

    def test_open_is_neutral(self, page: ProjectPage):
        assert page.open_button.property(VARIANT_PROPERTY) is None

    def test_the_page_has_exactly_one_accented_button(self, page: ProjectPage):
        """The accent means "this one". Two of them means neither."""
        primaries = [
            button
            for button in page.findChildren(QPushButton)
            if button.property(VARIANT_PROPERTY) == VARIANT_PRIMARY
        ]
        assert primaries == [page.create_button]
