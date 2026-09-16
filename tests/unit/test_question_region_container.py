"""The Question Region container invariant, and the bubble-size operations.

What these lock down:
    A Question Region is stored as several sibling zones, one per printed column
    (``docs/TEMPLATE_FORMAT.md``). The rectangle the user drew is therefore not
    any single zone's ``bounds`` - it is the union of them - and before this pass
    nothing guaranteed that union matched what was drawn. Changing the column
    count from the dialog's default of 4 to 1 shrank the region to a quarter of
    its width. See ``docs/development/template_gui_fix_diagnosis.md`` §1.

    These tests assert the invariant directly: the union of the generated zones
    reproduces the container for every internal layout parameter the designer
    exposes.
"""

from __future__ import annotations

import itertools

import pytest

from omr_scanner.domain.geometry import NormalizedRect, NormalizedSize
from omr_scanner.domain.template import (
    BubbleGrid,
    BubbleOverride,
    NormalizedPoint,
    OmrTemplate,
    Zone,
)
from omr_scanner.domain.template_authoring import (
    ColumnLayoutMode,
    apply_default_bubble_radius,
    build_blank_template,
    generate_question_columns,
    place_grid_in_bounds,
    set_zone_bubble_size,
    zone_inherits_bubble_size,
)

LABELS = ("A", "B", "C", "D")

# The brief's exact numbers: x=100, y=200, w=800, h=1000 on a 2000x2500 page.
CONTAINER = NormalizedRect(x=0.05, y=0.08, width=0.4, height=0.4)
BUBBLE = NormalizedSize(width=0.01, height=0.008)

TOLERANCE = 1e-9
"""Floating-point tolerance for the container invariant. The strips are computed
by dividing the container and adding the pieces back up, so the sum differs from
the original by at most a few ULPs - and by nothing that could ever be visible."""


def _outer_rect(zones: tuple[Zone, ...]) -> tuple[float, float, float, float]:
    """The union of ``zones``' bounds - the Question Region's outer rectangle."""
    left = min(zone.bounds.x for zone in zones)
    top = min(zone.bounds.y for zone in zones)
    right = max(zone.bounds.right for zone in zones)
    bottom = max(zone.bounds.bottom for zone in zones)
    return (left, top, right - left, bottom - top)


