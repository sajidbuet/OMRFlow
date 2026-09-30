"""Describe a marked examination statistically - scores, questions and sets.

Purpose:
    The analytics behind the Results stage's *Dashboard* tab: how a cohort's
    marks are distributed, how each question behaved, whether a set looks
    different from the others and how internally consistent the paper was.
    Structured values, not widgets, so a later report, CSV or JSON export can
    use exactly what the dashboard shows.

Scope:
    Pure functions over value objects - no Qt, and no database except in
    :func:`load_keys_and_policies`, which only reads the key and policy
    revisions results already point at.

Where the data comes from - and why nothing here scores anything:
    The input is the list of :class:`~omr_scanner.services.scoring_store.StoredResult`
    rows the Results table itself shows. Those rows already carry every
    decision upstream made: one row per *candidate* (so a duplicate, a
    superseded sheet or a rescan is never a second row), the **effective**
    answer string and set code after Resolve, and the exact key and policy
    revision that produced the mark. A rejected, deferred or discarded sheet
    never reaches a result row, so it cannot reach these statistics either.

    The per-question classification (correct / incorrect / multiple / blank /
    withdrawn) is regenerated with
    :func:`~omr_scanner.domain.scoring.score_answers` over those stored
    inputs - the same pure function, key revision and policy revision that
    produced the mark, exactly as the Results detail panel does. There is no
    second marking implementation here to fall out of step with it.

Which scripts count:
    Only results with status *Scored* and a mark (:attr:`StoredResult.has_mark`).
    Absent and cannot-be-scored candidates have no mark and are excluded,
    never counted as zero. A *stale* result keeps its mark on the Results
    table and is included here likewise - so the counts reconcile with that
    table - but :attr:`ExamAnalytics.stale_count` says how many there are so
    the dashboard can say the figures are not current.

Overall exam versus a set:
    Every set has its own answer key, and OMRFlow records **no mapping**
    between one set's question 37 and another's. Sets are commonly printed
    with the questions reordered, so question-level statistics are only ever
    computed within one set. The *Overall Exam* scope combines every set's
    marks for the score statistics; its question statistics exist only when
    every scored script in it belongs to the same set (a one-set examination),
    where the two scopes are the same population.

Formulas (all documented again beside the code):
    * Standard deviation and variance: sample (``n - 1``) estimates.
    * Quartiles: linear interpolation between order statistics (numpy's
      default, the same as Excel's ``QUARTILE.INC``).
    * Skewness: the adjusted Fisher-Pearson coefficient ``G1``.
    * Discrimination: the *corrected* point-biserial correlation - Pearson's
      r between a question's 0/1 correctness and the number of *other*
      questions answered correctly, so a question is not correlated with a
      total that contains itself.
    * Reliability: KR-20 over the 0/1 correctness matrix (population
      variances, which is how the formula is defined), and the standard error
      of measurement ``SEM = SD x sqrt(1 - KR20)`` in questions-correct units.

Privacy:
    Nothing is logged. The values carry candidate ids only because the
    records are built from result rows; no statistic exposes one.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import TYPE_CHECKING

import numpy as np

from omr_scanner.domain.scoring import (
    BLANK,
    MULTIPLE,
    AnswerKey,
    QuestionOutcome,
    ScoringPolicy,
    score_answers,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from numpy.typing import NDArray

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.services.scoring_store import StoredResult

__all__ = [
    "DEFAULT_SETTINGS",
    "OVERALL",
    "RANGE_DASH",
    "AnalyticsSettings",
    "BoxSummary",
    "Difficulty",
    "ExamAnalytics",
    "FlagKind",
    "Histogram",
    "HistogramBin",
    "QuestionFlag",
    "QuestionStat",
    "Reliability",
    "ScopeAnalytics",
    "ScoreSummary",
    "ScriptRecord",
    "SetComparisonRow",
    "analyse",
    "build_records",
    "difficulty_for",
    "load_keys_and_policies",
    "point_biserial",
    "question_statistics",
    "reliability",
    "score_histogram",
    "score_summary",
]


RANGE_DASH = chr(0x2013)
"""The en dash (U+2013) between the ends of a mark range, as in "70 to 74"."""

OVERALL: str | None = None
"""The scope key of the whole examination; a set's scope key is its code."""


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AnalyticsSettings:
    """Every threshold the analytics use, in one place.

    Proportions are fractions of 1, not percentages. These are conventional
    defaults for classroom MCQ analysis, **not** psychometric standards: a
    band or a flag is a prompt for a person to look, never a verdict.

    Attributes:
        easy_min: Proportion correct at or above which a question is *Easy*.
        moderate_min: ... *Moderate* (below :attr:`easy_min`).
        difficult_min: ... *Difficult* (below :attr:`moderate_min`); anything
            lower is *Very difficult*.
        flag_high_correct: Proportion correct at or above which a question is
            listed for review as possibly too easy to tell candidates apart.
        flag_low_correct: Proportion correct below which a question is listed
            for review - a possible key error or an ambiguous question.
        flag_blank_rate: Proportion blank at or above which a question is
            listed for review.
        flag_negative_discrimination: Discrimination below which a question
            is listed for review. Zero: *any* negative value means the
            stronger candidates did worse on it than the weaker ones.
        min_candidates_discrimination: Fewest responses for which a
            discrimination index is reported. Below it the correlation is
            dominated by one or two candidates.
        min_candidates_reliability: Fewest candidates for KR-20.
        min_items_reliability: Fewest questions for KR-20 (the formula needs
            at least two).
        min_candidates_distractor_flags: Fewest responses before distractor
            flags are raised; on a handful of scripts every option looks odd.
        min_incorrect_unused_distractor: Fewest wrong or multiple answers to
            a question before a distractor nobody chose is flagged. With only
            a few wrong answers, some option going unchosen is expected.
        ranked_count: How many questions the easiest / hardest lists show.
        target_bins: Roughly how many histogram bins to aim for. A 0-100
            examination gets 20 bins of 5 marks.
    """

    easy_min: float = 0.80
    moderate_min: float = 0.50
    difficult_min: float = 0.20
    flag_high_correct: float = 0.95
    flag_low_correct: float = 0.20
    flag_blank_rate: float = 0.10
    flag_negative_discrimination: float = 0.0
    min_candidates_discrimination: int = 5
    min_candidates_reliability: int = 5
    min_items_reliability: int = 2
    min_candidates_distractor_flags: int = 20
    min_incorrect_unused_distractor: int = 10
    ranked_count: int = 10
    target_bins: int = 20


