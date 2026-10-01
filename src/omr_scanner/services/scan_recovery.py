"""Crash-safe Scan / Resolve recovery on project open (0.1.1 phase 3).

Purpose:
    Bring a project left behind by an abnormal termination - a forced kill, a
    crash, a power cut - back to a state in which everything the operator was
    shown as done is still done, everything that was not is visibly still to
    do, and nothing has been read twice. See
    ``docs/decisions/ADR-0006-crash-safe-scan-work-units.md``.

The durable Scan work unit (what "a sheet is completed" means):
    A sheet's recognition result **and** the conflicts that result implies for
    that sheet are committed in **one** transaction
    (:func:`omr_scanner.services.batch_store.record_results` with a template).
    Batch-scope review state - re-import links, duplicate Student IDs,
    undefined set codes - depends on the whole batch, so it is recomputed
    (idempotently) by :func:`complete_batch_review_state` when a run ends and
    **before** the batch's status leaves ``running``. A batch still ``running``
    when a project is opened therefore means exactly "its batch-scope review
    state may be incomplete", which is what :func:`recover_on_open` repairs.
    No marker column was needed: the batch status already carries the fact.

Responsibilities:
    * :func:`complete_batch_review_state` - the batch-scope passes, in order.
    * :func:`recover_on_open` - the repair ordering run once per writable open.
    * :func:`restore_targets` - which batch Scan and Resolve show on reopen.
    * :func:`scan_progress` / :func:`resolve_progress` - progress derived from
      committed rows with count queries (never a stored percentage).

What does NOT belong here:
    * Recognition. Nothing in this module reads an image: recovery works from
      stored ``result_json`` only, so a committed sheet is never recognised
      again by recovery.
    * Lifecycle transitions. Recovery never creates, closes, seals or reopens
      a session, never creates a batch and never records a supersession - a
      crash is not a lifecycle event. Those belong to
      :mod:`omr_scanner.services.scan_sessions`.
    * Starting a run. Reopening a project only reconstructs state; Resume is
      still the operator's command.
    * Qt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import exists, func, select, update

from omr_scanner.database.models import (
    BatchScan,
    BatchStatus,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import review_store, scan_lifecycle, scan_sessions
from omr_scanner.services.recognition_models import ScanResult

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.review import ReviewCounts
    from omr_scanner.domain.template import OmrTemplate

_LOGGER = logging.getLogger(__name__)

RESYNC_WINDOW = 500
"""Stored results re-derived per transaction during recovery. Bounds memory
to one window of decoded results whatever the batch size."""

_STALE = (ScanJobStatus.QUEUED.value, ScanJobStatus.PROCESSING.value)
_RESUMABLE = tuple(status.value for status in ScanJobStatus if status.is_resumable)


def _now() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------------------------------
# The batch-scope half of the work unit
# ----------------------------------------------------------------------
def complete_batch_review_state(database: ProjectDatabase, batch_id: str) -> int:
    """Run the batch-scope review passes for ``batch_id``. Idempotent.

    Returns:
        How many duplicate-identifier conflicts the batch now has.

    Order matters and is the Scan stage's historical order: a re-import of a
    rejected scan's exact bytes is linked back to it *before* duplicate
    detection, so it does not take part. Called when a run ends - before the
    batch's status is settled - and by :func:`recover_on_open` for a batch a
    crash left ``running``. Re-running it on unchanged data writes nothing.
    """
    scan_lifecycle.sync_reimports(database, batch_id)
    duplicates = review_store.sync_duplicate_identifiers(database, batch_id)
    review_store.sync_undefined_set_codes(database, batch_id)
    return duplicates


# ----------------------------------------------------------------------
# Recovery
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BatchRecovery:
    """What recovery did to one batch.

    Attributes:
        batch_id: The batch.
        was_running: The batch was ``running`` - a run was interrupted.
        scans_returned: ``queued``/``processing`` rows returned to ``pending``.
        sheets_resynced: Stored results whose per-sheet review state was
            re-derived (no recognition; from ``result_json``).
        template_found: Whether a template matching the batch's geometry was
            available for that re-derivation.
        status_after: The batch status recovery left.
    """

    batch_id: str
    was_running: bool
    scans_returned: int = 0
    sheets_resynced: int = 0
    template_found: bool = True
    status_after: str = ""


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Everything :func:`recover_on_open` repaired."""

    batches: tuple[BatchRecovery, ...] = ()
    stray_scans_returned: int = 0
    warnings: tuple[str, ...] = field(default=())

    @property
    def scans_returned(self) -> int:
        """Every stale row returned to ``pending``."""
        return self.stray_scans_returned + sum(item.scans_returned for item in self.batches)

    @property
    def interrupted_batches(self) -> int:
        """Batches a crash had left ``running``."""
        return sum(1 for item in self.batches if item.was_running)

    @property
    def changed_anything(self) -> bool:
        """Whether recovery had anything to do."""
        return bool(self.scans_returned or self.batches)


