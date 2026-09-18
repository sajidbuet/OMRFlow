"""End-to-end Phase 5: durable batches, resume, and source integrity.

Scope:
    The real :func:`~omr_scanner.services.batch_processor.process_batch`, the
    real recognition engine, real rendered sheets and a real project database.
    Nothing here is mocked, because every claim Phase 5 makes - "results
    survive", "resume does not redo work", "one bad sheet does not stop the
    batch", "originals are unchanged" - is a claim about what actually happens
    to files and rows.

Mapping to the Phase 5 test plan:
    ==== =======================================================
    Test Covered by
    ==== =======================================================
    A    :class:`TestANormalBatch`
    B    :class:`TestBBrokenImages`
    C    :class:`TestCRecognitionFailure`
    E    :class:`TestEResume`
    F    :class:`TestFSimulatedInterruption`
    G/H  :class:`TestGAndHWorkerCounts`
    I    :class:`TestIDuplicateIdentifiers`
    J    :class:`TestJSourceIntegrity`
    K    :class:`TestKPersistenceAcrossReopen`
    ==== =======================================================

    D (cancellation) and L (GUI responsiveness) need a Qt event loop and live
    in ``tests/gui/test_scan_persistence.py``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database import open_project_database
from omr_scanner.database.models import BatchStatus
from omr_scanner.services import batch_store
from omr_scanner.services.batch_processor import BatchOptions, process_batch
from omr_scanner.services.recognition_models import RecognitionOutcome

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase


def sheet_marks(roll: str, *, set_code: str = "A", answer: str = "B") -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: set_code},
        "questions_0": dict.fromkeys(range(10), answer),
        "questions_1": dict.fromkeys(range(10), answer),
    }


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[ProjectDatabase]:
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def scans_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "scans"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def make_scan(scans_dir: Path, template):
    def make(name: str, roll: str = "120317", **kwargs: str) -> Path:
        path = scans_dir / name
        cv2.imwrite(str(path), render_marked_sheet(template, sheet_marks(roll, **kwargs)))
        return path

    return make


def run_batch(
    database: ProjectDatabase,
    batch_id: str,
    paths,
    template,
    *,
    workers: int = 1,
    options: BatchOptions | None = None,
):
    """Process ``paths``, recording every result the way the GUI worker does.

    The recorder is attached through ``on_result`` - the same hook the Qt
    worker uses - so these tests exercise the real persistence path rather than
    a parallel one written for testing.
    """
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    batch_store.mark_queued(database, batch_id, paths)
    batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)
    report = process_batch(
        paths,
        template,
        options=options or BatchOptions(),
        on_result=recorder.record,
        workers=workers,
    )
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)
    return report, recorder


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------
# Test A - a normal batch
# ----------------------------------------------------------------------
class TestANormalBatch:
    def test_every_sheet_is_processed_and_persisted(
        self, database, template, make_scan
    ):
        paths = [make_scan(f"scan{i}.png", roll=f"10000{i}") for i in range(5)]
        identity = batch_store.BatchIdentity.of(template)
        batch_id = batch_store.create_batch(database, paths, identity=identity)

        report, recorder = run_batch(database, batch_id, paths, template)

        assert report.total == 5
        assert recorder.healthy is True
        summary = batch_store.load_summary(database, batch_id)
        assert summary.total == 5
        assert summary.processed == 5
        assert summary.pending == 0
        assert summary.status == BatchStatus.COMPLETED.value

    def test_the_persisted_values_are_the_recognised_values(
        self, database, template, make_scan
    ):
        paths = [make_scan("one.png", roll="120317")]
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(database, batch_id, paths, template)

        stored = batch_store.completed_results(database, batch_id)
        assert len(stored) == 1
        assert stored[0].identifier_value == "120317"
        assert stored[0].set_code_value == "A"
        assert len(stored[0].answers) == 20


# ----------------------------------------------------------------------
# Test B - broken images
# ----------------------------------------------------------------------
class TestBBrokenImages:
    def test_a_corrupt_file_fails_alone_and_the_batch_finishes(
        self, database, template, make_scan, scans_dir
    ):
        good = [make_scan("good1.png"), make_scan("good2.png", roll="100002")]
        corrupt = scans_dir / "corrupt.png"
        corrupt.write_bytes(b"\x89PNG\r\n\x1a\n this is not an image at all")
        paths = [good[0], corrupt, good[1]]

        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        report, _recorder = run_batch(database, batch_id, paths, template)

        assert report.total == 3
        summary = batch_store.load_summary(database, batch_id)
        assert summary.failed == 1
        assert summary.completed + summary.warning == 2
        assert summary.pending == 0
        # The loop finished, but the work did not entirely succeed - and the
        # stored status says so rather than claiming a clean run.
        assert summary.status == BatchStatus.COMPLETED_WITH_ERRORS.value

    def test_the_failure_reason_is_recorded(self, database, template, scans_dir):
        corrupt = scans_dir / "corrupt.png"
        corrupt.write_bytes(b"nonsense")
        batch_id = batch_store.create_batch(
            database, [corrupt], identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(database, batch_id, [corrupt], template)

        stored = batch_store.completed_results(database, batch_id)
        assert len(stored) == 1
        assert stored[0].outcome is RecognitionOutcome.ERROR
        assert stored[0].registration_message
        assert batch_store.categorise_error(stored[0]) == batch_store.ErrorCategory.IMAGE


# ----------------------------------------------------------------------
# Test C - recognition failure
# ----------------------------------------------------------------------
class TestCRecognitionFailure:
    def test_a_sheet_without_markers_fails_without_inventing_an_answer(
        self, database, template, make_scan, scans_dir
    ):
        import numpy as np

        blank = scans_dir / "blank_page.png"
        # A plain white page: decodable, but there is nothing on it to register
        # against. The engine must say so rather than guess.
        cv2.imwrite(str(blank), np.full((1000, 700), 255, dtype=np.uint8))
        paths = [make_scan("ok.png"), blank]

        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(database, batch_id, paths, template)

        stored = {
            item.source_path: item
            for item in batch_store.completed_results(database, batch_id)
        }
        failed = stored[blank]
        assert failed.outcome in (
            RecognitionOutcome.REGISTRATION_FAILED,
            RecognitionOutcome.ERROR,
        )
        # No fabricated confidence: nothing was measured at all.
        assert failed.identifier_value == ""
        assert failed.answers == ()
        # And the good sheet beside it is untouched.
        assert stored[paths[0]].identifier_value == "120317"


# ----------------------------------------------------------------------
# Test E - resume
# ----------------------------------------------------------------------
class TestEResume:
    def test_resume_processes_only_what_is_left(self, database, template, make_scan):
        paths = [make_scan(f"scan{i}.png", roll=f"20000{i}") for i in range(6)]
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )

        # A partial run, as if it had been cancelled after three sheets.
        first_half = paths[:3]
        run_batch(database, batch_id, first_half, template)
        batch_store.mark_cancelled(database, batch_id)

        remaining = batch_store.resumable_scans(database, batch_id)
        assert remaining == tuple(paths[3:])

        report, _recorder = run_batch(database, batch_id, list(remaining), template)

        # The resumed run read three sheets, not six.
        assert report.total == 3
        summary = batch_store.load_summary(database, batch_id)
        assert summary.processed == 6
        assert summary.pending == 0
        assert summary.status == BatchStatus.COMPLETED.value

    def test_the_final_results_cover_the_whole_batch(self, database, template, make_scan):
        paths = [make_scan(f"scan{i}.png", roll=f"30000{i}") for i in range(4)]
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(database, batch_id, paths[:2], template)
        batch_store.mark_cancelled(database, batch_id)
        run_batch(
            database, batch_id, list(batch_store.resumable_scans(database, batch_id)), template
        )

        stored = batch_store.completed_results(database, batch_id)
        assert len(stored) == 4
        assert {item.identifier_value for item in stored} == {
            "300000",
            "300001",
            "300002",
            "300003",
        }

    def test_resume_after_everything_finished_has_nothing_to_do(
        self, database, template, make_scan
    ):
        paths = [make_scan("only.png")]
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(database, batch_id, paths, template)
        assert batch_store.resumable_scans(database, batch_id) == ()


# ----------------------------------------------------------------------
# Test F - simulated interruption
# ----------------------------------------------------------------------
class TestFSimulatedInterruption:
    def test_results_survive_closing_and_reopening_the_database(
        self, tmp_path, template, make_scan
    ):
        paths = [make_scan(f"scan{i}.png", roll=f"40000{i}") for i in range(4)]
        db_path = tmp_path / "database.sqlite"

        first = open_project_database(db_path, create=True)
        batch_id = batch_store.create_batch(
            first, paths, identity=batch_store.BatchIdentity.of(template)
        )
        # Two sheets finish, then the "application" is killed: no cancel, no
        # finalise, and two rows still claimed as in-flight.
        run_batch(first, batch_id, paths[:2], template)
        batch_store.mark_queued(first, batch_id, paths[2:])
        batch_store.set_batch_status(first, batch_id, BatchStatus.RUNNING)
        first.close()

        second = open_project_database(db_path)
        try:
            batches, scans = batch_store.recover_interrupted(second)
            assert (batches, scans) == (1, 2)

            summary = batch_store.load_summary(second, batch_id)
            assert summary.processed == 2
            assert summary.pending == 2
            assert summary.status == BatchStatus.INTERRUPTED.value

            # And the two that were in flight are exactly what resume picks up.
            assert batch_store.resumable_scans(second, batch_id) == tuple(paths[2:])
            run_batch(second, batch_id, paths[2:], template)
            assert batch_store.load_summary(second, batch_id).processed == 4
        finally:
            second.close()

    def test_an_unflushed_buffer_is_the_only_work_at_risk(
        self, database, template, make_scan
    ):
        # The documented durability bound: results are committed in groups, so
        # an abrupt end loses at most what is still buffered - never more.
        paths = [make_scan(f"scan{i}.png", roll=f"50000{i}") for i in range(3)]
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        recorder = batch_store.BatchRecorder(
            database=database, batch_id=batch_id, flush_every=2, flush_interval=3600.0
        )
        process_batch(paths, template, on_result=recorder.record, workers=1)

        # Two committed by the count rule, one still buffered when the process
        # "died" without a final flush.
        assert recorder.unsaved == 1
        assert batch_store.load_summary(database, batch_id).processed == 2
        assert len(batch_store.resumable_scans(database, batch_id)) == 1


# ----------------------------------------------------------------------
# Tests G and H - worker counts
# ----------------------------------------------------------------------
class TestGAndHWorkerCounts:
    def test_one_worker_processes_everything(self, database, template, make_scan):
        paths = [make_scan(f"scan{i}.png", roll=f"60000{i}") for i in range(3)]
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(database, batch_id, paths, template, workers=1)
        assert batch_store.load_summary(database, batch_id).processed == 3

    def test_multiple_workers_persist_the_same_results_as_one(
        self, tmp_path, template, make_scan
    ):
        # Recognition must not depend on how the work was divided, and neither
        # must what gets written down.
        paths = [make_scan(f"scan{i}.png", roll=f"70000{i}") for i in range(4)]

        def stored_with(workers: int, name: str) -> list[tuple[str, str, str]]:
            handle = open_project_database(tmp_path / name, create=True)
            try:
                batch_id = batch_store.create_batch(
                    handle, paths, identity=batch_store.BatchIdentity.of(template)
                )
                run_batch(handle, batch_id, paths, template, workers=workers)
                return [
                    (item.source_path.name, item.identifier_value, item.outcome.value)
                    for item in batch_store.completed_results(handle, batch_id)
                ]
            finally:
                handle.close()

        single = stored_with(1, "single.sqlite")
        multi = stored_with(3, "multi.sqlite")

        assert len(single) == 4
        # Ordered by batch_index in both cases, so this compares content *and*
        # order - a pool that returned results out of order would show here.
        assert single == multi


# ----------------------------------------------------------------------
# Test I - duplicate identifiers
# ----------------------------------------------------------------------
class TestIDuplicateIdentifiers:
    def test_duplicate_rolls_get_distinct_output_names_and_both_are_stored(
        self, database, template, make_scan, tmp_path
    ):
        output = tmp_path / "output"
        # Six digits: the answer sheet template's identifier field has six
        # printed columns, so this is a roll number it can actually represent.
        paths = [make_scan(f"dup{i}.png", roll="210312") for i in range(3)]
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(
            database,
            batch_id,
            paths,
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )

        written = sorted(item.name for item in output.iterdir())
        assert written == ["210312.png", "210312_a.png", "210312_b.png"]
        # Three separate rows, none overwriting another.
        summary = batch_store.load_summary(database, batch_id)
        assert summary.total == 3
        assert summary.processed == 3


# ----------------------------------------------------------------------
# Test J - source integrity (mandatory Phase 5 exit criterion)
# ----------------------------------------------------------------------
class TestJSourceIntegrity:
    def test_originals_are_byte_for_byte_unchanged_by_a_batch(
        self, database, template, make_scan, scans_dir, tmp_path
    ):
        paths = [make_scan(f"scan{i}.png", roll=f"80000{i}") for i in range(4)]
        corrupt = scans_dir / "corrupt.png"
        corrupt.write_bytes(b"deliberately unreadable")
        paths.append(corrupt)

        before = {path: digest(path) for path in paths}
        sizes_before = {path: path.stat().st_size for path in paths}

        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(
            database,
            batch_id,
            paths,
            template,
            options=BatchOptions(
                output_dir=tmp_path / "renamed", rename_with_identifier=True
            ),
        )

        after = {path: digest(path) for path in paths}
        assert after == before, "a source scan was modified by processing"
        assert {path: path.stat().st_size for path in paths} == sizes_before
        # Every original is still where it was - renaming copies, never moves.
        assert all(path.is_file() for path in paths)

    def test_the_renamed_copies_are_copies_not_moves(
        self, database, template, make_scan, tmp_path
    ):
        output = tmp_path / "output"
        source = make_scan("original.png", roll="999999")
        before = digest(source)

        batch_id = batch_store.create_batch(
            database, [source], identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(
            database,
            batch_id,
            [source],
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )

        assert source.is_file()
        assert digest(source) == before
        copy = output / "999999.png"
        assert copy.is_file()
        assert digest(copy) == before


# ----------------------------------------------------------------------
# Test K - persistence across reopening
# ----------------------------------------------------------------------
class TestKPersistenceAcrossReopen:
    def test_a_finished_batch_is_still_there_after_reopening(
        self, tmp_path, template, make_scan
    ):
        paths = [make_scan(f"scan{i}.png", roll=f"90000{i}") for i in range(3)]
        db_path = tmp_path / "database.sqlite"

        handle = open_project_database(db_path, create=True)
        batch_id = batch_store.create_batch(
            handle, paths, identity=batch_store.BatchIdentity.of(template)
        )
        run_batch(handle, batch_id, paths, template)
        handle.close()

        reopened = open_project_database(db_path)
        try:
            summary = batch_store.load_summary(reopened, batch_id)
            assert summary.processed == 3
            assert summary.status == BatchStatus.COMPLETED.value
            listed = batch_store.list_batches(reopened)
            assert len(listed) == 1
            assert listed[0].batch_id == batch_id
            assert listed[0].template_name == template.name
            restored = batch_store.completed_results(reopened, batch_id)
            assert [item.identifier_value for item in restored] == [
                "900000",
                "900001",
                "900002",
            ]
        finally:
            reopened.close()

    def test_an_old_project_database_gains_the_batch_tables(self, tmp_path):
        # Migration, not create_all: a project made before Phase 5 must gain
        # the tables when it is opened by this build.
        from sqlalchemy import text

        from omr_scanner.database.migrations import MIGRATIONS, SCHEMA_VERSION

        db_path = tmp_path / "old.sqlite"
        handle = open_project_database(db_path, create=True)
        assert handle.schema_version == SCHEMA_VERSION
        assert SCHEMA_VERSION >= 2
        with handle.session() as session:
            names = {
                row[0]
                for row in session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).all()
            }
        handle.close()
        assert {"scan_batch", "batch_scan"} <= names
        assert [item.version for item in MIGRATIONS] == list(
            range(1, len(MIGRATIONS) + 1)
        )
