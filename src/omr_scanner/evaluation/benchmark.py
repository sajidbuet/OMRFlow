"""Comparing what recognition read against what was actually on the paper.

Purpose:
    Turn "it seems to work" into numbers: how many sheets, how many read
    correctly, which questions went wrong and *how* they went wrong - and, when
    a previous run is available, whether a change improved anything.

Responsibilities:
    * :func:`compare_result` - one result against one ground truth, producing
      a classified list of disagreements.
    * :func:`evaluate` - a whole dataset, producing summary metrics and every
      individual error.
    * :func:`write_report` - ``summary.json`` and ``errors.csv``.
    * :func:`compare_baseline` - this run against a stored one.

What does NOT belong here:
    * Any decision about whether a number is *good enough*. This module reports;
      a human judges. Thresholds that fail a build on a heuristic metric are how
      teams learn to ignore their own benchmarks.
    * Recognition. See the package docstring.

Why errors are categorised rather than counted:
    "94% accurate" says nothing actionable. Fifty false blanks mean the fill
    threshold is too high; fifty false marks mean it is too low; fifty wrong
    options mean the grid is misaligned by one column, which is a different bug
    entirely and would look identical in a single accuracy figure.

On confidence buckets:
    Accuracy is reported per confidence band because that is how a calibration
    is later checked. Until it *is* checked against real data, the band is a
    decision score and nothing more - it is not a probability, and this module
    deliberately does not describe it as one.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from omr_scanner.evaluation.ground_truth import MULTIPLE_SEPARATOR, SheetGroundTruth
from omr_scanner.services.recognition_models import (
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_LOGGER = logging.getLogger(__name__)

SUMMARY_FILENAME = "summary.json"
ERRORS_FILENAME = "errors.csv"

CONFIDENCE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("0.00-0.50", 0.0, 0.5),
    ("0.50-0.75", 0.5, 0.75),
    ("0.75-0.90", 0.75, 0.9),
    ("0.90-1.00", 0.9, 1.0001),
)
"""Bands the per-question accuracy is reported in. Coarse on purpose: finer
bands on a few hundred synthetic sheets would be noise with decimal places."""


class ErrorCategory(StrEnum):
    """How one disagreement between the engine and the truth is classified.

    Named for what a developer would *do* about them, which is why "the engine
    said B and the truth is C" and "the engine said B and the truth is blank"
    are different categories despite both being one wrong answer.
    """

    FALSE_MARK = "FALSE_MARK"
    """The truth is blank; the engine read a mark. Usually a threshold that is
    too low, or ink bleeding from a neighbour."""

    FALSE_BLANK = "FALSE_BLANK"
    """The truth is a mark; the engine read blank. Usually a threshold that is
    too high, or a faint mark."""

    WRONG_OPTION = "WRONG_OPTION"
    """Both are single marks, but different ones. Usually a geometry bug - one
    column out - rather than a threshold problem."""

    MISSED_MULTIPLE_MARK = "MISSED_MULTIPLE_MARK"
    """Two marks on the paper, one reported. The dangerous one: a candidate's
    second mark silently discarded."""

    FALSE_MULTIPLE_MARK = "FALSE_MULTIPLE_MARK"
    """One mark on the paper, two reported. Noisy, but safe - it is flagged."""

    ROLL_ERROR = "ROLL_ERROR"
    """The candidate identifier did not match."""

    SET_ERROR = "SET_ERROR"
    """The question-paper set code did not match."""

    ALIGNMENT_ERROR = "ALIGNMENT_ERROR"
    """The sheet should have registered and did not, or vice versa."""

    PROCESSING_FAILURE = "PROCESSING_FAILURE"
    """The scan could not be processed at all."""


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    """One disagreement, with enough context to go and look at it.

    Attributes:
        scan: The scan's file name.
        category: What kind of disagreement.
        question: Question number, or ``None`` for a sheet-level error.
        expected: What the ground truth says.
        actual: What the engine read.
        status: The engine's own status for that group or sheet - so a record
            can distinguish "wrong and confident" from "wrong and flagged",
            which are very different problems.
        confidence: The engine's decision score for that group, when there is
            one.
    """

    scan: str
    category: ErrorCategory
    question: int | None = None
    expected: str = ""
    actual: str = ""
    status: str = ""
    confidence: float = 0.0

    def as_row(self) -> dict[str, Any]:
        """Return this record as a flat CSV row."""
        return {
            "scan": self.scan,
            "category": self.category.value,
            "question": "" if self.question is None else self.question,
            "expected": self.expected,
            "actual": self.actual,
            "status": self.status,
            "confidence": f"{self.confidence:.4f}",
        }


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    """Dataset-level metrics.

    Every count is a count of *sheets* unless its name says questions. Rates
    are floats in ``[0, 1]`` and are ``0.0`` when their denominator is zero,
    never ``NaN``: a report that a spreadsheet cannot open helps nobody.
    """

    dataset: str = ""
    engine_version: str = ""
    scans: int = 0
    processed: int = 0
    failed: int = 0
    expected_failures: int = 0
    roll_checked: int = 0
    roll_correct: int = 0
    set_checked: int = 0
    set_correct: int = 0
    questions_checked: int = 0
    questions_correct: int = 0
    blanks_expected: int = 0
    blanks_correct: int = 0
    multiples_expected: int = 0
    multiples_correct: int = 0
    ambiguous_expected: int = 0
    ambiguous_handled: int = 0
    flagged_questions: int = 0
    error_counts: dict[str, int] = field(default_factory=dict)
    accuracy_by_confidence: dict[str, dict[str, float]] = field(default_factory=dict)
    total_seconds: float = 0.0
    mean_seconds_per_scan: float = 0.0

    @property
    def roll_accuracy(self) -> float:
        """Exact-match rate of the candidate identifier."""
        return _rate(self.roll_correct, self.roll_checked)

    @property
    def set_accuracy(self) -> float:
        """Exact-match rate of the set code."""
        return _rate(self.set_correct, self.set_checked)

    @property
    def question_accuracy(self) -> float:
        """Exact-match rate over every checked question."""
        return _rate(self.questions_correct, self.questions_checked)

    @property
    def blank_accuracy(self) -> float:
        """How often a genuinely blank question was read as blank."""
        return _rate(self.blanks_correct, self.blanks_expected)

    @property
    def multiple_accuracy(self) -> float:
        """How often a genuine double mark was read as both marks."""
        return _rate(self.multiples_correct, self.multiples_expected)

    @property
    def ambiguous_handled_rate(self) -> float:
        """How often a deliberately borderline mark was handled acceptably.

        "Acceptably" means the engine either read what was drawn, or declined
        to read it and said so. Confidently reading a *different* option is the
        only outcome that counts against it.
        """
        return _rate(self.ambiguous_handled, self.ambiguous_expected)

    def to_dict(self) -> dict[str, Any]:
        """Return the summary, including the derived rates, as plain data."""
        return {
            "dataset": self.dataset,
            "engine_version": self.engine_version,
            "scans": self.scans,
            "processed": self.processed,
            "failed": self.failed,
            "expected_failures": self.expected_failures,
            "roll_checked": self.roll_checked,
            "roll_correct": self.roll_correct,
            "roll_accuracy": self.roll_accuracy,
            "set_checked": self.set_checked,
            "set_correct": self.set_correct,
            "set_accuracy": self.set_accuracy,
            "questions_checked": self.questions_checked,
            "questions_correct": self.questions_correct,
            "question_accuracy": self.question_accuracy,
            "blanks_expected": self.blanks_expected,
            "blanks_correct": self.blanks_correct,
            "blank_accuracy": self.blank_accuracy,
            "multiples_expected": self.multiples_expected,
            "multiples_correct": self.multiples_correct,
            "multiple_accuracy": self.multiple_accuracy,
            "ambiguous_expected": self.ambiguous_expected,
            "ambiguous_handled": self.ambiguous_handled,
            "ambiguous_handled_rate": self.ambiguous_handled_rate,
            "flagged_questions": self.flagged_questions,
            "error_counts": dict(sorted(self.error_counts.items())),
            "accuracy_by_confidence": self.accuracy_by_confidence,
            "total_seconds": self.total_seconds,
            "mean_seconds_per_scan": self.mean_seconds_per_scan,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> BenchmarkSummary:
        """Rebuild a summary from a stored report, ignoring derived rates."""
        names = {member.name for member in field_names()}
        return cls(**{key: value for key, value in payload.items() if key in names})


def field_names() -> tuple[Any, ...]:
    """Return :class:`BenchmarkSummary`'s declared fields.

    A function rather than an inline ``dataclasses.fields`` call so that
    :meth:`BenchmarkSummary.from_dict` reads as prose and the import stays in
    one place.
    """
    from dataclasses import fields as dataclass_fields

    return dataclass_fields(BenchmarkSummary)


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Everything one benchmark run produced.

    Attributes:
        summary: The dataset-level metrics.
        errors: Every disagreement, in dataset order.
    """

    summary: BenchmarkSummary
    errors: tuple[ErrorRecord, ...] = ()

    @property
    def failing_scans(self) -> tuple[str, ...]:
        """Names of the scans with at least one disagreement, in order."""
        seen: dict[str, None] = {}
        for record in self.errors:
            seen.setdefault(record.scan, None)
        return tuple(seen)


