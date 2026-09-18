"""Judging whether a template is safe to run a batch with (Phase 4).

Purpose:
    Turn a :class:`~omr_scanner.services.recognition_models.ScanResult` - the
    same thing the Scan page already produces - into an explicit verdict an
    operator can act on: is this template's registration, geometry and
    threshold behaviour trustworthy on the scans it was actually tested
    against.

Responsibilities:
    * :class:`CalibrationStatus` - the four-way verdict, and nothing finer.
    * :func:`evaluate_calibration` - one scan's verdict, from signals Phase 3
      already computes.
    * :func:`aggregate_calibration` - a sample's verdict, worst scan wins.
    * :func:`apply_calibration` / :func:`write_calibration_report` - recording
      a run onto the template and to disk.

What does NOT belong here:
    * Recognition, registration or measurement of any kind. Every signal this
      module reads - ``registration``, ``warnings``, ``bubbles[*].usable``,
      ``answers[*].needs_review`` - already exists on a
      :class:`~omr_scanner.services.recognition_models.ScanResult`. A
      calibration check that re-derived one of these from pixels would be the
      exact duplication ``docs/ARCHITECTURE.md`` forbids, and would risk
      disagreeing with the engine about its own result.
    * A synthetic recognition engine. This module never reads an image.

Why four statuses and not a score:
    A single number invites the one question this module must refuse to
    answer: "is 0.83 good enough?" Four named states, each backed by an
    explicit, documented rule, keep the judgement inspectable - an operator
    (or a test) can ask *which* rule fired, not just what the number came out
    to.

The fail-safe this module exists to state out loud:
    Registration failure already stops Phase 3 from producing any field or
    answer at all (:mod:`omr_scanner.services.recognition_service`) - there is
    nothing here to duplicate. What this module adds is the layer above that:
    even a sheet that *did* register can still be geometrically wrong (bubble
    windows landing off the page) or behaviourally wrong (thresholds that
    produce ambiguity everywhere), and a badly calibrated template must not
    walk away from either looking like a confident pass.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from omr_scanner.domain.template import CalibrationRecord, IgnoredFieldDefinition
from omr_scanner.services.recognition_models import (
    ENGINE_VERSION,
    RegistrationStatus,
    utc_timestamp,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.recognition_models import BubbleView, ScanResult

UNUSABLE_BUBBLE_FAILURE_FRACTION = 0.05
"""Fraction of a sheet's bubbles that must come back ``usable=False`` before
calibration is *failed* outright, rather than merely flagged.

``usable=False`` means the sampling window fell (partly or entirely) off the
rectified page or could not gather
:attr:`~omr_scanner.imaging.metrics.BubbleMetricsConfig.min_sample_pixels`
(:mod:`omr_scanner.imaging.metrics`) - the direct, already-computed evidence
that a bubble's *mapped position* is wrong, which is exactly the "mapped
bubble regions extend outside image bounds" condition Phase 4 exists to
catch. A well-registered sheet from a template whose geometry actually
matches the print produces zero of these; the fraction is not zero rather
than one bubble because a template may legitimately place a handful of
bubbles close to the page edge, and one edge-adjacent bubble losing its
annulus is not the same failure as geometry that no longer corresponds to the
scan at all."""

UNUSABLE_BUBBLE_REVIEW_FRACTION = 0.0
"""Above this fraction (and below :data:`UNUSABLE_BUBBLE_FAILURE_FRACTION`),
calibration is downgraded to *needs review* rather than passed outright: even
one unmeasurable bubble is worth a look, just not yet a reason to refuse the
template."""

SYSTEMATIC_AMBIGUITY_REVIEW_FRACTION = 0.30
"""Fraction of a sheet's *checked* groups (answers plus field character
positions) that must need review before calibration calls it *systematic*
ambiguity rather than a few genuinely uncertain marks.

