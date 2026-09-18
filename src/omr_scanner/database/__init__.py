"""Persistence: SQLite schema, migrations and session management.

Purpose:
    Own everything about *how* project data is stored, so no other layer has to
    know SQL, SQLAlchemy or the schema version.

Responsibilities:
    * ORM table definitions (:mod:`omr_scanner.database.models`).
    * Engine/session lifecycle (:mod:`omr_scanner.database.engine`).
    * Schema creation and forward migration
      (:mod:`omr_scanner.database.migrations`).

What does NOT belong here:
    * Workflow logic. "Create a project" is a service operation that happens to
      use the database; it is not a database operation.
    * Domain rules. The database stores what the domain considers valid; it does
      not re-derive validity.
    * Any Qt or OpenCV import.

Invariants:
    * One SQLite file per project, always ``<project>/database.sqlite``.
    * Migrations are linear and forward-only; see
      ``docs/decisions/ADR-0003-schema-migrations.md``.
"""

from omr_scanner.database.engine import ProjectDatabase, open_project_database
from omr_scanner.database.migrations import SCHEMA_VERSION, current_schema_version
from omr_scanner.database.models import BatchScan, BatchStatus, ScanBatch, ScanJobStatus

__all__ = [
    "SCHEMA_VERSION",
    "BatchScan",
    "BatchStatus",
    "ProjectDatabase",
    "ScanBatch",
    "ScanJobStatus",
    "current_schema_version",
    "open_project_database",
]
