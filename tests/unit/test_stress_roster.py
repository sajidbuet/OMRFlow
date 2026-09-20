"""Unit tests for the deterministic synthetic stress roster generator.

Fast, pure-function tests only - no recognition, no database. The real,
end-to-end reconciliation-against-a-recognised-batch test lives in
``tests/integration/test_stress_reconciliation_and_reports.py``.
"""

from __future__ import annotations

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.domain.reconciliation import AttendanceState
from omr_scanner.evaluation import stress_dataset as sd
from omr_scanner.evaluation import stress_roster as sr
from omr_scanner.evaluation.test_cases import FieldLayout


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template(roll_digits=7)


@pytest.fixture(scope="module")
def layout(template):
    return FieldLayout.of(template)


@pytest.fixture
def spec():
    return sd.StressDatasetSpec(seed=20260920, sheet_count=2000)


class TestDeterminism:
    def test_the_same_spec_produces_an_identical_roster_every_time(self, spec, layout):
        first = sr.generate_stress_roster(spec, layout)
        second = sr.generate_stress_roster(spec, layout)
        assert first == second

    def test_a_different_seed_produces_a_different_roster(self, layout):
        spec_a = sd.StressDatasetSpec(seed=1, sheet_count=200)
        spec_b = sd.StressDatasetSpec(seed=2, sheet_count=200)
        ids_a = {record.candidate_id for record in sr.generate_stress_roster(spec_a, layout)}
        ids_b = {record.candidate_id for record in sr.generate_stress_roster(spec_b, layout)}
        assert ids_a != ids_b

    def test_absent_with_script_candidate_is_stable_across_calls(self, spec, layout):
        first = sr.absent_with_script_candidate_id(spec, layout)
        second = sr.absent_with_script_candidate_id(spec, layout)
        assert first is not None
        assert first == second


class TestRosterShape:
    def test_one_candidate_per_sheet_index(self, spec, layout):
        roster = sr.generate_stress_roster(spec, layout)
        # Injective in practice for a 7-digit identifier space at this scale
        # (10,000,000 possible rolls, 2,000 sheets) - the module tolerates a
        # collision rather than assuming its absence, but none is expected
        # here.
        assert len(roster) == spec.sheet_count

    def test_every_candidate_id_matches_the_natural_roll_scheme(self, spec, layout):
        roster = sr.generate_stress_roster(spec, layout)
        expected = {sd.natural_roll(spec, layout, index) for index in range(spec.sheet_count)}
        actual = {record.candidate_id for record in roster}
        assert actual == expected

    def test_exactly_one_candidate_is_marked_absent(self, spec, layout):
        roster = sr.generate_stress_roster(spec, layout)
        absent = [r for r in roster if r.imported_attendance is AttendanceState.ABSENT]
        assert len(absent) == 1
        assert absent[0].candidate_id == sr.absent_with_script_candidate_id(spec, layout)

    def test_the_forced_absent_candidate_is_a_clean_sheet(self, spec, layout):
        # A recognition-uncertainty guard: the one candidate this module
        # relies on being read back with an exact matching identifier must
        # come from an undistorted sheet, never a noisy kind.
        candidate_id = sr.absent_with_script_candidate_id(spec, layout)
        matching = [
            index
            for index in range(spec.sheet_count)
            if sd.natural_roll(spec, layout, index) == candidate_id
            and spec.kind_for_index(index) is sd.StressCaseKind.CLEAN
        ]
        assert matching


class TestReconciliationRelevantCaseKindsExist:
    """Confirms this (seed, sheet_count) pair actually draws the needed kinds.

    A guard against a seed change silently making the integration test's
    duplicate/unknown-candidate assertions vacuous.
    """

    def test_at_least_one_duplicate_generating_kind_is_drawn(self, spec):
        duplicate_kinds = {
            sd.StressCaseKind.DUPLICATE_ID,
            sd.StressCaseKind.DUPLICATE_ROLL_DIFFERENT_IMAGE,
            sd.StressCaseKind.EXACT_DUPLICATE_SCAN,
        }
        found = any(spec.kind_for_index(i) in duplicate_kinds for i in range(spec.sheet_count))
        assert found

    def test_at_least_one_unknown_candidate_kind_is_drawn(self, spec):
        found = any(
            spec.kind_for_index(i) is sd.StressCaseKind.UNKNOWN_CANDIDATE
            for i in range(spec.sheet_count)
        )
        assert found
