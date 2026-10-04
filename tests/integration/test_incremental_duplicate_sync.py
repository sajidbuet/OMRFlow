"""Cross-sheet duplicate Student IDs reach Resolve within one commit (0.1.1 revised phase 7).

Revised phase 6 re-derived session-wide duplicate groups only when a whole
unit (default 200 sheets) finished. Now each commit group's groups are
re-derived right after its commit - bounded to the identifiers those sheets
hold - through the one duplicate system
(:func:`~omr_scanner.services.review_store.sync_duplicate_identifiers_for`).
The gap between the commit and that pass is recoverable: the unit stays
``running`` until its batch-scope pass, so recovery completes it from stored
results, without reading the sheet again. Human decisions are never undone.

Sheets 17 and 18 of the stress dataset (seed 42) are two different images that
carry the same Student ID.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from sqlalchemy import select
from tests.engine_rig import EngineRig, Killed, digest, readable_sheets
from tests.quality_rig import rescan_of

from omr_scanner.database.models import BatchScan, BatchStatus, ReviewConflict, ScanBatch
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.domain.review import ConflictState, ConflictType, ReasonCode
from omr_scanner.domain.session_snapshot import SessionActivity
from omr_scanner.services import review_store, session_snapshot
from omr_scanner.services.continuous_engine import Claim, EngineHooks

SHEETS = readable_sheets(48)
FIRST, SECOND = 17, 18
LIMITS = EngineLimits(max_in_flight=2, claim_window=2, max_commit_group=1)
BIG_UNITS = UnitPolicy(max_unit_size=50, trickle_seconds=0)


@pytest.fixture
def rig(project_session):
    return EngineRig(project_session)


def scan_of(rig: EngineRig, data: bytes) -> int | None:
    with rig.database.session() as session:
        return session.scalar(
            select(BatchScan.scan_id).where(BatchScan.content_sha256 == digest(data))
        )


def duplicates(rig: EngineRig) -> dict[int, str]:
    """``scan id -> state`` of every duplicate-ID record."""
    with rig.database.session() as session:
        return {
            int(scan): str(state)
            for scan, state in session.execute(
                select(ReviewConflict.scan_id, ReviewConflict.state).where(
                    ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value
                )
            ).all()
        }


def status_of(rig: EngineRig, scan_id: int) -> str:
    with rig.database.session() as session:
        return str(session.get(BatchScan, scan_id).status)  # type: ignore[union-attr]


def unit_status(rig: EngineRig, scan_id: int) -> str:
    with rig.database.session() as session:
        batch = session.get(BatchScan, scan_id).batch_id  # type: ignore[union-attr]
        return str(session.get(ScanBatch, batch).status)  # type: ignore[union-attr]


def committed(rig: EngineRig, scan_id: int | None) -> bool:
    return scan_id is not None and status_of(rig, scan_id) in ("completed", "warning")


def test_a_duplicate_is_in_resolve_before_its_unit_finishes(rig):
    rig.source("a")
    rig.source("b")
    # Source a's unit reads sheet 17 and finishes.
    rig.write("a", [("first.png", SHEETS[FIRST])])
    rig.new_engine(limits=LIMITS, unit_policy=BIG_UNITS)
    rig.make_ready()
    rig.run()
    first = scan_of(rig, SHEETS[FIRST])
    assert first is not None and duplicates(rig) == {}
    # Later, source b's unit holds sheet 18 followed by more sheets.
    rig.write("b", [("a-second.png", SHEETS[SECOND])])
    rig.write("b", [(f"b-later{i}.png", SHEETS[30 + i]) for i in range(6)])
    rig.make_ready()
    engine = rig.engine
    assert engine is not None and engine.form_units()
    seen_while_running = False
    for _ in range(200):
        engine.step()
        second = scan_of(rig, SHEETS[SECOND])
        if committed(rig, second):
            assert second is not None
            assert unit_status(rig, second) == BatchStatus.RUNNING.value
            assert duplicates(rig) == {
                first: ConflictState.OPEN.value, second: ConflictState.OPEN.value
            }
            seen_while_running = True
            break
    assert seen_while_running, "the duplicate must not wait for the unit to finish"


def test_the_pass_is_bounded_to_the_committed_sheets(rig, monkeypatch):
    calls: list[list[int]] = []
    original = review_store.sync_duplicate_identifiers_for

    def recording(database: object, scan_ids: Sequence[int]) -> object:
        ids = list(scan_ids)
        calls.append(ids)
        return original(database, ids)

    monkeypatch.setattr(review_store, "sync_duplicate_identifiers_for", recording)
    rig.source("a")
    rig.write("a", [(f"{i:06d}.png", SHEETS[i]) for i in range(12)])
    rig.new_engine(limits=EngineLimits(max_in_flight=4, claim_window=4, max_commit_group=3),
                   unit_policy=BIG_UNITS)
    rig.make_ready()
    rig.run()
    per_commit = [ids for ids in calls if len(ids) <= 3]
    assert per_commit, "the engine must re-derive after each commit group"
    assert all(1 <= len(ids) <= 3 for ids in per_commit)
    # Never the whole session per commit: only the unit-end pass sees all 12.
    assert sum(1 for ids in calls if len(ids) == 12) == 1


class KillBeforeSync(EngineHooks):
    """Raise once the commit holding ``target`` is durable, before its duplicate pass."""

    def __init__(self, rig: EngineRig, data: bytes) -> None:
        self.rig = rig
        self.data = data

    def syncing_duplicates(self, batch_id: str, claims: Sequence[Claim]) -> None:
        target = scan_of(self.rig, self.data)
        if target is not None and any(claim.scan_id == target for claim in claims):
            raise Killed("syncing_duplicates")


def test_a_kill_between_commit_and_duplicate_pass_is_completed_by_recovery(rig):
    rig.source("a")
    rig.write("a", [("first.png", SHEETS[FIRST])])
    rig.new_engine(limits=LIMITS, unit_policy=BIG_UNITS)
    rig.make_ready()
    rig.run()
    first = scan_of(rig, SHEETS[FIRST])
    rig.write("a", [("second.png", SHEETS[SECOND])])
    rig.write("a", [(f"later{i}.png", SHEETS[30 + i]) for i in range(4)])
    rig.engine._hooks = KillBeforeSync(rig, SHEETS[SECOND])  # type: ignore[union-attr]
    rig.make_ready()
    with pytest.raises(Killed):
        rig.run()
    second = scan_of(rig, SHEETS[SECOND])
    assert second is not None and committed(rig, second)  # durable ...
    assert second not in duplicates(rig)  # ... its cross-sheet state not yet
    assert unit_status(rig, second) == BatchStatus.RUNNING.value  # the recorded marker
    snapshot = session_snapshot.take_snapshot(rig.database, rig.session_id, now=rig.clock())
    assert not snapshot.caught_up and snapshot.running_batches >= 1
    assert snapshot.activity is SessionActivity.PROCESSING
    reads_before = rig.recognise.calls[digest(SHEETS[SECOND])]

    rig.new_engine(limits=LIMITS, unit_policy=BIG_UNITS)  # restart: recovery first
    assert duplicates(rig) == {
        first: ConflictState.OPEN.value, second: ConflictState.OPEN.value
    }
    assert rig.recognise.calls[digest(SHEETS[SECOND])] == reads_before == 1
    rig.run()
    assert duplicates(rig) == {
        first: ConflictState.OPEN.value, second: ConflictState.OPEN.value
    }


def test_a_human_decision_survives_later_syncs_and_restarts(rig):
    rig.source("a")
    rig.source("b")
    rig.write("a", [("first.png", SHEETS[FIRST]), ("second.png", SHEETS[SECOND])])
    rig.new_engine(limits=LIMITS, unit_policy=BIG_UNITS)
    rig.make_ready()
    rig.run()
    first = scan_of(rig, SHEETS[FIRST])
    second = scan_of(rig, SHEETS[SECOND])
    with rig.database.session() as session:
        conflict = session.scalars(
            select(ReviewConflict.conflict_id)
            .where(ReviewConflict.scan_id == first)
            .where(ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value)
        ).one()
    review_store.accept_machine_value(
        rig.database, conflict, reviewer="Reviewer", reason=ReasonCode.MACHINE_CONFIRMED
    )
    # A third sheet with the same Student ID arrives from another source.
    rig.write("b", [("third.png", rescan_of(FIRST))])
    rig.write("b", [(f"later{i}.png", SHEETS[30 + i]) for i in range(3)])
    rig.make_ready()
    rig.run()
    third = scan_of(rig, rescan_of(FIRST))
    expected = {
        first: ConflictState.RESOLVED.value,
        second: ConflictState.OPEN.value,
        third: ConflictState.OPEN.value,
    }
    assert duplicates(rig) == expected
    for _ in range(2):
        rig.engine.shutdown()  # type: ignore[union-attr]
        rig.new_engine(limits=LIMITS, unit_policy=BIG_UNITS)
        rig.run()
        assert duplicates(rig) == expected
    accepted = [
        item.action for item in review_store.history_for(rig.database, conflict)
    ].count("accepted")
    assert accepted == 1
