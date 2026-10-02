"""Crash-safe Scan / Resolve persistence, headless (0.1.1 phase 3, ADR-0006).

Scope:
    The service-level guarantees behind S1, S2, S3 and R1, with real
    recognition results (read once per module from rendered sheets) and a
    real project database. The real-process kill matrix is
    ``tests/crash/test_crash_matrix.py``; the Scan / Resolve pages after a
    reopen are ``tests/gui/test_crash_reopen_gui.py``.

    ==== ==================================================================
    Area Covered by
    ==== ==================================================================
    S1   :class:`TestWorkUnit`, :class:`TestRecoveryCompletesReviewState`
    S2   :class:`TestRecoveryStatuses`, :class:`TestReconstruction`
    S3   :class:`TestRecorderCommitBoundary`
    R1   :class:`TestResolveDurability`
    ---  :class:`TestLifecycleUntouched`, :class:`TestDeterminism`,
         :class:`TestHealthChecks`, :class:`TestResyncIdempotence`
    ==== ==================================================================

What "the shape a crash leaves" means here:
    These tests write the exact database state a killed process leaves - a
    batch still ``running``, rows still ``queued``, results committed with or
    without their conflicts - through the same service functions the Scan
    stage uses, then call the same recovery the main window calls. That a
    *real* kill leaves these shapes is what the crash matrix proves.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import cv2
import pytest
from sqlalchemy import func, select, update
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    BatchStatus,
    BatchSupersession,
    ProcessingManifest,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
    ScanSession,
)
from omr_scanner.domain.review import ConflictState, ConflictType, ReasonCode
from omr_scanner.domain.scan_sessions import BatchMembership, ScanSessionState
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import (
    batch_store,
    create_project,
    project_health,
    review_store,
    save_template,
    scan_recovery,
    scan_sessions,
)
from omr_scanner.services.batch_processor import BatchOptions, ProcessedScan, process_batch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services import ProjectSession

REVIEWER = "Phase Three Tester"


def _marks(roll: str | None) -> dict:
    marks: dict = {
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }
    if roll is not None:
        marks["roll_number"] = dict(enumerate(roll))
    return marks


@pytest.fixture(scope="module")
def template() -> OmrTemplate:
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def sheets(tmp_path_factory: pytest.TempPathFactory, template: OmrTemplate) -> list[Path]:
    """Twelve sheets: eight clean, two sharing a roll, one with no roll, one unreadable."""
    directory = tmp_path_factory.mktemp("phase3_sheets")
    rolls: list[str | None] = [f"2000{index:02d}" for index in range(8)]
    rolls += ["300000", "300000", None]
    paths = []
    for index, roll in enumerate(rolls):
        path = directory / f"sheet_{index:02d}.png"
        cv2.imwrite(str(path), render_marked_sheet(template, _marks(roll)))
        paths.append(path)
    corrupt = directory / "sheet_99_corrupt.png"
    corrupt.write_bytes(b"not an image at all")
    paths.append(corrupt)
    return paths


@pytest.fixture(scope="module")
def outcomes(sheets: list[Path], template: OmrTemplate) -> list[ProcessedScan]:
    """Recognised once for the module; every test records copies of these."""
    report = process_batch(sheets, template, options=BatchOptions(), workers=1)
    assert len(report.processed) == len(sheets)
    return list(report.processed)


@pytest.fixture
def project(workspace: Path, template: OmrTemplate):  # type: ignore[no-untyped-def]
    session = create_project(workspace, "Crash Safe Examination")
    path = session.project.layout.templates_dir / "sheet.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    save_template(template, path)
    try:
        yield session, path
    finally:
        session.close()


def _register(
    database: ProjectDatabase, sheets: list[Path], template: OmrTemplate, path: Path
) -> str:
    return batch_store.create_batch(
        database, sheets, identity=batch_store.BatchIdentity.of(template, path)
    )


def _conflict_identities(database: ProjectDatabase, batch_id: str) -> set[tuple]:
    with database.session() as session:
        rows = session.execute(
            select(
                BatchScan.filename,
                ReviewConflict.conflict_type,
                ReviewConflict.zone_id,
                ReviewConflict.group_key,
                ReviewConflict.state,
                ReviewConflict.machine_value,
            )
            .join(BatchScan, BatchScan.scan_id == ReviewConflict.scan_id)
            .where(ReviewConflict.batch_id == batch_id)
        ).all()
    return {tuple(row) for row in rows}


def _counts(database: ProjectDatabase) -> dict[str, int]:
    with database.session() as session:
        def count(model: object) -> int:
            return int(session.scalar(select(func.count()).select_from(model)) or 0)  # type: ignore[arg-type]

        return {
            "sessions": count(ScanSession),
            "batches": count(ScanBatch),
            "supersessions": count(BatchSupersession),
            "scans": count(BatchScan),
            "conflicts": count(ReviewConflict),
            "audit": count(AuditEvent),
            "manifests": count(ProcessingManifest),
        }


def _reference(database: ProjectDatabase, batch_id: str, template: OmrTemplate, outcomes) -> None:  # type: ignore[no-untyped-def]
    """An uninterrupted run, as the Scan stage records it."""
    recorder = batch_store.BatchRecorder(
        database=database, batch_id=batch_id, template=template, commit_when_idle=True
    )
    for item in outcomes:
        recorder.record(item)
    assert recorder.flush()
    scan_recovery.complete_batch_review_state(database, batch_id)
    batch_store.finalise_batch(database, batch_id)


def _set_running(database: ProjectDatabase, batch_id: str, queued: list[Path]) -> None:
    batch_store.mark_queued(database, batch_id, queued)
    batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)


# ----------------------------------------------------------------------
# S1 - the durable work unit
# ----------------------------------------------------------------------
class TestWorkUnit:
    def test_a_sheets_conflicts_commit_in_the_same_transaction_as_its_result(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        failed = next(item for item in outcomes if item.outcome.value == "error")

        batch_store.record_results(database, batch_id, [failed], template=template)

        scan_id = batch_store.scan_ids_by_path(database, batch_id)[failed.source_path]
        with database.session() as db:
            conflicts = db.scalars(
                select(ReviewConflict).where(ReviewConflict.scan_id == scan_id)
            ).all()
            row = db.get(BatchScan, scan_id)
            assert row is not None and row.status == ScanJobStatus.FAILED.value
        # A failed read always raises exactly one sheet conflict.
        assert len(conflicts) == 1
        assert conflicts[0].conflict_type == ConflictType.IMAGE_UNREADABLE.value

    def test_a_failure_while_writing_conflicts_rolls_the_result_back(
        self, project, sheets, outcomes, template, monkeypatch
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        batch_store.mark_queued(database, batch_id, sheets)

        def explode(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("simulated failure inside the work unit")

        monkeypatch.setattr(review_store, "sync_conflicts_in_session", explode)
        with pytest.raises(RuntimeError):
            batch_store.record_results(database, batch_id, outcomes[:3], template=template)

        with database.session() as db:
            statuses = db.scalars(
                select(BatchScan.status).where(BatchScan.batch_id == batch_id)
            ).all()
            results = db.scalars(
                select(BatchScan.result_json).where(BatchScan.batch_id == batch_id)
            ).all()
        # Nothing of the unit survives: no sheet is recorded as read without
        # the review state its read implies.
        assert set(statuses) == {ScanJobStatus.QUEUED.value}
        assert set(results) == {""}

    def test_without_a_template_only_recognition_is_written(
        self, project, sheets, outcomes, template
    ):
        # The recognition-only path (headless stress tool, benchmark) is
        # unchanged; it is also the shape an earlier build left mid-run.
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        batch_store.record_results(database, batch_id, outcomes)
        assert _counts(database)["conflicts"] == 0


class TestRecorderCommitBoundary:
    """S3: what the Scan stage counts is exactly what has committed."""

    def test_on_commit_reports_only_committed_sheets(self, project, sheets, outcomes, template):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        committed: list[Path] = []

        def seen(items: object) -> None:
            for item in items:  # type: ignore[attr-defined]
                # The callback runs after the transaction: the row is durable.
                with database.session() as db:
                    status = db.scalar(
                        select(BatchScan.status)
                        .where(BatchScan.batch_id == batch_id)
                        .where(BatchScan.source_path == str(item.source_path))
                    )
                assert status in ("completed", "warning", "failed")
                committed.append(item.source_path)

        recorder = batch_store.BatchRecorder(
            database=database, batch_id=batch_id, template=template, on_commit=seen, flush_every=5
        )
        for item in outcomes[:7]:
            recorder.record(item)
        assert len(committed) == 5 and recorder.unsaved == 2
        recorder.flush()
        assert committed == [item.source_path for item in outcomes[:7]]

    def test_a_failing_store_reports_nothing_as_committed(
        self, project, sheets, outcomes, template, monkeypatch
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        calls: list[object] = []

        def broken(*_args: object, **_kwargs: object) -> None:
            raise OMRScannerError("disk full", user_message="disk full")

        monkeypatch.setattr(batch_store, "record_results", broken)
        recorder = batch_store.BatchRecorder(
            database=database, batch_id=batch_id, template=template, on_commit=calls.append
        )
        for item in outcomes[:3]:
            recorder.record(item)
        assert recorder.flush() is False
        assert calls == []
        assert recorder.persisted == 0
        assert [item.source_path for item in recorder.unsaved_results()] == [
            item.source_path for item in outcomes[:3]
        ]

    def test_commit_when_idle_commits_each_sheet_alone_when_the_writer_keeps_up(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        groups: list[int] = []
        recorder = batch_store.BatchRecorder(
            database=database,
            batch_id=batch_id,
            template=template,
            commit_when_idle=True,
            on_commit=lambda items: groups.append(len(items)),
        )
        for item in outcomes[:6]:
            recorder.record(item)
            # Sheets arriving more slowly than a commit takes.
            time.sleep(0.25)
        recorder.flush()
        assert groups == [1] * 6

    def test_commit_when_idle_lets_a_backlog_share_a_commit(
        self, project, sheets, outcomes, template, monkeypatch
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        real = batch_store.record_results

        def slow(*args: object, **kwargs: object) -> None:
            time.sleep(0.2)
            real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(batch_store, "record_results", slow)
        groups: list[int] = []
        recorder = batch_store.BatchRecorder(
            database=database,
            batch_id=batch_id,
            template=template,
            commit_when_idle=True,
            flush_every=4,
            on_commit=lambda items: groups.append(len(items)),
        )
        for item in outcomes[:9]:
            recorder.record(item)
        recorder.flush()
        assert sum(groups) == 9
        assert groups[0] == 1  # the first sheet never waits
        assert max(groups) > 1  # arrivals faster than a commit coalesce ...
        assert max(groups) <= 4  # ... and never beyond the group bound


# ----------------------------------------------------------------------
# S1 - recovery completes review state, from stored results only
# ----------------------------------------------------------------------
class TestRecoveryCompletesReviewState:
    def test_conflicts_an_earlier_build_never_wrote_are_created_exactly_once(
        self, project, sheets, outcomes, template, workspace
    ):
        # Reference: the same sheets, uninterrupted.
        reference = create_project(workspace, "Reference Examination")
        try:
            ref_batch = _register(reference.database, sheets, template, project[1])
            _reference(reference.database, ref_batch, template, outcomes)
            expected = {row[:6] for row in _conflict_identities(reference.database, ref_batch)}
        finally:
            reference.close()

        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        # Recognition committed, conflicts never written - what a pre-phase-3
        # build left when it was killed before its run ended.
        batch_store.record_results(database, batch_id, outcomes)
        assert _counts(database)["conflicts"] == 0

        report = scan_recovery.recover_on_open(database, templates=[template])

        assert report.interrupted_batches == 1
        assert report.batches[0].sheets_resynced == len(outcomes)
        assert _conflict_identities(database, batch_id) == expected
        with database.session() as db:
            detected = db.scalar(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "detected")
            )
        assert detected == len(expected)

    def test_a_scan_stage_batch_is_not_re_derived_but_still_completed(
        self, project, sheets, outcomes, template
    ):
        # The Scan stage marks its batches: every result committed with its
        # conflicts. Recovery then skips the per-row re-derivation (minutes on
        # a large batch) and still completes the batch-scope state.
        session, path = project
        database = session.database
        batch_id = batch_store.create_batch(
            database,
            sheets,
            identity=batch_store.BatchIdentity.of(template, path),
            settings={scan_recovery.WORK_UNIT_SETTING: True},
        )
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes, template=template)

        report = scan_recovery.recover_on_open(database, templates=[template])

        assert report.batches[0].sheets_resynced == 0
        kinds = {row[1] for row in _conflict_identities(database, batch_id)}
        assert ConflictType.IDENTIFIER_DUPLICATE.value in kinds

    def test_batch_scope_review_state_is_completed_after_a_crash(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        # Every work unit committed; killed before the duplicate-ID pass.
        batch_store.record_results(database, batch_id, outcomes, template=template)
        types = {row[1] for row in _conflict_identities(database, batch_id)}
        assert ConflictType.IDENTIFIER_DUPLICATE.value not in types

        scan_recovery.recover_on_open(database, templates=[template])

        duplicates = [
            row
            for row in _conflict_identities(database, batch_id)
            if row[1] == ConflictType.IDENTIFIER_DUPLICATE.value
        ]
        assert sorted(row[0] for row in duplicates) == ["sheet_08.png", "sheet_09.png"]

    def test_recovery_never_recognises_a_sheet(
        self, project, sheets, outcomes, template, monkeypatch
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes[:6])
        before = {
            row.filename: (row.attempt_count, row.result_json)
            for row in _rows(database, batch_id)
        }

        def forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("recovery must not recognise a sheet")

        import omr_scanner.services.recognition_service as recognition_service

        monkeypatch.setattr(recognition_service, "recognise_scan", forbidden)
        monkeypatch.setattr("omr_scanner.services.batch_processor.recognise_scan", forbidden)
        scan_recovery.recover_on_open(database, templates=[template])

        after = {
            row.filename: (row.attempt_count, row.result_json)
            for row in _rows(database, batch_id)
        }
        assert after == before

    def test_a_missing_template_still_recovers_statuses_and_says_so(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes[:4])
        path.unlink()

        report = scan_recovery.recover_on_open(database, templates=[])

        assert report.batches[0].template_found is False
        assert report.warnings
        summary = batch_store.load_summary(database, batch_id)
        assert summary is not None and summary.status == BatchStatus.INTERRUPTED.value

    def test_a_failed_sheet_left_without_its_conflict_is_repaired_in_a_finished_batch(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _reference(database, batch_id, template, outcomes)
        failed = next(item for item in outcomes if item.outcome.value == "error")
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[failed.source_path]
        with database.session() as db:
            db.execute(
                ReviewConflict.__table__.delete().where(ReviewConflict.scan_id == scan_id)
            )
        health = project_health.full_check(database, session.root)
        assert "SCAN_REVIEW_STATE_MISSING" in {issue.code for issue in health.issues}

        scan_recovery.recover_on_open(database, templates=[template])

        health = project_health.full_check(database, session.root)
        assert "SCAN_REVIEW_STATE_MISSING" not in {issue.code for issue in health.issues}


def _rows(database: ProjectDatabase, batch_id: str) -> list[BatchScan]:
    with database.session() as session:
        return list(
            session.scalars(
                select(BatchScan)
                .where(BatchScan.batch_id == batch_id)
                .order_by(BatchScan.batch_index)
            ).all()
        )


# ----------------------------------------------------------------------
# S2 - statuses after recovery
# ----------------------------------------------------------------------
class TestRecoveryStatuses:
    def test_in_flight_sheets_become_pending_never_failed_or_completed(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes[:5], template=template)

        scan_recovery.recover_on_open(database, templates=[template])

        rows = _rows(database, batch_id)
        assert [row.status for row in rows[5:]] == [ScanJobStatus.PENDING.value] * (len(rows) - 5)
        assert all(row.result_json == "" for row in rows[5:])
        summary = batch_store.load_summary(database, batch_id)
        assert summary is not None
        assert summary.status == BatchStatus.INTERRUPTED.value
        assert summary.pending == len(rows) - 5
        assert batch_store.resumable_scans(database, batch_id) == tuple(sheets[5:])

    def test_a_recorded_failure_stays_a_failure(self, project, sheets, outcomes, template):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes, template=template)

        scan_recovery.recover_on_open(database, templates=[template])

        summary = batch_store.load_summary(database, batch_id)
        assert summary is not None and summary.failed == 1
        assert summary.status == BatchStatus.COMPLETED_WITH_ERRORS.value

    def test_recovery_writes_no_processing_manifest(self, project, sheets, outcomes, template):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes[:3], template=template)
        before = _counts(database)["manifests"]
        scan_recovery.recover_on_open(database, templates=[template])
        assert _counts(database)["manifests"] == before


class TestLifecycleUntouched:
    def test_no_session_batch_or_supersession_is_created(self, project, sheets, outcomes, template):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes[:5], template=template)
        before = _counts(database)
        info_before = scan_sessions.batch_info(database, batch_id)

        scan_recovery.recover_on_open(database, templates=[template])

        after = _counts(database)
        for key in ("sessions", "batches", "supersessions", "scans"):
            assert after[key] == before[key], key
        info_after = scan_sessions.batch_info(database, batch_id)
        assert info_after is not None and info_before is not None
        assert info_after.scan_session_id == info_before.scan_session_id
        assert info_after.role == info_before.role
        assert info_after.membership is info_before.membership is BatchMembership.OPEN
        current = scan_sessions.active_scan_session(database)
        assert current is not None and current.state is ScanSessionState.OPEN

    def test_a_sealed_batch_stays_sealed_and_gains_no_members(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        batch_store.record_results(database, batch_id, outcomes[:4], template=template)
        scan_sessions.seal_batch(database, batch_id, sealed_by=REVIEWER)
        sealed_at = scan_sessions.batch_info(database, batch_id).sealed_at  # type: ignore[union-attr]
        _set_running(database, batch_id, sheets[4:])

        scan_recovery.recover_on_open(database, templates=[template])

        info = scan_sessions.batch_info(database, batch_id)
        assert info is not None
        assert info.membership is BatchMembership.SEALED
        assert info.sealed_at == sealed_at
        assert info.total_scans == len(sheets)
        assert batch_store.resumable_scans(database, batch_id) == tuple(sheets[4:])
        # Its own unfinished members may still be offered to it.
        assert batch_store.add_scans_to_batch(database, batch_id, sheets) == 0

    def test_a_closed_session_stays_closed(self, project, sheets, outcomes, template):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        batch_store.record_results(database, batch_id, outcomes[:4], template=template)
        current = scan_sessions.active_scan_session(database)
        assert current is not None
        scan_sessions.close_scan_session(database, current.scan_session_id, closed_by=REVIEWER)
        # The shape a crash leaves if the batch was being resumed.
        _set_running(database, batch_id, sheets[4:])

        scan_recovery.recover_on_open(database, templates=[template])

        after = scan_sessions.get_scan_session(database, current.scan_session_id)
        assert after is not None and after.state is ScanSessionState.CLOSED


class TestDeterminism:
    def test_repeated_recovery_changes_nothing(self, project, sheets, outcomes, template):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes[:7])

        scan_recovery.recover_on_open(database, templates=[template])
        first = (_counts(database), _conflict_identities(database, batch_id), _statuses(database))
        for _ in range(3):
            report = scan_recovery.recover_on_open(database, templates=[template])
            assert report.interrupted_batches == 0
            again = (
                _counts(database), _conflict_identities(database, batch_id), _statuses(database)
            )
            assert again == first


def _statuses(database: ProjectDatabase) -> list[tuple[str, str]]:
    with database.session() as session:
        return [
            (str(name), str(status))
            for name, status in session.execute(
                select(BatchScan.filename, BatchScan.status).order_by(BatchScan.filename)
            ).all()
        ] + [
            (str(batch_id), str(status))
            for batch_id, status in session.execute(
                select(ScanBatch.batch_id, ScanBatch.status).order_by(ScanBatch.batch_id)
            ).all()
        ]


# ----------------------------------------------------------------------
# Reconstruction
# ----------------------------------------------------------------------
class TestReconstruction:
    def test_progress_is_derived_from_committed_rows(self, project, sheets, outcomes, template):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes[:6], template=template)
        scan_recovery.recover_on_open(database, templates=[template])

        progress = scan_recovery.scan_progress(database, batch_id)
        assert progress is not None
        assert (progress.total, progress.recognised, progress.pending) == (len(sheets), 6, 6)

    def test_reopen_targets_the_unfinished_batch_and_its_resolve_queue(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _set_running(database, batch_id, sheets)
        batch_store.record_results(database, batch_id, outcomes[:6], template=template)
        scan_recovery.recover_on_open(database, templates=[template])

        targets = scan_recovery.restore_targets(database)
        assert targets.scan_batch_id == batch_id
        assert targets.resolve_batch_id == batch_id

    def test_a_finished_batch_is_not_restored_to_scan_but_still_to_resolve(
        self, project, sheets, outcomes, template
    ):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _reference(database, batch_id, template, outcomes)

        targets = scan_recovery.restore_targets(database)
        assert targets.scan_batch_id is None
        assert targets.resolve_batch_id == batch_id


# ----------------------------------------------------------------------
# R1 - Resolve decisions are their own durable units
# ----------------------------------------------------------------------
class TestResolveDurability:
    def test_committed_decisions_survive_a_reopen_and_the_queue_is_exactly_the_rest(
        self, workspace, sheets, outcomes, template
    ):
        session = create_project(workspace, "Resolve Durability")
        root = session.root
        path = session.project.layout.templates_dir / "sheet.omrt"
        path.parent.mkdir(parents=True, exist_ok=True)
        save_template(template, path)
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _reference(database, batch_id, template, outcomes)
        queue = review_store.list_conflicts(database, batch_id)
        assert len(queue) >= 3
        corrected = next(item for item in queue if item.allows_value_correction)
        accepted = next(item for item in queue if item.conflict_id != corrected.conflict_id)
        review_store.correct_value(
            database,
            corrected.conflict_id,
            value="9",
            reviewer=REVIEWER,
            reason=ReasonCode.OTHER,
            reason_text="read from the paper",
        )
        review_store.accept_machine_value(database, accepted.conflict_id, reviewer=REVIEWER)
        history_before = review_store.history_for(database, corrected.conflict_id)
        session.close()

        from omr_scanner.services import open_project

        reopened = open_project(root)
        try:
            database = reopened.database
            scan_recovery.recover_on_open(database, templates=[template])
            provenance = review_store.provenance_for(database, corrected.conflict_id)
            assert provenance.value == "9"
            record = review_store.get_conflict(database, corrected.conflict_id)
            assert record is not None
            assert record.observation.value == corrected.observation.value  # machine kept
            assert review_store.history_for(database, corrected.conflict_id) == history_before
            counts = scan_recovery.resolve_progress(database, batch_id)
            assert counts.resolved == 2
            unresolved = review_store.list_conflicts(
                database,
                batch_id,
                filters=review_store.ConflictFilter(
                    states=(ConflictState.OPEN, ConflictState.DEFERRED)
                ),
            )
            assert {item.conflict_id for item in unresolved} == {
                item.conflict_id for item in queue
            } - {corrected.conflict_id, accepted.conflict_id}
            assert counts.unresolved == len(unresolved)
        finally:
            reopened.close()


# ----------------------------------------------------------------------
# Project Health
# ----------------------------------------------------------------------
class TestHealthChecks:
    CODES: ClassVar[set[str]] = {
        "BATCH_LEFT_RUNNING",
        "SCAN_COMPLETED_WITHOUT_RESULT",
        "SCAN_REVIEW_STATE_MISSING",
        "REVIEW_STATE_WITHOUT_RECOGNITION",
        "CONFLICT_DETECTED_TWICE",
    }

    def _codes(self, session: ProjectSession) -> set[str]:
        report = project_health.full_check(session.database, session.root)
        return {issue.code for issue in report.issues}

    def test_a_cleanly_processed_batch_raises_none(self, project, sheets, outcomes, template):
        session, path = project
        batch_id = _register(session.database, sheets, template, path)
        _reference(session.database, batch_id, template, outcomes)
        assert not (self._codes(session) & self.CODES)

    def test_each_inconsistency_is_reported(self, project, sheets, outcomes, template):
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        _reference(database, batch_id, template, outcomes)
        rows = _rows(database, batch_id)
        with database.session() as db:
            db.execute(update(ScanBatch).values(status=BatchStatus.RUNNING.value))
            # A sheet "read" with no stored result, and a conflict left on it.
            conflicted = db.scalar(select(ReviewConflict.scan_id).limit(1))
            db.execute(
                update(BatchScan).where(BatchScan.scan_id == conflicted).values(result_json="")
            )
            failed = next(row for row in rows if row.status == ScanJobStatus.FAILED.value)
            db.execute(
                ReviewConflict.__table__.delete().where(ReviewConflict.scan_id == failed.scan_id)
            )
            any_conflict = db.scalar(select(ReviewConflict.conflict_id).limit(1))
            db.add(
                AuditEvent(
                    occurred_at=batch_store._now(),
                    batch_id=batch_id,
                    conflict_id=any_conflict,
                    action="detected",
                )
            )
        assert self._codes(session) >= self.CODES


class TestResyncIdempotence:
    def test_resyncing_a_withdrawn_conflict_appends_nothing(
        self, project, sheets, outcomes, template
    ):
        # Regression: an identity test (`is`) on a state read back from SQLite
        # never matched, so every re-sync withdrew it again with a new event.
        session, path = project
        database = session.database
        batch_id = _register(database, sheets, template, path)
        blank = next(
            item for item in outcomes if item.source_path.name == "sheet_10.png"
        )
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[blank.source_path]
        batch_store.record_results(database, batch_id, [blank], template=template)
        clean = next(item for item in outcomes if item.source_path.name == "sheet_00.png")
        # The same sheet re-read cleanly: its conflict is withdrawn once.
        reread = type(blank)(result=type(blank.result).from_dict(
            {**clean.result.to_dict(), "source_path": str(blank.source_path)}
        ))
        review_store.sync_conflicts(
            database, batch_id=batch_id, scan_id=scan_id, result=reread.result, template=template
        )
        with database.session() as db:
            states = set(db.scalars(select(ReviewConflict.state)).all())
            events = db.scalar(select(func.count()).select_from(AuditEvent))
        assert states == {ConflictState.WITHDRAWN.value}
        for _ in range(3):
            review_store.sync_conflicts(
                database,
                batch_id=batch_id,
                scan_id=scan_id,
                result=reread.result,
                template=template,
            )
        with database.session() as db:
            assert db.scalar(select(func.count()).select_from(AuditEvent)) == events


def test_the_recorded_result_round_trips_for_rederivation(outcomes, template):
    """Recovery re-derives from ``result_json``; it must say what the live read said."""
    from omr_scanner.services.conflict_policy import detect_conflicts
    from omr_scanner.services.recognition_models import ScanResult

    for item in outcomes:
        stored = ScanResult.from_dict(json.loads(json.dumps(item.result.to_dict())))
        assert detect_conflicts(stored, template) == detect_conflicts(item.result, template)