DEFAULT_SETTINGS = AnalyticsSettings()


class Difficulty(StrEnum):
    """A question's difficulty band, from its proportion correct."""

    EASY = "easy"
    MODERATE = "moderate"
    DIFFICULT = "difficult"
    VERY_DIFFICULT = "very_difficult"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            Difficulty.EASY: "Easy",
            Difficulty.MODERATE: "Moderate",
            Difficulty.DIFFICULT: "Difficult",
            Difficulty.VERY_DIFFICULT: "Very difficult",
        }[self]


def difficulty_for(
    proportion: float | None, settings: AnalyticsSettings = DEFAULT_SETTINGS
) -> Difficulty | None:
    """The band a proportion correct falls in, or ``None`` when there is none."""
    if proportion is None:
        return None
    if proportion >= settings.easy_min:
        return Difficulty.EASY
    if proportion >= settings.moderate_min:
        return Difficulty.MODERATE
    if proportion >= settings.difficult_min:
        return Difficulty.DIFFICULT
    return Difficulty.VERY_DIFFICULT


def describe_bands(settings: AnalyticsSettings = DEFAULT_SETTINGS) -> tuple[str, ...]:
    """The difficulty bands as sentences, for a tooltip or a report."""

    def pct(value: float) -> str:
        return f"{value * 100:g}%"

    return (
        f"Easy: {pct(settings.easy_min)} or more correct",
        f"Moderate: {pct(settings.moderate_min)} to below {pct(settings.easy_min)}",
        f"Difficult: {pct(settings.difficult_min)} to below {pct(settings.moderate_min)}",
        f"Very difficult: below {pct(settings.difficult_min)}",
    )


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------
_CORRECT, _INCORRECT, _MULTIPLE, _BLANK, _WITHDRAWN = range(5)
_CODES = {
    QuestionOutcome.CORRECT: _CORRECT,
    QuestionOutcome.INCORRECT: _INCORRECT,
    QuestionOutcome.MULTIPLE: _MULTIPLE,
    QuestionOutcome.BLANK: _BLANK,
    QuestionOutcome.WRONG_QUESTION: _WITHDRAWN,
}


@dataclass(frozen=True, slots=True)
class ScriptRecord:
    """One scored candidate, reduced to what the analytics need.

    Attributes:
        candidate_id: Whose result this is. Never shown by a statistic.
        set_code: The **effective** set, as recorded on the result.
        mark: The final mark, exact.
        answers: The effective canonical answer string.
        first_question: Printed number of ``answers[0]``.
        key_answers: The answers of the key revision that produced the mark,
            or ``""`` when that revision can no longer be read.
        outcomes: One classification code per question, or ``None`` when the
            breakdown cannot be regenerated (the key or policy revision is
            missing, or the lengths disagree). Such a record still counts in
            the score statistics - it has a mark - but not in the question
            ones.
        stale: Whether the Results table flags this result as out of date.
    """

    candidate_id: str
    set_code: str
    mark: Fraction
    answers: str
    first_question: int
    key_answers: str = ""
    outcomes: tuple[int, ...] | None = None
    stale: bool = False


