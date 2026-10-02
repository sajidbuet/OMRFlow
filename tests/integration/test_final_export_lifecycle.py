"""Final Export requires a CLOSED scan session; reopening makes it stale (0.1.1 phase 4).

Scope:
    ARCHITECTURE_NOTES §6.1 / §8.2 / §14.3 through the services: results of an
    open session are provisional (on screen and in every export), Final Export
    is refused until the session is closed, closing runs the closure checks
    and either lists the blockers and changes nothing or closes (sealing the
    batches, audited), every output records its session, its finality and the
    close it came from, reopening makes a final output stale, re-closing does
    not revive it, and only regeneration does.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from tests.integration.test_reject_and_rescan import OPERATOR, TEMPLATE, build_world, reject
from tests.integration.test_session_population import SessionWorld

from omr_scanner.database.models import AuditEvent, GeneratedReport
from omr_scanner.domain.reporting import ReadinessIssueKind
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.scan_sessions import BatchMembership, ScanSessionState
from omr_scanner.services import (
    create_project,
    report_store,
    scan_lifecycle,
    scan_sessions,
    scoring_store,
)
from omr_scanner.services.scan_sessions import ScanSessionError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def sw(workspace: Path, tmp_path: Path) -> Iterator[SessionWorld]:
    session = create_project(workspace, "Final Export")
    try:
        world = SessionWorld(build_world(session, tmp_path))
        # The two sheets whose Student ID / set code is in dispute are set
        # aside, so the session's only closure blockers are the ones a test adds.
        for name in ("s_x.png", "s_blur.png"):
            scan_lifecycle.exclude_scan(
                world.database, world.ids[name], reviewer=OPERATOR,
                reason=RejectionReason.FOLDED,
            )
        for code in "123":
            world.world.score(code)
        yield world
    finally:
        if not session.is_closed:
            session.close()


def generate(sw: SessionWorld, code: str, *, final: bool, **kwargs: object):
    return report_store.generate_for_set(
        sw.database, sw.world.set_ids[code], sw.batches[0], TEMPLATE,
        project_name="FinalExport", output_dir=sw.world.session.project.layout.exports_dir,
        computed_by=OPERATOR, final=final, **kwargs,
    )


def kinds(sw: SessionWorld) -> set[str]:
    return {item.kind for item in scan_sessions.closure_blockers(sw.database, sw.scan_session_id)}


def info(sw: SessionWorld):
    return scan_sessions.get_scan_session(sw.database, sw.scan_session_id)


class TestOpenSessionIsProvisional:
    def test_1_a_preview_of_an_open_session_says_provisional_everywhere(self, sw):
        outcome = generate(sw, "1", final=False)
        assert outcome.ok, outcome.warnings
        assert "Provisional" in outcome.output_path.name
        assert outcome.warnings[0].startswith("PROVISIONAL RESULTS")
        with sw.database.session() as session:
            row = session.get(GeneratedReport, outcome.report_id)
            assert row.scan_session_id == sw.scan_session_id
            assert row.is_final is False and row.session_closed_at is None

    def test_2_final_export_of_an_open_session_is_refused(self, sw):
        readiness = report_store.check_readiness(
            sw.database, sw.world.rosters["1"], sw.batches[0], TEMPLATE, "1",
            for_final_export=True,
        )
        assert ReadinessIssueKind.SESSION_OPEN in {item.kind for item in readiness.issues}
        outcome = generate(sw, "1", final=True)
        assert outcome.status == "blocked" and outcome.output_path is None
        assert any("closed session" in item for item in outcome.warnings)
        # ...and an acknowledgement cannot talk its way past it.
        assert generate(sw, "1", final=True, acknowledge_incomplete=True).status == "blocked"


class TestClosureBlockers:
    def test_3_an_unresolved_conflict_blocks_closing(self, sw):
        scan_lifecycle.restore_scan(sw.database, sw.ids["s_x.png"], reviewer=OPERATOR)
        assert "conflicts" in kinds(sw)

    def test_4_an_outstanding_rescan_blocks_unless_accepted(self, sw):
        reject(sw.world, "s1b.png")
        sw.world.score("1")  # rejecting made set 1's results stale
        blockers = scan_sessions.closure_blockers(sw.database, sw.scan_session_id)
        assert [(item.kind, item.acknowledgeable) for item in blockers] == [("rescans", True)]
        with pytest.raises(ScanSessionError):
            scan_sessions.close_scan_session(sw.database, sw.scan_session_id, closed_by=OPERATOR)
        closed = scan_sessions.close_scan_session(
            sw.database, sw.scan_session_id, closed_by=OPERATOR, acknowledge_incomplete=True
        )
        assert closed.state is ScanSessionState.CLOSED
        with sw.database.session() as session:
            detail = session.scalars(
                select(AuditEvent.detail)
                .where(AuditEvent.entity_id == sw.scan_session_id)
                .where(AuditEvent.action == "session_closed")
            ).one()
        assert "incomplete results accepted" in detail
        outcome = generate(sw, "1", final=True, acknowledge_incomplete=True)
        assert outcome.ok, outcome.warnings
        assert outcome.warnings[0].startswith("INCOMPLETE RESULTS")

    def test_5_unread_sheets_block_closing(self, sw):
        sw.add_batch([("late.tif", "100555", "1", 10)], status="pending")
        assert "unread" in kinds(sw)

    def test_6_a_blocker_leaves_the_session_open_and_unchanged(self, sw):
        sw.add_batch([("late.tif", "100555", "1", 10)], status="pending")
        before = info(sw)
        with pytest.raises(ScanSessionError, match="closure blockers"):
            scan_sessions.close_scan_session(sw.database, sw.scan_session_id, closed_by=OPERATOR)
        after = info(sw)
        assert after.state is ScanSessionState.OPEN
        assert after.closed_at == before.closed_at
        assert scan_sessions.batch_info(sw.database, sw.batches[-1]).membership is (
            BatchMembership.SEALED
        )  # sealed when it was added, not by the refused close


class TestCloseAndExport:
    def close(self, sw: SessionWorld):
        assert scan_sessions.closure_blockers(sw.database, sw.scan_session_id) == ()
        return scan_sessions.close_scan_session(
            sw.database, sw.scan_session_id, closed_by=OPERATOR,
            reason="Close session and generate final export",
        )

    def test_7_8_closing_then_exporting_records_session_and_close(self, sw):
        closed = self.close(sw)
        assert closed.state is ScanSessionState.CLOSED
        outcome = generate(sw, "1", final=True)
        assert outcome.ok, outcome.warnings
        assert "Provisional" not in outcome.output_path.name
        with sw.database.session() as session:
            row = session.get(GeneratedReport, outcome.report_id)
            assert row.scan_session_id == sw.scan_session_id
            assert row.is_final is True
            assert row.session_closed_at.replace(tzinfo=None) == closed.closed_at.replace(
                tzinfo=None
            )
        status = report_store.final_export_status(sw.database, sw.batches[0], "1")
        assert status.state == "current" and status.report_id == outcome.report_id

    def test_9_10_11_reopen_stales_reclose_does_not_revive_regenerate_does(self, sw):
        self.close(sw)
        assert generate(sw, "1", final=True).ok
        scan_sessions.reopen_scan_session(sw.database, sw.scan_session_id, reopened_by=OPERATOR)
        assert report_store.final_export_status(sw.database, sw.batches[0], "1").state == "stale"
        self.close(sw)
        assert report_store.final_export_status(sw.database, sw.batches[0], "1").state == "stale"
        regenerated = generate(sw, "1", final=True)
        assert regenerated.ok
        status = report_store.final_export_status(sw.database, sw.batches[0], "1")
        assert status.state == "current" and status.report_id == regenerated.report_id
        # Another set never exported finally says so.
        assert report_store.final_export_status(sw.database, sw.batches[0], "3").state == "none"

    def test_12_a_one_batch_session_closes_and_exports_in_one_step(self, sw):
        assert len(sw.batches) == 1
        self.close(sw)
        # Sets 1 and 3 are complete; set 2 carries this world's deliberate
        # missing-script candidate, which blocks its export on its own merits.
        for code in "13":
            assert generate(sw, code, final=True).ok
        assert scoring_store.list_results(
            sw.database, sw.world.rosters["1"], sw.batches[0], TEMPLATE
        )
