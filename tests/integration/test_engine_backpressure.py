"""Bounds, faults and stopping of the continuous engine (revised phase 6).

Recognition here is a test double behind the engine's ``Recogniser``
protocol: it can hold sheets indefinitely (a slow or stuck worker), answer
out of order, lose a worker, or answer instantly with a cheap result - so
backpressure and shutdown are asserted from the engine's own counters and the
database, without timing. The batches are registered directly in the session
(no intake), each a finite sealed unit.
"""

from __future__ import annotations

import gc
import tracemalloc
from collections import deque
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from tests.crash.harness import integrity
from tests.crash.harness import template as harness_template
from tests.engine_rig import EngineRig, digest, durable_view, readable_sheets

from omr_scanner.database.models import BatchScan, BatchStatus, ReviewConflict, ScanJobStatus
from omr_scanner.domain.processing import EngineLimits, EngineState, UnitPolicy
from omr_scanner.services import batch_store, scan_recovery, scan_sessions
from omr_scanner.services.continuous_engine import ContinuousEngine
from omr_scanner.services.recognition_pool import RecognitionDone, error_result

TERMINAL = {ScanJobStatus.COMPLETED.value, ScanJobStatus.WARNING.value, ScanJobStatus.FAILED.value}


class HeldRecogniser:
    """Holds every sheet until :meth:`release` lets ``n`` of them finish (FIFO or LIFO).

    The answer is a cheap failure result (no image is read): enough to commit
    a complete work unit, which is all a bound needs.
    """

    def __init__(self, *, lifo: bool = False) -> None:
        self.queue: deque[tuple[int, Path]] = deque()
        self.allowed = 0
        self.lifo = lifo
        self.max_outstanding = 0
        self.submissions = 0
        self.closed = False
        self.lose: set[int] = set()
        """Tickets whose next delivery reports a lost worker."""

    @property
    def outstanding(self) -> int:
        return len(self.queue)

    @property
    def accepting(self) -> bool:
        return not self.closed

    def submit(self, ticket: int, path: Path) -> None:
        self.queue.append((ticket, path))
        self.submissions += 1
        self.max_outstanding = max(self.max_outstanding, len(self.queue))

    def release(self, count: int = 1_000_000) -> None:
        self.allowed += count

    def poll(self, timeout: float) -> list[RecognitionDone]:
        done = []
        while self.queue and self.allowed > 0:
            ticket, path = self.queue.pop() if self.lifo else self.queue.popleft()
            self.allowed -= 1
            lost = ticket in self.lose
            self.lose.discard(ticket)
            done.append(
                RecognitionDone(
                    ticket=ticket,
                    result=error_result(path, "test double: not read"),
                    worker_lost=lost,
                )
            )
        return done

    def cancel_queued(self) -> list[int]:
        dropped = [ticket for ticket, _ in self.queue]
        self.queue.clear()
        return dropped

    def close(self) -> list[int]:
        self.closed = True
        return self.cancel_queued()


class ReversedOrder:
    """Another recogniser whose finished sheets come back last-submitted first."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    @property
    def outstanding(self) -> int:
        return int(self.inner.outstanding)

    @property
    def accepting(self) -> bool:
        return bool(self.inner.accepting)

    def submit(self, ticket: int, path: Path) -> None:
        self.inner.submit(ticket, path)

    def poll(self, timeout: float) -> list[RecognitionDone]:
        return list(reversed(self.inner.poll(timeout)))

    def cancel_queued(self) -> list[int]:
        return list(self.inner.cancel_queued())

    def close(self) -> list[int]:
        return list(self.inner.close())


@pytest.fixture
def rig(project_session) -> Iterator[EngineRig]:
    yield EngineRig(project_session)


def register_units(rig: EngineRig, units: int, size: int, tmp: Path) -> list[str]:
    """``units`` finite sealed batches of ``size`` placeholder files in the rig's session."""
    identity = batch_store.BatchIdentity.of(rig.template)
    batch_ids = []
    for unit in range(units):
        paths = []
        for index in range(size):
            path = tmp / f"u{unit:03d}" / f"{index:05d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Distinct bytes: identical files would (rightly) be linked as
            # exact duplicates by the registration step and never read.
            path.write_bytes(f"{unit}:{index}".encode())
            paths.append(path)
        batch_id = scan_sessions.start_batch(
            rig.database, paths, identity=identity, scan_session_id=rig.session_id,
            settings={scan_recovery.WORK_UNIT_SETTING: True}, started_by="op",
        )
        scan_sessions.seal_batch(rig.database, batch_id)
        batch_ids.append(batch_id)
    return batch_ids


