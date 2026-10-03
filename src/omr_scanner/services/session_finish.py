"""Finish scan session: final reconciliation, the complete blocker set, an audited close.

Purpose:
    The backend of *Finish scan session* (0.1.1 revised phase 7,
    ``ACCEPTANCE_CRITERIA.md`` §3, ``ARCHITECTURE_NOTES.md`` §14.3), and the
    named, audited *Reopen*. Phase 8 puts the buttons on it.

:func:`finish_scan_session`, in order:
    1. Refuses a session that is not open (blocker ``session_not_open``).
    2. Takes the project's coordinator lease - unless the caller *is* the
       coordinator (the engine passes ``coordinated=True``). If another
       coordinator is processing, nothing is reconciled and the answer
       includes ``processing_active``.
    3. Decides any read sheet that has no scan-quality decision yet.
    4. **Final reconciliation** of every enabled source attached to the
       session - paused ones included: the operator asked to finish, and a
       reconciliation only lists and stabilises, it never registers. A source
       that cannot be listed is ``source_unreachable``; disabled sources are
       reported by name.
    5. Collects **every** blocker (:class:`~omr_scanner.domain.session_finish.BlockerCode`)
       from one session snapshot (bounded queries) plus the rescan matcher.
    6. Closes only when nothing blocks - or when everything that blocks is an
       outstanding rescan / unmatched replacement / deferred sheet and a named
       operator accepted incomplete results (:class:`IncompleteAcceptance`,
       audited in the close event). The close itself is
       :func:`omr_scanner.services.scan_sessions.close_scan_session`: one
       transaction that seals every open batch, sets ``closed`` and writes the
       audit event - a crash leaves the session either open or closed, never
       half-closed - and that re-checks the Phase C conditions inside.

The lower-level :func:`~omr_scanner.services.scan_sessions.close_scan_session`
keeps its Phase C checks unchanged (the finite workflow's one-step *Close
session and generate final export* still uses it); late files for a session
closed that way are still **held**, never added.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from omr_scanner.domain.intake import SourceKind
from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.domain.session_finish import (
    BlockerCode,
    FinishBlocker,
    FinishOutcome,
    IncompleteAcceptance,
    SourceCheck,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import (
    coordinator,
    quality_decisions,
    review_store,
    scan_lifecycle,
    scan_sessions,
    session_snapshot,
)
from omr_scanner.services import intake as intake_service

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.session_snapshot import SessionSnapshot
    from omr_scanner.services.intake import IntakeService

_LOGGER = logging.getLogger(__name__)


class FinishError(OMRScannerError):
    """*Finish* or *Reopen* was refused outright. Always carries a ``user_message``."""


class _FinishRun:
    """The lease owner for one *Finish* call (held only for its duration)."""


def _final_reconciliation(
    database: ProjectDatabase,
    scan_session_id: str,
    intake: IntakeService | None,
    *,
    reconcile: bool,
) -> tuple[tuple[SourceCheck, ...], list[FinishBlocker], tuple[str, ...]]:
    checks: list[SourceCheck] = []
    blockers: list[FinishBlocker] = []
    disabled: list[str] = []
    for source in intake_service.list_sources(database):
        if source.kind is not SourceKind.WATCHED or source.attached_session_id != scan_session_id:
            continue
        if not source.enabled:
            disabled.append(source.label)
            continue
        if not reconcile or intake is None:
            checks.append(
                SourceCheck(source.source_id, source.label, True, source.reachability.value,
                            "not reconciled")
            )
            blockers.append(
                FinishBlocker(
                    BlockerCode.SOURCE_NOT_RECONCILED,
                    source_id=source.source_id,
                    source_label=source.label,
                    detail="no final reconciliation could run",
                )
            )
            continue
        try:
            report = intake.reconcile(source.source_id)
        except Exception as exc:
            _LOGGER.exception("Final reconciliation of source %s failed", source.source_id)
            checks.append(
                SourceCheck(source.source_id, source.label, True, "error", str(exc))
            )
            blockers.append(
                FinishBlocker(
                    BlockerCode.SOURCE_UNREACHABLE,
                    source_id=source.source_id,
                    source_label=source.label,
                    detail=str(exc),
                )
            )
            continue
        checks.append(
            SourceCheck(
                source.source_id, source.label, True, report.reachability.value, report.detail
            )
        )
        if not report.online:
            blockers.append(
                FinishBlocker(
                    BlockerCode.SOURCE_UNREACHABLE,
                    source_id=source.source_id,
                    source_label=source.label,
                    detail=report.detail or report.reachability.value,
                )
            )
    return tuple(checks), blockers, tuple(disabled)


def _snapshot_blockers(
    database: ProjectDatabase, scan_session_id: str, snapshot: SessionSnapshot
) -> list[FinishBlocker]:
    """Every count-based blocker, from one snapshot and the rescan matcher."""
    partition = snapshot.partition
    found: list[FinishBlocker] = []

    def add(code: BlockerCode, count: int) -> None:
        if count > 0:
            found.append(FinishBlocker(code, count=count))

    add(BlockerCode.FILES_STABILIZING, partition.stabilizing)
    add(BlockerCode.FILES_READY, partition.ready)
    add(BlockerCode.SHEETS_QUEUED, partition.queued)
    add(BlockerCode.SHEETS_PROCESSING, partition.processing)
    add(BlockerCode.UNITS_RUNNING, snapshot.running_batches)
    add(BlockerCode.UNRESOLVED_CONFLICTS, snapshot.conflicts.total - snapshot.conflicts.done)
    awaiting = snapshot.rescans.total - snapshot.rescans.done - snapshot.outstanding_suggestions
    unmatched = 0
    if awaiting > 0:
        unmatched = sum(
            1
            for candidates in scan_lifecycle.session_possible_rescans(
                database, scan_session_id
            ).values()
            if candidates
        )
    add(BlockerCode.RESCAN_OUTSTANDING, awaiting - unmatched)
    add(BlockerCode.REPLACEMENT_UNMATCHED, unmatched)
    add(BlockerCode.RESCAN_SUGGESTED, snapshot.outstanding_suggestions)
    add(BlockerCode.FILES_AWAITING_DECISION, snapshot.pending_decisions)
    add(BlockerCode.SHEETS_DEFERRED, partition.deferred)
    return found


def finish_blockers(
    database: ProjectDatabase, scan_session_id: str
) -> tuple[FinishBlocker, ...]:
    """What would block *Finish* right now, from stored state only (no reconciliation).

    Read-only: for a preview. :func:`finish_scan_session` is the operation.
    """
    snapshot = session_snapshot.take_snapshot(database, scan_session_id)
    if snapshot.session_state != ScanSessionState.OPEN.value:
        return (FinishBlocker(BlockerCode.SESSION_NOT_OPEN),)
    return tuple(_snapshot_blockers(database, scan_session_id, snapshot))


def finish_scan_session(
    database: ProjectDatabase,
    scan_session_id: str,
    *,
    closed_by: str,
    intake: IntakeService | None = None,
    acknowledge: IncompleteAcceptance | None = None,
    reason: str = "",
    coordinated: bool = False,
) -> FinishOutcome:
    """Validate and, if nothing blocks, close a scan session. See the module docstring.

    Args:
        database: The open, writable project.
        scan_session_id: The session.
        closed_by: The named operator finishing it. Required.
        intake: The intake service the final reconciliation runs with. Without
            one, every enabled source is ``source_not_reconciled``.
        acknowledge: An explicit acceptance of incomplete results, or ``None``.
        reason: Recorded with the close.
        coordinated: The caller already holds the project's coordinator lease
            (the continuous engine finishing its own session).

    Returns:
        The outcome: closed, or every blocker. Never only the first.

    Raises:
        FinishError: The session does not exist.
        omr_scanner.services.review_store.ReviewError: No operator named.
    """
    name = review_store.validate_reviewer(closed_by)
    if acknowledge is not None:
        review_store.validate_reviewer(acknowledge.reviewer)
    info = scan_sessions.get_scan_session(database, scan_session_id)
    if info is None:
        raise FinishError(
            f"No scan session {scan_session_id!r}",
            user_message="That scan session no longer exists in this project.",
        )
    if info.state is not ScanSessionState.OPEN:
        return FinishOutcome(
            scan_session_id=scan_session_id,
            closed=False,
            blockers=(FinishBlocker(BlockerCode.SESSION_NOT_OPEN),),
        )
    owner = _FinishRun()
    lease: coordinator.CoordinatorLease | None = None
    busy = False
    if not coordinated:
        try:
            lease = coordinator.acquire(
                database,
                coordinator.CoordinatorKind.SESSION_FINISH,
                owner=owner,
                label=f"finishing session {scan_session_id[:8]}",
            )
        except coordinator.CoordinatorBusyError:
            busy = True
    try:
        if not busy:
            quality_decisions.evaluate_stored(database, scan_session_id)
        checks, source_blockers, disabled = _final_reconciliation(
            database, scan_session_id, intake, reconcile=not busy
        )
        snapshot = session_snapshot.take_snapshot(database, scan_session_id)
        blockers: list[FinishBlocker] = []
        if busy:
            blockers.append(FinishBlocker(BlockerCode.PROCESSING_ACTIVE))
        blockers += _snapshot_blockers(database, scan_session_id, snapshot)
        blockers += source_blockers
        accepted = [item for item in blockers if acknowledge is not None and item.acknowledgeable]
        refused = [item for item in blockers if item not in accepted]
        if refused:
            _LOGGER.info(
                "Finish of session %s refused: %s",
                scan_session_id[:8],
                ", ".join(f"{item.code.value}={item.count}" for item in blockers),
            )
            return FinishOutcome(
                scan_session_id=scan_session_id,
                closed=False,
                blockers=tuple(blockers),
                sources=checks,
                disabled_sources=disabled,
            )
        closer = acknowledge.reviewer if accepted and acknowledge is not None else name
        detail = reason.strip()
        if accepted and acknowledge is not None:
            detail = " ".join(
                part for part in (detail, acknowledge.reason.strip()) if part
            )
        try:
            scan_sessions.close_scan_session(
                database,
                scan_session_id,
                closed_by=closer,
                reason=detail,
                acknowledge_incomplete=bool(accepted),
            )
        except scan_sessions.ScanSessionError as exc:
            # The primitive's own re-check found something the snapshot did
            # not (an operator decision changed in between): nothing closed.
            _LOGGER.info("Close of session %s refused at commit: %s", scan_session_id[:8], exc)
            again = session_snapshot.take_snapshot(database, scan_session_id)
            return FinishOutcome(
                scan_session_id=scan_session_id,
                closed=False,
                blockers=tuple(_snapshot_blockers(database, scan_session_id, again))
                or tuple(blockers),
                sources=checks,
                disabled_sources=disabled,
            )
        _LOGGER.info(
            "Scan session %s finished and closed by %s%s",
            scan_session_id[:8],
            closer,
            " (incomplete results accepted)" if accepted else "",
        )
        return FinishOutcome(
            scan_session_id=scan_session_id,
            closed=True,
            blockers=tuple(blockers) if accepted else (),
            accepted=tuple(accepted),
            sources=checks,
            disabled_sources=disabled,
        )
    finally:
        if lease is not None:
            lease.release()


def reopen_session(
    database: ProjectDatabase, scan_session_id: str, *, reopened_by: str, reason: str = ""
) -> scan_sessions.ScanSessionInfo:
    """Reopen a closed session - explicit, by a named operator, audited.

    Delegates to :func:`omr_scanner.services.scan_sessions.reopen_scan_session`
    (one transaction: state, ``final_outputs_stale_since`` and the audit
    event), so every final output generated while closed reads stale from now
    on; nothing generated is deleted. Held files stay held until an operator
    releases them. Closing again runs every check again.
    """
    name = review_store.validate_reviewer(reopened_by)
    return scan_sessions.reopen_scan_session(
        database, scan_session_id, reopened_by=name, reason=reason
    )


__all__ = [
    "FinishError",
    "finish_blockers",
    "finish_scan_session",
    "reopen_session",
]
