"""The session snapshot: one scan session's progress from bounded grouped queries (revised phase 7).

Purpose:
    :func:`take_snapshot` returns an immutable
    :class:`~omr_scanner.domain.session_snapshot.SessionSnapshot` - the
    partitioning counts, the three progress lines, the caught-up predicate with
    source reachability, per-source state and the registration-failure rate
    alarm (``ARCHITECTURE_NOTES.md`` §14.1-14.2) - for phase 8 to poll on a
    timer. Read-only: it never writes.

Bounded, by construction:
    A fixed number of grouped SQL statements whatever the session's size
    (about fifteen; :attr:`SessionSnapshot.query_count` says exactly). Sheets
    are **grouped in SQL** by ``(batch, status, lifecycle state, has an
    unresolved conflict, has an unanswered suggested rescan, needs a retry)``
    and each *group* is classified with the canonical rule
    (:func:`omr_scanner.domain.session_population.classify`) - never one Python
    object per sheet, never an image. The few sheets in an explicit rescan
    lineage, whose session can depend on another session, are classified by
    the canonical bounded path
    (:func:`omr_scanner.services.session_population.sheets_of_session`).

One definition each:
    * which sheets count: ``classify`` (the effective-set rule of
      :mod:`omr_scanner.services.session_population`);
    * which conflicts are required: ``RESOLUTION_TYPES`` and the open /
      deferred / resolved states the Resolve queue uses (the tests check the
      conflict line against
      :func:`omr_scanner.services.review_store.count_conflicts`);
    * which suggested rescans are unanswered:
      :func:`omr_scanner.services.quality_decisions.outstanding_clause`;
    * the operator's intent: :mod:`omr_scanner.services.session_controls`.

Consistency:
    All statements run inside one read transaction (an explicit SQLite
    ``BEGIN``), so the buckets and the independently counted total describe the
    same instant even while the coordinator commits. A writer waits at most for
    this read (``busy_timeout``); the snapshot never writes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, literal, select

from omr_scanner.database.models import (
    BatchScan,
    BatchStatus,
    BatchSupersession,
    IntakeFile,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
    ScanQualityDecision,
    ScanRejection,
    ScanSession,
)
from omr_scanner.domain.intake import IntakeState, Reachability, SourceKind
from omr_scanner.domain.quality_decision import QualityDecision
from omr_scanner.domain.review import RESOLUTION_TYPES, ConflictState
from omr_scanner.domain.session_controls import ProcessingIntent, SessionControls
from omr_scanner.domain.session_population import (
    READ_STATUSES,
    SheetDisposition,
    SheetFacts,
    classify,
    lineage_roots,
)
from omr_scanner.domain.session_snapshot import (
    DEFAULT_ALARM_POLICY,
    DEFAULT_CAUGHT_UP_POLICY,
    CaughtUpPolicy,
    Partition,
    Progress,
    RegistrationAlarm,
    RegistrationAlarmPolicy,
    SessionActivity,
    SessionSnapshot,
    SourceSnapshot,
)
from omr_scanner.services import intake as intake_service
from omr_scanner.services import quality_decisions, session_controls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase

RATE_WINDOW_SECONDS = 300.0
"""Per-source processing rate is measured over the last five minutes."""

_RESOLUTION_VALUES = tuple(item.value for item in RESOLUTION_TYPES)
_UNRESOLVED = (ConflictState.OPEN.value, ConflictState.DEFERRED.value)
_IN_WORKER = (ScanJobStatus.QUEUED.value, ScanJobStatus.PROCESSING.value)

_LEDGER_BUCKET: dict[str, str] = {
    IntakeState.DISCOVERED.value: "stabilizing",
    IntakeState.STABILIZING.value: "stabilizing",
    IntakeState.READY.value: "ready",
    IntakeState.HELD.value: "held",
    IntakeState.VANISHED.value: "vanished",
    IntakeState.UNREADABLE.value: "unreadable_pending_decision",
    IntakeState.UNSUPPORTED.value: "unreadable_pending_decision",
    IntakeState.DUPLICATE_CONTENT.value: "duplicate",
}
"""An unregistered ledger row's bucket. ``ignored`` is outside the partition;
a ``registered`` row is counted through its ``batch_scan`` instead."""

_DISPOSITION_BUCKET: dict[SheetDisposition, str] = {
    SheetDisposition.REJECTED_PENDING_RESCAN: "rescan_required",
    SheetDisposition.SUPERSEDED_BY_REPLACEMENT: "superseded",
    SheetDisposition.BATCH_SUPERSEDED: "superseded",
    SheetDisposition.EXACT_DUPLICATE: "duplicate",
    SheetDisposition.REIMPORT_OF_REJECTED: "duplicate",
    SheetDisposition.EXCLUDED: "excluded",
    SheetDisposition.DEFERRED: "deferred",
    SheetDisposition.COUNTED_IN_OTHER_SESSION: "counted_elsewhere",
}


def utc_now() -> datetime:
    """The production clock."""
    return datetime.now(UTC)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


@dataclass
class _Run:
    """The read transaction, counting the statements it issues."""

    session: Session
    count: int = 0

    def all(self, statement: Any) -> list[Any]:
        self.count += 1
        return list(self.session.execute(statement).all())

    def scalar(self, statement: Any) -> Any:
        self.count += 1
        return self.session.scalar(statement)


@dataclass
class _Sheets:
    """Classified sheet groups, accumulated."""

    buckets: Counter[str] = field(default_factory=Counter)
    dispositions: Counter[SheetDisposition] = field(default_factory=Counter)
    processed: int = 0
    conflicts_open: int = 0
    conflicts_resolved: int = 0
    suggestions: int = 0
    retry: int = 0
    by_batch_registered: Counter[str] = field(default_factory=Counter)
    by_batch_processed: Counter[str] = field(default_factory=Counter)

    def add(
        self,
        *,
        batch_id: str,
        status: str,
        disposition: SheetDisposition,
        conflict: bool,
        suggestion: bool,
        retry: bool,
        count: int,
        unresolved: int,
        resolved: int,
    ) -> None:
        self.dispositions[disposition] += count
        bucket = _bucket(disposition, status, conflict=conflict, suggestion=suggestion)
        self.buckets[bucket] += count
        self.by_batch_registered[batch_id] += count
        if status in READ_STATUSES:
            self.by_batch_processed[batch_id] += count
            if bucket != "duplicate":
                self.processed += count
        if disposition.counts:
            self.conflicts_open += unresolved
            self.conflicts_resolved += resolved
            if suggestion:
                self.suggestions += count
            if retry:
                self.retry += count


def _bucket(
    disposition: SheetDisposition, status: str, *, conflict: bool, suggestion: bool
) -> str:
    """One registered sheet's partition bucket."""
    if disposition is SheetDisposition.NOT_READ:
        return "processing" if status in _IN_WORKER else "queued"
    if disposition.counts:
        if suggestion:
            return "rescan_required"
        if conflict:
            return "conflict"
        return "accepted"
    return _DISPOSITION_BUCKET[disposition]


