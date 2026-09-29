"""Tests for the synthetic examination's answer keys and solution sheets.

The property everything here protects is that there is **one** key per set,
and that every representation of it - the text file, the solution sheet's
marks, the manifest - is read off that one object rather than drawn again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation.answer_keys import (
    SOLUTION_ROLE,
    clear_stale_solutions,
    describe_solutions,
    generate_answer_keys,
    planned_set_codes,
    question_plan,
    serialise_answer_key,
    set_code_markable,
    solution_case,
    solution_file_names,
    solution_stem,
)
from omr_scanner.evaluation.case_plans import plan_dataset
from omr_scanner.evaluation.test_cases import FieldLayout
from omr_scanner.services.answer_key import read_key

if TYPE_CHECKING:
    from pathlib import Path


def keys_for(template, sets=("10", "11", "12"), seed=7):
    return generate_answer_keys(template, FieldLayout.of(template), sets, seed=seed)


class TestTheKeysAreDrawnFromTheTemplate:
    @pytest.mark.parametrize(
        ("blocks", "per_block", "labels"),
        [(1, 7, ("A", "B")), (2, 10, ("A", "B", "C", "D")), (2, 9, ("P", "Q", "R", "S", "T", "U"))],
    )
    def test_every_question_is_keyed_with_one_of_its_own_options(
        self, blocks, per_block, labels
    ):
        template = build_answer_sheet_template(
            question_blocks=blocks, questions_per_block=per_block, answer_labels=labels
        )
        key = keys_for(template, ("A",))["A"]
        assert sorted(key.answers) == list(range(1, blocks * per_block + 1))
        assert set(key.answers.values()) <= set(labels)
        assert len(key.key_string) == blocks * per_block

    def test_lowercase_labels_keep_the_templates_spelling_but_the_key_string_is_upper(self):
        template = build_answer_sheet_template(answer_labels=("a", "b", "c", "d"))
        key = keys_for(template, ("A",))["A"]
        assert set(key.answers.values()) <= {"a", "b", "c", "d"}
        assert key.key_string == key.key_string.upper()

    def test_each_set_gets_its_own_key(self):
        keys = keys_for(build_answer_sheet_template())
        strings = {key.key_string for key in keys.values()}
        assert len(strings) == 3

    def test_the_same_seed_gives_the_same_keys(self):
        template = build_answer_sheet_template()
        assert keys_for(template) == keys_for(template)

    def test_a_different_seed_gives_different_keys(self):
        template = build_answer_sheet_template()
        assert keys_for(template, seed=1)["10"] != keys_for(template, seed=2)["10"]

    def test_adding_a_set_leaves_the_others_keys_alone(self):
        """Each set has its own stream, so a fourth set moves nothing."""
        template = build_answer_sheet_template()
        three = keys_for(template, ("10", "11", "12"))
        four = keys_for(template, ("10", "11", "12", "13"))
        for code in three:
            assert three[code] == four[code]

    def test_a_template_with_no_questions_has_no_key(self):
        template = build_answer_sheet_template(question_blocks=0)
        assert question_plan(template) is None
        assert keys_for(template) == {}

    def test_multi_character_options_are_refused_early(self):
        template = build_answer_sheet_template(answer_labels=("AA", "BB"))
        with pytest.raises(ValueError, match="one character per question"):
            keys_for(template)

    def test_options_that_collide_with_reserved_symbols_are_refused(self):
        template = build_answer_sheet_template(answer_labels=("A", "_"))
        with pytest.raises(ValueError, match="Cannot generate an answer key"):
            keys_for(template)


class TestTheTextFileIsWhatTheAnswerKeyStageReads:
    @pytest.mark.parametrize("labels", [("A", "B", "C", "D"), ("a", "b", "c", "d", "e")])
    def test_it_round_trips_through_read_key(self, labels):
        template = build_answer_sheet_template(answer_labels=labels)
        for code, key in keys_for(template).items():
            text = serialise_answer_key(key)
            draft = read_key(text, key.plan, code)
            assert draft.is_valid, draft.issues
            assert draft.answers == key.key_string
            rebuilt = draft.to_key()
            assert rebuilt.set_code == code
            assert rebuilt.first_question == key.plan.first_question
            for number, label in key.answers.items():
                assert rebuilt.answer_for(number) == label.upper()

    def test_it_is_one_line_with_one_trailing_newline(self):
        key = keys_for(build_answer_sheet_template())["10"]
        text = serialise_answer_key(key)
        assert text.endswith("\n")
        assert text.count("\n") == 1
        assert text.strip() == key.key_string


class TestTheSolutionSheetIsAnOrdinaryCase:
    def test_every_answer_is_the_key_and_the_identifier_is_blank(self):
        template = build_answer_sheet_template(roll_digits=5)
        layout = FieldLayout.of(template)
        key = keys_for(template, ("B",))["B"]
        case = solution_case(key, layout, index=1)
        assert case.answers == key.answers
        assert case.roll == "_" * 5
        assert all(column == () for column in case.roll_marks)
        assert case.set_code == "B"
        assert case.expect_failure is False

    def test_it_is_clean(self):
        template = build_answer_sheet_template()
        layout = FieldLayout.of(template)
        case = solution_case(keys_for(template, ("A",))["A"], layout, index=1)
        assert case.distortion.rotation_degrees == 0.0
        assert case.distortion.noise_sigma == 0.0
        assert case.folds == ()
        assert not case.omit_markers and not case.damaged_markers

    @pytest.mark.parametrize("code", ["1", "12", "103"])
    def test_a_multi_character_symbolic_code_is_one_mark(self, code):
        template = build_answer_sheet_template(set_symbols=("1", "12", "103"))
        layout = FieldLayout.of(template)
        assert set_code_markable(layout, code)
        case = solution_case(keys_for(template, (code,))[code], layout, index=1)
        assert case.set_code == code
        assert case.set_marks == ((code,),)

    def test_a_positional_multi_digit_code_marks_each_position(self):
        template = build_answer_sheet_template(
            set_symbols=tuple("0123456789"), set_positions=3
        )
        layout = FieldLayout.of(template)
        case = solution_case(keys_for(template, ("103",))["103"], layout, index=1)
        assert case.set_code == "103"
        assert case.set_marks == (("1",), ("0",), ("3",))

    def test_a_code_the_field_cannot_spell_is_not_markable(self):
        layout = FieldLayout.of(build_answer_sheet_template())
        assert not set_code_markable(layout, "10")
        assert not set_code_markable(layout, "E")
        assert set_code_markable(layout, "C")


class TestTheFiles:
    def test_the_names_follow_the_attendance_workbooks(self):
        names = solution_file_names(["10", "11"], ".png")
        assert names["10"].sheet == "Set_10_Solution.png"
        assert names["10"].answer_key == "Set_10_Answer_Key.txt"
        assert names["11"].ground_truth == "Set_11_Solution.json"

    def test_an_unsafe_code_is_sanitised_only_in_the_file_name(self):
        assert solution_stem("A/1") == "Set_A-1"
        assert solution_stem("") == "Set_Unset"

    def test_two_codes_that_would_share_a_file_are_refused(self):
        with pytest.raises(ValueError, match="share the solution file name"):
            solution_file_names(["A/1", "A-1"], ".png")

    def test_the_manifest_block_names_every_file_and_the_real_code(self):
        template = build_answer_sheet_template()
        keys = keys_for(template, ("A", "B"))
        block = describe_solutions(
            keys, solution_file_names(list(keys), ".jpg"), FieldLayout.of(template)
        )
        assert block["role"] == SOLUTION_ROLE
        assert [entry["set_code"] for entry in block["sets"]] == ["A", "B"]
        first = block["sets"][0]
        assert first["answers"] == keys["A"].key_string
        assert first["sheet"] == "solution/Set_A_Solution.jpg"
        assert first["set_code_marked"] is True

    def test_stale_solution_files_are_removed_and_nothing_else(self, tmp_path: Path):
        folder = tmp_path / "solution"
        folder.mkdir()
        stale = [
            folder / "Set_4_Solution.png",
            folder / "Set_4_Answer_Key.txt",
            folder / "Set_4_Solution.json",
            folder / "Set_4_Solution.jpg",
        ]
        for path in stale:
            path.write_text("old", encoding="utf-8")
        keep = folder / "my_notes.txt"
        keep.write_text("mine", encoding="utf-8")
        removed = clear_stale_solutions(folder)
        assert sorted(removed) == sorted(stale)
        assert [path.name for path in folder.iterdir()] == ["my_notes.txt"]

    def test_clearing_a_missing_folder_is_harmless(self, tmp_path: Path):
        assert clear_stale_solutions(tmp_path / "absent") == ()


class TestSetsWithoutARoster:
    def test_the_sets_are_the_resolved_codes_the_sheets_carry(self):
        template = build_answer_sheet_template()
        cases = plan_dataset(template, count=40, seed=3)
        codes = planned_set_codes(cases)
        assert codes
        assert set(codes) <= {"A", "B", "C", "D"}
        assert list(codes) == sorted(codes)
