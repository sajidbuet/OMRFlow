# Revised phase 5 handoff — intake sources and ledger (roadmap 0.1.1-D)

> Naming: handoffs in this directory are lettered in revised-phase order
> (A = revised 1 … D = revised 4), so revised phase 5 is `PHASE_E_HANDOFF.md`.
> It implements **roadmap phase 0.1.1-D**, not roadmap phase E.

Branch `feat/0.1.1-phase5-intake-ledger`, from `main` at `fd063f8` (the
Phase 4 merge; Phase 4 tip `98dc522`). Worktree `C:\Research\OMRflow-p5`.
**Not merged, not tagged, nothing released.**

## Implementation plan (written before code changes)

### What the code does today (inventory, 2026-10-02, `fd063f8`)

* **Manual intake.** *Add Folder* (`ScanPage.add_scan_paths`) only lists paths
  (`scan_import.collect_scan_files`: glob, supported suffix, natural sort); it
  writes nothing. *Process All* registers the batch (`scan_sessions.start_batch`
  → `batch_store.insert_batch`, one `batch_scan` row per path with size and
  float mtime). The worker thread then hashes the batch
  (`scan_provenance.compute_hashes_for_batch` → `hash_file`, SHA-256, 1 MiB
  streaming) and links exact duplicates before recognition
  (`scan_lifecycle.link_exact_duplicates`, session-wide, status `duplicate`,
  `scan_rejection.state = duplicate_content`).
* **Decoding** is `alignment_service.load_scan_image` (`np.fromfile` +
  `cv2.imdecode`). OpenCV 5.0.0 is installed and rejects every truncated
  JPEG/PNG/TIFF/BMP probed (cut 2 bytes to 90 %); `pyproject` allows
  OpenCV ≥ 4.9, whose libjpeg is lenient with truncated JPEGs - so OpenCV
  alone is not a sufficient completeness check.
* **No source, ledger, stabilisation, reachability or copy ingest exists.**
  Schema is 15. Scans are referenced in place (ADR-0002).
* **Defect found while inspecting:** the schema-14 upgrade fixtures
  (`tests/fixtures/schema14/*/database.sqlite`) were never committed -
  `.gitignore` ignores `*.sqlite` and only re-includes schema 12/13 - so
  `test_session_scope_migration` cannot pass on a clean checkout.

### Design

1. **Pure vocabulary** `domain/intake.py`: states, the transition table,
   reason codes, the stabilisation policy (configuration, not constants), the
   name/exclusion classifier, the "due for verification" rule.
2. **Image completeness** `services/image_integrity.py`: one function that
   takes the bytes of one read and decides: structural end-of-image check
   (JPEG EOI, PNG chunk walk to IEND, TIFF strip/tile extents, BMP pixel
   extent), TIFF page count (multi-page refused), and a full decode through
   the same `cv2.imdecode` call recognition uses (refactored out of
   `load_scan_image`, not duplicated).
3. **Filesystem boundary** `services/intake_fs.py`: a small protocol
   (`list_source`, `read_snapshot`) with the real `os.scandir` implementation
   (UNC and long-path aware, sharing-violation classification) and the
   content-addressed project copy store with temp-file + verify + atomic
   rename. Tests use a fake filesystem and an injectable clock.
4. **Migration 16** (additive): `intake_source`, `intake_source_attachment`,
   `intake_file` (the ledger), `batch_scan.intake_file_id` /
   `registered_at`, `scan_batch.source_id`; indexes for the per-source
   current-path lookup, hash, state/ready order and session.
