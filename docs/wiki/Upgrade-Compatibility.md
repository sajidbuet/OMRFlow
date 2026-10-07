# Upgrade Compatibility

What OMRFlow promises about opening a project written by a different version.

## The two version numbers that matter

| | Current source (`0.1.1` development line, unreleased) |
|---|---|
| **Project format version** — the shape of `project.json` | **3** |
| **Database schema version** — the shape of `database.sqlite` | **17** on `main` (since the revised phase 7 merge `9169933`; phases 8–10 add no migration) |

(This table previously read format 2 / schema 9, which no longer matched the
code; corrected 2026-10-01 from `project_format_version` in a project written
by current code and `SCHEMA_VERSION` in `database/migrations.py`. It then read
14 after phase 4 had moved `main` to 15; corrected 2026-10-02. It read 16 /
"17 on a branch" until revised phase 10; corrected 2026-10-07.)

## Upgrading to `0.1.1-alpha.0` — verified on the installed build (revised phase 10)

Recorded 2026-10-07 against the `0.1.1-alpha.0` candidate installer
(`OMRFlow-0.1.1-alpha.0-Setup-x64.exe`, commit `73e364b`), **installed**, not
from source. Evidence:
[`docs/release/validation/0.1.1-alpha.0-installed/`](../release/validation/0.1.1-alpha.0-installed/README.md).

| From | How the project was made | Result on the installed `0.1.1-alpha.0` |
|---|---|---|
| **`v0.1.0-alpha.2` (schema 9)** | by the `v0.1.0-alpha.2` code itself - its own release-validation workflow suite, run from the `v0.1.0-alpha.2` tag: real recognition of 5 sheets, a 6-candidate roster, reconciliation, a verified key, 6 results, one generated report - then opened and closed in the **installed `v0.1.0-alpha.2` release** (downloaded, checksum verified) | **PASS** - the release installed **in place over `v0.1.0-alpha.2`** (one uninstall entry, user data kept); migrations 10–17 ran on opening; pre-migration backup `before-migration-9-to-17`; every row and value the old build wrote unchanged; one backfilled session; integrity and Project Health clean; the stages show what `v0.1.0-alpha.2` showed (6 candidates, 5 scored, 1 absent, key verified) |
| **schema 12** (`0.1.0-alpha.2` + later work, `0ed96ed`) | the committed fixtures `tests/fixtures/schema12/unique_sets` and `colliding_sets`, written by that build | **PASS** - migrations 13–17, backup, every old value kept, backfill, integrity; `colliding_sets` keeps set `a` without a canonical code and Project Health names the collision, as designed |

Opening an older project in `0.1.1-alpha.0` is **forward-only**:

- The database is migrated in place (backed up first, under `backups/`).
- **`project.json` is also updated** - `0.1.1` records the project's active
  template in it - and the pre-migration backup holds the *database only*.
- The actual **`v0.1.0-alpha.2` then refuses the project**, writable and
  read-only, with *"The project file is not valid and could not be opened."*
  (its strict `project.json` reader rejects the new key before it reaches the
  schema check; checked with the `v0.1.0-alpha.2` tagged code on a project the
  installed `0.1.1-alpha.0` had opened). The *"created with a newer version"*
  message quoted under Schema 13 below is what the schema-12 build `0ed96ed`
  says, not the release.
- So restoring `backups/…before-migration-…sqlite3` alone does **not** make a
  project usable by `v0.1.0-alpha.2` again. **To keep the option of going
  back, copy the whole project folder before opening it in `0.1.1-alpha.0`.**

Installing `0.1.1-alpha.0` over `v0.1.0-alpha.2` leaves three runtime DLLs
the old build shipped and the new one does not (`libcrypto-3.dll`,
`libssl-3.dll`, `libffi-8.dll`) in the installation folder; `0.1.1-alpha.0`
does not load them, and its uninstaller removes them.

## Schema 17 — quality decisions and operator controls (`0.1.1` line, revised phase 7, branch)

Migration 17 adds one table (`scan_quality_decision`) and five columns: the
scan session's pinned scan-quality policy and its pause / stop intent, and a
per-source intake pause. It **writes no row and reinterprets nothing**: every
existing session reads "processing running, intake on, no quality policy
pinned yet" - exactly how an upgraded project behaved before. Quality decisions
for sheets read before the upgrade are derived from their stored results the
first time they are needed. A backup of the database is taken first. A
schema-17 project is refused by any earlier build with the usual "created with
a newer version of OMRFlow" message. Tested from a schema-16 project written by
the schema-16 build (`tests/fixtures/schema16`).

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

## No schema change — continuous processing (`0.1.1` line, revised phase 6)

The continuous-processing engine stores everything in schema-16 columns that
already existed: a sheet being read is `batch_scan.status = processing` (a
value that existed and is now written), and a unit's provenance is in its
`settings_json`. A project processed by it opens in any schema-16 build, whose
recovery on open returns unfinished `processing` sheets to `pending` exactly as
it does for an interrupted Scan batch. (Whether that build's Scan stage then
offers *Resume* for a particular unit depends on which batch it adopts - not
tested.)

## Schema 16 — intake sources and ledger (`0.1.1` line, revised phase 5)

