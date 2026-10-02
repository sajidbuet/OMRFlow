"""Scan sessions and finite batches: the lifecycle service (0.1.1 phase 2).

Purpose:
    Own ``Project -> ScanSession -> finite ScanBatch`` for one project database
    (``docs/decisions/ADR-0005-scan-sessions-and-finite-batches.md``): create,
    rename, close, reopen and combine sessions; the active-session pointer;
    attaching a new batch to a session (with template pinning); sealing a
    batch; recording and reversing batch supersession; the upgrade backfill;
    and the batch a downstream stage reads until session-level results exist.

Every lifecycle change is one transaction that also writes its
:class:`~omr_scanner.database.models.AuditEvent` - the change and its record
commit together or not at all.

What does NOT belong here:
    * Qt.
    * Session-level *aggregation* - which sheets of which batches count for
      attendance, scoring, results and reports. That is the effective-scan-set
      service of session-level results (revised phase 4). Until then
      :func:`downstream_batch_id` names the one batch those stages read.
    * Crash recovery (S1, S2, S3, R1 - revised phase 3).
    * Set codes. Sessions never interpret a set code; the one physical ->
      logical translation stays :func:`omr_scanner.services.review_store.effective_set_codes`.

Read-only compatibility:
    A schema-13 project opened read-only is never migrated, so it has no
    ``scan_session`` table and no lifecycle columns. :func:`has_lifecycle_schema`
    says so, and every read here then presents each batch as a **virtual
    one-batch session** (:data:`VIRTUAL_PREFIX` ids, never written anywhere).

Naming: a ``ScanSession`` is ``scan_session``; ``session`` is the ORM session.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    BatchStatus,
    BatchSupersession,
    ProjectSetting,
    ScanBatch,
    ScanRejection,
    ScanSession,
    SettingKey,
)
from omr_scanner.domain.scan_lifecycle import LifecycleState
from omr_scanner.domain.scan_sessions import (
    BATCH_ENTITY,
    SESSION_ENTITY,
    BatchFacts,
    BatchMembership,
    BatchRole,
    LegacyBatch,
    ReplacementLink,
    ScanSessionState,
    SessionAction,
    attach_problem,
    close_problem,
    plan_backfill,
    reopen_problem,
    supersession_problem,
)
from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.services.batch_store import BatchIdentity

_LOGGER = logging.getLogger(__name__)

LIFECYCLE_SCHEMA_VERSION = 14
"""The first schema with scan sessions."""

VIRTUAL_PREFIX = "virtual:"
"""Prefix of a virtual session id (read-only, pre-session project)."""


class ScanSessionError(OMRScannerError):
    """A scan-session operation was refused. Always carries a ``user_message``."""


class TemplatePinError(ScanSessionError):
    """A batch's template identity differs from its session's pinned identity.

    Attributes:
        differences: One sentence per difference, as a dialog shows them.
    """

    def __init__(self, message: str, *, user_message: str, differences: tuple[str, ...]) -> None:
        super().__init__(message, user_message=user_message)
        self.differences = differences


# ----------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScanSessionInfo:
    """One session as the GUI and the tests see it (detached from the ORM)."""

    scan_session_id: str
    name: str
    state: ScanSessionState
    origin: str
    created_at: datetime | None
    created_by: str = ""
    closed_at: datetime | None = None
    closed_by: str = ""
    reopened_at: datetime | None = None
    reopen_count: int = 0
    final_outputs_stale_since: datetime | None = None
    template_name: str = ""
    geometry_fingerprint: str = ""
    merged_into_session_id: str | None = None
    batch_count: int = 0
    virtual: bool = False

    @property
    def label(self) -> str:
        """``"Exam 2026-10-01 (open)"``."""
        return f"{self.name} ({self.state.label.lower()})"


@dataclass(frozen=True, slots=True)
class BatchInfo:
    """One batch's lifecycle position."""

    batch_id: str
    scan_session_id: str | None
    role: BatchRole
    membership: BatchMembership
    created_at: datetime
    status: str
    total_scans: int
    sealed_at: datetime | None = None
    superseded_by: str | None = None
    supersedes: tuple[str, ...] = ()

    @property
    def is_superseded(self) -> bool:
        """Whether a live supersession replaces this batch."""
        return self.superseded_by is not None


@dataclass(frozen=True, slots=True)
class BackfillReport:
    """What the upgrade backfill did, also stored as JSON in ``project_setting``."""

    sessions_created: int
    batches_assigned: int
    grouped: tuple[tuple[str, ...], ...] = ()
    ambiguous: tuple[str, ...] = ()
    ran_at: str = ""

    def as_json(self) -> str:
        """The stored form."""
        return json.dumps(
            {
                "ran_at": self.ran_at,
                "sessions_created": self.sessions_created,
                "batches_assigned": self.batches_assigned,
                "grouped": [list(group) for group in self.grouped],
                "ambiguous": list(self.ambiguous),
            },
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class CombineOutcome:
    """What *Combine into one session* moved."""

    target_id: str
    moved_batches: tuple[str, ...]
    emptied_sessions: tuple[str, ...]
    refusals: tuple[str, ...] = field(default_factory=tuple)


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


def has_lifecycle_schema(database: ProjectDatabase) -> bool:
    """Whether the database has scan sessions (schema 14 or later)."""
    return database.schema_version >= LIFECYCLE_SCHEMA_VERSION


def has_scope_schema(database: ProjectDatabase) -> bool:
    """Whether the database records session scope (schema 15 or later)."""
    return database.schema_version >= 15


def _audit(
    session: Session,
    *,
    action: SessionAction,
    entity_type: str,
    entity_id: str,
    actor: str = "",
    batch_id: str = "",
    previous_value: str = "",
    new_value: str = "",
    reason: str = "",
    detail: str = "",
) -> None:
    session.add(
        AuditEvent(
            occurred_at=_now(),
            batch_id=batch_id,
            scan_id=0,
            conflict_id=0,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action.value,
            reviewer=actor.strip(),
            previous_value=previous_value,
            new_value=new_value,
            reason_text=reason,
            detail=detail,
        )
    )


def _setting(session: Session, key: str) -> str | None:
    row = session.get(ProjectSetting, key)
    return row.value if row is not None else None


def _set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(ProjectSetting, key)
    moment = _now()
    if row is None:
        session.add(ProjectSetting(key=key, value=value, updated_at=moment))
    else:
        row.value = value
        row.updated_at = moment


def _project_id(session: Session) -> str:
    return _setting(session, SettingKey.PROJECT_ID) or ""


def _require(session: Session, scan_session_id: str) -> ScanSession:
    row = session.get(ScanSession, scan_session_id)
    if row is None:
        raise ScanSessionError(
            f"No scan session {scan_session_id!r}",
            user_message="That scan session no longer exists in this project.",
        )
    return row


def _batch_count(session: Session, scan_session_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ScanBatch)
            .where(ScanBatch.scan_session_id == scan_session_id)
        )
        or 0
    )


def _info(session: Session, row: ScanSession) -> ScanSessionInfo:
    return ScanSessionInfo(
        scan_session_id=row.scan_session_id,
        name=row.name,
        state=ScanSessionState(row.state),
        origin=row.origin,
        created_at=row.created_at,
        created_by=row.created_by,
        closed_at=row.closed_at,
        closed_by=row.closed_by,
        reopened_at=row.reopened_at,
        reopen_count=row.reopen_count,
        final_outputs_stale_since=row.final_outputs_stale_since,
        template_name=row.template_name,
        geometry_fingerprint=row.geometry_fingerprint,
        merged_into_session_id=row.merged_into_session_id,
        batch_count=_batch_count(session, row.scan_session_id),
    )


