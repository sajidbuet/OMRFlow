"""Phase 10 integration tests: migration, provenance, locking, backup, health.

Purpose:
    Exercise the production-hardening services against a real project
    database, the same way every earlier phase's integration suite does -
    real recognition results, real SQLite files, real migrations - rather
    than mocks standing in for the thing being hardened.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import text

from omr_scanner.database.engine import open_project_database
from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.services import project_backup, project_service


class TestMigrationOntoAnExistingPhase9Project:
    """A project reported on before Phase 10 must keep everything and gain the rest."""

    def test_a_phase_9_project_upgrades_and_gains_hardening_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "database.sqlite"
        handle = open_project_database(db_path, create=True)
        handle.close()

        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                DROP TABLE IF EXISTS batch_scan_history;
                DROP TABLE IF EXISTS processing_manifest;
                DELETE FROM schema_migration WHERE version >= 7;
                """
            )
            connection.commit()
        finally:
            connection.close()

        reopened = open_project_database(db_path)
        try:
            assert reopened.schema_version == SCHEMA_VERSION
            with reopened.session() as session:
                names = {
                    row[0]
                    for row in session.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    ).all()
                }
                columns = {
                    row[1]
                    for row in session.execute(text("PRAGMA table_info(batch_scan)")).all()
                }
            assert {"batch_scan_history", "processing_manifest"} <= names
            assert {"content_sha256", "content_hash_algorithm"} <= columns
        finally:
            reopened.close()

    def test_batch_scan_history_is_append_only(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import delete, insert, update

        from omr_scanner.database.models import BatchScanHistory
        from omr_scanner.errors import DatabaseError
        from omr_scanner.services import batch_store

        db_path = tmp_path / "database.sqlite"
        handle = open_project_database(db_path, create=True)

        scan_path = tmp_path / "scan_0001.png"
        batch_id = batch_store.create_batch(
            handle, [scan_path], identity=batch_store.BatchIdentity()
        )
        scan_id = batch_store.scan_ids_by_path(handle, batch_id)[scan_path]

        with handle.session() as session:
            session.execute(
                insert(BatchScanHistory).values(
                    scan_id=scan_id,
                    batch_id=batch_id,
                    previous_status="completed",
                    previous_outcome="complete",
                    previous_attempt_count=1,
                    previous_result_json="{}",
                    previous_content_sha256="",
                    reason="test",
                    requested_by="tester",
                    archived_at=datetime.now(UTC),
                )
            )

        try:
            try:
                with handle.session() as session:
                    session.execute(
                        update(BatchScanHistory)
                        .where(BatchScanHistory.scan_id == scan_id)
                        .values(reason="tampered")
                    )
            except DatabaseError:
                pass
            else:
                raise AssertionError("Expected the append-only trigger to refuse an UPDATE")

            try:
                with handle.session() as session:
                    session.execute(
                        delete(BatchScanHistory).where(BatchScanHistory.scan_id == scan_id)
                    )
            except DatabaseError:
                pass
            else:
                raise AssertionError("Expected the append-only trigger to refuse a DELETE")
        finally:
            handle.close()


class TestAutomaticBackupBeforeMigration:
    """A backup must be created automatically before a non-trivial migration.

    Phase 10 audit finding: §5 asks for this trigger explicitly; the prior
    implementation pass built the backup mechanism itself but never wired
    it in. Fixed and pinned here.
    """

    def test_opening_an_older_schema_project_creates_a_backup_first(
        self, tmp_path: Path
    ) -> None:
        with project_service.create_project(tmp_path, "Pre-Migration Exam") as session:
            root = session.root
            db_path = session.database.path

        # Wind the schema ledger back, without touching any table - only
        # `current_schema_version` (which reads this ledger) needs to
        # believe an older version is current for the trigger to fire.
        connection = sqlite3.connect(db_path)
        try:
            connection.execute("DELETE FROM schema_migration WHERE version >= 7")
            connection.commit()
        finally:
            connection.close()

        backups_dir = root / project_backup.BACKUP_DIR_NAME
        assert not backups_dir.exists()

        with project_service.open_project(root) as reopened:
            assert reopened.database.schema_version == SCHEMA_VERSION

        entries = project_backup.list_backups(backups_dir)
        assert len(entries) == 1
        assert entries[0].is_complete
        assert "before-migration" in (entries[0].manifest.reason if entries[0].manifest else "")

    def test_opening_an_already_current_project_creates_no_backup(
        self, tmp_path: Path
    ) -> None:
        with project_service.create_project(tmp_path, "Current Exam") as session:
            root = session.root

        with project_service.open_project(root):
            pass

        backups_dir = root / project_backup.BACKUP_DIR_NAME
        assert project_backup.list_backups(backups_dir) == ()

    def test_a_freshly_created_project_creates_no_backup(self, tmp_path: Path) -> None:
        # Creating a project migrates it from schema 0 - there is no existing
        # data to protect, and no backup should be made merely for that.
        with project_service.create_project(tmp_path, "Brand New Exam") as session:
            root = session.root

        backups_dir = root / project_backup.BACKUP_DIR_NAME
        assert not backups_dir.exists()
