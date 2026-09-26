"""Tests for what deserves human review (Phase 6).

Scope:
    :mod:`omr_scanner.services.conflict_policy` is a pure function of a
    recognition result and a template, so every conflict type can be generated
    here without rendering a sheet - which is the point: a taxonomy that could
    only be exercised through image recognition would be tested once, slowly,
    and never at its edges.

The property that matters most:
    Detection is **deterministic and stable**. The same result must always
    produce the same conflicts with the same keys, because those keys are what
    make storing them idempotent - and therefore what stops a Phase 5 retry
    filling the review queue with duplicates.
"""

from __future__ import annotations

import pytest
from tests.conftest import build_answer_sheet_template
from tests.unit.test_recognition_contract import make_answer, make_result

from omr_scanner.domain.review import (
    WHOLE_FIELD,
    ConflictScope,
    ConflictType,
    FieldKind,
)
from omr_scanner.services.conflict_policy import (
    ConflictPolicy,
    describe_template_choices,
    detect_conflicts,
    detect_duplicate_identifiers,
    group_cells,
    group_labels,
)
from omr_scanner.services.recognition_models import (
    CharacterView,
    FieldView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanQuality,
    StatusCode,
)


@pytest.fixture
def template():
    return build_answer_sheet_template()


def character(
    position: int,
    value: str = "1",
    status: str = "resolved",
    *,
    confidence: float = 1.0,
    top_fill: float = 0.9,
    margin: float = 0.8,
) -> CharacterView:
    """One identifier/set-code position with an explicit status."""
    return CharacterView(
        position=position,
        value=value,
        status=status,
        top_fill=top_fill,
        margin=margin,
        confidence=confidence,
    )


def field_view(zone_id: str, label: str, characters, value: str = "", status: str = "resolved"):
    """One recognised field built from explicit character positions."""
    return FieldView(
        zone_id=zone_id,
        label=label,
        field_type="numeric",
        value=value,
        status=status,
        needs_review=status != "resolved",
        characters=tuple(characters),
    )


def clean_result(**kwargs: object):
    """A result with nothing wrong with it, overridable field by field."""
    defaults = {
        "outcome": RecognitionOutcome.COMPLETE,
        "registration": RegistrationStatus.REGISTERED,
        "warnings": (),
        "status_codes": ("OK",),
        "fields": (),
        "answers": (),
        "bubbles": (),
        "identifier_zone_id": "roll_number",
        "set_code_zone_id": "set_code",
    }
    defaults.update(kwargs)
    return make_result(**defaults)


def types_of(conflicts) -> set[ConflictType]:
    return {item.conflict_type for item in conflicts}


class TestNothingWrongProducesNothing:
    def test_a_clean_sheet_has_no_conflicts(self, template):
        result = clean_result(
            fields=(
                field_view("roll_number", "Roll", [character(i) for i in range(6)]),
            ),
            answers=(make_answer(1), make_answer(2)),
        )
        assert detect_conflicts(result, template) == ()

    def test_a_blank_answer_is_not_a_conflict(self, template):
        # A candidate is entitled to leave a question blank.
        result = clean_result(
            answers=(make_answer(1, "", "blank", needs_review=False),) * 3
        )
        assert detect_conflicts(result, template) == ()


class TestIdentifierConflicts:
    def test_a_wholly_blank_identifier_is_one_conflict_not_six(self, template):
        result = clean_result(
            fields=(
                field_view(
                    "roll_number",
                    "Roll",
                    [character(i, "", "blank") for i in range(6)],
                    value="______",
                    status="blank",
                ),
            )
        )
        found = detect_conflicts(result, template)
        assert len(found) == 1
        assert found[0].conflict_type is ConflictType.IDENTIFIER_BLANK
        assert found[0].field.group_key == WHOLE_FIELD

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("blank", ConflictType.IDENTIFIER_INCOMPLETE),
            ("multiple", ConflictType.IDENTIFIER_MULTIPLE),
            ("uncertain", ConflictType.IDENTIFIER_UNCERTAIN),
            ("unreadable", ConflictType.IDENTIFIER_UNREADABLE),
        ],
    )
    def test_each_bad_position_maps_to_its_own_type(self, template, status, expected):
        characters = [character(0, "1"), character(1, "?", status), character(2, "3")]
        result = clean_result(
            fields=(field_view("roll_number", "Roll", characters, value="1?3"),)
        )
        found = detect_conflicts(result, template)
        assert types_of(found) == {expected}
        assert found[0].field.group_key == 1
        assert found[0].field.kind is FieldKind.IDENTIFIER

    def test_a_resolved_but_low_confidence_digit_is_flagged(self, template):
        characters = [character(0), character(1, "7", confidence=0.4), character(2)]
        result = clean_result(
            fields=(field_view("roll_number", "Roll", characters, value="173"),)
        )
        found = detect_conflicts(result, template)
        assert types_of(found) == {ConflictType.IDENTIFIER_LOW_CONFIDENCE}
        # The machine's own reading travels with the conflict, unchanged.
        assert found[0].observation.value == "7"
        assert found[0].observation.confidence == pytest.approx(0.4)

    def test_a_fully_confident_identifier_raises_nothing(self, template):
        result = clean_result(
            fields=(
                field_view("roll_number", "Roll", [character(i) for i in range(6)]),
            )
        )
        assert detect_conflicts(result, template) == ()


