"""Tests for the single decision rule applied to every response group."""

from __future__ import annotations

import pytest

from omr_scanner.domain.template import RecognitionSettings
from omr_scanner.imaging.metrics import BubbleMeasurement
from omr_scanner.recognition.decide import (
    decide_group,
    reading_from_measurement,
    readings_from_measurements,
)
from omr_scanner.recognition.models import BubbleReading, MarkStatus

SETTINGS = RecognitionSettings()
"""Template defaults: fill 0.55, blank 0.25, margin 0.12, confidence 0.60."""


def reading(label: str, fill: float, *, usable: bool = True) -> BubbleReading:
    """A reading carrying only the quantity the decision rule uses."""
    return BubbleReading(
        label=label, fill_ratio=fill, mean_darkness=fill, contrast=60.0, usable=usable
    )


def measurement(fill: float, *, usable: bool = True) -> BubbleMeasurement:
    return BubbleMeasurement(
        center_x=10.0,
        center_y=10.0,
        fill_ratio=fill,
        mean_darkness=fill,
        paper_level=240.0,
        contrast=60.0,
        sample_pixels=200,
        usable=usable,
    )


class TestReadingsFromMeasurements:
    def test_each_measurement_is_paired_with_its_symbol(self):
        readings = readings_from_measurements(
            [measurement(0.9), measurement(0.02)], ["A", "B"]
        )
        assert [item.label for item in readings] == ["A", "B"]
        assert readings[0].fill_ratio == pytest.approx(0.9)

    def test_a_missing_measurement_is_unusable_rather_than_empty(self):
        # Reporting a bubble that fell off the page as "0% filled" would make an
        # unreadable group indistinguishable from a deliberately blank one.
        item = reading_from_measurement(None, "C")
        assert item.usable is False
        assert item.fill_ratio == 0.0

    def test_a_length_mismatch_is_refused(self):
        with pytest.raises(ValueError, match="exactly one symbol"):
            readings_from_measurements([measurement(0.9)], ["A", "B"])


class TestCandidateOrdering:
    def test_the_darkest_bubble_leads_regardless_of_printed_position(self):
        decision = decide_group(
            [reading("A", 0.05), reading("B", 0.08), reading("C", 0.93), reading("D", 0.04)],
            settings=SETTINGS,
        )
        assert decision.leading_label == "C"
        assert decision.top_fill == pytest.approx(0.93)
        assert decision.runner_up_fill == pytest.approx(0.08)
        assert decision.margin == pytest.approx(0.85)

    def test_a_multiple_mark_is_reported_in_printed_order_not_darkness_order(self):
        # `D` is darker than `B`, but the sheet reads "B-D" left to right.
        decision = decide_group(
            [reading("A", 0.02), reading("B", 0.71), reading("C", 0.03), reading("D", 0.95)],
            settings=SETTINGS,
        )
        assert decision.value == "B-D"

    def test_a_single_bubble_group_has_no_runner_up(self):
        decision = decide_group([reading("X", 0.90)], settings=SETTINGS)
        assert decision.runner_up_fill == 0.0
        assert decision.margin == pytest.approx(0.90)
        assert decision.status is MarkStatus.RESOLVED


