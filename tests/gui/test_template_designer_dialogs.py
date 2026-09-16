"""GUI tests for the region-creation dialogs.

Per the project's GUI testing policy (``docs/TESTING.md``), a modal dialog's
own `exec()` is never called here (nothing would ever click it under the
offscreen platform) - every test constructs the dialog directly and drives
its internal `_build_zones()`/`_on_accept()`, exactly the behaviour `exec()`
would trigger on a real "OK" click.
"""

from __future__ import annotations

import pytest

from omr_scanner.domain.geometry import NormalizedRect, NormalizedSize
from omr_scanner.domain.template import Zone
from omr_scanner.domain.template_authoring import generate_question_columns
from omr_scanner.gui.template_designer.dialogs import (
    CreateColumnArrayDialog,
    QuestionBlockDialog,
    QuestionSetDialog,
)

pytestmark = pytest.mark.gui

BOUNDS = NormalizedRect(x=0.1, y=0.1, width=0.5, height=0.3)


class TestQuestionSetDialogEnumeratedMode:
    def test_multi_character_tokens_are_kept_whole_not_split_into_digits(self, qtbot):
        dialog = QuestionSetDialog(bounds=BOUNDS, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        dialog.symbols_edit.setText("10,11,12")
        (zone,) = dialog._build_zones()
        assert zone.field.symbols == ("10", "11", "12")
        assert zone.field.character_count == 1

    def test_a_leading_zero_is_never_coerced_to_a_number(self, qtbot):
        dialog = QuestionSetDialog(bounds=BOUNDS, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        dialog.symbols_edit.setText("01,02,03")
        (zone,) = dialog._build_zones()
        assert zone.field.symbols == ("01", "02", "03")

    def test_arbitrary_symbols_are_accepted(self, qtbot):
        dialog = QuestionSetDialog(bounds=BOUNDS, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        dialog.symbols_edit.setText("A,B,*,#")
        (zone,) = dialog._build_zones()
        assert zone.field.symbols == ("A", "B", "*", "#")

    def test_a_duplicate_token_is_rejected(self, qtbot):
        dialog = QuestionSetDialog(bounds=BOUNDS, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        dialog.symbols_edit.setText("A,B,B,D")
        with pytest.raises(ValueError, match="Duplicate"):
            dialog._build_zones()

    def test_an_empty_token_from_a_stray_comma_is_rejected(self, qtbot):
        dialog = QuestionSetDialog(bounds=BOUNDS, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        dialog.symbols_edit.setText("A,B,,D")
        with pytest.raises(ValueError, match="empty"):
            dialog._build_zones()


class TestQuestionSetDialogPositionalMode:
    def test_a_two_digit_positional_code_produces_two_character_positions(self, qtbot):
        dialog = QuestionSetDialog(bounds=BOUNDS, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        dialog.mode_box.setCurrentIndex(1)
        dialog.code_length_box.setValue(2)
        (zone,) = dialog._build_zones()
        assert zone.field.character_count == 2
        assert zone.field.symbols == tuple(str(d) for d in range(10))
        assert zone.bubble_count == 20  # 2 positions x 10 symbols each

    def test_a_three_digit_positional_code_is_supported(self, qtbot):
        dialog = QuestionSetDialog(bounds=BOUNDS, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        dialog.mode_box.setCurrentIndex(1)
        dialog.code_length_box.setValue(3)
        (zone,) = dialog._build_zones()
        assert zone.field.character_count == 3

    def test_switching_modes_toggles_which_fields_are_visible(self, qtbot):
        dialog = QuestionSetDialog(bounds=BOUNDS, existing_zone_ids=[])
        qtbot.addWidget(dialog)
        assert dialog.form.isRowVisible(dialog.symbols_edit) is True
        assert dialog.form.isRowVisible(dialog.code_length_box) is False
        dialog.mode_box.setCurrentIndex(1)
        assert dialog.form.isRowVisible(dialog.symbols_edit) is False
        assert dialog.form.isRowVisible(dialog.code_length_box) is True


class TestQuestionBlockDialogSpacingFields:
    def test_untouched_defaults_reproduce_the_original_auto_fit_geometry(self, qtbot):
        dialog = QuestionBlockDialog(
            bounds=BOUNDS, existing_zone_ids=[], image_width=2000, image_height=3000
        )
        qtbot.addWidget(dialog)
        new_zones = dialog._build_zones()

        original = generate_question_columns(
            id_prefix="questions", label_prefix="Questions", first_question=1,
            question_count=100, answer_labels=("A", "B", "C", "D"), columns=4,
            questions_per_column=25, bounds=BOUNDS,
            bubble_size=NormalizedSize(width=0.022, height=0.016), column_gap=0.01,
        )
        for new, old in zip(new_zones, original, strict=True):
            assert new.bounds.x == pytest.approx(old.bounds.x, abs=1e-4)
            assert new.bounds.width == pytest.approx(old.bounds.width, abs=1e-4)
            assert new.grid.row_pitch == pytest.approx(old.grid.row_pitch, abs=1e-4)
            assert new.grid.column_pitch == pytest.approx(old.grid.column_pitch, abs=1e-4)

    def test_editing_the_column_gap_changes_the_generated_x_positions(self, qtbot):
        dialog = QuestionBlockDialog(
            bounds=BOUNDS, existing_zone_ids=[], image_width=2000, image_height=3000
        )
        qtbot.addWidget(dialog)
        before = [zone.bounds.x for zone in dialog._build_zones()]
        dialog.column_gap_box.setValue(dialog.column_gap_box.value() + 100.0)
        after = [zone.bounds.x for zone in dialog._build_zones()]
        assert after[1:] != before[1:]
        assert after[0] == pytest.approx(before[0])  # the first column never moves

    def test_changing_a_field_emits_a_preview_with_the_same_zones_accept_would_produce(self, qtbot):
        dialog = QuestionBlockDialog(
            bounds=BOUNDS, existing_zone_ids=[], image_width=2000, image_height=3000
        )
        qtbot.addWidget(dialog)
        previews: list[tuple] = []
        dialog.preview_requested.connect(previews.append)
        dialog.column_gap_box.setValue(dialog.column_gap_box.value() + 50.0)
        assert previews  # at least the edit above fired one
        assert previews[-1] == dialog._build_zones()

    def test_the_generated_columns_belong_to_one_group(self, qtbot):
        dialog = QuestionBlockDialog(
            bounds=BOUNDS, existing_zone_ids=[], image_width=2000, image_height=3000
        )
        qtbot.addWidget(dialog)
        zones = dialog._build_zones()
        assert len({zone.field.group_id for zone in zones}) == 1


class TestCreateColumnArrayDialog:
    def _reference(self) -> Zone:
        return generate_question_columns(
            id_prefix="questions", label_prefix="Questions", first_question=1,
            question_count=20, answer_labels=("A", "B", "C", "D"), columns=1,
            questions_per_column=20, bounds=NormalizedRect(x=0.05, y=0.5, width=0.15, height=0.3),
            bubble_size=NormalizedSize(width=0.01, height=0.01),
        )[0]

    def test_default_fields_come_from_the_reference_column(self, qtbot):
        reference = self._reference()
        dialog = CreateColumnArrayDialog(
            reference=reference, existing_zone_ids=[], image_width=2000
        )
        qtbot.addWidget(dialog)
        assert dialog.per_column_box.value() == reference.field.question_count
        assert dialog.first_question_box.value() == reference.field.first_question

    def test_accepting_produces_five_columns_of_twenty_questions(self, qtbot):
        reference = self._reference()
        dialog = CreateColumnArrayDialog(
            reference=reference, existing_zone_ids=[], image_width=2000
        )
        qtbot.addWidget(dialog)
        dialog.columns_box.setValue(5)
        dialog._on_accept()
        zones = dialog.result_zones()
        assert len(zones) == 5
        assert sum(zone.bubble_count for zone in zones) == 400
        ranges = [(z.field.first_question, z.field.last_question) for z in zones]
        assert ranges == [(1, 20), (21, 40), (41, 60), (61, 80), (81, 100)]