Ambiguous marks are not, by themselves, a defect - a candidate's blank or
double-marked question is real data (see the module docstring and
``docs/scan_workflow.md``). What this threshold catches is the *pattern* named
explicitly as a critical-failure condition: "recognition produces systematic
ambiguity across the page", which on a genuinely well-registered sheet with
reasonable thresholds should be a small minority of questions, not a third of
them."""

NEAR_THRESHOLD_BAND = 0.05
"""How close a bubble's fill ratio must sit to the zone's own fill threshold
to count as "near threshold" in a quality summary.

A plain distance in fill-ratio units (the same ``[0, 1]`` scale
:attr:`~omr_scanner.domain.template.RecognitionSettings.fill_ratio_threshold`
is expressed in), not a statistical estimate: it answers "how many
measurements would flip if the threshold moved by five percentage points",
which is the concrete, testable question an operator turning the threshold
slider is actually asking. See :func:`_near_threshold_count`."""

GEOMETRY_WARNING_CODES: frozenset[str] = frozenset(
    {"LARGE_REPROJECTION_ERROR", "ASPECT_RATIO_DEVIATION"}
)
"""Which :class:`~omr_scanner.imaging.models.AlignmentWarning` codes indicate a
geometry problem serious enough to need a human look, rather than a cosmetic
observation.

