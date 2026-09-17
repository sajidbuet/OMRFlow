"""Tests for assembling per-bubble decisions into field values."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.conftest import DIGIT_SYMBOLS, build_answer_sheet_template

from omr_scanner.domain.template import (
    FieldType,
    GridFieldDefinition,
    SymbolAxis,
)
from omr_scanner.imaging.metrics import BubbleMeasurement
from omr_scanner.recognition.fields import (
    choose_identifier_zone,
    choose_set_code_zone,
    field_status,
    recognise_grid_zone,
    recognise_question_zone,
    recognise_template,
    zone_groups,
)
from omr_scanner.recognition.models import FieldStatus, MarkStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from omr_scanner.domain.template import OmrTemplate, Zone

MARKED = 0.95
EMPTY = 0.02
FAINT = 0.40
"""Fill ratios that sit unambiguously in each band of the default settings."""


def cell(fill: float, *, usable: bool = True) -> BubbleMeasurement:
    return BubbleMeasurement(
        center_x=0.0,
        center_y=0.0,
        fill_ratio=fill,
        mean_darkness=fill,
        paper_level=240.0,
        contrast=0.3,
        sample_pixels=200,
        usable=usable,
    )


def grid_for(zone: Zone, marks: Mapping[int, str | list[str] | None]) -> dict:
    """Measurements for every bubble of ``zone``, with ``marks`` filled in.

    ``marks`` maps a group key to the label(s) marked in it; a key that is
    absent, or maps to ``None``, leaves that group empty.
    """
    measurements: dict[tuple[int, int], BubbleMeasurement] = {}
    for group in zone_groups(zone):
        wanted = marks.get(group.key)
        chosen = (
            set()
            if wanted is None
            else {wanted} if isinstance(wanted, str) else set(wanted)
        )
        for index, position in enumerate(group.cells):
            measurements[position] = cell(MARKED if group.labels[index] in chosen else EMPTY)
    return measurements


def zone_by_id(template: OmrTemplate, zone_id: str) -> Zone:
    return next(zone for zone in template.zones if zone.id == zone_id)


@pytest.fixture
def template() -> OmrTemplate:
    return build_answer_sheet_template()


class TestZoneGroups:
    def test_a_vertical_grid_makes_one_group_per_column(self, template):
        roll = zone_by_id(template, "roll_number")
        groups = zone_groups(roll)
        assert len(groups) == 6  # six digit positions
        # Position 0's ten bubbles run *down* column 0.
        assert groups[0].cells == tuple((row, 0) for row in range(10))
        assert groups[0].labels == DIGIT_SYMBOLS

    def test_a_horizontal_grid_makes_one_group_per_row(self):
        template = build_answer_sheet_template()
        horizontal = zone_by_id(template, "roll_number").model_copy(
            update={
                "field": GridFieldDefinition(
                    type=FieldType.NUMERIC,
                    symbols=DIGIT_SYMBOLS,
                    character_count=6,
                    symbol_axis=SymbolAxis.HORIZONTAL,
                )
            }
        )
        groups = zone_groups(horizontal)
        assert groups[0].cells == tuple((0, column) for column in range(10))
        assert groups[1].cells == tuple((1, column) for column in range(10))

    def test_a_question_block_makes_one_group_per_question(self, template):
        questions = zone_by_id(template, "questions_0")
        groups = zone_groups(questions)
        assert len(groups) == 10
        assert groups[0].cells == ((0, 0), (0, 1), (0, 2), (0, 3))
        assert groups[0].labels == ("A", "B", "C", "D")

    def test_an_ignored_zone_has_no_groups(self, template):
        from omr_scanner.domain.template import IgnoredFieldDefinition

        ignored = zone_by_id(template, "roll_number").model_copy(
            update={"field": IgnoredFieldDefinition(type=FieldType.IGNORED)}
        )
        assert zone_groups(ignored) == ()


class TestNumericField:
    def test_a_fully_marked_roll_number_reads_back_exactly(self, template):
        roll = zone_by_id(template, "roll_number")
        digits = "120317"
        marks = dict(enumerate(digits))

        result = recognise_grid_zone(
            roll, grid_for(roll, marks), settings=template.recognition
        )
        assert result.value == digits
        assert result.status is FieldStatus.RESOLVED
        assert result.is_reliable is True
        assert result.needs_review is False

    def test_a_leading_zero_survives(self, template):
        # The value is a string of printed positions, never a number.
        roll = zone_by_id(template, "roll_number")
        marks = dict(enumerate("001234"))
        result = recognise_grid_zone(roll, grid_for(roll, marks), settings=template.recognition)
        assert result.value == "001234"

    def test_a_blank_digit_column_is_marked_blank_not_guessed(self, template):
        roll = zone_by_id(template, "roll_number")
        marks: dict[int, str | None] = dict(enumerate("120317"))
        marks[2] = None  # candidate left the third column empty

        result = recognise_grid_zone(roll, grid_for(roll, marks), settings=template.recognition)
        assert result.value == "12_317"
        assert result.status is FieldStatus.INCOMPLETE
        assert result.is_reliable is False
        assert result.groups[2].status is MarkStatus.BLANK

    def test_an_entirely_blank_field_is_blank(self, template):
        roll = zone_by_id(template, "roll_number")
        result = recognise_grid_zone(roll, grid_for(roll, {}), settings=template.recognition)
        assert result.value == "______"
        assert result.status is FieldStatus.BLANK
        assert result.is_reliable is False

    def test_a_double_marked_digit_keeps_both_and_does_not_produce_a_roll_number(
        self, template
    ):
        roll = zone_by_id(template, "roll_number")
        marks: dict[int, str | list[str]] = dict(enumerate("120317"))
        marks[1] = ["2", "7"]

        result = recognise_grid_zone(roll, grid_for(roll, marks), settings=template.recognition)
        assert result.value == "1?0317"
        assert result.status is FieldStatus.MULTIPLE
        assert result.is_reliable is False
        # Both marks are still recoverable from the evidence.
        assert result.groups[1].value == "2-7"

    def test_a_faint_digit_is_uncertain_rather_than_accepted(self, template):
        roll = zone_by_id(template, "roll_number")
        measurements = grid_for(roll, dict(enumerate("120317")))
        # Weaken position 4's mark ("1") to the ambiguous band.
        groups = zone_groups(roll)
        faint_group = groups[4]
        index = faint_group.labels.index("1")
        measurements[faint_group.cells[index]] = cell(FAINT)

        result = recognise_grid_zone(roll, measurements, settings=template.recognition)
        assert result.value == "1203?7"
        assert result.status is FieldStatus.UNCERTAIN
        assert result.is_reliable is False

    def test_a_column_that_could_not_be_sampled_is_unreadable_not_blank(self, template):
        roll = zone_by_id(template, "roll_number")
        measurements = grid_for(roll, dict(enumerate("120317")))
        for position in zone_groups(roll)[0].cells:
            measurements[position] = cell(0.0, usable=False)

        result = recognise_grid_zone(roll, measurements, settings=template.recognition)
        assert result.value == "?20317"
        assert result.status is FieldStatus.UNREADABLE

    def test_a_missing_measurement_is_unreadable_rather_than_absent(self, template):
        roll = zone_by_id(template, "roll_number")
        result = recognise_grid_zone(roll, {}, settings=template.recognition)
        assert result.status is FieldStatus.UNREADABLE
        assert result.value == "??????"

    def test_a_question_block_cannot_be_read_as_a_grid_field(self, template):
        with pytest.raises(ValueError, match="not a character-grid field"):
            recognise_grid_zone(
                zone_by_id(template, "questions_0"), {}, settings=template.recognition
            )


class TestSetCodeField:
    def test_a_single_letter_set_code(self, template):
        zone = zone_by_id(template, "set_code")
        result = recognise_grid_zone(zone, grid_for(zone, {0: "B"}), settings=template.recognition)
        assert result.value == "B"
        assert result.status is FieldStatus.RESOLVED

    def test_a_two_digit_enumerated_set_code_is_never_split_or_renumbered(self):
        # "01" is a set code, not the number one. It must come back as "01".
        template = build_answer_sheet_template(set_symbols=("01", "02", "10", "11"))
        zone = zone_by_id(template, "set_code")
        result = recognise_grid_zone(
            zone, grid_for(zone, {0: "01"}), settings=template.recognition
        )
        assert result.value == "01"
        assert isinstance(result.value, str)

    def test_a_positional_two_column_set_code_spells_both_positions(self):
        template = build_answer_sheet_template(
            set_symbols=DIGIT_SYMBOLS, set_positions=2
        )
        zone = zone_by_id(template, "set_code")
        result = recognise_grid_zone(
            zone, grid_for(zone, {0: "1", 1: "1"}), settings=template.recognition
        )
        assert result.value == "11"

    def test_a_positional_set_code_with_one_blank_position_is_incomplete(self):
        template = build_answer_sheet_template(set_symbols=DIGIT_SYMBOLS, set_positions=2)
        zone = zone_by_id(template, "set_code")
        result = recognise_grid_zone(
            zone, grid_for(zone, {0: "1"}), settings=template.recognition
        )
        assert result.value == "1_"
        assert result.status is FieldStatus.INCOMPLETE

    def test_nothing_assumes_a_set_code_is_one_character_long(self):
        for positions in (1, 2, 3):
            template = build_answer_sheet_template(
                set_symbols=DIGIT_SYMBOLS, set_positions=positions
            )
            zone = zone_by_id(template, "set_code")
            marks = dict.fromkeys(range(positions), "7")
            result = recognise_grid_zone(
                zone, grid_for(zone, marks), settings=template.recognition
            )
            assert result.value == "7" * positions


class TestAlphanumericField:
    def test_the_alphabet_comes_from_the_template_not_from_this_module(self):
        symbols = ("Q", "W", "E", "R", "T", "Y")
        template = build_answer_sheet_template(set_symbols=symbols, set_positions=3)
        zone = zone_by_id(template, "set_code")
        result = recognise_grid_zone(
            zone, grid_for(zone, {0: "W", 1: "Y", 2: "Q"}), settings=template.recognition
        )
        assert result.value == "WYQ"

    def test_a_symbol_the_template_does_not_declare_cannot_be_produced(self):
        # Every label in a result comes from the zone's own symbol list, so an
        # undeclared symbol has no bubble to be read from and no way to appear.
        template = build_answer_sheet_template(set_symbols=("A", "B"), set_positions=1)
        zone = zone_by_id(template, "set_code")
        assert {label for group in zone_groups(zone) for label in group.labels} == {"A", "B"}

        # Marking every bubble there is still yields only declared symbols.
        result = recognise_grid_zone(
            zone, grid_for(zone, {0: ["A", "B"]}), settings=template.recognition
        )
        assert set(result.value) <= {"A", "B", "?", "_"}
        assert result.groups[0].value == "A-B"


class TestQuestionBlock:
    def test_questions_are_numbered_as_printed_not_from_zero(self, template):
        second = zone_by_id(template, "questions_1")
        answers = recognise_question_zone(
            second, grid_for(second, {0: "A"}), settings=template.recognition
        )
        assert answers[0].number == 11
        assert answers[-1].number == 20

    @pytest.mark.parametrize(
        ("marks", "expected_value", "expected_status"),
        [
            ({0: "B"}, "B", MarkStatus.RESOLVED),
            ({}, "", MarkStatus.BLANK),
            ({0: ["B", "D"]}, "B-D", MarkStatus.MULTIPLE),
            ({0: ["A", "B", "C", "D"]}, "A-B-C-D", MarkStatus.MULTIPLE),
        ],
    )
    def test_the_documented_value_conventions(
        self, template, marks, expected_value, expected_status
    ):
        zone = zone_by_id(template, "questions_0")
        answers = recognise_question_zone(
            zone, grid_for(zone, marks), settings=template.recognition
        )
        assert answers[0].value == expected_value
        assert answers[0].status is expected_status

    def test_a_grid_field_cannot_be_read_as_a_question_block(self, template):
        with pytest.raises(ValueError, match="not a question block"):
            recognise_question_zone(
                zone_by_id(template, "roll_number"), {}, settings=template.recognition
            )


class TestFieldStatusSeverity:
    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            ([], FieldStatus.BLANK),
            ([MarkStatus.RESOLVED, MarkStatus.RESOLVED], FieldStatus.RESOLVED),
            ([MarkStatus.BLANK, MarkStatus.BLANK], FieldStatus.BLANK),
            ([MarkStatus.RESOLVED, MarkStatus.BLANK], FieldStatus.INCOMPLETE),
            ([MarkStatus.RESOLVED, MarkStatus.UNCERTAIN], FieldStatus.UNCERTAIN),
            ([MarkStatus.UNCERTAIN, MarkStatus.MULTIPLE], FieldStatus.MULTIPLE),
            ([MarkStatus.MULTIPLE, MarkStatus.UNREADABLE], FieldStatus.UNREADABLE),
        ],
    )
    def test_the_worst_position_decides_the_field(self, statuses, expected):
        assert field_status(statuses) is expected


class TestZoneRoles:
    def test_the_only_numeric_zone_is_the_identifier(self, template):
        assert choose_identifier_zone(template).id == "roll_number"

    def test_the_set_code_zone_is_found_by_its_field_type(self, template):
        assert choose_set_code_zone(template).id == "set_code"

    def test_a_keyword_breaks_the_tie_between_two_numeric_zones(self, template):
        # A booklet serial printed *before* the roll number must not win simply
        # by appearing first in the document.
        serial = zone_by_id(template, "roll_number").model_copy(
            update={"id": "booklet_serial", "label": "Booklet serial"}
        )
        reordered = template.model_copy(
            update={"zones": (serial, *template.zones)}
        )
        assert choose_identifier_zone(reordered).id == "roll_number"

    def test_without_a_keyword_the_first_numeric_zone_wins(self, template):
        first = zone_by_id(template, "roll_number").model_copy(
            update={"id": "field_one", "label": "Field one"}
        )
        second = zone_by_id(template, "roll_number").model_copy(
            update={"id": "field_two", "label": "Field two"}
        )
        reordered = template.model_copy(update={"zones": (first, second)})
        assert choose_identifier_zone(reordered).id == "field_one"

    def test_a_template_with_no_numeric_zone_has_no_identifier(self, template):
        without = template.model_copy(
            update={"zones": tuple(z for z in template.zones if z.id != "roll_number")}
        )
        assert choose_identifier_zone(without) is None


class TestRecogniseTemplate:
    def test_every_zone_is_read_and_answers_are_ordered_by_question_number(self, template):
        measurements = {
            zone.id: grid_for(zone, {0: "A"} if zone.id.startswith("questions") else {0: "1"})
            for zone in template.zones
        }
        result = recognise_template(template, measurements)

        assert [field.zone_id for field in result.fields] == ["roll_number", "set_code"]
        assert [answer.number for answer in result.answers] == list(range(1, 21))
        assert result.identifier_zone_id == "roll_number"
        assert result.set_code_zone_id == "set_code"

    def test_a_zone_missing_from_the_measurements_is_unreadable_not_dropped(self, template):
        result = recognise_template(template, {})
        assert len(result.fields) == 2
        assert all(field.status is FieldStatus.UNREADABLE for field in result.fields)
        assert len(result.answers) == 20

    def test_review_and_mark_counts_summarise_the_sheet(self, template):
        measurements = {}
        for zone in template.zones:
            if zone.id == "questions_0":
                measurements[zone.id] = grid_for(zone, {0: ["A", "C"], 1: "B"})
            elif zone.id.startswith("questions"):
                measurements[zone.id] = grid_for(zone, {})
            else:
                is_roll = zone.id == "roll_number"
                marks = dict(enumerate("120317")) if is_roll else {0: "A"}
                measurements[zone.id] = grid_for(zone, marks)

        result = recognise_template(template, measurements)
        assert result.multiple_mark_count == 1
        assert result.blank_answer_count == 18
        assert result.review_count == 1
        assert result.identifier.value == "120317"
        assert result.set_code.value == "A"
