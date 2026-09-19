"""Forward-only schema migrations for project databases.

Purpose:
    Bring a project database - new or created by an older build - up to the
    schema this build expects, and refuse to touch one written by a newer build.

Strategy (see ``docs/decisions/ADR-0003-schema-migrations.md``):
    * Migrations are an ordered tuple of :class:`Migration` records with
      consecutive version numbers starting at 1.
    * ``SCHEMA_VERSION`` is derived from that tuple; it is never edited by hand.
    * Each migration runs inside its own transaction and, on success, inserts a
      row into ``schema_migration``. A failed migration leaves the database at
      the previous version.
    * Migrations are forward-only. Opening a database whose version exceeds
      ``SCHEMA_VERSION`` raises :class:`omr_scanner.errors.SchemaVersionError`
      rather than guessing how to downgrade.

What does NOT belong here:
    * Data import/repair routines. A migration adapts *structure*; bulk data
      work belongs in a service so it can report progress.
    * ``Base.metadata.create_all`` at application start-up. Schema creation must
      go through migration 1 so that every database carries a ledger row.

How to add a migration (Phase 1 onward):
    1. Add the table/column to :mod:`omr_scanner.database.models`.
    2. Append a :class:`Migration` with the next version number and a function
       that performs the change with explicit SQL or ``table.create(connection)``.
    3. Add a test that opens a database created at the previous version and
       asserts the upgrade succeeds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, insert, inspect, select, text

from omr_scanner import __version__
from omr_scanner.database.models import (
    AuditEvent,
    Base,
    BatchScan,
    CandidateRoster,
    ProjectSetting,
    ReconciliationDecision,
    ReconciliationEntryRow,
    ReconciliationRun,
    ReconciliationScript,
    RegisteredCandidate,
    ReviewConflict,
    ScanBatch,
    SchemaMigration,
)
from omr_scanner.errors import DatabaseError, SchemaVersionError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    """One schema change.

    Attributes:
        version: Consecutive version number, starting at 1.
        description: Short text stored in the ledger.
        apply: Callable performing the change on an open connection. It must be
            idempotent only in the sense of being safe to re-run after a failed
            attempt that rolled back; it is never run twice on success.
    """

    version: int
    description: str
    apply: Callable[[Connection], None]


def _migration_001_initial_schema(connection: Connection) -> None:
    """Create the schema-version ledger and the project settings table.

    Historically this was ``Base.metadata.create_all``. It is now pinned to the
    two tables that actually belonged to version 1: leaving it as "create
    everything" would mean a brand-new database got the Phase 5 batch tables
    from migration 1 and then migration 2 tried to create them again, while an
    existing database took a different path to the same schema. One table, one
    migration, the same sequence for every database.
    """
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[SchemaMigration.__tablename__],
            Base.metadata.tables[ProjectSetting.__tablename__],
        ],
    )


def _migration_002_batch_persistence(connection: Connection) -> None:
    """Add durable batch state (Phase 5): ``scan_batch`` and ``batch_scan``.

    Nothing in an existing project needs converting - before this version there
    was no batch state to convert, because a run lived only in the Scan page's
    memory and was lost when the window closed.
    """
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[ScanBatch.__tablename__],
            Base.metadata.tables[BatchScan.__tablename__],
        ],
    )


AUDIT_IMMUTABILITY_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS audit_event_is_append_only_update
    BEFORE UPDATE ON audit_event
    BEGIN
        SELECT RAISE(ABORT, 'audit_event is append-only: rows cannot be updated');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_event_is_append_only_delete
    BEFORE DELETE ON audit_event
    BEGIN
        SELECT RAISE(ABORT, 'audit_event is append-only: rows cannot be deleted');
    END
    """,
)
"""Database-level enforcement that the provenance ledger is append-only.

Two triggers, not an elaborate scheme (Phase 6 brief §51 explicitly rules out
building tamper-proof enterprise logging here). Their job is to turn an
accidental rewrite - a careless ORM flush, a hand-written statement during
maintenance, a future refactor that forgets - into a loud failure rather than a
silent loss of history. Deliberate, documented removal by a database
administrator remains possible; that is outside what the application can or
should prevent.

``IF NOT EXISTS`` so re-running the migration on a partially applied database
is safe."""


def _migration_003_review_and_audit(connection: Connection) -> None:
    """Add Phase 6 conflict review: ``review_conflict``, ``audit_event``.

    Purely additive. Phase 5's batches, scans and recognition results are left
    exactly as they are - a project processed before this version opens, keeps
    every result, and simply has no conflicts recorded for its existing
    batches until those are reviewed or re-run.
    """
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[ReviewConflict.__tablename__],
            Base.metadata.tables[AuditEvent.__tablename__],
        ],
    )
    for statement in AUDIT_IMMUTABILITY_TRIGGERS:
        connection.execute(text(statement))


