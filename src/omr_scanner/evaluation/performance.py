"""How well each synthetic candidate does, decided against the answer key.

Purpose:
    Give a synthetic cohort a realistic spread of marks. Each candidate gets a
    target score drawn from a truncated normal distribution, and exactly that
    many of their answers are made to agree with the key of the set they sat;
    the rest are made valid but wrong. The population's marks then follow the
    configured bell curve, rather than clustering around chance as answers
    drawn independently of the key do.

Responsibilities:
    * :class:`PerformanceDistribution` / :class:`PerformancePolicy` - the
      configuration, shared by the dialog, the command line and the API.
    * :func:`sample_truncated_normal` - one draw, by inverse transform.
    * :func:`intended_response` - one candidate's answers against one key.
    * :func:`apply_performance` - a whole planned dataset.
    * :func:`summarise_scores` - what the cohort actually came out as.

What does NOT belong here:
    * The key itself (:mod:`omr_scanner.evaluation.answer_keys`).
    * Test conditions. A blank, a double mark, a faint pencil stay exactly as
      the case plan stated them; see "Intended first, test conditions after".
    * Question difficulty, candidate ability or anything item-response-shaped.
      Every question is equally likely to be the one a candidate gets right.

The order, and why correctness is not rolled per question:
    ::

        sample target fraction  ~ truncated normal(mean, sd, [min, max])
        target_correct          = round(fraction * N), clamped to [0, N]
        choose exactly target_correct questions, uniformly, anywhere on the paper
            -> the key's answer
        every other question    -> a uniformly chosen option that is not the key's

    Rolling ``rng.random() < 0.65`` per question would give each candidate a
    binomial score around 65 % with a spread fixed by the question count, not
    by the configured standard deviation. Choosing the *number* first is what
    makes the cohort's marks follow the requested distribution, and choosing
    *which* questions uniformly over the whole paper is what stops correctness
    correlating with position.

Intended first, test conditions after:
    The case plan marks the questions it is testing explicitly and fills the
    rest in with :meth:`~omr_scanner.evaluation.test_cases.SheetBuilder.answer_all`,
    which flags those plans ``free``. Only free plans are re-labelled here, and
    only their *option* changes - the mark style, fill, offset and size a case
    chose stay as they were. So the rendered sheet is the intended response
    with the case's test conditions laid over it, and every existing edge case
    survives unchanged. The intended response is recorded in full
    (:class:`~omr_scanner.evaluation.test_cases.IntendedResponse`), so the two
    can be told apart afterwards.

Truncation:
    By inverse transform: ``u`` uniform on ``[F(a), F(b)]``, returned as
    ``F^-1(u)``, where ``F`` is the untruncated normal CDF. Exact, one draw per
    candidate, no rejection loop, and only the standard library's
    :class:`statistics.NormalDist`. Clamping instead would pile the tail mass
    onto the bounds as a spike.

Randomness:
    One stream, ``Random(f"{seed}:performance")``, walked in case order. Not the
    case planner's generator and not the answer keys' streams, so turning the
    model on or off moves nothing else the seed decides.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from omr_scanner.evaluation.test_cases import IntendedResponse, MarkPlan, SheetCase

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from omr_scanner.evaluation.answer_keys import SyntheticAnswerKey
    from omr_scanner.evaluation.test_cases import FieldLayout


class PerformanceDistribution(StrEnum):
    """How each candidate's share of correct answers is decided."""

    NORMAL = "normal"
    """A truncated normal distribution of scores. The default."""

    RANDOM = "random"
    """The generator's behaviour before answer keys existed: every freely
    filled answer is an independent uniform choice, unrelated to any key. Kept
    so a dataset generated that way can be regenerated exactly."""


DEFAULT_MEAN = 0.65
DEFAULT_STDDEV = 0.15
DEFAULT_MINIMUM = 0.0
DEFAULT_MAXIMUM = 1.0


