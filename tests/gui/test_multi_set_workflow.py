"""The combined multi-set workflow (Part E), without Reject & Rescan.

Scope:
    One batch holding Sets 1, 2 and 3. Reconciling Set 2 must see only Set 2's
    scripts; a Set-2 script whose Student ID lost a digit must lead the
    operator to the right roster ID; next / previous unresolved must walk the
    remaining exceptions; and all of it must still hold after the project is
    closed and reopened.

    Reject & Rescan is not implemented yet, so steps 6-10 of the scenario
    (reject a scan, import its replacement, confirm only the replacement
    counts) are deferred with it; see README.md.

The scans are stored rows rather than rendered sheets: a Student ID one digit
*shorter* than the roster's cannot come from a fixed-width roll grid, and this
scenario is about reconciliation, not recognition - which
``tests/gui/test_attendance_investigation.py`` drives on real scans.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from omr_scanner.database.models import BatchScan, ScanBatch
from omr_scanner.domain.reconciliation import ReconciliationStatus
from omr_scanner.domain.review import ConflictType
from omr_scanner.gui.attendance.page import AttendancePage
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.services import (
    batch_store,
    open_project,
    project_sets,
    reconciliation_store,
    review_store,
)
from omr_scanner.services.candidate_import import read_roster

if TYPE_CHECKING:
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

TIMEOUT_MS = 30_000

ROSTERS = {
    "1": "Roll No.,Name,Total\n100121,ONE A,50\n100122,ONE B,50\n",
    "2": (
        "Roll No.,Name,Total\n"
        "200121,TWO A,50\n"
        "200122,TWO B,50\n"
        "200123,TWO C,50\n"
        "200124,TWO D,ABSENT\n"
    ),
    "3": "Roll No.,Name,Total\n300121,THREE A,50\n300122,THREE B,50\n",
}

SCRIPTS = (
    ("s1a.png", "100121", "1"),
    ("s1b.png", "100122", "1"),
    ("s2a.png", "200121", "2"),
    ("s2b.png", "200122", "2"),
    ("s2c.png", "20123", "2"),  # 200123 wrote five digits: one missing
    ("s3a.png", "300121", "3"),
    ("s3b.png", "300122", "3"),
    ("s_x.png", "200124", "7"),  # a set this project does not have
)


def _store_batch(project_session: ProjectSession, tmp_path: Path) -> str:
    database = project_session.database
    batch_id = batch_store.new_batch_id()
    now = datetime.now(UTC)
    with database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id, created_at=now, updated_at=now,
                source_folder=str(tmp_path), status="completed", total_scans=len(SCRIPTS),
            )
        )
        session.flush()
        for index, (name, roll, code) in enumerate(SCRIPTS):
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=index, source_path=str(tmp_path / name),
                    filename=name, status="completed", identifier_value=roll,
                    set_code_value=code, result_json="",
                )
            )
    return batch_id


def _open_page(qtbot, session: ProjectSession, batch_id: str) -> AttendancePage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "attendance")
    page = AttendancePage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(session)
    page.set_operator("Dr. A. Rahman")
    page.set_batch(batch_id)
    return page


def _set_id(session: ProjectSession, code: str) -> str:
    return next(
        item.set_id for item in project_sets.list_sets(session.database) if item.code == code
    )


@pytest.fixture
def scenario(qtbot, project_session: ProjectSession, tmp_path):
    for code in "123":
        project_sets.add_set(project_session.database, code, f"Paper {code}")
    batch_id = _store_batch(project_session, tmp_path)
    page = _open_page(qtbot, project_session, batch_id)
    for code in "123":
        path = tmp_path / f"set{code}.csv"
        path.write_text(ROSTERS[code], encoding="utf-8")
        page.select_set(_set_id(project_session, code))
        with qtbot.waitSignal(page.reconciled, timeout=TIMEOUT_MS):
            assert page.commit_roster(read_roster(path), source_path=path)
    page.select_set(_set_id(project_session, "2"))
    page.refresh_table()
    yield page, batch_id
    page.close()


def _all(page: AttendancePage) -> dict[str, object]:
    return {
        entry.candidate_id: entry
        for entry in reconciliation_store.list_entries(
            page.database, page.state.roster.roster_id, page.state.batch_id
        )
    }


def _select(page: AttendancePage, candidate_id: str) -> None:
    for row, entry in enumerate(page.state.entries):
        if entry.candidate_id == candidate_id:
            page.table.selectRow(row)
            return
    raise AssertionError(candidate_id)


class TestSetTwo:
    def test_other_sets_scripts_are_not_unknown_ids_in_set_two(self, scenario):
        page, _batch = scenario
        entries = _all(page)
        assert not any(key.startswith(("1001", "3001")) for key in entries)
        names = {view.script.source_name for entry in entries.values() for view in entry.scripts}
        assert names == {"s2a.png", "s2b.png", "s2c.png"}

    def test_the_summary_says_what_was_left_out(self, scenario):
        page, _batch = scenario
        text = page.summary_label.text()
        assert "This set reconciles <b>3</b> script(s)" in text
        assert "4 belong to other sets (set 1: 2, set 3: 2)" in text
        assert "not yet resolved" in text
        assert "Resolve" in text

    def test_the_undefined_set_code_is_raised_on_resolve(self, scenario):
        page, batch_id = scenario
        undefined = [
            item
            for item in review_store.list_conflicts(page.database, batch_id)
            if item.conflict_type is ConflictType.SET_CODE_UNDEFINED
        ]
        assert len(undefined) == 1
        assert undefined[0].observation.value == "7"

    def test_the_short_id_leads_to_the_right_candidate(self, scenario):
        page, _batch = scenario
        entries = _all(page)
        assert entries["20123"].status is ReconciliationStatus.UNKNOWN_ID
        page.status_filter.setCurrentIndex(0)
        _select(page, "20123")
        leads = [page.leads_list.item(i).text() for i in range(page.leads_list.count())]
        assert leads, "a one-digit deletion must be suggested"
        assert leads[0].startswith("200123")
        assert "1 edit away" in leads[0]

    def test_the_missing_candidate_leads_back_to_the_short_script(self, scenario):
        page, _batch = scenario
        page.status_filter.setCurrentIndex(0)
        _select(page, "200123")
        leads = [page.leads_list.item(i).text() for i in range(page.leads_list.count())]
        assert any("s2c.png" in text and "read as 20123" in text for text in leads)

    def test_next_unresolved_walks_the_remaining_exceptions(self, scenario):
        page, _batch = scenario
        wanted = [e.candidate_id for e in page.state.entries if e.needs_attention]
        assert set(wanted) == {"200123", "20123"}
        page.table.setCurrentCell(-1, -1)
        visited = []
        for _ in wanted:
            assert page.select_next_unresolved() is True
            visited.append(page._selected_entry().candidate_id)
        assert visited == wanted
        assert page.select_next_unresolved() is True
        assert "continued from the top" in page.navigation_note.text()


class TestAfterReopening:
    def test_set_two_is_still_scoped_and_its_leads_still_work(
        self, qtbot, scenario, project_session
    ):
        page, batch_id = scenario
        root = project_session.root
        page.close()
        project_session.close()

        with open_project(root) as reopened:
            fresh = _open_page(qtbot, reopened, batch_id)
            try:
                fresh.select_set(_set_id(reopened, "2"))
                with qtbot.waitSignal(fresh.reconciled, timeout=TIMEOUT_MS):
                    assert fresh.reconcile()
                entries = _all(fresh)
                assert not any(key.startswith(("1001", "3001")) for key in entries)
                assert entries["20123"].status is ReconciliationStatus.UNKNOWN_ID
                fresh.status_filter.setCurrentIndex(0)
                _select(fresh, "20123")
                assert fresh.leads_list.item(0).text().startswith("200123")
            finally:
                fresh.close()
