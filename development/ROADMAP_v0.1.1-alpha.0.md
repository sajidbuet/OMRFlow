# v0.1.1-alpha.0 — Implementation roadmap

> **Status: proposed, awaiting review. Nothing in this document is
> implemented.** It is a plan, written on 2026-09-29 from an inspection of the
> code at `main` `e9dd813`. Every "today" statement below is from reading that
> code (file references are to `src/omr_scanner/`); items marked *by code
> reading* were not reproduced by running the application.
>
> Canonical status lives in [`docs/wiki/Development-Roadmap.md`](../docs/wiki/Development-Roadmap.md);
> this is the working detail for the release, in the same spirit as
> [`ROADMAP.md`](ROADMAP.md) for Phases 0–10.

---

## Release objective

Move OMRFlow from a *finite, single-folder, single-sitting* workflow to one
that stays correct when an examination is scanned **by several scanner
stations, over hours, with files arriving while processing runs, the
application restarted in between, and rejected scripts rescanned later** —
while fixing the set-code identity problems found during alpha.2 validation.

The architecture stays:

    scanners write image files  ──►  OMRFlow discovers, registers and processes them robustly

OMRFlow does not control scanners, does not become a server, and does not let
anything but itself write its database.

---

## Baseline

**Released:** `v0.1.0-alpha.2` (tag, 2026-09-24).

**Actual working baseline:** `main`, 29 commits past that tag, **unreleased**:
Reject & Rescan (migration 11) and its cross-batch hardening, the Attendance
reconciliation workstation, per-set scoring rosters, the Answer Key (Step 7)
rework with answer-key provenance (migration 12), synthetic solution sheets
and write-ins. `CHANGELOG.md` `[Unreleased]` does not yet describe the Step 7
rework; it should before any release is cut.

Schema version **12** (`database/migrations.py`, `SCHEMA_VERSION =
MIGRATIONS[-1].version`). The next migration number is therefore **13**.

What the code does today, relevant to this release:

| Area | Today |
|---|---|
| Intake | Explicit *Add Scan(s)* / *Add Folder* (single folder, non-recursive) into an **in-memory** list (`gui/scan/page.py` `add_scan_paths`); nothing reaches the database until *Process All*. Images are **read in place**; `source_path` is stored as given, unresolved (`services/batch_store.py`). No watching, polling or "import new files". |
| Batch | `scan_batch`: template identity + fingerprints + `status` (`new/running/interrupted/cancelled/completed/completed_with_errors`). A batch **already accepts more files at any time** (`add_scans_to_batch`); there is no closed state. *Reprocess All*, and the first run after the project is reopened, create a **new** batch. |
| Scan row | `batch_scan`: `status` (`pending/queued/processing/completed/warning/failed/cancelled` — `processing` is never written), outcome, identifier/set readings, `result_json`, size, mtime, `content_sha256` (computed at run start, **not at registration**). Unique only on `(batch_id, source_path)`. |
| Processing | `ProcessPoolExecutor` (spawn), bounded submission (`workers × 4`), pool recycled every 500 sheets, 1 OpenCV thread per worker; workers never touch the DB; results buffered and committed every 25 sheets / 2 s from the batch QThread (`services/parallel_batch.py`, `batch_processor.py`, `batch_store.BatchRecorder`). **No pause.** *Cancel* discards the in-flight sheets' results and marks them `cancelled`. |
| Recovery | On open, `recover_interrupted` resets `queued/processing` → `pending`, `running` batches → `interrupted`; *Resume Batch* replays in `batch_index` order after an advisory template-compatibility check. Kill/resume measured at 100, 1,000 and 10,000 sheets (Phase 10). `processing_manifest` exists (migration 7) but **nothing writes it**. |
| Exact duplicates | Two active scans with identical bytes are **not flagged or skipped**. `scan_provenance.duplicate_groups` exists but has no caller. Hash is used only for re-imports of rejected sheets, replacement refusal, relink and purge. |
| Duplicate Student ID | `review_store.sync_duplicate_identifiers` — **per batch, on the machine reading** (ignores corrections). Reconciliation marks `DUPLICATE_SCRIPT`; scoring blocks unless one script is primary. |
| Reject & Rescan | `scan_rejection` (unique per scan) holds the current link `scan_id → replacement_scan_id`; history in `audit_event`. Replacement may come from **any** batch; it "counts in the batch of the sheet it replaces" (`adopted_replacements`, `counted_elsewhere`). Re-import of a rejected image detected by hash (`sync_reimports`). |
| Downstream scope | **Every stage works on one `batch_id`.** Attendance, Results and Reports default to `list_batches(limit=1)` ordered by `updated_at` (`gui/attendance/page.py`, `gui/results/page.py`, `gui/reports/page.py`); reconciliation and results are keyed `(roster_id, batch_id)`. The only multi-batch read is adoption of confirmed cross-batch replacements. |
| Set codes | One bare string everywhere; no logical/physical mapping, active or dormant. Case handling inconsistent: registry, Resolve, Attendance, Reports compare exactly; scoring upper-cases before key lookup; keys stored as typed. |
| Database | SQLite, rollback journal (WAL deliberately off, ADR-0002 — synced/network folders), `foreign_keys=ON` only, **no `busy_timeout`**, single process guarded by a project lock file. Writers today: GUI thread, batch QThread (recorder, hash pass). |
| GUI scale | Scan list is a lazy `QAbstractTableModel` (100k rows: 2.8 KB / 0.29 ms) but every `ScanEntry` is held in memory; Resolve pages 500 rows in SQL; Attendance, Results, Reports are `QTableWidget`s filled completely (bounded by candidates, deliberately deferred in Phase 10). |
| Telemetry | Scan page: EMA rate, ETA, workers, finish time (`services/batch_progress.py`). `services/telemetry.py` (JSONL sampler) is used only by stress tools. No queue-depth or backlog figure. |
| Scale evidence | 10,000 sheets in 873 s (≈11.2 sheets/s, 8 workers, shared machine — rough order of magnitude), DB 2,117.8 MB. Kill/resume at 10,000 passed. **The 100,000-sheet campaign has never been run** (preflight estimate 22 h 30 min, 53.5 GB). |

