"""Unit tests for the project-level examination Set rules.

Scope:
    The pure domain layer only - :mod:`omr_scanner.domain.exam_sets` touches
    no database, so these tests do not either. Persistence is covered by
    ``tests/integration/test_project_sets.py`` and the dialog by
    ``tests/gui/test_project_config_dialog.py``.
"""

from __future__ import annotations

import pytest

from omr_scanner.domain.exam_sets import (
    MAX_EXAM_NAME_LENGTH,
    MAX_SET_CODE_LENGTH,
    ExamSet,
    find_conflicting_set,
    normalise_description,
    validate_exam_name,
    validate_set_code,
)

EXAMPLE_EXAM_NAME = "Recruitment Exam, Bangladesh Submarine Cable Regulatory Authority"


class TestExamName:
    def test_a_realistic_title_is_accepted_unchanged(self) -> None:
        assert validate_exam_name(EXAMPLE_EXAM_NAME) == EXAMPLE_EXAM_NAME

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert validate_exam_name("  Annual Exam 2026\t") == "Annual Exam 2026"

    def test_a_blank_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            validate_exam_name("")

    def test_a_whitespace_only_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            validate_exam_name("   \t  ")

    def test_an_over_long_name_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="characters or fewer"):
            validate_exam_name("x" * (MAX_EXAM_NAME_LENGTH + 1))

    def test_characters_a_folder_name_could_not_hold_are_allowed(self) -> None:
        """An exam title is not a path, so the folder-name rules must not apply.

        `ProjectMetadata.name` rejects these characters because it doubles as
        a directory name. An examination title legitimately contains them -
        "Exam: Round 2 (Written/Viva)" is an ordinary title - and rejecting
        it here would be the two concepts being conflated again.
        """
        title = 'Exam: Round 2 (Written/Viva) | "Final"'
        assert validate_exam_name(title) == title


class TestSetCode:
    @pytest.mark.parametrize("code", ["1", "2", "10", "11", "A", "B", "EEE-01"])
    def test_the_documented_code_shapes_are_accepted(self, code: str) -> None:
        assert validate_set_code(code) == code

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert validate_set_code("  10 ") == "10"

    def test_a_blank_code_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            validate_set_code("")

    def test_a_whitespace_only_code_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be blank"):
            validate_set_code("   ")

    def test_an_internal_space_is_allowed(self) -> None:
        """Rejected characters are the ones that break an exact match, not spaces."""
        assert validate_set_code("SET 10") == "SET 10"

    def test_a_trailing_line_break_is_trimmed_rather_than_rejected(self) -> None:
        """Pasting a cell out of a spreadsheet brings the line ending with it.

        That is exactly the "accidental surrounding whitespace" the brief
        asks to trim, so it is trimmed - it is only a line break *inside* the
        code that cannot be interpreted.
        """
        assert validate_set_code("10\n") == "10"

    def test_an_embedded_line_break_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tabs or line breaks"):
            validate_set_code("10\n11")

    def test_an_embedded_tab_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tabs or line breaks"):
            validate_set_code("1\t0")

    def test_an_over_long_code_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="characters or fewer"):
            validate_set_code("x" * (MAX_SET_CODE_LENGTH + 1))

    def test_the_length_limit_matches_the_existing_set_code_columns(self) -> None:
        """A code defined here must always fit `answer_key_revision.set_code`.

        That column is `String(32)`. If this limit ever drifted above it, a
        perfectly valid project-level set would become unstorable by the
        phase that links the two.
        """
        from omr_scanner.database.models import AnswerKeyRevision

        column = AnswerKeyRevision.__table__.columns["set_code"]
        assert column.type.length == MAX_SET_CODE_LENGTH


class TestDescription:
    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert normalise_description("  Name of Post: AE (Electrical) ") == (
            "Name of Post: AE (Electrical)"
        )

    def test_a_long_description_is_left_alone(self) -> None:
        long_text = "Name of Post: " + ("Assistant Engineer " * 40)
        assert normalise_description(long_text) == long_text.strip()

    def test_an_empty_description_is_allowed(self) -> None:
        assert normalise_description("   ") == ""


class TestConflictDetection:
    def _set(self, set_id: str, code: str) -> ExamSet:
        return ExamSet(set_id=set_id, code=code, description="")

    def test_an_unused_code_has_no_conflict(self) -> None:
        existing = (self._set("a", "10"), self._set("b", "11"))
        assert find_conflicting_set("12", existing) is None

    def test_a_used_code_reports_the_set_using_it(self) -> None:
        existing = (self._set("a", "10"), self._set("b", "11"))
        conflict = find_conflicting_set("11", existing)
        assert conflict is not None
        assert conflict.set_id == "b"

    def test_a_set_does_not_conflict_with_itself_when_being_edited(self) -> None:
        existing = (self._set("a", "10"), self._set("b", "11"))
        assert find_conflicting_set("11", existing, ignoring="b") is None

    def test_comparison_is_case_sensitive(self) -> None:
        """`a` and `A` are different codes everywhere else in the application."""
        existing = (self._set("a", "A"),)
        assert find_conflicting_set("a", existing) is None


class TestExamSetValue:
    def test_display_label_reads_as_a_person_would_say_it(self) -> None:
        item = ExamSet(set_id="x", code="10", description="Electrical")
        assert item.display_label == "Set 10"

    def test_it_is_immutable(self) -> None:
        item = ExamSet(set_id="x", code="10")
        with pytest.raises(AttributeError):
            item.code = "11"  # type: ignore[misc]
