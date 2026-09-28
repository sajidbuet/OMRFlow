"""Taking a decision - or a whole sheet's worth - back again (Phase 6).

Scope:
    That undo is a change to the **stored** record and not a gesture on screen:
    the effective value moves, the conflict's state moves, downstream consumers
    see the new answer, and the decision that was reversed is still in the
    ledger with its own reviewer, reason and timestamp.

Why these assert against the database rather than a returned object:
    An undo that returned a convincing value object and wrote nothing would
    pass every test that only read what it returned - and would lose an
    examination office's corrections the moment they closed the project. Every
    assertion below re-reads through the ordinary public query functions.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template
from tests.unit.test_review_store import (
    REVIEWER,
    result_with_disputed_digit,
)

from omr_scanner.database import open_project_database
from omr_scanner.domain.review import (
    ConflictState,
    ReasonCode,
    ReviewAction,
    ValueSource,
)
from omr_scanner.services import batch_store, review_store
from omr_scanner.services.review_store import ReviewError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase

OTHER_REVIEWER = "Dr. Y"


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


@pytest.fixture
def batch(database, template, tmp_path: Path) -> tuple[str, list[int]]:
    paths = []
    for index in range(3):
        path = tmp_path / f"sheet_{index}.png"
        path.write_bytes(b"placeholder")
        paths.append(path)
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    ids = [batch_store.scan_ids_by_path(database, batch_id)[path] for path in paths]
    return batch_id, ids


def stage(database, template, batch_id: str, scan_id: int, name: str) -> int:
    """Stage one identifier conflict on a sheet and return its id."""
    review_store.sync_conflicts(
        database,
        batch_id=batch_id,
        scan_id=scan_id,
        result=result_with_disputed_digit(Path(name)),
        template=template,
    )
    found = review_store.list_conflicts(
        database, batch_id, filters=review_store.ConflictFilter(scan_id=scan_id)
    )
    return found[0].conflict_id


@pytest.fixture
def conflict(database, template, batch) -> int:
    batch_id, ids = batch
    return stage(database, template, batch_id, ids[0], "sheet_0.png")


def correct(database, conflict_id: int, value: str, reviewer: str = REVIEWER) -> None:
    review_store.correct_value(
        database,
        conflict_id,
        value=value,
        reviewer=reviewer,
        reason=ReasonCode.DOMINANT_MARK,
    )


class TestUndoRestoresTheStoredValue:
    def test_undoing_a_correction_returns_the_machine_value(self, database, conflict):
        machine = review_store.get_conflict(database, conflict).observation.value
        correct(database, conflict, "1")
        assert review_store.provenance_for(database, conflict).value == "1"

        review_store.undo_decision(database, conflict, reviewer=REVIEWER)

        found = review_store.provenance_for(database, conflict)
        assert found.value == machine
        assert found.source is ValueSource.MACHINE
        assert found.machine_value == machine

    def test_undoing_a_correction_reopens_the_conflict(self, database, conflict):
        correct(database, conflict, "1")
        assert review_store.get_conflict(database, conflict).state is (
            ConflictState.RESOLVED
        )

        review_store.undo_decision(database, conflict, reviewer=REVIEWER)

        assert review_store.get_conflict(database, conflict).state is ConflictState.OPEN

    def test_undoing_an_acceptance_returns_the_value_to_the_machine(
        self, database, conflict
    ):
        review_store.accept_machine_value(database, conflict, reviewer=REVIEWER)
        review_store.undo_decision(database, conflict, reviewer=REVIEWER)

        found = review_store.provenance_for(database, conflict)
        assert found.source is ValueSource.MACHINE
        assert review_store.get_conflict(database, conflict).state is ConflictState.OPEN

    def test_undoing_a_deferral_returns_the_conflict_to_open(self, database, conflict):
        review_store.defer(database, conflict, reviewer=REVIEWER)
        review_store.undo_decision(database, conflict, reviewer=REVIEWER)
        assert review_store.get_conflict(database, conflict).state is ConflictState.OPEN

    def test_undo_steps_back_one_decision_rather_than_resetting(
        self, database, conflict
    ):
        # The case a plain "reopen" gets wrong. Two people have decided this;
        # taking the second decision back must leave the first one standing,
        # not throw away both and return to what the machine read.
        correct(database, conflict, "1", reviewer=REVIEWER)
        correct(database, conflict, "7", reviewer=OTHER_REVIEWER)

        review_store.undo_decision(database, conflict, reviewer=OTHER_REVIEWER)

        found = review_store.provenance_for(database, conflict)
        assert found.value == "1"
        assert found.reviewer == REVIEWER
        assert found.source is ValueSource.HUMAN
        assert review_store.get_conflict(database, conflict).state is (
            ConflictState.RESOLVED
        )

    def test_undoing_twice_reaches_the_machine_value(self, database, conflict):
        machine = review_store.get_conflict(database, conflict).observation.value
        correct(database, conflict, "1")
        correct(database, conflict, "7")

        review_store.undo_decision(database, conflict, reviewer=REVIEWER)
        review_store.undo_decision(database, conflict, reviewer=REVIEWER)

        found = review_store.provenance_for(database, conflict)
        assert found.value == machine
        assert found.source is ValueSource.MACHINE
        assert review_store.get_conflict(database, conflict).state is ConflictState.OPEN

    def test_undoing_a_reopen_puts_the_decision_back(self, database, conflict):
        correct(database, conflict, "1")
        review_store.reopen(database, conflict, reviewer=REVIEWER)
        assert review_store.provenance_for(database, conflict).source is (
            ValueSource.MACHINE
        )

        review_store.undo_decision(database, conflict, reviewer=REVIEWER)

        found = review_store.provenance_for(database, conflict)
        assert found.value == "1"
        assert found.source is ValueSource.HUMAN

    def test_deciding_again_after_an_undo_works_normally(self, database, conflict):
        correct(database, conflict, "1")
        review_store.undo_decision(database, conflict, reviewer=REVIEWER)
        correct(database, conflict, "7", reviewer=OTHER_REVIEWER)

        found = review_store.provenance_for(database, conflict)
        assert found.value == "7"
        assert found.reviewer == OTHER_REVIEWER


class TestUndoIsRefusedWhenThereIsNothingToUndo:
    def test_a_conflict_nobody_decided_cannot_be_undone(self, database, conflict):
        with pytest.raises(ReviewError):
            review_store.undo_decision(database, conflict, reviewer=REVIEWER)

    def test_an_unnamed_undo_is_refused(self, database, conflict):
        correct(database, conflict, "1")
        with pytest.raises(ReviewError):
            review_store.undo_decision(database, conflict, reviewer="   ")
        # And nothing was written.
        assert review_store.provenance_for(database, conflict).value == "1"


class TestTheLedgerKeepsWhatWasUndone:
    def test_the_reversed_decision_is_still_in_the_history(self, database, conflict):
        correct(database, conflict, "1")
        review_store.undo_decision(database, conflict, reviewer=OTHER_REVIEWER)

        history = review_store.history_for(database, conflict)
        assert [item.action for item in history] == [
            ReviewAction.DETECTED,
            ReviewAction.CORRECTED,
            ReviewAction.UNDONE,
        ]
        # The correction keeps its own author and value; it is not rewritten.
        assert history[1].reviewer == REVIEWER
        assert history[1].new_value == "1"
        # And the reversal is attributed to whoever performed it.
        assert history[2].reviewer == OTHER_REVIEWER

    def test_the_history_reads_decision_undo_decision(self, database, conflict):
        correct(database, conflict, "1")
        review_store.undo_decision(database, conflict, reviewer=REVIEWER)
        correct(database, conflict, "7")

        assert [item.action for item in review_store.history_for(database, conflict)] == [
            ReviewAction.DETECTED,
            ReviewAction.CORRECTED,
            ReviewAction.UNDONE,
            ReviewAction.CORRECTED,
        ]
        assert review_store.provenance_for(database, conflict).value == "7"

    def test_the_cached_state_still_agrees_with_the_ledger(self, database, conflict):
        for step in ("correct", "undo", "correct", "defer", "undo", "reopen", "undo"):
            if step == "correct":
                correct(database, conflict, "1")
            elif step == "defer":
                review_store.defer(database, conflict, reviewer=REVIEWER)
            elif step == "reopen":
                review_store.reopen(database, conflict, reviewer=REVIEWER)
            else:
                review_store.undo_decision(database, conflict, reviewer=REVIEWER)

            history = review_store.history_for(database, conflict)
            assert review_store.recompute_state(history) is (
                review_store.get_conflict(database, conflict).state
            ), step

    def test_a_reversed_conflict_is_marked_as_reopened_in_the_queue(
        self, database, batch, conflict
    ):
        batch_id, _ = batch
        correct(database, conflict, "1")
        review_store.undo_decision(database, conflict, reviewer=REVIEWER)

        record = next(
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.conflict_id == conflict
        )
        assert record.state is ConflictState.OPEN
        assert record.reversed_before is True
        assert record.state_label == "Reopened"
        # Still the same glyph-plus-word pattern every other state uses.
        assert record.state_marker


class TestFindingWhatToUndo:
    def test_the_newest_standing_decision_is_the_target(
        self, database, template, batch
    ):
        batch_id, ids = batch
        first = stage(database, template, batch_id, ids[0], "sheet_0.png")
        second = stage(database, template, batch_id, ids[1], "sheet_1.png")
        correct(database, first, "1")
        correct(database, second, "7")

        target = review_store.last_decision(database, batch_id)
        assert target is not None
        assert target.conflict_id == second
        assert target.scan_id == ids[1]
        assert target.value == "7"
        assert target.action is ReviewAction.CORRECTED

    def test_an_undone_decision_is_no_longer_the_target(
        self, database, template, batch
    ):
        batch_id, ids = batch
        first = stage(database, template, batch_id, ids[0], "sheet_0.png")
        second = stage(database, template, batch_id, ids[1], "sheet_1.png")
        correct(database, first, "1")
        correct(database, second, "7")
        review_store.undo_decision(database, second, reviewer=REVIEWER)

        target = review_store.last_decision(database, batch_id)
        assert target is not None
        assert target.conflict_id == first

    def test_a_batch_nobody_has_decided_has_no_target(self, database, batch, conflict):
        batch_id, _ = batch
        assert review_store.last_decision(database, batch_id) is None

    def test_the_target_carries_enough_to_repeat_the_decision(
        self, database, batch, conflict
    ):
        batch_id, _ = batch
        review_store.correct_value(
            database,
            conflict,
            value="7",
            reviewer=REVIEWER,
            reason=ReasonCode.THRESHOLD_ERROR,
            reason_text="looked again",
        )
        target = review_store.last_decision(database, batch_id)
        assert target is not None
        assert target.value == "7"
        assert target.reason == ReasonCode.THRESHOLD_ERROR.value
        assert target.reason_text == "looked again"
        assert target.describe


class TestUndoingAWholeSheet:
    @pytest.fixture
    def finished_sheet(self, database, template, batch) -> tuple[str, int, list[int]]:
        """A sheet whose conflicts were all decided, then another sheet started.

        The shape the feature exists for: an operator works one sheet to the
        end and moves on, and then realises they were reading the wrong column.
        """
        batch_id, ids = batch
        first = stage(database, template, batch_id, ids[0], "sheet_0.png")
        second = stage(database, template, batch_id, ids[1], "sheet_1.png")
        correct(database, first, "1")
        # Moving on to another sheet is what ends the session.
        correct(database, second, "7")
        return batch_id, ids[0], [first, second]

    def test_the_last_finished_sheet_is_the_one_just_left(
        self, database, finished_sheet
    ):
        batch_id, _first_scan, _ = finished_sheet
        found = review_store.last_resolved_sheet(database, batch_id)
        assert found is not None
        # `sheet_1` was finished most recently - both are complete, and the run
        # for the sheet still being worked on is the newest.
        assert found.decisions == 1

    def test_undoing_a_sheet_restores_every_decision_it_made(
        self, database, template, batch
    ):
        batch_id, ids = batch
        # One sheet carrying two disputed positions is the realistic case; the
        # fixture template gives one per sheet, so two sheets stand in for the
        # two-decisions-in-one-session shape.
        first = stage(database, template, batch_id, ids[0], "sheet_0.png")
        correct(database, first, "1")
        review_store.reopen(database, first, reviewer=REVIEWER)
        correct(database, first, "7")
        assert review_store.provenance_for(database, first).value == "7"

        found = review_store.undo_resolved_sheet(database, batch_id, reviewer=REVIEWER)

        assert found is not None
        assert found.scan_id == ids[0]
        assert found.decisions == 3
        # Everything the run did is gone, back to what the machine read.
        after = review_store.provenance_for(database, first)
        assert after.source is ValueSource.MACHINE
        assert review_store.get_conflict(database, first).state is ConflictState.OPEN

    def test_undoing_a_sheet_leaves_another_sheets_decisions_alone(
        self, database, finished_sheet
    ):
        batch_id, _first_scan, (first, second) = finished_sheet
        review_store.undo_resolved_sheet(database, batch_id, reviewer=REVIEWER)

        # The newest run is the one on `second`; `first` must be untouched.
        assert review_store.provenance_for(database, first).value == "1"
        assert review_store.get_conflict(database, first).state is (
            ConflictState.RESOLVED
        )
        assert review_store.provenance_for(database, second).source is (
            ValueSource.MACHINE
        )

    def test_undoing_a_sheet_restores_the_unresolved_count(
        self, database, finished_sheet
    ):
        batch_id, _, (_, second) = finished_sheet
        scan_id = review_store.get_conflict(database, second).scan_id
        before = review_store.count_conflicts_for_scan(database, batch_id, scan_id)
        assert before.unresolved == 0

        review_store.undo_resolved_sheet(database, batch_id, reviewer=REVIEWER)

        after = review_store.count_conflicts_for_scan(database, batch_id, scan_id)
        assert after.unresolved == 1

    def test_undoing_a_sheet_reports_what_it_reversed(self, database, finished_sheet):
        batch_id, _, _ = finished_sheet
        found = review_store.undo_resolved_sheet(database, batch_id, reviewer=REVIEWER)
        assert found is not None
        assert len(found.reversed_commands) == found.decisions
        assert all(item.action.is_human for item in found.reversed_commands)

    def test_a_batch_with_no_finished_sheet_reports_nothing(
        self, database, batch, conflict
    ):
        batch_id, _ = batch
        assert review_store.last_resolved_sheet(database, batch_id) is None
        assert (
            review_store.undo_resolved_sheet(database, batch_id, reviewer=REVIEWER)
            is None
        )

    def test_an_unnamed_sheet_undo_is_refused(self, database, finished_sheet):
        batch_id, _, (first, _) = finished_sheet
        with pytest.raises(ReviewError):
            review_store.undo_resolved_sheet(database, batch_id, reviewer="  ")
        assert review_store.provenance_for(database, first).value == "1"


class TestProvenanceForAWholeSheet:
    def test_it_includes_conflicts_nobody_has_decided(self, database, batch, conflict):
        # The review overlay needs these: a position nobody has touched is
        # exactly what it has to draw as unresolved.
        batch_id, ids = batch
        found = review_store.provenance_for_scan(database, batch_id, ids[0])
        assert set(found) == {conflict}
        assert found[conflict].source is ValueSource.MACHINE

    def test_it_agrees_with_the_single_conflict_projection(
        self, database, batch, conflict
    ):
        batch_id, ids = batch
        correct(database, conflict, "1")
        review_store.undo_decision(database, conflict, reviewer=REVIEWER)
        correct(database, conflict, "7")

        found = review_store.provenance_for_scan(database, batch_id, ids[0])
        assert found[conflict] == review_store.provenance_for(database, conflict)

    def test_a_sheet_with_no_conflicts_returns_nothing(self, database, batch, conflict):
        batch_id, ids = batch
        assert review_store.provenance_for_scan(database, batch_id, ids[2]) == {}


class TestDownstreamSeesTheUndo:
    def test_the_effective_identifier_follows_an_undo(self, database, conflict, batch):
        batch_id, ids = batch
        correct(database, conflict, "7")
        after_decision = review_store.effective_identifiers(database, batch_id)[ids[0]]
        assert after_decision.source is ValueSource.HUMAN

        review_store.undo_decision(database, conflict, reviewer=REVIEWER)

        after_undo = review_store.effective_identifiers(database, batch_id)[ids[0]]
        assert after_undo.source is ValueSource.MACHINE
        # And the identifier is unknown again, because the position that
        # decides it is open again.
        assert after_undo.unresolved is True

    def test_the_export_resolution_follows_an_undo(self, database, conflict, batch):
        batch_id, _ = batch
        correct(database, conflict, "7")
        template = build_answer_sheet_template()
        decided = review_store.sheet_resolutions(database, batch_id, template)
        assert any(item.reviewed for item in decided.values())

        review_store.undo_decision(database, conflict, reviewer=REVIEWER)

        after = review_store.sheet_resolutions(database, batch_id, template)
        assert not any(item.reviewed for item in after.values())
        assert sum(item.unresolved for item in after.values()) == 1


class TestCorrectingAWholeFieldAtOnce:
    @pytest.fixture
    def two_positions(self, database, template, batch) -> tuple[str, int, list[int]]:
        """One sheet whose roll number has two disputed positions."""
        from tests.unit.test_recognition_contract import make_answer, make_result
        from tests.unit.test_review_store import roll_field

        from omr_scanner.services.recognition_models import (
            CharacterView,
            RecognitionOutcome,
            RegistrationStatus,
        )

        batch_id, ids = batch
        field = roll_field()
        # A second disputed position on the same field.
        field = type(field)(
            zone_id=field.zone_id,
            label=field.label,
            field_type=field.field_type,
            value="??",
            status="multiple",
            needs_review=True,
            characters=(
                CharacterView(
                    position=0,
                    value="1-4",
                    status="multiple",
                    top_fill=0.8,
                    margin=0.02,
                    confidence=0.0,
                ),
                CharacterView(
                    position=1,
                    value="3-8",
                    status="multiple",
                    top_fill=0.8,
                    margin=0.02,
                    confidence=0.0,
                ),
            ),
        )
        result = make_result(
            source_path=Path("sheet_0.png"),
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
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[0],
            result=result,
            template=template,
        )
        found = review_store.list_conflicts(
            database, batch_id, filters=review_store.ConflictFilter(scan_id=ids[0])
        )
        return batch_id, ids[0], [item.conflict_id for item in found]

    def test_one_call_decides_several_positions(self, database, two_positions):
        batch_id, scan_id, conflicts = two_positions
        assert len(conflicts) == 2

        edit = review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="roll_number",
            values={0: "1", 1: "3"},
            display_value="13",
            field_label="Roll number",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )

        assert edit.conflict_count == 2
        assert sorted(edit.changed) == [0, 1]
        for conflict_id in conflicts:
            assert review_store.get_conflict(database, conflict_id).state is (
                ConflictState.RESOLVED
            )

    def test_every_position_shares_one_action_identifier(self, database, two_positions):
        batch_id, scan_id, conflicts = two_positions
        edit = review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="roll_number",
            values={0: "1", 1: "3"},
            display_value="13",
            field_label="Roll number",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        groups = set()
        for conflict_id in conflicts:
            corrected = [
                item
                for item in review_store.history_for(database, conflict_id)
                if item.action is ReviewAction.CORRECTED
            ]
            assert len(corrected) == 1
            groups.add(review_store.group_of(corrected[0].detail))
        assert groups == {edit.group}

    def test_the_reason_reaches_every_position(self, database, two_positions):
        batch_id, scan_id, conflicts = two_positions
        review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="roll_number",
            values={0: "1", 1: "3"},
            display_value="13",
            field_label="Roll number",
            reviewer=REVIEWER,
            reason=ReasonCode.THRESHOLD_ERROR,
            reason_text="read from the script",
        )
        for conflict_id in conflicts:
            found = review_store.provenance_for(database, conflict_id)
            assert found.reason == ReasonCode.THRESHOLD_ERROR.value
            assert found.reason_text == "read from the script"

    def test_other_without_a_note_is_refused(self, database, two_positions):
        batch_id, scan_id, conflicts = two_positions
        with pytest.raises(ReviewError):
            review_store.correct_field(
                database,
                batch_id=batch_id,
                scan_id=scan_id,
                zone_id="roll_number",
                values={0: "1", 1: "3"},
                display_value="13",
                field_label="Roll number",
                reviewer=REVIEWER,
                reason=ReasonCode.OTHER,
            )
        assert review_store.get_conflict(database, conflicts[0]).state is (
            ConflictState.OPEN
        )

    def test_a_position_already_decided_that_way_is_left_alone(
        self, database, two_positions
    ):
        batch_id, scan_id, conflicts = two_positions
        correct(database, conflicts[0], "1")

        edit = review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="roll_number",
            values={0: "1", 1: "3"},
            display_value="13",
            field_label="Roll number",
            reviewer=OTHER_REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )

        assert edit.changed == {1: "3"}
        assert edit.unchanged == (0,)
        # ...and its history did not gain a second identical correction.
        corrected = [
            item
            for item in review_store.history_for(database, conflicts[0])
            if item.action is ReviewAction.CORRECTED
        ]
        assert len(corrected) == 1

    def test_a_position_with_no_conflict_is_reported_not_invented(
        self, database, two_positions
    ):
        batch_id, scan_id, _ = two_positions
        edit = review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="roll_number",
            values={0: "1", 1: "3", 7: "9"},
            display_value="13",
            field_label="Roll number",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        assert edit.missing == (7,)
        assert 7 not in edit.changed

    def test_an_edit_that_changes_nothing_is_refused(self, database, two_positions):
        batch_id, scan_id, conflicts = two_positions
        for conflict_id, value in zip(conflicts, ("1", "3"), strict=True):
            correct(database, conflict_id, value)

        with pytest.raises(ReviewError):
            review_store.correct_field(
                database,
                batch_id=batch_id,
                scan_id=scan_id,
                zone_id="roll_number",
                values={0: "1", 1: "3"},
                display_value="13",
                field_label="Roll number",
                reviewer=REVIEWER,
                reason=ReasonCode.DOMINANT_MARK,
            )

    def test_undoing_the_edit_takes_back_every_position(self, database, two_positions):
        batch_id, scan_id, conflicts = two_positions
        edit = review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="roll_number",
            values={0: "1", 1: "3"},
            display_value="13",
            field_label="Roll number",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )

        reversed_commands = review_store.undo_field_edit(
            database, batch_id=batch_id, group=edit.group, reviewer=REVIEWER
        )

        assert len(reversed_commands) == 2
        for conflict_id in conflicts:
            assert review_store.get_conflict(database, conflict_id).state is (
                ConflictState.OPEN
            )
            assert review_store.provenance_for(database, conflict_id).source is (
                ValueSource.MACHINE
            )

    def test_the_undo_target_names_the_group(self, database, two_positions):
        batch_id, scan_id, _ = two_positions
        edit = review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="roll_number",
            values={0: "1", 1: "3"},
            display_value="13",
            field_label="Roll number",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        target = review_store.last_decision(database, batch_id)
        assert target is not None
        assert target.group == edit.group

    def test_a_later_decision_is_not_rolled_over(self, database, two_positions):
        # An edit whose positions have since been decided again must not be
        # quietly rolled back over that later work.
        batch_id, scan_id, conflicts = two_positions
        edit = review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=scan_id,
            zone_id="roll_number",
            values={0: "1", 1: "3"},
            display_value="13",
            field_label="Roll number",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        correct(database, conflicts[0], "7", reviewer=OTHER_REVIEWER)

        review_store.undo_field_edit(
            database, batch_id=batch_id, group=edit.group, reviewer=REVIEWER
        )

        assert review_store.provenance_for(database, conflicts[0]).value == "7"
        assert review_store.provenance_for(database, conflicts[1]).source is (
            ValueSource.MACHINE
        )


class TestQueueOrderIsSheetMajor:
    def test_a_sheets_conflicts_are_contiguous(self, database, template, batch):
        # The root cause of the queue jumping: ordering by severity across the
        # batch split each sheet's conflicts apart.
        batch_id, ids = batch
        for index, scan_id in enumerate(ids):
            stage(database, template, batch_id, scan_id, f"sheet_{index}.png")

        order = [item.scan_id for item in review_store.list_conflicts(database, batch_id)]
        runs = [key for key, _ in itertools.groupby(order)]
        assert len(runs) == len(set(runs)), "a sheet appears in two separate runs"


class TestUndoSurvivesReopeningTheProject:
    def test_an_undone_decision_is_still_undone_after_reopening(
        self, tmp_path, template
    ):
        path = tmp_path / "database.sqlite"
        handle = open_project_database(path, create=True)
        try:
            scan = tmp_path / "sheet_0.png"
            scan.write_bytes(b"placeholder")
            batch_id = batch_store.create_batch(
                handle, [scan], identity=batch_store.BatchIdentity.of(template)
            )
            scan_id = batch_store.scan_ids_by_path(handle, batch_id)[scan]
            conflict_id = stage(handle, template, batch_id, scan_id, "sheet_0.png")
            correct(handle, conflict_id, "1")
            review_store.undo_decision(handle, conflict_id, reviewer=REVIEWER)
        finally:
            handle.close()

        reopened = open_project_database(path, create=False)
        try:
            found = review_store.provenance_for(reopened, conflict_id)
            assert found.source is ValueSource.MACHINE
            assert review_store.get_conflict(reopened, conflict_id).state is (
                ConflictState.OPEN
            )
            # And the reversed decision is still readable.
            actions = [
                item.action for item in review_store.history_for(reopened, conflict_id)
            ]
            assert ReviewAction.CORRECTED in actions
            assert ReviewAction.UNDONE in actions
        finally:
            reopened.close()
