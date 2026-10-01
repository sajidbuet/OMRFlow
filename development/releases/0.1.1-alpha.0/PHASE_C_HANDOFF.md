# Revised phase 3 handoff — crash-safe Scan / Resolve persistence

Branch `feat/0.1.1-phase3-crash-safe-persistence`, from `main` at `1798e84`.
**Not merged, not tagged, nothing released.** This is the crash-safety part of
roadmap phase 0.1.1-B ([prompts/02-scan-sessions.md](prompts/02-scan-sessions.md)),
moved to revised phase 3 (ROADMAP.md §5.1). Decision record:
[ADR-0006](../../../docs/decisions/ADR-0006-crash-safe-scan-work-units.md).

## Baseline

| | |
|---|---|
| Starting commit | `1798e84` *Merge feat/0.1.1-phase2-scan-session-lifecycle into main* (the Phase 2 merge commit; Phase 2 tip `e0bb8e4`) |
| Version / schema | `0.1.1-alpha.0` / 14; migration 15 free (still free - none was added) |
| Focused baseline (`main`) | `tests/integration/test_batch_persistence.py` + `tests/gui/test_scan_persistence.py`: **37 passed** (2 min 04 s) |
| Full baseline (`main`) | **6,240 passed, 1 failed, 16 skipped, 4 deselected** (1 h 17 min 51 s). The failure: `test_stress_kill_resume.py::...[1000]` - *"the process never durably committed a single sheet before the deadline"* (its fixed 90 s wait). That run shared the machine with this phase's benchmarks; the same test passed in the Phase 2 baseline. Recorded as a load-dependent timeout of an unchanged test, not investigated further. |
| ruff / mypy (`main`) | All checks passed / no issues in 206 source files |

## Defect closure

**S1 — recognition persisted, review state incomplete.**
*Original:* a sheet's own conflicts were written only when a whole run ended
(`ScanPage._generate_conflicts` over the in-memory report); after a kill the
committed sheets had results and **no** conflicts, and a resume synchronised
only the sheets it read, so those conflicts were never created. The
duplicate-ID / undefined-set / re-import passes had the same window.
*Implementation:* the durable work unit - `record_results(..., template=...)`
writes the result and calls `review_store.sync_conflicts_in_session` in the
same transaction (a failure rolls the whole unit back). Batch-scope state is
`scan_recovery.complete_batch_review_state`, run before the batch leaves
`running` (run end, stop-and-exit, recovery). `recover_on_open` re-derives
per-sheet review state from `result_json` for batches not marked as written
by this Scan stage - repairing data an earlier build wrote - without reading
an image. *Tests:* `TestWorkUnit`, `TestRecoveryCompletesReviewState`
(integration); crash cases 13a/13b (real kills). *Status:* **closed**
(implemented; tested).

**S2 — interrupted work not reconstructed on reopen.**
*Original:* `on_project_changed` cleared the batch; nothing re-adopted it;
*Resume* said "nothing to resume". *Implementation:*
`MainWindow._restore_persisted_work` → `scan_recovery.restore_targets`: the
active session's newest batch, when it has unfinished members, is adopted;
the progress panel is rendered from one grouped query
(`ScanPage.show_stored_progress`) before Resume. No session, batch or
supersession is created. *Tests:* `TestScanAfterReopen` (GUI),
`TestReconstruction`, `TestLifecycleUntouched` (integration), crash cases 1-4,
14. *Status:* **closed** (implemented; tested).

**S3 — uncommitted work shown as completed.**
*Original:* a sheet was counted when a worker returned it and drawn done when
the recorder buffered it. *Implementation:* `BatchRecorder.on_commit` →
`BatchWorker._on_committed` counts it and emits `scan_done`;
`ProgressSnapshot.in_flight` ("N saving") for read-but-unsaved; a failing
store's sheets are listed at the end and named "not saved", never counted.
*Tests:* `TestRecorderCommitBoundary` (integration),
`TestCompletedCountIsCommitted` (GUI: a store held mid-commit shows 0
processed, ≥1 in flight, no row done). *Status:* **closed** (implemented;
tested).

**R1 — Resolve could not reconstruct itself.**
*Original:* Resolve held no batch until Scan navigated to it.
*Implementation:* on open, Resolve loads `restore_targets().resolve_batch_id`
(the downstream batch, else the restored Scan batch) with the project
template. Decisions were already one transaction each. *Tests:*
`TestResolveAfterReopen` (GUI), `TestResolveDurability` (integration), crash
cases 5-9 and 15. *Status:* **closed** (implemented; tested).

