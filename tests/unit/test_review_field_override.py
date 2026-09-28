"""Overriding a confidently read position through the whole-field editor.

Scope:
    The explicit "edit the full field" action may change a position the machine
    read confidently and raised no conflict for - a clean ``9`` that the paper
    shows is an ``8``. These tests pin down how that is stored:

    * on a ``MANUAL_OVERRIDE`` record, never on a fabricated detection;
    * with the machine's reading preserved and the operator's value effective;
    * as part of the same grouped edit, so one undo takes all of it back;
    * without weakening the ordinary position model - a conflict-less position
      with no explicit override request is still refused.

Why these assert against the database:
    As for the rest of the review store: a returned value object proves nothing
    about what a reopened project will say. Everything is re-read through the
    public query functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template
from tests.unit.test_review_store import REVIEWER

from omr_scanner.database import open_project_database
from omr_scanner.domain.review import (
    ConflictState,
    ConflictType,
    FieldKind,
    MachineObservation,
    ReasonCode,
    ReviewAction,
    ValueSource,
)
from omr_scanner.services import batch_store, review_store
from omr_scanner.services.review_store import ReviewError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase
    from omr_scanner.services.recognition_models import ScanResult

OTHER_REVIEWER = "Dr. Y"
CONFIDENT_NINE = MachineObservation(
    value="9", status="resolved", confidence=0.97, top_fill=0.91, margin=0.8
)
"""What recognition read at position 2: a clean, undisputed 9."""


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[ProjectDatabase]:
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    try:
        yield handle
    finally:
        handle.close()


def _result(path: Path) -> ScanResult:
    """A roll number disputed at positions 0 and 1, confident at position 2."""
    from tests.unit.test_recognition_contract import make_answer, make_result

    from omr_scanner.services.recognition_models import (
        CharacterView,
        FieldView,
        RecognitionOutcome,
        RegistrationStatus,
    )

    field = FieldView(
        zone_id="roll_number",
        label="Roll",
        field_type="numeric",
        value="??9",
        status="multiple",
        needs_review=True,
        characters=(
            CharacterView(
                position=0, value="1-4", status="multiple",
                top_fill=0.8, margin=0.02, confidence=0.0,
            ),
            CharacterView(
                position=1, value="3-8", status="multiple",
                top_fill=0.8, margin=0.02, confidence=0.0,
            ),
            CharacterView(
                position=2, value="9", status="resolved",
                top_fill=0.9, margin=0.8, confidence=1.0,
            ),
        ),
    )
    return make_result(
        source_path=path,
        outcome=RecognitionOutcome.REVIEW,
        registration=RegistrationStatus.REGISTERED,
        warnings=(),
        status_codes=("MULTIPLE_MARK",),
        fields=(field,),
        answers=(make_answer(1, "B", "resolved", needs_review=False),),
        bubbles=(),
        identifier_zone_id="roll_number",
        set_code_zone_id="set_code",
    )


@pytest.fixture
def sheet(database, template, tmp_path: Path) -> tuple[str, int, list[int]]:
    """One sheet: two disputed roll-number positions and one confident one."""
    path = tmp_path / "sheet_0.png"
    path.write_bytes(b"placeholder")
    batch_id = batch_store.create_batch(
        database, [path], identity=batch_store.BatchIdentity.of(template)
    )
    scan_id = batch_store.scan_ids_by_path(database, batch_id)[path]
    review_store.sync_conflicts(
        database,
        batch_id=batch_id,
        scan_id=scan_id,
        result=_result(Path("sheet_0.png")),
        template=template,
    )
    found = review_store.list_conflicts(
        database, batch_id, filters=review_store.ConflictFilter(scan_id=scan_id)
    )
    return batch_id, scan_id, [item.conflict_id for item in found]


def edit(
    database, sheet, values, *, overrides=None, reviewer=REVIEWER, **extra: str
) -> review_store.FieldEdit:
    batch_id, scan_id, _ = sheet
    return review_store.correct_field(
        database,
        batch_id=batch_id,
        scan_id=scan_id,
        zone_id="roll_number",
        values=values,
        display_value="".join(values[key] for key in sorted(values)),
        field_label="Roll number",
        reviewer=reviewer,
        reason=ReasonCode.MISCLASSIFICATION,
        reason_text=extra.pop("reason_text", "read from the script"),
        overrides=overrides,
        field_kind=FieldKind.IDENTIFIER,
        previous_value=extra.pop("previous_value", "??9"),
    )


def override_record(database, sheet) -> review_store.ConflictRecord:
    batch_id, scan_id, _ = sheet
    found = [
        item
        for item in review_store.list_conflicts(
            database,
            batch_id,
            filters=review_store.ConflictFilter(scan_id=scan_id, include_withdrawn=True),
        )
        if item.conflict_type is ConflictType.MANUAL_OVERRIDE
    ]
    assert len(found) == 1, found
    return found[0]


class TestOnlyDisputedPositions:
    def test_no_override_record_is_created(self, database, sheet):
        result = edit(database, sheet, {0: "1", 1: "3"})
        assert result.overridden == ()
        batch_id, scan_id, _ = sheet
        types = {
            item.conflict_type
            for item in review_store.list_conflicts(
                database,
                batch_id,
                filters=review_store.ConflictFilter(scan_id=scan_id, include_withdrawn=True),
            )
        }
        assert ConflictType.MANUAL_OVERRIDE not in types

    def test_a_confident_position_without_an_override_request_is_still_refused(
        self, database, sheet
    ):
        # The position model is not weakened: only an *explicit* override may
        # write a position that has no conflict.
        result = edit(database, sheet, {0: "1", 1: "3", 2: "8"})
        assert result.missing == (2,)
        assert 2 not in result.changed


class TestOverridingOneConfidentPosition:
    def test_it_is_stored_on_an_override_record(self, database, sheet):
        result = edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})

        assert result.overridden == (2,)
        assert result.changed == {2: "8"}
        record = override_record(database, sheet)
        assert record.state is ConflictState.RESOLVED
        assert record.field.group_key == 2
        assert record.field.kind is FieldKind.IDENTIFIER

    def test_the_machine_value_is_kept_and_the_manual_one_is_effective(
        self, database, sheet
    ):
        edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        record = override_record(database, sheet)

        assert record.observation.value == "9"
        assert record.observation.confidence == pytest.approx(0.97)
        found = review_store.provenance_for(database, record.conflict_id)
        assert found.machine_value == "9"
        assert found.value == "8"
        assert found.source is ValueSource.HUMAN

    def test_no_detection_is_fabricated(self, database, sheet):
        edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        history = review_store.history_for(
            database, override_record(database, sheet).conflict_id
        )
        actions = [item.action for item in history]
        assert ReviewAction.DETECTED not in actions
        assert actions == [ReviewAction.CORRECTED]

    def test_the_audit_event_says_it_was_an_explicit_override(self, database, sheet):
        result = edit(
            database,
            sheet,
            {2: "8"},
            overrides={2: CONFIDENT_NINE},
            previous_value="139",
        )
        (event,) = review_store.history_for(
            database, override_record(database, sheet).conflict_id
        )
        assert review_store.is_override(event.detail)
        assert review_store.group_of(event.detail) == result.group
        assert event.reviewer == REVIEWER
        assert event.occurred_at
        assert event.previous_value == "9"
        assert event.new_value == "8"
        assert event.machine_value == "9"
        assert event.reason_code == ReasonCode.MISCLASSIFICATION.value
        assert event.reason_text == "read from the script"
        assert "Roll number set to '8' (was '139')" in event.detail
        assert "raised no conflict" in event.detail

    def test_agreeing_with_the_machine_overrides_nothing(self, database, sheet):
        with pytest.raises(ReviewError):
            edit(database, sheet, {2: "9"}, overrides={2: CONFIDENT_NINE})
        batch_id, scan_id, _ = sheet
        assert not [
            item
            for item in review_store.list_conflicts(
                database,
                batch_id,
                filters=review_store.ConflictFilter(scan_id=scan_id, include_withdrawn=True),
            )
            if item.conflict_type is ConflictType.MANUAL_OVERRIDE
        ]

    def test_it_is_not_an_open_question(self, database, sheet):
        batch_id, _, _ = sheet
        before = review_store.count_conflicts(database, batch_id)
        edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        after = review_store.count_conflicts(database, batch_id)
        assert after.open_count == before.open_count
        assert after.unresolved == before.unresolved

    def test_it_cannot_be_deferred(self, database, sheet):
        edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        with pytest.raises(ReviewError):
            review_store.defer(
                database, override_record(database, sheet).conflict_id, reviewer=REVIEWER
            )

    def test_reopening_restores_the_machine_reading_without_opening_anything(
        self, database, sheet
    ):
        edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        record = override_record(database, sheet)
        review_store.reopen(database, record.conflict_id, reviewer=REVIEWER)

        record = override_record(database, sheet)
        assert record.state is ConflictState.WITHDRAWN
        found = review_store.provenance_for(database, record.conflict_id)
        assert found.value == "9"
        assert found.source is ValueSource.MACHINE
        # ...and the override is still in the history.
        actions = [
            item.action for item in review_store.history_for(database, record.conflict_id)
        ]
        assert actions == [ReviewAction.CORRECTED, ReviewAction.REOPENED]


class TestConflictedAndConfidentTogether:
    def test_one_edit_writes_both_kinds(self, database, sheet):
        _, _, conflicts = sheet
        result = edit(
            database, sheet, {0: "1", 1: "3", 2: "8"}, overrides={2: CONFIDENT_NINE}
        )

        assert result.changed == {0: "1", 1: "3", 2: "8"}
        assert result.overridden == (2,)
        for conflict_id in conflicts:
            assert review_store.get_conflict(database, conflict_id).state is (
                ConflictState.RESOLVED
            )
        assert override_record(database, sheet).state is ConflictState.RESOLVED

    def test_every_position_shares_the_one_group_and_reason(self, database, sheet):
        _, _, conflicts = sheet
        result = edit(
            database, sheet, {0: "1", 1: "3", 2: "8"}, overrides={2: CONFIDENT_NINE}
        )
        ids = [*conflicts, override_record(database, sheet).conflict_id]
        for conflict_id in ids:
            corrected = [
                item
                for item in review_store.history_for(database, conflict_id)
                if item.action is ReviewAction.CORRECTED
            ]
            assert len(corrected) == 1
            assert review_store.group_of(corrected[0].detail) == result.group
            assert corrected[0].reason_text == "read from the script"

    def test_only_the_confident_position_is_marked_as_an_override(
        self, database, sheet
    ):
        _, _, conflicts = sheet
        edit(database, sheet, {0: "1", 1: "3", 2: "8"}, overrides={2: CONFIDENT_NINE})
        for conflict_id in conflicts:
            (corrected,) = [
                item
                for item in review_store.history_for(database, conflict_id)
                if item.action is ReviewAction.CORRECTED
            ]
            assert not review_store.is_override(corrected.detail)


class TestGroupedUndo:
    def test_one_undo_reverts_conflicted_and_confident_positions(self, database, sheet):
        batch_id, _, conflicts = sheet
        result = edit(
            database, sheet, {0: "1", 1: "3", 2: "8"}, overrides={2: CONFIDENT_NINE}
        )
        target = review_store.last_decision(database, batch_id)
        assert target is not None
        assert target.group == result.group

        reversed_commands = review_store.undo_field_edit(
            database, batch_id=batch_id, group=result.group, reviewer=REVIEWER
        )

        assert len(reversed_commands) == 3
        for conflict_id in conflicts:
            assert review_store.get_conflict(database, conflict_id).state is (
                ConflictState.OPEN
            )
        record = override_record(database, sheet)
        # Not reopened as a question: it rests, and the machine's 9 stands.
        assert record.state is ConflictState.WITHDRAWN
        found = review_store.provenance_for(database, record.conflict_id)
        assert found.value == "9"
        assert found.source is ValueSource.MACHINE

    def test_the_undone_override_stays_in_the_history(self, database, sheet):
        batch_id, _, _ = sheet
        result = edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        review_store.undo_field_edit(
            database, batch_id=batch_id, group=result.group, reviewer=REVIEWER
        )
        record = override_record(database, sheet)
        events = review_store.history_for(database, record.conflict_id)
        assert [item.action for item in events] == [
            ReviewAction.CORRECTED,
            ReviewAction.UNDONE,
        ]
        assert events[1].new_value == "9"
        # The cached state agrees with the ledger's own fold.
        assert review_store.recompute_state(events, override=True) is record.state

    def test_the_undone_override_leaves_the_working_queue(self, database, sheet):
        batch_id, scan_id, _ = sheet
        result = edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        review_store.undo_field_edit(
            database, batch_id=batch_id, group=result.group, reviewer=REVIEWER
        )
        visible = review_store.list_conflicts(
            database, batch_id, filters=review_store.ConflictFilter(scan_id=scan_id)
        )
        assert ConflictType.MANUAL_OVERRIDE not in {
            item.conflict_type for item in visible
        }

    def test_a_later_superseding_decision_is_not_rolled_back(self, database, sheet):
        batch_id, _, conflicts = sheet
        result = edit(
            database, sheet, {0: "1", 1: "3", 2: "8"}, overrides={2: CONFIDENT_NINE}
        )
        record = override_record(database, sheet)
        review_store.correct_value(
            database,
            record.conflict_id,
            value="7",
            reviewer=OTHER_REVIEWER,
            reason=ReasonCode.CLEAR_VISUAL_MARK,
        )

        review_store.undo_field_edit(
            database, batch_id=batch_id, group=result.group, reviewer=REVIEWER
        )

        assert review_store.provenance_for(database, record.conflict_id).value == "7"
        for conflict_id in conflicts:
            assert review_store.provenance_for(database, conflict_id).source is (
                ValueSource.MACHINE
            )

    def test_a_correction_on_the_override_record_is_still_marked(self, database, sheet):
        # A redo re-issues through the ordinary correction path, which knows
        # nothing about field edits. The ledger row must still say "override".
        edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        record = override_record(database, sheet)
        review_store.correct_value(
            database,
            record.conflict_id,
            value="7",
            reviewer=OTHER_REVIEWER,
            reason=ReasonCode.CLEAR_VISUAL_MARK,
        )
        latest = review_store.history_for(database, record.conflict_id)[-1]
        assert review_store.is_override(latest.detail)

    def test_overriding_again_after_undo_reuses_the_record(self, database, sheet):
        batch_id, _, _ = sheet
        first = edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        review_store.undo_field_edit(
            database, batch_id=batch_id, group=first.group, reviewer=REVIEWER
        )
        second = edit(database, sheet, {2: "6"}, overrides={2: CONFIDENT_NINE})

        record = override_record(database, sheet)  # exactly one, asserted inside
        assert record.state is ConflictState.RESOLVED
        assert review_store.provenance_for(database, record.conflict_id).value == "6"
        assert second.group != first.group


class TestReReadingTheSheet:
    def test_a_re_read_does_not_withdraw_a_standing_override(
        self, database, template, sheet
    ):
        batch_id, scan_id, _ = sheet
        edit(database, sheet, {2: "8"}, overrides={2: CONFIDENT_NINE})
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            result=_result(Path("sheet_0.png")),
            template=template,
        )
        record = override_record(database, sheet)
        assert record.state is ConflictState.RESOLVED
        assert review_store.provenance_for(database, record.conflict_id).value == "8"


class TestSetCodeOverride:
    def test_a_multi_character_symbol_can_override_a_confident_position(
        self, database, sheet
    ):
        # Nothing here assumes one character per position: a set code whose
        # symbols are "10", "11", "12" overrides by symbol, not by index.
        batch_id, scan_id, _ = sheet
        result = review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="set_code",
            values={0: "11", 1: "12"},
            display_value="1112",
            field_label="Set code",
            reviewer=REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
            overrides={
                0: MachineObservation(value="10", status="resolved", confidence=0.9),
                1: MachineObservation(value="12", status="resolved", confidence=0.9),
            },
            field_kind=FieldKind.SET_CODE,
            previous_value="1012",
        )
        assert result.overridden == (0,)
        assert result.unchanged == (1,)
        records = [
            item
            for item in review_store.list_conflicts(
                database, batch_id, filters=review_store.ConflictFilter(scan_id=scan_id)
            )
            if item.conflict_type is ConflictType.MANUAL_OVERRIDE
        ]
        assert len(records) == 1
        assert records[0].field.kind is FieldKind.SET_CODE
        assert records[0].observation.value == "10"
        found = review_store.provenance_for(database, records[0].conflict_id)
        assert (found.machine_value, found.value) == ("10", "11")


class TestFieldsAreSequencesOfSymbols:
    """The shared split / join / machine-reading helpers, with wide symbols."""

    @staticmethod
    def shape() -> object:
        from omr_scanner.services.conflict_policy import FieldShape

        mixed = (*(str(digit) for digit in range(10)), "10", "11")
        return FieldShape(zone_id="set_code", label="Set code", positions=(mixed, mixed))

    def test_split_keeps_whole_symbols(self):
        from omr_scanner.services import split_field_value

        shape = self.shape()
        assert split_field_value(shape, "102") == ["10", "2"]
        assert split_field_value(shape, "1110") == ["11", "10"]
        assert split_field_value(shape, "72") == ["7", "2"]
        # Nothing padded, nothing truncated.
        assert split_field_value(shape, "1") is None
        assert split_field_value(shape, "10210") is None

    def test_split_symbols_like_b2_and_x(self):
        from omr_scanner.services import split_field_value
        from omr_scanner.services.conflict_policy import FieldShape

        shape = FieldShape(
            zone_id="set_code",
            label="Set code",
            positions=(("A", "B2", "X"), ("A", "B2", "X")),
        )
        assert split_field_value(shape, "B2X") == ["B2", "X"]
        assert split_field_value(shape, "AB2") == ["A", "B2"]
        assert split_field_value(shape, "B") is None

    def test_join_is_the_inverse(self):
        from omr_scanner.services import join_field_value, split_field_value

        shape = self.shape()
        for text in ("102", "1110", "72"):
            assert join_field_value(split_field_value(shape, text)) == text

    def test_the_machine_reading_is_one_symbol_per_position(self):
        from tests.unit.test_recognition_contract import make_result

        from omr_scanner.services import machine_field_symbols
        from omr_scanner.services.recognition_models import (
            CharacterView,
            FieldView,
            RecognitionOutcome,
            RegistrationStatus,
        )

        field = FieldView(
            zone_id="set_code",
            label="Set code",
            field_type="set_code",
            value="10?_",
            status="multiple",
            needs_review=True,
            characters=(
                CharacterView(position=0, value="10", status="resolved",
                              top_fill=0.9, margin=0.8, confidence=1.0),
                CharacterView(position=1, value="2-3", status="multiple",
                              top_fill=0.8, margin=0.0, confidence=0.0),
                CharacterView(position=2, value="", status="blank",
                              top_fill=0.1, margin=0.0, confidence=0.0),
            ),
        )
        result = make_result(
            source_path=Path("s.png"),
            outcome=RecognitionOutcome.REVIEW,
            registration=RegistrationStatus.REGISTERED,
            warnings=(),
            status_codes=(),
            fields=(field,),
            answers=(),
            bubbles=(),
            identifier_zone_id="roll_number",
            set_code_zone_id="set_code",
        )
        # The same rendering recognition uses for the assembled value.
        assert machine_field_symbols(result, "set_code") == ["10", "?", "_"]
        assert machine_field_symbols(result, "absent") is None


class TestReassemblyWithoutAStoredResult:
    def test_a_sheet_with_no_stored_result_falls_back_to_the_old_rule(
        self, database, sheet
    ):
        # The `sheet` fixture never stores a recognition result, so there are
        # no per-position machine symbols to lay decisions over. The effective
        # value must still carry the decision, by the documented fallback.
        batch_id, scan_id, conflicts = sheet
        review_store.correct_value(
            database, conflicts[0], value="1", reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        found = review_store.effective_identifiers(database, batch_id)[scan_id]
        assert found.source is ValueSource.HUMAN
        assert found.value == "1"
