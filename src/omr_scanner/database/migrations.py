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

from sqlalchemy import Connection, Engine, insert, inspect, select

from omr_scanner import __version__
from omr_scanner.database.models import Base, SchemaMigration
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
    """Create the schema-version ledger and the project settings table."""
    Base.metadata.create_all(connection)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        description="Initial schema: schema_migration, project_setting",
        apply=_migration_001_initial_schema,
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
