"""ORM table definitions for a project database.

Purpose:
    Declare the tables that exist in schema version 1.

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
    PROJECT_FORMAT_VERSION = "project_format_version"
    CREATED_WITH = "created_with"


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

    @property
    def is_terminal(self) -> bool:
        """Whether this state represents finished work that resume must keep."""
        return self in (
            ScanJobStatus.COMPLETED,
            ScanJobStatus.WARNING,
            ScanJobStatus.FAILED,
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

    def __repr__(self) -> str:
        """Return a debugging representation naming the batch and its state."""
        return f"ScanBatch(batch_id={self.batch_id!r}, status={self.status!r})"


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
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_conflict", "conflict_id", "event_id"),
        Index("ix_audit_event_batch", "batch_id"),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    scan_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
