# Phase 5 handoff — Batch Scan Processing Pipeline

**Implemented:** 2026-09-19
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2, OpenCV
5.0.0, NumPy 2.5.3, SQLAlchemy 2.x, 16 logical CPUs

This document is the entry point for whoever continues the work. Read alongside
`docs/scan_workflow.md` §11-§13 (the operator-facing description),
`docs/DATA_MODEL.md` (the two new tables) and `docs/ARCHITECTURE.md`
("Durable batches (Phase 5)").

**Phase 5 makes a batch reliable and resumable. It says nothing about whether
the values it durably recorded are correct.** Phase 3 recognition remains
pending validation with a sufficiently large real-world dataset.

---

## 1. What already existed

Phase 5's brief said not to assume it starts from scratch, and it did not. The
following were already implemented, correct, and were **preserved unchanged**
except where noted:

| Capability | Where | Verdict |
|---|---|---|
| Scan discovery, recursive/non-recursive, natural sort, format filter | `services/scan_import.py` | Kept as-is |
| Batch loop with per-file error isolation | `services/batch_processor.py` | Kept as-is |
| Multiprocessing pool, `spawn` on every platform, template sent once per worker | `services/parallel_batch.py` | One addition (§4) |
| Worker-crash isolation (a dead process becomes one failed *scan*) | `parallel_batch._outcome_of` | Kept as-is |
| Cooperative cancellation | `batch_processor` + `parallel_batch` | Kept as-is |
| Worker-count setting (Automatic / Single core / Custom) with a measured cap | `config/processing.py`, Settings dialog | Kept as-is |
| Progress, elapsed, smoothed ETA, throughput, counters | `services/batch_progress.py` | Kept as-is |
| Qt threading model: `QThread` coordinator, pull-based progress, no widget access off the GUI thread | `gui/scan/worker.py` | One addition (§4) |
| Duplicate-identifier allocation (`_a`, `_b`, … `_aa`) | `services/filename_manager.py` | Kept as-is |
| Copy-never-move renaming | `batch_processor._copy_to_output` | Kept as-is |
| CSV export | `services/scan_export.py` | Kept as-is |
| Throughput benchmark script | `scripts/benchmark_batch.py` | Kept as-is |
| Migration framework with a documented "how to add one" procedure | `database/migrations.py` | Followed exactly |

**Nothing was rewritten.** No second batch-processing path, no second
recognition path, no replacement for the worker pool.

## 2. What was missing

Persistence, and everything that depends on it: a batch state model, durable
per-scan records, incremental commits, resume, retry, template/settings
compatibility guarding, batch identity, reopening a batch, crash recovery, and
handling a *storage* failure differently from a *recognition* failure. Plus
three smaller gaps: submission backpressure, a close-during-processing warning,
and results filtering.

---

## 3. Implementation summary

### `database/models.py`, `database/migrations.py` (extended)

Two tables, added by **migration 2** — `scan_batch` and `batch_scan` — plus the
`BatchStatus` and `ScanJobStatus` enums. Field-by-field description:
`docs/DATA_MODEL.md`.

Migration 1 was pinned to the two tables that actually belonged to version 1.
It previously called `Base.metadata.create_all` with no table list, which would
have meant a brand-new database got the Phase 5 tables from migration 1 and
then migration 2 tried to create them again, while an existing database took a
different path to the same schema. One table, one migration, the same sequence
for every database.

### `services/batch_store.py` (new, ~700 lines)

The repository layer. `create_batch`, `record_results`, `mark_queued`,
`mark_cancelled`, `recover_interrupted`, `resumable_scans`, `failed_scans`,
`scan_paths`, `completed_results`, `check_compatibility`, `load_summary`,
`list_batches`, `finalise_batch`, plus `BatchIdentity`, `BatchSummary`,
`CompatibilityVerdict`, `ErrorCategory` and `BatchRecorder`.

Contains no Qt and no recognition. It is attached to a run through
`process_batch`'s **existing** `on_result` hook, which is why a caller with no
project — a test, the benchmark, `python -m omr_scanner.tools.recognise` — runs
exactly the code path it always did.

### `gui/scan/worker.py` (extended)