def _rate(correct: int, total: int) -> float:
    """Return ``correct/total``, or ``0.0`` when nothing was checked."""
    return correct / total if total else 0.0


def compare_result(result: ScanResult, truth: SheetGroundTruth) -> list[ErrorRecord]:
    """Classify every disagreement between one result and its ground truth.

    Args:
        result: What the engine read.
        truth: What was on the paper.

    Returns:
        The disagreements, sheet-level first. An empty list means the engine
        read this sheet exactly right.
    """
    scan = truth.scan or result.source_path.name
    errors: list[ErrorRecord] = []

    registered = result.registration is not RegistrationStatus.FAILED
    if truth.expect_failure:
        # A sheet that was *meant* to be unreadable and was read anyway is a
        # finding too: it means the engine rectified a page it should have
        # refused, and whatever it reported came from somewhere.
        if registered:
            errors.append(
                ErrorRecord(
                    scan=scan,
                    category=ErrorCategory.ALIGNMENT_ERROR,
                    expected="registration_failed",
                    actual=result.registration.value,
                    status=result.outcome.value,
                )
            )
        return errors

    if result.outcome is RecognitionOutcome.ERROR:
        return [
            ErrorRecord(
                scan=scan,
                category=ErrorCategory.PROCESSING_FAILURE,
                expected="processed",
                actual=result.error_code or result.registration_message,
                status=result.outcome.value,
            )
        ]
    if not registered:
        return [
            ErrorRecord(
                scan=scan,
                category=ErrorCategory.ALIGNMENT_ERROR,
                expected="registered",
                actual=result.registration.value,
                status=result.outcome.value,
            )
        ]

    if truth.roll and result.identifier_value != truth.roll:
        errors.append(
            ErrorRecord(
                scan=scan,
                category=ErrorCategory.ROLL_ERROR,
                expected=truth.roll,
                actual=result.identifier_value,
                status=result.identifier.status if result.identifier else "",
            )
        )
    if truth.set_code and result.set_code_value != truth.set_code:
        errors.append(
            ErrorRecord(
                scan=scan,
                category=ErrorCategory.SET_ERROR,
                expected=truth.set_code,
                actual=result.set_code_value,
                status=result.set_code.status if result.set_code else "",
            )
        )

    for number, expected in sorted(truth.answers.items()):
        answer = result.answer(number)
        actual = answer.value if answer is not None else ""
        if actual == expected:
            continue
        if truth.is_ambiguous(number):
            # The mark was drawn deliberately borderline. Flagging it, or
            # declining to read it, is the behaviour this engine is supposed to
            # have. Only a *confident* reading of some other option is a
            # mistake worth recording.
            if answer is None or answer.needs_review or not actual:
                continue
            errors.append(
                ErrorRecord(
                    scan=scan,
                    category=ErrorCategory.WRONG_OPTION,
                    question=number,
                    expected=expected,
                    actual=actual,
                    status=answer.status,
                    confidence=answer.confidence,
                )
            )
            continue
        errors.append(
            ErrorRecord(
                scan=scan,
                category=_classify_answer(expected, actual),
                question=number,
                expected=expected,
                actual=actual,
                status=answer.status if answer is not None else "missing",
                confidence=answer.confidence if answer is not None else 0.0,
            )
        )
    return errors