5. **`services/intake.py`**: source CRUD + attach/detach (audited), lazy
   built-in manual source, reconciliation (listing outside transactions,
   one short write transaction per pass, verification reads outside
   transactions, compare-and-set commits), reachability, restart recovery
   (no I/O: unstable rows re-observe, ready rows flagged for re-verification,
   order kept), held diversion for closed sessions, the registration API
   (explicit item list → one finite batch, copy-ingest per the ADR, links,
   then Phase 4's `link_exact_duplicates` for duplicate content), manual
   recording (*Add Folder → Process All*) converging on the same ledger and
   duplicate path.
6. **Manual convergence**: the Scan worker's hashing step calls
   `intake.record_manual_batch`, which hashes with `hash_file` (as before),
   writes the same `batch_scan.content_sha256`, records the built-in manual
   source's ledger rows, and then the unchanged `link_exact_duplicates`.
   No GUI change.
7. **ADR-0008**: copy for watched sources, reference in place for manual.
8. **Project Health** ledger invariants.
9. **Tests**: state-machine unit tests, fake-filesystem scenarios, real
   temporary-directory integration, real writer subprocess killed mid-file,
   real intake subprocess killed and restarted, three-source acceptance with
   exact identity sets, migration from real schema-15 fixtures (written by
   `main` `fd063f8`), measurements.

Not in this phase: unit scheduler, background runner, recognition of intake
items, quality decisions, held-file decisions, GUI.

---

## 1. Status

| Track | Status |
|---|---|
| Implemented | **Yes** - everything in the plan above; headless |
| Automated tests | **Passing** (counts in §19) |
| Synthetic / temp-filesystem validation | Fake-filesystem scenarios, real local temporary directories, real writer and engine **process kills**, a seeded 2,400-arrival soak. **Not** the phase 9 synthetic intake campaign |
| Real network-share validation | **Not performed.** UNC paths exercised only as path shapes (fakes, `extended_path`); no SMB `mtime` caching, oplocks or disconnects tested |
| Real scanner validation | **Not performed** |
| Production qualification | **Not performed** |

Not operator-validated: there is no operator-facing watched intake in this
phase.

## 2. Branch

* Branch `feat/0.1.1-phase5-intake-ledger`; base `fd063f8` (`main`, the Phase 4
  merge); worktree `C:\Research\OMRflow-p5`; pushed, not merged, not tagged.
* Commits and tip: see §19 (recorded at the end).

## 3. Schema

* **Schema 16, migration 16** (`_migration_016_intake`), additive.
* New tables: `intake_source`, `intake_source_attachment`, `intake_file`.
* New columns: `batch_scan.intake_file_id` (FK `SET NULL`),
  `batch_scan.registered_at`, `scan_batch.source_id` (FK `RESTRICT`); all
  NULL for existing rows; model columns `deferred` (read-only schema-15 opens
  keep working).
* Indexes: `uq_intake_file_current_path` (partial, `is_current = 1`),
  `uq_intake_file_content`, `ix_intake_file_hash`, `ix_intake_file_state_ready`,
  `ix_intake_file_source_state`, `ix_intake_file_session`,
  `uq_intake_file_batch_scan` (partial), `uq_intake_attachment_live` (partial),
  `ix_intake_attachment_session`, `ix_intake_source_kind`,
  `ix_batch_scan_intake_file`, `ix_scan_batch_source`.
* Migration / backup tests (`test_intake_migration`, 8): real schema-15
  fixtures written by `main` `fd063f8` (`tests/fixtures/schema15`, committed
  with their databases). Read-only open: not migrated. Writable open: the
  pre-migration backup `before-migration-15-to-16` exists and is schema 15;
  schema 16; **no** source and **no** ledger row; every new link NULL; scans,
  statuses, hashes, audit-event count, sessions and each session's effective
  set identical; finite fixture's results and *Final export current*
  unchanged; Health clean; fresh and upgraded schemas identical (columns and
  intake indexes). The phase 4 duplicate still applies across the upgrade.

## 4. ADR-0008 - copy versus reference

* **Decision:** watched sources ingest by **verified, content-addressed copy**
  (`scans_original/intake/<aa>/<sha256><ext>`); the built-in manual source
  reads **in place** (unchanged ADR-0002 behaviour); a watched source may be
  set to `reference` explicitly.
* **Rationale:** scanner shares go offline and get cleared; counters reset;
  Resolve needs the image later.
* **Crash behaviour:** copy = re-read source and match the verified hash →
  write `.ingest-<random>.part` → flush + fsync → re-read and hash the temp →
  atomic rename → one commit. Crash before the rename: only a `.part`, removed
  by recovery. After the rename, before the commit: an unreferenced correct
  copy, reused by the retry. After the commit, before the duplicate link:
  completed by recovery. A different file already at the destination is moved
  aside (`.mismatch-<n>`), never deleted.
* **Source deletion:** after a copy, nothing downstream depends on the source;
  the ledger row stays as history. Sources are never modified.
* **Disk space:** one copy per unique page (exact duplicates cost nothing).
* **Sync folders:** a project in a synced folder uploads every copy and may
  present placeholders on another machine; recommendation: keep an active
  session's project on a local disk.

## 5. Source model

* Project-level `intake_source`; `kind` `watched` / `manual`; the manual source
  is built-in and lazily created (first *Process All*, or `manual_source()`).
* Session relationship: `intake_source_attachment` (≤ 1 live per source;
  history). A row is intended for the session attached when it was first
  observed; re-attaching moves nothing; rows observed while unattached and not
  yet registered join the next attached session; attaching to a closed session
  is refused; the manual source is never attached (its files belong to the
  batch being processed).
* Recursion per source; relative paths `/`-separated from the root; dot-folders
  skipped; symlinked folders not followed.
* Exclusions: `fnmatch` file and folder patterns; temporary names always
  ignored for watched sources.
* Reachability: `unknown`, `online`, `unreachable`, `permission_denied`,
  `disabled`, with detail and change time; `last_attempt_at`,
  `last_reconciled_at` (successful), `last_file_seen_at` / `_path`.
* Audited: created, updated, enabled, disabled, attached, detached.

## 6. Ledger model

Every persisted field and its purpose: [DATA_MODEL.md](../../../docs/DATA_MODEL.md),
*IntakeFile* (and *IntakeSource*, *IntakeSourceAttachment*). Key points:
uniqueness `(source_id, relative_path, content_sha256)`; one current row per
path; `(file_size, mtime_ns)` is the observation cache; `content_sha256` is the
hash of the bytes that were also fully decoded; `previous_intake_file_id` +
`path_reused` carry path history; `batch_scan_id` / `duplicate_of_scan_id` /
`ingest_path` / `registered_at` are the registration outcome; no recognition
state (`status`, `outcome`, readings) is stored (a test enforces it).

## 7. State machine

```text
(new)       -> discovered | ignored | ready (manual one-shot only)
discovered  -> stabilizing | ready | ignored | vanished | unreadable | unsupported
stabilizing -> stabilizing | ready | ignored | vanished | unreadable | unsupported
ready       -> registered | duplicate_content | stabilizing | vanished | held
registered  -> duplicate_content
vanished    -> discovered
held, duplicate_content, ignored, unreadable, unsupported -> (none)
```

Non-terminal: `discovered` (seen once), `stabilizing` (not yet proven),
`ready` (proven, unregistered), `vanished` (gone before registration;
reopenable). Terminal: `registered` (processing state now lives in
`batch_scan`), `duplicate_content`, `ignored` (name rule, or same bytes at the
same path), `unreadable` (stable but never decodes - an intake outcome),
`unsupported` (multi-page TIFF), `held` (closed session). **HELD in phase 5:**
entered when a ready row's session is closed (at reconciliation, or when
`register` is called for a closed session); it has no exit - release or discard
is phase 7's operator decision; the transition table needs no schema change
for that. Every other transition raises `IntakeTransitionError` (tested
exhaustively over all 100 state pairs).

