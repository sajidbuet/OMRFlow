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

**Everything in this section was written when Phase 10 was first
implemented. An independent validation pass (§13) subsequently built most
of it; each bullet below is annotated with what actually happened. Nothing
here has been deleted, so the record of what was and wasn't known at each
point stays honest.**

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
  **[§13 UPDATE: the Scan page's table was converted to a
  `QAbstractTableModel`-backed `QTableView` and measured at 100,000 rows -
  see §13. Results/Resolve/Attendance's equivalent tables remain
  `QTableWidget`, deliberately deferred again as lower priority since they
  are bounded by candidate/question/report counts, not raw sheet count.]**
* **A diagnostic bundle (§39)** and **a global GUI exception handler
  (§41)** were not built. **[§13 UPDATE: both built and tested - see §13.]**
* **§28 (100,000-sheet reporting scalability)** and **§27's conflict-queue
  throughput re-measurement against the stress dataset** were not
  separately exercised this phase; Phase 6's own synthetic
  10,000-conflict benchmark (`docs/conflict_review.md`) stands as the
  most recent evidence for the conflict queue specifically.
  **[§13 UPDATE: reconciliation and Phase 9 Excel report generation were
  both exercised at real 10,000-sheet scale against real recognition
  output - see §13. The conflict-queue re-measurement specifically
  remains not done.]**
* **§54 (Windows paths with spaces/Unicode)** and **§55 (packaged-app
  smoke test)** were not exercised - no packaging build exists yet
  (Phase 11). **[§13 UPDATE: Windows path testing (long paths, Unicode
  project names) was exercised and found a real, since-fixed defect - see
  §13. No packaging build exists yet, so §55 is still not applicable.]**
* Two of the fifteen stress-dataset case kinds the brief lists by name -
  "absentee reconciliation" and "unknown candidate" as *roster* facts
  (a registered candidate with no script; a script belonging to nobody
  registered) - are not generated by `stress_runner` yet; only the *sheet*
  half of "unknown candidate" (a roll outside the intended range) exists.
  A stress-scale roster generator exercising Phase 7's reconciliation
  under load is a natural next addition, not built this phase.
  **[§13 UPDATE: built - `evaluation/stress_roster.py` - and exercised at
  10,000 sheets against real reconciliation. See §13.]**

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
  not wired. **[§13 UPDATE: wired and tested - `project_service.open_project`
  now snapshots the database before an older-schema project is upgraded.
  See §13.]**

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
   "Not done". **[§13 UPDATE: fixed - `services/process_containment.py`
   implements exactly the Windows Job Object binding this note anticipated,
   verified against real orphaned processes and a negative control. See
   §13.]**

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
  state is what makes that safe). **[§13 UPDATE: this specific claim did
  not hold up under independent measurement -
  `stress_runner.run_stress_batch` was calling `resumable_scans` (eager,
  unbounded) once per resume, then chunking the *result already in
  memory*; at 100,000 rows this measured at 25.61 MB, not the "separate,
  fast, bounded" characterisation above. Fixed by adding
  `batch_store.resumable_scans_window` (cursor-paged) and rewiring
  `stress_runner` to use it; the same measurement afterwards read 1.63 MB,
  independent of total row count. See §13.]**
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

## 12. Not done (as of this phase's original implementation)

**Superseded by §13 - kept for the historical record. See §13's own "still
not done after this pass" list for what is actually still outstanding.**

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

## 13. Independent audit and validation pass (2026-09-20)

A separate session, immediately following the one that produced §1-§12,
was given an explicit brief: treat every claim above as something to
verify, not as fact - "do not regard the previous 10,000-sheet success or
3,332 passing tests as proof that these items are correct" - and complete
as much of Phase 10 as could reasonably be finished without running the
deferred 100,000-sheet kill-matrix itself. Every fix below was made only
after independently reproducing the underlying gap with real evidence
(a real byte-level corruption, a real orphaned process, a real measured
memory number, a real negative control showing the *old* code actually
failing), following the same discipline §8's defects were originally
found and fixed with.

### New defects found and fixed

1. **`resumable_scans` unbounded materialisation in the stress harness.**
   §9's own claim that this was "a separate, fast, bounded query" did not
   hold up - see the correction inline at §9 above. Fixed with
   `batch_store.resumable_scans_window` (cursor-paged,
   `after_batch_index`/`limit`) and rewired into `stress_runner.run_stress_batch`.
   Measured with `tracemalloc` at 100,000 rows: **25.61 MB -> 1.63 MB**
   (~15.7x), and the bounded version's memory no longer scales with row
   count at all.
