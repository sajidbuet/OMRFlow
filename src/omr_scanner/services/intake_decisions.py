"""Files waiting for an operator's decision: held, unreadable, unsupported (revised phase 7).

Purpose:
    Some intake files can never become sheets by themselves (``docs/intake.md``):

    * **held** - ready, but intended for a scan session that is **closed**.
      Never silently registered into it (phase 5 diverts them);
    * **unreadable** - stable, but never fully decoded after the bounded
      retries; the quality policy calls this a rescan
      (:func:`~omr_scanner.services.quality_decisions.decode_failure_verdict`);
    * **unsupported** - decodable but not admissible in this release (a
      multi-page TIFF).

    This module lists them (paged, with provenance: source, path, when it
    arrived, why it waits, what can be decided) and applies an operator's
    decision - one audited transaction each. Phase 8 renders it.

The decisions (:class:`FileDecision`):
    * *release* (held only) - into its own session once that session is open
      again, or into another **open** session the operator names. The file is
      re-read and re-verified before it is registered, exactly like a ready
      file after a restart.
    * *retry* (unreadable only) - observe and read the file afresh.
    * *dismiss* - keep the record, never register it (``ignored``, reason
      ``operator_dismissed``).

    The ledger's machine transitions (:data:`~omr_scanner.domain.intake.TRANSITIONS`)
    are unchanged: these exits exist only here
    (:data:`~omr_scanner.domain.intake.OPERATOR_TRANSITIONS`).

What does NOT belong here:
    Qt; reconciliation or registration (the intake service and the engine do
    those once a decision has put the file back in their path).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from omr_scanner.database.models import AuditEvent, IntakeFile, IntakeSource, ScanSession
from omr_scanner.domain.intake import (
    IntakeReason,
    IntakeState,
    SourceKind,
    require_operator_transition,
)
from omr_scanner.domain.quality_decision import DEFAULT_POLICY, QualityVerdict
from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import quality_decisions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from omr_scanner.database.engine import ProjectDatabase

_LOGGER = logging.getLogger(__name__)

FILE_ENTITY = "intake_file"
"""``audit_event.entity_type`` of an operator's decision on one ledger row."""

PENDING_STATES = (
    IntakeState.HELD.value,
    IntakeState.UNREADABLE.value,
    IntakeState.UNSUPPORTED.value,
)


class FileDecision(StrEnum):
    """What an operator may decide about a waiting file."""

    RELEASE = "release"
    RETRY = "retry"
    DISMISS = "dismiss"

    @property
    def action(self) -> str:
        """The ``audit_event.action`` it records (20 characters at most)."""
        return {
            FileDecision.RELEASE: "file_released",
            FileDecision.RETRY: "file_retried",
            FileDecision.DISMISS: "file_dismissed",
        }[self]


OPTIONS: dict[IntakeState, tuple[FileDecision, ...]] = {
    IntakeState.HELD: (FileDecision.RELEASE, FileDecision.DISMISS),
    IntakeState.UNREADABLE: (FileDecision.RETRY, FileDecision.DISMISS),
    IntakeState.UNSUPPORTED: (FileDecision.DISMISS,),
}

_WHY: dict[IntakeState, str] = {
    IntakeState.HELD: (
        "Arrived for a scan session that is closed. It was not added to the closed "
        "session's results."
    ),
    IntakeState.UNREADABLE: (
        "The file never decoded as a complete image after repeated reads. The sheet "
        "probably has to be scanned again."
    ),
    IntakeState.UNSUPPORTED: (
        "The file is an image this version does not accept (for example a multi-page "
        "TIFF). Scan the pages as separate files."
    ),
}


class DecisionError(OMRScannerError):
    """A file decision was refused. Always carries a ``user_message``."""


