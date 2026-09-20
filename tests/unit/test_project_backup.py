"""Unit tests for project backup/snapshot (Phase 10, §5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner.database.engine import open_project_database
from omr_scanner.services import project_backup


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    path = tmp_path / "database.sqlite"
    open_project_database(path, create=True).close()
    return path


class TestCreateBackup:
    def test_creates_a_verifiable_backup_with_a_manifest(
        self, database_path: Path, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        manifest = project_backup.create_backup(
            database_path, backups_dir, reason="manual", schema_version=7
        )
        backup_path = backups_dir / manifest.backup_file
        assert backup_path.is_file()
        assert (backups_dir / (manifest.backup_file + ".json")).is_file()
        assert manifest.schema_version == 7
        assert manifest.reason == "manual"

    def test_two_backups_never_collide(self, database_path: Path, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        first = project_backup.create_backup(
            database_path, backups_dir, reason="manual", schema_version=7
        )
        second = project_backup.create_backup(
            database_path, backups_dir, reason="manual", schema_version=7
        )
        assert first.backup_file != second.backup_file

    def test_a_missing_source_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(project_backup.BackupError):
            project_backup.create_backup(
                tmp_path / "nope.sqlite", tmp_path / "backups", reason="x", schema_version=1
            )


class TestListAndVerify:
    def test_a_complete_backup_is_listed_and_verifies(
        self, database_path: Path, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        project_backup.create_backup(database_path, backups_dir, reason="manual", schema_version=7)

        entries = project_backup.list_backups(backups_dir)
        assert len(entries) == 1
        assert entries[0].is_complete
        assert project_backup.verify_backup(entries[0])

    def test_an_incomplete_backup_missing_its_manifest_is_reported_separately(
        self, database_path: Path, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        manifest = project_backup.create_backup(
            database_path, backups_dir, reason="manual", schema_version=7
        )
        (backups_dir / (manifest.backup_file + ".json")).unlink()

        entries = project_backup.list_backups(backups_dir)
        assert len(entries) == 1
        assert entries[0].is_complete is False
        assert entries[0].manifest is None

    def test_a_tampered_backup_fails_verification(
        self, database_path: Path, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        manifest = project_backup.create_backup(
            database_path, backups_dir, reason="manual", schema_version=7
        )
        backup_path = backups_dir / manifest.backup_file
        with backup_path.open("ab") as handle:
            handle.write(b"tampered bytes")

        entries = project_backup.list_backups(backups_dir)
        assert not project_backup.verify_backup(entries[0])

    def test_no_backups_directory_yields_an_empty_list(self, tmp_path: Path) -> None:
        assert project_backup.list_backups(tmp_path / "nonexistent") == ()


class TestRestoreBackup:
    def test_restores_to_a_new_location(self, database_path: Path, tmp_path: Path) -> None:
        backups_dir = tmp_path / "backups"
        project_backup.create_backup(database_path, backups_dir, reason="manual", schema_version=7)
        entry = project_backup.list_backups(backups_dir)[0]

        destination = tmp_path / "restored.sqlite"
        restored = project_backup.restore_backup(entry, destination)
        assert restored.is_file()

    def test_never_overwrites_an_existing_destination(
        self, database_path: Path, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        project_backup.create_backup(database_path, backups_dir, reason="manual", schema_version=7)
        entry = project_backup.list_backups(backups_dir)[0]

        destination = tmp_path / "restored.sqlite"
        destination.write_bytes(b"pre-existing content")

        restored = project_backup.restore_backup(entry, destination)
        assert restored != destination
        assert destination.read_bytes() == b"pre-existing content"

    def test_an_incomplete_backup_cannot_be_restored(
        self, database_path: Path, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        manifest = project_backup.create_backup(
            database_path, backups_dir, reason="manual", schema_version=7
        )
        (backups_dir / (manifest.backup_file + ".json")).unlink()
        entry = project_backup.list_backups(backups_dir)[0]

        with pytest.raises(project_backup.BackupError):
            project_backup.restore_backup(entry, tmp_path / "restored.sqlite")

    def test_a_tampered_backup_cannot_be_restored(
        self, database_path: Path, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        manifest = project_backup.create_backup(
            database_path, backups_dir, reason="manual", schema_version=7
        )
        backup_path = backups_dir / manifest.backup_file
        with backup_path.open("ab") as handle:
            handle.write(b"tampered")
        entry = project_backup.list_backups(backups_dir)[0]

        with pytest.raises(project_backup.BackupError):
            project_backup.restore_backup(entry, tmp_path / "restored.sqlite")