Chosen from the *existing* warning vocabulary - see
``docs/IMAGE_PROCESSING.md`` and the ``AlignmentWarning`` docstrings - rather
than invented for Phase 4: a large reprojection residual or a marker
quadrilateral whose aspect ratio has drifted from the template's own page size
are both statements about the *transform*, and a transform that is off in
either way puts every bubble sampled through it in the wrong place. The
remaining warnings (``LOW_MARKER_SCORE``, ``MULTIPLE_CORNER_CANDIDATES``,
``MARKER_NEAR_IMAGE_EDGE``, ``LOW_ORIENTATION_CONFIDENCE``,
``ORIENTATION_ASSUMED``) describe a marginal but still-correct registration
and are reported as ordinary warnings instead."""


class CalibrationStatus(StrEnum):
    """A template's verdict against one scan, or a whole sample.

    Deliberately four states and not a score - see the module docstring.
    Ordered worst-first in :data:`_SEVERITY` so that combining several
    findings, or several scans, is "take the worst", never an average that
    could hide the one real problem behind several clean results.
    """

    PASSED = "passed"
    """Registered cleanly, no geometry problem, no systematic ambiguity."""

    PASSED_WITH_WARNINGS = "passed_with_warnings"
    """Usable, but something is worth a look: a marginal registration
    warning, an isolated unusable bubble, ordinary review-worthy marks below
    the systematic-ambiguity threshold."""

    NEEDS_REVIEW = "needs_review"
    """An operator should look before trusting this template on a batch: a
    geometry-class alignment warning, a meaningful fraction of unusable
    bubbles, or ambiguity that looks systematic rather than incidental."""

    FAILED = "failed"
    """The sheet did not register, or so many bubble windows landed off the
    page that geometry cannot be trusted at all. No batch should be run
    against a template in this state."""

    @property
    def passed(self) -> bool:
        """Whether this status permits proceeding without a second look."""
        return self is CalibrationStatus.PASSED

    @property
    def label(self) -> str:
        """A short label for the status, as shown in the calibration UI."""
        return _STATUS_LABELS[self]


_STATUS_LABELS: dict[CalibrationStatus, str] = {
    CalibrationStatus.PASSED: "Validation passed",
    CalibrationStatus.PASSED_WITH_WARNINGS: "Validation passed with warnings",
    CalibrationStatus.NEEDS_REVIEW: "Needs review",
    CalibrationStatus.FAILED: "Calibration failed",
}

_SEVERITY: dict[CalibrationStatus, int] = {
    CalibrationStatus.PASSED: 0,
    CalibrationStatus.PASSED_WITH_WARNINGS: 1,
    CalibrationStatus.NEEDS_REVIEW: 2,
    CalibrationStatus.FAILED: 3,
}


def _worse(current: CalibrationStatus, candidate: CalibrationStatus) -> CalibrationStatus:
    """Return whichever of two statuses is more severe."""
    return candidate if _SEVERITY[candidate] > _SEVERITY[current] else current


@dataclass(frozen=True, slots=True)
class CalibrationFinding:
    """One explicit reason behind a calibration verdict.

    Attributes:
        severity: How much this finding, on its own, should worry an operator.
        code: A short, stable machine-readable name (``"REGISTRATION_FAILED"``,
            ``"UNUSABLE_BUBBLES"``, ``"SYSTEMATIC_AMBIGUITY"``,
            ``"GEOMETRY_WARNING"``, ...), for a test to assert against without
            parsing the sentence.
        message: The diagnostic sentence an operator reads - specific about
            what was measured, per the "bad vs better" examples in
            ``docs/calibration_workflow.md``, never just "template error".
    """

    severity: CalibrationStatus
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """One scan's calibration verdict, with the counts behind it.

    Everything here is read from an already-produced
    :class:`~omr_scanner.services.recognition_models.ScanResult`; nothing is
    computed from pixels.

    Attributes:
        scan: The scan's file name.
        status: The verdict.
        findings: Every explicit reason behind it, worst first.
        registered: Whether the sheet registered at all.
        markers_detected: Registration markers found (0 or 4 - Phase 1 either
            finds all four or fails outright, so a partial count never arises;
            see :mod:`omr_scanner.imaging.alignment`).
        orientation_ok: Whether the orientation mark was actually found, as
            opposed to a fallback being assumed.
        bubbles_total: Bubbles measured.
        bubbles_unusable: Of those, how many the sampler could not measure at
            all.
        near_threshold_count: Bubbles whose fill ratio sits within
            :data:`NEAR_THRESHOLD_BAND` of their zone's fill threshold.
        answers_total: Questions checked.
        answers_blank: Genuinely blank answers.
        answers_multiple: Double-marked answers.
        answers_needing_review: Answers the engine flagged for a human,
            whatever the reason.
        fields_needing_review: Non-question fields (identifier, set code, ...)
            flagged for a human.
        identifier_value: The recognised candidate identifier, as a plain
            string, for the per-scan summary table - never validated against
            anything external here.
        set_code_value: The recognised set code, likewise.
    """

    scan: str
    status: CalibrationStatus
    findings: tuple[CalibrationFinding, ...] = ()
    registered: bool = False
    markers_detected: int = 0
    orientation_ok: bool = False
    bubbles_total: int = 0
    bubbles_unusable: int = 0
    near_threshold_count: int = 0
    answers_total: int = 0
    answers_blank: int = 0
    answers_multiple: int = 0
    answers_needing_review: int = 0
    fields_needing_review: int = 0
    identifier_value: str = ""
    set_code_value: str = ""

    @property
    def ambiguous_count(self) -> int:
        """Answers or fields flagged for review.

        The "Ambiguous" column of the per-scan summary table
        (``docs/calibration_workflow.md``).
        """
        return self.answers_needing_review + self.fields_needing_review

    def to_dict(self) -> dict[str, Any]:
        """Return this report as JSON-safe plain data."""
        return {
            "scan": self.scan,
            "status": self.status.value,
            "findings": [
                {"severity": item.severity.value, "code": item.code, "message": item.message}
                for item in self.findings
            ],
            "registered": self.registered,
            "markers_detected": self.markers_detected,
            "orientation_ok": self.orientation_ok,
            "bubbles_total": self.bubbles_total,
            "bubbles_unusable": self.bubbles_unusable,
            "near_threshold_count": self.near_threshold_count,
            "answers_total": self.answers_total,
            "answers_blank": self.answers_blank,
            "answers_multiple": self.answers_multiple,
            "answers_needing_review": self.answers_needing_review,
            "fields_needing_review": self.fields_needing_review,
            "identifier_value": self.identifier_value,
            "set_code_value": self.set_code_value,
        }


@dataclass(frozen=True, slots=True)
class CalibrationSampleReport:
    """The aggregate verdict over several representative scans.

    One perfect scan proves little (``docs/calibration_workflow.md``
    "Representative samples"); this is what lets an operator ask the question
    that actually matters - "across everything I tested, is this template
    safe" - without re-reading every individual report.

    Attributes:
        reports: Every scan's report, in the order they were tested.
        status: The worst status among :attr:`reports`; ``PASSED`` only when
            every scan passed cleanly.
    """

    reports: tuple[CalibrationReport, ...] = ()
    status: CalibrationStatus = CalibrationStatus.PASSED

    @property
    def scan_count(self) -> int:
        """How many scans this sample covers."""
        return len(self.reports)

    @property
    def registered_count(self) -> int:
        """How many scans registered successfully."""
        return sum(item.registered for item in self.reports)

    @property
    def marker_failure_count(self) -> int:
        """How many scans failed to register at all (0 of 4 markers usable)."""
        return sum(not item.registered for item in self.reports)

    @property
    def scans_with_ambiguity(self) -> int:
        """How many scans carry at least one answer or field needing review."""
        return sum(item.ambiguous_count > 0 for item in self.reports)

    @property
    def scans_with_multiple_marks(self) -> int:
        """How many scans carry at least one double-marked question."""
        return sum(item.answers_multiple > 0 for item in self.reports)

    @property
    def passed_count(self) -> int:
        """Scans that passed with no finding at all."""
        return sum(item.status is CalibrationStatus.PASSED for item in self.reports)

    @property
    def warned_count(self) -> int:
        """Scans that passed, but with a warning worth a look."""
        return sum(
            item.status is CalibrationStatus.PASSED_WITH_WARNINGS for item in self.reports
        )

    @property
    def review_count(self) -> int:
        """Scans that need an operator's review before this template is trusted."""
        return sum(item.status is CalibrationStatus.NEEDS_REVIEW for item in self.reports)

    @property
    def failed_count(self) -> int:
        """Scans calibration refuses to call usable at all."""
        return sum(item.status is CalibrationStatus.FAILED for item in self.reports)

    def to_dict(self) -> dict[str, Any]:
        """Return this sample report as JSON-safe plain data."""
        return {
            "status": self.status.value,
            "scan_count": self.scan_count,
            "registered_count": self.registered_count,
            "marker_failure_count": self.marker_failure_count,
            "scans_with_ambiguity": self.scans_with_ambiguity,
            "scans_with_multiple_marks": self.scans_with_multiple_marks,
            "passed_count": self.passed_count,
            "warned_count": self.warned_count,
            "review_count": self.review_count,
            "failed_count": self.failed_count,
            "reports": [item.to_dict() for item in self.reports],
        }


