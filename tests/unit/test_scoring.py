"""The scoring arithmetic (Phase 8).

Scope:
    :mod:`omr_scanner.domain.scoring` in isolation - a pure function over value
    objects. No database, no GUI, no recognition.

Why this level carries the weight:
    These marks may end up on a transcript. The rules are a table, and a table
    is best tested as one - every case here is hand-calculated in the phase
    brief or derived from it, and asserted exactly rather than to a tolerance.

Exactness:
    Every expected value is a :class:`~fractions.Fraction`, never a float.
    ``assert score == 0.5`` would pass for a value that is not one half.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from omr_scanner.domain.scoring import (
    BLANK,
    MARK_DISPLAY_PLACES,
    MULTIPLE,
    RESERVED_SYMBOLS,
    AnswerKey,
    AnswerKeyStatus,
    NegativeMarking,
    QuestionOutcome,
    ResultStatus,
    ScoringPolicy,
    canonical_answer_string,
    format_mark,
    parse_mark,
    score_answers,
)

LABELS = ("A", "B", "C", "D")


def key(answers: str, *, wrong: tuple[int, ...] = (), first: int = 1) -> AnswerKey:
    """A verified key over ``answers``."""
    return AnswerKey(
        set_code="A",
        answers=answers,
        wrong_questions=frozenset(wrong),
        first_question=first,
        status=AnswerKeyStatus.VERIFIED,
    )


NO_NEGATIVE = ScoringPolicy(correct_mark=Fraction(1))
FIXED_QUARTER = ScoringPolicy(
    correct_mark=Fraction(1),
    incorrect_penalty=Fraction(1, 4),
    mode=NegativeMarking.FIXED,
    clamp_minimum=False,
)
ONE_PER_THREE = ScoringPolicy(
    correct_mark=Fraction(1), mode=NegativeMarking.ONE_PER_THREE, clamp_minimum=False
)
ONE_PER_FOUR = ScoringPolicy(
    correct_mark=Fraction(1), mode=NegativeMarking.ONE_PER_FOUR, clamp_minimum=False
)


# ----------------------------------------------------------------------
class TestExactArithmetic:
    """Marks are rational, and rounding happens once, for display."""

    def test_a_decimal_mark_is_read_exactly(self):
        assert parse_mark("0.25") == Fraction(1, 4)
        assert parse_mark("0.1") == Fraction(1, 10)

    def test_a_tenth_is_really_a_tenth(self):
        # Fraction(0.1) from a float is not one tenth; this is why marks are
        # read from text and never routed through float.
        assert parse_mark("0.1") + parse_mark("0.2") == parse_mark("0.3")

    def test_thirds_do_not_drift_over_a_hundred_questions(self):
        total = sum((Fraction(1, 3) for _ in range(300)), Fraction(0))
        assert total == 100

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Fraction(1), "1.00"),
            (Fraction(1, 4), "0.25"),
            (Fraction(-1, 4), "-0.25"),
            (Fraction(1, 3), "0.33"),
            (Fraction(2, 3), "0.67"),
            (Fraction(0), "0.00"),
        ],
    )
    def test_marks_format_half_up(self, value, expected):
        assert format_mark(value) == expected

    def test_negative_zero_is_normalised(self):
        # "-0.00" on a transcript is a bug report waiting to happen.
        assert format_mark(Fraction(-1, 1000)) == "0.00"

    def test_display_rounding_never_feeds_back(self):
        # A third displayed as 0.33 must not become 0.33 in the arithmetic.
        breakdown = score_answers("B" * 3 + "A" * 10, key("A" * 13), ONE_PER_THREE)
        assert breakdown.final_score == Fraction(9)
        assert format_mark(breakdown.final_score) == "9.00"
        assert MARK_DISPLAY_PLACES == 2


class TestHandCalculatedCases:
    """Exactly the cases the phase brief lists."""

    @pytest.mark.parametrize(
        ("student", "expected"),
        [("ABCD", 4), ("ABAD", 3), ("AB_D", 3), ("AB?D", 3)],
    )
    def test_no_negative_marking(self, student, expected):
        assert score_answers(student, key("ABCD"), NO_NEGATIVE).final_score == expected

    def test_fixed_quarter_deduction(self):
        # Q1 correct +1, Q2 incorrect -0.25, Q3 multiple -0.25, Q4 blank 0.
        breakdown = score_answers("AA?_", key("ABCD"), FIXED_QUARTER)
        assert breakdown.final_score == Fraction(1, 2)
        assert [item.outcome for item in breakdown.questions] == [
            QuestionOutcome.CORRECT,
            QuestionOutcome.INCORRECT,
            QuestionOutcome.MULTIPLE,
            QuestionOutcome.BLANK,
        ]
        assert [item.mark for item in breakdown.questions] == [
            Fraction(1),
            Fraction(-1, 4),
            Fraction(-1, 4),
            Fraction(0),
        ]


class TestOnePerThree:
    def test_three_wrong_costs_exactly_one_mark(self):
        breakdown = score_answers(
            "A" * 10 + "BBB" + "__", key("A" * 15), ONE_PER_THREE
        )
        assert breakdown.final_score == 9
        assert breakdown.correct_count == 10
        assert breakdown.incorrect_count == 3
        assert breakdown.blank_count == 2

    @pytest.mark.parametrize(
        ("wrong", "penalty"), [(1, Fraction(1, 3)), (2, Fraction(2, 3)), (3, Fraction(1))]
    )
    def test_fractional_penalties_are_not_truncated(self, wrong, penalty):
        # The failure this mode most invites: one or two wrong answers being
        # floored away to nothing.
        answers = "B" * wrong + "A" * (10 - wrong)
        breakdown = score_answers(answers, key("A" * 10), ONE_PER_THREE)
        assert breakdown.final_score == Fraction(10 - wrong) - penalty

    def test_six_wrong_costs_exactly_two_marks(self):
        breakdown = score_answers("B" * 6 + "A" * 10, key("A" * 16), ONE_PER_THREE)
        assert breakdown.final_score == 8

    def test_a_multiple_costs_the_same_as_a_wrong_answer(self):
        assert score_answers("?" * 3 + "A" * 10, key("A" * 13), ONE_PER_THREE).final_score == 9


class TestOnePerFour:
    def test_four_wrong_costs_exactly_one_mark(self):
        breakdown = score_answers("B" * 4 + "A" * 12, key("A" * 16), ONE_PER_FOUR)
        assert breakdown.final_score == 11

    def test_eight_wrong_costs_exactly_two_marks(self):
        assert score_answers("B" * 8 + "A" * 12, key("A" * 20), ONE_PER_FOUR).final_score == 10

    @pytest.mark.parametrize(
        ("wrong", "penalty"),
        [(1, Fraction(1, 4)), (2, Fraction(1, 2)), (3, Fraction(3, 4))],
    )
    def test_fractional_penalties_are_preserved(self, wrong, penalty):
        answers = "B" * wrong + "A" * (12 - wrong)
        breakdown = score_answers(answers, key("A" * 12), ONE_PER_FOUR)
        assert breakdown.final_score == Fraction(12 - wrong) - penalty


class TestWrongQuestions:
    """A withdrawn question is a question nobody can get wrong."""

    @pytest.mark.parametrize("response", ["B", "A", MULTIPLE, BLANK])
    def test_every_response_receives_full_credit(self, response):
        answers = "A" + response + "CD"
        breakdown = score_answers(answers, key("ABCD", wrong=(2,)), FIXED_QUARTER)
        second = breakdown.questions[1]
        assert second.outcome is QuestionOutcome.WRONG_QUESTION
        assert second.mark == Fraction(1)
        assert second.wrong_question is True

    @pytest.mark.parametrize("response", ["B", "A", MULTIPLE, BLANK])
    def test_no_deduction_is_ever_applied(self, response):
        answers = "A" + response + "CD"
        breakdown = score_answers(answers, key("ABCD", wrong=(2,)), FIXED_QUARTER)
        assert breakdown.questions[1].mark >= 0
        assert breakdown.penalised_count == 0

    def test_it_takes_precedence_over_blank_and_multiple(self):
        # The ordering an implementation that checked "blank first" gets wrong.
        breakdown = score_answers("__?_", key("ABCD", wrong=(1, 2, 3, 4)), FIXED_QUARTER)
        assert breakdown.final_score == 4
        assert breakdown.blank_count == 0
        assert breakdown.multiple_count == 0
        assert breakdown.wrong_question_count == 4

    def test_several_wrong_questions_in_one_paper(self):
        answers = "B" * 20
        breakdown = score_answers(answers, key("A" * 20, wrong=(17, 20)), NO_NEGATIVE)
        assert breakdown.wrong_question_count == 2
        assert breakdown.final_score == 2

    def test_counts_exclude_wrong_questions(self):
        breakdown = score_answers("AB", key("AB", wrong=(2,)), NO_NEGATIVE)
        assert breakdown.correct_count == 1, "Q2 is a wrong question, not a correct one"
        assert breakdown.wrong_question_count == 1


class TestBlanksAndMultiples:
    def test_a_blank_is_not_a_wrong_answer(self):
        # The distinction the brief insists on: blank != wrong.
        blank = score_answers("A_", key("AB"), FIXED_QUARTER)
        wrong = score_answers("AA", key("AB"), FIXED_QUARTER)
        assert blank.final_score == 1
        assert wrong.final_score == Fraction(3, 4)

    def test_a_blank_can_be_worth_something(self):
        policy = ScoringPolicy(correct_mark=Fraction(1), blank_mark=Fraction(1, 4))
        assert score_answers("A_", key("AB"), policy).final_score == Fraction(5, 4)

    def test_a_multiple_can_be_penalised_separately(self):
        policy = ScoringPolicy(
            correct_mark=Fraction(1),
            incorrect_penalty=Fraction(1, 4),
            multiple_penalty=Fraction(1, 2),
            mode=NegativeMarking.FIXED,
            clamp_minimum=False,
        )
        breakdown = score_answers("B?", key("AA"), policy)
        assert breakdown.questions[0].mark == Fraction(-1, 4)
        assert breakdown.questions[1].mark == Fraction(-1, 2)

    def test_a_multiple_defaults_to_the_incorrect_penalty(self):
        assert FIXED_QUARTER.effective_multiple_penalty == Fraction(1, 4)

    def test_no_negative_marking_means_no_penalty_for_either(self):
        assert NO_NEGATIVE.effective_incorrect_penalty == 0
        assert NO_NEGATIVE.effective_multiple_penalty == 0


class TestMinimumScore:
    def test_a_total_is_clamped_when_the_policy_says_so(self):
        policy = ScoringPolicy(
            correct_mark=Fraction(1),
            incorrect_penalty=Fraction(1),
            mode=NegativeMarking.FIXED,
            clamp_minimum=True,
        )
        breakdown = score_answers("BBBB", key("AAAA"), policy)
        assert breakdown.raw_score == -4
        assert breakdown.final_score == 0
        assert breakdown.clamped is True

    def test_a_negative_total_survives_when_clamping_is_off(self):
        policy = ScoringPolicy(
            correct_mark=Fraction(1),
            incorrect_penalty=Fraction(1),
            mode=NegativeMarking.FIXED,
            clamp_minimum=False,
        )
        breakdown = score_answers("BBBB", key("AAAA"), policy)
        assert breakdown.final_score == -4
        assert breakdown.clamped is False

    def test_the_floor_need_not_be_zero(self):
        policy = ScoringPolicy(
            correct_mark=Fraction(1),
            incorrect_penalty=Fraction(1),
            mode=NegativeMarking.FIXED,
            clamp_minimum=True,
            minimum_score=Fraction(-2),
        )
        assert score_answers("BBBB", key("AAAA"), policy).final_score == -2

    def test_clamping_happens_once_at_the_end(self):
        # Not per question: a mid-paper dip below the floor must be recoverable.
        policy = ScoringPolicy(
            correct_mark=Fraction(1),
            incorrect_penalty=Fraction(1),
            mode=NegativeMarking.FIXED,
            clamp_minimum=True,
        )
        breakdown = score_answers("BBAAAA", key("AAAAAA"), policy)
        assert breakdown.raw_score == 2
        assert breakdown.final_score == 2


class TestBoundaries:
    def test_one_question(self):
        assert score_answers("A", key("A"), NO_NEGATIVE).final_score == 1

    def test_a_large_paper(self):
        breakdown = score_answers("A" * 500, key("A" * 500), NO_NEGATIVE)
        assert breakdown.final_score == 500
        assert breakdown.question_count == 500

    def test_a_key_of_the_wrong_length_is_refused(self):
        with pytest.raises(ValueError, match="question"):
            score_answers("ABC", key("ABCD"), NO_NEGATIVE)

    def test_an_answer_string_of_the_wrong_length_is_refused(self):
        with pytest.raises(ValueError, match="question"):
            score_answers("ABCDE", key("ABCD"), NO_NEGATIVE)

    def test_a_zero_penalty_is_honoured(self):
        policy = ScoringPolicy(
            correct_mark=Fraction(1),
            incorrect_penalty=Fraction(0),
            mode=NegativeMarking.FIXED,
        )
        assert score_answers("B", key("A"), policy).final_score == 0

    def test_question_numbering_need_not_start_at_one(self):
        breakdown = score_answers("AB", key("AB", first=51), NO_NEGATIVE, first_question=51)
        assert [item.number for item in breakdown.questions] == [51, 52]

    def test_the_reserved_symbols_are_exactly_two(self):
        assert {BLANK, MULTIPLE} == RESERVED_SYMBOLS


class TestDeterminism:
    def test_the_same_inputs_produce_the_same_breakdown(self):
        first = score_answers("AB?_C", key("ABCDE"), FIXED_QUARTER)
        second = score_answers("AB?_C", key("ABCDE"), FIXED_QUARTER)
        assert first == second

    def test_the_total_is_the_sum_of_the_parts(self):
        breakdown = score_answers("AB?_C", key("ABCDE"), FIXED_QUARTER)
        assert breakdown.raw_score == sum(
            (item.mark for item in breakdown.questions), Fraction(0)
        )

    def test_counts_add_up_to_the_question_count(self):
        breakdown = score_answers("AB?_C", key("ABCDE", wrong=(5,)), FIXED_QUARTER)
        assert (
            breakdown.correct_count
            + breakdown.incorrect_count
            + breakdown.blank_count
            + breakdown.multiple_count
            + breakdown.wrong_question_count
            == breakdown.question_count
        )


class TestCanonicalAnswerString:
    """Question N is character N, always."""

    def test_single_answers_use_their_labels(self):
        found = canonical_answer_string({1: "A", 2: "B"}, [1, 2], LABELS)
        assert found == "AB"

    def test_a_blank_keeps_its_position(self):
        found = canonical_answer_string({1: "A", 3: "C"}, [1, 2, 3], LABELS)
        assert found == f"A{BLANK}C"

    def test_a_double_mark_becomes_a_question_mark(self):
        found = canonical_answer_string({1: "B-D"}, [1], LABELS)
        assert found == MULTIPLE

    def test_the_string_is_never_compressed(self):
        numbers = list(range(1, 101))
        found = canonical_answer_string({}, numbers, LABELS)
        assert len(found) == 100
        assert set(found) == {BLANK}

    def test_a_missing_question_does_not_shorten_the_string(self):
        found = canonical_answer_string({1: "A"}, [1, 2, 3, 4], LABELS)
        assert len(found) == 4

    def test_lowercase_is_normalised(self):
        assert canonical_answer_string({1: "b"}, [1], LABELS) == "B"

    def test_an_unknown_symbol_is_not_read_as_blank(self):
        # Reporting it blank would credit a candidate for a question they
        # answered; the caller is expected to have blocked such a sheet.
        assert canonical_answer_string({1: "Z"}, [1], LABELS) == MULTIPLE

    def test_six_option_papers_work(self):
        labels = ("A", "B", "C", "D", "E", "F")
        assert canonical_answer_string({1: "F"}, [1], labels) == "F"

    def test_numbering_offset_is_respected(self):
        found = canonical_answer_string({51: "A", 52: "B"}, [51, 52], LABELS)
        assert found == "AB"


class TestPolicyDescription:
    def test_it_names_every_rule(self):
        lines = "\n".join(FIXED_QUARTER.describe())
        assert "Correct answer:   +1.00" in lines
        assert "-0.25" in lines
        assert "Minimum total" in lines

    def test_no_negative_marking_says_so_explicitly(self):
        lines = "\n".join(NO_NEGATIVE.describe())
        assert "no negative marking" in lines

    def test_a_disabled_clamp_says_negatives_are_allowed(self):
        lines = "\n".join(FIXED_QUARTER.describe())
        assert "negative totals allowed" in lines


class TestResultStatusSemantics:
    def test_only_a_scored_candidate_has_a_mark(self):
        assert ResultStatus.SCORED.has_mark is True
        assert ResultStatus.ABSENT.has_mark is False
        assert ResultStatus.BLOCKED.has_mark is False

    def test_an_absent_candidate_is_not_a_zero(self):
        # Zero would be indistinguishable from somebody who sat the paper and
        # answered nothing, and would drag every average down.
        assert ResultStatus.ABSENT.label == "Absent"