def _default_name(session: Session, moment: datetime) -> str:
    """``"<exam> - 2026-10-01"``, de-duplicated with a counter."""
    exam = (_setting(session, SettingKey.EXAM_NAME) or "").strip() or "Scan session"
    base = f"{exam} - {moment.astimezone().strftime('%Y-%m-%d')}"
    names = set(session.scalars(select(ScanSession.name)).all())
    if base not in names:
        return base
    counter = 2
    while f"{base} ({counter})" in names:
        counter += 1
    return f"{base} ({counter})"


def _live(session: Session) -> dict[str, str]:
    """Every live supersession, ``superseded -> superseding``."""
    return {
        str(old): str(new)
        for old, new in session.execute(
            select(
                BatchSupersession.superseded_batch_id, BatchSupersession.superseding_batch_id
            ).where(BatchSupersession.reversed_at.is_(None))
        ).all()
    }


def _batch_row_facts(session: Session, batch_id: str) -> tuple[str | None, datetime | None, str]:
    found = session.execute(
        select(ScanBatch.scan_session_id, ScanBatch.sealed_at, ScanBatch.role).where(
            ScanBatch.batch_id == batch_id
        )
    ).first()
    if found is None:
        raise ScanSessionError(
            f"No batch {batch_id!r}", user_message="That batch is no longer in this project."
        )
    return found[0], found[1], str(found[2])


def _new_session_row(
    session: Session,
    *,
    name: str,
    origin: str,
    created_by: str,
    moment: datetime,
    notes: str = "",
) -> ScanSession:
    row = ScanSession(
        scan_session_id=_new_id(),
        project_id=_project_id(session),
        name=name.strip() or _default_name(session, moment),
        state=ScanSessionState.OPEN.value,
        origin=origin,
        created_at=moment,
        created_by=created_by.strip(),
        notes=notes,
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        action=SessionAction.CREATED,
        entity_type=SESSION_ENTITY,
        entity_id=row.scan_session_id,
        actor=created_by,
        new_value=row.name,
        detail=f"origin={origin}",
    )
    return row


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------
def create_scan_session(
    database: ProjectDatabase,
    *,
    name: str = "",
    created_by: str = "",
    activate: bool = True,
    origin: str = "operator",
) -> ScanSessionInfo:
    """Create an empty, open session (and by default make it the active one)."""
    moment = _now()
    with database.session() as session:
        row = _new_session_row(
            session, name=name, origin=origin, created_by=created_by, moment=moment
        )
        if activate:
            _activate(session, row.scan_session_id, actor=created_by)
        info = _info(session, row)
    _LOGGER.info("Scan session %s created (%s)", info.scan_session_id, origin)
    return info


def rename_scan_session(
    database: ProjectDatabase, scan_session_id: str, name: str, *, renamed_by: str = ""
) -> ScanSessionInfo:
    """Give a session a new name (any state)."""
    wanted = name.strip()
    if not wanted:
        raise ScanSessionError(
            "Blank scan session name", user_message="A scan session's name must not be blank."
        )
    with database.session() as session:
        row = _require(session, scan_session_id)
        previous = row.name
        if previous != wanted:
            row.name = wanted
            _audit(
                session,
                action=SessionAction.RENAMED,
                entity_type=SESSION_ENTITY,
                entity_id=scan_session_id,
                actor=renamed_by,
                previous_value=previous,
                new_value=wanted,
            )
        return _info(session, row)


def get_scan_session(database: ProjectDatabase, scan_session_id: str) -> ScanSessionInfo | None:
    """One session, or ``None``. Virtual ids resolve on a pre-session database."""
    if scan_session_id.startswith(VIRTUAL_PREFIX):
        return next(
            (
                item
                for item in list_scan_sessions(database)
                if item.scan_session_id == scan_session_id
            ),
            None,
        )
    if not has_lifecycle_schema(database):
        return None
    with database.session() as session:
        row = session.get(ScanSession, scan_session_id)
        return _info(session, row) if row is not None else None


def list_scan_sessions(database: ProjectDatabase) -> tuple[ScanSessionInfo, ...]:
    """Every session, oldest first.

    On a schema-13 project opened read-only: one **virtual** session per batch,
    named after it, open, never stored.
    """
    if not has_lifecycle_schema(database):
        with database.session() as session:
            rows = session.execute(
                select(ScanBatch.batch_id, ScanBatch.created_at).order_by(
                    ScanBatch.created_at, ScanBatch.batch_id
                )
            ).all()
        return tuple(
            ScanSessionInfo(
                scan_session_id=f"{VIRTUAL_PREFIX}{batch_id}",
                name=f"Batch {str(batch_id)[:8]} (not yet upgraded)",
                state=ScanSessionState.OPEN,
                origin="virtual",
                created_at=created_at,
                batch_count=1,
                virtual=True,
            )
            for batch_id, created_at in rows
        )
    with database.session() as session:
        rows_ = session.scalars(
            select(ScanSession).order_by(ScanSession.created_at, ScanSession.scan_session_id)
        ).all()
        return tuple(_info(session, row) for row in rows_)


def active_scan_session(database: ProjectDatabase) -> ScanSessionInfo | None:
    """The session the operator is working in, or ``None``.

    A pointer to a session that no longer exists reads as ``None``. On a
    pre-session read-only project: the virtual session of the newest batch.
    """
    if not has_lifecycle_schema(database):
        virtual = list_scan_sessions(database)
        return virtual[-1] if virtual else None
    with database.session() as session:
        pointer = _setting(session, SettingKey.ACTIVE_SCAN_SESSION)
        if not pointer:
            return None
        row = session.get(ScanSession, pointer)
        return _info(session, row) if row is not None else None


def _activate(session: Session, scan_session_id: str, *, actor: str = "") -> None:
    previous = _setting(session, SettingKey.ACTIVE_SCAN_SESSION) or ""
    if previous == scan_session_id:
        return
    _set_setting(session, SettingKey.ACTIVE_SCAN_SESSION, scan_session_id)
    _audit(
        session,
        action=SessionAction.ACTIVATED,
        entity_type=SESSION_ENTITY,
        entity_id=scan_session_id,
        actor=actor,
        previous_value=previous,
        new_value=scan_session_id,
    )


def set_active_scan_session(
    database: ProjectDatabase, scan_session_id: str, *, activated_by: str = ""
) -> ScanSessionInfo:
    """Make ``scan_session_id`` the active session of this project."""
    with database.session() as session:
        row = _require(session, scan_session_id)
        if row.project_id and row.project_id != _project_id(session):
            raise ScanSessionError(
                "Session belongs to another project",
                user_message="That scan session belongs to a different project.",
            )
        _activate(session, scan_session_id, actor=activated_by)
        return _info(session, row)


@dataclass(frozen=True, slots=True)
class ClosureBlocker:
    """One reason a scan session cannot be closed yet (ARCHITECTURE_NOTES §14.3).

    Attributes:
        kind: ``running``, ``unread``, ``conflicts``, ``rescans`` or ``deferred``.
        message: The operator-facing sentence.
        acknowledgeable: Whether an operator may close past it by explicitly
            accepting incomplete results - only outstanding rescans and
            deferred sheets, matching the existing *Export incomplete
            results* decision. Unread sheets and unresolved conflicts never.
    """

    kind: str
    message: str
    acknowledgeable: bool = False


