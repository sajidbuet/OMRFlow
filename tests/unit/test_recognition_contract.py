"""Tests for the published shape of a recognition result.

Why these matter more than most:
    :class:`~omr_scanner.services.recognition_models.ScanResult` is the
    boundary the rest of OMRFlow is built on. Phase 4's review screen, Phase
    5's persistence, Phase 8's scoring and the benchmark harness all consume
    it, and a future Recognition Engine v2 has to keep producing it. These
    tests are the executable half of that promise: they assert the contract,
    not the engine.

Nothing here runs recognition, opens an image or imports OpenCV. That is
itself part of what is being tested - a consumer of results must not need the
engine, and if these tests ever *did* need it, the boundary would have leaked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omr_scanner.recognition.models import FieldStatus, MarkStatus
from omr_scanner.services.recognition_models import (
    ENGINE_NAME,
    ENGINE_VERSION,
    RESULT_SCHEMA_VERSION,
    AnswerView,
    BubbleView,
    CharacterView,
    FieldView,
    MarkerView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanQuality,
    ScanResult,
    StageTimings,
    StatusCode,
    ZoneView,
    derive_status_codes,
    utc_timestamp,
)


def make_answer(
    number: int, value: str = "B", status: str = "resolved", **kwargs: object
) -> AnswerView:
    """One answer view, with sensible defaults for what a test is not about."""
    return AnswerView(
        number=number,
        zone_id=kwargs.get("zone_id", "questions_0"),
        value=value,
        status=status,
        needs_review=kwargs.get("needs_review", status != "resolved"),
        top_fill=kwargs.get("top_fill", 0.9),
        margin=kwargs.get("margin", 0.7),
        confidence=kwargs.get("confidence", 0.95),
    )


def make_field(zone_id: str, value: str, status: str, positions: int = 2) -> FieldView:
    """One field view with ``positions`` character positions."""
    return FieldView(
        zone_id=zone_id,
        label=zone_id.replace("_", " ").title(),
        field_type="numeric",
        value=value,
        status=status,
        needs_review=status != FieldStatus.RESOLVED.value,
        characters=tuple(
            CharacterView(
                position=index,
                value=value[index] if index < len(value) else "",
                status=MarkStatus.RESOLVED.value,
                top_fill=0.9,
                margin=0.7,
                confidence=0.95,
            )
            for index in range(positions)
        ),
    )


def make_result(**kwargs: object) -> ScanResult:
    """A fully populated result, for the serialisation tests."""
    defaults: dict = {
        "source_path": Path("scans/sheet_001.png"),
        "outcome": RecognitionOutcome.REVIEW,
        "registration": RegistrationStatus.REGISTERED_WITH_WARNING,
        "registration_message": "Registered, but with reservations.",
        "warnings": ("MULTIPLE_CORNER_CANDIDATES",),
        "status_codes": ("ALIGNMENT_WARNING", "MULTIPLE_MARK"),
        "fields": (make_field("roll_number", "12", FieldStatus.RESOLVED.value),),
        "answers": (make_answer(1), make_answer(2, "B-D", MarkStatus.MULTIPLE.value)),
        "identifier_zone_id": "roll_number",
        "set_code_zone_id": None,
        "zones": (
            ZoneView(
                zone_id="roll_number",
                label="Roll number",
                field_type="numeric",
                x=10.0,
                y=20.0,
                width=100.0,
                height=200.0,
                color="#3366CC",
                status="resolved",
            ),
        ),
        "bubbles": (
            BubbleView(
                zone_id="roll_number",
                row=0,
                column=0,
                label="1",
                x=15.5,
                y=25.5,
                width=12.0,
                height=10.0,
                fill_ratio=0.87,
                selected=True,
                leading=True,
                group_status="resolved",
                mean_darkness=0.72,
                contrast=0.61,
                paper_level=248.0,
                ink_threshold=176.5,
                sample_pixels=182,
                usable=True,
                rank=0,
            ),
        ),
        "markers": (MarkerView(role="top_left", x=120.0, y=118.0, score=0.93),),
        "canonical_width": 1240,
        "canonical_height": 1754,
        "source_width": 2480,
        "source_height": 3508,
        "elapsed_seconds": 0.42,
        "template_id": "b9f2",
        "template_name": "Sample sheet",
        "template_version": 1,
        "recognised_at": "2026-09-18T09:00:00+00:00",
        "quality": ScanQuality(marker_count=4, min_marker_score=0.91, brightness=0.95),
        "timings": StageTimings(load=0.1, register=0.2, measure=0.1, total=0.42),
    }
    defaults.update(kwargs)
    return ScanResult(**defaults)


class TestEngineIdentity:
    def test_a_result_records_which_engine_produced_it(self):
        result = make_result()
        assert result.engine_name == ENGINE_NAME
        assert result.engine_version == ENGINE_VERSION

    def test_the_engine_version_is_not_the_application_version(self):
        from omr_scanner import __version__

        # They are allowed to coincide by accident, but they must be separate
        # strings: a benchmark compares engines, not releases.
        assert __version__ != ENGINE_VERSION

    def test_the_schema_version_is_recorded_in_the_document(self):
        assert make_result().to_dict()["schema_version"] == RESULT_SCHEMA_VERSION

    def test_a_result_records_the_template_it_was_read_with(self):
        result = make_result()
        payload = result.to_dict()["template"]
        assert payload == {"id": "b9f2", "name": "Sample sheet", "format_version": 1}


class TestSerialisation:
    def test_a_result_round_trips_through_json(self):
        original = make_result()
        restored = ScanResult.from_dict(json.loads(json.dumps(original.to_dict())))

        assert restored.source_path == original.source_path
        assert restored.outcome is original.outcome
        assert restored.registration is original.registration
        assert restored.answers == original.answers
        assert restored.fields == original.fields
        assert restored.bubbles == original.bubbles
        assert restored.zones == original.zones
        assert restored.markers == original.markers
        assert restored.quality == original.quality
        assert restored.timings == original.timings
        assert restored.status_codes == original.status_codes

    def test_the_preview_image_is_never_serialised(self):
        # A JSON document full of base64 pixels helps nobody, and a result is
        # data; the picture is regenerated when something wants to look at it.
        assert "preview" not in make_result().to_dict()

    def test_every_value_in_the_document_is_json_native(self):
        payload = make_result().to_dict()
        # json.dumps with no default= raises on anything it cannot represent,
        # which is exactly the assertion: no Path, no Enum, no NumPy scalar.
        json.dumps(payload)

    def test_a_document_from_a_newer_schema_is_refused(self):
        payload = make_result().to_dict()
        payload["schema_version"] = RESULT_SCHEMA_VERSION + 1
        with pytest.raises(ValueError, match="newer than this build"):
            ScanResult.from_dict(payload)

    def test_unknown_keys_are_ignored_rather_than_fatal(self):
        payload = make_result().to_dict()
        payload["something_a_later_version_added"] = 42
        payload["answers"][0]["also_new"] = "x"
        restored = ScanResult.from_dict(payload)
        assert restored.answers[0].number == 1

    def test_a_minimal_document_loads_on_its_defaults(self):
        restored = ScanResult.from_dict({"source_path": "a.png", "outcome": "error",
                                         "registration": "registration_failed"})
        assert restored.answers == ()
        assert restored.quality is None
        assert restored.timings == StageTimings()

    def test_a_non_finite_measurement_becomes_null_rather_than_invalid_json(self):
        result = make_result(elapsed_seconds=float("nan"))
        payload = result.to_dict()
        assert payload["elapsed_seconds"] is None
        json.dumps(payload)  # NaN would produce a document many parsers reject


class TestConvenienceAccessors:
    def test_the_identifier_is_found_through_its_zone_id(self):
        result = make_result()
        assert result.identifier_value == "12"
        assert result.identifier_is_reliable is True

    def test_an_unresolved_identifier_is_not_reliable(self):
        result = make_result(
            fields=(make_field("roll_number", "1?", FieldStatus.UNCERTAIN.value),)
        )
        assert result.identifier_is_reliable is False

    def test_an_unregistered_sheet_never_has_a_reliable_identifier(self):
        result = make_result(registration=RegistrationStatus.FAILED)
        assert result.identifier_is_reliable is False

    def test_answers_can_be_looked_up_by_question_number(self):
        result = make_result()
        assert result.answer(2).value == "B-D"
        assert result.answer(99) is None

    def test_bubbles_can_be_filtered_by_zone(self):
        result = make_result()
        assert len(result.bubbles_for("roll_number")) == 1
        assert result.bubbles_for("questions_0") == ()

    def test_has_status_accepts_a_code_or_its_string(self):
        result = make_result()
        assert result.has_status(StatusCode.MULTIPLE_MARK)
        assert result.has_status("ALIGNMENT_WARNING")
        assert not result.has_status(StatusCode.BLANK)

    def test_review_count_covers_fields_and_answers(self):
        result = make_result()
        assert result.review_count == 1  # the multiply-marked answer


class TestBubbleEvidence:
    def test_a_bubble_carries_the_numbers_behind_its_decision(self):
        bubble = make_result().bubbles[0]
        # The point of keeping these: a future recalibration can ask "what
        # would a threshold of 0.6 have decided?" without re-reading any image.
        assert bubble.fill_ratio == pytest.approx(0.87)
        assert bubble.ink_threshold == pytest.approx(176.5)
        assert bubble.paper_level == pytest.approx(248.0)
        assert bubble.sample_pixels == 182
        assert bubble.rank == 0

    def test_the_evidence_fields_default_so_an_older_document_still_loads(self):
        minimal = BubbleView(
            zone_id="z", row=0, column=0, label="A", x=1.0, y=2.0,
            width=3.0, height=4.0, fill_ratio=0.5, selected=False,
            leading=False, group_status="resolved",
        )
        assert minimal.ink_threshold == 0.0
        assert minimal.usable is True


class TestStatusCodes:
    def test_a_clean_sheet_is_just_ok(self):
        codes = derive_status_codes(
            outcome=RecognitionOutcome.COMPLETE,
            registration=RegistrationStatus.REGISTERED,
            error_code="",
            identifier=make_field("roll", "12", FieldStatus.RESOLVED.value),
            set_code=None,
            answers=(make_answer(1),),
            fields_=(),
            has_zones=True,
        )
        assert codes == ("OK",)

    def test_several_conditions_are_reported_together(self):
        codes = derive_status_codes(
            outcome=RecognitionOutcome.REVIEW,
            registration=RegistrationStatus.REGISTERED_WITH_WARNING,
            error_code="",
            identifier=make_field("roll", "1?", FieldStatus.UNCERTAIN.value),
            set_code=None,
            answers=(
                make_answer(1, "B-D", MarkStatus.MULTIPLE.value),
                make_answer(2, "", MarkStatus.BLANK.value),
            ),
            fields_=(),
            has_zones=True,
        )
        assert set(codes) == {
            "ALIGNMENT_WARNING",
            "MULTIPLE_MARK",
            "BLANK",
            "ROLL_UNREADABLE",
        }
        assert "OK" not in codes

    @pytest.mark.parametrize(
        ("error_code", "expected"),
        [
            ("INVALID_IMAGE", StatusCode.IMAGE_LOAD_ERROR),
            ("INSUFFICIENT_MARKERS", StatusCode.MARKER_NOT_FOUND),
            ("AMBIGUOUS_MARKERS", StatusCode.MARKER_NOT_FOUND),
            ("ORIENTATION_NOT_FOUND", StatusCode.ORIENTATION_FAILED),
            ("ALIGNMENT_TRANSFORM_FAILED", StatusCode.ALIGNMENT_FAILED),
        ],
    )
    def test_every_imaging_failure_code_maps_to_a_status(self, error_code, expected):
        codes = derive_status_codes(
            outcome=RecognitionOutcome.REGISTRATION_FAILED,
            registration=RegistrationStatus.FAILED,
            error_code=error_code,
            identifier=None,
            set_code=None,
            answers=(),
            fields_=(),
            has_zones=True,
        )
        assert expected.value in codes

    def test_an_unrecognised_failure_code_is_still_reported(self):
        codes = derive_status_codes(
            outcome=RecognitionOutcome.ERROR,
            registration=RegistrationStatus.FAILED,
            error_code="SOMETHING_NEW",
            identifier=None,
            set_code=None,
            answers=(),
            fields_=(),
            has_zones=True,
        )
        assert StatusCode.PROCESSING_ERROR.value in codes

    def test_a_template_with_no_zones_is_reported_as_invalid(self):
        codes = derive_status_codes(
            outcome=RecognitionOutcome.COMPLETE,
            registration=RegistrationStatus.REGISTERED,
            error_code="",
            identifier=None,
            set_code=None,
            answers=(),
            fields_=(),
            has_zones=False,
        )
        assert StatusCode.INVALID_TEMPLATE.value in codes

    def test_codes_are_sorted_so_two_equal_results_compare_equal(self):
        codes = derive_status_codes(
            outcome=RecognitionOutcome.REVIEW,
            registration=RegistrationStatus.REGISTERED_WITH_WARNING,
            error_code="",
            identifier=None,
            set_code=None,
            answers=(make_answer(1, "", MarkStatus.BLANK.value),),
            fields_=(),
            has_zones=True,
        )
        assert list(codes) == sorted(codes)


class TestTimestamps:
    def test_the_timestamp_is_utc_and_sortable(self):
        stamp = utc_timestamp()
        assert stamp.endswith("+00:00")
        # ISO-8601 in UTC sorts lexicographically, which is what a batch
        # processed across midnight depends on.
        assert stamp > "2026-01-01T00:00:00+00:00"