def load_keys_and_policies(
    database: ProjectDatabase, results: Iterable[StoredResult]
) -> tuple[dict[int, AnswerKey], dict[int, ScoringPolicy]]:
    """Read the key and policy revisions the given results were computed with.

    One read per *distinct* revision - a handful per examination - so it is
    cheap enough for the GUI thread; the expensive part, regenerating every
    breakdown, is :func:`build_records`.
    """
    from omr_scanner.services import scoring_store

    keys: dict[int, AnswerKey] = {}
    policies: dict[int, ScoringPolicy] = {}
    for item in results:
        if item.answer_key_id is not None and item.answer_key_id not in keys:
            stored = scoring_store.get_key(database, item.answer_key_id)
            if stored is not None:
                keys[item.answer_key_id] = stored.key
        if item.policy_id is not None and item.policy_id not in policies:
            policy = scoring_store.get_policy(database, item.policy_id)
            if policy is not None:
                policies[item.policy_id] = policy.policy
    return keys, policies


def build_records(
    results: Iterable[StoredResult],
    keys: Mapping[int, AnswerKey],
    policies: Mapping[int, ScoringPolicy],
) -> tuple[ScriptRecord, ...]:
    """Reduce stored results to analytics records.

    Only results with a mark are kept. Each one's per-question outcomes are
    regenerated with :func:`~omr_scanner.domain.scoring.score_answers` from
    its own stored key and policy revisions - the breakdown the Results
    detail panel shows for it, by construction.
    """
    records: list[ScriptRecord] = []
    for item in results:
        if not item.has_mark or item.final_score is None:
            continue
        key = keys.get(item.answer_key_id) if item.answer_key_id is not None else None
        policy = policies.get(item.policy_id) if item.policy_id is not None else None
        outcomes: tuple[int, ...] | None = None
        if key is not None and policy is not None:
            try:
                breakdown = score_answers(
                    item.answer_string, key, policy, first_question=item.first_question
                )
            except ValueError:
                breakdown = None
            if breakdown is not None:
                outcomes = tuple(_CODES[question.outcome] for question in breakdown.questions)
        records.append(
            ScriptRecord(
                candidate_id=item.candidate_id,
                set_code=item.set_code,
                mark=item.final_score,
                answers=item.answer_string,
                first_question=item.first_question,
                key_answers=key.answers if key is not None else "",
                outcomes=outcomes,
                stale=item.is_stale,
            )
        )
    return tuple(records)


# ----------------------------------------------------------------------
# Score statistics
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BoxSummary:
    """A five-number summary with Tukey whiskers.

    The whiskers reach the most extreme observations within ``1.5 x IQR`` of
    the quartiles; anything further out is an outlier and listed.
    """

    minimum: float
    q1: float
    median: float
    q3: float
    maximum: float
    lower_whisker: float
    upper_whisker: float
    outliers: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class ScoreSummary:
    """Descriptive statistics of a set of marks.

    ``None`` means "not meaningful for this many candidates", never zero: a
    standard deviation of one mark does not exist.
    """

    count: int
    mean: float
    median: float
    modes: tuple[float, ...]
    """Every most-frequent value, ascending. Empty when every mark occurs
    exactly once (and there is more than one): then no value is typical."""
    mode_frequency: int
    minimum: float
    maximum: float
    q1: float
    q3: float
    std_dev: float | None
    """Sample standard deviation (``n - 1``). ``None`` below two marks."""
    variance: float | None
    skewness: float | None
    """Adjusted Fisher-Pearson ``G1``. ``None`` below three marks or when
    every mark is the same."""
    box: BoxSummary

    @property
    def iqr(self) -> float:
        """Interquartile range."""
        return self.q3 - self.q1


