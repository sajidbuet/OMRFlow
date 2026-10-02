"""Intake sources and the intake ledger: the headless service (0.1.1 revised phase 5).

Purpose:
    Know, durably and restart-safely, what every intake source has produced;
    prove when a file is complete and safe to consume; keep path and provenance
    apart from content identity; and hand each valid, unique file to the
    processing boundary **exactly once** - without becoming the processing
    engine (``ARCHITECTURE_NOTES.md`` §10, roadmap phase 0.1.1-D,
    ``docs/intake.md``, ADR-0008).

Responsibilities:
    * Sources: :func:`create_source`, :func:`update_source`,
      :func:`set_source_enabled`, :func:`attach_source`, :func:`detach_source`,
      :func:`manual_source` (the built-in source, created lazily) - each change
      audited.
    * :class:`IntakeService` - :meth:`~IntakeService.reconcile` (authoritative
      ``os.scandir`` reconciliation, stabilisation, one-read hash-and-decode
      verification, reachability), :meth:`~IntakeService.recover_after_restart`,
      :meth:`~IntakeService.ready_items` and :meth:`~IntakeService.register`
      (the registration API phase 6 calls).
    * Manual convergence: :func:`record_manual_batch` and
      :func:`link_exact_duplicates` - *Add Folder -> Process All* records into
      the built-in manual source through the same content hash
      (:func:`~omr_scanner.services.scan_provenance.hash_file`), the same ledger
      and the same phase 4 duplicate rule.

What does NOT belong here:
    * Qt, notifications, a background thread or timer. Reconciliation runs when
      it is called; the continuous engine is phase 6.
    * Deciding *when* to register or how many files make a unit (phase 6), or
      what to do with a held, unreadable or duplicate file (phase 7).
    * Recognition state. Once registered, a file's processing state is its
      ``batch_scan.status``.

Transactions:
    No transaction is held across a directory listing, a file read, hashing,
    decoding or copying. Each pass is: list (no transaction) -> one short
    transaction applying the observations -> read and verify each due file (no
    transaction) -> one short transaction applying the verdicts, each checked
    against the row as it was read (compare-and-set). A kill at any point leaves
    rows :meth:`IntakeService.recover_after_restart` and the next pass handle.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, true, update

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    IntakeFile,
    IntakeSource,
    IntakeSourceAttachment,
    ProjectSetting,
    ScanBatch,
    ScanJobStatus,
    ScanRejection,
    ScanSession,
    SettingKey,
)
from omr_scanner.domain.intake import (
    IGNORE_REASONS,
    MANUAL_POLICY,
    MANUAL_SOURCE_LABEL,
    SOURCE_ENTITY,
    Exclusions,
    IngestMode,
    IntakeReason,
    IntakeState,
    ObservedFile,
    Reachability,
    SourceAction,
    SourceKind,
    SourceListing,
    StabilityPolicy,
    classify_name,
    default_policy,
    due_for_verification,
    require_transition,
)
from omr_scanner.domain.scan_sessions import BatchRole, ScanSessionState
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import scan_provenance
from omr_scanner.services.image_integrity import check_image_bytes
from omr_scanner.services.intake_fs import (
    IngestError,
    IngestStore,
    IntakeFileSystem,
    OsFileSystem,
    SourceListingError,
)
from omr_scanner.services.scan_import import SUPPORTED_SCAN_SUFFIXES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.services.batch_store import BatchIdentity

_LOGGER = logging.getLogger(__name__)

INTAKE_SCHEMA_VERSION = 16
"""The first schema with intake sources and the ledger."""

Clock = Callable[[], datetime]
"""Returns the current time, timezone-aware UTC. Injected by tests."""

def _seen_settled(row: IntakeFile, moment: datetime) -> None:
    """A settled row seen again: write only if it was absent.

    ``last_seen_at`` of a settled (terminal) row records when its presence was
    last *confirmed by a change* - first seen, or seen again after being
    absent. A present, unchanged settled row is not rewritten on every pass:
    rewriting ten thousand rows every poll would cost a journal write per row
    for no information, since the source's ``last_reconciled_at`` already says
    when it was last seen. Unsettled and ready rows are stamped every pass.
    """
    if not row.present:
        row.present = True
        row.last_seen_at = moment


class IntakeError(OMRScannerError):
    """An intake operation was refused. Always carries a ``user_message``."""


def utc_now() -> datetime:
    """The production clock."""
    return datetime.now(UTC)


def _aware(moment: datetime | None) -> datetime | None:
    """SQLite returns stored datetimes naive; every one written here is UTC."""
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def has_intake_schema(database: ProjectDatabase) -> bool:
    """Whether the database has intake tables (schema 16 or later)."""
    return database.schema_version >= INTAKE_SCHEMA_VERSION


def _require_writable(database: ProjectDatabase) -> None:
    if database.read_only:
        raise IntakeError(
            "intake on a read-only project",
            user_message="This project is open read-only; intake cannot record anything.",
        )
    if not has_intake_schema(database):
        raise IntakeError(
            "intake schema missing",
            user_message="This project has not been upgraded for intake sources yet.",
        )


# ----------------------------------------------------------------------
# Views
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SourceInfo:
    """One source as callers and tests see it (detached from the ORM)."""

    source_id: str
    label: str
    kind: SourceKind
    root_path: str
    recursive: bool
    exclusions: Exclusions
    policy: StabilityPolicy
    ingest_mode: IngestMode
    enabled: bool
    is_builtin: bool
    reachability: Reachability
    reachability_detail: str = ""
    reachability_changed_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    last_file_seen_at: datetime | None = None
    last_file_seen_path: str = ""
    attached_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One intake ledger row, detached. Every field is the persisted column."""

    intake_file_id: int
    source_id: str
    scan_session_id: str | None
    relative_path: str
    absolute_path: str
    file_name: str
    file_size: int
    mtime_ns: int
    content_sha256: str | None
    state: IntakeState
    state_reason: IntakeReason
    detail: str
    first_seen_at: datetime
    last_seen_at: datetime
    stable_since: datetime | None
    observations: int
    attempts: int
    ready_at: datetime | None
    verified_at: datetime | None
    reverify_required: bool
    present: bool
    is_current: bool
    path_reused: bool
    previous_intake_file_id: int | None
    image_format: str
    page_count: int
    batch_scan_id: int | None
    duplicate_of_scan_id: int | None
    ingest_path: str
    registered_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReadyItem:
    """A file phase 6 may register: ready, verified, intended for the session.

    Attributes:
        intake_file_id: The ledger row.
        source_id: Its source.
        scan_session_id: The session it is intended for.
        relative_path: Provenance.
        absolute_path: Where it was observed.
        content_sha256: The verified hash.
        file_size: Bytes.
        ready_at: When it became ready - the first key of the stable order.
    """

    intake_file_id: int
    source_id: str
    scan_session_id: str
    relative_path: str
    absolute_path: str
    content_sha256: str
    file_size: int
    ready_at: datetime


@dataclass(slots=True)
class ReconcileReport:
    """What one reconciliation of one source did (counts of ledger changes)."""

    source_id: str
    reachability: Reachability
    detail: str = ""
    listed: int = 0
    new_rows: int = 0
    changed: int = 0
    vanished: int = 0
    reappeared: int = 0
    ignored: int = 0
    verified: int = 0
    ready: int = 0
    held: int = 0
    unreadable: int = 0
    unsupported: int = 0
    unchanged_content: int = 0
    not_yet: int = 0
    """Due files that could not be read yet (locked, denied) or changed while read."""
    unlisted_folders: tuple[str, ...] = ()

    @property
    def online(self) -> bool:
        """Whether the listing succeeded."""
        return self.reachability is Reachability.ONLINE


@dataclass(frozen=True, slots=True)
class Registration:
    """What one :meth:`IntakeService.register` call did.

    Attributes:
        batch_id: The finite batch created, or ``None`` when nothing was
            registered.
        registered: ``(intake_file_id, scan_id)`` per newly registered file, in
            batch order.
        duplicates: ``(intake_file_id, original_scan_id)`` per file linked as
            exact-duplicate content (never recognised).
        already_registered: Ids that were registered by an earlier call - the
            idempotent repeat.
        returned: Ids sent back to stabilising or marked vanished because the
            source no longer matched the verified bytes.
        failed: ``(intake_file_id, detail)`` per file left ready because its
            copy could not be written (disk full, permission).
        held: Ids diverted to ``held`` because the session is closed.
    """

    batch_id: str | None
    registered: tuple[tuple[int, int], ...] = ()
    duplicates: tuple[tuple[int, int], ...] = ()
    already_registered: tuple[int, ...] = ()
    returned: tuple[int, ...] = ()
    failed: tuple[tuple[int, str], ...] = ()
    held: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """What :meth:`IntakeService.recover_after_restart` reset or completed."""

    restabilise: int = 0
    reverify: int = 0
    duplicates_completed: int = 0
    temporaries_removed: int = 0


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def _new_id() -> str:
    return uuid.uuid4().hex


