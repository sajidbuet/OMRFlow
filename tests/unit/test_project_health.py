"""Unit tests for project health checking (Phase 10, §6/§7)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import insert

from omr_scanner.database.engine import open_project_database
from omr_scanner.database.models import (
    AnswerKeyRevision,
    CandidateRoster,
    ReconciliationEntryRow,
)
from omr_scanner.services import batch_store, project_backup, project_health, scan_provenance


@pytest.fixture
def database(tmp_path: Path):
    db_path = tmp_path / "database.sqlite"
    handle = open_project_database(db_path, create=True)
    yield handle
    handle.close()


class TestQuickCheck:
    def test_a_healthy_database_reports_ok(self, database) -> None:
        report = project_health.quick_check(database)
        assert report.is_ok
        assert report.level == project_health.HealthLevel.OK

    def test_a_corrupted_database_is_detected(self, tmp_path: Path) -> None:
        db_path = tmp_path / "database.sqlite"
        open_project_database(db_path, create=True).close()

        # Damage the file directly at the byte level - a real corruption
        # scenario, not a synthetic "corrupt=True" flag (per the brief's own
        # instruction not to rely solely on a flag for this test).
        with db_path.open("r+b") as handle:
            handle.seek(100)
            handle.write(b"\xff" * 200)

        damaged = open_project_database(db_path, read_only=True)
        try:
            report = project_health.quick_check(damaged)
            assert not report.is_ok
            assert report.level == project_health.HealthLevel.ERROR
            assert report.issues[0].code == "SQLITE_QUICK_CHECK_FAILED"
        finally:
            damaged.close()


class TestFullCheckIntegrity:
    def test_a_healthy_project_has_no_integrity_or_foreign_key_issues(
        self, database, tmp_path: Path
    ) -> None:
        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "SQLITE_INTEGRITY_CHECK_FAILED" not in codes
        assert "FOREIGN_KEY_VIOLATION" not in codes

    def test_a_damaged_database_fails_full_integrity_check(self, tmp_path: Path) -> None:
        db_path = tmp_path / "database.sqlite"
        open_project_database(db_path, create=True).close()
        with db_path.open("r+b") as handle:
            handle.seek(100)
            handle.write(b"\xff" * 200)

        damaged = open_project_database(db_path, read_only=True)
        try:
            report = project_health.full_check(damaged, tmp_path)
            assert report.level == project_health.HealthLevel.ERROR
        finally:
            damaged.close()


class TestSchemaVersion:
    def test_matches_the_current_build(self, database, tmp_path: Path) -> None:
        report = project_health.full_check(database, tmp_path)
        assert not any(
            issue.code in ("SCHEMA_NEWER_THAN_BUILD", "SCHEMA_OLDER_THAN_BUILD")
            for issue in report.issues
        )


class TestSourceScanAvailability:
    def test_a_missing_scan_is_reported(self, database, tmp_path: Path) -> None:
        missing = tmp_path / "gone.png"
        batch_id = batch_store.create_batch(
            database, [missing], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)  # nothing to hash

        # Fabricate a hash as if it had been imported and later deleted.
        from sqlalchemy import update

        from omr_scanner.database.models import BatchScan

        with database.session() as session:
            session.execute(update(BatchScan).values(content_sha256="deadbeef"))

        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "SOURCE_SCANS_MISSING" in codes

    def test_a_changed_scan_is_reported(self, database, tmp_path: Path) -> None:
        source = tmp_path / "a.png"
        source.write_bytes(b"ORIGINAL")
        batch_id = batch_store.create_batch(
            database, [source], identity=batch_store.BatchIdentity()
        )
        scan_provenance.compute_hashes_for_batch(database, batch_id)
        source.write_bytes(b"CHANGED CONTENT")

        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "SOURCE_SCANS_CHANGED" in codes
        assert report.level == project_health.HealthLevel.ERROR


class TestUnresolvedExceptions:
    def test_unresolved_reconciliation_exceptions_are_reported(
        self, database, tmp_path: Path
    ) -> None:
        with database.session() as session:
            session.execute(
                insert(CandidateRoster).values(
                    roster_id=1,
                    created_at=datetime.now(UTC),
                    source_name="roster.xlsx",
                    imported_by="tester",
                )
            )
        batch_store.create_batch(
            database, [tmp_path / "a.png"], identity=batch_store.BatchIdentity(), batch_id="b1"
        )
        with database.session() as session:
            session.execute(
                insert(ReconciliationEntryRow).values(
                    roster_id=1,
                    batch_id="b1",
                    entry_key="k1",
                    entry_order=0,
                    candidate_id="C1",
                    status="unknown_id",
                    updated_at=datetime.now(UTC),
                )
            )

        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "UNRESOLVED_RECONCILIATION_EXCEPTIONS" in codes

    def test_matched_candidates_are_not_reported_as_exceptions(
        self, database, tmp_path: Path
    ) -> None:
        with database.session() as session:
            session.execute(
                insert(CandidateRoster).values(
                    roster_id=1,
                    created_at=datetime.now(UTC),
                    source_name="roster.xlsx",
                    imported_by="tester",
                )
            )
        batch_store.create_batch(
            database, [tmp_path / "a.png"], identity=batch_store.BatchIdentity(), batch_id="b1"
        )
        with database.session() as session:
            session.execute(
                insert(ReconciliationEntryRow).values(
                    roster_id=1,
                    batch_id="b1",
                    entry_key="k1",
                    entry_order=0,
                    candidate_id="C1",
                    status="matched",
                    updated_at=datetime.now(UTC),
                )
            )

        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "UNRESOLVED_RECONCILIATION_EXCEPTIONS" not in codes


class TestMissingVerifiedKey:
    def test_a_recognised_set_without_a_verified_key_is_reported(
        self, database, tmp_path: Path
    ) -> None:
        batch_store.create_batch(
            database, [tmp_path / "a.png"], identity=batch_store.BatchIdentity()
        )
        from sqlalchemy import update

        from omr_scanner.database.models import BatchScan

        with database.session() as session:
            session.execute(update(BatchScan).values(set_code_value="A"))

        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "SETS_WITHOUT_A_VERIFIED_KEY" in codes

    def test_a_verified_key_clears_the_finding(self, database, tmp_path: Path) -> None:
        batch_store.create_batch(
            database, [tmp_path / "a.png"], identity=batch_store.BatchIdentity()
        )
        from sqlalchemy import update

        from omr_scanner.database.models import BatchScan

        with database.session() as session:
            session.execute(update(BatchScan).values(set_code_value="A"))
            session.execute(
                insert(AnswerKeyRevision).values(
                    set_code="A",
                    revision=1,
                    answers="ABCD",
                    question_count=4,
                    status="verified",
                    created_at=datetime.now(UTC),
                )
            )

        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "SETS_WITHOUT_A_VERIFIED_KEY" not in codes


class TestBackupPresence:
    def test_no_backups_is_reported(self, database, tmp_path: Path) -> None:
        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "NO_BACKUPS" in codes

    def test_a_complete_backup_clears_the_finding(self, database, tmp_path: Path) -> None:
        backups_dir = tmp_path / project_backup.BACKUP_DIR_NAME
        project_backup.create_backup(
            database.path, backups_dir, reason="manual", schema_version=database.schema_version
        )
        report = project_health.full_check(database, tmp_path)
        codes = {issue.code for issue in report.issues}
        assert "NO_BACKUPS" not in codes