def _flag_columns() -> tuple[Any, ...]:
    """Per-sheet flags as SQL expressions over ``batch_scan`` (correlated, indexed)."""
    unresolved = (
        select(func.count())
        .select_from(ReviewConflict)
        .where(ReviewConflict.scan_id == BatchScan.scan_id)
        .where(ReviewConflict.conflict_type.in_(_RESOLUTION_VALUES))
        .where(ReviewConflict.state.in_(_UNRESOLVED))
        .scalar_subquery()
    )
    resolved = (
        select(func.count())
        .select_from(ReviewConflict)
        .where(ReviewConflict.scan_id == BatchScan.scan_id)
        .where(ReviewConflict.conflict_type.in_(_RESOLUTION_VALUES))
        .where(ReviewConflict.state == ConflictState.RESOLVED.value)
        .scalar_subquery()
    )
    lifecycle = func.coalesce(
        select(ScanRejection.state)
        .where(ScanRejection.scan_id == BatchScan.scan_id)
        .scalar_subquery(),
        literal("active"),
    )
    suggestion = case((quality_decisions.outstanding_clause(BatchScan.scan_id), 1), else_=0)
    retry = case(
        (
            select(ScanQualityDecision.scan_id)
            .where(ScanQualityDecision.scan_id == BatchScan.scan_id)
            .where(ScanQualityDecision.decision == QualityDecision.RETRY_PROCESSING.value)
            .exists(),
            1,
        ),
        else_=0,
    )
    conflict = case((unresolved > 0, 1), else_=0)
    return lifecycle, conflict, suggestion, retry, unresolved, resolved