def _template_for(
    batch: ScanBatch, candidates: Sequence[OmrTemplate]
) -> OmrTemplate | None:
    """The template a batch was read with, matched by geometry fingerprint.

    The batch's own recorded path first, then whatever the caller offers (the
    project's active template). Never a template whose geometry differs:
    deriving conflicts against other zones would invent review state.
    """
    from omr_scanner.services.template_service import load_template

    if batch.template_path:
        try:
            recorded = load_template(Path(batch.template_path))
        except OMRScannerError:
            recorded = None
        if recorded is not None and recorded.geometry_fingerprint() == batch.geometry_fingerprint:
            return recorded
    for candidate in candidates:
        if candidate.geometry_fingerprint() == batch.geometry_fingerprint:
            return candidate
    return None


def _resync_rows(
    database: ProjectDatabase,
    batch_id: str,
    template: OmrTemplate,
    *,
    only_failed_without_conflicts: bool,
) -> int:
    """Re-derive per-sheet conflicts from stored results, one window at a time.

    Uses the exact function the live work unit uses
    (:func:`review_store.sync_conflicts_in_session`), so a sheet whose state
    is already complete is left byte-for-byte unchanged - no event, no row.
    """
    resynced = 0
    cursor = -1
    while True:
        with database.session() as session:
            statement = (
                select(BatchScan.scan_id, BatchScan.filename, BatchScan.result_json)
                .where(BatchScan.batch_id == batch_id)
                .where(BatchScan.result_json != "")
                .where(BatchScan.scan_id > cursor)
                .order_by(BatchScan.scan_id)
                .limit(RESYNC_WINDOW)
            )
            if only_failed_without_conflicts:
                statement = statement.where(
                    BatchScan.status == ScanJobStatus.FAILED.value
                ).where(~exists().where(ReviewConflict.scan_id == BatchScan.scan_id))
            window = session.execute(statement).all()
            if not window:
                return resynced
            for scan_id, filename, payload in window:
                cursor = int(scan_id)
                try:
                    result = ScanResult.from_dict(json.loads(payload))
                except (ValueError, TypeError, KeyError):
                    _LOGGER.exception("Stored result for %s could not be decoded", filename)
                    continue
                review_store.sync_conflicts_in_session(
                    session,
                    batch_id=batch_id,
                    scan_id=int(scan_id),
                    result=result,
                    template=template,
                )
                resynced += 1


def _status_from_rows(database: ProjectDatabase, batch_id: str) -> BatchStatus:
    """``interrupted`` while work remains; otherwise the terminal status the rows imply."""
    with database.session() as session:
        counts: dict[str, int] = {
            str(status): int(count)
            for status, count in session.execute(
                select(BatchScan.status, func.count())
                .where(BatchScan.batch_id == batch_id)
                .group_by(BatchScan.status)
            ).all()
        }
    if any(counts.get(status, 0) for status in _RESUMABLE):
        return BatchStatus.INTERRUPTED
    if counts.get(ScanJobStatus.FAILED.value, 0):
        return BatchStatus.COMPLETED_WITH_ERRORS
    return BatchStatus.COMPLETED


