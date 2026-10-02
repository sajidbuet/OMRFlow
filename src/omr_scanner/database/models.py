"""ORM table definitions for a project database.

Purpose:
    Declare the tables of the current schema. Each phase's section names the
    migration that created it.

What does NOT belong here:
    * Queries. Modules that need data write their own statements or repository
      helpers; models only describe tables.
    * Speculative columns for later phases. ``docs/DATA_MODEL.md`` describes the
      intended evolution; each phase adds its own tables together with a
      migration.

Invariants:
    * Every table declared here must be created by migration 1 in
      :mod:`omr_scanner.database.migrations`. Adding a table to this module
      without a migration is a bug: existing project databases would never gain
      it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Deterministic constraint names.

SQLite cannot drop an unnamed constraint, so later migrations would be unable to
alter anything that SQLAlchemy had auto-named. Fixing the convention now keeps
future schema changes possible.
"""


class Base(DeclarativeBase):
    """Declarative base for every OMRFlow table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class SchemaMigration(Base):
    """Ledger of applied schema migrations.

    One row per applied migration, so the database records not just *which*
    version it is at but how it got there - useful when diagnosing a project
    that has travelled between application versions.
    """

    __tablename__ = "schema_migration"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_by_version: Mapped[str] = mapped_column(String(50), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation naming the migration version."""
        return f"SchemaMigration(version={self.version}, description={self.description!r})"


