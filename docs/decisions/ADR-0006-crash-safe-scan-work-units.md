# ADR-0006: Crash-safe Scan work units and restart semantics

- **Status:** Accepted
- **Date:** 2026-10-01
- **Phase:** 0.1.1-alpha.0, revised phase 3 (roadmap phase 0.1.1-B, crash-safety part)
- **Builds on:** ADR-0005 (ScanSession → finite ScanBatch); Phase 5 batch persistence
- **Schema:** none (stays at 14)

## Context

The invariant (ACCEPTANCE_CRITERIA X15/X16): *anything OMRFlow presents to the
operator as completed is already durably recoverable after an abnormal
termination*. Before this decision four required defects broke it
(ARCHITECTURE_NOTES §13.5):

| | Defect, as found in the code |
|---|---|
| **S1** | A sheet's own conflicts were written only when a whole run ended (`ScanPage._generate_conflicts` iterating the in-memory report). A run killed at sheet 9,000 left 9,000 committed results with **no** conflicts, and a resume synchronised only the sheets *it* read - so those conflicts were never created. Duplicate-ID, undefined-set and re-import passes had the same window. |
| **S2** | On reopen nothing re-adopted the interrupted batch; *Resume* reported "nothing to resume". |
| **S3** | A sheet was counted and drawn as done when it was *buffered* for a later group commit, and on a multi-core run when a worker returned it - before it was released in batch order, let alone committed. |
| **R1** | Resolve held no batch after a reopen until Scan navigated to it. |

## Decision

### 1. The durable Scan work unit

A sheet is **completed** when one transaction has committed

```text
batch_scan row: status, outcome, identifier, set code, result_json, attempt_count
+ every review_conflict that result implies for that sheet (with its DETECTED /
  RE_RECOGNISED / WITHDRAWN audit events)
```

`batch_store.record_results(..., template=...)` applies the outcome and calls
`review_store.sync_conflicts_in_session` **inside the same session**; any
failure rolls the whole unit back (the row stays `queued`). A sheet is never
saved as read while the review state its read implies is missing - the
"recognition committed, conflicts not yet generated" state cannot be produced by
this build. Callers with no Resolve stage (headless stress tool, benchmark)
omit the template and write recognition only, as before.

**Batch-scope review state** - re-import links, duplicate Student IDs,
undefined set codes - is a fact about the whole batch, not one sheet, so it
cannot be part of a sheet's unit. It is recomputed by
`scan_recovery.complete_batch_review_state` (idempotent) when a run ends **and
before the batch's status leaves `running`**. `running` at rest therefore means
precisely "batch-scope review state may be incomplete" - the durable marker the
alternative design would have needed a column for. *No migration was needed.*

### 2. Commit granularity: one sheet per commit whenever the writer keeps up

Measured (PHASE_C_HANDOFF.md, `scripts/benchmark_commit_granularity.py`): one
work unit costs ≈ 13-14 ms on the development machine's local SSD (rollback
journal, `synchronous=FULL`); strictly one sheet per commit cut end-to-end
throughput by 18-29 % at 4-8 workers. The Scan stage therefore uses
`BatchRecorder(commit_when_idle=True)`: a sheet is committed **as soon as it
arrives** unless less time has passed since the previous commit finished than
that commit took; only then does it join the next commit, still bounded by 25
sheets / 2 s. Arrivals slower than a commit are committed one by one; a backlog
coalesces, and throughput matches grouped commits. What a crash can lose is
only work that was never shown as done (§3).

### 3. What the operator is shown is committed state

`BatchRecorder.on_commit` fires after each successful commit with exactly the
sheets it made durable. `BatchWorker` counts a sheet - and emits `scan_done`,
which draws the row - only from there. A sheet a worker has read but the store
has not committed is **in flight** (`ProgressSnapshot.in_flight`, shown as
"N saving") and is never processed. If the store fails, the unsaved sheets are
handed to the page when the run ends (so they can still be exported) and the
completion line says "N not saved"; they are never counted.

### 4. Restart ordering

On every writable project open (`MainWindow._adopt_session`):

