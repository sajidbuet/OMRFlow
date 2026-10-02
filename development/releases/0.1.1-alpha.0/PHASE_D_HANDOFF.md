# Revised phase 4 handoff — session-wide effective sheet set

> **Read the "Phase C completion audit" section at the end first.** It
> supersedes the sections before it where they differ: schema (now 15), the
> "population key" (now a recorded store), combining sessions, duplicate-sync
> cost and the Final Export lifecycle.

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

## Test results (2026-10-02)

| Run | Result |
|---|---|
| Baseline, `main` at `0e94d67`, main checkout | 6,294 passed, 16 skipped, 0 failed, 5 deselected (1 h 28 min, run alongside other work) |
| First full run of the branch | 6,349 passed, 29 skipped, **6 failed**: crash cases 05 / 08, one Resolve GUI test, one reject/rescan reopen test, two `TestDownstreamBatch` tests. All six encoded batch-only behaviour; each was updated with an in-test explanation (section above). |
| Final full run, branch at `048334f`, worktree | **6,356 passed, 29 skipped, 0 failed**, 5 `stress` deselected (53 min 12 s) |
| `tests/local` (13 real-sheet tests) from the main checkout, against the branch's `src` | 13 passed, 10 skipped (fixture-dependent, the same skips as on `main`) |
| `tests/crash` (non-stress) after the crash-test update | 16 passed |
| `tests/crash -m stress` (the 1,000-sheet 1/25/50/75/99 % kill series) | 1 passed (2 min 20 s) |
| `ruff check src tests tools scripts` / `mypy` | clean / no issues (210 files) |

New tests:
- `tests/unit/test_session_population_rules.py`: 25 tests.
- `tests/integration/test_session_population.py`: 31 tests:
  - the 12-case duplicate matrix;
  - dispositions, chains, isolation, Resolve, downstream and Health;
  - the readiness test.
- `tests/integration/test_session_acceptance.py`: 16 tests, the acceptance
  scenario.
- `tests/gui/test_session_resolve_gui.py`: 3 tests.

Mid-run I counted the letters F and E in the progress output and took them for
failures. They were OpenCV log lines (`[ERROR:0@…] … PngDecoder`) from a
deliberate corrupt-image test. The run's own summary is the authority, and it
reports 0 failed.

---

## Phase C completion audit (2026-10-02)

Audited against ROADMAP §0.1.1-C, ARCHITECTURE_NOTES §§6, 8, 9, 14 and
ACCEPTANCE C1–C10, not against the sections above. Each gap below was found
in code and closed in code, with tests.

### Gaps found and closed

| # | Gap | Was it real? | Closed by |
|---|---|---|---|
| A | Duplicate-ID sync re-derived the whole session after every ID-changing decision | Real | `sync_duplicate_identifiers_for`: bounded to the touched groups |
| B | Byte-identical images under another name, folder or batch were read and became second scripts (defect 4) | Real: hashes were computed at run start but never consulted | `link_exact_duplicates` at registration, before recognition |
| C | No closed-session requirement, no provisional labels, no one-step close-and-export, no closure checks, no report scope or staleness | Real | Closure checks; `SESSION_OPEN`; provisional labelling; Reports one-step flow; migration 15 report columns; `final_export_status` |
| C′ | The earlier unread-sheet guard did not gate `generate_xlsx` itself | Real | `with_session_issues` in both readiness and generation |
| D | The derived "population key" could switch stores under Combine, silently orphan a session's decisions, recorded no report scope, and misread upgraded split state | Real | Migration 15: a recorded store per session; combine refuses or records an explicit choice; legacy-aware upgrade binding |
| E | Services were addressed by an arbitrary batch id | Real | `services/session_scope.py`; pages hold `scan_session_id`; *Switch Scan Session* |
| F | No Resolve batch filter | Real | Diagnostic batch filter (a *source* filter needs Phase D) |
| G | Dashboard reading its own population | **Not real**: it already used `state.every_result` | A test that fails if it ever reads independently |
| H | No golden one-batch regression of workbook cells | Real | Fixture captured by `main`; values compared |
| C4 | (set, identifier) duplicate grouping option | Real | `DuplicateGrouping`, Project Configuration checkbox |
| scope | Renamed-copy export over the session's effective set | Real | `services/renamed_export.py`, Session menu |
| C10 | Generator-ground-truth multi-batch test | Real | `test_generated_session_ground_truth` |