def score_summary(marks: Sequence[Fraction]) -> ScoreSummary | None:
    """Describe a set of marks, or ``None`` when there are none."""
    if not marks:
        return None
    values = np.array([float(mark) for mark in marks], dtype=np.float64)
    count = int(values.size)
    mean = float(values.mean())
    q1, median, q3 = (float(v) for v in np.percentile(values, [25.0, 50.0, 75.0]))

    tally = Counter(marks)
    frequency = max(tally.values())
    modes: tuple[float, ...] = (
        ()
        if frequency == 1 and count > 1
        else tuple(sorted(float(value) for value, seen in tally.items() if seen == frequency))
    )

    std_dev = variance = skewness = None
    if count >= 2:
        variance = float(values.var(ddof=1))
        std_dev = math.sqrt(variance)
    if count >= 3:
        deviations = values - mean
        m2 = float(np.mean(deviations**2))
        if m2 > 0:
            m3 = float(np.mean(deviations**3))
            g1 = m3 / m2**1.5
            skewness = g1 * math.sqrt(count * (count - 1)) / (count - 2)

    spread = q3 - q1
    low_fence, high_fence = q1 - 1.5 * spread, q3 + 1.5 * spread
    inside = values[(values >= low_fence) & (values <= high_fence)]
    outside = values[(values < low_fence) | (values > high_fence)]
    box = BoxSummary(
        minimum=float(values.min()),
        q1=q1,
        median=median,
        q3=q3,
        maximum=float(values.max()),
        lower_whisker=float(inside.min()) if inside.size else q1,
        upper_whisker=float(inside.max()) if inside.size else q3,
        outliers=tuple(sorted(float(v) for v in outside)),
    )
    return ScoreSummary(
        count=count,
        mean=mean,
        median=median,
        modes=modes,
        mode_frequency=frequency,
        minimum=box.minimum,
        maximum=box.maximum,
        q1=q1,
        q3=q3,
        std_dev=std_dev,
        variance=variance,
        skewness=skewness,
        box=box,
    )


@dataclass(frozen=True, slots=True)
class HistogramBin:
    """One bar of the marks histogram: ``[lower, upper)``, or closed if last."""

    lower: float
    upper: float
    count: int
    proportion: float
    label: str
    closed: bool = False


@dataclass(frozen=True, slots=True)
class Histogram:
    """The marks histogram, with the bin width it was built with."""

    bins: tuple[HistogramBin, ...]
    width: float
    total: int


_NICE_STEPS = (1.0, 2.0, 2.5, 5.0)


def _nice_width(span: float, target: int, *, integral: bool) -> float:
    """The smallest "nice" width that gives at most ``target`` bins."""
    if span <= 0:
        return 1.0
    raw = span / max(target, 1)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for step in (*_NICE_STEPS, 10.0):
        width = step * magnitude
        if integral and width < 1:
            continue
        if integral and not float(width).is_integer():
            continue
        if width >= raw - 1e-12:
            return width
    return 10.0 * magnitude  # pragma: no cover - the loop always returns


