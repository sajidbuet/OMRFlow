# ADR-0009: The continuous engine - finite units, durable claims, one coordinator writing

- **Status:** Accepted
- **Date:** 2026-10-02
- **Phase:** 0.1.1-alpha.0, revised phase 6 / roadmap Phase E (first part:
  unit scheduler, writer strategy, restart sequence)
- **Builds on:** ADR-0005 (sessions and finite batches), ADR-0006 (crash-safe
  work units), ADR-0007 (session effective set), ADR-0008 (intake copy or
  reference)
- **Schema:** unchanged (**16**). `batch_scan.status = 'processing'` existed
  and is now written; no migration.
- **Answers:** ARCHITECTURE_NOTES §11, §13.1 and §16 Q8; prompt 05 items 1-4
  and 10

## Context

Phase 5 left a durable intake ledger that stops at *ready* and a registration
API (`IntakeService.register`) that turns ready files of one source into one
sealed `ScanBatch`. Nothing processed those batches while files kept arriving,
and three questions had to be answered before anything could:

1. **What is a unit, and who decides when one forms?** The architecture is
   `Project -> ScanSession -> finite ScanBatch units -> sheets`; a batch must
   never become an indefinitely growing "live batch".
2. **Who writes the database?** In a live session intake, recording,
   conflict sync and an operator's Resolve decisions all write. SQLite is a
   single-writer store with a rollback journal (ADR-0002).
3. **What does "completed" mean when the process can die at any instant**, and
   how does a restart avoid both losing a sheet and reading a committed one
   again?

## Decision

### 1. Units

A unit is exactly a phase 5 registration: the oldest ready files of **one
source**, in the ledger's stable order `(ready_at, intake_file_id)`, at most
`UnitPolicy.max_unit_size` (default 200) - or whatever that source has ready
once its oldest file has waited `trickle_seconds` (default 30 s; both
configuration, unvalidated starting points). Sources are served in the order
of their oldest ready file. `domain.processing.plan_units` is the pure rule.
The batch is sealed at registration and never extended; new arrivals form new
units of the same session.

### 2. One coordinator writes; `busy_timeout` is the safety net

The engine (`services/continuous_engine.ContinuousEngine`) is used from **one
thread** - its coordinator - and that thread performs **every** processing
write: intake reconciliation and registration (phase 5's service, called from
there), claims, result commits, claim releases and unit finalisation. Each is
one short explicit transaction; none is held across a listing, a file read, a
copy or a recognition. **Worker processes never open the database** (X3,
asserted by `tests/unit/test_processing_rules.py`): they receive a ticket and a
path and return a `ScanResult`.

The other writer a live session has - an operator's Resolve decision on the GUI
thread, through its own connection - is not routed through a queue. It waits on
SQLite's lock for at most one of the engine's short transactions.
`PRAGMA busy_timeout = 5000` is now set explicitly on every connection
(`database.engine.BUSY_TIMEOUT_MS`) - the value Python's `sqlite3` already
applied implicitly - as the safety net. **Measured:** 192 Resolve decisions
made while the engine registered and recorded 40 sheets: no `database is
locked`, median 59 ms, worst 134 ms (`test_engine_contention.py`).

A writer-thread queue shared by all writers was considered and rejected for
this phase: it would make every Resolve decision asynchronous (the GUI could no
longer report "saved" when the call returns), split transaction ownership
across threads, and buy nothing the measurement showed was needed. Revisit if
a real share or a slow disk shows lock waits approaching the timeout.

The rollback journal (no WAL) stands, per ADR-0002.

### 3. Durable claims and the work unit

Per-sheet state lives in `batch_scan.status`:

```text
pending / cancelled -> processing             claim: one transaction (compare-and-set),
                                              which also marks the batch running
processing -> completed | warning | failed    the phase 3 work unit commits
processing -> pending                         claim released: withdrawn before it
                                              started, its worker process died, a
                                              non-draining stop, or restart recovery
```

* **Claim** (`claim_scans`): rows move `pending`/`cancelled -> processing` by
  compare-and-set, in batch order then `batch_index`, together with their
  batch's `running` status, in one transaction - *before* anything is
  submitted. An in-memory queue is never the only record of submitted work.