class TestSetCodeConflicts:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ("blank", ConflictType.SET_CODE_BLANK),
            ("multiple", ConflictType.SET_CODE_MULTIPLE),
            ("uncertain", ConflictType.SET_CODE_UNCERTAIN),
            ("unreadable", ConflictType.SET_CODE_UNREADABLE),
        ],
    )
    def test_each_set_code_problem_maps_to_its_own_type(self, template, status, expected):
        result = clean_result(
            fields=(
                field_view("set_code", "Set", [character(0, "?", status)], value="?"),
            )
        )
        found = detect_conflicts(result, template)
        assert types_of(found) == {expected}
        assert found[0].field.kind is FieldKind.SET_CODE

    def test_a_multi_position_set_code_is_reported_per_position(self, template):
        # Set codes may be multi-digit ("10", "11", "12"). Phase 6 must not
        # regress that into an assumption of one character.
        characters = [character(0, "1"), character(1, "?", "multiple")]
        result = clean_result(
            fields=(field_view("set_code", "Set", characters, value="1?"),)
        )
        found = detect_conflicts(result, template)
        assert len(found) == 1
        assert found[0].field.group_key == 1


class TestAnswersAreNeverConflicts:
    """Whatever a question says, it never reaches the review queue.

    An ambiguous answer is a recognition result, not a dispute about identity:
    nobody has to decide it before the batch can go on.
    """

    @pytest.mark.parametrize(
        "status", ["multiple", "uncertain", "unreadable", "resolved", "blank"]
    )
    def test_no_answer_status_produces_a_conflict(self, template, status):
        result = clean_result(
            answers=(make_answer(1, "B-D", status, needs_review=True),)
        )
        assert detect_conflicts(result, template) == ()

    def test_a_hundred_ambiguous_answers_produce_nothing(self, template):
        # The failure this change exists to fix: a queue of one entry per
        # double-marked question, in which the two that mattered could not be
        # found.
        result = clean_result(
            answers=tuple(
                make_answer(n, "A-C", "multiple", needs_review=True)
                for n in range(1, 21)
            )
        )
        assert detect_conflicts(result, template) == ()

    def test_ambiguous_answers_beside_a_disputed_identifier_leave_only_the_identifier(
        self, template
    ):
        result = clean_result(
            fields=(
                field_view(
                    "roll_number", "Roll", [character(0, "?", "multiple")], value="?"
                ),
                field_view("set_code", "Set", [character(0, "?", "multiple")], value="?"),
            ),
            answers=tuple(
                make_answer(n, "A-C", "multiple", needs_review=True)
                for n in range(1, 11)
            ),
        )
        found = detect_conflicts(result, template)
        assert types_of(found) == {
            ConflictType.IDENTIFIER_MULTIPLE,
            ConflictType.SET_CODE_MULTIPLE,
        }
        assert all(item.field.kind is not FieldKind.QUESTION for item in found)

    def test_the_answer_types_are_still_nameable_for_an_old_project(self):
        # Not raised any more, but a project written by an earlier build stores
        # these strings. Refusing to name one would make it unreadable.
        assert ConflictType("answer_multiple") is ConflictType.ANSWER_MULTIPLE
        assert ConflictType.ANSWER_MULTIPLE.requires_resolution is False
        assert ConflictType.IDENTIFIER_MULTIPLE.requires_resolution is True
        assert ConflictType.SET_CODE_MULTIPLE.requires_resolution is True
        assert ConflictType.REGISTRATION_FAILED.requires_resolution is True


class TestSheetLevelConflicts:
    def test_a_failed_registration_produces_exactly_one_conflict(self, template):
        # A page that never registered has no fields, answers or bubbles to
        # dispute. A hundred "unreadable answer" conflicts would be noise
        # standing in for the single fact that matters.
        result = make_result(
            outcome=RecognitionOutcome.REGISTRATION_FAILED,
            registration=RegistrationStatus.FAILED,
            registration_message="Only 2 of 4 registration markers were found.",
            status_codes=("ALIGNMENT_FAILED", "MARKER_NOT_FOUND"),
            fields=(),
            answers=(),
            bubbles=(),
            warnings=(),
        )
        found = detect_conflicts(result, template)
        assert len(found) == 1
        assert found[0].conflict_type is ConflictType.REGISTRATION_FAILED
        assert found[0].conflict_type.scope is ConflictScope.SHEET
        assert "2 of 4" in found[0].observation.detail

    def test_an_undecodable_image_is_a_processing_failure_not_a_value(self, template):
        result = make_result(
            outcome=RecognitionOutcome.ERROR,
            registration=RegistrationStatus.FAILED,
            status_codes=(StatusCode.IMAGE_LOAD_ERROR.value,),
            fields=(),
            answers=(),
            bubbles=(),
            warnings=(),
        )
        found = detect_conflicts(result, template)
        assert types_of(found) == {ConflictType.IMAGE_UNREADABLE}
        # Not a value a reviewer can choose between, and the model says so, so
        # the UI cannot offer "pick A, B, C or D" for a corrupt JPEG.
        assert found[0].conflict_type.is_processing_failure is True
        assert found[0].conflict_type.allows_value_correction is False

    def test_an_assumed_orientation_is_flagged_by_default(self, template):
        result = clean_result(quality=ScanQuality(orientation_assumed=True))
        found = detect_conflicts(result, template)
        assert types_of(found) == {ConflictType.ORIENTATION_ASSUMED}

    def test_alignment_warnings_are_not_flagged_by_default(self, template):
        # The repository's own real sample raises MULTIPLE_CORNER_CANDIDATES on
        # every sheet; one conflict per sheet for it would drown the queue.
        result = clean_result(warnings=("MULTIPLE_CORNER_CANDIDATES",))
        assert detect_conflicts(result, template) == ()

    def test_alignment_warnings_can_be_switched_on(self, template):
        result = clean_result(warnings=("LARGE_REPROJECTION_ERROR",))
        found = detect_conflicts(
            result, template, policy=ConflictPolicy(flag_alignment_warnings=True)
        )
        assert types_of(found) == {ConflictType.ALIGNMENT_WARNING}


