"""Persisted operator controls for a scan session (0.1.1 revised phase 7).

Purpose:
    Store the operator's pause / resume / stop **intent** for intake and
    recognition durably, each change one transaction with its audit event, so
    that the continuous engine
    (:class:`~omr_scanner.services.continuous_engine.ContinuousEngine`) can
    restore it after a restart instead of resuming blindly. Revised phase 6
    kept these flags in memory; ARCHITECTURE_NOTES §14.3 requires them to
    survive.

Responsibilities:
    * :func:`get_controls` - the current intent (one immutable value).
    * :func:`set_intake_paused`, :func:`set_source_paused` - intake intent.
    * :func:`set_processing_intent` and the named shortcuts
      :func:`pause_processing`, :func:`resume_processing`,
      :func:`request_finish_current`, :func:`record_cancel_queued`.

What this module does NOT do:
    Act. It records intent; the engine reads it on every step and obeys
    (``docs/intake.md``, "Controls"). Nothing here touches a sheet, a claim or
    the intake ledger, so recording intent can never lose work.

Schema:
    The columns are migration 17's. On an older or read-only project every
    session reads "intake on, processing running" and writes are refused.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from omr_scanner.database.models import AuditEvent, IntakeSource, ScanSession
from omr_scanner.domain.session_controls import (
    CONTROL_ENTITY,
    ControlAction,
    ProcessingIntent,
    SessionControls,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.services.quality_decisions import has_quality_schema

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase

_LOGGER = logging.getLogger(__name__)


class ControlError(OMRScannerError):
    """A control change was refused. Always carries a ``user_message``."""


def _now() -> datetime:
    return datetime.now(UTC)


def _audit(
    session: Session,
    *,
    action: ControlAction,
    entity_id: str,
    actor: str,
    previous: str,
    new: str,
    reason: str = "",
    detail: str = "",
    moment: datetime,
) -> None:
    session.add(
        AuditEvent(
            occurred_at=moment,
            batch_id="",
            scan_id=0,
            conflict_id=0,
            entity_type=CONTROL_ENTITY,
            entity_id=entity_id,
            action=action.value,
            reviewer=actor.strip(),
            previous_value=previous,
            new_value=new,
            reason_text=reason.strip(),
            detail=detail,
        )
    )


def _require_writable(database: ProjectDatabase) -> None:
    if database.read_only or not has_quality_schema(database):
        raise ControlError(
            "controls need a writable schema-17 project",
            user_message="This project is read-only or not upgraded; controls cannot be saved.",
        )


def _require_session(session: Session, scan_session_id: str) -> ScanSession:
    row = session.get(ScanSession, scan_session_id)
    if row is None:
        raise ControlError(
            f"No scan session {scan_session_id!r}",
            user_message="That scan session no longer exists in this project.",
        )
    return row


def get_controls(database: ProjectDatabase, scan_session_id: str) -> SessionControls:
    """The session's persisted intent. Two indexed reads; never writes."""
    if not has_quality_schema(database):
        return SessionControls(scan_session_id=scan_session_id)
    with database.session() as session:
        found = session.execute(
            select(ScanSession.intake_paused, ScanSession.processing_intent).where(
                ScanSession.scan_session_id == scan_session_id
            )
        ).first()
        paused = frozenset(
            str(item)
            for item in session.scalars(
                select(IntakeSource.source_id).where(IntakeSource.intake_paused.is_(True))
            ).all()
        )
    if found is None:
        return SessionControls(scan_session_id=scan_session_id, paused_sources=paused)
    try:
        intent = ProcessingIntent(str(found[1]))
    except ValueError:
        # An intent this build does not know is not permission to run.
        intent = ProcessingIntent.PAUSED
    return SessionControls(
        scan_session_id=scan_session_id,
        intake_paused=bool(found[0]),
        processing=intent,
        paused_sources=paused,
    )


def set_intake_paused(
    database: ProjectDatabase, scan_session_id: str, paused: bool, *, actor: str = ""
) -> SessionControls:
    """Pause or resume intake for every source of the session. Audited; idempotent."""
    _require_writable(database)
    moment = _now()
    with database.session() as session:
        row = _require_session(session, scan_session_id)
        if bool(row.intake_paused) != paused:
            row.intake_paused = paused
            _audit(
                session,
                action=ControlAction.INTAKE_PAUSED if paused else ControlAction.INTAKE_RESUMED,
                entity_id=scan_session_id,
                actor=actor,
                previous="paused" if not paused else "on",
                new="paused" if paused else "on",
                moment=moment,
            )
    _LOGGER.info("Scan session %s: intake %s", scan_session_id[:8], "paused" if paused else "on")
    return get_controls(database, scan_session_id)


