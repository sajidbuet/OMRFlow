# Architecture decision records

Short documents recording decisions that are expensive to reverse, and *why*
they were taken. The rationale is the point: it lets a later developer or agent
tell an accident from a deliberate constraint.

Write an ADR when a decision constrains future work, is likely to be questioned
later, or was chosen over a reasonable alternative. Do not write one for
ordinary coding choices.

| ADR | Decision |
|---|---|
| [ADR-0001](ADR-0001-desktop-python-pyside6.md) | Desktop application in Python with PySide6, not browser/Electron-first |
| [ADR-0002](ADR-0002-project-on-disk-layout.md) | A project is a folder with a JSON manifest and a SQLite database |
| [ADR-0003](ADR-0003-schema-migrations.md) | Hand-written forward-only migrations instead of Alembic |
| [ADR-0004](ADR-0004-normalized-template-coordinates.md) | Normalised template coordinates and a stored bubble pitch |
| [ADR-0005](ADR-0005-scan-sessions-and-finite-batches.md) | Scan sessions and finite batches |
| [ADR-0006](ADR-0006-crash-safe-scan-work-units.md) | Crash-safe Scan work units and restart semantics |
| [ADR-0007](ADR-0007-session-effective-sheet-set.md) | One canonical effective sheet set per scan session |
| [ADR-0008](ADR-0008-intake-copy-or-reference.md) | Watched intake ingests a verified, content-addressed copy; manual imports are read in place |
| [ADR-0009](ADR-0009-continuous-engine-single-writer.md) | The continuous engine: finite units, durable claims, one coordinator writing, `busy_timeout` as safety net |

Naming: `ADR-NNNN-short-title.md`, numbered sequentially. Status is Proposed,
Accepted, Superseded (naming the replacement) or Deprecated. An accepted ADR is
not edited to change the decision - a new ADR supersedes it.
