"""Tests for database initialisation, reopening and schema migration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from omr_scanner.database import SCHEMA_VERSION, open_project_database
from omr_scanner.database.migrations import (
    MIGRATIONS,
    apply_pending_migrations,
    current_schema_version,
)
from omr_scanner.database.models import ProjectSetting, SchemaMigration
from omr_scanner.errors import DatabaseError, SchemaVersionError


def test_initialisation_creates_the_schema_and_records_the_version(tmp_path: Path):
    with open_project_database(tmp_path / "database.sqlite", create=True) as database:
        assert database.schema_version == SCHEMA_VERSION
        with database.session() as session:
            applied = session.execute(select(SchemaMigration.version)).scalars().all()

    assert list(applied) == [migration.version for migration in MIGRATIONS]


def test_reopening_an_existing_database_applies_nothing(tmp_path: Path):
    path = tmp_path / "database.sqlite"
    with open_project_database(path, create=True) as database:
        first_version = database.schema_version

    with open_project_database(path) as reopened:
        assert reopened.schema_version == first_version
        assert apply_pending_migrations(reopened.engine) == ()


def test_data_survives_a_close_and_reopen(tmp_path: Path):
    path = tmp_path / "database.sqlite"
    with open_project_database(path, create=True) as database, database.session() as session:
        session.add(
            ProjectSetting(key="project_name", value="Persisted Exam", updated_at=datetime.now(UTC))
        )

    with open_project_database(path) as reopened, reopened.session() as session:
        stored = session.get(ProjectSetting, "project_name")
        assert stored is not None
        assert stored.value == "Persisted Exam"


def test_opening_a_missing_database_without_create_fails(tmp_path: Path):
    with pytest.raises(DatabaseError, match="not found"):
        open_project_database(tmp_path / "absent.sqlite")


def test_creating_in_a_missing_directory_fails(tmp_path: Path):
    with pytest.raises(DatabaseError, match="directory does not exist"):
        open_project_database(tmp_path / "nowhere" / "database.sqlite", create=True)


def test_a_newer_schema_is_refused(tmp_path: Path):
    path = tmp_path / "database.sqlite"
    with open_project_database(path, create=True) as database, database.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO schema_migration "
                "(version, description, applied_at, applied_by_version) "
                "VALUES (:version, 'from the future', :applied_at, '99.0')"
            ),
            # Passed as text: sqlite3's datetime adapter is deprecated in 3.12.
            {"version": SCHEMA_VERSION + 1, "applied_at": datetime.now(UTC).isoformat()},
        )

    with pytest.raises(SchemaVersionError, match="newer"):
        open_project_database(path)


def test_current_schema_version_of_an_untouched_file_is_zero(tmp_path: Path):
    from sqlalchemy import create_engine

    path = tmp_path / "empty.sqlite"
    path.touch()
    engine = create_engine(f"sqlite:///{path}")
    try:
        assert current_schema_version(engine) == 0
    finally:
        engine.dispose()


def test_migration_versions_are_consecutive_from_one():
    versions = [migration.version for migration in MIGRATIONS]

    assert versions == list(range(1, len(MIGRATIONS) + 1))
    assert versions[-1] == SCHEMA_VERSION


def test_failed_transaction_rolls_back_every_statement(tmp_path: Path):
    path = tmp_path / "database.sqlite"
    now = datetime.now(UTC)

    with open_project_database(path, create=True) as database:
        with database.session() as session:
            session.add(ProjectSetting(key="existing", value="1", updated_at=now))

        with (
            pytest.raises(DatabaseError, match="transaction failed"),
            database.session() as session,
        ):
            session.add(ProjectSetting(key="new_row", value="2", updated_at=now))
            session.flush()
            # Duplicate primary key: fails the transaction, so "new_row" must not
            # survive either.
            session.add(ProjectSetting(key="existing", value="3", updated_at=now))
            session.flush()

        with database.session() as session:
            assert session.get(ProjectSetting, "new_row") is None
            assert session.get(ProjectSetting, "existing").value == "1"


def test_foreign_key_enforcement_is_enabled(tmp_path: Path):
    """Later phases rely on cascading deletes, which SQLite ignores by default."""
    with (
        open_project_database(tmp_path / "database.sqlite", create=True) as database,
        database.engine.connect() as connection,
    ):
        enabled = connection.execute(text("PRAGMA foreign_keys")).scalar()

    assert enabled == 1