def closure_blockers(database: ProjectDatabase, scan_session_id: str) -> tuple[ClosureBlocker, ...]:
    """Everything that stops a session closing, from persisted state (0.1.1 phase 4).

    A batch still running; sheets not read yet (``pending`` / ``queued`` /
    ``processing`` / ``cancelled``); unresolved required conflicts on the
    session's sheets (open or deferred, session-wide, as Resolve counts them);
    rejected sheets whose rescan is outstanding; deferred sheets. Empty means
    the session may be closed.
    """
    from omr_scanner.domain.session_population import SheetDisposition
    from omr_scanner.services import review_store, session_population

    found: list[ClosureBlocker] = []
    with database.session() as session:
        running = session.scalar(
            select(func.count())
            .select_from(ScanBatch)
            .where(ScanBatch.scan_session_id == scan_session_id)
            .where(ScanBatch.status == BatchStatus.RUNNING.value)
        )
    if running:
        found.append(
            ClosureBlocker(
                "running", f"{running} batch(es) of this scan session are still being read."
            )
        )
    population = session_population.session_population(database, scan_session_id)
    if not population.batch_ids:
        return tuple(found)
    counts = population.counts()
    unread = counts.get(SheetDisposition.NOT_READ, 0)
    if unread:
        found.append(
            ClosureBlocker(
                "unread",
                f"{unread} sheet(s) have not been read yet - resume the batch on the "
                "Scan stage.",
            )
        )
    conflicts = review_store.count_conflicts(
        database, population.batch_ids[0], session_wide=True
    )
    unresolved = conflicts.open_count + conflicts.deferred
    if unresolved:
        found.append(
            ClosureBlocker(
                "conflicts",
                f"{unresolved} conflict(s) are unresolved on the Resolve stage.",
            )
        )
    rescans = counts.get(SheetDisposition.REJECTED_PENDING_RESCAN, 0)
    if rescans:
        found.append(
            ClosureBlocker(
                "rescans",
                f"{rescans} rejected sheet(s) are awaiting a rescan.",
                acknowledgeable=True,
            )
        )
    deferred = counts.get(SheetDisposition.DEFERRED, 0)
    if deferred:
        found.append(
            ClosureBlocker(
                "deferred",
                f"{deferred} sheet(s) are deferred on the Attendance stage.",
                acknowledgeable=True,
            )
        )
    return tuple(found)


def close_scan_session(
    database: ProjectDatabase,
    scan_session_id: str,
    *,
    closed_by: str = "",
    reason: str = "",
    acknowledge_incomplete: bool = False,
) -> ScanSessionInfo:
    """Close a session: run the closure checks, seal every open batch, refuse new batches.

    Refused while one of its batches is being processed (``running``) - a
    session cannot be declared finished under a run that is still reading -
    and, since 0.1.1 phase 4, while any :func:`closure_blockers` remain:
    unread sheets and unresolved conflicts always; outstanding rescans and
    deferred sheets unless ``acknowledge_incomplete`` (an operator's explicit
    acceptance of incomplete results, recorded in the close's audit event).
    """
    moment = _now()
    with database.session() as session:
        row = _require(session, scan_session_id)
        problem = close_problem(ScanSessionState(row.state))
        if problem:
            raise ScanSessionError(f"Cannot close {scan_session_id}", user_message=problem)
    found = closure_blockers(database, scan_session_id)
    if any(item.kind == "running" for item in found):
        # Checked first, as before: a session cannot be declared finished
        # under a run that is still reading.
        raise ScanSessionError(
            "Batch running",
            user_message=(
                "A batch of this scan session is still being processed. Wait for "
                "it to finish (or cancel it), then close the session."
            ),
        )
    blockers = list(found)
    accepted = [item for item in blockers if item.acknowledgeable and acknowledge_incomplete]
    refused = [item for item in blockers if item not in accepted]
    if refused:
        raise ScanSessionError(
            f"Cannot close {scan_session_id}: closure blockers",
            user_message="The scan session cannot be closed yet: "
            + " ".join(item.message for item in refused),
        )
    if accepted and not closed_by.strip():
        raise ScanSessionError(
            "Closing with incomplete results needs an operator",
            user_message=(
                "Closing with incomplete results must be accepted by a named operator. "
                "Set your name in File > Settings first."
            ),
        )
    with database.session() as session:
        row = _require(session, scan_session_id)
        running = session.scalars(
            select(ScanBatch.batch_id)
            .where(ScanBatch.scan_session_id == scan_session_id)
            .where(ScanBatch.status == BatchStatus.RUNNING.value)
        ).all()
        if running:
            raise ScanSessionError(
                "Batch running",
                user_message=(
                    "A batch of this scan session is still being processed. Wait for "
                    "it to finish (or cancel it), then close the session."
                ),
            )
        open_batches = session.scalars(
            select(ScanBatch.batch_id)
            .where(ScanBatch.scan_session_id == scan_session_id)
            .where(ScanBatch.sealed_at.is_(None))
            .order_by(ScanBatch.created_at)
        ).all()
        for batch_id in open_batches:
            _seal(session, str(batch_id), actor=closed_by, reason="scan session closed")
        row.state = ScanSessionState.CLOSED.value
        row.closed_at = moment
        row.closed_by = closed_by.strip()
        _audit(
            session,
            action=SessionAction.CLOSED,
            entity_type=SESSION_ENTITY,
            entity_id=scan_session_id,
            actor=closed_by,
            previous_value=ScanSessionState.OPEN.value,
            new_value=ScanSessionState.CLOSED.value,
            reason=reason,
            detail=f"sealed {len(open_batches)} open batch(es)"
            + (
                "; incomplete results accepted: "
                + " ".join(item.message for item in accepted)
                if accepted
                else ""
            ),
        )
        return _info(session, row)


def reopen_scan_session(
    database: ProjectDatabase, scan_session_id: str, *, reopened_by: str = "", reason: str = ""
) -> ScanSessionInfo:
    """Reopen a closed session: explicit, audited, and final outputs become stale.

    Sealed batches stay sealed - reopening lets the session receive *new*
    batches; it never reopens a batch's membership.
    """
    moment = _now()
    with database.session() as session:
        row = _require(session, scan_session_id)
        problem = reopen_problem(ScanSessionState(row.state))
        if problem:
            raise ScanSessionError(f"Cannot reopen {scan_session_id}", user_message=problem)
        if row.merged_into_session_id:
            raise ScanSessionError(
                "Session was combined",
                user_message="This scan session was combined into another one and is empty.",
            )
        row.state = ScanSessionState.OPEN.value
        row.reopened_at = moment
        row.reopened_by = reopened_by.strip()
        row.reopen_count += 1
        row.final_outputs_stale_since = moment
        _audit(
            session,
            action=SessionAction.REOPENED,
            entity_type=SESSION_ENTITY,
            entity_id=scan_session_id,
            actor=reopened_by,
            previous_value=ScanSessionState.CLOSED.value,
            new_value=ScanSessionState.OPEN.value,
            reason=reason,
            detail="final outputs generated while closed are stale",
        )
        return _info(session, row)


# ----------------------------------------------------------------------
# Batches
# ----------------------------------------------------------------------
def _identity_tuple(identity: BatchIdentity) -> tuple[str, str, str, str]:
    return (
        identity.template_id,
        identity.geometry_fingerprint,
        identity.recognition_fingerprint,
        identity.engine_version,
    )


def pin_differences(row: ScanSession, identity: BatchIdentity) -> tuple[str, ...]:
    """How ``identity`` differs from the session's pinned identity (empty = compatible).

    An unpinned session (no batch yet) is compatible with anything.
    """
    if not row.template_id and not row.geometry_fingerprint:
        return ()
    differences: list[str] = []
    if row.template_id != identity.template_id:
        differences.append(
            f"This scan session's batches were read with template '{row.template_name}' "
            f"and the loaded template is '{identity.template_name}'."
        )
    elif row.geometry_fingerprint != identity.geometry_fingerprint:
        differences.append(
            "The template's geometry has changed since this scan session's first batch."
        )
    if row.recognition_fingerprint != identity.recognition_fingerprint:
        differences.append(
            "The recognition thresholds have changed since this scan session's first batch."
        )
    if row.engine_version != identity.engine_version:
        differences.append(
            f"This scan session was started with recognition engine {row.engine_version} "
            f"and this build is {identity.engine_version}."
        )
    return tuple(differences)