def _classify_groups(
    run: _Run,
    scan_session_id: str,
    batches: list[str],
    superseded: set[str],
    linked: set[int],
    with_quality: bool,
) -> _Sheets:
    sheets = _Sheets()
    if not batches:
        return sheets
    lifecycle, conflict, suggestion, retry, unresolved, resolved = _flag_columns()
    if not with_quality:
        suggestion = literal(0)
        retry = literal(0)
    statement = (
        select(
            BatchScan.batch_id,
            BatchScan.status,
            lifecycle,
            conflict,
            suggestion,
            retry,
            func.count(),
            func.coalesce(func.sum(unresolved), 0),
            func.coalesce(func.sum(resolved), 0),
        )
        .where(BatchScan.batch_id.in_(batches))
        .group_by(BatchScan.batch_id, BatchScan.status, lifecycle, conflict, suggestion, retry)
    )
    if linked:
        statement = statement.where(BatchScan.scan_id.not_in(sorted(linked)))
    for row in run.all(statement):
        batch_id, status, state, has_conflict, has_suggestion, needs_retry = row[:6]
        count, open_n, done_n = row[6:]
        facts = SheetFacts(
            scan_id=0,
            batch_id=str(batch_id),
            status=str(status),
            lifecycle=str(state),
            batch_superseded=str(batch_id) in superseded,
        )
        sheets.add(
            batch_id=str(batch_id),
            status=str(status),
            disposition=classify(facts, scan_session_id),
            conflict=bool(has_conflict),
            suggestion=bool(has_suggestion),
            retry=bool(needs_retry),
            count=int(count),
            unresolved=int(open_n or 0),
            resolved=int(done_n or 0),
        )
    return sheets


def _classify_linked(
    database: ProjectDatabase,
    run: _Run,
    scan_session_id: str,
    linked: set[int],
    sheets: _Sheets,
    with_quality: bool,
) -> int:
    """Classify sheets in an explicit rescan lineage (few) with the canonical bounded path.

    Returns how many of them were adopted from another session (rescans read
    there whose lineage counts here).
    """
    from omr_scanner.services import session_population

    if not linked:
        return 0
    classified = session_population.sheets_of_session(database, scan_session_id, linked)
    if not classified.dispositions:
        return 0
    lifecycle, conflict, suggestion, retry, unresolved, resolved = _flag_columns()
    del lifecycle
    if not with_quality:
        suggestion = literal(0)
        retry = literal(0)
    rows = run.all(
        select(
            BatchScan.scan_id,
            BatchScan.status,
            conflict,
            suggestion,
            retry,
            unresolved,
            resolved,
        ).where(BatchScan.scan_id.in_(sorted(classified.dispositions)))
    )
    for scan_id, status, has_conflict, has_suggestion, needs_retry, open_n, done_n in rows:
        scan = int(scan_id)
        sheets.add(
            batch_id=classified.batch_of[scan],
            status=str(status),
            disposition=classified.dispositions[scan],
            conflict=bool(has_conflict),
            suggestion=bool(has_suggestion),
            retry=bool(needs_retry),
            count=1,
            unresolved=int(open_n or 0),
            resolved=int(done_n or 0),
        )
    return len(classified.adopted & set(classified.dispositions))


