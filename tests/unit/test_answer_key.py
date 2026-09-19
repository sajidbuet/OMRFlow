"""Reading, validating and scanning an answer key (Phase 8).

Scope:
    :mod:`omr_scanner.services.answer_key` in isolation. No database, no GUI.

Why the messages are asserted, not just the refusals:
    A key is the standard every candidate is measured against. "Invalid key"
    tells an operator nothing; the phase brief requires the message to name the
    questions, and a test that only checked ``is_valid is False`` would let
    that regress silently.
"""

from __future__ import annotations

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.domain.scoring import BLANK, MULTIPLE, AnswerKeySource, AnswerKeyStatus
from omr_scanner.services.answer_key import (
    AnswerKeyError,
    QuestionPlan,
    key_from_scan,
    normalise_key_text,
    parse_wrong_questions,
    plan_for,
    read_key,
)


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def plan(template) -> QuestionPlan:
    return plan_for(template)


def small_plan(count: int = 4, *, first: int = 1) -> QuestionPlan:
    """A plan of ``count`` A-D questions."""
    return QuestionPlan(
        numbers=tuple(range(first, first + count)), labels=("A", "B", "C", "D")
    )


class TestReadingTheTemplate:
    def test_a_real_template_yields_its_questions(self, plan):
        assert plan.question_count > 0
        assert plan.labels == ("A", "B", "C", "D")
        assert plan.is_contiguous is True
        assert plan.first_question == 1

    def test_a_template_with_no_questions_is_refused(self, template):
        stripped = template.model_copy(
            update={
                "zones": tuple(
                    zone
                    for zone in template.zones
                    if zone.field.type.value != "question_block"
                )
            }
        )
        with pytest.raises(AnswerKeyError) as caught:
            plan_for(stripped)
        assert "no question regions" in caught.value.user_message


class TestReadingAKey:
    def test_a_valid_key_reads(self):
        draft = read_key("ABCD", small_plan(), "A")
        assert draft.is_valid is True
        assert draft.answers == "ABCD"
        assert draft.set_code == "A"

    def test_lowercase_is_normalised(self):
        assert read_key("abcd", small_plan(), "A").answers == "ABCD"

    def test_surrounding_whitespace_is_ignored(self):
        assert read_key("  ABCD  ", small_plan(), "A").is_valid is True

    def test_line_breaks_and_separators_are_ignored(self):
        draft = read_key("AB\nCD", small_plan(), "A")
        assert draft.is_valid is True
        assert draft.answers == "ABCD"
        assert "\n" in draft.ignored_characters

    def test_commas_from_a_spreadsheet_are_ignored(self):
        assert read_key("A,B,C,D", small_plan(), "A").answers == "ABCD"

    def test_a_multi_character_set_code_is_kept(self):
        assert read_key("ABCD", small_plan(), "10").set_code == "10"
        assert read_key("ABCD", small_plan(), "X1").set_code == "X1"

    def test_wrong_questions_are_recorded(self):
        draft = read_key("ABCD", small_plan(), "A", wrong_questions=[2, 4])
        assert draft.is_valid is True
        assert draft.wrong_questions == {2, 4}

    def test_the_source_is_carried_through(self):
        draft = read_key("ABCD", small_plan(), "A", source=AnswerKeySource.SCANNED)
        assert draft.to_key().source is AnswerKeySource.SCANNED

    def test_a_built_key_starts_as_a_draft(self):
        # Recognition completing, or a key being typed, is not verification.
        assert read_key("ABCD", small_plan(), "A").to_key().status is (
            AnswerKeyStatus.DRAFT
        )

    def test_numbering_offset_is_respected(self):
        draft = read_key("ABCD", small_plan(first=51), "A")
        assert draft.to_key().first_question == 51
        assert draft.to_key().answer_for(51) == "A"


class TestKeyRefusals:
    def test_a_short_key_names_the_missing_questions(self):
        draft = read_key("AB", small_plan(), "A")
        assert draft.is_valid is False
        message = draft.issues[0].message
        assert "contains 2 answer(s)" in message
        assert "contains 4 questions" in message
        assert "Questions 3-4" in message

    def test_one_missing_answer_is_named_in_the_singular(self):
        draft = read_key("ABC", small_plan(), "A")
        assert "Question 4" in draft.issues[0].message

    def test_the_brief_s_example_message(self):
        draft = read_key("A" * 98, small_plan(100), "A")
        message = draft.issues[0].message
        assert "98 answer(s)" in message
        assert "100 questions" in message
        assert "Questions 99-100" in message

    def test_a_long_key_says_what_to_remove(self):
        draft = read_key("ABCDEF", small_plan(), "A")
        message = draft.issues[0].message
        assert "only 4 questions" in message
        assert "2 extra" in message

    def test_an_unknown_character_names_its_question(self):
        draft = read_key("ABZD", small_plan(), "A")
        assert any("Question 3" in item.message for item in draft.issues)
        assert any("'Z'" in item.message for item in draft.issues)
        assert any("A/B/C/D" in item.message for item in draft.issues)

    def test_a_blank_in_a_key_is_refused(self):
        draft = read_key(f"AB{BLANK}D", small_plan(), "A")
        assert draft.is_valid is False
        assert any("no answer" in item.message for item in draft.issues)
        assert any("wrong question" in item.message for item in draft.issues)

    def test_a_multiple_in_a_key_is_refused(self):
        draft = read_key(f"AB{MULTIPLE}D", small_plan(), "A")
        assert any("multiple answer" in item.message for item in draft.issues)

    def test_a_key_with_no_set_is_refused(self):
        draft = read_key("ABCD", small_plan(), "  ")
        assert any("no question-paper set" in item.message for item in draft.issues)

    def test_every_problem_is_reported_not_just_the_first(self):
        # An operator fixing a key one message at a time gives up.
        draft = read_key("ZZZZ", small_plan(), "A")
        assert len(draft.issues) == 4

    def test_an_invalid_draft_refuses_to_become_a_key(self):
        draft = read_key("AB", small_plan(), "A")
        with pytest.raises(AnswerKeyError):
            draft.to_key()

    def test_an_out_of_range_wrong_question_is_reported(self):
        draft = read_key("ABCD", small_plan(), "A", wrong_questions=[99])
        assert draft.is_valid is False
        assert any("questions run 1-4" in item.message.lower() for item in draft.issues)

    def test_a_stray_character_is_reported_never_dropped(self):
        # Silently dropping it would shorten the key and mark every candidate
        # against the wrong questions from that point on.
        draft = read_key("AB*CD", small_plan(), "A")
        assert draft.is_valid is False
        assert "*" not in draft.ignored_characters


