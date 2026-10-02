# Revised phase 4 handoff — session-wide effective sheet set

Branch `feat/0.1.1-phase4-session-effective-set`, from `main` at `0e94d67`
(the Phase 3 merge; Phase 3 tip `3937ef5`). **Not merged, not tagged, nothing
released.**

## Implementation plan (written before code changes)

### What the code does today (inventory, 2026-10-02)

* **One selector.** Attendance, Results and Reports read
  `scan_sessions.downstream_batch_id` - the active session's newest primary,
  non-superseded batch. Its docstring calls it a temporary single-batch rule.
* **Per-sheet lifecycle already exists and is the right primitive.**
  `scan_rejection` holds one row per non-active scan (`rejected_pending_rescan`,
  `superseded_by_replacement`, `reimport_of_rejected`, `excluded`, `deferred`);
  no row = active; only `active` is eligible. A rescan is an explicit link
  (`scan_rejection.replacement_scan_id`, unique) from a rejected original to an
  active replacement, in any batch.
* **Batch-level supersession** (`batch_supersession`, Reprocess All) is read
  only by `downstream_batch_id`, display and health - never by reconciliation,
  scoring or review.
* **Cross-batch rescans** are handled by a per-batch rule: a replacement counts
  in its *original's* batch (`adopted_replacements` / `counted_elsewhere`).
  Attendance and scoring apply it; duplicate detection does not.
* **Re-implemented eligibility** in ~16 places (inventory list in the final
  handoff): `_ineligible_scans` SQL, `sync_duplicate_identifiers`' own rule,
  `batch_scripts` / `_placements`, `script_scope`'s unscoped branch,
  `gather_inputs`, `_unattached_rescans`, the CSV export, Project Health, ...
* **Downstream state is keyed `(roster_id, batch_id)`**: `reconciliation_run`,
  `reconciliation_entry`, `reconciliation_script`, `reconciliation_decision`,
  `candidate_result`. `review_conflict` and `scan_rejection` are keyed by the
  *sheet's own* batch (per-sheet records - correct as they are).
* **Duplicate IDs** are detected per batch, on the *machine* reading.

### Design

1. **One canonical resolver**, `services/session_population.py` (rules in
   `domain/session_population.py`, pure): for the scan session a batch belongs
   to, every sheet of every batch of the session gets exactly one
   *disposition*, derived only from persisted rows (`batch_scan.status`,
   `scan_rejection`, `batch_supersession`, `scan_batch.scan_session_id`). The
   *effective set* is the sheets whose disposition counts. No image, no new
   table.
2. **Population key instead of a migration.** Downstream tables stay keyed by
   a `batch_id` column, but the value written and read is the session's
   **population key batch** - one deterministic batch of the session (the
   oldest one that already holds downstream state, else the oldest batch).
   Every downstream entry point normalises any batch of the session to that
   key, so a session has exactly one reconciliation / result set. For every
   one-batch session (all upgraded projects) the key is that batch, so stored
   state is read exactly as before. Schema stays 14.
3. **Lineage, not batches.** A confirmed replacement counts in the session of
   its lineage root; a superseded original never counts; a chain
   `A → B → C` leaves only the active leaf. Supersession is never inferred from
   Student IDs. New cross-session links are refused; existing ones keep
   counting where they did and are reported by Project Health.
4. **Session-wide consumers**: duplicate detection (on *effective* IDs, so a
   correction can create or clear a duplicate), Attendance
   (`batch_scripts` / `_placements` / `script_scope`), scoring
   (`gather_inputs`), Results, Reports (readiness, generation, rescans /
   deferred), the Resolve queue and counts, and the post-lifecycle re-derive
   (`_after_change`). The per-batch `adopted` / `elsewhere` overlay is replaced
   by the population.
5. **Batches stay operational**: Scan's progress, resume, retry, CSV export of
   the batch list and the batch-scope review passes keep their batch scope.
6. **Project Health**: lineage cycles, dangling links, cross-session links,
   lifecycle row in a different batch than its scan, two effective sheets in
   one lineage, reconciliation rows naming a missing scan.
7. **Tests**: domain unit tests; service tests (dispositions, chains,
   sessions, idempotence); duplicate matrix (12 cases); multi-batch
   Attendance / scoring / Results / Reports with identity-set comparisons;
   reopen; a ≥3-batch, ≥100-script acceptance scenario; GUI scope labels.