def recover_on_open(
    database: ProjectDatabase, *, templates: Sequence[OmrTemplate] = ()
) -> RecoveryReport:
    """Repair what an abnormal termination left behind. Run once per writable open.

    Args:
        database: The open, writable project database (migrated and
            backfilled by :func:`~omr_scanner.services.project_service.open_project`).
        templates: Templates the caller can offer for re-deriving review state
            (the project's active template); each is used only for a batch
            whose geometry fingerprint it matches.

    Returns:
        What was repaired. Deterministic: run again on the result, it changes
        nothing.

    For every batch a crash left ``running``, in this order:

    1. ``queued``/``processing`` rows (sheets submitted to a worker, never
       committed) return to ``pending`` - retryable, **never** ``failed``,
       never completed;
    2. every stored result's per-sheet review state is re-derived from
       ``result_json`` (no image is read). For a sheet committed by this build
       it is already complete and nothing is written; for one committed by an
       earlier build - which wrote conflicts only when a whole run ended - the
       missing conflicts are created, once;
    3. the batch-scope passes (:func:`complete_batch_review_state`);
    4. only then the status: ``interrupted`` while work remains, otherwise
       the terminal status its rows imply. Until this step commits the batch
       is still ``running``, so a crash *during* recovery is repaired by the
       next open, from the same starting point.

    Then, for any other batch that has review state, a failed sheet with a
    stored result but no conflict at all (always a crash artefact: a failed
    read always raises exactly one) has its conflict re-derived.

    Never touches lifecycle state: no session or batch is created, sealed,
    closed or reopened and no supersession is recorded. Never writes a
    processing manifest - the interrupted run did not finish, and the next
    real run boundary records one as usual.
    """
    recovered: list[BatchRecovery] = []
    warnings: list[str] = []
    with database.session() as session:
        running = list(
            session.scalars(
                select(ScanBatch)
                .where(ScanBatch.status == BatchStatus.RUNNING.value)
                .order_by(ScanBatch.created_at, ScanBatch.batch_id)
            ).all()
        )
        others = list(
            session.scalars(
                select(ScanBatch)
                .where(ScanBatch.status != BatchStatus.RUNNING.value)
                .where(exists().where(ReviewConflict.batch_id == ScanBatch.batch_id))
                .where(
                    exists()
                    .where(BatchScan.batch_id == ScanBatch.batch_id)
                    .where(BatchScan.status == ScanJobStatus.FAILED.value)
                    .where(BatchScan.result_json != "")
                    .where(~exists().where(ReviewConflict.scan_id == BatchScan.scan_id))
                )
            ).all()
        )

    for batch in running:
        batch_id = batch.batch_id
        with database.session() as session:
            returned = int(
                getattr(
                    session.execute(
                        update(BatchScan)
                        .where(BatchScan.batch_id == batch_id)
                        .where(BatchScan.status.in_(_STALE))
                        .values(status=ScanJobStatus.PENDING.value, started_at=None)
                    ),
                    "rowcount",
                    0,
                )
                or 0
            )
        template = _template_for(batch, templates)
        resynced = 0
        if template is not None:
            resynced = _resync_rows(
                database, batch_id, template, only_failed_without_conflicts=False
            )
        else:
            message = (
                f"Batch {batch_id[:8]}: its template could not be found, so the review "
                "state of sheets read before the interruption could not be re-checked."
            )
            warnings.append(message)
            _LOGGER.warning("%s", message)
        complete_batch_review_state(database, batch_id)
        status = _status_from_rows(database, batch_id)
        with database.session() as session:
            session.execute(
                update(ScanBatch)
                .where(ScanBatch.batch_id == batch_id)
                .where(ScanBatch.status == BatchStatus.RUNNING.value)
                .values(status=status.value, updated_at=_now())
            )
        recovered.append(
            BatchRecovery(
                batch_id=batch_id,
                was_running=True,
                scans_returned=returned,
                sheets_resynced=resynced,
                template_found=template is not None,
                status_after=status.value,
            )
        )

    for batch in others:
        template = _template_for(batch, templates)
        if template is None:
            continue
        repaired = _resync_rows(
            database, batch.batch_id, template, only_failed_without_conflicts=True
        )
        if repaired:
            recovered.append(
                BatchRecovery(
                    batch_id=batch.batch_id,
                    was_running=False,
                    sheets_resynced=repaired,
                    status_after=batch.status,
                )
            )

    # A stale row in a batch that is *not* running cannot be produced by this
    # build; an older one could leave it. Same rule: back to pending.
    with database.session() as session:
        stray = int(
            getattr(
                session.execute(
                    update(BatchScan)
                    .where(BatchScan.status.in_(_STALE))
                    .values(status=ScanJobStatus.PENDING.value, started_at=None)
                ),
                "rowcount",
                0,
            )
            or 0
        )

    report = RecoveryReport(
        batches=tuple(recovered), stray_scans_returned=stray, warnings=tuple(warnings)
    )
    if report.changed_anything:
        _LOGGER.info(
            "Recovery on open: %d interrupted batch(es), %d scan(s) returned to pending, "
            "%d stored result(s) re-checked",
            report.interrupted_batches,
            report.scans_returned,
            sum(item.sheets_resynced for item in report.batches),
        )
    return report