def evaluate_calibration(result: ScanResult, template: OmrTemplate) -> CalibrationReport:
    """Judge one scan's :class:`ScanResult` and return the calibration verdict.

    Args:
        result: What recognition produced for this scan. Ordinarily built
            with ``keep_bubble_measurements=True`` - see
            :class:`~omr_scanner.services.recognition_settings.RecognitionOptions`
            - so that the unusable-bubble and near-threshold checks have
            something to look at; a result built without them is judged on
            registration and answer/field review status alone.
        template: The template the scan was read with, needed only to look up
            each zone's own fill threshold for the near-threshold count -
            never to re-derive anything Phase 3 already decided.

    Returns:
        The report.

    The fail-safe, stated once: registration failure is judged first and
    alone - a sheet that never registered has no fields, no answers and no
    bubbles to say anything else about, and asking it to also fail the
    ambiguity check would be asking a question that has already been
    answered.
    """
    scan_name = result.source_path.name

    if result.registration is RegistrationStatus.FAILED:
        reason = (
            result.registration_message
            or result.error_code
            or "the page could not be rectified"
        )
        finding = CalibrationFinding(
            severity=CalibrationStatus.FAILED,
            code="REGISTRATION_FAILED",
            message=(
                f"Registration failed: {reason}. "
                "No bubble geometry can be validated for this scan."
            ),
        )
        return CalibrationReport(
            scan=scan_name, status=CalibrationStatus.FAILED, findings=(finding,)
        )

    findings: list[CalibrationFinding] = []
    status = CalibrationStatus.PASSED

    # -- geometry-class alignment warnings ------------------------------
    geometry_warnings = [code for code in result.warnings if code in GEOMETRY_WARNING_CODES]
    if geometry_warnings:
        status = _worse(status, CalibrationStatus.NEEDS_REVIEW)
        findings.append(
            CalibrationFinding(
                severity=CalibrationStatus.NEEDS_REVIEW,
                code="GEOMETRY_WARNING",
                message=(
                    "Registration succeeded, but with a geometry warning "
                    f"({', '.join(geometry_warnings)}): the fitted transform is a poor "
                    "fit to the detected markers, so bubble positions on this scan "
                    "may be displaced. Check the marker overlay."
                ),
            )
        )
    other_warnings = [code for code in result.warnings if code not in GEOMETRY_WARNING_CODES]
    if other_warnings:
        status = _worse(status, CalibrationStatus.PASSED_WITH_WARNINGS)
        findings.append(
            CalibrationFinding(
                severity=CalibrationStatus.PASSED_WITH_WARNINGS,
                code="ALIGNMENT_WARNING",
                message=f"Registered with reservations: {', '.join(other_warnings)}.",
            )
        )

    # -- bubble sampling / geometry containment -------------------------
    bubbles = result.bubbles
    unusable = sum(1 for bubble in bubbles if not bubble.usable)
    if bubbles:
        fraction = unusable / len(bubbles)
        if fraction > UNUSABLE_BUBBLE_FAILURE_FRACTION:
            status = _worse(status, CalibrationStatus.FAILED)
            findings.append(
                CalibrationFinding(
                    severity=CalibrationStatus.FAILED,
                    code="UNUSABLE_BUBBLES",
                    message=(
                        f"{unusable} of {len(bubbles)} bubble sampling windows "
                        f"({fraction:.0%}) could not be measured - they fall partly or "
                        "entirely off the registered page. This usually means the "
                        "template's geometry no longer matches this scan; check "
                        "region placement in the Template Designer before trusting "
                        "any value on this sheet."
                    ),
                )
            )
        elif fraction > UNUSABLE_BUBBLE_REVIEW_FRACTION:
            status = _worse(status, CalibrationStatus.NEEDS_REVIEW)
            findings.append(
                CalibrationFinding(
                    severity=CalibrationStatus.NEEDS_REVIEW,
                    code="UNUSABLE_BUBBLES",
                    message=(
                        f"{unusable} of {len(bubbles)} bubble sampling windows could not "
                        "be measured. Check whether the affected regions sit close to "
                        "the page edge on this scan."
                    ),
                )
            )

    near_threshold = _near_threshold_count(bubbles, template)

    # -- systematic ambiguity -------------------------------------------
    checked = len(result.answers) + sum(len(item.characters) for item in result.fields)
    reviewable = result.review_count
    if checked:
        ambiguity_fraction = reviewable / checked
        if ambiguity_fraction > SYSTEMATIC_AMBIGUITY_REVIEW_FRACTION:
            status = _worse(status, CalibrationStatus.NEEDS_REVIEW)
            findings.append(
                CalibrationFinding(
                    severity=CalibrationStatus.NEEDS_REVIEW,
                    code="SYSTEMATIC_AMBIGUITY",
                    message=(
                        f"{reviewable} of {checked} recognised positions "
                        f"({ambiguity_fraction:.0%}) need review. That is widespread "
                        "enough to suggest a threshold or geometry mismatch rather "
                        "than a few genuinely uncertain marks - not necessarily a "
                        "defect, but worth a look before trusting this template."
                    ),
                )
            )
        elif reviewable:
            status = _worse(status, CalibrationStatus.PASSED_WITH_WARNINGS)

    identifier = result.identifier
    set_code = result.set_code
    fields_needing_review = sum(item.needs_review for item in result.fields)

    return CalibrationReport(
        scan=scan_name,
        status=status,
        findings=tuple(findings),
        registered=True,
        markers_detected=len(result.markers),
        orientation_ok=not (result.quality.orientation_assumed if result.quality else False),
        bubbles_total=len(bubbles),
        bubbles_unusable=unusable,
        near_threshold_count=near_threshold,
        answers_total=len(result.answers),
        answers_blank=sum(
            1 for answer in result.answers if answer.value == "" and not answer.needs_review
        ),
        answers_multiple=sum(1 for answer in result.answers if "-" in answer.value),
        answers_needing_review=sum(answer.needs_review for answer in result.answers),
        fields_needing_review=fields_needing_review,
        identifier_value=identifier.value if identifier is not None else "",
        set_code_value=set_code.value if set_code is not None else "",
    )