@dataclass(frozen=True, slots=True)
class PendingFile:
    """One file waiting for an operator: everything needed to decide, and nothing more.

    Attributes:
        intake_file_id: The ledger row.
        source_id / source_label: Which scanner source it came from.
        scan_session_id: The session it was intended for.
        session_state: That session's state (``closed`` for a held file).
        relative_path / file_name / absolute_path: Where it was seen.
        state: ``held``, ``unreadable`` or ``unsupported``.
        reason / detail: Why, as a code and in the ledger's words.
        why: One sentence for an operator.
        arrived_at: When the file was first seen.
        waiting_since: When it entered this state.
        options: The decisions that apply.
        quality: For an unreadable file, the quality policy's verdict (a
            suggested rescan) - ``None`` otherwise.
    """

    intake_file_id: int
    source_id: str
    source_label: str
    scan_session_id: str | None
    session_state: str
    relative_path: str
    file_name: str
    absolute_path: str
    state: IntakeState
    reason: IntakeReason
    detail: str
    why: str
    arrived_at: datetime | None
    waiting_since: datetime | None
    options: tuple[FileDecision, ...]
    quality: QualityVerdict | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def count_pending(database: ProjectDatabase, scan_session_id: str) -> int:
    """How many of the session's files wait for a decision (one count query)."""
    if not quality_decisions.has_quality_schema(database):
        return 0
    with database.session() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(IntakeFile)
                .where(IntakeFile.scan_session_id == scan_session_id)
                .where(IntakeFile.state.in_(PENDING_STATES))
            )
            or 0
        )


def pending_decisions(
    database: ProjectDatabase,
    scan_session_id: str,
    *,
    offset: int = 0,
    limit: int = 100,
) -> tuple[PendingFile, ...]:
    """The session's waiting files, oldest arrival first, one page at a time."""
    if not quality_decisions.has_quality_schema(database):
        return ()
    policy = quality_decisions.pinned_policy(database, scan_session_id)
    with database.session() as session:
        rows = session.execute(
            select(IntakeFile, IntakeSource.label, ScanSession.state)
            .join(IntakeSource, IntakeSource.source_id == IntakeFile.source_id)
            .outerjoin(ScanSession, ScanSession.scan_session_id == IntakeFile.scan_session_id)
            .where(IntakeFile.scan_session_id == scan_session_id)
            .where(IntakeFile.state.in_(PENDING_STATES))
            .order_by(IntakeFile.first_seen_at, IntakeFile.intake_file_id)
            .offset(max(0, offset))
            .limit(max(0, limit))
        ).all()
        found: list[PendingFile] = []
        for row, label, session_state in rows:
            state = IntakeState(row.state)
            found.append(
                PendingFile(
                    intake_file_id=row.intake_file_id,
                    source_id=row.source_id,
                    source_label=str(label),
                    scan_session_id=row.scan_session_id,
                    session_state=str(session_state or ""),
                    relative_path=row.relative_path,
                    file_name=row.file_name,
                    absolute_path=row.absolute_path,
                    state=state,
                    reason=IntakeReason(row.state_reason),
                    detail=row.detail,
                    why=_WHY[state],
                    arrived_at=_aware(row.first_seen_at),
                    waiting_since=_aware(row.state_changed_at),
                    options=OPTIONS[state],
                    quality=(
                        quality_decisions.decode_failure_verdict(policy or DEFAULT_POLICY)
                        if state is IntakeState.UNREADABLE
                        else None
                    ),
                )
            )
        return tuple(found)


