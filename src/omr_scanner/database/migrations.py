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

A consequence of ``create_all`` worth knowing before you write one:
    Migrations 2-8 build their tables with ``Base.metadata.create_all``, which
    reflects **today's** models rather than the models as they stood when that
    migration was written. So a *column added later* to one of those tables
    appears in a brand-new database the moment its creating migration runs -
    ``candidate_roster.set_id``, added by migration 9, is present from
    migration 4 in any database created now - while a database written by an
    older build gains it from migration 9's ``ALTER TABLE``.

    Both routes arrive at the same schema, which is why every ``ALTER`` here
    is guarded by a ``PRAGMA table_info`` check instead of assuming the column
    is absent. It also means a historical schema cannot be reproduced by
    running the migration list with a cap: a test that needs a genuinely
    old-shaped table must write that table's DDL out (see
    ``tests/integration/test_project_sets.py::TestUpgradingFromSchemaEight``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, insert, inspect, select, text

from omr_scanner import __version__
from omr_scanner.database.models import (
    AnswerKeyRevision,
    AuditEvent,
    Base,
    BatchScan,
    BatchScanHistory,
    BatchSupersession,
    CandidateResult,
    CandidateRoster,
    GeneratedReport,
    ProcessingManifest,
    ProjectSet,
    ProjectSetting,
    ReconciliationDecision,
    ReconciliationEntryRow,
    ReconciliationRun,
    ReconciliationScript,
    RegisteredCandidate,
    ReportLayoutConfig,
    ReportTemplateAssociation,
    ReviewConflict,
    ScanBatch,
    ScanRejection,
    ScanSession,
    SchemaMigration,
    ScoringPolicyRevision,
)
from omr_scanner.domain.set_identity import canonical_code
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


def _migration_005_scoring(connection: Connection) -> None:
    """Add Phase 8 answer keys, scoring policy and results.

    Purely additive. A project scored before this version does not exist - this
    is the first phase that produces marks - so nothing needs converting, and
    every table Phases 0-7 rely on is untouched.
    """
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[AnswerKeyRevision.__tablename__],
            Base.metadata.tables[ScoringPolicyRevision.__tablename__],
            Base.metadata.tables[CandidateResult.__tablename__],
        ],
    )


def _migration_006_reporting(connection: Connection) -> None:
    """Add Phase 9 report templates, layout configuration and generation audit.

    Purely additive, following migration 5's own precedent: a project scored
    before this version has never been reported on, so nothing needs
    converting, and every table Phases 1-8 rely on is untouched.
    """
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[ReportTemplateAssociation.__tablename__],
            Base.metadata.tables[ReportLayoutConfig.__tablename__],
            Base.metadata.tables[GeneratedReport.__tablename__],
        ],
    )


def _migration_007_production_hardening(connection: Connection) -> None:
    """Add Phase 10 provenance and reprocessing history.

    Three parts, all additive.

    **``batch_scan`` gains ``content_sha256``/``content_hash_algorithm``.**
    Existing rows are left at the column defaults (``''``/``'sha256'``) rather
    than backfilled: hashing a scan already on disk means reading it again,
    and a batch processed before this version can simply be re-hashed the next
    time it is opened for reprocessing or a health check, rather than paying
    that cost for every project the moment it upgrades.

    **``batch_scan_history`` and its immutability triggers** are new -
    nothing before this version ever reprocessed a completed sheet, so there
    is no existing data to migrate into it.

    **``processing_manifest``** is new for the same reason: no version before
    this one assembled one.
    """
    existing = {
        row[1] for row in connection.execute(text("PRAGMA table_info(batch_scan)")).all()
    }
    if "content_sha256" not in existing:
        connection.execute(
            text("ALTER TABLE batch_scan ADD COLUMN content_sha256 VARCHAR(64) NOT NULL DEFAULT ''")
        )
    if "content_hash_algorithm" not in existing:
        connection.execute(
            text(
                "ALTER TABLE batch_scan ADD COLUMN content_hash_algorithm "
                "VARCHAR(20) NOT NULL DEFAULT 'sha256'"
            )
        )

    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[BatchScanHistory.__tablename__],
            Base.metadata.tables[ProcessingManifest.__tablename__],
        ],
    )
    for statement in (
        """
        CREATE TRIGGER IF NOT EXISTS batch_scan_history_is_append_only_update
        BEFORE UPDATE ON batch_scan_history
        BEGIN
            SELECT RAISE(ABORT, 'batch_scan_history is append-only: rows cannot be updated');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS batch_scan_history_is_append_only_delete
        BEFORE DELETE ON batch_scan_history
        BEGIN
            SELECT RAISE(ABORT, 'batch_scan_history is append-only: rows cannot be deleted');
        END
        """,
    ):
        connection.execute(text(statement))


