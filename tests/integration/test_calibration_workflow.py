"""End-to-end tests for the Phase 4 calibration workflow.

Scope:
    Real templates, real rendered sheets, the real
    :class:`~omr_scanner.services.recognition_service.RecognitionEngine` and
    the real :mod:`omr_scanner.services.calibration_service` judgement - no
    hand-built ``ScanResult`` fixtures here, because the whole point of this
    module is to prove the pieces agree with each other once wired together.

The load-bearing test in this file:
    :class:`TestMiscalibrationIsNeverAConfidentPass` - a template whose
    registration markers do not match what is actually printed on the scan
    must come back FAILED, and carrying **no** fields, answers or bubbles at
    all, never a plausible-looking wrong result. That is the single
    behaviour the whole of Phase 4 exists to make visible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.domain.geometry import NormalizedPoint, NormalizedRect
from omr_scanner.imaging.metrics import BubbleMetricsConfig
from omr_scanner.services import (
    CalibrationStatus,
    RecognitionEngine,
    RecognitionOptions,
    RegistrationStatus,
    aggregate_calibration,
    apply_calibration,
    evaluate_calibration,
    load_template,
    save_template,
)

if TYPE_CHECKING:
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate

STANDARD_MARKS = {
    "roll_number": dict(enumerate("120317")),
    "set_code": {0: "A"},
    "questions_0": dict.fromkeys(range(10), "B"),
    "questions_1": dict.fromkeys(range(10), "C"),
}


@pytest.fixture(scope="module")
def template() -> OmrTemplate:
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def engine() -> RecognitionEngine:
    return RecognitionEngine(
        RecognitionOptions(with_preview=False, keep_bubble_measurements=True)
    )


def write_sheet(tmp_path: Path, template: OmrTemplate, name: str = "sheet.png") -> Path:
    image = render_marked_sheet(template, STANDARD_MARKS)
    path = tmp_path / name
    cv2.imwrite(str(path), image)
    return path


def shifted_markers(template: OmrTemplate, *, dx: float, dy: float) -> OmrTemplate:
    """Return a copy of ``template`` with every registration marker moved.

    A plain, honest way to build "a template that does not match this scan" -
    the marker centres are the one thing Phase 1 detection searches for, and
    moving them beyond :attr:`~omr_scanner.domain.template.RegistrationMarker.search_radius`
    is exactly what happens when the wrong template is selected for a batch.
    """
    moved = tuple(
        marker.model_copy(
            update={
                "center": NormalizedPoint(
                    x=min(max(marker.center.x + dx, 0.0), 1.0),
                    y=min(max(marker.center.y + dy, 0.0), 1.0),
                )
            }
        )
        for marker in template.registration_markers
    )
    return template.model_copy(update={"registration_markers": moved})


class TestMiscalibrationIsNeverAConfidentPass:
    """Spec section 36: the major calibration exit criterion."""

    def test_a_template_with_badly_displaced_markers_fails_to_register(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        path = write_sheet(tmp_path, template)
        # Default search_radius is 0.05; 0.3 is nowhere near recoverable.
        mismatched = shifted_markers(template, dx=0.3, dy=0.3)

        result = engine.process(path, mismatched)

        assert result.registration is RegistrationStatus.FAILED
        # The fail-safe, stated as data: nothing was measured or decided.
        assert result.fields == ()
        assert result.answers == ()
        assert result.bubbles == ()
        assert result.identifier_value == ""

    def test_the_calibration_verdict_for_that_scan_is_failed_not_a_pass(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        path = write_sheet(tmp_path, template)
        mismatched = shifted_markers(template, dx=0.3, dy=0.3)
        result = engine.process(path, mismatched)

        report = evaluate_calibration(result, mismatched)

        assert report.status is CalibrationStatus.FAILED
        assert report.registered is False
        assert report.findings[0].code == "REGISTRATION_FAILED"
        # The message is diagnostic, not "Template error." (spec section 61).
        assert "registration" in report.findings[0].message.lower()

    def test_the_correctly_matched_template_reads_the_same_scan_cleanly(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        # The control: the *same* scan, read with the template it actually
        # matches, must not also fail - the failure above is about the
        # mismatch, not about the scan or the engine.
        path = write_sheet(tmp_path, template)
        result = engine.process(path, template)
        report = evaluate_calibration(result, template)

        assert result.registration is not RegistrationStatus.FAILED
        assert result.identifier_value == "120317"
        assert report.status in (CalibrationStatus.PASSED, CalibrationStatus.PASSED_WITH_WARNINGS)


def shifted_zones(template: OmrTemplate, *, dx: float, dy: float) -> OmrTemplate:
    """Return a copy of ``template`` with every zone and bubble grid moved.

    The dangerous mismatch, and a different one from :func:`shifted_markers`:
    the *registration markers* are untouched, so Phase 1 still rectifies the
    page perfectly. Only the bubble geometry is wrong - which is exactly what
    a mis-drawn or wrong-page template looks like, and exactly the case that
    produces confident values read from blank paper.
    """
    zones = []
    for zone in template.zones:
        bounds = zone.bounds
        update: dict = {
            "bounds": NormalizedRect(
                x=min(max(bounds.x + dx, 0.0), 1.0 - bounds.width),
                y=min(max(bounds.y + dy, 0.0), 1.0 - bounds.height),
                width=bounds.width,
                height=bounds.height,
            )
        }
        if zone.grid is not None:
            origin = zone.grid.origin
            update["grid"] = zone.grid.model_copy(
                update={
                    "origin": NormalizedPoint(
                        x=min(max(origin.x + dx, 0.0), 1.0),
                        y=min(max(origin.y + dy, 0.0), 1.0),
                    )
                }
            )
        zones.append(zone.model_copy(update=update))
    return template.model_copy(update={"zones": tuple(zones)})


class TestADisplacedTemplateThatStillRegistersIsNeverAPass:
    """The hardest miscalibration case, and the one with no registration error.

    When only the bubble geometry is wrong, every existing signal stays
    clean: the markers are found, the transform is exact, no sampling window
    falls off the page, and every group reads a confident BLANK because it is
    sampling bare paper. The verdict must still not be a pass - otherwise
    Phase 4 endorses a template that will misread an entire batch.
    """

    @pytest.mark.parametrize("offset", [0.02, 0.05, 0.12])
    def test_a_displaced_template_is_never_reported_as_passed(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine, offset: float
    ):
        path = write_sheet(tmp_path, template)
        displaced = shifted_zones(template, dx=offset, dy=offset)

        result = engine.process(path, displaced)
        report = evaluate_calibration(result, displaced)

        # Registration itself genuinely succeeds - this is not the marker case.
        assert result.registration is not RegistrationStatus.FAILED
        assert report.status in (CalibrationStatus.NEEDS_REVIEW, CalibrationStatus.FAILED), (
            f"a template displaced by {offset} of the page reported "
            f"{report.status.value}, which an operator would read as safe to "
            "run a batch with"
        )

    def test_the_identifier_read_from_the_wrong_place_is_not_silently_accepted(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        path = write_sheet(tmp_path, template)
        displaced = shifted_zones(template, dx=0.02, dy=0.02)
        result = engine.process(path, displaced)
        report = evaluate_calibration(result, displaced)

        # The correctly matched template reads this sheet as "120317".
        assert result.identifier_value != "120317"
        assert report.status is not CalibrationStatus.PASSED
        assert any(item.code == "NO_MARKS_DETECTED" for item in report.findings)

    def test_the_same_scan_with_the_matching_template_still_passes(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        # The control: the rule above must not fire on a correct template, or
        # it would simply be refusing everything.
        path = write_sheet(tmp_path, template)
        report = evaluate_calibration(engine.process(path, template), template)
        assert report.status in (
            CalibrationStatus.PASSED, CalibrationStatus.PASSED_WITH_WARNINGS
        )
        assert not any(item.code == "NO_MARKS_DETECTED" for item in report.findings)
        assert report.groups_with_marks > 0


class TestOverlayGeometryComesFromTheSampler:
    """Spec section 48: the displayed geometry is the engine's own, not a copy.

    Covers an identifier bubble, a set-code bubble and a question bubble,
    because a geometry error can be confined to one kind of zone.
    """

    def representative_bubbles(self, result):
        """One bubble from the identifier, the set code and a question block."""
        chosen = {}
        for bubble in result.bubbles:
            if bubble.zone_id == result.identifier_zone_id:
                chosen.setdefault("identifier", bubble)
            elif bubble.zone_id == result.set_code_zone_id:
                chosen.setdefault("set_code", bubble)
            else:
                chosen.setdefault("question", bubble)
        return chosen

    def test_every_bubble_reports_the_region_that_was_actually_sampled(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        path = write_sheet(tmp_path, template)
        result = engine.process(path, template)
        chosen = self.representative_bubbles(result)
        assert set(chosen) == {"identifier", "set_code", "question"}

        ratio = BubbleMetricsConfig().sample_radius_ratio
        for kind, bubble in chosen.items():
            assert bubble.sample_half_width == pytest.approx(
                bubble.width / 2.0 * ratio
            ), f"{kind} bubble reports a sampling window it was not measured over"
            assert bubble.sample_half_height == pytest.approx(bubble.height / 2.0 * ratio)
            # And it is genuinely a different region from the printed bubble,
            # so an overlay cannot draw one while meaning the other.
            assert bubble.sample_half_width < bubble.width / 2.0

    def test_the_reported_window_follows_the_samplers_configuration(
        self, tmp_path: Path, template: OmrTemplate
    ):
        # The shared-data-path proof: change what the *sampler* does and the
        # reported geometry must change with it. A GUI-side reimplementation
        # using the default ratio would pass the test above and fail this one.
        path = write_sheet(tmp_path, template)
        narrow = RecognitionEngine(
            RecognitionOptions(
                with_preview=False,
                keep_bubble_measurements=True,
                metrics=BubbleMetricsConfig(sample_radius_ratio=0.4),
            )
        )
        result = narrow.process(path, template)
        bubble = result.bubbles[0]
        assert bubble.sample_half_width == pytest.approx(bubble.width / 2.0 * 0.4)

    def test_a_bubble_centre_is_the_centre_the_sampler_measured_at(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        # `BubbleView.x/y` is copied from `BubbleMeasurement.center_x/y` - the
        # value `measure_bubble` was called with - so a bubble the overlay
        # draws is at the position the fill ratio beside it was read from.
        path = write_sheet(tmp_path, template)
        result = engine.process(path, template)
        for bubble in self.representative_bubbles(result).values():
            assert 0.0 < bubble.x < result.canonical_width
            assert 0.0 < bubble.y < result.canonical_height


class TestSmallOffsetsAreToleratedButLargeOnesAreNot:
    """Spec section 37: controlled offsets, small and large."""

    def test_a_small_marker_offset_still_registers(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        path = write_sheet(tmp_path, template)
        # Well inside the default 0.05 search radius.
        slightly_off = shifted_markers(template, dx=0.01, dy=0.01)
        result = engine.process(path, slightly_off)
        assert result.registration is not RegistrationStatus.FAILED

    def test_a_large_marker_offset_is_flagged_not_silently_absorbed(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        path = write_sheet(tmp_path, template)
        way_off = shifted_markers(template, dx=0.35, dy=-0.35)
        result = engine.process(path, way_off)
        assert result.registration is RegistrationStatus.FAILED


class TestTheOrdinaryCalibrationPath:
    """The whole calibration pipeline, template through validation status.

    Template -> scan -> registration -> diagnostics -> threshold change ->
    reclassification -> validation status (spec section 63).
    """

    def test_a_clean_representative_sample_passes(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        paths = [write_sheet(tmp_path, template, f"sheet_{i}.png") for i in range(3)]
        reports = [evaluate_calibration(engine.process(path, template), template) for path in paths]
        sample = aggregate_calibration(reports)
        assert sample.status in (CalibrationStatus.PASSED, CalibrationStatus.PASSED_WITH_WARNINGS)
        assert sample.registered_count == 3

    def test_a_threshold_change_propagates_through_a_full_session(
        self, tmp_path: Path, template: OmrTemplate
    ):
        # Question 2 carries a deliberately faint mark - a raw fill ratio of
        # 0.35, which the template's default thresholds (fill 0.55, blank
        # 0.25) place squarely in the ambiguous band. Exactly the "raw score
        # unchanged, classification changes with the threshold" scenario
        # spec section 45 asks for.
        marks = {
            "roll_number": dict(enumerate("120317")),
            "set_code": {0: "A"},
            "questions_0": {0: "B", 1: ("A", 0.35)},
        }
        image = render_marked_sheet(template, marks)
        path = tmp_path / "sheet.png"
        cv2.imwrite(str(path), image)

        strict_engine = RecognitionEngine(
            RecognitionOptions(with_preview=False, keep_bubble_measurements=True)
        )
        session = strict_engine.open_session(path, template)

        baseline = session.recompute(template)
        assert baseline.answer(1).value == "B"
        faint = baseline.answer(2)
        assert faint.value == ""
        assert faint.status == "uncertain"
        assert faint.needs_review is True
        measured_fill = faint.top_fill
        assert 0.25 < measured_fill < 0.55  # inside the default template's ambiguous band

        # Lower the fill threshold below this question's own measured fill:
        # the same raw evidence now counts as a resolved mark.
        looser = template.model_copy(
            update={
                "recognition": template.recognition.model_copy(
                    update={"fill_ratio_threshold": measured_fill - 0.05}
                )
            }
        )
        after = session.recompute(looser)
        resolved = after.answer(2)
        assert resolved.value == "A"
        assert resolved.status == "resolved"
        assert resolved.needs_review is False
        assert resolved.top_fill == pytest.approx(measured_fill)  # the raw score never moved
        # Every other answer, and the registration itself, is untouched.
        assert after.answer(1).value == "B"
        assert after.canonical_width == baseline.canonical_width
        assert after.markers == baseline.markers

    def test_applying_and_saving_a_calibration_round_trips(
        self, tmp_path: Path, template: OmrTemplate, engine: RecognitionEngine
    ):
        template_path = tmp_path / "sheet.omrt"
        save_template(template, template_path)
        path = write_sheet(tmp_path, template)

        report = evaluate_calibration(engine.process(path, template), template)
        sample = aggregate_calibration([report])
        stamped = apply_calibration(template, status=sample.status, sample_count=1)
        save_template(stamped, template_path)

        reloaded = load_template(template_path)
        assert reloaded.calibration.is_recorded
        assert reloaded.is_calibration_current()

    def test_editing_the_template_afterwards_invalidates_the_saved_calibration(
        self, template: OmrTemplate
    ):
        stamped = apply_calibration(template, status=CalibrationStatus.PASSED, sample_count=1)
        assert stamped.is_calibration_current()

        edited = stamped.model_copy(
            update={
                "recognition": stamped.recognition.model_copy(
                    update={"fill_ratio_threshold": 0.7}
                )
            }
        )
        assert edited.is_calibration_current() is False


class TestBackwardCompatibility:
    """Spec section 53: templates saved before Phase 4 must keep loading."""

    def test_a_template_document_with_no_calibration_field_loads_uncalibrated(
        self, tmp_path: Path, template: OmrTemplate
    ):
        import json

        path = tmp_path / "old.omrt"
        payload = template.model_dump(mode="json")
        del payload["calibration"]  # simulate a document written before Phase 4
        path.write_text(json.dumps(payload), encoding="utf-8")

        reloaded = load_template(path)
        assert reloaded.calibration.is_recorded is False
        assert reloaded.is_calibration_current() is False