## 8. Stabilisation

* **Observation rule:** the same `(size, mtime_ns)` on ≥ *K* consecutive
  passes (`observations`).
* **Time rule:** the first of them ≥ *T* seconds ago (`stable_since`), on the
  injected clock.
* **Empty files** are never read (`empty_file`), never unreadable.
* **Sharing / lock:** a sharing violation is `locked` - not an attempt; an ACL
  denial `access_denied` - not an attempt; both stay stabilising (and are
  reported as stalled after `stall_after_seconds`). Real-handle test: the CRT
  reports a sharing violation as plain `EACCES`, so `classify_open_error`
  re-probes with `CreateFileW`.
* **Hash + decode:** one `read()` of the whole file; `hash_bytes` and
  `check_image_bytes` on that buffer.
* **Post-read stat:** handle metadata before/after and path metadata after
  close must all equal the observation and the byte count; otherwise
  `changed_during_read`, observations reset.
* **Retry:** a decode failure counts an attempt; back-off
  `retry_backoff_seconds × 2^(n-1)`; `unreadable` after `max_decode_attempts`
  on one version; any metadata change is a new version (count reset; a terminal
  version is followed by a new row).
* Values (starting points, configuration): local K=2, T=5 s, 3 attempts,
  back-off 5 s, poll 10 s; network (UNC) T=15 s, back-off 15 s, poll 30 s;
  manual K=1, T=0.

