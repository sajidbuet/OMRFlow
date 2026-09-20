# Phase 10 handoff — Integration, Recovery & Production Hardening

**Implemented:** 2026-09-20
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2,
SQLAlchemy 2.x, psutil 7.2.2, 16 logical CPUs.

Read alongside `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md` and
`tools/benchmark_stress.py`'s own module docstring (the exact CLI commands).

**Phase 10 does not change what Phases 1-9 compute.** No recognition
threshold, scoring rule, ranking formula or reconciliation classification was
touched. This phase makes the existing pipeline survive abrupt termination,
detect its own damage, avoid silent duplication, and scale its architecture
(not its accuracy) to 100,000 sheets - and is explicit, throughout, about
which of those claims are backed by an automated test run in this
environment and which remain to be run at full scale (§9 below).

---

## 1. What already existed

Phase 5 had already built most of the resumable-state-machine foundation
this phase's brief re-asks for, under different names:

| Capability | Where | Verdict |
|---|---|---|
| Per-sheet durable state machine (`pending/queued/processing/completed/warning/failed/cancelled`) | `database.models.ScanJobStatus`, `services.batch_store` | Reused as-is; nothing rewritten |
| Coordinator-only database writes (workers never touch the DB) | `services.parallel_batch` (workers return results only), `services.batch_store.BatchRecorder` | Already single-writer by construction - satisfies §4 directly |
| Crash recovery on project open (`queued`/`processing` -> `pending`, never `failed`) | `services.batch_store.recover_interrupted` | Reused as-is |
| Bounded job submission (never more than `workers * 4` futures in flight) | `services.parallel_batch.QUEUE_DEPTH_PER_WORKER` | Already satisfies §17 directly |
| Append-only audit ledger, DB-trigger-enforced | `database.models.AuditEvent` (Phase 6) | Pattern reused for `BatchScanHistory` |
| Atomic whole-file writes (`temp + fsync + replace`) | `utils.json_io.write_json_atomic` | Reused for backups' sidecar manifests |
| "Regenerate, never patch" precedent | Phase 8 scoring, Phase 9 reporting | Extended to recognition itself (`mark_for_reprocessing`) |

## 2. What was missing

1. Content-hash provenance - scans were tracked by size/mtime only, never a
   cryptographic hash; no duplicate-content detection.
2. Any form of project locking - two processes could open and write the same
   project database at once.
3. A read-only project mode.
4. Any backup/snapshot mechanism.
5. Any database health/integrity check beyond what SQLite raises on its own.
6. A way to reprocess a sheet without silently losing its previous machine
   reading.
7. Worker recycling and configurable OpenCV thread count.
8. Any synthetic dataset generator capable of addressing sheet *N* of
   100,000 independently and cheaply - the existing Phase 3.5 generator
   (`evaluation.synthetic_dataset`) is a curated, whole-dataset-at-once
   tool, a different and still-complete subsystem this phase reuses the
   rendering primitives of rather than replacing.
9. Any telemetry, CLI benchmark entry point, or kill/resume test harness.

## 3. What was built

### `services/scan_provenance.py` — content-hash provenance (§9, §10)

`hash_file`/`hash_bytes` (streaming SHA-256, 1 MiB chunks - bounded memory
regardless of file size), `compute_hashes_for_batch` (threaded, batched
writes, skips virtual stress sources), `duplicate_groups` (exact-file
duplicates, explicitly distinct from Phase 7's duplicate-candidate-ID
exceptions), `check_availability` (present/missing/changed/unverifiable),
`relink_scan` (verifies content hash before accepting a replacement path).
Wired into `gui/scan/worker.py`'s `BatchWorker.run()` - hashing happens off
the GUI thread, before recognition starts, and is a no-op with no project
open.

### `services/project_lock.py` — project locking (§3)