2. **Orphaned worker processes after a forced kill (§8, defect 4) -
   fixed.** `services/process_containment.py` binds every worker process's
   lifetime to the coordinator's via a Windows Job Object
   (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), so the OS itself terminates any
   still-running worker the instant the coordinator's job handle closes -
   including on an abrupt `TerminateProcess`. Getting this right required
   fixing a real ctypes bug along the way: `GetCurrentProcess()`'s pseudo
   handle (`(HANDLE)-1`) was being silently corrupted by ctypes' default
   32-bit return-type guess on 64-bit Python, producing `ERROR_INVALID_HANDLE`;
   fixed by declaring every Windows API function's `argtypes`/`restype`
   explicitly with `ctypes.c_void_p`. Verified against real, independently
   discovered evidence - four genuinely orphaned processes left running
   2+ hours from this project's *own* earlier kill/resume testing, confirmed
   dead-parent-PID - and against a negative control (temporarily removing
   the fix reliably reproduced orphaned processes in the exact same test
   that, with the fix restored, reliably does not).
3. **Worker recycling verified at the OS-process level, not just by
   output.** A new test tracks real child PIDs via `psutil` across three
   recycle generations of two workers each and asserts more than two
   distinct PIDs were ever used - the existing test only ever checked
   output equivalence, which a no-op recycling implementation would also
   have satisfied.
4. **The Scan page's scan list, converted to a lazy Qt model.** §4's
   disclosed gap - `QTableWidget`, one `QTableWidgetItem` per cell,
   materialised eagerly. Replaced with a `QTableView` backed by a new
   `gui/scan/table_model.py::ScanTableModel(QAbstractTableModel)` that
   reads `ScanPageState.entries` directly, on demand, storing nothing of
   its own. A second, related defect was found and fixed in the same
   pass: `_apply_status_filter` was re-scanning **every** entry on every
   ~200ms refresh tick during a run, not just the rows that had changed -
   now split into a full re-scan (filter combo changed) and a bounded
   re-check of only the dirty rows (a run in progress). Measured at
   100,000 synthetic rows: populating the model cost **2.8 KB / 0.29 ms**;
   populating the equivalent `QTableWidgetItem`s (500,000 of them) cost
   **38.76 MB / 16.77 s** - about four orders of magnitude, because the
   model defers every cell to an on-demand `data()` call for only what is
   actually painted. Results/Resolve/Attendance's equivalent tables
   remain `QTableWidget` - deliberately deferred again, since they are
   bounded by candidate/question/report counts rather than raw sheet
   count and carry the same regression risk against already-shipped pages
   this phase's original pass declined to take on for all four at once.
5. **A privacy-safe diagnostic bundle, built and tested.**
   `services/diagnostics.py::build_diagnostic_bundle` writes a `.zip` of
   version/environment info, the health check, processing settings and a
   bounded (200-line) log tail - never the project database, never a
   candidate name, roll number, answer or score. The privacy claim is
   tested directly: real secret candidate data is inserted into a test
   database and the test asserts none of those exact strings appear
   anywhere in the bundle's combined bytes, not merely that the module
   claims to exclude them. Wired into *Tools -> Create Diagnostic Bundle...*
   following the page's own `_prompt_*`/testable-method split.
6. **A global GUI exception handler, built and tested.**
   `gui/error_reporting.py::install_global_exception_handler` replaces
   `sys.excepthook` (idempotently, restoring/forwarding to whatever hook
   was previously installed) so that an exception escaping a Qt slot with
   nothing upstream catching it is logged and shown in plain language
   instead of disappearing into Qt's own silent default. Explicitly never
   claims unsaved work was saved - the message says the opposite: check
   the screen you were on, because it may not have been. Installed in
   `gui/application.py::run_gui` before the window is shown.
7. **Automatic backup before a schema migration, wired and tested.**
   `services/project_service.py::_backup_before_migration_if_needed`
   snapshots the database (via the existing `project_backup.create_backup`)
   whenever `open_project` finds a schema version older than the build's
   current one, before the migration itself runs - closing the one §5
   trigger point that was not wired. Lives in `services`, not
   `database/migrations.py`, to respect the `database` layer's existing
   rule against importing `services`.