`BatchWorker` gained an optional `recorder`. It records each result before
emitting `scan_done`, flushes before emitting `finished_report`, and exposes
`persistence_failure` so the page can report a batch whose results were
computed but not stored.

### `gui/scan/page.py` (extended)

Batch lifecycle (`_ensure_batch`, `_settle_batch_state`,
`_report_persistence_failure`), `resume_batch`, `retry_failed`, `adopt_batch`,
`_confirm_compatible`, the status filter, the batch-state label, and
`is_processing`/`shutdown_batch` for the main window to drive.

### `gui/main_window.py` (extended)

`_recover_interrupted_batches` on project open; a close-during-processing
confirmation that stops and *waits for* the batch before releasing the
database.

---

## 4. Multiprocessing: what changed, and what did not

**What did not change:** the pool, the start method, the worker functions, the
initialiser, the crash handling, the cancellation protocol, the fallback to
sequential when a pool cannot start, or the worker-count policy. Phase 5 did
not touch recognition inside a worker at all.

**What changed:** submission is now bounded.

`recognise_in_parallel` previously submitted every path up front, building one
`Future` per scan before the first page was read. For ten thousand scans that
is ten thousand futures and ten thousand queued messages, which costs memory,
delays the first result and makes cancellation slower. Submission is now capped
at `QUEUE_DEPTH_PER_WORKER` (4) times the worker count, topped up before every
wait. Four per worker is deep enough that no worker ever waits for the parent
to hand it the next page, and shallow enough that "stop" means stopping within
a page or two.

The jobs themselves were already tiny — an index and a path, never an image —
and still are.

**Windows safety**, audited and unchanged: `spawn` explicitly on every platform
(never the fork default), module-level worker functions so they are importable
by name in the child, `multiprocessing.freeze_support()` in `main()`, template
and options sent once per worker through the initialiser rather than per task,
and nothing Qt-shaped anywhere near a worker.

**Cancellation and the pool:** `should_cancel` is polled between completions;
already-queued futures are cancelled, sheets already inside a worker are
allowed to finish (OpenCV cannot be interrupted mid-warp), and the executor is
shut down with `wait=True`. The Qt side cancels *and waits* on shutdown, so no
worker process outlives the window.

**Measured, 24 copies of the real sample sheet, 16 logical CPUs:**

```text
 workers    seconds    scans/s   speed-up   read
       1       8.02       2.99      1.00x     24
       2       6.15       3.90      1.30x     24
       4       4.47       5.37      1.79x     24
       8       4.05       5.92      1.98x     24
```

Multiprocessing genuinely works and has not silently fallen back to sequential.
The speed-up is lower than Phase 3's recorded 3.4x→11.6x figures because this
run used 24 scans rather than 48, so pool start-up is a larger share of a
shorter run; it is reported as measured rather than reconciled.

---

## 5. Persistence and resume

**Where:** the project's own SQLite file, `<project>/database.sqlite`, through
the existing `ProjectDatabase` and its transactional `session()` scope. No new
storage mechanism, no second database, no giant JSON file.

**What is persisted:** see `docs/DATA_MODEL.md`. The recognition result itself
is stored as JSON via `ScanResult.to_dict()` — already a versioned,
round-tripping contract — rather than exploded into columns that would need a
migration every time recognition gained a measurement.

**How resume works:**

1. Every scan is registered `pending` before the first sheet is read, so a
   crash one second into a run still leaves a resumable record of the intent.
2. Finished sheets are committed in groups of 25, or every 2 seconds, whichever
   comes first.
3. `resumable_scans` returns `pending`/`queued`/`processing`/`cancelled` rows in
   **batch order** — which is what keeps duplicate-identifier suffixes stable
   across an interruption.
4. Opening a project runs `recover_interrupted`, which returns stale
   `queued`/`processing` rows to `pending` and marks their batch `interrupted`.

**Only the coordinating process writes.** Worker processes return recognition
results and nothing else. SQLite is a single-writer store and the architecture
keeps it that way by construction rather than by locking discipline — the same
rule that already stopped workers from naming files.

**Cross-thread writing was verified empirically**, not assumed: the database is
opened on the GUI thread and written from the `BatchWorker` thread, and a probe
confirmed SQLAlchemy's pooling permits it. That probe also caught a real bug
before it reached a test — child rows were being inserted before their parent,
failing the foreign key — now fixed with an explicit `session.flush()` and a
comment explaining why it is load-bearing.