def _migration_008_project_sets(connection: Connection) -> None:
    """Add the project's own registry of examination sets: ``project_set``.

    Purely additive, and deliberately **not** backfilled.

    A project created before this version has no list of sets anywhere to
    convert from. The set codes it does contain live on rows that describe
    something that *happened* - an answer key that was entered
    (``answer_key_revision.set_code``), a report that was generated
    (``generated_report.set_code``), a sheet whose printed set code
    recognition read (``batch_scan.set_code_value``) - and inventing registry
    entries from them would put the application in the position of asserting,
    with no evidence and no description to show, which sets an examination
    was *intended* to have. A project that has processed papers for sets 10
    and 11 might have three more that were never scanned.

    The honest behaviour, and the one implemented here, is that an upgraded
    project simply starts with an empty set list that the operator fills in
    from *Project Configuration*, while every existing answer key, report and
    scan keeps working exactly as before - none of them consult this table.
    :func:`omr_scanner.services.project_sets.suggest_sets_from_existing_data`
    exists so the interface can *offer* the codes already present as a
    starting point, which is a suggestion the operator accepts or rejects,
    not a silent migration.
    """
    Base.metadata.create_all(
        connection,
        tables=[Base.metadata.tables[ProjectSet.__tablename__]],
    )


def _migration_009_per_set_attendance(connection: Connection) -> None:
    """Scope attendance to a set, and link a result template to one.

    Two additive columns, and **no backfill**, which is the whole point.

    ``candidate_roster.set_id`` is what makes attendance per-set: a candidate
    is only ever reached through a roster, so a roster that knows its set makes
    every candidate, every reconciliation entry and every stored mark belong to
    that set transitively - without touching
    :class:`~omr_scanner.database.models.RegisteredCandidate`,
    :class:`~omr_scanner.database.models.ReconciliationEntryRow` or
    :class:`~omr_scanner.database.models.CandidateResult`, whose existing
    ``roster_id`` foreign keys already carry the isolation.

    ``report_template_association.set_id`` and ``source_kind`` record which
    defined set a result template belongs to and whether it arrived as that
    set's attendance workbook.

    **An existing roster keeps ``set_id`` NULL.** A project processed before
    this version has exactly one roster for the whole examination, and nothing
    in the data says which of several later-defined sets it was meant to be.
    Guessing would silently attach one set's candidate list to another set's
    report - the precise failure this phase exists to prevent - so the roster
    is left unassigned, reported as such, and assigned only by an operator.
    The same reasoning applies to a template association that predates the set
    registry: its ``set_code`` still resolves it for every existing Phase 9
    path, and ``set_id`` is filled in when a set with that code is known.
    """
    existing = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(candidate_roster)")).all()
    }
    if "set_id" not in existing:
        connection.execute(
            text("ALTER TABLE candidate_roster ADD COLUMN set_id VARCHAR(32) NULL "
                 "REFERENCES project_set(set_id)")
        )
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_candidate_roster_set_active "
            "ON candidate_roster (set_id, is_active)"
        )
    )

    association = {
        row[1]
        for row in connection.execute(
            text("PRAGMA table_info(report_template_association)")
        ).all()
    }
    if "set_id" not in association:
        connection.execute(
            text("ALTER TABLE report_template_association ADD COLUMN set_id VARCHAR(32) "
                 "NULL REFERENCES project_set(set_id)")
        )
    if "source_kind" not in association:
        connection.execute(
            text(
                "ALTER TABLE report_template_association ADD COLUMN source_kind "
                "VARCHAR(20) NOT NULL DEFAULT 'manual'"
            )
        )


