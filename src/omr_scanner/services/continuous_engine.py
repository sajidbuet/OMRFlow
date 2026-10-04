"""The continuous-processing engine: registered intake to durable sheets (0.1.1 revised phase 6).

Purpose:
    Turn the intake ledger's ready files (phase 5) into recognised, durably
    committed sheets **while files keep arriving**, across a succession of
    finite, sealed ``ScanBatch`` units of one open ``ScanSession`` - and stay
    correct when the process is killed at any point. Headless: no Qt, no
    thread of its own; every operation is a method a test (or the thin
    :meth:`ContinuousEngine.run` loop) calls. See
    ``docs/decisions/ADR-0009-continuous-engine-single-writer.md``.

The model it preserves:
    ``Project -> ScanSession -> finite ScanBatch units -> sheets``. A unit is
    registered by :meth:`omr_scanner.services.intake.IntakeService.register`
    (one source, ready order, sealed at creation) and is never extended; new
    arrivals form new units. "Caught up" never means the session is complete.

Responsibilities, one method each:
    * :meth:`ContinuousEngine.start` - the restart sequence: scan recovery
      (:func:`~omr_scanner.services.scan_recovery.recover_on_open`: stale
      claims back to ``pending``, interrupted units' review state completed),
      then intake recovery (:class:`~omr_scanner.services.intake.IntakeService`),
      then work resumes in the **same** session and the **same** batches.
    * :meth:`~ContinuousEngine.poll_intake` / :meth:`~ContinuousEngine.form_units`
      - reconcile due sources; register due units
      (:func:`~omr_scanner.domain.processing.plan_units`).
    * :meth:`~ContinuousEngine.step` - one bounded iteration: collect finished
      sheets, commit them, finalise finished units, claim and submit more.
    * :meth:`~ContinuousEngine.pause_intake`, :meth:`~ContinuousEngine.pause_scheduling`,
      :meth:`~ContinuousEngine.cancel_queued`, :meth:`~ContinuousEngine.shutdown`
      - the mechanical stop primitives phase 7 builds operator policy on.
    * :meth:`~ContinuousEngine.status` - an immutable snapshot for phase 8.

Ownership (who writes):
    The engine's **coordinator thread** - whichever thread calls its methods,
    one at a time - is its only writer: claims, commits, releases, unit
    finalisation and intake registration all run there, each a short explicit
    transaction, none held across recognition or file I/O. Workers
    (:mod:`~omr_scanner.services.recognition_pool`) never open the database.

"Completed means durable" - the transaction boundary:
    A sheet is completed when one committed transaction holds its result row
    (status, outcome, readings, ``result_json``, attempt) and every conflict
    that result implies for that sheet -
    :func:`~omr_scanner.services.batch_store.record_results` with the template
    and ``claimed_only=True``, the phase 3 work unit. Only after that commit
    returns is the sheet counted, acknowledged or reported (:class:`EngineHooks`
    ``committed`` fires first, so a kill between the two is testable).
    The work unit also records the sheet's scan-quality decision (revised
    phase 7, :mod:`~omr_scanner.services.quality_decisions`).
    Batch-scope review state (re-imports, duplicate Student IDs across the
    session, undefined set codes) depends on other sheets and is completed by
    :func:`~omr_scanner.services.scan_recovery.complete_batch_review_state`
    when a unit finishes, **before** the batch leaves ``running`` - and by
    recovery if a kill lands first (ADR-0006, unchanged). Since revised phase
    7 the session-wide duplicate Student-ID groups of each committed group are
    also re-derived right after its commit (bounded), so a cross-unit
    duplicate reaches Resolve within one commit rather than at unit end.

Operator intent (revised phase 7):
    Pause / stop intent and intake pauses are persisted
    (:mod:`~omr_scanner.services.session_controls`) and read every step;
    :meth:`ContinuousEngine.finish_current_and_stop` and
    :meth:`ContinuousEngine.cancel_queued_and_stop` are the two operator stop
    policies. :meth:`ContinuousEngine.start` takes the project's coordinator
    lease (:mod:`~omr_scanner.services.coordinator`) first.

Never resubmitted:
    Only ``pending`` / ``cancelled`` rows are claimed, by compare-and-set into
    ``processing``; a committed row is ``completed`` / ``warning`` /
    ``failed`` and can never be claimed again, and a result can only be
    written onto a row still ``processing`` (``claimed_only``).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update

from omr_scanner.database.models import BatchScan, BatchStatus, ScanBatch, ScanJobStatus
from omr_scanner.domain.intake import SourceKind
from omr_scanner.domain.processing import (
    EngineLimits,
    EngineState,
    UnitCandidate,
    UnitPolicy,
    plan_units,
)
from omr_scanner.domain.session_controls import ProcessingIntent, SessionControls
from omr_scanner.domain.session_finish import FinishOutcome, IncompleteAcceptance
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import (
    batch_store,
    coordinator,
    quality_decisions,
    review_store,
    scan_recovery,
    scan_sessions,
    session_controls,
)
from omr_scanner.services import intake as intake_service
from omr_scanner.services.batch_processor import ProcessedScan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.recognition_pool import Recogniser, RecognitionDone

_LOGGER = logging.getLogger(__name__)

CLAIMABLE = (ScanJobStatus.PENDING.value, ScanJobStatus.CANCELLED.value)
"""Rows the engine may claim: never attempted, or stopped by an operator."""

UNFINISHED = (
    ScanJobStatus.PENDING.value,
    ScanJobStatus.CANCELLED.value,
    ScanJobStatus.QUEUED.value,
    ScanJobStatus.PROCESSING.value,
)
"""Rows that keep a unit from being finished."""

ENGINE_SETTING = "registered_by"
"""Key in a unit's ``settings_json`` naming what registered it (provenance)."""

ENGINE_NAME = "continuous_engine"


def utc_now() -> datetime:
    """The production clock."""
    return datetime.now(UTC)


# ----------------------------------------------------------------------
# Values
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Claim:
    """One sheet this engine has durably claimed (``processing``)."""

    scan_id: int
    batch_id: str
    batch_index: int
    path: Path


