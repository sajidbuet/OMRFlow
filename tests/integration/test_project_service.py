"""End-to-end tests for creating, validating, opening and closing projects."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from omr_scanner.domain.project import (
    PROJECT_FORMAT_VERSION,
    ProjectDirectory,
    ProjectLayout,
    ProjectMetadata,
)
from omr_scanner.errors import ProjectExistsError, ProjectValidationError
from omr_scanner.services import (
    create_project,
    is_project_directory,
    open_project,
    read_project_metadata,
)
from omr_scanner.services.project_lock import ProjectLockHeldError
from omr_scanner.utils.json_io import read_json, write_json_atomic
from omr_scanner.utils.logging_setup import configure_logging


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------
def test_create_project_builds_the_documented_directory_structure(workspace: Path):
    with create_project(workspace, "Physics Midterm") as session:
        root = session.root

        assert root == workspace / "Physics Midterm"
        assert (root / "project.json").is_file()
        assert (root / "database.sqlite").is_file()
        for directory in ProjectDirectory:
            assert (root / directory.value).is_dir()


def test_create_project_writes_readable_metadata(workspace: Path):
    with create_project(workspace, "Chemistry Final", description="Batch 2026") as session:
        payload = read_json(session.project.layout.project_file)

    assert payload["name"] == "Chemistry Final"
    assert payload["description"] == "Batch 2026"
    assert payload["project_format_version"] == PROJECT_FORMAT_VERSION
    assert payload["project_id"]


def test_create_project_mirrors_identity_into_the_database(workspace: Path):
    from sqlalchemy import select

    from omr_scanner.database.models import ProjectSetting, SettingKey

    with create_project(workspace, "Biology Quiz") as session, session.database.session() as db:
        stored = dict(db.execute(select(ProjectSetting.key, ProjectSetting.value)).all())

    assert stored[SettingKey.PROJECT_NAME] == "Biology Quiz"
    assert stored[SettingKey.PROJECT_ID] == session.project.metadata.project_id


def test_create_project_starts_a_project_log(workspace: Path, logging_sandbox: None):
    configure_logging(level=logging.INFO)

    with create_project(workspace, "Logged Exam") as session:
        log_file = session.project.layout.log_file

    assert log_file.is_file()
    assert "Logged Exam" in log_file.read_text(encoding="utf-8")


def test_create_project_rejects_an_invalid_name(workspace: Path):
    with pytest.raises(ProjectValidationError):
        create_project(workspace, "Exam/2026")

    assert list(workspace.iterdir()) == []


def test_create_project_refuses_a_non_empty_directory(workspace: Path):
    existing = workspace / "Occupied"
    existing.mkdir()
    (existing / "notes.txt").write_text("keep me", encoding="utf-8")

    with pytest.raises(ProjectExistsError):
        create_project(workspace, "Occupied")

    assert (existing / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_create_project_accepts_a_separate_directory_name(workspace: Path):
    with create_project(workspace, "Exam 2026", directory_name="exam_2026") as session:
        assert session.root.name == "exam_2026"
        assert session.name == "Exam 2026"


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------
def test_project_can_be_reopened_with_its_metadata_intact(workspace: Path):
    with create_project(workspace, "Reopened Exam", description="first run") as created:
        root = created.root
        original_id = created.project.metadata.project_id

    with open_project(root) as reopened:
        assert reopened.name == "Reopened Exam"
        assert reopened.project.metadata.description == "first run"
        assert reopened.project.metadata.project_id == original_id
        assert reopened.database.schema_version >= 1


def test_open_project_recreates_missing_subdirectories(workspace: Path):
    with create_project(workspace, "Synced Exam") as session:
        root = session.root
    (root / ProjectDirectory.EXPORTS.value).rmdir()

    with open_project(root) as reopened:
        assert (reopened.root / ProjectDirectory.EXPORTS.value).is_dir()


def test_open_project_rejects_a_plain_directory(tmp_path: Path):
    with pytest.raises(ProjectValidationError, match="Not an OMRFlow project") as failure:
        open_project(tmp_path)

    assert failure.value.user_message == "This folder is not an OMRFlow project."


def test_open_project_rejects_damaged_metadata(workspace: Path):
    with create_project(workspace, "Damaged Exam") as session:
        root = session.root
    (root / "project.json").write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(ProjectValidationError, match="Could not read") as failure:
        open_project(root)

    assert "damaged" in failure.value.user_message


def test_open_project_rejects_metadata_from_a_newer_format(workspace: Path):
    with create_project(workspace, "Future Exam") as session:
        root = session.root
    project_file = root / "project.json"
    payload = read_json(project_file)
    payload["project_format_version"] = PROJECT_FORMAT_VERSION + 1
    write_json_atomic(project_file, payload)

    with pytest.raises(ProjectValidationError, match="newer"):
        open_project(root)


def test_open_project_rejects_a_project_without_a_database(workspace: Path):
    with create_project(workspace, "Dbless Exam") as session:
        root = session.root
    (root / "database.sqlite").unlink()

    with pytest.raises(ProjectValidationError, match="database"):
        open_project(root)


def test_open_project_rejects_metadata_failing_validation(tmp_path: Path):
    root = tmp_path / "Handmade"
    root.mkdir()
    (root / "project.json").write_text(json.dumps({"name": ""}), encoding="utf-8")
    (root / "database.sqlite").touch()

    with pytest.raises(ProjectValidationError, match="Invalid project file"):
        open_project(root)


# ---------------------------------------------------------------------------
# Helpers and session lifecycle
# ---------------------------------------------------------------------------
def test_is_project_directory_distinguishes_projects_from_folders(workspace: Path, tmp_path: Path):
    with create_project(workspace, "Detectable Exam") as session:
        assert is_project_directory(session.root)

    assert not is_project_directory(tmp_path)


def test_read_project_metadata_does_not_need_the_database(workspace: Path):
    with create_project(workspace, "Metadata Exam") as session:
        root = session.root

    metadata = read_project_metadata(root)

    assert isinstance(metadata, ProjectMetadata)
    assert metadata.name == "Metadata Exam"


def test_closing_a_session_twice_is_harmless(workspace: Path):
    session = create_project(workspace, "Closable Exam")

    session.close()
    session.close()

    assert session.is_closed


def test_closed_project_releases_its_files(workspace: Path):
    """A closed project must be movable; on Windows an open handle would block it."""
    session = create_project(workspace, "Movable Exam")
    root = session.root
    session.close()

    moved = root.parent / "Moved Exam"
    root.rename(moved)

    assert ProjectLayout(moved).database_file.is_file()


# ---------------------------------------------------------------------------
# Project locking and read-only mode (Phase 10, §3, §42)
# ---------------------------------------------------------------------------
def test_a_second_open_of_the_same_project_is_refused(workspace: Path):
    with create_project(workspace, "Locked Exam") as first:
        root = first.root
        with pytest.raises(ProjectLockHeldError):
            open_project(root)


def test_the_lock_is_released_on_close_and_the_project_reopens(workspace: Path):
    with create_project(workspace, "Reopenable Exam") as first:
        root = first.root
    # `first` has closed by now (context manager exited).
    with open_project(root) as second:
        assert second.name == "Reopenable Exam"


def test_force_lock_removes_an_existing_lock_and_opens(workspace: Path):
    with create_project(workspace, "Force Unlocked Exam") as first:
        root = first.root
        with open_project(root, force_lock=True) as second:
            assert second.name == "Force Unlocked Exam"


def test_read_only_open_does_not_take_the_lock_and_forbids_writes(workspace: Path):
    with create_project(workspace, "Read Only Exam") as created:
        root = created.root

    with open_project(root, read_only=True) as session:
        assert session.read_only is True
        # A second, ordinary (writable) open must still succeed: read-only
        # sessions never hold the write lock at all.
        with open_project(root) as writer:
            assert writer.read_only is False


def test_read_only_open_coexists_with_an_already_open_writer(workspace: Path):
    with create_project(workspace, "Coexisting Exam") as writer:
        root = writer.root
        with open_project(root, read_only=True) as reader:
            assert reader.read_only is True
            assert reader.name == "Coexisting Exam"
