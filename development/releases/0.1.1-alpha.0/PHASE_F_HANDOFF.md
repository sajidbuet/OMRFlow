# Revised phase 6 handoff — continuous-processing engine (roadmap 0.1.1-E, first part)

> Naming: handoffs here are lettered in revised-phase order (A = revised 1 …
> E = revised 5), so revised phase 6 is `PHASE_F_HANDOFF.md`. It implements the
> **first part of roadmap phase 0.1.1-E** (unit scheduler, writer strategy,
> restart sequence - ROADMAP §5.1), not roadmap phase F (the operational GUI).

## 1. Status

| Track | Status |
|---|---|
| Implemented | **Yes** - headless continuous engine, durable claims, bounded warm pool, single coordinator writer, restart sequence, stop primitives, status snapshot, Health checks |
| Automated tests | **Passing** (exact counts in §22) |
| Synthetic validation | Deterministic fault injection at 8 boundaries, real process kills at 6 boundaries, 1/25/50/75/99 % restart series, repeated-restart torture, a 3,000-file endurance run - local disk and fake filesystem. **Not** the phase 9 synthetic intake campaign |
| Network-share validation | **Not performed** |
| Real scanner validation | **Not performed** |
| Power-loss testing | **Not performed** (process kills only) |
| Production qualification | **Not performed** |

Precise wording: *the Phase 6 continuous-processing architecture is
implemented and passes automated crash/restart, multi-batch and
bounded-processing tests.* It is headless: nothing in the GUI starts it.

## 2. Branch

* Branch `feat/0.1.1-phase6-continuous-engine`, worktree
  `C:\Research\OMRflow-p6`. Pushed; **not merged, not tagged, nothing
  released.**

## 3. Baseline and tip

* Baseline: `main` at `208367f` (*Merge chore/github-social-preview*), which
  contains the Phase 5 merge `59ba8df` (Phase 5 tip `8c3b34f`).
* Phase 5 presence verified before any change: schema 16; `intake_source`,
  `intake_source_attachment`, `intake_file`; `batch_scan.intake_file_id` /
  `registered_at`; `scan_batch.source_id`; `IntakeService.ready_items` /
  `register`; verified project copies for watched files; manual-source
  convergence; the ledger state machine; phase 4 duplicate handling.
* Tip and commit count: §23.

## 4. Schema

**Before 16, after 16. No migration.** `batch_scan.status = 'processing'`
already existed as a value (`ScanJobStatus.PROCESSING`) and is now written. A
unit's provenance goes in `scan_batch.settings_json`
(`"review_state_with_results": true`, `"registered_by": "continuous_engine"`).
Consequently no upgrade test, no pre-migration backup step and no data-model
table change were needed; `docs/DATA_MODEL.md` and
`docs/wiki/Upgrade-Compatibility.md` say so explicitly.

## 5. Checkpoint A - architecture audit (before code)

| Question | What the code did at `208367f` |
|---|---|
| Scan worker pipeline | `ScanPage._start_batch`: `start_batch` -> `mark_queued` (run rows `queued`) -> batch `running` -> `BatchWorker` (QThread): `intake.record_manual_batch` (hash + manual ledger) -> `link_exact_duplicates` -> `process_batch` (`parallel_batch.recognise_in_parallel`, a `spawn` pool per call, 4 tasks queued per worker) |
| DB writer | `BatchRecorder` on the worker thread (adaptive grouping, `on_commit` reports only committed sheets - phase 3 S3) |
| Per-sheet transaction | `record_results(..., template=)`: result row + that sheet's conflicts in one transaction (phase 3 work unit) |
| Worker / DB ownership | workers return `ScanResult`s only; the coordinator writes |
| Crash persistence | `scan_recovery.recover_on_open`: stale `queued`/`processing` -> `pending`, re-derive review state, `complete_batch_review_state`, status from rows; idempotent |
| Duplicate / conflict sync | batch-scope pass `complete_batch_review_state` (re-imports, bounded session-wide duplicate IDs `sync_duplicate_identifiers_for`, undefined set codes) before a batch leaves `running`; already moved out of `ScanPage._generate_conflicts` in phase 3 |
| Cancellation | cooperative flag; queued futures cancelled; running sheets finish; `mark_cancelled` |
| `PROCESSING` | defined, never written (in-flight = `queued`) |
| `busy_timeout` | not set explicitly (Python `sqlite3` applies 5 s implicitly) |
| Effective set | `session_population.session_population(...).effective` - the canonical helper; reused, no new one |

