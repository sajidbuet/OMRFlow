# Implementation prompt pack — `0.1.1-alpha.0`

> **Written, not executed.** Instructions for later Claude Code sessions, one
> per phase, run in order on the current `main`. They implement
> [../ROADMAP.md](../ROADMAP.md); where a prompt and the roadmap disagree, the
> roadmap wins and the discrepancy is reported.

| # | File | Phase | Depends on |
|---|---|---|---|
| 01 | [01-set-identity.md](01-set-identity.md) | 0.1.1-A — Set identity (includes the version bump) | — |
| 02 | [02-scan-sessions.md](02-scan-sessions.md) | 0.1.1-B — Scan sessions and finite batches | 01 |
| 03 | [03-session-results.md](03-session-results.md) | 0.1.1-C — Session-level review, reconciliation, scoring and reporting | 02 |
| 04 | [04-intake-ledger.md](04-intake-ledger.md) | 0.1.1-D — Intake sources and ledger | 02 |
| 05 | [05-continuous-processing.md](05-continuous-processing.md) | 0.1.1-E — Continuous processing, quality decisions and session controls | 03, 04 |
| 06 | [06-operational-gui.md](06-operational-gui.md) | 0.1.1-F — Operational GUI | 05 |
| 07 | [07-qualification-release.md](07-qualification-release.md) | 0.1.1-G — Qualification and Alpha release | 01–06 |

03 and 04 may run in either order after 02. Never run two prompts at once on
the same branch.

How to use one: start a fresh session in the repository, create the branch the
prompt names from the current `main`, paste the whole file as the first
message, and let the session finish with its handoff.

## Rules every prompt repeats

Each prompt is self-contained on purpose. All of them require:

1. **Inspect before changing.** Run `git status`, `git log --oneline -10` and
   the full test suite first; record the baseline counts. The code is the fact,
   the plan is the intent: record every discrepancy in the handoff.
2. **Migrations.** Numbers 1–12 are taken. Take the next free number **at the
   time you write it** (13 is the first free one today); never renumber a merged
   migration; additive and forward-only (ADR-0003); guard `ALTER`s with
   `PRAGMA table_info`; upgrade test from a real schema-12 fixture created by
   current code.
3. **Preserve** Phase 5 batch semantics, Phase 6 conflict identity and
   idempotent sync, Reject & Rescan (`scan_rejection`, operator rejection,
   confirmed replacement, undo, re-import detection, purge), the Attendance
   reconciliation workstation, the Answer Key stage and answer-key provenance,
   scoring and reporting behaviour, Resolve behaviour, cross-batch rescan
   provenance, the Phase 10 harness and its assertions, the layering rules, the
   single-writer database, and the finite workflow with no new operator step.
4. **Answer ambiguity is never a conflict.** No new recognition or
   scan-quality thresholds.
4a. **Lifecycle and supersession invariants** (ROADMAP.md §2): a SEALED batch
   never gains members; a CLOSED session never gains batches or intake;
   reopening stales prior final outputs; only effective, non-superseded
   batch/sheet membership contributes to session-level results; nothing is
   deleted because it is superseded.
4b. **Crash-safe persistence and resume is required**: every unit of work
   commits atomically (ARCHITECTURE_NOTES.md §13.3); abnormal termination —
   not only a normal close — must preserve committed Scan and Resolve work;
   recovery never creates a new session or a superseding batch; progress is
   derived from committed rows. Prove it with **real process kills**
   (ACCEPTANCE_CRITERIA.md §5.4), never only with a simulated exception.
5. **Verification**:
   ```powershell
   .venv\Scripts\python.exe -m ruff check src tests tools scripts
   .venv\Scripts\python.exe -m mypy
   .venv\Scripts\python.exe -m pytest -q
   ```
6. **Status reporting** on the six tracks of ROADMAP.md §7, separately, using
   *implemented*, *tested*, *synthetically validated*, *network-share
   validated*, *real-scanner validated*, *production qualified* — never as
   synonyms. Passing tests alone never complete a phase.
7. **Documentation** per ROADMAP.md §10, including a new
   `development/releases/0.1.1-alpha.0/PHASE_<X>_HANDOFF.md`.
8. Small, reviewable commits; push the branch; **do not merge to `main`, tag or
   release unless the user asks.**