# ----------------------------------------------------------------------
# Reconstruction
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScanProgress:
    """A batch's progress, derived from committed rows (never stored).

    Attributes:
        batch_id: The batch.
        total: Members.
        completed: Read cleanly (committed).
        warning: Read, needing review (committed).
        failed: Could not be read (committed; retryable on request).
        pending: Never attempted, cancelled, or returned by recovery.
        status: The batch's stored status.
    """

    batch_id: str
    total: int
    completed: int
    warning: int
    failed: int
    pending: int
    status: str

    @property
    def recognised(self) -> int:
        """Sheets with a committed result, failures included."""
        return self.completed + self.warning + self.failed


def scan_progress(database: ProjectDatabase, batch_id: str) -> ScanProgress | None:
    """Reconstruct a batch's progress with one grouped count query."""
    from omr_scanner.services import batch_store

    summary = batch_store.load_summary(database, batch_id)
    if summary is None:
        return None
    return ScanProgress(
        batch_id=batch_id,
        total=summary.total,
        completed=summary.completed,
        warning=summary.warning,
        failed=summary.failed,
        pending=summary.pending,
        status=summary.status,
    )


def resolve_progress(database: ProjectDatabase, batch_id: str) -> ReviewCounts:
    """Reconstruct a batch's Resolve counts from persisted rows (grouped counts)."""
    return review_store.count_conflicts(database, batch_id)


@dataclass(frozen=True, slots=True)
class RestoreTargets:
    """Which batch each stage shows when a project is reopened.

    Attributes:
        scan_batch_id: The active scan session's newest batch, when it still
            has unfinished members - the batch the Scan stage was holding when
            the application stopped. ``None`` when that batch is finished (the
            next Process All then starts a new batch, as before).
        resolve_batch_id: The batch Resolve opens on: the downstream batch of
            the active session (the single-batch rule until session-level
            aggregation), else the restored Scan batch, else the session's
            newest batch.
    """

    scan_batch_id: str | None = None
    resolve_batch_id: str | None = None


def restore_targets(database: ProjectDatabase) -> RestoreTargets:
    """Decide what Scan and Resolve reconstruct on reopen. Read-only; bounded."""
    current = scan_sessions.active_scan_session(database)
    newest: str | None = None
    if current is not None:
        batches = scan_sessions.batches_of(database, current.scan_session_id)
        if batches:
            newest = max(batches, key=lambda item: (item.created_at, item.batch_id)).batch_id
    else:
        with database.session() as session:
            found = session.scalar(
                select(ScanBatch.batch_id)
                .order_by(ScanBatch.created_at.desc(), ScanBatch.batch_id.desc())
                .limit(1)
            )
        newest = str(found) if found is not None else None

    scan_batch: str | None = None
    if newest is not None:
        with database.session() as session:
            unfinished = session.scalar(
                select(func.count())
                .select_from(BatchScan)
                .where(BatchScan.batch_id == newest)
                .where(BatchScan.status.in_(_RESUMABLE))
            )
        if unfinished:
            scan_batch = newest
    resolve_batch = scan_sessions.downstream_batch_id(database) or scan_batch or newest
    return RestoreTargets(scan_batch_id=scan_batch, resolve_batch_id=resolve_batch)


__all__ = [
    "RESYNC_WINDOW",
    "BatchRecovery",
    "RecoveryReport",
    "RestoreTargets",
    "ScanProgress",
    "complete_batch_review_state",
    "recover_on_open",
    "resolve_progress",
    "restore_targets",
    "scan_progress",
]