class EngineHooks:
    """Observation points at the engine's durable boundaries. Every method a no-op.

    Used by the tests and by the real-process crash harness, which pauses a
    child at a hook and kills it there. A hook that raises propagates: that is
    how a test injects a fault at an exact boundary.
    """

    def registered(self, batch_id: str, scan_ids: Sequence[int]) -> None:
        """A unit was registered (committed by the intake service)."""

    def claimed(self, claims: Sequence[Claim]) -> None:
        """Claims committed; nothing submitted yet."""

    def submitted(self, claim: Claim) -> None:
        """One claimed sheet was handed to the recogniser (the submission log)."""

    def recognised(self, claim: Claim) -> None:
        """A worker returned this sheet; nothing written yet."""

    def before_commit(self, batch_id: str, claims: Sequence[Claim]) -> None:
        """About to open the work-unit transaction for these sheets."""

    def committed(self, batch_id: str, claims: Sequence[Claim]) -> None:
        """The work-unit transaction committed; not yet acknowledged in memory."""

    def syncing_duplicates(self, batch_id: str, claims: Sequence[Claim]) -> None:
        """Committed and acknowledged; the bounded cross-sheet duplicate pass is next.

        A kill here leaves committed sheets whose session-wide duplicate
        conflicts may not exist yet - in a unit that is still ``running``,
        which recovery completes (revised phase 7).
        """

    def released(self, claims: Sequence[Claim]) -> None:
        """Claims returned to ``pending`` (committed)."""

    def finalising(self, batch_id: str) -> None:
        """A unit's batch-scope review state is about to be completed."""

    def finalised(self, batch_id: str, status: str) -> None:
        """A unit left ``running`` with ``status``."""


@dataclass(frozen=True, slots=True)
class StartupReport:
    """What the restart sequence did.

    Attributes:
        scan_recovery: Stale claims returned, interrupted units completed.
        intake_recovery: The intake ledger's restart repair.
        decisions_backfilled: Read sheets that had no scan-quality decision yet
            (read before migration 17) and were decided from stored results.
        controls: The operator intent restored from the database - processing
            is resumed only when it says ``running``.
    """

    scan_recovery: scan_recovery.RecoveryReport
    intake_recovery: intake_service.RecoveryReport | None = None
    decisions_backfilled: int = 0
    controls: SessionControls | None = None


@dataclass(frozen=True, slots=True)
class StepReport:
    """What one :meth:`ContinuousEngine.step` did."""

    recognised: int = 0
    committed: int = 0
    claimed: int = 0
    released: int = 0
    finalised: tuple[str, ...] = ()

    @property
    def idle(self) -> bool:
        """Nothing happened in this step."""
        return not (
            self.recognised or self.committed or self.claimed or self.released or self.finalised
        )


@dataclass(frozen=True, slots=True)
class EngineStatus:
    """An immutable snapshot of one engine and its session (for phase 8).

    Counts are from committed rows (one grouped query); in-memory figures say
    what *this* engine holds right now. ``caught_up`` means "no runnable work
    at this moment" - never "the examination is complete"; the session stays
    open until an operator closes it (phase 7).

    Attributes:
        state: The engine's lifecycle state.
        scan_session_id: The session it serves.
        batch_ids: Every batch of the session, oldest first.
        registered: Sheets registered in the session's batches.
        pending: Committed ``pending`` / ``cancelled`` rows - not completed.
        processing: Committed ``queued`` / ``processing`` rows (claimed).
        completed: Committed ``completed`` + ``warning`` rows.
        failed: Committed ``failed`` rows (retryable on request, as today).
        duplicates: Exact-duplicate rows (never read).
        in_flight: Sheets this engine has claimed and not yet committed.
        writer_backlog: Of those, read and waiting for the writer.
        ready_intake: Ready, unregistered intake files for the session.
        units_registered: Units this engine registered since it started.
        sheets_submitted: Sheets handed to the recogniser since it started.
        sheets_committed: Sheets committed since it started.
        intake_paused / scheduling_paused: Whether intake / claiming is
            paused - by the in-memory stop primitives **or** by the persisted
            operator intent (revised phase 7).
        skipped_batches: ``(batch id, reason)`` the engine will not process
            (another template, another engine version, superseded, running
            elsewhere).
        last_error: The most recent failure message, or ``""``.
        caught_up: Running, nothing in flight, nothing claimable, no ready
            intake and no unit awaiting finalisation - this engine's view
            only. The session-level "Caught up - watching for new scans",
            with source reachability, is
            :func:`omr_scanner.services.session_snapshot.take_snapshot`.
        processing_intent: The persisted recognition intent.
    """

    state: EngineState
    scan_session_id: str
    batch_ids: tuple[str, ...]
    registered: int
    pending: int
    processing: int
    completed: int
    failed: int
    duplicates: int
    in_flight: int
    writer_backlog: int
    ready_intake: int
    units_registered: int
    sheets_submitted: int
    sheets_committed: int
    intake_paused: bool
    scheduling_paused: bool
    skipped_batches: tuple[tuple[str, str], ...]
    last_error: str
    caught_up: bool
    processing_intent: ProcessingIntent = ProcessingIntent.RUNNING


@dataclass
class _Counters:
    units_registered: int = 0
    submitted: int = 0
    committed: int = 0
    released: int = 0
    worker_lost: int = 0
    writer_failures: int = 0


# ----------------------------------------------------------------------
# Durable primitives (each one short transaction)
# ----------------------------------------------------------------------
def claim_scans(
    database: ProjectDatabase, batch_ids: Sequence[str], limit: int
) -> tuple[Claim, ...]:
    """Claim up to ``limit`` claimable rows of ``batch_ids``, in that batch order.

    One transaction: the rows move ``pending``/``cancelled`` -> ``processing``
    by compare-and-set, and every batch they belong to becomes ``running`` -
    so a kill after this commit leaves exactly what recovery repairs (a
    ``running`` batch with stale ``processing`` rows), and a kill before it
    leaves nothing claimed. Within a batch, rows are taken in ``batch_index``
    order.
    """
    if limit < 1 or not batch_ids:
        return ()
    order = {batch_id: position for position, batch_id in enumerate(batch_ids)}
    moment = utc_now()
    with database.session() as session:
        with_work = sorted(
            (
                str(batch_id)
                for (batch_id,) in session.execute(
                    select(BatchScan.batch_id)
                    .where(BatchScan.batch_id.in_(list(order)))
                    .where(BatchScan.status.in_(CLAIMABLE))
                    .distinct()
                ).all()
            ),
            key=lambda item: order[item],
        )
        picked: list[Claim] = []
        for batch_id in with_work:
            remaining = limit - len(picked)
            if remaining <= 0:
                break
            rows = session.execute(
                select(BatchScan.scan_id, BatchScan.batch_index, BatchScan.source_path)
                .where(BatchScan.batch_id == batch_id)
                .where(BatchScan.status.in_(CLAIMABLE))
                .order_by(BatchScan.batch_index)
                .limit(remaining)
            ).all()
            picked += [
                Claim(int(scan_id), batch_id, int(index), Path(str(path)))
                for scan_id, index, path in rows
            ]
        if not picked:
            return ()
        changed = session.execute(
            update(BatchScan)
            .where(BatchScan.scan_id.in_([item.scan_id for item in picked]))
            .where(BatchScan.status.in_(CLAIMABLE))
            .values(status=ScanJobStatus.PROCESSING.value, started_at=moment)
        )
        if batch_store._rows_changed(changed) != len(picked):
            # Cannot happen inside one transaction of the single writer; if it
            # ever does, nothing is claimed rather than claiming ambiguously.
            raise OMRScannerError(
                "claim compare-and-set mismatch",
                user_message="Another process changed the scans being claimed.",
            )
        session.execute(
            update(ScanBatch)
            .where(ScanBatch.batch_id.in_(sorted({item.batch_id for item in picked})))
            .where(ScanBatch.status != BatchStatus.RUNNING.value)
            .values(status=BatchStatus.RUNNING.value, updated_at=moment)
        )
    return tuple(picked)


