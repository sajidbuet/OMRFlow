"""The combined multi-set workflow (Part E), now with Reject & Rescan.

Scope:
    One batch holding Sets 1, 2 and 3. Reconciling Set 2 must see only Set 2's
    scripts; a Set-2 script whose Student ID lost a digit must lead the
    operator to the right roster ID; next / previous unresolved must walk the
    remaining exceptions; and all of it must still hold after the project is
    closed and reopened.

    :class:`TestRejectAndRescan` continues the same scenario: a poor-quality
    Set-2 scan is rejected and stops counting, its rescan arrives and is
    offered - never linked - until the operator confirms it on the Resolve
    stage's *Rejected / Rescan* view; the replacement is then the only valid
    script for reconciliation, navigation and results; the audit trail, the
    close/reopen, and the original's later purge eligibility are checked.

The scans are stored rows rather than rendered sheets: a Student ID one digit
*shorter* than the roster's cannot come from a fixed-width roll grid, and this
scenario is about reconciliation, not recognition - which
``tests/gui/test_attendance_investigation.py`` drives on real scans.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select, update
from tests.conftest import build_answer_sheet_template

from omr_scanner.database.models import AuditEvent, BatchScan, ScanBatch
from omr_scanner.domain.reconciliation import ReconciliationStatus
from omr_scanner.domain.review import ConflictType
from omr_scanner.domain.scan_lifecycle import (
    LIFECYCLE_ENTITY,
    LifecycleState,
    RejectionReason,
)
from omr_scanner.domain.scoring import ResultStatus
from omr_scanner.gui.attendance.page import AttendancePage
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.review.page import FILTER_RESCAN, ResolvePage
from omr_scanner.services import (
    batch_store,
    open_project,
    project_sets,
    reconciliation_store,
    review_store,
    scan_lifecycle,
    scan_provenance,
    scoring_store,
)
from omr_scanner.services.answer_key import plan_for, read_key
from omr_scanner.services.candidate_import import read_roster
from omr_scanner.services.recognition_models import (
    AnswerView,
    FieldView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
)

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


TEMPLATE = build_answer_sheet_template()
PLAN = plan_for(TEMPLATE)


def _result_json(path: Path, roll: str, code: str, correct: int) -> str:
    """A stored recognition result whose first ``correct`` answers are ``A``."""
    result = ScanResult(
        source_path=path,
        outcome=RecognitionOutcome.COMPLETE,
        registration=RegistrationStatus.REGISTERED,
        fields=(
            FieldView(
                zone_id="roll_number", label="Roll", field_type="numeric",
                value=roll, status="complete", needs_review=False, characters=(),
            ),
            FieldView(
                zone_id="set_code", label="Set", field_type="set_code",
                value=code, status="complete", needs_review=False, characters=(),
            ),
        ),
        answers=tuple(
            AnswerView(
                number=number, zone_id="q", value="A" if index < correct else "B",
                status="resolved", needs_review=False,
                top_fill=0.9, margin=0.4, confidence=0.9,
            )
            for index, number in enumerate(PLAN.numbers)
        ),
        identifier_zone_id="roll_number",
        set_code_zone_id="set_code",
    )
    return json.dumps(result.to_dict())


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
            path = tmp_path / name
            path.write_bytes(f"scan:{name}".encode())
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=index, source_path=str(path),
                    filename=name, status="completed", identifier_value=roll,
                    set_code_value=code,
                    # Recognition results, so the scenario can be scored: the
                    # rejected original answers 10 correctly, its rescan 17.
                    result_json=_result_json(path, roll, code, 10),
                )
            )
    scan_provenance.compute_hashes_for_batch(database, batch_id)
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


# ----------------------------------------------------------------------
# The same scenario, continued: Reject & Rescan
# ----------------------------------------------------------------------
OPERATOR = "Dr. A. Rahman"


def _scan_id(page: AttendancePage, name: str) -> int:
    return next(
        scan_id
        for path, scan_id in batch_store.scan_ids_by_path(
            page.database, page.state.batch_id
        ).items()
        if path.name == name
    )


def _add_rescan(page: AttendancePage, tmp_path: Path, name: str, roll: str, code: str) -> int:
    """Register and record a rescan the way the Scan stage now does."""
    path = tmp_path / name
    path.write_bytes(f"rescan:{name}".encode())
    database, batch_id = page.database, page.state.batch_id
    assert batch_store.add_scans_to_batch(database, batch_id, [path]) == 1
    scan_id = batch_store.scan_ids_by_path(database, batch_id)[path]
    with database.session() as session:
        session.execute(
            update(BatchScan)
            .where(BatchScan.scan_id == scan_id)
            .values(
                status="completed", identifier_value=roll, set_code_value=code,
                result_json=_result_json(path, roll, code, 17),
                content_sha256=scan_provenance.hash_file(path),
            )
        )
    scan_lifecycle.sync_reimports(database, batch_id)
    review_store.sync_duplicate_identifiers(database, batch_id)
    return scan_id


def _resolve_page(qtbot, session: ProjectSession, batch_id: str) -> ResolvePage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
    page = ResolvePage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(session)
    page.set_reviewer(OPERATOR)
    assert page.load_batch(batch_id, None)
    return page


def _verify_key(session: ProjectSession, code: str) -> None:
    stored = scoring_store.save_key(
        session.database, read_key("A" * PLAN.question_count, PLAN, code).to_key()
    )
    scoring_store.verify_key(session.database, stored.key_id, verified_by=OPERATOR)


class TestRejectAndRescan:
    def test_the_combined_scenario(self, qtbot, scenario, project_session, tmp_path):
        page, batch_id = scenario
        database = project_session.database
        original = _scan_id(page, "s2b.png")
        assert _all(page)["200122"].status is ReconciliationStatus.MATCHED

        # 2. Reject a poor-quality Set-2 scan.
        scan_lifecycle.reject_scan(
            database, original, reviewer=OPERATOR, reason=RejectionReason.POOR_QUALITY,
            note="Scanner streak across the answer grid",
        )
        # 3. It stops qualifying as a valid Set-2 script - and says why.
        with qtbot.waitSignal(page.reconciled, timeout=TIMEOUT_MS):
            assert page.reconcile()
        entry = _all(page)["200122"]
        assert entry.status is ReconciliationStatus.RESCAN_REQUIRED
        assert entry.script_count == 0
        page.status_filter.setCurrentIndex(
            page.status_filter.findText("Rejected — rescan required")
        )
        assert [item.candidate_id for item in page.state.entries] == ["200122"]
        assert "rejected - rescan required" in page.summary_label.text()
        # Other sets are untouched, and nothing became an unknown ID.
        assert not any(key.startswith(("1001", "3001")) for key in _all(page))

        # 4-5. Its rescan is read into the batch and offered - never linked.
        rescan = _add_rescan(page, tmp_path, "IMG_2291.jpg", "200122", "2")
        assert scan_lifecycle.state_of(database, original) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )
        resolve = _resolve_page(qtbot, project_session, batch_id)
        try:
            resolve.state_filter.setCurrentText(FILTER_RESCAN)
            assert [case.source_name for case in resolve.state.rescan_cases] == ["s2b.png"]
            panel = resolve.rescan_panel
            assert panel.select_candidate(rescan)
            assert "Possible rescan: IMG_2291.jpg" in panel.candidates_list.currentItem().text()
            assert resolve.confirm_replacement_for_case(rescan)
            # 8. Navigation: nothing is left awaiting a rescan in this view.
            assert resolve.select_next_unresolved() is False
        finally:
            resolve.close()

        # 6-7. The replacement is the only valid script; reconciliation agrees.
        page.status_filter.setCurrentIndex(0)
        with qtbot.waitSignal(page.reconciled, timeout=TIMEOUT_MS):
            assert page.reconcile()
        entries = _all(page)
        assert entries["200122"].status is ReconciliationStatus.MATCHED
        assert [view.script.scan_id for view in entries["200122"].scripts] == [rescan]
        duplicates = [
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        ]
        assert duplicates == []

        # 8. Next unresolved still walks the remaining exceptions only.
        wanted = [item.candidate_id for item in page.state.entries if item.needs_attention]
        assert set(wanted) == {"200123", "20123"}
        page.table.setCurrentCell(-1, -1)
        visited = []
        for _ in wanted:
            assert page.select_next_unresolved() is True
            visited.append(page._selected_entry().candidate_id)
        assert visited == wanted

        # 11. Results use only the replacement.
        _verify_key(project_session, "2")
        roster = page.state.roster.roster_id
        scoring_store.score_batch(database, roster, batch_id, TEMPLATE, computed_by=OPERATOR)
        results = {
            item.candidate_id: item
            for item in scoring_store.list_results(database, roster, batch_id, TEMPLATE)
        }
        assert results["200122"].status is ResultStatus.SCORED
        assert results["200122"].scan_id == rescan
        assert results["200122"].correct_count == 17

        # 10. The audit trail.
        with database.session() as session:
            actions = [
                row.action
                for row in session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.entity_type == LIFECYCLE_ENTITY)
                    .order_by(AuditEvent.event_id)
                ).all()
            ]
        assert actions == ["rejected", "replaced", "linked_replacement"]

        # 12. The superseded original is now purge-eligible (its file is not
        # in project storage, so nothing would be removed - but it is listed).
        plan = scan_lifecycle.plan_purge(database, project_session.root)
        assert [item.case.scan_id for item in plan.eligible] == [original]
        assert plan.pending == 0

        # 9. Close, reopen: all of it holds.
        root = project_session.root
        page.close()
        project_session.close()
        with open_project(root) as reopened:
            fresh = _open_page(qtbot, reopened, batch_id)
            try:
                fresh.select_set(_set_id(reopened, "2"))
                with qtbot.waitSignal(fresh.reconciled, timeout=TIMEOUT_MS):
                    assert fresh.reconcile()
                again = _all(fresh)
                assert again["200122"].status is ReconciliationStatus.MATCHED
                assert [view.script.scan_id for view in again["200122"].scripts] == [rescan]
                case = scan_lifecycle.get_case(reopened.database, original)
                assert case is not None
                assert case.state is LifecycleState.SUPERSEDED_BY_REPLACEMENT
                assert case.replacement_scan_id == rescan
                assert not any(key.startswith(("1001", "3001")) for key in again)
            finally:
                fresh.close()

    def test_the_rescan_arrives_in_a_later_batch(
        self, qtbot, scenario, project_session, tmp_path
    ):
        """Batch A, reject, reopen, Batch B, offer, confirm, unlink, undo, reopen."""
        page, batch_a = scenario
        database = project_session.database
        original = _scan_id(page, "s2b.png")
        partner = _scan_id(page, "s2a.png")
        roster = page.state.roster.roster_id
        scan_lifecycle.reject_scan(
            database, original, reviewer=OPERATOR, reason=RejectionReason.POOR_QUALITY
        )
        root = project_session.root
        page.close()
        project_session.close()

        with open_project(root) as session:
            database = session.database
            # Batch B: another day, another scanner, another folder and name.
            folder = tmp_path / "scanner_b"
            folder.mkdir()
            path = folder / "SCN_000913.tif"
            path.write_bytes(b"rescan:SCN_000913.tif")
            batch_b = batch_store.new_batch_id()
            now = datetime.now(UTC)
            with database.session() as db:
                db.add(
                    ScanBatch(
                        batch_id=batch_b, created_at=now, updated_at=now,
                        source_folder=str(folder), status="completed", total_scans=1,
                    )
                )
                db.flush()
                db.add(
                    BatchScan(
                        batch_id=batch_b, batch_index=0, source_path=str(path),
                        filename=path.name, status="completed", identifier_value="200122",
                        set_code_value="2",
                        result_json=_result_json(path, "200122", "2", 17),
                    )
                )
            scan_provenance.compute_hashes_for_batch(database, batch_b)
            scan_lifecycle.sync_reimports(database, batch_b)
            review_store.sync_duplicate_identifiers(database, batch_b)
            rescan = batch_store.scan_ids_by_path(database, batch_b)[path]

            # Offered from the other batch - labelled so - and never linked.
            resolve = _resolve_page(qtbot, session, batch_a)
            try:
                resolve.state_filter.setCurrentText(FILTER_RESCAN)
                panel = resolve.rescan_panel
                assert panel.select_candidate(rescan)
                text = panel.candidates_list.currentItem().text()
                assert "SCN_000913.tif" in text and f"batch {batch_b[:8]}" in text
                assert scan_lifecycle.state_of(database, original) is (
                    LifecycleState.REJECTED_PENDING_RESCAN
                )
                assert resolve.confirm_replacement_for_case(rescan)
            finally:
                resolve.close()

            # Only the Batch-B scan counts, and it counts once.
            reconciliation_store.reconcile_batch(database, roster, batch_a)
            entries = {
                e.candidate_id: e
                for e in reconciliation_store.list_entries(database, roster, batch_a)
            }
            assert entries["200122"].status is ReconciliationStatus.MATCHED
            assert [view.script.scan_id for view in entries["200122"].scripts] == [rescan]
            assert not any(key.startswith(("1001", "3001")) for key in entries)
            assert entries["20123"].status is ReconciliationStatus.UNKNOWN_ID
            scope = reconciliation_store.script_scope(database, roster, batch_b)
            assert (scope.in_set, scope.counted_elsewhere) == (0, 1)

            _verify_key(session, "2")
            scoring_store.score_batch(database, roster, batch_a, TEMPLATE, computed_by=OPERATOR)
            results = {
                item.candidate_id: item
                for item in scoring_store.list_results(database, roster, batch_a, TEMPLATE)
            }
            assert results["200122"].scan_id == rescan
            assert results["200122"].correct_count == 17

            with database.session() as db:
                trail = [
                    (row.action, row.batch_id)
                    for row in db.scalars(
                        select(AuditEvent)
                        .where(AuditEvent.entity_type == LIFECYCLE_ENTITY)
                        .order_by(AuditEvent.event_id)
                    ).all()
                ]
            assert trail == [
                ("rejected", batch_a), ("replaced", batch_a), ("linked_replacement", batch_b),
            ]
            plan = scan_lifecycle.plan_purge(database, session.root)
            assert [item.case.scan_id for item in plan.eligible] == [original]

            # Unlink, then Undo Reject: every stage agrees with a fresh detection.
            scan_lifecycle.remove_replacement(database, original, reviewer=OPERATOR)
            reconciliation_store.reconcile_batch(database, roster, batch_a)
            assert {
                e.candidate_id: e
                for e in reconciliation_store.list_entries(database, roster, batch_a)
            }["200122"].status is ReconciliationStatus.RESCAN_REQUIRED
            scan_lifecycle.undo_reject(database, original, reviewer=OPERATOR)
            assert scan_lifecycle.state_of(database, original) is LifecycleState.ACTIVE
            assert partner != original
            for batch in (batch_a, batch_b):
                before = review_store.count_conflicts(database, batch)
                review_store.sync_duplicate_identifiers(database, batch)
                after = review_store.count_conflicts(database, batch)
                assert (before.total, before.unresolved) == (after.total, after.unresolved)

        # Reopen: the restored state holds.
        with open_project(root) as reopened:
            assert scan_lifecycle.state_of(reopened.database, original) is LifecycleState.ACTIVE
            assert scan_lifecycle.state_of(reopened.database, rescan) is LifecycleState.ACTIVE
            reconciliation_store.reconcile_batch(reopened.database, roster, batch_a)
            entry = {
                e.candidate_id: e
                for e in reconciliation_store.list_entries(reopened.database, roster, batch_a)
            }["200122"]
            assert [view.script.scan_id for view in entry.scripts] == [original]