def _project_id(session: Session) -> str:
    row = session.get(ProjectSetting, SettingKey.PROJECT_ID)
    return row.value if row is not None else ""


def _audit(
    session: Session,
    *,
    action: SourceAction,
    source_id: str,
    moment: datetime,
    actor: str = "",
    previous_value: str = "",
    new_value: str = "",
    detail: str = "",
) -> None:
    session.add(
        AuditEvent(
            occurred_at=moment,
            batch_id="",
            scan_id=0,
            conflict_id=0,
            entity_type=SOURCE_ENTITY,
            entity_id=source_id,
            action=action.value,
            reviewer=actor.strip(),
            previous_value=previous_value,
            new_value=new_value,
            reason_text="",
            detail=detail,
        )
    )


def _live_attachment(session: Session, source_id: str) -> IntakeSourceAttachment | None:
    return session.scalars(
        select(IntakeSourceAttachment)
        .where(IntakeSourceAttachment.source_id == source_id)
        .where(IntakeSourceAttachment.detached_at.is_(None))
    ).first()


def _source_info(session: Session, row: IntakeSource) -> SourceInfo:
    attachment = _live_attachment(session, row.source_id)
    policy = (
        StabilityPolicy.from_json(row.policy_json, default=default_policy(row.root_path))
        if row.kind == SourceKind.WATCHED.value
        else MANUAL_POLICY
    )
    return SourceInfo(
        source_id=row.source_id,
        label=row.label,
        kind=SourceKind(row.kind),
        root_path=row.root_path,
        recursive=row.recursive,
        exclusions=Exclusions.from_json(row.exclusions_json),
        policy=policy,
        ingest_mode=IngestMode(row.ingest_mode),
        enabled=row.enabled,
        is_builtin=row.is_builtin,
        reachability=Reachability(row.reachability),
        reachability_detail=row.reachability_detail,
        reachability_changed_at=_aware(row.reachability_changed_at),
        last_attempt_at=_aware(row.last_attempt_at),
        last_reconciled_at=_aware(row.last_reconciled_at),
        last_file_seen_at=_aware(row.last_file_seen_at),
        last_file_seen_path=row.last_file_seen_path,
        attached_session_id=attachment.scan_session_id if attachment is not None else None,
    )


def _require_source(session: Session, source_id: str) -> IntakeSource:
    row = session.get(IntakeSource, source_id)
    if row is None:
        raise IntakeError(
            f"No intake source {source_id!r}",
            user_message="That intake source no longer exists in this project.",
        )
    return row


def _ledger_row(row: IntakeFile) -> LedgerRow:
    return LedgerRow(
        intake_file_id=row.intake_file_id,
        source_id=row.source_id,
        scan_session_id=row.scan_session_id,
        relative_path=row.relative_path,
        absolute_path=row.absolute_path,
        file_name=row.file_name,
        file_size=row.file_size,
        mtime_ns=row.mtime_ns,
        content_sha256=row.content_sha256,
        state=IntakeState(row.state),
        state_reason=IntakeReason(row.state_reason),
        detail=row.detail,
        first_seen_at=_aware(row.first_seen_at) or row.first_seen_at,
        last_seen_at=_aware(row.last_seen_at) or row.last_seen_at,
        stable_since=_aware(row.stable_since),
        observations=row.observations,
        attempts=row.attempts,
        ready_at=_aware(row.ready_at),
        verified_at=_aware(row.verified_at),
        reverify_required=row.reverify_required,
        present=row.present,
        is_current=row.is_current,
        path_reused=row.path_reused,
        previous_intake_file_id=row.previous_intake_file_id,
        image_format=row.image_format,
        page_count=row.page_count,
        batch_scan_id=row.batch_scan_id,
        duplicate_of_scan_id=row.duplicate_of_scan_id,
        ingest_path=row.ingest_path,
        registered_at=_aware(row.registered_at),
    )


def _move(
    row: IntakeFile,
    target: IntakeState,
    moment: datetime,
    reason: IntakeReason = IntakeReason.NONE,
    detail: str = "",
) -> None:
    """Change a row's state - only along :data:`~omr_scanner.domain.intake.TRANSITIONS`."""
    current = IntakeState(row.state)
    require_transition(current, target)
    if current is not target:
        row.state_changed_at = moment
    row.state = target.value
    row.state_reason = reason.value
    row.detail = detail


def _new_row(
    session: Session,
    *,
    source_id: str,
    observed: ObservedFile,
    state: IntakeState,
    reason: IntakeReason,
    moment: datetime,
    scan_session_id: str | None,
    previous: IntakeFile | None = None,
    detail: str = "",
) -> IntakeFile:
    require_transition(None, state)
    row = IntakeFile(
        source_id=source_id,
        scan_session_id=scan_session_id,
        relative_path=observed.relative_path,
        absolute_path=observed.absolute_path,
        file_name=observed.relative_path.rpartition("/")[2],
        file_size=observed.size,
        mtime_ns=observed.mtime_ns,
        content_sha256=None,
        state=state.value,
        state_reason=reason.value,
        detail=detail,
        state_changed_at=moment,
        first_seen_at=moment,
        last_seen_at=moment,
        stable_since=moment,
        observations=1,
        attempts=0,
        present=True,
        is_current=True,
        path_reused=bool(previous is not None and IntakeState(previous.state).consumed_content),
        previous_intake_file_id=previous.intake_file_id if previous is not None else None,
    )
    session.add(row)
    return row


def _closed_sessions(session: Session) -> set[str]:
    return set(
        session.scalars(
            select(ScanSession.scan_session_id).where(
                ScanSession.state == ScanSessionState.CLOSED.value
            )
        ).all()
    )


# ----------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------
def create_source(
    database: ProjectDatabase,
    *,
    label: str,
    root_path: str,
    recursive: bool = False,
    exclusions: Exclusions | None = None,
    policy: StabilityPolicy | None = None,
    ingest_mode: IngestMode = IngestMode.COPY,
    enabled: bool = True,
    created_by: str = "",
    clock: Clock = utc_now,
) -> SourceInfo:
    """Create a watched source (project-level). Audited.

    Args:
        database: The open, writable project database.
        label: A human name (*Scanner A*). Need not be unique; never identity.
        root_path: The folder, local or UNC, stored exactly as given.
        recursive: Include supported files in sub-folders.
        exclusions: File and folder patterns to ignore.
        policy: Stabilisation thresholds; ``None`` stores nothing, meaning the
            default for the path (network for UNC).
        ingest_mode: ADR-0008: ``copy`` (default for watched sources) or
            ``reference``.
        enabled: Whether reconciliation lists it.
        created_by: Who, for the audit ledger.
        clock: Injected time.
    """
    _require_writable(database)
    if not label.strip():
        raise IntakeError("empty label", user_message="Give the intake source a name.")
    if not root_path.strip():
        raise IntakeError("empty root", user_message="Choose the folder the source watches.")
    moment = clock()
    with database.session() as session:
        row = IntakeSource(
            source_id=_new_id(),
            project_id=_project_id(session),
            label=label.strip(),
            kind=SourceKind.WATCHED.value,
            root_path=root_path,
            recursive=recursive,
            exclusions_json=(exclusions or Exclusions()).to_json(),
            policy_json=policy.to_json() if policy is not None else "",
            ingest_mode=ingest_mode.value,
            enabled=enabled,
            is_builtin=False,
            reachability=(Reachability.UNKNOWN if enabled else Reachability.DISABLED).value,
            reachability_changed_at=moment,
            created_at=moment,
            created_by=created_by.strip(),
            updated_at=moment,
        )
        session.add(row)
        session.flush()
        _audit(
            session,
            action=SourceAction.CREATED,
            source_id=row.source_id,
            moment=moment,
            actor=created_by,
            new_value=row.label,
            detail=(
                f"root={root_path} recursive={recursive} ingest={ingest_mode.value} "
                f"enabled={enabled}"
            ),
        )
        return _source_info(session, row)


def update_source(
    database: ProjectDatabase,
    source_id: str,
    *,
    label: str | None = None,
    recursive: bool | None = None,
    exclusions: Exclusions | None = None,
    policy: StabilityPolicy | None = None,
    updated_by: str = "",
    clock: Clock = utc_now,
) -> SourceInfo:
    """Change a source's configuration. Audited.

    The root folder cannot be changed: every ledger row's relative path is
    relative to it, so moving it would silently rewrite provenance. A different
    folder is a new source.
    """
    _require_writable(database)
    moment = clock()
    with database.session() as session:
        row = _require_source(session, source_id)
        if row.is_builtin:
            raise IntakeError(
                "manual source is fixed",
                user_message="The built-in manual source has no settings to change.",
            )
        before = (
            f"label={row.label} recursive={row.recursive} exclusions={row.exclusions_json} "
            f"policy={row.policy_json or 'default'}"
        )
        if label is not None and label.strip():
            row.label = label.strip()
        if recursive is not None:
            row.recursive = recursive
        if exclusions is not None:
            row.exclusions_json = exclusions.to_json()
        if policy is not None:
            row.policy_json = policy.to_json()
        row.updated_at = moment
        after = (
            f"label={row.label} recursive={row.recursive} exclusions={row.exclusions_json} "
            f"policy={row.policy_json or 'default'}"
        )
        if before != after:
            _audit(
                session,
                action=SourceAction.UPDATED,
                source_id=source_id,
                moment=moment,
                actor=updated_by,
                previous_value=before,
                new_value=after,
            )
        return _source_info(session, row)


