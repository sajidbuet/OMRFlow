# Project Format

## On disk

```text
My Exam\
├── project.json          identity and settings; format version 2
├── database.sqlite       everything about candidates and results; schema 9
├── logs\                 what OMRFlow did on this project
├── answer_keys\
├── candidate_lists\      imported attendance workbooks
└── exports\              generated report workbooks
```

A project is entirely self-contained apart from the **source scans**, which
are referenced by path and content hash rather than copied in — see
[Moving Projects](Moving-Projects).

## `project.json`

Holds the project's identity and settings: its stable project identifier, its
display name, its examination name, its description, and when it was created
and last modified. **Format version 2**; version 1 is read backward
compatibly.

It is written atomically, so an interrupted write cannot leave a truncated
file.

## `database.sqlite`

The examination itself: candidate rosters, imported scan provenance,
recognised results, conflicts and their resolutions, attendance
reconciliation, answer keys and their revisions, marks and ranks, and the
report-template bindings.

**Schema version 9.** Migrations are append-only and are applied
automatically when an older project is opened; a newer schema is refused. See
[Upgrade Compatibility](Upgrade-Compatibility).

SQLite is used in WAL mode with foreign keys enforced.

> The full table-by-table description is
> **[`docs/DATA_MODEL.md`](https://github.com/sajidbuet/OMRFlow/blob/main/docs/DATA_MODEL.md)**.
> The rationale for this layout is
> [ADR-0002](https://github.com/sajidbuet/OMRFlow/blob/main/docs/decisions/ADR-0002-project-on-disk-layout.md),
> and for the migration approach
> [ADR-0003](https://github.com/sajidbuet/OMRFlow/blob/main/docs/decisions/ADR-0003-schema-migrations.md).

## Confidentiality

**A project folder is examination material.** The database holds candidate
identifiers, recognised answers, attendance states and marks; the imported
workbooks hold names and roll numbers. Never attach any of it to a public
issue — see
[SUPPORT.md](https://github.com/sajidbuet/OMRFlow/blob/main/SUPPORT.md).

## One writer at a time

A project is locked while open. A lock left by a crash is **never** removed
automatically: you are shown what held it and asked to choose. See
[Projects](Projects#only-one-process-at-a-time).

## Related

- [Backup & Data Retention](Backup-and-Data-Retention)
- [Moving Projects](Moving-Projects)
- [Upgrade Compatibility](Upgrade-Compatibility)