* **Commit**: `batch_store.record_results(..., template=..., claimed_only=True)`
  - the phase 3 work unit (result row + every conflict that result implies for
  that sheet, with their audit events) - written only onto rows still
  `processing`. A committed row can never be claimed again and a stray result
  can never overwrite one.
* **Acknowledge**: only after the commit returns does the engine count the
  sheet, drop it from its in-flight set and report it
  (`EngineHooks.committed` fires between the two, so a kill there is tested).
* **Batch-scope review state** (re-imports, session-wide duplicate Student IDs,
  undefined set codes) is completed by
  `scan_recovery.complete_batch_review_state` when a unit has nothing
  unfinished, **before** it leaves `running` - the ADR-0006 rule, unchanged.
  This is the incremental session-level conflict synchronisation: each unit's
  pass re-derives only the identifier groups its sheets belong to, against the
  session's effective set, so a later unit raises a duplicate on an earlier
  unit's sheet and a resolved conflict is never resurrected
  (`_apply_duplicate_groups` keeps human-touched records).
* A batch intake did not register (*Add Folder*, *Reprocess All*, an earlier
  build's) first gets the finite Scan worker's own registration step -
  `intake.record_manual_batch` and phase 4's `link_exact_duplicates` - so its
  exact duplicates are never read.

### 4. Restart

`ContinuousEngine.start()`:

1. `scan_recovery.recover_on_open` - stale `queued`/`processing` claims back to
   `pending` (retryable; never failed, never completed); every unit a kill left
   `running` gets its review state completed from stored results and leaves
   `running` (`interrupted` while work remains);
2. intake recovery (constructing `IntakeService`: unsettled rows re-observed,
   ready rows re-verified, unfinished duplicate links completed, `.part` copies
   removed);
3. processing resumes in the **same session and the same batches** - no
   session, batch or supersession is ever created by recovery.

Repeating it changes nothing.

### 5. Bounds

`EngineLimits.max_in_flight` bounds claimed-and-uncommitted sheets - which
bounds the pool's futures, the result objects and the writer backlog together,
because a result can only exist for a claimed sheet. Claims per transaction,
sheets per commit and units per pass are bounded too. Images never reach the
coordinator. Pending work grows in the database, not in memory.

### 6. Workers

`services/recognition_pool.ProcessRecogniser` keeps one `spawn` pool warm
across units, running the finite path's own worker entry points
(`parallel_batch.worker_initialise` / `worker_recognise`). A worker process
that dies breaks the executor; its sheets come back `worker_lost`, are released
and read again on a fresh pool once (`infrastructure_retries`), then recorded
as failures exactly as the finite path records them. Recycling drains then
replaces the pool (the finite path's safe pattern; `max_tasks_per_child` is
avoided for the reason `parallel_batch` documents). `InlineRecogniser` reads in
the calling thread (one worker; deterministic tests).

### 7. Stopping

Mechanical primitives only (operator policy is phase 7): `pause_intake`,
`pause_scheduling`, `cancel_queued`, `shutdown(drain=True|False, timeout)`.
After a clean `shutdown` every row of the session is committed
(`completed`/`warning`/`failed`), `pending` (not completed; processed next
time) or `duplicate`; nothing this engine claimed is left `processing`; every
unit it touched has left `running`. The exception is `FAULTED` (the writer kept
failing): claims are left for recovery, the only safe owner of a database that
cannot be written.

## Consequences

* "Completed" means durably committed, at every boundary - tested by
  deterministic fault injection at eight boundaries, by real process kills at
  six, and by 1 / 25 / 50 / 75 / 99 % interruption series.
* Batch-scope conflicts (cross-sheet) lag a sheet's own commit by at most one
  unit; recovery completes them if a kill lands in between (ADR-0006).
* The finite Scan stage is unchanged and remains a coordinator of its own. **The
  engine must not run while the Scan stage runs a batch of the same project**:
  its recovery returns every stale claim in the project to `pending`. Phase 8
  integrates the two; until then the engine is headless.
* The engine renames nothing; renamed copies stay an explicit export over the
  effective set (ARCHITECTURE_NOTES §11).
* A watched unit's result records phase 5's content-addressed copy name as its
  `source_name`; the original name is in the ledger (ADR-0008). Phase 8 should
  show the ledger's name.
* Not validated: a real network share, a real scanner, power loss, a fresh
  100,000-sheet run.