Plain-file lock (`.omrflow.lock`: pid, hostname, app version, timestamp),
not `QLockFile` - keeps Qt out of the services layer while providing the
same guarantee (`psutil.pid_exists` for liveness). `acquire()` never removes
an existing lock, however stale it looks; `force_acquire()` is the explicit
operator-decision path. Wired into `services/project_service.py`
(`open_project(..., read_only=, force_lock=)`) and `gui/main_window.py` (a
three-choice dialog: Cancel / Open Read-Only / Remove Lock and Open, and -
critically - the choice-owning dialog lives in `_prompt_open_project`, never
inside the testable `open_project_at`, to avoid re-introducing the exact
modal-in-a-testable-method hang class Phases 8 and 9 each found once).

### Read-only mode (§8, §42)

`database/engine.py`: `open_project_database(..., read_only=True)` opens a
genuine SQLite URI read-only connection (`mode=ro`) - every write fails at
the SQLite level, not merely by application convention. Two "create a
default row on first read" functions (`scoring_store.active_policy`,
`report_store.get_layout_config`) were found, during this phase's own
testing, to attempt a write even on a read-only session; both were patched
to return an unpersisted default instead. `main_window.py` skips
crash-recovery repair (a write) for a read-only session.

### `services/project_backup.py` — snapshots (§5)

`create_backup` uses SQLite's own online backup API
(`sqlite3.Connection.backup()`), never a raw file copy - a live database
copied byte-for-byte can capture a torn write. The sidecar `.json` manifest
is written *last*, so an interrupted backup can never be mistaken for a
complete one (`list_backups` reports a `.sqlite3` with no manifest as
incomplete, never as corrupt). `verify_backup` re-hashes; `restore_backup`
never overwrites an existing destination.

### `services/project_health.py` — health checking (§6, §7)

`quick_check` (`PRAGMA quick_check`, cheap, safe on every open).
`full_check` (integrity check, foreign-key check, schema version, stale
job detection, source-scan availability via `scan_provenance`, unresolved
Phase 6 conflicts, unresolved Phase 7 reconciliation exceptions, sets
missing a verified Phase 8 key, backup presence, free disk space). If
`integrity_check` itself fails, every subsequent structural query is
skipped rather than risked against a database already known to be
corrupted - each `PRAGMA` call is also wrapped against
`sqlite3.DatabaseError`, since a sufficiently corrupted file raises there
rather than returning a descriptive row. No repair path exists anywhere in
this module, by design (§7).

### `database/migrations.py` — migration 7

Additive only: `batch_scan.content_sha256`/`content_hash_algorithm`
columns, `batch_scan_history` (append-only, trigger-enforced, for
reprocessing history - §13), `processing_manifest` (a reproducibility
snapshot table, assembled by reading Phases 5-9's own tables rather than
duplicating them). Schema version 6 -> 7.

### `services/batch_store.py` additions — deterministic reprocessing (§12, §13)

`mark_for_reprocessing(database, batch_id, scan_ids, *, reason,
requested_by)` archives a scan's current status/outcome/result into
`BatchScanHistory` before resetting it to `pending` - the primitive
"reprocess one/selected/failed/complete-batch" (§12) are all built from.
`reprocess_failed_scans`/`reprocess_batch` are the two named convenience
wrappers. `reprocessing_history(scan_id)` returns every superseded result,
oldest first - a sheet reprocessed twice leaves two records, never one
overwritten row.

### `config/processing.py` / `services/batch_processor.py` / `services/parallel_batch.py` — worker recycling and OpenCV threads (§16, §18)

`ProcessingSettings.opencv_threads` (default 1) and
`.worker_recycle_after` (default 500, `0` disables), threaded through
`BatchOptions` into `recognise_in_parallel`.

**A genuine defect found and fixed during this phase's own testing** (see
§8 below): worker recycling is deliberately **not** implemented via
`ProcessPoolExecutor`'s own `max_tasks_per_child` constructor argument,
because that stdlib feature was found to hang permanently, in a minimal
reproduction with no OMRFlow code involved at all, on this project's
supported platform. `recognise_in_parallel` instead recycles by running a
fresh, short-lived pool per bounded slice of the batch
(`_recognise_batch_in_parallel`) - provably safe, at the cost of a full
drain at each recycle boundary rather than a seamless mid-stream swap.

### `evaluation/stress_dataset.py` — the 100,000-sheet generator (§19, §20, §21)

