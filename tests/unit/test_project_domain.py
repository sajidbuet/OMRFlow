"""Tests for the project domain model: layout, metadata and serialisation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omr_scanner.domain.project import (
    DATABASE_FILE_NAME,
    PROJECT_FILE_NAME,
    PROJECT_FORMAT_VERSION,
    Project,
    ProjectDirectory,
    ProjectLayout,
    ProjectMetadata,
)


def test_layout_resolves_the_documented_structure(tmp_path: Path):
    layout = ProjectLayout(tmp_path / "Exam")

    assert layout.project_file.name == PROJECT_FILE_NAME
    assert layout.database_file.name == DATABASE_FILE_NAME
    assert layout.templates_dir == layout.root / "templates"
    assert layout.scans_original_dir == layout.root / "scans_original"
    assert layout.scans_aligned_dir == layout.root / "scans_aligned"
    assert layout.answer_keys_dir == layout.root / "answer_keys"
    assert layout.candidate_lists_dir == layout.root / "candidate_lists"
    assert layout.exports_dir == layout.root / "exports"
    assert layout.logs_dir == layout.root / "logs"


def test_layout_lists_every_declared_directory(tmp_path: Path):
    layout = ProjectLayout(tmp_path / "Exam")

    names = {path.name for path in layout.all_directories()}

    assert names == {item.value for item in ProjectDirectory}


def test_paths_inside_a_project_are_stored_relative(tmp_path: Path):
    layout = ProjectLayout(tmp_path / "Exam")
    absolute = layout.templates_dir / "sheet.omrt"

    relative = layout.relative_to_root(absolute)

    assert relative == Path("templates") / "sheet.omrt"
    assert not relative.is_absolute()
    assert layout.resolve(relative) == absolute


def test_relative_to_root_rejects_paths_outside_the_project(tmp_path: Path):
    layout = ProjectLayout(tmp_path / "Exam")

    with pytest.raises(ValueError, match=r"not in the subpath|does not start with"):
        layout.relative_to_root(tmp_path / "elsewhere" / "scan.png")


def test_metadata_round_trips_through_json():
    original = ProjectMetadata(name="Midterm 2026", description="Section A")

    restored = ProjectMetadata.model_validate(original.model_dump(mode="json"))

    assert restored == original
    assert restored.project_format_version == PROJECT_FORMAT_VERSION


def test_metadata_generates_a_unique_project_id():
    first = ProjectMetadata(name="Exam")
    second = ProjectMetadata(name="Exam")

    assert first.project_id != second.project_id


@pytest.mark.parametrize("name", ["", "   ", "Exam/2026", "Exam: Physics", "Exam?", "Exam."])
def test_metadata_rejects_names_that_are_not_valid_folder_names(name: str):
    with pytest.raises(ValidationError):
        ProjectMetadata(name=name)


def test_metadata_strips_surrounding_whitespace():
    assert ProjectMetadata(name="  Final Exam  ").name == "Final Exam"


def test_metadata_requires_timezone_aware_timestamps():
    with pytest.raises(ValidationError, match="timezone aware"):
        ProjectMetadata(name="Exam", created_at=datetime(2026, 1, 1))


def test_touched_advances_only_the_modified_timestamp():
    epoch = datetime(2026, 1, 1, tzinfo=UTC)
    original = ProjectMetadata(name="Exam", created_at=epoch, modified_at=epoch)

    updated = original.touched()

    assert updated.created_at == original.created_at
    assert updated.modified_at > original.modified_at
    assert updated.project_id == original.project_id


def test_project_exposes_name_and_root(tmp_path: Path):
    layout = ProjectLayout(tmp_path / "Exam")
    project = Project(ProjectMetadata(name="Exam"), layout)

    assert project.name == "Exam"
    assert project.root == layout.root
    assert "Exam" in repr(project)