def _pin(row: ScanSession, identity: BatchIdentity) -> None:
    row.template_id = identity.template_id
    row.template_name = identity.template_name
    row.geometry_fingerprint = identity.geometry_fingerprint
    row.recognition_fingerprint = identity.recognition_fingerprint
    row.engine_version = identity.engine_version


def _seal(session: Session, batch_id: str, *, actor: str = "", reason: str = "") -> bool:
    from omr_scanner.services import processing_manifest

    _scan_session_id, sealed_at, _role = _batch_row_facts(session, batch_id)
    if sealed_at is not None:
        return False
    total = int(
        session.scalar(
            select(func.count()).select_from(BatchScan).where(BatchScan.batch_id == batch_id)
        )
        or 0
    )
    session.execute(
        update(ScanBatch)
        .where(ScanBatch.batch_id == batch_id)
        .values(sealed_at=_now(), sealed_by=actor.strip(), total_scans=total)
    )
    _audit(
        session,
        action=SessionAction.BATCH_SEALED,
        entity_type=BATCH_ENTITY,
        entity_id=batch_id,
        batch_id=batch_id,
        actor=actor,
        reason=reason,
        new_value=str(total),
        detail=f"membership final at {total} scan(s)",
    )
    session.flush()
    processing_manifest.add_manifest(session, batch_id, trigger="sealed")
    return True


def seal_batch(
    database: ProjectDatabase, batch_id: str, *, sealed_by: str = "", reason: str = ""
) -> bool:
    """Make a batch's membership final. Idempotent: ``False`` if already sealed.

    Sealing is not completion: the batch's unfinished sheets still process,
    resume and retry. A processing manifest is written at the seal.
    """
    with database.session() as session:
        return _seal(session, batch_id, actor=sealed_by, reason=reason)


def seal_batch_in(
    session: Session, batch_id: str, *, sealed_by: str = "", reason: str = ""
) -> bool:
    """:func:`seal_batch` inside a transaction the caller owns (intake registration)."""
    return _seal(session, batch_id, actor=sealed_by, reason=reason)


def _open_session_for_new_batch(
    session: Session,
    scan_session_id: str | None,
    *,
    created_by: str,
) -> ScanSession:
    """The session a new batch joins: the one named, or the active one (implicit)."""
    if scan_session_id is not None:
        row = _require(session, scan_session_id)
    else:
        pointer = _setting(session, SettingKey.ACTIVE_SCAN_SESSION)
        found = session.get(ScanSession, pointer) if pointer else None
        if found is None:
            found = _new_session_row(
                session, name="", origin="implicit", created_by=created_by, moment=_now()
            )
            _activate(session, found.scan_session_id, actor=created_by)
        row = found
    problem = attach_problem(ScanSessionState(row.state))
    if problem:
        raise ScanSessionError(f"Session {row.scan_session_id} is closed", user_message=problem)
    return row


def start_batch(
    database: ProjectDatabase,
    paths: Sequence[Path],
    *,
    identity: BatchIdentity,
    source_folder: Path | None = None,
    settings: dict[str, Any] | None = None,
    role: BatchRole = BatchRole.SCAN,
    scan_session_id: str | None = None,
    started_by: str = "",
    seal_previous: bool = True,
    acknowledge_template_change: bool = False,
    batch_id: str | None = None,
) -> str:
    """Register a new finite batch in a session - the Scan stage's entry point.

    Args:
        database: The open project database.
        paths: The scans, in batch order.
        identity: Template and engine the batch is read with.
        source_folder: Where its scans came from.
        settings: Run configuration, as for ``create_batch``.
        role: Why the batch exists.
        scan_session_id: The session to join; ``None`` means the active one,
            created implicitly (named after the exam and the date) when the
            project has none - the first *Process All* asks nothing.
        started_by: Who started it, for the audit ledger.
        seal_previous: Seal the session's other open batches first - the
            operator starting another batch is what makes the previous one
            final (ADR-0005, "the seal trigger").
        acknowledge_template_change: The operator saw that this batch's
            template differs from the session's pinned one and continued;
            recorded as an audit event. Without it a difference raises.
        batch_id: Override the generated id; for tests.

    Returns:
        The new batch id.

    Raises:
        ScanSessionError: The session is closed.
        TemplatePinError: The template differs from the session's pin and the
            change was not acknowledged.
    """
    with database.session() as session:
        return start_batch_in(
            session,
            paths,
            identity=identity,
            source_folder=source_folder,
            settings=settings,
            role=role,
            scan_session_id=scan_session_id,
            started_by=started_by,
            seal_previous=seal_previous,
            acknowledge_template_change=acknowledge_template_change,
            batch_id=batch_id,
        )


def start_batch_in(
    session: Session,
    paths: Sequence[Path],
    *,
    identity: BatchIdentity,
    source_folder: Path | None = None,
    settings: dict[str, Any] | None = None,
    role: BatchRole = BatchRole.SCAN,
    scan_session_id: str | None = None,
    started_by: str = "",
    seal_previous: bool = True,
    acknowledge_template_change: bool = False,
    batch_id: str | None = None,
) -> str:
    """:func:`start_batch` inside a transaction the caller owns.

    For a caller that must commit the new batch together with records of its
    own - intake registration links its ledger rows to the batch's scans in the
    same transaction (0.1.1 revised phase 5). Same rules, same audit events.
    """
    from omr_scanner.services import batch_store

    row = _open_session_for_new_batch(session, scan_session_id, created_by=started_by)
    differences = pin_differences(row, identity)
    if differences and not acknowledge_template_change:
        raise TemplatePinError(
            f"Template differs from scan session {row.scan_session_id}",
            user_message=" ".join(differences),
            differences=differences,
        )
    if seal_previous:
        for previous in session.scalars(
            select(ScanBatch.batch_id)
            .where(ScanBatch.scan_session_id == row.scan_session_id)
            .where(ScanBatch.sealed_at.is_(None))
        ).all():
            _seal(session, str(previous), actor=started_by, reason="another batch started")
    identifier = batch_store.insert_batch(
        session,
        paths,
        identity=identity,
        source_folder=source_folder,
        settings=settings,
        batch_id=batch_id,
        scan_session_id=row.scan_session_id,
        role=role.value,
    )
    if not row.template_id and not row.geometry_fingerprint:
        _pin(row, identity)
    elif differences:
        _audit(
            session,
            action=SessionAction.TEMPLATE_ACKNOWLEDGED,
            entity_type=SESSION_ENTITY,
            entity_id=row.scan_session_id,
            batch_id=identifier,
            actor=started_by,
            detail=" ".join(differences),
        )
    _audit(
        session,
        action=SessionAction.BATCH_ATTACHED,
        entity_type=BATCH_ENTITY,
        entity_id=identifier,
        batch_id=identifier,
        actor=started_by,
        new_value=row.scan_session_id,
        detail=f"role={role.value} scans={len(paths)}",
    )
    return identifier


def attach_new_batch(
    database: ProjectDatabase,
    paths: Sequence[Path],
    *,
    identity: BatchIdentity,
    source_folder: Path | None = None,
    settings: dict[str, Any] | None = None,
    batch_id: str | None = None,
) -> str:
    """What :func:`omr_scanner.services.batch_store.create_batch` does now.

    Attach to the active session (implicit creation), sealing nothing - the
    programmatic path the stress runner, tools and tests use. A template that
    differs from the active session's pin, or a closed active session, starts
    a **new** implicit session instead (audited by its creation): batches read
    under different rules are never mixed silently, and a programmatic caller
    has nobody to ask.
    """
    try:
        return start_batch(
            database,
            paths,
            identity=identity,
            source_folder=source_folder,
            settings=settings,
            batch_id=batch_id,
            seal_previous=False,
        )
    except ScanSessionError as exc:
        _LOGGER.info("Starting a new implicit scan session: %s", exc.user_message)
        fresh = create_scan_session(
            database, origin="implicit", created_by="", activate=True
        )
        return start_batch(
            database,
            paths,
            identity=identity,
            source_folder=source_folder,
            settings=settings,
            batch_id=batch_id,
            scan_session_id=fresh.scan_session_id,
            seal_previous=False,
        )