class ProjectSetting(Base):
    """Key/value project metadata mirrored from ``project.json``.

    Why a key/value table rather than a wide ``project`` table: Phase 0 does not
    yet know which metadata later phases will keep in the database, and a
    key/value store lets a phase add a setting without a migration. Data that is
    *queried* or *joined* (candidates, scans, results) gets proper typed tables
    instead - see ``docs/DATA_MODEL.md``.

    The authoritative copy of project identity lives in ``project.json``; this
    table exists so a database file found on its own can still be identified.
    """

    __tablename__ = "project_setting"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation naming the setting key."""
        return f"ProjectSetting(key={self.key!r})"


class SettingKey:
    """Well-known :class:`ProjectSetting` keys.

    Defined as constants so a typo becomes an import error rather than a silently
    missing row.
    """

    PROJECT_ID = "project_id"
    PROJECT_NAME = "project_name"
    EXAM_NAME = "exam_name"
    PROJECT_FORMAT_VERSION = "project_format_version"
    CREATED_WITH = "created_with"
    ACTIVE_SCAN_SESSION = "active_scan_session"
    """The ``scan_session.scan_session_id`` the operator is working in (0.1.1 phase 2)."""
    DUPLICATE_ID_GROUPING = "duplicate_id_grouping"
    """How duplicate Student IDs are grouped: ``identifier`` (default) or
    ``set_and_identifier`` - an examination-office decision (0.1.1 phase 4)."""
    SCAN_SESSION_BACKFILL = "scan_session_backfill"
    """JSON upgrade report written by the scan-session backfill: when it ran,
    the sessions it created and every ambiguous relationship it left alone."""


# ----------------------------------------------------------------------
# Phase 5: durable batch state
# ----------------------------------------------------------------------
class BatchStatus(StrEnum):
    """Lifecycle of one batch run.

    Stored as its string value so a database row stays readable by a human with
    a SQLite browser, and so adding a state later cannot renumber the existing
    ones.
    """

    NEW = "new"
    """Registered, with its scans enumerated, but not started."""

    RUNNING = "running"
    """A run is in progress *in some process*. A batch found in this state when
    a project is opened was interrupted rather than left running - see
    :func:`~omr_scanner.services.batch_store.recover_interrupted`."""

    INTERRUPTED = "interrupted"
    """A run stopped without finishing and without being cancelled - the
    application was closed or died. Resumable."""

    CANCELLED = "cancelled"
    """The operator stopped the run. Resumable; completed work is kept."""

    COMPLETED = "completed"
    """Every scan reached a terminal state and none failed."""

    COMPLETED_WITH_ERRORS = "completed_with_errors"
    """Every scan reached a terminal state and at least one failed. Distinct
    from :attr:`COMPLETED` because "the loop finished" and "the work succeeded"
    are different claims, and only the second one may be reported as success."""


class ScanJobStatus(StrEnum):
    """Lifecycle of one scan inside a batch.

    The state machine is deliberately small. Every transition is written by the
    coordinator in the parent process (never by a worker), which is what lets
    the resume logic below trust it.
    """

    PENDING = "pending"
    """Enumerated, never attempted. Resume processes these."""

    QUEUED = "queued"
    """Submitted to the pool but not yet started. Treated exactly as
    :attr:`PENDING` on resume."""

    PROCESSING = "processing"
    """A worker is reading it. A row left in this state is *stale* - the process
    that owned it is gone - and is returned to :attr:`PENDING` when the batch is
    reopened, because nothing else could ever move it on."""

    COMPLETED = "completed"
    """Read cleanly, nothing needing a human."""

    WARNING = "warning"
    """Read, but something needs review: a blank, a double mark, a faint one.
    A *result*, not a failure - resume leaves these alone."""

    FAILED = "failed"
    """Could not be read or registered. Resume leaves these alone unless the
    operator explicitly asks for a retry."""

    CANCELLED = "cancelled"
    """Not attempted because the run was stopped. Resume processes these."""

    DUPLICATE = "duplicate"
    """Never read, on purpose: its bytes are identical to an earlier sheet of
    the same scan session, found from the content hash at registration
    (0.1.1 phase 4). Linked to that sheet by its ``scan_rejection`` record
    (``duplicate_content``). Terminal - resume and retry leave it alone."""

    @property
    def is_terminal(self) -> bool:
        """Whether this state represents finished work that resume must keep."""
        return self in (
            ScanJobStatus.COMPLETED,
            ScanJobStatus.WARNING,
            ScanJobStatus.FAILED,
            ScanJobStatus.DUPLICATE,
        )

    @property
    def is_resumable(self) -> bool:
        """Whether an ordinary resume should process a scan in this state."""
        return self in (
            ScanJobStatus.PENDING,
            ScanJobStatus.QUEUED,
            ScanJobStatus.PROCESSING,
            ScanJobStatus.CANCELLED,
        )


class ScanBatch(Base):
    """One batch run over a folder of scans.

    Why the fingerprints are stored rather than only the template id: resuming
    a half-finished batch with a template that has been edited in between, or
    with different recognition thresholds, would silently mix results produced
    under two different sets of rules. The stored fingerprints let
    :func:`~omr_scanner.services.batch_store.check_compatibility` say so before
    a single extra sheet is read, instead of leaving an examination office with
    a CSV whose rows are not comparable.
    """

    __tablename__ = "scan_batch"
    __table_args__ = (Index("ix_scan_batch_session", "scan_session_id", "created_at"),)

    batch_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_folder: Mapped[str] = mapped_column(Text, nullable=False, default="")
    template_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    template_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    template_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    geometry_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    recognition_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    engine_version: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=BatchStatus.NEW.value
    )
    total_scans: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Scan sessions (migration 14, 0.1.1 phase 2) -----------------------
    # Deferred: an ordinary `select(ScanBatch)` does not name them, so a
    # schema-13 project opened read-only (never migrated) still reads every
    # batch. `services.scan_sessions` reads them explicitly, after checking the
    # schema has them.
    scan_session_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("scan_session.scan_session_id", ondelete="RESTRICT"),
        nullable=True,
        deferred=True,
    )
    """The session the batch belongs to. NULL only before the upgrade backfill."""
    sealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True
    )
    """When membership became final. NULL = OPEN (may gain members)."""
    sealed_by: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default="", deferred=True
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scan", server_default="scan", deferred=True
    )
    """:class:`~omr_scanner.domain.scan_sessions.BatchRole` value."""

    # --- Intake (migration 16, 0.1.1 revised phase 5) ------------------------
    source_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("intake_source.source_id", ondelete="RESTRICT"),
        nullable=True,
        deferred=True,
    )
    """The intake source the batch's files came from - provenance, never
    identity. NULL for batches made before intake sources existed."""

    def __repr__(self) -> str:
        """Return a debugging representation naming the batch and its state."""
        return f"ScanBatch(batch_id={self.batch_id!r}, status={self.status!r})"


# ----------------------------------------------------------------------
# 0.1.1 phase 2: scan sessions and batch supersession
# ----------------------------------------------------------------------
class ScanSession(Base):
    """The examination-level unit a project's finite batches are grouped into.

    ``Project -> ScanSession -> ScanBatch -> sheets``; see
    ``docs/decisions/ADR-0005-scan-sessions-and-finite-batches.md``. A session
    pins the template identity of its first batch so it cannot silently mix
    batches read against different geometry or thresholds.
    """

    __tablename__ = "scan_session"
    __table_args__ = (Index("ix_scan_session_state", "state", "created_at"),)

    scan_session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="implicit")
    """``implicit`` (created by Process All), ``operator`` (New Session) or
    ``backfill`` (upgrade)."""
    template_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    template_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    geometry_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    recognition_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    engine_version: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    """The pinned template identity; empty until the first batch is attached."""
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopened_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reopen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_outputs_stale_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Set on reopen: any final output generated while the session was closed is
    stale from this moment (each output also records the close it came from,
    :attr:`GeneratedReport.session_closed_at`)."""
    merged_into_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Set when *Combine* emptied this session into another one."""
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # --- Session scope (migration 15, 0.1.1 phase 4) -------------------------
    # Deferred like scan_batch's lifecycle columns, so a schema-14 project
    # opened read-only (never migrated) still reads every session.
    downstream_batch_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("scan_batch.batch_id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        deferred=True,
    )
    """The session's **bound downstream store**: the ``batch_id`` value its
    reconciliation and result rows are keyed by. ``NULL`` until it first holds
    downstream state. Never derived from batch history once set - see ADR-0007."""

    def __repr__(self) -> str:
        """Return a debugging representation naming the session and its state."""
        return f"ScanSession(id={self.scan_session_id!r}, state={self.state!r})"


class BatchSupersession(Base):
    """One batch replacing another as its session's effective reading.

    First-class rather than a ``supersedes_batch_id`` column, so that the reason,
    the operator, the moment and an audited **reversal** are part of the record,
    and a later reading (Reprocess All, a whole-batch rescan, an algorithmic
    re-read) is expressed the same way. Never deleted: a reversal sets
    :attr:`reversed_at`; the row stays as history.
    """

    __tablename__ = "batch_supersession"
    __table_args__ = (
        Index("ix_batch_supersession_superseded", "superseded_batch_id", "reversed_at"),
        Index("ix_batch_supersession_superseding", "superseding_batch_id"),
    )

    supersession_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    superseded_batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="RESTRICT"), nullable=False
    )
    superseding_batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    recorded_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reversed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reversal_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    @property
    def is_live(self) -> bool:
        """Whether this supersession is in force."""
        return self.reversed_at is None

    def __repr__(self) -> str:
        """Return a debugging representation naming both batches."""
        return (
            f"BatchSupersession({self.superseded_batch_id!r} -> "
            f"{self.superseding_batch_id!r}, live={self.is_live})"
        )


class BatchScan(Base):
    """One scan's durable record inside a batch.

    The recognition result is stored as JSON in :attr:`result_json` rather than
    exploded into typed columns. That is deliberate:
    :meth:`~omr_scanner.services.recognition_models.ScanResult.to_dict` is
    already a versioned, tested contract that round-trips, and duplicating its
    forty-odd fields as columns would mean a migration every time recognition
    gains a measurement. The handful of columns that *are* typed are the ones a
    query needs to filter or sort on without decoding every row.
    """

    __tablename__ = "batch_scan"
    __table_args__ = (
        # One row per source file per batch: re-enumerating a folder must update
        # the existing rows rather than silently duplicate the batch.
        UniqueConstraint("batch_id", "source_path", name="batch_scan_batch_source"),
        Index("ix_batch_scan_batch_status", "batch_id", "status"),
    )

    scan_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    modified_at: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ScanJobStatus.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    registration: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    identifier_value: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    set_code_value: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    output_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    output_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    copied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    error_category: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    """SHA-256 of the source file's bytes (Phase 10, §9/§10).

    Computed once, at registration - not at read time - so that duplicate
    detection and later "has this file changed since import" checks never have
    to re-read the file to get an answer they already have. Empty for a scan
    whose source could not be read at registration time (recorded, not fatal:
    the scan still fails, later, with a readable reason) and for a virtual
    stress-test source, which is identified by seed and index instead - see
    :mod:`omr_scanner.evaluation.stress_dataset`.
    """
    content_hash_algorithm: Mapped[str] = mapped_column(
        String(20), nullable=False, default="sha256"
    )
    """Named rather than assumed, so a stronger algorithm can be introduced
    later without reinterpreting old hashes as the new kind."""

    # --- Intake (migration 16, 0.1.1 revised phase 5) ------------------------
    # Deferred, so a schema-15 project opened read-only still reads every scan.
    intake_file_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("intake_file.intake_file_id", ondelete="SET NULL"),
        nullable=True,
        deferred=True,
    )
    """The intake ledger row this scan's file was observed as - provenance.
    Several scans may name one row (a *Reprocess All* re-reads the same file);
    the row itself names the one registration that consumed it. NULL for scans
    registered before intake existed."""
    registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True
    )
    """When intake registered the file into this scan (NULL before migration 16)."""

    def __repr__(self) -> str:
        """Return a debugging representation naming the file and its state."""
        return f"BatchScan(filename={self.filename!r}, status={self.status!r})"


# ----------------------------------------------------------------------
# Phase 6: conflicts and the audit ledger
# ----------------------------------------------------------------------
class ReviewConflict(Base):
    """One disputed value awaiting, or having received, a human decision.

    The machine's observation is stored here **once, at detection**, and no
    code path in the application updates
    :attr:`machine_value`/:attr:`machine_status`/:attr:`machine_confidence`
    except the re-recognition path, which records the change as an audit event
    first (:class:`AuditEvent`, ``RE_RECOGNISED``). A human correction never
    touches these columns at all: it appends an event, and the effective value
    is projected from the ledger.

    :attr:`state` is a cache. It is always reconstructible by folding this
    conflict's ordered events, and
    ``tests/unit/test_review_store.py`` asserts the two agree - so the cache can
    make the queue fast without becoming a second, divergent source of truth.
    """

    __tablename__ = "review_conflict"
    __table_args__ = (
        # The identity that makes detection idempotent: re-reading a sheet
        # produces the same (type, zone, group) and therefore updates this row
        # instead of creating a second one. Without it a Phase 5 retry would
        # fill the queue with duplicates.
        UniqueConstraint(
            "batch_id",
            "scan_id",
            "conflict_type",
            "zone_id",
            "group_key",
            name="review_conflict_identity",
        ),
        Index("ix_review_conflict_batch_state", "batch_id", "state"),
        Index("ix_review_conflict_scan", "scan_id"),
    )

    conflict_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    scan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("batch_scan.scan_id", ondelete="CASCADE"), nullable=False
    )
    conflict_type: Mapped[str] = mapped_column(String(40), nullable=False)
    scope: Mapped[str] = mapped_column(String(10), nullable=False, default="field")
    severity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(15), nullable=False, default="open")

    zone_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    group_key: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    field_kind: Mapped[str] = mapped_column(String(15), nullable=False, default="other")
    field_label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    question_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    machine_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    machine_status: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    machine_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    machine_top_fill: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    machine_margin: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    machine_candidates: Mapped[str] = mapped_column(Text, nullable=False, default="")
    machine_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    related_scan_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation naming the conflict and its state."""
        return (
            f"ReviewConflict(id={self.conflict_id}, type={self.conflict_type!r}, "
            f"state={self.state!r})"
        )


