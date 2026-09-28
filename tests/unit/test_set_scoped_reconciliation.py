"""A set's reconciliation sees that set's scripts, and nothing else silently.

Scope:
    One batch containing papers of three sets. Each set's roster must be
    reconciled against the scripts whose **effective** set code is that set's
    - never against another set's scripts as "unknown candidate IDs" - and a
    script whose set code is unsettled or names no defined set must be
    counted, and raised on the Resolve stage, rather than vanish.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.unit.test_recognition_contract import make_result
from tests.unit.test_reconciliation_store import validation_of

from omr_scanner.database import open_project_database
from omr_scanner.database.models import BatchScan, ScanBatch
from omr_scanner.domain.reconciliation import AttendanceState, ReconciliationStatus
from omr_scanner.domain.review import ConflictState, ConflictType, ReasonCode
from omr_scanner.services import (
    batch_store,
    project_sets,
    reconciliation_store,
    review_store,
)
from omr_scanner.services.recognition_models import (
    CharacterView,
    FieldView,
    RecognitionOutcome,
    RegistrationStatus,
)
from omr_scanner.services.reconciliation_store import ScriptSetPlacement

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.recognition_models import ScanResult

REVIEWER = "Dr. A. Rahman"
P = AttendanceState.PRESENT

# (file, roll, set code as read)
SCRIPTS = (
    ("s1a.png", "100001", "1"),
    ("s1b.png", "100002", "1"),
    ("s2a.png", "200001", "2"),
    ("s2b.png", "200002", "2"),
    ("s3a.png", "300001", "3"),
    ("s3b.png", "300002", "3"),
    ("s_unread.png", "200003", "?"),  # set code double-marked
    ("s_undefined.png", "300003", "9"),  # a set this project does not have
)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[ProjectDatabase]:
    handle = open_project_database(tmp_path / "database.sqlite", create=True)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def world(database, tmp_path):
    """Three sets, one roster each, one batch holding all their scripts."""
    sets = {code: project_sets.add_set(database, code, f"Paper {code}") for code in "123"}
    rosters = {
        "1": [("100001", "ONE A", P), ("100002", "ONE B", P)],
        "2": [("200001", "TWO A", P), ("200002", "TWO B", P), ("200003", "TWO C", P)],
        "3": [("300001", "THREE A", P), ("300002", "THREE B", P), ("300003", "THREE C", P)],
    }
    roster_ids = {
        code: reconciliation_store.import_roster(
            database, validation_of(rows), set_id=sets[code].set_id
        )
        for code, rows in rosters.items()
    }
    batch_id = batch_store.new_batch_id()
    now = datetime.now(UTC)
    with database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id, created_at=now, updated_at=now,
                source_folder=str(tmp_path), status="completed", total_scans=len(SCRIPTS),
            )
        )
        session.flush()
        for index, (name, roll, code) in enumerate(SCRIPTS):
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=index, source_path=str(tmp_path / name),
                    filename=name, status="completed", identifier_value=roll,
                    set_code_value=code, result_json="",
                )
            )
    ids = {
        row.filename: row.scan_id
        for row in _scans(database, batch_id)
    }
    # The double-marked set code is a Resolve-stage conflict, as a real read makes it.
    review_store.sync_conflicts(
        database,
        batch_id=batch_id,
        scan_id=ids["s_unread.png"],
        result=_double_marked_set_code(tmp_path / "s_unread.png"),
        template=_template(),
    )
    return sets, roster_ids, batch_id, ids


def _scans(database, batch_id) -> list[BatchScan]:
    from sqlalchemy import select

    with database.session() as session:
        return list(
            session.scalars(select(BatchScan).where(BatchScan.batch_id == batch_id)).all()
        )


def _template() -> OmrTemplate:
    from tests.conftest import build_answer_sheet_template

    return build_answer_sheet_template(set_symbols=("1", "2", "3", "9"))


def _double_marked_set_code(path: Path) -> ScanResult:
    field = FieldView(
        zone_id="set_code", label="Set code", field_type="set_code", value="?",
        status="multiple", needs_review=True,
        characters=(
            CharacterView(position=0, value="2-3", status="multiple",
                          top_fill=0.8, margin=0.0, confidence=0.0),
        ),
    )
    return make_result(
        source_path=path, outcome=RecognitionOutcome.REVIEW,
        registration=RegistrationStatus.REGISTERED, warnings=(), status_codes=(),
        fields=(field,), answers=(), bubbles=(),
        identifier_zone_id="roll_number", set_code_zone_id="set_code",
    )


def reconciled(database, roster_id, batch_id):
    reconciliation_store.reconcile_batch(database, roster_id, batch_id)
    return {
        entry.candidate_id: entry
        for entry in reconciliation_store.list_entries(database, roster_id, batch_id)
    }


def script_names(entries) -> set[str]:
    return {view.script.source_name for entry in entries.values() for view in entry.scripts}


class TestEachSetSeesOnlyItsOwnScripts:
    @pytest.mark.parametrize(
        ("code", "names"),
        [
            ("1", {"s1a.png", "s1b.png"}),
            ("2", {"s2a.png", "s2b.png"}),
            ("3", {"s3a.png", "s3b.png"}),
        ],
    )
    def test_only_that_sets_scripts_are_reconciled(self, database, world, code, names):
        _sets, roster_ids, batch_id, _ids = world
        entries = reconciled(database, roster_ids[code], batch_id)
        assert script_names(entries) == names

    def test_another_sets_scripts_are_never_unknown_ids(self, database, world):
        _sets, roster_ids, batch_id, _ids = world
        entries = reconciled(database, roster_ids["2"], batch_id)
        statuses = {entry.status for entry in entries.values()}
        assert ReconciliationStatus.UNKNOWN_ID not in statuses
        assert not any(entry_id.startswith(("1000", "3000")) for entry_id in entries)

    def test_the_scope_says_what_was_left_out_and_why(self, database, world):
        _sets, roster_ids, batch_id, _ids = world
        review_store.sync_undefined_set_codes(database, batch_id)
        scope = reconciliation_store.script_scope(database, roster_ids["2"], batch_id)
        assert scope.set_code == "2"
        assert scope.in_set == 2
        assert scope.other_sets == {"1": 2, "3": 2}
        assert scope.unresolved == 2, "the double mark, and the undefined set 9"
        assert scope.needs_attention == 2

    def test_a_candidate_whose_script_has_an_unsettled_set_code_is_given_a_lead(
        self, database, world
    ):
        from omr_scanner.services.reconciliation_leads import script_leads

        _sets, roster_ids, batch_id, ids = world
        entries = reconciled(database, roster_ids["2"], batch_id)
        missing = entries["200003"]
        # Reject & Rescan terminology pass: a script with exactly this ID
        # exists, its set code merely unsettled, so this reads "Script found -
        # set unresolved" rather than "Missing script". The leads are unchanged.
        assert missing.status is ReconciliationStatus.SCRIPT_SET_UNRESOLVED
        outside = reconciliation_store.out_of_set_scripts(
            database, roster_ids["2"], batch_id
        )
        leads = script_leads(missing, list(entries.values()), outside=outside)
        # The script with the same ID and an unresolved set code comes first;
        # the undefined-set script reading 300003 (one edit away) is also a
        # fair lead, and ranks below it.
        assert [lead.scan_id for lead in leads] == [
            ids["s_unread.png"],
            ids["s_undefined.png"],
        ]
        assert "not yet resolved" in leads[0].reason
        assert leads[0].distance == 0
        assert "not a defined set" in leads[1].reason


class TestUnresolvedAndOverriddenSetCodes:
    def test_an_unresolved_set_code_belongs_to_no_set(self, database, world):
        _sets, roster_ids, batch_id, _ids = world
        for code in "123":
            assert "s_unread.png" not in script_names(
                reconciled(database, roster_ids[code], batch_id)
            )

    def test_resolving_it_brings_the_script_into_that_set(self, database, world):
        _sets, roster_ids, batch_id, ids = world
        (conflict,) = [
            item
            for item in review_store.list_conflicts(
                database, batch_id,
                filters=review_store.ConflictFilter(scan_id=ids["s_unread.png"]),
            )
            if item.field.kind.value == "set_code"
        ]
        review_store.correct_value(
            database, conflict.conflict_id, value="2", reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        two = reconciled(database, roster_ids["2"], batch_id)
        assert two["200003"].status is ReconciliationStatus.MATCHED
        assert "s_unread.png" not in script_names(reconciled(database, roster_ids["3"], batch_id))

    def test_a_manual_override_moves_a_script_between_sets(self, database, world):
        # s3b was read confidently as set 3; the paper is set 2's.
        _sets, roster_ids, batch_id, ids = world
        from omr_scanner.domain.review import FieldKind, MachineObservation

        review_store.correct_field(
            database,
            batch_id=batch_id,
            scan_id=ids["s3b.png"],
            zone_id="set_code",
            values={0: "2"},
            display_value="2",
            field_label="Set code",
            reviewer=REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
            overrides={0: MachineObservation(value="3", status="resolved", confidence=1.0)},
            field_kind=FieldKind.SET_CODE,
            previous_value="3",
        )
        assert "s3b.png" in script_names(reconciled(database, roster_ids["2"], batch_id))
        three = reconciled(database, roster_ids["3"], batch_id)
        assert "s3b.png" not in script_names(three)
        assert three["300002"].status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT


class TestAnUndefinedSetCode:
    def test_it_is_raised_on_the_resolve_stage(self, database, world):
        _sets, _roster_ids, batch_id, ids = world
        assert review_store.sync_undefined_set_codes(database, batch_id) == 1
        (record,) = [
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.conflict_type is ConflictType.SET_CODE_UNDEFINED
        ]
        assert record.scan_id == ids["s_undefined.png"]
        assert record.state is ConflictState.OPEN
        assert "not one of this project's sets" in record.observation.detail

    def test_the_sync_is_idempotent(self, database, world):
        _sets, _roster_ids, batch_id, _ids = world
        review_store.sync_undefined_set_codes(database, batch_id)
        review_store.sync_undefined_set_codes(database, batch_id)
        assert (
            sum(
                1
                for item in review_store.list_conflicts(database, batch_id)
                if item.conflict_type is ConflictType.SET_CODE_UNDEFINED
            )
            == 1
        )

    def test_correcting_it_places_the_script_in_a_set(self, database, world):
        _sets, roster_ids, batch_id, _ids = world
        review_store.sync_undefined_set_codes(database, batch_id)
        (record,) = [
            item
            for item in review_store.list_conflicts(database, batch_id)
            if item.conflict_type is ConflictType.SET_CODE_UNDEFINED
        ]
        review_store.correct_value(
            database, record.conflict_id, value="3", reviewer=REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
        )
        three = reconciled(database, roster_ids["3"], batch_id)
        assert three["300003"].status is ReconciliationStatus.MATCHED
        scope = reconciliation_store.script_scope(database, roster_ids["3"], batch_id)
        assert scope.undefined == 0

    def test_adding_the_set_withdraws_the_untouched_conflict(self, database, world):
        _sets, _roster_ids, batch_id, _ids = world
        review_store.sync_undefined_set_codes(database, batch_id)
        project_sets.add_set(database, "9", "Paper 9")
        assert review_store.sync_undefined_set_codes(database, batch_id) == 0
        (record,) = [
            item
            for item in review_store.list_conflicts(
                database, batch_id,
                filters=review_store.ConflictFilter(include_withdrawn=True),
            )
            if item.conflict_type is ConflictType.SET_CODE_UNDEFINED
        ]
        assert record.state is ConflictState.WITHDRAWN

    def test_nothing_is_raised_when_no_sets_are_defined(self, tmp_path):
        handle = open_project_database(tmp_path / "plain.sqlite", create=True)
        try:
            batch_id = batch_store.new_batch_id()
            now = datetime.now(UTC)
            with handle.session() as session:
                session.add(ScanBatch(batch_id=batch_id, created_at=now, updated_at=now,
                                      source_folder=str(tmp_path), status="completed",
                                      total_scans=1))
                session.flush()
                session.add(BatchScan(batch_id=batch_id, batch_index=0,
                                      source_path=str(tmp_path / "a.png"), filename="a.png",
                                      status="completed", identifier_value="1",
                                      set_code_value="Z", result_json=""))
            assert review_store.sync_undefined_set_codes(handle, batch_id) == 0
        finally:
            handle.close()


class TestAnUnscopedRosterIsUnchanged:
    def test_a_project_list_from_before_sets_reconciles_every_script(self, database, world):
        _sets, _roster_ids, batch_id, _ids = world
        legacy = reconciliation_store.import_roster(
            database, validation_of([("100001", "ONE A", P)]), set_id=None
        )
        entries = reconciled(database, legacy, batch_id)
        assert len(script_names(entries)) == len(SCRIPTS)


def test_placement_is_an_enumeration_ready_for_rescan():
    # Reject & Rescan added three placements, as this enumeration anticipated.
    assert {item.value for item in ScriptSetPlacement} == {
        "in_set", "other_set", "unresolved", "undefined",
        "rejected", "rejected_unplaced", "superseded",
    }