def engine_with(rig: EngineRig, recogniser, limits: EngineLimits) -> ContinuousEngine:
    engine = ContinuousEngine(
        rig.database, scan_session_id=rig.session_id, template=rig.template,
        recogniser=recogniser, limits=limits, started_by="op",
    )
    engine.start()
    return engine


def counts(rig: EngineRig) -> dict[str, int]:
    with rig.database.session() as session:
        return {
            str(status): int(count)
            for status, count in session.execute(
                select(BatchScan.status, func.count()).group_by(BatchScan.status)
            ).all()
        }


class TestBackpressure:
    def test_producers_outrun_a_stuck_worker_and_memory_stays_bounded(self, rig, tmp_path):
        """§19 / §42: thousands pending, at most ``max_in_flight`` claimed or held."""
        register_units(rig, units=12, size=100, tmp=tmp_path)
        held = HeldRecogniser()
        limits = EngineLimits(max_in_flight=8, claim_window=5, max_commit_group=3)
        engine = engine_with(rig, held, limits)
        for _ in range(50):
            engine.step()
            assert engine.in_flight <= 8
        assert held.max_outstanding <= 8
        assert held.submissions == 8  # nothing beyond the bound was ever created
        state = counts(rig)
        assert state[ScanJobStatus.PROCESSING.value] == 8
        assert state[ScanJobStatus.PENDING.value] == 1200 - 8
        # The bottleneck goes away: everything is processed, still bounded.
        held.release()
        while engine.step(wait=0).claimed or engine.in_flight:
            assert engine.in_flight <= 8
            assert engine.status().writer_backlog <= 8
        state = counts(rig)
        assert set(state) == {ScanJobStatus.FAILED.value}
        assert state[ScanJobStatus.FAILED.value] == 1200
        assert held.max_outstanding <= 8
        assert engine.status().caught_up
        engine.shutdown()

    def test_memory_follows_the_bounds_not_the_backlog(self, rig, tmp_path, project_session):
        """§20: peak traced memory of a run is independent of how much is pending."""
        def peak_for(units: int) -> int:
            local = EngineRig(project_session)
            register_units(local, units=units, size=50, tmp=tmp_path / f"m{units}")
            held = HeldRecogniser()
            engine = engine_with(local, held, EngineLimits(max_in_flight=6, claim_window=6))
            gc.collect()
            tracemalloc.start()
            for _ in range(40):
                engine.step()
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert held.submissions == 6
            engine.shutdown(drain=False)
            return peak

        small = peak_for(2)  # 100 pending
        large = peak_for(40)  # 2,000 pending
        # Twenty times the backlog, and well under twice the memory: the cost
        # that grows is the per-step batch list, not one object per sheet.
        assert large < small * 2 + 2_000_000

    def test_a_slow_writer_backs_up_into_the_workers_not_into_memory(
        self, rig, tmp_path, monkeypatch
    ):
        """§43: commits are slow; the result backlog stays bounded and nothing is lost."""
        import time

        register_units(rig, units=3, size=20, tmp=tmp_path)
        held = HeldRecogniser()
        held.release()
        original = batch_store.record_results
        sizes: list[int] = []

        def slow(*args: Any, **kwargs: Any) -> Any:
            sizes.append(len(args[2]))
            time.sleep(0.01)
            return original(*args, **kwargs)

        monkeypatch.setattr(batch_store, "record_results", slow)
        engine = engine_with(rig, held, EngineLimits(max_in_flight=4, claim_window=4,
                                                     max_commit_group=2))
        steps = 0
        while (engine.step().claimed or engine.in_flight) and steps < 1_000:
            steps += 1
            assert engine.in_flight <= 4
            assert engine.status().writer_backlog <= 4
        assert held.max_outstanding <= 4
        assert max(sizes) <= 2
        assert sum(sizes) == 60
        assert counts(rig) == {ScanJobStatus.FAILED.value: 60}
        engine.shutdown()
        assert integrity(rig.project.root).sqlite_ok