Migration 16 adds three tables (`intake_source`, `intake_source_attachment`,
`intake_file`) and three empty provenance columns. It **writes no row** and
changes nothing an operator sees: existing scans stay exactly as they were, and
the built-in *manual import* source is created the first time *Process All*
runs afterwards. A backup of the database is taken first, as for every
migration. A schema-16 project is refused by any earlier build with the usual
"created with a newer version" message. Tested from schema-15 projects written
by the schema-15 build.

## Schema 15 — session scope (`0.1.1` line, revised phase 4)

Migration 15 records each scan session's downstream store and the session,
finality and close of each generated report; the first editable open binds
existing sessions to the state the previous build showed. Details:
[ADR-0007](../decisions/ADR-0007-session-effective-sheet-set.md).

## Crash-safe persistence (`0.1.1` line, phase 3) — still schema 14

No migration. Two things happen to an existing project the first time this
build opens it **for editing**:

- a batch an earlier build left *running* (the application was killed or
  crashed mid-run) is recovered: sheets in flight return to *pending*, and the
  review conflicts of every sheet it had already saved are re-derived from the
  stored results - an earlier build wrote them only when a whole run ended, so
  a crash left them missing. They are created once; no image is read again;
- in any batch that already has review conflicts, an unreadable sheet left
  without its conflict gets it.

Nothing else is rewritten. Batches registered by this build carry a
`review_state_with_results` entry in their stored settings; an earlier build
of this schema would ignore it.

## Schema 14 — scan sessions (`0.1.1` line, phase 2)

Opening an older project in a build of this line applies migration 14 (two new
tables, four columns on `scan_batch`), after the usual backup. Then, on that
first **writable** open, every existing batch is put in a **scan session**:

- each batch gets its own one-batch session, and is sealed (its list of scans
  is final; it can still be resumed and reviewed);
- two batches share one session only when joined by confirmed rescans in an
  unambiguous way; anything less clear is left separate and listed in Project
  Health;
- unrelated batches are **never** combined automatically. *Combine Into This
  Session* on the Scan stage does it, explicitly, if you want it.

Stored results, keys, attendance, reconciliation and reports are not changed.
Attendance, Results and Reports then read the newest batch of the active
session **by creation time** — previously "the most recently updated batch".
For almost every project that is the same batch; if a project had an older
batch retried after a newer one was processed, the newer one is now read.

A schema-13 build refuses a schema-14 project with *"This project was created
with a newer version of OMRFlow."* A read-only open in the new build does not
migrate, and shows each batch as its own session.

Tested: automated upgrade tests from four schema-13 projects written by the
schema-13 build (`tests/fixtures/schema13/`). Not yet performed: an upgrade
through an installed release build.

## Schema 13 — set identity (`0.1.1` line, phase A)

Opening a schema-12 project (written by `0.1.0-alpha.2` or the unreleased
work after it) in a `0.1.1` source build applies migration 13: two columns on
`project_set` and a unique index. A backup is taken first, as for every
migration. Nothing else changes — scans, review decisions, answer keys,
results and reports keep their stored values. If the project defines two sets
that differ only in case (`A` and `a`), both are kept and the conflict is
reported; see [Examination Sets](Examination-Sets).

**Going back is not possible.** Once migrated, the schema-12 build (`0ed96ed`)
refuses the project with *"This project was created with a newer version of
OMRFlow. Please update OMRFlow to open it."* This was checked by opening a
migrated project with the schema-12 code. **The released `v0.1.0-alpha.2`
refuses it differently** - *"The project file is not valid and could not be
opened."* - see *Upgrading to `0.1.1-alpha.0`* above (corrected 2026-10-07:
this paragraph used to attribute the schema-12 build's message to
`0.1.0-alpha.2`). Copy the whole project folder if you may need the older
build. A **read-only** open in the new build does not migrate, so a schema-12
project can be inspected without upgrading it.

Tested: automated upgrade tests from schema-12 projects written by the
schema-12 build itself (`tests/fixtures/schema12/`), and (revised phase 10)
the same fixtures opened in the **installed** `0.1.1-alpha.0` candidate.

## What is *not* promised, before 1.0

- **No compatibility between prerelease versions.** An Alpha may change the
  schema. There is no supported path back.
- **No guarantee that a project created by one Alpha will open in the next.**
  It is expected to, via migration, and it is tested at the schema level.
  `v0.1.0-alpha.2` → the `0.1.1-alpha.0` *candidate* installer has been
  performed on one machine with one synthetic project (above); the upgrade
  from `0.1.0-alpha.1` has not been exercised.

From `v1.0.0` onwards, opening a project created by any earlier 1.x release
becomes a promise rather than an expectation.

## Before upgrading anything you care about

1. Close OMRFlow.
2. Back up the **whole project folder** (copy it). **Project Health /
   Recovery…** snapshots the database safely, but the database alone is not
   enough to take a project back to `v0.1.0-alpha.2` after `0.1.1-alpha.0` has
   opened it (`project.json` changes too).
3. Upgrade.
4. Open the project and check a sample of results.

See [Upgrading OMRFlow](Upgrading-OMRFlow).

## Related

- [Upgrading OMRFlow](Upgrading-OMRFlow)
- [Backup & Data Retention](Backup-and-Data-Retention)
- [Project Format](Project-Format)