### Latent alpha.2 defects this release must address (documented here, not fixed)

1. **Late scans can split the cohort** *(by code reading)*. After a project
   is reopened the Scan page has no batch (`on_project_changed` sets
   `state.batch_id = None`), so the next *Process All* creates a second batch.
   Attendance, Results and Reports then read only the most recently updated
   batch: candidates whose scripts are in the first batch appear **absent**.
2. **The batch Results reads can change silently** *(by code reading,
   confirmed at `services/batch_store.py:1061`, `:588`, `:710`)*. "Latest" is
   `ORDER BY updated_at DESC`. `updated_at` is bumped when failed sheets in an
   older batch are retried, **and by `recover_interrupted` on every project
   open** — so an older batch left *interrupted* becomes the one
   Attendance/Results/Reports read as soon as the project is opened.
3. **Duplicate Student IDs are detected on the machine reading, per batch.**
   A corrected ID that collides with another sheet is not flagged; the same
   candidate in two batches is never flagged.
4. **Byte-identical images are processed twice** when added under different
   paths (e.g. copied from two stations), producing two active scripts.
5. **Set-code case mismatch.** A lower-case set code cannot find its answer
   key in Results (key stored as typed, lookup upper-cased).
6. **`processing_manifest` is never written**, although documented as the
   batch's processing record.

---

## Architectural principles

1. Preserve raw input; derive effective values in one place.
2. One central definition of set identity (canonical logical code).
3. Discovery is idempotent: seeing the same bytes again never creates a
   second active script.
4. The durable record is the database, never GUI state or filesystem events.
5. Filesystem notifications are hints at most; a **polled reconciliation of
   each source folder is the source of truth**.
6. A rescan is linked to the script it replaces, never treated as a new
   candidate.
7. Extend `scan_batch` / `batch_scan` / `scan_rejection` / `audit_event`;
   do not build a parallel pipeline.
8. The finite-folder workflow (*Add Folder → Process All*) keeps working
   unchanged.
9. Single writer to SQLite, deliberately; concurrency comes from worker
   processes that never touch the database.
10. Large-project views stay lazy and SQL-filtered.
11. Deterministic, reproducible processing and reprocessing.

---

## Terminology and batch semantics (recommendation)

| Term | Meaning in v0.1.1 | Stored as |
|---|---|---|
| **Project** | One examination. | project folder, `project.json` |
| **Intake source** | A place scans come from: *Scanner Station 1*, *Network drop*, *Manual import*. Stable identity, **project-level** (a source outlives batches). | new `intake_source` table |
| **Intake file** | One file seen in a source folder, tracked from discovery to registration. The discovery ledger. | new `intake_file` table |
| **Scan batch** | *Unchanged meaning:* one reading of the examination's scripts with one template identity. Receives files from **any number of sources**, while it is open. | `scan_batch` (+ intake state) |
| **Active batch** | The batch the project currently feeds, and that Resolve, Attendance, Results and Reports read. **Explicit**, never "latest by `updated_at`". | `project_setting` key (existing table) |
| **Ingest session** | *Not a new entity.* An operator's "box of scripts on station 2 this morning" is the set of intake files of one source in a time window; filtering by source + time covers it. Deferred until real use shows a need. | — |
| **Examination set** | Logical question-paper set (`project_set`), unchanged, plus an optional physical mark. | `project_set` |
| **Rescan / replacement** | Existing `scan_rejection` link; a rescan arrives through intake like any file and is linked by the operator. | `scan_rejection` |

Answers to the release's batch questions:

- **When is a batch created?** When the operator starts reading an
  examination (first *Process All* / first intake start), or explicitly by
  *Reprocess All* / a template change. **Not** implicitly on reopening a
  project: the Scan stage re-adopts the active batch.
- **Can a source feed several batches / a batch several sources?** Yes to
  both. Source is recorded per scan row; the batch is the reading.
- **Can processing start before a batch is closed?** Yes — that is the point.
- **Batch intake states:** `open` (accepting and processing new files),
  `closed` (no new registrations; processing, resolution and rescans of its
  rejected sheets continue), and archival stays as today (an old batch
  superseded by *Reprocess All* is simply no longer active). One `intake_state`
  column, orthogonal to the existing processing `status`.
- **File arriving after closure:** stays in `intake_file` as `held` and is
  shown to the operator ("7 files arrived after Batch closed") with *Add to
  batch* / *Ignore*. Never silently dropped, never silently added.
- **Rescan relation:** unchanged model — the replacement is linked to the
  original in `scan_rejection`; with a single active batch it is normally in
  the same batch; cross-batch adoption keeps working for legacy projects.
