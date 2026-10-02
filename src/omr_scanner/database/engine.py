"""Project database engine and session lifecycle.

Purpose:
    Create, open and cleanly close the SQLite database that belongs to one
    project, and hand out short-lived sessions.

Responsibilities:
    * Build a correctly configured SQLAlchemy engine for a project file.
    * Run pending migrations on open.
    * Provide :meth:`ProjectDatabase.session`, a transactional scope that commits
      on success and rolls back on failure.

What does NOT belong here:
    * Table definitions (:mod:`omr_scanner.database.models`) or migration
      contents (:mod:`omr_scanner.database.migrations`).
    * Business queries.

Design notes:
    * Journal mode is left at SQLite's default (rollback journal) rather than
      WAL. Projects are frequently stored in synchronised folders (OneDrive,
      network shares) where the extra ``-wal``/``-shm`` side files are a common
      source of corruption and sync conflicts. OMRFlow is single-writer, so WAL
      would buy little. See ``docs/decisions/ADR-0002-project-on-disk-layout.md``.
    * ``PRAGMA foreign_keys=ON`` is enabled per connection because SQLite
      disables foreign key enforcement by default, and later phases rely on
      cascading relationships (scan -> recognition result -> conflict).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from omr_scanner.database.migrations import apply_pending_migrations, current_schema_version
from omr_scanner.errors import DatabaseError

logger = logging.getLogger(__name__)


BUSY_TIMEOUT_MS = 5_000
"""How long a connection waits for another connection's write lock before
SQLite reports ``database is locked`` (ADR-0009).