## 9. Collision behaviour

| Case | Behaviour (tested) |
|---|---|
| Same filename, different source | Independent rows, each registered (`TestCollisions`, `TestThreeSources`, acceptance) |
| Same path, same bytes | Unchanged metadata: nothing, no re-read. Touched metadata: an `ignored / unchanged_content` row pointing at the holder; nothing registered |
| Same path, new bytes | New row, `path_reused`, previous row and its scan untouched; registered again as new content |
| Same bytes, different paths (one source) | `duplicate_content` linked to the first (phase 4 rule; in one call: linked to the first of the call) |
| Same bytes, different sources | `duplicate_content`, linked across sources |
| Same bytes, different sessions | Both registered; never merged |

## 10. Exact-content integration with phase 4

No second duplicate definition exists. Registration creates the `batch_scan`
rows with the verified hash and calls `scan_lifecycle.link_exact_duplicates`
(phase 4, unchanged: session-wide, live batches, re-import of rejected content
first); `intake.mirror_duplicates` then marks the ledger rows whose scans it
linked. The manual path calls the same function. Architecture tests: the
intake modules import no `hashlib` (hashes come from `scan_provenance`), and
only `scan_lifecycle` writes `duplicate_content` lifecycle links. The one
intake-side rule - a repeat **within one registration call** gets no second
scan (content-addressed copies would collide on `(batch_id, source_path)`) - is
retargeted to the effective sheet when phase 4 linked the first copy.

## 11. Manual Add Folder

Unchanged for the operator. *Process All* registers the batch as before; the
worker's hashing step is now `intake.record_manual_batch`: it runs the same
`compute_hashes_for_batch`, records each hashed scan in the manual source
(`relative_path` = the path as chosen), links scan ↔ row and batch → source;
then `link_exact_duplicates` + `mirror_duplicates`. A *Reprocess All* re-read
links to the existing rows; re-adding the same folder in the session yields
phase 4's duplicate scan with the ledger row unchanged; new bytes at a manual
path are `path_reused`. Manual readiness evidence is the operator's choice
(one observation, decode at recognition as before), so its rows carry no
decode-evidence fields - D9 compares name, size, `mtime_ns`, hash and state.

## 12. Watched-source storage

`batch_scan.source_path` = the project copy (absolute, as all scan paths);
`intake_file.absolute_path` / `relative_path` = where it was observed;
`ingest_path` = the copy, project-relative. `ingest_mode = reference` reads in
place and re-hashes at registration.

## 13. Restart / recovery

| State at kill | After reopen |
|---|---|
| discovered / stabilizing | observations 0, timer cleared: full quiet period again |
| ready (unregistered) | `reverify_required`; read + hash + decode again on the next pass; `ready_at` kept (order unchanged); different bytes → ready anew; a failure → stabilizing |
| registered, duplicate link not committed | `link_exact_duplicates` + mirror run by recovery |
| copy `.part` left | removed |
| copy renamed, not committed | row still ready → re-verified → registration reuses the copy |
| vanished / terminal | unchanged |
| unreachable source | unchanged until the next pass updates it |
| files written while stopped | found by the first pass |
| writer killed mid-commit (hot journal) | project reopens (fixed, §20) |

