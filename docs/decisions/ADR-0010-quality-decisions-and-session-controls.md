# ADR-0010: Quality decisions, operator intent and one coordinator per project

- **Status:** Accepted
- **Date:** 2026-10-04
- **Phase:** 0.1.1-alpha.0, revised phase 7 / roadmap Phase E (second part:
  quality decisions, rescan suggestions, controls, snapshot, finish-session)
- **Builds on:** ADR-0006 (crash-safe work units), ADR-0007 (session effective
  set), ADR-0008 (intake), ADR-0009 (continuous engine, single writer)
- **Schema:** **17** (migration 17, additive)
- **Answers:** ARCHITECTURE_NOTES §§12, 13.1, 14; ACCEPTANCE_CRITERIA E3, E4,
  E7, E8; the four carry-forward items of `PHASE_F_HANDOFF.md` §§25-31

## Context

Revised phase 6 left a headless engine whose operator semantics were missing:
no interpretation of scan-quality evidence, pause flags in memory (reset by a
restart), cross-sheet duplicate conflicts only at unit end, no guard against
the engine and the finite Scan stage processing one project at once, and no
session-level progress or closure validation.

## Decisions

### 1. Quality decisions are persisted per sheet, in the work unit; the policy is pinned per session

A pure function (`domain/quality_decision.py`) maps stored evidence to
`ACCEPT` / `ACCEPT_WITH_WARNING` / `RESCAN_REQUIRED` / `RETRY_PROCESSING`. Its
policy is versioned data with a SHA-256 fingerprint of canonical JSON;
`DEFAULT_POLICY` is labelled **UNVALIDATED DEFAULT**.

*Why a table (`scan_quality_decision`) and not a derivation on demand:* the
geometry verdict exists only inside `batch_scan.result_json`; a snapshot
polled every second cannot count suggested rescans without decoding every
stored result. The row is written by `record_results` in the **same
transaction** as the sheet's result and conflicts (the ADR-0006 work unit), so
a committed sheet always has its decision and a crash never leaves half of one.
Each row records the fingerprint that produced it.

*Why pin per session:* a session must not be re-interpreted because the
application's default changed after scanning began. The session pins the
policy the first time a sheet is decided (`scan_session.quality_policy_json`);
it is never replaced. A policy this build cannot read is refused, not
silently swapped.

### 2. A suggestion never changes the population

`RESCAN_REQUIRED` creates no `scan_rejection` row and does not affect the
effective set. It is *outstanding* until an operator answers: any non-active
lifecycle (confirmation is exactly `reject_scan`), an audited dismissal, or a
human resolution of the sheet's own evidence conflict in Resolve (the same
question). One SQL predicate (`quality_decisions.outstanding_clause`) is the
only definition.

### 3. Operator intent is persisted and read every step

`scan_session.intake_paused`, `scan_session.processing_intent`
(`running` / `paused` / `stopped`) and `intake_source.intake_paused`, each change
one transaction with its audit event. The engine reads them on every step;
`start()` resumes recognition only when the stored intent is `running`.
*Finish current and stop* and *Cancel queued work* persist `stopped` **before**
they act, so a crash during the drain comes back stopped. The engine's
in-memory stop primitives remain (mechanics) and are OR-ed with the intent.

### 4. One coordinator per project: a process-local lease

`services/coordinator.py`: a lease keyed by the project's database file, held
by the finite Scan stage for exactly its run (taken before it registers or
claims anything, given back after the batch has left `running`) and by the
engine from `start()` (before its recovery touches a claim) to `shutdown()`. A
second coordinator of either kind gets a typed `CoordinatorBusyError`.

*Why process-local is sufficient:* the project lock already refuses a second
process (reclaiming a dead process's lock is an explicit operator decision), so
the remaining hazard is two coordinators in one process. A process-local lease
cannot outlive a crash. Owners are held weakly, so an owner that no longer
exists cannot keep the project; an exception escaping an engine coordinator
method leaves it `faulted` (its claims for recovery, as after a crash) and
releases the lease.

*Rejected:* a lock row or file in the project - it would need its own crash
recovery and could outlive a dead process, the very poisoning this must avoid.

### 5. Cross-sheet duplicates: after each commit group, bounded; the running unit is the recovery marker

After each work-unit commit the engine runs the existing bounded
`sync_duplicate_identifiers_for` for that group's sheets (separate short
transaction). Option B of the brief: a kill between the commit and the pass
leaves the unit `running` - the existing marker (ADR-0006) that its
batch-scope pass is owed - and recovery completes it from stored results; the
snapshot never reports *caught up* while a unit is `running`. Human-touched
records are kept, as always. No second conflict system.

*Rejected:* syncing inside the work-unit transaction - it would read other
sheets' rows under the writer's transaction for every commit and lengthen it
with session-wide work.

### 6. Finish-session is its own service; `close_scan_session` keeps its Phase C checks

`session_finish.finish_scan_session` reconciles every enabled source, returns
every blocker as a typed code (or closes through `close_scan_session`, the
existing one-transaction close). The lower-level `close_scan_session` keeps
exactly its Phase C checks: the finite workflow's one-step *Close session and
generate final export* and existing callers that close a session with files
still pending (to have them held) are unchanged. Files for a closed session
are held either way, never added.

### 7. The worker-lost retry counter stays in memory

Analysed and tested (`tests/integration/test_phase7_recovery.py`): a restart
resetting it only grants a sheet up to `1 + infrastructure_retries` more
submissions per process start; a result can only be written onto a still
claimed row and a committed row is terminal, so no duplicate result, conflict,
audit event or effective-set change can follow; the placeholder of a lost
worker is a software fault, never a rescan. Persisting it would protect
against nothing that matters for correctness.

## Consequences

* Schema 17; a schema-17 project is refused by older builds with the existing
  "newer version" message. Upgrade from a real schema-16 fixture tested.
* The finite workflow writes decision rows too (no new operator step; finite
  closure unchanged).
* Phase 8 builds on: `take_snapshot`, `finish_scan_session` /
  `engine.finish_session`, `session_controls`, `intake_decisions`,
  `quality_decisions`, `session_possible_rescans`, `CoordinatorBusyError`.
* Not validated: the quality mapping, the caught-up allowance and the
  registration-alarm window are starting values, not calibrated on real data.