def set_source_paused(
    database: ProjectDatabase, source_id: str, paused: bool, *, actor: str = ""
) -> bool:
    """Pause or resume one intake source. Audited; idempotent. Returns the new state.

    A paused source is not reconciled or registered from; its reachability is
    left as the last listing found it (paused is not unreachable), and its
    ledger rows keep their states - nothing on disk is lost.
    """
    _require_writable(database)
    moment = _now()
    with database.session() as session:
        row = session.get(IntakeSource, source_id)
        if row is None:
            raise ControlError(
                f"No intake source {source_id!r}",
                user_message="That intake source no longer exists in this project.",
            )
        if bool(row.intake_paused) != paused:
            row.intake_paused = paused
            _audit(
                session,
                action=ControlAction.SOURCE_PAUSED if paused else ControlAction.SOURCE_RESUMED,
                entity_id=source_id,
                actor=actor,
                previous="paused" if not paused else "on",
                new="paused" if paused else "on",
                detail=f"source '{row.label}'",
                moment=moment,
            )
    _LOGGER.info("Intake source %s %s", source_id[:8], "paused" if paused else "resumed")
    return paused


_INTENT_ACTION: dict[ProcessingIntent, ControlAction] = {
    ProcessingIntent.RUNNING: ControlAction.PROCESSING_RESUMED,
    ProcessingIntent.PAUSED: ControlAction.PROCESSING_PAUSED,
    ProcessingIntent.STOPPED: ControlAction.FINISH_REQUESTED,
}


def set_processing_intent(
    database: ProjectDatabase,
    scan_session_id: str,
    intent: ProcessingIntent,
    *,
    actor: str = "",
    reason: str = "",
    action: ControlAction | None = None,
) -> SessionControls:
    """Record the recognition intent. Audited when it changes (or ``action`` is forced).

    Recording ``paused`` or ``stopped`` is enough for the next coordinator to
    obey it: the engine reads the intent on every step and after a restart.
    """
    _require_writable(database)
    moment = _now()
    with database.session() as session:
        row = _require_session(session, scan_session_id)
        previous = str(row.processing_intent)
        if previous != intent.value or action is not None:
            row.processing_intent = intent.value
            _audit(
                session,
                action=action if action is not None else _INTENT_ACTION[intent],
                entity_id=scan_session_id,
                actor=actor,
                previous=previous,
                new=intent.value,
                reason=reason,
                moment=moment,
            )
    _LOGGER.info("Scan session %s: processing %s", scan_session_id[:8], intent.value)
    return get_controls(database, scan_session_id)


def pause_processing(
    database: ProjectDatabase, scan_session_id: str, *, actor: str = ""
) -> SessionControls:
    """Claim nothing new; what a worker already holds finishes and is recorded."""
    return set_processing_intent(database, scan_session_id, ProcessingIntent.PAUSED, actor=actor)


def resume_processing(
    database: ProjectDatabase, scan_session_id: str, *, actor: str = ""
) -> SessionControls:
    """Return to claiming work (also the only way out of ``stopped``)."""
    return set_processing_intent(database, scan_session_id, ProcessingIntent.RUNNING, actor=actor)


def request_finish_current(
    database: ProjectDatabase, scan_session_id: str, *, actor: str = "", reason: str = ""
) -> SessionControls:
    """Persist *Finish current and stop* **before** the engine starts draining.

    Recorded first, so a crash while what is current is still settling comes
    back stopped - never resumed - exactly as the operator asked.
    """
    return set_processing_intent(
        database,
        scan_session_id,
        ProcessingIntent.STOPPED,
        actor=actor,
        reason=reason,
        action=ControlAction.FINISH_REQUESTED,
    )


def record_cancel_queued(
    database: ProjectDatabase, scan_session_id: str, *, actor: str, reason: str = ""
) -> SessionControls:
    """Persist *Cancel queued work* (stopped) for a named operator. The deliberate stop.

    Raises:
        omr_scanner.services.review_store.ReviewError: No operator named -
            cancelling queued work is never anonymous.
    """
    from omr_scanner.services import review_store

    name = review_store.validate_reviewer(actor)
    return set_processing_intent(
        database,
        scan_session_id,
        ProcessingIntent.STOPPED,
        actor=name,
        reason=reason,
        action=ControlAction.QUEUE_CANCELLED,
    )


__all__ = [
    "ControlError",
    "get_controls",
    "pause_processing",
    "record_cancel_queued",
    "request_finish_current",
    "resume_processing",
    "set_intake_paused",
    "set_processing_intent",
    "set_source_paused",
]
