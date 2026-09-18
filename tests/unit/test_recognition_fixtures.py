"""Tests for the committed recognition-result fixtures.

What these are really guarding:
    ``tests/fixtures/recognition/*.json`` is the data Phase 4 and Phase 5 will
    be written against before either phase can run recognition for itself. If a
    schema change makes them unloadable, or a careless edit makes one of them
    no longer represent its scenario, those phases start from a broken
    foundation - and the breakage would otherwise surface months later, in a
    different part of the application.

Deliberately no OpenCV, no template, no engine: loading a stored result must
work with nothing but the result model, which is the whole reason the fixtures
exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omr_scanner.services.recognition_models import (
    RESULT_SCHEMA_VERSION,
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    StatusCode,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "recognition"

EXPECTED_FIXTURES = (
    "alignment_warning",
    "blank_answers",
    "duplicate_roll_a",
    "duplicate_roll_b",
    "invalid_roll",
    "invalid_set",
    "low_confidence",
    "mixed_ambiguity",
    "multiple_marks",
    "orientation_failure",
    "perfect_scan",
)
"""Every scenario that must exist. Named explicitly rather than globbed: a
fixture silently disappearing is exactly the failure this list catches."""


def load(name: str) -> ScanResult:
    """Load one fixture by name."""
    payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return ScanResult.from_dict(payload)


class TestTheFixtureSetIsIntact:
    def test_every_documented_fixture_is_present(self):
        present = sorted(path.stem for path in FIXTURE_DIR.glob("*.json"))
        assert present == sorted(EXPECTED_FIXTURES)

    def test_the_directory_documents_itself(self):
        assert (FIXTURE_DIR / "README.md").is_file()

    @pytest.mark.parametrize("name", EXPECTED_FIXTURES)
    def test_each_fixture_declares_the_current_schema_version(self, name: str):
        payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert payload["schema_version"] == RESULT_SCHEMA_VERSION

    @pytest.mark.parametrize("name", EXPECTED_FIXTURES)
    def test_each_fixture_loads_into_the_result_model(self, name: str):
        result = load(name)
        assert result.source_path.name == f"{name}.png"
        assert result.engine_version
        assert result.template_id

    @pytest.mark.parametrize("name", EXPECTED_FIXTURES)
    def test_each_fixture_round_trips_unchanged(self, name: str):
        original = load(name)
        again = ScanResult.from_dict(json.loads(json.dumps(original.to_dict())))
        assert again.to_dict() == original.to_dict()

    @pytest.mark.parametrize("name", EXPECTED_FIXTURES)
    def test_no_fixture_carries_a_developers_absolute_path(self, name: str):
        # A committed fixture that says "C:/Users/someone/..." leaks a machine
        # layout and makes every rebuild a diff.
        text = (FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
        assert ":\\" not in text
        assert "/home/" not in text

    @pytest.mark.parametrize("name", EXPECTED_FIXTURES)
    def test_no_fixture_carries_a_rebuild_timestamp(self, name: str):
        assert load(name).recognised_at == ""


class TestEachScenarioIsWhatItClaims:
    def test_perfect_scan_resolved_everything(self):
        result = load("perfect_scan")
        assert result.outcome is RecognitionOutcome.COMPLETE
        assert result.identifier_value == "120317"
        assert result.review_count == 0
        assert all(answer.value for answer in result.answers)

    def test_blank_answers_has_unanswered_questions(self):
        result = load("blank_answers")
        assert result.has_status(StatusCode.BLANK)
        assert [answer.number for answer in result.answers if answer.value == ""]

    def test_multiple_marks_keeps_both_marks(self):
        result = load("multiple_marks")
        assert result.has_status(StatusCode.MULTIPLE_MARK)
        doubled = [answer for answer in result.answers if "-" in answer.value]
        assert len(doubled) == 2
        # Both marks survive to the value; neither is silently discarded.
        assert doubled[0].value == "A-C"
        assert doubled[0].needs_review is True

    def test_low_confidence_flags_rather_than_guesses(self):
        result = load("low_confidence")
        assert result.has_status(StatusCode.LOW_CONFIDENCE)
        uncertain = [answer for answer in result.answers if answer.status == "uncertain"]
        assert uncertain
        assert all(answer.needs_review for answer in uncertain)

    def test_invalid_roll_is_not_usable_as_an_identity(self):
        result = load("invalid_roll")
        assert result.has_status(StatusCode.ROLL_UNREADABLE)
        assert result.identifier_is_reliable is False
        assert "_" in result.identifier_value  # the blank column is visible

    def test_invalid_set_reports_the_set_code_as_unreadable(self):
        result = load("invalid_set")
        assert result.has_status(StatusCode.SET_UNREADABLE)
        assert result.set_code is not None
        assert result.set_code.status != "resolved"

    def test_mixed_ambiguity_carries_several_conditions_at_once(self):
        result = load("mixed_ambiguity")
        assert result.has_status(StatusCode.BLANK)
        assert result.has_status(StatusCode.MULTIPLE_MARK)
        assert result.has_status(StatusCode.LOW_CONFIDENCE)

    def test_the_duplicate_pair_claims_one_roll_number(self):
        first, second = load("duplicate_roll_a"), load("duplicate_roll_b")
        assert first.identifier_value == second.identifier_value
        assert first.identifier_is_reliable and second.identifier_is_reliable
        # Different sheets, though - a reviewer has to be able to tell them apart.
        assert first.answers[0].value != second.answers[0].value

    def test_alignment_warning_registered_but_with_a_reservation(self):
        result = load("alignment_warning")
        assert result.registration is RegistrationStatus.REGISTERED_WITH_WARNING
        assert result.warnings
        assert result.has_status(StatusCode.ALIGNMENT_WARNING)

    def test_orientation_failure_reads_nothing_at_all(self):
        result = load("orientation_failure")
        assert result.outcome is RecognitionOutcome.REGISTRATION_FAILED
        assert result.registration is RegistrationStatus.FAILED
        assert result.has_status(StatusCode.ORIENTATION_FAILED)
        # The rule that matters: an unregistered page never produces answers.
        assert result.answers == ()
        assert result.identifier_value == ""


class TestAFixtureIsUsableWithoutTheEngine:
    def test_a_consumer_can_work_entirely_from_stored_results(self):
        # This is the Phase 4 rehearsal: build a review queue from fixtures,
        # touching no image, no template and no recognition code.
        queue = [
            (result.source_path.name, result.review_count)
            for name in EXPECTED_FIXTURES
            for result in [load(name)]
            if result.review_count
        ]
        assert queue, "no fixture needs review - the set has lost its point"
        assert all(count > 0 for _name, count in queue)

    def test_bubble_evidence_survives_the_round_trip_to_disk(self):
        # The evidence is what a future recalibration re-scores; a fixture
        # without it would let that capability rot unnoticed.
        result = load("perfect_scan")
        assert result.bubbles
        selected = [bubble for bubble in result.bubbles if bubble.selected]
        assert selected
        assert all(bubble.ink_threshold > 0 for bubble in selected)
        assert all(bubble.sample_pixels > 0 for bubble in selected)