## Durable work unit — when a sheet is completed

A sheet is completed when **one committed transaction** holds its
`batch_scan` row (status, outcome, identifier, set code, `result_json`,
`attempt_count`) **and** every `review_conflict` that result implies for that
sheet, with their `DETECTED` / `RE_RECOGNISED` / `WITHDRAWN` events. Only then
is it counted or drawn as processed. Batch-scope conflicts (duplicate IDs,
undefined set codes, re-imports) depend on other sheets; they are complete
when the batch leaves `running`. A batch found `running` at rest is exactly
the state recovery completes.

## Commit granularity

Measured with `scripts/benchmark_commit_granularity.py`: 600 stress-dataset
sheets (seed 20261001: 544 complete, 48 review, 8 unreadable) on the suite's
synthetic answer sheet, recognised with the Scan stage's engine options (no
per-bubble measurements kept), recorded through the real work unit (result +
conflicts) on the local SSD; rollback journal, `synchronous=FULL`. Three
repetitions on an otherwise idle machine.

**Discarded measurements.** Two earlier passes are not used: the first used
`examples/templates/100_question_4_choice_example.omrt`, against which every
stress sheet fails registration (fast reads, tiny results - it produced an
"18-29 % slower" figure that does not hold); the second kept per-bubble
measurements, which the Scan stage does not store. (The Phase 10
`test_stress_kill_resume` uses that template too, so it has only ever
exercised failed sheets - recorded under discrepancies.)

Writer only (the 600 results replayed through `BatchRecorder`; a second
connection polling the batch's counts every 50 ms):

| Sheets per commit | Commit latency mean / p95 (3 runs) | Writer ceiling | Reader's worst wait | `database is locked` | DB growth |
|---|---|---|---|---|---|
| 1 | 12.0-12.8 / 18.1-20.4 ms | ≈ 78 sheets/s | 85-150 ms | 0 | 5,180 KiB |
| 25 | 50.8-84.6 ms per commit (2.0-3.4 ms a sheet) | ≈ 430 sheets/s | 29 ms (run 2) | 0 | 5,180 KiB |

Recognition alone: 19.3-21.7 sheets/s with 8 workers on this machine and
template. A per-sheet commit therefore occupies roughly a quarter of the
coordinator's time per sheet; the writer is not the bottleneck.

End to end (recognise and record as the Scan stage does), sheets/s per run:

| Policy | 4 workers (runs 2 / 3 / 4) | 8 workers (runs 2 / 3 / 4) | Mean sheets per commit | Wait to be saved, mean |
|---|---|---|---|---|
| 1 per commit | 17.6 / 18.3 / 10.4 | 26.9 / 9.2 / 20.1 | 1 | 20-47 ms |
| 25 per commit | 24.2 / 10.4 / 15.5 | 23.2 / 16.5 / 15.6 | 20-24 | 513-970 ms |
| adaptive | 24.5 / 4.2 / 17.1 | 20.8 / 19.1 / 16.5 | 1.7-3.6 | 49-314 ms |

End-to-end throughput varied by up to 6× between repetitions of the *same*
configuration, so it does **not** separate the policies; no throughput claim
is made from it. What is robust: the commit cost, the ceiling, and the
latency ordering (per-sheet < adaptive < 25-group).

**Decision.** Per-sheet commits are **practical** on this hardware. The Scan
stage uses the *adaptive* policy (`BatchRecorder(commit_when_idle=True)`): a
sheet commits alone as soon as it arrives unless less time has passed since
the previous commit finished than that commit took; then it joins the next
commit, bounded by 25 sheets / 2 s. With spaced arrivals that is exactly one
sheet per commit; the in-order release of a multi-core run delivers bursts,
which shared commits (1.7-3.6 on average). **Rationale:** per-sheet durability
wherever the writer keeps up, without a strict rule's failure mode on storage
where a commit is slower than a sheet's recognition (a share or a slow disk -
**not measured**), where it would throttle the run instead of grouping. The
GUI counts only committed sheets either way; a kill can lose only sheets never
shown as done (in a worker, or waiting for the commit in progress), and those
are re-read.

## Recovery algorithm (startup order)

`MainWindow._adopt_session`:

1. `open_project`: schema migration if needed, scan-session backfill (Phase 2),
   project lock;
2. settle the project template (`_adopt_project_template`);
3. `scan_recovery.recover_on_open(database, templates=[project template])`,
   for each batch left `running`, oldest first:
   1. `queued`/`processing` rows → `pending` (retryable; never `failed`,
      never completed);
   2. per-sheet review state re-derived from `result_json` - every row for a
      batch without the `review_state_with_results` setting, only failed
      sheets lacking their conflict for one with it; no image read;
   3. `complete_batch_review_state`;
   4. the status: `interrupted` while work remains, else
      `completed`/`completed_with_errors` from the rows - only now does the
      batch leave `running`;

   then, for any other batch with review state, failed sheets with a stored
   result and no conflict are re-derived; finally any stray stale row →
   `pending`;
4. every page adopts the project (`_broadcast_project_change`);
5. `_restore_persisted_work`: Scan adopts the unfinished batch, Resolve loads
   its batch.

No step recognises a sheet or starts a run; no step creates, seals, closes or
reopens a session or batch, records a supersession, or writes a processing
manifest. A crash during recovery leaves the batch `running`, so the next open
repeats it from the same starting point; repeating it writes nothing.

Measured cost (`scripts/benchmark_recovery.py`, 10,000 rows, 6,000 committed,
synthetic results whose 40 rolls repeat so every sheet is in a duplicate-ID
conflict - a worst case for the batch pass):

| Writer | `recover_on_open` | again (no-op) | `restore_targets` | `scan_progress` | `count_conflicts` | Resolve first page (500) | Scan list fill (`completed_results`) |
|---|---|---|---|---|---|---|---|
| this Scan stage (marked) | 10.7 s | 0.14 s | 0.12 s | 0.04 s | 0.27 s | 1.0 s | 7.0 s |
| earlier build (results only) | 42.6 s (6,000 re-derived, once) | 0.05 s | 0.05 s | 0.02 s | 0.17 s | 2.2 s | 7.1 s |

Counts are grouped queries; the one unbounded read on reopen is the Scan
list, which loads every stored result (pre-existing `adopt_batch`
behaviour). See *Known limitations*.

## Crash matrix

Real child processes (`tests/crash/scan_resolve_child.py`: the real main
window offscreen, real Scan and Resolve stages, a 2-process worker pool unless
stated) killed with `Process.kill()` (`TerminateProcess`) from the test
process; Phase 10's `kill_run_abruptly` (coordinator only, orphan check) and
`integrity_report`. Evidence: the child's evidence log (written and flushed
before each boundary is passed) and the database read-only after the child is
gone. Kill points are states, never sleeps. Dataset: 40 stress-dataset sheets,
seed 42 (blank/multiple/faint/ambiguous marks, skew, an ambiguous and a
duplicate Student ID, a byte-identical duplicate, three sets, one unreadable
file). "Re-read" = sheets committed before the kill that a later run
submitted to recognition (from the submission log); every case also asserts
`attempt_count == 1` on every row (an independent, database-side witness).

Figures from the final full-suite run (`tests/crash/test_crash_matrix.py`,
evidence written via `OMRFLOW_CRASH_EVIDENCE`); every case had also passed in
development runs before it.