def take_snapshot(
    database: ProjectDatabase,
    scan_session_id: str,
    *,
    now: datetime | None = None,
    caught_up_policy: CaughtUpPolicy = DEFAULT_CAUGHT_UP_POLICY,
    alarm_policy: RegistrationAlarmPolicy = DEFAULT_ALARM_POLICY,
    rate_window_seconds: float = RATE_WINDOW_SECONDS,
) -> SessionSnapshot:
    """The session's progress at ``now``, from a bounded number of grouped queries.

    Read-only. Safe to call from any thread while the coordinator commits
    (each call uses its own connection and one short read transaction).
    """
    moment = now or utc_now()
    with_quality = quality_decisions.has_quality_schema(database)
    controls = session_controls.get_controls(database, scan_session_id)
    sources = [
        source
        for source in intake_service.list_sources(database)
        if source.attached_session_id == scan_session_id
    ]
    with database.session() as session:
        # One read transaction: every count below describes the same instant.
        session.connection().exec_driver_sql("BEGIN")
        run = _Run(session)
        state = run.scalar(
            select(ScanSession.state).where(ScanSession.scan_session_id == scan_session_id)
        )
        batch_rows = run.all(
            select(ScanBatch.batch_id, ScanBatch.status, ScanBatch.source_id)
            .where(ScanBatch.scan_session_id == scan_session_id)
            .order_by(ScanBatch.created_at, ScanBatch.batch_id)
        )
        batches = [str(row[0]) for row in batch_rows]
        source_of = {str(row[0]): (str(row[2]) if row[2] else "") for row in batch_rows}
        running = sum(1 for row in batch_rows if str(row[1]) == BatchStatus.RUNNING.value)
        superseded = {
            str(item)
            for (item,) in run.all(
                select(BatchSupersession.superseded_batch_id).where(
                    BatchSupersession.reversed_at.is_(None)
                )
            )
        }
        links = run.all(
            select(ScanRejection.scan_id, ScanRejection.replacement_scan_id).where(
                ScanRejection.replacement_scan_id.is_not(None)
            )
        )
        lineage = lineage_roots({int(new): int(old) for old, new in links if new is not None})
        linked = set(lineage.root_of) | set(lineage.cycles)
        sheets = _classify_groups(
            run, scan_session_id, batches, superseded, linked, with_quality
        )
        registered_rows = int(
            run.scalar(
                select(func.count())
                .select_from(BatchScan)
                .where(BatchScan.batch_id.in_(batches))
            )
            or 0
        ) if batches else 0
        ledger = run.all(
            select(
                IntakeFile.source_id,
                IntakeFile.state,
                IntakeFile.batch_scan_id.is_not(None),
                func.count(),
            )
            .where(IntakeFile.scan_session_id == scan_session_id)
            .group_by(IntakeFile.source_id, IntakeFile.state, IntakeFile.batch_scan_id.is_not(None))
        ) if intake_service.has_intake_schema(database) else []
        recent = _recent_by_source(run, batches, moment - timedelta(seconds=rate_window_seconds))
        alarms = _alarm_samples(run, batches, alarm_policy.window)
        adopted = _classify_linked(
            database, run, scan_session_id, linked, sheets, with_quality
        )
        query_count = run.count + 1  # + BEGIN

    # --- the ledger side of the partition ------------------------------------
    ledger_buckets: Counter[str] = Counter()
    per_source_ledger: dict[str, Counter[str]] = {}
    ignored = 0
    unregistered_discovered = 0
    for source_id, intake_state, has_scan, count in ledger:
        if has_scan:
            continue  # counted through its batch_scan row
        if intake_state == IntakeState.IGNORED.value:
            ignored += int(count)
            continue
        bucket = _LEDGER_BUCKET.get(str(intake_state))
        if bucket is None:
            continue  # "registered" without a sheet: not discoverable work
        unregistered_discovered += int(count)
        ledger_buckets[bucket] += int(count)
        per_source_ledger.setdefault(str(source_id), Counter())[bucket] += int(count)

    totals = Counter(sheets.buckets)
    totals.update(ledger_buckets)
    partition = Partition(**{name: int(totals.get(name, 0)) for name in Partition.__slots__})
    discovered = unregistered_discovered + registered_rows + adopted

    recognition = Progress(
        done=sheets.processed,
        total=max(0, discovered - partition.duplicate - partition.vanished),
    )
    conflicts = Progress(
        done=sheets.conflicts_resolved,
        total=sheets.conflicts_resolved + sheets.conflicts_open,
    )
    replaced = sheets.dispositions.get(SheetDisposition.SUPERSEDED_BY_REPLACEMENT, 0)
    awaiting = sheets.dispositions.get(SheetDisposition.REJECTED_PENDING_RESCAN, 0)
    rescans = Progress(done=replaced, total=replaced + awaiting + sheets.suggestions)

    # --- per source ----------------------------------------------------------
    processed_by_source: Counter[str] = Counter()
    registered_by_source: Counter[str] = Counter()
    for batch_id, count in sheets.by_batch_registered.items():
        registered_by_source[source_of.get(batch_id, "")] += count
    for batch_id, count in sheets.by_batch_processed.items():
        processed_by_source[source_of.get(batch_id, "")] += count
    known = {source.source_id for source in sources}
    extra = [
        source
        for source in intake_service.list_sources(database)
        if source.source_id not in known
        and (source.source_id in per_source_ledger or source.source_id in registered_by_source)
    ]
    snapshots: list[SourceSnapshot] = []
    for source in [*sources, *extra]:
        counts = per_source_ledger.get(source.source_id, Counter())
        last = _aware(source.last_reconciled_at)
        allowance = caught_up_policy.allowance_seconds(source.policy.poll_interval_seconds)
        recently = last is not None and (moment - last).total_seconds() <= allowance
        processed = processed_by_source.get(source.source_id, 0)
        samples, failures = alarms.get(source.source_id, (0, 0))
        snapshots.append(
            SourceSnapshot(
                source_id=source.source_id,
                label=source.label,
                kind=source.kind.value,
                enabled=source.enabled,
                intake_paused=not controls.intake_allowed(source.source_id),
                reachability=source.reachability.value,
                reachable=source.reachability is Reachability.ONLINE,
                last_reconciled_at=last,
                reconciled_recently=bool(recently),
                stabilizing=counts.get("stabilizing", 0),
                ready=counts.get("ready", 0),
                held=counts.get("held", 0),
                unreadable=counts.get("unreadable_pending_decision", 0),
                vanished=counts.get("vanished", 0),
                registered=registered_by_source.get(source.source_id, 0),
                processed=processed,
                recent_processed=recent.get(source.source_id, 0),
                rate_per_minute=(
                    recent.get(source.source_id, 0) / (rate_window_seconds / 60.0)
                    if processed
                    else None
                ),
                alarm=alarm_policy.evaluate(samples, failures)
                if source.kind is SourceKind.WATCHED or samples
                else None,
            )
        )

    watched = [
        item for item in snapshots
        if item.kind == SourceKind.WATCHED.value and item.source_id in known
    ]
    disabled = tuple(item.label for item in watched if not item.enabled)
    unreachable = tuple(
        item.label for item in watched if item.enabled and not item.reachable
    )
    activity = _activity(
        state=str(state or ""),
        controls=controls,
        watched=watched,
        partition=partition,
        running=running,
    )
    return SessionSnapshot(
        scan_session_id=scan_session_id,
        session_state=str(state or ""),
        taken_at=moment,
        activity=activity,
        caught_up=activity is SessionActivity.CAUGHT_UP,
        partition=partition,
        discovered_excluding_ignored=discovered,
        ignored=ignored,
        recognition=recognition,
        conflicts=conflicts,
        rescans=rescans,
        outstanding_suggestions=sheets.suggestions,
        retry_processing=sheets.retry,
        pending_decisions=partition.held + partition.unreadable_pending_decision,
        controls=controls,
        sources=tuple(snapshots),
        disabled_sources=disabled,
        unreachable_sources=unreachable,
        running_batches=running,
        query_count=query_count,
    )