def _migration_004_reconciliation(connection: Connection) -> None:
    """Add Phase 7 candidate reconciliation, and generalise the audit ledger.

    Two parts.

    **The five new tables** are purely additive: a project processed before
    this version keeps every batch, result and conflict, and simply has no
    roster until one is imported.

    **``audit_event`` gains ``entity_type`` and ``entity_id``** so that a
    decision about a candidate or a script is recorded in the same append-only
    ledger, under the same triggers, as a decision about a recognition
    conflict - rather than in a second history table with its own, weaker
    guarantees.

    Existing rows are **deliberately not backfilled.** ``ADD COLUMN`` is a
    schema change and does not fire the immutability triggers; an ``UPDATE`` to
    populate the new columns would, and rightly so. The column default
    (``'conflict'``) is therefore chosen to be already correct for every row
    that predates this migration, and a conflict's history is still queried by
    ``conflict_id`` exactly as before. Nothing has to be rewritten, so nothing
    is.
    """
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[CandidateRoster.__tablename__],
            Base.metadata.tables[RegisteredCandidate.__tablename__],
            Base.metadata.tables[ReconciliationRun.__tablename__],
            Base.metadata.tables[ReconciliationEntryRow.__tablename__],
            Base.metadata.tables[ReconciliationScript.__tablename__],
            Base.metadata.tables[ReconciliationDecision.__tablename__],
        ],
    )

    existing = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(audit_event)")).all()
    }
    if "entity_type" not in existing:
        connection.execute(
            text(
                "ALTER TABLE audit_event ADD COLUMN entity_type "
                "VARCHAR(20) NOT NULL DEFAULT 'conflict'"
            )
        )
    if "entity_id" not in existing:
        connection.execute(
            text(
                "ALTER TABLE audit_event ADD COLUMN entity_id "
                "VARCHAR(64) NOT NULL DEFAULT ''"
            )
        )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_audit_event_entity "
            "ON audit_event (entity_type, entity_id, event_id)"
        )
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="Initial schema: schema_migration, project_setting",
        apply=_migration_001_initial_schema,
    ),
    Migration(
        version=2,
        description="Phase 5 batch persistence: scan_batch, batch_scan",
        apply=_migration_002_batch_persistence,
    ),
    Migration(
        version=3,
        description="Phase 6 conflict review: review_conflict, audit_event",
        apply=_migration_003_review_and_audit,
    ),
    Migration(
        version=4,
        description=(
            "Phase 7 reconciliation: candidate_roster, registered_candidate, "
            "reconciliation_run/entry/script/decision; audit_event entity columns"
        ),
        apply=_migration_004_reconciliation,
    ),
)

SCHEMA_VERSION: int = MIGRATIONS[-1].version
"""Schema version this build writes and expects."""


def current_schema_version(engine: Engine) -> int:
    """Return the schema version recorded in the database.

    Args:
        engine: Engine bound to the project database.

    Returns:
        The highest applied migration version, or ``0`` for a database that has
        never been migrated (including an empty file).
    """
    inspector = inspect(engine)
    if SchemaMigration.__tablename__ not in inspector.get_table_names():
        return 0
    with engine.connect() as connection:
        highest = connection.execute(
            select(SchemaMigration.version).order_by(SchemaMigration.version.desc()).limit(1)
        ).scalar()
    return int(highest) if highest is not None else 0


def apply_pending_migrations(engine: Engine) -> tuple[int, ...]:
    """Upgrade the database to :data:`SCHEMA_VERSION`.

    Args:
        engine: Engine bound to the project database.

    Returns:
        The versions applied by this call, in order. Empty when the database was
        already current.

    Raises:
        SchemaVersionError: The database was written by a newer build.
        DatabaseError: A migration failed; the database remains at the last
            successfully applied version.
    """
    version = current_schema_version(engine)
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Project database schema version {version} is newer than supported "
            f"version {SCHEMA_VERSION}",
            user_message=(
                "This project was created with a newer version of OMRFlow. "
                "Please update OMRFlow to open it."
            ),
        )

    applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version <= version:
            continue
        logger.info("Applying schema migration %d (%s)", migration.version, migration.description)
        try:
            with engine.begin() as connection:
                migration.apply(connection)
                connection.execute(
                    insert(SchemaMigration).values(
                        version=migration.version,
                        description=migration.description,
                        applied_at=datetime.now(UTC),
                        applied_by_version=__version__,
                    )
                )
        # Any driver-level failure is translated into a DatabaseError so callers
        # never have to import SQLAlchemy exception types.
        except Exception as exc:
            raise DatabaseError(
                f"Schema migration {migration.version} failed: {exc}",
                user_message="The project database could not be upgraded.",
            ) from exc
        applied.append(migration.version)

    return tuple(applied)
