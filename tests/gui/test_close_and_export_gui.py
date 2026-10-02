"""Reports: "Close session and generate final export" through the real page (ACCEPTANCE C8).

Driven through the page's methods with its dialogs answered (never a modal):

* on an open session, a final export offers the one step; declining changes nothing;
* blockers - closure blockers *and* a set's own final-export blockers - are
  listed and nothing is closed or exported;
* with no template the page cannot generate, so it does not close either
  (found by the scripted GUI run: closing then exporting nothing);
* otherwise the session is closed, the final workbook generated, and the set
  shows *Final export current*; reopening shows it stale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.integration.test_reject_and_rescan import OPERATOR, TEMPLATE, build_world
from tests.integration.test_session_population import SessionWorld

from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.reports.page import ReportsPage
from omr_scanner.services import create_project, report_store, scan_lifecycle, scan_sessions

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.gui

TIMEOUT_MS = 60_000


@pytest.fixture
def sw(workspace: Path, tmp_path: Path) -> Iterator[SessionWorld]:
    session = create_project(workspace, "Close and Export")
    try:
        world = SessionWorld(build_world(session, tmp_path))
        for code in "123":
            world.world.score(code)
        yield world
    finally:
        if not session.is_closed:
            session.close()


def make_page(qtbot, sw: SessionWorld, *, template=TEMPLATE) -> ReportsPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
    page = ReportsPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(sw.world.session)
    page.set_reviewer(OPERATOR)
    if template is not None:
        page.set_template(template)
    page.set_batch(sw.batches[0])
    return page


def select(page: ReportsPage, code: str) -> None:
    row = next(index for index, item in enumerate(page.state.sets) if item.set_code == code)
    page.set_table.selectRow(row)


def state(sw: SessionWorld) -> ScanSessionState:
    return scan_sessions.get_scan_session(sw.database, sw.scan_session_id).state


def test_declining_the_one_step_changes_nothing(qtbot, sw):
    page = make_page(qtbot, sw)
    offered: list[str] = []
    page.confirm_close_and_export = lambda name: offered.append(name) or False
    select(page, "1")
    assert page.generate_selected_xlsx() is False
    assert offered == ["Midterm"] and state(sw) is ScanSessionState.OPEN
    assert "Provisional" in page.exam_name_label.text()


def test_blockers_are_listed_and_nothing_is_closed(qtbot, sw):
    page = make_page(qtbot, sw)
    page.confirm_close_and_export = lambda _name: True
    shown: list[list[str]] = []
    page.show_closure_blockers = lambda _name, items: shown.append(items)
    select(page, "2")
    assert page.generate_selected_xlsx() is False
    assert state(sw) is ScanSessionState.OPEN
    assert any("conflict" in item for item in shown[0])  # closure blocker
    assert any(item.startswith("Set 2:") for item in shown[0])  # the set's own blocker


def test_without_a_template_the_session_is_not_closed(qtbot, sw):
    page = make_page(qtbot, sw, template=None)
    page.confirm_close_and_export = lambda _name: True
    select(page, "1")
    assert page.generate_selected_xlsx() is False
    assert state(sw) is ScanSessionState.OPEN


def test_closing_then_exporting_in_one_step_and_stale_on_reopen(qtbot, sw):
    for name in ("s_x.png", "s_blur.png"):
        scan_lifecycle.exclude_scan(
            sw.database, sw.ids[name], reviewer=OPERATOR, reason=RejectionReason.FOLDED
        )
    sw.world.score("1")
    page = make_page(qtbot, sw)
    page.confirm_close_and_export = lambda _name: True
    page.show_closure_blockers = lambda _name, items: pytest.fail(f"blocked: {items}")
    select(page, "1")
    with qtbot.waitSignal(page.reports_generated, timeout=TIMEOUT_MS):
        assert page.generate_selected_xlsx() is True
    assert state(sw) is ScanSessionState.CLOSED
    assert report_store.final_export_status(sw.database, sw.batches[0], "1").state == "current"
    select(page, "1")
    assert "Final export current" in page.detail_label.text()
    scan_sessions.reopen_scan_session(sw.database, sw.scan_session_id, reopened_by=OPERATOR)
    page.refresh_table()
    select(page, "1")
    assert "STALE" in page.detail_label.text()
    page.close()