def _activity(
    *,
    state: str,
    controls: SessionControls,
    watched: Iterable[SourceSnapshot],
    partition: Partition,
    running: int,
) -> SessionActivity:
    """What the session is doing - the first rule that applies.

    *Caught up* needs every one of: open; recognition running; intake on for
    the session and every enabled source; nothing stabilising, ready, queued or
    being read, no unit still ``running``; every enabled source reachable and
    reconciled within its allowance. Disabled sources are not watched and do
    not count either way (they are listed by name). A quiet period never closes
    a session: caught up is not complete.
    """
    if state == "closed":
        return SessionActivity.CLOSED
    if controls.processing is ProcessingIntent.STOPPED:
        return SessionActivity.PROCESSING_STOPPED
    if controls.processing is ProcessingIntent.PAUSED:
        return SessionActivity.PROCESSING_PAUSED
    enabled = [item for item in watched if item.enabled]
    if any(not item.reachable and not item.intake_paused for item in enabled):
        return SessionActivity.WAITING_FOR_SOURCE
    if (
        partition.stabilizing
        or partition.ready
        or partition.queued
        or partition.processing
        or running
    ):
        return SessionActivity.PROCESSING
    if controls.intake_paused or any(item.intake_paused for item in enabled):
        return SessionActivity.INTAKE_PAUSED
    if any(not item.reconciled_recently for item in enabled):
        return SessionActivity.CHECKING_SOURCES
    return SessionActivity.CAUGHT_UP


