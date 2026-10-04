"""Reports' one-step close goes through *Finish scan session* (0.1.1 revised phase 7).

The pre-merge review found *Close session and generate final export* closing
through the Phase C checks directly, so a session the finish policy refuses
could still be closed from Reports. These drive the real page (dialogs
answered, never modal) on continuous-intake sessions and check that:

* the page asks the authoritative service - ``finish_scan_session`` - and
  nothing else decides;
* blockers the Phase C checks never saw refuse the close: a held file, an
  unanswered suggested rescan, an unreachable source, queued work, another
  coordinator processing;
* outstanding rescans close only by the named operator's audited acceptance
  (cancel leaves the session open);
* a clean session closes, and the finite workflow's final export still runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import select
from tests.engine_rig import EngineRig, readable_sheets, sheet_bytes
from tests.integration.test_reject_and_rescan import OPERATOR, build_world
from tests.integration.test_session_finish import (
    BIG,
    resolve_all_conflicts,
    resolve_all_conflicts_except_evidence,
    settle,
)
from tests.integration.test_session_population import SessionWorld
from tests.quality_rig import blank_page, displaced_id

from omr_scanner.database.models import AuditEvent, ScanBatch
from omr_scanner.domain.processing import EngineLimits
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.domain.session_finish import BlockerCode
from omr_scanner.gui import session_close
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.reports.page import ReportsPage
from omr_scanner.services import (
    create_project,
    quality_decisions,
    report_store,
    scan_lifecycle,
    scan_sessions,
    session_finish,
)
from omr_scanner.services.intake import IntakeService

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from omr_scanner.domain.session_finish import FinishOutcome

pytestmark = pytest.mark.gui

SHEETS = readable_sheets(8)
TIMEOUT_MS = 60_000


class Dialogs:
    """The page's dialogs, answered; what each was shown is recorded."""

    def __init__(self, page: ReportsPage, *, accept_incomplete: bool = False) -> None:
        self.blockers: list[list[str]] = []
        self.incomplete: list[list[str]] = []
        page.confirm_close_and_export = lambda _name: True  # type: ignore[method-assign]
        page.show_closure_blockers = (  # type: ignore[method-assign]
            lambda _name, items: self.blockers.append(items)
        )

        def incomplete(_name: str, items: list[str]) -> bool:
            self.incomplete.append(items)
            return accept_incomplete

        page.confirm_incomplete_close = incomplete  # type: ignore[method-assign]


@pytest.fixture
def finishes(monkeypatch: pytest.MonkeyPatch) -> list[FinishOutcome]:
    """Every outcome of the authoritative service while the test runs (it still runs)."""
    seen: list[FinishOutcome] = []
    real = session_finish.finish_scan_session

    def spy(*args: Any, **kwargs: Any) -> FinishOutcome:
        outcome = real(*args, **kwargs)
        seen.append(outcome)
        return outcome

    monkeypatch.setattr(session_finish, "finish_scan_session", spy)
    return seen


@pytest.fixture
def rig(project_session, monkeypatch: pytest.MonkeyPatch, finishes) -> EngineRig:
    made = EngineRig(project_session)
    # The final reconciliation lists the rig's sources on its fake disk.
    monkeypatch.setattr(
        session_close,
        "make_intake",
        lambda project: IntakeService(
            project.database, project.root, fs=made.fs, clock=made.clock, recover=False
        ),
    )
    return made


def reports_page(qtbot, rig: EngineRig) -> ReportsPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
    page = ReportsPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(rig.project)
    page.set_reviewer("Operator")
    with rig.database.session() as session:
        batch = session.scalars(
            select(ScanBatch.batch_id)
            .where(ScanBatch.scan_session_id == rig.session_id)
            .order_by(ScanBatch.created_at)
        ).first()
    assert batch is not None
    page.set_batch(batch)
    return page


def state(rig: EngineRig) -> ScanSessionState:
    info = scan_sessions.get_scan_session(rig.database, rig.session_id)
    assert info is not None
    return info.state


def clean_session(rig: EngineRig) -> None:
    rig.source("a")
    rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(3)])
    settle(rig)
    resolve_all_conflicts(rig)


def stop_engine(rig: EngineRig) -> None:
    """The coordinator stops; its lease goes with it."""
    assert rig.engine is not None
    rig.engine.shutdown()
    rig.engine = None


def refused(qtbot, rig: EngineRig, finishes: list[FinishOutcome]) -> tuple[Dialogs, set[BlockerCode]]:
    page = reports_page(qtbot, rig)
    dialogs = Dialogs(page, accept_incomplete=True)
    assert page._ensure_closed_for_final([]) is None
    assert state(rig) is ScanSessionState.OPEN
    assert finishes and not finishes[-1].closed
    assert len(dialogs.blockers) == 1 and dialogs.incomplete == []
    return dialogs, set(finishes[-1].codes)