class TestOrderAndFaults:
    def test_out_of_order_completion_gives_the_same_durable_state(self, workspace):
        """§59: later-submitted sheets finish first; the outcome is identical."""
        from tests.engine_rig import OPTIONS

        from omr_scanner.services import create_project
        from omr_scanner.services.recognition_pool import InlineRecogniser

        sheets = readable_sheets(30)[:24]
        views = []
        for name, lifo in (("Fifo", False), ("Lifo", True)):
            project = create_project(workspace, name)
            try:
                rig = EngineRig(project)
                rig.source("a")
                rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(sheets)])
                inline = InlineRecogniser(
                    rig.template, options=OPTIONS, recognise=rig.recognise, per_poll=8
                )
                recogniser: Any = ReversedOrder(inline) if lifo else inline
                engine = rig.new_engine(
                    recogniser=recogniser,
                    limits=EngineLimits(max_in_flight=8, claim_window=8, max_commit_group=1),
                    unit_policy=UnitPolicy(max_unit_size=12, trickle_seconds=0),
                )
                rig.make_ready()
                rig.run()
                engine.shutdown()
                views.append(durable_view(rig.database, rig.session_id))
            finally:
                project.close()
        assert views[0] == views[1]

    def test_a_recognition_fault_fails_one_sheet_and_the_rest_continue(self, rig):
        """§44: an exception inside recognition is that sheet's failure, as today."""
        sheets = readable_sheets(12)[:8]
        rig.source("a")
        rig.write("a", [(f"{i:06d}.png", data) for i, data in enumerate(sheets)])
        bad = digest(sheets[3])
        rig.recognise.fail = {bad}
        engine = rig.new_engine()
        rig.make_ready()
        rig.run()
        with rig.database.session() as session:
            rows = session.execute(
                select(BatchScan.content_sha256, BatchScan.status, BatchScan.scan_id)
            ).all()
            failed = [row for row in rows if row[0] == bad]
            assert failed[0][1] == ScanJobStatus.FAILED.value
            assert session.scalar(
                select(func.count()).select_from(ReviewConflict).where(
                    ReviewConflict.scan_id == failed[0][2]
                )
            ) == 1
        assert all(row[1] in TERMINAL for row in rows)
        assert sum(1 for row in rows if row[1] != ScanJobStatus.FAILED.value) >= 6
        assert engine.state is EngineState.RUNNING
        engine.shutdown()
        assert integrity(rig.project.root).health_errors == []

    def test_a_lost_worker_is_retried_once_then_recorded_as_today(self, rig, tmp_path):
        """A dead worker process is an infrastructure fault, not a paper fault."""
        register_units(rig, units=1, size=4, tmp=tmp_path)
        held = HeldRecogniser()
        engine = engine_with(rig, held, EngineLimits(max_in_flight=4, claim_window=4,
                                                     infrastructure_retries=1))
        engine.step()
        tickets = [ticket for ticket, _ in held.queue]
        held.lose = {tickets[0], tickets[1]}
        held.release(4)
        engine.step()
        # Two lost: released to pending and claimed again; two committed.
        with rig.database.session() as session:
            first = session.get(BatchScan, tickets[0])
            assert first is not None and first.attempt_count == 0
        held.lose = {tickets[1]}  # lost a second time: beyond the retry budget
        held.release()
        while engine.step().claimed or engine.in_flight:
            pass
        with rig.database.session() as session:
            rows = {row.scan_id: row for row in session.scalars(select(BatchScan)).all()}
        assert all(row.status == ScanJobStatus.FAILED.value for row in rows.values())
        assert all(row.attempt_count == 1 for row in rows.values())
        assert held.submissions == 6
        engine.shutdown()


