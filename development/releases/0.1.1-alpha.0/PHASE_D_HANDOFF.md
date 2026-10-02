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

## What was built

| Piece | Where |
|---|---|
| Pure rules: `SheetDisposition`, `SheetFacts`, `classify`, `lineage_roots` | `src/omr_scanner/domain/session_population.py` |
| The resolver: `population`, `population_key`, `session_key`, value readers, `describe` | `src/omr_scanner/services/session_population.py` |
| Attendance on the population (`_batch_readings`, `batch_scripts`, `script_scope`, every entry point normalised to the key) | `services/reconciliation_store.py` |
| Scoring on the population (`gather_inputs`; key normalised) | `services/scoring_store.py` |
| Reports / final export on the key (readiness, generation, unattached rescans / deferred) | `services/report_store.py` |
| Session-wide duplicates; re-sync after identifier decisions (`_refreshes_duplicates`); session-wide `list_conflicts` / `count_conflicts` / undo | `services/review_store.py` |
| Session-wide `list_cases`, `count_cases`, `processed_sheets`, `outstanding_for_set`, `list_dispositions`, `deferred_for_set`; `_after_change` per key; cross-session links refused | `services/scan_lifecycle.py` |
| `downstream_batch_id` returns the session key; `describe_downstream` names the session | `services/scan_sessions.py` |
| Resolve queue, summary, undo and views session-wide; per-sheet work in the sheet's own batch (`_sheet_batch`); header names the session | `gui/review/page.py` |
| Attendance / Results headers name the session; undefined-set sync over the session before reconciling | `gui/attendance/page.py`, `gui/attendance/worker.py`, `gui/results/page.py` |
| Project Health `_session_population_issues` | `services/project_health.py` |
| ADR | `docs/decisions/ADR-0007-session-effective-sheet-set.md` |

Schema: **14, unchanged.** Migration 15 not used.

## Deviations from the plan

* Plan item 6 named "two effective sheets in one lineage" and "reconciliation
  rows naming a missing scan" as Health checks. Neither was added as its own
  check:
  * Two effective sheets in one lineage cannot arise without a link on a row
    that is not superseded. `scan_rejection.scan_id` and
    `replacement_scan_id` are both unique, so every chain is linear and only
    its leaf can be active. That case is reported as
    `CONTRADICTORY_REPLACEMENT_LINK`.
  * `reconciliation_script.scan_id` is a foreign key to `batch_scan`, so a
    missing scan is reported by the existing foreign-key check.
* Added beyond the plan: `SESSION_DOWNSTREAM_SPLIT`, for a session whose
  downstream state sits under more than one batch (possible only for data made
  before this phase, or after *Combine* of two sessions that both held state).

## Added safety: Final Export waits for unread sheets

Downstream now reads the whole session. A session can therefore have sheets
that are not read yet, for example from a later batch still running or
interrupted. `report_store.check_readiness` adds a blocking `SCORING_INCOMPLETE`
issue, "*N sheet(s) of this scan session have not been read yet*", until
every sheet is read. Tested in
`test_session_population.py::TestReadiness`.

## Behaviour changes an existing test encoded (updated, with reasons)

* `tests/gui/test_scan_session_gui.py::TestReprocessAndRescan::test_reprocess_all_creates_a_superseding_batch`
  asserted `downstream_batch_id == reprocess batch`. Phase 4 stores a session's
  downstream state under a key that never moves (the original batch) and draws
  the population from live batches only. The test now asserts that the key is
  the original and that every effective sheet comes from the reprocess batch.
* `tests/integration/test_scan_sessions.py::TestDownstreamBatch` (2 tests)
  asserted the session's *newest* batch, and `None` while a reprocess runs.
  They now assert the stable key, that the later batch is in the key's
  population, and that the superseded original contributes nothing. While
  sheets are unread, Final Export is refused (above).
* `tests/integration/test_reject_and_rescan_followup.py::TestCountedExactlyOnce::test_reopening_changes_nothing`
  concatenated the results of two batches. On the writable reopen, the
  scan-session backfill groups two batches joined by a confirmed rescan into one
  session, so both batch ids now return the *same* result rows. The test now
  asserts that the two batches share a session, that the result ids are
  identical, and that the rescan is scored once.