def set_source_enabled(
    database: ProjectDatabase,
    source_id: str,
    enabled: bool,
    *,
    actor: str = "",
    clock: Clock = utc_now,
) -> SourceInfo:
    """Enable or disable a watched source. Audited; idempotent.

    Disabling changes no file row: the source's files keep their states and
    their history, and its reachability reads ``disabled``.
    """
    _require_writable(database)
    moment = clock()
    with database.session() as session:
        row = _require_source(session, source_id)
        if row.enabled != enabled:
            row.enabled = enabled
            row.reachability = (Reachability.UNKNOWN if enabled else Reachability.DISABLED).value
            row.reachability_detail = ""
            row.reachability_changed_at = moment
            row.updated_at = moment
            _audit(
                session,
                action=SourceAction.ENABLED if enabled else SourceAction.DISABLED,
                source_id=source_id,
                moment=moment,
                actor=actor,
            )
        return _source_info(session, row)


def get_source(database: ProjectDatabase, source_id: str) -> SourceInfo | None:
    """One source, or ``None``."""
    if not has_intake_schema(database):
        return None
    with database.session() as session:
        row = session.get(IntakeSource, source_id)
        return _source_info(session, row) if row is not None else None


def list_sources(database: ProjectDatabase) -> tuple[SourceInfo, ...]:
    """Every source, oldest first."""
    if not has_intake_schema(database):
        return ()
    with database.session() as session:
        rows = session.scalars(select(IntakeSource).order_by(IntakeSource.created_at)).all()
        return tuple(_source_info(session, row) for row in rows)


def _manual_source_row(session: Session, moment: datetime) -> IntakeSource:
    row = session.scalars(select(IntakeSource).where(IntakeSource.is_builtin.is_(True))).first()
    if row is not None:
        return row
    row = IntakeSource(
        source_id=_new_id(),
        project_id=_project_id(session),
        label=MANUAL_SOURCE_LABEL,
        kind=SourceKind.MANUAL.value,
        root_path="",
        recursive=False,
        exclusions_json=Exclusions().to_json(),
        policy_json="",
        ingest_mode=IngestMode.REFERENCE.value,
        enabled=True,
        is_builtin=True,
        reachability=Reachability.ONLINE.value,
        reachability_changed_at=moment,
        created_at=moment,
        created_by="",
        updated_at=moment,
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        action=SourceAction.CREATED,
        source_id=row.source_id,
        moment=moment,
        new_value=row.label,
        detail="built-in manual source, created on first use",
    )
    return row


def manual_source(database: ProjectDatabase, *, clock: Clock = utc_now) -> SourceInfo:
    """The built-in manual source, created (and audited) on first use."""
    _require_writable(database)
    with database.session() as session:
        return _source_info(session, _manual_source_row(session, clock()))


def attach_source(
    database: ProjectDatabase,
    source_id: str,
    scan_session_id: str,
    *,
    actor: str = "",
    clock: Clock = utc_now,
) -> SourceInfo:
    """Make ``scan_session_id`` the session a source's new files are intended for.

    * Refused for a closed session (it receives no intake) and for the manual
      source (its files belong to the batch the operator processes).
    * A previous live attachment is ended first; both changes are audited.
    * Ledger rows already intended for a session keep it - nothing is moved
      implicitly. Rows observed while the source served no session and not yet
      registered (discovered, stabilising, ready) become intended for this one.
    """
    _require_writable(database)
    moment = clock()
    with database.session() as session:
        row = _require_source(session, source_id)
        if row.is_builtin:
            raise IntakeError(
                "manual source attach",
                user_message="The manual source is not attached to sessions.",
            )
        target = session.get(ScanSession, scan_session_id)
        if target is None:
            raise IntakeError(
                f"No scan session {scan_session_id!r}",
                user_message="That scan session no longer exists in this project.",
            )
        if target.state == ScanSessionState.CLOSED.value:
            raise IntakeError(
                "attach to closed session",
                user_message="That scan session is closed; it receives no intake. Reopen it first.",
            )
        live = _live_attachment(session, source_id)
        if live is not None and live.scan_session_id == scan_session_id:
            return _source_info(session, row)
        if live is not None:
            live.detached_at = moment
            live.detached_by = actor.strip()
            session.flush()
            _audit(
                session,
                action=SourceAction.DETACHED,
                source_id=source_id,
                moment=moment,
                actor=actor,
                previous_value=live.scan_session_id,
            )
        session.add(
            IntakeSourceAttachment(
                source_id=source_id,
                scan_session_id=scan_session_id,
                attached_at=moment,
                attached_by=actor.strip(),
            )
        )
        session.execute(
            update(IntakeFile)
            .where(IntakeFile.source_id == source_id)
            .where(IntakeFile.scan_session_id.is_(None))
            .where(
                IntakeFile.state.in_(
                    [
                        IntakeState.DISCOVERED.value,
                        IntakeState.STABILIZING.value,
                        IntakeState.READY.value,
                    ]
                )
            )
            .values(scan_session_id=scan_session_id)
        )
        _audit(
            session,
            action=SourceAction.ATTACHED,
            source_id=source_id,
            moment=moment,
            actor=actor,
            previous_value=live.scan_session_id if live is not None else "",
            new_value=scan_session_id,
        )
        session.flush()
        return _source_info(session, row)


def detach_source(
    database: ProjectDatabase, source_id: str, *, actor: str = "", clock: Clock = utc_now
) -> SourceInfo:
    """End a source's live attachment. Audited; idempotent.

    Files observed afterwards are intended for no session until it is attached
    again; rows already intended for a session keep it.
    """
    _require_writable(database)
    moment = clock()
    with database.session() as session:
        row = _require_source(session, source_id)
        live = _live_attachment(session, source_id)
        if live is not None:
            live.detached_at = moment
            live.detached_by = actor.strip()
            session.flush()
            _audit(
                session,
                action=SourceAction.DETACHED,
                source_id=source_id,
                moment=moment,
                actor=actor,
                previous_value=live.scan_session_id,
            )
        return _source_info(session, row)


def source_sessions(
    database: ProjectDatabase, source_id: str
) -> tuple[tuple[str, datetime, datetime | None], ...]:
    """Every session a source has served: ``(session, attached, detached)``."""
    with database.session() as session:
        return tuple(
            (str(owner), _aware(start) or start, _aware(end))
            for owner, start, end in session.execute(
                select(
                    IntakeSourceAttachment.scan_session_id,
                    IntakeSourceAttachment.attached_at,
                    IntakeSourceAttachment.detached_at,
                )
                .where(IntakeSourceAttachment.source_id == source_id)
                .order_by(IntakeSourceAttachment.attachment_id)
            ).all()
        )


# ----------------------------------------------------------------------
# Ledger reads
# ----------------------------------------------------------------------
def ledger(
    database: ProjectDatabase,
    *,
    source_id: str | None = None,
    current_only: bool = False,
) -> tuple[LedgerRow, ...]:
    """Ledger rows, oldest first (for tests, Health and phase 7/8 views)."""
    if not has_intake_schema(database):
        return ()
    with database.session() as session:
        query = select(IntakeFile).order_by(IntakeFile.intake_file_id)
        if source_id is not None:
            query = query.where(IntakeFile.source_id == source_id)
        if current_only:
            query = query.where(IntakeFile.is_current == true())
        return tuple(_ledger_row(row) for row in session.scalars(query).all())


def ledger_row(database: ProjectDatabase, intake_file_id: int) -> LedgerRow | None:
    """One ledger row, or ``None``."""
    with database.session() as session:
        row = session.get(IntakeFile, intake_file_id)
        return _ledger_row(row) if row is not None else None


def state_counts(
    database: ProjectDatabase, *, source_id: str | None = None
) -> dict[IntakeState, int]:
    """``state -> rows`` (every row, current or not)."""
    if not has_intake_schema(database):
        return {}
    with database.session() as session:
        query = select(IntakeFile.state, func.count()).group_by(IntakeFile.state)
        if source_id is not None:
            query = query.where(IntakeFile.source_id == source_id)
        return {IntakeState(state): int(count) for state, count in session.execute(query).all()}