def _migration_010_roster_source_path(connection: Connection) -> None:
    """Record the full path an attendance file was imported from.

    One additive column, empty for every existing roster: a project imported
    before this version recorded the file's *name* only, and nothing can
    recover which folder it came from. Two files called ``attendance.xlsx`` in
    different folders were therefore indistinguishable once imported - the
    table showed the same name before and after a replacement.
    """
    existing = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(candidate_roster)")).all()
    }
    if "source_path" not in existing:
        connection.execute(
            text(
                "ALTER TABLE candidate_roster ADD COLUMN source_path TEXT NOT NULL "
                "DEFAULT ''"
            )
        )


def _migration_011_reject_and_rescan(connection: Connection) -> None:
    """Add Reject & Rescan: ``scan_rejection``.

    Purely additive, and nothing to backfill: no build before this one could
    reject a scan, so every existing scan is - correctly - active, which is
    exactly what the *absence* of a row here means. No existing table is
    altered; ``batch_scan``, the review ledger and every result keep their
    rows and their meaning.

    Built with ``create_all`` on a named table, like migrations 2-8, so a
    re-run after a failed attempt that rolled back is harmless.
    """
    Base.metadata.create_all(
        connection,
        tables=[Base.metadata.tables[ScanRejection.__tablename__]],
    )


ANSWER_KEY_PROVENANCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("created_by", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("template_id", "VARCHAR(100) NOT NULL DEFAULT ''"),
    ("template_name", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("template_fingerprint", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("source_sha256", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("source_metadata_json", "TEXT NOT NULL DEFAULT ''"),
)


def _migration_012_answer_key_provenance(connection: Connection) -> None:
    """Record who created an answer-key revision, against which template, from what.

    Six additive columns on ``answer_key_revision`` and **no backfill**. A
    revision stored by an earlier build recorded none of this, and inventing
    a creator or a template for it would put provenance on a key that has
    none; the interface says "not recorded" instead. Every existing revision
    keeps its answers, status, verification and the results that point at it.
    """
    existing = {
        row[1]
        for row in connection.execute(text("PRAGMA table_info(answer_key_revision)")).all()
    }
    for name, ddl in ANSWER_KEY_PROVENANCE_COLUMNS:
        if name not in existing:
            connection.execute(
                text(f"ALTER TABLE answer_key_revision ADD COLUMN {name} {ddl}")
            )


def _migration_013_set_identity(connection: Connection) -> None:
    """Give every examination set a canonical identity and an optional printed mark.

    Two additive columns on ``project_set`` and one partial unique index:

    * ``canonical_code`` - the set's code in canonical form
      (:func:`omr_scanner.domain.set_identity.canonical_code`: NFKC, trimmed,
      upper case), unique where not NULL.
    * ``physical_mark`` - what the sheet prints for the set when that is not
      its code; ``''`` (the sheet prints the code) for every existing set.

    **Why filling ``canonical_code`` belongs in the migration**, against this
    module's "structure, not data" rule: it is not examination data but a
    deterministic function of a column already in the same row - index
    metadata, in effect - and the unique index it backs is meaningless until it
    is filled. The table holds tens of rows, so there is no progress to report.

    **Collisions are kept, never merged.** A project created before canonical
    identity may define both ``A`` and ``a``. The first of such a group in the
    operator's order (``display_order``, then ``code``, the order
    :func:`omr_scanner.services.project_sets.list_sets` lists them in) receives
    the canonical code; every later one keeps ``NULL``. No row is deleted or
    renamed, and nothing that names a set code elsewhere -
    ``batch_scan.set_code_value``, answer-key revisions, results, generated
    reports, rejection records - is rewritten. The collision is reported by
    Project Health and Project Configuration, and the set-dependent stages
    refuse to proceed until the operator renames or removes one of the sets.
    """
    existing = {
        row[1] for row in connection.execute(text("PRAGMA table_info(project_set)")).all()
    }
    if "canonical_code" not in existing:
        connection.execute(
            text("ALTER TABLE project_set ADD COLUMN canonical_code VARCHAR(32) NULL")
        )
    if "physical_mark" not in existing:
        connection.execute(
            text(
                "ALTER TABLE project_set ADD COLUMN physical_mark VARCHAR(32) "
                "NOT NULL DEFAULT ''"
            )
        )

    rows = connection.execute(
        text(
            "SELECT set_id, code, canonical_code FROM project_set "
            "ORDER BY display_order, code, set_id"
        )
    ).all()
    taken = {str(row[2]) for row in rows if row[2] is not None}
    for set_id, code, current in rows:
        if current is not None:
            continue
        wanted = canonical_code(str(code))
        if wanted in taken:
            logger.warning(
                "Set %r collides with another set's canonical code %r; it is kept "
                "without a canonical code until an operator renames it",
                code,
                wanted,
            )
            continue
        taken.add(wanted)
        connection.execute(
            text("UPDATE project_set SET canonical_code = :wanted WHERE set_id = :set_id"),
            {"wanted": wanted, "set_id": set_id},
        )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_project_set_canonical_code "
            "ON project_set (canonical_code) WHERE canonical_code IS NOT NULL"
        )
    )


SCAN_BATCH_LIFECYCLE_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "scan_session_id",
        "VARCHAR(32) NULL REFERENCES scan_session(scan_session_id) ON DELETE RESTRICT",
    ),
    ("sealed_at", "DATETIME NULL"),
    ("sealed_by", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("role", "VARCHAR(20) NOT NULL DEFAULT 'scan'"),
)


