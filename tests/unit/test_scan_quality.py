"""Tests for the scan-quality vocabulary, its thresholds and its persistence.

Scope:
    :mod:`omr_scanner.domain.scan_quality` is pure vocabulary and
    :mod:`omr_scanner.services.scan_quality` turns measurements into a verdict.
    Both are exercised here without rendering a page, which is the point: a
    taxonomy reachable only through image recognition would be tested once,
    slowly, and never at its edges.

    The behaviour of the *measurement* against real paper lives in
    ``tests/integration/test_scan_quality_sheet.py``; the algorithm itself in
    ``tests/unit/test_page_geometry.py``.

The line these tests defend:
    A scan-quality problem is **not** a conflict about a value. Identity and
    set-code conflicts must keep working exactly as they did, an ambiguous
    answer must still never become a conflict, and the two kinds must stay
    distinguishable in the queue. Most of the file is about that boundary.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from numpy.typing import NDArray
from tests.conftest import build_answer_sheet_template
from tests.unit.test_recognition_contract import make_answer, make_result

from omr_scanner.domain.review import (
    RESOLUTION_TYPES,
    ConflictScope,
    ConflictType,
)
from omr_scanner.domain.scan_quality import (
    DEFAULT_THRESHOLDS,
    PageArea,
    ScanQualityAssessment,
    ScanQualityIssue,
    ScanQualityIssueCode,
    ScanQualityStatus,
    ScanQualityThresholds,
    issue_label,
)
from omr_scanner.recognition.models import FieldStatus
from omr_scanner.services.conflict_policy import ConflictPolicy, detect_conflicts
from omr_scanner.services.recognition_models import (
    CharacterView,
    FieldView,
    MarkStatus,
    RecognitionOutcome,
    ScanResult,
    StatusCode,
)
from omr_scanner.services.scan_quality import assess_page_geometry, build_probe_sites


def make_assessment(
    status: ScanQualityStatus = ScanQualityStatus.REVIEW,
    *,
    code: ScanQualityIssueCode = ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION,
    reason: str = "Page geometry distortion (lower-right): questions 81-100.",
    **kwargs: object,
) -> ScanQualityAssessment:
    """Build an assessment without measuring anything."""
    issue = ScanQualityIssue(
        code=code,
        status=status,
        detail="Local registration failed in the lower-right answer region.",
        areas=(PageArea.BOTTOM_RIGHT,),
        zone_ids=("questions_1",),
        zone_labels=("Questions 81-100",),
        question_range="81-100",
        metrics={"affected_sites": 6.0, "affected_ratio": 0.18},
    )
    return ScanQualityAssessment(
        status=status,
        issues=(issue,),
        probe_count=33,
        matched_count=33,
        affected_count=6,
        displacement_p95_pitch=0.4543,
        nonprojective_p95_pitch=0.1492,
        evaluated=True,
        reason=reason,
        **kwargs,  # type: ignore[arg-type]
    )


# ----------------------------------------------------------------------
# The vocabulary
# ----------------------------------------------------------------------
class TestTheStatus:
    """Three named outcomes, ordered, with one rule for combining them."""

    def test_only_pass_means_no_attention(self) -> None:
        assert not ScanQualityStatus.PASS.needs_attention
        assert ScanQualityStatus.REVIEW.needs_attention
        assert ScanQualityStatus.UNUSABLE.needs_attention

    def test_a_sheet_is_as_bad_as_its_worst_finding(self) -> None:
        """A sheet is as bad as its worst finding, never the average.

        One unusable identifier is not offset by nine healthy answer blocks.
        """
        assert (
            ScanQualityStatus.worse_of(
                ScanQualityStatus.PASS,
                ScanQualityStatus.UNUSABLE,
                ScanQualityStatus.REVIEW,
            )
            is ScanQualityStatus.UNUSABLE
        )

    def test_no_findings_is_a_pass(self) -> None:
        assert ScanQualityStatus.worse_of() is ScanQualityStatus.PASS


class TestPageAreas:
    """A place on the page, for a sentence a person reads."""

    @pytest.mark.parametrize(
        ("x", "y", "expected"),
        [
            (0.1, 0.1, PageArea.TOP_LEFT),
            (0.5, 0.5, PageArea.CENTER),
            (0.9, 0.9, PageArea.BOTTOM_RIGHT),
            (0.9, 0.1, PageArea.TOP_RIGHT),
            (0.1, 0.9, PageArea.BOTTOM_LEFT),
        ],
    )
    def test_the_grid_is_read_in_the_obvious_order(
        self, x: float, y: float, expected: PageArea
    ) -> None:
        assert PageArea.containing(x, y) is expected

    def test_the_page_corners_themselves_land_inside(self) -> None:
        assert PageArea.containing(0.0, 0.0) is PageArea.TOP_LEFT
        assert PageArea.containing(1.0, 1.0) is PageArea.BOTTOM_RIGHT

    def test_a_point_off_the_page_is_clamped_not_dropped(self) -> None:
        assert PageArea.containing(-0.4, 1.9) is PageArea.BOTTOM_LEFT

    def test_a_non_finite_coordinate_does_not_raise(self) -> None:
        """A point that is not a point is reported as the middle, not refused."""
        assert PageArea.containing(float("nan"), 0.5) is PageArea.CENTER

    def test_every_area_and_issue_has_a_readable_name(self) -> None:
        assert all(area.label for area in PageArea)
        assert all(issue_label(code) for code in ScanQualityIssueCode)


class TestThresholds:
    """Every tunable in one object, and none of them free to be nonsense."""

    def test_the_defaults_are_usable(self) -> None:
        assert DEFAULT_THRESHOLDS.review_displacement_pitch < 0.5
        assert DEFAULT_THRESHOLDS.min_probes >= 4

    def test_a_displacement_threshold_past_half_a_pitch_is_refused(self) -> None:
        """A threshold past half a pitch could never be reached honestly.

        Beyond that the probe cannot measure unambiguously, because the
        neighbouring feature is an equally good match.
        """
        with pytest.raises(ValueError, match="review_displacement_pitch"):
            ScanQualityThresholds(review_displacement_pitch=0.6)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("min_affected_sites", 0),
            ("min_nonprojective_pitch", 0.0),
            ("min_probes", 2),
            ("min_matched_ratio", 0.0),
            ("unusable_affected_ratio", 1.5),
            ("critical_coverage_ratio", 0.0),
        ],
    )
    def test_impossible_thresholds_are_refused(self, field: str, value: float) -> None:
        with pytest.raises(ValueError, match=field):
            ScanQualityThresholds(**{field: value})  # type: ignore[arg-type]


class TestTheAssessment:
    """What a stored verdict can be asked."""

    def test_a_default_assessment_is_a_pass_that_was_never_run(self) -> None:
        """"Not contradicted" and "confirmed good" must never be confused."""
        blank = ScanQualityAssessment()
        assert blank.status is ScanQualityStatus.PASS
        assert not blank.evaluated
        assert not blank.needs_attention

    def test_it_reports_its_codes_and_zones(self) -> None:
        assessment = make_assessment()
        assert assessment.codes == ("PAGE_GEOMETRY_DISTORTION",)
        assert assessment.affected_zone_ids == ("questions_1",)
        assert (
            assessment.issue(ScanQualityIssueCode.PAGE_GEOMETRY_DISTORTION) is not None
        )
        assert assessment.issue(ScanQualityIssueCode.PARTIAL_PAGE) is None

    def test_an_issue_summarises_itself_with_place_and_questions(self) -> None:
        summary = make_assessment().issues[0].summary()
        assert "Page geometry distortion" in summary
        assert "lower-right" in summary
        assert "81-100" in summary


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------
class TestPersistence:
    """A stored assessment has to survive the project it is stored in."""

    def test_it_round_trips_through_json(self) -> None:
        result = make_result(answers=(make_answer(1, "A"),))
        result = _with_quality(result, make_assessment())
        payload = json.loads(json.dumps(result.to_dict()))
        assert ScanResult.from_dict(payload).scan_quality == result.scan_quality

    def test_a_project_written_before_the_feature_still_loads(self) -> None:
        """The backward-compatibility guarantee, exercised rather than asserted."""
        payload = make_result(answers=(make_answer(1, "A"),)).to_dict()
        payload.pop("scan_quality", None)
        restored = ScanResult.from_dict(payload)
        assert restored.scan_quality is None
        assert restored.answers == make_result(
            answers=(make_answer(1, "A"),)
        ).answers

    def test_missing_coverage_reads_as_fully_captured(self) -> None:
        """Silence about coverage must not condemn every older scan."""
        result = _with_quality(make_result(), make_assessment())
        payload = result.to_dict()
        payload["scan_quality"].pop("coverage_ratio")
        assert ScanResult.from_dict(payload).scan_quality is not None
        restored = ScanResult.from_dict(payload).scan_quality
        assert restored is not None
        assert restored.coverage_ratio == 1.0

    def test_an_unknown_issue_code_is_skipped_not_fatal(self) -> None:
        """A newer build's project must open, not refuse.

        A durable record may be written by a version that knows findings this
        one does not. Declining to open the project because one finding has an
        unfamiliar name would turn "incompletely understood" into "unreadable".
        """
        result = _with_quality(make_result(), make_assessment())
        payload = result.to_dict()
        payload["scan_quality"]["issues"].append(
            {"code": "SOMETHING_FROM_THE_FUTURE", "status": "review", "detail": "?"}
        )
        restored = ScanResult.from_dict(payload).scan_quality
        assert restored is not None
        assert restored.codes == ("PAGE_GEOMETRY_DISTORTION",)

    def test_a_corrupt_assessment_does_not_stop_the_result_loading(self) -> None:
        payload = _with_quality(make_result(), make_assessment()).to_dict()
        payload["scan_quality"] = "not a mapping at all"
        assert ScanResult.from_dict(payload).scan_quality is None


def clean_result(**kwargs: object) -> ScanResult:
    """A result with nothing wrong with it except what a test puts there.

    ``make_result`` deliberately ships a *marginal* sheet - a low-confidence
    roll number, a double-marked answer - because the serialisation tests it
    was written for want every field populated. Reusing it here would mean
    every assertion about scan quality competed with conflicts this feature
    has nothing to do with.
    """
    defaults: dict[str, object] = {
        "outcome": RecognitionOutcome.COMPLETE,
        "status_codes": (),
        "fields": (resolved_identifier(),),
        "answers": (make_answer(1, "A"),),
        "identifier_zone_id": "roll_number",
    }
    return make_result(**{**defaults, **kwargs})


def resolved_identifier(value: str = "12") -> FieldView:
    """An identifier the engine was completely sure of.

    ``confidence=1.0`` exactly, because that is where
    :mod:`omr_scanner.services.conflict_policy` draws the line - anything below
    it is a low-confidence conflict, so 0.95 would quietly add an identity
    conflict to every test in this file.
    """
    return FieldView(
        zone_id="roll_number",
        label="Roll number",
        field_type="numeric",
        value=value,
        status=FieldStatus.RESOLVED.value,
        needs_review=False,
        characters=tuple(
            CharacterView(
                position=index,
                value=digit,
                status=MarkStatus.RESOLVED.value,
                top_fill=0.9,
                margin=0.8,
                confidence=1.0,
            )
            for index, digit in enumerate(value)
        ),
    )


def _with_quality(
    result: ScanResult, assessment: ScanQualityAssessment
) -> ScanResult:
    """Return ``result`` carrying ``assessment``."""
    import dataclasses

    return dataclasses.replace(result, scan_quality=assessment)


# ----------------------------------------------------------------------
# The review queue
# ----------------------------------------------------------------------
class TestReviewIntegration:
    """Scan quality joins the existing queue without changing what is in it."""

    def test_it_is_a_sheet_scope_conflict(self) -> None:
        assert ConflictType.SCAN_QUALITY.scope is ConflictScope.SHEET

    def test_it_cannot_be_answered_with_a_value(self) -> None:
        """"The lower-right corner was curled" has no answer of A, B, C or D."""
        assert not ConflictType.SCAN_QUALITY.allows_value_correction

    def test_it_is_not_a_processing_failure(self) -> None:
        """The sheet was read.

        Something about the paper is in doubt, which is a different thing from a JPEG
        that would not decode.
        """
        assert not ConflictType.SCAN_QUALITY.is_processing_failure

    def test_it_appears_in_the_resolution_queue(self) -> None:
        assert ConflictType.SCAN_QUALITY.requires_resolution
        assert ConflictType.SCAN_QUALITY in RESOLUTION_TYPES

    def test_it_has_its_own_label_distinct_from_alignment(self) -> None:
        """A reviewer must be able to tell the categories apart at a glance."""
        assert ConflictType.SCAN_QUALITY.label == "Scan quality"
        assert ConflictType.SCAN_QUALITY.label != ConflictType.ALIGNMENT_WARNING.label

    def test_a_clean_sheet_raises_nothing(self) -> None:
        template = build_answer_sheet_template()
        result = _with_quality(
            clean_result(),
            ScanQualityAssessment(status=ScanQualityStatus.PASS, evaluated=True),
        )
        assert detect_conflicts(result, template) == ()

    def test_a_doubtful_sheet_raises_exactly_one_scan_quality_conflict(self) -> None:
        template = build_answer_sheet_template()
        result = _with_quality(clean_result(), make_assessment())
        conflicts = detect_conflicts(result, template)
        assert [item.conflict_type for item in conflicts] == [
            ConflictType.SCAN_QUALITY
        ]
        assert "lower-right" in conflicts[0].observation.detail

    def test_an_unusable_sheet_is_ranked_as_serious(self) -> None:
        template = build_answer_sheet_template()
        result = _with_quality(
            make_result(), make_assessment(ScanQualityStatus.UNUSABLE)
        )
        conflicts = detect_conflicts(result, template)
        assert conflicts[0].severity == 1

    def test_it_can_be_switched_off(self) -> None:
        template = build_answer_sheet_template()
        result = _with_quality(clean_result(), make_assessment())
        policy = ConflictPolicy(flag_scan_quality=False)
        assert detect_conflicts(result, template, policy=policy) == ()

    def test_detection_stays_idempotent(self) -> None:
        """The key is what stops a retry filling the queue with duplicates."""
        template = build_answer_sheet_template()
        result = _with_quality(make_result(), make_assessment())
        first = detect_conflicts(result, template)
        second = detect_conflicts(result, template)
        assert [item.key for item in first] == [item.key for item in second]


class TestTheExistingPolicyIsUnchanged:
    """The regression guard on everything this feature must not touch."""

    def test_an_ambiguous_answer_is_still_not_a_conflict(self) -> None:
        """An ambiguous answer is still not a conflict.

        The rule that keeps the queue workable, restated here because this
        feature adds a *sheet*-scope reason and must not reopen that door.
        """
        template = build_answer_sheet_template()
        result = _with_quality(
            clean_result(
                answers=(
                    make_answer(1, "A-B", status=MarkStatus.MULTIPLE.value),
                    make_answer(2, "", status=MarkStatus.UNREADABLE.value),
                ),
                status_codes=(StatusCode.MULTIPLE_MARK.value,),
            ),
            make_assessment(),
        )
        raised = {item.conflict_type for item in detect_conflicts(result, template)}
        assert raised == {ConflictType.SCAN_QUALITY}

    def test_a_geometry_problem_does_not_suppress_an_identity_conflict(self) -> None:
        """Both findings must surface, not one in place of the other.

        The reviewer needs to know the sheet is bent *and* that the roll
        number did not resolve.
        """
        template = build_answer_sheet_template()
        identifier = FieldView(
            zone_id="roll_number",
            label="Roll number",
            field_type="numeric",
            value="",
            status=FieldStatus.BLANK.value,
            needs_review=True,
            characters=(
                CharacterView(
                    position=0,
                    value="",
                    status=MarkStatus.BLANK.value,
                    top_fill=0.0,
                    margin=0.0,
                    confidence=0.0,
                ),
            ),
        )
        result = _with_quality(
            clean_result(fields=(identifier,), identifier_zone_id="roll_number"),
            make_assessment(),
        )
        raised = {item.conflict_type for item in detect_conflicts(result, template)}
        assert ConflictType.SCAN_QUALITY in raised
        assert any(item.value.startswith("identifier_") for item in raised)


class TestInsufficientEvidenceIsNotAPass:
    """"We looked and it is sound" and "we could not tell" are different.

    The distinction this whole check turns on. A fold destroys the very
    printing the probe needs, so "could not measure" is one of the shapes a
    folded sheet arrives in - and collapsing it into a pass would let exactly
    the wrong sheet through.
    """

    def test_a_blank_page_is_not_confirmed_clean(self) -> None:
        """No printing located, so nothing has been confirmed about it."""
        template = build_answer_sheet_template()
        blank: NDArray[np.uint8] = np.full(
            (template.page.canonical_height_px, template.page.canonical_width_px),
            245,
            dtype=np.uint8,
        )
        assessment = assess_page_geometry(blank, template)
        assert assessment.matched_count == 0
        assert not assessment.evaluated
        assert not assessment.is_confirmed_clean

    def test_it_says_so_with_an_issue_rather_than_by_omission(self) -> None:
        template = build_answer_sheet_template()
        blank: NDArray[np.uint8] = np.full(
            (template.page.canonical_height_px, template.page.canonical_width_px),
            245,
            dtype=np.uint8,
        )
        assessment = assess_page_geometry(blank, template)
        assert ScanQualityIssueCode.GEOMETRY_NOT_VERIFIED.value in assessment.codes
        assert assessment.status is not ScanQualityStatus.PASS
        assert assessment.reason

    def test_zero_affected_probes_is_not_evidence_of_soundness(self) -> None:
        """The exact shape the brief forbids: ``affected == 0`` -> PASS.

        Zero probes moved because zero probes were measured, which is not the
        same fact at all.
        """
        template = build_answer_sheet_template()
        blank: NDArray[np.uint8] = np.full(
            (template.page.canonical_height_px, template.page.canonical_width_px),
            245,
            dtype=np.uint8,
        )
        assessment = assess_page_geometry(blank, template)
        assert assessment.affected_count == 0
        assert not assessment.is_confirmed_clean

    def test_a_template_too_sparse_to_probe_is_recorded_not_blamed(self) -> None:
        """A property of the document, not a defect in any one sheet.

        Flagging every sheet in a batch because the *template* describes too
        little printing would be a decision nobody made, so this is recorded as
        not evaluated without raising a finding against the sheet.
        """
        template = build_answer_sheet_template()
        page: NDArray[np.uint8] = np.full(
            (template.page.canonical_height_px, template.page.canonical_width_px),
            245,
            dtype=np.uint8,
        )
        assessment = assess_page_geometry(page, template, sites=())
        assert not assessment.evaluated
        assert not assessment.is_confirmed_clean
        assert assessment.codes == ()
        assert assessment.reason

    def test_an_unevaluated_assessment_reports_its_evidence(self) -> None:
        """Every number the caller needs to diagnose *why* it could not tell."""
        template = build_answer_sheet_template()
        blank: NDArray[np.uint8] = np.full(
            (template.page.canonical_height_px, template.page.canonical_width_px),
            245,
            dtype=np.uint8,
        )
        assessment = assess_page_geometry(blank, template)
        assert assessment.probe_count > 0
        assert assessment.matched_ratio == 0.0
        assert assessment.affected_ratio == 0.0
        assert assessment.unmatched_count == assessment.probe_count


class TestConfirmedClean:
    """The predicate callers should use, rather than reading ``status`` alone."""

    def test_a_measured_sound_sheet_is_confirmed(self) -> None:
        assessment = ScanQualityAssessment(
            status=ScanQualityStatus.PASS, evaluated=True, probe_count=30, matched_count=30
        )
        assert assessment.is_confirmed_clean

    def test_an_unmeasured_sheet_is_not(self) -> None:
        """Even though its status is, technically, PASS."""
        assessment = ScanQualityAssessment(status=ScanQualityStatus.PASS, evaluated=False)
        assert assessment.status is ScanQualityStatus.PASS
        assert not assessment.is_confirmed_clean

    def test_a_flagged_sheet_is_not(self) -> None:
        assert not make_assessment().is_confirmed_clean


# ----------------------------------------------------------------------
# Probe geometry from a template
# ----------------------------------------------------------------------
class TestProbeSitesFromATemplate:
    """The bridge from a template document to plain probe geometry."""

    def test_every_recognisable_zone_is_covered(self) -> None:
        template = build_answer_sheet_template()
        groups = {site.group_id for site in build_probe_sites(template)}
        assert "roll_number" in groups
        assert any(name.startswith("questions") for name in groups)

    def test_sites_carry_the_zone_pitch_in_pixels(self) -> None:
        template = build_answer_sheet_template()
        sites = build_probe_sites(template)
        assert sites
        assert all(site.pitch_px > 1.0 for site in sites)
        assert all(len(site.features) >= 6 for site in sites)

    def test_site_ids_are_unique_and_stable(self) -> None:
        template = build_answer_sheet_template()
        first = build_probe_sites(template)
        second = build_probe_sites(template)
        ids = [site.site_id for site in first]
        assert len(set(ids)) == len(ids)
        assert ids == [site.site_id for site in second]

    def test_an_ignored_zone_is_never_probed(self) -> None:
        """An ignored region is not printed with a lattice to find."""
        template = build_answer_sheet_template()
        ignored = {zone.id for zone in template.zones if zone.grid is None}
        groups = {site.group_id for site in build_probe_sites(template)}
        assert not (groups & ignored)
