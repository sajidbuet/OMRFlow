"""Tests for the region-generation and designer-validation helpers.

These are pure domain tests - no Qt, no image, no file I/O - covering the
arithmetic that turns a user's high-level intent ("7 digit student ID",
"questions 1-100 in 4 columns") into valid `.omrt` geometry, and the
designer-facing checks layered on top of what `OmrTemplate` already enforces.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize
from omr_scanner.domain.template import FieldType, MarkerRole, OmrTemplate, SymbolAxis, Zone
from omr_scanner.domain.template_authoring import (
    build_blank_template,
    fit_grid_to_bounds,
    generate_character_grid_zone,
    generate_ignored_zone,
    generate_question_columns,
    resize_zone,
    translate_zone,
    validate_template_for_designer,
)

DIGITS = tuple(str(digit) for digit in range(10))
LETTERS = ("A", "B", "C", "D")


class TestFitGridToBounds:
    def test_a_single_bubble_is_centred_in_its_bounds(self):
        bounds = NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.1)
        grid = fit_grid_to_bounds(
            bounds=bounds, rows=1, columns=1, bubble_size=NormalizedSize(width=0.02, height=0.02)
        )
        assert grid.origin.x == pytest.approx(bounds.center.x)
        assert grid.origin.y == pytest.approx(bounds.center.y)
        assert grid.row_pitch == 0.0
        assert grid.column_pitch == 0.0

    def test_the_first_and_last_bubble_are_inset_by_half_a_bubble(self):
        bounds = NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.2)
        bubble = NormalizedSize(width=0.02, height=0.02)
        grid = fit_grid_to_bounds(bounds=bounds, rows=2, columns=4, bubble_size=bubble)

        first = grid.bubble_center(0, 0)
        last = grid.bubble_center(1, 3)
        assert first.x == pytest.approx(bounds.x + bubble.width / 2)
        assert first.y == pytest.approx(bounds.y + bubble.height / 2)
        assert last.x == pytest.approx(bounds.right - bubble.width / 2)
        assert last.y == pytest.approx(bounds.bottom - bubble.height / 2)

    def test_every_bubble_centre_lies_within_the_bounds(self):
        bounds = NormalizedRect(x=0.05, y=0.05, width=0.4, height=0.6)
        bubble = NormalizedSize(width=0.015, height=0.012)
        grid = fit_grid_to_bounds(bounds=bounds, rows=10, columns=7, bubble_size=bubble)
        for row in range(10):
            for column in range(7):
                center = grid.bubble_center(row, column)
                assert bounds.contains_point(center)

    def test_zero_rows_is_rejected(self):
        with pytest.raises(ValueError, match="at least one row"):
            fit_grid_to_bounds(
                bounds=NormalizedRect(x=0, y=0, width=0.5, height=0.5),
                rows=0,
                columns=3,
                bubble_size=NormalizedSize(width=0.02, height=0.02),
            )

    def test_a_bubble_too_large_for_the_bounds_is_rejected(self):
        with pytest.raises(ValueError, match="does not fit"):
            fit_grid_to_bounds(
                bounds=NormalizedRect(x=0, y=0, width=0.05, height=0.05),
                rows=1,
                columns=1,
                bubble_size=NormalizedSize(width=0.5, height=0.5),
            )


class TestGenerateCharacterGridZone:
    def test_a_seven_digit_student_id_has_seventy_bubbles(self):
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=7,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.5),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        assert zone.bubble_count == 70
        assert zone.field.type is FieldType.NUMERIC

    def test_a_question_set_region_has_one_bubble_per_value(self):
        zone = generate_character_grid_zone(
            zone_id="set",
            label="Question set",
            field_type=FieldType.SET_CODE,
            symbols=LETTERS,
            character_count=1,
            bounds=NormalizedRect(x=0.4, y=0.1, width=0.1, height=0.2),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        assert zone.bubble_count == 4

    def test_a_question_block_field_type_is_rejected(self):
        with pytest.raises(ValueError, match="not a character-grid field kind"):
            generate_character_grid_zone(
                zone_id="x",
                label="x",
                field_type=FieldType.QUESTION_BLOCK,
                symbols=LETTERS,
                character_count=1,
                bounds=NormalizedRect(x=0, y=0, width=0.1, height=0.1),
                bubble_size=NormalizedSize(width=0.02, height=0.02),
            )

    def test_a_generated_zone_is_a_valid_domain_object(self):
        # If this constructs without raising, every OmrTemplate/Zone invariant
        # (grid fits bounds, valid colour, non-empty symbols) already holds.
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.5),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
            symbol_axis=SymbolAxis.HORIZONTAL,
        )
        assert zone.field.symbol_axis is SymbolAxis.HORIZONTAL


class TestGenerateQuestionColumns:
    def test_one_hundred_questions_in_four_columns_of_four_choices(self):
        zones = generate_question_columns(
            id_prefix="q",
            label_prefix="Questions",
            first_question=1,
            question_count=100,
            answer_labels=LETTERS,
            columns=4,
            questions_per_column=25,
            bounds=NormalizedRect(x=0.1, y=0.5, width=0.8, height=0.45),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        assert len(zones) == 4
        assert sum(zone.bubble_count for zone in zones) == 400

    def test_question_numbers_are_contiguous_and_non_overlapping(self):
        zones = generate_question_columns(
            id_prefix="q",
            label_prefix="Questions",
            first_question=1,
            question_count=100,
            answer_labels=LETTERS,
            columns=4,
            questions_per_column=25,
            bounds=NormalizedRect(x=0.1, y=0.5, width=0.8, height=0.45),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        ranges = [(zone.field.first_question, zone.field.last_question) for zone in zones]
        assert ranges == [(1, 25), (26, 50), (51, 75), (76, 100)]

    def test_a_starting_question_number_other_than_one_is_honoured(self):
        zones = generate_question_columns(
            id_prefix="q",
            label_prefix="Questions",
            first_question=21,
            question_count=20,
            answer_labels=LETTERS,
            columns=1,
            questions_per_column=20,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.5),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        assert zones[0].field.first_question == 21
        assert zones[0].field.last_question == 40

    def test_an_uneven_split_leaves_the_last_column_shorter(self):
        zones = generate_question_columns(
            id_prefix="q",
            label_prefix="Questions",
            first_question=1,
            question_count=10,
            answer_labels=LETTERS,
            columns=3,
            questions_per_column=4,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        assert [zone.field.question_count for zone in zones] == [4, 4, 2]

    def test_columns_that_would_be_empty_are_not_created(self):
        zones = generate_question_columns(
            id_prefix="q",
            label_prefix="Questions",
            first_question=1,
            question_count=10,
            answer_labels=LETTERS,
            columns=5,
            questions_per_column=4,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        assert len(zones) == 3

    def test_zone_ids_are_unique_and_use_the_prefix(self):
        zones = generate_question_columns(
            id_prefix="block",
            label_prefix="Questions",
            first_question=1,
            question_count=50,
            answer_labels=LETTERS,
            columns=2,
            questions_per_column=25,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.8, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        ids = [zone.id for zone in zones]
        assert ids == ["block_0", "block_1"]
        assert len(set(ids)) == len(ids)

    def test_zero_columns_is_rejected(self):
        with pytest.raises(ValueError, match="columns"):
            generate_question_columns(
                id_prefix="q",
                label_prefix="Q",
                first_question=1,
                question_count=10,
                answer_labels=LETTERS,
                columns=0,
                questions_per_column=5,
                bounds=NormalizedRect(x=0, y=0, width=0.5, height=0.5),
                bubble_size=NormalizedSize(width=0.02, height=0.02),
            )

    def test_a_gap_leaving_no_room_is_rejected(self):
        with pytest.raises(ValueError, match="column_gap"):
            generate_question_columns(
                id_prefix="q",
                label_prefix="Q",
                first_question=1,
                question_count=10,
                answer_labels=LETTERS,
                columns=4,
                questions_per_column=5,
                bounds=NormalizedRect(x=0, y=0, width=0.1, height=0.5),
                bubble_size=NormalizedSize(width=0.01, height=0.01),
                column_gap=0.5,
            )


class TestGenerateIgnoredZone:
    def test_an_ignored_zone_has_no_grid_and_no_bubbles(self):
        zone = generate_ignored_zone(
            zone_id="logo",
            label="Logo",
            bounds=NormalizedRect(x=0.5, y=0.5, width=0.2, height=0.1),
        )
        assert zone.grid is None
        assert zone.bubble_count == 0


class TestTranslateZone:
    def test_a_zone_moves_by_the_requested_offset(self):
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        moved = translate_zone(zone, dx=0.05, dy=-0.02)
        assert moved.bounds.x == pytest.approx(0.15)
        assert moved.bounds.y == pytest.approx(0.08)
        assert moved.bounds.width == zone.bounds.width
        assert moved.bounds.height == zone.bounds.height

    def test_bubble_centres_move_by_the_same_amount_as_the_bounds(self):
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        before = zone.grid.bubble_center(2, 3)
        moved = translate_zone(zone, dx=0.05, dy=-0.02)
        after = moved.grid.bubble_center(2, 3)
        assert after.x == pytest.approx(before.x + 0.05)
        assert after.y == pytest.approx(before.y - 0.02)

    def test_a_move_off_the_page_is_clamped_not_rejected(self):
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        moved = translate_zone(zone, dx=5.0, dy=5.0)
        assert moved.bounds.right <= 1.0
        assert moved.bounds.bottom <= 1.0

    def test_the_result_is_a_valid_zone(self):
        # Every override, if any, must still land inside the moved bounds -
        # this is really testing that Zone's own validator still accepts it.
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        translate_zone(zone, dx=0.1, dy=0.1)  # does not raise

    def test_an_ignored_zone_translates_without_a_grid(self):
        zone = generate_ignored_zone(
            zone_id="logo", label="Logo", bounds=NormalizedRect(x=0.1, y=0.1, width=0.1, height=0.1)
        )
        moved = translate_zone(zone, dx=0.05, dy=0.05)
        assert moved.bounds.x == pytest.approx(0.15)
        assert moved.grid is None


class TestResizeZone:
    def test_resizing_keeps_the_bubble_count(self):
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        resized = resize_zone(zone, bounds=NormalizedRect(x=0.1, y=0.1, width=0.4, height=0.5))
        assert resized.bubble_count == zone.bubble_count

    def test_resizing_re_fits_the_grid_to_the_new_bounds(self):
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        new_bounds = NormalizedRect(x=0.1, y=0.1, width=0.4, height=0.3)
        resized = resize_zone(zone, bounds=new_bounds)
        last = resized.grid.bubble_center(0, 4)
        assert last.x == pytest.approx(new_bounds.right - 0.01)

    def test_resizing_clears_bubble_overrides(self):
        from omr_scanner.domain.template import BubbleGrid, BubbleOverride

        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.2, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        with_override = zone.model_copy(
            update={
                "grid": BubbleGrid(
                    origin=zone.grid.origin,
                    row_pitch=zone.grid.row_pitch,
                    column_pitch=zone.grid.column_pitch,
                    bubble_size=zone.grid.bubble_size,
                    overrides=(
                        BubbleOverride(row=0, column=0, center=NormalizedPoint(x=0.5, y=0.5)),
                    ),
                )
            }
        )
        resized = resize_zone(
            with_override, bounds=NormalizedRect(x=0.1, y=0.1, width=0.4, height=0.3)
        )
        assert resized.grid.overrides == ()

    def test_an_ignored_zone_resizes_without_a_grid(self):
        zone = generate_ignored_zone(
            zone_id="logo", label="Logo", bounds=NormalizedRect(x=0.1, y=0.1, width=0.1, height=0.1)
        )
        resized = resize_zone(zone, bounds=NormalizedRect(x=0.2, y=0.2, width=0.3, height=0.3))
        assert resized.bounds.width == pytest.approx(0.3)


class TestBuildBlankTemplate:
    def test_all_four_markers_and_the_orientation_marker_are_present(self):
        template = build_blank_template(
            name="Blank", canonical_width_px=1240, canonical_height_px=1754
        )
        assert len(template.registration_markers) == 4
        assert {marker.role for marker in template.registration_markers} == set(MarkerRole)
        assert template.orientation_marker is not None

    def test_it_starts_with_no_zones(self):
        template = build_blank_template(
            name="Blank", canonical_width_px=1240, canonical_height_px=1754
        )
        assert template.zones == ()

    def test_the_canonical_size_matches_the_reference_image(self):
        template = build_blank_template(
            name="Blank", canonical_width_px=2000, canonical_height_px=3000
        )
        assert template.page.canonical_width_px == 2000
        assert template.page.canonical_height_px == 3000

    def test_it_is_immediately_a_valid_saveable_template(self):
        # Round trips through the exact validation the file format uses.
        template = build_blank_template(
            name="Blank", canonical_width_px=1240, canonical_height_px=1754
        )
        from omr_scanner.domain.template import OmrTemplate

        restored = OmrTemplate.model_validate(template.model_dump(mode="json"))
        assert restored == template


class TestValidateTemplateForDesigner:
    def _template_with_zones(self, zones: tuple[Zone, ...]) -> OmrTemplate:
        return build_blank_template(
            name="T", canonical_width_px=1240, canonical_height_px=1754
        ).model_copy(update={"zones": zones})

    def test_a_template_with_no_zones_gets_a_warning(self):
        template = build_blank_template(
            name="T", canonical_width_px=1240, canonical_height_px=1754
        )
        report = validate_template_for_designer(template)
        assert any("no regions" in warning for warning in report.warnings)
        assert not report.errors

    def test_a_clean_template_with_zones_is_clean(self):
        zone = generate_character_grid_zone(
            zone_id="sid",
            label="Student ID",
            field_type=FieldType.NUMERIC,
            symbols=DIGITS,
            character_count=5,
            bounds=NormalizedRect(x=0.1, y=0.15, width=0.2, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        report = validate_template_for_designer(self._template_with_zones((zone,)))
        assert report.is_clean

    def test_a_region_overlapping_a_marker_is_a_warning(self):
        template = build_blank_template(
            name="T", canonical_width_px=1240, canonical_height_px=1754
        )
        marker = template.registration_markers[0]
        overlap = generate_ignored_zone(
            zone_id="overlap",
            label="Overlap",
            bounds=NormalizedRect(
                x=marker.center.x - 0.01, y=marker.center.y - 0.01, width=0.05, height=0.05
            ),
        )
        report = validate_template_for_designer(
            template.model_copy(update={"zones": (overlap,)})
        )
        assert any("overlaps the" in warning and "marker" in warning for warning in report.warnings)

    def test_two_overlapping_regions_are_warned_about(self):
        first = generate_ignored_zone(
            zone_id="a", label="A", bounds=NormalizedRect(x=0.3, y=0.3, width=0.2, height=0.2)
        )
        second = generate_ignored_zone(
            zone_id="b", label="B", bounds=NormalizedRect(x=0.35, y=0.35, width=0.2, height=0.2)
        )
        report = validate_template_for_designer(self._template_with_zones((first, second)))
        assert any("overlaps region" in warning for warning in report.warnings)

    def test_non_overlapping_regions_are_not_warned_about(self):
        first = generate_ignored_zone(
            zone_id="a", label="A", bounds=NormalizedRect(x=0.1, y=0.1, width=0.1, height=0.1)
        )
        second = generate_ignored_zone(
            zone_id="b", label="B", bounds=NormalizedRect(x=0.5, y=0.5, width=0.1, height=0.1)
        )
        report = validate_template_for_designer(self._template_with_zones((first, second)))
        assert not any("overlaps region" in warning for warning in report.warnings)

    def test_duplicate_question_numbers_are_an_error(self):
        first = generate_question_columns(
            id_prefix="a",
            label_prefix="Q",
            first_question=1,
            question_count=10,
            answer_labels=LETTERS,
            columns=1,
            questions_per_column=10,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        second = generate_question_columns(
            id_prefix="b",
            label_prefix="Q",
            first_question=5,
            question_count=10,
            answer_labels=LETTERS,
            columns=1,
            questions_per_column=10,
            bounds=NormalizedRect(x=0.5, y=0.1, width=0.3, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        report = validate_template_for_designer(self._template_with_zones((*first, *second)))
        assert any("defined in both" in error for error in report.errors)

    def test_a_gap_in_question_numbers_is_a_warning(self):
        zones = generate_question_columns(
            id_prefix="a",
            label_prefix="Q",
            first_question=1,
            question_count=10,
            answer_labels=LETTERS,
            columns=1,
            questions_per_column=10,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        more = generate_question_columns(
            id_prefix="b",
            label_prefix="Q",
            first_question=16,
            question_count=5,
            answer_labels=LETTERS,
            columns=1,
            questions_per_column=5,
            bounds=NormalizedRect(x=0.5, y=0.1, width=0.3, height=0.3),
            bubble_size=NormalizedSize(width=0.02, height=0.015),
        )
        report = validate_template_for_designer(self._template_with_zones((*zones, *more)))
        assert any("not covered" in warning and "11-15" in warning for warning in report.warnings)

    def test_contiguous_question_numbers_across_zones_have_no_gap_warning(self):
        first = generate_question_columns(
            id_prefix="a",
            label_prefix="Q",
            first_question=1,
            question_count=25,
            answer_labels=LETTERS,
            columns=1,
            questions_per_column=25,
            bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.3),
            bubble_size=NormalizedSize(width=0.01, height=0.008),
        )
        second = generate_question_columns(
            id_prefix="b",
            label_prefix="Q",
            first_question=26,
            question_count=25,
            answer_labels=LETTERS,
            columns=1,
            questions_per_column=25,
            bounds=NormalizedRect(x=0.5, y=0.1, width=0.3, height=0.3),
            bubble_size=NormalizedSize(width=0.01, height=0.008),
        )
        report = validate_template_for_designer(self._template_with_zones((*first, *second)))
        assert not any("not covered" in warning for warning in report.warnings)


class TestReferenceImageField:
    """The one additive field Phase 2 adds to `OmrTemplate` itself."""

    def _template(self) -> OmrTemplate:
        return build_blank_template(
            name="T", canonical_width_px=1240, canonical_height_px=1754
        )

    def test_it_defaults_to_none(self):
        assert self._template().reference_image is None

    def test_it_round_trips_through_json(self):
        from omr_scanner.domain.template import OmrTemplate

        template = self._template().model_copy(update={"reference_image": "sheet.png"})
        restored = OmrTemplate.model_validate(template.model_dump(mode="json"))
        assert restored.reference_image == "sheet.png"

    def test_a_document_without_the_field_still_loads(self):
        # Simulates a Phase 0/1 template file written before this field existed.
        from omr_scanner.domain.template import OmrTemplate

        payload = self._template().model_dump(mode="json")
        del payload["reference_image"]
        restored = OmrTemplate.model_validate(payload)
        assert restored.reference_image is None

    def test_an_unrelated_document_still_rejects_extra_fields(self):
        payload = self._template().model_dump(mode="json")
        payload["totally_unknown_field"] = "x"
        from omr_scanner.domain.template import OmrTemplate

        with pytest.raises(ValidationError):
            OmrTemplate.model_validate(payload)
