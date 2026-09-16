"""Tests for `DesignerState` - the designer's mutation layer over `OmrTemplate`.

Pure Python, no Qt: every mutation the canvas or a dialog eventually triggers
is exercised here directly, so a signal-wiring bug in the widget layer can
never hide a mutation bug underneath it.
"""

from __future__ import annotations

import pytest

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect, NormalizedSize
from omr_scanner.domain.template import (
    FieldType,
    MarkerRole,
    OmrTemplate,
    OrientationMarker,
    RegistrationMarker,
    Zone,
)
from omr_scanner.domain.template_authoring import (
    build_blank_template,
    generate_character_grid_zone,
    generate_ignored_zone,
    generate_question_columns,
)
from omr_scanner.gui.template_designer.state import (
    MANUAL_CONFIRMED,
    MISSING,
    DesignerState,
    DetectionMethod,
    MarkerStatus,
)

DIGITS = tuple(str(digit) for digit in range(10))


def _template() -> OmrTemplate:
    return build_blank_template(name="T", canonical_width_px=1240, canonical_height_px=1754)


def _zone(zone_id: str = "sid", x: float = 0.1, y: float = 0.1) -> Zone:
    return generate_character_grid_zone(
        zone_id=zone_id,
        label="Student ID",
        field_type=FieldType.NUMERIC,
        symbols=DIGITS,
        character_count=5,
        bounds=NormalizedRect(x=x, y=y, width=0.2, height=0.3),
        bubble_size=NormalizedSize(width=0.02, height=0.015),
    )


class TestDirtyTracking:
    def test_a_freshly_constructed_state_is_not_dirty(self):
        state = DesignerState(_template())
        assert state.is_dirty is False

    def test_a_mutation_marks_the_document_dirty(self):
        state = DesignerState(_template())
        state.add_zones((_zone(),))
        assert state.is_dirty is True

    def test_mark_saved_clears_the_dirty_flag(self, tmp_path):
        state = DesignerState(_template())
        state.add_zones((_zone(),))
        state.mark_saved(tmp_path / "t.omrt")
        assert state.is_dirty is False
        assert state.template_path == tmp_path / "t.omrt"

    def test_undoing_back_to_the_saved_point_clears_dirty(self, tmp_path):
        state = DesignerState(_template())
        state.mark_saved(tmp_path / "t.omrt")
        state.add_zones((_zone(),))
        assert state.is_dirty is True
        state.undo()
        assert state.is_dirty is False


