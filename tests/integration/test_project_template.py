"""The project's template is project state, not per-screen state.

Every one of these tests works a real project directory on disk, because the
thing under test is precisely what survives being written to ``project.json``
and read back - an in-memory double would pass whether or not that worked.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.domain.project import ProjectMetadata
from omr_scanner.errors import ProjectValidationError
from omr_scanner.services import (
    ProjectSession,
    active_template_is_missing,
    adopt_template_if_unambiguous,
    create_project,
    discover_templates,
    open_project,
    resolve_active_template,
    save_template,
    set_active_template,
)
from omr_scanner.utils.json_io import read_json


def _add_template(session: ProjectSession, stem: str) -> Path:
    """Write a valid template into the project under ``stem``."""
    return save_template(
        build_answer_sheet_template(), session.project.layout.templates_dir / f"{stem}.omrt"
    )


# ---------------------------------------------------------------------------
# 1. A chosen template is remembered
# ---------------------------------------------------------------------------
def test_choosing_a_template_records_it_in_project_metadata(workspace: Path):
    with create_project(workspace, "Physics") as session:
        template = _add_template(session, "Midterm")
        set_active_template(session, template)

        payload = read_json(session.project.layout.project_file)

    assert payload["active_template"] == "templates/Midterm.omrt"


# ---------------------------------------------------------------------------
# 2. Reopening restores it without asking
# ---------------------------------------------------------------------------
def test_reopening_a_project_restores_the_template(workspace: Path):
    with create_project(workspace, "Physics") as session:
        root = session.root
        set_active_template(session, _add_template(session, "Midterm"))

    with open_project(root) as reopened:
        resolved = resolve_active_template(reopened.project)

    assert resolved is not None
    assert resolved.name == "Midterm.omrt"
    assert resolved.is_file()


# ---------------------------------------------------------------------------
# 3. The stored path is relative, so the project stays portable
# ---------------------------------------------------------------------------
def test_the_template_path_survives_moving_the_project(workspace: Path, tmp_path: Path):
    with create_project(workspace, "Physics") as session:
        root = session.root
        set_active_template(session, _add_template(session, "Midterm"))
        stored = session.project.metadata.active_template

    # Stored as a project-relative POSIX path: no drive letter, no separator
    # that means something different on the other operating system.
    assert stored == "templates/Midterm.omrt"

    moved = tmp_path / "somewhere else" / "Physics"
    moved.parent.mkdir(parents=True)
    shutil.copytree(root, moved)

    with open_project(moved) as relocated:
        resolved = resolve_active_template(relocated.project)

    assert resolved is not None
    assert resolved.is_file()
    assert moved in resolved.parents


# ---------------------------------------------------------------------------
# 4. A template that has gone missing is reported, not crashed on
# ---------------------------------------------------------------------------
def test_a_deleted_template_leaves_the_project_openable(workspace: Path):
    with create_project(workspace, "Physics") as session:
        root = session.root
        template = _add_template(session, "Midterm")
        set_active_template(session, template)
        template.unlink()

    with open_project(root) as reopened:
        assert active_template_is_missing(reopened.project)
        assert resolve_active_template(reopened.project) is None
        # The recorded choice is kept, not silently erased: it is what tells
        # the user *which* file to put back.
        assert reopened.project.metadata.active_template == "templates/Midterm.omrt"


def test_replacing_a_missing_template_clears_the_problem(workspace: Path):
    with create_project(workspace, "Physics") as session:
        template = _add_template(session, "Midterm")
        set_active_template(session, template)
        template.unlink()

        set_active_template(session, _add_template(session, "Replacement"))

        assert not active_template_is_missing(session.project)
        resolved = resolve_active_template(session.project)
        assert resolved is not None and resolved.name == "Replacement.omrt"


# ---------------------------------------------------------------------------
# 5. Adoption: one template is obvious, two are not
# ---------------------------------------------------------------------------
def test_a_lone_template_is_adopted_without_being_asked_about(workspace: Path):
    with create_project(workspace, "Physics") as session:
        _add_template(session, "Midterm")
        assert session.project.metadata.active_template is None

        adopted = adopt_template_if_unambiguous(session)

        assert adopted is not None and adopted.name == "Midterm.omrt"
        assert session.project.metadata.active_template == "templates/Midterm.omrt"


def test_two_templates_are_never_guessed_between(workspace: Path):
    with create_project(workspace, "Physics") as session:
        _add_template(session, "Midterm")
        _add_template(session, "Final")

        assert adopt_template_if_unambiguous(session) is None
        assert session.project.metadata.active_template is None


def test_an_already_chosen_template_is_not_second_guessed(workspace: Path):
    with create_project(workspace, "Physics") as session:
        chosen = _add_template(session, "Midterm")
        set_active_template(session, chosen)
        _add_template(session, "Final")

        assert adopt_template_if_unambiguous(session) is None
        assert session.project.metadata.active_template == "templates/Midterm.omrt"


def test_discovery_is_ordered_the_same_way_on_every_machine(workspace: Path):
    with create_project(workspace, "Physics") as session:
        for stem in ("zebra", "Alpha", "middle"):
            _add_template(session, stem)

        names = [path.name for path in discover_templates(session.project)]

    assert names == ["Alpha.omrt", "middle.omrt", "zebra.omrt"]


def test_a_project_with_no_templates_is_simply_empty(workspace: Path):
    with create_project(workspace, "Physics") as session:
        assert discover_templates(session.project) == ()
        assert adopt_template_if_unambiguous(session) is None
        assert resolve_active_template(session.project) is None
        # Nothing was ever chosen, so nothing is missing.
        assert not active_template_is_missing(session.project)


# ---------------------------------------------------------------------------
# 6. Switching projects does not carry the template across
# ---------------------------------------------------------------------------
def test_each_project_keeps_its_own_template(workspace: Path):
    with create_project(workspace, "Physics") as physics:
        physics_root = physics.root
        set_active_template(physics, _add_template(physics, "PhysicsSheet"))

    with create_project(workspace, "Chemistry") as chemistry:
        chemistry_root = chemistry.root
        set_active_template(chemistry, _add_template(chemistry, "ChemistrySheet"))

    with open_project(physics_root) as physics:
        physics_template = resolve_active_template(physics.project)
    with open_project(chemistry_root) as chemistry:
        chemistry_template = resolve_active_template(chemistry.project)

    assert physics_template is not None and physics_template.name == "PhysicsSheet.omrt"
    assert chemistry_template is not None and chemistry_template.name == "ChemistrySheet.omrt"
    assert physics_root in physics_template.parents
    assert chemistry_root in chemistry_template.parents


# ---------------------------------------------------------------------------
# 7. Path safety
# ---------------------------------------------------------------------------
def test_a_template_outside_the_project_is_refused(workspace: Path, tmp_path: Path):
    outside = save_template(build_answer_sheet_template(), tmp_path / "loose.omrt")
    with create_project(workspace, "Physics") as session:
        with pytest.raises(ProjectValidationError):
            set_active_template(session, outside)
        assert session.project.metadata.active_template is None


def test_a_read_only_session_does_not_record_a_choice(workspace: Path):
    with create_project(workspace, "Physics") as session:
        root = session.root
        _add_template(session, "Midterm")

    with open_project(root, read_only=True) as session:
        with pytest.raises(ProjectValidationError):
            set_active_template(session, session.project.layout.templates_dir / "Midterm.omrt")
        # Adoption still answers the question - a read-only project must still
        # be usable - it just does not write the answer down.
        adopted = adopt_template_if_unambiguous(session)
        assert adopted is not None and adopted.name == "Midterm.omrt"
        assert session.project.metadata.active_template is None

    assert read_json(root / "project.json").get("active_template") is None


@pytest.mark.parametrize(
    "hostile",
    [
        "../outside/sheet.omrt",
        "templates/../../sheet.omrt",
        "/etc/sheet.omrt",
        "C:/Windows/sheet.omrt",
        "\\\\server\\share\\sheet.omrt",
    ],
)
def test_metadata_rejects_a_stored_path_that_escapes_the_project(hostile: str):
    """A hand-edited or hostile ``project.json`` cannot point outside the project."""
    with pytest.raises(ValueError):
        ProjectMetadata(name="Physics", active_template=hostile)


def test_metadata_normalises_a_windows_separator_to_posix():
    metadata = ProjectMetadata(name="Physics", active_template="templates\\Midterm.omrt")

    assert metadata.active_template == "templates/Midterm.omrt"


# ---------------------------------------------------------------------------
# 8. Backward compatibility
# ---------------------------------------------------------------------------
def test_a_project_written_before_this_feature_still_opens(workspace: Path):
    """``active_template`` is absent from every project.json written so far."""
    with create_project(workspace, "Physics") as session:
        root = session.root
        _add_template(session, "Midterm")

    project_file = root / "project.json"
    payload = read_json(project_file)
    payload.pop("active_template", None)
    project_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with open_project(root) as reopened:
        assert reopened.project.metadata.active_template is None
        assert not active_template_is_missing(reopened.project)
        # ...and the single template it owns is adopted on first open, so the
        # upgrade costs the user nothing.
        assert adopt_template_if_unambiguous(reopened) is not None
