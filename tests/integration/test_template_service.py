"""Tests for loading and saving ``.omrt`` template documents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omr_scanner.domain.geometry import NormalizedRect, NormalizedSize
from omr_scanner.domain.template import TEMPLATE_FORMAT_VERSION
from omr_scanner.domain.template_authoring import (
    ColumnLayoutMode,
    build_blank_template,
    generate_question_columns,
    translate_zone,
)
from omr_scanner.errors import TemplateError
from omr_scanner.services import list_templates, load_template, save_template
from omr_scanner.utils.json_io import read_json, write_json_atomic


def test_example_template_loads(example_template_path: Path):
    template = load_template(example_template_path)

    assert template.name == "Example A4 answer sheet"
    assert len(template.zones) == 4


def test_save_then_load_round_trip(tmp_path: Path, example_template_path: Path):
    original = load_template(example_template_path)

    written = save_template(original, tmp_path / "copy.omrt")
    reloaded = load_template(written)

    assert reloaded == original


def test_suffix_is_added_when_missing(tmp_path: Path, example_template_path: Path):
    template = load_template(example_template_path)

    written = save_template(template, tmp_path / "sheet")

    assert written.name == "sheet.omrt"
    assert written.is_file()


def test_missing_file_is_reported(tmp_path: Path):
    with pytest.raises(TemplateError, match="not found"):
        load_template(tmp_path / "absent.omrt")


def test_damaged_file_is_reported(tmp_path: Path):
    path = tmp_path / "broken.omrt"
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(TemplateError, match="damaged"):
        load_template(path)


def test_unrelated_json_is_rejected(tmp_path: Path):
    path = tmp_path / "unrelated.omrt"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    with pytest.raises(TemplateError, match="Invalid template") as failure:
        load_template(path)

    assert failure.value.user_message == "The template file is not valid."


def test_newer_format_version_is_refused(tmp_path: Path, example_template_path: Path):
    payload = read_json(example_template_path)
    payload["format_version"] = TEMPLATE_FORMAT_VERSION + 1
    path = tmp_path / "future.omrt"
    write_json_atomic(path, payload)

    with pytest.raises(TemplateError, match="newer"):
        load_template(path)


def test_semantically_invalid_template_is_refused(tmp_path: Path, example_template_path: Path):
    payload = read_json(example_template_path)
    # Widen the roll-number pitch so the grid no longer fits inside its zone.
    payload["zones"][0]["grid"]["column_pitch"] = 0.2
    path = tmp_path / "inconsistent.omrt"
    write_json_atomic(path, payload)

    with pytest.raises(TemplateError, match="extends beyond the zone bounds"):
        load_template(path)


def test_listing_templates_of_a_project(project_session, example_template_path: Path):
    templates_dir = project_session.project.layout.templates_dir
    assert list_templates(templates_dir) == ()

    save_template(load_template(example_template_path), templates_dir / "sheet.omrt")

    assert [path.name for path in list_templates(templates_dir)] == ["sheet.omrt"]


def test_listing_a_missing_directory_returns_nothing(tmp_path: Path):
    assert list_templates(tmp_path / "absent") == ()


def test_a_manually_adjusted_question_column_survives_save_and_reload(tmp_path: Path):
    """Part D "custom spacing persistence": a non-uniformly-moved column round-trips exactly."""
    template = build_blank_template(name="T", canonical_width_px=1240, canonical_height_px=1754)
    zones = generate_question_columns(
        id_prefix="q",
        label_prefix="Questions",
        first_question=1,
        question_count=100,
        answer_labels=("A", "B", "C", "D"),
        columns=5,
        questions_per_column=20,
        bounds=NormalizedRect(x=0.05, y=0.5, width=0.9, height=0.3),
        bubble_size=NormalizedSize(width=0.01, height=0.01),
        row_pitch=0.02,
        column_pitch=0.03,
        column_gap=0.02,
        layout_mode=ColumnLayoutMode.FROM_PITCH,
    )
    # Move column 4 (0-based index 3) independently, leaving the others alone -
    # the template must preserve this irregular offset exactly.
    moved_column = translate_zone(zones[3], dx=0.037, dy=-0.011)
    zones = (*zones[:3], moved_column, zones[4])
    template = template.model_copy(update={"zones": zones})

    written = save_template(template, tmp_path / "custom_spacing.omrt")
    reloaded = load_template(written)

    # Exact equality, matching `test_save_then_load_round_trip` above - JSON
    # round-trips these floats exactly, and every other column's zone must be
    # completely unaffected by column 4's manual move.
    assert reloaded.zone_by_id("q_3") == moved_column
    for unmoved_id in ("q_0", "q_1", "q_2", "q_4"):
        original = template.zone_by_id(unmoved_id)
        assert reloaded.zone_by_id(unmoved_id) == original