- **Multiple batches at once:** supported for history and legacy projects, but
  one active batch is the cohort downstream stages read. Full project-wide
  cross-batch aggregation (reconciliation and results keyed by project rather
  than batch) is **deferred**: it rewrites the `(roster_id, batch_id)` keys of
  reconciliation, results and reports and is not needed once late scans stop
  creating accidental batches. *(Decision requested — see Open design questions 1.)*

---

## Proposed data-model changes (no migration is created by this document)

All additive, defaults chosen so an alpha.2 / current-`main` project opens
unchanged; each migration is guarded by `PRAGMA table_info`, as migrations
4–12 are.

**Migration 13 — set identity** (Phase 1)

| Change | Default | Purpose |
|---|---|---|
| `project_set.physical_mark` VARCHAR(32) NULL | NULL = printed as its own code | logical ↔ physical |
| `project_set.canonical_code` VARCHAR(32) NULL, unique index where not NULL | computed by the migration from `code` | one identity per set |

Collision handling: if two existing sets canonicalise to the same value
(`A` and `a`), the migration fills `canonical_code` for the first by
`display_order` and leaves the other NULL; Project Health and Project
Configuration report "Sets A and a are the same set code" and the set-dependent
stages refuse to score until the operator renames or merges. **No automatic
merge.** Existing `answer_key_revision.set_code`, `batch_scan.set_code_value`,
`candidate_result.set_code`, `generated_report.set_code` are **not rewritten**:
they are compared through the canonical function.

**Migration 14 — intake** (Phase 2)

- `intake_source`: `source_id` (uuid hex), `display_name`, `kind`
  (`watched_folder` / `manual`), `folder` (as entered), `recursive`,
  `station_note`, `active`, `created_at`, `updated_at`, `last_poll_at`,
  `last_file_at`, `health` (`online` / `unreachable` / `permission_denied` /
  `disabled`), `health_detail`, `health_changed_at`.
- `intake_file`: `intake_file_id`, `source_id` FK, `relative_path`,
  `absolute_path_seen`, `size`, `mtime`, `content_sha256`, `state`,
  `first_seen_at`, `last_seen_at`, `stable_since`, `attempts`, `last_error`,
  `scan_id` NULL FK→`batch_scan`, `duplicate_of_scan_id` NULL. Unique on
  `(source_id, relative_path, content_sha256)`; indexed on `content_sha256`
  and `state`.
