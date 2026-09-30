# v0.1.1-alpha.0 — implementation roadmap (superseded)

> **Superseded on 2026-09-30. Do not plan or implement from this file.**
>
> The single authoritative plan for `0.1.1-alpha.0` is
> [`development/releases/0.1.1-alpha.0/ROADMAP.md`](releases/0.1.1-alpha.0/ROADMAP.md),
> with its [architecture notes](releases/0.1.1-alpha.0/ARCHITECTURE_NOTES.md),
> [acceptance criteria](releases/0.1.1-alpha.0/ACCEPTANCE_CRITERIA.md) and
> [prompt pack](releases/0.1.1-alpha.0/prompts/).

This file held the proposal of 2026-09-29 (commit `073a95e`, written against
`main` at `e9dd813`). Its **single, growing "active batch"** model was not
adopted: the reconciled plan uses `Project → ScanSession → one or more finite
ScanBatch objects → sheets`, with session-level Attendance, reconciliation,
scoring, Results and Reports.

Carried into the reconciled plan: the baseline inspection, the six latent
`0.1.0-alpha.2` defects, canonical case-insensitive set identity and the
logical ↔ physical set-mark mapping, the intake ledger and stabilisation rules,
the duplicate/rescan table, pause/resume semantics, priorities, the acceptance
matrix, performance targets and migration discipline. What was kept and what
was not is listed in the reconciled `ROADMAP.md` §11.

The full original text is in git history: `git show 073a95e:development/ROADMAP_v0.1.1-alpha.0.md`.
