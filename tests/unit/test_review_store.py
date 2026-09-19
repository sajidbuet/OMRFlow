"""Tests for conflicts, decisions and the provenance ledger (Phase 6).

Scope:
    The rules that make a correction trustworthy: the machine's reading is
    never overwritten, every decision is named and reasoned, the ledger cannot
    be rewritten, and the effective value is always reconstructible from the
    history that justifies it.

Why a real SQLite file rather than a mock:
    Every property worth asserting here is a property of the database - the
    unique constraint that makes detection idempotent, the transaction that
    makes a correction atomic, and the triggers that make the ledger
    append-only. A mocked session would assert that the code calls the
    functions it calls, which is the one thing worth nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete, update
from tests.conftest import build_answer_sheet_template
from tests.unit.test_recognition_contract import make_answer, make_result

from omr_scanner.database import open_project_database
from omr_scanner.database.models import AuditEvent, BatchScan, ReviewConflict
from omr_scanner.domain.review import (
    ConflictState,
    ConflictType,
    ReasonCode,
    ReviewAction,
    ValueSource,
)
from omr_scanner.services import batch_store, review_store
from omr_scanner.services.recognition_models import (
    RecognitionOutcome,
    RegistrationStatus,
)
from omr_scanner.services.review_store import ReviewError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omr_scanner.database import ProjectDatabase

REVIEWER = "Dr. X"
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
    """A batch with three registered scans, and their durable ids."""
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


def result_with_double_mark(path: Path = Path("sheet_0.png")):
    """A result whose question 1 carries two marks."""
    return make_result(
        source_path=path,
        outcome=RecognitionOutcome.REVIEW,
        registration=RegistrationStatus.REGISTERED,
        warnings=(),
        status_codes=("MULTIPLE_MARK",),
        fields=(),
        answers=(make_answer(1, "B-D", "multiple"),),
        bubbles=(),
        identifier_zone_id="roll_number",
        set_code_zone_id="set_code",
    )


@pytest.fixture
def conflict(database, template, batch) -> int:
    """One stored ANSWER_MULTIPLE conflict, and its id."""
    batch_id, ids = batch
    review_store.sync_conflicts(
        database,
        batch_id=batch_id,
        scan_id=ids[0],
        result=result_with_double_mark(),
        template=template,
    )
    found = review_store.list_conflicts(database, batch_id)
    return found[0].conflict_id


class TestDetectionIsIdempotent:
    def test_syncing_creates_one_conflict_per_problem(self, database, template, batch):
        batch_id, ids = batch
        count = review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[0],
            result=result_with_double_mark(),
            template=template,
        )
        assert count == 1
        assert len(review_store.list_conflicts(database, batch_id)) == 1

    def test_syncing_the_same_sheet_again_creates_nothing_new(
        self, database, template, batch
    ):
        # The Phase 5 case: a retry, a resume, a re-review. Without this a
        # resumed batch would fill the queue with duplicates.
        batch_id, ids = batch
        for _ in range(4):
            review_store.sync_conflicts(
                database,
                batch_id=batch_id,
                scan_id=ids[0],
                result=result_with_double_mark(),
                template=template,
            )
        assert len(review_store.list_conflicts(database, batch_id)) == 1

    def test_an_unchanged_resync_writes_nothing_to_the_ledger(
        self, database, template, batch, conflict
    ):
        before = len(review_store.history_for(database, conflict))
        review_store.sync_conflicts(
            database,
            batch_id=batch[0],
            scan_id=batch[1][0],
            result=result_with_double_mark(),
            template=template,
        )
        assert len(review_store.history_for(database, conflict)) == before

    def test_a_changed_machine_reading_is_recorded_not_overwritten_silently(
        self, database, template, batch, conflict
    ):
        # Even the machine does not get to revise itself without a trace.
        changed = make_result(
            source_path=Path("sheet_0.png"),
            outcome=RecognitionOutcome.REVIEW,
            registration=RegistrationStatus.REGISTERED,
            warnings=(),
            fields=(),
            answers=(make_answer(1, "B-C", "multiple"),),
            bubbles=(),
            identifier_zone_id="roll_number",
            set_code_zone_id="set_code",
        )
        review_store.sync_conflicts(
            database,
            batch_id=batch[0],
            scan_id=batch[1][0],
            result=changed,
            template=template,
        )
        history = review_store.history_for(database, conflict)
        reread = [item for item in history if item.action is ReviewAction.RE_RECOGNISED]
        assert len(reread) == 1
        assert reread[0].previous_value == "B-D"
        assert reread[0].new_value == "B-C"

    def test_a_conflict_the_machine_no_longer_reports_is_withdrawn_not_deleted(
        self, database, template, batch, conflict
    ):
        clean = make_result(
            source_path=Path("sheet_0.png"),
            outcome=RecognitionOutcome.COMPLETE,
            registration=RegistrationStatus.REGISTERED,
            warnings=(),
            fields=(),
            answers=(make_answer(1, "B", "resolved", needs_review=False),),
            bubbles=(),
            identifier_zone_id="roll_number",
            set_code_zone_id="set_code",
        )
        review_store.sync_conflicts(
            database, batch_id=batch[0], scan_id=batch[1][0], result=clean, template=template
        )
        record = review_store.get_conflict(database, conflict)
        assert record.state is ConflictState.WITHDRAWN
        # Kept: the fact that the machine once disputed this is itself evidence.
        assert review_store.history_for(database, conflict)

    def test_a_human_decision_is_never_withdrawn_by_a_later_read(
        self, database, template, batch, conflict
    ):
        # A machine may withdraw its own complaint. It may not erase a person's
        # decision.
        review_store.correct_value(
            database,
            conflict,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        clean = make_result(
            source_path=Path("sheet_0.png"),
            outcome=RecognitionOutcome.COMPLETE,
            registration=RegistrationStatus.REGISTERED,
            warnings=(),
            fields=(),
            answers=(make_answer(1, "B", "resolved", needs_review=False),),
            bubbles=(),
            identifier_zone_id="roll_number",
            set_code_zone_id="set_code",
        )
        review_store.sync_conflicts(
            database, batch_id=batch[0], scan_id=batch[1][0], result=clean, template=template
        )
        record = review_store.get_conflict(database, conflict)
        assert record.state is ConflictState.RESOLVED
        assert review_store.provenance_for(database, conflict).reviewer == REVIEWER


class TestMachineValueIsNeverOverwritten:
    def test_a_correction_leaves_the_machine_value_alone(self, database, conflict):
        review_store.correct_value(
            database,
            conflict,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )
        record = review_store.get_conflict(database, conflict)
        assert record.observation.value == "B-D"
        found = review_store.provenance_for(database, conflict)
        assert found.value == "B"
        assert found.machine_value == "B-D"
        assert found.was_corrected is True

    def test_two_corrections_both_leave_it_alone(self, database, conflict):
        review_store.correct_value(
            database, conflict, value="B", reviewer=REVIEWER, reason=ReasonCode.DOMINANT_MARK
        )
        review_store.reopen(database, conflict, reviewer=OTHER_REVIEWER)
        review_store.correct_value(
            database,
            conflict,
            value="D",
            reviewer=OTHER_REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
        )
        record = review_store.get_conflict(database, conflict)
        assert record.observation.value == "B-D"

    def test_the_machine_columns_are_untouched_in_storage(self, database, conflict):
        review_store.correct_value(
            database, conflict, value="A", reviewer=REVIEWER, reason=ReasonCode.STRAY_MARK
        )
        with database.session() as session:
            row = session.get(ReviewConflict, conflict)
            assert row.machine_value == "B-D"
            assert row.machine_status == "multiple"


class TestReviewerAndReasonAreRequired:
    @pytest.mark.parametrize("name", ["", "   ", "\t\n"])
    def test_a_correction_without_a_reviewer_is_refused(self, database, conflict, name):
        with pytest.raises(ReviewError, match="reviewer"):
            review_store.correct_value(
                database,
                conflict,
                value="B",
                reviewer=name,
                reason=ReasonCode.DOMINANT_MARK,
            )
        # And nothing was written.
        assert review_store.get_conflict(database, conflict).state is ConflictState.OPEN

    def test_accepting_without_a_reviewer_is_refused(self, database, conflict):
        with pytest.raises(ReviewError):
            review_store.accept_machine_value(database, conflict, reviewer="")

    def test_other_requires_an_explanation(self, database, conflict):
        with pytest.raises(ReviewError, match="other"):
            review_store.correct_value(
                database,
                conflict,
                value="B",
                reviewer=REVIEWER,
                reason=ReasonCode.OTHER,
                reason_text="   ",
            )

    def test_other_with_an_explanation_is_accepted(self, database, conflict):
        found = review_store.correct_value(
            database,
            conflict,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.OTHER,
            reason_text="Candidate annotated the margin",
        )
        assert found.reason_text == "Candidate annotated the margin"

    def test_a_reviewer_name_is_trimmed(self, database, conflict):
        review_store.accept_machine_value(database, conflict, reviewer="  Dr. X  ")
        assert review_store.provenance_for(database, conflict).reviewer == "Dr. X"

    def test_a_value_correction_is_refused_for_a_processing_failure(
        self, database, template, batch
    ):
        batch_id, ids = batch
        broken = make_result(
            source_path=Path("sheet_1.png"),
            outcome=RecognitionOutcome.ERROR,
            registration=RegistrationStatus.FAILED,
            status_codes=("IMAGE_LOAD_ERROR",),
            fields=(),
            answers=(),
            bubbles=(),
            warnings=(),
        )
        review_store.sync_conflicts(
            database, batch_id=batch_id, scan_id=ids[1], result=broken, template=template
        )
        found = review_store.list_conflicts(
            database, batch_id, filters=review_store.ConflictFilter(scan_id=ids[1])
        )
        with pytest.raises(ReviewError, match="value"):
            review_store.correct_value(
                database,
                found[0].conflict_id,
                value="B",
                reviewer=REVIEWER,
                reason=ReasonCode.CLEAR_VISUAL_MARK,
            )


class TestAcceptingIsAReviewEvent:
    def test_accepting_changes_the_source_without_changing_the_value(
        self, database, conflict
    ):
        # "A human checked this" and "nobody has looked" are different facts,
        # even when the value is identical.
        before = review_store.provenance_for(database, conflict)
        assert before.source is ValueSource.MACHINE

        after = review_store.accept_machine_value(database, conflict, reviewer=REVIEWER)
        assert after.value == before.value == "B-D"
        assert after.source is ValueSource.HUMAN
        assert after.reviewer == REVIEWER
        assert after.was_corrected is False

    def test_accepting_records_a_reason_without_the_reviewer_typing_one(
        self, database, conflict
    ):
        found = review_store.accept_machine_value(database, conflict, reviewer=REVIEWER)
        assert found.reason == ReasonCode.MACHINE_CONFIRMED.value


class TestAuditHistory:
    def test_history_begins_with_what_the_machine_saw(self, database, conflict):
        history = review_store.history_for(database, conflict)
        assert history[0].action is ReviewAction.DETECTED
        assert history[0].new_value == "B-D"
        assert history[0].reviewer == ""

    def test_every_action_appends_exactly_one_event(self, database, conflict):
        review_store.correct_value(
            database, conflict, value="B", reviewer=REVIEWER, reason=ReasonCode.DOMINANT_MARK
        )
        review_store.reopen(database, conflict, reviewer=OTHER_REVIEWER)
        review_store.correct_value(
            database,
            conflict,
            value="D",
            reviewer=OTHER_REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
        )
        actions = [item.action for item in review_store.history_for(database, conflict)]
        assert actions == [
            ReviewAction.DETECTED,
            ReviewAction.CORRECTED,
            ReviewAction.REOPENED,
            ReviewAction.CORRECTED,
        ]

    def test_the_first_correction_is_preserved_verbatim(self, database, conflict):
        review_store.correct_value(
            database, conflict, value="B", reviewer=REVIEWER, reason=ReasonCode.DOMINANT_MARK
        )
        review_store.reopen(database, conflict, reviewer=OTHER_REVIEWER)
        review_store.correct_value(
            database,
            conflict,
            value="D",
            reviewer=OTHER_REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
        )
        first = review_store.history_for(database, conflict)[1]
        # Not rewritten to look as though Dr. X had chosen D all along.
        assert first.reviewer == REVIEWER
        assert first.new_value == "B"
        assert first.reason_code == ReasonCode.DOMINANT_MARK.value

    def test_history_is_ordered_oldest_first(self, database, conflict):
        review_store.defer(database, conflict, reviewer=REVIEWER)
        review_store.accept_machine_value(database, conflict, reviewer=REVIEWER)
        history = review_store.history_for(database, conflict)
        ids = [item.event_id for item in history]
        assert ids == sorted(ids)


class TestAuditIsAppendOnly:
    def test_the_module_exposes_no_way_to_update_or_delete_an_event(self):
        # Enforcement level one: the repository's own surface. A reviewer of
        # this code should not have to rely on discipline to see it.
        exported = set(review_store.__all__)
        forbidden = {
            name
            for name in exported
            if ("event" in name or "audit" in name)
            and any(verb in name for verb in ("update", "delete", "edit", "remove"))
        }
        assert forbidden == set()
        assert not hasattr(review_store, "update_event")
        assert not hasattr(review_store, "delete_event")

    def test_the_database_refuses_an_update(self, database, conflict):
        # Enforcement level three: a trigger, so that a hand-written statement
        # or a careless future flush fails loudly rather than rewriting history.
        with pytest.raises(Exception, match="append-only"), database.session() as session:
            session.execute(update(AuditEvent).values(reviewer="Someone Else"))

    def test_the_database_refuses_a_delete(self, database, conflict):
        with pytest.raises(Exception, match="append-only"), database.session() as session:
            session.execute(delete(AuditEvent))

    def test_the_ledger_survives_a_refused_tamper(self, database, conflict):
        before = review_store.history_for(database, conflict)
        with pytest.raises(Exception, match="append-only"), database.session() as session:
            session.execute(update(AuditEvent).values(new_value="tampered"))
        assert review_store.history_for(database, conflict) == before


class TestReopenAndSupersede:
    def test_reopening_falls_back_to_the_machine_value(self, database, conflict):
        # Reopening withdraws the *decision*, so the effective value reverts to
        # what the machine read until somebody decides again.
        review_store.correct_value(
            database, conflict, value="B", reviewer=REVIEWER, reason=ReasonCode.DOMINANT_MARK
        )
        review_store.reopen(database, conflict, reviewer=OTHER_REVIEWER)
        found = review_store.provenance_for(database, conflict)
        assert found.source is ValueSource.MACHINE
        assert found.value == "B-D"
        assert review_store.get_conflict(database, conflict).state is ConflictState.OPEN

    def test_the_second_correction_wins(self, database, conflict):
        review_store.correct_value(
            database, conflict, value="B", reviewer=REVIEWER, reason=ReasonCode.DOMINANT_MARK
        )
        review_store.reopen(database, conflict, reviewer=OTHER_REVIEWER)
        review_store.correct_value(
            database,
            conflict,
            value="D",
            reviewer=OTHER_REVIEWER,
            reason=ReasonCode.MISCLASSIFICATION,
        )
        found = review_store.provenance_for(database, conflict)
        assert found.value == "D"
        assert found.reviewer == OTHER_REVIEWER
        assert found.machine_value == "B-D"

    def test_deferring_decides_nothing(self, database, conflict):
        found = review_store.defer(database, conflict, reviewer=REVIEWER)
        assert found.source is ValueSource.MACHINE
        assert review_store.get_conflict(database, conflict).state is ConflictState.DEFERRED

    def test_a_deferred_conflict_still_counts_as_unresolved(self, database, batch, conflict):
        review_store.defer(database, conflict, reviewer=REVIEWER)
        counts = review_store.count_conflicts(database, batch[0])
        assert counts.deferred == 1
        assert counts.unresolved == 1
        assert counts.is_clear is False


class TestCachedStateMatchesTheLedger:
    @pytest.mark.parametrize(
        "steps",
        [
            ("correct",),
            ("accept",),
            ("defer",),
            ("correct", "reopen"),
            ("correct", "reopen", "correct"),
            ("defer", "accept"),
        ],
    )
    def test_the_cached_state_is_always_reconstructible(self, database, conflict, steps):
        # The cache exists to make a ten-thousand-row queue fast. It must never
        # become a second, divergent source of truth.
        for step in steps:
            if step == "correct":
                review_store.correct_value(
                    database,
                    conflict,
                    value="B",
                    reviewer=REVIEWER,
                    reason=ReasonCode.DOMINANT_MARK,
                )
            elif step == "accept":
                review_store.accept_machine_value(database, conflict, reviewer=REVIEWER)
            elif step == "defer":
                review_store.defer(database, conflict, reviewer=REVIEWER)
            else:
                review_store.reopen(database, conflict, reviewer=REVIEWER)

        history = review_store.history_for(database, conflict)
        assert review_store.recompute_state(history) is (
            review_store.get_conflict(database, conflict).state
        )


class TestTransactionalCorrections:
    def test_a_failed_write_leaves_neither_the_event_nor_the_state(
        self, database, conflict, monkeypatch
    ):
        # A correction is atomic: an event without its state change, or a state
        # change without its event, must not be committable.
        before_state = review_store.get_conflict(database, conflict).state
        before_events = len(review_store.history_for(database, conflict))

        real = review_store._append_event

        def explode(session: object, **kwargs: object) -> None:
            real(session, **kwargs)
            raise RuntimeError("storage went away mid-transaction")

        monkeypatch.setattr(review_store, "_append_event", explode)
        with pytest.raises(RuntimeError, match="storage went away"):
            review_store.correct_value(
                database,
                conflict,
                value="B",
                reviewer=REVIEWER,
                reason=ReasonCode.DOMINANT_MARK,
            )

        monkeypatch.undo()
        assert review_store.get_conflict(database, conflict).state is before_state
        assert len(review_store.history_for(database, conflict)) == before_events


class TestQueueReads:
    def test_filtering_by_state(self, database, template, batch):
        batch_id, ids = batch
        for index, scan_id in enumerate(ids):
            review_store.sync_conflicts(
                database,
                batch_id=batch_id,
                scan_id=scan_id,
                result=result_with_double_mark(Path(f"sheet_{index}.png")),
                template=template,
            )
        found = review_store.list_conflicts(database, batch_id)
        review_store.correct_value(
            database,
            found[0].conflict_id,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
        )

        open_only = review_store.list_conflicts(
            database,
            batch_id,
            filters=review_store.ConflictFilter(states=(ConflictState.OPEN,)),
        )
        assert len(open_only) == 2
        resolved = review_store.list_conflicts(
            database,
            batch_id,
            filters=review_store.ConflictFilter(states=(ConflictState.RESOLVED,)),
        )
        assert len(resolved) == 1

    def test_filtering_by_type(self, database, template, batch, conflict):
        found = review_store.list_conflicts(
            database,
            batch[0],
            filters=review_store.ConflictFilter(
                conflict_types=(ConflictType.IDENTIFIER_BLANK,)
            ),
        )
        assert found == ()

    def test_searching_by_identifier(self, database, template, batch, conflict):
        with database.session() as session:
            row = session.get(BatchScan, batch[1][0])
            row.identifier_value = "170501"
        found = review_store.list_conflicts(
            database, batch[0], filters=review_store.ConflictFilter(search="1705")
        )
        assert len(found) == 1
        miss = review_store.list_conflicts(
            database, batch[0], filters=review_store.ConflictFilter(search="999999")
        )
        assert miss == ()

    def test_withdrawn_conflicts_are_hidden_from_the_working_queue(
        self, database, template, batch, conflict
    ):
        with database.session() as session:
            session.execute(
                update(ReviewConflict)
                .where(ReviewConflict.conflict_id == conflict)
                .values(state=ConflictState.WITHDRAWN.value)
            )
        assert review_store.list_conflicts(database, batch[0]) == ()
        shown = review_store.list_conflicts(
            database,
            batch[0],
            filters=review_store.ConflictFilter(states=(ConflictState.WITHDRAWN,)),
        )
        assert len(shown) == 1

    def test_the_queue_is_paged_and_counted_in_sql_at_batch_scale(
        self, database, template, tmp_path
    ):
        """10,000 conflicts must cost a page, not a walk (Phase 6 brief §31).

        Asserted by counting SQL statements rather than by timing, because a
        wall clock measures the machine that happened to run the test, while
        "the number of statements does not grow with the number of rows" is the
        property that actually keeps the queue responsive. A generous time
        ceiling is kept as a coarse guard against an accidental O(n) rewrite.
        """
        import time

        from sqlalchemy import event, insert

        from omr_scanner.database.models import ScanBatch

        scan_count = 500
        per_scan = 20
        total = scan_count * per_scan

        paths = [tmp_path / f"bulk_{index}.png" for index in range(scan_count)]
        for path in paths:
            path.write_bytes(b"placeholder")
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        ids = batch_store.scan_ids_by_path(database, batch_id)

        now = datetime.now(UTC)
        rows = [
            {
                "batch_id": batch_id,
                "scan_id": ids[path],
                "conflict_type": ConflictType.ANSWER_MULTIPLE.value,
                "scope": "field",
                "severity": 0,
                # A tenth already decided, so the state filter has real work.
                "state": (
                    ConflictState.RESOLVED.value
                    if question % 10 == 0
                    else ConflictState.OPEN.value
                ),
                "zone_id": "questions_0",
                "group_key": question,
                "field_kind": "question",
                "field_label": f"Q{question + 1}",
                "question_number": question + 1,
                "machine_value": "B-D",
                "machine_status": "multiple",
                "machine_confidence": 0.4,
                "machine_top_fill": 0.8,
                "machine_margin": 0.01,
                "machine_candidates": "",
                "machine_detail": "",
                "related_scan_ids": "",
                "created_at": now,
                "updated_at": now,
            }
            for path in paths
            for question in range(per_scan)
        ]
        with database.session() as session:
            session.execute(insert(ReviewConflict), rows)
            session.execute(
                update(ScanBatch)
                .where(ScanBatch.batch_id == batch_id)
                .values(total_scans=scan_count)
            )
        assert total >= 10_000

        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany) -> None:
            statements.append(statement)

        event.listen(database.engine, "before_cursor_execute", record)
        try:
            statements.clear()
            started = time.perf_counter()
            page = review_store.list_conflicts(
                database,
                batch_id,
                filters=review_store.ConflictFilter(states=(ConflictState.OPEN,)),
                limit=50,
            )
            page_statements = len(
                [item for item in statements if item.lstrip().upper().startswith("SELECT")]
            )
            page_seconds = time.perf_counter() - started

            statements.clear()
            started = time.perf_counter()
            counts = review_store.count_conflicts(database, batch_id)
            count_statements = len(
                [item for item in statements if item.lstrip().upper().startswith("SELECT")]
            )
            count_seconds = time.perf_counter() - started
        finally:
            event.remove(database.engine, "before_cursor_execute", record)

        # One page is one page, whatever is behind it.
        assert len(page) == 50
        assert page_statements <= 2, statements

        # The summary is grouped queries, not a walk over 10,000 rows.
        assert count_statements <= 4, statements
        assert counts.total == total
        assert counts.open_count == total - total // 10
        assert counts.resolved == total // 10
        assert counts.by_type[ConflictType.ANSWER_MULTIPLE] == total

        # Coarse guard. Both are milliseconds in practice; the ceiling is set
        # high enough that only an O(n) regression could trip it.
        assert page_seconds < 1.0
        assert count_seconds < 1.0

    def test_paging_is_stable(self, database, template, batch):
        batch_id, ids = batch
        for index, scan_id in enumerate(ids):
            review_store.sync_conflicts(
                database,
                batch_id=batch_id,
                scan_id=scan_id,
                result=result_with_double_mark(Path(f"sheet_{index}.png")),
                template=template,
            )
        first_page = review_store.list_conflicts(database, batch_id, limit=2, offset=0)
        second_page = review_store.list_conflicts(database, batch_id, limit=2, offset=2)
        assert len(first_page) == 2
        assert len(second_page) == 1
        ids_seen = [item.conflict_id for item in (*first_page, *second_page)]
        assert len(set(ids_seen)) == 3

    def test_per_sheet_counts(self, database, batch, conflict):
        counts = review_store.count_conflicts_for_scan(database, batch[0], batch[1][0])
        assert counts.total == 1
        assert counts.unresolved == 1


class TestPersistenceRoundTrip:
    def test_everything_survives_closing_and_reopening_the_database(
        self, tmp_path, template
    ):
        db_path = tmp_path / "database.sqlite"
        scan = tmp_path / "sheet.png"
        scan.write_bytes(b"placeholder")

        first = open_project_database(db_path, create=True)
        batch_id = batch_store.create_batch(
            first, [scan], identity=batch_store.BatchIdentity.of(template)
        )
        scan_id = batch_store.scan_ids_by_path(first, batch_id)[scan]
        review_store.sync_conflicts(
            first,
            batch_id=batch_id,
            scan_id=scan_id,
            result=result_with_double_mark(scan),
            template=template,
        )
        conflict_id = review_store.list_conflicts(first, batch_id)[0].conflict_id
        review_store.correct_value(
            first,
            conflict_id,
            value="B",
            reviewer=REVIEWER,
            reason=ReasonCode.DOMINANT_MARK,
            reason_text="Bubble B visibly darker",
        )
        first.close()

        second = open_project_database(db_path)
        try:
            record = review_store.get_conflict(second, conflict_id)
            assert record.state is ConflictState.RESOLVED
            assert record.observation.value == "B-D"

            found = review_store.provenance_for(second, conflict_id)
            assert found.value == "B"
            assert found.reviewer == REVIEWER
            assert found.reason_text == "Bubble B visibly darker"
            assert found.machine_value == "B-D"

            history = review_store.history_for(second, conflict_id)
            assert [item.action for item in history] == [
                ReviewAction.DETECTED,
                ReviewAction.CORRECTED,
            ]
        finally:
            second.close()

    def test_candidate_evidence_round_trips(self, database, template, batch):
        from omr_scanner.services.recognition_models import BubbleView

        batch_id, ids = batch
        bubbles = tuple(
            BubbleView(
                zone_id="questions_0",
                row=0,
                column=index,
                label=label,
                x=10.0,
                y=10.0,
                width=20.0,
                height=20.0,
                fill_ratio=fill,
                selected=fill > 0.5,
                leading=index == 1,
                group_status="multiple",
            )
            for index, (label, fill) in enumerate(
                [("A", 0.12), ("B", 0.71), ("C", 0.08), ("D", 0.68)]
            )
        )
        result = make_result(
            source_path=Path("sheet_0.png"),
            outcome=RecognitionOutcome.REVIEW,
            registration=RegistrationStatus.REGISTERED,
            warnings=(),
            fields=(),
            answers=(make_answer(1, "B-D", "multiple"),),
            bubbles=bubbles,
            identifier_zone_id="roll_number",
            set_code_zone_id="set_code",
        )
        review_store.sync_conflicts(
            database, batch_id=batch_id, scan_id=ids[0], result=result, template=template
        )
        record = review_store.list_conflicts(database, batch_id)[0]
        # Ranked best first, so a reviewer reads the leader and its runner-up.
        assert [item.label for item in record.observation.candidates] == ["B", "D", "A", "C"]
        assert record.observation.candidates[0].fill_ratio == pytest.approx(0.71)
        assert record.observation.runner_up.label == "D"

    def test_a_legacy_result_without_evidence_is_handled_not_crashed(
        self, database, conflict
    ):
        # A batch processed before Phase 6, or with per-bubble evidence
        # switched off (the normal batch setting), has no candidates. The UI
        # says so rather than failing.
        record = review_store.get_conflict(database, conflict)
        assert record.observation.has_evidence is False
        assert record.observation.runner_up is None