def stalled_items(
    database: ProjectDatabase, *, now: datetime | None = None
) -> tuple[LedgerRow, ...]:
    """Unsettled rows first seen longer ago than their source's stall ceiling.

    Reported only - being stalled changes no state (a scanner may be paused
    for a long time between pages of one file).
    """
    moment = now or utc_now()
    found: list[LedgerRow] = []
    for source in list_sources(database):
        ceiling = source.policy.stall_after_seconds
        with database.session() as session:
            rows = session.scalars(
                select(IntakeFile)
                .where(IntakeFile.source_id == source.source_id)
                .where(
                    IntakeFile.state.in_(
                        [IntakeState.DISCOVERED.value, IntakeState.STABILIZING.value]
                    )
                )
                .where(IntakeFile.present.is_(True))
            ).all()
            found.extend(
                _ledger_row(row)
                for row in rows
                if (moment - (_aware(row.first_seen_at) or moment)).total_seconds() >= ceiling
            )
    return tuple(found)


# ----------------------------------------------------------------------
# Verification of one due file (no transaction)
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Due:
    intake_file_id: int
    absolute_path: str
    size: int
    mtime_ns: int
    state: IntakeState
    attempts: int
    content_sha256: str | None


@dataclass(frozen=True, slots=True)
class _Verdict:
    due: _Due
    kind: str
    """``ready``, ``decode_failed``, ``unsupported``, ``not_yet``, ``changed``, ``gone``."""
    reason: IntakeReason = IntakeReason.NONE
    detail: str = ""
    sha256: str = ""
    image_format: str = ""
    width: int = 0
    height: int = 0
    pages: int = 0


def _verify(fs: IntakeFileSystem, due: _Due) -> _Verdict:
    """Read the file **once**, hash those bytes, fully decode **those** bytes, re-stat.

    The hash and the decode always describe the same bytes: both are computed
    from the one buffer this read returned, never from a second open.
    """
    snapshot = fs.read_snapshot(due.absolute_path)
    if snapshot.data is None:
        if snapshot.failure is IntakeReason.DISAPPEARED:
            return _Verdict(due, "gone", IntakeReason.DISAPPEARED, snapshot.detail)
        return _Verdict(due, "not_yet", snapshot.failure, snapshot.detail)
    expected = (due.size, due.mtime_ns)
    observed = [
        (meta.size, meta.mtime_ns)
        for meta in (snapshot.before, snapshot.after, snapshot.path_after)
        if meta is not None
    ]
    if (
        snapshot.path_after is None
        or len(observed) != 3
        or any(item != expected for item in observed)
        or len(snapshot.data) != due.size
    ):
        return _Verdict(
            due,
            "changed",
            IntakeReason.CHANGED_DURING_READ,
            "The file changed while it was being read; it will be observed again.",
        )
    digest = scan_provenance.hash_bytes(snapshot.data)
    check = check_image_bytes(snapshot.data)
    if check.ok:
        return _Verdict(
            due, "ready", sha256=digest, image_format=check.image_format,
            width=check.width, height=check.height, pages=check.page_count,
        )
    if check.reason is IntakeReason.MULTIPAGE_TIFF:
        return _Verdict(
            due, "unsupported", IntakeReason.MULTIPAGE_TIFF, check.detail, sha256=digest,
            image_format=check.image_format, pages=check.page_count,
        )
    return _Verdict(
        due, "decode_failed", IntakeReason.DECODE_FAILED, check.detail, sha256=digest,
        image_format=check.image_format, pages=check.page_count,
    )