def batch_info(database: ProjectDatabase, batch_id: str) -> BatchInfo | None:
    """One batch's lifecycle position, or ``None``."""
    found = batches_info(database, (batch_id,))
    return found[0] if found else None


def batches_info(database: ProjectDatabase, batch_ids: Sequence[str]) -> tuple[BatchInfo, ...]:
    """Lifecycle positions for the named batches, in the order given."""
    wanted = list(dict.fromkeys(batch_ids))
    if not wanted:
        return ()
    with database.session() as session:
        if not has_lifecycle_schema(database):
            rows = session.execute(
                select(
                    ScanBatch.batch_id, ScanBatch.created_at, ScanBatch.status,
                    ScanBatch.total_scans,
                ).where(ScanBatch.batch_id.in_(wanted))
            ).all()
            by_id = {
                str(row[0]): BatchInfo(
                    batch_id=str(row[0]),
                    scan_session_id=f"{VIRTUAL_PREFIX}{row[0]}",
                    role=BatchRole.LEGACY,
                    membership=BatchMembership.OPEN,
                    created_at=row[1],
                    status=str(row[2]),
                    total_scans=int(row[3]),
                )
                for row in rows
            }
            return tuple(by_id[item] for item in wanted if item in by_id)
        live = _live(session)
        supersedes: dict[str, list[str]] = {}
        for old, new in live.items():
            supersedes.setdefault(new, []).append(old)
        rows_ = session.execute(
            select(
                ScanBatch.batch_id, ScanBatch.scan_session_id, ScanBatch.role,
                ScanBatch.sealed_at, ScanBatch.created_at, ScanBatch.status,
                ScanBatch.total_scans,
            ).where(ScanBatch.batch_id.in_(wanted))
        ).all()
        by_id = {
            str(row[0]): BatchInfo(
                batch_id=str(row[0]),
                scan_session_id=row[1],
                role=BatchRole(row[2]),
                membership=BatchMembership.of(row[3]),
                sealed_at=row[3],
                created_at=row[4],
                status=str(row[5]),
                total_scans=int(row[6]),
                superseded_by=live.get(str(row[0])),
                supersedes=tuple(sorted(supersedes.get(str(row[0]), ()))),
            )
            for row in rows_
        }
        return tuple(by_id[item] for item in wanted if item in by_id)


def batches_of(database: ProjectDatabase, scan_session_id: str) -> tuple[BatchInfo, ...]:
    """A session's batches, oldest first (bounded: one query for ids, one for facts)."""
    if scan_session_id.startswith(VIRTUAL_PREFIX):
        return batches_info(database, (scan_session_id.removeprefix(VIRTUAL_PREFIX),))
    with database.session() as session:
        ids = [
            str(item)
            for item in session.scalars(
                select(ScanBatch.batch_id)
                .where(ScanBatch.scan_session_id == scan_session_id)
                .order_by(ScanBatch.created_at, ScanBatch.batch_id)
            ).all()
        ]
    return batches_info(database, ids)


def session_of_batch(database: ProjectDatabase, batch_id: str) -> str | None:
    """The session a batch belongs to (a virtual id on a pre-session database)."""
    found = batch_info(database, batch_id)
    return found.scan_session_id if found is not None else None


# ----------------------------------------------------------------------
# Supersession
# ----------------------------------------------------------------------
def record_supersession(
    database: ProjectDatabase,
    superseded_batch_id: str,
    superseding_batch_id: str,
    *,
    reason: str,
    recorded_by: str = "",
) -> int:
    """Record that ``superseding`` replaces ``superseded``; return the record id.

    Nothing is deleted: the superseded batch, its sheets, results, conflicts
    and history stay exactly as they are. Session-level results (revised phase
    4) exclude it; until then a downstream stage simply reads another batch
    (:func:`downstream_batch_id`).

    Raises:
        ScanSessionError: The rules of
            :func:`~omr_scanner.domain.scan_sessions.supersession_problem` refuse
            it (self, cross-session, unsealed, already superseded, cycle).
    """
    with database.session() as session:
        record_id = _record_supersession(
            session, superseded_batch_id, superseding_batch_id, reason=reason, actor=recorded_by
        )
    return record_id


def _record_supersession(
    session: Session, old: str, new: str, *, reason: str, actor: str
) -> int:
    old_session, old_sealed, _ = _batch_row_facts(session, old)
    new_session, new_sealed, _ = _batch_row_facts(session, new)
    problem = supersession_problem(
        BatchFacts(old, old_session, old_sealed is not None),
        BatchFacts(new, new_session, new_sealed is not None),
        _live(session),
    )
    if problem:
        raise ScanSessionError(f"Supersession {old} -> {new} refused", user_message=problem)
    record = BatchSupersession(
        superseded_batch_id=old,
        superseding_batch_id=new,
        reason=reason.strip(),
        recorded_by=actor.strip(),
        recorded_at=_now(),
    )
    session.add(record)
    session.flush()
    _audit(
        session,
        action=SessionAction.SUPERSEDED,
        entity_type=BATCH_ENTITY,
        entity_id=old,
        batch_id=old,
        actor=actor,
        previous_value=old,
        new_value=new,
        reason=reason,
        detail=f"supersession {record.supersession_id}",
    )
    return int(record.supersession_id)


def reverse_supersession(
    database: ProjectDatabase, superseded_batch_id: str, *, reversed_by: str = "", reason: str = ""
) -> bool:
    """Reverse the live supersession of a batch. ``False`` when there is none.

    The record stays, marked reversed; the reversal is its own audit event.
    """
    with database.session() as session:
        record = session.scalars(
            select(BatchSupersession)
            .where(BatchSupersession.superseded_batch_id == superseded_batch_id)
            .where(BatchSupersession.reversed_at.is_(None))
        ).first()
        if record is None:
            return False
        record.reversed_at = _now()
        record.reversed_by = reversed_by.strip()
        record.reversal_reason = reason.strip()
        _audit(
            session,
            action=SessionAction.SUPERSESSION_REVERSED,
            entity_type=BATCH_ENTITY,
            entity_id=superseded_batch_id,
            batch_id=superseded_batch_id,
            actor=reversed_by,
            previous_value=record.superseding_batch_id,
            reason=reason,
            detail=f"supersession {record.supersession_id}",
        )
    return True


def live_supersessions(database: ProjectDatabase) -> dict[str, str]:
    """``superseded -> superseding`` for every live supersession."""
    if not has_lifecycle_schema(database):
        return {}
    with database.session() as session:
        return _live(session)