def decide_file(
    database: ProjectDatabase,
    intake_file_id: int,
    decision: FileDecision,
    *,
    reviewer: str,
    note: str = "",
    target_session_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> IntakeState:
    """Apply a named operator's decision to one waiting file. One audited transaction.

    Args:
        database: The open, writable project.
        intake_file_id: The ledger row.
        decision: *release*, *retry* or *dismiss* (see :data:`OPTIONS`).
        reviewer: Who decided. Required.
        note: Their words, recorded.
        target_session_id: For *release*: an **open** session to release the
            file into. Default: the session it was intended for, which must be
            open again (reopened).
        clock: Injected time.

    Returns:
        The row's new state.

    Raises:
        DecisionError: The row is not waiting for a decision, the decision
            does not apply to its state, or a release has no open session.
        omr_scanner.services.review_store.ReviewError: No reviewer.
    """
    from omr_scanner.services import review_store

    name = review_store.validate_reviewer(reviewer)
    if database.read_only or not quality_decisions.has_quality_schema(database):
        raise DecisionError(
            "decisions need a writable schema-17 project",
            user_message="This project is read-only or not upgraded; nothing was changed.",
        )
    moment = (clock or _now)()
    with database.session() as session:
        row = session.get(IntakeFile, intake_file_id)
        if row is None or row.state not in PENDING_STATES:
            raise DecisionError(
                f"intake row {intake_file_id} is not waiting for a decision",
                user_message="That file is not waiting for a decision.",
            )
        state = IntakeState(row.state)
        if decision not in OPTIONS[state]:
            raise DecisionError(
                f"{decision.value} does not apply to a {state.value} file",
                user_message=f"A {state.value} file cannot be given that decision.",
            )
        previous_session = row.scan_session_id
        if decision is FileDecision.RELEASE:
            source = session.get(IntakeSource, row.source_id)
            if source is None or source.kind != SourceKind.WATCHED.value:
                raise DecisionError(
                    "only a watched source's file can be released",
                    user_message="Only a file from a watched scanner source can be released.",
                )
            target = target_session_id or row.scan_session_id
            target_row = session.get(ScanSession, target) if target else None
            if target_row is None or target_row.state != ScanSessionState.OPEN.value:
                raise DecisionError(
                    f"release target {target!r} is not an open session",
                    user_message=(
                        "A held file can only be released into an open scan session. "
                        "Reopen its session, or choose another open session."
                    ),
                )
            require_operator_transition(state, IntakeState.READY)
            row.scan_session_id = target_row.scan_session_id
            row.state = IntakeState.READY.value
            row.state_reason = IntakeReason.OPERATOR_RELEASED.value
            row.detail = f"Released by {name}; re-read and verified before registration."
            # Bytes may have changed while it was held: re-read before use,
            # exactly as for a ready file after a restart.
            row.reverify_required = True
            new_state = IntakeState.READY
        elif decision is FileDecision.RETRY:
            require_operator_transition(state, IntakeState.STABILIZING)
            row.state = IntakeState.STABILIZING.value
            row.state_reason = IntakeReason.OPERATOR_RETRY.value
            row.detail = f"Retry requested by {name}; observed and read afresh."
            row.attempts = 0
            row.observations = 0
            row.stable_since = None
            row.retry_after = None
            row.content_sha256 = None
            new_state = IntakeState.STABILIZING
        else:
            require_operator_transition(state, IntakeState.IGNORED)
            row.state = IntakeState.IGNORED.value
            row.state_reason = IntakeReason.OPERATOR_DISMISSED.value
            row.detail = f"Dismissed by {name}: not processed. {note.strip()}".strip()
            new_state = IntakeState.IGNORED
        row.state_changed_at = moment
        session.add(
            AuditEvent(
                occurred_at=moment,
                batch_id="",
                scan_id=0,
                conflict_id=0,
                entity_type=FILE_ENTITY,
                entity_id=str(intake_file_id),
                action=decision.action,
                reviewer=name,
                previous_value=state.value,
                new_value=new_state.value,
                reason_text=note.strip(),
                detail=(
                    f"{row.relative_path} (source {row.source_id[:8]}): {state.value} -> "
                    f"{new_state.value}"
                    + (
                        f"; session {previous_session} -> {row.scan_session_id}"
                        if previous_session != row.scan_session_id
                        else ""
                    )
                ),
            )
        )
    _LOGGER.info(
        "Intake file %d: %s by %s (%s -> %s)",
        intake_file_id, decision.value, name, state.value, new_state.value,
    )
    return new_state


__all__ = [
    "FILE_ENTITY",
    "OPTIONS",
    "PENDING_STATES",
    "DecisionError",
    "FileDecision",
    "PendingFile",
    "count_pending",
    "decide_file",
    "pending_decisions",
]