def _classify_answer(expected: str, actual: str) -> ErrorCategory:
    """Name the kind of mistake one wrong answer represents."""
    expected_multiple = MULTIPLE_SEPARATOR in expected
    actual_multiple = MULTIPLE_SEPARATOR in actual

    if expected_multiple and not actual_multiple:
        return ErrorCategory.MISSED_MULTIPLE_MARK
    if actual_multiple and not expected_multiple:
        return ErrorCategory.FALSE_MULTIPLE_MARK
    if not expected and actual:
        return ErrorCategory.FALSE_MARK
    if expected and not actual:
        return ErrorCategory.FALSE_BLANK
    return ErrorCategory.WRONG_OPTION


def evaluate(
    results: Sequence[ScanResult],
    truths: Mapping[str, SheetGroundTruth],
    *,
    dataset: str = "",
) -> BenchmarkReport:
    """Compare a whole run against a whole dataset's ground truth.

    Args:
        results: What the engine produced, in any order.
        truths: Ground truth keyed by scan *stem* (the file name without its
            extension), as
            :func:`~omr_scanner.evaluation.ground_truth.load_ground_truth_directory`
            returns.
        dataset: Dataset name, recorded in the summary.

    Returns:
        The report. A result with no ground truth is skipped with a warning
        rather than counted as correct - silently scoring an unlabelled sheet
        as a pass is how a benchmark starts lying.
    """
    errors: list[ErrorRecord] = []
    counters = _Counters()
    engine_version = results[0].engine_version if results else ""

    for result in sorted(results, key=lambda item: item.source_path.name):
        truth = truths.get(result.source_path.stem)
        if truth is None:
            _LOGGER.warning(
                "No ground truth for %s; it is not counted", result.source_path.name
            )
            continue
        counters.observe(result, truth)
        errors.extend(compare_result(result, truth))

    counts: dict[str, int] = {}
    for record in errors:
        counts[record.category.value] = counts.get(record.category.value, 0) + 1

    summary = BenchmarkSummary(
        dataset=dataset,
        engine_version=engine_version,
        scans=counters.scans,
        processed=counters.processed,
        failed=counters.failed,
        expected_failures=counters.expected_failures,
        roll_checked=counters.roll_checked,
        roll_correct=counters.roll_correct,
        set_checked=counters.set_checked,
        set_correct=counters.set_correct,
        questions_checked=counters.questions_checked,
        questions_correct=counters.questions_correct,
        blanks_expected=counters.blanks_expected,
        blanks_correct=counters.blanks_correct,
        multiples_expected=counters.multiples_expected,
        multiples_correct=counters.multiples_correct,
        ambiguous_expected=counters.ambiguous_expected,
        ambiguous_handled=counters.ambiguous_handled,
        flagged_questions=counters.flagged_questions,
        error_counts=counts,
        accuracy_by_confidence=counters.confidence_table(),
        total_seconds=counters.total_seconds,
        mean_seconds_per_scan=_rate_float(counters.total_seconds, counters.scans),
    )
    return BenchmarkReport(summary=summary, errors=tuple(errors))