def _format_number(value: float) -> str:
    """A bin edge or a statistic without spurious decimals."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def score_histogram(
    marks: Sequence[Fraction],
    maximum_possible: float,
    *,
    minimum_possible: float = 0.0,
    settings: AnalyticsSettings = DEFAULT_SETTINGS,
) -> Histogram | None:
    """Bin a set of marks for the histogram.

    Args:
        marks: The marks.
        maximum_possible: The paper's full mark - the histogram spans the
            whole scale, not just the marks observed, so an easy paper looks
            easy. Widened if a mark exceeds it.
        minimum_possible: The scale's floor; widened for a negative mark.
        settings: :attr:`AnalyticsSettings.target_bins` sets the width.

    Every mark lands in exactly one bin: bins are half-open ``[lower,
    upper)`` except the last, which is closed so the full mark is counted.
    """
    if not marks:
        return None
    values = [float(mark) for mark in marks]
    low = min(minimum_possible, min(values))
    high = max(maximum_possible, max(values))
    integral = all(mark.denominator == 1 for mark in marks) and float(high).is_integer()
    width = _nice_width(high - low, settings.target_bins, integral=integral)
    start = math.floor(low / width) * width
    count = max(1, math.ceil((high - start) / width - 1e-9))
    edges = [start + index * width for index in range(count + 1)]
    counts = [0] * count
    for value in values:
        index = min(int((value - start) // width), count - 1)
        counts[max(index, 0)] += 1
    total = len(values)
    integer_labels = integral and float(width).is_integer()
    bins: list[HistogramBin] = []
    for index in range(count):
        lower, upper = edges[index], edges[index + 1]
        closed = index == count - 1
        if integer_labels:
            top = upper if closed else upper - 1
            label = (
                _format_number(lower)
                if top <= lower
                else f"{_format_number(lower)}{RANGE_DASH}{_format_number(top)}"
            )
        else:
            label = (
                f"{_format_number(lower)} to {_format_number(upper)}"
                if closed
                else f"{_format_number(lower)} to <{_format_number(upper)}"
            )
        bins.append(
            HistogramBin(
                lower=lower,
                upper=upper,
                count=counts[index],
                proportion=counts[index] / total,
                label=label,
                closed=closed,
            )
        )
    return Histogram(bins=tuple(bins), width=width, total=total)


# ----------------------------------------------------------------------
# Question statistics
# ----------------------------------------------------------------------
class FlagKind(StrEnum):
    """Why a question is listed under *Questions to review*."""

    NEGATIVE_DISCRIMINATION = "negative_discrimination"
    LOW_CORRECT = "low_correct"
    HIGH_CORRECT = "high_correct"
    HIGH_BLANK = "high_blank"
    DISTRACTOR_OVER_KEY = "distractor_over_key"
    UNUSED_DISTRACTOR = "unused_distractor"
    KEY_VARIES = "key_varies"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            FlagKind.NEGATIVE_DISCRIMINATION: "Negative discrimination",
            FlagKind.LOW_CORRECT: "Very low % correct",
            FlagKind.HIGH_CORRECT: "Very high % correct",
            FlagKind.HIGH_BLANK: "High unanswered rate",
            FlagKind.DISTRACTOR_OVER_KEY: "A distractor outdrew the key",
            FlagKind.UNUSED_DISTRACTOR: "Distractor chosen by nobody",
            FlagKind.KEY_VARIES: "Scored under different key revisions",
        }[self]


@dataclass(frozen=True, slots=True)
class QuestionFlag:
    """One reason to look at one question again. A prompt, never a verdict."""

    number: int
    kind: FlagKind
    detail: str


@dataclass(frozen=True, slots=True)
class QuestionStat:
    """How one question behaved in one scope.

    Proportions are over :attr:`responses` - the scripts on which the
    question counted (not withdrawn) - and are ``None`` when there are none.
    """

    number: int
    responses: int
    correct: int
    incorrect: int
    multiple: int
    blank: int
    withdrawn: int
    key: tuple[str, ...]
    """The key's answer(s). More than one only when the scope's results were
    scored under key revisions that disagree on this question - i.e. some of
    them are stale."""
    option_counts: tuple[tuple[str, int], ...]
    """How many scripts marked each option, in the template's label order,
    over every script in the scope (withdrawn or not)."""
    option_blank: int
    option_multiple: int
    discrimination: float | None
    difficulty: Difficulty | None

    def _share(self, value: int) -> float | None:
        return value / self.responses if self.responses else None

    @property
    def p_correct(self) -> float | None:
        """Proportion answered correctly."""
        return self._share(self.correct)

    @property
    def p_incorrect(self) -> float | None:
        """Proportion answered with one wrong option."""
        return self._share(self.incorrect)

    @property
    def p_multiple(self) -> float | None:
        """Proportion with a confirmed multiple mark."""
        return self._share(self.multiple)

    @property
    def p_blank(self) -> float | None:
        """Proportion left unanswered."""
        return self._share(self.blank)

    @property
    def scripts(self) -> int:
        """Every script in the scope, for the option percentages."""
        return sum(count for _, count in self.option_counts) + self.option_blank + (
            self.option_multiple
        )

    @property
    def is_withdrawn(self) -> bool:
        """Whether the question was withdrawn for every script in scope."""
        return self.responses == 0 and self.withdrawn > 0


def point_biserial(item: NDArray[np.float64], rest: NDArray[np.float64]) -> float | None:
    """Pearson's r between a 0/1 item and a rest score; ``None`` if undefined.

    Undefined when either variable is constant - everybody right, everybody
    wrong, or every rest score equal - which is reported as unavailable
    rather than as zero.
    """
    if item.size < 2:
        return None
    item_sd = float(item.std())
    rest_sd = float(rest.std())
    if item_sd == 0 or rest_sd == 0:
        return None
    covariance = float(np.mean((item - item.mean()) * (rest - rest.mean())))
    value = covariance / (item_sd * rest_sd)
    return max(-1.0, min(1.0, value))


def _matrix(records: Sequence[ScriptRecord]) -> NDArray[np.int8]:
    """The outcome codes of records that have them, one row per script."""
    rows = [record.outcomes for record in records if record.outcomes is not None]
    width = max((len(row) for row in rows), default=0)
    if any(len(row) != width for row in rows):
        # Different paper lengths in one scope cannot be analysed together.
        raise ValueError("records cover different numbers of questions")
    if not rows:
        return np.zeros((0, 0), dtype=np.int8)
    return np.array(rows, dtype=np.int8)


def question_statistics(
    records: Sequence[ScriptRecord],
    labels: Sequence[str],
    settings: AnalyticsSettings = DEFAULT_SETTINGS,
) -> tuple[QuestionStat, ...]:
    """Every question's statistics over ``records``, which must be one set.

    Records without outcomes are skipped. Discrimination is the corrected
    point-biserial: each script's rest score is the number of the *other*
    counted questions it answered correctly.
    """
    usable = [record for record in records if record.outcomes is not None]
    codes = _matrix(usable)
    if codes.size == 0:
        return ()
    correct = (codes == _CORRECT).astype(np.float64)
    counted = codes != _WITHDRAWN
    totals = correct.sum(axis=1)
    first = usable[0].first_question
    order = list(labels)
    known = set(order)
    stats: list[QuestionStat] = []
    for column in range(codes.shape[1]):
        mask = counted[:, column]
        responses = int(mask.sum())
        item = correct[mask, column]
        rest = totals[mask] - item
        discrimination = (
            point_biserial(item, rest)
            if responses >= settings.min_candidates_discrimination
            else None
        )
        tally: Counter[str] = Counter()
        blank = multiple = 0
        keys: list[str] = []
        for record in usable:
            character = record.answers[column] if column < len(record.answers) else BLANK
            if character == BLANK:
                blank += 1
            elif character == MULTIPLE or character not in known:
                multiple += 1
            else:
                tally[character] += 1
            if column < len(record.key_answers):
                expected = record.key_answers[column]
                if expected not in keys:
                    keys.append(expected)
        column_codes = codes[:, column]
        p_correct = float(item.mean()) if responses else None
        stats.append(
            QuestionStat(
                number=first + column,
                responses=responses,
                correct=int((column_codes == _CORRECT).sum()),
                incorrect=int((column_codes == _INCORRECT).sum()),
                multiple=int((column_codes == _MULTIPLE).sum()),
                blank=int((column_codes == _BLANK).sum()),
                withdrawn=int((column_codes == _WITHDRAWN).sum()),
                key=tuple(sorted(keys)),
                option_counts=tuple((label, tally.get(label, 0)) for label in order),
                option_blank=blank,
                option_multiple=multiple,
                discrimination=discrimination,
                difficulty=difficulty_for(p_correct, settings),
            )
        )
    return tuple(stats)


def review_flags(
    stats: Sequence[QuestionStat], settings: AnalyticsSettings = DEFAULT_SETTINGS
) -> tuple[QuestionFlag, ...]:
    """Questions worth a second look, in question order.

    Each flag is evidence to weigh - a negative discrimination can mean a
    wrong key, an ambiguous question, an unusual cohort or a data problem -
    and nothing here changes a key or a mark.
    """
    flags: list[QuestionFlag] = []
    for stat in stats:
        if stat.responses == 0:
            continue
        p_correct = stat.p_correct or 0.0
        p_blank = stat.p_blank or 0.0
        # Compared at the two decimals it is shown with, so a -0.004 that
        # reads as "-0.00" is not presented as a negative item.
        if (
            stat.discrimination is not None
            and round(stat.discrimination, 2) < settings.flag_negative_discrimination
        ):
            flags.append(
                QuestionFlag(
                    stat.number,
                    FlagKind.NEGATIVE_DISCRIMINATION,
                    f"Discrimination {stat.discrimination:+.2f}: stronger candidates "
                    "did worse on this question than weaker ones.",
                )
            )
        if p_correct < settings.flag_low_correct:
            flags.append(
                QuestionFlag(
                    stat.number, FlagKind.LOW_CORRECT, f"{p_correct * 100:.1f}% correct."
                )
            )
        elif p_correct >= settings.flag_high_correct:
            flags.append(
                QuestionFlag(
                    stat.number, FlagKind.HIGH_CORRECT, f"{p_correct * 100:.1f}% correct."
                )
            )
        if p_blank >= settings.flag_blank_rate:
            flags.append(
                QuestionFlag(stat.number, FlagKind.HIGH_BLANK, f"{p_blank * 100:.1f}% blank.")
            )
        if len(stat.key) > 1:
            flags.append(
                QuestionFlag(
                    stat.number,
                    FlagKind.KEY_VARIES,
                    "Results in this scope were marked with keys that disagree "
                    f"here ({', '.join(stat.key)}); recalculate the stale ones.",
                )
            )
        elif stat.key and stat.scripts >= settings.min_candidates_distractor_flags:
            chosen = dict(stat.option_counts)
            key_count = chosen.get(stat.key[0], 0)
            rival = max(
                ((label, count) for label, count in stat.option_counts if label != stat.key[0]),
                key=lambda pair: pair[1],
                default=None,
            )
            if rival is not None and rival[1] > key_count:
                flags.append(
                    QuestionFlag(
                        stat.number,
                        FlagKind.DISTRACTOR_OVER_KEY,
                        f"Option {rival[0]} was chosen by {rival[1]}, the key "
                        f"({stat.key[0]}) by {key_count}.",
                    )
                )
            # On a very easy question an unchosen distractor is expected, so
            # it is only worth saying when enough candidates got it wrong.
            unused = [
                label for label, count in stat.option_counts
                if count == 0 and label != stat.key[0]
            ]
            wrong = stat.incorrect + stat.multiple
            if unused and wrong >= settings.min_incorrect_unused_distractor:
                flags.append(
                    QuestionFlag(
                        stat.number,
                        FlagKind.UNUSED_DISTRACTOR,
                        f"No candidate chose {', '.join(unused)}.",
                    )
                )
    return tuple(flags)


# ----------------------------------------------------------------------
# Reliability
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Reliability:
    """KR-20 and the standard error of measurement, or why they are absent."""

    kr20: float | None
    sem: float | None
    """In questions-correct units, the scale KR-20 is computed on."""
    candidates: int
    items: int
    reason: str = ""
    """Why :attr:`kr20` or :attr:`sem` is unavailable; empty when both exist."""


def reliability(
    records: Sequence[ScriptRecord], settings: AnalyticsSettings = DEFAULT_SETTINGS
) -> Reliability:
    """KR-20 over the 0/1 correctness of one set's counted questions.

    ``KR20 = k/(k-1) x (1 - sum(p x q) / var(X))`` where ``p`` is each
    question's proportion correct, ``q = 1 - p``, and ``X`` is the number of
    those ``k`` questions a candidate answered correctly; the variances are
    population variances, as the formula defines them. A question withdrawn
    for any script in the scope is left out, so every candidate is measured
    on the same questions. KR-20 can be negative; it is reported as it is,
    and the SEM is then unavailable.
    """
    usable = [record for record in records if record.outcomes is not None]
    try:
        codes = _matrix(usable)
    except ValueError:
        return Reliability(None, None, len(usable), 0, "The scripts cover different papers.")
    candidates = int(codes.shape[0])
    if candidates < settings.min_candidates_reliability:
        return Reliability(
            None,
            None,
            candidates,
            int(codes.shape[1]) if codes.size else 0,
            f"Needs at least {settings.min_candidates_reliability} scored scripts "
            "with question detail.",
        )
    keep = ~(codes == _WITHDRAWN).any(axis=0)
    items = int(keep.sum())
    if items < settings.min_items_reliability:
        return Reliability(
            None,
            None,
            candidates,
            items,
            f"Needs at least {settings.min_items_reliability} questions that were not withdrawn.",
        )
    correct = (codes[:, keep] == _CORRECT).astype(np.float64)
    totals = correct.sum(axis=1)
    variance = float(totals.var())
    if variance == 0:
        return Reliability(
            None,
            None,
            candidates,
            items,
            "Every candidate answered the same number of questions correctly, "
            "so there is no score variance to explain.",
        )
    p = correct.mean(axis=0)
    kr20 = (items / (items - 1)) * (1.0 - float(np.sum(p * (1.0 - p))) / variance)
    if kr20 < 0:
        return Reliability(
            kr20,
            None,
            candidates,
            items,
            "KR-20 is negative, so the standard error of measurement is not defined.",
        )
    sem = math.sqrt(variance) * math.sqrt(max(0.0, 1.0 - kr20))
    return Reliability(kr20, sem, candidates, items)


# ----------------------------------------------------------------------
# Scopes and the whole examination
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScopeAnalytics:
    """Everything the dashboard shows for one scope.

    Attributes:
        key: :data:`OVERALL` or a set code.
        label: ``"Overall Exam"`` or ``"Set 10"``.
        count: Scored scripts in the scope.
        summary / histogram: ``None`` when :attr:`count` is zero.
        questions: Per-question statistics; empty when
            :attr:`question_note` says why.
        question_note: Why question statistics are unavailable, or empty.
        excluded_from_questions: Scored scripts whose breakdown could not be
            regenerated and so are absent from the question statistics.
    """

    key: str | None
    label: str
    count: int
    summary: ScoreSummary | None
    histogram: Histogram | None
    questions: tuple[QuestionStat, ...] = ()
    question_note: str = ""
    excluded_from_questions: int = 0
    reliability: Reliability | None = None
    flags: tuple[QuestionFlag, ...] = ()

    def question(self, number: int) -> QuestionStat | None:
        """One question's statistics, by printed number."""
        return next((stat for stat in self.questions if stat.number == number), None)

    @property
    def counted_questions(self) -> tuple[QuestionStat, ...]:
        """Questions with at least one counted response."""
        return tuple(stat for stat in self.questions if stat.responses)

    def difficulty_counts(self) -> dict[Difficulty, int]:
        """How many questions fall in each band, every band present."""
        counts = dict.fromkeys(Difficulty, 0)
        for stat in self.questions:
            if stat.difficulty is not None:
                counts[stat.difficulty] += 1
        return counts

    def easiest(self, limit: int = DEFAULT_SETTINGS.ranked_count) -> tuple[QuestionStat, ...]:
        """The highest proportion correct first; ties by question number."""
        ranked = sorted(
            self.counted_questions, key=lambda stat: (-(stat.p_correct or 0.0), stat.number)
        )
        return tuple(ranked[:limit])

    def hardest(self, limit: int = DEFAULT_SETTINGS.ranked_count) -> tuple[QuestionStat, ...]:
        """The lowest proportion correct first; ties by question number."""
        ranked = sorted(
            self.counted_questions, key=lambda stat: (stat.p_correct or 0.0, stat.number)
        )
        return tuple(ranked[:limit])