Found while verifying, and fixed:
- **Upgraded split state read the older decisions.** A schema-14 session
  reconciled again after a later batch arrived showed its newer state under
  schema 14; the derived rule picked the older.
- **Combine did not re-derive the combined session.** A combined session's
  duplicate IDs and reconciliation were not re-derived.
- **"Close session and generate final export" could close and export
  nothing.** This happened when no template was loaded, or when a set's own
  readiness blocked it. The scripted GUI run found it.

### Effective-population rules (state → effective? → downstream treatment)

| State / condition | Effective? | Downstream treatment |
|---|---|---|
| Read, active | yes | Reconciled, scored, reported, duplicate-checked |
| Read but failed (unreadable) | yes (`EFFECTIVE_UNREADABLE`) | Counted as a script with no identity; an Attendance exception |
| Not read yet (pending / queued / processing / cancelled) | no (`NOT_READ`) | Blocks closing and Final Export |
| Batch superseded by *Reprocess All* | no (`BATCH_SUPERSEDED`) | History only; the reprocess batch counts |
| Rejected, rescan awaited | no, **listed** | *Rescan required* against the candidate; blocks close unless incomplete results are accepted |
| Superseded original (rescan confirmed), chains A→B→C | no | History; only the chain's newest sheet counts |
| Re-import of a rejected image | no | Linked to the original; not read (phase 4: before recognition) |
| **Exact-content duplicate** (same session, live original) | no (`EXACT_DUPLICATE`) | Linked to the earliest copy; status `duplicate`; never read |
| Excluded | no | Out of every count; restorable |
| Deferred | no, **listed** | Open item; blocks close unless accepted |
| Confirmed rescan read in another session | counts in the **original's** session | `COUNTED_IN_OTHER_SESSION` here; Health warns; new cross-session links refused |
| Another session's sheets (same ID or same bytes) | not in this population | Never a duplicate here; never merged |

### Incremental duplicate synchronisation

- **Before:** every Resolve decision that could change a Student ID re-ran the
  full session rebuild: every effective identifier read and regrouped.
- **Now:** `review_store.sync_duplicate_identifiers_for(scan_ids)` re-derives
  only these values:
  - the touched sheets' current values;
  - the values their duplicate records name (the group a corrected sheet is
    leaving).

  Candidates are:
  - sheets read as those values, via `ix_batch_scan_identifier`;
  - sheets with a human identifier decision (the only way an effective value
    departs from the reading);
  - those groups' records, via `ix_review_conflict_type_value`.

  Candidates are classified with `session_population.sheets_of_session`
  (indexed reads by scan id), not the whole population.
- **Cost** therefore follows the touched groups plus the number of
  person-decided identifiers, not the session size.
- **Callers:** Resolve decisions and undo, lifecycle changes, and a batch
  finishing. The full rebuild runs only for recovery, combine, a grouping
  change, tests, and above 2,000 touched sheets at once.
- **Evidence:**
  - the 12 required cases, each also checked against a full rebuild
    (`assert_rebuild_changes_nothing`);
  - an instrumented 6,000-sheet session in which one correction fed at most 5
    identifiers to the grouping and wrote only the 3 sheets of the affected
    group;
  - the Resolve GUI and crash suites.

### Measurements

Synthetic rows only: one session of 5 sealed batches, 1 ID in 500 read twice,
local SSD, development machine, **one run each**
(`scratchpad/p4_bench2.py`, not committed; the 100,000 run overlapped a
fixture-generation script).