@dataclass(frozen=True, slots=True)
class PerformancePolicy:
    """The candidate-performance configuration.

    Fractions of the paper, ``0``-``1``, like every other rate in the
    generator's API; the dialog shows them as percentages.

    Attributes:
        distribution: See :class:`PerformanceDistribution`.
        mean: Mean share of questions answered correctly, before truncation.
        stddev: Standard deviation, before truncation. ``0`` gives every
            candidate the mean.
        minimum / maximum: The truncation bounds.
    """

    distribution: PerformanceDistribution = PerformanceDistribution.NORMAL
    mean: float = DEFAULT_MEAN
    stddev: float = DEFAULT_STDDEV
    minimum: float = DEFAULT_MINIMUM
    maximum: float = DEFAULT_MAXIMUM

    @property
    def active(self) -> bool:
        """Whether answers are decided against the key at all."""
        return self.distribution is not PerformanceDistribution.RANDOM

    def validate(self) -> None:
        """Refuse an impossible configuration.

        Raises:
            ValueError: Unless ``0 <= minimum <= mean <= maximum <= 1`` and
                ``stddev >= 0``, all finite.
        """
        values = (self.mean, self.stddev, self.minimum, self.maximum)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Candidate performance settings must be finite numbers")
        if self.stddev < 0.0:
            raise ValueError("The standard deviation of candidate scores cannot be negative")
        if not 0.0 <= self.minimum <= self.mean <= self.maximum <= 1.0:
            raise ValueError(
                "Candidate performance needs 0% <= minimum <= mean <= maximum <= 100% "
                f"(got minimum {self.minimum:.0%}, mean {self.mean:.0%}, "
                f"maximum {self.maximum:.0%})"
            )

    def describe(self) -> dict[str, Any]:
        """As manifest data."""
        return {
            "distribution": self.distribution.value,
            "mean": self.mean,
            "stddev": self.stddev,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


DEFAULT_PERFORMANCE = PerformancePolicy()
"""The one default, used by the API, the command line and the dialog alike."""

LEGACY_RANDOM = PerformancePolicy(distribution=PerformanceDistribution.RANDOM)

_UNIT = statistics.NormalDist()
_EDGE = 1e-12
"""Keeps ``u`` strictly inside ``(0, 1)``, where ``inv_cdf`` is defined."""


def sample_truncated_normal(rng: random.Random, policy: PerformancePolicy) -> float:
    """One draw from the policy's truncated normal distribution.

    Args:
        rng: The performance stream. Exactly one value is consumed, whatever the
            parameters - so a change of spread never shifts which candidate
            receives which draw.
        policy: A validated policy.

    Returns:
        A fraction in ``[minimum, maximum]``.
    """
    u = rng.random()
    low, high = policy.minimum, policy.maximum
    if policy.stddev == 0.0 or low == high:
        return min(max(policy.mean, low), high)
    lower = _UNIT.cdf((low - policy.mean) / policy.stddev)
    upper = _UNIT.cdf((high - policy.mean) / policy.stddev)
    if upper - lower <= _EDGE:
        # The whole interval is so far into one tail that its probability mass
        # underflows. The distribution conditioned on it concentrates at the
        # bound nearest the mean.
        return low if abs(low - policy.mean) <= abs(high - policy.mean) else high
    p = min(max(lower + u * (upper - lower), _EDGE), 1.0 - _EDGE)
    value = policy.mean + policy.stddev * _UNIT.inv_cdf(p)
    return min(max(value, low), high)


def truncated_moments(policy: PerformancePolicy) -> tuple[float, float]:
    """The mean and standard deviation the truncated distribution actually has.

    What a large cohort's marks should converge to - which is not the
    configured mean when the bounds cut one tail harder than the other.
    """
    low, high = policy.minimum, policy.maximum
    if policy.stddev == 0.0 or low == high:
        return min(max(policy.mean, low), high), 0.0
    alpha = (low - policy.mean) / policy.stddev
    beta = (high - policy.mean) / policy.stddev
    mass = _UNIT.cdf(beta) - _UNIT.cdf(alpha)
    phi_a, phi_b = _UNIT.pdf(alpha), _UNIT.pdf(beta)
    shift = (phi_a - phi_b) / mass
    mean = policy.mean + policy.stddev * shift
    variance = policy.stddev**2 * (1.0 + (alpha * phi_a - beta * phi_b) / mass - shift**2)
    return mean, math.sqrt(max(variance, 0.0))


def intended_response(
    rng: random.Random,
    key: SyntheticAnswerKey,
    layout: FieldLayout,
    policy: PerformancePolicy,
) -> IntendedResponse:
    """Decide one candidate's intended answers against ``key``.

    Raises:
        ValueError: A question offers only one option, so it cannot be answered
            wrongly, and the candidate's score could not be made exactly what
            was drawn.
    """
    numbers = list(key.plan.numbers)
    fraction = sample_truncated_normal(rng, policy)
    target = min(max(round(fraction * len(numbers)), 0), len(numbers))
    correct = set(rng.sample(numbers, target))

    answers: dict[int, str] = {}
    for number in numbers:
        right = key.answers[number]
        if number in correct:
            answers[number] = right
            continue
        wrong = [label for label in layout.labels_for(number) if label != right]
        if not wrong:
            raise ValueError(
                f"Question {number} offers only one option, so no candidate can "
                "answer it wrongly; the performance model cannot be applied to "
                "this template"
            )
        answers[number] = rng.choice(wrong)
    return IntendedResponse(
        answer_key_set=key.set_code,
        target_fraction=fraction,
        target_correct=target,
        scored_questions=len(numbers),
        answers=answers,
    )


def apply_intended(case: SheetCase, intended: IntendedResponse, layout: FieldLayout) -> SheetCase:
    """Lay ``intended`` under ``case``'s own test conditions.

    Every ``free`` question plan takes the intended option and keeps its style,
    fill, offset and size; every plan the case stated explicitly is left alone.
    The expected answers are re-derived from the plans actually drawn, so the
    ground truth still describes the rendered sheet.
    """
    marks = {zone: dict(plans) for zone, plans in case.marks.items()}
    answers = dict(case.answers)
    for number, label in intended.answers.items():
        try:
            zone_id, group = layout.locate(number)
        except KeyError:
            continue
        plan = marks.get(zone_id, {}).get(group)
        if plan is None or not plan.free:
            continue
        chosen: MarkPlan = replace(plan, labels=(label,))
        marks[zone_id][group] = chosen
        answers[number] = chosen.value
    return replace(case, marks=marks, answers=answers, intended=intended)


def apply_performance(
    cases: Sequence[SheetCase],
    answer_sets: Sequence[str],
    keys: Mapping[str, SyntheticAnswerKey],
    layout: FieldLayout,
    policy: PerformancePolicy,
    *,
    seed: int,
) -> list[SheetCase]:
    """Decide every candidate's intended answers and lay them under the cases.

    Args:
        cases: The planned, identity-bound cases.
        answer_sets: For each case, the set whose key its candidate answered -
            the paper they sat. Must name a key in ``keys``.
        keys: The canonical keys.
        layout: The template's field layout.
        policy: The performance configuration.
        seed: The dataset's master seed.

    Returns:
        ``cases`` unchanged when the policy is :attr:`PerformanceDistribution.RANDOM`
        or there are no keys; otherwise every case with its intended response.

    Raises:
        ValueError: The policy is invalid, or a case names a set with no key.
    """
    policy.validate()
    if not policy.active or not keys:
        return list(cases)
    rng = random.Random(f"{seed}:performance")
    shaped: list[SheetCase] = []
    for case, set_code in zip(cases, answer_sets, strict=True):
        key = keys.get(set_code)
        if key is None:
            raise ValueError(f"No answer key was generated for set '{set_code}'")
        shaped.append(apply_intended(case, intended_response(rng, key, layout, policy), layout))
    return shaped


HISTOGRAM_BINS = 10


def summarise_scores(cases: Sequence[SheetCase]) -> dict[str, Any] | None:
    """What the cohort's intended scores actually came out as.

    Computed from the intended responses - the true synthetic scores, before
    any test condition - so it describes the performance model, not the
    renderer. ``None`` when no case has one.
    """
    scores = [
        case.intended.target_correct / case.intended.scored_questions
        for case in cases
        if case.intended is not None and case.intended.scored_questions
    ]
    if not scores:
        return None
    histogram = [0] * HISTOGRAM_BINS
    for score in scores:
        histogram[min(int(score * HISTOGRAM_BINS), HISTOGRAM_BINS - 1)] += 1
    return {
        "candidates": len(scores),
        "mean": round(statistics.fmean(scores), 4),
        "median": round(statistics.median(scores), 4),
        "stddev": round(statistics.pstdev(scores), 4),
        "minimum": round(min(scores), 4),
        "maximum": round(max(scores), 4),
        "histogram": {
            f"{index * 100 // HISTOGRAM_BINS}-{(index + 1) * 100 // HISTOGRAM_BINS}%": count
            for index, count in enumerate(histogram)
        },
    }


__all__ = [
    "DEFAULT_MAXIMUM",
    "DEFAULT_MEAN",
    "DEFAULT_MINIMUM",
    "DEFAULT_PERFORMANCE",
    "DEFAULT_STDDEV",
    "LEGACY_RANDOM",
    "PerformanceDistribution",
    "PerformancePolicy",
    "apply_intended",
    "apply_performance",
    "intended_response",
    "sample_truncated_normal",
    "summarise_scores",
    "truncated_moments",
]
