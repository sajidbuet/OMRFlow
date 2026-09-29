"""Tests for the key-relative candidate-performance model.

Two kinds of assertion. **Exact** ones for a single candidate - the number of
answers matching the key is the drawn target, every wrong answer is a real
option that is not the key's - because those are properties of the
construction, not of chance. **Statistical** ones for a population, with a
fixed seed and tolerances several standard errors wide, because a finite sample
never has exactly the configured mean.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from tests.conftest import REPOSITORY_ROOT, build_answer_sheet_template

from omr_scanner.evaluation.answer_keys import generate_answer_keys
from omr_scanner.evaluation.case_plans import DatasetProfile, plan_dataset
from omr_scanner.evaluation.performance import (
    DEFAULT_PERFORMANCE,
    LEGACY_RANDOM,
    PerformanceDistribution,
    PerformancePolicy,
    apply_performance,
    intended_response,
    sample_truncated_normal,
    summarise_scores,
    truncated_moments,
)
from omr_scanner.evaluation.test_cases import FieldLayout, SheetCase, TestCaseTag
from omr_scanner.services.template_service import load_template

if TYPE_CHECKING:
    from omr_scanner.domain.template import OmrTemplate

POPULATION = 10_000

ECE_TEMPLATE = REPOSITORY_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"
"""A real 100-question, four-option form with a two-digit set code."""


@pytest.fixture(scope="module")
def hundred():
    """A 100-question, four-option paper with three sets."""
    template = load_template(ECE_TEMPLATE)
    layout = FieldLayout.of(template)
    keys = generate_answer_keys(template, layout, ("A", "B", "C"), seed=11)
    return template, layout, keys


def correct_count(answers, key) -> int:
    return sum(1 for number, label in answers.items() if key.answers[number] == label)


class TestThePolicy:
    def test_the_defaults_are_the_brief(self):
        assert DEFAULT_PERFORMANCE.distribution is PerformanceDistribution.NORMAL
        assert (DEFAULT_PERFORMANCE.mean, DEFAULT_PERFORMANCE.stddev) == (0.65, 0.15)
        assert (DEFAULT_PERFORMANCE.minimum, DEFAULT_PERFORMANCE.maximum) == (0.0, 1.0)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"minimum": 0.7, "mean": 0.65},
            {"maximum": 0.6, "mean": 0.65},
            {"stddev": -0.01},
            {"maximum": 1.2},
            {"minimum": -0.1},
            {"mean": float("nan")},
        ],
    )
    def test_impossible_settings_are_refused(self, kwargs):
        with pytest.raises(ValueError):
            PerformancePolicy(**kwargs).validate()

    def test_the_bounds_may_meet_the_mean(self):
        PerformancePolicy(mean=0.5, minimum=0.5, maximum=0.5).validate()


class TestTruncatedNormalSampling:
    @pytest.mark.parametrize(
        "policy",
        [
            DEFAULT_PERFORMANCE,
            PerformancePolicy(mean=0.9, stddev=0.2),  # cut hard at 100 %
            PerformancePolicy(mean=0.5, stddev=0.3, minimum=0.3, maximum=0.8),
        ],
    )
    def test_a_large_sample_matches_the_truncated_distribution(self, policy):
        rng = random.Random(20260929)
        draws = [sample_truncated_normal(rng, policy) for _ in range(POPULATION)]
        expected_mean, expected_sd = truncated_moments(policy)
        # Standard error of the mean at n=10,000 is sd/100; five of them.
        assert statistics.fmean(draws) == pytest.approx(expected_mean, abs=5 * expected_sd / 100)
        assert statistics.pstdev(draws) == pytest.approx(expected_sd, rel=0.05)
        assert min(draws) >= policy.minimum
        assert max(draws) <= policy.maximum

    def test_truncation_does_not_pile_mass_on_the_bounds(self):
        """Clamping would put ~2.5 % of a mean-0.9, SD-0.2 cohort at exactly 100 %."""
        rng = random.Random(5)
        policy = PerformancePolicy(mean=0.9, stddev=0.2)
        draws = [sample_truncated_normal(rng, policy) for _ in range(POPULATION)]
        assert sum(1 for value in draws if value == 1.0) == 0

    def test_zero_spread_gives_everybody_the_mean(self):
        rng = random.Random(1)
        policy = PerformancePolicy(stddev=0.0)
        assert {sample_truncated_normal(rng, policy) for _ in range(20)} == {0.65}

    def test_an_interval_deep_in_one_tail_does_not_fail(self):
        rng = random.Random(1)
        policy = PerformancePolicy(mean=0.0, stddev=0.001, minimum=0.0, maximum=1.0)
        value = sample_truncated_normal(rng, policy)
        assert 0.0 <= value <= 0.01
        far = PerformancePolicy(mean=0.5, stddev=0.001, minimum=0.5, maximum=1.0)
        assert 0.5 <= sample_truncated_normal(rng, far) <= 1.0

    def test_exactly_one_value_is_drawn_per_candidate(self):
        """So changing the spread never reassigns draws between candidates."""
        first, second = random.Random(9), random.Random(9)
        sample_truncated_normal(first, DEFAULT_PERFORMANCE)
        sample_truncated_normal(second, PerformancePolicy(stddev=0.3))
        assert first.random() == second.random()


class TestOneCandidate:
    def test_exactly_the_target_number_of_answers_match_the_key(self, hundred):
        _template, layout, keys = hundred
        rng = random.Random(3)
        for code in ("A", "B", "C"):
            for _ in range(200):
                response = intended_response(rng, keys[code], layout, DEFAULT_PERFORMANCE)
                assert response.answer_key_set == code
                assert response.scored_questions == 100
                assert response.target_correct == min(
                    max(round(response.target_fraction * 100), 0), 100
                )
                assert correct_count(response.answers, keys[code]) == response.target_correct

    def test_every_wrong_answer_is_a_real_option_that_is_not_the_key(self, hundred):
        _template, layout, keys = hundred
        rng = random.Random(4)
        key = keys["B"]
        for _ in range(100):
            response = intended_response(rng, key, layout, DEFAULT_PERFORMANCE)
            for number, label in response.answers.items():
                assert label in layout.labels_for(number)

    def test_wrong_answers_spread_over_every_other_option(self, hundred):
        _template, layout, keys = hundred
        rng = random.Random(8)
        key = keys["A"]
        policy = PerformancePolicy(mean=0.0, stddev=0.0)  # everything wrong
        chosen: dict[int, set[str]] = {}
        for _ in range(60):
            for number, label in intended_response(rng, key, layout, policy).answers.items():
                chosen.setdefault(number, set()).add(label)
        for number, labels in chosen.items():
            assert labels == set(layout.labels_for(number)) - {key.answers[number]}

    def test_correct_answers_are_not_bunched_by_question_number(self, hundred):
        """No Q1-Q70 right, Q71-Q100 wrong."""
        _template, layout, keys = hundred
        rng = random.Random(12)
        first_half = second_half = 0
        for _ in range(500):
            response = intended_response(rng, keys["C"], layout, DEFAULT_PERFORMANCE)
            for number, label in response.answers.items():
                if label == keys["C"].answers[number]:
                    if number <= 50:
                        first_half += 1
                    else:
                        second_half += 1
        assert first_half / (first_half + second_half) == pytest.approx(0.5, abs=0.02)

    def test_a_question_with_one_option_cannot_be_got_wrong(self):
        """The template model refuses such a question; the model refuses it too."""
        template = build_answer_sheet_template(answer_labels=("A", "B"))
        layout = FieldLayout.of(template)
        key = generate_answer_keys(template, layout, ("A",), seed=1)["A"]
        layout = replace(
            layout,
            question_zones=tuple(
                (zone, first, (key.answers[first],), count)
                for zone, first, _labels, count in layout.question_zones
            ),
        )
        with pytest.raises(ValueError, match="only one option"):
            intended_response(
                random.Random(1), key, layout, PerformancePolicy(mean=0.0, stddev=0.0)
            )


class TestThePopulation:
    def test_marks_follow_the_bell_curve_not_a_flat_line(self, hundred):
        _template, layout, keys = hundred
        rng = random.Random(20260929)
        codes = ("A", "B", "C")
        scores = [
            intended_response(rng, keys[codes[index % 3]], layout, DEFAULT_PERFORMANCE)
            .target_correct
            for index in range(POPULATION)
        ]
        fractions = [score / 100 for score in scores]
        expected_mean, expected_sd = truncated_moments(DEFAULT_PERFORMANCE)
        assert statistics.fmean(fractions) == pytest.approx(expected_mean, abs=0.005)
        # Rounding to whole questions adds variance of 1/12 question^2.
        assert statistics.pstdev(fractions) == pytest.approx(expected_sd, rel=0.05)
        assert min(scores) >= 0
        assert max(scores) <= 100

        # A normal with SD 15 puts ~68 % within one SD of the mean; a uniform
        # spread over 0-100 would put ~30 % there.
        within = sum(1 for value in fractions if abs(value - 0.65) <= 0.15) / len(fractions)
        assert within == pytest.approx(0.68, abs=0.03)
        tails = sum(1 for value in fractions if value < 0.2 or value > 0.99) / len(fractions)
        assert tails < 0.02

    def test_independent_rolls_would_have_been_much_narrower(self, hundred):
        """Why the target is drawn per candidate: per-question dice give SD ~4.8 %."""
        _template, layout, keys = hundred
        rng = random.Random(2)
        scores = [
            intended_response(rng, keys["A"], layout, DEFAULT_PERFORMANCE).target_correct / 100
            for _ in range(2000)
        ]
        assert statistics.pstdev(scores) > 0.12


class TestApplyingItToAPlannedDataset:
    def _cases(
        self,
        template: OmrTemplate,
        profile: DatasetProfile = DatasetProfile.MIXED,
        count: int = 120,
    ) -> list[SheetCase]:
        return plan_dataset(template, count=count, seed=21, profile=profile)

    def test_legacy_random_changes_nothing(self, hundred):
        template, layout, keys = hundred
        cases = self._cases(template)
        sets = ["A"] * len(cases)
        assert apply_performance(cases, sets, keys, layout, LEGACY_RANDOM, seed=1) == cases

    def test_free_answers_follow_the_intended_response(self, hundred):
        template, layout, keys = hundred
        cases = self._cases(template, profile=DatasetProfile.BASELINE)
        sets = [("A", "B", "C")[index % 3] for index in range(len(cases))]
        shaped = apply_performance(cases, sets, keys, layout, DEFAULT_PERFORMANCE, seed=1)
        for case, code in zip(shaped, sets, strict=True):
            assert case.intended is not None
            assert case.intended.answer_key_set == code
            assert (
                correct_count(case.intended.answers, keys[code]) == case.intended.target_correct
            )

    def test_explicit_test_conditions_survive(self, hundred):
        """A blank, a double mark or the first option stays exactly as planned."""
        template, layout, keys = hundred
        cases = self._cases(template)
        sets = ["B"] * len(cases)
        shaped = apply_performance(cases, sets, keys, layout, DEFAULT_PERFORMANCE, seed=1)
        changed = 0
        for before, after in zip(cases, shaped, strict=True):
            for zone, plans in before.marks.items():
                for group, plan in plans.items():
                    new = after.marks[zone][group]
                    if not plan.free:
                        assert new == plan
                    else:
                        # Only the option moves; style, fill and offset stay.
                        assert (new.style, new.fill, new.offset_x) == (
                            plan.style,
                            plan.fill,
                            plan.offset_x,
                        )
                        changed += new != plan
            if TestCaseTag.ALL_BLANK in before.tags:
                assert all(value == "" for value in after.answers.values())
            if TestCaseTag.MULTIPLE_ANSWER in before.tags:
                assert any("-" in value for value in after.answers.values())
        assert changed > 0

    def test_the_rendered_truth_is_rederived_from_the_marks(self, hundred):
        template, layout, keys = hundred
        cases = self._cases(template)
        shaped = apply_performance(
            cases, ["C"] * len(cases), keys, layout, DEFAULT_PERFORMANCE, seed=1
        )
        for case in shaped:
            for number, value in case.answers.items():
                zone, group = layout.locate(number)
                assert case.marks[zone][group].value == value

    def test_a_clean_sheets_rendered_answers_score_its_target(self, hundred):
        """Where no test condition applies, rendered state == intended response."""
        template, layout, keys = hundred
        cases = self._cases(template, profile=DatasetProfile.BASELINE, count=30)
        shaped = apply_performance(
            cases, ["A"] * len(cases), keys, layout, DEFAULT_PERFORMANCE, seed=1
        )
        full = [case for case in shaped if TestCaseTag.ALL_ANSWERED in case.tags]
        assert full
        for case in full:
            assert case.intended is not None
            assert correct_count(case.answers, keys["A"]) == case.intended.target_correct

    def test_it_is_reproducible(self, hundred):
        template, layout, keys = hundred
        cases = self._cases(template)
        sets = ["A"] * len(cases)
        first = apply_performance(cases, sets, keys, layout, DEFAULT_PERFORMANCE, seed=5)
        again = apply_performance(cases, sets, keys, layout, DEFAULT_PERFORMANCE, seed=5)
        other = apply_performance(cases, sets, keys, layout, DEFAULT_PERFORMANCE, seed=6)
        assert first == again
        assert first != other

    def test_a_set_without_a_key_is_refused(self, hundred):
        template, layout, keys = hundred
        cases = self._cases(template, count=5)
        with pytest.raises(ValueError, match="No answer key"):
            apply_performance(cases, ["Z"] * 5, keys, layout, DEFAULT_PERFORMANCE, seed=1)

    def test_the_summary_reports_the_cohort(self, hundred):
        template, layout, keys = hundred
        cases = self._cases(template, count=200)
        shaped = apply_performance(
            cases, ["A"] * len(cases), keys, layout, DEFAULT_PERFORMANCE, seed=1
        )
        summary = summarise_scores(shaped)
        assert summary is not None
        assert summary["candidates"] == 200
        assert 0.55 < summary["mean"] < 0.75
        assert sum(summary["histogram"].values()) == 200
        assert summarise_scores(cases) is None