`StressDatasetSpec(seed, sheet_count, distribution)`. `plan_for_index(spec,
template, index)` is a pure function of its three arguments - reusing
Phase 3.5's own `SheetBuilder`/`FieldLayout`/`render_case` primitives, never
a second drawing implementation - and `kind_for_index` draws one of 15 case
kinds (clean, no-answer, multiple-answer, low-confidence, ambiguous mark,
skewed, blank ID, ambiguous ID, duplicate ID, exact-duplicate-scan,
duplicate-roll-different-image, unknown-candidate, multiple-sets,
malformed, conflict-generating) from a documented default distribution,
seeded from `(seed, index)` alone. `render_sheet_for_index` returns real
PNG bytes for every kind except `MALFORMED`, which returns deliberately
undecodable bytes - the same failure a damaged real scan produces, not a
simulated flag. `virtual_source_path`/`is_stress_source` encode `(seed,
index)` into a path-separator-free identity string that survives a
`pathlib.Path` round trip on Windows unchanged (a real bug found and fixed
during this module's own testing - the obvious `stress://seed/index` form
does not survive that round trip).

Two categories the brief also names - "absentee reconciliation" and
"unknown candidate" - describe a *roster* relationship, not a property of
one image, and are only partially represented here (see §9, "Not done").

### `evaluation/stress_runner.py` — orchestration, reusing the unmodified pipeline

`create_stress_batch` registers `spec.sheet_count` rows with virtual source
paths - nothing is rendered. `run_stress_batch` processes whatever
`batch_store.resumable_scans` says remains, in chunks of
`MATERIALIZE_CHUNK_SIZE` (2,000) sheets: each chunk is rendered to *real*
temporary PNG files, handed unchanged to
`services.batch_processor.process_batch`, and deleted immediately after.
Peak disk usage is bounded by the chunk size, never by the run's total
sheet count, while `services/batch_processor.py` and
`services/parallel_batch.py` needed **zero changes** to support it - the
one seam required is rewriting each finished `ProcessedScan.result.source_path`
from its temporary chunk path back to the sheet's permanent virtual identity
before it reaches `batch_store`, since that identity is the primary key
`record_results` matches against.

This module lives in `evaluation`, not `services`, because
`tests/unit/test_architecture.py`'s own commentary places the QA/benchmark
layer *above* services (it may call any service; no service may call it
back) - putting stress orchestration inside `services` would have been
exactly backwards.

### `services/telemetry.py` — sampling (§24, §25)