- `batch_scan.source_id` NULL FK, `batch_scan.intake_file_id` NULL FK,
  `batch_scan.registered_at` NULL. NULL for every existing row ("added
  manually before intake sources existed").
- `scan_batch.intake_state` VARCHAR(10) NOT NULL DEFAULT `'open'`.
- A single built-in `manual` source row is **not** inserted by the migration
  (migrations change structure, not data — `migrations.py` module rule);
  it is created on first use by the service.

**No migration** for the active-batch pointer: the existing `project_setting`
key/value table (migration 1) holds `active_batch_id`. When absent (every
existing project) the service picks the most recently *created* non-superseded
batch once and records it, and says so in the status bar.

Rollback: forward-only, as ADR-0003; the backup taken on open
(`services/project_backup.py`) is the recovery route, and must be verified to
run before migrations 13 and 14.

---

## Logical vs physical set codes (recommendation)

- `project_set.physical_mark` optional; NULL means the sheet prints the
  logical code itself.
- Validated against the template's set field with the existing
  `services/answer_key.can_print_set_code`, and unique among sets.
- **Raw is kept:** `batch_scan.set_code_value` and the review ledger keep what
  the machine read and what a reviewer corrected **on the paper** (a physical
  value).
- **One translation point:** a new pure function (proposed
  `services/set_identity.py`: `canonical_code()`, `logical_for_physical()`,
  `physical_for_logical()`) used by `review_store.effective_set_codes` and
  `review_store.sync_undefined_set_codes`. Everything downstream already reads
  effective codes: reconciliation placement (`reconciliation_store._placements`),
  scoring (`scoring.build_candidate_answers` / `inputs_for_candidate`), reports
  (`report_store`, `report_readiness`) — they become correct without their own
  mapping.
- Consumers that need the physical side explicitly: Resolve's set-code editor
  (offers physical marks, shows the logical set beside each), the Answer Key
  solution-sheet check (`check_sheet_set` compares the sheet's raw read with
  the chosen set's physical mark; UNREPRESENTABLE disappears for mapped sets),
  and the synthetic generator (`evaluation/answer_keys.set_code_markable`,
  `attendance_dataset`), which marks the physical symbol.
- Rejection's `declared_set_code` stays logical (it is typed by the operator).
- Export/CSV shows both columns: *Set (as read)*, *Set*.

## Set-code canonicalisation (recommendation)

**Logical set codes are case-insensitive.** Canonical form:
`unicodedata.normalize("NFKC", code).strip().upper()`. Rationale: OMR set
fields print upper-case letters and digits; recognition returns template
symbols; scoring and the solution-sheet check already upper-case; the only
exact-case comparisons are in the registry and the stages fed by it, where no
evidence exists of projects relying on `a` ≠ `A`. The stored `code` keeps the
operator's spelling for display.

Every comparison of set codes goes through `canonical_code()`:
`exam_sets.find_conflicting_set` (defining `a` when `A` exists is refused),
`review_store.sync_undefined_set_codes`, `reconciliation_store._placements`,
`scan_lifecycle` declared-set validation, `scoring.usable_set_code`,
`scoring_store` key lookups and `verified_keys`, `report_store` associations
and `report_readiness`. A cross-stage test drives one lower-case logical code
from definition to report.

---

## Multi-source ingestion architecture (recommendation)

1. **Sources** are project-level rows. *Add Folder* keeps working and records
   into an implicit `manual` source.
2. **Discovery** — a background intake worker (thread, not a process: the work
   is listing and hashing) **polls** each active source every N seconds
   (default 10 s local, 30 s network path; configurable) and reconciles the
   listing with `intake_file`. `QFileSystemWatcher` may be added later as a
   hint that triggers an early poll (P2); it is never relied on — it is
   unreliable on SMB.
3. **Stability** — a file becomes `ready` when (a) its extension is a
   supported image and not a temporary pattern (`*.tmp`, `*.part`, `~*`,
   `.*`), (b) size and mtime are unchanged across polls spanning at least the
   stability interval (default 5 s local / 15 s network), (c) it can be opened
   for reading (on Windows, a file still held for writing fails to open), and
   (d) it decodes (`imaging` validation already used at recognition). Failing
   (d) after the interval → `unreadable` with retries and backoff (3 attempts),
   reported as a *file* problem, not an OMR failure.
4. **Identity** — SHA-256 is computed **at discovery** (moving the hash pass
   earlier than today). A `ready` file whose hash equals an active scan's in
   the active batch becomes `duplicate` (linked via `duplicate_of_scan_id`),
   is **not registered**, and is listed for the operator. Path alone is never
   identity; `(source_id, relative_path, size, mtime)` only avoids re-hashing
   unchanged files on each poll.
5. **Registration** — `ready`, non-duplicate files are registered into the
   active batch through the existing `add_scans_to_batch` (extended with
   `source_id`, `intake_file_id`), then queued.
6. **Writes** — the intake worker does filesystem work and hashing, and hands
   **database writes to one writer** (the existing recorder pattern, generalised
   to a single DB-writer thread/queue shared by the batch recorder and
   intake). `PRAGMA busy_timeout` (e.g. 5 s) set in `database/engine.py` as a
   safety net.
7. **Source health** — `listdir` failure → `unreachable` (network) or
   `permission_denied`; files known to that source are **not** marked vanished
   while it is unreachable. A file that disappears from a reachable source
   before registration → `vanished`; after registration nothing changes (the
   scan row keeps its hash; `check_availability`/`relink_scan` already handle
   moved images).

Throughput sanity check: the one measured figure is ≈11 sheets/s ≈ 670/min
(8 workers, shared development machine). Three medium-speed stations at an
**assumed** 60 sheets/min each would supply 180/min — processing should keep
up with margin on similar hardware. This is an assumption to be replaced by a
measurement in Phase 7.

## Processing state machine (minimal extension)

Keep `ScanJobStatus` on `batch_scan` exactly as it is. Put pre-registration
states on `intake_file`, where they belong:

    intake_file:  discovered → stabilizing → ready → registered
                                     ↘ unreadable (retrying → failed_read)
                          ready → duplicate
                          any unregistered → vanished | held (batch closed)
    batch_scan:   pending → queued → completed | warning | failed   (unchanged)
                  lifecycle (scan_rejection): active / rejected_pending_rescan /
                  superseded_by_replacement / reimport_of_rejected  (unchanged)

"Needs resolution" is not a status: it is open conflicts (`review_conflict`),
as today.

## Pause / resume / stop intake (semantics)

| Control | Effect | State after restart |
|---|---|---|
| **Intake: On / Paused** (per source and global) | Paused: no polling, no registration; nothing lost — files are found on resume | persisted in `intake_source.active` / project setting |
| **Processing: Running / Paused** | Paused: no new submissions; in-flight sheets **finish and are recorded** | paused persists; queued rows stay `pending` |
| **Finish current and stop** | Replaces today's *Cancel* as the default stop: in-flight results recorded, nothing discarded | batch `interrupted` if work remains |
| **Cancel queued work** | Explicit, confirms, marks remaining `cancelled` (today's behaviour), in-flight still recorded | as today |
| **Resume** | Continues from `pending`/`cancelled` rows in `batch_index` order | unchanged mechanism |
| **Close batch intake** | No further registrations; later arrivals `held` | persisted |

## Duplicate and rescan model (recommendation)

| Case | Detection | Outcome |
|---|---|---|
| 1. Exact duplicate image | same SHA-256 as an active scan in the batch | `intake_file.state = duplicate`, not registered, linked, shown; zero processing cost |
| 2. Same physical sheet scanned twice (different bytes) | same **effective** Student ID (+ optional answer-string similarity as a hint) | `IDENTIFIER_DUPLICATE` conflict (as today, but on effective IDs, whole active batch); reconciliation `DUPLICATE_SCRIPT`; operator marks primary or rejects one |
| 3. Legitimate rescan after rejection | operator links via existing *Rescan* flow; suggestions from effective ID (existing `replacement_candidates`) | `scan_rejection` link; original `superseded_by_replacement` |
| 4. Candidate in several batches | only meaningful for legacy / deliberate multiple batches | flagged in the active batch's duplicate check when a counted script in another batch shares the effective ID (`counted_elsewhere` extended); not merged automatically |
| 5. Same Student ID on two different physical scripts | same as 2 — indistinguishable by machine; the written ID (write-in box) and answers are the evidence | conflict, human decision |
| 6. Manually replaced / resolved | existing review ledger + `scan_rejection` | unchanged |
| + Re-import of a rejected image | existing `sync_reimports` by hash | unchanged; moves to intake time |

Which script counts is unchanged in principle: `scan_rejection` lifecycle
(only `active` is eligible) + reconciliation primary/excluded + scoring's
`working_script`. Lineage records: `scan_rejection` (current link), `audit_event`
`scan_lifecycle` events (history), `intake_file` (where and when each file was
first seen, and its duplicates).

---

## Phases

Phase boundaries follow dependencies: identity first (everything keys on it),
then the intake ledger, then continuous processing on top of it, then the
duplicate/rescan semantics that depend on intake data, then the GUI, then scale
and release validation. Each phase ends with `pytest`, `ruff`, `mypy` green and
a handoff note.

### Phase 1 — Set identity and cohort scope (P0)

**Goal.** One canonical set identity with an optional physical mark, and an
explicit active batch that late scans join instead of splitting the cohort.

**Architectural changes.** `services/set_identity.py` (new); migration 13;
`domain/exam_sets.py` (canonical uniqueness), `services/project_sets.py`,
`services/review_store.py` (`effective_set_codes`, `sync_undefined_set_codes`
translate once), `services/scoring.py` / `scoring_store.py` /
`reconciliation_store.py` / `report_store.py` / `report_readiness.py` /
`scan_lifecycle.py` (compare canonically), `services/answer_key.py`
(`check_sheet_set` uses physical mark); active-batch pointer in
`project_setting` with a small service (proposed in `services/batch_store.py`:
`active_batch_id()` / `set_active_batch()`); Scan page re-adopts it on open;
Attendance/Results/Reports read it instead of `list_batches(limit=1)`.

**User-visible.** Project Configuration → Sets gains *Printed on sheet as*;
Resolve shows *Set 10 (A on sheet)*; reopening a project and adding scans adds
them to the same batch; the batch every stage reads is named in its header.

**Non-goals.** Project-wide multi-batch aggregation; automatic set merges.

**Dependencies.** None.

**Risks.** Canonicalisation touching every stage — a missed comparison
reintroduces the defect; the one-time active-batch choice for existing
multi-batch projects may pick the batch an operator did not mean (mitigated by
showing it and allowing change).

**Tests.** Unit: canonical form (case, whitespace, NFKC, digits); physical ↔
logical round trip; `can_print` validation; collision migration. Integration: a
lower-case logical set and a mapped `10 → A` set driven from definition →
recognition → Resolve → Attendance → Answer Key → Results → Reports; migration
from a schema-12 project with sets `A` and `a`; reopen-and-add-scans lands in
the same batch; retrying failed sheets in an old batch does not move Results.
GUI: set editor, Resolve set display, stage headers name the batch.

**Acceptance.** Every set-code comparison in `src` goes through
`set_identity` (enforced by a test that greps for direct comparisons of
`set_code` fields, as existing architecture tests do); the cross-stage tests
pass; a schema-12 project opens, migrates and scores identically.

**Likely files.** `database/models.py`, `database/migrations.py`,
`domain/exam_sets.py`, `services/project_sets.py`, `services/review_store.py`,
`services/reconciliation_store.py`, `services/scoring.py`,
`services/scoring_store.py`, `services/report_store.py`,
`services/report_readiness.py`, `services/scan_lifecycle.py`,
`services/answer_key.py`, `services/batch_store.py`,
`gui/project_config_dialog.py`, `gui/review/…`, `gui/scan/page.py`,
`gui/attendance/page.py`, `gui/results/page.py`, `gui/reports/page.py`,
`evaluation/answer_keys.py`, `evaluation/attendance_dataset.py`.

### Phase 2 — Intake ledger and idempotent registration (P0)

**Goal.** Files from named sources are discovered, stabilised, hashed and
registered exactly once, surviving restart.

**Architectural changes.** Migration 14; `services/intake.py` (new: source
CRUD, `poll_source()` reconciliation, stability evaluation, duplicate
decision, registration through `batch_store.add_scans_to_batch`);
`services/scan_provenance.py` (hash at discovery; `duplicate_groups` finally
used); `services/scan_import.py` (reused for listing / supported extensions);
single DB-writer coordination + `busy_timeout` in `database/engine.py`.

**User-visible.** *Add Folder* unchanged; new *Intake sources…* dialog to add a
watched folder; a manual *Check sources now*.

**Non-goals.** Continuous background processing (Phase 3); GUI panel (Phase 5).

**Dependencies.** Phase 1 (registration targets the active batch).

**Risks.** Hashing large files over SMB is slow (a 7 MB PNG read per file);
long paths / UNC paths on Windows; mtime granularity on network shares.

**Tests.** Unit: stability rules with a fake clock and a fake filesystem
(growing file, unchanged file, temp names, locked file, decode failure).
Integration: same file in two sources → one registration; same bytes renamed
→ duplicate; file vanishes before stable → `vanished`; restart mid-discovery
→ no duplicate rows; 10,000-file folder polled twice → second poll registers
nothing and does not re-hash.

**Acceptance.** Re-polling any source any number of times, including across a
kill and restart, never adds a second active scan for the same bytes; every
registered scan records source, path, hash, first-seen time.

**Likely files.** `database/models.py`, `database/migrations.py`,
`database/engine.py`, new `services/intake.py`, `services/batch_store.py`,
`services/scan_provenance.py`, `services/scan_import.py`.

### Phase 3 — Continuous processing and operational controls (P0 core, P1 controls)

**Goal.** Processing runs while files arrive, can be paused and resumed without
losing in-flight work, and distinguishes source outages from bad sheets.

**Architectural changes.** A long-lived processing loop that draws `pending`
rows from the active batch as they are registered (extending `BatchWorker` /
`batch_processor.process_batch`, which today take a fixed list);
*Finish current* replacing destructive *Cancel*; persisted Intake/Processing
pause flags; source health transitions; retry/backoff for transient read
errors; `processing_manifest` written at run boundaries (closing latent defect 6).

**User-visible.** *Intake: On/Paused*, *Processing: Running/Paused*, *Finish
current and stop*; progress that says "caught up — waiting for files" rather
than an ETA while intake is open.

**Non-goals.** Scheduling across machines; per-source worker allocation.

**Dependencies.** Phase 2.

**Risks.** Reworking the one batch loop that Phase 10 hardened (pool recycling,
reorder buffer, flush-on-failure); conflict generation and reconciliation
today run at batch end — they must run incrementally or on demand without
O(n²) cost.

**Tests.** Integration with a simulated scanner writing files over time;
pause/resume with in-flight work recorded; kill during continuous run → restart
resumes and discovers files written while stopped; network source removed and
restored (a directory rename in tests) → `unreachable` then `online`, no
vanished files. Stress: continuous 10,000-sheet arrival with 3 sources.

**Acceptance.** No sheet processed twice, none lost, none processed while
still being written, across the continuous and kill scenarios; *Finish current*
records every in-flight result.

**Likely files.** `gui/scan/worker.py`, `services/batch_processor.py`,
`services/parallel_batch.py`, `services/batch_store.py`,
`services/batch_progress.py`, `services/intake.py`, `gui/scan/page.py`,
`gui/main_window.py`.

### Phase 4 — Duplicate and rescan semantics across sources (P0 correctness, P1 UX)

**Goal.** Every duplicate case above behaves as specified, with lineage.

**Architectural changes.** `review_store.sync_duplicate_identifiers` on
**effective** IDs across the whole active batch, incrementally;
`counted_elsewhere` consulted for legacy multi-batch projects; rescans arrive
through intake and are suggested for open rejections (existing
`replacement_candidates`, now also by source/time); `sync_reimports` at
registration; audit events for duplicate-image decisions and held files.

**User-visible.** Resolve's rescan suggestions include "arrived from Station 3
at 11:42"; duplicate images listed with the scan they duplicate; corrected IDs
that collide raise a conflict.

**Non-goals.** Automatic linking of rescans; answer-similarity scoring beyond
a hint.

**Dependencies.** Phases 1–3.

**Risks.** Effective-ID duplicate checks after every correction must stay cheap
at 100k sheets (index on effective values, or incremental maintenance).

**Tests.** One integration test per row of the duplicate table; lineage
survives undo/relink and restart; Results includes exactly one script per
candidate in every case.

**Acceptance.** The duplicate table's outcomes hold in automated tests and on a
real two-station run (Phase 7 row G–I).

**Likely files.** `services/review_store.py`, `services/conflict_policy.py`,
`services/scan_lifecycle.py`, `services/reconciliation_store.py`,
`services/intake.py`, `gui/review/rescan.py`, `gui/review/page.py`.

### Phase 5 — Operator GUI (P1)

**Goal.** Operators can see, per source, whether scanning is alive, what is
pending, whether processing has caught up, and which source produced a sheet —
without a dashboard.

**Architectural changes.** A compact *Sources* strip/table on the Scan stage
(source, state, discovered, pending, processed, failed, last file);
*Source* column and filter in the scan list; the scan list backed by SQL paging
instead of holding every `ScanEntry` in memory (continuous intake makes the
list unbounded); held/duplicate/unreadable lists; the batch named in stage
headers.

**User-visible.** As above; nothing added to other stages beyond a source
filter where scans are listed (Resolve's *All processed sheets*).

**Non-goals.** Charts, remote dashboards, per-operator accounts.

**Dependencies.** Phases 2–4 (data exists).

**Risks.** Scan page complexity (already the largest page); 1366×768 fit.

**Tests.** GUI tests with the project's qtguitesting workflow; rendered
acceptance at 1366×768 and a narrower size at 175 % scaling; the 100k-row
paging test.

**Acceptance.** An operator can answer the six questions in the brief from the
Scan stage alone; memory stays bounded with 100k registered scans.

**Likely files.** `gui/scan/page.py`, `gui/scan/table_model.py`, new
`gui/scan/sources_panel.py` (or similar), `gui/review/page.py`,
`gui/theme/stylesheet.py`.

### Phase 6 — Recovery, telemetry and scale (P0 recovery, P1 telemetry)

**Goal.** Restart and network interruption are safe under continuous intake;
operators see backlog and rate; performance is measured, not assumed.

**Architectural changes.** Restart sequence: `recover_interrupted` → intake
reconciliation poll of every source (finds files written while stopped) →
resume processing if it was running; `BatchProgressTracker` gains backlog
(registered pending + ready unregistered) and "caught up" state; telemetry
sampler optional in normal runs; DB growth instrumentation.

**User-visible.** "Resuming: 1,204 registered sheets pending, 37 new files
found since last run"; backlog and sheets/minute on the Scan stage.

**Non-goals.** Moving off SQLite; WAL on network paths.

**Dependencies.** Phases 2–3.

**Risks.** DB size: 10k sheets produced 2.1 GB (≈210 KB/sheet, dominated by
`result_json`), so 100k extrapolates to ≈21 GB — disk and backup time become
operational constraints; single-writer throughput with intake + recorder +
GUI writes.

**Tests.** The existing Phase 10 kill/resume harness extended with an intake
source and files written during downtime; SMB disconnect simulation;
continuous-arrival stress at 10k and 20k.

**Acceptance.** Every restart criterion in *Release exit criteria* holds in
automated kill tests; measured continuous throughput and DB growth recorded.

**Likely files.** `gui/main_window.py`, `services/batch_store.py`,
`services/intake.py`, `services/batch_progress.py`, `services/telemetry.py`,
`evaluation/stress_runner.py`, `evaluation/qualification.py`,
`evaluation/stress_dataset.py` (sources and arrival schedule).

### Phase 7 — Release validation (P0 for the release)

**Goal.** Evidence, on real scanners and at scale, that the release does what it
claims — see the acceptance matrix.

**Dependencies.** Phases 1–6.

**Acceptance.** The release exit criteria.

---

## Priority classification

| Item | Priority |
|---|---|
| Canonical set identity (case-insensitive, one function) | **P0** |
| Logical ↔ physical set mapping | **P0** |
| Explicit active batch; late scans join it; no silent batch switch | **P0** |
| Persistent intake sources | **P0** |
| Intake ledger with hash-at-discovery; idempotent registration | **P0** |
| Stable-file detection | **P0** |
| Exact-duplicate image handling | **P0** |
| Duplicate Student ID on effective values, whole active batch | **P0** |
| Restart recovery incl. files arriving while stopped | **P0** |
| Single DB writer coordination + `busy_timeout` | **P0** |
| Continuous processing loop | **P0** |
| Pause / Finish current / Resume semantics | P1 |
| Source health and network-failure states and messaging | P1 |
| Sources panel, source column/filter | P1 |
| SQL-paged scan list for unbounded intake | P1 |
| Rescan suggestions by source/time; re-import at registration | P1 |
| Backlog / sheets-per-minute telemetry; "caught up" instead of ETA | P1 |
| `processing_manifest` actually written | P1 |
| Held files after batch close | P1 |
| `QFileSystemWatcher` hints for faster discovery | P2 |
| Per-source operator/scanner metadata beyond a note | P2 |
| Answer-similarity hint for same-sheet-twice | P2 |
| Project-wide cross-batch aggregation (reconciliation/results keyed by project) | Deferred |
| Ingest-session entity | Deferred |
| Distributed DB / server, cloud sync, remote dashboard | Deferred |
| Scanner drivers, vendor SDKs, TWAIN/WIA control, network discovery | Deferred |
| Multi-user concurrent editing, permissions, message queues | Deferred |

---

## Cross-cutting testing requirements

- Existing suites stay green; no test weakened.
- A fake filesystem + fake clock layer for intake unit tests (no sleeps).
- A **simulated scanner** helper (writes files slowly, renames, locks) used by
  integration and stress tests.
- The synthetic generator gains optional *sources* and an *arrival schedule*
  (index-addressed like `stress_dataset.py`), plus physical set marks.
- Migration tests from a real schema-12 project fixture (the Phase 10
  `TestUpgradingFromSchemaEight` pattern).
- Architecture test: no direct `set_code` string comparison outside
  `set_identity`.
- GUI: qtguitesting workflow, 1366×768 and narrower, 175 % scaling.

## Real-world acceptance matrix

| # | Scenario | Observable acceptance |
|---|---|---|
| A | Single scanner, finite folder | *Add Folder → Process All* identical to today; same results as alpha.2 on the same images |
| B | Two scanners, files copied in afterwards | Both folders as sources; every sheet registered once; source recorded per scan |
| C | Three scanners writing continuously | Processing keeps pace or backlog is visible; no sheet processed before stable; totals equal files written |
| D | Network share disconnected mid-run | Source shows *unreachable*; nothing marked vanished; on reconnect files resume; no OMR failures reported for the outage |
| E | OMRFlow stopped, files added, restarted | New files discovered on start; no reprocessing of committed sheets |
| F | Same image imported twice (two paths/sources) | One active scan; the other listed as duplicate, linked |
| G | Same physical script scanned twice | Duplicate-ID conflict; one counts after decision; both kept |
| H | Rejected sheet later rescanned on another station | Suggested and linked; original superseded; lineage in audit |
| I | Candidate's script in another (legacy) batch | Flagged; not double-counted |
| J | File still being written | Not registered until stable; no decode failure recorded |
| K | Large backlog while processing continues | GUI responsive; backlog and rate shown; memory bounded |
| L | 100,000-sheet synthetic run | See exit criteria — the campaign has never been run and must be |
| M | Logical set `10` printed as `A` | Resolve, Attendance, Answer Key, Results, Reports all show Set 10; raw `A` retained |
| N | Set codes typed `a` / `A` | One set; key found; duplicates refused at definition |
| O | Kill during continuous processing | Resume safe; Phase 10 kill criteria still hold |

Step 7 validation continues in parallel (not a feature stream): more real
solution sheets, real blanks/double marks, skewed/noisy/photographed samples,
a blank real set field, and an operator's acceptance.

## Migration / backward compatibility

- A current-`main` (schema 12) project opens, migrates to 14, and produces the
  same results for the same images without operator action.
- NULL `source_id` = "added before sources"; NULL `physical_mark` = identity;
  `intake_state` default `open`; active batch chosen once and shown.
- Set-code collisions block only the set-dependent stages, with a message
  naming the sets; nothing is merged silently.
- An alpha.2 build refuses a schema-14 project with the existing "created with
  a newer version" message (unchanged mechanism).

## Performance targets

Stated as targets to verify, not claims:

- Continuous intake of ≥ 3 sources with combined arrival ≥ 180 sheets/min
  processed without growing backlog on a machine comparable to the Phase 10
  one (8 workers).
- Discovery poll of a 10,000-file folder with nothing new: < 2 s, no hashing.
- GUI interactions < 200 ms with 100,000 registered scans; memory bounded
  (Scan list no longer proportional to scan count).
- Restart to resumed processing on a 100k project: < 60 s.

## Release exit criteria

v0.1.1-alpha.0 is ready when **all** of these hold, with evidence recorded:

1. Several sources contribute safely (matrix B, C).
2. Continuously added files are eventually discovered, including those written
   while OMRFlow was stopped (C, E).
3. Partially written files are never processed prematurely (J).
4. Duplicate image ingestion is idempotent (F).
5. Cross-batch/cross-station rescans preserve lineage (H).
6. Restart does not duplicate or lose work (E, O).
7. Logical/physical set mapping works end to end (M).
8. Set-code identity is consistent across stages (N).
9. Answer Key and Results use the resolved logical set (M, N).
10. Audit provenance answers, for any sheet: source, path, hash, first seen,
    batch, attempts, effective ID and set, corrections, lineage, supersession,
    scoring inclusion.
11. GUI responsive on a 100k-scan project (K).
12. **A 100,000-sheet run completes** (L). *Note:* the brief asks that the 100k
    test "remain successful"; it has never been run. Proposed minimum: the R0
    uninterrupted run with intake enabled; kill/resume at 10k. If R0 is not
    run, the release notes must say so.
13. A schema-12 project migrates and scores identically.
14. The finite-folder workflow is unchanged (A).
15. At least one real two-station, shared-folder session (B–D) on real
    scanners.

## Known risks

- Phase 3 reworks the batch loop Phase 10 hardened; regressions there are the
  largest risk — keep the fixed-list path and extend rather than replace.
- Incremental conflict generation / reconciliation at 100k.
- SMB behaviour (mtime granularity, locks, latency) differs by server; only a
  real share test (matrix D) settles it.
- DB size growth (≈21 GB extrapolated at 100k) and backup time.
- Set canonicalisation touching every stage.
- Operators relying on today's "latest batch" behaviour when several batches
  exist.

## Open design questions — recommendations

1. **What is a batch in continuous operation?** One reading of the
   examination with one template identity, fed by any sources; one *active*
   batch. **Decision requested:** approve "single active batch" and defer
   project-wide cross-batch aggregation.
2. **Closed explicitly?** Yes — *Close intake* is explicit; batches otherwise
   stay open. Closing is reversible until results are exported.
3. **Multiple sources → one batch?** Yes.
4. **Auto-create batches per source?** No; sources feed the active batch.
5. **Physical → logical translation?** Once, in `review_store`'s effective-set
   derivation via `services/set_identity.py`.
6. **Case-insensitive?** Yes, canonical upper-case NFKC, display spelling kept.
7. **Project with `A` and `a`?** Migration marks the collision; set-dependent
   stages blocked until renamed/merged by the operator.
8. **Stability interval?** 5 s local, 15 s network, plus open-for-read and
   decode checks; configurable.
9. **Disconnected source?** `unreachable` health; its files frozen, not
   vanished.
10. **Exact vs physical duplicates?** Exact = same SHA-256, never registered;
    physical = same effective ID, a conflict for a person.
11. **Rescan lineage records?** `scan_rejection` (link), `audit_event`
    (history), `intake_file` (arrival provenance).
12. **Filtering without clutter?** Source column + filter in the Scan list and
    Resolve's *All processed sheets* only; one compact Sources table on Scan.
13. **What to reuse?** `batch_store` (`add_scans_to_batch`, recorder,
    `recover_interrupted`, resumable windows), `parallel_batch`,
    `scan_provenance` (hashing, `duplicate_groups`, relink, availability),
    `scan_import` (listing), `scan_lifecycle` (rejection, replacement,
    re-import), `review_store` effective values, `batch_progress`,
    `telemetry`, `stress_dataset`/`qualification` harness,
    `project_setting`, `audit_event`.

## Conflicts with the previous roadmap

- The canonical roadmap (`docs/wiki/Development-Roadmap.md`) plans Phase 11B
  (real-data qualification → `v0.1.0-beta.1`) next and contains no
  `v0.1.1` line. This release inserts functional work before Beta; 11B's
  real-data qualification continues alongside it rather than being replaced.
- Phase 10's decision that Attendance/Results/Reports stay `QTableWidget`
  stands; only the Scan list (unbounded under continuous intake) needs paging.
- Phase 5's "a future phase that wants concurrent writers needs a deliberate
  architecture" is respected: there are still no concurrent writers — intake
  hands writes to the single writer.
