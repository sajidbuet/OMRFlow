"""Deterministic reprocessing and preserved history (Phase 10, §12/§13).

Uses real recognition over real rendered sheets, the same discipline every
other integration suite in this project follows - a reprocessed sheet's
"before" state must be something recognition actually produced, not a
hand-built fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database.engine import open_project_database
from omr_scanner.services import batch_processor, batch_store
from omr_scanner.services.batch_processor import BatchOptions
from omr_scanner.services.filename_manager import FilenameAllocator


@pytest.fixture
def template():
    return build_answer_sheet_template(roll_digits=7)


@pytest.fixture
def database(tmp_path: Path):
    db_path = tmp_path / "database.sqlite"
    handle = open_project_database(db_path, create=True)
    yield handle
    handle.close()


def _marks(roll: str) -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "B"),
    }


def _make_scan(tmp_path: Path, template, name: str, roll: str) -> Path:
    import cv2

    image = render_marked_sheet(template, _marks(roll))
    path = tmp_path / name
    cv2.imwrite(str(path), image)
    return path


def _run_and_record(database, template, paths) -> str:
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    batch_processor.process_batch(
        paths,
        template,
        options=BatchOptions(),
        allocator=FilenameAllocator(),
        on_result=recorder.record,
        workers=1,
    )
    recorder.flush()
    return batch_id


class TestMarkForReprocessing:
    def test_reprocessing_one_sheet_archives_its_previous_result(
        self, database, template, tmp_path: Path
    ) -> None:
        paths = [_make_scan(tmp_path, template, "a.png", "1203111")]
        batch_id = _run_and_record(database, template, paths)
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[paths[0]]

        summary_before = batch_store.load_summary(database, batch_id)
        assert summary_before.completed == 1

        changed = batch_store.mark_for_reprocessing(
            database, batch_id, [scan_id], reason="template edited", requested_by="tester"
        )
        assert changed == 1

        summary_after = batch_store.load_summary(database, batch_id)
        assert summary_after.pending == 1
        assert summary_after.completed == 0

        history = batch_store.reprocessing_history(database, scan_id)
        assert len(history) == 1
        assert history[0].previous_status == "completed"
        assert history[0].reason == "template edited"
        assert history[0].requested_by == "tester"

    def test_a_second_reprocessing_appends_rather_than_overwrites_history(
        self, database, template, tmp_path: Path
    ) -> None:
        paths = [_make_scan(tmp_path, template, "a.png", "1203111")]
        batch_id = _run_and_record(database, template, paths)
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[paths[0]]

        batch_store.mark_for_reprocessing(database, batch_id, [scan_id], reason="first")
        _run_and_record_resume(database, batch_id, template, paths)
        batch_store.mark_for_reprocessing(database, batch_id, [scan_id], reason="second")

        history = batch_store.reprocessing_history(database, scan_id)
        assert [entry.reason for entry in history] == ["first", "second"]

    def test_reprocessing_one_sheet_does_not_touch_an_unrelated_sheet(
        self, database, template, tmp_path: Path
    ) -> None:
        paths = [
            _make_scan(tmp_path, template, "a.png", "1203111"),
            _make_scan(tmp_path, template, "b.png", "1203222"),
        ]
        batch_id = _run_and_record(database, template, paths)
        ids = batch_store.scan_ids_by_path(database, batch_id)
        scan_id_a = ids[paths[0]]

        batch_store.mark_for_reprocessing(database, batch_id, [scan_id_a], reason="x")

        summary = batch_store.load_summary(database, batch_id)
        assert summary.pending == 1
        assert summary.completed == 1

    def test_reprocessing_a_never_processed_scan_is_a_harmless_no_op(
        self, database, template, tmp_path: Path
    ) -> None:
        paths = [_make_scan(tmp_path, template, "a.png", "1203111")]
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[paths[0]]

        changed = batch_store.mark_for_reprocessing(database, batch_id, [scan_id], reason="x")
        assert changed == 0
        assert batch_store.reprocessing_history(database, scan_id) == ()

    def test_an_id_from_a_different_batch_is_ignored(
        self, database, template, tmp_path: Path
    ) -> None:
        paths = [_make_scan(tmp_path, template, "a.png", "1203111")]
        batch_id = _run_and_record(database, template, paths)
        other_paths = [_make_scan(tmp_path, template, "b.png", "1203222")]
        other_batch_id = _run_and_record(database, template, other_paths)
        other_scan_id = batch_store.scan_ids_by_path(database, other_batch_id)[other_paths[0]]

        changed = batch_store.mark_for_reprocessing(
            database, batch_id, [other_scan_id], reason="x"
        )
        assert changed == 0


class TestReprocessFailedAndBatch:
    def test_reprocess_failed_scans_only_touches_failed_ones(
        self, database, template, tmp_path: Path
    ) -> None:
        good = _make_scan(tmp_path, template, "good.png", "1203111")
        bad = tmp_path / "corrupt.png"
        bad.write_bytes(b"not an image")
        batch_id = _run_and_record(database, template, [good, bad])

        summary_before = batch_store.load_summary(database, batch_id)
        assert summary_before.failed == 1
        assert summary_before.completed == 1

        changed = batch_store.reprocess_failed_scans(database, batch_id, reason="retry")
        assert changed == 1

        summary_after = batch_store.load_summary(database, batch_id)
        assert summary_after.pending == 1
        assert summary_after.completed == 1
        assert summary_after.failed == 0

    def test_reprocess_batch_touches_every_scan(
        self, database, template, tmp_path: Path
    ) -> None:
        paths = [
            _make_scan(tmp_path, template, "a.png", "1203111"),
            _make_scan(tmp_path, template, "b.png", "1203222"),
        ]
        batch_id = _run_and_record(database, template, paths)

        changed = batch_store.reprocess_batch(database, batch_id, reason="geometry changed")
        assert changed == 2

        summary = batch_store.load_summary(database, batch_id)
        assert summary.pending == 2
        assert summary.completed == 0


class TestDeterministicRecomputation:
    def test_reprocessing_a_completed_sheet_reproduces_the_same_result(
        self, database, template, tmp_path: Path
    ) -> None:
        """Same source, same template, same engine -> the same recognised value."""
        paths = [_make_scan(tmp_path, template, "a.png", "1203111")]
        batch_id = _run_and_record(database, template, paths)
        scan_id = batch_store.scan_ids_by_path(database, batch_id)[paths[0]]

        first_results = batch_store.completed_results(database, batch_id)
        assert len(first_results) == 1
        first_identifier = first_results[0].identifier_value

        batch_store.mark_for_reprocessing(database, batch_id, [scan_id], reason="x")
        _run_and_record_resume(database, batch_id, template, paths)

        second_results = batch_store.completed_results(database, batch_id)
        assert len(second_results) == 1
        assert second_results[0].identifier_value == first_identifier


def _run_and_record_resume(database, batch_id, template, all_paths) -> None:
    """Resume a batch, processing only what `resumable_scans` says is left."""
    remaining = batch_store.resumable_scans(database, batch_id)
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    batch_processor.process_batch(
        list(remaining),
        template,
        options=BatchOptions(),
        allocator=FilenameAllocator(),
        on_result=recorder.record,
        workers=1,
    )
    recorder.flush()
