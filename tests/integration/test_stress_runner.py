"""Integration tests for the stress-run orchestrator (Phase 10, §19-§25, §30/§31).

Runs at a small, fast scale (tens to low hundreds of sheets) - what is under
test is the *architecture* (bounded disk usage, correct resume, no
duplication, identity preserved through the temp-file seam), which a small
run exercises identically to a 100,000-sheet one. The mandatory full-scale
100,000-sheet kill/resume acceptance matrix is a separate, deliberately
unautomated run - see ``development/PHASE_10_HANDOFF.md`` and
``tools/benchmark_stress.py --help`` for the exact commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.database.engine import open_project_database
from omr_scanner.evaluation import stress_dataset, stress_runner
from omr_scanner.services import batch_store


@pytest.fixture
def template():
    return build_answer_sheet_template(roll_digits=7)


@pytest.fixture
def database(tmp_path: Path):
    db_path = tmp_path / "database.sqlite"
    handle = open_project_database(db_path, create=True)
    yield handle
    handle.close()


class TestCreateStressBatch:
    def test_registers_every_sheet_with_no_files_on_disk(self, database, template) -> None:
        spec = stress_dataset.StressDatasetSpec(seed=1, sheet_count=50)
        batch_id = stress_runner.create_stress_batch(database, spec, template)

        summary = batch_store.load_summary(database, batch_id)
        assert summary.total == 50
        assert summary.pending == 50
        assert summary.completed == 0

    def test_registering_does_not_create_any_temp_file(
        self, database, template, tmp_path: Path
    ) -> None:
        before = list(tmp_path.iterdir())
        spec = stress_dataset.StressDatasetSpec(seed=1, sheet_count=50)
        stress_runner.create_stress_batch(database, spec, template)
        after = list(tmp_path.iterdir())
        assert before == after


class TestRunStressBatch:
    def test_processes_every_sheet_and_persists_durably(
        self, database, template, tmp_path: Path
    ) -> None:
        spec = stress_dataset.StressDatasetSpec(seed=2, sheet_count=40)
        batch_id = stress_runner.create_stress_batch(database, spec, template)
        recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)

        stress_runner.run_stress_batch(
            database,
            batch_id,
            template,
            spec,
            workers=1,
            scratch_root=tmp_path,
            on_result=recorder.record,
        )
        recorder.flush()

        summary = batch_store.load_summary(database, batch_id)
        assert summary.pending == 0
        assert summary.total == 40
        assert summary.completed + summary.warning + summary.failed == 40

    def test_leaves_no_temporary_files_behind(self, database, template, tmp_path: Path) -> None:
        spec = stress_dataset.StressDatasetSpec(seed=3, sheet_count=30)
        batch_id = stress_runner.create_stress_batch(database, spec, template)
        recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)

        stress_runner.run_stress_batch(
            database, batch_id, template, spec, workers=1, scratch_root=tmp_path,
            on_result=recorder.record,
        )
        recorder.flush()

        assert list(tmp_path.glob("omrflow_stress_*")) == []

    def test_never_materializes_more_than_the_chunk_size_at_once(
        self, database, template, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(stress_runner, "MATERIALIZE_CHUNK_SIZE", 10)
        spec = stress_dataset.StressDatasetSpec(seed=4, sheet_count=35)
        batch_id = stress_runner.create_stress_batch(database, spec, template)
        recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)

        peak_files = 0

        def _counting_result(processed) -> None:
            nonlocal peak_files
            current = sum(1 for _ in tmp_path.rglob("*.png"))
            peak_files = max(peak_files, current)
            recorder.record(processed)

        stress_runner.run_stress_batch(
            database, batch_id, template, spec, workers=1, scratch_root=tmp_path,
            on_result=_counting_result,
        )
        recorder.flush()
        assert peak_files <= 10

    def test_results_carry_the_permanent_virtual_identity_not_a_temp_path(
        self, database, template, tmp_path: Path
    ) -> None:
        spec = stress_dataset.StressDatasetSpec(seed=5, sheet_count=20)
        batch_id = stress_runner.create_stress_batch(database, spec, template)
        seen_paths = []

        stress_runner.run_stress_batch(
            database, template=template, batch_id=batch_id, spec=spec, workers=1,
            scratch_root=tmp_path, on_result=lambda p: seen_paths.append(p.result.source_path),
        )

        assert len(seen_paths) == 20
        assert all(stress_dataset.is_stress_source(str(p)) for p in seen_paths)

    def test_resuming_only_processes_what_is_left(
        self, database, template, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(stress_runner, "MATERIALIZE_CHUNK_SIZE", 5)
        spec = stress_dataset.StressDatasetSpec(seed=6, sheet_count=20)
        batch_id = stress_runner.create_stress_batch(database, spec, template)
        recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)

        # Simulate an interruption after the first chunk by cancelling once
        # five sheets have completed.
        completed = {"count": 0}

        def _count_then_cancel(processed) -> None:
            completed["count"] += 1
            recorder.record(processed)

        stress_runner.run_stress_batch(
            database, batch_id, template, spec, workers=1, scratch_root=tmp_path,
            on_result=_count_then_cancel,
            should_cancel=lambda: completed["count"] >= 5,
        )
        recorder.flush()

        summary_mid = batch_store.load_summary(database, batch_id)
        assert summary_mid.pending == 15
        completed_after_first_pass = (
            summary_mid.completed + summary_mid.warning + summary_mid.failed
        )
        assert completed_after_first_pass == 5

        # Resume: only the remaining 15 should be processed.
        second_recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
        stress_runner.run_stress_batch(
            database, batch_id, template, spec, workers=1, scratch_root=tmp_path,
            on_result=second_recorder.record,
        )
        second_recorder.flush()

        summary_final = batch_store.load_summary(database, batch_id)
        assert summary_final.pending == 0
        assert summary_final.total == 20
        assert summary_final.completed + summary_final.warning + summary_final.failed == 20

    def test_resuming_never_duplicates_a_completed_row(
        self, database, template, tmp_path: Path
    ) -> None:
        spec = stress_dataset.StressDatasetSpec(seed=7, sheet_count=15)
        batch_id = stress_runner.create_stress_batch(database, spec, template)
        recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
        stress_runner.run_stress_batch(
            database, batch_id, template, spec, workers=1, scratch_root=tmp_path,
            on_result=recorder.record,
        )
        recorder.flush()

        # A "resume" with nothing left to do must be a no-op, not a re-run.
        reports = stress_runner.run_stress_batch(
            database, batch_id, template, spec, workers=1, scratch_root=tmp_path,
            on_result=recorder.record,
        )
        assert reports == []

        with database.session() as session:
            from sqlalchemy import func, select

            from omr_scanner.database.models import BatchScan

            count = session.scalar(
                select(func.count()).select_from(BatchScan).where(BatchScan.batch_id == batch_id)
            )
        assert count == 15
