"""A session's downstream store is recorded, and combining cannot lose decisions.

Scope:
    Migration 15's ``scan_session.downstream_batch_id`` (ADR-0007, revised):
    the store is bound by the upgrade step and by the first downstream write,
    is never re-derived afterwards, and *Combine into one session* refuses
    when more than one session already holds Attendance / Results decisions -
    unless the operator names whose decisions the combined session keeps.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select, update
from tests.integration.test_reject_and_rescan import OPERATOR, build_world
from tests.integration.test_session_population import SessionWorld

from omr_scanner.database.models import (
    AuditEvent,
    ReconciliationDecision,
    ReconciliationRun,
    ScanBatch,
    ScanSession,
)
from omr_scanner.services import (
    create_project,
    open_project,
    project_health,
    reconciliation_store,
    scan_sessions,
    session_population,
)
from omr_scanner.services.scan_sessions import ScanSessionError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def sw(workspace: Path, tmp_path: Path) -> Iterator[SessionWorld]:
    session = create_project(workspace, "Store Binding")
    try:
        world = SessionWorld(build_world(session, tmp_path))
        # build_world reconciled before the batch joined a session, as a
        # schema-14 project upgraded by migration 15 would have: bind it now.
        session_population.bind_downstream_stores(world.database)
        yield world
    finally:
        if not session.is_closed:
            session.close()


def bound(database, scan_session_id: str) -> str | None:
    with database.session() as session:
        return session.scalar(
            select(ScanSession.downstream_batch_id).where(
                ScanSession.scan_session_id == scan_session_id
            )
        )


def second_session_with_decisions(sw: SessionWorld, *, older: bool) -> tuple[str, str]:
    """Another session whose own batch was reconciled (so it holds decisions)."""
    other = scan_sessions.create_scan_session(sw.database, name="Second sitting", activate=False)
    batch = sw.add_batch(
        [("o1.tif", "100121", "1", 10)], scan_session_id=other.scan_session_id
    )
    if older:
        with sw.database.session() as session:
            first = session.get(ScanBatch, sw.batches[0])
            session.execute(
                update(ScanBatch)
                .where(ScanBatch.batch_id == batch)
                .values(created_at=first.created_at - timedelta(hours=1))
            )
    reconciliation_store.reconcile_batch(sw.database, sw.world.rosters["1"], batch)
    return other.scan_session_id, batch


class TestBinding:
    def test_the_upgrade_step_binds_a_session_holding_state(self, sw):
        assert bound(sw.database, sw.scan_session_id) == sw.batches[0]
        assert session_population.bind_downstream_stores(sw.database) == 0  # idempotent

    def test_the_first_downstream_write_binds_a_new_session(self, sw):
        other, batch = second_session_with_decisions(sw, older=False)
        assert bound(sw.database, other) == batch

    def test_adding_batches_never_moves_the_store(self, sw):
        for index in range(3):
            sw.add_batch([(f"x{index}.tif", f"10088{index}", "1", 10)])
            assert session_population.population_key(sw.database, sw.batches[-1]) == (
                sw.batches[0]
            )

    def test_the_store_survives_reopening(self, sw):
        root = sw.world.session.root
        sw.world.session.close()
        with open_project(root) as reopened:
            assert bound(reopened.database, sw.scan_session_id) == sw.batches[0]
        with open_project(root, read_only=True) as read_only:
            assert session_population.population_key(
                read_only.database, sw.batches[0]
            ) == sw.batches[0]


class TestCombine:
    def test_combining_two_sessions_with_decisions_is_refused(self, sw):
        other, _batch = second_session_with_decisions(sw, older=False)
        problems = scan_sessions.combine_problems(sw.database, [other], sw.scan_session_id)
        assert any("Attendance or Results decisions" in item for item in problems)
        with pytest.raises(ScanSessionError):
            scan_sessions.combine_scan_sessions(
                sw.database, [other], sw.scan_session_id, combined_by=OPERATOR
            )
        assert session_population.session_of_batch(sw.database, _batch) == other

    def test_an_explicit_choice_keeps_one_sessions_decisions_and_records_it(self, sw):
        other, batch = second_session_with_decisions(sw, older=True)
        with sw.database.session() as session:
            runs_before = session.scalar(select(func.count()).select_from(ReconciliationRun))
            decisions_before = session.scalar(
                select(func.count()).select_from(ReconciliationDecision)
            )
        scan_sessions.combine_scan_sessions(
            sw.database, [other], sw.scan_session_id, combined_by=OPERATOR,
            keep_downstream_of=sw.scan_session_id, reason="one sitting",
        )
        # The older batch now in the session holds state too; the old rule
        # ("oldest batch holding state") would have switched to it. The store
        # is the recorded choice instead.
        assert bound(sw.database, sw.scan_session_id) == sw.batches[0]
        assert session_population.population_key(sw.database, batch) == sw.batches[0]
        assert bound(sw.database, other) is None
        with sw.database.session() as session:
            # Nothing was deleted: the other session's decisions remain as history.
            assert session.scalar(select(func.count()).select_from(ReconciliationRun)) == (
                runs_before
            )
            assert session.scalar(
                select(func.count()).select_from(ReconciliationDecision)
            ) == decisions_before
            detail = session.scalars(
                select(AuditEvent.detail)
                .where(AuditEvent.entity_id == sw.scan_session_id)
                .where(AuditEvent.action == "session_combined")
            ).all()
        assert any("downstream decisions kept from" in item for item in detail)
        # The combined session is one population: o1 and s1a share 100121.
        from omr_scanner.database.models import ReviewConflict

        with sw.database.session() as session:
            flagged = set(
                session.scalars(
                    select(ReviewConflict.scan_id).where(
                        ReviewConflict.conflict_type == "identifier_duplicate",
                        ReviewConflict.state != "withdrawn",
                    )
                ).all()
            )
        assert {sw.ids["o1.tif"], sw.ids["s1a.png"]} <= flagged
        codes = {
            item.code
            for item in project_health.full_check(sw.database, sw.world.session.root).issues
        }
        assert "SESSION_DOWNSTREAM_SPLIT" not in codes
        assert "SESSION_STORE_NOT_IN_SESSION" not in codes

    def test_keeping_the_other_sessions_decisions_rebinds_to_its_store(self, sw):
        other, batch = second_session_with_decisions(sw, older=False)
        scan_sessions.combine_scan_sessions(
            sw.database, [other], sw.scan_session_id, combined_by=OPERATOR,
            keep_downstream_of=other,
        )
        assert bound(sw.database, sw.scan_session_id) == batch

    def test_only_one_holder_needs_no_choice(self, sw):
        other = scan_sessions.create_scan_session(sw.database, name="Late", activate=False)
        sw.add_batch([("l1.tif", "100777", "1", 10)], scan_session_id=other.scan_session_id)
        scan_sessions.combine_scan_sessions(
            sw.database, [other.scan_session_id], sw.scan_session_id, combined_by=OPERATOR
        )
        assert bound(sw.database, sw.scan_session_id) == sw.batches[0]


class TestHealth:
    def test_a_store_outside_its_session_is_an_error(self, sw):
        other = scan_sessions.create_scan_session(sw.database, name="Elsewhere", activate=False)
        with sw.database.session() as session:
            session.execute(
                update(ScanBatch)
                .where(ScanBatch.batch_id == sw.batches[0])
                .values(scan_session_id=other.scan_session_id)
            )
        codes = {
            item.code
            for item in project_health.full_check(sw.database, sw.world.session.root).issues
        }
        assert "SESSION_STORE_NOT_IN_SESSION" in codes
