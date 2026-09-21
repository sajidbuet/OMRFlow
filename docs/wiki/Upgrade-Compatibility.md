# Upgrade Compatibility

What OMRFlow promises about opening a project written by a different version.

## The two version numbers that matter

| | This release |
|---|---|
| **Project format version** — the shape of `project.json` | **2** |
| **Database schema version** — the shape of `database.sqlite` | **9** |

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

## What is *not* promised, before 1.0

- **No compatibility between prerelease versions.** An Alpha may change the
  schema. There is no supported path back.
- **No guarantee that a project created by one Alpha will open in the next.**
  It is expected to, via migration, and it is tested at the schema level —
  but an upgrade between two *released builds* has never been performed,
  because `0.1.0-alpha.1` is the first release.

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
