"""Crash and restart of the continuous engine, boundary by boundary (revised phase 6).

Each case abandons a running engine at an exact durable boundary (a hook
raises :class:`tests.engine_rig.Killed`: open transactions roll back, nothing
in memory survives), builds a new engine on the same database - what a
restarted process does - and lets it finish. The outcome is compared with an
**uninterrupted control run** of the same files in another project, by sheet
content: results, effective set, conflicts, unread duplicates. Recognition is
counted per sheet: a sheet committed before the kill must have been read
exactly once over the whole run.

Real process kills of the same boundaries are in ``tests/crash/test_engine_kills.py``.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterator, Sequence

import pytest
from sqlalchemy import func, select
from tests.crash.harness import integrity
from tests.engine_rig import (
    EngineRig,
    Killed,
    KillAt,
    committed_contents,
    digest,
    durable_view,
    readable_sheets,
    structure,
)

from omr_scanner.database.models import (
    BatchScan,
    BatchSupersession,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
)
from omr_scanner.domain.processing import EngineLimits, EngineState
from omr_scanner.domain.review import ConflictState, ConflictType
from omr_scanner.services import create_project, review_store, scan_sessions
from omr_scanner.services.continuous_engine import Claim, EngineHooks


@pytest.fixture
def rigs(workspace) -> Iterator[list[EngineRig]]:
    """``rigs`` factory list: each call of ``make(rigs, name)`` adds a fresh project."""
    made: list[EngineRig] = []
    yield made
    for rig in made:
        rig.project.close()


def make(rigs: list[EngineRig], workspace, name: str) -> EngineRig:
    rig = EngineRig(create_project(workspace, name))
    rigs.append(rig)
    return rig


def load(rig: EngineRig, sheets: Sequence[bytes], sources: str = "ab") -> None:
    """Spread ``sheets`` over the named sources, round robin, in file order."""
    for name in sources:
        rig.source(name)
    for index, data in enumerate(sheets):
        name = sources[index % len(sources)]
        rig.write(name, [(f"{index:06d}.png", data)])


def finish(rig: EngineRig, **options: object) -> None:
    """Start an engine, let intake settle, run until caught up, stop cleanly."""
    engine = rig.new_engine(**options)  # type: ignore[arg-type]
    rig.make_ready()
    rig.run()
    engine.shutdown()


def control(rigs: list[EngineRig], workspace, sheets: Sequence[bytes], sources: str = "ab"):
    rig = make(rigs, workspace, "Control")
    load(rig, sheets, sources)
    finish(rig)
    return durable_view(rig.database, rig.session_id)


Kill = tuple[set[str], Counter[str]]
"""What was committed at a kill, and how often each sheet had been read by then."""


def at_kill(rig: EngineRig) -> Kill:
    return committed_contents(rig.database), Counter(rig.recognise.calls)


def assert_recovered(rig: EngineRig, expected: dict, kills: Sequence[Kill] = ()) -> None:
    """The run converged on the control, and no sheet committed before a kill was read after it."""
    view = durable_view(rig.database, rig.session_id)
    assert view == expected
    for committed, calls_then in kills:
        for key in committed:
            assert rig.recognise.calls[key] == calls_then[key], (
                "a sheet committed before a kill was recognised again after it"
            )
    if len(kills) == 1:
        # One interruption: everything committed before it was read exactly once.
        for key in kills[0][0]:
            assert rig.recognise.calls[key] == 1
    with rig.database.session() as session:
        assert not session.scalar(
            select(func.count()).select_from(BatchScan).where(
                BatchScan.status.in_([ScanJobStatus.PROCESSING.value, ScanJobStatus.QUEUED.value])
            )
        )
        assert not session.scalar(select(func.count()).select_from(BatchSupersession))
        sessions = set(session.scalars(select(ScanBatch.scan_session_id)).all())
    assert sessions == {rig.session_id}
    checks = integrity(rig.project.root)
    assert checks.sqlite_ok
    assert checks.health_errors == []


SHEETS = 40
BOUNDARIES = [
    ("registered", 2),
    ("claimed", 3),
    ("submitted", 7),
    ("recognised", 9),
    ("before_commit", 12),
    ("committed", 12),
    ("finalising", 2),
    ("finalised", 2),
]


class TestKillAtEachBoundary:
    @pytest.mark.parametrize(("boundary", "count"), BOUNDARIES)
    def test_restart_converges_on_the_uninterrupted_run(self, rigs, workspace, boundary, count):
        sheets = readable_sheets(48)[:SHEETS]
        expected = control(rigs, workspace, sheets)
        rig = make(rigs, workspace, "Killed")
        load(rig, sheets)
        rig.new_engine(hooks=KillAt(boundary, count))
        rig.make_ready()
        assert rig.run_until_killed() == boundary
        before = structure(rig.database, rig.session_id)
        kill = at_kill(rig)

        rig.new_engine()
        # Recovery itself: no row left claimed, nothing created.
        with rig.database.session() as session:
            assert not session.scalar(
                select(func.count()).select_from(BatchScan).where(
                    BatchScan.status == ScanJobStatus.PROCESSING.value
                )
            )
        after_recovery = structure(rig.database, rig.session_id)
        assert after_recovery["batches"] == before["batches"]
        assert after_recovery["members"] == before["members"]
        rig.make_ready()
        rig.run()
        rig.engine.shutdown()  # type: ignore[union-attr]
        assert_recovered(rig, expected, [kill])
        # Units that existed at the kill kept their identity and membership.
        final = structure(rig.database, rig.session_id)
        assert final["batches"][: len(before["batches"])] == [
            row for row in final["batches"] if row[0] in {b[0] for b in before["batches"]}
        ]
        old_members = {row for row in before["members"]}
        assert old_members <= set(final["members"])


class TestMandatoryRegressions:
    def test_commit_then_death_before_acknowledgement(self, rigs, workspace):
        """§24: the work unit committed, the process died before it was acknowledged."""
        sheets = readable_sheets(12)[:8]
        rig = make(rigs, workspace, "Ack")
        load(rig, sheets, "a")
        hooks = KillAt("committed", 3)
        rig.new_engine(hooks=hooks)
        rig.make_ready()
        rig.run_until_killed()
        acknowledged_lost = hooks.committed_ids[-1]
        with rig.database.session() as session:
            row = session.get(BatchScan, acknowledged_lost)
            assert row is not None
            assert row.status in (ScanJobStatus.COMPLETED.value, ScanJobStatus.WARNING.value,
                                  ScanJobStatus.FAILED.value)
            content = row.content_sha256
            scans_before = session.scalar(select(func.count()).select_from(BatchScan))
            detected_before = session.scalar(
                select(func.count()).select_from(ReviewConflict).where(
                    ReviewConflict.scan_id == acknowledged_lost
                )
            )
        rig.new_engine()
        rig.make_ready()
        rig.run()
        with rig.database.session() as session:
            row = session.get(BatchScan, acknowledged_lost)
            assert row is not None and row.attempt_count == 1
            assert session.scalar(
                select(func.count()).select_from(BatchScan).where(BatchScan.content_sha256 == content)
            ) == 1
            assert session.scalar(select(func.count()).select_from(BatchScan)) == scans_before
            assert session.scalar(
                select(func.count()).select_from(ReviewConflict).where(
                    ReviewConflict.scan_id == acknowledged_lost
                )
            ) == detected_before
        assert rig.recognise.calls[content] == 1
        # No completion is recorded twice in the audit ledger either.
        checks = integrity(rig.project.root)
        assert "CONFLICT_DETECTED_TWICE" not in checks.health_codes

    def test_claimed_but_never_committed(self, rigs, workspace):
        """§25: claimed (durably), died before the result committed."""
        sheets = readable_sheets(12)[:8]
        rig = make(rigs, workspace, "Claim")
        load(rig, sheets, "a")
        hooks = KillAt("recognised", 2)
        rig.new_engine(hooks=hooks)
        rig.make_ready()
        rig.run_until_killed()
        with rig.database.session() as session:
            claimed = session.execute(
                select(BatchScan.scan_id, BatchScan.batch_id, BatchScan.intake_file_id)
                .where(BatchScan.status == ScanJobStatus.PROCESSING.value)
            ).all()
            batch_ids = set(session.scalars(select(ScanBatch.batch_id)).all())
        assert claimed
        rig.new_engine()
        with rig.database.session() as session:
            for scan_id, batch_id, intake_id in claimed:
                row = session.get(BatchScan, scan_id)
                assert row is not None
                assert row.status == ScanJobStatus.PENDING.value
                assert (row.batch_id, row.intake_file_id) == (batch_id, intake_id)
        rig.make_ready()
        rig.run()
        with rig.database.session() as session:
            assert set(session.scalars(select(ScanBatch.batch_id)).all()) == batch_ids
            for scan_id, _batch, _intake in claimed:
                row = session.get(BatchScan, scan_id)
                assert row is not None
                assert row.status != ScanJobStatus.PENDING.value
                assert row.attempt_count == 1
            counts = session.execute(
                select(BatchScan.content_sha256, func.count()).group_by(BatchScan.content_sha256)
            ).all()
        assert all(count == 1 for _sha, count in counts)

    def test_committed_sheets_are_never_resubmitted(self, rigs, workspace):
        """§63: recognition invocations, counted across a kill and a restart."""
        sheets = readable_sheets(30)[:24]
        rig = make(rigs, workspace, "Count")
        load(rig, sheets, "ab")
        rig.new_engine(hooks=KillAt("committed", 9))
        rig.make_ready()
        rig.run_until_killed()
        committed_before = committed_contents(rig.database)
        assert len(committed_before) >= 9
        rig.new_engine()
        rig.make_ready()
        rig.run()
        for key in committed_before:
            assert rig.recognise.calls[key] == 1
        reread = {key for key, calls in rig.recognise.calls.items() if calls > 1}
        assert not (reread & committed_before)
        # Only genuinely uncommitted work (in flight at the kill) was read again.
        assert len(reread) <= EngineLimits(max_in_flight=4).max_in_flight

    def test_a_crash_creates_no_new_batch(self, rigs, workspace):
        """§64: same session, same batch, same membership; no recovery batch."""
        sheets = readable_sheets(14)[:10]
        rig = make(rigs, workspace, "Same")
        load(rig, sheets, "a")
        rig.new_engine(hooks=KillAt("committed", 3))
        rig.make_ready()
        rig.run_until_killed()
        before = scan_sessions.batches_of(rig.database, rig.session_id)
        assert len(before) == 1
        unit = before[0]
        with rig.database.session() as session:
            members = sorted(session.scalars(
                select(BatchScan.scan_id).where(BatchScan.batch_id == unit.batch_id)
            ).all())
        rig.new_engine()
        rig.make_ready()
        rig.run()
        after = scan_sessions.batches_of(rig.database, rig.session_id)
        assert [item.batch_id for item in after] == [unit.batch_id]
        assert after[0].sealed_at == unit.sealed_at
        assert after[0].superseded_by is None
        assert after[0].role == unit.role
        with rig.database.session() as session:
            assert sorted(session.scalars(
                select(BatchScan.scan_id).where(BatchScan.batch_id == unit.batch_id)
            ).all()) == members
            assert not session.scalar(select(func.count()).select_from(BatchSupersession))
        assert scan_sessions.list_scan_sessions(rig.database)[0].scan_session_id == rig.session_id


class _KillAfterTotal(EngineHooks):
    """Kill once ``target`` sheets in total (across restarts) are committed."""

    def __init__(self, already: int, target: int) -> None:
        self.total = already
        self.target = target

    def committed(self, batch_id: str, claims: Sequence[Claim]) -> None:
        self.total += len(claims)
        if self.total >= self.target:
            raise Killed("committed")


PERCENT_SHEETS = 100


class TestRestartAtProgressPoints:
    @pytest.fixture
    def dataset(self):
        return readable_sheets(120)[:PERCENT_SHEETS]

    @pytest.mark.parametrize("percent", [1, 25, 50, 75, 99])
    def test_single_interruption(self, rigs, workspace, dataset, percent):
        expected = control(rigs, workspace, dataset, "abc")
        rig = make(rigs, workspace, f"P{percent}")
        load(rig, dataset, "abc")
        target = max(1, round(len(expected["results"]) * percent / 100))
        rig.new_engine(hooks=_KillAfterTotal(0, target))
        rig.make_ready()
        rig.run_until_killed()
        kill = at_kill(rig)
        assert len(kill[0]) >= target
        finish(rig)
        assert_recovered(rig, expected, [kill])

    def test_series_at_1_25_50_75_99_percent_in_one_project(self, rigs, workspace, dataset):
        expected = control(rigs, workspace, dataset, "abc")
        rig = make(rigs, workspace, "Series")
        load(rig, dataset, "abc")
        total = len(expected["results"])
        kills: list[Kill] = []
        for percent in (1, 25, 50, 75, 99):
            target = max(1, round(total * percent / 100))
            already = len(committed_contents(rig.database))
            if already >= target:
                continue
            rig.new_engine(hooks=_KillAfterTotal(already, target))
            rig.make_ready()
            rig.run_until_killed()
            kills.append(at_kill(rig))
            assert len(kills[-1][0]) >= target
        assert len(kills) == 5
        finish(rig)
        assert_recovered(rig, expected, kills)


class TestRepeatedRestarts:
    def test_seeded_torture_converges(self, rigs, workspace):
        """§28: kill, resume, kill, resume ... at seeded boundaries; one final state."""
        sheets = readable_sheets(70)[:60]
        expected = control(rigs, workspace, sheets, "abc")
        rig = make(rigs, workspace, "Torture")
        load(rig, sheets, "abc")
        chooser = random.Random(6)
        # Boundaries that recur while work remains (registration is in the
        # matrix above: once every unit exists it can no longer be reached).
        names = ["claimed", "submitted", "recognised", "before_commit", "committed",
                 "finalising"]
        kills: list[Kill] = []
        for _cycle in range(8):
            boundary = chooser.choice(names)
            count = 1 if boundary == "finalising" else chooser.randint(1, 3)
            rig.new_engine(hooks=KillAt(boundary, count))
            rig.make_ready()
            try:
                rig.run()
            except Killed:
                kills.append(at_kill(rig))
                rig.engine = None
            # Every restart's recovery is idempotent: running it twice changes nothing.
            if rig.engine is None:
                rig.new_engine()
                snapshot = structure(rig.database, rig.session_id)
                rig.engine = None
                rig.new_engine()
                assert structure(rig.database, rig.session_id) == snapshot
                rig.engine = None
        assert len(kills) >= 4
        finish(rig)
        assert_recovered(rig, expected, kills)


class TestWriterFailure:
    def test_failure_inside_the_work_unit_rolls_back_result_and_conflicts(
        self, rigs, workspace, monkeypatch
    ):
        """§12 / §45: the result row and its conflicts commit together or not at all."""
        sheets = readable_sheets(12)[:8]
        rig = make(rigs, workspace, "Writer")
        load(rig, sheets, "a")
        engine = rig.new_engine(limits=EngineLimits(max_in_flight=1, claim_window=1))
        rig.make_ready()
        engine.form_units()
        original = review_store.sync_conflicts_in_session
        failures = {"left": 2}

        def failing(session, **kwargs):  # type: ignore[no-untyped-def]
            # Fail *after* the result row was flushed in the same transaction.
            if failures["left"]:
                failures["left"] -= 1
                raise RuntimeError("disk full (injected)")
            return original(session, **kwargs)

        monkeypatch.setattr(review_store, "sync_conflicts_in_session", failing)
        engine.step()  # claim + submit
        engine.step()  # read; the commit fails (1)
        status = engine.status()
        assert status.writer_backlog == 1
        assert status.completed == 0 and status.failed == 0
        assert "disk full" in status.last_error
        with rig.database.session() as session:
            row = session.scalars(
                select(BatchScan).where(BatchScan.status == ScanJobStatus.PROCESSING.value)
            ).one()
            assert row.result_json == "" and row.attempt_count == 0
            assert not session.scalar(
                select(func.count()).select_from(ReviewConflict).where(
                    ReviewConflict.scan_id == row.scan_id
                )
            )
        engine.step()  # commit fails again (2)
        engine.step()  # succeeds
        assert engine.status().writer_backlog == 0
        rig.run()
        with rig.database.session() as session:
            attempts = session.scalars(select(BatchScan.attempt_count)).all()
        assert set(attempts) == {1}
        assert integrity(rig.project.root).health_errors == []

    def test_a_writer_that_keeps_failing_faults_and_a_restart_recovers(
        self, rigs, workspace, monkeypatch
    ):
        sheets = readable_sheets(12)[:6]
        expected = control(rigs, workspace, sheets, "a")
        rig = make(rigs, workspace, "Faulted")
        load(rig, sheets, "a")
        engine = rig.new_engine()
        rig.make_ready()
        engine.form_units()
        from omr_scanner.services import batch_store

        original = batch_store.record_results

        def broken(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("database is locked (injected)")

        monkeypatch.setattr(batch_store, "record_results", broken)
        for _ in range(20):
            engine.step()
            if engine.state is EngineState.FAULTED:
                break
        assert engine.state is EngineState.FAULTED
        assert engine.status().completed == 0
        engine.shutdown()  # leaves the claims for recovery
        monkeypatch.setattr(batch_store, "record_results", original)
        rig.engine = None
        finish(rig)
        assert_recovered(rig, expected)


class TestResolveAcrossRestarts:
    def test_operator_work_survives_and_late_conflicts_still_appear(self, rigs, workspace):
        """§15 / §38: resolved stays resolved, unresolved stays, new ones are raised."""
        from omr_scanner.domain.review import ReasonCode

        sheets = readable_sheets(48)[:40]
        rig = make(rigs, workspace, "Resolve")
        load(rig, sheets[:20], "a")
        finish(rig)
        with rig.database.session() as session:
            conflicts = session.scalars(
                select(ReviewConflict).order_by(ReviewConflict.conflict_id)
            ).all()
            open_ids = [row.conflict_id for row in conflicts if row.state == ConflictState.OPEN.value]
        assert len(open_ids) >= 2
        decided = open_ids[0]
        review_store.accept_machine_value(
            rig.database, decided, reviewer="Reviewer", reason=ReasonCode.MACHINE_CONFIRMED
        )
        untouched = open_ids[1]

        # More sheets arrive (a later unit), and the engine is killed mid-unit.
        rig.source("b")
        for index, data in enumerate(sheets[20:], start=20):
            rig.write("b", [(f"{index:06d}.png", data)])
        rig.new_engine(hooks=KillAt("committed", 4))
        rig.make_ready()
        rig.run_until_killed()
        finish(rig)

        with rig.database.session() as session:
            kept = session.get(ReviewConflict, decided)
            still = session.get(ReviewConflict, untouched)
            assert kept is not None and kept.state == ConflictState.RESOLVED.value
            assert still is not None and still.state in (
                ConflictState.OPEN.value, ConflictState.WITHDRAWN.value
            )
            events = review_store.history_for(rig.database, decided)
        assert [item.action for item in events].count("accepted") == 1
        expected = control(rigs, workspace, sheets, "ab")
        view = durable_view(rig.database, rig.session_id)
        # Everything but the one human decision equals the uninterrupted run.
        assert view["results"] == expected["results"]
        assert view["effective"] == expected["effective"]
        def machine(conflicts):  # type: ignore[no-untyped-def]
            return sorted((c[0], c[1], c[2], c[3], c[5]) for c in conflicts)
        assert machine(view["conflicts"]) == machine(expected["conflicts"])

    def test_late_unit_raises_a_session_duplicate_and_a_resolved_one_is_not_resurrected(
        self, rigs, workspace
    ):
        """§14 / §16: duplicate Student IDs across finite units of one session."""
        from omr_scanner.domain.review import ReasonCode

        sheets = readable_sheets(48)[:40]
        expected = control(rigs, workspace, sheets, "ab")
        duplicate_scans = [
            item for item in expected["conflicts"]
            if item[1] == ConflictType.IDENTIFIER_DUPLICATE.value
        ]
        if not duplicate_scans:
            pytest.skip("dataset produced no duplicate Student ID")
        rig = make(rigs, workspace, "Dup")
        load(rig, sheets, "ab")
        finish(rig)
        assert durable_view(rig.database, rig.session_id)["conflicts"] == expected["conflicts"]
        with rig.database.session() as session:
            dup = session.scalars(
                select(ReviewConflict).where(
                    ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value
                )
            ).first()
            assert dup is not None
            dup_id = dup.conflict_id
        review_store.accept_machine_value(
            rig.database, dup_id, reviewer="Reviewer", reason=ReasonCode.MACHINE_CONFIRMED
        )
        # Two restarts with nothing new: recovery and the unit passes keep the decision.
        finish(rig)
        finish(rig)
        with rig.database.session() as session:
            row = session.get(ReviewConflict, dup_id)
            assert row is not None and row.state == ConflictState.RESOLVED.value
        assert [item.action for item in review_store.history_for(rig.database, dup_id)].count(
            "detected"
        ) == 1


def test_duplicate_content_is_never_read(rigs, workspace):
    """§51: a file diverted as duplicate content at intake is never claimed."""
    sheets = readable_sheets(12)[:4]
    rig = make(rigs, workspace, "Bytes")
    rig.source("a")
    rig.source("b")
    rig.write("a", [("000001.png", sheets[0]), ("000002.png", sheets[1])])
    rig.write("b", [("000001.png", sheets[0]), ("000009.png", sheets[2])])
    finish(rig)
    assert rig.recognise.calls[digest(sheets[0])] == 1
    view = durable_view(rig.database, rig.session_id)
    assert digest(sheets[0]) in view["unread_duplicates"]
    assert view["effective"].count(digest(sheets[0])) == 1