def _recent_by_source(run: _Run, batches: list[str], since: datetime) -> dict[str, int]:
    """Sheets read since ``since``, per source (one grouped query)."""
    if not batches:
        return {}
    rows = run.all(
        select(ScanBatch.source_id, func.count())
        .join(BatchScan, BatchScan.batch_id == ScanBatch.batch_id)
        .where(ScanBatch.batch_id.in_(batches))
        .where(BatchScan.status.in_(sorted(READ_STATUSES)))
        .where(BatchScan.finished_at >= since)
        .group_by(ScanBatch.source_id)
    )
    return {str(source): int(count) for source, count in rows if source}


def _alarm_samples(run: _Run, batches: list[str], window: int) -> dict[str, tuple[int, int]]:
    """Per source: ``(samples, registration failures)`` among its latest ``window`` read sheets.

    The evidence is each sheet's stored recognition outcome
    (``registration_failed``) - no new measurement. One windowed query.
    """
    if not batches:
        return {}
    rank = (
        func.row_number()
        .over(
            partition_by=ScanBatch.source_id,
            order_by=(BatchScan.finished_at.desc(), BatchScan.scan_id.desc()),
        )
        .label("rank")
    )
    failed = case((BatchScan.outcome == "registration_failed", 1), else_=0).label("failed")
    latest = (
        select(ScanBatch.source_id.label("source_id"), failed, rank)
        .join(BatchScan, BatchScan.batch_id == ScanBatch.batch_id)
        .where(ScanBatch.batch_id.in_(batches))
        .where(ScanBatch.source_id.is_not(None))
        .where(BatchScan.status.in_(sorted(READ_STATUSES)))
        .subquery()
    )
    rows = run.all(
        select(latest.c.source_id, func.count(), func.sum(latest.c.failed))
        .where(latest.c.rank <= window)
        .group_by(latest.c.source_id)
    )
    return {str(source): (int(count), int(fails or 0)) for source, count, fails in rows}


def registration_alarm(
    database: ProjectDatabase,
    scan_session_id: str,
    source_id: str,
    *,
    policy: RegistrationAlarmPolicy = DEFAULT_ALARM_POLICY,
) -> RegistrationAlarm:
    """One source's alarm state, without the rest of the snapshot."""
    with database.session() as session:
        run = _Run(session)
        batches = [
            str(item)
            for (item,) in run.all(
                select(ScanBatch.batch_id).where(ScanBatch.scan_session_id == scan_session_id)
            )
        ]
        samples, failures = _alarm_samples(run, batches, policy.window).get(source_id, (0, 0))
    return policy.evaluate(samples, failures)


__all__ = ["RATE_WINDOW_SECONDS", "registration_alarm", "take_snapshot", "utc_now"]