def _near_threshold_count(bubbles: Sequence[BubbleView], template: OmrTemplate) -> int:
    """Count bubbles within :data:`NEAR_THRESHOLD_BAND` of their fill threshold.

    Read per zone via
    :meth:`~omr_scanner.domain.template.OmrTemplate.effective_recognition`, so
    a zone-level override is honoured exactly as the decision layer honoured
    it (:mod:`omr_scanner.recognition.decide`). Zones the template no longer
    declares (a stale result read against an edited template) are skipped
    rather than guessed at.
    """
    if not bubbles:
        return 0
    thresholds: dict[str, float] = {}
    for zone in template.zones:
        if isinstance(zone.field, IgnoredFieldDefinition):
            continue
        thresholds[zone.id] = template.effective_recognition(zone).fill_ratio_threshold

    count = 0
    for bubble in bubbles:
        threshold = thresholds.get(bubble.zone_id)
        if threshold is None or not bubble.usable:
            continue
        if abs(bubble.fill_ratio - threshold) <= NEAR_THRESHOLD_BAND:
            count += 1
    return count


def aggregate_calibration(reports: Sequence[CalibrationReport]) -> CalibrationSampleReport:
    """Combine several scans' reports into one sample verdict.

    Args:
        reports: One report per representative scan, in any order.

    Returns:
        The sample report. Its :attr:`~CalibrationSampleReport.status` is the
        worst of ``reports`` - see the module docstring for why an average
        would hide the one scan that matters. An empty sample reports
        :attr:`CalibrationStatus.NEEDS_REVIEW`: no scans tested means nothing
        was actually verified, which is not the same thing as a pass.
    """
    if not reports:
        return CalibrationSampleReport(reports=(), status=CalibrationStatus.NEEDS_REVIEW)
    status = CalibrationStatus.PASSED
    for report in reports:
        status = _worse(status, report.status)
    return CalibrationSampleReport(reports=tuple(reports), status=status)