class TestNormalisation:
    @pytest.mark.parametrize(
        ("text", "kept"),
        [
            ("A B C D", "ABCD"),
            ("A\tB\nC\rD", "ABCD"),
            ("A,B;C|D", "ABCD"),
            ("abcd", "ABCD"),
        ],
    )
    def test_separators_are_removed_and_letters_uppercased(self, text, kept):
        assert normalise_key_text(text)[0] == kept

    def test_what_was_removed_is_reported(self):
        _, ignored = normalise_key_text("A B")
        assert ignored == " "


class TestWrongQuestionParsing:
    def test_a_comma_separated_list_reads(self):
        numbers, error = parse_wrong_questions("2, 4", small_plan())
        assert numbers == {2, 4}
        assert not error

    def test_semicolons_work_too(self):
        assert parse_wrong_questions("2; 4", small_plan())[0] == {2, 4}

    def test_an_empty_list_is_fine(self):
        assert parse_wrong_questions("", small_plan()) == (frozenset(), "")

    def test_an_unreadable_entry_is_reported_not_skipped(self):
        numbers, error = parse_wrong_questions("2, banana", small_plan())
        assert numbers == {2}
        assert "banana" in error

    def test_an_out_of_range_number_is_reported(self):
        numbers, error = parse_wrong_questions("99", small_plan())
        assert numbers == frozenset()
        assert "Questions run 1-4" in error

    def test_zero_is_out_of_range(self):
        assert parse_wrong_questions("0", small_plan())[1]

    def test_duplicates_collapse(self):
        assert parse_wrong_questions("2, 2, 2", small_plan())[0] == {2}


class TestReadingAKeyFromAScan:
    """A recognised solution sheet is a draft, never a verified key."""

    def _result(self, values: dict[int, str], *, registered: bool = True) -> object:
        from pathlib import Path

        from omr_scanner.services.recognition_models import (
            AnswerView,
            FieldView,
            RecognitionOutcome,
            RegistrationStatus,
            ScanResult,
        )

        return ScanResult(
            source_path=Path("solution.png"),
            outcome=RecognitionOutcome.COMPLETE,
            registration=(
                RegistrationStatus.REGISTERED
                if registered
                else RegistrationStatus.FAILED
            ),
            fields=(
                FieldView(
                    zone_id="set_code",
                    label="Set",
                    field_type="set_code",
                    value="B",
                    status="complete",
                    needs_review=False,
                    characters=(),
                ),
            ),
            answers=tuple(
                AnswerView(
                    number=number,
                    zone_id="q",
                    value=values.get(number, "A"),
                    status="resolved",
                    needs_review=False,
                    top_fill=0.9,
                    margin=0.4,
                    confidence=0.9,
                )
                for number in range(1, 5)
            ),
            set_code_zone_id="set_code",
        )

    def test_a_clean_sheet_reads_as_a_key(self):
        scanned = key_from_scan(self._result({1: "A", 2: "B", 3: "C", 4: "D"}), small_plan())
        assert scanned.answers == "ABCD"
        assert scanned.is_clean is True
        assert "All 4 questions" in scanned.summary

    def test_the_set_code_is_suggested(self):
        assert key_from_scan(self._result({}), small_plan()).set_code == "B"

    def test_a_blank_is_reported_not_guessed(self):
        scanned = key_from_scan(self._result({2: ""}), small_plan())
        assert scanned.answers[1] == BLANK
        assert scanned.unreadable == (2,)
        assert scanned.is_clean is False
        assert "not a single clear mark" in scanned.summary

    def test_a_double_mark_is_reported_not_guessed(self):
        scanned = key_from_scan(self._result({3: "A-C"}), small_plan())
        assert scanned.answers[2] == MULTIPLE
        assert scanned.unreadable == (3,)

    def test_a_failed_registration_is_never_clean(self):
        scanned = key_from_scan(self._result({}, registered=False), small_plan())
        assert scanned.registered is False
        assert scanned.is_clean is False
        assert "could not be registered" in scanned.summary

    def test_an_unreadable_sheet_still_produces_a_full_length_string(self):
        scanned = key_from_scan(self._result({1: "", 2: "", 3: "", 4: ""}), small_plan())
        assert len(scanned.answers) == 4
        assert scanned.unreadable == (1, 2, 3, 4)

    def test_a_scanned_key_with_blanks_cannot_be_built(self):
        scanned = key_from_scan(self._result({2: ""}), small_plan())
        draft = read_key(
            scanned.answers, small_plan(), "B", source=AnswerKeySource.SCANNED
        )
        assert draft.is_valid is False, "a blank is not an answer"