class AuditEvent(Base):
    """One immutable entry in the provenance ledger.

    **Append-only, enforced at three levels** (Phase 6 brief §27):

    1. The repository (:mod:`omr_scanner.services.review_store`) exposes
       ``append_event`` and no update or delete for events.
    2. Nothing in the application constructs an ``UPDATE`` or ``DELETE`` against
       this table, and a test asserts the module exposes no such function.
    3. SQLite triggers installed by migration 3 raise on any ``UPDATE`` or
       ``DELETE`` of a row here, so even a hand-written statement or a future
       careless ORM flush fails loudly instead of quietly rewriting history.

    **No foreign key, deliberately.** A conflict may in principle be removed by
    housekeeping; the record that it once existed and what a named person
    decided about it must outlive that. ``conflict_id`` is therefore a plain
    indexed column, and the ledger is the one table in the schema that nothing
    cascades into.

    **One ledger for the whole application** (Phase 7). ``entity_type`` and
    ``entity_id`` were added by migration 4 so that a decision about a
    *candidate* or a *script* is recorded here, under the same triggers, rather
    than in a second history table with its own weaker guarantees. Rows written
    before that migration read back as ``entity_type='conflict'`` with an empty
    ``entity_id``; their ``conflict_id`` still identifies them and is still what
    a conflict's history is queried by. **They were deliberately not
    backfilled** - an ``UPDATE`` here is aborted by the triggers, which is
    precisely their job.
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_conflict", "conflict_id", "event_id"),
        Index("ix_audit_event_batch", "batch_id"),
        Index("ix_audit_event_entity", "entity_type", "entity_id", "event_id"),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    scan_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entity_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="conflict", server_default="conflict"
    )
    entity_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    previous_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    new_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    machine_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    reason_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation naming the action and its author."""
        return (
            f"AuditEvent(id={self.event_id}, action={self.action!r}, "
            f"reviewer={self.reviewer!r})"
        )


# ----------------------------------------------------------------------
# Phase 7 - candidate roster and reconciliation
# ----------------------------------------------------------------------
# Every table below holds candidate data. None of it may reach a log line; see
# `docs/ARCHITECTURE.md` ("Logging and privacy"). Repositories that read these
# tables log counts and source row numbers instead.