## 14. Source outage

`TestReachability::test_one_source_unreachable_does_not_touch_the_others`:
A, B, C online; B's rows recorded and partly registered; B made unreachable
while a file arrives on A → `reconcile_all` reports `[online, unreachable,
online]`; B's rows' `(state, present)` identical before and after, none
`vanished`; A's new file becomes ready; B's detail reads "not reachable".
B returns with a new file → found and ready; B's ledger 3 rows, nothing
duplicated. `test_permission_denied_then_restored` likewise. The acceptance
scenario repeats this with C registering during A's outage.

## 15. Registration API (what phase 6 receives)

* `IntakeService(database, project_root, fs=None, clock=utc_now, store=None)`
  (recovery runs).
* `reconcile(source_id)` / `reconcile_all()` → `ReconcileReport`.
* `ready_items(scan_session_id, source_id=None, limit=None)` →
  `ReadyItem(intake_file_id, source_id, scan_session_id, relative_path,
  absolute_path, content_sha256, file_size, ready_at)`, ordered
  `(ready_at, intake_file_id)`.
* `register(scan_session_id, source_id, intake_file_ids, identity, settings,
  started_by, seal=True, acknowledge_template_change=False)` →
  `Registration(batch_id, registered[(intake_id, scan_id)],
  duplicates[(intake_id, original_scan_id)], already_registered, returned,
  failed[(intake_id, detail)], held)`. One finite batch (`role = scan`,
  `source_id`, sealed by default) of `pending` scans in that order; nothing
  recognised. Phase 6 decides which items and when, then runs the unchanged
  `process_batch` on the batch.

## 16. Integration acceptance (`test_intake_acceptance`)

Three sources - A `\\scanner-a\scans` (UNC-shaped), B `D:\exam\Scanner B (room
2)`, C `E:\ছবি\scanner c` (recursive; folder exclusion `excluded`, name
exclusion `*_preview.jpg`) - one session; every hazard of §65 of the brief. The
end state is compared as an **exact set of 20 ledger identities**
`(source, path, state, content, path_reused)`:

* A: `000001.jpg` img1 registered + img9 registered `path_reused`;
  `000002.jpg`, `000003.jpg` (from `scan.tmp`), `000004.jpg` (arrived during the
  outage) registered; `scan.tmp` ignored; `gone.jpg` vanished.
* B: `000001.jpg` (same name as A's, other bytes), `000004.jpg` (zero-byte
  first), `000005.jpg` (written while stopped) registered; `000002.png` (slow
  writer) **two rows** - the two-thirds version unreadable after stalling
  through three retries during A's outage, the completed file registered;
  `book.tif` unsupported (3 pages).