def start_reprocess_batch(
    database: ProjectDatabase,
    original_batch_id: str,
    *,
    identity: BatchIdentity,
    settings: dict[str, Any] | None = None,
    started_by: str = "",
    reason: str = "Reprocess All",
    acknowledge_template_change: bool = False,
) -> str:
    """*Reprocess All*: a new ``reprocess`` batch over the same files, superseding the original.

    One transaction: seal the original (if still open), create the reprocess
    batch in the original's session with the original's scans in their order,
    and record the supersession. The original batch is kept, whole.
    """
    from omr_scanner.services import batch_store

    paths = list(batch_store.scan_paths(database, original_batch_id))
    with database.session() as session:
        old_session, _sealed, _role = _batch_row_facts(session, original_batch_id)
        if old_session is None:
            raise ScanSessionError(
                "Batch has no session",
                user_message="This batch has not been assigned to a scan session yet.",
            )
        row = _open_session_for_new_batch(session, old_session, created_by=started_by)
        differences = pin_differences(row, identity)
        if differences and not acknowledge_template_change:
            raise TemplatePinError(
                f"Template differs from scan session {row.scan_session_id}",
                user_message=" ".join(differences),
                differences=differences,
            )
        for previous in session.scalars(
            select(ScanBatch.batch_id)
            .where(ScanBatch.scan_session_id == row.scan_session_id)
            .where(ScanBatch.sealed_at.is_(None))
        ).all():
            _seal(session, str(previous), actor=started_by, reason=reason)
        source = session.scalar(
            select(ScanBatch.source_folder).where(ScanBatch.batch_id == original_batch_id)
        )
        from pathlib import Path as _Path

        identifier = batch_store.insert_batch(
            session,
            paths,
            identity=identity,
            source_folder=_Path(source) if source else None,
            settings=settings,
            scan_session_id=row.scan_session_id,
            role=BatchRole.REPROCESS.value,
        )
        _audit(
            session,
            action=SessionAction.BATCH_ATTACHED,
            entity_type=BATCH_ENTITY,
            entity_id=identifier,
            batch_id=identifier,
            actor=started_by,
            new_value=row.scan_session_id,
            detail=f"role=reprocess of {original_batch_id} scans={len(paths)}",
        )
        if differences:
            _audit(
                session,
                action=SessionAction.TEMPLATE_ACKNOWLEDGED,
                entity_type=SESSION_ENTITY,
                entity_id=row.scan_session_id,
                batch_id=identifier,
                actor=started_by,
                detail=" ".join(differences),
            )
        _record_supersession(
            session, original_batch_id, identifier, reason=reason, actor=started_by
        )
    return identifier


# ----------------------------------------------------------------------
# The batch a downstream stage reads (until session-level results)
# ----------------------------------------------------------------------
_NOT_YET_READABLE = (BatchStatus.NEW.value, BatchStatus.RUNNING.value)


def downstream_batch_id(database: ProjectDatabase) -> str | None:
    """The population key Attendance, Results and Reports read for the active session.

    **Since 0.1.1 phase 4 this names a whole scan session, not one batch.**
    It returns the active session's *population key*
    (:func:`omr_scanner.services.session_population.population_key`) - the
    one ``batch_id`` value the session's downstream state is stored under -
    and every downstream service expands it to the session's effective sheet
    set, from all of its batches. ``None`` while the active session has no
    batch that has been (or is being) read: only ``new`` or ``running``
    batches, or none at all.

    With **no** active session, the newest batch that belongs to no session
    yet and is readable - a batch written before the upgrade backfill ran (or
    directly, by a tool) - is its own one-batch population. On a pre-session
    project opened read-only: the newest batch by creation.

    Never chosen by ``updated_at`` (defect 2's cause): a retry or
    ``recover_interrupted`` cannot move it, and nor can a later batch, a
    rescan batch or a reprocess batch - those change the population, not its
    key.
    """
    if not has_lifecycle_schema(database):
        with database.session() as session:
            found = session.scalar(
                select(ScanBatch.batch_id)
                .order_by(ScanBatch.created_at.desc(), ScanBatch.batch_id.desc())
                .limit(1)
            )
        return str(found) if found is not None else None
    with database.session() as session:
        pointer = _setting(session, SettingKey.ACTIVE_SCAN_SESSION)
        if pointer and session.get(ScanSession, pointer) is None:
            pointer = None
        live = _live(session)
        owner = (
            ScanBatch.scan_session_id == pointer
            if pointer
            else ScanBatch.scan_session_id.is_(None)
        )
        rows = session.execute(
            select(ScanBatch.batch_id, ScanBatch.role, ScanBatch.status)
            .where(owner)
            .order_by(ScanBatch.created_at.desc(), ScanBatch.batch_id.desc())
        ).all()
    from omr_scanner.services import session_population

    if pointer:
        if not any(str(status) not in _NOT_YET_READABLE for _b, _r, status in rows):
            return None
        return session_population.session_key(database, pointer)
    for batch_id, role, status in rows:
        if (
            BatchRole(role).is_primary
            and str(batch_id) not in live
            and str(status) not in _NOT_YET_READABLE
        ):
            return session_population.population_key(database, str(batch_id))
    return None


def downstream_session_id(database: ProjectDatabase) -> str | None:
    """The scan session Attendance, Results and Reports select by default.

    The **active** session, once it has a batch that has been (or is being)
    read - the session-level counterpart of :func:`downstream_batch_id`,
    which names that session's downstream store. ``batch:<id>`` for a batch
    that belongs to no session yet (before the upgrade backfill). ``None``
    when there is nothing to show. Pages hold this id and resolve the store
    from it (:mod:`omr_scanner.services.session_scope`).
    """
    from omr_scanner.services import session_population

    key = downstream_batch_id(database)
    if key is None:
        return None
    return session_population.session_of_batch(database, key)


def describe_downstream(database: ProjectDatabase, batch_id: str | None) -> str:
    """One sentence for a stage header naming the session and what it holds.

    Since 0.1.1 phase 4 the stages read the whole session's effective sheet
    set, so this says how many scripts count and from how many batches.
    """
    if batch_id is None:
        return ""
    from omr_scanner.services import session_population

    population = session_population.population(database, batch_id)
    found = (
        None
        if population.session_id.startswith(session_population.LONE_BATCH_PREFIX)
        else get_scan_session(database, population.session_id)
    )
    name = found.name if found is not None else f"batch {batch_id[:8]}"
    return f"Scan session '{name}': {session_population.describe(population)}."


# ----------------------------------------------------------------------
# Combine
# ----------------------------------------------------------------------
def downstream_holders(
    database: ProjectDatabase, session_ids: Sequence[str]
) -> dict[str, str]:
    """``session id -> its downstream store`` for each of ``session_ids`` holding any.

    A session "holds downstream state" once Attendance has reconciled or
    Results has scored against it - the operator decisions a combine must not
    silently lose (0.1.1 phase 4).
    """
    from omr_scanner.services import session_population

    found: dict[str, str] = {}
    for scan_session_id in dict.fromkeys(session_ids):
        store = session_population.held_store(database, scan_session_id)
        if store is not None:
            found[scan_session_id] = store
    return found