def _migration_014_scan_sessions(connection: Connection) -> None:
    """Add scan sessions, finite-batch membership and batch supersession.

    Structure only (ADR-0005):

    * ``scan_session`` - the examination-level container, with its pinned
      template identity and close / reopen record;
    * ``batch_supersession`` - one batch replacing another, with reason, actor,
      time and an audited reversal;
    * on ``scan_batch``: ``scan_session_id`` (nullable at the database level),
      ``sealed_at`` / ``sealed_by`` (membership: NULL = open) and ``role``
      (``scan`` for every existing row until the backfill re-labels it), each
      guarded by ``PRAGMA table_info``, plus an index by session.

    **No data is moved here.** Assigning existing batches to sessions is the
    upgrade *backfill*
    (:func:`omr_scanner.services.scan_sessions.backfill_legacy_batches`), run on
    the first writable open after this migration and audited, because grouping
    batches is a decision about examination data, not structure.
    """
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[ScanSession.__tablename__],
            Base.metadata.tables[BatchSupersession.__tablename__],
        ],
    )
    existing = {
        row[1] for row in connection.execute(text("PRAGMA table_info(scan_batch)")).all()
    }
    for name, ddl in SCAN_BATCH_LIFECYCLE_COLUMNS:
        if name not in existing:
            connection.execute(text(f"ALTER TABLE scan_batch ADD COLUMN {name} {ddl}"))
    connection.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_scan_batch_session "
            "ON scan_batch (scan_session_id, created_at)"
        )
    )


SESSION_SCOPE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "scan_session",
        "downstream_batch_id",
        "VARCHAR(32) NULL REFERENCES scan_batch(batch_id) ON DELETE SET NULL",
    ),
    ("generated_report", "scan_session_id", "VARCHAR(32) NULL"),
    ("generated_report", "is_final", "BOOLEAN NOT NULL DEFAULT 0"),
    ("generated_report", "session_closed_at", "DATETIME NULL"),
)