class TestGroupOutcomes:
    def test_one_clear_mark_resolves(self):
        decision = decide_group(
            [reading("A", 0.03), reading("B", 0.91), reading("C", 0.02)], settings=SETTINGS
        )
        assert decision.status is MarkStatus.RESOLVED
        assert decision.value == "B"
        assert decision.character == "B"
        assert decision.needs_review is False
        assert decision.confidence == pytest.approx(1.0)

    def test_nothing_marked_is_blank(self):
        decision = decide_group(
            [reading("A", 0.04), reading("B", 0.06), reading("C", 0.03)], settings=SETTINGS
        )
        assert decision.status is MarkStatus.BLANK
        assert decision.value == ""
        assert decision.character == "_"
        assert decision.needs_review is False

    def test_two_marks_are_both_kept(self):
        decision = decide_group(
            [reading("A", 0.88), reading("B", 0.02), reading("C", 0.79), reading("D", 0.01)],
            settings=SETTINGS,
        )
        assert decision.status is MarkStatus.MULTIPLE
        assert decision.value == "A-C"
        assert decision.selected == (0, 2)
        assert decision.needs_review is True
        # The rule never silently prefers the darker of the two.
        assert "A" in decision.value and "C" in decision.value

    def test_four_marks_are_all_kept(self):
        decision = decide_group(
            [reading("A", 0.8), reading("B", 0.8), reading("C", 0.8), reading("D", 0.8)],
            settings=SETTINGS,
        )
        assert decision.value == "A-B-C-D"
        assert decision.status is MarkStatus.MULTIPLE

    def test_a_mark_too_faint_to_accept_is_uncertain_with_no_value(self):
        # 0.40 sits between "certainly empty" (0.25) and "marked" (0.55).
        decision = decide_group(
            [reading("A", 0.40), reading("B", 0.03)], settings=SETTINGS
        )
        assert decision.status is MarkStatus.UNCERTAIN
        assert decision.value == ""
        assert decision.character == "?"
        assert decision.needs_review is True
        # The reviewer can still see what it nearly was.
        assert decision.leading_label == "A"

    def test_two_similar_readings_where_only_one_crosses_are_uncertain(self):
        # Half-erased answers look like this: one mark accepted, but the
        # runner-up is right behind it.
        decision = decide_group(
            [reading("A", 0.58), reading("B", 0.52)], settings=SETTINGS
        )
        assert decision.status is MarkStatus.UNCERTAIN
        assert decision.margin == pytest.approx(0.06)
        assert decision.needs_review is True

    def test_an_unsampleable_group_is_unreadable_not_blank(self):
        decision = decide_group(
            [reading("A", 0.0, usable=False), reading("B", 0.0, usable=False)],
            settings=SETTINGS,
        )
        assert decision.status is MarkStatus.UNREADABLE
        assert decision.leading_index is None
        assert decision.leading_label == ""
        assert decision.needs_review is True

    def test_unusable_bubbles_are_excluded_from_the_comparison(self):
        decision = decide_group(
            [reading("A", 0.99, usable=False), reading("B", 0.90), reading("C", 0.02)],
            settings=SETTINGS,
        )
        assert decision.value == "B"
        assert decision.leading_label == "B"

    def test_an_empty_group_is_a_malformed_template_not_a_blank_answer(self):
        with pytest.raises(ValueError, match="at least one bubble"):
            decide_group([], settings=SETTINGS)


class TestConfidenceIsInterpretable:
    def test_confidence_is_the_fraction_of_the_required_separation_achieved(self):
        settings = RecognitionSettings(ambiguity_margin=0.40)
        # Margin 0.20 out of the 0.40 the template demands.
        decision = decide_group(
            [reading("A", 0.80), reading("B", 0.60)], settings=settings
        )
        assert decision.status is MarkStatus.MULTIPLE  # both crossed 0.55

        settings = RecognitionSettings(
            fill_ratio_threshold=0.70, blank_ratio_threshold=0.25, ambiguity_margin=0.40
        )
        decision = decide_group(
            [reading("A", 0.80), reading("B", 0.60)], settings=settings
        )
        assert decision.status is MarkStatus.UNCERTAIN  # margin 0.20 < 0.40
        assert decision.confidence == 0.0

    def test_confidence_saturates_at_one_and_never_exceeds_it(self):
        decision = decide_group(
            [reading("A", 1.00), reading("B", 0.00)], settings=SETTINGS
        )
        assert decision.confidence == pytest.approx(1.0)

    @pytest.mark.parametrize(
        "readings",
        [
            [reading("A", 0.40), reading("B", 0.03)],  # too faint
            [reading("A", 0.58), reading("B", 0.52)],  # too close
            [reading("A", 0.88), reading("B", 0.79)],  # double mark
            [reading("A", 0.0, usable=False)],  # unreadable
        ],
    )
    def test_a_group_needing_a_human_reports_zero_not_a_plausible_percentage(self, readings):
        decision = decide_group(readings, settings=SETTINGS)
        assert decision.confidence == 0.0
        assert decision.needs_review is True

    def test_confidence_always_lies_in_the_unit_interval(self):
        for top in (0.0, 0.05, 0.24, 0.26, 0.54, 0.56, 0.99, 1.0):
            for runner_up in (0.0, 0.1, 0.5, 0.9):
                decision = decide_group(
                    [reading("A", top), reading("B", min(runner_up, top))],
                    settings=SETTINGS,
                )
                assert 0.0 <= decision.confidence <= 1.0