def test_a_held_file_refuses_the_close(qtbot, rig, finishes):
    clean_session(rig)
    rig.write("a", [("broken.png", sheet_bytes(36)[30])])
    for _ in range(5):
        rig.clock.advance(60)
        rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
    stop_engine(rig)
    # The Phase C checks alone would have closed this session.
    assert scan_sessions.closure_blockers(rig.database, rig.session_id) == ()
    dialogs, codes = refused(qtbot, rig, finishes)
    assert codes == {BlockerCode.FILES_AWAITING_DECISION}
    assert any("await an operator decision" in item for item in dialogs.blockers[0])


def test_an_unanswered_suggested_rescan_refuses_the_close(qtbot, rig, finishes):
    clean_session(rig)
    rig.write("a", [("blank.png", blank_page())])
    settle(rig)
    resolve_all_conflicts_except_evidence(rig)
    stop_engine(rig)
    assert quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
    dialogs, codes = refused(qtbot, rig, finishes)
    assert BlockerCode.RESCAN_SUGGESTED in codes
    assert any("suggested rescan" in item for item in dialogs.blockers[0])


def test_an_unreachable_source_fails_final_reconciliation(qtbot, rig, finishes):
    clean_session(rig)
    stop_engine(rig)
    rig.fs.unreachable.add(rig.root("a"))
    assert scan_sessions.closure_blockers(rig.database, rig.session_id) == ()
    dialogs, codes = refused(qtbot, rig, finishes)
    assert codes == {BlockerCode.SOURCE_UNREACHABLE}
    assert any("Scanner a" in item and "final check" in item for item in dialogs.blockers[0])


def test_queued_work_and_a_running_coordinator_refuse_the_close(qtbot, rig, finishes):
    clean_session(rig)
    stop_engine(rig)
    rig.new_engine(unit_policy=BIG, limits=EngineLimits(max_in_flight=1, claim_window=1))
    rig.write("a", [("q1.png", SHEETS[5]), ("q2.png", SHEETS[6])])
    rig.make_ready()
    assert rig.engine.form_units()  # type: ignore[union-attr]
    rig.engine.step()  # type: ignore[union-attr]  # one claimed, one queued
    # The engine still holds the coordinator lease: the page may not close under it.
    _dialogs, codes = refused(qtbot, rig, finishes)
    assert BlockerCode.PROCESSING_ACTIVE in codes
    stop_engine(rig)
    dialogs, codes = refused(qtbot, rig, finishes)
    assert codes & {BlockerCode.SHEETS_QUEUED, BlockerCode.SHEETS_PROCESSING}
    assert any("not been read yet" in item or "being read" in item for item in dialogs.blockers[0])


def outstanding_rescan(rig: EngineRig) -> None:
    rig.source("a")
    rig.write("a", [("0.png", SHEETS[0]), ("moved.png", displaced_id(3))])
    settle(rig)
    resolve_all_conflicts_except_evidence(rig)
    moved = quality_decisions.outstanding_suggestions(rig.database, rig.session_id)[0]
    quality_decisions.confirm_suggestion(rig.database, moved.scan_id, reviewer="Operator")
    resolve_all_conflicts(rig)
    stop_engine(rig)


def test_an_outstanding_rescan_cancelled_leaves_the_session_open(qtbot, rig, finishes):
    outstanding_rescan(rig)
    page = reports_page(qtbot, rig)
    dialogs = Dialogs(page, accept_incomplete=False)
    assert page._ensure_closed_for_final([]) is None
    assert state(rig) is ScanSessionState.OPEN
    assert dialogs.blockers == []
    assert dialogs.incomplete == [["1 rejected sheet(s) are awaiting a rescan."]]
    assert [item.codes for item in finishes] == [(BlockerCode.RESCAN_OUTSTANDING,)]


def test_an_outstanding_rescan_closes_only_by_named_audited_acceptance(qtbot, rig, finishes):
    outstanding_rescan(rig)
    page = reports_page(qtbot, rig)
    dialogs = Dialogs(page, accept_incomplete=True)
    assert page._ensure_closed_for_final([]) == "acknowledged"
    assert state(rig) is ScanSessionState.CLOSED
    assert dialogs.blockers == [] and len(dialogs.incomplete) == 1
    assert [item.closed for item in finishes] == [False, True]
    assert [item.code for item in finishes[-1].accepted] == [BlockerCode.RESCAN_OUTSTANDING]
    with rig.database.session() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "session_closed")
        ).one()
    assert event.reviewer == "Operator"
    assert "incomplete results accepted" in event.detail
    assert "Close session and generate final export" in event.reason_text


