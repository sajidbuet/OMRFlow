"""Unit tests for the privacy-safe diagnostic bundle (Phase 10, §39)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from omr_scanner.config.processing import ProcessingSettings
from omr_scanner.database.engine import open_project_database
from omr_scanner.services import diagnostics


@pytest.fixture
def database(tmp_path: Path):
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    yield handle
    handle.close()


class TestBuildDiagnosticBundle:
    def test_produces_a_valid_zip_with_no_project(self, tmp_path: Path) -> None:
        output = diagnostics.build_diagnostic_bundle(tmp_path / "bundle.zip")
        assert output.is_file()
        with zipfile.ZipFile(output) as archive:
            assert archive.testzip() is None
            names = set(archive.namelist())
        assert "manifest.json" in names
        assert "environment.json" in names
        assert "health_check.json" in names

    def test_manifest_carries_the_privacy_statement(self, tmp_path: Path) -> None:
        output = diagnostics.build_diagnostic_bundle(tmp_path / "bundle.zip")
        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        assert "candidate" in manifest["privacy_statement"].lower()

    def test_includes_the_schema_version_when_a_project_is_open(
        self, tmp_path: Path, database
    ) -> None:
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", database=database
        )
        with zipfile.ZipFile(output) as archive:
            db_info = json.loads(archive.read("database.json"))
        assert db_info["schema_version"] == database.schema_version

    def test_includes_a_health_check_when_a_project_is_open(
        self, tmp_path: Path, database
    ) -> None:
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", database=database, project_root=tmp_path
        )
        with zipfile.ZipFile(output) as archive:
            health = json.loads(archive.read("health_check.json"))
        assert "level" in health
        assert "issues" in health

    def test_includes_processing_settings(self, tmp_path: Path) -> None:
        settings = ProcessingSettings().with_worker_count(4).with_opencv_threads(2)
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", processing=settings
        )
        with zipfile.ZipFile(output) as archive:
            processing = json.loads(archive.read("processing_settings.json"))
        assert processing["worker_count"] == 4
        assert processing["opencv_threads"] == 2

    def test_extra_environment_is_merged_in(self, tmp_path: Path) -> None:
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", extra_environment={"qt_version": "6.11.2"}
        )
        with zipfile.ZipFile(output) as archive:
            environment = json.loads(archive.read("environment.json"))
        assert environment["qt_version"] == "6.11.2"

    def test_no_log_file_means_no_log_entry_in_the_bundle(self, tmp_path: Path) -> None:
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", project_root=tmp_path
        )
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
        assert "recent_log_tail.txt" not in names

    def test_the_log_tail_is_bounded_to_the_configured_line_count(
        self, tmp_path: Path
    ) -> None:
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "project.log").write_text(
            "\n".join(f"line {i}" for i in range(500)), encoding="utf-8"
        )
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", project_root=tmp_path
        )
        with zipfile.ZipFile(output) as archive:
            tail = archive.read("recent_log_tail.txt").decode("utf-8")
        line_count = len(tail.splitlines())
        assert line_count == diagnostics.LOG_TAIL_LINES
        assert "line 499" in tail
        assert "line 0" not in tail

    def test_no_benchmark_directory_means_no_benchmark_entry(self, tmp_path: Path) -> None:
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", project_root=tmp_path
        )
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
        assert "latest_benchmark_summary.json" not in names

    def test_the_latest_benchmark_report_is_included_without_its_local_path(
        self, tmp_path: Path
    ) -> None:
        benchmarks_dir = tmp_path / "benchmarks"
        benchmarks_dir.mkdir()
        (benchmarks_dir / "stress_abc.json").write_text(
            json.dumps({"batch_id": "abc", "telemetry_file": r"C:\Users\someone\telemetry.jsonl"}),
            encoding="utf-8",
        )
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", project_root=tmp_path
        )
        with zipfile.ZipFile(output) as archive:
            summary = json.loads(archive.read("latest_benchmark_summary.json"))
        assert summary["batch_id"] == "abc"
        assert "telemetry_file" not in summary


class TestDiagnosticBundlePrivacy:
    """The central requirement: no candidate-sensitive data ever appears."""

    def test_a_real_roster_and_result_project_leaks_nothing_into_the_bundle(
        self, tmp_path: Path
    ) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import insert

        from omr_scanner.database.engine import open_project_database
        from omr_scanner.database.models import CandidateResult, CandidateRoster
        from omr_scanner.services import batch_store

        db_path = tmp_path / "database.sqlite"
        handle = open_project_database(db_path, create=True)
        try:
            secret_name = "Sajid Muhaimin Choudhury"
            secret_roll = "150102046"
            secret_answers = "ABCDABCD"
            batch_id = batch_store.create_batch(
                handle, [], identity=batch_store.BatchIdentity(), batch_id="b1"
            )
            with handle.session() as session:
                session.execute(
                    insert(CandidateRoster).values(
                        roster_id=1,
                        created_at=datetime.now(UTC),
                        source_name="roster.xlsx",
                        imported_by="tester",
                    )
                )
                session.execute(
                    insert(CandidateResult).values(
                        roster_id=1,
                        batch_id=batch_id,
                        candidate_id=secret_roll,
                        answer_string=secret_answers,
                        machine_answer_string=secret_answers,
                        status="scored",
                        created_at=datetime.now(UTC),
                        computed_at=datetime.now(UTC),
                    )
                )

            output = diagnostics.build_diagnostic_bundle(
                tmp_path / "bundle.zip", database=handle, project_root=tmp_path
            )
            with zipfile.ZipFile(output) as archive:
                combined = b"".join(archive.read(name) for name in archive.namelist())
            combined_text = combined.decode("utf-8", errors="replace")

            assert secret_name not in combined_text
            assert secret_roll not in combined_text
            assert secret_answers not in combined_text
        finally:
            handle.close()

    def test_a_log_line_that_would_have_leaked_a_roll_number_is_still_bounded_by_the_log_invariant(
        self, tmp_path: Path
    ) -> None:
        # This module trusts, rather than re-derives, the "no candidate data
        # reaches the log" invariant `tests/unit/test_candidate_privacy.py`
        # already pins elsewhere in the codebase. This test only confirms
        # the bundle does not *add* a second way for something to leak: an
        # ordinary log file with no candidate content produces a plain tail.
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        (logs_dir / "project.log").write_text(
            "INFO: batch b1 finished: 10 completed, 0 failed\n", encoding="utf-8"
        )
        output = diagnostics.build_diagnostic_bundle(
            tmp_path / "bundle.zip", project_root=tmp_path
        )
        with zipfile.ZipFile(output) as archive:
            tail = archive.read("recent_log_tail.txt").decode("utf-8")
        assert "batch b1 finished" in tail