def release_claims(database: ProjectDatabase, scan_ids: Sequence[int]) -> int:
    """Return claimed rows to ``pending`` (compare-and-set); one transaction.

    Used for sheets withdrawn before they started, sheets whose worker process
    died, and in-flight sheets a non-draining stop abandons. Never touches a
    committed row. Returns how many rows changed.
    """
    if not scan_ids:
        return 0
    with database.session() as session:
        changed = session.execute(
            update(BatchScan)
            .where(BatchScan.scan_id.in_(list(scan_ids)))
            .where(BatchScan.status == ScanJobStatus.PROCESSING.value)
            .values(status=ScanJobStatus.PENDING.value, started_at=None)
        )
        return batch_store._rows_changed(changed)


# ----------------------------------------------------------------------
# The engine
# ----------------------------------------------------------------------
class ContinuousEngine:
    """Processes one scan session's registered work, continuously and crash-safely.

    Args:
        database: The open, writable project database.
        scan_session_id: The session this engine serves. It processes only
            this session's batches and registers only into it.
        template: The template its units are read with (the session's pin).
            A batch registered with another template, geometry, recognition
            settings or engine version is skipped and reported, never read
            with the wrong rules.
        recogniser: Where sheets are read
            (:class:`~omr_scanner.services.recognition_pool.ProcessRecogniser`
            in production). The engine owns it from here and closes it.
        intake_factory: Builds the intake service. Called by :meth:`start`
            **after** scan recovery (the restart order of ARCHITECTURE_NOTES
            §13.5); constructing the service runs intake recovery. ``None``:
            no intake - the engine processes already registered batches only.
        limits: Every bound (:class:`~omr_scanner.domain.processing.EngineLimits`).
        unit_policy: Unit size and trickle timeout.
        clock: Injected time, for unit formation and intake polling.
        hooks: Observation / fault-injection points.
        started_by: Recorded on every unit it registers and seals.
        template_path: Recorded in the units' identity, as the Scan stage does.

    One engine per project, used from one thread. Since revised phase 7 that is
    **enforced**: :meth:`start` takes the project's coordinator lease
    (:mod:`omr_scanner.services.coordinator`) *before* its recovery touches a
    claim, so it is refused - :class:`~omr_scanner.services.coordinator.CoordinatorBusyError`
    - while the finite Scan stage (or another engine) processes the project,
    and the finite Scan stage is refused while this engine holds it.

    Operator intent is persisted (:mod:`omr_scanner.services.session_controls`)
    and read on every step: a paused or stopped session claims nothing new,
    a paused session's or source's intake is neither reconciled nor
    registered, and a restart resumes recognition only when the stored intent
    is ``running``.
    """

    def __init__(
        self,
        database: ProjectDatabase,
        *,
        scan_session_id: str,
        template: OmrTemplate,
        recogniser: Recogniser,
        intake_factory: Callable[[], intake_service.IntakeService] | None = None,
        limits: EngineLimits | None = None,
        unit_policy: UnitPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
        hooks: EngineHooks | None = None,
        started_by: str = "continuous engine",
        template_path: Path | None = None,
    ) -> None:
        self._database = database
        self._session_id = scan_session_id
        self._template = template
        self._identity = batch_store.BatchIdentity.of(template, template_path)
        self._recogniser = recogniser
        self._intake_factory = intake_factory
        self._intake: intake_service.IntakeService | None = None
        self._limits = limits if limits is not None else EngineLimits()
        self._policy = unit_policy if unit_policy is not None else UnitPolicy()
        self._clock = clock
        self._hooks = hooks if hooks is not None else EngineHooks()
        self._started_by = started_by
        self._state = EngineState.NEW
        self._intake_paused = False
        self._scheduling_paused = False
        self._in_flight: dict[int, Claim] = {}
        self._buffer: list[tuple[Claim, ProcessedScan]] = []
        self._owned: set[str] = set()
        self._skipped: dict[str, str] = {}
        self._compatible: set[str] = set()
        self._prepared: set[str] = set()
        self._runnable_cache: tuple[float, tuple[str, ...]] | None = None
        self._worker_lost: dict[int, int] = defaultdict(int)
        self._last_poll: dict[str, datetime] = {}
        self._last_error = ""
        self._counters = _Counters()
        self._lease: coordinator.CoordinatorLease | None = None
        self._abandoned = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def state(self) -> EngineState:
        """The engine's lifecycle state."""
        return self._state

    @property
    def scan_session_id(self) -> str:
        """The session this engine serves."""
        return self._session_id

    @property
    def intake(self) -> intake_service.IntakeService | None:
        """The intake service, once :meth:`start` has built it."""
        return self._intake

    @property
    def in_flight(self) -> int:
        """Sheets claimed by this engine and not yet committed."""
        return len(self._in_flight)

    def start(self) -> StartupReport:
        """Run the restart sequence and begin. Idempotent per process start.

        0. Take the project's **coordinator lease** - before anything touches
           a claim. Refused (:class:`~omr_scanner.services.coordinator.CoordinatorBusyError`,
           nothing changed, the engine stays ``new``) while another
           coordinator processes the project.
        1. Scan recovery: every stale ``queued``/``processing`` claim returns
           to ``pending`` (retryable, never failed or completed); every unit a
           kill left ``running`` has its review state completed from stored
           results - including the cross-sheet duplicate pass a kill may have
           cut short - and leaves ``running`` (``interrupted`` while work
           remains). No session, batch or supersession is created - the work
           resumes in the same session and the same batches.
        2. Scan-quality decisions for read sheets that have none (read before
           migration 17), from stored results - no image is read.
        3. Intake recovery: built by ``intake_factory`` (unsettled rows
           re-observed, ready rows re-verified, unfinished duplicate links
           completed, interrupted copies removed).
        4. The persisted operator intent is read back: processing resumes
           only if it says ``running``; a paused or stopped session stays so,
           and paused intake stays paused (it is re-read on every step).

        Calling it again on the result changes nothing.
        """
        if self._state is not EngineState.NEW:
            raise OMRScannerError(
                f"engine already {self._state.value}",
                user_message="The processing engine has already been started.",
            )
        lease = coordinator.acquire(
            self._database,
            coordinator.CoordinatorKind.CONTINUOUS_ENGINE,
            owner=self,
            label=f"continuous engine for session {self._session_id[:8]}",
        )
        try:
            recovered = scan_recovery.recover_on_open(self._database, templates=[self._template])
            backfilled = quality_decisions.evaluate_stored(self._database, self._session_id)
            intake_report: intake_service.RecoveryReport | None = None
            if self._intake_factory is not None:
                self._intake = self._intake_factory()
                intake_report = self._intake.last_recovery
            controls = session_controls.get_controls(self._database, self._session_id)
        except BaseException:
            lease.release()
            raise
        self._lease = lease
        self._state = EngineState.RUNNING
        _LOGGER.info(
            "Continuous engine started for session %s: %d interrupted unit(s) recovered, "
            "%d claim(s) returned to pending, %d decision(s) backfilled, processing %s, "
            "intake %s%s",
            self._session_id[:8],
            recovered.interrupted_batches,
            recovered.scans_returned,
            backfilled,
            controls.processing.value,
            "paused" if controls.intake_paused else "on",
            f", intake {intake_report}" if intake_report is not None else "",
        )
        return StartupReport(
            scan_recovery=recovered,
            intake_recovery=intake_report,
            decisions_backfilled=backfilled,
            controls=controls,
        )

    def _abandon(self) -> None:
        """An exception escaped a coordinator method: stop trusting this engine.

        What a crash would leave: claims stay ``processing`` and units stay
        ``running`` for the next coordinator's recovery. The lease is given
        up (that next coordinator must be able to start) and the recogniser is
        closed (a dead coordinator's workers die with it).
        """
        if self._state is EngineState.STOPPED:
            return
        self._state = EngineState.FAULTED
        self._abandoned = True
        if self._lease is not None:
            self._lease.release()
        try:
            self._recogniser.close()
        except Exception:  # pragma: no cover - best effort on the way down
            _LOGGER.exception("Closing the recogniser of an abandoned engine failed")
        _LOGGER.warning(
            "Continuous engine for session %s abandoned after an error; %d claim(s) left "
            "for recovery",
            self._session_id[:8],
            len(self._in_flight),
        )

    # ------------------------------------------------------------------
    # Persisted operator intent (revised phase 7)
    # ------------------------------------------------------------------
    def controls(self) -> SessionControls:
        """The session's persisted operator intent, as stored now."""
        return session_controls.get_controls(self._database, self._session_id)

    def finish_current_and_stop(
        self, *, actor: str = "", reason: str = "", timeout: float | None = None
    ) -> EngineStatus:
        """*Finish current and stop* - the default safe stop (ARCHITECTURE_NOTES §14.3).

        1. The intent ``stopped`` is **persisted first**, so a crash from here
           on comes back stopped, never resumed.
        2. Nothing new is claimed or registered.
        3. **Current** work finishes and is committed: every sheet this engine
           has already claimed and handed to the recogniser - queued in the
           bounded pool or inside a worker, at most
           :attr:`~omr_scanner.domain.processing.EngineLimits.max_in_flight` -
           and every result read and awaiting the writer. Unlike
           :meth:`cancel_queued_and_stop`, sheets queued in the pool are *not*
           withdrawn: they are already the engine's current work.
        4. Units this engine touched have their batch-scope review state
           completed and leave ``running``; the rest of a unit stays
           ``pending`` - durable and resumed only by an operator.
        5. The engine shuts down. Afterwards no row is left ``processing``
           (unless the writer itself is failing - ``faulted``). If
           ``timeout`` runs out first, what has not returned is released to
           ``pending`` (never lost, never counted).
        """
        session_controls.request_finish_current(
            self._database, self._session_id, actor=actor, reason=reason
        )
        self._intake_paused = True
        self._scheduling_paused = True
        drained = self._drain(timeout)
        return self.shutdown(drain=drained, timeout=0.0)

    def cancel_queued_and_stop(
        self, *, actor: str, reason: str = "", timeout: float | None = None
    ) -> EngineStatus:
        """*Cancel queued work* - the explicit, deliberate stop, for a named operator.

        Distinct from :meth:`finish_current_and_stop`: sheets submitted but not
        yet started are **withdrawn** and their claims released at once.
        Sheets already inside a worker cannot be interrupted; they finish and
        are recorded. Committed work is never touched. The intent ``stopped``
        (action ``queue_cancelled``) is persisted first; a restart does not
        resume.

        Raises:
            omr_scanner.services.review_store.ReviewError: No operator named.
        """
        session_controls.record_cancel_queued(
            self._database, self._session_id, actor=actor, reason=reason
        )
        self._intake_paused = True
        self._scheduling_paused = True
        if self._state is EngineState.RUNNING:
            self.cancel_queued()
        drained = self._drain(timeout)
        return self.shutdown(drain=drained, timeout=0.0)

    def finish_session(
        self,
        *,
        closed_by: str,
        acknowledge: IncompleteAcceptance | None = None,
        reason: str = "",
    ) -> FinishOutcome:
        """*Finish scan session* from the coordinator itself (it holds the lease).

        Runs :func:`omr_scanner.services.session_finish.finish_scan_session`
        with this engine's intake service, so the final reconciliation of
        every source is done by the one writer of the intake ledger. Sheets
        this engine still has in flight are reported as blockers - finish
        current work first. The engine keeps running afterwards; a closed
        session registers nothing new (late files are held).
        """
        from omr_scanner.services import session_finish

        try:
            return session_finish.finish_scan_session(
                self._database,
                self._session_id,
                closed_by=closed_by,
                intake=self._intake,
                acknowledge=acknowledge,
                reason=reason,
                coordinated=self._state is EngineState.RUNNING,
            )
        except OMRScannerError:
            raise  # a refusal (no operator named, no such session): nothing changed
        except BaseException:
            self._abandon()
            raise

    def _drain(self, timeout: float | None) -> bool:
        """Step until nothing claimed by this engine is in flight. ``False`` on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._in_flight and self._state is EngineState.RUNNING:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self.step(wait=0.2)
        return not self._in_flight

    # ------------------------------------------------------------------
    # Stop primitives (mechanics; the operator policies above use them)
    # ------------------------------------------------------------------
    def pause_intake(self) -> None:
        """Stop reconciling sources and registering units. Nothing is lost."""
        self._intake_paused = True

    def resume_intake(self) -> None:
        """Undo :meth:`pause_intake`."""
        self._intake_paused = False

    def pause_scheduling(self) -> None:
        """Claim nothing new; sheets already in flight still finish and commit."""
        self._scheduling_paused = True

    def resume_scheduling(self) -> None:
        """Undo :meth:`pause_scheduling`."""
        self._scheduling_paused = False

    def cancel_queued(self) -> int:
        """Withdraw every submitted sheet that has not started; release its claim.

        Sheets already inside a worker cannot be interrupted and still finish
        and commit. Returns how many claims were released.
        """
        dropped = self._recogniser.cancel_queued()
        return self._release(
            [self._in_flight[ticket] for ticket in dropped if ticket in self._in_flight]
        )

    def shutdown(self, *, drain: bool = True, timeout: float | None = None) -> EngineStatus:
        """Stop cleanly, leaving every sheet unambiguous. Idempotent.

        The Phase 6 shutdown contract. In order: intake and scheduling stop;
        queued sheets are withdrawn and their claims released; with ``drain``
        the sheets inside workers finish and are **committed** (up to
        ``timeout`` seconds), otherwise they are abandoned and their claims
        released; every unit this engine touched has its batch-scope review
        state completed and leaves ``running`` (``completed`` /
        ``completed_with_errors`` when finished, ``interrupted`` when work
        remains); the recogniser is closed.

        Afterwards each of the session's rows is exactly one of: committed
        (``completed`` / ``warning`` / ``failed``), ``pending`` (not
        completed; processed on the next start), or ``duplicate``. No row this
        engine claimed is left ``processing`` - unless the writer itself is
        failing (:attr:`EngineState.FAULTED`), when the claims are left for
        recovery, the only safe owner of a database that cannot be written.
        """
        if self._state is EngineState.NEW:
            self._recogniser.close()
            self._state = EngineState.STOPPED
        if self._state is EngineState.STOPPED or self._abandoned:
            return self.status()
        try:
            return self._shutdown(drain=drain, timeout=timeout)
        finally:
            # The project is free for the next coordinator once this engine
            # has settled - stopped cleanly, or faulted with its claims left
            # for that coordinator's recovery.
            if self._lease is not None:
                self._lease.release()  # idempotent; an abandoned engine already did

    def _shutdown(self, *, drain: bool, timeout: float | None) -> EngineStatus:
        self._intake_paused = True
        self._scheduling_paused = True
        if self._state is not EngineState.FAULTED:
            self._state = EngineState.STOPPING
            self.cancel_queued()
            if drain:
                deadline = None if timeout is None else time.monotonic() + timeout
                while self._in_flight and self._state is EngineState.STOPPING:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    self.step(wait=0.2)
        # Whatever has not returned by now never will: its result is discarded.
        self._recogniser.close()
        if self._state is not EngineState.FAULTED:
            self._commit_all()
            if self._buffer:
                # Read results the writer still cannot save: the claims stay
                # `processing` for recovery, and the engine says so.
                self._state = EngineState.FAULTED
                _LOGGER.error(
                    "Continuous engine stopped with %d unsaved result(s); recovery on the "
                    "next start returns their sheets to pending",
                    len(self._buffer),
                )
        if self._state is not EngineState.FAULTED:
            buffered = {claim.scan_id for claim, _processed in self._buffer}
            self._release(
                [claim for ticket, claim in list(self._in_flight.items()) if ticket not in buffered]
            )
            self._finalise(force=True)
            self._state = EngineState.STOPPED
        _LOGGER.info(
            "Continuous engine stopped (%s): %d sheet(s) committed, %d claim(s) released, "
            "%d still in flight",
            self._state.value,
            self._counters.committed,
            self._counters.released,
            len(self._in_flight),
        )
        return self.status()

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------
    def poll_intake(self, *, force: bool = False) -> tuple[intake_service.ReconcileReport, ...]:
        """Reconcile every enabled watched source attached to this session that is due.

        A source is due once its policy's poll interval has passed since this
        engine last reconciled it (``force``: now). One source failing never
        stops the others (phase 5's reachability rules).
        """
        if self._intake is None or self._intake_paused or self._state is not EngineState.RUNNING:
            return ()
        try:
            return self._poll_intake(force=force)
        except BaseException:
            self._abandon()
            raise

    def _poll_intake(self, *, force: bool) -> tuple[intake_service.ReconcileReport, ...]:
        assert self._intake is not None
        controls = self.controls()
        if controls.intake_paused:
            return ()
        now = self._clock()
        reports: list[intake_service.ReconcileReport] = []
        for source in intake_service.list_sources(self._database):
            if (
                source.kind is not SourceKind.WATCHED
                or not source.enabled
                or source.attached_session_id != self._session_id
                or not controls.intake_allowed(source.source_id)
            ):
                # A paused source is not listed at all: its reachability
                # stays what the last listing found, its files keep their
                # states, and nothing on its disk is lost (revised phase 7).
                continue
            last = self._last_poll.get(source.source_id)
            if (
                not force
                and last is not None
                and (now - last).total_seconds() < source.policy.poll_interval_seconds
            ):
                continue
            self._last_poll[source.source_id] = now
            try:
                reports.append(self._intake.reconcile(source.source_id))
            except Exception as exc:
                self._last_error = f"Reconciling source {source.label}: {exc}"
                _LOGGER.exception("Reconciliation of source %s failed", source.source_id)
        return tuple(reports)

    def form_units(self) -> tuple[str, ...]:
        """Register the units that are due now, as finite sealed batches.

        :func:`~omr_scanner.domain.processing.plan_units` decides which sources
        and how many files; each unit is the source's oldest ready files in the
        ledger's stable order, registered through phase 5's
        :meth:`~omr_scanner.services.intake.IntakeService.register` (copy,
        verify, one sealed batch, phase 4's duplicate rule). Returns the new
        batch ids.
        """
        if self._intake is None or self._intake_paused or self._state is not EngineState.RUNNING:
            return ()
        try:
            return self._form_units()
        except BaseException:
            self._abandon()
            raise

    def _form_units(self) -> tuple[str, ...]:
        assert self._intake is not None
        controls = self.controls()
        if controls.intake_paused:
            return ()
        candidates = [
            UnitCandidate(
                item.source_id, item.oldest_ready_at, item.oldest_intake_file_id, item.count
            )
            for item in self._intake.ready_sources(scan_session_id=self._session_id)
            if controls.intake_allowed(item.source_id)
        ]
        planned = plan_units(
            candidates,
            now=self._clock(),
            policy=self._policy,
            limit=self._limits.max_units_per_poll,
        )
        created: list[str] = []
        for unit in planned:
            items = self._intake.ready_items(
                scan_session_id=self._session_id, source_id=unit.source_id, limit=unit.take
            )
            if not items:
                continue
            try:
                registration = self._intake.register(
                    scan_session_id=self._session_id,
                    source_id=unit.source_id,
                    intake_file_ids=[item.intake_file_id for item in items],
                    identity=self._identity,
                    settings={
                        scan_recovery.WORK_UNIT_SETTING: True,
                        ENGINE_SETTING: ENGINE_NAME,
                    },
                    started_by=self._started_by,
                    seal=True,
                )
            except OMRScannerError as exc:
                self._last_error = exc.user_message or str(exc)
                _LOGGER.warning("Could not register a unit of source %s: %s", unit.source_id, exc)
                continue
            if registration.failed:
                self._last_error = (
                    f"{len(registration.failed)} file(s) could not be copied into the project; "
                    "they stay ready and are retried."
                )
            if registration.batch_id is not None:
                created.append(registration.batch_id)
                self._runnable_cache = None
                self._counters.units_registered += 1
                self._hooks.registered(
                    registration.batch_id, [scan for _intake, scan in registration.registered]
                )
                _LOGGER.info(
                    "Unit %s registered from source %s: %d sheet(s), %d duplicate(s)",
                    registration.batch_id[:8],
                    unit.source_id[:8],
                    len(registration.registered),
                    len(registration.duplicates),
                )
        return tuple(created)

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    def step(self, *, wait: float = 0.0) -> StepReport:
        """One bounded iteration: collect, commit, finalise, claim, submit.

        Args:
            wait: Longest to wait for the first finished sheet when some are
                in flight (``0``: never block - the deterministic test mode).
        """
        if self._state not in (EngineState.RUNNING, EngineState.STOPPING):
            return StepReport()
        try:
            return self._step(wait=wait)
        except BaseException:
            self._abandon()
            raise

    def _step(self, *, wait: float) -> StepReport:
        recognised = released = 0
        outstanding = len(self._in_flight) - len(self._buffer)
        done = self._recogniser.poll(wait if outstanding > 0 else 0.0)
        retry: list[Claim] = []
        for item in done:
            claim = self._in_flight.get(item.ticket)
            if claim is None:  # pragma: no cover - a ticket this engine never issued
                _LOGGER.error("Recogniser returned unknown ticket %s", item.ticket)
                continue
            retries = self._worker_lost[claim.scan_id]
            if item.worker_lost and retries < self._limits.infrastructure_retries:
                self._worker_lost[claim.scan_id] += 1
                self._counters.worker_lost += 1
                retry.append(claim)
                continue
            self._hooks.recognised(claim)
            self._buffer.append((claim, _processed(item)))
            recognised += 1
        if retry:
            released += self._release(retry)

        committed = self._commit_all()
        finalised = self._finalise(force=False)
        claimed = 0
        if (
            self._state is EngineState.RUNNING
            and not self._scheduling_paused
            and self._recogniser.accepting
            and self.controls().processing_allowed
        ):
            claimed = self._claim_and_submit()
        return StepReport(
            recognised=recognised,
            committed=committed,
            claimed=claimed,
            released=released,
            finalised=finalised,
        )

    def run_until_idle(self, *, max_steps: int = 1_000_000, wait: float = 0.05) -> int:
        """Poll, register and step until caught up (or ``max_steps``). Returns steps taken.

        For tests and scripted runs: "idle" is :attr:`EngineStatus.caught_up`
        for this engine's view - nothing in flight, nothing claimable, nothing
        ready to register. A trickle unit waits for its timeout on the
        injected clock, so a test advances the clock rather than sleeping.
        """
        steps = 0
        while steps < max_steps and self._state is EngineState.RUNNING:
            self.poll_intake()
            self.form_units()
            report = self.step(wait=wait)
            steps += 1
            if (
                report.idle
                and not self._in_flight
                and (not self._may_claim() or not self._claimable_remaining())
            ):
                # Nothing more will happen without new files - or, while the
                # operator has paused or stopped processing, without a resume.
                break
        return steps

    def _may_claim(self) -> bool:
        return not self._scheduling_paused and self.controls().processing_allowed

    def run(
        self,
        should_stop: Callable[[], bool],
        *,
        idle_sleep: float = 0.5,
        wait: float = 0.2,
        sleeper: Callable[[float], None] = time.sleep,
        drain_on_stop: bool = True,
    ) -> EngineStatus:
        """The thin continuous loop: poll intake, form units, step, until told to stop.

        No logic of its own - every decision is in the methods it calls, which
        the tests exercise directly. A thread or the GUI (phase 8) runs this.
        Shuts down (draining by default) when ``should_stop`` returns true.
        """
        if self._state is EngineState.NEW:
            self.start()
        while not should_stop() and self._state is EngineState.RUNNING:
            self.poll_intake()
            self.form_units()
            report = self.step(wait=wait)
            if report.idle and not self._in_flight:
                sleeper(idle_sleep)
        return self.shutdown(drain=drain_on_stop)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    RUNNABLE_REFRESH_SECONDS = 1.0
    """How long the list of runnable batches is reused between steps. Units
    this engine registers or finishes invalidate it at once; a batch another
    part of the application adds (a *Reprocess All*) is seen within this."""

    def _runnable_batches(self) -> list[str]:
        """This session's batches the engine may claim from, oldest first (briefly cached)."""
        cached = self._runnable_cache
        if cached is not None and time.monotonic() - cached[0] < self.RUNNABLE_REFRESH_SECONDS:
            return list(cached[1])
        runnable = self._find_runnable_batches()
        self._runnable_cache = (time.monotonic(), tuple(runnable))
        return runnable

    def _find_runnable_batches(self) -> list[str]:
        runnable: list[str] = []
        for info in scan_sessions.batches_of(self._database, self._session_id):
            batch_id = info.batch_id
            if info.is_superseded:
                self._skipped[batch_id] = "superseded"
                continue
            if info.status == BatchStatus.RUNNING.value and batch_id not in self._owned:
                self._skipped[batch_id] = "running in another coordinator"
                continue
            if batch_id not in self._compatible:
                verdict = batch_store.check_compatibility(self._database, batch_id, self._identity)
                if not verdict.compatible:
                    self._skipped[batch_id] = verdict.summary
                    continue
                self._compatible.add(batch_id)
            self._skipped.pop(batch_id, None)
            runnable.append(batch_id)
        return runnable

    def _claimable_remaining(self) -> bool:
        runnable = self._runnable_batches()
        if not runnable:
            return False
        with self._database.session() as session:
            return bool(
                session.scalar(
                    select(func.count())
                    .select_from(BatchScan)
                    .where(BatchScan.batch_id.in_(runnable))
                    .where(BatchScan.status.in_(CLAIMABLE))
                )
            )

    def _prepare(self, runnable: Sequence[str]) -> None:
        """Give a batch intake did not register its registration step, once.

        A unit registered by intake carries every sheet's verified hash and its
        duplicate links. A batch registered otherwise - *Add Folder*,
        *Reprocess All*, a resumed batch of an earlier build - gets exactly the
        step the finite Scan stage's worker runs before reading: the manual
        source's ledger and content hashes
        (:func:`~omr_scanner.services.intake.record_manual_batch`) and phase 4's
        exact-duplicate rule (:func:`~omr_scanner.services.intake.link_exact_duplicates`).
        Both are idempotent. Never fatal: like the finite path, a batch whose
        hashing fails is still read.
        """
        due = [batch_id for batch_id in runnable if batch_id not in self._prepared]
        if not due:
            return
        with self._database.session() as session:
            unhashed = {
                str(batch_id)
                for (batch_id,) in session.execute(
                    select(BatchScan.batch_id)
                    .where(BatchScan.batch_id.in_(due))
                    .where(BatchScan.status.in_(CLAIMABLE))
                    .where(BatchScan.content_sha256 == "")
                    .distinct()
                ).all()
            }
        for batch_id in due:
            if batch_id in unhashed:
                try:
                    intake_service.record_manual_batch(self._database, batch_id)
                    intake_service.link_exact_duplicates(self._database, batch_id)
                except Exception as exc:
                    self._last_error = f"Hashing unit {batch_id[:8]} failed: {exc}"
                    _LOGGER.exception("Registration step for batch %s failed", batch_id)
            self._prepared.add(batch_id)

    def _claim_and_submit(self) -> int:
        free = self._limits.max_in_flight - len(self._in_flight)
        if free <= 0:
            return 0
        runnable = self._runnable_batches()
        if not runnable:
            return 0
        self._prepare(runnable)
        try:
            claims = claim_scans(self._database, runnable, min(free, self._limits.claim_window))
        except OMRScannerError as exc:
            # Nothing was claimed (the transaction rolled back); try next step.
            self._last_error = f"Claiming work failed: {exc}"
            _LOGGER.warning("Claiming work failed: %s", exc)
            return 0
        if not claims:
            return 0
        self._owned.update(claim.batch_id for claim in claims)
        for claim in claims:
            self._in_flight[claim.scan_id] = claim
        self._hooks.claimed(claims)
        for claim in claims:
            self._recogniser.submit(claim.scan_id, claim.path)
            self._counters.submitted += 1
            self._hooks.submitted(claim)
        return len(claims)

    def _release(self, claims: Sequence[Claim]) -> int:
        if not claims:
            return 0
        try:
            changed = release_claims(self._database, [claim.scan_id for claim in claims])
        except OMRScannerError as exc:
            # The rows stay `processing`; recovery returns them on next start.
            self._last_error = f"Releasing claims failed: {exc}"
            _LOGGER.exception("Releasing %d claim(s) failed", len(claims))
            return 0
        for claim in claims:
            self._in_flight.pop(claim.scan_id, None)
        self._counters.released += changed
        self._hooks.released(claims)
        return changed

    def _commit_all(self) -> int:
        """Commit every buffered result, one work-unit transaction per group."""
        committed = 0
        while self._buffer and self._state is not EngineState.FAULTED:
            batch_id = self._buffer[0][0].batch_id
            group = [
                item for item in self._buffer if item[0].batch_id == batch_id
            ][: self._limits.max_commit_group]
            claims = [claim for claim, _processed in group]
            self._hooks.before_commit(batch_id, claims)
            try:
                applied = batch_store.record_results(
                    self._database,
                    batch_id,
                    [processed for _claim, processed in group],
                    template=self._template,
                    claimed_only=True,
                )
            except Exception as exc:
                # Deliberately broad, as BatchRecorder: the transaction rolled
                # back, nothing of the group is durable, the results stay
                # buffered and are retried; the rows stay `processing`.
                self._counters.writer_failures += 1
                self._last_error = f"Results could not be saved: {exc}"
                _LOGGER.exception(
                    "Could not commit %d result(s) of unit %s (attempt %d)",
                    len(group),
                    batch_id[:8],
                    self._counters.writer_failures,
                )
                if self._counters.writer_failures >= self._limits.writer_retry_limit:
                    self._state = EngineState.FAULTED
                    _LOGGER.error("Continuous engine faulted: the writer keeps failing")
                break
            self._counters.writer_failures = 0
            self._hooks.committed(batch_id, claims)
            # Acknowledgement - only now does the engine count these sheets.
            done = {claim.scan_id for claim in claims}
            self._buffer = [item for item in self._buffer if item[0].scan_id not in done]
            for claim in claims:
                self._in_flight.pop(claim.scan_id, None)
                self._worker_lost.pop(claim.scan_id, None)
            if applied != len(group):
                _LOGGER.warning(
                    "Unit %s: %d of %d result(s) were not written - their rows were no "
                    "longer claimed by this engine",
                    batch_id[:8],
                    len(group) - applied,
                    len(group),
                )
            self._counters.committed += applied
            committed += applied
            self._sync_duplicates(batch_id, claims)
        return committed

    def _sync_duplicates(self, batch_id: str, claims: Sequence[Claim]) -> None:
        """Make the session-wide duplicate Student-ID state current for just-committed sheets.

        Revised phase 7: incremental, per commit group, instead of only when a
        whole unit (up to 200 sheets) finishes - so a duplicate between a sheet
        read now and one read hours ago in another unit or from another source
        is in Resolve's queue within one commit. **Bounded**: only the
        identifier groups these sheets belong to (or are leaving) are
        re-derived (:func:`~omr_scanner.services.review_store.sync_duplicate_identifiers_for`,
        the one duplicate system - human-touched decisions are kept).

        Its own short transaction, after the work unit committed. The gap is
        recoverable by construction: the unit is still ``running`` until its
        batch-scope pass has run, so a kill in between leaves exactly what
        :func:`~omr_scanner.services.scan_recovery.recover_on_open` completes -
        from stored results, without reading any sheet again. A failure here
        is logged and left to that same unit-end pass.
        """
        self._hooks.syncing_duplicates(batch_id, claims)
        try:
            review_store.sync_duplicate_identifiers_for(
                self._database, [claim.scan_id for claim in claims]
            )
        except OMRScannerError as exc:
            self._last_error = f"Duplicate check after a commit failed: {exc}"
            _LOGGER.exception("Incremental duplicate sync for unit %s failed", batch_id[:8])

    def _finalise(self, *, force: bool) -> tuple[str, ...]:
        """Let each finished unit this engine touched leave ``running``.

        A unit is finished when no row is unfinished and this engine holds
        nothing of it. ``force`` (shutdown): every unit this engine holds
        nothing of, finished or not - an unfinished one becomes
        ``interrupted``, exactly as recovery would leave it, without a
        processing manifest (the run did not finish).
        """
        if not self._owned:
            return ()
        held = {claim.batch_id for claim in self._in_flight.values()}
        candidates = sorted(self._owned - held)
        if not candidates:
            return ()
        with self._database.session() as session:
            unfinished = {
                str(batch_id)
                for (batch_id,) in session.execute(
                    select(BatchScan.batch_id)
                    .where(BatchScan.batch_id.in_(candidates))
                    .where(BatchScan.status.in_(UNFINISHED))
                    .distinct()
                ).all()
            }
        finalised: list[str] = []
        for batch_id in candidates:
            if batch_id in unfinished and not force:
                continue
            try:
                self._hooks.finalising(batch_id)
                scan_recovery.complete_batch_review_state(self._database, batch_id)
                if batch_id in unfinished:
                    batch_store.set_batch_status(
                        self._database, batch_id, BatchStatus.INTERRUPTED
                    )
                    status = BatchStatus.INTERRUPTED.value
                else:
                    summary = batch_store.finalise_batch(self._database, batch_id)
                    status = summary.status if summary is not None else ""
            except OMRScannerError as exc:
                # The unit stays `running`: completed later, or by recovery.
                self._last_error = f"Finishing unit {batch_id[:8]} failed: {exc}"
                _LOGGER.exception("Finishing unit %s failed", batch_id)
                continue
            self._owned.discard(batch_id)
            self._runnable_cache = None
            self._hooks.finalised(batch_id, status)
            finalised.append(batch_id)
            _LOGGER.info("Unit %s finished: %s", batch_id[:8], status)
        return tuple(finalised)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def status(self) -> EngineStatus:
        """An immutable snapshot (bounded: a few grouped queries)."""
        batches = scan_sessions.batches_of(self._database, self._session_id)
        batch_ids = tuple(item.batch_id for item in batches)
        counts: dict[str, int] = {}
        if batch_ids:
            with self._database.session() as session:
                counts = {
                    str(status): int(count)
                    for status, count in session.execute(
                        select(BatchScan.status, func.count())
                        .where(BatchScan.batch_id.in_(list(batch_ids)))
                        .group_by(BatchScan.status)
                    ).all()
                }
        ready = 0
        if self._intake is not None:
            ready = sum(
                item.count for item in self._intake.ready_sources(scan_session_id=self._session_id)
            )
        def count(*statuses: ScanJobStatus) -> int:
            return sum(counts.get(item.value, 0) for item in statuses)

        controls = self.controls()
        caught_up = (
            self._state is EngineState.RUNNING
            and not self._in_flight
            and not self._owned
            and ready == 0
            and not self._claimable_remaining()
        )
        return EngineStatus(
            state=self._state,
            scan_session_id=self._session_id,
            batch_ids=batch_ids,
            registered=sum(counts.values()),
            pending=count(ScanJobStatus.PENDING, ScanJobStatus.CANCELLED),
            processing=count(ScanJobStatus.QUEUED, ScanJobStatus.PROCESSING),
            completed=count(ScanJobStatus.COMPLETED, ScanJobStatus.WARNING),
            failed=count(ScanJobStatus.FAILED),
            duplicates=count(ScanJobStatus.DUPLICATE),
            in_flight=len(self._in_flight),
            writer_backlog=len(self._buffer),
            ready_intake=ready,
            units_registered=self._counters.units_registered,
            sheets_submitted=self._counters.submitted,
            sheets_committed=self._counters.committed,
            intake_paused=self._intake_paused or controls.intake_paused,
            scheduling_paused=self._scheduling_paused or not controls.processing_allowed,
            skipped_batches=tuple(sorted(self._skipped.items())),
            last_error=self._last_error,
            caught_up=caught_up,
            processing_intent=controls.processing,
        )


def _processed(item: RecognitionDone) -> ProcessedScan:
    """A worker's result as the work unit records it (the engine renames nothing).

    Exactly what :func:`~omr_scanner.services.batch_processor.finalise_scan`
    produces for a run that does not rename, so the stored row is identical to
    the finite path's.
    """
    return ProcessedScan(result=item.result)


__all__ = [
    "CLAIMABLE",
    "ENGINE_NAME",
    "ENGINE_SETTING",
    "Claim",
    "ContinuousEngine",
    "EngineHooks",
    "EngineStatus",
    "StartupReport",
    "StepReport",
    "claim_scans",
    "release_claims",
]