* C: `sub/000010.jpg` (A's 000001 bytes) duplicate_content of A's scan;
  `held.jpg` (locked at first), `sub/000011.jpg` (written while stopped),
  `sub/000012.jpg` (during A's outage) registered; `broken.jpg` unreadable;
  `notes.txt`, `thumb_preview.jpg` ignored; `excluded/x.jpg` never listed.

13 `batch_scan` rows: 12 read-to-be scans, one per unique content, plus one
`duplicate`. Every intermediate pass asserted that no ready / registered row
holds bytes that are not a complete image. A restart mid-stabilisation and a
stopped period are inside the scenario.

## 17. Performance

`scripts/benchmark_intake.py`, one run each, **AMD Ryzen 9 5900HS (8C/16T),
31 GB RAM, NVMe SSD, NTFS, Windows 11, Python 3.12.7, OpenCV 5.0.0**. The full
baseline test suite was running concurrently (figures are pessimistic). Bulk
files are 907-byte synthetic JPEGs (listing and ledger costs, not I/O);
hash/decode uses 20 synthetic A4 300 dpi pages (mean 196 KB - real scans are
typically 0.2-1 MB). Listings were warm-cache (files just written); **no
cold-cache and no SMB figure is claimed**.

| Seconds | 1,000 files | 10,000 files |
|---|---:|---:|
| `os.scandir` listing (twice) | 0.005 / 0.006 | 0.060 / 0.058 |
| Reconcile, first (insert every row) | 0.381 | 4.357 |
| Reconcile, second (observe every row) | 0.149 | 1.613 |
| Reconcile, verify (read + hash + full decode every file) | 1.718 | 16.080 |
| Reconcile, steady (every row ready, nothing changed) | 0.148 | 1.015 |
| Register all (units of 500: copy + verify + commit + duplicate link) | 7.90 | 54.77 |
| Reconcile after registration (every row terminal) | 0.054 | 0.501 |
| Restart (recovery + first reconcile) | 0.588 | 1.215 |
| Register 500 exact duplicates (phase 4 link + mirror) | 2.37 | 2.59 |

* Hashing: ~1,100-1,400 MB/s; hash + structural check + full decode: 11.7 ms
  per 196 KB page (decode dominates).
* **Found and fixed by this measurement:** the first 10,000-file run showed a
  quiet poll costing 3.26 s (28× the 1,000-file figure): an ORM bulk `UPDATE`
  of `last_seen_at` re-evaluated against every loaded row per 500-id chunk, and
  every settled row was rewritten every pass. After the fix the ratios are
  linear (7-11×).
* Registration: ~5.5 ms/file; a profile of one 500-file unit attributes 57 %
  to the copy store's per-file open / fsync / rename / re-hash on Windows, the
  rest to per-row SQL. Linear.
* Duplicate lookup: phase 4's `link_exact_duplicates` uses
  `ix_batch_scan_content`; 500 duplicates against 10,000 registered rows cost
  the same as against 1,000.
* Query plans: the per-pass load uses `ix_intake_file_source_state`
  (`source_id`; history rows included - SQLite without `ANALYZE` statistics
  does not choose the partial current-path index); the content lookup uses
  `uq_intake_file_content`; ready order uses `ix_intake_file_state_ready`.
* **Stabilisation latency** (real clock, T = 2 s, K = 2, poll every 0.5 s, 30
  files written in 1 KB chunks): last byte → ready min 2.14, median 2.42, p95
  2.61, max 2.67 s - i.e. *T* plus up to about one poll interval, as designed.
  With the starting values (T = 5 s, poll 10 s) expect roughly 5-20 s; not
  measured on a share.

## 18. Project Health

New `_intake_issues` (all ERROR; none for transient states; nothing repaired;
`test_intake_health`, 9): `INTAKE_REGISTERED_WITHOUT_SCAN`,
`INTAKE_LINK_BEFORE_REGISTRATION`, `INTAKE_LINK_MISMATCH`,
`INTAKE_HASH_MISMATCH`, `INTAKE_SESSION_MISMATCH`,
`INTAKE_DUPLICATE_WITHOUT_ORIGINAL`, `INTAKE_MALFORMED_HASH`,
`INTAKE_COPY_MISSING`, `INTAKE_MANUAL_SOURCE_DUPLICATED`. A live attachment to
a missing session cannot occur (foreign key, `RESTRICT`) and is left to the
existing foreign-key check.

## 19. Tests

New tests (304, plus 1 `stress`):

| File | Tests | Covers |
|---|---:|---|
| `tests/unit/test_intake_rules.py` | 147 | every allowed transition; every one of the other state pairs refused; initial states; name and exclusion rules; policy; quiet-period edges |
| `tests/unit/test_image_integrity.py` | 35 | complete JPEG/PNG/TIFF/BMP; header + truncated body at 10-99 %; last bytes missing; zero bytes; random bytes; multi-page TIFF; truncation refused even with a lenient decoder |
| `tests/integration/test_intake_ledger.py` | 61 | fake filesystem + clock: main path, diversions, growing files, writer patterns, collisions, recursion, reachability, restart, registration API |
| `tests/integration/test_intake_filesystem.py` | 18 | real directories: spaces / Unicode / nesting / long paths, a real exclusive handle, three sources, copy integrity and crash points, a real close/reopen mid-stabilisation and ready-but-unregistered |
| `tests/integration/test_intake_sessions.py` | 7 | session ownership |
| `tests/integration/test_intake_manual.py` | 14 | manual convergence, D9, one duplicate rule / one hash routine (architecture) |
| `tests/integration/test_intake_migration.py` | 8 | migration 16 from schema-15 fixtures |
| `tests/integration/test_intake_health.py` | 9 | Health findings |
| `tests/integration/test_intake_acceptance.py` | 1 | the three-source scenario |
| `tests/integration/test_intake_processes.py` | 2 | a real writer killed mid-file; the real engine killed while three real writers continue, then restarted |
| `tests/integration/test_hot_journal_reopen.py` | 1 | reopen after a real kill mid-commit (fails without the fix) |
| `tests/gui/test_manual_intake_gui.py` | 1 | the real Scan stage records into the manual source |
| `tests/integration/test_intake_soak.py` (`stress`) | 1 | 2,400 seeded arrivals, random restarts |

Changed tests (each with an in-test explanation):
`test_session_scope_migration` - two assertions pinned the *current* schema to
15; they now accept the current schema (migration 16 runs after 15).

| Run | Result |
|---|---|
| Baseline, clean worktree of `main` `fd063f8` (`C:\Research\OMRflow-base`), `QT_QPA_PLATFORM=offscreen` forced | **6,398 passed, 45 skipped, 5 failed**, 5 deselected (62 min). Failures: the 4 `test_session_scope_migration` upgrade tests (their fixture databases were never committed - fixed on this branch) and `test_resolve_page::...test_nothing_is_clipped_at_a_supported_size[1366-768]`, which fails only with offscreen forced (passes on the native platform the suite uses on Windows; checked on the baseline both ways) |
| Branch, first full run (`52fcbab`'s parent), offscreen forced | **6,704 passed, 45 skipped, 3 failed**, 6 deselected (56 min): the same offscreen-only layout test, and two regressions this branch introduced - a hard-coded version string in `image_integrity` (`test_version`) and migration 16's description tripping `test_no_schema_change_was_needed`'s heuristic. Both fixed (`52fcbab`); the affected files re-run: 169 passed |
| **Branch, final full run** (`6dfceb3`, the suite's normal native Qt platform on Windows) | **6,723 passed, 29 skipped, 0 failed**, 6 `stress` deselected (1 h 07 min). 6,419 (phase 4's final count) + 304 new non-stress tests = 6,723; the 29 skips are the count phase 4 recorded |
| Targeted phase 3/4 suites (`tests/crash`, exact duplicates, session population / acceptance / scan sessions, crash-safe persistence, provenance, Scan / session / crash-reopen GUI, both session migrations, architecture) | **281 passed** (11 min) |
| `tests/crash -m stress` (1,000-sheet kill series) | **1 passed** (2 min 40 s) |
| `-m stress tests/integration/test_intake_soak.py` | **1 passed** (2 min 29 s) |
| `tests/local` (real sheets) from the main checkout against the branch's `src` | **13 passed, 10 skipped** (fixture-dependent; the same as phase 4 recorded) |
| `ruff check src tests tools scripts` / `mypy src/omr_scanner` | clean / no issues (216 files) |
| Mutation check | removing the post-read metadata comparison makes `test_file_changing_during_the_read_returns_to_stabilization` fail (restored) |

Skips: with offscreen forced, 45 - of which 18 are tests that skip under
offscreen by design (Windows-style template-designer tests, the window-manager
title) and 23 are `tests/local` real-sheet tests whose untracked fixtures exist
only in the main checkout; on the native platform, 29.

New tests: 304 non-stress (the counts above include the lenient-decoder test
added last) + 1 `stress`.

## 20. Roadmap / code discrepancies

* **Defect (pre-existing, fixed):** after a process was killed mid-commit, a
  hot rollback journal made `open_project` fail - the pre-migration read-only
  probe raised an uncaught `OperationalError` ("attempt to write a readonly
  database"). Found by this phase's real-kill test (intermittently, when the
  kill landed in a commit), reproduced deterministically, fixed with
  `database.recover_interrupted_transaction`; regression test with a real kill
  fails without the fix. Phase 3's kill matrix passed without catching it.
* **Defect (pre-existing, fixed):** the schema-14 fixture databases were never
  committed (`.gitignore`); `test_session_scope_migration` could not pass on a
  clean checkout.
* **Finding:** Python's `open()` on Windows reports a sharing violation without
  `winerror`; the fake filesystem could not have shown it.
* **Finding:** OpenCV 5.0.0 rejects truncated images that OpenCV 4.x's libjpeg
  accepts; completeness does not rely on either.
* Prompt 04 asked for an engine object with a background runner thread; the
  brief for this phase excludes any continuous engine, so **no runner** was
  built: `IntakeService.reconcile` is the step phase 6 will schedule.
* Prompt 04 names the handoff `PHASE_D_HANDOFF.md`; that name is taken by
  revised phase 4, so this is `PHASE_E_HANDOFF.md`.
* The roadmap names `intake_file`, `intake_source`, `batch_scan.intake_file_id`,
  `registered_at`, `scan_batch.source_id` - used as named. Added beyond it:
  `intake_source_attachment` (explicit session history) and the `unsupported`
  state (multi-page TIFF is neither unreadable nor a name rule).
* The roadmap's diagram lets `duplicate_content` follow `ready` directly; here
  a duplicate is first registered as a `batch_scan` (status `duplicate`, never
  read) because phase 4's rule operates on scans - so the ledger path is
  `ready -> registered -> duplicate_content` (or `ready -> duplicate_content`
  for an in-call repeat).
* ARCHITECTURE_NOTES §10.2 lists "last seen" per row; for settled rows it is
  written only when presence changes (§17's measurement).
* `docs/wiki/Upgrade-Compatibility.md` still said schema 14 after phase 4;
  corrected. `README.md` still called phase 4 unmerged; corrected.

## 21. Known limitations

### Phase 5

* Headless only; nothing in the GUI shows sources or the ledger.
* No real SMB / network share, no real scanner, no power-loss testing (process
  kills only).
* The stabilisation values are starting points measured on a local SSD.
* Kills were landed at random points of a live loop and at chosen points by
  simulation (copy failures, duplicate link not committed); not every
  individual transaction boundary was hit with a real kill.
* A file whose writer stalls longer than the decode retries is recorded
  `unreadable` before it completes; the completed file is then a new row (no
  file is lost, but the ledger holds that history).
* A file held open with read sharing and fully written is ready when stable -
  open handles are not detectable portably.
* Read-only open of a project with a hot journal still fails until a writable
  open has recovered it (the read-only path cannot roll back by design).
* Unreferenced project copies are not cleaned up; no storage quota.
* `batch_scan.source_path` stays absolute (existing behaviour).
* Settled rows' `last_seen_at` is not refreshed every pass (documented).
* A 0-byte stray file `src/omr_scanner/services/intake_fs.py` was created in
  the **main checkout** (`C:\Research\OMRflow`) by a mistaken relative-path
  write during this work; deleting it was blocked by the session's permission
  policy - it should be deleted by hand (it is untracked; it would fail the
  main checkout's docstring architecture test).

### Deferred to phases 6-8

* Phase 6: the unit scheduler (which ready items, unit size, trickle timeout),
  the background engine and its restart sequence, the writer strategy for
  concurrent continuous work, continuous recognition and conflict sync.
* Phase 7: what to do with held, unreadable, unsupported and duplicate files;
  quality decisions; stall alarms; session controls.
* Phase 8: source management, intake status and every other GUI.
