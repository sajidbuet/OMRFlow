"""Tests for durable batch state (Phase 5).

Scope:
    The persistence *rules* - which scans a resume picks up, what a crash
    leaves behind and how it is repaired, when two configurations may not be
    mixed, and what happens when the store itself fails. Recognition never
    runs here; results are hand-built, because the point is to test the state
    machine rather than the engine that ordinarily drives it.

Why a real SQLite file rather than a mock:
    Every interesting property of this module is a property of the database -
    the foreign key, the unique constraint, the transaction boundary, the
    grouped count. A mocked session would assert that the code calls the
    functions it calls, which is the one thing worth nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template
from tests.unit.test_recognition_contract import make_answer, make_result

from omr_scanner.database import open_project_database
from omr_scanner.database.models import BatchStatus, ScanJobStatus
from omr_scanner.services import batch_store
from omr_scanner.services.batch_processor import ProcessedScan
from omr_scanner.services.recognition_models import (
    RecognitionOutcome,
    RegistrationStatus,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase


@pytest.fixture
def database(tmp_path: Path) -> Iterator[ProjectDatabase]:
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def identity(template):
    return batch_store.BatchIdentity.of(template, Path("sheet.omrt"))


def paths_for(count: int, root: Path) -> list[Path]:
    """Create ``count`` real (tiny) files so ``stat`` has something to read."""
    made = []
    for index in range(count):
        path = root / f"scan_{index:03d}.png"
        path.write_bytes(b"not really an image")
        made.append(path)
    return made


def outcome_for(path: Path, outcome: RecognitionOutcome) -> ProcessedScan:
    """One finished sheet with the given outcome."""
    registration = (
        RegistrationStatus.FAILED
        if outcome
        in (RecognitionOutcome.ERROR, RecognitionOutcome.REGISTRATION_FAILED)
        else RegistrationStatus.REGISTERED
    )
    result = make_result(
        source_path=path,
        outcome=outcome,
        registration=registration,
        answers=(make_answer(1),),
        fields=(),
        bubbles=(),
        warnings=(),
    )
    return ProcessedScan(result=result, output_name=f"{path.stem}.png")


class TestRegisteringABatch:
    def test_every_scan_starts_pending(self, database, identity, tmp_path):
        paths = paths_for(4, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)

        summary = batch_store.load_summary(database, batch_id)
        assert summary is not None
        assert summary.total == 4
        assert summary.pending == 4
        assert summary.processed == 0
        assert summary.status == BatchStatus.NEW.value

    def test_batch_order_is_preserved_exactly(self, database, identity, tmp_path):
        # Batch order decides which of two sheets sharing a roll number keeps
        # the plain file name, so a resume that reordered them would rename
        # different sheets than the original run would have.
        paths = paths_for(6, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        assert list(batch_store.scan_paths(database, batch_id)) == paths

    def test_a_missing_file_is_still_registered(self, database, identity, tmp_path):
        # A file that vanished between listing the folder and registering the
        # batch must become a scan that *fails*, with a reason - not a scan
        # that quietly never existed.
        ghost = tmp_path / "gone.png"
        batch_id = batch_store.create_batch(database, [ghost], identity=identity)
        assert batch_store.scan_paths(database, batch_id) == (ghost,)

    def test_two_batches_are_independent(self, database, identity, tmp_path):
        first = batch_store.create_batch(database, paths_for(2, tmp_path), identity=identity)
        other = tmp_path / "other"
        other.mkdir()
        second = batch_store.create_batch(database, paths_for(3, other), identity=identity)
        assert first != second
        assert len(batch_store.list_batches(database)) == 2


class TestRecordingResults:
    def test_results_are_counted_by_outcome(self, database, identity, tmp_path):
        paths = paths_for(3, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database,
            batch_id,
            [
                outcome_for(paths[0], RecognitionOutcome.COMPLETE),
                outcome_for(paths[1], RecognitionOutcome.REVIEW),
                outcome_for(paths[2], RecognitionOutcome.ERROR),
            ],
        )

        summary = batch_store.load_summary(database, batch_id)
        assert (summary.completed, summary.warning, summary.failed) == (1, 1, 1)
        assert summary.pending == 0
        assert summary.processed == 3

    def test_a_recorded_result_round_trips_as_a_scanresult(
        self, database, identity, tmp_path
    ):
        # The claim "resume keeps earlier work" is only true if the earlier
        # work comes back as a usable result, not merely as a row saying it
        # happened.
        paths = paths_for(1, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database, batch_id, [outcome_for(paths[0], RecognitionOutcome.COMPLETE)]
        )

        restored = batch_store.completed_results(database, batch_id)
        assert len(restored) == 1
        assert restored[0].source_path == paths[0]
        assert restored[0].outcome is RecognitionOutcome.COMPLETE

    def test_recording_the_same_scan_twice_increments_the_attempt_count(
        self, database, identity, tmp_path
    ):
        paths = paths_for(1, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database, batch_id, [outcome_for(paths[0], RecognitionOutcome.ERROR)]
        )
        batch_store.record_results(
            database, batch_id, [outcome_for(paths[0], RecognitionOutcome.COMPLETE)]
        )

        summary = batch_store.load_summary(database, batch_id)
        # One row, not two: a retry updates the scan it retried.
        assert summary.total == 1
        assert summary.completed == 1
        assert summary.failed == 0

    def test_a_failure_records_a_machine_readable_category(
        self, database, identity, tmp_path
    ):
        # An operator triaging four hundred failures needs to know whether they
        # are looking at one bad scanner batch or four hundred problems, so the
        # category has to survive into the row rather than only exist in prose.
        from sqlalchemy import select

        from omr_scanner.database.models import BatchScan

        paths = paths_for(1, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        failed = make_result(
            source_path=paths[0],
            outcome=RecognitionOutcome.REGISTRATION_FAILED,
            registration=RegistrationStatus.FAILED,
            registration_message="Only 2 of 4 registration markers were found.",
            status_codes=("MARKER_NOT_FOUND",),
            answers=(),
            fields=(),
            bubbles=(),
            warnings=(),
        )
        assert batch_store.categorise_error(failed) == batch_store.ErrorCategory.REGISTRATION

        batch_store.record_results(database, batch_id, [ProcessedScan(result=failed)])
        with database.session() as session:
            row = session.scalars(
                select(BatchScan).where(BatchScan.batch_id == batch_id)
            ).one()
            assert row.status == ScanJobStatus.FAILED.value
            assert row.error_category == batch_store.ErrorCategory.REGISTRATION
            assert "2 of 4" in row.error_message
            assert row.attempt_count == 1


class TestResumeSelection:
    def test_resume_returns_only_unfinished_scans(self, database, identity, tmp_path):
        paths = paths_for(5, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database,
            batch_id,
            [
                outcome_for(paths[0], RecognitionOutcome.COMPLETE),
                outcome_for(paths[1], RecognitionOutcome.REVIEW),
                outcome_for(paths[2], RecognitionOutcome.ERROR),
            ],
        )

        remaining = batch_store.resumable_scans(database, batch_id)
        # Completed and needs-review are finished work. A failure is finished
        # too - resume does not silently read it again.
        assert remaining == (paths[3], paths[4])

    def test_resume_can_be_asked_to_include_failures(self, database, identity, tmp_path):
        paths = paths_for(3, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database,
            batch_id,
            [
                outcome_for(paths[0], RecognitionOutcome.COMPLETE),
                outcome_for(paths[1], RecognitionOutcome.ERROR),
            ],
        )
        assert batch_store.resumable_scans(database, batch_id, include_failed=True) == (
            paths[1],
            paths[2],
        )
        assert batch_store.failed_scans(database, batch_id) == (paths[1],)

    def test_resume_keeps_batch_order(self, database, identity, tmp_path):
        paths = paths_for(6, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        # Finish them out of order, as a multicore run would.
        batch_store.record_results(
            database,
            batch_id,
            [
                outcome_for(paths[4], RecognitionOutcome.COMPLETE),
                outcome_for(paths[1], RecognitionOutcome.COMPLETE),
            ],
        )
        assert batch_store.resumable_scans(database, batch_id) == (
            paths[0],
            paths[2],
            paths[3],
            paths[5],
        )


class TestCancellationAndRecovery:
    def test_cancelling_leaves_unfinished_scans_resumable(
        self, database, identity, tmp_path
    ):
        paths = paths_for(4, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database, batch_id, [outcome_for(paths[0], RecognitionOutcome.COMPLETE)]
        )
        changed = batch_store.mark_cancelled(database, batch_id)

        assert changed == 3
        summary = batch_store.load_summary(database, batch_id)
        assert summary.status == BatchStatus.CANCELLED.value
        assert summary.completed == 1
        assert summary.pending == 3
        assert len(batch_store.resumable_scans(database, batch_id)) == 3

    def test_a_crash_leaves_stale_processing_rows_that_recovery_repairs(
        self, database, identity, tmp_path
    ):
        # Simulate the shape a crash leaves: rows claimed by a process that no
        # longer exists. Nothing else would ever move them on, so a resume
        # that ignored them would skip exactly the sheets that were in flight.
        paths = paths_for(4, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.mark_queued(database, batch_id, paths[:2])
        batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)
        assert batch_store.resumable_scans(database, batch_id) == tuple(paths)

        batches, scans = batch_store.recover_interrupted(database)

        assert (batches, scans) == (1, 2)
        summary = batch_store.load_summary(database, batch_id)
        assert summary.status == BatchStatus.INTERRUPTED.value
        # Repaired to PENDING, never to FAILED: "we do not know what happened"
        # is not the same as "this sheet is bad".
        assert summary.failed == 0
        assert summary.pending == 4

    def test_recovery_is_safe_to_run_when_nothing_is_stale(
        self, database, identity, tmp_path
    ):
        batch_store.create_batch(database, paths_for(2, tmp_path), identity=identity)
        assert batch_store.recover_interrupted(database) == (0, 0)

    def test_recovery_does_not_touch_finished_work(self, database, identity, tmp_path):
        paths = paths_for(3, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database, batch_id, [outcome_for(paths[0], RecognitionOutcome.COMPLETE)]
        )
        batch_store.mark_queued(database, batch_id, paths[1:])
        batch_store.recover_interrupted(database)

        summary = batch_store.load_summary(database, batch_id)
        assert summary.completed == 1
        assert summary.pending == 2


class TestFinalStatus:
    def test_a_clean_run_completes(self, database, identity, tmp_path):
        paths = paths_for(2, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database,
            batch_id,
            [outcome_for(path, RecognitionOutcome.COMPLETE) for path in paths],
        )
        summary = batch_store.finalise_batch(database, batch_id)
        assert summary.status == BatchStatus.COMPLETED.value

    def test_a_run_with_a_failure_says_so(self, database, identity, tmp_path):
        # "The loop finished" and "the work succeeded" are different claims.
        paths = paths_for(2, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database,
            batch_id,
            [
                outcome_for(paths[0], RecognitionOutcome.COMPLETE),
                outcome_for(paths[1], RecognitionOutcome.ERROR),
            ],
        )
        summary = batch_store.finalise_batch(database, batch_id)
        assert summary.status == BatchStatus.COMPLETED_WITH_ERRORS.value

    def test_a_partial_run_stays_interrupted(self, database, identity, tmp_path):
        paths = paths_for(3, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        batch_store.record_results(
            database, batch_id, [outcome_for(paths[0], RecognitionOutcome.COMPLETE)]
        )
        summary = batch_store.finalise_batch(database, batch_id)
        assert summary.status == BatchStatus.INTERRUPTED.value


class TestCompatibility:
    def test_the_same_configuration_is_compatible(self, database, identity, tmp_path):
        batch_id = batch_store.create_batch(
            database, paths_for(1, tmp_path), identity=identity
        )
        verdict = batch_store.check_compatibility(database, batch_id, identity)
        assert verdict.compatible is True
        assert verdict.differences == ()

    def test_changed_thresholds_are_reported(self, database, identity, template, tmp_path):
        batch_id = batch_store.create_batch(
            database, paths_for(1, tmp_path), identity=identity
        )
        retuned = template.model_copy(
            update={
                "recognition": template.recognition.model_copy(
                    update={"fill_ratio_threshold": 0.7}
                )
            }
        )
        verdict = batch_store.check_compatibility(
            database, batch_id, batch_store.BatchIdentity.of(retuned)
        )
        assert verdict.compatible is False
        assert any("threshold" in item.lower() for item in verdict.differences)

    def test_edited_geometry_is_reported(self, database, identity, template, tmp_path):
        from omr_scanner.domain.geometry import NormalizedPoint

        batch_id = batch_store.create_batch(
            database, paths_for(1, tmp_path), identity=identity
        )
        moved = template.model_copy(
            update={
                "registration_markers": tuple(
                    marker.model_copy(
                        update={
                            "center": NormalizedPoint(
                                x=min(marker.center.x + 0.01, 1.0), y=marker.center.y
                            )
                        }
                    )
                    for marker in template.registration_markers
                )
            }
        )
        verdict = batch_store.check_compatibility(
            database, batch_id, batch_store.BatchIdentity.of(moved)
        )
        assert verdict.compatible is False
        assert any("geometry" in item.lower() for item in verdict.differences)

    def test_a_different_template_is_reported_by_name(self, database, identity, tmp_path):
        batch_id = batch_store.create_batch(
            database, paths_for(1, tmp_path), identity=identity
        )
        other = batch_store.BatchIdentity(
            template_id="something-else", template_name="Other sheet"
        )
        verdict = batch_store.check_compatibility(database, batch_id, other)
        assert verdict.compatible is False
        assert "Other sheet" in verdict.summary

    def test_an_unknown_batch_is_never_compatible(self, database, identity):
        verdict = batch_store.check_compatibility(database, "nosuchbatch", identity)
        assert verdict.compatible is False


class TestBatchRecorder:
    def test_results_are_buffered_until_the_flush_threshold(
        self, database, identity, tmp_path
    ):
        paths = paths_for(6, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        # A long interval so only the count rule can fire, which is what this
        # test is about.
        recorder = batch_store.BatchRecorder(
            database=database, batch_id=batch_id, flush_every=4, flush_interval=3600.0
        )

        for path in paths[:3]:
            recorder.record(outcome_for(path, RecognitionOutcome.COMPLETE))
        assert recorder.unsaved == 3
        assert batch_store.load_summary(database, batch_id).processed == 0

        recorder.record(outcome_for(paths[3], RecognitionOutcome.COMPLETE))
        assert recorder.unsaved == 0
        assert batch_store.load_summary(database, batch_id).processed == 4

    def test_a_final_flush_commits_the_remainder(self, database, identity, tmp_path):
        paths = paths_for(3, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        recorder = batch_store.BatchRecorder(
            database=database, batch_id=batch_id, flush_every=100, flush_interval=3600.0
        )
        for path in paths:
            recorder.record(outcome_for(path, RecognitionOutcome.COMPLETE))

        assert recorder.flush() is True
        assert recorder.persisted == 3
        assert batch_store.load_summary(database, batch_id).processed == 3

    def test_a_storage_failure_is_reported_not_raised(
        self, database, identity, tmp_path, monkeypatch
    ):
        # A full disk or a revoked network share must not end the run and lose
        # the results still in memory - but it must never be silent either.
        paths = paths_for(2, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)

        def explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk is full")

        monkeypatch.setattr(batch_store, "record_results", explode)
        recorder.record(outcome_for(paths[0], RecognitionOutcome.COMPLETE))

        assert recorder.flush() is False
        assert recorder.healthy is False
        assert "disk is full" in recorder.failure
        # The buffer is kept, so a later flush can still save the work.
        assert recorder.unsaved == 1
        assert recorder.persisted == 0

    def test_a_recovered_store_saves_the_buffered_work(
        self, database, identity, tmp_path, monkeypatch
    ):
        paths = paths_for(2, tmp_path)
        batch_id = batch_store.create_batch(database, paths, identity=identity)
        recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)

        real = batch_store.record_results

        def unavailable(*_args: object, **_kwargs: object) -> None:
            raise OSError("temporarily unavailable")

        monkeypatch.setattr(batch_store, "record_results", unavailable)
        recorder.record(outcome_for(paths[0], RecognitionOutcome.COMPLETE))
        recorder.flush()
        assert recorder.unsaved == 1

        monkeypatch.setattr(batch_store, "record_results", real)
        assert recorder.flush() is True
        assert batch_store.load_summary(database, batch_id).processed == 1


class TestStatusModel:
    @pytest.mark.parametrize(
        "status",
        [ScanJobStatus.COMPLETED, ScanJobStatus.WARNING, ScanJobStatus.FAILED],
    )
    def test_finished_states_are_terminal(self, status):
        assert status.is_terminal is True
        assert status.is_resumable is False

    @pytest.mark.parametrize(
        "status",
        [
            ScanJobStatus.PENDING,
            ScanJobStatus.QUEUED,
            ScanJobStatus.PROCESSING,
            ScanJobStatus.CANCELLED,
        ],
    )
    def test_unfinished_states_are_resumable(self, status):
        assert status.is_resumable is True
        assert status.is_terminal is False

    def test_every_status_is_one_or_the_other(self):
        # A state that is neither would be invisible to both the resume query
        # and the completed count, and a scan in it would simply disappear.
        for status in ScanJobStatus:
            assert status.is_terminal != status.is_resumable