class TestThresholdsComeFromTheTemplate:
    def test_raising_the_fill_threshold_turns_a_mark_into_an_uncertainty(self):
        readings = [reading("A", 0.60), reading("B", 0.02)]
        assert decide_group(readings, settings=SETTINGS).status is MarkStatus.RESOLVED

        strict = RecognitionSettings(fill_ratio_threshold=0.80)
        assert decide_group(readings, settings=strict).status is MarkStatus.UNCERTAIN

    def test_raising_the_blank_threshold_turns_an_uncertainty_into_a_blank(self):
        readings = [reading("A", 0.40), reading("B", 0.02)]
        assert decide_group(readings, settings=SETTINGS).status is MarkStatus.UNCERTAIN

        lenient = RecognitionSettings(blank_ratio_threshold=0.50)
        assert decide_group(readings, settings=lenient).status is MarkStatus.BLANK

    def test_a_resolved_mark_is_at_full_confidence_by_construction(self):
        # Worth stating outright, because it means `min_confidence` can never
        # queue a resolved mark: resolving *requires* margin >= ambiguity_margin,
        # and confidence is that ratio clamped to 1.0. `ambiguity_margin` is the
        # knob that governs accepted marks; `min_confidence` governs the other
        # statuses (see the blank case below). Widening ambiguity_margin is the
        # supported way to demand more separation.
        for margin_setting in (0.05, 0.12, 0.30):
            settings = RecognitionSettings(
                ambiguity_margin=margin_setting, min_confidence=1.0
            )
            decision = decide_group(
                [reading("A", 0.95), reading("B", 0.02)], settings=settings
            )
            assert decision.status is MarkStatus.RESOLVED
            assert decision.confidence == pytest.approx(1.0)
            assert decision.needs_review is False

    def test_raising_the_minimum_confidence_queues_a_nearly_marked_blank(self):
        # A blank whose darkest bubble is creeping up on the "certainly empty"
        # ceiling: confidence falls, and a demanding template sends it to review.
        readings = [reading("A", 0.20), reading("B", 0.02)]
        relaxed = RecognitionSettings(blank_ratio_threshold=0.25, min_confidence=0.10)
        decision = decide_group(readings, settings=relaxed)
        assert decision.status is MarkStatus.BLANK
        assert decision.confidence == pytest.approx(0.2)
        assert decision.needs_review is False

        demanding = RecognitionSettings(blank_ratio_threshold=0.25, min_confidence=0.50)
        strict_decision = decide_group(readings, settings=demanding)
        assert strict_decision.status is MarkStatus.BLANK  # still reported as blank
        assert strict_decision.needs_review is True  # but shown to a human

    def test_a_zero_ambiguity_margin_is_satisfied_by_any_separation(self):
        settings = RecognitionSettings(ambiguity_margin=0.0)
        decision = decide_group(
            [reading("A", 0.60), reading("B", 0.54)], settings=settings
        )
        assert decision.status is MarkStatus.RESOLVED
        assert decision.confidence == pytest.approx(1.0)
