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
from omr_scanner.recognition.models import BLANK_CHARACTER, UNRESOLVED_CHARACTER
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
SUMMARY_CSV_FILENAME = "summary.csv"
CATEGORY_FILENAME = "category_metrics.csv"
RUN_CONFIG_FILENAME = "run_config.json"

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

    ROLL_AMBIGUITY_MISSED = "ROLL_AMBIGUITY_MISSED"
    """The identifier contained a deliberately unreadable column and the engine
    reported a confident value anyway.

    Distinct from a plain identifier error because the fix is different: the
    engine did not read the wrong digit, it failed to *notice* that it could
    not read one, and a script silently filed under a confidently wrong
    identifier is worse than one held for review."""

    SET_ERROR = "SET_ERROR"
    """The question-paper set code did not match."""

    SET_AMBIGUITY_MISSED = "SET_AMBIGUITY_MISSED"
    """As :attr:`ROLL_AMBIGUITY_MISSED`, for the set code - which decides which
    answer key a script is marked against."""

    ALIGNMENT_ERROR = "ALIGNMENT_ERROR"
    """The sheet should have registered and did not, or vice versa."""

    ORIENTATION_ERROR = "ORIENTATION_ERROR"
    """The page registered, but upside down or in the wrong quarter turn.

    Recorded separately because it produces an entire sheet of plausible
    nonsense rather than an obvious failure."""

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
    dataset_path: str = ""
    scans: int = 0
    processed: int = 0
    failed: int = 0
    expected_failures: int = 0
    sheets_correct: int = 0
    registration_expected: int = 0
    registration_succeeded: int = 0
    duplicate_groups_expected: int = 0
    duplicate_sheets_expected: int = 0
    duplicate_groups_detected: int = 0
    duplicate_groups_missed: int = 0
    duplicate_groups_false: int = 0
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
    def sheet_accuracy(self) -> float:
        """How often an entire sheet was read with no disagreement at all.

        The strictest metric here, and the one closest to what a user
        experiences: one wrong bubble anywhere means a script somebody has to
        look at.
        """
        return _rate(self.sheets_correct, self.scans - self.expected_failures)

    @property
    def registration_rate(self) -> float:
        """How often a sheet that was *supposed* to register did so.

        Sheets generated as deliberate failures are excluded from the
        denominator: counting them would make the rate depend on how many
        unreadable pages the dataset chose to include.
        """
        return _rate(self.registration_succeeded, self.registration_expected)

    @property
    def duplicate_detection_rate(self) -> float:
        """How many of the planted duplicate-identifier groups were found.

        A batch-level property, deliberately kept out of the recognition rates:
        detecting duplicates is the job of the batch layer, and folding it into
        answer accuracy would hide a regression in either one.
        """
        return _rate(self.duplicate_groups_detected, self.duplicate_groups_expected)

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
            "dataset_path": self.dataset_path,
            "engine_version": self.engine_version,
            "scans": self.scans,
            "processed": self.processed,
            "failed": self.failed,
            "expected_failures": self.expected_failures,
            "sheets_correct": self.sheets_correct,
            "sheet_accuracy": self.sheet_accuracy,
            "registration_expected": self.registration_expected,
            "registration_succeeded": self.registration_succeeded,
            "registration_rate": self.registration_rate,
            "duplicate_groups_expected": self.duplicate_groups_expected,
            "duplicate_sheets_expected": self.duplicate_sheets_expected,
            "duplicate_groups_detected": self.duplicate_groups_detected,
            "duplicate_groups_missed": self.duplicate_groups_missed,
            "duplicate_groups_false": self.duplicate_groups_false,
            "duplicate_detection_rate": self.duplicate_detection_rate,
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
class CategoryMetrics:
    """How one *kind* of test case fared.

    The reason the generator tags its sheets at all. A dataset that is 98%
    correct overall may be 100% correct on everything except ticks, and the
    single figure is worse than useless for deciding what to work on next.

    Attributes:
        tag: The test-case tag, as the ground truth spells it.
        sheets: Sheets carrying this tag.
        sheets_correct: Of those, how many had no disagreement at all.
        questions_checked: Unambiguous questions on those sheets.
        questions_correct: Of those, how many read correctly.
        registration_failures: Sheets that did not register when they should
            have.
        errors: Total disagreements recorded on those sheets.
        expected_failures: Sheets tagged as deliberate failures, excluded from
            the rates.
    """

    tag: str
    sheets: int = 0
    sheets_correct: int = 0
    questions_checked: int = 0
    questions_correct: int = 0
    registration_failures: int = 0
    errors: int = 0
    expected_failures: int = 0

    @property
    def scored(self) -> int:
        """Sheets of this kind that have an accuracy at all.

        A category made entirely of deliberate failures - a page cropped in
        half, say - has none: there is nothing to read, and the only question
        is whether the engine refused it, which the error count answers.
        """
        return self.sheets - self.expected_failures

    @property
    def sheet_accuracy(self) -> float:
        """How often a sheet of this kind was read with no disagreement.

        ``0.0`` when nothing was scored; check :attr:`scored` before showing
        this as a percentage, or a category that behaved perfectly will be
        displayed as a total failure.
        """
        return _rate(self.sheets_correct, self.scored)

    @property
    def question_accuracy(self) -> float:
        """How often a question on a sheet of this kind read correctly."""
        return _rate(self.questions_correct, self.questions_checked)

    def as_row(self) -> dict[str, Any]:
        """Return this category as a flat CSV row."""
        return {
            "tag": self.tag,
            "sheets": self.sheets,
            "scored": self.scored,
            "sheets_correct": self.sheets_correct,
            "sheet_accuracy": f"{self.sheet_accuracy:.4f}" if self.scored else "",
            "questions_checked": self.questions_checked,
            "questions_correct": self.questions_correct,
            "question_accuracy": f"{self.question_accuracy:.4f}",
            "registration_failures": self.registration_failures,
            "errors": self.errors,
            "expected_failures": self.expected_failures,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return this category, including its rates, as plain data."""
        return {
            "tag": self.tag,
            "sheets": self.sheets,
            "scored": self.scored,
            "sheets_correct": self.sheets_correct,
            "sheet_accuracy": self.sheet_accuracy,
            "questions_checked": self.questions_checked,
            "questions_correct": self.questions_correct,
            "question_accuracy": self.question_accuracy,
            "registration_failures": self.registration_failures,
            "errors": self.errors,
            "expected_failures": self.expected_failures,
        }


CATEGORY_COLUMNS: tuple[str, ...] = (
    "tag",
    "sheets",
    "scored",
    "sheets_correct",
    "sheet_accuracy",
    "questions_checked",
    "questions_correct",
    "question_accuracy",
    "registration_failures",
    "errors",
    "expected_failures",
)
"""Columns of ``category_metrics.csv``."""


@dataclass(frozen=True, slots=True)
class BenchmarkRunConfig:
    """What produced a benchmark result, recorded so it can be reproduced.

    A number without its conditions is not a measurement. Two runs that differ
    only in a fill threshold are comparable; two that differ in the dataset,
    the template or the engine version are not, and without this record there
    is no way to tell which case you are looking at.

    Attributes:
        dataset: Dataset name.
        dataset_path: Where it was read from.
        template: Template name.
        template_path: Where the template came from.
        engine_name: Engine that produced the results.
        engine_version: Its version.
        worker_count: How many processes the batch used.
        settings: The recognition settings in force, as plain data.
        generator: The dataset manifest's generator record, when known - seed,
            profile, DPI and format, which is what makes a dataset repeatable.
        started_at: ISO-8601 UTC timestamp.
        notes: Free text.
    """

    dataset: str = ""
    dataset_path: str = ""
    template: str = ""
    template_path: str = ""
    engine_name: str = ""
    engine_version: str = ""
    worker_count: int = 0
    settings: dict[str, Any] = field(default_factory=dict)
    generator: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the configuration as plain data."""
        return {
            "dataset": self.dataset,
            "dataset_path": self.dataset_path,
            "template": self.template,
            "template_path": self.template_path,
            "engine_name": self.engine_name,
            "engine_version": self.engine_version,
            "worker_count": self.worker_count,
            "settings": dict(self.settings),
            "generator": dict(self.generator),
            "started_at": self.started_at,
            "notes": self.notes,
            "disclaimer": SYNTHETIC_DISCLAIMER,
        }


SYNTHETIC_DISCLAIMER = (
    "Synthetic datasets measure regression consistency and controlled "
    "edge-case handling. They do not establish real-world recognition accuracy."
)
"""Written into every run configuration.

Not decoration. A summary.json with a 0.999 in it will eventually be pasted
into a slide, and the file itself should say what it does and does not mean."""


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Everything one benchmark run produced.

    Attributes:
        summary: The dataset-level metrics.
        errors: Every disagreement, in dataset order.
        categories: Per-test-case-kind metrics, worst accuracy first.
        config: What produced this run.
    """

    summary: BenchmarkSummary
    errors: tuple[ErrorRecord, ...] = ()
    categories: tuple[CategoryMetrics, ...] = ()
    config: BenchmarkRunConfig = field(default_factory=BenchmarkRunConfig)

    @property
    def failing_scans(self) -> tuple[str, ...]:
        """Names of the scans with at least one disagreement, in order."""
        seen: dict[str, None] = {}
        for record in self.errors:
            seen.setdefault(record.scan, None)
        return tuple(seen)

    def errors_for(self, scan: str) -> tuple[ErrorRecord, ...]:
        """Every disagreement recorded against one scan."""
        return tuple(record for record in self.errors if record.scan == scan)

    def with_config(self, config: BenchmarkRunConfig) -> BenchmarkReport:
        """Return this report with its run configuration attached.

        Separate from :func:`evaluate` because the conditions of a run - which
        template, how many workers, which settings - are known to the caller
        that *ran* it, not to the comparison that scores it.
        """
        from dataclasses import replace as _replace

        return _replace(self, config=config)


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
                category=(
                    ErrorCategory.ORIENTATION_ERROR
                    if ORIENTATION_STATUS in result.status_codes
                    else ErrorCategory.ALIGNMENT_ERROR
                ),
                expected="registered",
                actual=result.registration.value,
                status=result.outcome.value,
            )
        ]

    roll_verdict = grid_verdict(
        truth.roll, result.identifier_value, truth.roll_marks, ambiguous=truth.roll_ambiguous
    )
    if roll_verdict is not None:
        errors.append(
            ErrorRecord(
                scan=scan,
                category=roll_verdict,
                expected=truth.roll,
                actual=result.identifier_value,
                status=result.identifier.status if result.identifier else "",
            )
        )
    set_verdict = grid_verdict(
        truth.set_code,
        result.set_code_value,
        truth.set_marks,
        ambiguous=truth.set_ambiguous,
        mismatch=ErrorCategory.SET_ERROR,
        ambiguity=ErrorCategory.SET_AMBIGUITY_MISSED,
    )
    if set_verdict is not None:
        errors.append(
            ErrorRecord(
                scan=scan,
                category=set_verdict,
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


ORIENTATION_STATUS = "ORIENTATION_FAILED"
"""The engine's own status code for a page whose orientation could not be
resolved. Matched as a string so this module keeps working if the code list
grows."""


def grid_verdict(
    expected: str,
    actual: str,
    marks: Sequence[Sequence[str]] = (),
    *,
    ambiguous: bool = False,
    mismatch: ErrorCategory = ErrorCategory.ROLL_ERROR,
    ambiguity: ErrorCategory = ErrorCategory.ROLL_AMBIGUITY_MISSED,
) -> ErrorCategory | None:
    """Judge one grid field - an identifier or a set code - against its truth.

    Args:
        expected: The truth, in the engine's own convention: the symbol per
            column, ``"_"`` for a blank column, ``"?"`` for one that cannot
            resolve.
        actual: What the engine read.
        marks: What was actually drawn in each column, when the ground truth
            records it. Used only for the ambiguous columns.
        ambiguous: The field contains at least one deliberately borderline
            column.
        mismatch: Category for a plain disagreement.
        ambiguity: Category for a borderline column resolved confidently to
            something that was never drawn.

    Returns:
        ``None`` when the reading is acceptable, otherwise the category.

    An unambiguous field is a string comparison. An ambiguous one is judged
    column by column, and a borderline column may legitimately come back as the
    symbol that was faintly drawn, as ``"_"`` or as ``"?"`` - the engine is
    allowed to resolve a faint mark *or* to decline it. Only a confident third
    answer is wrong, and it gets its own category because the failure is "did
    not notice it was guessing" rather than "misread a digit".
    """
    if not expected:
        return None
    if not ambiguous:
        return None if actual == expected else mismatch
    if len(actual) != len(expected):
        return mismatch

    verdict: ErrorCategory | None = None
    for position, (want, got) in enumerate(zip(expected, actual, strict=True)):
        if want != UNRESOLVED_CHARACTER:
            if got != want:
                return mismatch
            continue
        drawn = set(marks[position]) if position < len(marks) else set()
        if got in {UNRESOLVED_CHARACTER, BLANK_CHARACTER} or got in drawn:
            continue
        verdict = ambiguity
    return verdict


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
    dataset_path: str = "",
) -> BenchmarkReport:
    """Compare a whole run against a whole dataset's ground truth.

    Args:
        results: What the engine produced, in any order.
        truths: Ground truth keyed by scan *stem* (the file name without its
            extension), as
            :func:`~omr_scanner.evaluation.ground_truth.load_ground_truth_directory`
            returns.
        dataset: Dataset name, recorded in the summary.
        dataset_path: Where the dataset came from, recorded in the summary so a
            stored report can be traced back to the images it scored.

    Returns:
        The report. A result with no ground truth is skipped with a warning
        rather than counted as correct - silently scoring an unlabelled sheet
        as a pass is how a benchmark starts lying.
    """
    errors: list[ErrorRecord] = []
    counters = _Counters()
    engine_version = results[0].engine_version if results else ""
    matched: list[tuple[ScanResult, SheetGroundTruth]] = []

    for result in sorted(results, key=lambda item: item.source_path.name):
        truth = truths.get(result.source_path.stem)
        if truth is None:
            _LOGGER.warning(
                "No ground truth for %s; it is not counted", result.source_path.name
            )
            continue
        sheet_errors = compare_result(result, truth)
        counters.observe(result, truth, sheet_errors)
        errors.extend(sheet_errors)
        matched.append((result, truth))

    counts: dict[str, int] = {}
    for record in errors:
        counts[record.category.value] = counts.get(record.category.value, 0) + 1

    duplicates = _duplicate_metrics(matched)

    summary = BenchmarkSummary(
        dataset=dataset,
        dataset_path=dataset_path,
        engine_version=engine_version,
        scans=counters.scans,
        sheets_correct=counters.sheets_correct,
        registration_expected=counters.registration_expected,
        registration_succeeded=counters.registration_succeeded,
        duplicate_groups_expected=duplicates[0],
        duplicate_sheets_expected=duplicates[1],
        duplicate_groups_detected=duplicates[2],
        duplicate_groups_missed=duplicates[3],
        duplicate_groups_false=duplicates[4],
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
    return BenchmarkReport(
        summary=summary, errors=tuple(errors), categories=counters.categories()
    )


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
        self.sheets_correct = 0
        self.registration_expected = 0
        self.registration_succeeded = 0
        self.total_seconds = 0.0
        self._bands: dict[str, list[int]] = {name: [0, 0] for name, _, _ in CONFIDENCE_BANDS}
        self._categories: dict[str, list[int]] = {}

    def observe(
        self,
        result: ScanResult,
        truth: SheetGroundTruth,
        errors: Sequence[ErrorRecord] = (),
    ) -> None:
        """Fold one sheet into the totals.

        Args:
            result: What the engine read.
            truth: What was on the paper.
            errors: The disagreements :func:`compare_result` already found, so
                the sheet-level and per-category counts agree with the error
                list by construction rather than by two separate judgements.
        """
        self.scans += 1
        self.total_seconds += result.elapsed_seconds
        registered = result.registration is not RegistrationStatus.FAILED
        before = (self.questions_checked, self.questions_correct)

        if truth.expect_failure:
            self.expected_failures += 1
            self._fold_categories(truth, errors, before, registered=registered)
            return

        self.registration_expected += 1
        self.registration_succeeded += int(registered)
        if not registered or result.outcome is RecognitionOutcome.ERROR:
            self.failed += 1
            self.sheets_correct += int(not errors)
            self._fold_categories(truth, errors, before, registered=registered)
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

        self.sheets_correct += int(not errors)
        self._fold_categories(truth, errors, before, registered=True)

    def _fold_categories(
        self,
        truth: SheetGroundTruth,
        errors: Sequence[ErrorRecord],
        before: tuple[int, int],
        *,
        registered: bool,
    ) -> None:
        """Attribute one sheet to every test-case tag it carries.

        A sheet counts in full towards each of its tags rather than being
        divided between them. A sheet tagged both ``BLUR`` and ``FAINT_MARK``
        is a complete test of each, and splitting it would make both categories
        look like fractions of a sheet.
        """
        checked = self.questions_checked - before[0]
        correct = self.questions_correct - before[1]
        tags = truth.tags or (UNTAGGED,)
        for tag in tags:
            bucket = self._categories.setdefault(tag, [0, 0, 0, 0, 0, 0, 0])
            bucket[0] += 1
            bucket[1] += int(not errors)
            bucket[2] += checked
            bucket[3] += correct
            bucket[4] += int(not registered and not truth.expect_failure)
            bucket[5] += len(errors)
            bucket[6] += int(truth.expect_failure)

    def categories(self) -> tuple[CategoryMetrics, ...]:
        """Return the per-tag metrics, least accurate first.

        Ordered by sheet accuracy because the first row of that table is the
        thing worth looking at; alphabetical order would bury it. Categories
        with nothing to score - those made entirely of deliberate failures -
        sort to the end instead of to the top, where their ``0.0`` would read
        as the worst result in the dataset.
        """
        metrics = [
            CategoryMetrics(
                tag=tag,
                sheets=bucket[0],
                sheets_correct=bucket[1],
                questions_checked=bucket[2],
                questions_correct=bucket[3],
                registration_failures=bucket[4],
                errors=bucket[5],
                expected_failures=bucket[6],
            )
            for tag, bucket in self._categories.items()
        ]
        metrics.sort(key=lambda item: (item.scored == 0, item.sheet_accuracy, item.tag))
        return tuple(metrics)

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


UNTAGGED = "UNTAGGED"
"""Category for a sheet whose ground truth carries no tags - a real dataset,
or one produced before tagging existed. Present so those sheets appear in the
category table rather than silently vanishing from it."""


def _duplicate_metrics(
    matched: Sequence[tuple[ScanResult, SheetGroundTruth]],
) -> tuple[int, int, int, int, int]:
    """Score the planted duplicate-identifier groups.

    Returns:
        ``(groups expected, sheets in them, detected, missed, false)``.

    A group counts as *detected* when every sheet the generator put in it came
    back carrying the same identifier, so that the batch layer would see them
    as one collision. A *false* group is a set of sheets that share a
    recognised identifier without having been planted together - which is a
    recognition error showing up as a spurious duplicate, and the most
    confusing failure of the two to meet in production.

    Deliberately reported on its own, never folded into identifier accuracy:
    finding duplicates is the batch layer's responsibility and a change there
    should not move a recognition number.
    """
    planted: dict[str, set[str]] = {}
    for _result, truth in matched:
        # A sheet whose identifier was drawn deliberately unreadable is left
        # out of its group: the engine is *right* to decline it, so requiring
        # it to collide would score correct caution as a miss. The case is
        # still visible in the category table under its own tag.
        if truth.duplicate_group and not truth.roll_ambiguous:
            planted.setdefault(truth.duplicate_group, set()).add(truth.scan)
    expected = {group: scans for group, scans in planted.items() if len(scans) > 1}

    observed: dict[str, set[str]] = {}
    for result, truth in matched:
        value = result.identifier_value
        if not value or UNRESOLVED_CHARACTER in value or set(value) == {BLANK_CHARACTER}:
            # An identifier the engine could not read is not a duplicate; it is
            # a sheet somebody has to look at, and it is counted as such by the
            # identifier metrics.
            continue
        observed.setdefault(value, set()).add(truth.scan)
    collisions = {value: scans for value, scans in observed.items() if len(scans) > 1}

    detected = sum(
        1 for scans in expected.values() if any(scans <= found for found in collisions.values())
    )
    false = sum(
        1
        for scans in collisions.values()
        if not any(scans <= planted_scans for planted_scans in expected.values())
    )
    return (
        len(expected),
        sum(len(scans) for scans in expected.values()),
        detected,
        len(expected) - detected,
        false,
    )


def write_report(report: BenchmarkReport, directory: Path) -> tuple[Path, ...]:
    """Write a benchmark run's output files into ``directory``.

    Writes ``summary.json``, ``summary.csv``, ``errors.csv``,
    ``category_metrics.csv`` and ``run_config.json``.

    Returns:
        The paths written, in that order.

    Several formats on purpose: the JSON is what a later run compares against,
    the CSVs are what a person sorts in a spreadsheet to find the twenty sheets
    worth looking at, and the run configuration is what makes the numbers mean
    anything a month later.
    """
    directory.mkdir(parents=True, exist_ok=True)
    summary_path = directory / SUMMARY_FILENAME
    summary_csv_path = directory / SUMMARY_CSV_FILENAME
    errors_path = directory / ERRORS_FILENAME
    category_path = directory / CATEGORY_FILENAME
    config_path = directory / RUN_CONFIG_FILENAME

    payload = report.summary.to_dict()
    payload["categories"] = [category.to_dict() for category in report.categories]
    payload["disclaimer"] = SYNTHETIC_DISCLAIMER
    summary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    flat = report.summary.to_dict()
    with summary_csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["metric", "value"])
        for key, value in flat.items():
            if isinstance(value, dict):
                continue
            writer.writerow([key, value])

    columns = ["scan", "category", "question", "expected", "actual", "status", "confidence"]
    with errors_path.open("w", encoding="utf-8", newline="") as stream:
        error_writer = csv.DictWriter(stream, fieldnames=columns)
        error_writer.writeheader()
        for record in report.errors:
            error_writer.writerow(record.as_row())

    with category_path.open("w", encoding="utf-8", newline="") as stream:
        category_writer = csv.DictWriter(stream, fieldnames=list(CATEGORY_COLUMNS))
        category_writer.writeheader()
        for category in report.categories:
            category_writer.writerow(category.as_row())

    config_path.write_text(
        json.dumps(report.config.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    _LOGGER.info(
        "Benchmark report written: %d error(s) over %d scan(s) -> %s",
        len(report.errors),
        report.summary.scans,
        directory,
    )
    return summary_path, summary_csv_path, errors_path, category_path, config_path


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
    "sheet_accuracy",
    "question_accuracy",
    "roll_accuracy",
    "set_accuracy",
    "blank_accuracy",
    "multiple_accuracy",
    "registration_rate",
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


def compare_categories(
    baseline: Sequence[CategoryMetrics],
    current: Sequence[CategoryMetrics],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> tuple[MetricComparison, ...]:
    """Say which *kinds* of test case moved between two runs.

    Args:
        baseline: The stored run's categories.
        current: This run's.
        tolerance: How far a rate may move before it counts as a change.

    Returns:
        One comparison per tag present in either run, named
        ``"<TAG>.sheet_accuracy"``, worst first.

    A category that appears in only one of the two runs is reported against a
    baseline of zero rather than skipped: a tag that has just started failing,
    or a whole family that has silently stopped being generated, is exactly
    what a regression comparison exists to surface.
    """
    before = {item.tag: item.sheet_accuracy for item in baseline}
    after = {item.tag: item.sheet_accuracy for item in current}
    comparisons: list[MetricComparison] = []
    for tag in sorted(set(before) | set(after)):
        was = before.get(tag, 0.0)
        now = after.get(tag, 0.0)
        if now > was + tolerance:
            verdict = "improved"
        elif now < was - tolerance:
            verdict = "regressed"
        else:
            verdict = "unchanged"
        comparisons.append(
            MetricComparison(
                metric=f"{tag}.sheet_accuracy",
                baseline=was,
                current=now,
                verdict=verdict,
                tolerance=tolerance,
            )
        )
    comparisons.sort(key=lambda item: (item.delta, item.metric))
    return tuple(comparisons)


def load_categories(path: Path) -> tuple[CategoryMetrics, ...]:
    """Read the category metrics back out of a stored ``summary.json``.

    Returns an empty tuple for a report written before categories existed, so
    an old baseline still compares on its overall metrics instead of failing.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("categories") or ()
    names = {item.name for item in _category_fields()}
    return tuple(
        CategoryMetrics(**{key: value for key, value in row.items() if key in names})
        for row in rows
    )


def _category_fields() -> tuple[Any, ...]:
    """Return :class:`CategoryMetrics`'s declared fields."""
    from dataclasses import fields as dataclass_fields

    return dataclass_fields(CategoryMetrics)


__all__ = [
    "CATEGORY_COLUMNS",
    "CATEGORY_FILENAME",
    "COMPARED_METRICS",
    "CONFIDENCE_BANDS",
    "DEFAULT_TOLERANCE",
    "ERRORS_FILENAME",
    "ORIENTATION_STATUS",
    "RUN_CONFIG_FILENAME",
    "SUMMARY_CSV_FILENAME",
    "SUMMARY_FILENAME",
    "SYNTHETIC_DISCLAIMER",
    "UNTAGGED",
    "BenchmarkReport",
    "BenchmarkRunConfig",
    "BenchmarkSummary",
    "CategoryMetrics",
    "ErrorCategory",
    "ErrorRecord",
    "MetricComparison",
    "compare_baseline",
    "compare_categories",
    "compare_result",
    "evaluate",
    "grid_verdict",
    "load_categories",
    "load_summary",
    "write_report",
]