def combine_problems(
    database: ProjectDatabase,
    source_ids: Sequence[str],
    target_id: str,
    *,
    keep_downstream_of: str | None = None,
) -> tuple[str, ...]:
    """Every reason *Combine into one session* would be refused (empty = allowed).

    Checked with what is safely known now, no more: the sessions exist and
    belong to this project; the target is open; every batch matches the
    target's pinned template identity; and no **effective** (non-superseded)
    batch of a source would bring a sheet already effective in the target -
    the same file path, or the same content hash where one was recorded.
    Session-level duplicate *identity* (two scripts for one candidate) is the
    effective-scan-set service's to judge, later.

    **Downstream decisions (0.1.1 phase 4).** A combined session has one
    downstream store. When more than one of the sessions already holds
    Attendance / Results state, combining would leave all but one session's
    reconciliation decisions outside the combined session; that is refused
    unless the operator names, in ``keep_downstream_of``, the session whose
    decisions the combined session keeps (the others stay in the database as
    history and the choice is audited).
    """
    problems: list[str] = []
    sources = [item for item in dict.fromkeys(source_ids) if item != target_id]
    if not sources:
        return ("Choose at least one other scan session to combine into this one.",)
    holders = downstream_holders(database, [target_id, *sources])
    if len(holders) > 1 and keep_downstream_of not in holders:
        with database.session() as session:
            names = [
                (row.name if row is not None else item[:8])
                for item in holders
                for row in (session.get(ScanSession, item),)
            ]
        problems.append(
            "More than one of these scan sessions already has Attendance or Results "
            f"decisions ({", ".join(repr(item) for item in names)}). A combined session "
            "keeps one session's decisions; choose which, or the combine is refused."
        )
    elif keep_downstream_of is not None and keep_downstream_of not in holders:
        problems.append("The session chosen to keep decisions from holds none.")
    with database.session() as session:
        project_id = _project_id(session)
        target = session.get(ScanSession, target_id)
        if target is None:
            return ("The target scan session no longer exists.",)
        if target.project_id and target.project_id != project_id:
            problems.append("The target scan session belongs to a different project.")
        if ScanSessionState(target.state) is not ScanSessionState.OPEN:
            problems.append(f"'{target.name}' is closed; reopen it before combining into it.")
        live = _live(session)

        def effective_members(scan_session_id: str) -> tuple[set[str], set[str], list[str]]:
            ids = [
                str(item)
                for item in session.scalars(
                    select(ScanBatch.batch_id).where(ScanBatch.scan_session_id == scan_session_id)
                ).all()
            ]
            effective = [item for item in ids if item not in live]
            paths: set[str] = set()
            hashes: set[str] = set()
            if effective:
                for path, digest in session.execute(
                    select(BatchScan.source_path, BatchScan.content_sha256).where(
                        BatchScan.batch_id.in_(effective)
                    )
                ).all():
                    paths.add(str(path).casefold())
                    if digest:
                        hashes.add(str(digest))
            return paths, hashes, ids

        target_paths, target_hashes, _ = effective_members(target_id)
        for source_id in sources:
            source = session.get(ScanSession, source_id)
            if source is None:
                problems.append(f"Scan session {source_id[:8]} no longer exists.")
                continue
            if source.project_id and source.project_id != project_id:
                problems.append(f"'{source.name}' belongs to a different project.")
            for batch_id, template_id, geometry, recognition, engine in session.execute(
                select(
                    ScanBatch.batch_id, ScanBatch.template_id, ScanBatch.geometry_fingerprint,
                    ScanBatch.recognition_fingerprint, ScanBatch.engine_version,
                ).where(ScanBatch.scan_session_id == source_id)
            ).all():
                if target.template_id and (template_id, geometry, recognition, engine) != (
                    target.template_id,
                    target.geometry_fingerprint,
                    target.recognition_fingerprint,
                    target.engine_version,
                ):
                    problems.append(
                        f"Batch {str(batch_id)[:8]} of '{source.name}' was read with a "
                        f"different template identity from '{target.name}'."
                    )
            paths, hashes, _ = effective_members(source_id)
            shared_paths = paths & target_paths
            shared_hashes = hashes & target_hashes
            if shared_paths or shared_hashes:
                problems.append(
                    f"'{source.name}' and '{target.name}' both hold "
                    f"{max(len(shared_paths), len(shared_hashes))} identical effective "
                    "sheet(s); combining them would count those sheets twice."
                )
            target_paths |= paths
            target_hashes |= hashes
    return tuple(problems)


def combine_scan_sessions(
    database: ProjectDatabase,
    source_ids: Sequence[str],
    target_id: str,
    *,
    combined_by: str,
    reason: str = "",
    keep_downstream_of: str | None = None,
) -> CombineOutcome:
    """Move every batch of the source sessions into the open target session.

    Explicit and operator-initiated only - nothing ever calls this
    automatically. Refused, listing every reason, when
    :func:`combine_problems` finds any. A source session left empty is closed
    and records :attr:`~omr_scanner.database.models.ScanSession.merged_into_session_id`.
    Each moved batch keeps its role, seal and supersession records; one audit
    event per moved batch names its previous session, which is what an
    operator's reversal would need. No reversal command exists yet.
    """
    if not combined_by.strip():
        raise ScanSessionError(
            "Combine needs an operator",
            user_message="Set your name in File > Settings before combining scan sessions.",
        )
    problems = combine_problems(
        database, source_ids, target_id, keep_downstream_of=keep_downstream_of
    )
    if problems:
        raise ScanSessionError(
            "Combine refused", user_message="Cannot combine: " + " ".join(problems)
        )
    sources = [item for item in dict.fromkeys(source_ids) if item != target_id]
    holders = downstream_holders(database, [target_id, *sources])
    kept = keep_downstream_of if keep_downstream_of in holders else next(iter(holders), None)
    moved: list[str] = []
    moment = _now()
    with database.session() as session:
        target = _require(session, target_id)
        for source_id in sources:
            source = _require(session, source_id)
            for batch_id in session.scalars(
                select(ScanBatch.batch_id).where(ScanBatch.scan_session_id == source_id)
            ).all():
                session.execute(
                    update(ScanBatch)
                    .where(ScanBatch.batch_id == batch_id)
                    .values(scan_session_id=target_id)
                )
                moved.append(str(batch_id))
                _audit(
                    session,
                    action=SessionAction.COMBINED,
                    entity_type=BATCH_ENTITY,
                    entity_id=str(batch_id),
                    batch_id=str(batch_id),
                    actor=combined_by,
                    previous_value=source_id,
                    new_value=target_id,
                    reason=reason,
                )
            source.merged_into_session_id = target_id
            if ScanSessionState(source.state) is ScanSessionState.OPEN:
                source.state = ScanSessionState.CLOSED.value
                source.closed_at = moment
                source.closed_by = combined_by.strip()
            _audit(
                session,
                action=SessionAction.COMBINED,
                entity_type=SESSION_ENTITY,
                entity_id=source_id,
                actor=combined_by,
                previous_value=source_id,
                new_value=target_id,
                reason=reason,
                detail=f"moved {len(moved)} batch(es) so far into '{target.name}'",
            )
            if _setting(session, SettingKey.ACTIVE_SCAN_SESSION) == source_id:
                _activate(session, target_id, actor=combined_by)
        if has_scope_schema(database):
            # The combined session's store is a recorded choice, never the
            # derivation rule over the merged batch list (which could move).
            target.downstream_batch_id = holders[kept] if kept is not None else None
            for source_id in sources:
                _require(session, source_id).downstream_batch_id = None
            if len(holders) > 1 and kept is not None:
                _audit(
                    session,
                    action=SessionAction.COMBINED,
                    entity_type=SESSION_ENTITY,
                    entity_id=target_id,
                    actor=combined_by,
                    previous_value=",".join(sorted(holders)),
                    new_value=kept,
                    reason=reason,
                    detail=(
                        f"downstream decisions kept from session {kept[:8]} (store "
                        f"{holders[kept][:8]}); the other session(s)' decisions are "
                        "retained as history under "
                        + ", ".join(
                            store[:8] for owner, store in holders.items() if owner != kept
                        )
                    ),
                )
        if not target.template_id and moved:
            first = session.execute(
                select(
                    ScanBatch.template_id, ScanBatch.template_name, ScanBatch.geometry_fingerprint,
                    ScanBatch.recognition_fingerprint, ScanBatch.engine_version,
                )
                .where(ScanBatch.scan_session_id == target_id)
                .order_by(ScanBatch.created_at)
                .limit(1)
            ).one()
            (
                target.template_id, target.template_name, target.geometry_fingerprint,
                target.recognition_fingerprint, target.engine_version,
            ) = first
    if moved:
        # The combined session is a new population (0.1.1 phase 4): its
        # duplicate IDs and existing reconciliations are re-derived over it.
        # A combine is rare and explicit, so this is the full rebuild.
        from omr_scanner.services import scan_lifecycle

        scan_lifecycle.refresh_session_after_combine(database, moved[0])
    return CombineOutcome(
        target_id=target_id, moved_batches=tuple(moved), emptied_sessions=tuple(sources)
    )