A safety net, not the concurrency strategy: one coordinator per project owns
the processing writes (the continuous engine or the Scan stage's recorder) and
every write is a short transaction, so a second writer in the same process - a
Resolve decision on the GUI thread - waits at most one such transaction. Five
seconds is the value Python's ``sqlite3`` module has always applied implicitly
(its ``timeout`` default); setting it here makes it explicit, testable and the
same on every connection, read-only ones included."""


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
    """Configure each new SQLite connection: foreign keys on, explicit busy timeout."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


class ProjectDatabase:
    """An open connection pool for one project's SQLite file.

    The object owns the SQLAlchemy engine and must be closed (or used as a
    context manager) so the file handle is released - on Windows an open handle
    prevents the project folder from being moved or deleted.

    Args:
        engine: Engine bound to the project database file.
        path: The database file the engine points at.
    """

    def __init__(self, engine: Engine, path: Path, *, read_only: bool = False) -> None:
        self._engine = engine
        self._path = path
        self._read_only = read_only
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @property
    def engine(self) -> Engine:
        """The underlying SQLAlchemy engine."""
        return self._engine

    @property
    def path(self) -> Path:
        """Absolute path of the SQLite file."""
        return self._path

    @property
    def schema_version(self) -> int:
        """Schema version currently recorded in the database."""
        return current_schema_version(self._engine)

    @property
    def read_only(self) -> bool:
        """Whether this connection was opened read-only (Phase 10, §42).

        A read-only connection never runs migrations and any write inside
        :meth:`session` fails at the SQLite level - the GUI uses this flag to
        visibly disable actions rather than let them fail silently, per the
        phase brief's explicit requirement that a read-only mode must never
        let an action look like it succeeded when it could not write.
        """
        return self._read_only

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session that commits on success and rolls back on error.

        Yields:
            An open :class:`sqlalchemy.orm.Session`.

        Raises:
            DatabaseError: The transaction failed; the original SQLAlchemy error
                is attached as ``__cause__``.
        """
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            raise DatabaseError(
                f"Database transaction failed: {exc}",
                user_message="A database operation failed. See the log for details.",
            ) from exc
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        """Dispose of the engine and release the file handle."""
        self._engine.dispose()
        logger.debug("Closed project database %s", self._path)

    def __enter__(self) -> ProjectDatabase:
        """Enter a context manager that closes the database on exit."""
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Close the database when leaving the context."""
        self.close()


def recover_interrupted_transaction(database_path: Path) -> bool:
    """Let SQLite roll back a *hot journal* a killed writer left behind.

    A process killed in the middle of a commit leaves ``<db>-journal`` holding
    the pages needed to undo its partial write. SQLite rolls such a journal
    back automatically - but only on a connection that may write, so a
    read-only connection (:func:`open_project_database` with ``read_only``,
    used to peek at the schema version before a migration) fails with
    "attempt to write a readonly database" until it is.

    This opens one ordinary connection and reads, which is exactly SQLite's
    own crash recovery: the database returns to its last committed state and
    nothing else is written. SQLite only treats a journal as hot when no live
    process holds the database's write lock, so a running writer is never
    disturbed. Found by the 0.1.1 revised phase 5 real-kill intake test.

    Returns:
        Whether a journal was present and a recovery connection succeeded.
    """
    journal = database_path.with_name(database_path.name + "-journal")
    if not database_path.is_file() or not journal.exists():
        return False
    try:
        connection = sqlite3.connect(str(database_path), timeout=10)
        try:
            connection.execute("PRAGMA schema_version").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        logger.exception("Could not recover the interrupted transaction in %s", database_path)
        return False
    logger.warning("Rolled back an interrupted transaction in %s", database_path.name)
    return True


def open_project_database(
    database_path: Path,
    *,
    create: bool = False,
    echo: bool = False,
    read_only: bool = False,
) -> ProjectDatabase:
    """Open a project database, applying any pending schema migrations.

    Args:
        database_path: Path of the SQLite file. Its parent directory must exist.
        create: When ``True`` the file may be created if missing. When ``False``
            a missing file is an error, which is what distinguishes "open an
            existing project" from "create a new one".
        echo: Forward SQL statements to the logger; debugging aid only.
        read_only: Open the file without writing to it at all - no migration
            is attempted, even if one is pending, and every write inside
            :meth:`ProjectDatabase.session` fails at the SQLite level.
            Incompatible with ``create``. This is the safe-mode path (Phase
            10, §8/§42): a project a newer build wrote, or one whose health
            check failed, can still be *looked at* without risking a write
            this build's migrations do not understand, or a write to a
            database already known to be damaged.

    Returns:
        An open :class:`ProjectDatabase`, at :data:`~omr_scanner.database.SCHEMA_VERSION`
        unless ``read_only`` left it at whatever version the file already had.

    Raises:
        DatabaseError: The file is missing, is not a database, ``read_only``
            and ``create`` were both given, or (not read-only) a migration
            failed.
        SchemaVersionError: Not read-only, and the file was written by a newer
            OMRFlow build.
    """
    resolved = database_path.expanduser().resolve()

    if read_only and create:
        raise DatabaseError(
            "open_project_database: read_only and create are mutually exclusive",
            user_message="Internal error opening the project database.",
        )
    if not create and not resolved.is_file():
        raise DatabaseError(
            f"Project database not found: {resolved}",
            user_message="The project database file is missing.",
        )
    if create and not resolved.parent.is_dir():
        raise DatabaseError(
            f"Cannot create database, directory does not exist: {resolved.parent}",
            user_message="The project folder does not exist.",
        )

    if read_only:
        # A URI connection, so SQLite itself refuses a write rather than this
        # merely being an application-level convention nothing enforces.
        engine = create_engine(
            f"sqlite:///file:{resolved.as_posix()}?mode=ro&uri=true", echo=echo, future=True
        )
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
        logger.info("Project database %s opened read-only", resolved.name)
        return ProjectDatabase(engine, resolved, read_only=True)

    engine = create_engine(f"sqlite:///{resolved}", echo=echo, future=True)
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)

    try:
        applied = apply_pending_migrations(engine)
    except Exception:
        engine.dispose()
        raise

    if applied:
        logger.info("Project database %s migrated to version %d", resolved.name, applied[-1])
    else:
        logger.debug("Project database %s already current", resolved.name)

    return ProjectDatabase(engine, resolved)