Reused rather than rebuilt: `record_results`, `complete_batch_review_state`,
`finalise_batch`, `recover_on_open`, `record_manual_batch`,
`link_exact_duplicates`, `check_compatibility`, the worker entry points, the
intake service, the effective-set service, the phase 3 crash harness.

## 6. Architecture implemented

```text
IntakeService.reconcile / ready_sources --plan_units--> IntakeService.register
                                                         (one source; sealed unit)
ContinuousEngine.step():
  Recogniser.poll -> buffer           (results; workers never write)
  record_results(template, claimed_only=True)   (work unit, one txn per group)
  acknowledge                          (count, report - only now)
  unit finished? complete_batch_review_state -> finalise_batch
  claim_scans (pending -> processing + batch running, one txn) -> Recogniser.submit
```

* `domain/processing.py` - `EngineState`, `EngineLimits`, `UnitPolicy`,
  `plan_units` (pure).
* `services/recognition_pool.py` - `Recogniser` protocol, `ProcessRecogniser`
  (warm `spawn` pool, the finite path's worker functions, drain-and-replace
  recycling, worker death reported per ticket), `InlineRecogniser`.
* `services/continuous_engine.py` - `ContinuousEngine` (`start`,
  `poll_intake`, `form_units`, `step`, `run_until_idle`, `run`,
  `pause_intake`/`resume_intake`, `pause_scheduling`/`resume_scheduling`,
  `cancel_queued`, `shutdown`, `status`), `claim_scans`, `release_claims`,
  `EngineHooks`, `EngineStatus`.
* `batch_store.record_results(claimed_only=)` (compare-and-set; returns rows
  written), `IntakeService.ready_sources` (shared readiness predicate
  `_ready_for`), `database.engine.BUSY_TIMEOUT_MS`,
  `project_health._processing_issues`.
* ADR-0009.

## 7. Scheduler semantics

* **Unit formation:** sources ordered by their oldest ready file
  `(ready_at, intake_file_id)`; a source forms a unit of its oldest
  `max_unit_size` ready files as soon as that many are ready, or of whatever it
  has once its oldest has waited `trickle_seconds`; a remainder after a full
  unit is judged at the next pass; at most `max_units_per_poll` units per pass.
  Defaults 200 / 30 s / 1 - starting values, **not validated**.
* **Claim order:** the session's runnable batches by `(created_at, batch_id)`,
  then `batch_index`. Runnable = in this session, not superseded, compatible
  with the engine's template/geometry/recognition settings/engine version
  (`check_compatibility`), and not `running` under another coordinator.
  Skipped batches are reported with the reason (`EngineStatus.skipped_batches`).
* **Durable order** does not depend on completion order: results are matched
  to rows by path within their batch; `test_out_of_order_completion_*` reverses
  completion order and gets an identical durable state.
* A batch intake did not register (manual, *Reprocess All*) first gets the
  finite worker's registration step (`record_manual_batch` +
  `link_exact_duplicates`), once.

## 8. Worker / writer ownership

One coordinator thread - whichever calls the engine - performs every
processing write (claims, commits, releases, unit passes, intake
reconciliation and registration). Workers (processes) never open the database
(`test_worker_side_modules_never_reach_the_database`). A concurrent Resolve
decision on another thread waits on SQLite's lock; `PRAGMA busy_timeout =
5000` set explicitly. A writer queue was rejected (ADR-0009 §2).

## 9. Processing state machine

```text
pending / cancelled -> processing              claim (compare-and-set, one txn with batch running)
processing -> completed | warning | failed     work unit commits
processing -> pending                          released: cancelled before start, worker died
                                               (once), non-draining stop, restart recovery