@dataclass(frozen=True, slots=True)
class SetComparisonRow:
    """One set's line in the Overall scope's comparison table."""

    set_code: str
    count: int
    summary: ScoreSummary | None
    reliability: Reliability | None


@dataclass(frozen=True, slots=True)
class ExamAnalytics:
    """The whole examination, every scope computed once.

    Built off the GUI thread by :func:`analyse`; switching scope on the
    dashboard is then a dictionary lookup.
    """

    scopes: tuple[ScopeAnalytics, ...]
    set_comparison: tuple[SetComparisonRow, ...]
    maximum_possible: float
    stale_count: int = 0
    excluded_count: int = 0
    labels: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def scope(self, key: str | None) -> ScopeAnalytics | None:
        """One scope by key."""
        return next((item for item in self.scopes if item.key == key), None)

    @property
    def overall(self) -> ScopeAnalytics:
        """The Overall Exam scope, which always exists."""
        found = self.scope(OVERALL)
        assert found is not None  # analyse() always builds it
        return found


def _scope(
    key: str | None,
    label: str,
    records: Sequence[ScriptRecord],
    labels: Sequence[str],
    maximum_possible: float,
    settings: AnalyticsSettings,
    *,
    question_note: str = "",
) -> ScopeAnalytics:
    """Compute one scope."""
    marks = [record.mark for record in records]
    summary = score_summary(marks)
    histogram = score_histogram(marks, maximum_possible, settings=settings)
    if not records:
        return ScopeAnalytics(key, label, 0, None, None, question_note=question_note)
    if question_note:
        return ScopeAnalytics(
            key, label, len(records), summary, histogram, question_note=question_note
        )
    excluded = sum(1 for record in records if record.outcomes is None)
    try:
        questions = question_statistics(records, labels, settings)
    except ValueError:
        return ScopeAnalytics(
            key,
            label,
            len(records),
            summary,
            histogram,
            question_note="The scripts in this scope cover different numbers of questions.",
        )
    note = (
        ""
        if questions
        else (
            "No scored script in this scope has a readable answer-key revision, "
            "so no question can be analysed."
        )
    )
    return ScopeAnalytics(
        key=key,
        label=label,
        count=len(records),
        summary=summary,
        histogram=histogram,
        questions=questions,
        question_note=note,
        excluded_from_questions=excluded,
        reliability=reliability(records, settings),
        flags=review_flags(questions, settings),
    )


