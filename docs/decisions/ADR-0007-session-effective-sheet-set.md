# ADR-0007: The scan session's effective sheet set and its persisted scope

- **Status:** Accepted (revised 2026-10-02 by the Phase C completion audit)
- **Date:** 2026-10-02
- **Phase:** 0.1.1-alpha.0, revised phase 4 / roadmap Phase C (session-level
  review, reconciliation, scoring and reporting)
- **Builds on:** ADR-0005 (ScanSession → finite ScanBatch), ADR-0006 (crash-safe
  work units), the Reject & Rescan lifecycle (`scan_rejection`, migration 11)
- **Schema:** **15** (migration 15: additive columns and indexes; no table rebuilt)
- **Answers:** ARCHITECTURE_NOTES §8.3 (representation; finding F4) and F5
  (report scope)

## Context

Phase 2 grouped batches into scan sessions, but every downstream stage still
read **one batch**: `scan_sessions.downstream_batch_id` picked a batch, and
Attendance, scoring, Results, Reports, duplicate-ID detection and the Resolve
queue saw only that batch's sheets. A session read in three batches was
therefore three populations. A Student ID read in batch 1 and again in batch 3
was never a duplicate. Candidates whose script was in a later batch showed as
missing. Each module also filtered its own "active sheets", in slightly
different ways.

Every Phase 7–8 table is unique on `(roster_id, batch_id, …)` with `batch_id` a
NOT NULL foreign key (F4). `generated_report` recorded no scope at all (F5).

## Decision

### 1. One resolver for the effective sheet set

`services/session_population.py` is the only place that decides which recorded
sheets count for a scan session. Every sheet of the session's batches gets
exactly one `SheetDisposition`, decided by the pure
`domain/session_population.classify` in this order:

| Order | Fact | Disposition | Counts | Listed |
|---|---|---|---|---|
| 1 | its batch is replaced by a live batch supersession (Reprocess All) | `BATCH_SUPERSEDED` | no | no |
| 2 | its rescan lineage's root belongs to another session | `COUNTED_IN_OTHER_SESSION` | no | no |
| 3 | `scan_rejection.state` is not active | `SUPERSEDED_BY_REPLACEMENT`, `REJECTED_PENDING_RESCAN`, `REIMPORT_OF_REJECTED`, `EXACT_DUPLICATE` (`duplicate_content`), `EXCLUDED`, `DEFERRED`; an unknown state is treated as pending | no | pending and deferred only |
| 4 | `batch_scan.status` is `duplicate` (left unread at registration) | `EXACT_DUPLICATE` | no | no |
| 5 | not read (`pending`, `queued`, `processing`, `cancelled`) | `NOT_READ` | no | no |
| 6 | read but failed | `EFFECTIVE_UNREADABLE` | yes | — |
| 7 | otherwise | `EFFECTIVE` | yes | — |

"Counts" is the **effective set**. "Listed" sheets appear against their
candidate (*rescan required*, *deferred*) but are never scored.

`duplicate_content` is the one new lifecycle state. No existing state meant "a
copy of a sheet that still counts"; re-imports cover copies of *rejected*
sheets only.

The effective set is consumed by:
- duplicate-ID detection;
- the Resolve queue and its counts;
- Attendance (reconciliation);
- scoring and Results (the Dashboard analyses the Results rows themselves);
- Reports and final export;
- the renamed-copy export.

Value readers read each sheet's review ledger from **the batch the sheet was
read into**, where its records live.

### 2. Supersession is explicit, never inferred

A sheet stops counting only because of a persisted fact:
- a confirmed rescan link;
- a lifecycle state (including an exact-duplicate link made from content hashes);
- a live batch supersession.

Matching Student IDs never imply supersession, and nothing follows a "latest
batch wins" rule. Chains A→B→C are walked to their root linearly, with cycle
detection. Only the newest sheet of a valid chain can count. A new rescan link
between two sessions is refused (use *Combine sessions*). A legacy one counts
in the original's session and is reported by Project Health.

### 3. Persisted session scope — §8.3 evaluated, option (a) in its minimal form

The first implementation of this ADR stored no scope. It derived a
"population key": the oldest batch of the session already holding downstream
state, else the oldest batch. The completion audit tested that design against
the required properties and found it **incomplete**:

