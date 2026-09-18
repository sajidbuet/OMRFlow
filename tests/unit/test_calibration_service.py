"""Tests for the Phase 4 calibration judgement.

Why these are pure:
    Every fixture here is a hand-built :class:`ScanResult`, never a real
    recognition run - the point is to test the *rules* documented in
    :mod:`omr_scanner.services.calibration_service`, not the engine that would
    ordinarily produce their inputs. A calibration status that is itself
    untested is a calibration status that will eventually call a broken
    template "passed".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template
from tests.unit.test_recognition_contract import make_answer, make_field, make_result

from omr_scanner.services.calibration_service import (
    GEOMETRY_WARNING_CODES,
    NEAR_THRESHOLD_BAND,
    SYSTEMATIC_AMBIGUITY_REVIEW_FRACTION,
    UNUSABLE_BUBBLE_FAILURE_FRACTION,
    CalibrationSampleReport,
    CalibrationStatus,
    aggregate_calibration,
    apply_calibration,
    evaluate_calibration,
    separation_label,
    write_calibration_report,
)
from omr_scanner.services.recognition_models import (
    BubbleView,
    RecognitionOutcome,
    RegistrationStatus,
)


@pytest.fixture
def template():
    return build_answer_sheet_template()


def bubble(
    *, usable: bool = True, fill_ratio: float = 0.1, leading: bool = False, **kwargs: object
) -> BubbleView:
    """One bubble measurement, defaulting to a clean, empty, usable reading."""
    defaults = {
        "zone_id": "questions_0",
        "row": 0,
        "column": 0,
        "label": "A",
        "x": 10.0,
        "y": 10.0,
        "width": 20.0,
        "height": 20.0,
        "fill_ratio": fill_ratio,
        "selected": False,
        "leading": leading,
        "group_status": "blank",
        "usable": usable,
    }
    defaults.update(kwargs)
    return BubbleView(**defaults)


class TestRegistrationFailureIsJudgedAloneAndFirst:
    def test_a_failed_registration_is_always_failed(self):
        result = make_result(
            outcome=RecognitionOutcome.REGISTRATION_FAILED,
            registration=RegistrationStatus.FAILED,
            registration_message="Only 2 of 4 registration markers were found.",
            fields=(),
            answers=(),
            bubbles=(),
            warnings=(),
        )
        report = evaluate_calibration(result, build_answer_sheet_template())
        assert report.status is CalibrationStatus.FAILED
        assert report.registered is False
        assert len(report.findings) == 1
        assert report.findings[0].code == "REGISTRATION_FAILED"
        assert "2 of 4" in report.findings[0].message

    def test_a_failed_registration_never_reaches_the_other_checks(self, template):
        # Even a result whose *other* fields would independently trip the
        # unusable-bubble or ambiguity rules must not be double-counted; a
        # sheet that never registered has no bubbles or answers to speak of.
        result = make_result(
            outcome=RecognitionOutcome.REGISTRATION_FAILED,
            registration=RegistrationStatus.FAILED,
            fields=(),
            answers=(),
            bubbles=(bubble(usable=False),) * 50,
            warnings=("LARGE_REPROJECTION_ERROR",),
        )
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.FAILED
        assert [item.code for item in report.findings] == ["REGISTRATION_FAILED"]


class TestUnusableBubbles:
    def test_no_unusable_bubbles_is_a_clean_pass(self, template):
        result = make_result(
            bubbles=(bubble(),) * 20, answers=(), fields=(), warnings=()
        )
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.PASSED
        assert report.bubbles_unusable == 0

    def test_a_few_unusable_bubbles_needs_review_not_failure(self, template):
        bubbles = (bubble(usable=False),) + (bubble(),) * 39  # 1/40 = 2.5%
        assert 0.0 < 1 / len(bubbles) <= UNUSABLE_BUBBLE_FAILURE_FRACTION
        result = make_result(bubbles=bubbles, answers=(), fields=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.NEEDS_REVIEW
        assert any(item.code == "UNUSABLE_BUBBLES" for item in report.findings)

    def test_many_unusable_bubbles_fails_outright(self, template):
        bubbles = (bubble(usable=False),) * 10 + (bubble(),) * 10  # 50%
        result = make_result(bubbles=bubbles, answers=(), fields=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.FAILED
        finding = next(item for item in report.findings if item.code == "UNUSABLE_BUBBLES")
        assert finding.severity is CalibrationStatus.FAILED
        assert "10 of 20" in finding.message

    def test_the_failure_fraction_boundary_is_exact(self, template):
        # Exactly at the documented fraction must not fail - "more than" means
        # strictly more than, and a boundary test is the only thing that keeps
        # a future edit from quietly changing "<=" to "<" or back.
        total = 100
        unusable = int(UNUSABLE_BUBBLE_FAILURE_FRACTION * total)
        bubbles = (bubble(usable=False),) * unusable + (bubble(),) * (total - unusable)
        result = make_result(bubbles=bubbles, answers=(), fields=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.status is not CalibrationStatus.FAILED


class TestGeometryWarnings:
    @pytest.mark.parametrize("code", sorted(GEOMETRY_WARNING_CODES))
    def test_a_geometry_warning_needs_review(self, template, code):
        result = make_result(warnings=(code,), bubbles=())
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.NEEDS_REVIEW
        assert any(item.code == "GEOMETRY_WARNING" for item in report.findings)

    def test_a_cosmetic_warning_is_only_a_warning(self, template):
        result = make_result(warnings=("MARKER_NEAR_IMAGE_EDGE",), bubbles=())
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.PASSED_WITH_WARNINGS
        assert any(item.code == "ALIGNMENT_WARNING" for item in report.findings)
        assert not any(item.code == "GEOMETRY_WARNING" for item in report.findings)


class TestSystematicAmbiguity:
    def test_a_couple_of_review_items_is_only_a_warning(self, template):
        # 1 of 10 (10%) is well under the systematic-ambiguity fraction.
        answers = tuple(
            make_answer(1, "?", "uncertain") if number == 1 else make_answer(number)
            for number in range(1, 11)
        )
        result = make_result(answers=answers, fields=(), bubbles=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.PASSED_WITH_WARNINGS

    def test_systematic_ambiguity_across_the_page_needs_review(self, template):
        answers = tuple(
            make_answer(number, "?", "uncertain") if number <= 4 else make_answer(number)
            for number in range(1, 11)
        )
        fraction = sum(a.needs_review for a in answers) / len(answers)
        assert fraction > SYSTEMATIC_AMBIGUITY_REVIEW_FRACTION
        result = make_result(answers=answers, fields=(), bubbles=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.NEEDS_REVIEW
        assert any(item.code == "SYSTEMATIC_AMBIGUITY" for item in report.findings)

    def test_blank_and_multiple_answers_are_not_treated_as_errors(self, template):
        # A genuine blank or double mark is candidate data, not a defect - see
        # the module docstring. Neither should, by itself, move the status.
        answers = (
            make_answer(1, "", "blank", needs_review=False),
            make_answer(2, "B-D", "multiple", needs_review=False),
        )
        result = make_result(answers=answers, fields=(), bubbles=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.status is CalibrationStatus.PASSED
        assert report.answers_blank == 1
        assert report.answers_multiple == 1


class TestNearThreshold:
    def test_a_bubble_far_from_threshold_is_not_counted(self, template):
        threshold = template.recognition.fill_ratio_threshold
        far = bubble(zone_id="questions_0", fill_ratio=0.0, usable=True)
        result = make_result(bubbles=(far,), answers=(), fields=(), warnings=())
        report = evaluate_calibration(result, template)
        assert abs(far.fill_ratio - threshold) > NEAR_THRESHOLD_BAND
        assert report.near_threshold_count == 0

    def test_a_bubble_next_to_the_threshold_is_counted(self, template):
        threshold = template.recognition.fill_ratio_threshold
        close = bubble(zone_id="questions_0", fill_ratio=threshold + 0.01, usable=True)
        result = make_result(bubbles=(close,), answers=(), fields=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.near_threshold_count == 1

    def test_an_unusable_bubble_is_never_counted_as_near_threshold(self, template):
        threshold = template.recognition.fill_ratio_threshold
        close_but_unusable = bubble(
            zone_id="questions_0", fill_ratio=threshold, usable=False
        )
        result = make_result(bubbles=(close_but_unusable,), answers=(), fields=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.near_threshold_count == 0

    def test_a_zone_not_in_the_template_any_more_is_skipped_not_guessed(self, template):
        stray = bubble(zone_id="no_such_zone", fill_ratio=0.5, usable=True)
        result = make_result(bubbles=(stray,), answers=(), fields=(), warnings=())
        report = evaluate_calibration(result, template)
        assert report.near_threshold_count == 0


class TestAmbiguousCountAndFieldReview:
    def test_ambiguous_count_combines_answers_and_fields(self):
        answers = (make_answer(1, "?", "uncertain"),)
        fields = (make_field("roll_number", "1?", "uncertain"),)
        result = make_result(answers=answers, fields=fields, bubbles=(), warnings=())
        report = evaluate_calibration(result, build_answer_sheet_template())
        assert report.answers_needing_review == 1
        assert report.fields_needing_review == 1
        assert report.ambiguous_count == 2


class TestAggregateCalibration:
    def test_an_empty_sample_is_not_a_pass(self):
        sample = aggregate_calibration(())
        assert sample.status is CalibrationStatus.NEEDS_REVIEW
        assert sample.scan_count == 0

    def test_the_sample_status_is_the_worst_of_its_scans(self, template):
        clean_result = make_result(bubbles=(), answers=(), fields=(), warnings=())
        clean = evaluate_calibration(clean_result, template)
        failed = evaluate_calibration(
            make_result(
                outcome=RecognitionOutcome.REGISTRATION_FAILED,
                registration=RegistrationStatus.FAILED,
                fields=(),
                answers=(),
                bubbles=(),
                warnings=(),
            ),
            template,
        )
        sample = aggregate_calibration((clean, failed))
        assert sample.status is CalibrationStatus.FAILED
        assert sample.scan_count == 2
        assert sample.registered_count == 1
        assert sample.marker_failure_count == 1

    def test_counts_reconcile_with_the_individual_reports(self, template):
        with_marks = evaluate_calibration(
            make_result(
                answers=(make_answer(1, "B-D", "multiple", needs_review=False),),
                fields=(),
                bubbles=(),
                warnings=(),
            ),
            template,
        )
        clean = evaluate_calibration(
            make_result(answers=(make_answer(1),), fields=(), bubbles=(), warnings=()), template
        )
        sample = aggregate_calibration((with_marks, clean))
        assert sample.scans_with_multiple_marks == 1
        assert sample.passed_count == 2


class TestApplyingAndWritingAReport:
    def test_apply_calibration_stamps_the_templates_own_fingerprints(self, template):
        stamped = apply_calibration(template, status=CalibrationStatus.PASSED, sample_count=3)
        assert stamped.calibration.is_recorded
        assert stamped.calibration.sample_count == 3
        assert stamped.calibration.status == "passed"
        assert stamped.is_calibration_current()
        # The original is never mutated.
        assert template.calibration.is_recorded is False

    def test_writes_a_self_describing_json_report(self, tmp_path: Path, template):
        report = evaluate_calibration(
            make_result(answers=(make_answer(1),), fields=(), bubbles=(), warnings=()), template
        )
        sample = aggregate_calibration((report,))
        path = write_calibration_report(sample, template, tmp_path / "calibration.json")

        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["template"]["name"] == template.name
        assert payload["sample"]["status"] == "passed"
        assert payload["sample"]["reports"][0]["scan"] == "sheet_001.png"
        assert "does not guarantee" in payload["disclaimer"]

    def test_a_report_never_embeds_an_image(self, tmp_path: Path, template):
        sample = CalibrationSampleReport()
        path = write_calibration_report(sample, template, tmp_path / "c.json")
        text = path.read_text(encoding="utf-8")
        assert "data:image" not in text
        assert "base64" not in text


class TestSeparationLabel:
    def test_not_enough_data_with_too_few_bubbles(self):
        assert separation_label((bubble(leading=True),)) == "not enough data"

    def test_clearly_separated_scores_report_well_separated(self):
        bubbles = tuple(
            bubble(fill_ratio=0.95, leading=True) for _ in range(5)
        ) + tuple(bubble(fill_ratio=0.02, leading=False) for _ in range(15))
        assert separation_label(bubbles) == "well separated"

    def test_overlapping_scores_report_poor_separation(self):
        # Both populations spread widely around the *same* mean (0.5): no
        # separation between marked and unmarked bubbles at all.
        spread = (0.3, 0.4, 0.5, 0.6, 0.7)
        bubbles = tuple(bubble(fill_ratio=value, leading=True) for value in spread) + tuple(
            bubble(fill_ratio=value, leading=False) for value in spread * 3
        )
        assert separation_label(bubbles) == "poorly separated"

    def test_unusable_bubbles_are_excluded(self):
        bubbles = (bubble(usable=False, leading=True, fill_ratio=0.99),) * 5
        assert separation_label(bubbles) == "not enough data"