def analyse(
    records: Sequence[ScriptRecord],
    *,
    set_codes: Sequence[str] = (),
    labels: Sequence[str] = (),
    maximum_possible: float | None = None,
    settings: AnalyticsSettings = DEFAULT_SETTINGS,
) -> ExamAnalytics:
    """Compute every scope of an examination.

    Args:
        records: The scored scripts, from :func:`build_records`.
        set_codes: The project's defined sets, in the operator's order. Every
            one becomes a scope even with no scored scripts, so the dashboard
            can say so; a set code found on a result but not defined is added
            after them.
        labels: The template's option labels, in order.
        maximum_possible: The full mark. ``None`` uses the highest mark
            observed (a histogram needs a scale).
        settings: Thresholds.
    """
    found = sorted({record.set_code for record in records if record.set_code})
    ordered = list(dict.fromkeys([*set_codes, *found]))
    top = (
        maximum_possible
        if maximum_possible is not None
        else max((float(record.mark) for record in records), default=0.0)
    )
    by_set = {
        code: [record for record in records if record.set_code == code] for code in ordered
    }
    populated = [code for code in ordered if by_set[code]]
    overall_note = ""
    if len(populated) > 1:
        overall_note = (
            "Question statistics are shown per set. Each set has its own answer "
            "key and OMRFlow records no mapping between one set's question "
            "numbers and another's, so question numbers are not combined across "
            "sets. Choose a set above."
        )
    scopes = [
        _scope(
            OVERALL, "Overall Exam", records, labels, top, settings, question_note=overall_note
        )
    ]
    comparison: list[SetComparisonRow] = []
    for code in ordered:
        scope = _scope(code, f"Set {code}", by_set[code], labels, top, settings)
        scopes.append(scope)
        comparison.append(SetComparisonRow(code, scope.count, scope.summary, scope.reliability))
    return ExamAnalytics(
        scopes=tuple(scopes),
        set_comparison=tuple(comparison),
        maximum_possible=top,
        stale_count=sum(1 for record in records if record.stale),
        excluded_count=sum(1 for record in records if record.outcomes is None),
        labels=tuple(labels),
    )


def maximum_mark(question_count: int, policy: ScoringPolicy) -> float:
    """The full mark of a paper under a policy: every question at its best."""
    return float(question_count * max(policy.correct_mark, policy.blank_mark))