| # | Case | Kill point (state) | Scale | Committed at kill | Retried by resume | Re-read | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | Clean close halfway through Scan | Operator Cancel + stop-and-exit after 20 commits | 40 / 2 w | 20 (rest `cancelled`, none `failed`) | 20 | 0 | **Pass** - reopened *20 / 40 recognised · 20 pending*; lock released |
| 2 | Forced kill halfway through Scan | Free-running kill once ≥ 20 commits were logged (sheets in the workers) | 40 / 2 w | 20 | 20 (exactly the uncommitted set) | 0 | **Pass** |
| 3 | Kill while a sheet is in the worker | Paused as `sheet_00005.png` entered the single worker | 40 / 1 w | 5 | 35 (incl. that sheet) | 0 | **Pass** - the sheet was `queued`, became `pending` (not failed, no result), read once on resume |
| 4 | Kill after a commit, before the next sheet | Paused right after the 7th commit | 40 / 1 w | 7 (= exactly what the log saw) | 33 | 0 | **Pass** - last committed sheet not resubmitted |
| 5 | Clean close halfway through Resolve | Clean close after 3 decisions | 40 (6 conflicts) | 3 decisions | - | - | **Pass** - 3 ledger events; queue = the other 3 |
| 6 | Forced kill after several corrections | Paused after the 3rd committed decision | 40 (6 conflicts) | 3 corrections | - | - | **Pass** - all 3 `CORRECTED` events, in order |
| 7 | Reopen retains corrections | (case 6's project) | | 3 | - | - | **Pass** - machine value kept, effective = override `9`, one human event each |
| 8 | Unresolved stay unresolved | (case 6's project, 3 reopens) | | - | - | - | **Pass** - queue = exactly the 3 unresolved, each reopen |
| 9 | Repeated restarts | 3 reopens after the kill | | - | - | - | **Pass** - snapshot identical: 6 conflicts, 12 audit rows, 1 session, 1 batch, 0 supersessions, 40 scans; reconciliation/result/rejection/report/history tables unchanged |
| 10 | Results after interruption | Two kills, at 13 and 26 committed (cumulative) | 40 / 2 w | 14, then 26 | 14 | 0 | **Pass** - per-sheet results (status + full result JSON minus timings), conflicts, effective IDs and set codes, reconciliation counts and all 43 `candidate_result` rows equal the uninterrupted reference |
| 11 | Kills at 1/25/50/75/99 % | One project killed at each point in turn, resumed between | 40 / 2 w | 1, 10, 20, 30, 39 | 39, 30, 20, 10, then 1 | 0 at every step | **Pass** - final semantics equal the reference |
| 11 (stress) | Same, at scale (`-m stress`) | as above | **1,000 / 4 w** | 11, 250, 502, 752, 990 | 989, 750, 498, 248, then 10 | 0 at every step | **Pass** (3 min 34 s, final code) - final semantics equal a 1,000-sheet uninterrupted reference |
| 12 | Integrity | Straight after a kill at 12 commits, before any reopen | 40 / 2 w | ≥ 12 | - | - | **Pass** - `quick_check`/`integrity_check` `ok`, no FK rows, no health error; `BATCH_LEFT_RUNNING` + `STALE_PROCESSING_JOBS` before the reopen, gone after |
| 13a | Recognition committed, batch-scope review not yet | Paused after every result committed, before the batch pass | 40 / 2 w | 40 | 0 (reopen recognises nothing) | 0 | **Pass** - 0 duplicate-ID conflicts at the kill, 4 after the reopen; all conflicts = the reference; a second reopen changed nothing |
| 13b | An earlier build's results without conflicts | Pre-phase-3 write path (`legacy_scan`) paused after 20 commits | 40 / 2 w | 20, **0 conflicts** | 20 | 0 | **Pass** - the reopen created exactly the reference's conflicts for those sheets (1), none detected twice; after resume all conflicts = the reference |
| 14 | Sealed batch interrupted | Clean stop at 8, session closed (seals) and reopened, then killed while resuming at 18 commits | 40 / 2 w | 18 | 22 | 0 | **Pass** - same batch, `sealed_at` unchanged, 40 members, nothing added |
| 15 | Resolve reachable after reopen | (case 6's project) | | - | - | - | **Pass** - opened on its batch without Scan: *6 total · 3 unresolved · 3 resolved*, queue of 3 |

"Committed at kill" is read from the database after the kill; in every case
the child's logged commits were a subset of it.

Every case also asserted, after recovery: `quick_check` = `ok`,
`integrity_check` = `ok`, `foreign_key_check` empty, **no Project Health
error**, no duplicate conflict identity, no conflict detected twice, exactly
one session, one batch, no supersession, 40 scan rows. Health findings left
were the expected warnings `UNRESOLVED_CONFLICTS`, `SETS_WITHOUT_A_VERIFIED_KEY`
and `NO_BACKUPS` (plus `STALE_PROCESSING_JOBS` and `BATCH_LEFT_RUNNING`
straight after a kill, before recovery - case 12).

## Resolve persistence

Each decision (accept, correct, field edit, defer, reopen, undo) is one
transaction with its audit event (Phase 6; unchanged). On reopen Resolve is
loaded directly; the summary (*N total · U unresolved · R resolved*) and the
queue come from `count_conflicts` / `list_conflicts` over persisted rows. After
a kill following three corrections (case 6): all three `CORRECTED` events
present in order with reviewer *Crash Harness*; machine value unchanged;
effective value the override; one human event per conflict (case 7); the
queue equals exactly the unresolved conflicts on three successive reopens
(case 8); three reopens changed no row and no ledger entry (case 9). Not
kept: the selected row and the in-memory redo list (ARCHITECTURE_NOTES R3).

## Integrity

SQLite checks and Project Health after every crash case: as above, all clean.
New Project Health findings (bounded aggregate queries):
`BATCH_LEFT_RUNNING` (warning), `SCAN_COMPLETED_WITHOUT_RESULT`,
`SCAN_REVIEW_STATE_MISSING`, `REVIEW_STATE_WITHOUT_RECOGNITION`,
`CONFLICT_DETECTED_TWICE` (errors) - each shown to fire on a constructed bad
state and to stay silent on a cleanly processed batch. The audit ledger stays
append-only (migration-3 triggers unchanged); recovery appends events only for
conflicts it actually creates.

## Tests

| File | Tests | Kind |
|---|---|---|
| `tests/integration/test_crash_safe_persistence.py` | 28 | headless: work unit atomic and rolled back together; recognition-only path unchanged; `on_commit` reports only committed sheets; failing store commits nothing; adaptive policy one-by-one when spaced, coalescing a backlog within the bound; conflicts an earlier build never wrote created exactly once; marked batch not re-derived but completed; batch-scope state after a crash; recovery never recognises; missing template recovers status and warns; repair of a failed sheet without its conflict; in-flight → pending; recorded failures stay failures; no manifest written; no new session/batch/supersession; sealed stays sealed; closed stays closed; repeated recovery identical; progress from rows; restore targets (unfinished / finished batch); Resolve decisions across a reopen; Project Health findings fire and stay silent; withdrawn-conflict re-sync regression; stored results round-trip for re-derivation |
| `tests/gui/test_crash_reopen_gui.py` | 8 | Scan shows the interrupted batch from committed rows before Resume; same session and batch; Resume reads only the uncommitted; Process All on a restored batch skips the committed; sealed batch shown sealed and resumes its members; Resolve opens with decisions and the exact queue without Scan; read-but-uncommitted is in flight, not processed; failing store counts nothing ("not saved") |
| `tests/crash/test_crash_matrix.py` | 16 (+1 `stress`) | real-process kill / restart matrix above |
| **Total new** | **52** in the default run, **+1** `stress` | |

No existing test was edited.

Final runs (branch, after the last code change `f7a76a8`):

| | Result |
|---|---|
| pytest (full, worktree) | **6,280 passed, 29 skipped, 0 failed**, 5 `stress` deselected (56 min 18 s) |
| the 13 extra skips | `tests/local/test_real_marked_sheets.py`, whose real-sheet fixture (`Scratch/Project1`, untracked) exists only in the main checkout; run there against this branch's `src`: **13 passed** (`tests/local` is unchanged on the branch) |
| ⇒ equivalent | **6,293 passed, 16 skipped, 0 failed** = baseline 6,241 + 52 new; the same 16 environment skips |
| `-m stress` crash series | 1 passed (1,000 sheets, 3 min 34 s) |
| `tests/gui` alone (during development) | 1,839 passed, 2 skipped |
| ruff | `ruff check src tests tools scripts`: All checks passed |
| mypy | no issues in 208 source files |

The baseline's one failure (`test_stress_kill_resume[1000]`, its 90 s deadline
under load) passed re-run alone on `main` (2 passed, 1 deselected) and in the
branch's full run.

## Validation tracks

```text
Implemented:               Yes - branch feat/0.1.1-phase3-crash-safe-persistence, not merged.
Automated tests:           Passing (above).
Crash/restart validation:  Real-process kills, 15 matrix cases at 40 sheets;
                           1/25/50/75/99 % series at 40 and at 1,000 sheets.
Synthetic validation:      The crash runs above on the stress dataset; results,
                           conflicts, effective IDs/set codes, reconciliation and
                           scores after interruption equal an uninterrupted run
                           (case 10, 40 sheets).
Network-share validation:  Not performed.
Real-scanner validation:   Not performed.
Production qualification:  Not performed.
```

Rendered inspection (not operator validation): a project produced by a real
kill at 45 / 120 committed and a second kill after two Resolve decisions,
reopened in a visible window at 1366×768 and devicePixelRatio 1.75 (the
machine's native 175 % scaling): the Scan stage listed the batch with its
results and read *"Batch interrupted - press Resume to read the 75 remaining
scan(s)." / "45 / 120 recognised · 1 failed (retryable) · 75 pending"*, bar
37.5 %, Resume enabled; Resolve opened on the batch with *7 total · 5
unresolved · 2 resolved* and five queued conflicts. The progress readout sits
below the fold of the Scan stage's left column at this window size (existing
layout, not changed) and wraps onto two lines without clipping. Screenshots:
`test-output/gui/phase3/` (git-ignored).

## Power loss

**Actual power removal was not tested.** Durability across power loss is
*inferred* from SQLite's transaction semantics under OMRFlow's settings -
rollback journal (`DELETE`), `synchronous=FULL` (SQLite's default; not
overridden), foreign keys on - and from the process-kill tests, and holds only
as far as the storage honours flushes. A synchronised folder or network share
weakens it (ADR-0002); SMB is revised phase 10. A process kill is not a power
loss.

## Known limitations

* **Reopening fills the Scan list from every stored result** (`adopt_batch` →
  `completed_results`, pre-existing): 7 s at 6,000 committed rows here;
  roughly linear, so minutes at 100,000. Counts and the Resolve page are
  bounded; the list model is not paged (revised phase 8).
* Recovery re-derives every stored result of a batch an **earlier build**
  wrote (≈ 5 ms a row here - 42.6 s against 10.7 s for 6,000 rows - once); batches this Scan stage registers skip
  it. A batch registered by a recognition-only tool (stress CLI) is also
  re-derived on its first crash-open in the GUI, gaining the conflicts its
  writer never produced.
* Schema stays 14, so a Phase 2 (schema-14) build can still open a project
  this build wrote and would write results without conflicts into a batch
  marked `review_state_with_results`; recovery would then skip re-deriving
  them. Phase 2 was never released.
* In single-worker runs a sheet being committed is shown neither processed
  nor *saving* for that commit's duration (the read is reported after the
  result is offered to the store).
* `processing` is never written: a sheet in a worker is `queued` in the
  database (pre-existing).
* The headless stress CLI (`tools/benchmark_stress.py`) still records
  recognition only, as before.
* The child process leaves via `os._exit` after the application's own close
  path: the offscreen Qt teardown at interpreter exit aborted it
  (`0xC0000409`, `0xC0000005`) after the project was released while the
  harness was built. Not investigated; not observed in the GUI test suite.
* One `tests/gui -x` run during development appeared stalled for > 45 minutes
  while three other test/benchmark runs shared the machine; it was killed and
  a full verbose re-run passed (1,839 passed, 2 skipped). Cause not
  determined.
* The 40-sheet dataset raises only six conflicts; case 5-9 exercise three
  decisions each. The 1,000-sheet series covers Scan only.
* Report workbook cell values were **not** compared in case 10 (the result
  template's roster rows would need matching synthetic rolls); reconciliation
  counts and every `candidate_result` row were.
* Multi-batch sessions are still downstream-single-batch (phase 4); Resolve on
  reopen shows the downstream batch, not a session queue.

## Roadmap / code discrepancies

1. ARCHITECTURE_NOTES §13.4 says "`PROCESSING` itself is never written"; still
   true - in-flight is `queued`. Case 3 therefore checks `queued` → `pending`.
2. The Phase 10 kill test uses `100_question_4_choice_example.omrt`, against
   which every stress sheet fails registration - it has never exercised a
   successfully read sheet. Not changed here.
3. ACCEPTANCE §5.4 case 10 says "session-level results"; until phase 4 that is
   the single downstream batch, which is what was compared.
4. The prompt's suggested open order puts recovery before template settling;
   it runs after, because recovery re-derives conflicts against the template.
5. The wiki recovery page claimed safety under power loss; corrected.
6. A defect outside the four: stop-and-exit (`ScanPage.shutdown_batch`)
   skipped the batch-scope passes; fixed. An idempotence defect in
   `sync_conflicts` (`is` on a string) re-withdrew withdrawn conflicts on every
   re-sync; fixed.

## Phase 4 starting point

> Phase 4 may now build the authoritative session-level effective scan set,
> reconciliation, scoring, Results and Reports on top of crash-safe persisted
> Scan/Resolve state.

Schema 14; next migration **15**. Keep `record_results(..., template=...)` as
the only Scan write path for sheets that Resolve will see, and run any new
batch- or session-scope review pass from `complete_batch_review_state` (or its
session-level successor) **before** a batch leaves `running`.