class CandidateRoster(Base):
    """One imported candidate/attendance list.

    A project may hold several - an office that re-imports a corrected list
    keeps the old one, because reconciliation decisions were made against it
    and an audit trail that points at a deleted roster explains nothing. Only
    one is :attr:`is_active` at a time, and that is the one reconciliation
    reads.

    The *source file* itself is never modified, moved or copied into the
    project. Only :attr:`source_name` - the file's name, not its path - is kept,
    so a project database cannot leak the directory layout of somebody's
    machine.
    """

    __tablename__ = "candidate_roster"
    __table_args__ = (
        Index("ix_candidate_roster_active", "is_active"),
        Index("ix_candidate_roster_set_active", "set_id", "is_active"),
    )

    roster_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("project_set.set_id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Which examination set this roster belongs to.

    The persistent relationship key for per-set attendance: a candidate is
    reached only through a roster, and a roster belongs to exactly one set, so
    the same roll number in two sets is two unrelated candidates in two
    unrelated rosters and cannot collide (the uniqueness constraint on
    :class:`RegisteredCandidate` is per roster, never project-wide).

    ``NULL`` means "imported before per-set attendance existed" - a project
    that had one roster for the whole examination. Nothing guesses which set
    such a roster belongs to; see
    :func:`omr_scanner.services.reconciliation_store.unassigned_rosters` and
    the migration note in ``docs/DATA_MODEL.md``.

    ``ondelete="RESTRICT"``: deleting a set that still has attendance
    imported against it must fail loudly rather than cascade away an
    examination's candidate list - see
    :func:`omr_scanner.services.project_sets.references_to_set`, which
    reports this as a blocking reference before the database ever has to.
    """
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    """The full path of the file this roster was imported from, or ``""``.

    :attr:`source_name` is what a table shows; this is what tells two files
    of the same name in different folders apart. Empty for a roster imported
    before migration 10, which recorded the name only."""
    source_sheet: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_format: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    column_map: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_present: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expected_absent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attendance_unknown: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_attendance_column: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    imported_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation. Names no candidate."""
        return (
            f"CandidateRoster(id={self.roster_id}, "
            f"candidates={self.candidate_count}, active={self.is_active})"
        )


class RegisteredCandidate(Base):
    """One candidate exactly as the imported roster declared them.

    **Write-once.** Nothing in the application updates a row here after import.
    An operator who establishes that a candidate recorded absent actually sat
    the paper does not change :attr:`imported_attendance`; the decision is a
    :class:`ReconciliationDecision` and an :class:`AuditEvent`, and the two
    values are then reported side by side. That separation is the whole reason
    this table exists rather than a mutable "candidate" table.
    """

    __tablename__ = "registered_candidate"
    __table_args__ = (
        # Makes a duplicate ID impossible to store even if validation were
        # bypassed, and is the index reconciliation matches scripts through.
        UniqueConstraint(
            "roster_id", "candidate_id", name="registered_candidate_identity"
        ),
        Index("ix_registered_candidate_roster", "roster_id", "row_order"),
    )

    candidate_row_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    roster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("candidate_roster.roster_id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    row_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_attendance: Mapped[str] = mapped_column(
        String(10), nullable=False, default="unknown"
    )
    imported_value: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation. Deliberately names no candidate."""
        return (
            f"RegisteredCandidate(row_id={self.candidate_row_id}, "
            f"roster={self.roster_id})"
        )


class ReconciliationRun(Base):
    """The state of reconciliation for one roster against one batch.

    One row per pair, updated in place. A new row per *run* would grow without
    bound and, worse, leave yesterday's classifications sitting in the database
    looking current - which the phase brief rules out explicitly.
    """

    __tablename__ = "reconciliation_run"
    __table_args__ = (
        UniqueConstraint("roster_id", "batch_id", name="reconciliation_run_identity"),
    )

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    roster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("candidate_roster.roster_id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    counts: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation."""
        return f"ReconciliationRun(id={self.run_id}, roster={self.roster_id})"


class ReconciliationEntryRow(Base):
    """One candidate's reconciliation state, or one unplaceable script group.

    A **cache of a pure function** - :func:`omr_scanner.services.reconciliation.reconcile`
    recomputes every column here from the roster, the scripts and the standing
    decisions. It is stored so the interface can filter, count and page a
    ten-thousand-row cohort in SQL rather than in Python, and it is rewritten in
    place on every reconciliation so a stale classification cannot survive a
    change of roster.

    :attr:`issues` is a comma-separated set, not a single status, because the
    conditions genuinely co-occur and a schema that could hold only one would
    make a physical script invisible.
    """

    __tablename__ = "reconciliation_entry"
    __table_args__ = (
        UniqueConstraint(
            "roster_id", "batch_id", "entry_key", name="reconciliation_entry_identity"
        ),
        Index("ix_reconciliation_entry_status", "roster_id", "batch_id", "status"),
    )

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    roster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("candidate_roster.roster_id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    entry_key: Mapped[str] = mapped_column(String(80), nullable=False)
    entry_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_row_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_registered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    issues: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    script_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_attendance: Mapped[str] = mapped_column(
        String(10), nullable=False, default="unknown"
    )
    effective_attendance: Mapped[str] = mapped_column(
        String(10), nullable=False, default="unknown"
    )
    attendance_source: Mapped[str] = mapped_column(
        String(10), nullable=False, default="imported"
    )
    resolution: Mapped[str] = mapped_column(String(15), nullable=False, default="open")
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    reason_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewer: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation. Deliberately names no candidate."""
        return (
            f"ReconciliationEntryRow(id={self.entry_id}, status={self.status!r}, "
            f"scripts={self.script_count})"
        )


class ReconciliationScript(Base):
    """The link between one scanned script and the entry it was filed under.

    One row per scan in the batch, always - including a script belonging to
    nobody and a script an operator has set aside. That is what "nothing
    disappears" means in storage terms: a script that is missing from this
    table would be missing from every count and every screen.
    """

    __tablename__ = "reconciliation_script"
    __table_args__ = (
        UniqueConstraint(
            "roster_id", "batch_id", "scan_id", name="reconciliation_script_identity"
        ),
        Index("ix_reconciliation_script_entry", "entry_id"),
    )

    link_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    roster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("candidate_roster.roster_id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    entry_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("reconciliation_entry.entry_id", ondelete="CASCADE"),
        nullable=False,
    )
    scan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("batch_scan.scan_id", ondelete="CASCADE"), nullable=False
    )
    source_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    machine_candidate_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    effective_candidate_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    assigned_candidate_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    assignment: Mapped[str] = mapped_column(String(10), nullable=False, default="machine")
    identifier_unresolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    reason_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewer: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation. Deliberately names no candidate."""
        return (
            f"ReconciliationScript(link={self.link_id}, scan={self.scan_id}, "
            f"excluded={self.excluded})"
        )


class ReconciliationDecision(Base):
    """One standing decision an operator has made.

    The **input** to reconciliation, not an output. Re-running recomputes every
    classification from scratch, and these rows are what survive that and steer
    it - which is why they live apart from
    :class:`ReconciliationEntryRow`, whose every column is disposable.

    A decision is a *current* position; its history is in :class:`AuditEvent`,
    appended in the same transaction and never rewritten. ``tests`` assert the
    two agree.
    """

    __tablename__ = "reconciliation_decision"
    __table_args__ = (
        UniqueConstraint(
            "roster_id",
            "batch_id",
            "target_kind",
            "target_key",
            name="reconciliation_decision_identity",
        ),
    )

    decision_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    roster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("candidate_roster.roster_id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    target_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    target_key: Mapped[str] = mapped_column(String(64), nullable=False)
    assigned_candidate_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    attendance_override: Mapped[str] = mapped_column(
        String(10), nullable=False, default="unknown"
    )
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    reason_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewer: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation. Deliberately names no candidate."""
        return (
            f"ReconciliationDecision(id={self.decision_id}, "
            f"kind={self.target_kind!r})"
        )


# ----------------------------------------------------------------------
# Phase 8 - answer keys, scoring policy and results
# ----------------------------------------------------------------------
# Marks may end up on a transcript, so every table below is arranged around
# one rule: a result must be reproducible from stored inputs. Nothing here
# stores a number that cannot be recomputed, and nothing overwrites an input
# that a stored result depends on.


class AnswerKeyRevision(Base):
    """One question-paper set's answer key, at one revision.

    **Revisions are never edited.** Correcting a verified key that results
    already reference creates revision *n+1* and marks the old one superseded;
    the old row stays, because a :class:`CandidateResult` points at it and
    "which key produced this mark" must always have an answer.

    :attr:`wrong_questions` is stored per revision rather than beside the set,
    because flagging a question invalid changes every mark for that set - it is
    part of the key, not a separate setting that could drift out of step with
    it.
    """

    __tablename__ = "answer_key_revision"
    __table_args__ = (
        UniqueConstraint("set_code", "revision", name="answer_key_revision_identity"),
        Index("ix_answer_key_revision_set", "set_code", "status"),
    )

    key_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_code: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    answers: Mapped[str] = mapped_column(Text, nullable=False)
    wrong_questions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    first_question: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="draft")
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="manual")
    source_scan: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # Provenance (migration 12). Empty on a revision stored by an earlier
    # build, which recorded none of it - shown as "not recorded", never guessed.
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    template_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    template_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    template_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation. Names no candidate, and no answers."""
        return (
            f"AnswerKeyRevision(set={self.set_code!r}, rev={self.revision}, "
            f"status={self.status!r})"
        )


class ScoringPolicyRevision(Base):
    """The marking rules, at one revision.

    Every mark is stored as an **exact rational string** (``"1"``, ``"1/4"``,
    ``"-1/3"``), parsed back with :class:`fractions.Fraction`. A column of
    ``FLOAT`` would make a stored policy round-trip to something a fraction of
    a mark away from what the operator typed, which is precisely what Phase 8
    forbids.

    A new revision is created whenever any score-affecting rule changes.
    Results reference the revision they were computed under, so changing the
    rules makes them **stale** rather than wrong.
    """

    __tablename__ = "scoring_policy_revision"
    __table_args__ = (
        UniqueConstraint("revision", name="scoring_policy_revision_identity"),
    )

    policy_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    correct_mark: Mapped[str] = mapped_column(String(40), nullable=False, default="1")
    blank_mark: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    incorrect_penalty: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    multiple_penalty: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    clamp_minimum: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    minimum_score: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation."""
        return (
            f"ScoringPolicyRevision(rev={self.revision}, mode={self.mode!r}, "
            f"active={self.is_active})"
        )


class CandidateResult(Base):
    """One candidate's mark, and everything needed to reproduce it.

    **The per-question breakdown is deliberately not stored.** A hundred
    questions across ten thousand candidates is a million rows that would have
    to be kept in step with a total they could contradict. Instead this row
    keeps the *inputs* - the answer string, the key revision, the policy
    revision - and the breakdown is regenerated by the same pure function that
    produced the mark. A detail view and a total can then never disagree,
    because there is only one calculation.

    :attr:`answer_key_id` and :attr:`policy_id` are the provenance the phase
    turns on: a result identifies the exact revisions used, so a later key does
    not retroactively change what an earlier mark was computed from.
    """

    __tablename__ = "candidate_result"
    __table_args__ = (
        UniqueConstraint(
            "roster_id", "batch_id", "candidate_id", name="candidate_result_identity"
        ),
        Index("ix_candidate_result_status", "roster_id", "batch_id", "status"),
        Index("ix_candidate_result_key", "answer_key_id"),
    )

    result_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    roster_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("candidate_roster.roster_id", ondelete="CASCADE"),
        nullable=False,
    )
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    set_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    # The authoritative inputs. Everything below them is derived.
    answer_string: Mapped[str] = mapped_column(Text, nullable=False, default="")
    machine_answer_string: Mapped[str] = mapped_column(Text, nullable=False, default="")
    corrected_questions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answer_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    answer_key_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_question: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Derived, and recomputed rather than patched whenever an input changes.
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incorrect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blank_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    multiple_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wrong_question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_score: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    final_score: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    clamped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(String(15), nullable=False, default="blocked")
    blocks: Mapped[str] = mapped_column(Text, nullable=False, default="")

    stale_reasons: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """Reserved, and deliberately always empty.

    Staleness is **derived** on read by
    :func:`~omr_scanner.services.scoring_store.stale_reasons_for`, which
    compares the revisions above against the ones in force. Storing it as well
    would be a second copy of a fact that changes without this row being
    touched - verifying a key makes results stale without writing to any of
    them - so a stored flag would be wrong from the moment it was written, and
    would still be wrong after a restart. The column is kept rather than
    dropped because removing one in SQLite means rebuilding the table, and a
    rebuild of the table that holds every mark is not a tidying-up job.
    """
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation. Deliberately names no candidate."""
        return (
            f"CandidateResult(id={self.result_id}, status={self.status!r}, "
            f"key_rev={self.answer_key_revision}, policy_rev={self.policy_revision})"
        )


# ----------------------------------------------------------------------
# Phase 9 - Result management & reporting.
#
# A generated workbook is an output, never the primary record - the tables
# below exist to make a report *traceable* to what produced it, not to hold a
# second copy of a candidate's mark. See `reporting/excel.py` (the mechanics)
# and `services/report_store.py` (persistence and orchestration).
# ----------------------------------------------------------------------


class ReportTemplateAssociation(Base):
    """The result/absentee workbook an operator has selected for one set.

    **One row per set, updated in place** rather than revisioned. Unlike an
    answer key or a scoring policy, a template association is not
    score-affecting - it is presentation input, and re-selecting a template
    for a set is "change what file this points to", not "supersede a prior
    official standard". :class:`GeneratedReport` is what remains reproducible
    per generation, by recording this row's hash *at that moment*.
    """

    __tablename__ = "report_template_association"
    __table_args__ = (
        UniqueConstraint("set_code", name="report_template_association_set"),
    )

    association_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    set_code: Mapped[str] = mapped_column(String(32), nullable=False)
    set_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("project_set.set_id", ondelete="RESTRICT"),
        nullable=True,
    )
    """The defined set this template belongs to, when there is one.

    :attr:`set_code` remains the row's identity because every Phase 8/9 path
    already looks a template up by the code printed on the paper. This column
    is the *persistent* link to :class:`ProjectSet`, so that correcting a set's
    code does not orphan its template, and so that a template can never be
    silently reached through a set it does not belong to.

    ``NULL`` for a template associated before sets were defined, and for a
    code that names no defined set (which stays possible on purpose: a project
    may have answer keys for a set nobody has entered into the registry yet).
    """
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    """How this template came to be chosen: ``"attendance"`` when it is the
    workbook imported as that set's attendance list - the case Part 2 makes
    ordinary - or ``"manual"`` when an operator picked a separate file."""
    template_path: Mapped[str] = mapped_column(Text, nullable=False)
    template_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    header_row: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    roll_column: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    marks_column: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    serial_column: Mapped[int | None] = mapped_column(Integer, nullable=True)
    name_column: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_column: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation. Names no candidate."""
        return (
            f"ReportTemplateAssociation(set={self.set_code!r}, "
            f"sheet={self.sheet_name!r})"
        )


class ReportLayoutConfig(Base):
    """Report presentation settings: header text, logo, fonts, page setup.

    :attr:`set_code` is ``""`` for the project-wide default and a real set
    code for a per-set override (§16: "Allow set-specific override if
    needed, but provide sensible project-level defaults"). A generation looks
    up the row for its own set first and falls back to the ``""`` row.

    Column headers, logo and page setup only ever *supplement* a template
    that does not already define them - see ``docs/reporting.md`` "Layout
    preservation is the default" - so every text field empty is a valid,
    common configuration meaning "use the template exactly as supplied".
    """

    __tablename__ = "report_layout_config"
    __table_args__ = (
        UniqueConstraint("set_code", name="report_layout_config_set"),
    )

    config_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    title_text: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    subtitle_text: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    examination_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    footer_text: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    column_header_overrides_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    auto_update_marks_header: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    logo_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    logo_max_width_px: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    logo_max_height_px: Mapped[int] = mapped_column(Integer, nullable=False, default=120)

    font_family: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    title_font_size: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    header_font_size: Mapped[int] = mapped_column(Integer, nullable=False, default=11)
    body_font_size: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    bold_headers: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    page_size: Mapped[str] = mapped_column(String(10), nullable=False, default="A4")
    orientation: Mapped[str] = mapped_column(String(10), nullable=False, default="portrait")
    margin_top_mm: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    margin_bottom_mm: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    margin_left_mm: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    margin_right_mm: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    fit_to_width: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scale_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    center_horizontally: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    repeat_header_row: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    def __repr__(self) -> str:
        """Return a debugging representation."""
        return f"ReportLayoutConfig(set={self.set_code!r})"


class GeneratedReport(Base):
    """One report-generation attempt, and everything it was computed from.

    **Append-only.** A new generation writes a new row rather than editing
    the last one for that set - the same rule Phase 8's key and policy
    revisions follow - so "what did the Set A report look like on the day it
    was filed" always has an answer, even after a later regeneration.

    Deliberately holds no candidate data (§21, §49): counts, hashes and
    revision numbers only. The generated workbook itself, on disk at
    :attr:`output_path`, is where the candidate information lives.
    """

    __tablename__ = "generated_report"
    __table_args__ = (
        Index("ix_generated_report_set", "set_code", "generated_at"),
    )

    report_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_code: Mapped[str] = mapped_column(String(32), nullable=False)
    report_type: Mapped[str] = mapped_column(String(20), nullable=False)

    template_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    template_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    answer_key_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    layout_config_snapshot_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )

    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    present_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    absent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scored_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    output_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    status: Mapped[str] = mapped_column(String(15), nullable=False, default="failed")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    application_version: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    # --- Scope and finality (migration 15, 0.1.1 phase 4), deferred ---------
    scan_session_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, deferred=True
    )
    """The scan session the output was generated from (``None``: before
    migration 15, or a batch belonging to no session)."""
    is_final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0", deferred=True
    )
    """A Final Export (from a CLOSED session), as opposed to a preview."""
    session_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, deferred=True
    )
    """The ``closed_at`` of the close the output came from. A final output is
    current only while its session is closed by that same close."""

    def __repr__(self) -> str:
        """Return a debugging representation. Deliberately names no candidate."""
        return (
            f"GeneratedReport(id={self.report_id}, set={self.set_code!r}, "
            f"type={self.report_type!r}, status={self.status!r})"
        )


# ----------------------------------------------------------------------
# Phase 10 - Integration, recovery & production hardening.
#
# Nothing here duplicates Phase 5's batch state or Phase 6's audit ledger -
# both are reused as-is. This section adds exactly two things those phases
# had no reason to need: a place to keep a *superseded* recognition result
# when a sheet is deliberately reprocessed (`BatchScanHistory`), and a
# reproducibility snapshot of what a batch was run with (`ProcessingManifest`).
# See `services/batch_store.py::mark_for_reprocessing` and
# `services/processing_manifest.py`.
# ----------------------------------------------------------------------


class BatchScanHistory(Base):
    """A recognition result a reprocessing run has superseded.

    **Append-only** (enforced by triggers, same pattern as :class:`AuditEvent`).
    :class:`BatchScan` holds only the *current* result - Phase 5's own design,
    kept unchanged here - so reprocessing a completed sheet would otherwise
    overwrite the only record of what the machine read the first time. This
    table exists so it does not: before
    :func:`~omr_scanner.services.batch_store.mark_for_reprocessing` resets a
    row to ``pending``, it archives the row's current state here, first.

    This is deliberately separate from Phase 6's :class:`AuditEvent` ledger.
    That ledger records *human decisions about a machine value*; this table
    records that recognition itself was asked to run again, by whom, and what
    it had produced immediately before - a different question, about the
    recognition stage rather than the review stage, and one Phase 6 was never
    asked to answer.
    """

    __tablename__ = "batch_scan_history"
    __table_args__ = (
        Index("ix_batch_scan_history_scan", "scan_id", "archived_at"),
    )

    history_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("batch_scan.scan_id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    previous_outcome: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    previous_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    previous_result_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    previous_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation naming the archived scan."""
        return f"BatchScanHistory(scan_id={self.scan_id}, archived_at={self.archived_at!r})"


class ProcessingManifest(Base):
    """A reproducibility snapshot: exactly what a batch was run with.

    One row per manifest generation - not one per batch - because a manifest
    may reasonably be regenerated (an operator asking "what produced this?"
    after answer keys or scoring were later revised should still be able to
    see the manifest as it stood right after processing, not a snapshot
    quietly rewritten under it). See
    :func:`omr_scanner.services.processing_manifest.build_manifest`, which
    assembles :attr:`payload_json` by reading the tables Phases 1-9 already
    maintain - :class:`ScanBatch`'s own fingerprints, answer-key and
    scoring-policy revisions, report template associations - rather than
    duplicating any of them here.
    """

    __tablename__ = "processing_manifest"
    __table_args__ = (
        Index("ix_processing_manifest_batch", "batch_id", "generated_at"),
    )

    manifest_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    application_version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    def __repr__(self) -> str:
        """Return a debugging representation naming the batch and moment."""
        return (
            f"ProcessingManifest(batch_id={self.batch_id!r}, "
            f"generated_at={self.generated_at!r})"
        )


# ----------------------------------------------------------------------
# Project-level examination sets
# ----------------------------------------------------------------------
class ProjectSet(Base):
    """One operator-defined division of the examination this project processes.

    The project's own registry of which sets exist - ``Set 10 - Name of Post:
    Assistant Engineer (Electrical)`` - as distinct from the bare ``set_code``
    strings that :class:`AnswerKeyRevision`,
    :class:`ReportTemplateAssociation`, :class:`GeneratedReport` and
    :attr:`BatchScan.set_code_value` already carry. Those are *references* to
    a set by the code printed on the paper; this table is where a project says
    what those codes mean.

    :attr:`code` deliberately uses the same ``String(32)`` those columns use,
    and holds the same value, so that a later phase can join them directly.
    **No foreign key is declared from them to here yet**: doing so would
    change how answer keys and reports behave, which the phase that
    introduced this table was explicitly scoped out of. The join is possible;
    it is simply not yet made.

    :attr:`set_id` - not :attr:`code`, and never a row number - is what
    attendance, candidates and results should link to when they gain that
    relationship, because an operator may legitimately correct a code
    (a typo, a renumbered paper) long after data has been attached to the set
    it names.

    Set identity (migration 13, phase 0.1.1-A): :attr:`canonical_code` is
    the canonical logical identity of :attr:`code`
    (:func:`omr_scanner.domain.set_identity.canonical_code`), unique where not
    NULL. It is NULL only for the second and later of a group of sets that a
    project created before canonical identity existed and whose codes now
    collide (``A`` and ``a``): those rows are kept, reported, and never merged.
    :attr:`physical_mark` is what the sheet prints for the set when that is not
    :attr:`code` (Set ``10`` printed as ``A``); ``""`` means the code itself.
    """

    __tablename__ = "project_set"
    __table_args__ = (
        # One definition per code. The service layer checks this first and
        # reports it in plain language; the constraint is what guarantees it
        # even if some future caller forgets to.
        UniqueConstraint("code", name="project_set_code"),
        Index("ix_project_set_order", "display_order"),
        # One set per canonical identity. Partial, because a colliding legacy
        # row deliberately carries NULL until an operator renames it.
        Index(
            "ux_project_set_canonical_code",
            "canonical_code",
            unique=True,
            sqlite_where=text("canonical_code IS NOT NULL"),
        ),
    )

    set_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canonical_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    physical_mark: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default=""
    )

    def __repr__(self) -> str:
        """Return a debugging representation naming the set and its code."""
        return f"ProjectSet(set_id={self.set_id!r}, code={self.code!r})"


# ----------------------------------------------------------------------
# Reject & Rescan
# ----------------------------------------------------------------------
class ScanRejection(Base):
    """An operator's standing decision that one scan is unusable.

    The **current position** of a scan's logical lifecycle - see
    :class:`~omr_scanner.domain.scan_lifecycle.LifecycleState` - kept apart
    from :class:`BatchScan` on purpose. ``batch_scan`` is the *processing*
    record: reprocessing archives and resets it, resume rewrites its status.
    A rejection must survive all of that, so it lives here, keyed by the
    scan, and nothing in the processing path writes to this table.

    Its **history** is in :class:`AuditEvent` (``entity_type`` of
    ``scan_lifecycle``), appended in the same transaction as every change
    here - the same arrangement :class:`ReconciliationDecision` uses. A row is
    never deleted: an undone rejection rests with ``state='active'``, so the
    record that the scan was once rejected outlives the undo.

    **Logical state and file state are separate columns.** :attr:`state` says
    whether the scan may contribute to a result; :attr:`file_state` says
    whether its image is still on disk. Purging an image changes only the
    second.

    Identity columns are **snapshots and declarations, not effective
    values**. :attr:`recognised_candidate_id` is what the review ledger said
    at rejection time; :attr:`declared_candidate_id` is what the operator
    typed to identify the case. Neither feeds reconciliation's effective
    Student ID - that remains the review ledger's alone.
    """

    __tablename__ = "scan_rejection"
    __table_args__ = (
        UniqueConstraint("scan_id", name="scan_rejection_scan"),
        # A scan replaces at most one rejected original. NULLs are distinct in
        # SQLite, so every case still awaiting a rescan can hold NULL here.
        UniqueConstraint("replacement_scan_id", name="scan_rejection_replacement"),
        Index("ix_scan_rejection_batch_state", "batch_id", "state"),
    )

    rejection_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("batch_scan.scan_id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_batch.batch_id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    declared_candidate_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    declared_set_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    recognised_candidate_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    recognised_set_code: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    source_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    rejected_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    replacement_scan_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("batch_scan.scan_id", ondelete="SET NULL"), nullable=True
    )
    replaced_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    replaced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reimport_of_scan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_state: Mapped[str] = mapped_column(String(15), nullable=False, default="present")
    file_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """JSON: every file a purge moved or deleted, where to, and what it left
    in place and why - so the record explains itself without the image."""
    file_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    file_action_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation. Names no candidate."""
        return f"ScanRejection(scan_id={self.scan_id}, state={self.state!r})"


# ----------------------------------------------------------------------
# 0.1.1 revised phase 5: intake sources and the intake ledger (migration 16)
# ----------------------------------------------------------------------
class IntakeSource(Base):
    """A named place scans arrive from: a watched folder, or the manual import.

    **Project-level** - a source outlives any one scan session and is attached
    to a session when it is used (:class:`IntakeSourceAttachment`). The source
    that produced a file is provenance; it never decides whose script a file
    is. Reachability is *source* state and never implies anything about its
    files' state. See ``docs/intake.md``.
    """

    __tablename__ = "intake_source"
    __table_args__ = (Index("ix_intake_source_kind", "kind", "enabled"),)

    source_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    """:class:`~omr_scanner.domain.intake.SourceKind` value."""
    root_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """The folder, local or UNC, exactly as configured (never normalised away).
    Empty for the manual source, whose files are recorded by their own path."""
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exclusions_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    policy_json: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """The source's :class:`~omr_scanner.domain.intake.StabilityPolicy`; empty
    means the default for its path (local or network)."""
    ingest_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="copy")
    """:class:`~omr_scanner.domain.intake.IngestMode` value (ADR-0008)."""
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """The built-in manual source; at most one per project."""
    reachability: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    reachability_detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reachability_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """The last **successful** full reconciliation."""
    last_file_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_file_seen_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        """Return a debugging representation naming the source."""
        return f"IntakeSource(id={self.source_id!r}, kind={self.kind!r}, label={self.label!r})"


class IntakeSourceAttachment(Base):
    """Which scan session a source serves, and when (history, never deleted).

    At most one live attachment per source (``detached_at`` NULL). A file is
    intended for the session its source was attached to when it was first
    observed; changing the attachment never moves an existing ledger row.
    """

    __tablename__ = "intake_source_attachment"
    __table_args__ = (
        Index(
            "uq_intake_attachment_live",
            "source_id",
            unique=True,
            sqlite_where=text("detached_at IS NULL"),
        ),
        Index("ix_intake_attachment_session", "scan_session_id"),
    )

    attachment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("intake_source.source_id", ondelete="RESTRICT"), nullable=False
    )
    scan_session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("scan_session.scan_session_id", ondelete="RESTRICT"), nullable=False
    )
    attached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attached_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    detached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detached_by: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class IntakeFile(Base):
    """The intake ledger: one row per observed version of one file in one source.

    **Path is not identity.** A row is one ``(source, relative path, content)``
    - unique once its content hash is known. Unchanged ``(size, mtime_ns)`` is
    only a cache that spares re-reading a file on every pass. The same path
    holding new bytes later is a **new** row (``path_reused`` when the old
    row's content was consumed); the old row is never changed into the new
    file. Exactly one row per ``(source, relative path)`` is *current*.

    Filesystem intake state only. Once registered, the file's processing state
    is its :class:`BatchScan` status - never duplicated here. High-volume
    machine state: polls are not audited.
    """

    __tablename__ = "intake_file"
    __table_args__ = (
        Index(
            "uq_intake_file_current_path",
            "source_id",
            "relative_path",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
        Index(
            "uq_intake_file_content",
            "source_id",
            "relative_path",
            "content_sha256",
            unique=True,
        ),
        Index("ix_intake_file_hash", "content_sha256"),
        Index("ix_intake_file_state_ready", "state", "ready_at", "intake_file_id"),
        Index("ix_intake_file_source_state", "source_id", "state"),
        Index("ix_intake_file_session", "scan_session_id", "state"),
        Index(
            "uq_intake_file_batch_scan",
            "batch_scan_id",
            unique=True,
            sqlite_where=text("batch_scan_id IS NOT NULL"),
        ),
    )

    intake_file_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("intake_source.source_id", ondelete="RESTRICT"), nullable=False
    )
    scan_session_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("scan_session.scan_session_id", ondelete="RESTRICT"), nullable=True
    )
    """The session this observation is intended for: its source's live
    attachment when first observed (NULL while the source served none)."""
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    """``/``-separated, relative to the source root; for the manual source, the
    file's own path. Provenance and location, not identity."""
    absolute_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """The path as observed, platform form, never normalised."""
    file_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mtime_ns: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """SHA-256 of the bytes that were hashed **and fully decoded from one read**.
    NULL until verified."""
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    """:class:`~omr_scanner.domain.intake.IntakeState` value."""
    state_reason: Mapped[str] = mapped_column(String(30), nullable=False, default="")
    """:class:`~omr_scanner.domain.intake.IntakeReason` value."""
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """The last error or explanation, in plain words."""
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When the current ``(size, mtime_ns)`` was first observed. NULL after a
    restart: stability is never trusted across one."""
    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Consecutive observations of the current ``(size, mtime_ns)``."""
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Full reads of the current version that failed to decode."""
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """When it first became ready - with the row id, the registration order."""
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverify_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """Ready before a restart: re-read before registration (order kept)."""
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """Seen by the last listing that could have seen it."""
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """The latest row for its ``(source, relative path)``."""
    path_reused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    """New bytes at a path whose earlier content was already registered."""
    previous_intake_file_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("intake_file.intake_file_id", ondelete="RESTRICT"), nullable=True
    )
    """The row that held this path before (path reuse), or that already holds
    these bytes at this path (``unchanged_content``)."""
    image_format: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    image_width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    image_height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    batch_scan_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("batch_scan.scan_id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    """The ``batch_scan`` its registration created (or adopted). Unique."""
    duplicate_of_scan_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("batch_scan.scan_id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    """For ``duplicate_content``: the effective sheet of the session its bytes repeat."""
    ingest_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    """The verified project copy, relative to the project root; empty when
    read in place (ADR-0008)."""
    registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        """Return a debugging representation naming the row and its state."""
        return (
            f"IntakeFile(id={self.intake_file_id}, path={self.relative_path!r}, "
            f"state={self.state!r})"
        )