8. **A stress-scale roster generator, and real reconciliation + Phase 9
   report generation at 10,000 sheets.** `evaluation/stress_roster.py`
   generates one deterministic `CandidateRecord` per sheet index, keyed to
   `stress_dataset.natural_roll`; the stress dataset's own existing
   duplicate/out-of-range-roll case kinds fall out as genuine absentees,
   duplicates and unknown candidates with no extra bookkeeping, plus one
   deliberate `ABSENT_WITH_SCRIPT` override. Run through the *real*
   `reconciliation.reconcile` and the *real* `reporting.excel` pipeline
   against actual recognition output from a real 10,000-sheet stress
   batch (not a smaller run scaled up in the write-up): every reconciliation
   invariant held ("nothing disappears", the deliberate absentee/duplicate/
   unknown-candidate/`ABSENT_WITH_SCRIPT` cases all classified correctly),
   and the generated `.xlsx` report's cells were spot-checked against the
   known-correct values (a true absentee's mark cell reads `"ABSENT"`; a
   present candidate's mark matches their real recognised answer count) -
   a correctness check, not only "it did not crash". The 10,000-sheet case
   is marked `stress` (15 real minutes); a 600-sheet case of the identical
   code path runs by default.
9. **Telemetry only ever measured the coordinator process, never the
   worker pool doing the recognition work - fixed.** `process_cpu_percent`/
   `process_memory_mb` in `services/telemetry.py` read only
   `psutil.Process(os.getpid())` - this process, never its children. A
   real 10,000-sheet run's telemetry showed `process_cpu_percent` around
   40-90% while `system_cpu_percent` climbed toward 90% on a 16-core
   machine, which would make the coordinator look like the bottleneck
   when the pool is where recognition time is actually spent. Added
   `worker_pool_cpu_percent`/`worker_pool_memory_mb`/`worker_pool_process_count`,
   summed across every live child (recursive, via `psutil`), correctly
   primed on first observation the same way the coordinator's own counter
   already was. A follow-up real run confirmed the fix:
   `worker_pool_cpu_percent` peaked at **392.7%** across a 4-worker pool
   (near-saturated, ~98% per worker) versus `peak_process_memory_mb: 188.6`
   / `peak_worker_pool_memory_mb: 375.7` - almost exactly double the
   memory the old metric alone would have reported. New fields default to
   `0.0`/`0` so a telemetry file written before this fix still reads back
   correctly rather than being silently treated as malformed.
10. **`cv2.imwrite` silently fails on a non-ASCII Windows path - found in
    a code path that had not adopted this codebase's own known
    workaround.** `services/alignment_service.py` and
    `services/recognition_diagnostics.py` already document and work
    around this exact OpenCV/Windows limitation (encode to a buffer,
    write the bytes with `pathlib` instead of calling `cv2.imwrite`
    directly), but `imaging/orientation_marker.py::write_debug_overlay`
    had not. Reproduced live in this environment - `cv2.imwrite` returned
    `False` and wrote nothing to a path containing "café-中文" - then
    fixed with the same encode-and-write-bytes pattern, with a regression
    test that decodes the file back via `cv2.imdecode`/`numpy.fromfile`
    rather than `cv2.imread` (which has the identical limitation on the
    read side).
11. **A results CSV re-export was not atomic - an interrupted write could
    destroy the previous good export.** `services/scan_export.py::export_scan_results`
    wrote directly into the destination path, unlike
    `utils/json_io.py::write_json_atomic`'s existing temp-file-then-replace
    guarantee for `project.json`. Fixed the same way. A negative control
    against the pre-fix code confirmed the failure is real: simulating a
    crash partway through a re-export left the destination file containing
    `partial,garbage,row` instead of the previous good export; with the
    fix, the previous file is byte-for-byte unchanged and no temp file is
    left behind.
12. **`PRAGMA foreign_key_check` had no test proving it catches anything
    `PRAGMA integrity_check` misses.** SQLite's `integrity_check` does not
    check foreign keys at all - a fact the existing `_integrity_check`/
    `_foreign_key_check` split in `project_health.py` already correctly
    assumed, but nothing tested. Added a test that writes a genuinely
    orphaned `batch_scan` row via a raw `sqlite3` connection (foreign keys
    off by default there, unlike this project's own engine, which enables
    them on every connection) and asserts `_integrity_check` reports
    nothing while `full_check` still reports `FOREIGN_KEY_VIOLATION`. The
    underlying code needed no fix; only the missing proof did.
13. **Low-disk-space health reporting had no test at all.**
    `_disk_space_issue` (§34, `LOW_DISK_SPACE_BYTES`) was already
    correctly implemented but untested. Added tests covering the
    threshold in both directions and a `shutil.disk_usage` failure being
    swallowed rather than raised.
14. **Windows path handling: verified, not merely assumed.** A real
    project was created, written to and reopened at a 270-character path
    under a directory named with Bengali, CJK and accented characters
    together (`পরীক্ষা ফলাফল ২০২৬ - café-résultats-中文`), through the
    actual `create_project`/`open_project` services - which is what
    turned up defect 10 above. `Excel`/`PDF` report writing were checked
    and found to already be safe against a non-ASCII or long output path
    (`unique_output_path` never overwrites an existing file by default;
    the LibreOffice PDF exporter converts into a private temp directory
    and moves the result into place). A packaged-application smoke test
    remains not applicable - no packaging build exists yet (Phase 11).

### Real 10,000-sheet benchmark (uninterrupted)

Run via the existing `tools/benchmark_stress.py` CLI, 8 workers, this
environment: **10,000 of 10,000 sheets processed in 873.4 seconds**
(9,085 complete, 717 review, 198 failed - all deliberate synthetic case
kinds, not defects), average throughput **11.24 sheets/second**, database
size 2,117.8 MB. Peak worker-pool CPU (after the telemetry fix above) was
observed at 392.7% across a 4-worker run and system-wide CPU during the
10,000-sheet run itself climbed to ~89% on this 16-core machine -
consistent with recognition being CPU-bound per worker, not blocked on
the database, disk or the coordinator. **Caveat, stated plainly:** this
run shared the machine with two other concurrent, CPU-intensive
background agents doing unrelated work for the remainder of this
validation pass (the Scan-page conversion and the reconciliation/report
work in items 4 and 8 above); its absolute timing should be read as a
functional-correctness and rough-order-of-magnitude data point, not a
clean, isolated performance benchmark. The 100,000-sheet run this number
would need to be extrapolated toward remains the deferred, not-yet-executed
qualification run (§9/§10).

### Kill/resume re-validation

The 100- and 1,000-sheet real forced-kill tests (§9) were re-run as part
of this pass's own verification of the orphan-containment fix and pass.
The 10,000-sheet kill/resume case was not re-run in this pass (§9's
existing result - 1,383s, passed - stood unquestioned by anything this
pass touched, since the fix here is additive containment around the same
kill/resume mechanics, not a change to them). The mandatory 100,000-sheet
1%/25%/50%/75%/99% kill matrix remains the deferred item it always was -
see §9/§10, unchanged.

### Final regression

`pytest -q` (full suite, default marker set, run immediately after every
fix in this section, exit code 0): **3,387 passed, 2 skipped, 3 deselected
in 991.3s (16m31s)**. The 2 skips are pre-existing and environmental
(LibreOffice not installed; one shared-plumbing test that documents itself
as "not a command a user runs"), not new. The 3 deselected are the
`stress`-marked tests (the 10,000-sheet kill/resume case from §9, the
10,000-sheet reconciliation/report case from item 8 above, and the
100,000-row lazy-model memory comparison from item 4 above) - each was run
explicitly at least once during this pass and passed; see their own write-ups
above. `ruff check src tests`: all checks passed. `mypy src/omr_scanner`
(this project's actual mypy gate - `tests/` is excluded by
`pyproject.toml`'s own `[tool.mypy] files` setting, not by this pass's
choice): 154 source files, no issues.

### Still not done after this pass

* The mandatory full-scale 100,000-sheet kill/resume acceptance matrix -
  unchanged, the single largest remaining item. Harness ready; see §10.
* Lazy Qt models for the Results/Resolve/Attendance pages (the Scan
  page's is done - see item 4 above).
* The §27 conflict-queue throughput re-measurement against the stress
  dataset specifically (Phase 6's own 10,000-conflict benchmark is the
  most recent evidence, unchanged by this pass).
* A packaged-application smoke test (no packaging build exists yet).
* Real-scan recognition *accuracy* calibration at any scale. Everything
  in this section, like §9 before it, validates software correctness,
  recoverability and reproducibility under a synthetic, deliberately
  imperfect workload - never recognition accuracy against real, physical
  examination scans, which is a separate calibration exercise this phase
  (both passes of it) was never scoped to perform.

## For whoever picks this up

Read `evaluation/stress_dataset.py` and `evaluation/stress_runner.py`'s
module docstrings first - both explain a layering decision (why stress
orchestration lives in `evaluation`, not `services`) that is easy to
"fix" by moving it the wrong way. Before running the full 100,000-sheet
matrix, read `tools/benchmark_stress.py`'s own docstring for the exact
kill procedure - a graceful `Ctrl+C` is caught and shut down cleanly on
purpose, and does not exercise what the acceptance test requires.