`TelemetryRecorder` samples on a background thread at a fixed interval
(default 5s) and appends one JSON line per sample - a run killed mid-way
loses at most the one sample in flight, never an earlier one. Reads CPU
(process and system), memory, database file size, disk free space, and
throughput via `psutil`, newly added as a dependency (not previously
present, per §24's own instruction to check first).
`estimate_completion` averages a rolling window (12 samples, ~1 minute) so
one unusually slow sheet cannot swing the ETA.

### `tools/benchmark_stress.py` — the CLI (§52)

`python -m omr_scanner.tools.benchmark_stress <project> --create --template
<path> --sheets N --seed N [--workers N] [--opencv-threads N]
[--worker-recycle-after N] [--force-lock]`. Same command, without
`--create`, resumes. Writes a JSON report
(`<project>/benchmarks/stress_<batch_id>.json`) and the raw telemetry file
beside it. Exit codes distinguish finished (`0`) from cancelled-with-work-
remaining (`3`), so a script driving the kill matrix can tell the two
apart without parsing output.

**A genuine defect found and fixed during this tool's own manual testing**:
the first version held a `ProjectSession` but closed only its `.database`,
never releasing the project lock - every run after the first refused to
open. Fixed by keeping and closing the whole session (§8 below).

### GUI

* `gui/settings_dialog.py`: a new "Advanced (production hardening)" section
  exposing `opencv_threads` and `worker_recycle_after` (§16, §18).
* `gui/health_dialog.py`: **Project Health & Recovery**, one dialog for both
  concerns per §57's explicit instruction against several disconnected
  ones - "Run Full Check" (colour-coded issue list) and a Backups section
  ("Create Backup Now", "Restore Selected To..."). No repair action exists
  anywhere in it, matching §7. Wired into `Tools -> Project Health /
  Recovery...`, enabled only while a project is open.

## 4. What was NOT built, stated honestly

* **Lazy Qt models for large tables (§26/§27).** The Scan page's result
  list (`gui/scan/page.py`) is a `QTableWidget` - item-based, materialising
  one `QTableWidgetItem` per cell. This was inspected, not converted: a
  100,000-row batch feeding that table would build 500,000 widget items,
  which is exactly what §26 forbids. Converting it (and the Results,
  Resolve and Attendance pages' equivalent tables) to a
  `QAbstractTableModel`-backed view is a real, disclosed gap, not
  attempted this phase given the regression risk of touching several
  already-shipped, working GUI pages without the time budget to verify
  each one as carefully as this phase's other changes were verified. It
  does **not** affect the mandatory 100,000-sheet acceptance test, which
  runs entirely through the headless CLI and never opens a Qt table.
* **A diagnostic bundle (§39)** and **a global GUI exception handler
  (§41)** were not built.
* **§28 (100,000-sheet reporting scalability)** and **§27's conflict-queue
  throughput re-measurement against the stress dataset** were not
  separately exercised this phase; Phase 6's own synthetic
  10,000-conflict benchmark (`docs/conflict_review.md`) stands as the
  most recent evidence for the conflict queue specifically.
* **§54 (Windows paths with spaces/Unicode)** and **§55 (packaged-app
  smoke test)** were not exercised - no packaging build exists yet
  (Phase 11).
* Two of the fifteen stress-dataset case kinds the brief lists by name -
  "absentee reconciliation" and "unknown candidate" as *roster* facts
  (a registered candidate with no script; a script belonging to nobody
  registered) - are not generated by `stress_runner` yet; only the *sheet*
  half of "unknown candidate" (a roll outside the intended range) exists.
  A stress-scale roster generator exercising Phase 7's reconciliation
  under load is a natural next addition, not built this phase.

## 5. Database changes

Migration 7 (`SCHEMA_VERSION` 6 -> 7): `batch_scan.content_sha256` /
`content_hash_algorithm` (additive columns, defaulted for existing rows,
never backfilled - a batch processed before this version is simply
re-hashed the next time it is touched); `batch_scan_history` (new table,
append-only, two triggers); `processing_manifest` (new table, currently
populated only by `services/report_store.py`'s pre-existing patterns being
followed, not yet written to automatically by any Phase 10 code path - see
§9).

## 6. Recovery architecture

* **Autosave.** Already-present `utils.json_io.write_json_atomic`
  (temp-file + `fsync` + atomic replace) covers every whole-file save
  (`project.json`, `omrflow.config.json`, `.omrt` templates) - audited this
  phase, not newly built, and confirmed genuinely atomic. Structured
  project state (batches, results, conflicts, reconciliation, scores,
  reports) was already transactional via SQLAlchemy sessions before this
  phase; nothing needed to change there.
* **Crash recovery.** Unchanged from Phase 5: `recover_interrupted` runs on
  every project open, moving stale `queued`/`processing` rows back to
  `pending`. Now additionally skipped for a read-only session (a write it
  cannot perform).
* **Project locking.** New this phase (§3 above).
* **Backups.** New this phase (§5 above); not yet wired to run
  automatically before a migration - `project_backup.create_backup` exists
  and is manually invokable from the Health dialog, but
  `database/migrations.py`'s `apply_pending_migrations` does not yet call
  it automatically. A disclosed gap: §5 asks for a backup "before
  database/schema migration" as one of several triggers, and this one was
  not wired.

## 7. Reprocessing model

Reprocess one sheet: `mark_for_reprocessing(db, batch_id, [scan_id],
reason=...)`. Selected sheets: same, with more ids. Failed sheets:
`reprocess_failed_scans`. Complete batch: `reprocess_batch`. In every case,
the sheet's previous `(status, outcome, attempt_count, result_json,
content_sha256)` is archived to `BatchScanHistory` before the row resets to
`pending`; a subsequent `process_batch`/`resumable_scans` pass reprocesses
it exactly as it would any other pending sheet - no second processing code
path exists for a reprocessed sheet versus a never-processed one.
Dependency invalidation for score/report changes was already correct from
Phases 8 and 9 (their own "regenerate, never patch" rules) and is
unaffected by this phase.

## 8. Defects found during this phase's own testing

Two genuinely new, previously-undiscovered defects, plus one recurrence of
an already-known anti-pattern:

1. **`ProcessPoolExecutor(max_tasks_per_child=N)` hangs permanently on this
   platform.** Reproduced with a minimal script - two workers, six trivial
   tasks, `max_tasks_per_child=2`, `mp_context="spawn"`, an `initializer` -
   with zero OMRFlow code involved. Exactly four of six tasks completed;
   the pool never spawned replacement workers for the remaining two. Fixed
   by implementing recycling as a sequence of short-lived pools
   (`_recognise_batch_in_parallel`) instead of relying on the stdlib
   parameter at all - see §3 above. Left as a documented, permanent design
   note in `services/parallel_batch.py`'s module docstring, since a future
   contributor's first instinct will reasonably be to "simplify" back to
   the stdlib parameter.
2. **`tools/benchmark_stress.py` never released the project lock.** The
   first implementation opened a `ProjectSession` but closed only its
   `.database` attribute; `ProjectSession.close()` (which releases the
   lock) was never called, so every run after the first refused to open,
   even after a perfectly clean exit. Found by the tool's own manual
   end-to-end test in this handoff's preparation, before any automated
   test could have caught it (none yet exercised a second real CLI
   invocation against the same project) - a `test_a_forced_kill_...`
   integration test was subsequently written specifically because this
   class of bug exists.
3. **The obvious `stress://seed/index` virtual-path scheme does not survive
   Windows.** `PureWindowsPath` rewrites `stress://1/000001` to
   `stress:\1\000001`, silently destroying the `stress://` prefix
   `is_stress_source` checks for. Found while writing this module's own
   unit tests (a round-trip-through-`Path` test, following the same
   discipline `scan_provenance.py`'s identical constant already
   documents having needed once before). Fixed by choosing a
   separator-free prefix (`stress:`, joined to `seed-index` with `-`).
4. **Killing the coordinator process leaves its worker processes running,
   orphaned.** `Process.kill()` on the CLI's parent process does not
   terminate the `ProcessPoolExecutor` worker processes it had already
   spawned - on Windows, worker processes are not tied to their parent's
   lifetime by default. Observed directly during this phase's own
   kill/resume testing: after each forced kill, 1-2 python processes with
   flat, non-progressing CPU time remained running until manually
   terminated. **This does not corrupt or duplicate any data** - orphaned
   workers never held a database connection or write access in the first
   place (§4's single-writer architecture holds regardless) - but it is a
   genuine, disclosed operator-experience gap: a real crash could leave
   orphaned OMRFlow worker processes consuming memory and a CPU core
   indefinitely until the machine is rebooted or they are found and killed
   by hand. Not fixed this phase (a Windows Job Object binding worker
   lifetime to the coordinator's is the natural fix); listed in §12,
   "Not done".

## 9. Testing

### Automated (this environment)

| Suite | New tests |
|---|---|
| `tests/unit/test_scan_provenance.py` | 18 |
| `tests/unit/test_project_lock.py` | 10 |
| `tests/unit/test_project_backup.py` | 11 |
| `tests/unit/test_project_health.py` | 13 |
| `tests/unit/test_readonly_defaults.py` | 2 |
| `tests/unit/test_stress_dataset.py` | 18 |
| `tests/unit/test_telemetry.py` | 9 |
| `tests/integration/test_production_hardening.py` | 2 |
| `tests/integration/test_reprocessing.py` | 8 |
| `tests/integration/test_stress_runner.py` | 8 |
| `tests/integration/test_stress_kill_resume.py` | 2 (100- and 1,000-sheet real forced kills) + 1 marked `stress` (10,000-sheet, see below) |
| `tests/gui/test_health_dialog.py` | 13 |
| Additions to existing files (locking/read-only in `test_project_service.py`, lock-dialog split in `test_main_window.py`, recycling/OpenCV threads in `test_parallel_batch.py`, advanced settings in `test_processing_settings_gui.py`) | 15 |
| **Total new** | **130** (129 in the default run, 1 marked `stress`) |

Full regression suite (all phases, default marker set): see the final
report for the exact count from the run made immediately before this
document was finalised.

### Real forced-kill-and-resume tests (§14, §30, §31, §44) — genuinely executed, not simulated

`tests/integration/test_stress_kill_resume.py` starts the real CLI as a
subprocess, polls the actual SQLite file (a separate read-only connection)
until a target number of sheets have durably committed, calls
`Process.kill()` (`TerminateProcess` on Windows - abrupt, no graceful
shutdown, no chance to release the lock), then resumes with `--force-lock`
and asserts: the final row count equals the sheet count exactly; every
sheet reached a terminal state; and every sheet that had already committed
before the kill has an **unchanged** status and result after the resume.

* **100 sheets: passed** (`test_a_forced_kill_mid_run_resumes_without_loss_or_duplication[100]`).
* **1,000 sheets: passed** (`[1000]`).
* **10,000 sheets: passed** (`[10000]`, marked `stress`, run explicitly with
  `-m stress`). Real elapsed time for the full kill-then-resume cycle:
  **1,383 seconds (~23 minutes)** in this environment (16 logical CPUs,
  2 workers). Confirms the same no-loss, no-duplication, exact-final-count
  properties at an order of magnitude closer to the mandatory 100,000-sheet
  scale.

### The mandatory 100,000-sheet run — what was and was not executed

* **Registration at full scale was executed and measured directly**:
  `evaluation.stress_runner.create_stress_batch` registered 100,000 sheets
  in this environment; see the final report for the exact measured time
  and peak Python-allocated memory. This demonstrates the database and
  batch-registration architecture handles the full mandatory scale without
  loading the whole dataset into memory at once (`resumable_scans`
  returning the full list of paths is a separate, fast, bounded query, not
  evidence against this - see the module docstrings for why per-sheet
  state is what makes that safe).
* **Full 100,000-sheet *processing*, and the mandatory 1% / 25% / 50% /
  75% / 99% kill-and-resume acceptance matrix against it, were NOT
  executed in this session.** This was an explicit, agreed scope decision
  (a multi-hour-to-multi-day undertaking, five separate deliberate abrupt
  terminations against a full-scale run), not an oversight or a
  technical limitation. The harness that runs it
  (`tools/benchmark_stress.py`, exercised end-to-end at 100/1,000/10,000
  sheets above) is complete and ready to run; see §10 below for the exact
  commands.

**Phase 10 is therefore not marked fully complete against its own §64
acceptance checklist** - specifically the "100,000-sheet acceptance"
section - until that run is actually performed and its results recorded
here.

## 10. Exact commands for the deferred 100,000-sheet run

```powershell
# First run: registers the batch and begins processing.
.venv\Scripts\python.exe -m omr_scanner.tools.benchmark_stress `
    D:\stress_100k --create `
    --template examples\templates\100_question_4_choice_example.omrt `
    --sheets 100000 --seed 20260920 --workers 8 --worker-recycle-after 500

# Watch the batch's committed count (a separate, read-only process):
.venv\Scripts\python.exe -c "
from pathlib import Path
from omr_scanner.database.engine import open_project_database
from omr_scanner.services import batch_store
db = open_project_database(Path(r'D:\stress_100k\database.sqlite'), read_only=True)
batches = batch_store.list_batches(db)
print(batches[0])
"

# At the desired checkpoint (1% / 25% / 50% / 75% / 99% of 100,000 sheets
# durably committed), terminate the process ABRUPTLY - not Ctrl+C, which
# this tool catches and shuts down cleanly on purpose. Use Task Manager
# "End task", or from another PowerShell window:
Get-Process python | Where-Object { $_.MainWindowTitle -eq '' } | Stop-Process -Force
# (or, more precisely, capture the PID this command's own launch prints
# and `Stop-Process -Id <pid> -Force`)

# Resume - the same command, plus --force-lock (the explicit operator
# decision the lock model requires after a genuine abrupt termination):
.venv\Scripts\python.exe -m omr_scanner.tools.benchmark_stress `
    D:\stress_100k `
    --template examples\templates\100_question_4_choice_example.omrt `
    --sheets 100000 --seed 20260920 --workers 8 --worker-recycle-after 500 `
    --force-lock

# Repeat the kill-and-resume cycle at each of the five checkpoints against
# either the same run (continuing from where the previous kill left off)
# or five fresh runs from a common starting snapshot, per the brief's own
# "begin from a known clean project or controlled snapshot" instruction.

# After the run finishes cleanly, verify integrity directly:
.venv\Scripts\python.exe -c "
from pathlib import Path
from omr_scanner.database.engine import open_project_database
from omr_scanner.services import project_health
db = open_project_database(Path(r'D:\stress_100k\database.sqlite'))
report = project_health.full_check(db, Path(r'D:\stress_100k'))
print(report.level, len(report.issues), 'issue(s)')
for issue in report.issues:
    print(' ', issue.level.value, issue.code, issue.message)
"

# The benchmark report itself (§50's metrics, as far as this CLI measures
# them - see tools/benchmark_stress.py's own docstring for which §50
# fields it does and does not populate):
Get-Content D:\stress_100k\benchmarks\stress_*.json
```

To run the equivalent smaller presets used for this phase's own validation:

```powershell
# 100 sheets
.venv\Scripts\python.exe -m omr_scanner.tools.benchmark_stress out\s100 --create --template <template> --sheets 100 --seed 1 --workers 4
# 1,000 sheets
.venv\Scripts\python.exe -m omr_scanner.tools.benchmark_stress out\s1000 --create --template <template> --sheets 1000 --seed 1 --workers 4
# 10,000 sheets
.venv\Scripts\python.exe -m omr_scanner.tools.benchmark_stress out\s10000 --create --template <template> --sheets 10000 --seed 1 --workers 4
```

The automated equivalent of the smaller kill/resume runs:
`.venv\Scripts\python.exe -m pytest tests/integration/test_stress_kill_resume.py -q`
(100 and 1,000 sheets, always run) and
`... -m stress -q` (adds the 10,000-sheet case).

## 11. A rule this phase reinforced, not invented

The modal-dialog-in-a-testable-method anti-pattern, first found in Phase
8's `ResultsPage._on_scored` and again in Phase 9's
`ReportsPage._on_generated`, was deliberately designed *around* in this
phase's lock-conflict dialog before it could recur a third time:
`MainWindow.open_project_at` never shows a dialog, and the three-choice
resolution lives entirely in `_prompt_open_project`/
`_prompt_resolve_lock_conflict`, which a test never calls directly. A
dedicated test (`test_opening_a_locked_project_directly_reports_an_error`)
exists specifically to pin this boundary in place.

## 12. Not done

* The mandatory full-scale 100,000-sheet kill/resume acceptance matrix
  (§9 above) - the single largest remaining item.
* Automatic cleanup of orphaned worker processes left behind by a forced
  kill (§8, defect 4) - a real, disclosed operator-experience gap, not a
  data-integrity one.
* Lazy Qt models for the Scan/Results/Resolve/Attendance tables (§26/§27).
* A diagnostic bundle (§39) and a global GUI exception handler (§41).
* Automatic backup-before-migration wiring (§5's other trigger points -
  before a recovery attempt - are also not wired; only the manual
  "Create Backup Now" path exists).
* Stress-scale roster generation exercising Phase 7's reconciliation and
  Phase 9's reporting under 100,000-candidate load (§28).
* Windows-specific path testing (spaces, Unicode, another drive) and a
  packaged-application smoke test (§54/§55) - no packaging build exists
  yet (Phase 11).

## For whoever picks this up

Read `evaluation/stress_dataset.py` and `evaluation/stress_runner.py`'s
module docstrings first - both explain a layering decision (why stress
orchestration lives in `evaluation`, not `services`) that is easy to
"fix" by moving it the wrong way. Before running the full 100,000-sheet
matrix, read `tools/benchmark_stress.py`'s own docstring for the exact
kill procedure - a graceful `Ctrl+C` is caught and shut down cleanly on
purpose, and does not exercise what the acceptance test requires.