class TestDuplicateIdentifiers:
    def test_two_sheets_with_the_same_id_both_get_a_conflict(self):
        found = detect_duplicate_identifiers([(1, "170501"), (2, "170501"), (3, "170502")])
        assert set(found) == {1, 2}
        assert found[1].conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        # Each one points at the other, so the review interface can offer to
        # jump between the sheets involved.
        assert found[1].related_scan_ids == (2,)
        assert found[2].related_scan_ids == (1,)

    def test_three_sheets_all_reference_the_other_two(self):
        found = detect_duplicate_identifiers([(1, "A"), (2, "A"), (3, "A")])
        assert found[2].related_scan_ids == (1, 3)

    def test_a_unique_identifier_produces_nothing(self):
        assert detect_duplicate_identifiers([(1, "A"), (2, "B")]) == {}

    def test_blank_identifiers_are_never_duplicates_of_each_other(self):
        # Two sheets with no readable ID are not evidence of a duplicate
        # candidate; they already have their own conflicts.
        assert detect_duplicate_identifiers([(1, ""), (2, ""), (3, "")]) == {}

    def test_it_is_batch_scope(self):
        assert ConflictType.IDENTIFIER_DUPLICATE.scope is ConflictScope.BATCH


class TestDeterminismAndIdentity:
    def test_the_same_result_produces_the_same_conflicts_every_time(self, template):
        result = clean_result(
            fields=(
                field_view(
                    "roll_number",
                    "Roll",
                    [character(0), character(1, "?", "multiple"), character(2)],
                ),
            ),
            answers=(make_answer(1, "B-D", "multiple"), make_answer(3, "?", "uncertain")),
        )
        first = detect_conflicts(result, template)
        second = detect_conflicts(result, template)
        assert [item.key for item in first] == [item.key for item in second]

    def test_keys_are_unique_within_one_sheet(self, template):
        result = clean_result(
            fields=(
                field_view(
                    "roll_number",
                    "Roll",
                    [character(i, "?", "multiple") for i in range(6)],
                    value="??????",
                ),
            )
        )
        found = detect_conflicts(result, template)
        keys = [item.key for item in found]
        assert len(keys) == 6
        assert len(keys) == len(set(keys))

    def test_serious_conflicts_sort_before_ordinary_ones(self, template):
        result = clean_result(
            fields=(
                field_view(
                    "roll_number", "Roll", [character(0, "?", "multiple")], value="?"
                ),
                field_view(
                    "set_code", "Set", [character(0, "1", confidence=0.4)], value="1"
                ),
            ),
        )
        found = detect_conflicts(result, template)
        assert found[0].conflict_type is ConflictType.IDENTIFIER_MULTIPLE
        assert found[-1].conflict_type is ConflictType.SET_CODE_LOW_CONFIDENCE


class TestTemplateDrivenChoices:
    def test_labels_come_from_the_template_never_a_hard_coded_alphabet(self, template):
        zone = next(
            item for item in template.zones if item.id.startswith("questions_")
        )
        labels = group_labels(template, zone.id, 0)
        assert labels
        assert labels == zone.field.answer_labels

    def test_identifier_labels_are_the_templates_symbols(self, template):
        labels = group_labels(template, "roll_number", 0)
        assert labels == tuple("0123456789")

    def test_cells_come_from_the_same_grouping_recognition_used(self, template):
        cells = group_cells(template, "roll_number", 0)
        assert len(cells) == len(group_labels(template, "roll_number", 0))

    def test_an_unknown_zone_yields_nothing_rather_than_guessing(self, template):
        assert group_labels(template, "no_such_zone", 0) == ()
        assert group_cells(template, "no_such_zone", 0) == ()

    def test_every_field_zone_offers_its_own_symbol_set(self, template):
        choices = describe_template_choices(template)
        assert "roll_number" in choices
        assert "set_code" in choices
        assert all(values for values in choices.values())
