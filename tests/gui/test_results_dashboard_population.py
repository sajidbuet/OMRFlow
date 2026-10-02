"""The Results Dashboard analyses exactly the Results rows (0.1.1 phase 4 dependency).

ROADMAP Phase C's carried dependency: the Dashboard is fed the session's rows
through ``ResultsPage.state.every_result`` - never a second read - so its
counts keep reconciling with the Results summary. This test would fail if the
Dashboard ever read results on its own: every result read is made to fail
once the Results tab has loaded, and the in-memory rows are cut down to one
set, which the Dashboard must then report and nothing more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.analytics_fixtures import OPERATOR, build_dashboard_exam

from omr_scanner.domain.scoring import ResultStatus
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.results.page import ResultsPage
from omr_scanner.services import create_project, scoring_store, session_scope

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.gui

TIMEOUT_MS = 20_000


def test_the_dashboard_consumes_the_results_rows_and_reads_nothing_itself(
    qtbot, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = create_project(workspace, "Dashboard population")
    try:
        built = build_dashboard_exam(
            session, tmp_path, sizes={"10": 12, "11": 14}, question_count=20
        )
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
        assert page.state.scan_session_id is not None
        full = page.state.every_result
        assert full

        def no_second_read(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("the Dashboard read results on its own")

        monkeypatch.setattr(scoring_store, "list_results", no_second_read)
        monkeypatch.setattr(session_scope, "results", no_second_read)
        only_ten = tuple(item for item in full if item.set_code == "10")
        page.state.every_result = only_ten
        with qtbot.waitSignal(page.dashboard.computed, timeout=TIMEOUT_MS):
            page.tabs.setCurrentWidget(page.dashboard)
        scored = sum(1 for item in only_ten if item.status is ResultStatus.SCORED)
        assert page.dashboard.kpi_cards["count"].value.text() == str(scored)
        assert scored < sum(1 for item in full if item.status is ResultStatus.SCORED)
        page.shutdown()
        page.close()
    finally:
        if not session.is_closed:
            session.close()