SESSION_SCOPE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_batch_scan_identifier ON batch_scan (identifier_value)",
    "CREATE INDEX IF NOT EXISTS ix_batch_scan_content ON batch_scan (content_sha256)",
    "CREATE INDEX IF NOT EXISTS ix_review_conflict_type_value "
    "ON review_conflict (conflict_type, machine_value)",
    "CREATE INDEX IF NOT EXISTS ix_generated_report_session "
    "ON generated_report (scan_session_id, generated_at)",
)


def _migration_015_session_scope(connection: Connection) -> None:
    """Persist a scan session's downstream scope and its final outputs' provenance.

    Structure only (ADR-0007, revised):

    * ``scan_session.downstream_batch_id`` - the session's **bound downstream
      store**: the one ``batch_id`` value its reconciliation and results rows
      are keyed by. Recorded on the session, so the store can never move
      because batches are added, combined or superseded; ``NULL`` until the
      session first holds downstream state (bound by
      :func:`omr_scanner.services.session_population.bind_downstream_stores`
      on the first writable open, and by the first reconciliation or scoring).
    * ``generated_report.scan_session_id`` / ``is_final`` /
      ``session_closed_at`` - which session an output was generated from,
      whether it was a final export, and the close it came from. A final
      output is current only while its session is still closed by that same
      close (reopening and re-closing produce a different ``closed_at``).
    * indexes for the bounded paths: ``batch_scan.identifier_value`` (the
      duplicate-ID groups a correction touches), ``batch_scan.content_sha256``
      (exact-content duplicates at registration) and
      ``review_conflict(conflict_type, machine_value)`` (a group's records).

    Every column is additive and nullable or defaulted; nothing is rebuilt.
    """
    for table, name, ddl in SESSION_SCOPE_COLUMNS:
        existing = {
            row[1] for row in connection.execute(text(f"PRAGMA table_info({table})")).all()
        }
        if name not in existing:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
    for statement in SESSION_SCOPE_INDEXES:
        connection.execute(text(statement))


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
    Migration(
        version=5,
        description=(
            "Phase 8 scoring: answer_key_revision, scoring_policy_revision, "
            "candidate_result"
        ),
        apply=_migration_005_scoring,
    ),
    Migration(
        version=6,
        description=(
            "Phase 9 reporting: report_template_association, "
            "report_layout_config, generated_report"
        ),
        apply=_migration_006_reporting,
    ),
    Migration(
        version=7,
        description=(
            "Phase 10 production hardening: batch_scan content-hash columns, "
            "batch_scan_history, processing_manifest"
        ),
        apply=_migration_007_production_hardening,
    ),
    Migration(
        version=8,
        description="Project-level examination sets: project_set",
        apply=_migration_008_project_sets,
    ),
    Migration(
        version=9,
        description=(
            "Per-set attendance and templates: candidate_roster.set_id, "
            "report_template_association.set_id/source_kind"
        ),
        apply=_migration_009_per_set_attendance,
    ),
    Migration(
        version=10,
        description="Attendance file provenance: candidate_roster.source_path",
        apply=_migration_010_roster_source_path,
    ),
    Migration(
        version=11,
        description="Reject & Rescan: scan_rejection",
        apply=_migration_011_reject_and_rescan,
    ),
    Migration(
        version=12,
        description=(
            "Answer-key provenance: answer_key_revision.created_by, template "
            "identity, source hash and metadata"
        ),
        apply=_migration_012_answer_key_provenance,
    ),
    Migration(
        version=13,
        description=(
            "Set identity: project_set.canonical_code (unique where not NULL), "
            "project_set.physical_mark"
        ),
        apply=_migration_013_set_identity,
    ),
    Migration(
        version=14,
        description=(
            "Scan sessions: session and batch-supersession tables; batch "
            "session link, seal and role columns"
        ),
        apply=_migration_014_scan_sessions,
    ),
    Migration(
        version=15,
        description=(
            "Session scope: the session's recorded downstream store; generated "
            "reports' session, final flag and close; lookup indexes"
        ),
        apply=_migration_015_session_scope,
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
