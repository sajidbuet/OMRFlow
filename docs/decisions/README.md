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

Naming: `ADR-NNNN-short-title.md`, numbered sequentially. Status is Proposed,
Accepted, Superseded (naming the replacement) or Deprecated. An accepted ADR is
not edited to change the decision - a new ADR supersedes it.