| Sheets | Population build | Full rebuild (first / again) | Bounded pass, one ID change | One Resolve correction, end to end |
|---:|---:|---:|---:|---:|
| 10,000 | 0.071 s | 0.607 / 0.569 s | 0.025 s | 0.088 s |
| 50,000 | 0.374 s | 3.119 / 2.698 s | 0.019 s | 0.041 s |
| 100,000 | 0.695 s | 5.525 / 5.186 s | 0.021 s | 0.041 s |

The changed sheet's ID was unique (candidate set of 1), and no other sheet had
a human identifier decision. **Not measured:**
- sessions with many human identifier decisions (which enlarge the candidate
  set);
- real recognition, or network storage.

No production throughput claim is made.

### Exact-content duplicates

1. **When is SHA-256 computed?** In the run's registration step, on the worker
   thread, after the files' rows exist and **before any sheet is read**
   (`BatchWorker._hash_sources_for_provenance`). It is not computed at *Add
   Files* on the GUI thread, where hashing thousands of files would freeze the
   window.
2. **Known before recognition?** Yes. `link_exact_duplicates` runs next, and
   the linked files are removed from the run's work list.
3. **Scope:** session-wide, over the live batches of the same session.
4. **The cases:**
   - another file name or folder, or another batch of the session: linked;
   - after reopening: the same, since hashes persist;
   - first copy rejected or superseded: an existing re-import, now before
     recognition;
   - first copy excluded or deferred: `duplicate_content` (the copy cannot
     bring it back);
   - another session: not a duplicate.
5. **Recorded and linkable:** `scan_rejection` (`duplicate_content`,
   `reimport_of_scan_id`) plus a `duplicate_content_linked` audit event. The
   Scan list shows *Duplicate of <file> (not read)*, also after reopening.
   `scan_lifecycle.list_cases(include_reimports=True)` lists it with
   re-imports. Resolve's Rejected/Rescan view, which shows rescan cases only,
   does not, as it does not list re-imports.
6. **Recognition avoided:** yes.
   - The real Scan stage reads one of two identical files, and none of a later
     same-session copy (`test_exact_duplicates_gui`).
   - The crash matrix asserts the dataset's identical image has attempt count 0.
7. **The effective set excludes it deterministically:** `EXACT_DUPLICATE`.

### Final Export lifecycle

- **Open:**
  - Results header and Reports heading read PROVISIONAL;
  - previews are named `…_Result_Provisional`, with a PROVISIONAL line in the
    Processing Log;
  - `SESSION_OPEN` blocks Final Export and cannot be acknowledged.
- **Close-and-export:**
  1. It checks the closure blockers, generation's preconditions and each
     chosen set's blocking readiness issues.
  2. Any blocker: listed, nothing closed, nothing exported.
  3. Otherwise: closes (sealing batches, audited) and generates.
- **Blockers:**
  - hard: a running batch, unread sheets, unresolved conflicts;
  - acknowledgeable (audited acceptance by a named operator): outstanding
    rescans, deferred sheets.
- **Closed:** each output records `scan_session_id`, `is_final` and
  `session_closed_at`; `final_export_status` reports *current*.
- **Reopen:** *stale*. Re-closing does not revive the old output; only
  regenerating does. Pre-migration outputs read as *no final export*.

### ADR-0007 and persistence

Retained and revised. The first, unrecorded design failed four of the required
properties (ADR-0007 §3). **Migration 15 is used** (additive):
- `scan_session.downstream_batch_id`, the recorded store;
- `generated_report.scan_session_id`, `is_final`, `session_closed_at`;
- four indexes.

**Combine:** refused when more than one session holds decisions, unless the
operator names whose to keep. That is audited; the others stay as history, and
the combined session is re-derived.

**Upgrade:** tested from schema-14 projects written by the schema-14 build
(`tests/fixtures/schema14`, `test_session_scope_migration`). Read-only reads
what that build showed. A writable open backs up, migrates, binds the stores
(legacy split state binds to the newer state, audited) and changes no result.