# ----------------------------------------------------------------------
# The service
# ----------------------------------------------------------------------
class IntakeService:
    """Reconciliation, recovery and registration for one open project.

    One instance per process, used from one thread: the project's single writer
    (ARCHITECTURE_NOTES §13.1). Creating it runs
    :meth:`recover_after_restart` - stability is never trusted across a
    restart.

    Args:
        database: The open, writable project database.
        project_root: The project folder (for the copy store).
        fs: Filesystem; :class:`~omr_scanner.services.intake_fs.OsFileSystem`
            by default, a fake in tests.
        clock: Time; :func:`utc_now` by default.
        store: Copy store; ``IngestStore(project_root)`` by default.
        recover: Run restart recovery now (default ``True``).
    """

    def __init__(
        self,
        database: ProjectDatabase,
        project_root: Path,
        *,
        fs: IntakeFileSystem | None = None,
        clock: Clock = utc_now,
        store: IngestStore | None = None,
        recover: bool = True,
    ) -> None:
        _require_writable(database)
        self._database = database
        self._root = project_root
        self._fs: IntakeFileSystem = fs or OsFileSystem()
        self._clock = clock
        self._store = store or IngestStore(project_root)
        self.last_recovery = RecoveryReport()
        if recover:
            self.last_recovery = self.recover_after_restart()

    @property
    def database(self) -> ProjectDatabase:
        """The project database."""
        return self._database

    # ------------------------------------------------------------------
    # Restart
    # ------------------------------------------------------------------
    def recover_after_restart(self) -> RecoveryReport:
        """Make the ledger safe to continue from after any interruption.

        No file is read. In one transaction:

        * every unsettled row's stability evidence is discarded (observations
          0, no quiet-period start, no back-off): it must be observed afresh;
        * every ready, unregistered row is flagged for re-verification - read,
          hashed and decoded again before it may be registered - keeping its
          ``ready_at``, so the registration order survives the restart.

        Then registrations whose duplicate link had not committed are completed
        (:func:`link_exact_duplicates` for their batches), and the copy store's
        own interrupted ``.part`` files are removed.
        """
        moment = self._clock()
        with self._database.session() as session:
            unsettled = session.execute(
                update(IntakeFile)
                .where(
                    IntakeFile.state.in_(
                        [IntakeState.DISCOVERED.value, IntakeState.STABILIZING.value]
                    )
                )
                .values(observations=0, stable_since=None, retry_after=None)
            )
            reverify = session.execute(
                update(IntakeFile)
                .where(IntakeFile.state == IntakeState.READY.value)
                .where(IntakeFile.reverify_required.is_(False))
                .values(
                    reverify_required=True,
                    state_reason=IntakeReason.REVERIFY.value,
                    detail="Ready before a restart; re-read before registration.",
                    state_changed_at=moment,
                )
            )
            pending_batches = sorted(
                {
                    str(batch_id)
                    for (batch_id,) in session.execute(
                        select(BatchScan.batch_id)
                        .join(IntakeFile, IntakeFile.batch_scan_id == BatchScan.scan_id)
                        .where(IntakeFile.state == IntakeState.REGISTERED.value)
                        .where(
                            BatchScan.status.in_(
                                [
                                    status.value
                                    for status in ScanJobStatus
                                    if status.is_resumable or status is ScanJobStatus.DUPLICATE
                                ]
                            )
                        )
                    ).all()
                }
            )
        completed = 0
        for batch_id in pending_batches:
            completed += len(link_exact_duplicates(self._database, batch_id, clock=self._clock))
        removed = self._store.clean_temporaries()
        report = RecoveryReport(
            restabilise=int(getattr(unsettled, "rowcount", 0) or 0),
            reverify=int(getattr(reverify, "rowcount", 0) or 0),
            duplicates_completed=completed,
            temporaries_removed=removed,
        )
        _LOGGER.info("Intake recovery: %s", report)
        return report

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def reconcile_all(self) -> tuple[ReconcileReport, ...]:
        """Reconcile every watched source, each independently.

        One source failing - unreachable, denied, or an unexpected error -
        never stops the others.
        """
        reports: list[ReconcileReport] = []
        for source in list_sources(self._database):
            if source.kind is not SourceKind.WATCHED:
                continue
            try:
                reports.append(self.reconcile(source.source_id))
            except Exception:
                _LOGGER.exception("Reconciliation of source %s failed", source.source_id)
                reports.append(
                    ReconcileReport(
                        source_id=source.source_id,
                        reachability=source.reachability,
                        detail="reconciliation failed unexpectedly; see the log",
                    )
                )
        return tuple(reports)

    def reconcile(self, source_id: str) -> ReconcileReport:
        """Reconcile one source against the filesystem. Idempotent.

        1. List the folder (no transaction). A listing failure updates the
           source's reachability **only** - no file row changes.
        2. Apply the observations in one transaction: new rows, changed
           metadata, reappearances, disappearances (only where the listing
           could have seen the file), held diversion for closed sessions.
        3. Read and verify each due file (no transaction).
        4. Apply the verdicts in one transaction, each only if the row is
           still as it was read.
        """
        with self._database.session() as session:
            source = _source_info(session, _require_source(session, source_id))
        if source.kind is not SourceKind.WATCHED:
            raise IntakeError(
                "manual source reconcile",
                user_message="The manual source is recorded when its batch is processed.",
            )
        moment = self._clock()
        if not source.enabled:
            self._set_reachability(source_id, Reachability.DISABLED, "", moment, success=False)
            return ReconcileReport(source_id=source_id, reachability=Reachability.DISABLED)
        try:
            listing = self._fs.list_source(
                source.root_path, recursive=source.recursive, exclusions=source.exclusions
            )
        except SourceListingError as exc:
            self._set_reachability(source_id, exc.reachability, exc.detail, moment, success=False)
            return ReconcileReport(
                source_id=source_id, reachability=exc.reachability, detail=exc.detail
            )

        report = ReconcileReport(
            source_id=source_id,
            reachability=Reachability.ONLINE,
            listed=len(listing.files),
            unlisted_folders=listing.unlisted_folders,
        )
        due = self._apply_listing(source, listing, moment, report)
        verdicts = [_verify(self._fs, item) for item in due]
        self._apply_verdicts(source, verdicts, report)
        detail = (
            f"{len(listing.unlisted_folders)} sub-folder(s) could not be listed"
            if listing.unlisted_folders
            else ""
        )
        report.detail = detail
        self._set_reachability(source_id, Reachability.ONLINE, detail, moment, success=True)
        return report

    def _set_reachability(
        self,
        source_id: str,
        reachability: Reachability,
        detail: str,
        moment: datetime,
        *,
        success: bool,
    ) -> None:
        with self._database.session() as session:
            row = _require_source(session, source_id)
            if row.reachability != reachability.value:
                row.reachability_changed_at = moment
                _LOGGER.info(
                    "Intake source %s: %s -> %s %s",
                    source_id, row.reachability, reachability.value, detail,
                )
            row.reachability = reachability.value
            row.reachability_detail = detail
            row.last_attempt_at = moment
            if success:
                row.last_reconciled_at = moment

    def _apply_listing(
        self,
        source: SourceInfo,
        listing: SourceListing,
        moment: datetime,
        report: ReconcileReport,
    ) -> list[_Due]:
        """Transaction 2 of a pass; returns the rows due for verification."""
        policy = source.policy
        newest: ObservedFile | None = None
        with self._database.session() as session:
            attached = _live_attachment(session, source.source_id)
            intended = attached.scan_session_id if attached is not None else None
            closed = _closed_sessions(session)
            current = {
                row.relative_path: row
                for row in session.scalars(
                    select(IntakeFile)
                    .where(IntakeFile.source_id == source.source_id)
                    .where(IntakeFile.is_current == true())
                ).all()
            }
            listed: set[str] = set()
            for observed in listing.files:
                listed.add(observed.relative_path)
                reason = classify_name(
                    observed.relative_path,
                    exclusions=source.exclusions,
                    supported_suffixes=SUPPORTED_SCAN_SUFFIXES,
                )
                row = current.get(observed.relative_path)
                if row is None:
                    _new_row(
                        session,
                        source_id=source.source_id,
                        observed=observed,
                        state=IntakeState.IGNORED if reason else IntakeState.DISCOVERED,
                        reason=reason,
                        moment=moment,
                        scan_session_id=intended,
                    )
                    report.new_rows += 1
                    report.ignored += 1 if reason else 0
                    if not reason and (newest is None or observed.mtime_ns > newest.mtime_ns):
                        newest = observed
                    continue
                state = IntakeState(row.state)
                changed = (row.file_size, row.mtime_ns) != observed.signature
                name_ignored = (
                    state is IntakeState.IGNORED
                    and IntakeReason(row.state_reason) in IGNORE_REASONS
                )
                if name_ignored and reason:
                    # Still ignored by name: refresh the observation only.
                    if changed:
                        row.file_size, row.mtime_ns = observed.signature
                    _seen_settled(row, moment)
                    continue
                if state.is_unsettled:
                    if reason:
                        _move(row, IntakeState.IGNORED, moment, reason)
                        report.ignored += 1
                    elif changed or row.stable_since is None:
                        _move(
                            row, IntakeState.STABILIZING, moment,
                            IntakeReason.CHANGED if changed else IntakeReason.QUIET_PERIOD,
                        )
                        row.file_size, row.mtime_ns = observed.signature
                        row.absolute_path = observed.absolute_path
                        row.observations = 1
                        row.stable_since = moment
                        row.attempts = 0 if changed else row.attempts
                        row.retry_after = None if changed else row.retry_after
                        report.changed += 1 if changed else 0
                    else:
                        row.observations += 1
                        if row.state == IntakeState.DISCOVERED.value:
                            _move(row, IntakeState.STABILIZING, moment, IntakeReason.QUIET_PERIOD)
                        if observed.size == 0:
                            row.state_reason = IntakeReason.EMPTY_FILE.value
                            row.detail = "The file is empty (a placeholder, or not written yet)."
                    row.present = True
                    row.last_seen_at = moment
                    continue
                if state is IntakeState.READY:
                    if changed:
                        _move(row, IntakeState.STABILIZING, moment, IntakeReason.CHANGED)
                        row.file_size, row.mtime_ns = observed.signature
                        row.content_sha256 = None
                        row.ready_at = None
                        row.verified_at = None
                        row.reverify_required = False
                        row.observations = 1
                        row.stable_since = moment
                        row.attempts = 0
                        report.changed += 1
                    elif row.scan_session_id in closed:
                        _move(
                            row, IntakeState.HELD, moment, IntakeReason.SESSION_CLOSED,
                            "Ready for a scan session that is closed; not registered.",
                        )
                        report.held += 1
                    row.present = True
                    row.last_seen_at = moment
                    continue
                if state is IntakeState.VANISHED:
                    _move(row, IntakeState.DISCOVERED, moment, IntakeReason.NONE, "Reappeared.")
                    row.file_size, row.mtime_ns = observed.signature
                    row.absolute_path = observed.absolute_path
                    row.content_sha256 = None
                    row.observations = 1
                    row.stable_since = moment
                    row.attempts = 0
                    row.retry_after = None
                    row.present = True
                    row.last_seen_at = moment
                    if row.scan_session_id is None:
                        row.scan_session_id = intended
                    report.reappeared += 1
                    continue
                # Terminal: registered, duplicate, unreadable, unsupported or
                # held. Unchanged: nothing to do. (A name-ignored row reaching
                # here has become admissible - its exclusion was removed.)
                if not changed and not name_ignored:
                    _seen_settled(row, moment)
                    continue
                # Different metadata at a settled path (or a name newly
                # admissible): a new observation, a new row. The old row is
                # never changed into the new file.
                row.is_current = False
                session.flush()
                _new_row(
                    session,
                    source_id=source.source_id,
                    observed=observed,
                    state=IntakeState.IGNORED if reason else IntakeState.DISCOVERED,
                    reason=reason,
                    moment=moment,
                    scan_session_id=intended,
                    previous=row,
                )
                report.new_rows += 1
                report.ignored += 1 if reason else 0
                if not reason and (newest is None or observed.mtime_ns > newest.mtime_ns):
                    newest = observed

            for relative, row in current.items():
                if relative in listed or not row.present or not listing.covers(relative):
                    continue
                row.present = False
                state = IntakeState(row.state)
                if state.is_unsettled or state is IntakeState.READY:
                    _move(
                        row, IntakeState.VANISHED, moment, IntakeReason.DISAPPEARED,
                        "Disappeared before it was registered.",
                    )
                    row.observations = 0
                    row.stable_since = None
                    row.reverify_required = False
                    report.vanished += 1
            session.flush()
            due_rows = session.scalars(
                select(IntakeFile)
                .where(IntakeFile.source_id == source.source_id)
                .where(IntakeFile.is_current == true())
                .where(IntakeFile.present.is_(True))
                .where(
                    or_(
                        IntakeFile.state.in_(
                            [IntakeState.DISCOVERED.value, IntakeState.STABILIZING.value]
                        ),
                        (IntakeFile.state == IntakeState.READY.value)
                        & IntakeFile.reverify_required.is_(True),
                    )
                )
                .order_by(IntakeFile.intake_file_id)
            ).all()
            due = [
                _Due(
                    intake_file_id=row.intake_file_id,
                    absolute_path=row.absolute_path,
                    size=row.file_size,
                    mtime_ns=row.mtime_ns,
                    state=IntakeState(row.state),
                    attempts=row.attempts,
                    content_sha256=row.content_sha256,
                )
                for row in due_rows
                if row.file_size > 0
                and (
                    row.state == IntakeState.READY.value
                    or due_for_verification(
                        observations=row.observations,
                        stable_since=_aware(row.stable_since),
                        retry_after=_aware(row.retry_after),
                        now=moment,
                        policy=policy,
                    )
                )
            ]
            if newest is not None:
                source_row = _require_source(session, source.source_id)
                source_row.last_file_seen_at = moment
                source_row.last_file_seen_path = newest.relative_path
        return due

    def _apply_verdicts(
        self, source: SourceInfo, verdicts: Sequence[_Verdict], report: ReconcileReport
    ) -> None:
        """Transaction 4 of a pass: apply each verdict if the row is unchanged."""
        if not verdicts:
            return
        moment = self._clock()
        policy = source.policy
        with self._database.session() as session:
            closed = _closed_sessions(session)
            for verdict in verdicts:
                due = verdict.due
                row = session.get(IntakeFile, due.intake_file_id)
                if (
                    row is None
                    or not row.is_current
                    or IntakeState(row.state) is not due.state
                    or (row.file_size, row.mtime_ns) != (due.size, due.mtime_ns)
                ):
                    continue  # moved on meanwhile; the next pass decides
                report.verified += 1
                reverifying = due.state is IntakeState.READY
                if verdict.kind in ("not_yet", "changed", "gone"):
                    report.not_yet += 1
                    if verdict.kind == "gone":
                        row.present = False
                        _move(
                            row, IntakeState.VANISHED, moment, IntakeReason.DISAPPEARED,
                            "Disappeared before it was registered.",
                        )
                        report.vanished += 1
                        continue
                    target = IntakeState.STABILIZING
                    _move(row, target, moment, verdict.reason, verdict.detail)
                    if reverifying:
                        row.content_sha256 = None
                        row.ready_at = None
                        row.reverify_required = False
                    if verdict.kind == "changed":
                        row.observations = 0
                        row.stable_since = None
                    continue
                if verdict.kind == "decode_failed":
                    attempts = (0 if reverifying else row.attempts) + 1
                    if reverifying:
                        row.content_sha256 = None
                        row.ready_at = None
                        row.reverify_required = False
                        _move(row, IntakeState.STABILIZING, moment, IntakeReason.DECODE_RETRY,
                              verdict.detail)
                    row.attempts = attempts
                    if attempts >= policy.max_decode_attempts and not reverifying:
                        if self._fold_if_known(session, row, verdict.sha256, moment, report):
                            continue
                        row.content_sha256 = verdict.sha256
                        _move(row, IntakeState.UNREADABLE, moment, IntakeReason.DECODE_FAILED,
                              f"Never decoded after {attempts} attempt(s): {verdict.detail}")
                        row.image_format = verdict.image_format
                        report.unreadable += 1
                    else:
                        _move(row, IntakeState.STABILIZING, moment, IntakeReason.DECODE_RETRY,
                              verdict.detail)
                        row.retry_after = moment + policy.backoff(attempts)
                        report.not_yet += 1
                    continue
                if verdict.kind == "unsupported":
                    if reverifying:
                        _move(row, IntakeState.STABILIZING, moment, IntakeReason.REVERIFY)
                        row.content_sha256 = None
                        row.ready_at = None
                        row.reverify_required = False
                    if self._fold_if_known(session, row, verdict.sha256, moment, report):
                        continue
                    row.content_sha256 = verdict.sha256
                    row.image_format = verdict.image_format
                    row.page_count = verdict.pages
                    _move(row, IntakeState.UNSUPPORTED, moment, IntakeReason.MULTIPAGE_TIFF,
                          verdict.detail)
                    report.unsupported += 1
                    continue
                # ready
                if reverifying:
                    if verdict.sha256 == row.content_sha256:
                        row.reverify_required = False
                        row.state_reason = IntakeReason.NONE.value
                        row.detail = ""
                        row.verified_at = moment
                        report.ready += 1
                        continue
                    # Different bytes than before the restart, never registered:
                    # this content became ready now.
                    row.reverify_required = False
                    row.content_sha256 = None
                    _move(row, IntakeState.STABILIZING, moment, IntakeReason.REVERIFY)
                if self._fold_if_known(session, row, verdict.sha256, moment, report):
                    continue
                row.content_sha256 = verdict.sha256
                row.image_format = verdict.image_format
                row.image_width = verdict.width
                row.image_height = verdict.height
                row.page_count = verdict.pages
                row.verified_at = moment
                row.ready_at = moment
                row.attempts = 0
                row.retry_after = None
                _move(row, IntakeState.READY, moment)
                report.ready += 1
                if row.scan_session_id in closed:
                    _move(
                        row, IntakeState.HELD, moment, IntakeReason.SESSION_CLOSED,
                        "Ready for a scan session that is closed; not registered.",
                    )
                    report.held += 1
                session.flush()

    @staticmethod
    def _fold_if_known(
        session: Session,
        row: IntakeFile,
        sha256: str,
        moment: datetime,
        report: ReconcileReport,
    ) -> bool:
        """If the ledger already holds these bytes at this path, say so instead.

        Same ``(source, relative path, content)`` is one logical item: the new
        observation becomes ``ignored`` (``unchanged_content``), pointing at the
        row that holds the content, which becomes current again with the new
        observation's metadata. Nothing is registered twice.
        """
        known = session.scalars(
            select(IntakeFile)
            .where(IntakeFile.source_id == row.source_id)
            .where(IntakeFile.relative_path == row.relative_path)
            .where(IntakeFile.content_sha256 == sha256)
            .where(IntakeFile.intake_file_id != row.intake_file_id)
        ).first()
        if known is None:
            return False
        _move(
            row, IntakeState.IGNORED, moment, IntakeReason.UNCHANGED_CONTENT,
            f"Same bytes as ledger row {known.intake_file_id} at this path; nothing new.",
        )
        row.previous_intake_file_id = known.intake_file_id
        row.path_reused = False
        row.is_current = False
        row.present = False
        session.flush()
        others = session.scalars(
            select(IntakeFile)
            .where(IntakeFile.source_id == row.source_id)
            .where(IntakeFile.relative_path == row.relative_path)
            .where(IntakeFile.is_current == true())
        ).all()
        for other in others:
            other.is_current = False
        session.flush()
        known.is_current = True
        known.present = True
        known.file_size = row.file_size
        known.mtime_ns = row.mtime_ns
        known.last_seen_at = moment
        report.unchanged_content += 1
        return True

    # ------------------------------------------------------------------
    # Registration API
    # ------------------------------------------------------------------
    def ready_items(
        self,
        *,
        scan_session_id: str,
        source_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[ReadyItem, ...]:
        """Files that may be registered into ``scan_session_id``, in stable order.

        Ready, verified (not awaiting re-verification after a restart), present,
        current, intended for that session. Ordered by ``ready_at`` then ledger
        id - the order phase 6 forms units in, and the order that survives a
        restart.
        """
        with self._database.session() as session:
            query = (
                select(IntakeFile)
                .where(IntakeFile.state == IntakeState.READY.value)
                .where(IntakeFile.reverify_required.is_(False))
                .where(IntakeFile.present.is_(True))
                .where(IntakeFile.is_current == true())
                .where(IntakeFile.scan_session_id == scan_session_id)
                .order_by(IntakeFile.ready_at, IntakeFile.intake_file_id)
            )
            if source_id is not None:
                query = query.where(IntakeFile.source_id == source_id)
            if limit is not None:
                query = query.limit(limit)
            return tuple(
                ReadyItem(
                    intake_file_id=row.intake_file_id,
                    source_id=row.source_id,
                    scan_session_id=str(row.scan_session_id),
                    relative_path=row.relative_path,
                    absolute_path=row.absolute_path,
                    content_sha256=str(row.content_sha256),
                    file_size=row.file_size,
                    ready_at=_aware(row.ready_at) or moment_floor(),
                )
                for row in session.scalars(query).all()
            )

    def register(
        self,
        *,
        scan_session_id: str,
        source_id: str,
        intake_file_ids: Sequence[int],
        identity: BatchIdentity,
        settings: dict[str, Any] | None = None,
        started_by: str = "",
        seal: bool = True,
        acknowledge_template_change: bool = False,
    ) -> Registration:
        """Register these ready files of one source as **one finite batch** of the session.

        The boundary phase 6's scheduler calls; it decides *which* files and
        *when*. Nothing is recognised here.

        1. **Validate** (one read transaction): every id names a row of
           ``source_id``. Rows already registered or linked are the idempotent
           repeat (``already_registered``). Any other row must be ready,
           verified, current, present and intended for ``scan_session_id`` -
           otherwise :class:`IntakeError`, nothing changed. A **closed**
           session refuses: the rows are diverted to ``held`` and no batch is
           created (never reopened, never another session).
        2. **Ingest** (no transaction): per ADR-0008, a ``copy`` source's file
           is read once more, must still hash to its verified value, and is
           stored as a verified project copy; a ``reference`` source's file
           must still have its verified size and time. A file that changed goes
           back to stabilising; one that vanished, to vanished; a copy that
           cannot be written (disk full) leaves the row ready (``failed``).
        3. **Commit** (one transaction): the batch (role ``scan``, its source
           recorded), one ``batch_scan`` per file in ``(ready_at, id)`` order
           carrying the verified hash, the ledger rows ``registered`` and
           linked, the batch sealed (by default: a unit's membership is final).
           Files repeating an earlier file of the same call are linked to it as
           duplicate content rather than given a second scan.
        4. **Duplicates**: phase 4's
           :func:`~omr_scanner.services.scan_lifecycle.link_exact_duplicates`
           over the new batch - the one session-wide rule manual imports use -
           and the ledger mirrors what it linked.

        Raises:
            IntakeError: An id is unknown, of another source, or not ready.
            omr_scanner.services.scan_sessions.TemplatePinError: The template
                differs from the session's pin and was not acknowledged.
        """
        from omr_scanner.services import scan_sessions

        wanted = list(dict.fromkeys(int(item) for item in intake_file_ids))
        moment = self._clock()
        with self._database.session() as session:
            source = _source_info(session, _require_source(session, source_id))
            target = session.get(ScanSession, scan_session_id)
            if target is None:
                raise IntakeError(
                    f"No scan session {scan_session_id!r}",
                    user_message="That scan session no longer exists in this project.",
                )
            rows = {
                row.intake_file_id: row
                for row in session.scalars(
                    select(IntakeFile).where(IntakeFile.intake_file_id.in_(wanted))
                ).all()
            }
            already: list[int] = []
            candidates: list[IntakeFile] = []
            for item in wanted:
                row = rows.get(item)
                if row is None or row.source_id != source_id:
                    raise IntakeError(
                        f"intake row {item} is not of source {source_id}",
                        user_message="A file to register is not part of that intake source.",
                    )
                state = IntakeState(row.state)
                if state.consumed_content:
                    if row.scan_session_id != scan_session_id:
                        raise IntakeError(
                            f"intake row {item} registered in another session",
                            user_message="That file was registered into another scan session.",
                        )
                    already.append(item)
                    continue
                if (
                    state is not IntakeState.READY
                    or row.reverify_required
                    or not row.present
                    or not row.is_current
                    or row.scan_session_id != scan_session_id
                ):
                    raise IntakeError(
                        f"intake row {item} is {state.value}, not ready for {scan_session_id}",
                        user_message=(
                            "Only verified, ready files intended for this scan session can "
                            "be registered."
                        ),
                    )
                candidates.append(row)
            candidates.sort(key=lambda row: (_aware(row.ready_at) or moment, row.intake_file_id))
            plan = [
                (row.intake_file_id, row.absolute_path, row.file_size, row.mtime_ns,
                 str(row.content_sha256), row.relative_path)
                for row in candidates
            ]
            closed = target.state == ScanSessionState.CLOSED.value
            if closed and candidates:
                for row in candidates:
                    _move(
                        row, IntakeState.HELD, moment, IntakeReason.SESSION_CLOSED,
                        "Ready for a scan session that is closed; not registered.",
                    )
                return Registration(
                    batch_id=None,
                    already_registered=tuple(already),
                    held=tuple(row.intake_file_id for row in candidates),
                )
        if not plan:
            return Registration(batch_id=None, already_registered=tuple(already))

        # --- 2. ingest, outside any transaction -------------------------
        ingested: dict[int, tuple[str, str]] = {}
        returned: list[tuple[int, str, IntakeReason, str]] = []
        failed: list[tuple[int, str]] = []
        for intake_id, absolute, size, mtime_ns, sha256, relative in plan:
            if source.ingest_mode is IngestMode.REFERENCE:
                snapshot_meta = self._fs.read_snapshot(absolute)
                if snapshot_meta.data is None:
                    kind = "gone" if snapshot_meta.failure is IntakeReason.DISAPPEARED else "not"
                    if kind == "gone":
                        returned.append((intake_id, "gone", IntakeReason.DISAPPEARED, ""))
                    else:
                        failed.append((intake_id, snapshot_meta.detail or "could not be read"))
                    continue
                if scan_provenance.hash_bytes(snapshot_meta.data) != sha256 or (
                    snapshot_meta.path_after is None
                    or (snapshot_meta.path_after.size, snapshot_meta.path_after.mtime_ns)
                    != (size, mtime_ns)
                ):
                    returned.append((intake_id, "changed", IntakeReason.SOURCE_CHANGED, ""))
                    continue
                ingested[intake_id] = (absolute, "")
                continue
            snapshot = self._fs.read_snapshot(absolute)
            if snapshot.data is None:
                if snapshot.failure is IntakeReason.DISAPPEARED:
                    returned.append((intake_id, "gone", IntakeReason.DISAPPEARED, ""))
                else:
                    failed.append((intake_id, snapshot.detail or "could not be read"))
                continue
            suffix = "." + relative.rpartition(".")[2] if "." in relative else ""
            try:
                stored = self._store.put(snapshot.data, sha256, suffix)
            except IngestError as exc:
                if exc.reason is IntakeReason.SOURCE_CHANGED:
                    returned.append((intake_id, "changed", IntakeReason.SOURCE_CHANGED, exc.detail))
                else:
                    failed.append((intake_id, exc.detail))
                continue
            ingested[intake_id] = (str(stored.path), stored.relative_path)

        # --- 3. one transaction: batch, scans, ledger, seal ----------------
        batch_id: str | None = None
        registered: list[tuple[int, int]] = []
        in_call_duplicates: list[tuple[int, int]] = []
        with self._database.session() as session:
            commit_moment = self._clock()
            for intake_id, kind, reason, detail in returned:
                row = session.get(IntakeFile, intake_id)
                if row is None or row.state != IntakeState.READY.value:
                    continue
                row.content_sha256 = None
                row.ready_at = None
                row.verified_at = None
                if kind == "gone":
                    row.present = False
                    _move(row, IntakeState.VANISHED, commit_moment, IntakeReason.DISAPPEARED,
                          "Disappeared before it was registered.")
                else:
                    _move(row, IntakeState.STABILIZING, commit_moment, reason,
                          detail or "Changed after it was verified; it will be verified again.")
                    row.observations = 0
                    row.stable_since = None
            order = [item for item in plan if item[0] in ingested]
            first_by_hash: dict[str, int] = {}
            members: list[tuple[int, str, str, str]] = []
            repeats: list[tuple[int, int]] = []
            for intake_id, _absolute, _size, _mtime, sha256, _relative in order:
                if sha256 in first_by_hash:
                    repeats.append((intake_id, first_by_hash[sha256]))
                    continue
                first_by_hash[sha256] = intake_id
                path, relative_copy = ingested[intake_id]
                members.append((intake_id, path, relative_copy, sha256))
            if members:
                batch_id = scan_sessions.start_batch_in(
                    session,
                    [Path(path) for _id, path, _rel, _sha in members],
                    identity=identity,
                    source_folder=Path(source.root_path) if source.root_path else None,
                    settings=settings,
                    role=BatchRole.SCAN,
                    scan_session_id=scan_session_id,
                    started_by=started_by,
                    seal_previous=False,
                    acknowledge_template_change=acknowledge_template_change,
                )
                session.flush()
                session.execute(
                    update(ScanBatch)
                    .where(ScanBatch.batch_id == batch_id)
                    .values(source_id=source_id)
                )
                scans = {
                    str(path): int(scan_id)
                    for scan_id, path in session.execute(
                        select(BatchScan.scan_id, BatchScan.source_path).where(
                            BatchScan.batch_id == batch_id
                        )
                    ).all()
                }
                scan_of: dict[int, int] = {}
                for intake_id, path, relative_copy, sha256 in members:
                    scan_id = scans[str(Path(path))]
                    scan_of[intake_id] = scan_id
                    session.execute(
                        update(BatchScan)
                        .where(BatchScan.scan_id == scan_id)
                        .values(
                            content_sha256=sha256,
                            content_hash_algorithm=scan_provenance.HASH_ALGORITHM,
                            intake_file_id=intake_id,
                            registered_at=commit_moment,
                        )
                    )
                    row = session.get(IntakeFile, intake_id)
                    assert row is not None
                    _move(row, IntakeState.REGISTERED, commit_moment)
                    row.batch_scan_id = scan_id
                    row.registered_at = commit_moment
                    row.ingest_path = relative_copy
                    registered.append((intake_id, scan_id))
                for intake_id, first in repeats:
                    row = session.get(IntakeFile, intake_id)
                    assert row is not None
                    _move(
                        row, IntakeState.DUPLICATE_CONTENT, commit_moment,
                        IntakeReason.NONE,
                        "Same bytes as another file registered in the same unit; not read.",
                    )
                    row.duplicate_of_scan_id = scan_of[first]
                    row.registered_at = commit_moment
                    row.ingest_path = ingested[intake_id][1]
                    in_call_duplicates.append((intake_id, scan_of[first]))
                if seal:
                    scan_sessions.seal_batch_in(
                        session, batch_id, sealed_by=started_by, reason="intake unit registered"
                    )

        # --- 4. phase 4's duplicate rule, then mirror it ------------------
        duplicates: list[tuple[int, int]] = []
        if batch_id is not None:
            duplicates = link_exact_duplicates(self._database, batch_id, clock=self._clock)
            duplicates += self._retarget_in_call(in_call_duplicates, duplicates, registered)
        _LOGGER.info(
            "Intake registration: batch %s, %d registered, %d duplicate(s), %d returned, "
            "%d failed", batch_id, len(registered), len(duplicates), len(returned), len(failed),
        )
        return Registration(
            batch_id=batch_id,
            registered=tuple(registered),
            duplicates=tuple(duplicates),
            already_registered=tuple(already),
            returned=tuple(item[0] for item in returned),
            failed=tuple(failed),
        )

    def _retarget_in_call(
        self,
        repeats: list[tuple[int, int]],
        linked: list[tuple[int, int]],
        registered: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """Point an in-call repeat at the effective sheet, never at a linked copy.

        A repeat names the scan of the first file with its bytes in the same
        call. When phase 4's rule then linked that first file itself (the
        session already held the bytes), the repeat names that original too.
        """
        scan_of = dict(registered)
        original_of_scan = {
            scan_of[intake]: original for intake, original in linked if intake in scan_of
        }
        result: list[tuple[int, int]] = []
        with self._database.session() as session:
            for intake_id, first_scan in repeats:
                target = original_of_scan.get(first_scan, first_scan)
                if target != first_scan:
                    row = session.get(IntakeFile, intake_id)
                    if row is not None:
                        row.duplicate_of_scan_id = target
                result.append((intake_id, target))
        return result


def moment_floor() -> datetime:
    """A sentinel ``ready_at`` for a row that somehow has none (never in practice)."""
    return datetime.min.replace(tzinfo=UTC)


# ----------------------------------------------------------------------
# Manual intake: Add Folder -> Process All
# ----------------------------------------------------------------------
def record_manual_batch(
    database: ProjectDatabase, batch_id: str, *, clock: Clock = utc_now
) -> int:
    """Record a manually added batch's files in the built-in manual source.

    Run by the Scan stage's worker as the registration step of *Process All*,
    after the batch's rows exist and **before any sheet is read** - where
    phase 4 already hashed them. It:

    1. hashes every unhashed real file with
       :func:`~omr_scanner.services.scan_provenance.compute_hashes_for_batch`
       (the one hashing routine, unchanged);
    2. records each hashed scan in the manual source's ledger, keyed by its
       path as chosen and its content hash: a known ``(path, content)`` links
       to its existing row (a *Reprocess All* re-reads the same file); new
       content at a known path is a new row flagged ``path_reused``; and
    3. links the batch to the manual source.

    The exact-duplicate decision is :func:`link_exact_duplicates` - phase 4's
    rule - called next by the worker. A file that could not be hashed (missing,
    unreadable) is not recorded: it has no content identity, and recognition
    reports it as before. Returns how many scans were newly linked to a row.
    """
    if database.read_only or not has_intake_schema(database):
        return 0
    scan_provenance.compute_hashes_for_batch(database, batch_id)
    with database.session() as session:
        rows = session.execute(
            select(
                BatchScan.scan_id, BatchScan.source_path, BatchScan.content_sha256,
                BatchScan.file_size,
            )
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.intake_file_id.is_(None))
            .where(BatchScan.content_sha256 != "")
            .order_by(BatchScan.batch_index)
        ).all()
    targets = [
        (int(scan_id), str(path), str(digest), int(size))
        for scan_id, path, digest, size in rows
        if not scan_provenance.is_virtual_source(str(path))
    ]
    if not targets:
        return 0
    stats: dict[str, tuple[int, int]] = {}
    for _scan_id, path, _digest, size in targets:
        try:
            info = Path(path).stat()
            stats[path] = (int(info.st_size), int(info.st_mtime_ns))
        except OSError:
            stats[path] = (size, 0)
    moment = clock()
    linked = 0
    with database.session() as session:
        source = _manual_source_row(session, moment)
        owner = session.scalar(
            select(ScanBatch.scan_session_id).where(ScanBatch.batch_id == batch_id)
        )
        session.execute(
            update(ScanBatch)
            .where(ScanBatch.batch_id == batch_id)
            .where(ScanBatch.source_id.is_(None))
            .values(source_id=source.source_id)
        )
        for scan_id, path, digest, _size in targets:
            size, mtime_ns = stats[path]
            row = session.scalars(
                select(IntakeFile)
                .where(IntakeFile.source_id == source.source_id)
                .where(IntakeFile.relative_path == path)
                .where(IntakeFile.content_sha256 == digest)
            ).first()
            if row is None:
                previous = session.scalars(
                    select(IntakeFile)
                    .where(IntakeFile.source_id == source.source_id)
                    .where(IntakeFile.relative_path == path)
                    .where(IntakeFile.is_current == true())
                ).first()
                if previous is not None:
                    previous.is_current = False
                    session.flush()
                row = _new_row(
                    session,
                    source_id=source.source_id,
                    observed=ObservedFile(
                        relative_path=path, absolute_path=path, size=size, mtime_ns=mtime_ns
                    ),
                    state=IntakeState.READY,
                    reason=IntakeReason.NONE,
                    moment=moment,
                    scan_session_id=owner,
                    previous=previous,
                    detail="Chosen by the operator (Add Files / Add Folder).",
                )
                row.file_name = Path(path).name
                row.content_sha256 = digest
                row.ready_at = moment
                _move(row, IntakeState.REGISTERED, moment)
                row.batch_scan_id = scan_id
                row.registered_at = moment
                session.flush()
            else:
                if row.batch_scan_id is None and IntakeState(row.state) is IntakeState.READY:
                    _move(row, IntakeState.REGISTERED, moment)
                    row.batch_scan_id = scan_id
                    row.registered_at = moment
                row.last_seen_at = moment
                row.present = True
            session.execute(
                update(BatchScan)
                .where(BatchScan.scan_id == scan_id)
                .values(intake_file_id=row.intake_file_id, registered_at=moment)
            )
            linked += 1
    return linked


def link_exact_duplicates(
    database: ProjectDatabase,
    batch_id: str,
    paths: Sequence[Path] | None = None,
    *,
    clock: Clock = utc_now,
) -> list[tuple[int, int]]:
    """Phase 4's exact-duplicate rule, then the ledger mirrors it.

    Calls :func:`omr_scanner.services.scan_lifecycle.link_exact_duplicates`
    unchanged - the **one** session-wide definition, for manual and watched
    intake alike - then marks every ledger row whose registered scan it linked
    (``batch_scan.status = duplicate``) as ``duplicate_content``, naming the
    sheet it repeats. Idempotent.

    Returns:
        ``(intake_file_id, original_scan_id)`` for every ledger row mirrored by
        this call.
    """
    from omr_scanner.services import scan_lifecycle

    scan_lifecycle.link_exact_duplicates(database, batch_id, paths)
    return mirror_duplicates(database, batch_id, clock=clock)


def mirror_duplicates(
    database: ProjectDatabase, batch_id: str, *, clock: Clock = utc_now
) -> list[tuple[int, int]]:
    """Mark ledger rows ``duplicate_content`` where phase 4's rule linked their scan.

    The second half of :func:`link_exact_duplicates`, for a caller (the Scan
    stage's worker) that calls the phase 4 function itself to keep its result.
    """
    if database.read_only or not has_intake_schema(database):
        return []
    mirrored: list[tuple[int, int]] = []
    moment = clock()
    with database.session() as session:
        found = session.execute(
            select(IntakeFile, ScanRejection.reimport_of_scan_id, ScanRejection.state)
            .join(BatchScan, BatchScan.scan_id == IntakeFile.batch_scan_id)
            .join(ScanRejection, ScanRejection.scan_id == BatchScan.scan_id)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.status == ScanJobStatus.DUPLICATE.value)
            .where(IntakeFile.state == IntakeState.REGISTERED.value)
        ).all()
        for row, original, lifecycle in found:
            _move(
                row, IntakeState.DUPLICATE_CONTENT, moment, IntakeReason.NONE,
                f"Same bytes as scan {original} of this scan session ({lifecycle}); not read.",
            )
            row.duplicate_of_scan_id = int(original) if original is not None else None
            mirrored.append((row.intake_file_id, int(original or 0)))
    return mirrored


def registered_scans(
    database: ProjectDatabase, intake_file_ids: Iterable[int]
) -> dict[int, int | None]:
    """``intake_file_id -> batch_scan id`` (``None`` while unregistered)."""
    ids = list(intake_file_ids)
    with database.session() as session:
        return {
            int(item): (int(scan) if scan is not None else None)
            for item, scan in session.execute(
                select(IntakeFile.intake_file_id, IntakeFile.batch_scan_id).where(
                    IntakeFile.intake_file_id.in_(ids)
                )
            ).all()
        }


__all__ = [
    "INTAKE_SCHEMA_VERSION",
    "IntakeError",
    "IntakeService",
    "LedgerRow",
    "ReadyItem",
    "ReconcileReport",
    "RecoveryReport",
    "Registration",
    "SourceInfo",
    "attach_source",
    "create_source",
    "detach_source",
    "get_source",
    "has_intake_schema",
    "ledger",
    "ledger_row",
    "link_exact_duplicates",
    "list_sources",
    "manual_source",
    "mirror_duplicates",
    "record_manual_batch",
    "registered_scans",
    "set_source_enabled",
    "source_sessions",
    "stalled_items",
    "state_counts",
    "update_source",
    "utc_now",
]
