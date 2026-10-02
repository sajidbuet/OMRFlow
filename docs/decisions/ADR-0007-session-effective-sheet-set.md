# ADR-0007: The scan session's effective sheet set

- **Status:** Accepted
- **Date:** 2026-10-02
- **Phase:** 0.1.1-alpha.0, phase 4 (session-wide effective sheet set and downstream processing)
- **Builds on:** ADR-0005 (ScanSession → finite ScanBatch), ADR-0006 (crash-safe work units),
  the Reject & Rescan lifecycle (`scan_rejection`, migration 11)
- **Schema:** none (stays at 14; migration 15 not used)

## Context

Phase 2 grouped batches into scan sessions, but every downstream stage still
read **one batch**: `scan_sessions.downstream_batch_id` picked a batch, and
Attendance, scoring, Results, Reports, duplicate-ID detection and the Resolve
queue saw only that batch's sheets (plus rescans "adopted" into it). A session
read in three batches was therefore three populations. A Student ID read in
batch 1 and again in batch 3 was never a duplicate, and candidates whose script
was in a later batch showed as missing. Each module also filtered its own
"active sheets", in slightly different ways.

## Decision

### 1. One resolver

`services/session_population.py` is the only place that decides which recorded
sheets count for a scan session. `population(database, batch_id)` accepts any
batch of the session and returns a `SessionPopulation`: every sheet of the
session's batches with exactly one `SheetDisposition`, decided by the pure
`domain/session_population.classify` in this order:

| Order | Fact | Disposition | Counts | Listed |
|---|---|---|---|---|
| 1 | its batch is replaced by a live batch supersession (Reprocess All) | `BATCH_SUPERSEDED` | no | no |
| 2 | its rescan lineage's root belongs to another session | `COUNTED_IN_OTHER_SESSION` | no | no |
| 3 | `scan_rejection.state` is not active | `SUPERSEDED_BY_REPLACEMENT`, `REJECTED_PENDING_RESCAN`, `REIMPORT_OF_REJECTED`, `EXCLUDED`, `DEFERRED` (an unknown state is treated as pending) | no | pending and deferred only |
| 4 | not read (`pending`, `queued`, `processing`, `cancelled`) | `NOT_READ` | no | no |
| 5 | read but failed | `EFFECTIVE_UNREADABLE` | yes | — |
| 6 | otherwise | `EFFECTIVE` | yes | — |

"Counts" is the **effective set**. "Listed" sheets appear against their
candidate (*rescan required*, *deferred*) but are never scored. No new
lifecycle state was added; the dispositions reuse the existing ones.

The consumers are duplicate-ID detection, the Resolve queue and its counts,
Attendance (reconciliation), scoring, Results, Reports and final export. Value
readers (`effective_identifiers`, `effective_set_codes`, `effective_answers`,
`results_by_scan`) read each sheet's review ledger from **the batch the sheet
was read into**, where its records live.

### 2. Supersession is explicit, never inferred

A sheet stops counting only because of a persisted fact:
- a confirmed rescan link (`scan_rejection.replacement_scan_id`);
- a lifecycle state;
- or a live batch supersession.

Matching Student IDs never imply supersession, and nothing follows a
"latest batch wins" rule. Chains A→B→C are walked to their root linearly, with
cycle detection (`lineage_roots`). Only the newest sheet of a valid chain can
count, because every earlier sheet is `superseded_by_replacement`.

### 3. The population key (no migration)

Downstream tables stay keyed `(roster_id, batch_id)`. A session's downstream
state is stored under one deterministic batch, the **population key**: the
oldest batch of the session that already holds reconciliation or results, else
its oldest batch. Every downstream entry point normalises any batch id of the
session to this key.

Once state exists under the key, it never moves, so adding a batch, a rescan
batch or a reprocess batch never orphans decisions. For a one-batch session
(every upgraded project), the key is that batch, so stored state reads as it
did before.

Per-sheet records (`review_conflict`, `scan_rejection`) stay in the sheet's own
batch.

### 4. Session isolation

Session-wide never means project-wide. Another session's sheets never enter
this population, its duplicate check or its results.

`confirm_replacement` now refuses a link between two sessions (use *Combine
sessions*). A cross-session link made before this phase is honoured through its
lineage root: it counts in the original's session. Project Health reports it.

### 5. Corruption is reported, not repaired

Project Health gains `_session_population_issues`:

| Check | Level |
|---|---|
| `RESCAN_LINEAGE_CYCLE` | error |
| `DANGLING_REPLACEMENT` | error |
| `CONTRADICTORY_REPLACEMENT_LINK` | error |
| `LIFECYCLE_BATCH_MISMATCH` | error |
| `CROSS_SESSION_REPLACEMENT` | warning |
| `SESSION_DOWNSTREAM_SPLIT` | warning |

The resolver never rewrites what it reads. A sheet on a cycle has no defined
root, and only its own lifecycle row decides it.

### 6. Duplicates across the session

`review_store.sync_duplicate_identifiers` compares the effective identifiers of
the session's effective set. A value takes part when it is non-empty and has no
`?` or `_`. Each duplicate record is written in its sheet's own batch, with the
related scans across batches. Re-running is idempotent: a row is touched only
when its related scans changed, and no audit event is written otherwise.
Resolve decisions that can change an identifier re-run the sync once per
session.

## Consequences

- **Counts agree by construction.** Every stage reads the same projection. The
  acceptance test compares identity sets (scan ids, candidate ids) across
  population, Attendance, scoring, report inputs and the generated workbook.
- **History is retained.** Superseded, rejected, excluded and replaced
  recognition rows are kept. The effective set is computed, not stored, and is
  rebuilt from the database without reading images.
- **Batch-scoped operational views stay batch-scoped.** These are the Scan
  stage's list and progress, resume/retry, and the Scan CSV export (an
  operational export of one run).
- **Behaviour change.** After *Reprocess All*, `downstream_batch_id` is the
  session's stable key (the original batch), not the reprocess batch. The
  population is drawn from the live (reprocess) batch. One Phase 2 test was
  updated with this explanation.
- **Final Export waits for the whole session.** While any sheet of the session
  is unread (`pending` / `queued` / `processing`, e.g. a later batch still
  running or interrupted), `report_store.check_readiness` adds a blocking
  `SCORING_INCOMPLETE` issue. Previews still work. Phase 2 got the same effect by
  returning no downstream batch while the only live batch ran.
- **Limitation.** Combining two sessions that both hold downstream state leaves
  two holding batches. The oldest is read and `SESSION_DOWNSTREAM_SPLIT` warns.
  The decisions under the other key are not merged automatically.
- **Status.** Implemented and tested (automated). Not operator-validated, not
  production-qualified. Performance figures are in the phase handoff, for
  synthetic rows only.