| Required property | Derived key (first implementation) |
|---|---|
| Future batches cannot change the authoritative state unpredictably | **Fails under Combine**: a combined session's oldest holding batch can be one brought in from the other session, switching every downstream read to the other session's decisions |
| Combining sessions cannot silently discard valid operator decisions | **Fails**: the non-key session's reconciliation decisions were left outside the combined session; only a Health warning said so |
| Report provenance identifies the session | **Fails**: `generated_report` had no scope column (F5) |
| Upgraded data reads what the previous build showed | **Fails** for a schema-14 session with downstream state in two batches: the previous build showed the newer, the rule picked the older |

The three §8.3 options were then weighed against the code:

- **(a) Generalise the scope key** in place, by rebuilding the five Phase 7–8
  tables with a `scan_session_id` scope. This is one code path, but the
  rebuild, the 12-step SQLite procedure and the rewrite of every downstream
  query are the line's largest regression risk.
- **(b) Session-scoped tables beside batch-scoped ones.** Two code paths to keep
  in step.
- **(c) Session as the only stored scope.** This is the cleanest long-term
  answer, but it needs (a)'s rebuild too.

**Chosen: (a) in its minimal, additive form.** The session's scope is a
recorded fact on the session row, `scan_session.downstream_batch_id`: the
session's **bound downstream store**. Downstream rows keep their existing key
columns, whose `batch_id` value is now an opaque storage key resolved only
through the session.
- **Bound** by the first downstream write (reconcile or score), and, for an
  upgraded project, by the upgrade step on the first writable open
  (`bind_downstream_stores`).
- **Never re-derived** once bound. Adding, combining or superseding batches
  cannot move it. A batch view is a filter, not a store (§8.2 "diagnostic").
- **Legacy split state** (a schema-14 session reconciled again after a later
  batch arrived) binds to the newest non-superseded holder, the state the
  previous build showed. An audit event names the batches kept as history.

Migration 15 also adds the report scope:
- `generated_report.scan_session_id`;
- `generated_report.is_final`;
- `generated_report.session_closed_at` (the close an output came from).

It also adds indexes for the bounded lookups: `batch_scan.identifier_value`,
`batch_scan.content_sha256`, and `review_conflict(conflict_type, machine_value)`.

Every column is nullable or defaulted, and **deferred** in the ORM, so a
schema-14 project opened read-only still reads. No table is rebuilt; nothing
is copied.

This keeps (c)'s semantics (the session is the authoritative selection; batch
views are filters) without (a)'s rebuild. A rebuild to drop the vestigial
`batch_id` key column remains possible later and is not needed for any
behaviour.

### 4. The session is the selection at the service boundary

`services/session_scope.py` takes a `scan_session_id` for the authoritative
operations:
- reconcile, entries and stored counts;
- score and results;
- overview, readiness, generate and final-export status.

It resolves the store from the session. Attendance, Results and Reports hold
`state.scan_session_id`, defaulting to the active session
(`scan_sessions.downstream_session_id`), and derive their store from it. The
Scan stage's Session menu can switch the active session, and the downstream
stages follow.

The batch-keyed store functions remain as compatibility wrappers for
per-entry operations and existing callers. They normalise any batch of a
session to its store. No function chooses "the latest batch" or uses
`updated_at`.

### 5. Combining sessions

When more than one session being combined already holds Attendance/Results
decisions, *Combine* is refused, listing them. The operator may instead name
whose decisions the combined session keeps (`keep_downstream_of`; the GUI asks):
- the combined session is bound to that session's store;
- the other decisions stay in the database as history; nothing is deleted or
  merged;
- the choice is an audit event.

With one holder, its store becomes the combined session's. After a combine,
duplicates and reconciliation are re-derived over the combined population.

### 6. Duplicate IDs: session-wide, on effective IDs, bounded

Duplicate IDs are grouped over the session's effective, reliable identifiers
(corrections included).

**Incremental and bounded.** A Resolve decision, a lifecycle change, or a batch
finishing runs `sync_duplicate_identifiers_for(scan_ids)`. It re-derives only:
- the identifier groups the touched sheets now hold, plus those their existing
  records name (the group a corrected sheet is leaving);
- the candidates: sheets read as one of those values (indexed), sheets with a
  human identifier decision, and those groups' records.