### Explicit session selection

- **Authoritative:** `services/session_scope.py` (by `scan_session_id`).
- **Pages:** `state.scan_session_id`, default
  `scan_sessions.downstream_session_id`; *Switch Scan Session*; the main
  window sets sessions.
- **Compatibility wrappers:** the batch-keyed functions of
  `reconciliation_store`, `scoring_store` and `report_store` (per-entry
  operations, existing callers), and `set_batch` on the pages.
- No `list_batches(limit=1)` caller remains, and nothing picks by `updated_at`.

### Golden one-batch regression

`tests/golden_one_batch.py` was run against `main` at `0e94d67`, verified to
import that build (schema 14, no `session_scope`). The same script on this
branch produced a **byte-identical JSON capture**:
- 22 reconciliation rows;
- 22 results;
- every cell of the Rollwise, Meritwise and Answer Key sheets of three sets.

`test_golden_one_batch` compares the values; Summary and Processing Log carry
timestamps and are left out.

### GUI, scripted

The real window was driven offscreen, 1366×768 at `QT_SCALE_FACTOR=1.75`;
screenshots are in `test-output/gui/phase4b/` (git-ignored). Verified:
- the Resolve session queue and its batch filter;
- the Attendance session heading;
- Results PROVISIONAL;
- the Dashboard count equal to the Results count (102/102);
- Final Export from an open session, offering close-and-export;
- the blocker dialog, listing closure and set-level blockers;
- a successful close with incomplete results accepted and the export: Set 3
  *Final export current*;
- after reopening: *STALE - session reopened; regenerate*.

This is scripted driving, **not** operator validation.

### Remaining limitations

**Phase 4:**
- A combine that keeps one session's decisions does not merge the other's.
  They are history.
- An output generated before migration 15 has no recorded session.
- Exact-duplicate suppression covers the Scan stage's runs. The headless
  stress runner reads every sheet, as before.
- An exact duplicate is listed on the Scan stage and in the audit ledger, not
  in a Resolve view.
- Bounded-pass cost grows with the number of person-decided identifiers in a
  session; that was not measured.
- Not operator-validated, not real-scan validated, not production-qualified.
  No power-loss testing.

**Later phases:**
- the Resolve source filter, hashing at discovery for watched sources, and
  held files at close (D);
- continuous processing and pause/finish (E);
- the operational GUI (F);
- the 100,000-sheet qualification campaign and SMB (G).

### Completion criteria P4-A … P4-O

| | Criterion | Status / evidence |
|---|---|---|
| A | One canonical effective-set service | `session_population` |
| B | Duplicates on effective identifiers | duplicate matrix tests |
| C | Bounded incremental duplicate reconciliation | `sync_duplicate_identifiers_for`; instrumented test; measurements |
| D | Exact-content duplicates session-wide at manual registration | `link_exact_duplicates`; integration and GUI tests |
| E | Resolve session-authoritative, survives reopen without Scan | Resolve GUI tests; crash case 15 |
| F | Same population for Attendance, scoring, Results, reports | identity-set acceptance; C10 test |
| G | Dashboard consumes the Results rows | `test_results_dashboard_population` |
| H | Provisional while open | Results/Reports labels; preview naming; tests |
| I | Final Export requires CLOSED; one-step close-and-export | lifecycle and GUI tests |
| J | Reopen invalidates final status; regeneration required | `test_final_export_lifecycle` |
| K | Persistent downstream scope; combine cannot silently discard | migration 15; binding and combine tests |
| L | One-batch golden regression | `test_golden_one_batch` |
| M | Phase 3 crash/reopen invariants | crash matrix 16/16; 1,000-sheet series |
| N | No image reread to reconstruct | population tests delete every image first |
| O | No later-phase scope pulled in | no intake, sources, scheduler or watched folders added |
