"""The Results stage's Dashboard tab, on a real scored multi-set project.

The project comes from :mod:`tests.analytics_fixtures`: three sets with
different keys, one batch, real reconciliation and scoring, plus a fourth set
defined with nobody in it. The dashboard is driven through its widgets and
its figures are checked against the Results tab's own rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QTableWidget
from tests.analytics_fixtures import OPERATOR, build_dashboard_exam

from omr_scanner.domain.scoring import ScoringPolicy
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.results.dashboard import NO_RESULTS_TEXT
from omr_scanner.gui.results.page import ResultsPage
from omr_scanner.services import create_project, scoring_store
from omr_scanner.services import result_analytics as ra

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.gui

SIZES = {"10": 20, "11": 24, "12": 28}
QUESTIONS = 40
TIMEOUT_MS = 20_000


@pytest.fixture
def exam(workspace: Path, tmp_path: Path):
    session = create_project(workspace, "Dashboard GUI")
    try:
        built = build_dashboard_exam(
            session, tmp_path, sizes=SIZES, question_count=QUESTIONS, empty_sets=("13",)
        )
        yield session, built
    finally:
        if not session.is_closed:
            session.close()


def make_page(qtbot, session, built) -> ResultsPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "results")
    page = ResultsPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(session)
    page.set_reviewer(OPERATOR)
    page.set_template(built.template)
    page.set_batch(built.batch_id)
    page.resize(1366, 768)
    page.show()
    qtbot.waitExposed(page)
    return page


def open_dashboard(qtbot, page: ResultsPage) -> None:
    with qtbot.waitSignal(page.dashboard.computed, timeout=TIMEOUT_MS):
        page.tabs.setCurrentWidget(page.dashboard)


@pytest.fixture
def page(qtbot, exam):
    session, built = exam
    page = make_page(qtbot, session, built)
    open_dashboard(qtbot, page)
    yield page
    page.shutdown()
    page.close()


def kpi(page: ResultsPage, key: str) -> str:
    return page.dashboard.kpi_cards[key].value.text()


# ----------------------------------------------------------------------
class TestTabs:
    def test_the_dashboard_tab_exists(self, qtbot, exam):
        session, built = exam
        page = make_page(qtbot, session, built)
        assert page.tabs.count() == 2
        assert [page.tabs.tabText(i) for i in range(2)] == ["Results", "Dashboard"]
        page.close()

    def test_the_results_tab_is_first_and_unchanged(self, qtbot, exam):
        session, built = exam
        page = make_page(qtbot, session, built)
        assert page.tabs.currentIndex() == 0
        results_tab = page.tabs.widget(0)
        for widget in (
            page.table, page.detail_table, page.filter_combo, page.search_box,
            page.score_button, page.check_button, page.configure_button,
            page.summary_label, page.policy_label,
        ):
            assert results_tab.isAncestorOf(widget)
            assert not page.dashboard.isAncestorOf(widget)
        assert page.table.rowCount() == sum(len(d.rolls) for d in built.sets.values())
        assert page.score_button.isVisible() and page.score_button.isEnabled()
        page.close()

    def test_the_dashboard_computes_nothing_until_it_is_opened(self, qtbot, exam):
        session, built = exam
        page = make_page(qtbot, session, built)
        assert page.dashboard.analytics is None
        page.close()

    def test_scoring_still_works_with_the_dashboard_present(
        self, qtbot, page: ResultsPage, exam
    ):
        _, built = exam
        page.tabs.setCurrentIndex(0)
        with qtbot.waitSignal(page.scored, timeout=TIMEOUT_MS):
            assert page.score_batch() is True
        assert page.state.counts.scored == sum(built.scored.values())


# ----------------------------------------------------------------------
class TestScope:
    def test_the_default_scope_is_overall(self, page: ResultsPage):
        combo = page.dashboard.scope_combo
        assert combo.currentData() is None
        assert combo.currentText().startswith("Overall Exam")

    def test_every_defined_set_is_offered(self, page: ResultsPage):
        combo = page.dashboard.scope_combo
        keys = [combo.itemData(i) for i in range(combo.count())]
        assert keys == [None, "10", "11", "12", "13"]

    def test_overall_counts_reconcile_with_the_results_tab(self, page: ResultsPage):
        assert kpi(page, "count") == str(page.state.counts.scored)

    def test_a_set_scope_recalculates_and_overall_restores(self, page: ResultsPage, exam):
        _, built = exam
        overall = (kpi(page, "count"), kpi(page, "mean"))
        assert page.dashboard.select_scope("11") is True
        assert kpi(page, "count") == str(built.scored["11"])
        assert kpi(page, "count") == str(page.state.counts.by_set["11"])
        assert (kpi(page, "count"), kpi(page, "mean")) != overall
        assert page.dashboard.select_scope(None) is True
        assert (kpi(page, "count"), kpi(page, "mean")) == overall

    def test_an_empty_set_says_so(self, page: ResultsPage):
        assert page.dashboard.select_scope("13") is True
        assert page.dashboard.message_label.isVisible()
        assert "No scored scripts for Set 13." in page.dashboard.message_label.text()
        assert not page.dashboard.figures.isVisible()
        assert page.dashboard.select_scope("10") is True
        assert page.dashboard.figures.isVisible()


# ----------------------------------------------------------------------
class TestCharts:
    def test_the_histogram_renders(self, page: ResultsPage):
        chart = page.dashboard.histogram
        assert chart.isVisible()
        assert chart.item_count() > 0
        assert sum(bar.count for bar in chart.bars) == page.state.counts.scored
        assert not chart.grab().isNull()

    def test_overall_question_analysis_is_per_set_only(self, page: ResultsPage):
        assert not page.dashboard.question_panels.isVisible()
        assert "per set" in page.dashboard.question_note.text()
        assert page.dashboard.comparison_box.isVisible()
        assert page.dashboard.comparison_table.rowCount() == 4

    def test_a_set_scope_shows_every_question(self, page: ResultsPage):
        page.dashboard.select_scope("10")
        board = page.dashboard
        assert board.question_panels.isVisible()
        for chart in (board.correct_chart, board.discrimination_chart, board.outcome_chart):
            assert chart.item_count() == QUESTIONS
            assert not chart.grab().isNull()
        assert not board.comparison_box.isVisible()
        assert board.easiest_table.rowCount() == ra.DEFAULT_SETTINGS.ranked_count

    def test_the_designed_negative_item_is_flagged(self, page: ResultsPage, exam):
        _, built = exam
        page.dashboard.select_scope("12")
        table: QTableWidget = page.dashboard.flags_table
        rows = {
            (table.item(r, 0).text(), table.item(r, 1).text()) for r in range(table.rowCount())
        }
        number = built.designed["negative_discrimination"]
        assert (f"Q{number}", ra.FlagKind.NEGATIVE_DISCRIMINATION.label) in rows

    def test_choosing_a_question_updates_the_distractor_panel(self, page: ResultsPage, exam):
        _, built = exam
        board = page.dashboard
        board.select_scope("10")
        assert board.select_question(5) is True
        assert board.selected_question == 5
        assert "<b>Q5</b>" in board.option_label.text()
        key = built.sets["10"].key[4]
        assert f"Correct answer: <b>{key}</b>" in board.option_label.text()
        labels = [bar.label for bar in board.option_chart.bars]
        assert labels[:4] == ["A", "B", "C", "D"] and "Blank" in labels
        assert [bar.label for bar in board.option_chart.bars if bar.is_key] == [key]

    def test_clicking_a_bar_selects_its_question(self, qtbot, page: ResultsPage):
        board = page.dashboard
        board.select_scope("10")
        chart = board.correct_chart
        board.scroll_area.ensureWidgetVisible(chart)
        plot = chart.plot_rect()
        slot = plot.width() / chart.item_count()
        point = QPoint(int(plot.left() + slot * 9.5), int(plot.center().y()))
        qtbot.mouseClick(chart, Qt.MouseButton.LeftButton, pos=point)
        assert board.selected_question == 10

    def test_clicking_a_review_row_selects_its_question(self, qtbot, page: ResultsPage):
        board = page.dashboard
        board.select_scope("12")
        table = board.flags_table
        number = table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        board._on_table_question(table, 0)
        assert board.selected_question == number

    def test_the_question_tooltip_gives_exact_values(self, page: ResultsPage):
        page.dashboard.select_scope("10")
        text = page.dashboard.correct_chart.tooltip_for(0)
        for part in ("Question 1", "Correct:", "Incorrect:", "Blank:", "Responses:",
                     "Discrimination:"):
            assert part in text


# ----------------------------------------------------------------------
class TestStatesAndFailures:
    def test_no_scored_results(self, qtbot, workspace, tmp_path):
        session = create_project(workspace, "Dashboard unscored")
        try:
            built = build_dashboard_exam(
                session, tmp_path, sizes={"10": 5}, question_count=10, score=False
            )
            page = make_page(qtbot, session, built)
            page.tabs.setCurrentWidget(page.dashboard)
            # Nothing scored: no inputs, so nothing to compute in the background.
            assert page.dashboard.analytics is None
            assert NO_RESULTS_TEXT in page.dashboard.message_label.text()
            assert not page.dashboard.figures.isVisible()
            page.shutdown()
            page.close()
        finally:
            session.close()

    def test_stale_results_are_announced(self, qtbot, page: ResultsPage):
        scoring_store.save_policy(page.database, ScoringPolicy(correct_mark=2))
        with qtbot.waitSignal(page.dashboard.computed, timeout=TIMEOUT_MS):
            page.refresh_table()
        assert page.dashboard.notice_label.isVisible()
        assert "need recalculating" in page.dashboard.notice_label.text()

    def test_a_failure_stays_on_the_dashboard(self, qtbot, exam, monkeypatch):
        session, built = exam

        def broken(*_args: object, **_kwargs: object) -> ra.ExamAnalytics:
            raise RuntimeError("boom")

        monkeypatch.setattr(ra, "analyse", broken)
        page = make_page(qtbot, session, built)
        page.tabs.setCurrentWidget(page.dashboard)
        qtbot.waitUntil(lambda: bool(page.dashboard.error), timeout=TIMEOUT_MS)
        assert "RuntimeError" in page.dashboard.message_label.text()
        assert "boom" not in page.dashboard.message_label.text()
        page.tabs.setCurrentIndex(0)
        page.refresh_table()
        assert page.table.rowCount() > 0 and page.score_button.isEnabled()
        page.shutdown()
        page.close()

    def test_returning_with_nothing_changed_recomputes_nothing(self, qtbot, page: ResultsPage):
        generation = page.dashboard._generation
        page.tabs.setCurrentIndex(0)
        page.refresh_table()
        page.tabs.setCurrentWidget(page.dashboard)
        qtbot.wait(100)
        assert page.dashboard._generation == generation


# ----------------------------------------------------------------------
class TestLayout:
    @pytest.mark.parametrize("size", [(1366, 768), (1100, 680)])
    def test_the_controls_stay_visible_when_resized(self, qtbot, page: ResultsPage, size):
        page.resize(*size)
        qtbot.wait(50)
        board = page.dashboard
        header = board.scope_combo.mapTo(page, QPoint(0, 0))
        assert board.scope_combo.isVisible()
        assert header.y() >= 0 and header.y() + board.scope_combo.height() <= page.height()
        assert board.scope_combo.width() >= board.scope_combo.minimumSizeHint().width()
        assert page.tabs.tabBar().isVisible()

    def test_a_small_window_scrolls_vertically(self, qtbot, page: ResultsPage):
        page.resize(1100, 680)
        page.dashboard.select_scope("10")
        qtbot.wait(50)
        bar = page.dashboard.scroll_area.verticalScrollBar()
        assert bar.maximum() > 0
        bar.setValue(bar.maximum())
        assert bar.value() == bar.maximum()
        # Charts keep a readable height rather than being squeezed to fit.
        assert page.dashboard.correct_chart.height() >= 200