Untouched groups are neither read nor written. The full rebuild
(`sync_duplicate_identifiers`) is used only by recovery, combine, a grouping
change, and tests. It is also the fallback when more than 2,000 sheets are
touched at once (a very large batch finishing).

**Grouping policy (C4).** Grouping is by identifier alone (the default,
unchanged) or by (set, identifier). It is an office decision in Project
Configuration: stored per project, audited, and every session is re-derived
on change.

### 7. Exact-content duplicates at registration (defect 4, manual adds)

The last step of registering a run's files runs after their SHA-256 is recorded
on the worker thread and before any sheet is read. That step,
`link_exact_duplicates`, handles a copy of a sheet in a **live batch of the same
session** (any file name, folder or batch):
- it is linked to the earliest such sheet (`duplicate_content`, audited);
- it gets `batch_scan.status = duplicate`, terminal and never resumed;
- it is **never recognised**.

A copy of a rejected or superseded scan becomes the existing re-import, now
also before recognition. Another session's identical image is not this
session's concern. A batch replaced by *Reprocess All* is never an original.

Hashing is in the run's registration step, not on the GUI thread at *Add Files*,
because hashing thousands of files there would freeze the window. Either way
the hash is known before recognition begins. Persistent intake sources hash at
discovery (Phase D).

### 8. Final Export requires a CLOSED session

**While open, results are provisional.** The Results header and the Reports
heading say so. A preview's file name (`…_Result_Provisional`) and its
Processing Log say so too.

**Final Export of an open session is refused.** `SESSION_OPEN` is a blocking
readiness issue and cannot be acknowledged away.

**Closing runs the closure checks** (§14.3):
- refused while a batch is running, while any sheet is unread, or while a
  conflict is unresolved;
- refused while a rescan is outstanding or a sheet is deferred, unless a named
  operator accepts incomplete results (audited). This reconciles §14.3 with
  the earlier owner decision that incomplete results may be exported when
  clearly marked.

**On an open session, Final Export offers "Close session and generate final
export".** It checks everything first:
- the closure blockers;
- generation's own preconditions;
- each chosen set's own blocking readiness issues.

If anything blocks, it lists it and closes nothing. Otherwise it closes the
session (sealing its batches, audited) and generates.

**Every output records its session, whether it was final, and the close it
came from.** A final output is current only while the session is closed by
that same close. Reopening makes it stale, and re-closing does not revive it;
only regeneration does. An output written before migration 15 has no recorded
session and is shown as "no final export", never guessed current.

### 9. Corruption is reported, not repaired

Project Health:

| Check | Level |
|---|---|
| `RESCAN_LINEAGE_CYCLE` | error |
| `DANGLING_REPLACEMENT` | error |
| `CONTRADICTORY_REPLACEMENT_LINK` | error |
| `LIFECYCLE_BATCH_MISMATCH` | error |
| `CROSS_SESSION_REPLACEMENT` | warning |
| `SESSION_DOWNSTREAM_SPLIT` (unbound session, state in several batches) | warning |
| `SESSION_STORE_NOT_IN_SESSION` (bound store not a batch of the session) | error |

## Consequences

- **Counts agree by construction.** Identity sets (scan ids, candidate ids)
  agree across population, Attendance, scoring, report inputs and the generated
  workbook. A one-batch session reproduces the pre-phase-4 reconciliation rows,
  results and workbook cells exactly. That is a golden test against `main` at
  `0e94d67`.
- **History is retained.** The effective set is computed, not stored, and is
  rebuilt from the database without reading images.
- **Batch-scoped operational views stay batch-scoped.** These are the Scan list
  and progress, resume/retry, and the Scan CSV export of one run. Resolve has a
  diagnostic batch filter. A *source* filter needs Phase D's intake source.
- **Behaviour changes, each with tests updated and explained:**
  - after *Reprocess All*, `downstream_batch_id` is the session's stable store;
  - closing a session runs closure checks;
  - an identical image is never read.
- **Measured (synthetic rows, one run each):** the bounded duplicate pass after
  one ID change took about 0.02 s at 10,000, 50,000 and 100,000 sheets. The
  full rebuild took 0.6 s / 3.1 s / 5.5 s. Figures and caveats are in the phase
  handoff.
- **Status.** Implemented and tested (automated; scripted GUI). Not
  operator-validated, not real-scan validated, not production-qualified.
