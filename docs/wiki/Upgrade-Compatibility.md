# Upgrade Compatibility

What OMRFlow promises about opening a project written by a different version.

## The two version numbers that matter

| | Current source (`0.1.1` development line, unreleased) |
|---|---|
| **Project format version** — the shape of `project.json` | **3** |
| **Database schema version** — the shape of `database.sqlite` | **13** |

(This table previously read format 2 / schema 9, which no longer matched the
code; corrected 2026-10-01 from `project_format_version` in a project written
by current code and `SCHEMA_VERSION` in `database/migrations.py`.)

Both appear in the diagnostic bundle (**Application menu → Tools → Create
Diagnostic Bundle…**). The schema a build *expects* is recorded as
`expected_schema_version`.

## The rules

**Older project, newer OMRFlow — migrated forward.**
Migrations are applied automatically and **in place**. They cannot be
reversed. Back up first.

**Newer project, older OMRFlow — refused.**
OMRFlow will not open a project written by a schema it does not understand.
It says so, naming both versions. It does not open it read-only and does not
attempt a downgrade — either would risk writing data the older build
misunderstands.

**Same version — always opens.**
Migrations are append-only: a released migration is never edited afterwards,
so two builds reporting the same schema version always agree about the shape
of the data.

**A migration that cannot complete stops.**
It fails with a message and leaves the project as it was, rather than
half-applying.

## Schema 13 — set identity (`0.1.1` line, phase A)

Opening a schema-12 project (written by `0.1.0-alpha.2` or the unreleased
work after it) in a `0.1.1` source build applies migration 13: two columns on
`project_set` and a unique index. A backup is taken first, as for every
migration. Nothing else changes — scans, review decisions, answer keys,
results and reports keep their stored values. If the project defines two sets
that differ only in case (`A` and `a`), both are kept and the conflict is
reported; see [Examination Sets](Examination-Sets).

**Going back is not possible.** Once migrated, a `0.1.0-alpha.2` build refuses
the project with *"This project was created with a newer version of OMRFlow.
Please update OMRFlow to open it."* This was checked by opening a migrated
project with the schema-12 code (`0ed96ed`); keep the pre-migration backup if
you may need the older build. A **read-only** open in the new build does not
migrate, so a schema-12 project can be inspected without upgrading it.

Tested: automated upgrade tests from schema-12 projects written by the
schema-12 build itself (`tests/fixtures/schema12/`). Not yet performed: an
upgrade through an installed release build.

## What is *not* promised, before 1.0

- **No compatibility between prerelease versions.** An Alpha may change the
  schema. There is no supported path back.
- **No guarantee that a project created by one Alpha will open in the next.**
  It is expected to, via migration, and it is tested at the schema level —
  but an upgrade between two *released builds* has never been performed.
  `0.1.0-alpha.2` is the first release with a predecessor, and the upgrade
  from `0.1.0-alpha.1` has not been exercised.

From `v1.0.0` onwards, opening a project created by any earlier 1.x release
becomes a promise rather than an expectation.

## Before upgrading anything you care about

1. Close OMRFlow.
2. Back up the project folder — or use **Project Health / Recovery…**, which
   snapshots the database safely.
3. Upgrade.
4. Open the project and check a sample of results.

See [Upgrading OMRFlow](Upgrading-OMRFlow).

## Related

- [Upgrading OMRFlow](Upgrading-OMRFlow)
- [Backup & Data Retention](Backup-and-Data-Retention)
- [Project Format](Project-Format)