```text
open_project: migrate if needed → scan-session backfill (ADR-0005)
→ settle the project template
→ scan_recovery.recover_on_open, for every batch left `running`:
     1. queued/processing rows → pending      (retryable; never failed/completed)
     2. per-sheet review state re-derived from stored result_json
        (no image read; writes nothing where it is already complete)
     3. complete_batch_review_state
     4. status: interrupted while work remains, else the terminal status
        the rows imply - only now does the batch leave `running`
   then, for any other batch with review state: a failed sheet with a stored
   result and no conflict (a failed read always raises exactly one) is
   re-derived
→ every page adopts the project
→ restore: Scan adopts the active session's newest batch if it has unfinished
  members (counts from one grouped query); Resolve loads the downstream batch
  (or that batch)
```

A crash *during* recovery leaves the batch `running`, so the next open starts
from the same point. Step 2 makes data written by a pre-phase-3 build (results
without conflicts) whole, exactly once. Nothing in this sequence recognises a
sheet or starts a run: *Resume* stays the operator's command. Process All on a
batch restored this way processes only members without a committed result.

The window's own stop-and-exit path (`ScanPage.shutdown_batch`) follows the
same order: batch-scope review state, then `cancelled`.

### 5. Recovery never changes lifecycle state

A crash is not a lifecycle event. Recovery never creates a session, a batch or
a supersession; never seals, closes or reopens; never moves a batch between
sessions. An OPEN batch stays OPEN; a SEALED batch stays SEALED, gains no
members, and its unfinished members resume (membership and processing are
orthogonal, ADR-0005). An OPEN session stays OPEN and a CLOSED one CLOSED.
Recovery writes **no processing manifest**: the interrupted run did not finish;
the next real run boundary writes one as usual.

### 6. Resolve

Each decision is already one transaction with its audit event (Phase 6). On
reopen Resolve is loaded directly; its counts and queue come from
`count_conflicts` / `list_conflicts` over persisted rows. The in-memory redo
list is not persisted (navigation state, ARCHITECTURE_NOTES R3).

### 7. Idempotence

Every repair is a function of persisted rows: re-running recovery writes
nothing. An idempotence defect found while building this was fixed:
`sync_conflicts` compared a state read from SQLite with `is`, so every re-sync
of a sheet whose conflict had been withdrawn appended another `WITHDRAWN`
event.

### 8. Observation points for crash tests

`services.run_hooks.RunHooks`, injected into `ScanPage.run_hooks` (``None`` in
the application), observes `submitted`, `started`, `committed`,
`run_recognised` and `review_state_completed`. The real-process harness uses
it to log submissions externally and to *pause* at an exact boundary before
killing the process from outside. No production behaviour depends on it.

## Consequences

* A sheet shown as processed has its result and its own conflicts committed;
  batch-scope conflicts (duplicate IDs) for a batch that a crash interrupted
  exist once the project has been reopened, before any page shows it.
* Opening a project after a crash costs one decode of every stored result of
  the interrupted batch (bounded windows of 500).
* Project Health reports the states this design rules out: `BATCH_LEFT_RUNNING`,
  `SCAN_COMPLETED_WITHOUT_RESULT`, `SCAN_REVIEW_STATE_MISSING`,
  `REVIEW_STATE_WITHOUT_RECOGNITION`, `CONFLICT_DETECTED_TWICE`.

## Power loss - what is and is not claimed

Process kills (`TerminateProcess`) were tested; **physical power removal was
not**. Durability across power loss is *inferred* from SQLite's transaction
guarantees under the settings OMRFlow uses - rollback journal (`DELETE`),
`synchronous=FULL` (SQLite's default), foreign keys on - and holds only as far
as the storage honours flushes. A synchronised folder or network share weakens
it (ADR-0002); SMB is Phase 10's qualification.

## Rejected alternatives

* **A durable "conflicts synced" marker column (migration 15).** Would let
  recovery skip complete sheets, but needs a schema change for a state this
  build cannot produce; the batch status already marks the only window
  (batch-scope state) that remains.
* **Strict one-sheet-per-transaction.** Measured 18-29 % slower; the adaptive
  policy gives the same per-sheet durability whenever the writer keeps up.
* **Starting Resume automatically on open.** Recovery repairs state; reading
  sheets is the operator's decision.