def apply_calibration(
    template: OmrTemplate, *, status: CalibrationStatus, sample_count: int
) -> OmrTemplate:
    """Return a copy of ``template`` recording a just-completed calibration run.

    Args:
        template: The template that was calibrated, with whatever recognition
            settings the operator chose to keep.
        status: The run's overall verdict (ordinarily a
            :class:`CalibrationSampleReport`'s, when several scans were
            tested).
        sample_count: How many scans the run tested.

    Returns:
        A new template (``model_copy`` under the hood, via
        :meth:`~omr_scanner.domain.template.OmrTemplate.with_calibration`) -
        the original is never mutated, so an operator who does not explicitly
        save this one back to disk has changed nothing.

    This is the one place a calibration run is allowed to *write* anything;
    :func:`evaluate_calibration` and :func:`aggregate_calibration` only judge.
    """
    record = CalibrationRecord(
        validated_at=utc_timestamp(),
        sample_count=sample_count,
        engine_version=ENGINE_VERSION,
        status=status.value,
        geometry_fingerprint=template.geometry_fingerprint(),
        recognition_fingerprint=template.recognition_fingerprint(),
    )
    return template.with_calibration(record)


def write_calibration_report(
    sample: CalibrationSampleReport,
    template: OmrTemplate,
    path: Path,
) -> Path:
    """Write a machine-readable calibration report to ``path``.

    Args:
        sample: The sample verdict to record.
        template: The template it was measured against - its name, id and
            current recognition settings are recorded so the report is
            self-describing months later.
        path: Destination file. Parent directories are created as needed.

    Returns:
        ``path``.

    Contents, per ``docs/calibration_workflow.md``: template identity and
    version, the recognition settings in force, when the run happened, one
    entry per tested scan with its findings, and the aggregate verdict. No
    scan image is ever embedded - a calibration report is a record of a
    judgement, not a copy of the data it was made from.
    """
    payload: dict[str, Any] = {
        "generated_at": utc_timestamp(),
        "engine_version": ENGINE_VERSION,
        "template": {
            "id": template.template_id,
            "name": template.name,
            "format_version": template.format_version,
        },
        "recognition_settings": template.recognition.model_dump(mode="json"),
        "sample": sample.to_dict(),
        "disclaimer": (
            "This report reflects the scans it was run against. It confirms "
            "registration, geometry and threshold behaviour on those samples; "
            "it does not guarantee recognition accuracy on every future scan."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def separation_label(bubbles: Sequence[BubbleView]) -> str:
    """Describe how clearly a sample's bubble scores separate into two groups.

    A deliberately simple, fully documented computation rather than a fitted
    statistic: splits every *usable* bubble's fill ratio at its own group's
    leading/non-leading status (leading = the darkest bubble of its response
    group - see :attr:`~omr_scanner.services.recognition_models.BubbleView.leading`),
    and compares the gap between the two populations' means to their combined
    standard deviation. This is not a calibrated confidence and is never
    described as one; it exists only to answer, in words, the concrete
    question "do marked and unmarked bubbles on this sample look clearly
    different from each other".

    Returns:
        ``"well separated"``, ``"some overlap"``, ``"poorly separated"``, or
        ``"not enough data"`` when fewer than two usable bubbles of each kind
        were measured.
    """
    leading = [b.fill_ratio for b in bubbles if b.usable and b.leading]
    other = [b.fill_ratio for b in bubbles if b.usable and not b.leading]
    if len(leading) < 2 or len(other) < 2:
        return "not enough data"

    gap = abs(statistics.mean(leading) - statistics.mean(other))
    spread = statistics.pstdev(leading) + statistics.pstdev(other)
    if spread <= 0.0:
        return "well separated"
    ratio = gap / spread
    if ratio >= 1.5:
        return "well separated"
    if ratio >= 0.5:
        return "some overlap"
    return "poorly separated"


__all__ = [
    "GEOMETRY_WARNING_CODES",
    "NEAR_THRESHOLD_BAND",
    "SYSTEMATIC_AMBIGUITY_REVIEW_FRACTION",
    "UNUSABLE_BUBBLE_FAILURE_FRACTION",
    "UNUSABLE_BUBBLE_REVIEW_FRACTION",
    "CalibrationFinding",
    "CalibrationReport",
    "CalibrationSampleReport",
    "CalibrationStatus",
    "aggregate_calibration",
    "apply_calibration",
    "evaluate_calibration",
    "separation_label",
    "write_calibration_report",
]
