# ADR-0003: Hand-written forward-only migrations instead of Alembic

- **Status:** Accepted
- **Date:** 2026-09-15
- **Phase:** 0

## Context

Every project carries its own SQLite database. A user will open a project created
by an older build of OMRFlow - probably long after the examination it belongs to -
and the schema must be brought up to date without losing data.

Options:

1. **Alembic**, the standard SQLAlchemy migration tool.
2. **A hand-written migration list** inside the application.
3. **`Base.metadata.create_all()` on open**, i.e. no migrations at all.

## Decision

A hand-written, forward-only migration list in
`omr_scanner/database/migrations.py`:

- `MIGRATIONS` is an ordered tuple of `Migration(version, description, apply)`
  with consecutive version numbers starting at 1;
- `SCHEMA_VERSION` is *derived* from that tuple and never edited by hand;
- opening a database applies every migration above its recorded version, each in
  its own transaction, recording a row in the `schema_migration` ledger;
- a database whose version exceeds `SCHEMA_VERSION` is refused with
  `SchemaVersionError`.

Option 3 is explicitly rejected: `create_all` adds missing tables but never
alters an existing one, so it silently produces a database that looks fine and
is subtly wrong.

## Rationale

- **The deployment model is different from a server's.** Alembic is built around
  one database that an operator migrates deliberately, with a revision graph,
  branches and downgrades. OMRFlow has thousands of small databases, each
  migrated automatically by whichever build opens it. The revision graph is
  unnecessary; linear versions are sufficient and easier to reason about.
- **No extra dependency, no extra CLI step.** Alembic would add a package, a
  configuration file, a migration directory and an environment script, plus the
  need to package all of it into a frozen Windows build.
- **SQLite's `ALTER TABLE` is limited anyway.** Non-trivial changes require the
  create-copy-swap dance regardless of the tool, so Alembic's autogeneration
  would need hand-editing in exactly the cases where it would have helped most.
- **Forward-only is the honest behaviour.** An older build cannot know how a
  newer one interpreted the data, so guessing a downgrade risks corrupting an
  examination record. Refusing to open is correct, and the user gets a clear
  "update OMRFlow" message.
- **The ledger is useful in itself.** One row per applied migration records how a
  project's database reached its current shape, and which application version
  wrote each step - valuable when diagnosing a project that has travelled
  between machines.

## Consequences

**Positive**

- No dependency, no separate tool to run, nothing extra to package.
- Migration code lives beside the models it changes and is reviewed with them.
- Applying migrations is part of opening a project, so a stale schema cannot be
  used by accident.

**Negative**

- Migrations are written by hand; there is no autogeneration from model diffs.
  Mitigated by the rule in `docs/DEVELOPMENT_GUIDE.md`: a model change without a
  migration is a bug, and every migration ships with a test that upgrades a
  database created at the previous version.
- No downgrade path. Accepted deliberately; users are directed to update the
  application instead.
- If the migration list ever becomes large or branched, revisit this decision.
  The ledger table is compatible with adopting Alembic later.

## Implementation note

Migration 1 uses `Base.metadata.create_all(connection)` to create the initial
schema. That is safe *as a migration* because it runs exactly once and is
recorded. Calling `create_all` at application start-up instead is the thing this
ADR forbids.