---

## 6. Tests added

| Level | File | Count | Covers |
|---|---|---|---|
| Unit | `tests/unit/test_batch_store.py` | 35 | Registration, batch order, per-outcome counting, result round-trip, attempt counting, resume selection (including out-of-order completion), cancellation, crash recovery, final status derivation, all four compatibility differences, recorder buffering, storage-failure reporting and recovery, and that every job status is either terminal or resumable but never neither. |
| Integration | `tests/integration/test_batch_persistence.py` | 17 | Plan tests A, B, C, E, F, G, H, I, J, K against the real engine and real rendered sheets. |
| GUI | `tests/gui/test_scan_persistence.py` | 20 | Plan tests D, E, L through the real page, a real project and a real `QThread`; plus retry, filtering, close-during-processing and crash recovery through `MainWindow`. |
| Smoke | `run_gui_smoke_tests.py` | +3 | Batch recorded; a partial run resumed (2 read, 2 pending, 2 resumed, 4 final); originals byte-for-byte unchanged — all against `examples/ECE-0000.png`. |

**Test J (source integrity) is the mandatory exit criterion** and is asserted
twice: once headlessly over a batch that includes renaming and a deliberately
corrupt file, and once through the GUI harness against the real sample.

---

## 7. Known limitations

- **No real examination-scale run.** The largest measured batch is 48 real
  scans (Phase 3's benchmark); Phase 5's own largest is 24. Ten thousand scans
  has been simulated for the progress/counting path only, and never with ten
  thousand database rows.
- **Storage failures are tested by injection, not in situ.** No test has run
  against a network share, a synchronised folder or a genuinely full disk —
  the three places a real examination office would most plausibly hit one.
- **Crash recovery is tested by simulating the state a crash leaves**, not by
  killing a live process mid-write.
- **Windows only, one machine, 16 threads.** Linux, macOS and low-memory
  machines are untested.
- The flush thresholds (25 sheets, 2 seconds) are reasoned and tested but not
  tuned against a slow or contended disk.
- `adopt_batch` reopens a batch's scans and results, but there is no *batch
  browser* UI yet: it is a public method a future phase or a script can call,
  not a dialog. `list_batches` exists and is tested for it.
- A resumed run starts a fresh `FilenameAllocator`, so it consults the output
  folder's existing contents rather than the previous run's in-memory history.
  That is correct — the allocator was always designed to do this, and the
  existing files are exactly the evidence — but it means the suffix a sheet
  receives depends on what is on disk, not on what the earlier run remembered.
- Deleting a batch is implemented (`delete_batch`) but not exposed in the GUI.

---

## 8. Phase 3 and Phase 4 status — unchanged

Phase 5 did not validate, and could not validate, either of them.

**Phase 3 recognition remains pending validation with a sufficiently large
real-world dataset.**

Phase 4's calibration workflow is unchanged by this phase; its own open items
stand as recorded in `development/PHASE_04_HANDOFF.md`.

---

## 9. Phase 6 entry criteria

**Already in place**

- A durable, queryable per-scan record with a stable identity — which is what a
  conflict queue must point at.
- `ScanResult` round-tripping through the database, so a review screen can
  reconstruct exactly what the engine saw without re-reading an image.
- An error category vocabulary for triage.
- The migration pattern demonstrated a second time, with a worked example of
  pinning an older migration's table list.

**Constraints to respect**

1. Everything in `development/PHASE_03_HANDOFF.md` §15 and
   `PHASE_04_HANDOFF.md` §15 still applies.
2. Only the coordinating process writes to the database. A future phase that
   wants concurrent writers needs a deliberate architecture, not an incremental
   loosening of this one.
3. `FieldValue` as its own table is Phase 6's to add (`docs/DATA_MODEL.md`);
   the machine value must never be overwritten by a human correction.
4. Do not let "the batch completed" start meaning "the results are correct".
   `BatchStatus.COMPLETED` vs `COMPLETED_WITH_ERRORS` exists precisely to keep
   those apart, and the distinction is load-bearing for how this software may
   be described.
