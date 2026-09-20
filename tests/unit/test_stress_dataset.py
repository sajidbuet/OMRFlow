"""Unit tests for the deterministic stress dataset generator.

Covers the seed+index-addressable sheet generator behind the mandatory
100,000-sheet production-hardening stress test (Phase 10, §19-§22).
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.evaluation import stress_dataset as sd


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template(roll_digits=7)


@pytest.fixture(scope="module")
def spec():
    return sd.StressDatasetSpec(seed=20260920, sheet_count=5000)


class TestDeterminism:
    def test_plan_for_index_is_pure(self, spec, template) -> None:
        assert sd.plan_for_index(spec, template, 12345) == sd.plan_for_index(
            spec, template, 12345
        )

    def test_render_sheet_for_index_is_byte_identical_across_calls(self, spec, template) -> None:
        first = sd.render_sheet_for_index(spec, template, 777)
        second = sd.render_sheet_for_index(spec, template, 777)
        assert first.png_bytes == second.png_bytes
        assert first.malformed_bytes == second.malformed_bytes

    def test_a_different_seed_produces_a_different_dataset(self, template) -> None:
        spec_a = sd.StressDatasetSpec(seed=1, sheet_count=100)
        spec_b = sd.StressDatasetSpec(seed=2, sheet_count=100)
        case_a = sd.plan_for_index(spec_a, template, 0)
        case_b = sd.plan_for_index(spec_b, template, 0)
        assert case_a.roll != case_b.roll

    def test_kind_for_index_does_not_depend_on_sheet_count(self, template) -> None:
        small = sd.StressDatasetSpec(seed=5, sheet_count=10)
        large = sd.StressDatasetSpec(seed=5, sheet_count=100_000)
        assert small.kind_for_index(7) == large.kind_for_index(7)


class TestDistribution:
    def test_the_distribution_sums_to_one(self) -> None:
        assert abs(sum(sd.DEFAULT_STRESS_DISTRIBUTION.values()) - 1.0) < 1e-9

    def test_every_kind_is_reachable_over_a_large_enough_sample(self, spec, template) -> None:
        seen = {spec.kind_for_index(i) for i in range(5000)}
        assert seen == set(sd.StressCaseKind)

    def test_clean_is_the_dominant_kind(self, spec) -> None:
        counts = dict.fromkeys(sd.StressCaseKind, 0)
        for i in range(5000):
            counts[spec.kind_for_index(i)] += 1
        assert counts[sd.StressCaseKind.CLEAN] > sum(counts.values()) * 0.5


class TestVirtualSourcePaths:
    def test_round_trips_through_pathlib_on_windows(self, spec) -> None:
        from pathlib import Path

        raw = sd.virtual_source_path(spec, 78431)
        as_path_str = str(Path(raw))
        assert sd.is_stress_source(as_path_str)
        assert sd.parse_virtual_source_path(as_path_str) == (spec.seed, 78431)

    def test_is_stress_source_rejects_a_real_path(self) -> None:
        assert not sd.is_stress_source(r"C:\scans\a.png")

    def test_every_index_gets_a_unique_path(self, spec) -> None:
        paths = {sd.virtual_source_path(spec, i) for i in range(1000)}
        assert len(paths) == 1000


class TestNaturalRoll:
    def test_rolls_are_unique_across_a_large_range(self, template, spec) -> None:
        layout = sd.FieldLayout.of(template)
        rolls = {sd.natural_roll(spec, layout, i) for i in range(5000)}
        assert len(rolls) == 5000

    def test_roll_length_matches_the_template(self, template, spec) -> None:
        layout = sd.FieldLayout.of(template)
        roll = sd.natural_roll(spec, layout, 42)
        assert len(roll) == layout.identifier_columns


class TestSpecialCaseKinds:
    def test_exact_duplicate_scan_matches_its_partner_byte_for_byte(self, spec, template) -> None:
        # Find an index the distribution actually assigned to this kind.
        target = next(
            i
            for i in range(5000)
            if spec.kind_for_index(i) is sd.StressCaseKind.EXACT_DUPLICATE_SCAN
        )
        partner_index = target - 1 if target > 0 else target + 1
        rendered = sd.render_sheet_for_index(spec, template, target)
        partner_clean = sd._clean_case(spec, sd.FieldLayout.of(template), partner_index)
        from omr_scanner.evaluation.synthetic_dataset import render_case

        expected = render_case(template, partner_clean, seed=spec.seed)
        ok, expected_bytes = cv2.imencode(".png", expected.image)
        assert ok
        assert rendered.png_bytes == expected_bytes.tobytes()

    def test_duplicate_id_shares_a_roll_but_not_content(self, spec, template) -> None:
        target = next(
            i for i in range(5000) if spec.kind_for_index(i) is sd.StressCaseKind.DUPLICATE_ID
        )
        partner_index = target - 1 if target > 0 else target + 1
        case = sd.plan_for_index(spec, template, target)
        layout = sd.FieldLayout.of(template)
        assert case.roll == sd.natural_roll(spec, layout, partner_index)

        rendered_target = sd.render_sheet_for_index(spec, template, target)
        rendered_partner = sd.render_sheet_for_index(spec, template, partner_index)
        # Same roll, but not required to be pixel-identical (different rng draw).
        if not rendered_target.is_malformed and not rendered_partner.is_malformed:
            assert (
                rendered_target.png_bytes != rendered_partner.png_bytes
                or target == partner_index
            )

    def test_malformed_sheets_are_not_decodable_images(self, spec, template) -> None:
        target = next(
            i for i in range(5000) if spec.kind_for_index(i) is sd.StressCaseKind.MALFORMED
        )
        rendered = sd.render_sheet_for_index(spec, template, target)
        assert rendered.is_malformed
        buffer = np.frombuffer(rendered.malformed_bytes, dtype=np.uint8)
        decoded = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        assert decoded is None

    def test_blank_id_leaves_the_identifier_unmarked(self, spec, template) -> None:
        target = next(
            i for i in range(5000) if spec.kind_for_index(i) is sd.StressCaseKind.BLANK_ID
        )
        case = sd.plan_for_index(spec, template, target)
        assert case.roll == "_" * len(case.roll)

    def test_unknown_candidate_roll_is_outside_the_natural_range(self, spec, template) -> None:
        target = next(
            i for i in range(5000) if spec.kind_for_index(i) is sd.StressCaseKind.UNKNOWN_CANDIDATE
        )
        layout = sd.FieldLayout.of(template)
        case = sd.plan_for_index(spec, template, target)
        natural_rolls = {sd.natural_roll(spec, layout, i) for i in range(spec.sheet_count)}
        assert case.roll not in natural_rolls

    def test_multiple_answer_reports_more_than_one_option(self, spec, template) -> None:
        target = next(
            i for i in range(5000) if spec.kind_for_index(i) is sd.StressCaseKind.MULTIPLE_ANSWER
        )
        case = sd.plan_for_index(spec, template, target)
        first_question = min(case.answers)
        assert "-" in case.answers[first_question]
