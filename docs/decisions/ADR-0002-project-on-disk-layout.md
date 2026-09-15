# ADR-0002: A project is a folder with a JSON manifest and a SQLite database

- **Status:** Accepted
- **Date:** 2026-09-15
- **Phase:** 0

## Context

Processing an examination takes hours or days and must survive being stopped,
resumed, backed up, moved between machines and audited afterwards. OMRFlow needs
a unit of work that holds scans, templates, recognised values, corrections,
candidate lists, keys and reports.

Options considered:

1. **A single container file** (like `.zip` or a single SQLite file holding
   image blobs).
2. **A folder with a manifest plus a database**, scans referenced in place.
3. **A central application database** holding all projects.

## Decision

A project is an ordinary folder:

```text
<Project Name>/
├── project.json          identity and metadata (discovery document)
├── database.sqlite       authoritative working data
├── templates/
├── scans_original/       never modified
├── scans_aligned/        derived, regenerable
├── answer_keys/
├── candidate_lists/
├── exports/
└── logs/
```

- `project.json` is the discovery document: it identifies the folder as a
  project and carries the metadata needed to list it without opening a database.
- `database.sqlite` is the authoritative store for working data.
- Paths recorded inside a project are **relative to the project root**.
- Original scans are referenced, not copied into the database.
- Missing sub-directories are recreated on open rather than treated as an error.

## Rationale

- **Transparency.** An examination office can see, back up and archive the work
  with ordinary tools. A support request can be answered by asking for
  `project.json` and a log, not a proprietary bundle.
- **Two documents, two jobs.** JSON is diffable, hand-inspectable and cheap to
  read for a recent-projects list; SQLite gives transactions, queries and
  integrity for the data that is actually processed. Using either alone means
  losing one of those properties.
- **Relative paths keep projects relocatable.** A project can be moved to an
  archive drive or synchronised to another machine without rewriting references.
- **Scans are referenced, not copied.** A batch may be several gigabytes.
  Duplicating it into a container would double the storage and the import time,
  and would make the copy diverge from the originals the institution archives.
- **Against a central database:** it makes every examination's data share one
  file, complicates backup and retention (different examinations have different
  retention rules), and creates a single point of loss.

### Two related choices

**SQLite journal mode is left at the default (rollback journal), not WAL.**
Projects are frequently kept in synchronised folders (OneDrive, network shares)
where WAL's extra `-wal`/`-shm` side files are a known source of sync conflicts
and corruption. OMRFlow is a single-writer application, so WAL's concurrency
benefit is small. Revisit only if profiling shows write throughput is a problem.

**`PRAGMA foreign_keys=ON` is set per connection.** SQLite disables foreign key
enforcement by default, and later phases depend on cascading relationships
(scan -> recognition result -> conflict).

## Consequences

**Positive**

- Backup, archival and transfer are folder operations.
- A stray `database.sqlite` is still identifiable: project identity is mirrored
  into the `project_setting` table.
- Deleting `scans_aligned/` frees space without losing anything unrecoverable.

**Negative**

- A project is not a single file, so it can be partially damaged - a folder can
  lose `project.json` while keeping the database, or vice versa. Opening
  validates both and refuses clearly rather than half-loading.
- Moving original scans after import will break references. Phase 5 must detect
  a missing source file and report it instead of failing silently.
- The user can edit `project.json` by hand. Validation is strict on load, and a
  malformed file is reported rather than silently defaulted.