def _rate_float(total: float, count: int) -> float:
    """Return ``total/count``, or ``0.0`` when nothing was counted."""
    return total / count if count else 0.0


class _Counters:
    """Running totals while a dataset is walked.

    A small mutable object rather than a dozen local variables threaded through
    a loop: adding a metric is one line here instead of four in three places.
    """

    def __init__(self) -> None:
        self.scans = 0
        self.processed = 0
        self.failed = 0
        self.expected_failures = 0
        self.roll_checked = 0
        self.roll_correct = 0
        self.set_checked = 0
        self.set_correct = 0
        self.questions_checked = 0
        self.questions_correct = 0
        self.blanks_expected = 0
        self.blanks_correct = 0
        self.multiples_expected = 0
        self.multiples_correct = 0
        self.ambiguous_expected = 0
        self.ambiguous_handled = 0
        self.flagged_questions = 0
        self.total_seconds = 0.0
        self._bands: dict[str, list[int]] = {name: [0, 0] for name, _, _ in CONFIDENCE_BANDS}

    def observe(self, result: ScanResult, truth: SheetGroundTruth) -> None:
        """Fold one sheet into the totals."""
        self.scans += 1
        self.total_seconds += result.elapsed_seconds
        registered = result.registration is not RegistrationStatus.FAILED

        if truth.expect_failure:
            self.expected_failures += 1
            return
        if not registered or result.outcome is RecognitionOutcome.ERROR:
            self.failed += 1
            return
        self.processed += 1

        if truth.roll:
            self.roll_checked += 1
            self.roll_correct += result.identifier_value == truth.roll
        if truth.set_code:
            self.set_checked += 1
            self.set_correct += result.set_code_value == truth.set_code

        for number, expected in truth.answers.items():
            answer = result.answer(number)
            actual = answer.value if answer is not None else ""
            correct = actual == expected

            if truth.is_ambiguous(number):
                # Counted in their own column, never in the accuracy rate: a
                # borderline mark has no single right answer, and folding it
                # into "answer accuracy" would make that headline number depend
                # on how many deliberately-unfair questions the dataset
                # happens to contain.
                self.ambiguous_expected += 1
                self.ambiguous_handled += bool(
                    correct or (answer is not None and answer.needs_review) or not actual
                )
                continue

            self.questions_checked += 1
            self.questions_correct += correct
            if expected == "":
                self.blanks_expected += 1
                self.blanks_correct += correct
            if MULTIPLE_SEPARATOR in expected:
                self.multiples_expected += 1
                self.multiples_correct += correct
            if answer is not None:
                self.flagged_questions += answer.needs_review
                self._record_confidence(answer.confidence, correct=correct)

    def _record_confidence(self, confidence: float, *, correct: bool) -> None:
        """Put one question into its confidence band."""
        for name, low, high in CONFIDENCE_BANDS:
            if low <= confidence < high:
                bucket = self._bands[name]
                bucket[0] += 1
                bucket[1] += int(correct)
                return

    def confidence_table(self) -> dict[str, dict[str, float]]:
        """Return per-band counts and accuracy, omitting empty bands."""
        return {
            name: {
                "questions": float(counted),
                "correct": float(correct),
                "accuracy": _rate(correct, counted),
            }
            for name, (counted, correct) in self._bands.items()
            if counted
        }