* `tests/gui/test_resolve_page.py::TestWorkspace::test_a_sheet_level_conflict_offers_no_value_buttons`
  selected "row 0". `create_batch` attaches to the active session, whose whole
  queue Resolve now shows, so the corrupt sheet's conflict is now selected by
  id.
* `tests/crash/test_crash_matrix.py` cases 05 and 08 expected only decided
  conflicts to leave the queue. Duplicates are now detected on the effective
  Student ID, so the harness's correction of one sheet of a duplicate pair
  withdraws its partner's undecided record, with an audit event. The expected
  queue removes exactly those audited withdrawals; the rest of each assertion
  is unchanged.

## GUI inspection

Rendered at 1366×768 and 1100×680 with `QT_SCALE_FACTOR=1.75` (device pixel
ratio 3.06 on the development display). The data was the acceptance
scenario's 3-batch session, reconciled and scored. Screenshots are in
`test-output/gui/phase4/` (git-ignored).

* Resolve's header reads *Scan session · 108 effective script(s) from 3
  batch(es) · 3 replaced · 3 rejected / excluded / deferred*. The queue holds
  the within-batch and cross-batch duplicates. The summary shows *1 rescan
  required*.
* **Defect found and fixed.** Results read *Marking batch f1a62e25 · 37
  scan(s) from …batch0* while marking 108 scripts from 3 batches, and
  Attendance read *Batch: f1a62e25*. Both now name the scan session. A
  one-batch session keeps the old text.
* After the fix, Results reads *Candidates 109 · Scored 102 · Cannot be scored
  7*:
  * 34 scored per set, as derived by hand in the acceptance test;
  * the 7 that cannot be scored are the 2 duplicates, absent-with-script, the
    unresolved identity, the missing script, the pending rescan and the
    deferred sheet.

  Reports shows 34 scored per set.
* The capture script's process ended with an access violation inside
  `MainWindow.close_project()` after every screenshot was saved. This is the
  teardown-abort class recorded in Phase 3; it was not investigated further
  here.

## Performance (measured, synthetic rows only)

`scratchpad/p4_bench.py` is a scratch script, not committed. It writes one
session of 5 sealed batches of `batch_scan` rows directly, with no images, on
the development machine's local SSD, where 1 Student ID in 500 is read twice.
Times are one run each, in seconds:

| Sheets | population | effective_identifiers | sync_duplicates (first / again) | count_conflicts (session) | list_conflicts (session, 500) |
|---:|---:|---:|---:|---:|---:|
| 1,000 (3 batches) | 0.033 | 0.059 | 0.088 / 0.082 | 0.024 | 0.025 |
| 10,000 | 0.109 | 0.609 | 0.861 / 0.954 | 0.145 | 0.095 |
| 50,000 | 0.783 | 3.165 | 4.828 / 4.383 | 0.544 | 0.890 |

Roughly linear: 5× the sheets gives about 5× the time. There is no pairwise
comparison of sheets; duplicates are grouped by value. **Not measured:**
100,000 sheets, network storage, or real recognition results. These figures
do not claim production performance.

**Cost to note.** Every Resolve decision that can change a Student ID re-runs
the session's duplicate sync once. At 50,000 sheets that is about 4–5 s per
such decision. Making it incremental is not done.

## Known limitations

* **Combining two sessions that both hold downstream state.** The oldest
  holder is read; the other's reconciliation decisions are not merged
  automatically. `SESSION_DOWNSTREAM_SPLIT` warns.
* **Legacy cross-session rescan links** are honoured (the replacement counts
  in the original's session) and reported. New ones are refused.
* **Per-batch operational surfaces stay per batch on purpose:** the Scan
  list, its progress, Resume / Retry, and the Scan CSV export (one run's
  readings, including sheets that no longer count).
* The undefined-set-code pass is per batch, run over every live batch of the
  session before reconciling. It is not deduplicated across a lineage: a
  superseded original with an undefined code keeps its conflict, which leaves
  the queue because its sheet no longer counts.
* Not operator-validated, not real-scan validated, not production-qualified.
  No power-loss testing.
