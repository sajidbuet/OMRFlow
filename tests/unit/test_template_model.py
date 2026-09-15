"""Tests for the ``.omrt`` template model and its validation rules."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize
from omr_scanner.domain.template import (
    TEMPLATE_FORMAT_VERSION,
    BubbleGrid,
    BubbleOverride,
    FieldType,
    GridFieldDefinition,
    MarkerRole,
    OmrTemplate,
    OrientationMarker,
    PageGeometry,
    QuestionBlockFieldDefinition,
    RecognitionSettings,
    RegistrationMarker,
    SymbolAxis,
    Zone,
)
from omr_scanner.utils.json_io import read_json

DIGITS = tuple(str(digit) for digit in range(10))


def _page() -> PageGeometry:
    return PageGeometry(
        width_mm=210.0, height_mm=297.0, canonical_width_px=1240, canonical_height_px=1754
    )


def _markers() -> tuple[RegistrationMarker, ...]:
    positions = {
        MarkerRole.TOP_LEFT: (0.05, 0.035),
        MarkerRole.TOP_RIGHT: (0.95, 0.035),
        MarkerRole.BOTTOM_RIGHT: (0.95, 0.965),
        MarkerRole.BOTTOM_LEFT: (0.05, 0.965),
    }
    return tuple(
        RegistrationMarker(
            role=role,
            center=NormalizedPoint(x=x, y=y),
            size=NormalizedSize(width=0.03, height=0.021),
        )
        for role, (x, y) in positions.items()
    )


def _orientation() -> OrientationMarker:
    return OrientationMarker(
        center=NormalizedPoint(x=0.14, y=0.035),
        size=NormalizedSize(width=0.05, height=0.012),
    )


def _roll_zone() -> Zone:
    return Zone(
        id="roll_number",
        label="Roll number",
        bounds=NormalizedRect(x=0.08, y=0.12, width=0.24, height=0.36),
        field=GridFieldDefinition(
            type=FieldType.NUMERIC, symbols=DIGITS, character_count=5
        ),
        grid=BubbleGrid(
            origin=NormalizedPoint(x=0.10, y=0.15),
            row_pitch=0.035,
            column_pitch=0.05,
            bubble_size=NormalizedSize(width=0.024, height=0.017),
        ),
    )


def _template(**overrides: Any) -> OmrTemplate:
    values: dict[str, Any] = {
        "name": "Unit test sheet",
        "page": _page(),
        "registration_markers": _markers(),
        "orientation_marker": _orientation(),
        "zones": (_roll_zone(),),
    }
    values.update(overrides)
    return OmrTemplate(**values)


# ---------------------------------------------------------------------------
# Bubble geometry
# ---------------------------------------------------------------------------
def test_bubble_center_follows_the_grid_pitch():
    grid = BubbleGrid(
        origin=NormalizedPoint(x=0.10, y=0.15),
        row_pitch=0.035,
        column_pitch=0.05,
        bubble_size=NormalizedSize(width=0.024, height=0.017),
    )

    center = grid.bubble_center(row=9, column=4)

    assert center.x == pytest.approx(0.30)
    assert center.y == pytest.approx(0.465)


def test_bubble_override_replaces_the_computed_center():
    grid = BubbleGrid(
        origin=NormalizedPoint(x=0.1, y=0.1),
        row_pitch=0.02,
        column_pitch=0.02,
        bubble_size=NormalizedSize(width=0.02, height=0.014),
        overrides=(
            BubbleOverride(row=1, column=1, center=NormalizedPoint(x=0.5, y=0.5)),
        ),
    )

    assert grid.bubble_center(1, 1) == NormalizedPoint(x=0.5, y=0.5)
    assert grid.bubble_center(0, 1).x == pytest.approx(0.12)


def test_bubble_center_rejects_negative_indices():
    grid = BubbleGrid(
        origin=NormalizedPoint(x=0.1, y=0.1),
        row_pitch=0.02,
        column_pitch=0.02,
        bubble_size=NormalizedSize(width=0.02, height=0.014),
    )

    with pytest.raises(ValueError, match="zero or positive"):
        grid.bubble_center(-1, 0)


# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------
def test_numeric_field_rows_and_columns_follow_the_symbol_axis():
    vertical = GridFieldDefinition(type=FieldType.NUMERIC, symbols=DIGITS, character_count=5)
    horizontal = GridFieldDefinition(
        type=FieldType.NUMERIC,
        symbols=DIGITS,
        character_count=5,
        symbol_axis=SymbolAxis.HORIZONTAL,
    )

    assert (vertical.rows, vertical.columns) == (10, 5)
    assert (horizontal.rows, horizontal.columns) == (5, 10)


def test_question_block_reports_its_last_question():
    block = QuestionBlockFieldDefinition(
        type=FieldType.QUESTION_BLOCK,
        first_question=21,
        question_count=20,
        answer_labels=("A", "B", "C", "D"),
    )

    assert block.last_question == 40
    assert (block.rows, block.columns) == (20, 4)


def test_duplicate_symbols_are_rejected():
    with pytest.raises(ValidationError):
        GridFieldDefinition(type=FieldType.NUMERIC, symbols=("1", "1"), character_count=1)


# ---------------------------------------------------------------------------
# Zone validation
# ---------------------------------------------------------------------------
def test_zone_rejects_a_grid_that_overflows_its_bounds():
    with pytest.raises(ValidationError, match="extends beyond the zone bounds"):
        Zone(
            id="roll",
            label="Roll",
            bounds=NormalizedRect(x=0.08, y=0.12, width=0.10, height=0.10),
            field=GridFieldDefinition(
                type=FieldType.NUMERIC, symbols=DIGITS, character_count=5
            ),
            grid=BubbleGrid(
                origin=NormalizedPoint(x=0.10, y=0.15),
                row_pitch=0.035,
                column_pitch=0.05,
                bubble_size=NormalizedSize(width=0.024, height=0.017),
            ),
        )


def test_zone_requires_a_grid_unless_it_is_ignored():
    with pytest.raises(ValidationError, match="must define a bubble grid"):
        Zone(
            id="roll",
            label="Roll",
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.2),
            field=GridFieldDefinition(
                type=FieldType.NUMERIC, symbols=DIGITS, character_count=1
            ),
        )


def test_zone_rejects_an_invalid_display_colour():
    with pytest.raises(ValidationError, match="RRGGBB"):
        Zone(
            id="ignored",
            label="Logo",
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.2),
            field={"type": "ignored"},
            display_color="blue",
        )


def test_ignored_zone_counts_no_bubbles():
    zone = Zone(
        id="ignored",
        label="Logo",
        bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.2),
        field={"type": "ignored"},
    )

    assert zone.bubble_count == 0


def test_zone_bubble_count_multiplies_rows_and_columns():
    assert _roll_zone().bubble_count == 50


# ---------------------------------------------------------------------------
# Template validation
# ---------------------------------------------------------------------------
def test_template_requires_all_four_marker_roles():
    duplicated = (*_markers()[:3], _markers()[0])

    with pytest.raises(ValidationError, match="Missing registration marker"):
        _template(registration_markers=duplicated)


def test_template_rejects_duplicate_zone_ids():
    with pytest.raises(ValidationError, match="Duplicate zone id"):
        _template(zones=(_roll_zone(), _roll_zone()))


def test_template_lookup_helpers():
    template = _template()

    assert template.zone_by_id("roll_number") is not None
    assert template.zone_by_id("absent") is None
    assert template.marker_by_role(MarkerRole.TOP_RIGHT).center.x == pytest.approx(0.95)


def test_zone_recognition_settings_fall_back_to_the_template_defaults():
    tuned = RecognitionSettings(fill_ratio_threshold=0.7, blank_ratio_threshold=0.3)
    zone = _roll_zone().model_copy(update={"recognition": tuned})
    template = _template(zones=(zone,))

    assert template.effective_recognition(zone) is tuned
    assert template.effective_recognition(_roll_zone()) == template.recognition


def test_recognition_settings_reject_an_impossible_threshold_order():
    with pytest.raises(ValidationError, match="must be smaller than"):
        RecognitionSettings(fill_ratio_threshold=0.3, blank_ratio_threshold=0.4)


def test_template_round_trips_through_json():
    original = _template()

    restored = OmrTemplate.model_validate(original.model_dump(mode="json"))

    assert restored == original


# ---------------------------------------------------------------------------
# The shipped example must stay valid
# ---------------------------------------------------------------------------
def test_example_template_resource_is_valid(example_template_path):
    template = OmrTemplate.model_validate(read_json(example_template_path))

    assert template.format_version == TEMPLATE_FORMAT_VERSION
    assert len(template.registration_markers) == 4
    assert {zone.id for zone in template.zones} == {
        "roll_number",
        "set_code",
        "questions_1_20",
        "instructions",
    }
    assert template.zone_by_id("questions_1_20") is not None