class TestShutdown:
    def _engine(self, rig: EngineRig, tmp_path: Path, held: HeldRecogniser) -> ContinuousEngine:
        register_units(rig, units=2, size=10, tmp=tmp_path)
        return engine_with(rig, held, EngineLimits(max_in_flight=6, claim_window=6))

    def _no_claims_left(self, rig) -> None:
        state = counts(rig)
        assert ScanJobStatus.PROCESSING.value not in state
        assert ScanJobStatus.QUEUED.value not in state
        with rig.database.session() as session:
            running = session.scalar(
                select(func.count()).select_from(batch_store.ScanBatch).where(
                    batch_store.ScanBatch.status == BatchStatus.RUNNING.value
                )
            )
        assert not running

    def test_with_nothing_queued(self, rig, tmp_path):
        held = HeldRecogniser()
        held.release()
        engine = self._engine(rig, tmp_path, held)
        while engine.step().claimed or engine.in_flight:
            pass
        engine.shutdown()
        assert engine.state is EngineState.STOPPED
        assert counts(rig) == {ScanJobStatus.FAILED.value: 20}
        self._no_claims_left(rig)

    def test_queued_work_is_released_never_lost(self, rig, tmp_path):
        held = HeldRecogniser()
        engine = self._engine(rig, tmp_path, held)
        engine.step()
        assert engine.in_flight == 6
        status = engine.shutdown()
        assert status.state is EngineState.STOPPED
        assert counts(rig) == {ScanJobStatus.PENDING.value: 20}
        self._no_claims_left(rig)

    def test_in_flight_work_finishes_and_is_committed_when_draining(self, rig, tmp_path):
        held = HeldRecogniser()
        engine = self._engine(rig, tmp_path, held)
        engine.step()
        # Three sheets are "inside a worker": not cancellable.
        running = [held.queue.popleft() for _ in range(3)]
        held.release(3)

        def cancel_rest() -> list[int]:
            dropped = [ticket for ticket, _ in held.queue]
            held.queue.clear()
            held.queue.extend(running)
            return dropped

        held.cancel_queued = cancel_rest  # type: ignore[method-assign]
        engine.shutdown(drain=True, timeout=5)
        assert counts(rig) == {ScanJobStatus.FAILED.value: 3, ScanJobStatus.PENDING.value: 17}
        self._no_claims_left(rig)

    def test_in_flight_work_is_released_when_not_draining(self, rig, tmp_path):
        held = HeldRecogniser()
        engine = self._engine(rig, tmp_path, held)
        engine.step()
        engine.shutdown(drain=False)
        assert counts(rig) == {ScanJobStatus.PENDING.value: 20}
        self._no_claims_left(rig)

    def test_pending_writer_results_are_committed_before_stopping(
        self, rig, tmp_path, monkeypatch
    ):
        held = HeldRecogniser()
        engine = self._engine(rig, tmp_path, held)
        engine.step()
        original = batch_store.record_results

        def busy(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("database is locked (injected)")

        monkeypatch.setattr(batch_store, "record_results", busy)
        held.release(2)
        engine.step()
        assert engine.status().writer_backlog == 2
        monkeypatch.setattr(batch_store, "record_results", original)
        engine.shutdown()
        assert counts(rig)[ScanJobStatus.FAILED.value] == 2
        self._no_claims_left(rig)
        # Unfinished units leave `running` as `interrupted`, no manifest.
        with rig.database.session() as session:
            statuses = set(session.scalars(select(batch_store.ScanBatch.status)).all())
        assert BatchStatus.INTERRUPTED.value in statuses

    def test_a_writer_still_failing_at_shutdown_leaves_claims_for_recovery(
        self, rig, tmp_path, monkeypatch
    ):
        held = HeldRecogniser()
        engine = self._engine(rig, tmp_path, held)
        engine.step()
        original = batch_store.record_results

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("disk full (injected)")

        monkeypatch.setattr(batch_store, "record_results", broken)
        held.release(2)
        engine.step()
        status = engine.shutdown()
        # Not "stopped": two read sheets could not be saved, so their claims
        # are deliberately left for recovery rather than reported as clean.
        assert status.state is EngineState.FAULTED
        assert counts(rig)[ScanJobStatus.PROCESSING.value] == 2
        monkeypatch.setattr(batch_store, "record_results", original)
        restarted = engine_with(rig, HeldRecogniser(), EngineLimits(max_in_flight=6,
                                                                    claim_window=6))
        assert ScanJobStatus.PROCESSING.value not in counts(rig)
        restarted.shutdown()

    def test_pause_and_cancel_primitives(self, rig, tmp_path):
        held = HeldRecogniser()
        engine = self._engine(rig, tmp_path, held)
        engine.pause_scheduling()
        engine.step()
        assert engine.in_flight == 0
        engine.resume_scheduling()
        engine.step()
        assert engine.in_flight == 6
        assert engine.cancel_queued() == 6
        assert engine.in_flight == 0
        assert counts(rig) == {ScanJobStatus.PENDING.value: 20}
        engine.pause_scheduling()
        engine.step()
        assert engine.in_flight == 0
        status = engine.status()
        assert status.scheduling_paused and not status.caught_up
        engine.shutdown()


def test_superseded_and_foreign_template_batches_are_not_processed(rig, tmp_path):
    """Effective membership: the engine reads only live batches it can read correctly."""
    first, second = register_units(rig, units=2, size=3, tmp=tmp_path)
    other_template = harness_template().model_copy(update={"template_id": "another"})
    foreign = scan_sessions.start_batch(
        rig.database, [tmp_path / "f.png"], identity=batch_store.BatchIdentity.of(other_template),
        scan_session_id=rig.session_id, started_by="op", acknowledge_template_change=True,
    )
    scan_sessions.seal_batch(rig.database, foreign)
    scan_sessions.record_supersession(rig.database, first, second, reason="test", recorded_by="op")
    held = HeldRecogniser()
    held.release()
    engine = engine_with(rig, held, EngineLimits(max_in_flight=4, claim_window=4))
    while engine.step().claimed or engine.in_flight:
        pass
    status = engine.status()
    skipped = dict(status.skipped_batches)
    assert skipped[first] == "superseded"
    assert foreign in skipped
    with rig.database.session() as session:
        by_batch = dict(
            session.execute(
                select(BatchScan.batch_id, func.count())
                .where(BatchScan.status == ScanJobStatus.PENDING.value)
                .group_by(BatchScan.batch_id)
            ).all()
        )
    assert by_batch == {first: 3, foreign: 1}
    assert held.submissions == 3
    engine.shutdown()


def _ids(items: Sequence[RecognitionDone]) -> list[int]:
    return [item.ticket for item in items]