class TestZoneMutations:
    def test_add_zones_appends_without_disturbing_existing_ones(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a", x=0.1),))
        state.add_zones((_zone("b", x=0.5),))
        assert [zone.id for zone in state.template.zones] == ["a", "b"]

    def test_replace_zone_swaps_one_zone_in_place(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a"),))
        replacement = state.template.zone_by_id("a").model_copy(update={"label": "Renamed"})
        state.replace_zone("a", replacement)
        assert state.template.zone_by_id("a").label == "Renamed"

    def test_replace_zone_with_an_unknown_id_raises(self):
        state = DesignerState(_template())
        with pytest.raises(KeyError):
            state.replace_zone("missing", _zone("missing"))

    def test_remove_zone_deletes_it(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a"),))
        state.remove_zone("a")
        assert state.template.zone_by_id("a") is None

    def test_removing_an_unknown_zone_is_a_silent_no_op(self):
        state = DesignerState(_template())
        state.remove_zone("does_not_exist")  # must not raise
        assert state.template.zones == ()

    def test_move_zone_shifts_bounds_and_grid_together(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a", x=0.1, y=0.1),))
        before_center = state.template.zone_by_id("a").grid.bubble_center(0, 0)
        state.move_zone("a", dx=0.1, dy=0.05)
        zone = state.template.zone_by_id("a")
        assert zone.bounds.x == pytest.approx(0.2)
        after_center = zone.grid.bubble_center(0, 0)
        assert after_center.x == pytest.approx(before_center.x + 0.1)

    def test_move_zone_with_an_unknown_id_raises(self):
        state = DesignerState(_template())
        with pytest.raises(KeyError):
            state.move_zone("missing", dx=0.1, dy=0.1)

    def test_resize_zone_changes_bounds_and_refits_the_grid(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a"),))
        new_bounds = NormalizedRect(x=0.1, y=0.1, width=0.4, height=0.3)
        state.resize_zone("a", bounds=new_bounds)
        zone = state.template.zone_by_id("a")
        assert zone.bounds.width == pytest.approx(0.4)
        assert zone.bubble_count == 50  # unchanged: still 10 symbols x 5 characters

    def test_duplicate_zone_creates_a_second_zone_offset_from_the_first(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a", x=0.1, y=0.1),))
        copy = state.duplicate_zone("a", new_id="a_copy")
        assert copy.id == "a_copy"
        assert [zone.id for zone in state.template.zones] == ["a", "a_copy"]
        assert copy.bounds.x != state.template.zone_by_id("a").bounds.x

    def test_duplicate_zone_with_an_existing_new_id_is_rejected(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a"), _zone("b", x=0.5)))
        with pytest.raises(ValueError, match="already in use"):
            state.duplicate_zone("a", new_id="b")

    def test_duplicate_zone_with_an_unknown_source_id_raises(self):
        state = DesignerState(_template())
        with pytest.raises(KeyError):
            state.duplicate_zone("missing", new_id="new")

    def test_apply_zones_replaces_the_whole_tuple_in_one_history_entry(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a", x=0.1), _zone("b", x=0.5)))
        renamed_a = state.template.zone_by_id("a").model_copy(update={"label": "A"})
        renamed_b = state.template.zone_by_id("b").model_copy(update={"label": "B"})
        state.apply_zones((renamed_a, renamed_b))
        assert state.template.zone_by_id("a").label == "A"
        assert state.template.zone_by_id("b").label == "B"
        state.undo()
        # One undo reverts BOTH renames together - proof this was one entry,
        # not two - which is exactly why Create Column Array and Distribute
        # Columns Evenly use this instead of `replace_zone` in a loop.
        assert state.template.zone_by_id("a").label != "A"
        assert state.template.zone_by_id("b").label != "B"

    def test_moving_one_question_column_leaves_its_siblings_untouched(self):
        state = DesignerState(_template())
        zones = generate_question_columns(
            id_prefix="q", label_prefix="Questions", first_question=1, question_count=60,
            answer_labels=("A", "B", "C", "D"), columns=3, questions_per_column=20,
            bounds=NormalizedRect(x=0.05, y=0.5, width=0.9, height=0.3),
            bubble_size=NormalizedSize(width=0.01, height=0.01),
        )
        state.add_zones(zones)
        before_sibling_bounds = state.template.zone_by_id("q_2").bounds
        state.move_zone("q_1", dx=0.017, dy=-0.003)
        assert state.template.zone_by_id("q_0").bounds == zones[0].bounds
        assert state.template.zone_by_id("q_2").bounds == before_sibling_bounds
        moved = state.template.zone_by_id("q_1")
        assert moved.field.first_question == 21
        assert moved.field.last_question == 40


class TestBubbleOverrides:
    def test_setting_an_override_adds_exactly_one_entry(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a"),))
        state.set_bubble_override("a", row=0, column=0, center_x=0.5, center_y=0.5)
        zone = state.template.zone_by_id("a")
        assert len(zone.grid.overrides) == 1
        assert zone.grid.bubble_center(0, 0) == NormalizedPoint(x=0.5, y=0.5)

    def test_setting_an_override_twice_for_the_same_cell_replaces_it(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a"),))
        state.set_bubble_override("a", row=1, column=1, center_x=0.3, center_y=0.3)
        state.set_bubble_override("a", row=1, column=1, center_x=0.6, center_y=0.6)
        zone = state.template.zone_by_id("a")
        assert len(zone.grid.overrides) == 1
        assert zone.grid.bubble_center(1, 1) == NormalizedPoint(x=0.6, y=0.6)

    def test_clearing_an_override_reverts_to_the_computed_position(self):
        state = DesignerState(_template())
        state.add_zones((_zone("a"),))
        computed = state.template.zone_by_id("a").grid.bubble_center(2, 2)
        state.set_bubble_override("a", row=2, column=2, center_x=0.9, center_y=0.9)
        state.clear_bubble_override("a", row=2, column=2)
        zone = state.template.zone_by_id("a")
        assert zone.grid.overrides == ()
        assert zone.grid.bubble_center(2, 2) == computed

    def test_an_override_on_a_zone_without_a_grid_raises(self):
        ignored = generate_ignored_zone(
            zone_id="x", label="X", bounds=NormalizedRect(x=0.1, y=0.1, width=0.1, height=0.1)
        )
        state = DesignerState(_template())
        state.add_zones((ignored,))
        with pytest.raises(KeyError):
            state.set_bubble_override("x", row=0, column=0, center_x=0.5, center_y=0.5)


class TestMarkers:
    def test_set_marker_replaces_only_the_named_role(self):
        state = DesignerState(_template())
        original_others = {
            role: state.template.marker_by_role(role)
            for role in MarkerRole
            if role is not MarkerRole.TOP_LEFT
        }
        new_marker = RegistrationMarker(
            role=MarkerRole.TOP_LEFT,
            center=NormalizedPoint(x=0.2, y=0.2),
            size=NormalizedSize(width=0.03, height=0.02),
        )
        state.set_marker(MarkerRole.TOP_LEFT, new_marker, status=MANUAL_CONFIRMED)

        assert state.template.marker_by_role(MarkerRole.TOP_LEFT).center.x == pytest.approx(0.2)
        for role, marker in original_others.items():
            assert state.template.marker_by_role(role) == marker

    def test_set_marker_records_the_given_status(self):
        state = DesignerState(_template())
        status = MarkerStatus(confirmed=False, method=DetectionMethod.AUTO, confidence=0.8)
        marker = RegistrationMarker(
            role=MarkerRole.TOP_RIGHT,
            center=NormalizedPoint(x=0.9, y=0.05),
            size=NormalizedSize(width=0.03, height=0.02),
        )
        state.set_marker(MarkerRole.TOP_RIGHT, marker, status=status)
        assert state.marker_status[MarkerRole.TOP_RIGHT] == status

    def test_confirm_marker_leaves_geometry_unchanged(self):
        state = DesignerState(_template())
        state.marker_status = {**state.marker_status, MarkerRole.TOP_LEFT: MISSING}
        before = state.template.marker_by_role(MarkerRole.TOP_LEFT)
        state.confirm_marker(MarkerRole.TOP_LEFT)
        assert state.template.marker_by_role(MarkerRole.TOP_LEFT) == before
        assert state.marker_status[MarkerRole.TOP_LEFT].confirmed is True

    def test_set_orientation_marker_replaces_it(self):
        state = DesignerState(_template())
        new_marker = OrientationMarker(
            center=NormalizedPoint(x=0.2, y=0.04),
            size=NormalizedSize(width=0.06, height=0.015),
            expected_near=MarkerRole.TOP_LEFT,
        )
        state.set_orientation_marker(new_marker, status=MANUAL_CONFIRMED)
        assert state.template.orientation_marker == new_marker
        assert state.orientation_status == MANUAL_CONFIRMED


class TestLoad:
    def test_load_replaces_the_document_and_discards_history(self, tmp_path):
        state = DesignerState(_template())
        state.add_zones((_zone("a"),))

        fresh = _template()
        state.load(fresh, template_path=tmp_path / "other.omrt", reference_image_path=None)

        assert state.template == fresh
        assert state.history.can_undo is False
        assert state.is_dirty is False
        assert state.template_path == tmp_path / "other.omrt"

    def test_load_resets_marker_status(self):
        state = DesignerState(_template())
        state.marker_status = {**state.marker_status, MarkerRole.TOP_LEFT: MISSING}
        state.load(_template(), template_path=None, reference_image_path=None)
        assert all(status == MANUAL_CONFIRMED for status in state.marker_status.values())
