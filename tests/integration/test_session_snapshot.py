"""The session snapshot (0.1.1 revised phase 7, ARCHITECTURE_NOTES §14.1-14.2).

Every snapshot taken here is checked for the partition (the buckets add up to
what was discovered, counted independently), agrees with the canonical
effective-set service and the Resolve stage's own conflict count, and is
bounded (the number of SQL statements does not grow with the session) and
read-only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from tests.engine_rig import EngineRig, readable_sheets, sheet_bytes
from tests.quality_rig import blank_page, displaced_id, rescan_of, roll_of

from omr_scanner.database.models import ReviewConflict
from omr_scanner.domain.intake import StabilityPolicy
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.domain.review import ConflictType, ReasonCode
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.session_population import SheetDisposition
from omr_scanner.domain.session_snapshot import (
    RegistrationAlarmPolicy,
    SessionActivity,
    SessionSnapshot,
)
from omr_scanner.services import (
    intake as intake_service,
)
from omr_scanner.services import (
    quality_decisions,
    review_store,
    scan_lifecycle,
    session_controls,
    session_population,
)
from omr_scanner.services.session_snapshot import take_snapshot

SHEETS = readable_sheets(48)


@pytest.fixture
def rig(project_session):
    return EngineRig(project_session)


def snap(rig: EngineRig, **options: object) -> SessionSnapshot:
    """Take a snapshot and check what every snapshot must satisfy."""
    snapshot = take_snapshot(
        rig.database, rig.session_id, now=options.pop("now", rig.clock()), **options  # type: ignore[arg-type]
    )
    assert snapshot.partitions, snapshot.partition.as_dict()
    assert snapshot.partition.total == snapshot.discovered_excluding_ignored
    population = session_population.session_population(rig.database, rig.session_id)
    counts = population.counts()
    part = snapshot.partition
    registered = part.total - (
        part.stabilizing + part.ready + part.held + part.vanished
        + part.unreadable_pending_decision
    )
    unregistered_duplicates = registered - len(population.dispositions)
    assert unregistered_duplicates >= 0
    assert part.queued + part.processing == counts.get(SheetDisposition.NOT_READ, 0)
    effective = len(population.effective)
    assert part.accepted + part.conflict + (
        part.rescan_required - counts.get(SheetDisposition.REJECTED_PENDING_RESCAN, 0)
    ) == effective
    assert part.superseded == counts.get(SheetDisposition.SUPERSEDED_BY_REPLACEMENT, 0) + (
        counts.get(SheetDisposition.BATCH_SUPERSEDED, 0)
    )
    assert part.excluded == counts.get(SheetDisposition.EXCLUDED, 0)
    assert part.deferred == counts.get(SheetDisposition.DEFERRED, 0)
    if population.batch_ids:
        resolve = review_store.count_conflicts(
            rig.database, population.batch_ids[0], session_wide=True
        )
        assert snapshot.conflicts.done == resolve.resolved
        assert snapshot.conflicts.total == resolve.resolved + resolve.open_count + resolve.deferred
    assert snapshot.outstanding_suggestions == quality_decisions.count_outstanding(
        rig.database, rig.session_id
    )
    return snapshot


def engine(rig: EngineRig, **options: object):  # type: ignore[no-untyped-def]
    if rig.engine is None:
        rig.new_engine(**options)  # type: ignore[arg-type]
    return rig.engine


class TestPartition:
    def test_every_stage_of_a_file_is_counted_once(self, rig):
        assert snap(rig).partition.total == 0
        rig.source("a")
        rig.source("b")
        rig.write("a", [(f"a{i}.png", SHEETS[i]) for i in range(6)])
        rig.write("b", [("blank.png", blank_page()), ("moved.png", displaced_id(9)),
                        ("copy.png", SHEETS[0]), ("broken.png", sheet_bytes(36)[30])])
        live = engine(rig, limits=EngineLimits(max_in_flight=2, claim_window=2),
                      unit_policy=UnitPolicy(max_unit_size=50, trickle_seconds=0))
        live.poll_intake(force=True)
        first = snap(rig)
        assert first.partition.stabilizing == 10
        assert first.activity is SessionActivity.PROCESSING
        rig.clock.advance(5.1)
        live.poll_intake(force=True)
        second = snap(rig)
        assert second.partition.ready == 9  # the malformed file is retried, not ready
        assert second.partition.stabilizing == 1
        live.form_units()
        live.form_units()  # one unit per source and pass
        third = snap(rig)
        # Registered: the byte-identical copy is linked as a duplicate, unread.
        assert third.partition.queued == 8 and third.partition.duplicate == 1
        live.step()
        assert snap(rig).partition.processing == 2
        rig.run()
        for _ in range(4):  # let the malformed file exhaust its decode attempts
            rig.clock.advance(60)
            live.poll_intake(force=True)
        done = snap(rig)
        assert done.partition.unreadable_pending_decision == 1
        assert done.partition.rescan_required == 2  # blank + displaced: suggested
        assert done.partition.queued == done.partition.processing == 0
        assert done.recognition.done == 8
        assert done.recognition.total == done.discovered_excluding_ignored - 1  # minus duplicate
        assert done.pending_decisions == 1

        # Operator decisions move sheets between buckets, never out of the total.
        total = done.partition.total
        blank = next(
            item.scan_id
            for item in quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
            if item.suggested_reason is RejectionReason.REGISTRATION
        )
        quality_decisions.confirm_suggestion(rig.database, blank, reviewer="op")
        after_reject = snap(rig)
        assert after_reject.partition.total == total
        assert after_reject.partition.rescan_required == 2
        assert (after_reject.rescans.done, after_reject.rescans.total) == (0, 2)
        some = next(iter(session_population.session_population(
            rig.database, rig.session_id).effective))
        scan_lifecycle.exclude_scan(
            rig.database, some, reviewer="op", reason=RejectionReason.ACCIDENTAL_SCAN
        )
        assert snap(rig).partition.excluded == 1
        assert snap(rig).partition.total == total

    def test_vanished_and_held_files_are_their_own_buckets(self, rig):
        a = rig.source("a")
        rig.write("a", [("gone.png", SHEETS[0]), ("kept.png", SHEETS[1])])
        live = engine(rig)
        live.poll_intake(force=True)
        rig.fs.delete(rig.root("a"), "gone.png")
        live.poll_intake(force=True)
        snapshot = snap(rig)
        assert snapshot.partition.vanished == 1
        assert snapshot.recognition.total == snapshot.discovered_excluding_ignored - 1
        assert intake_service.get_source(rig.database, a) is not None


class TestProgressLines:
    def test_recognition_progress_falls_when_files_arrive(self, rig):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(4)])
        engine(rig)
        rig.make_ready()
        rig.run()
        before = snap(rig).recognition
        assert before.fraction == 1.0
        rig.write("a", [(f"late{i}.png", SHEETS[10 + i]) for i in range(4)])
        rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
        after = snap(rig).recognition
        assert after.done == before.done and after.total > before.total
        assert after.fraction is not None and after.fraction < 1.0

    def test_conflict_and_rescan_lines_are_separate(self, rig):
        rig.source("a")
        rig.write("a", [("17.png", SHEETS[17]), ("18.png", SHEETS[18]),
                        ("moved.png", displaced_id(11))])
        engine(rig)
        rig.make_ready()
        rig.run()
        snapshot = snap(rig)
        assert snapshot.conflicts.total >= 2 and snapshot.conflicts.done == 0
        assert snapshot.rescans.total == 1 and snapshot.rescans.done == 0
        with rig.database.session() as session:
            duplicate = session.scalars(
                select(ReviewConflict.conflict_id).where(
                    ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value
                )
            ).first()
        review_store.accept_machine_value(
            rig.database, duplicate, reviewer="op", reason=ReasonCode.MACHINE_CONFIRMED
        )
        later = snap(rig)
        assert later.conflicts.done == 1
        assert later.rescans == snapshot.rescans  # untouched by a conflict decision
        moved = next(iter(
            quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
        )).scan_id
        quality_decisions.confirm_suggestion(
            rig.database, moved, reviewer="op", declared_candidate_id=roll_of(SHEETS[11])
        )
        rig.write("a", [("again.png", rescan_of(11))])
        rig.make_ready()
        rig.run()
        replacement = scan_lifecycle.session_possible_rescans(
            rig.database, rig.session_id
        )[moved][0].scan_id
        scan_lifecycle.confirm_replacement(rig.database, moved, replacement, reviewer="op")
        final = snap(rig)
        assert (final.rescans.done, final.rescans.total) == (1, 1)


class TestCaughtUp:
    def test_caught_up_needs_reachable_recently_checked_sources(self, rig):
        a = rig.source("a")
        b = rig.source("b")
        rig.write("a", [("1.png", SHEETS[1])])
        engine(rig)
        rig.make_ready()
        rig.run()
        rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
        snapshot = snap(rig)
        assert snapshot.activity is SessionActivity.CAUGHT_UP and snapshot.caught_up
        assert snapshot.session_state == "open"  # caught up is not complete
        # Time passes without a reconciliation: no longer known to be caught up.
        rig.clock.advance(3600)
        assert snap(rig).activity is SessionActivity.CHECKING_SOURCES
        # Scanner B's share disappears.
        rig.fs.unreachable.add(rig.root("b"))
        rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
        waiting = snap(rig)
        assert waiting.activity is SessionActivity.WAITING_FOR_SOURCE and not waiting.caught_up
        assert waiting.unreachable_sources == ("Scanner b",)
        # Disabled, it is named but no longer blocks.
        intake_service.set_source_enabled(rig.database, b, False, actor="op")
        rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
        disabled = snap(rig)
        assert disabled.disabled_sources == ("Scanner b",)
        assert disabled.caught_up
        assert a in {item.source_id for item in disabled.sources}

    def test_paused_processing_or_intake_is_never_caught_up(self, rig):
        rig.source("a")
        engine(rig)
        rig.engine.poll_intake(force=True)  # type: ignore[union-attr]
        assert snap(rig).caught_up
        session_controls.pause_processing(rig.database, rig.session_id)
        assert snap(rig).activity is SessionActivity.PROCESSING_PAUSED
        session_controls.resume_processing(rig.database, rig.session_id)
        session_controls.set_intake_paused(rig.database, rig.session_id, True)
        assert snap(rig).activity is SessionActivity.INTAKE_PAUSED


class TestPerSourceAndAlarm:
    def test_per_source_counts_rate_and_registration_alarm(self, rig):
        a = rig.source("a")
        b = rig.source("b")
        rig.write("a", [(f"blank{i}.png", blank_page(i)) for i in range(6)])
        rig.write("b", [(f"{i}.png", SHEETS[i]) for i in range(5)] + [("blank.png", blank_page(9))])
        engine(rig)
        rig.make_ready()
        rig.run()
        snapshot = snap(rig, now=datetime.now(UTC))
        by_id = {item.source_id: item for item in snapshot.sources}
        assert by_id[a].registered == by_id[a].processed == 6
        assert by_id[b].registered == by_id[b].processed == 6
        assert by_id[a].rate_per_minute is not None and by_id[a].recent_processed == 6
        # Every sheet of Scanner A failed registration: a wrong template, not bad paper.
        assert by_id[a].alarm is not None and by_id[a].alarm.raised
        assert by_id[a].alarm.failures == by_id[a].alarm.samples == 6
        # One failure among six is a sheet, not a systematic cause: no alarm.
        assert by_id[b].alarm is not None and not by_id[b].alarm.raised
        # The window and threshold are configuration.
        strict = snap(rig, now=datetime.now(UTC),
                      alarm_policy=RegistrationAlarmPolicy(window=6, min_samples=6,
                                                           alarm_fraction=0.1))
        assert {item.source_id: item.alarm.raised for item in strict.sources} == {  # type: ignore[union-attr]
            a: True, b: True,
        }

    def test_too_few_samples_never_alarm(self):
        policy = RegistrationAlarmPolicy()
        assert not policy.evaluate(samples=1, failures=1).raised
        assert not policy.evaluate(samples=4, failures=4).raised
        assert policy.evaluate(samples=5, failures=3).raised
        with pytest.raises(ValueError):
            RegistrationAlarmPolicy(window=3, min_samples=5)


class TestBoundedAndReadOnly:
    def test_statement_count_does_not_grow_with_the_session(self, rig):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(4)])
        engine(rig, unit_policy=UnitPolicy(max_unit_size=2, trickle_seconds=0))
        rig.make_ready()
        rig.run()
        small = snap(rig).query_count
        rig.write("a", [(f"more{i}.png", SHEETS[10 + i]) for i in range(16)])
        rig.make_ready()
        rig.run()
        large = snap(rig)
        assert large.partition.total == 20
        assert large.query_count == small, "the snapshot must be a fixed set of statements"

    def test_a_snapshot_writes_nothing(self, rig):
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(3)])
        engine(rig)
        rig.make_ready()
        rig.run()
        statements: list[str] = []

        def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
            statements.append(statement.split(None, 1)[0].upper())

        event.listen(rig.database.engine, "before_cursor_execute", record)
        try:
            snap(rig)
        finally:
            event.remove(rig.database.engine, "before_cursor_execute", record)
        assert statements and not {"INSERT", "UPDATE", "DELETE"} & set(statements)


def test_policy_objects_are_named_heuristics():
    from omr_scanner.domain import session_snapshot

    for policy in (session_snapshot.RegistrationAlarmPolicy, session_snapshot.CaughtUpPolicy):
        assert "not calibrated" in " ".join(str(policy.__doc__).split())
    assert StabilityPolicy  # the stabilisation thresholds stay intake's (unchanged)
