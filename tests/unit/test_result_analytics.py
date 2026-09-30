"""The Results dashboard's statistics, on small datasets worked out by hand.

Every expected value here is either computed by hand in a comment or by an
independent, obvious numpy expression - never by calling the function under
test a second way.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from fractions import Fraction
from typing import ClassVar

import numpy as np
import pytest

from omr_scanner.domain.scoring import (
    AnswerKey,
    AnswerKeyStatus,
    NegativeMarking,
    ResultStatus,
    ScoringPolicy,
    StaleReason,
    score_answers,
)
from omr_scanner.services import result_analytics as ra
from omr_scanner.services.scoring_store import StoredResult

LABELS = ("A", "B", "C", "D")
D = ra.RANGE_DASH
POLICY = ScoringPolicy()


def key_of(answers: str, set_code: str = "10", *, wrong: frozenset[int] = frozenset()) -> AnswerKey:
    return AnswerKey(
        set_code=set_code, answers=answers, wrong_questions=wrong,
        status=AnswerKeyStatus.VERIFIED,
    )


def stored(
    candidate: str,
    answers: str,
    *,
    set_code: str = "10",
    key_id: int = 1,
    key: AnswerKey | None = None,
    policy: ScoringPolicy = POLICY,
    status: ResultStatus = ResultStatus.SCORED,
    stale: tuple[StaleReason, ...] = (),
) -> StoredResult:
    """A stored result whose mark is what the real scorer gives these answers."""
    mark = None
    if status is ResultStatus.SCORED and key is not None:
        mark = score_answers(answers, key, policy).final_score
    return StoredResult(
        result_id=hash(candidate) & 0xFFFF, candidate_id=candidate, display_name="",
        scan_id=1, set_code=set_code, status=status, answer_string=answers,
        machine_answer_string=answers, corrected_questions=frozenset(),
        answer_key_id=key_id if status is ResultStatus.SCORED else None,
        answer_key_revision=1, policy_id=1, policy_revision=1, first_question=1,
        question_count=len(answers), correct_count=0, incorrect_count=0, blank_count=0,
        multiple_count=0, wrong_question_count=0, raw_score=mark, final_score=mark,
        clamped=False, blocks=(), stale_reasons=stale, computed_at=datetime.now(UTC),
    )


def records_for(rows: list[tuple[str, str]], key: AnswerKey, *, key_id: int = 1):
    results = [
        stored(f"c{index}", answers, set_code=code, key=key, key_id=key_id)
        for index, (code, answers) in enumerate(rows)
    ]
    return ra.build_records(results, {key_id: key}, {1: POLICY})


def mark_records(marks: list[int], set_code: str = "10") -> list[ra.ScriptRecord]:
    return [
        ra.ScriptRecord(f"c{i}", set_code, Fraction(mark), "", 1) for i, mark in enumerate(marks)
    ]


# ----------------------------------------------------------------------
class TestScoreSummary:
    MARKS: ClassVar[list[Fraction]] = [Fraction(v) for v in (2, 4, 4, 4, 5, 5, 7, 9)]

    def test_known_dataset(self):
        found = ra.score_summary(self.MARKS)
        assert found is not None
        assert found.count == 8
        assert found.mean == pytest.approx(5.0)
        assert found.median == pytest.approx(4.5)
        assert found.modes == (4.0,)
        assert found.mode_frequency == 3
        # Sample SD: sum of squared deviations is 32, over n - 1 = 7.
        assert found.std_dev == pytest.approx(math.sqrt(32 / 7))
        assert found.variance == pytest.approx(32 / 7)
        assert (found.minimum, found.maximum) == (2.0, 9.0)
        # Linear interpolation: Q1 at position 1.75 -> 4, Q3 at 5.25 -> 5.5.
        assert found.q1 == pytest.approx(4.0)
        assert found.q3 == pytest.approx(5.5)
        assert found.iqr == pytest.approx(1.5)

    def test_the_box_plot_whiskers_and_outliers(self):
        found = ra.score_summary(self.MARKS)
        assert found is not None
        # Fences 4 - 2.25 = 1.75 and 5.5 + 2.25 = 7.75: 9 is the only outlier.
        assert found.box.outliers == (9.0,)
        assert found.box.upper_whisker == 7.0
        assert found.box.lower_whisker == 2.0

    def test_skewness_matches_the_adjusted_formula(self):
        found = ra.score_summary(self.MARKS)
        values = np.array([float(v) for v in self.MARKS])
        n = values.size
        d = values - values.mean()
        g1 = np.mean(d**3) / np.mean(d**2) ** 1.5
        assert found is not None
        assert found.skewness == pytest.approx(g1 * math.sqrt(n * (n - 1)) / (n - 2))

    def test_every_mark_distinct_has_no_mode(self):
        found = ra.score_summary([Fraction(v) for v in (1, 2, 3)])
        assert found is not None and found.modes == ()

    def test_several_modes_are_all_reported(self):
        found = ra.score_summary([Fraction(v) for v in (1, 1, 2, 2, 3)])
        assert found is not None and found.modes == (1.0, 2.0)

    def test_one_mark_has_no_spread(self):
        found = ra.score_summary([Fraction(7)])
        assert found is not None
        assert found.mean == 7.0 and found.median == 7.0
        assert found.std_dev is None and found.variance is None and found.skewness is None
        assert found.modes == (7.0,)

    def test_identical_marks_have_no_skewness(self):
        found = ra.score_summary([Fraction(5)] * 4)
        assert found is not None
        assert found.std_dev == 0.0
        assert found.skewness is None

    def test_no_marks_is_none(self):
        assert ra.score_summary([]) is None

    def test_fractional_marks_are_exact_until_the_end(self):
        found = ra.score_summary([Fraction(2, 3), Fraction(4, 3)])
        assert found is not None and found.mean == pytest.approx(1.0)


# ----------------------------------------------------------------------
class TestHistogram:
    def test_a_hundred_mark_paper_gets_five_mark_bins(self):
        marks = [Fraction(v) for v in range(0, 101)]
        found = ra.score_histogram(marks, 100)
        assert found is not None
        assert found.width == 5
        assert len(found.bins) == 20
        assert found.bins[0].label == f"0{D}4"
        assert found.bins[14].label == f"70{D}74"
        assert found.bins[-1].label == f"95{D}100"
        assert found.bins[-1].closed is True

    def test_every_mark_is_counted_exactly_once(self):
        marks = [Fraction(v) for v in (0, 4, 5, 5, 74, 75, 99, 100, 100)]
        found = ra.score_histogram(marks, 100)
        assert found is not None
        assert sum(item.count for item in found.bins) == len(marks)
        by_label = {item.label: item.count for item in found.bins}
        assert by_label[f"0{D}4"] == 2
        assert by_label[f"5{D}9"] == 2
        assert by_label[f"70{D}74"] == 1
        assert by_label[f"75{D}79"] == 1
        assert by_label[f"95{D}100"] == 3
        assert sum(item.proportion for item in found.bins) == pytest.approx(1.0)

    def test_a_non_hundred_paper(self):
        marks = [Fraction(v) for v in (0, 1, 2, 29, 30)]
        found = ra.score_histogram(marks, 30)
        assert found is not None
        assert found.width == 2
        assert found.bins[-1].upper == 30
        assert found.bins[-1].count == 2  # 29 and 30, the closed last bin
        assert sum(item.count for item in found.bins) == 5

    def test_a_short_paper_uses_one_mark_bins(self):
        found = ra.score_histogram([Fraction(v) for v in (0, 3, 7)], 7)
        assert found is not None
        assert found.width == 1
        assert found.bins[-1].label == f"6{D}7"

    def test_negative_and_fractional_marks(self):
        marks = [Fraction(-2, 3), Fraction(1, 3), Fraction(10)]
        found = ra.score_histogram(marks, 10)
        assert found is not None
        assert found.bins[0].lower <= -2 / 3
        assert sum(item.count for item in found.bins) == 3
        assert " to <" in found.bins[0].label

    def test_no_marks_is_none(self):
        assert ra.score_histogram([], 100) is None


# ----------------------------------------------------------------------
class TestQuestionStatistics:
    KEY = key_of("ABCD")

    def test_counts_and_percentages(self):
        records = records_for(
            [("10", "ABCD"), ("10", "ACC_"), ("10", "B?C_"), ("10", "_BAD")], self.KEY
        )
        stats = ra.question_statistics(records, LABELS)
        q1, q2, q3, q4 = stats
        assert (q1.correct, q1.incorrect, q1.multiple, q1.blank) == (2, 1, 0, 1)
        assert (q2.correct, q2.incorrect, q2.multiple, q2.blank) == (2, 1, 1, 0)
        assert q3.p_correct == pytest.approx(0.75)
        assert q4.p_blank == pytest.approx(0.5)
        for stat in stats:
            total = stat.p_correct + stat.p_incorrect + stat.p_multiple + stat.p_blank
            assert total == pytest.approx(1.0)
            assert stat.responses == 4
        assert q1.key == ("A",)

    def test_distractor_counts_follow_the_labels(self):
        records = records_for(
            [("10", "ABCD"), ("10", "CBCD"), ("10", "CBCD"), ("10", "_BCD"), ("10", "?BCD")],
            self.KEY,
        )
        q1 = ra.question_statistics(records, LABELS)[0]
        assert q1.option_counts == (("A", 1), ("B", 0), ("C", 2), ("D", 0))
        assert (q1.option_blank, q1.option_multiple) == (1, 1)
        assert q1.scripts == 5

    def test_five_options_are_all_reported(self):
        key = key_of("E")
        records = records_for([("10", "E"), ("10", "A"), ("10", "E")], key)
        q1 = ra.question_statistics(records, ("A", "B", "C", "D", "E"))[0]
        assert [label for label, _ in q1.option_counts] == ["A", "B", "C", "D", "E"]
        assert dict(q1.option_counts)["E"] == 2

    def test_a_withdrawn_question_has_no_proportion(self):
        key = key_of("AB", wrong=frozenset({2}))
        records = records_for([("10", "AB"), ("10", "AC")], key)
        q1, q2 = ra.question_statistics(records, LABELS)
        assert q1.responses == 2
        assert q2.responses == 0 and q2.withdrawn == 2
        assert q2.p_correct is None and q2.difficulty is None
        assert q2.is_withdrawn is True

    def test_difficulty_bands(self):
        settings = ra.DEFAULT_SETTINGS
        assert ra.difficulty_for(0.80, settings) is ra.Difficulty.EASY
        assert ra.difficulty_for(0.7999, settings) is ra.Difficulty.MODERATE
        assert ra.difficulty_for(0.50, settings) is ra.Difficulty.MODERATE
        assert ra.difficulty_for(0.20, settings) is ra.Difficulty.DIFFICULT
        assert ra.difficulty_for(0.1999, settings) is ra.Difficulty.VERY_DIFFICULT
        assert ra.difficulty_for(None, settings) is None

    def test_the_bands_are_configurable(self):
        strict = ra.AnalyticsSettings(easy_min=0.9)
        assert ra.difficulty_for(0.85, strict) is ra.Difficulty.MODERATE


# ----------------------------------------------------------------------
class TestDiscrimination:
    def _cohort(self) -> tuple[ra.ScriptRecord, ...]:
        """Ten candidates: Q1-Q4 follow ability, Q5 is the reverse.

        Q5 is answered correctly only by the four weakest - a textbook
        negative item.
        """
        key = key_of("AAAAA")
        rows = []
        for ability in range(10):
            answers = "".join("A" if ability >= threshold else "B" for threshold in (2, 4, 6, 8))
            answers += "A" if ability < 4 else "B"
            rows.append(("10", answers))
        return records_for(rows, key)

    def test_sign_and_magnitude(self):
        stats = ra.question_statistics(self._cohort(), LABELS)
        for stat in stats[:4]:
            # Positive, though damped: the rest score includes the negative Q5.
            assert stat.discrimination is not None and stat.discrimination > 0.3
        assert stats[4].discrimination is not None and stats[4].discrimination < -0.5

    def test_it_is_the_corrected_point_biserial(self):
        records = self._cohort()
        stats = ra.question_statistics(records, LABELS)
        matrix = np.array(
            [[1.0 if code == 0 else 0.0 for code in r.outcomes] for r in records]
        )
        item = matrix[:, 0]
        rest = matrix.sum(axis=1) - item
        assert stats[0].discrimination == pytest.approx(float(np.corrcoef(item, rest)[0, 1]))

    def test_everybody_right_is_unavailable_not_zero(self):
        records = records_for([("10", "AB"), ("10", "AA"), ("10", "AC"), ("10", "AD"),
                               ("10", "AB")], key_of("AB"))
        assert ra.question_statistics(records, LABELS)[0].discrimination is None

    def test_too_few_candidates_is_unavailable(self):
        records = records_for([("10", "AB"), ("10", "BA")], key_of("AB"))
        assert ra.question_statistics(records, LABELS)[0].discrimination is None

    def test_negative_discrimination_is_flagged_for_review(self):
        stats = ra.question_statistics(self._cohort(), LABELS)
        flags = ra.review_flags(stats)
        assert any(
            flag.number == 5 and flag.kind is ra.FlagKind.NEGATIVE_DISCRIMINATION
            for flag in flags
        )


# ----------------------------------------------------------------------
class TestReliability:
    def test_kr20_matches_the_formula(self):
        rows = [
            "AAAAA", "AAAAB", "AAABB", "AABBB", "ABBBB", "BBBBB", "AABAB", "ABABA",
        ]
        records = records_for([("10", r) for r in rows], key_of("AAAAA"))
        found = ra.reliability(records)
        x = np.array([[1.0 if ch == "A" else 0.0 for ch in r] for r in rows])
        k = x.shape[1]
        p = x.mean(axis=0)
        variance = x.sum(axis=1).var()
        expected = k / (k - 1) * (1 - np.sum(p * (1 - p)) / variance)
        assert found.kr20 == pytest.approx(expected)
        assert found.sem == pytest.approx(math.sqrt(variance) * math.sqrt(1 - expected))
        assert (found.candidates, found.items) == (8, 5)
        assert found.reason == ""

    def test_zero_variance_is_not_available(self):
        records = records_for([("10", "AB")] * 6, key_of("AB"))
        found = ra.reliability(records)
        assert found.kr20 is None and found.sem is None
        assert "same number" in found.reason

    def test_too_few_candidates(self):
        records = records_for([("10", "AB"), ("10", "BA")], key_of("AB"))
        found = ra.reliability(records)
        assert found.kr20 is None and "at least" in found.reason

    def test_too_few_items(self):
        records = records_for([("10", r) for r in "ABABAB"], key_of("A"))
        found = ra.reliability(records)
        assert found.kr20 is None and "questions" in found.reason

    def test_a_withdrawn_question_is_left_out(self):
        rows = ["AAB", "ABB", "BAA", "BBA", "AAA", "BBB"]
        records = records_for([("10", r) for r in rows], key_of("AAA", wrong=frozenset({3})))
        assert ra.reliability(records).items == 2

    def test_a_negative_kr20_has_no_sem(self):
        # Two questions that disagree perfectly: total variance is zero... so
        # use three candidates where they anti-correlate without cancelling.
        rows = ["AB", "BA", "AB", "BA", "AA"]
        records = records_for([("10", r) for r in rows], key_of("AA"))
        found = ra.reliability(records)
        assert found.kr20 is not None and found.kr20 < 0
        assert found.sem is None and "negative" in found.reason


# ----------------------------------------------------------------------
class TestScopes:
    def _exam(self) -> ra.ExamAnalytics:
        key10, key11, key12 = key_of("AAAA", "10"), key_of("BBBB", "11"), key_of("CCCC", "12")
        keys = {10: key10, 11: key11, 12: key12}
        results = []
        for code, key_id, answers in (
            ("10", 10, ["AAAA", "AAA_", "AA__"]),
            ("11", 11, ["BBBB", "BBBB"]),
            ("12", 12, ["C___", "____", "CC__", "CCC_"]),
        ):
            for index, text in enumerate(answers):
                results.append(
                    stored(f"{code}-{index}", text, set_code=code, key=keys[key_id],
                           key_id=key_id)
                )
        records = ra.build_records(results, keys, {1: POLICY})
        return ra.analyse(records, set_codes=("10", "11", "12", "13"), labels=LABELS,
                          maximum_possible=4)

    def test_every_set_is_a_scope_even_an_empty_one(self):
        found = self._exam()
        assert [scope.label for scope in found.scopes] == [
            "Overall Exam", "Set 10", "Set 11", "Set 12", "Set 13",
        ]
        assert found.scope("13").count == 0
        assert found.scope("13").summary is None

    def test_each_set_is_computed_independently(self):
        found = self._exam()
        assert found.scope("10").summary.mean == pytest.approx(3.0)   # 4, 3, 2
        assert found.scope("11").summary.mean == pytest.approx(4.0)   # 4, 4
        assert found.scope("12").summary.mean == pytest.approx(1.5)   # 1, 0, 2, 3
        assert found.overall.count == 9
        assert found.overall.summary.mean == pytest.approx(23 / 9)  # (9 + 8 + 6) / 9

    def test_question_statistics_are_never_combined_across_sets(self):
        found = self._exam()
        assert found.overall.questions == ()
        assert "per set" in found.overall.question_note
        assert found.scope("12").question(2).p_correct == pytest.approx(0.5)

    def test_a_single_set_exam_has_overall_question_statistics(self):
        key = key_of("AB")
        records = records_for([("10", "AB"), ("10", "A_")], key)
        found = ra.analyse(records, set_codes=("10",), labels=LABELS, maximum_possible=2)
        assert found.overall.questions and found.overall.question_note == ""
        assert found.overall.question(2).p_correct == found.scope("10").question(2).p_correct

    def test_the_set_comparison_lists_every_set(self):
        rows = {row.set_code: row for row in self._exam().set_comparison}
        assert set(rows) == {"10", "11", "12", "13"}
        assert rows["11"].count == 2 and rows["11"].summary.std_dev == 0.0
        assert rows["13"].summary is None

    def test_easiest_and_hardest(self):
        scope = self._exam().scope("12")
        assert [stat.number for stat in scope.easiest(2)] == [1, 2]
        assert [stat.number for stat in scope.hardest(1)] == [4]

    def test_difficulty_counts_cover_every_band(self):
        counts = self._exam().scope("12").difficulty_counts()
        assert set(counts) == set(ra.Difficulty)
        assert sum(counts.values()) == 4


# ----------------------------------------------------------------------
class TestWhichResultsCount:
    def test_only_scored_results_with_a_mark(self):
        key = key_of("AB")
        results = [
            stored("a", "AB", key=key),
            stored("b", "", status=ResultStatus.ABSENT),
            stored("c", "AB", status=ResultStatus.BLOCKED),
        ]
        records = ra.build_records(results, {1: key}, {1: POLICY})
        assert [record.candidate_id for record in records] == ["a"]

    def test_the_stored_set_code_is_the_one_used(self):
        # The row's set code is the effective one written by scoring after
        # Resolve; the analytics never look anywhere else for it.
        key = key_of("AB", "12")
        records = ra.build_records(
            [stored("a", "AB", set_code="12", key=key)], {1: key}, {1: POLICY}
        )
        found = ra.analyse(records, set_codes=("11", "12"), labels=LABELS)
        assert found.scope("12").count == 1 and found.scope("11").count == 0

    def test_stale_results_count_and_are_reported(self):
        key = key_of("AB")
        results = [
            stored("a", "AB", key=key, stale=(StaleReason.SCORING_POLICY,)),
            stored("b", "A_", key=key),
        ]
        found = ra.analyse(ra.build_records(results, {1: key}, {1: POLICY}), labels=LABELS)
        assert found.overall.count == 2 and found.stale_count == 1

    def test_a_missing_key_revision_keeps_the_mark_but_not_the_questions(self):
        key = key_of("AB")
        results = [stored("a", "AB", key=key), stored("b", "A_", key=key, key_id=99)]
        records = ra.build_records(results, {1: key}, {1: POLICY})
        found = ra.analyse(records, set_codes=("10",), labels=LABELS)
        assert found.overall.count == 2
        assert found.excluded_count == 1
        assert found.scope("10").questions[0].responses == 1

    def test_the_breakdown_follows_the_policy_that_produced_the_mark(self):
        policy = ScoringPolicy(mode=NegativeMarking.ONE_PER_FOUR, clamp_minimum=False)
        key = key_of("AB")
        result = stored("a", "BB", key=key, policy=policy)
        (record,) = ra.build_records([result], {1: key}, {1: policy})
        assert record.mark == Fraction(3, 4)
        assert record.outcomes == (1, 0)  # incorrect, correct


# ----------------------------------------------------------------------
class TestEmptyStates:
    def test_no_scripts(self):
        found = ra.analyse([], set_codes=("10",), labels=LABELS, maximum_possible=100)
        assert found.overall.count == 0
        assert found.overall.summary is None and found.overall.histogram is None
        assert found.scope("10").count == 0

    def test_one_script(self):
        key = key_of("AB")
        found = ra.analyse(records_for([("10", "AB")], key), labels=LABELS, maximum_possible=2)
        scope = found.overall
        assert scope.summary.std_dev is None
        assert all(stat.discrimination is None for stat in scope.questions)
        assert scope.reliability.kr20 is None

    def test_no_nan_anywhere(self):
        key = key_of("AB")
        found = ra.analyse(records_for([("10", "AB")] * 3, key), labels=LABELS)
        for scope in found.scopes:
            for stat in scope.questions:
                for value in (stat.p_correct, stat.discrimination):
                    assert value is None or math.isfinite(value)

    def test_maximum_mark(self):
        assert ra.maximum_mark(100, ScoringPolicy()) == 100.0
        assert ra.maximum_mark(50, ScoringPolicy(correct_mark=Fraction(2))) == 100.0


# ----------------------------------------------------------------------
class TestReviewFlags:
    def test_blank_rate_and_extremes(self):
        key = key_of("AB")
        rows = [("10", "A_")] * 5 + [("10", "AB")] * 5
        stats = ra.question_statistics(records_for(rows, key), LABELS)
        kinds = {(flag.number, flag.kind) for flag in ra.review_flags(stats)}
        assert (1, ra.FlagKind.HIGH_CORRECT) in kinds
        assert (2, ra.FlagKind.HIGH_BLANK) in kinds

    def test_a_distractor_that_outdraws_the_key(self):
        key = key_of("A")
        rows = [("10", "B")] * 15 + [("10", "A")] * 10
        stats = ra.question_statistics(records_for(rows, key), LABELS)
        flags = [flag for flag in ra.review_flags(stats)
                 if flag.kind is ra.FlagKind.DISTRACTOR_OVER_KEY]
        assert flags and "Option B" in flags[0].detail

    def test_a_distractor_nobody_chose(self):
        key = key_of("A")
        rows = [("10", "A")] * 15 + [("10", "B")] * 6 + [("10", "C")] * 6
        stats = ra.question_statistics(records_for(rows, key), LABELS)
        flags = [flag for flag in ra.review_flags(stats)
                 if flag.kind is ra.FlagKind.UNUSED_DISTRACTOR]
        assert flags and flags[0].detail == "No candidate chose D."

    def test_an_unused_distractor_with_few_wrong_answers_is_not_flagged(self):
        key = key_of("A")
        rows = [("10", "A")] * 25 + [("10", "B")] * 3
        stats = ra.question_statistics(records_for(rows, key), LABELS)
        assert not [flag for flag in ra.review_flags(stats)
                    if flag.kind is ra.FlagKind.UNUSED_DISTRACTOR]

    def test_disagreeing_key_revisions_are_flagged(self):
        old, new = key_of("A"), key_of("B")
        results = [stored("a", "A", key=old, key_id=1), stored("b", "B", key=new, key_id=2)]
        records = ra.build_records(results, {1: old, 2: new}, {1: POLICY})
        stats = ra.question_statistics(records, LABELS)
        assert stats[0].key == ("A", "B")
        assert any(flag.kind is ra.FlagKind.KEY_VARIES for flag in ra.review_flags(stats))