# ----------------------------------------------------------------------
# Upgrade backfill
# ----------------------------------------------------------------------
def backfill_legacy_batches(database: ProjectDatabase) -> BackfillReport | None:
    """Put every pre-session batch in a session; run on a writable open after migration 14.

    ``None`` when there was nothing to do (every batch already has a session).
    Idempotent - only batches with no session are considered.

    The rule (:func:`~omr_scanner.domain.scan_sessions.plan_backfill`): each
    batch becomes its own one-batch ``legacy`` session, except that two
    batches joined by an unambiguous confirmed cross-batch rescan share one;
    unrelated batches are never combined; ambiguous relationships stay
    separate and are written to the upgrade report
    (``project_setting['scan_session_backfill']``) and the audit ledger. Every
    backfilled batch is **sealed** - it is history - and the session holding
    the most recently created batch becomes the active session, so that
    Attendance, Results and Reports go on reading the batch they read before
    (unless a newer batch exists elsewhere, which the report lists).
    """
    if database.read_only or not has_lifecycle_schema(database):
        return None
    moment = _now()
    with database.session() as session:
        rows = session.execute(
            select(
                ScanBatch.batch_id, ScanBatch.created_at, ScanBatch.template_id,
                ScanBatch.geometry_fingerprint, ScanBatch.recognition_fingerprint,
                ScanBatch.engine_version,
            ).where(ScanBatch.scan_session_id.is_(None))
        ).all()
        if not rows:
            return None
        legacy = [
            LegacyBatch(
                batch_id=str(row[0]),
                created_at=row[1],
                template_identity=(str(row[2]), str(row[3]), str(row[4]), str(row[5])),
            )
            for row in rows
        ]
        wanted = {item.batch_id for item in legacy}
        links: list[ReplacementLink] = []
        rejection_rows = session.execute(
            select(ScanRejection.batch_id, ScanRejection.replacement_scan_id).where(
                ScanRejection.state == LifecycleState.SUPERSEDED_BY_REPLACEMENT.value
            )
        ).all()
        replacement_ids = [int(item[1]) for item in rejection_rows if item[1] is not None]
        batch_of_scan = (
            {
                int(scan_id): str(batch_id)
                for scan_id, batch_id in session.execute(
                    select(BatchScan.scan_id, BatchScan.batch_id).where(
                        BatchScan.scan_id.in_(replacement_ids)
                    )
                ).all()
            }
            if replacement_ids
            else {}
        )
        for original_batch, replacement_scan in rejection_rows:
            if replacement_scan is None:
                continue
            replacement_batch = batch_of_scan.get(int(replacement_scan))
            if (
                replacement_batch is not None
                and replacement_batch != original_batch
                and original_batch in wanted
                and replacement_batch in wanted
            ):
                links.append(ReplacementLink(str(original_batch), replacement_batch))
        plan = plan_backfill(legacy, links)
        by_id = {item.batch_id: item for item in legacy}
        # In a grouped pair, a newer batch holding nothing but confirmed
        # replacements for the older one is what this release calls a
        # `rescan` batch; labelling it so keeps the downstream single-batch
        # rule on the original, where those replacements are counted.
        replacements_into: dict[str, int] = {}
        for link in links:
            replacements_into[link.replacement_batch] = (
                replacements_into.get(link.replacement_batch, 0) + 1
            )
        members_of = {
            str(batch_id): int(count)
            for batch_id, count in session.execute(
                select(BatchScan.batch_id, func.count())
                .where(BatchScan.batch_id.in_(list(wanted)))
                .group_by(BatchScan.batch_id)
            ).all()
        }
        newest_session = ""
        newest_created: datetime | None = None
        for group in plan.groups:
            first = by_id[group[0]]
            label = first.created_at.astimezone().strftime("%Y-%m-%d %H:%M")
            name = (
                f"Legacy batch {group[0][:8]} ({label})"
                if len(group) == 1
                else f"Legacy batches {', '.join(item[:8] for item in group)} ({label})"
            )
            row = _new_session_row(
                session, name=name, origin="backfill", created_by="", moment=moment,
                notes="Created by the 0.1.1 upgrade from a batch that predates scan sessions.",
            )
            template = session.execute(
                select(
                    ScanBatch.template_id, ScanBatch.template_name, ScanBatch.geometry_fingerprint,
                    ScanBatch.recognition_fingerprint, ScanBatch.engine_version,
                ).where(ScanBatch.batch_id == group[0])
            ).one()
            (
                row.template_id, row.template_name, row.geometry_fingerprint,
                row.recognition_fingerprint, row.engine_version,
            ) = template
            for position, batch_id in enumerate(group):
                role = BatchRole.LEGACY
                if (
                    len(group) > 1
                    and position > 0
                    and replacements_into.get(batch_id, 0) >= members_of.get(batch_id, 0) > 0
                ):
                    role = BatchRole.RESCAN
                session.execute(
                    update(ScanBatch)
                    .where(ScanBatch.batch_id == batch_id)
                    .values(scan_session_id=row.scan_session_id, role=role.value)
                )
                _seal(session, batch_id, reason="upgrade backfill")
                _audit(
                    session,
                    action=SessionAction.BACKFILLED,
                    entity_type=BATCH_ENTITY,
                    entity_id=batch_id,
                    batch_id=batch_id,
                    new_value=row.scan_session_id,
                    detail=(
                        "grouped by confirmed cross-batch rescan"
                        if len(group) > 1
                        else "own legacy session"
                    ),
                )
                created = by_id[batch_id].created_at
                if newest_created is None or created > newest_created:
                    newest_created, newest_session = created, row.scan_session_id
        if newest_session and not _setting(session, SettingKey.ACTIVE_SCAN_SESSION):
            _activate(session, newest_session)
        report = BackfillReport(
            sessions_created=len(plan.groups),
            batches_assigned=len(legacy),
            grouped=tuple(group for group in plan.groups if len(group) > 1),
            ambiguous=plan.ambiguous,
            ran_at=moment.isoformat(timespec="seconds"),
        )
        _set_setting(session, SettingKey.SCAN_SESSION_BACKFILL, report.as_json())
    _LOGGER.info(
        "Scan-session backfill: %d batch(es) into %d session(s); %d ambiguous case(s)",
        report.batches_assigned,
        report.sessions_created,
        len(report.ambiguous),
    )
    return report


def backfill_report(database: ProjectDatabase) -> dict[str, Any] | None:
    """The stored upgrade report, or ``None`` when no backfill has run."""
    if not has_lifecycle_schema(database):
        return None
    with database.session() as session:
        raw = _setting(session, SettingKey.SCAN_SESSION_BACKFILL)
    return json.loads(raw) if raw else None


__all__ = [
    "LIFECYCLE_SCHEMA_VERSION",
    "VIRTUAL_PREFIX",
    "BackfillReport",
    "BatchInfo",
    "CombineOutcome",
    "ScanSessionError",
    "ScanSessionInfo",
    "TemplatePinError",
    "active_scan_session",
    "attach_new_batch",
    "backfill_legacy_batches",
    "backfill_report",
    "batch_info",
    "batches_info",
    "batches_of",
    "close_scan_session",
    "combine_problems",
    "combine_scan_sessions",
    "create_scan_session",
    "describe_downstream",
    "downstream_batch_id",
    "get_scan_session",
    "has_lifecycle_schema",
    "list_scan_sessions",
    "live_supersessions",
    "pin_differences",
    "record_supersession",
    "rename_scan_session",
    "reopen_scan_session",
    "reverse_supersession",
    "seal_batch",
    "session_of_batch",
    "set_active_scan_session",
    "start_batch",
    "start_reprocess_batch",
]