duplicate                                      decided at registration; never claimed
```

`failed` stays a recorded recognition failure, retryable on request, exactly as
before. No new state.

## 10. Durable transaction boundary

A sheet is completed when one transaction has committed its row (status,
outcome, identifier, set code, `result_json`, attempt count) **and** every
conflict its result implies, with their audit events - written only onto a row
still `processing`. Only then is it counted or reported. Cross-sheet
(batch-scope) review state - re-imports, session-wide duplicate Student IDs,
undefined set codes - completes when the unit has nothing unfinished, before it
leaves `running`; a kill in between leaves the unit `running`, which recovery
completes (ADR-0006, unchanged). **This is the "equivalent crash-safety"
option, not one transaction:** for up to one unit's processing time a committed
sheet's *cross-sheet* duplicate conflict may not exist yet.

Both directions are tested: a failure inside the work unit after the row was
flushed (`test_failure_inside_the_work_unit_rolls_back_result_and_conflicts`)
leaves neither row nor conflict; a real kill inside the transaction
(`test_kill_at_a_boundary[in_commit]`, hot journal) leaves neither.

## 11. Recovery behaviour

`start()` = `recover_on_open` (stale claims -> `pending`; units left
`running` get review state completed and leave `running`) then intake recovery
(`IntakeService` construction). Same session, same batches, same membership;
no session, batch or supersession is created (`test_a_crash_creates_no_new_batch`,
matrix assertions). Calling it twice changes nothing (torture test). A sheet
committed before a kill is never submitted after it (submission log against the
database, deterministic and real-process).

## 12. Backpressure limits

`EngineLimits`: `max_in_flight` (default 16; `for_workers(n)` = 4 per worker),
`claim_window`, `max_commit_group` (25), `max_units_per_poll` (1),
`writer_retry_limit` (3), `infrastructure_retries` (1). In-flight bounds the
futures, the result objects and the writer backlog together. Measured: 1,200
pending behind a stuck recogniser - exactly 8 submitted, 8 `processing`, 1,192
`pending`; after release all processed with in-flight never above 8; slow
writer (10 ms extra per commit) - backlog never above 4, groups never above 2,
60/60 committed; traced peak memory over 40 steps with 2,000 pending is
asserted below twice that with 100 pending plus 2 MB
(`test_memory_follows_the_bounds_not_the_backlog`; Python allocations traced by
`tracemalloc`, not process RSS).

## 13. Cancellation and shutdown contract

`pause_intake`, `pause_scheduling` (in flight finishes and commits),
`cancel_queued` (queued sheets withdrawn, claims released), `shutdown(drain,
timeout)`: intake and scheduling stop; queued sheets released; with `drain` the
running sheets finish and commit, else they are released; every unit touched
leaves `running` (`completed`/`completed_with_errors`, or `interrupted` without
a manifest when work remains); the pool closes. **After a clean shutdown each
row is committed, `pending` or `duplicate`; nothing claimed remains.** Exception:
`FAULTED` (writer failing - including results that still cannot be saved at
shutdown) leaves claims for recovery and says so in its state. Tested with an empty
queue, queued work, running work (drain and no-drain), and pending writer
results; real clean stop in `test_clean_stop_leaves_nothing_claimed_and_resumes`.
Operator policy (*Finish current*, *Cancel queued* confirmations, persisted
flags) is phase 7.

## 14. Incremental conflict synchronisation

Each unit's batch-scope pass re-derives the duplicate-ID groups its sheets
belong to across the whole session's effective set (phase 4's bounded pass),
so a later unit raises a duplicate on an earlier, already finished unit's
sheet; human-touched conflicts are kept; untouched ones withdrawn when they no
longer apply. Tested: late duplicate across sources/units and restarts; a
resolved duplicate accepted, two further restarts, still resolved with one
`detected` event; operator decisions survive a kill mid-later-unit with
everything else equal to the uninterrupted run. No second conflict system.

## 15. Supersession / effective set

The engine never claims from a superseded batch; effective counts come from
`session_population` only. *Reprocess All* of a finished unit: the reprocess
batch is read once (its sheets hashed and duplicate-linked first), the
superseded unit's rows are kept, the effective count is unchanged across two
further restarts; a rejection lowers it by one and a restart does not change
that (`test_rejection_and_reprocess_count_each_sheet_once`). Phase 6 makes no
replacement decision (phase 7).

## 16. Finite-workflow compatibility

The Scan stage, `BatchWorker`, `BatchRecorder` and `process_batch` are
unchanged; `record_results`' new parameter defaults off. Equivalence: the same
40 sheets through headless *Add Folder -> Process All* (the Scan stage's exact
sequence) and through the engine (8-sheet units over three sources) give equal
results (normalised `result_json`, status, outcome, identifier, set code,
attempts), effective set, conflicts and unread duplicates; only `source_name`
differs (phase 5's content-addressed copy name for a watched file - ADR-0008).
Attendance entries and scores per set are equal too. The finite GUI suites
pass unchanged in the full run (§22).

## 17. Tests added

| File | Tests | Covers |
|---|---:|---|
| `tests/unit/test_processing_rules.py` | 19 | `plan_units` (full, trickle, backlog, stable source order, per-pass limit), limit validation, claim order and compare-and-set, release, X3 architecture |
| `tests/integration/test_continuous_engine.py` | 7 | two sources -> sealed units; arrivals form new units, sealed unit never grows; caught-up is not session-complete; §29 three-source scenario with late duplicate and restart; status snapshot and idempotent start; the run loop on a thread; no INFO logging on idle steps |
| `tests/integration/test_engine_recovery.py` | 24 | kill at 8 boundaries vs control; §24 commit-then-death; §25 claim-then-death; §63 never resubmitted; §64 no new batch; 1/25/50/75/99 % single interruptions and the series in one project; seeded torture (8 cycles, ≥4 kills); writer failure inside the unit; writer fault and recovery; Resolve decisions across restarts; late duplicate not resurrected; duplicate content never read |
| `tests/integration/test_engine_backpressure.py` | 14 | stuck worker, memory bound, slow writer, out-of-order completion, recognition fault, lost worker, shutdown x6 (incl. a writer still failing at shutdown -> `FAULTED`), pause/cancel, superseded and foreign-template batches |
| `tests/integration/test_engine_compatibility.py` | 5 | finite equivalence, partitioning equivalence (1 unit / 10 / 3 sources / arrivals / restart between units), Attendance and scores vs finite, rejection and reprocess effective counts, source outage |
| `tests/integration/test_engine_contention.py` | 2 | `busy_timeout` set; engine + concurrent Resolve decisions |
| `tests/integration/test_engine_process_pool.py` | 3 | warm pool = inline, one pool across 3 units; killed worker process loses nothing; recycling; no orphan processes |
| `tests/integration/test_engine_health.py` | 4 | clean run silent; running engine only warnings; the two new findings fire |
| `tests/crash/test_engine_kills.py` | 8 | real kills (below) |
| `tests/integration/test_engine_endurance.py` (`stress`) | 1 | §20 below |
| **Total** | **86** + 1 `stress` | |

Shared rig: `tests/engine_rig.py`; real-kill child: `tests/crash/engine_child.py`.
No existing test was edited.

## 18. Crash tests

**Deterministic** (`test_engine_recovery.py`; a hook raises at the boundary,
open transactions roll back, the engine object is abandoned): `registered`
(2nd unit), `claimed`, `submitted`, `recognised`, `before_commit`, `committed`
(the acknowledgement-lost case), `finalising`, `finalised` - each restarted and
compared with an uninterrupted control by content; writer failure inside the
work unit (after the row flush, before conflicts) for case 6/7.

**Real process kills** (`tests/crash/test_engine_kills.py`; `TerminateProcess`
via Phase 10's `kill_run_abruptly`; 30 real sheets over two real folders; real
`spawn` workers or in-process where a boundary needs one sheet at a time): after
claims committed (before submission), with a result in memory, **inside the
work-unit transaction** (result rows flushed, conflicts and commit not yet - a
hot journal on reopen), after commit before acknowledgement, at unit
finalisation, free-running with sheets inside the workers, a clean drain stop,
and three successive kills while units were still being registered. Every case:
outcome equals the control child's; no committed sheet submitted after its
kill; no orphaned worker; `quick_check`/`integrity_check`/`foreign_key_check`
clean; Health has no error; same session, units kept with their membership, no
supersession. Restarts pass `--force-lock` (the killed coordinator's lock is
reclaimed by an explicit decision, as in the application and in phase 3's
harness).

**Progress points:** 100 sheets over three sources, kills after 1, 25, 50, 75
and 99 % of the control's committed sheets, each in its own project and all
five in one project; each converges on the control and re-reads nothing
committed.

## 19. Endurance

`test_engine_endurance.py` (`stress`, default 3,000 files, 10 waves): three
sources on the fake filesystem (real copy store on disk), small distinct
images, recognition replayed from 60 real results (Student IDs repeat every 60
- a heavy duplicate-group load), a seeded kill at a random boundary after every
wave. Result (this machine, §21): **control 3,000 files, 60 units, 0 kills,
59.3 s (50.6 files/s); killed run 60 units, 7 kills, 62.5 s (48.0 files/s);
max in flight 16 (= the bound); database 33.9 MiB; final outcome equal to the
control; no sheet re-read after its commit; integrity and Health clean.**
These rates measure the coordinator plus intake with *replayed* recognition,
not OMR throughput.

## 20. Performance (real recognition)

`scripts/benchmark_engine.py --sheets 400 --workers 8 --unit 50 --repeat 3`;
AMD Ryzen 9 5900HS (AMD64 Family 25 Model 80, 8 cores / 16 threads), Windows 11
10.0.26200, Python 3.12.7, OpenCV 5.0.0, local NVMe SSD; stress-dataset seed
42 decodable sheets on the suite's synthetic answer sheet; Scan-stage engine
options.

| Path | sheets/s, runs 1 / 2 / 3 |
|---|---|
| Finite *Process All* (one batch) | 24.4 / 26.1 / 28.1 |
| Engine, 8 pre-registered sealed units | 25.3 / 24.4 / 25.8 |
| Engine with live watched intake (stabilise, copy, register, read) | 19.1 / 17.5 / 19.2 |

Pool start-up to first result 2.30-2.75 s; steady 53-58 ms per sheet across
the 8-worker pool. One warm pool served all 8 units (`pools_started == 1` is
asserted in `test_warm_pool_matches_in_process_reading`). The live-intake
figure is lower because stabilisation (0.2 s quiet period here) and
copy-ingest/registration (fsync per copy) run on the same coordinator thread.
Profiling the endurance run: about 14 ms of coordinator time per sheet for
claim, commit and unit passes. Desktop end-to-end throughput varies (phase 3
saw up to 6x run to run); these are three runs on an otherwise idle machine,
not a qualification.

## 21. Hardware for every figure here

AMD Ryzen 9 5900HS, 16 logical CPUs, 31 GB RAM, NVMe SSD (NTFS), Windows 11
Education 10.0.26200, Python 3.12.7, OpenCV 5.0.0, SQLite as bundled with
Python 3.12.7.

## 22. Test results

| Run | Result |
|---|---|
| Baseline full suite, worktree at `208367f` (PYTHONPATH on the worktree `src`, native Qt platform) | **6,723 passed, 29 skipped, 0 failed**, 6 `stress` deselected (1 h 03 min 36 s). Equal to phase 5's recorded final count. Note: the run started before any phase 6 edit, but the first two back-compatible source edits (`record_results(claimed_only=)`, `ready_sources`) landed in the worktree while it ran |
| **Final full suite** at `f0b8609` (every source and test change except the last fix) | **6,808 passed, 29 skipped, 0 failed**, 7 `stress` deselected (1 h 09 min 21 s) = 6,723 + 85 new |
| At the tip, after the last fix (`03a0ec4`, shutdown -> `FAULTED`, +1 test): every new engine file, the architecture test, `test_crash_safe_persistence`, `test_intake_ledger` | **200 passed**, 0 failed (9 min 13 s) |
| At the tip, `-m stress tests/crash tests/integration/test_intake_soak.py tests/integration/test_engine_endurance.py` | **3 passed** (7 min 08 s): phase 3's 1,000-sheet kill series, phase 5's 2,400-arrival soak, the 3,000-file engine endurance (7 kills; 48.8 / 45.5 files/s) |
| `tests/local` from the main checkout against the branch's `src` | **13 passed, 10 skipped** (as phase 5 recorded) |
| Real-kill file `tests/crash/test_engine_kills.py` | 8/8 in three separate runs (development, the full suite, the tip run) |
| `-m stress tests/integration/test_engine_endurance.py` (3,000 files) | **1 passed** (2 min 20 s) |
| `ruff check src tests tools scripts` | **All checks passed** |
| `mypy src/omr_scanner` | **Success: no issues found in 219 source files** |

Skips (29, unchanged from the baseline): 23 `tests/local` real-sheet tests
whose untracked fixtures exist only in the main checkout; the rest
environment-dependent (window-manager size, symbolic-link privilege, a
schema-12 checkout variable, LibreOffice absent, a template that prints every
digit, shared qtguitesting plumbing).

## 23. Commits

Baseline `208367f`. 15 commits on the branch: 14 implementation / test / documentation commits listed by `git log --oneline 208367f..` (last code change `03a0ec4`), plus the documentation commit that adds this handoff's final numbers, which is the branch tip (its SHA is in the final report).

## 24. Defects found

* **Found in this phase's own design, fixed before merge (`61a2ce4`):** a batch
  not registered by intake (manual, *Reprocess All*) carries no content hash;
  without the finite worker's registration step the engine would have read its
  exact duplicates. The engine now runs `record_manual_batch` +
  `link_exact_duplicates` once per such batch.
* **Test-harness defects found and fixed** (not product defects): on Windows a
  virtual environment's `python.exe` is a launcher, so `Popen.pid` is not the
  interpreter that writes the evidence log - the first version of the kill
  test therefore misattributed submissions; the first endurance loop
  reconciled intake on every step (492 listings per run) and under-reported
  throughput; the first benchmark waited for scan rows, which an in-unit exact
  duplicate never gets (hung; fixed to settle on the ledger).
* **Pre-existing behaviour noted, not changed:** a killed coordinator leaves
  the project lock; reopening requires the explicit *remove lock* decision.
* No pre-existing failing test.

## 25. Deviations from the roadmap / prompts

1. ARCHITECTURE_NOTES §11 says "one unit processes at a time". The engine
   claims across units in order, so a warm pool may hold the tail of one unit
   and the head of the next; each unit is still finalised on its own and is
   still the granularity of recovery. Reason: a pool drained at every unit
   boundary idles workers for nothing.
2. Prompt 05 item 3 asks for `PROCESSING` "when a sheet enters a worker". The
   engine writes it at the **claim**, immediately before submission to the
   bounded pool queue (at most 4 per worker) - a durable record that precedes
   the worker. The finite Scan stage still marks its run `queued` (unchanged;
   recovery treats both alike).
3. Prompt 05 item 4 (move conflict sync out of `ScanPage._generate_conflicts`)
   was already done in phase 3 (`complete_batch_review_state`); the engine
   calls it per unit.
4. The brief's "durable transaction must include duplicate-related state
   required at that point": cross-sheet duplicate state completes at unit end
   (ADR-0006's recorded-step option), not in the sheet's own transaction (§10).
5. Prompt 05's restart sequence ends "resume if it was running": a persisted
   running/paused flag is phase 7 (persisted pause flags); the engine resumes
   when it is started.
6. "Measure pool start-up and choose unit size / warm pool": measured (§20);
   chose a warm pool. The 200-file default unit size is a starting value; its
   effect on throughput was not separately measured.
7. The prompt suggests `EngineStatus` "caught up" in the §14.2 sense; this
   phase's `caught_up` ignores source reachability (the full predicate with
   reachability and per-source counts is phase 7's session snapshot).

## 26. Known limitations

* Headless: no GUI starts, pauses or shows the engine (phase 8).
* **The engine must not run while the finite Scan stage runs a batch of the
  same project**: both are coordinators, and the engine's recovery returns
  every stale claim in the project to `pending`. Nothing enforces this yet.
* Intake reconciliation and copy-ingest run on the coordinator thread (lower
  throughput with live intake, §20).
* Pause flags, the worker-lost retry counter and per-source poll times are in
  memory; they reset when the engine restarts.
* Cross-sheet conflicts appear at unit end, not per sheet.
* `EngineStatus.registered`/`pending`/... count every row of the session's
  batches, superseded history included; the effective count is
  `session_population`.
* A watched unit's stored `source_name` is the content-addressed copy name;
  Resolve shows that name (the original is in the ledger - phase 8 should show
  it).
* A batch another part of the application adds to the session is seen within
  1 s (the runnable-batch list is reused for a second).
* Real kills at 30 sheets; endurance with replayed recognition; real
  recognition at 400 sheets. No fresh 100k run (a pre-Beta gate). No SMB, real
  scanner or power-loss evidence.

## 27. Deferred to phase 7

The quality decision layer and suggested rejections; rescan suggestions by
source and time; held / unreadable / unsupported / duplicate-content file
decisions; persisted intake/processing pause flags; *Finish current* and
*Cancel queued* operator policy; the session snapshot (partitioning counts,
three progress lines, caught-up with reachability, per-source counts, rate
alarm); finish-session validation with intake blockers; "resume if it was
running" on restart.

## 28. Deferred to phase 8

Source configuration GUI, live scanner cards and per-source table, processing
progress and caught-up display, pause/resume/finish controls, held/unreadable
dialogs, operational rescan UI, the SQL-paged scan list, running the engine
from the main window (and integrating it with the finite Scan stage so the two
coordinators never overlap), showing the ledger's original file name.

## 29. Is Phase 6 complete against its acceptance criteria?

Against the brief's §76 list: Phase 5 work continuously processed - yes;
one session with many finite units - yes; processing while new units register
- yes; bounded scheduling and memory - yes (measured); workers never write -
yes (architecture test); completed means committed, conflicts durable before
completion - yes for the sheet's own conflicts, at unit end for cross-sheet
ones (§10, ADR-0006); in-flight retryable, completed never resubmitted, same
session and batch, no recovery batch, repeated restart idempotent,
1/25/50/75/99 % convergence - yes; multi-source - yes; late conflicts - yes;
effective set excludes superseded - yes; finite workflow intact - yes (the full suite, GUI included, passes with every pre-existing test unchanged);
Resolve edits survive restart - yes; no double counting in Attendance /
scoring - yes; clean shutdown explainable - yes; full suite, ruff, mypy -
passing (§22); documentation and this handoff - yes. Against ACCEPTANCE §2 E, the
first-part items E1, E2, E5, E6, E9 have evidence; E3, E4, E7, E8 belong to
phase 7.

## 30. Safe to merge?

On the evidence, the branch is **technically mergeable as headless
infrastructure**: the full suite passes with no pre-existing test edited, the
finite workflow is byte-for-byte unchanged in its code path, the new code is
reached only by callers that construct the engine (none in the application yet),
and there is no migration. Merging is the owner's decision, and the evidence is
automated only: no network share, real scanner, power loss, operator use or
fresh 100k run. The risks below apply once phase 8 starts the engine from the
GUI, not to merging the headless layer.

## 31. Remaining risks

* Unmeasured on a share or slow disk: commit latency, lock waits under the
  5 s `busy_timeout`, copy-ingest cost.
* The coordinator thread doing intake and processing is a throughput ceiling
  (about 50 files/s coordinator-only on this machine; real recognition at 8
  workers is about 25/s, so not the bottleneck here).
* Running the engine and the finite Scan stage on one project at once is
  unguarded until phase 8.
* Unit-size and trickle defaults are not validated.

## 32. Recommended next phase

**Revised phase 7 - quality / rescan / session controls** (roadmap E second
part): the quality decision layer feeding Reject & Rescan with suggested
rejections, rescan suggestions, held-file decisions, persisted pause / finish /
cancel semantics on top of this engine's primitives, the session snapshot, and
finish-session validation. Not started.
