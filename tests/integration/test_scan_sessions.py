"""Scan sessions and finite batches against a real project database (0.1.1 phase 2).

Covers :mod:`omr_scanner.services.scan_sessions`: session lifecycle, the
active-session pointer, implicit creation, sealing, template pinning,
supersession and *Reprocess All*, *Combine*, the downstream single-batch rule,
processing manifests, audit events and Project Health's lifecycle checks.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.conftest import build_answer_sheet_template

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    BatchStatus,
    BatchSupersession,
    ScanBatch,
)
from omr_scanner.domain.scan_sessions import BatchMembership, BatchRole, ScanSessionState
from omr_scanner.services import (
    batch_store,
    create_project,
    open_project,
    processing_manifest,
    project_health,
    scan_sessions,
)
from omr_scanner.services.batch_store import BatchIdentity, BatchSealedError
from omr_scanner.services.scan_sessions import ScanSessionError, TemplatePinError

OPERATOR = "Session Tester"


@contextmanager
def refused(text: str) -> Iterator[None]:
    """Expect a :class:`ScanSessionError` whose operator message contains ``text``."""
    with pytest.raises(ScanSessionError) as caught:
        yield
    assert text in caught.value.user_message, caught.value.user_message


@pytest.fixture
def project(tmp_path: Path):
    session = create_project(tmp_path / "workspace", "Sessions", exam_name="Board Exam")
    yield session
    if not session.is_closed:
        session.close()


@pytest.fixture
def identity() -> BatchIdentity:
    return BatchIdentity.of(build_answer_sheet_template())


def _files(folder: Path, count: int, prefix: str = "s") -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = folder / f"{prefix}{index:03d}.png"
        path.write_bytes(b"x")
        paths.append(path)
    return paths


def _actions(database, entity_id: str) -> list[str]:
    with database.session() as session:
        return list(
            session.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.entity_id == entity_id)
                .order_by(AuditEvent.event_id)
            ).all()
        )


def _finish(database, batch_id: str) -> None:
    """Mark every scan completed and settle the batch, as a run would."""
    with database.session() as session:
        for row in session.scalars(select(BatchScan).where(BatchScan.batch_id == batch_id)):
            row.status = "completed"
    batch_store.finalise_batch(database, batch_id)


class TestSessionLifecycle:
    def test_create_rename_close_reopen(self, project) -> None:
        database = project.database
        created = scan_sessions.create_scan_session(database, name="Morning", created_by=OPERATOR)
        assert created.state is ScanSessionState.OPEN
        assert scan_sessions.active_scan_session(database).scan_session_id == (
            created.scan_session_id
        )
        renamed = scan_sessions.rename_scan_session(
            database, created.scan_session_id, "Morning sitting", renamed_by=OPERATOR
        )
        assert renamed.name == "Morning sitting"
        closed = scan_sessions.close_scan_session(
            database, created.scan_session_id, closed_by=OPERATOR
        )
        assert closed.state is ScanSessionState.CLOSED and closed.closed_by == OPERATOR
        reopened = scan_sessions.reopen_scan_session(
            database, created.scan_session_id, reopened_by=OPERATOR
        )
        assert reopened.state is ScanSessionState.OPEN
        assert reopened.reopen_count == 1
        assert reopened.final_outputs_stale_since is not None
        assert _actions(database, created.scan_session_id) == [
            "session_created",
            "session_activated",
            "session_renamed",
            "session_closed",
            "session_reopened",
        ]

    def test_illegal_transitions_are_refused(self, project) -> None:
        database = project.database
        created = scan_sessions.create_scan_session(database)
        with refused("already open"):
            scan_sessions.reopen_scan_session(database, created.scan_session_id)
        scan_sessions.close_scan_session(database, created.scan_session_id)
        with refused("already closed"):
            scan_sessions.close_scan_session(database, created.scan_session_id)
        with refused("blank"):
            scan_sessions.rename_scan_session(database, created.scan_session_id, "  ")

    def test_a_running_batch_blocks_close(self, project, identity, tmp_path) -> None:
        database = project.database
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 2), identity=identity)
        batch_store.set_batch_status(database, batch, BatchStatus.RUNNING)
        active = scan_sessions.active_scan_session(database)
        with refused("still being processed"):
            scan_sessions.close_scan_session(database, active.scan_session_id)


class TestActiveSession:
    def test_first_batch_creates_an_implicit_session_named_after_the_exam(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        assert scan_sessions.active_scan_session(database) is None
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 2), identity=identity)
        active = scan_sessions.active_scan_session(database)
        assert active is not None and active.origin == "implicit"
        assert active.name.startswith("Board Exam - ")
        info = scan_sessions.batch_info(database, batch)
        assert info.scan_session_id == active.scan_session_id
        assert info.role is BatchRole.SCAN and info.membership is BatchMembership.OPEN

    def test_the_pointer_persists_across_reopen(self, project, identity, tmp_path) -> None:
        database = project.database
        scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        expected = scan_sessions.active_scan_session(database).scan_session_id
        root = project.root
        project.close()
        with open_project(root) as reopened:
            assert scan_sessions.active_scan_session(reopened.database).scan_session_id == expected

    def test_replacing_the_active_session(self, project) -> None:
        database = project.database
        first = scan_sessions.create_scan_session(database)
        second = scan_sessions.create_scan_session(database, activate=False)
        assert scan_sessions.active_scan_session(database).scan_session_id == first.scan_session_id
        scan_sessions.set_active_scan_session(database, second.scan_session_id)
        assert scan_sessions.active_scan_session(database).scan_session_id == (
            second.scan_session_id
        )

    def test_projects_are_isolated(self, tmp_path, identity) -> None:
        one = create_project(tmp_path / "w1", "One")
        two = create_project(tmp_path / "w2", "Two")
        try:
            scan_sessions.start_batch(one.database, _files(tmp_path / "f1", 1), identity=identity)
            assert scan_sessions.active_scan_session(two.database) is None
            assert scan_sessions.list_scan_sessions(two.database) == ()
        finally:
            one.close()
            two.close()


class TestFiniteBatches:
    def test_an_open_batch_accepts_members_and_a_sealed_one_refuses(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 2), identity=identity)
        assert batch_store.add_scans_to_batch(database, batch, _files(tmp_path / "b", 1)) == 1
        assert scan_sessions.seal_batch(database, batch, sealed_by=OPERATOR) is True
        with pytest.raises(BatchSealedError):
            batch_store.add_scans_to_batch(database, batch, _files(tmp_path / "c", 1))
        # Its own members (a resume) are fine.
        members = batch_store.scan_paths(database, batch)
        assert batch_store.add_scans_to_batch(database, batch, members) == 0
        assert batch_store.load_summary(database, batch).total == 3

    def test_sealing_is_idempotent_and_audited_once(self, project, identity, tmp_path) -> None:
        database = project.database
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        assert scan_sessions.seal_batch(database, batch) is True
        assert scan_sessions.seal_batch(database, batch) is False
        assert _actions(database, batch).count("batch_sealed") == 1

    def test_a_later_process_all_lands_in_the_same_session_and_seals_the_first(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        first = scan_sessions.start_batch(database, _files(tmp_path / "a", 2), identity=identity)
        root = project.root
        project.close()
        with open_project(root) as reopened:
            database = reopened.database
            second = scan_sessions.start_batch(
                database, _files(tmp_path / "b", 2), identity=identity
            )
            one, two = scan_sessions.batches_info(database, (first, second))
            assert one.scan_session_id == two.scan_session_id
            assert len(scan_sessions.list_scan_sessions(database)) == 1
            assert one.membership is BatchMembership.SEALED
            assert two.membership is BatchMembership.OPEN

    def test_close_seals_every_open_batch_and_refuses_new_ones(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        active = scan_sessions.active_scan_session(database)
        scan_sessions.close_scan_session(database, active.scan_session_id, closed_by=OPERATOR)
        assert scan_sessions.batch_info(database, batch).membership is BatchMembership.SEALED
        with refused("closed"):
            scan_sessions.start_batch(database, _files(tmp_path / "b", 1), identity=identity)
        with refused("closed"):
            scan_sessions.start_batch(
                database, _files(tmp_path / "c", 1), identity=identity, role=BatchRole.RESCAN,
                scan_session_id=active.scan_session_id,
            )

    def test_a_rescan_batch_joins_the_same_session(self, project, identity, tmp_path) -> None:
        database = project.database
        original = scan_sessions.start_batch(database, _files(tmp_path / "a", 2), identity=identity)
        scan_sessions.seal_batch(database, original)
        owner = scan_sessions.session_of_batch(database, original)
        rescan = scan_sessions.start_batch(
            database, _files(tmp_path / "r", 1, "rescan"), identity=identity,
            role=BatchRole.RESCAN, scan_session_id=owner, seal_previous=False,
        )
        info = scan_sessions.batch_info(database, rescan)
        assert info.role is BatchRole.RESCAN and info.scan_session_id == owner
        assert batch_store.load_summary(database, original).total == 2


class TestTemplatePinning:
    def test_a_different_template_is_refused_unless_acknowledged(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        other = BatchIdentity.of(build_answer_sheet_template(questions_per_block=5))
        with pytest.raises(TemplatePinError) as refused:
            scan_sessions.start_batch(database, _files(tmp_path / "b", 1), identity=other)
        assert refused.value.differences
        batch = scan_sessions.start_batch(
            database, _files(tmp_path / "c", 1), identity=other,
            acknowledge_template_change=True, started_by=OPERATOR,
        )
        with database.session() as session:
            acknowledged = session.scalars(
                select(AuditEvent.batch_id).where(AuditEvent.action == "template_ack")
            ).all()
        assert acknowledged == [batch]

    def test_a_programmatic_batch_with_another_template_starts_a_new_session(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        first = batch_store.create_batch(database, _files(tmp_path / "a", 1), identity=identity)
        other = BatchIdentity.of(build_answer_sheet_template(questions_per_block=5))
        second = batch_store.create_batch(database, _files(tmp_path / "b", 1), identity=other)
        assert scan_sessions.session_of_batch(database, first) != (
            scan_sessions.session_of_batch(database, second)
        )
        # Nothing was sealed by the programmatic path.
        assert scan_sessions.batch_info(database, first).membership is BatchMembership.OPEN


class TestSupersessionAndReprocess:
    def test_reprocess_all_supersedes_and_keeps_the_original(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        original = scan_sessions.start_batch(database, _files(tmp_path / "a", 3), identity=identity)
        _finish(database, original)
        reprocess = scan_sessions.start_reprocess_batch(
            database, original, identity=identity, started_by=OPERATOR
        )
        old, new = scan_sessions.batches_info(database, (original, reprocess))
        assert new.role is BatchRole.REPROCESS
        assert old.superseded_by == reprocess and new.supersedes == (original,)
        assert old.membership is BatchMembership.SEALED
        assert old.scan_session_id == new.scan_session_id
        # The original is whole: its rows and results are untouched.
        assert batch_store.load_summary(database, original).completed == 3
        assert batch_store.scan_paths(database, reprocess) == batch_store.scan_paths(
            database, original
        )
        assert scan_sessions.live_supersessions(database) == {original: reprocess}

    def test_illegal_supersessions_are_refused(self, project, identity, tmp_path) -> None:
        database = project.database
        a = scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        b = scan_sessions.start_batch(database, _files(tmp_path / "b", 1), identity=identity)
        with refused("itself"):
            scan_sessions.record_supersession(database, a, a, reason="x")
        with refused("sealed"):
            scan_sessions.record_supersession(database, b, a, reason="x")
        scan_sessions.record_supersession(database, a, b, reason="first")
        scan_sessions.seal_batch(database, b)
        with refused("cycle"):
            scan_sessions.record_supersession(database, b, a, reason="back")
        c = scan_sessions.start_batch(database, _files(tmp_path / "c", 1), identity=identity)
        with refused("already superseded"):
            scan_sessions.record_supersession(database, a, c, reason="again")
        other = scan_sessions.create_scan_session(database)
        d = scan_sessions.start_batch(
            database, _files(tmp_path / "d", 1), identity=identity,
            scan_session_id=other.scan_session_id,
        )
        with refused("same scan session"):
            scan_sessions.record_supersession(database, b, d, reason="cross")

    def test_reversal_is_audited_and_keeps_the_record(self, project, identity, tmp_path) -> None:
        database = project.database
        original = scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        _finish(database, original)
        reprocess = scan_sessions.start_reprocess_batch(database, original, identity=identity)
        assert scan_sessions.reverse_supersession(
            database, original, reversed_by=OPERATOR, reason="mistake"
        )
        assert scan_sessions.live_supersessions(database) == {}
        assert not scan_sessions.reverse_supersession(database, original)
        with database.session() as session:
            rows = session.scalars(select(BatchSupersession)).all()
            assert len(rows) == 1 and rows[0].reversed_by == OPERATOR
        assert "supersede_reversed" in _actions(database, original)
        assert scan_sessions.batch_info(database, reprocess) is not None


class TestDownstreamBatch:
    def test_reads_the_newest_primary_batch_by_creation(self, project, identity, tmp_path) -> None:
        database = project.database
        first = scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        _finish(database, first)
        second = scan_sessions.start_batch(database, _files(tmp_path / "b", 1), identity=identity)
        _finish(database, second)
        assert scan_sessions.downstream_batch_id(database) == second
        # Retrying the older batch bumps its updated_at; it must not win (defect 2).
        batch_store.reprocess_failed_scans(database, first, reason="retry")
        batch_store.set_batch_status(database, first, BatchStatus.COMPLETED)
        batch_store.recover_interrupted(database)
        assert scan_sessions.downstream_batch_id(database) == second

    def test_skips_rescan_superseded_and_running_batches(self, project, identity, tmp_path) -> None:
        database = project.database
        original = scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        _finish(database, original)
        owner = scan_sessions.session_of_batch(database, original)
        rescan = scan_sessions.start_batch(
            database, _files(tmp_path / "r", 1, "r"), identity=identity,
            role=BatchRole.RESCAN, scan_session_id=owner,
        )
        _finish(database, rescan)
        assert scan_sessions.downstream_batch_id(database) == original
        reprocess = scan_sessions.start_reprocess_batch(database, original, identity=identity)
        batch_store.set_batch_status(database, reprocess, BatchStatus.RUNNING)
        assert scan_sessions.downstream_batch_id(database) is None
        _finish(database, reprocess)
        assert scan_sessions.downstream_batch_id(database) == reprocess


class TestCombine:
    def _two_sessions(self, project, identity, tmp_path) -> tuple[str, str, str | None, object]:
        database = project.database
        first = scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        other = scan_sessions.create_scan_session(database, activate=False)
        second = scan_sessions.start_batch(
            database, _files(tmp_path / "b", 1), identity=identity,
            scan_session_id=other.scan_session_id,
        )
        return first, second, scan_sessions.session_of_batch(database, first), other

    def test_combine_moves_batches_and_is_audited(self, project, identity, tmp_path) -> None:
        database = project.database
        first, second, target, other = self._two_sessions(project, identity, tmp_path)
        outcome = scan_sessions.combine_scan_sessions(
            database, [other.scan_session_id], target, combined_by=OPERATOR, reason="defect 1"
        )
        assert outcome.moved_batches == (second,)
        assert scan_sessions.session_of_batch(database, second) == target
        emptied = scan_sessions.get_scan_session(database, other.scan_session_id)
        assert emptied.merged_into_session_id == target
        assert emptied.state is ScanSessionState.CLOSED
        assert "session_combined" in _actions(database, second)
        del first

    def test_combine_needs_a_named_operator_and_an_open_target(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        _first, _second, target, other = self._two_sessions(project, identity, tmp_path)
        with refused("name"):
            scan_sessions.combine_scan_sessions(database, [other.scan_session_id], target,
                                                combined_by="")
        scan_sessions.close_scan_session(database, target)
        problems = scan_sessions.combine_problems(database, [other.scan_session_id], target)
        assert any("closed" in item for item in problems)

    def test_combine_refuses_a_duplicate_effective_sheet(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        shared = _files(tmp_path / "shared", 1)
        scan_sessions.start_batch(database, shared, identity=identity)
        target = scan_sessions.active_scan_session(database).scan_session_id
        other = scan_sessions.create_scan_session(database, activate=False)
        scan_sessions.start_batch(
            database, shared, identity=identity, scan_session_id=other.scan_session_id
        )
        with refused("twice"):
            scan_sessions.combine_scan_sessions(
                database, [other.scan_session_id], target, combined_by=OPERATOR
            )

    def test_combine_refuses_another_template_identity(self, project, identity, tmp_path) -> None:
        database = project.database
        scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        target = scan_sessions.active_scan_session(database).scan_session_id
        other = scan_sessions.create_scan_session(database, activate=False)
        scan_sessions.start_batch(
            database, _files(tmp_path / "b", 1),
            identity=BatchIdentity.of(build_answer_sheet_template(questions_per_block=5)),
            scan_session_id=other.scan_session_id,
        )
        problems = scan_sessions.combine_problems(database, [other.scan_session_id], target)
        assert any("template identity" in item for item in problems)

    def test_combine_refuses_a_session_of_another_project(
        self, project, identity, tmp_path
    ) -> None:
        database = project.database
        scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        target = scan_sessions.active_scan_session(database).scan_session_id
        other = scan_sessions.create_scan_session(database, activate=False)
        from omr_scanner.database.models import ScanSession

        with database.session() as session:
            session.get(ScanSession, other.scan_session_id).project_id = "someone-else"
        problems = scan_sessions.combine_problems(database, [other.scan_session_id], target)
        assert any("different project" in item for item in problems)


class TestManifestsAndHealth:
    def test_manifests_are_written_at_seal_and_run_end(self, project, identity, tmp_path) -> None:
        database = project.database
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 2), identity=identity)
        _finish(database, batch)
        scan_sessions.seal_batch(database, batch)
        found = processing_manifest.manifests_for(database, batch)
        assert [item["trigger"] for item in found] == ["run_finished", "sealed"]
        assert found[-1]["batch"]["scan_session_id"] == scan_sessions.session_of_batch(
            database, batch
        )
        assert found[-1]["batch"]["total_scans"] == 2

    def test_a_clean_project_has_no_lifecycle_issues(self, project, identity, tmp_path) -> None:
        database = project.database
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 2), identity=identity)
        _finish(database, batch)
        scan_sessions.start_reprocess_batch(database, batch, identity=identity)
        codes = {item.code for item in project_health.full_check(database, project.root).issues}
        assert not codes & {
            "BATCH_WITHOUT_SCAN_SESSION", "SEALED_BATCH_MEMBERSHIP_CHANGED", "SUPERSESSION_CYCLE",
            "CROSS_SESSION_SUPERSESSION", "CLOSED_SESSION_HAS_OPEN_BATCH",
        }

    def test_health_reports_lifecycle_violations(self, project, identity, tmp_path) -> None:
        database = project.database
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 2), identity=identity)
        scan_sessions.seal_batch(database, batch)
        with database.session() as session:
            session.get(ScanBatch, batch).total_scans = 5
            session.add(
                ScanBatch(
                    batch_id="orphan".ljust(32, "0"), created_at=batch_store._now(),
                    updated_at=batch_store._now(), status="completed",
                )
            )
        codes = {item.code for item in project_health.full_check(database, project.root).issues}
        assert {"SEALED_BATCH_MEMBERSHIP_CHANGED", "BATCH_WITHOUT_SCAN_SESSION"} <= codes

    def test_the_manifest_payload_is_json(self, project, identity, tmp_path) -> None:
        database = project.database
        batch = scan_sessions.start_batch(database, _files(tmp_path / "a", 1), identity=identity)
        scan_sessions.seal_batch(database, batch)
        from omr_scanner.database.models import ProcessingManifest

        with database.session() as session:
            row = session.scalars(select(ProcessingManifest)).one()
            assert json.loads(row.payload_json)["batch"]["role"] == "scan"