def write_report(report: BenchmarkReport, directory: Path) -> tuple[Path, Path]:
    """Write ``summary.json`` and ``errors.csv`` into ``directory``.

    Returns:
        The two paths written, in that order.

    Both formats on purpose: the JSON is what a later run compares against, and
    the CSV is what a person sorts in a spreadsheet to find the twenty sheets
    worth looking at.
    """
    directory.mkdir(parents=True, exist_ok=True)
    summary_path = directory / SUMMARY_FILENAME
    errors_path = directory / ERRORS_FILENAME

    summary_path.write_text(
        json.dumps(report.summary.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    columns = ["scan", "category", "question", "expected", "actual", "status", "confidence"]
    with errors_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for record in report.errors:
            writer.writerow(record.as_row())

    _LOGGER.info(
        "Benchmark report written: %d error(s) over %d scan(s) -> %s",
        len(report.errors),
        report.summary.scans,
        directory,
    )
    return summary_path, errors_path


def load_summary(path: Path) -> BenchmarkSummary:
    """Read a stored ``summary.json`` back into a summary."""
    return BenchmarkSummary.from_dict(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True, slots=True)
class MetricComparison:
    """One metric, before and after.

    Attributes:
        metric: The metric's name.
        baseline: Its value in the stored run.
        current: Its value now.
        verdict: ``improved``, ``unchanged`` or ``regressed``.
        tolerance: The band inside which a change counts as unchanged.
    """

    metric: str
    baseline: float
    current: float
    verdict: str
    tolerance: float

    @property
    def delta(self) -> float:
        """How much the metric moved."""
        return self.current - self.baseline


COMPARED_METRICS: tuple[str, ...] = (
    "question_accuracy",
    "roll_accuracy",
    "set_accuracy",
    "blank_accuracy",
    "multiple_accuracy",
)
"""Which metrics a baseline comparison reports on."""

DEFAULT_TOLERANCE = 0.005
"""How far a rate may move before it is called a change.

Half a percentage point. Deliberately not zero: these datasets are small enough
that one sheet moves a rate by more than that, and a comparison that shouts
about every run would be ignored within a week."""


def compare_baseline(
    baseline: BenchmarkSummary,
    current: BenchmarkSummary,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[MetricComparison, ...]:
    """Say, metric by metric, whether this run improved on a stored one.

    Args:
        baseline: The stored run.
        current: This run.
        tolerance: How far a rate may move before it counts as a change.

    Returns:
        One comparison per metric in :data:`COMPARED_METRICS`.

    This function reports; it does not fail anything. Whether a regression on a
    synthetic dataset should block a change is a judgement that depends on what
    changed, and encoding it here would make the answer look objective when it
    is not.
    """
    comparisons: list[MetricComparison] = []
    for metric in COMPARED_METRICS:
        before = float(getattr(baseline, metric))
        after = float(getattr(current, metric))
        if after > before + tolerance:
            verdict = "improved"
        elif after < before - tolerance:
            verdict = "regressed"
        else:
            verdict = "unchanged"
        comparisons.append(
            MetricComparison(
                metric=metric,
                baseline=before,
                current=after,
                verdict=verdict,
                tolerance=tolerance,
            )
        )
    return tuple(comparisons)


__all__ = [
    "COMPARED_METRICS",
    "CONFIDENCE_BANDS",
    "DEFAULT_TOLERANCE",
    "ERRORS_FILENAME",
    "SUMMARY_FILENAME",
    "BenchmarkReport",
    "BenchmarkSummary",
    "ErrorCategory",
    "ErrorRecord",
    "MetricComparison",
    "compare_baseline",
    "compare_result",
    "evaluate",
    "load_summary",
    "write_report",
]