def test_without_an_operator_name_nothing_is_closed(qtbot, rig, finishes, monkeypatch):
    clean_session(rig)
    stop_engine(rig)
    page = reports_page(qtbot, rig)
    page.set_reviewer("")
    Dialogs(page)
    warnings: list[str] = []
    monkeypatch.setattr(
        "omr_scanner.gui.reports.page.QMessageBox.warning",
        lambda _parent, title, _text: warnings.append(title),
    )
    assert page._ensure_closed_for_final([]) is None
    assert state(rig) is ScanSessionState.OPEN
    assert warnings == ["Scan session not closed"]


def test_a_clean_continuous_session_closes_through_the_service(qtbot, rig, finishes):
    clean_session(rig)
    stop_engine(rig)
    page = reports_page(qtbot, rig)
    dialogs = Dialogs(page)
    assert page._ensure_closed_for_final([]) == "closed"
    assert state(rig) is ScanSessionState.CLOSED
    assert dialogs.blockers == [] and dialogs.incomplete == []
    assert len(finishes) == 1 and finishes[0].closed
    assert [item.label for item in finishes[0].sources] == ["Scanner a"]


# --- the finite workflow: one step closes and exports --------------------------


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


def test_final_export_closes_through_the_service_and_still_exports(qtbot, sw, finishes):
    from tests.integration.test_reject_and_rescan import TEMPLATE

    for name in ("s_x.png", "s_blur.png"):
        scan_lifecycle.exclude_scan(
            sw.database, sw.ids[name], reviewer=OPERATOR, reason=RejectionReason.FOLDED
        )
    sw.world.score("1")
    spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
    page = ReportsPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(sw.world.session)
    page.set_reviewer(OPERATOR)
    page.set_template(TEMPLATE)
    page.set_batch(sw.batches[0])
    dialogs = Dialogs(page)
    row = next(index for index, item in enumerate(page.state.sets) if item.set_code == "1")
    page.set_table.selectRow(row)
    with qtbot.waitSignal(page.reports_generated, timeout=TIMEOUT_MS):
        assert page.generate_selected_xlsx() is True
    assert dialogs.blockers == [] and dialogs.incomplete == []
    assert len(finishes) == 1 and finishes[0].closed
    info = scan_sessions.get_scan_session(sw.database, sw.scan_session_id)
    assert info is not None and info.state is ScanSessionState.CLOSED
    assert report_store.final_export_status(sw.database, sw.batches[0], "1").state == "current"
    page.close()


def test_a_set_that_cannot_export_closes_nothing_and_skips_the_service(qtbot, sw, finishes):
    """*Can this report be generated?* is the page's question, asked before any close."""
    spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
    page = ReportsPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(sw.world.session)
    page.set_reviewer(OPERATOR)
    from tests.integration.test_reject_and_rescan import TEMPLATE

    page.set_template(TEMPLATE)
    page.set_batch(sw.batches[0])
    dialogs = Dialogs(page, accept_incomplete=True)
    row = next(index for index, item in enumerate(page.state.sets) if item.set_code == "2")
    page.set_table.selectRow(row)
    assert page.generate_selected_xlsx() is False
    assert finishes == []  # nothing ran the close
    info = scan_sessions.get_scan_session(sw.database, sw.scan_session_id)
    assert info is not None and info.state is ScanSessionState.OPEN
    shown = dialogs.blockers[0]
    assert any(item.startswith("Set 2:") for item in shown)  # the set's own blocker
    assert any("conflict" in item for item in shown)  # the session's, from the same policy


# --- the Scan stage's Close scan session: the same policy ----------------------


def test_scan_stage_close_uses_the_same_policy(qtbot, rig, finishes):
    from omr_scanner.gui.scan.page import ScanPage

    clean_session(rig)
    rig.write("a", [("broken.png", sheet_bytes(36)[30])])
    for _ in range(5):
        rig.clock.advance(60)
        rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
    stop_engine(rig)
    scan_sessions.set_active_scan_session(rig.database, rig.session_id, activated_by="Operator")
    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    qtbot.addWidget(page)
    page.set_reviewer("Operator")
    page.on_project_changed(rig.project)
    shown: list[list[str]] = []
    page.show_close_blockers = shown.append  # type: ignore[method-assign]
    assert page.close_active_scan_session() is False
    assert state(rig) is ScanSessionState.OPEN
    assert finishes[-1].codes == (BlockerCode.FILES_AWAITING_DECISION,)
    assert any("await an operator decision" in item for item in shown[0])