def _generate(
    *,
    columns: int,
    bounds: NormalizedRect = CONTAINER,
    bubble_size: NormalizedSize = BUBBLE,
    column_gap: float = 0.01,
    question_count: int = 100,
    answer_labels: tuple[str, ...] = LABELS,
) -> tuple[Zone, ...]:
    return generate_question_columns(
        id_prefix="q",
        label_prefix="Questions",
        first_question=1,
        question_count=question_count,
        answer_labels=answer_labels,
        columns=columns,
        questions_per_column=-(-question_count // columns),
        bounds=bounds,
        bubble_size=bubble_size,
        column_gap=column_gap,
    )


class TestContainerGeometryIsInvariant:
    """The outer rectangle never moves or resizes, whatever changes inside it."""

    @pytest.mark.parametrize("columns", [1, 2, 3, 4, 5, 8, 10])
    def test_the_outer_rectangle_matches_the_container_for_any_column_count(
        self, columns: int
    ):
        x, y, width, height = _outer_rect(_generate(columns=columns))
        assert x == pytest.approx(CONTAINER.x, abs=TOLERANCE)
        assert y == pytest.approx(CONTAINER.y, abs=TOLERANCE)
        assert width == pytest.approx(CONTAINER.width, abs=TOLERANCE)
        assert height == pytest.approx(CONTAINER.height, abs=TOLERANCE)

    @pytest.mark.parametrize(
        ("before", "after"), [(4, 1), (1, 5), (5, 2), (2, 4)]
    )
    def test_changing_the_column_count_leaves_the_outer_rectangle_untouched(
        self, before: int, after: int
    ):
        """The brief's exact sequence: 4->1, 1->5, 5->2, 2->4."""
        original = _outer_rect(_generate(columns=before))
        changed = _outer_rect(_generate(columns=after))
        assert changed == pytest.approx(original, abs=TOLERANCE)

    @pytest.mark.parametrize(
        ("before", "after"), [(4, 1), (1, 5), (5, 2), (2, 4)]
    )
    def test_changing_the_column_count_does_regenerate_the_internal_layout(
        self, before: int, after: int
    ):
        """The other half of the requirement: the *inside* really does change.

        A fix that froze the whole region - including its contents - would pass
        every invariance test above and be useless.
        """
        original = _generate(columns=before)
        changed = _generate(columns=after)
        assert len(changed) == after
        assert [zone.bounds.width for zone in changed] != [
            zone.bounds.width for zone in original
        ]
        assert [zone.grid.origin.x for zone in changed if zone.grid] != [
            zone.grid.origin.x for zone in original if zone.grid
        ]

    def test_every_question_is_still_covered_exactly_once_after_a_reflow(self):
        for columns in (1, 2, 4, 5):
            zones = _generate(columns=columns)
            numbers = [
                number
                for zone in zones
                for number in range(zone.field.first_question, zone.field.last_question + 1)
            ]
            assert sorted(numbers) == list(range(1, 101))

    @pytest.mark.parametrize("gap", [0.0, 0.005, 0.02, 0.05])
    def test_changing_the_column_gap_leaves_the_outer_rectangle_untouched(self, gap: float):
        x, y, width, height = _outer_rect(_generate(columns=4, column_gap=gap))
        assert (x, y, width, height) == pytest.approx(
            (CONTAINER.x, CONTAINER.y, CONTAINER.width, CONTAINER.height), abs=TOLERANCE
        )

    @pytest.mark.parametrize("radius", [0.004, 0.008, 0.012])
    def test_changing_the_bubble_size_leaves_the_outer_rectangle_untouched(
        self, radius: float
    ):
        bubble = NormalizedSize(width=2 * radius, height=2 * radius)
        x, y, width, height = _outer_rect(_generate(columns=4, bubble_size=bubble))
        assert (x, y, width, height) == pytest.approx(
            (CONTAINER.x, CONTAINER.y, CONTAINER.width, CONTAINER.height), abs=TOLERANCE
        )

    @pytest.mark.parametrize("choices", [2, 3, 4, 5])
    def test_changing_the_choice_count_leaves_the_outer_rectangle_untouched(
        self, choices: int
    ):
        labels = LABELS[:choices] if choices <= 4 else ("A", "B", "C", "D", "E")
        x, y, width, height = _outer_rect(_generate(columns=4, answer_labels=labels))
        assert (x, y, width, height) == pytest.approx(
            (CONTAINER.x, CONTAINER.y, CONTAINER.width, CONTAINER.height), abs=TOLERANCE
        )

    @pytest.mark.parametrize("count", [20, 60, 100, 200])
    def test_changing_the_question_count_leaves_the_outer_rectangle_untouched(
        self, count: int
    ):
        x, y, width, height = _outer_rect(_generate(columns=4, question_count=count))
        assert (x, y, width, height) == pytest.approx(
            (CONTAINER.x, CONTAINER.y, CONTAINER.width, CONTAINER.height), abs=TOLERANCE
        )

    def test_the_strips_tile_the_container_with_exactly_the_requested_gap(self):
        gap = 0.02
        zones = _generate(columns=4, column_gap=gap)
        for first, second in itertools.pairwise(zones):
            assert second.bounds.x - first.bounds.right == pytest.approx(gap, abs=TOLERANCE)


class TestFromPitchModeStillGrowsDeliberately:
    """The array gesture keeps the opposite behaviour, and says so explicitly."""

    def test_from_pitch_derives_the_strip_size_and_ignores_the_container_width(self):
        wide = _outer_rect(
            generate_question_columns(
                id_prefix="q", label_prefix="Q", first_question=1, question_count=40,
                answer_labels=LABELS, columns=2, questions_per_column=20,
                bounds=NormalizedRect(x=0.05, y=0.5, width=0.9, height=0.4),
                bubble_size=BUBBLE, row_pitch=0.019, column_pitch=0.03, column_gap=0.02,
                layout_mode=ColumnLayoutMode.FROM_PITCH,
            )
        )
        assert wide[2] < 0.9  # sized by the pitch, not by the rectangle

    def test_from_pitch_without_a_pitch_is_rejected_rather_than_guessed(self):
        with pytest.raises(ValueError, match="FROM_PITCH"):
            generate_question_columns(
                id_prefix="q", label_prefix="Q", first_question=1, question_count=20,
                answer_labels=LABELS, columns=1, questions_per_column=20,
                bounds=CONTAINER, bubble_size=BUBBLE,
                layout_mode=ColumnLayoutMode.FROM_PITCH,
            )


class TestPlaceGridInBounds:
    def test_an_auto_fitted_pitch_fed_back_in_reproduces_the_auto_fitted_grid(self):
        """The two placement functions must agree where they overlap.

        If they did not, the dialog's "fit spacing to the region" checkbox would
        shift the layout the moment it was toggled off and straight back on.
        """
        from omr_scanner.domain.template_authoring import fit_grid_to_bounds

        fitted = fit_grid_to_bounds(
            bounds=CONTAINER, rows=25, columns=4, bubble_size=BUBBLE
        )
        placed = place_grid_in_bounds(
            bounds=CONTAINER,
            rows=25,
            columns=4,
            bubble_size=BUBBLE,
            row_pitch=fitted.row_pitch,
            column_pitch=fitted.column_pitch,
        )
        assert placed == fitted

    def test_a_single_column_axis_is_centred_exactly_as_auto_fit_centres_it(self):
        from omr_scanner.domain.template_authoring import fit_grid_to_bounds

        fitted = fit_grid_to_bounds(bounds=CONTAINER, rows=10, columns=1, bubble_size=BUBBLE)
        placed = place_grid_in_bounds(
            bounds=CONTAINER, rows=10, columns=1, bubble_size=BUBBLE,
            row_pitch=fitted.row_pitch, column_pitch=0.0,
        )
        assert placed.origin.x == pytest.approx(CONTAINER.center.x)
        assert placed.origin.x == pytest.approx(fitted.origin.x)

    def test_a_pitch_too_wide_for_the_region_is_rejected_with_a_readable_message(self):
        with pytest.raises(ValueError, match="wider than"):
            place_grid_in_bounds(
                bounds=CONTAINER, rows=4, columns=10, bubble_size=BUBBLE,
                row_pitch=0.01, column_pitch=0.2,
            )

    def test_a_pitch_too_tall_for_the_region_is_rejected_with_a_readable_message(self):
        with pytest.raises(ValueError, match="taller than"):
            place_grid_in_bounds(
                bounds=CONTAINER, rows=40, columns=4, bubble_size=BUBBLE,
                row_pitch=0.05, column_pitch=0.01,
            )


class TestSetZoneBubbleSize:
    """Radius changes move nothing: not a centre, not the region."""

    def _zone(self) -> Zone:
        return _generate(columns=1, question_count=20)[0]

    @pytest.mark.parametrize("radius", [0.004, 0.008, 0.012])
    def test_the_diameter_is_twice_the_radius(self, radius: float):
        zone = set_zone_bubble_size(
            self._zone(),
            bubble_size=NormalizedSize(width=2 * radius, height=2 * radius),
        )
        assert zone.grid is not None
        assert zone.grid.bubble_size.width == pytest.approx(2 * radius)
        assert zone.grid.bubble_size.height == pytest.approx(2 * radius)

    def test_every_bubble_centre_is_unchanged(self):
        before = self._zone()
        assert before.grid is not None
        after = set_zone_bubble_size(
            before, bubble_size=NormalizedSize(width=0.02, height=0.02)
        )
        assert after.grid is not None
        for row in range(before.field.rows):
            for column in range(before.field.columns):
                assert after.grid.bubble_center(row, column) == before.grid.bubble_center(
                    row, column
                )

    def test_the_region_rectangle_is_unchanged(self):
        before = self._zone()
        after = set_zone_bubble_size(
            before, bubble_size=NormalizedSize(width=0.02, height=0.02)
        )
        assert after.bounds == before.bounds

    def test_the_pitch_is_unchanged(self):
        before = self._zone()
        assert before.grid is not None
        after = set_zone_bubble_size(
            before, bubble_size=NormalizedSize(width=0.003, height=0.003)
        )
        assert after.grid is not None
        assert after.grid.row_pitch == before.grid.row_pitch
        assert after.grid.column_pitch == before.grid.column_pitch
        assert after.grid.origin == before.grid.origin

    def test_hand_placed_overrides_survive_a_radius_change(self):
        before = self._zone()
        assert before.grid is not None
        override_grid = BubbleGrid(
            origin=before.grid.origin,
            row_pitch=before.grid.row_pitch,
            column_pitch=before.grid.column_pitch,
            bubble_size=before.grid.bubble_size,
            overrides=(
                # A cell the user fine-tuned: resizing the bubbles must not
                # discard the position they chose (unlike `resize_zone`, which
                # drops overrides because the pitch they deviated from changed).
                BubbleOverride(row=2, column=1, center=NormalizedPoint(x=0.1, y=0.2)),
            ),
        )
        zone = before.model_copy(update={"grid": override_grid})
        after = set_zone_bubble_size(
            zone, bubble_size=NormalizedSize(width=0.02, height=0.02)
        )
        assert after.grid is not None
        assert after.grid.overrides == override_grid.overrides

    def test_a_zone_without_a_grid_is_returned_unchanged(self):
        from omr_scanner.domain.template_authoring import generate_ignored_zone

        zone = generate_ignored_zone(zone_id="logo", label="Logo", bounds=CONTAINER)
        assert set_zone_bubble_size(
            zone, bubble_size=NormalizedSize(width=0.02, height=0.02)
        ) == zone


class TestTemplateDefaultBubbleRadius:
    def _template(self) -> OmrTemplate:
        template = build_blank_template(
            name="T", canonical_width_px=2000, canonical_height_px=2500
        )
        return template.model_copy(update={"zones": _generate(columns=4)})

    def test_the_default_size_is_twice_the_radius_on_the_horizontal_axis(self):
        template = build_blank_template(
            name="T", canonical_width_px=2000, canonical_height_px=2500
        ).model_copy(update={"default_bubble_radius": 0.01})
        assert template.default_bubble_size.width == pytest.approx(0.02)

    def test_a_pixel_circular_bubble_stays_circular_in_pixels(self):
        width, height = 2000, 2500
        template = build_blank_template(
            name="T", canonical_width_px=width, canonical_height_px=height
        ).model_copy(update={"default_bubble_radius": 12.0 / width})
        size = template.default_bubble_size
        assert size.width * width == pytest.approx(24.0)
        assert size.height * height == pytest.approx(24.0)

    def test_a_document_without_the_field_falls_back_to_the_documented_default(self):
        from omr_scanner.domain.template_authoring import DEFAULT_BUBBLE_RADIUS

        template = build_blank_template(
            name="T", canonical_width_px=2000, canonical_height_px=2500
        ).model_copy(update={"default_bubble_radius": None})
        assert template.default_bubble_size.width == pytest.approx(2 * DEFAULT_BUBBLE_RADIUS)

    def test_applying_a_radius_resizes_every_inheriting_region(self):
        template = self._template()
        template = apply_default_bubble_radius(template, radius=0.005)
        for zone in template.zones:
            assert zone.grid is not None
            assert zone.grid.bubble_size == template.default_bubble_size

    def test_applying_a_radius_moves_no_region_and_no_bubble_centre(self):
        template = self._template()
        before_bounds = [zone.bounds for zone in template.zones]
        before_centres = [
            zone.grid.bubble_center(0, 0) for zone in template.zones if zone.grid
        ]
        updated = apply_default_bubble_radius(template, radius=0.005)
        assert [zone.bounds for zone in updated.zones] == before_bounds
        assert [
            zone.grid.bubble_center(0, 0) for zone in updated.zones if zone.grid
        ] == before_centres

    def test_a_region_with_its_own_radius_is_left_alone(self):
        template = self._template()
        template = apply_default_bubble_radius(template, radius=0.005)
        overridden = set_zone_bubble_size(
            template.zones[1], bubble_size=NormalizedSize(width=0.03, height=0.024)
        )
        template = template.model_copy(
            update={"zones": (template.zones[0], overridden, *template.zones[2:])}
        )

        updated = apply_default_bubble_radius(template, radius=0.009)

        assert updated.zones[1].grid is not None
        assert updated.zones[1].grid.bubble_size == overridden.grid.bubble_size
        assert updated.zones[0].grid is not None
        assert updated.zones[0].grid.bubble_size == updated.default_bubble_size

    def test_inheritance_is_decided_by_the_stored_size_not_by_a_flag(self):
        template = apply_default_bubble_radius(self._template(), radius=0.005)
        default = template.default_bubble_size
        assert all(zone_inherits_bubble_size(zone, default=default) for zone in template.zones)

        detached = set_zone_bubble_size(
            template.zones[0], bubble_size=NormalizedSize(width=0.03, height=0.024)
        )
        assert not zone_inherits_bubble_size(detached, default=default)

    def test_a_non_positive_radius_is_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            apply_default_bubble_radius(self._template(), radius=0.0)
