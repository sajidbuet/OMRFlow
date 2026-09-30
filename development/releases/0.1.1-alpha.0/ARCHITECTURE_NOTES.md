# `0.1.1-alpha.0` — architecture notes: live, multi-scanner intake

> **Planning document. Nothing here is implemented.** Written 2026-09-27
> against `main` at `2d18ccb` (application version `0.1.0-alpha.2`, schema
> version 9). Names of tables, columns and classes are *illustrations of the
> required semantics*; the implementing phase chooses the representation after
> re-inspecting the code.

Contents

1. The problem in one paragraph
2. What exists today (inspected, with references)
3. Why `ScanBatch` must not become an open-ended batch
4. Proposed concepts
5. Asset identity, provenance and collisions
6. Intake lifecycle and file stabilisation
7. Watching: reconciliation first, notifications second
8. Feeding the existing pipeline: finite processing units
9. Concurrency and the single writer
10. Operational outcomes and the scan-quality decision layer
11. Incremental conflict synchronisation
12. Rescan, replacement and supersession
13. The effective scan set, and Phases 7–9
14. Session-level progress model
15. Session lifecycle: pause, resume, finish
16. Backward compatibility
17. Existing defects and drift found while planning
18. Open design questions, by phase

---

## 1. The problem in one paragraph

`0.1.0-alpha.2` processes a **finite, static stack**: the operator lists
files, a `ScanBatch` is registered with one row per file, the batch is read,
conflicts are detected after it finishes, and Phases 7–9 consume *that batch*.
A real scanning room instead has several scanner PCs producing files
continuously into different folders — often network shares — for hours, while
operators resolve conflicts and physically rescan damaged sheets. The workload
is not known in advance, identities collide across batches, files arrive
half-written, sources disappear and come back, and "the examination's scans"
is a population that grows and is corrected over time. `0.1.1` adds that
operational layer **on top of** the finite machinery, not instead of it.

```text
0.1.0-alpha.2
    finite static batch

0.1.1-alpha.x
    dynamic examination intake session
        ├── one or many scan sources
        ├── manual or watched-folder intake
        ├── continuously changing workload
        ├── incremental recognition
        ├── incremental conflict review
        ├── scan-quality rejection
        ├── physical rescan/replacement
        └── explicit session closure
```

---

## 2. What exists today (inspected, with references)

All references are to `src/omr_scanner/` at `2d18ccb`. The implementing phase
must re-verify them; see `MERGE_NOTES.md` §3.

### 2.1 Durable batches (Phase 5)

| Fact | Where |
|---|---|
| `scan_batch` row: id (uuid4 hex), `source_folder`, template id/name/path, `geometry_fingerprint`, `recognition_fingerprint`, `engine_version`, `settings_json`, `status`, `total_scans` | `database/models.py` `ScanBatch` |
| `BatchStatus`: `new`, `running`, `interrupted`, `cancelled`, `completed`, `completed_with_errors` | `database/models.py` |
| `batch_scan` row per sheet: `batch_index`, `source_path`, `filename`, `file_size`, `modified_at`, `status`, `attempt_count`, `outcome`, `identifier_value`, `set_code_value`, `result_json`, timings, `content_sha256` | `database/models.py` `BatchScan` |
| **`UNIQUE(batch_id, source_path)`**; no uniqueness on filename or hash; nothing unique across batches | `database/models.py` |
| `ScanJobStatus`: `pending`, `queued`, `processing`, `completed`, `warning`, `failed`, `cancelled` | `database/models.py` |
| **Membership is fixed at creation.** `create_batch` writes every row and `total_scans` once; there is no API to add scans to an existing batch (a docstring mentions `register_scans`, which does not exist) | `services/batch_store.py` |
| Results matched to rows by `source_path` and committed in groups (25 sheets / 2 s) by `BatchRecorder`, from the `BatchWorker` QThread | `services/batch_store.py`, `gui/scan/worker.py` |
| Resume = `pending/queued/processing/cancelled` in `batch_index` order; Retry = `failed` | `services/batch_store.py` `resumable_scans` |
| `recover_interrupted` on project open: `queued/processing → pending`, `running → interrupted`; never `failed` | `services/batch_store.py`; called from `gui/main_window.py` |
| Fingerprint compatibility check before resume | `services/batch_store.py` `check_compatibility` |
| `process_batch` takes a finite `Sequence[Path]` and knows nothing about the database | `services/batch_processor.py` |
| Bounded pool: `spawn`, 4 tasks in flight per worker, recycling every 500 sheets by restarting the pool per slice | `services/parallel_batch.py` |
| Workers never touch the database; only the coordinator writes | `services/parallel_batch.py`, `services/batch_store.py` |
| SQLite rollback journal (WAL deliberately avoided for synced/network folders), `foreign_keys=ON`, **no `busy_timeout`** | `database/engine.py` |
| Progress: thread-safe `BatchProgressTracker`, GUI pulls snapshots on a 200 ms timer; total fixed at `start(total)`, `set_total` exists | `services/batch_progress.py` |

### 2.2 Provenance and duplicates (Phase 10)

| Fact | Where |
|---|---|
| SHA-256, streamed in 1 MiB chunks | `services/scan_provenance.py` `hash_file` |
| Hashes computed by `compute_hashes_for_batch` at the **start of each run** from `BatchWorker`, not at registration; failures only logged | `gui/scan/worker.py` |
| `duplicate_groups(database, batch_id)` groups by hash **within one batch**; **no caller in `src/`** | `services/scan_provenance.py` |
| `check_availability`: `present_unchanged` / `missing` / `changed` / `unverifiable`; used by the health check | `services/scan_provenance.py`, `services/project_health.py` |
| `relink_scan` accepts a new path only if the hash matches; **no caller in `src/`** | `services/scan_provenance.py` |
| Originals are referenced **in place** (`batch_scan.source_path` is the absolute original); `scans_original_dir` is only a dialog start folder | ADR-0002, `services/batch_store.py`, `gui/scan/page.py` |
| Optional renamed *copy* into an output folder; `_a`, `_b`… suffixes; allocator consults the directory | `services/batch_processor.py`, `services/filename_manager.py` |
| `batch_scan_history` (append-only, triggers) and `mark_for_reprocessing`; **no caller in `src/`** (the GUI's Retry does not archive) | `services/batch_store.py`, migration 7 |
| Project lock file `.omrflow.lock`, never removed automatically; genuine read-only mode | `services/project_lock.py`, `database/engine.py` |
| Migrations: ordered tuple, forward-only, one transaction each, `SCHEMA_VERSION = 9` | `database/migrations.py` |

### 2.3 Review (Phase 6) and scan quality

| Fact | Where |
|---|---|
| Conflict identity **`UNIQUE(batch_id, scan_id, conflict_type, zone_id, group_key)`**; states `open/resolved/deferred/withdrawn` | `database/models.py` `ReviewConflict`, `domain/review.py` |
| Only identity is a conflict: identifier ×6, set code ×5, `identifier_duplicate` (scope `batch`), sheet-level ×6 incl. `scan_quality`; answers never | `domain/review.py`, `services/conflict_policy.py` |
| `sync_conflicts` is idempotent per sheet: unchanged → no write; changed → `RE_RECOGNISED` event; gone → `WITHDRAWN` unless a human touched it | `services/review_store.py` |
| Conflict sync runs **only in the GUI after a run finishes**: per-sheet sync for that run, then `sync_duplicate_identifiers(batch)` once | `gui/scan/page.py` `_generate_conflicts` |
| Duplicate-ID detection: whole batch loaded, grouped by **identifier value only — set code ignored**, never across batches | `services/review_store.py`, `services/conflict_policy.py` `detect_duplicate_identifiers` |
| Append-only `audit_event` with no foreign key (so it outlives what it describes), UPDATE/DELETE triggers | migration 3 |
| Scan quality: `PASS / REVIEW / UNUSABLE` + issue codes; thresholds in `ScanQualityThresholds`; stored only inside `result_json`; surfaced as one `scan_quality` conflict. **No rescan state, no replacement concept** | `domain/scan_quality.py`, `services/conflict_policy.py` |

### 2.4 Downstream (Phases 7–9)

| Fact | Where |
|---|---|
| Reconciliation, scoring results and reports are keyed by **`(roster_id, batch_id)`** — `reconciliation_run`, `_entry`, `_script`, `_decision`, `candidate_result` | `database/models.py`, `services/reconciliation_store.py` `reconcile_batch` |
| `batch_scripts` takes **every** row of the batch, including failed ones (they become `#blank:` orphans) | `services/reconciliation_store.py` |
| Attendance, Results and Reports pages default to the **most recently updated batch** | `gui/attendance/page.py`, `gui/results/page.py`, `gui/reports/page.py` |
| Nearest thing to supersession: reconciliation's "exclude as accidental re-scan" | `domain/reconciliation.py` `ACCIDENTAL_RESCAN` |

### 2.5 Phase 10 qualification

`evaluation/stress_runner.py` registers N virtual `stress:` sources through
`create_batch`, processes them in windows through the unmodified
`process_batch`, and runs no conflict sync, hashing or reconciliation.
`evaluation/qualification.py` defines 15 release-blocking assertions
(sheet count, no duplicate active results, measured no-resubmission, no lost
or changed committed results, orphan workers, SQLite checks, health check,
semantic match with the reference run, exit code). **It tests deterministic
finite processing and recovery. It is preserved unchanged by this line.**

### 2.6 Things that do not exist

No file watcher or folder polling of any kind; no session, source or
scanner-station concept; no cross-batch view of conflicts or reconciliation;
no rescan or replacement state.

---

## 3. Why `ScanBatch` must not become an open-ended batch

Growing one batch indefinitely would break invariants other code relies on:

| Invariant | What an open-ended batch would do to it |
|---|---|
| Fixed membership; `total_scans` written once | Every consumer that reads `total_scans` as "the batch's size" becomes wrong mid-session |
| `finalise_batch` decides `completed` / `interrupted` from "anything still pending" | A live batch would oscillate between states as files arrive |
| Resume in `batch_index` order gives stable duplicate suffixes | Arrival order across three scanners is not reproducible, so indexes are not either |
| Fingerprint check per batch | A session that outlives a template tweak would silently mix rules inside one batch |
| Phase 10 assertions (`logical_sheet_count`, semantic reference) presume a known population | They would stop meaning anything |
| Downstream picks "the latest batch" | Harmless for one growing batch — but only because everything else broke |

So the batch stays exactly what Phase 5 made it: **a finite, durable,
resumable unit of recognition work**. What is new is the container above it
and the provenance beside it.

---

## 4. Proposed concepts

```text
Project                                    (existing)
 └── Intake session        "Scan session" in the UI; open-ended operational container
      ├── Scan source      provenance / input location: Scanner A, B, C, or a manual import
      │    └── Scan asset  one physical image file as discovered: provenance + intake state
      │          │
      │          └──(submitted to)──┐
      └── Processing unit ────────── ScanBatch (existing, Phase-5 semantics, fixed membership)
                                      └── BatchScan (existing) ── ReviewConflict, AuditEvent (existing)
```

| Concept | Role | New or existing |
|---|---|---|
| **Intake session** | The operational container for one examination's scanning: its sources, its pinned template and fingerprints, its quality policy, its lifecycle (`open` → `closing` → `closed`), its aggregate progress, and the scope for cross-sheet conflicts and for the effective scan set | New |
| **Scan source** | A configured input location: operator-visible label, directory (local or UNC), kind (`watched` or `manual`), enabled/disabled, reachability, last successful reconciliation, last error | New |
| **Scan asset** | One discovered image file: which source, where, when first seen, when it became ready, its content hash, its intake state, and its relationships (duplicate-of, replaces / superseded-by) | New |
| **Processing unit** | A finite `ScanBatch` created from READY assets and processed by the existing pipeline. Carries a reference to its session | Existing, plus one optional link |
| `BatchScan` | Unchanged meaning: one recognition job/result. Gains a link to the asset it read | Existing, plus one optional link |
| Review conflict / audit event | Unchanged meaning. Session scope is added for cross-sheet conflicts (§11); supersession is audited through the same ledger | Existing |

**Naming.** "Session" already means an ORM session and `ProjectSession`
(`services/project_service.py`) in this code base. The implementing phase
should use a distinct code name — `IntakeSession` is suggested — and call it
**Scan session** in the interface.

**Identity is not provenance.** Which scanner produced a file is recorded,
never used to decide whose script it is. A replacement may arrive from any
source.

---

## 5. Asset identity, provenance and collisions

**Filename is never identity.** Three scanners will all write `000001.jpg`.

Required semantics (representation chosen in Phase A):

| Property | Meaning |
|---|---|
| Internal asset id | Stable surrogate key, never derived from the path |
| Session id, source id | Where it came from |
| Original path, path relative to the source root, original filename | Provenance; the relative path is the within-source discovery key |
| First-seen time, stable/ready time | When it was discovered and when it became trustworthy |
| Observed size and modification time at ready time | What was hashed |
| Content hash (SHA-256, reusing `scan_provenance.hash_file`) and algorithm | Content identity |
| Stored-copy path, if ingested (§5.2) | What recognition and review actually read |
| Intake state and its reason | §6 |
| Duplicate-of asset | When the bytes equal an earlier asset's |
| Replaces / superseded-by | §12 |
| Processing history | The `BatchScan` row(s) that read it, plus existing `batch_scan_history` |

### 5.1 The collision rules

| Situation | Treatment |
|---|---|
| Same filename, different sources | Not a collision. Different assets |
| Same relative path in the same source, same observation already known | Already discovered; nothing happens (this is what makes reconciliation idempotent) |
| Same relative path in the same source, **different content** after the asset was accepted (scanner counter reset, file overwritten) | A **new** asset, flagged `path_reused`; the original asset and its results are untouched. Never silently re-read in place |
| Same content hash as an earlier asset, any source | `duplicate_content`: recorded, linked to the first, **not recognised again**, shown to the operator. Byte-identical files are a copy, not a rescan — a physical rescan never reproduces the same bytes |
| An asset's file disappears before it is ready | `vanished`; kept as a record |
| An asset's file disappears or changes after it was processed | Reported by the existing availability check; if the asset was ingested (§5.2) nothing downstream is affected |

### 5.2 Reference in place, or ingest a copy — an ADR for Phase A

ADR-0002 references scans in place. For a live session that is risky: the
Resolve stage **re-reads the sheet** on selection (`docs/conflict_review.md`
§11), and a scanner PC's share can go offline, be cleared by its operator, or
be reorganised mid-examination. The recommendation to evaluate:

- **Ingest by copy** for watched sources: once an asset is ready, copy it into
  the project (for example `scans/intake/<session>/<source>/<asset-id><ext>`),
  verify the copy's hash equals the source hash, and give recognition the
  copy. The original path is kept as provenance. The source is never modified,
  moved or deleted.
- Allow reference-in-place for a local manual source, preserving today's
  behaviour and disk usage.
- Record the decision as `docs/decisions/ADR-0005-…`, including disk-space
  consequences (roughly the size of the scanned cohort) and the interaction
  with the project on a synchronised folder.

### 5.3 Hash timing

Today the hash is computed at the start of a run. For intake it is needed
*before* queuing, to detect duplicate content before spending recognition on
it. Hash during stabilisation's read-back (§6) — one read serves stability,
decodability and identity.

---

## 6. Intake lifecycle and file stabilisation

### 6.1 States

```text
             DISCOVERED ──► STABILIZING ──► READY ──► SUBMITTED ──► (recognition, §8)
                  │              │   ▲          │
                  │              ▼   │          ├──► DUPLICATE_CONTENT   (terminal)
                  │           (changed) ◄───────┘
                  ├──► IGNORED        (unsupported / excluded name; terminal)
                  └──► VANISHED       (gone before ready; terminal, reopenable if it reappears)
                                 │
                                 └──► UNREADABLE  (never decodes within the limit → RESCAN REQUIRED)
```

`QUEUED` and `PROCESSING` in the user-facing lifecycle are **not duplicated on
the asset**: once submitted, the asset's processing state *is* its
`BatchScan.status`. Two sources of truth for one fact is how counts drift.

### 6.2 Stabilisation — a starting algorithm to qualify, not a guess to ship

An asset becomes READY only when **all** hold:

1. **Quiet period.** `(size, mtime_ns)` identical across at least *K*
   consecutive observations spanning at least *T* seconds. Suggested starting
   values for measurement, not defaults: *K* = 2, *T* = 3 s. The poll interval
   bounds the latency.
2. **Non-empty and a supported suffix**; temporary names written by scanner
   software (`*.tmp`, `~*`, names the source is configured to exclude) are
   ignored.
3. **Readable.** It can be opened for reading. On Windows a scanner still
   holding the file may refuse sharing — that is "not yet", not an error.
4. **Hashed and fully decoded from the same read.** A truncated JPEG or TIFF
   can have a valid header; the image must decode completely.
5. **Unchanged afterwards.** Re-stat after the read; any change returns the
   asset to STABILIZING.
6. **Not a duplicate** of an accepted asset (§5.1) — which diverts it to
   `DUPLICATE_CONTENT` rather than READY.

A file that never stabilises within a configurable ceiling is surfaced as
"stalled" rather than silently waiting forever. A file that stabilises but
never decodes becomes `UNREADABLE` → operationally RESCAN REQUIRED.

*K*, *T*, the poll interval and the ceiling are configuration, measured in
Phase B's tests and on a real SMB share in Phase F. Behaviour of `mtime`
granularity and caching on SMB clients is a known unknown; that is exactly why
the network-share qualification exists.

### 6.3 Restart

Stabilisation observations are **not trusted across a restart**: assets left
DISCOVERED/STABILIZING are re-observed from scratch; READY-but-unsubmitted
assets are re-verified (stat + hash) before submission.

---

## 7. Watching: reconciliation first, notifications second

- **Authoritative: periodic directory reconciliation.** Each enabled source is
  listed (`os.scandir`, optionally recursive) on an interval and the listing
  is diffed against known assets by relative path and observation. This alone
  is sufficient for correctness.
- **Optional: filesystem notifications**, only as a wake-up to reconcile
  sooner. Never the source of truth: notifications are lost on overflow, are
  unreliable over SMB, and deliver nothing for files created while OMRFlow was
  closed. There is no watcher dependency today; adding one (for example
  `watchdog`) needs the dependency audit (`packaging/audit_dependencies.py`)
  and the frozen-import check. `QFileSystemWatcher` would put Qt in the service
  layer and is excluded by the layering test. **`0.1.1-alpha.0` may ship with
  reconciliation only**; that is a complete implementation.
- **Recovery properties this gives for free:** a missed event, files present
  before the watcher started, files created during ten minutes of OMRFlow
  being closed, and a source that disappears and returns are all the same case
  — the next reconciliation finds what is unseen.
- **Source reachability.** A listing failure marks the source `unreachable`
  with the error and time; it never marks its assets missing or vanished, and
  never stops other sources. On return, a full reconciliation runs.
- **Cost at scale.** Listing a 10,000-file directory over SMB each interval is
  not free; Phase B measures it and may reconcile large sources incrementally
  (for example by skipping unchanged directories), but may not trade away
  correctness for it.

---

## 8. Feeding the existing pipeline: finite processing units

- A **unit scheduler** in the coordinator takes READY assets in a stable order
  (ready time, then asset id) and creates a new `ScanBatch` of at most *N*
  assets, or whatever is ready after *T* seconds of trickle, whichever comes
  first. Every unit is an ordinary Phase-5 batch: fixed membership, `pending`
  rows, `UNIQUE(batch_id, source_path)`, resumable, fingerprint-checked.
- One unit processes at a time through the unchanged `process_batch` /
  `parallel_batch`. Units are the granularity of pause and of crash recovery;
  the existing `recover_interrupted` and resume apply to them unmodified.
- The session **pins** the template identity and fingerprints; every unit
  must match. A template or threshold change mid-session is refused, or starts
  a new session, or goes through an explicit, audited operator
  acknowledgement modelled on today's `check_compatibility` prompt — Phase A
  decides which.
- Pool start-up is not free (`docs/scan_workflow.md` §10): keeping one warm
  pool across units, or sizing units so start-up is amortised, is a Phase C
  measurement.
- **Renamed output copies** (`_a`, `_b` suffixes) should not be produced per
  unit in session mode: arrival order is not reproducible, and a superseded
  original would take the plain `2103123.jpg`. Recommendation: in session mode,
  renamed copies are an explicit export over the **effective** set (§13),
  typically at closure, reusing `FilenameAllocator`.
- **Manual intake converges on the same path.** "Import folder into session"
  is a `manual` source reconciled once on demand; its files go through
  stabilisation, hashing, duplicate detection and units exactly like watched
  files. The finite Scan-page workflow outside a session is untouched (§16).

---

## 9. Concurrency and the single writer

Today writes happen from the `BatchWorker` thread during a run and from the
GUI thread during review, but review typically happens *after* a run. In a
live session intake, recording, conflict sync and human review all write
**at the same time**, and the engine sets no `busy_timeout`.

Requirements for Phase A to design and Phase C to prove:

- Workers still never touch the database.
- Every write stays short and transactional; no transaction spans I/O such as
  hashing or a directory listing.
- Either a single writer thread in the coordinator through which intake,
  recording and conflict sync submit their writes, **or** a bounded
  `busy_timeout` with retry, chosen explicitly and tested under contention.
  The GUI thread must never block for longer than a frame on a write.
- The rollback-journal choice (no WAL) stands unless Phase A records a reason
  to change it.
- **One OMRFlow coordinator per project.** Scanner PCs only write image files
  into their folders; they do not run OMRFlow against the project. Several
  OMRFlow instances writing one project remain out of scope — the existing
  project lock enforces it.

---

## 10. Operational outcomes and the scan-quality decision layer

### 10.1 Operational outcome of an asset

Derived — **projected**, in the Phase-6 spirit — from the asset, its
`BatchScan`, its conflicts and its relationships. Not a second stored truth;
a cached copy is allowed only with a recompute-equals-cache test.

| Outcome | Meaning |
|---|---|
| `ACCEPTED` | Recognised; no open conflict that `requires_resolution`; quality decision ACCEPT or ACCEPT WITH WARNING |
| `CONFLICT` | Recognised; at least one open identity conflict (sheet-local or cross-sheet) |
| `RESCAN_REQUIRED` | The physical image is unusable (§10.2) and no effective replacement exists |
| `SUPERSEDED` | Replaced by an effective replacement (§12). Kept, never deleted |
| `DUPLICATE` | Byte-identical to an earlier asset; not recognised |
| (in flight) | discovered / stabilising / ready / queued / processing |

**Recognition ambiguity is not physical scan failure.** An ambiguous roll or
set code is a `CONFLICT` and is resolved by a person reading the sheet; a
folded, unregistrable or undecodable image is `RESCAN_REQUIRED` and is resolved
by scanning the paper again. The existing conflict policy is unchanged: roll
and set-code ambiguity are conflicts; **answer ambiguity is never a conflict**.

### 10.2 The quality decision layer

A pure function — evidence in, decision out — with its policy as data:

```text
inputs:   recognition outcome (COMPLETE / REVIEW / REGISTRATION_FAILED / ERROR),
          decode failure, ScanQualityAssessment (status, issue codes, affected regions,
          evaluated / is_confirmed_clean), alignment warnings
policy:   a versioned, fingerprinted mapping, pinned by the session
output:   ACCEPT | ACCEPT_WITH_WARNING | RESCAN_REQUIRED, plus reasons
```

Starting mapping to *implement as configurable and label unvalidated*:

| Evidence | Suggested initial decision | Why it is only a suggestion |
|---|---|---|
| Image never decodes | RESCAN_REQUIRED | Nothing to read |
| Registration failed | RESCAN_REQUIRED | No values exist; but a wrong template also fails every sheet — a *rate* alarm is needed (§14) |
| `ScanQualityStatus.UNUSABLE` | RESCAN_REQUIRED | Identity region or most of the page untrustworthy |
| `REVIEW` | ACCEPT_WITH_WARNING (existing `scan_quality` conflict remains) | Most values legible; `docs/scan_quality.md` argues against discarding |
| `GEOMETRY_NOT_VERIFIED` | ACCEPT_WITH_WARNING | "Could not measure" is one shape a fold takes |
| `processing_error` | Neither — retry | A software fault is not a paper fault |

**No fold-percentage or displacement threshold is introduced by this line.**
The existing `ScanQualityThresholds` remain the evidence thresholds; the
decision mapping sits above them. Defaults are set by real-data qualification
(`ACCEPTANCE_CRITERIA.md` §5), and until then the UI and docs say they are
unvalidated.

---

## 11. Incremental conflict synchronisation

```text
unit results committed ──► per-sheet sync (existing sync_conflicts, idempotent)
                       └─► cross-sheet sync for the session (new scope)
                                   └─► review queue reflects both
```

- **Sheet-local conflicts** (identifier/set-code states, sheet-level types):
  run the existing `sync_conflicts` as each unit's results are committed —
  not after the whole session. Its idempotence already makes this safe.
- **Cross-sheet conflicts** (today only `identifier_duplicate`): computed over
  the **session's effective, reliable identifiers**, not one batch's. Worked
  example:

  ```text
  10:03  unit 12, sheet X reads 1705123 → unique, no conflict
  10:47  unit 40, sheet Y reads 1705123 → duplicate group {X, Y}
         → conflict rows for Y (unit 40) *and* X (unit 12) created or updated
  11:05  operator corrects Y's roll to 1705132 → group dissolves
         → X's duplicate conflict withdrawn (unless a human touched it); Y's kept with history
  ```

  This fits the existing identity key: each member's row keeps its own
  `batch_id` and `scan_id`, `group_key` carries the identifier,
  `related_scan_ids` may span units. What changes is the *scope* of the
  grouping query and its trigger (after every commit and after every decision
  that changes an effective identifier), plus the conflict's detail text
  ("in this batch" → "in this session").
- The recomputation must be **bounded**: an indexed query over identifiers
  touched since the last sync, not a reload of every row per commit.
- A superseded or duplicate-content asset **does not participate** in
  cross-sheet grouping; a replacement that shares the original's roll is not a
  duplicate of it.
- **Set code in duplicate grouping** — today's grouping ignores set code,
  while rosters are per set ("the same roll number in two sets is two
  unrelated candidates", `DATA_MODEL.md`). Whether a session groups by
  identifier or by (set, identifier) is a policy question for the examination
  office, not something to change silently. Phase C must surface it as a
  documented, tested `ConflictPolicy` option, default unchanged.
- Finite batches outside a session keep today's per-batch duplicate semantics
  exactly.

---

## 12. Rescan, replacement and supersession

```text
original asset ──► recognised ──► quality decision RESCAN_REQUIRED
                                        │  (rescan queue item opens)
         paper physically rescanned on any scanner
                                        │
replacement asset ──► discovered ──► ready ──► recognised
                                        │
            association: suggested, confirmed by a named operator (audited)
                                        │
                     replacement ACCEPTED / CONFLICT  → original SUPERSEDED, replacement EFFECTIVE
                     replacement RESCAN_REQUIRED     → chain continues; original stays open
```

- **Nothing is deleted.** The original keeps its image, provenance, source,
  quality assessment and reasons, whatever recognition it produced, and its
  conflicts (which leave the *active* queue by an exclusion rule, the way
  `review_store._resolution_only` retires legacy answer conflicts — not by
  deletion).
- **Association is explicit and audited** in `0.1.1-alpha.0`: OMRFlow
  *suggests* a replacement (for example, a new asset whose reliable roll and
  set match the original's partially-read identity, or the next asset from a
  source the operator put in "replacement mode"), and a named operator
  confirms. Every association, supersession and undo is an `audit_event`
  (entity type for assets), and the relationship is stored as data, not
  inferred from filenames. Automatic association without confirmation is a
  later option, only after qualification shows the suggestions are reliable.
- **Undo** is reopening: a new event restores the original as effective and
  the replacement as unmatched. History keeps both.
- **Unmatched replacement:** a new asset that looks like a rescan of an open
  item but has not been confirmed. It is recognised normally, and if it
  shares an identifier with an effective sheet it appears as a duplicate-ID
  conflict — which is correct until the operator links it.
- A replacement may itself need rescanning; chains are allowed, and the
  effective member is always the latest confirmed, accepted link.
- Existing `mark_for_reprocessing` (same file, re-read) is a different thing
  from replacement (different file, same paper) and both remain.

---

## 13. The effective scan set, and Phases 7–9

Phases 7–9 are keyed by `(roster_id, batch_id)` and the pages pick "the latest
batch". A session made of 40 units would therefore reconcile, score and report
**one unit** — useless, and silently so. This is why the phase plan gains a
dedicated phase (ROADMAP.md, phase E).

- **Effective scan set** of a session = assets that are recognised, not
  superseded, not duplicate-content, one `BatchScan` each (the latest
  effective reading).
- Phase 7 reconciliation, Phase 8 scoring and Phase 9 reporting must be able
  to take **a session's effective set** as their scope, in addition to a
  single batch. The representation (generalising the key to a scope, or a
  session-level run table beside the batch-level one) is a Phase E decision;
  **per-batch behaviour for finite projects must stay byte-for-byte the same.**
- While the session is open, reconciliation and results are **provisional**
  and labelled so; Final Export additionally requires the session to be
  closed (`report_readiness` gains that check).
- `ACCIDENTAL_RESCAN` exclusion in reconciliation remains available, but a
  superseded original never reaches reconciliation in the first place.

---

## 14. Session-level progress model

The per-unit `BatchProgressTracker` stays as it is. A **session snapshot** is
added: an immutable value computed by a bounded number of grouped SQL
queries, pulled by the GUI on a timer (the existing pattern), never pushed.

Counts, defined so they partition and can be checked:

```text
discovered (known workload, excluding ignored)
  = stabilizing + ready/waiting + queued + processing
  + accepted + conflict + rescan_required + superseded + duplicate
  + vanished + unreadable-pending-decision
```

The partition is an **assertion in tests and in the qualification campaign**,
not a hope.

Three independent progress lines — never one global percentage:

| Line | Numerator / denominator | Notes |
|---|---|---|
| Recognition | processed / (discovered − duplicate − vanished − ignored) | May go **down** when a scanner delivers 300 more: 3,476 / 3,527 → 3,476 / 3,827 |
| Conflict resolution | resolved / total required conflicts on effective scans | Cross-sheet conflicts can appear on already-accepted sheets |
| Rescan | replaced / ever required | "4 remaining" is the actionable number |

States shown:

| State | When |
|---|---|
| Processing | A unit is running |
| **Caught up — watching for new scans** | Session open; nothing stabilising, ready, queued or processing; every enabled source reconciled within its interval and reachable |
| Waiting for *Scanner B* (unreachable since 10:42) | A source is unreachable — never "caught up" |
| Processing paused | By the operator; discovery may continue |
| Closed | Finished (§15) |

"Caught up" never means "examination complete".

**Per-source diagnostics** (compact, secondary, collapsible): received,
accepted, conflicts, rescan required, duplicates, last file received,
reachability, and a **rate alarm** — for example a source whose recent
rescan-required or registration-failure rate is far above the others', which
usually means a scanner setting changed or the wrong template.

---

## 15. Session lifecycle: pause, resume, finish

| Action | Effect |
|---|---|
| **Pause Processing** | No new unit starts; the running unit finishes (existing cancel semantics). Discovery and stabilisation continue — cheap, read-only on sources, and keeps counts honest |
| **Resume Processing** | Units resume from READY assets |
| (optional) Pause / resume watching per source | For a scanner being serviced |
| **Finish Scan Session** | The operator's statement that *physical scanning for this examination is finished* |

Finish runs a final reconciliation of every configured source, then refuses
to close — listing each blocker — unless:

- no asset is stabilising, ready, queued or processing;
- no required conflict is unresolved;
- no rescan item is outstanding;
- no unmatched replacement exists;
- every enabled source was reachable for that final reconciliation (a
  disabled source is listed as such).

Closing is an audited event. Reopening a closed session (a late sheet found)
is allowed, audited, and makes results provisional again. A temporary absence
of new files never closes a session.

---

## 16. Backward compatibility

- **Additive migration only** (next is schema 10): new tables, nullable links
  from `scan_batch` and `batch_scan`. No existing row is rewritten; a batch
  with no session is a finite batch, exactly as today.
- The finite Scan-page workflow (Add Scan(s) / Add Folder → Process All →
  Resolve → Attendance → Results → Reports) is unchanged and stays the default
  when no session is open.
- Every existing test keeps passing unchanged; per-batch conflict, duplicate
  and reconciliation semantics are preserved for non-session batches.
- The Phase 10 stress runner and its 15 assertions are unchanged.
- **Forward-only schema.** Once a `0.1.0-alpha.2` project is opened by a
  schema-10 build, `0.1.0-alpha.2` will refuse it (`SchemaVersionError`).
  That is existing, intended behaviour, but it must be stated in the upgrade
  documentation, and the upgrade path should offer (or recommend) the existing
  backup first.
- An upgrade test opens a real schema-9 project fixture, migrates it, and
  proves every earlier query answers identically.

---

## 17. Existing defects and drift found while planning

Recorded, **not fixed** in this planning branch. Each is assigned to the
phase whose prompt must deal with it.

| Finding | Evidence | Assigned |
|---|---|---|
| Scans added to the list *after* a batch has been registered are processed but, by code reading, never persisted: `_ensure_batch` reuses `state.batch_id`, `add_scan_paths` does not reset it, and `record_results` matches only existing rows. **Inferred from code reading; not reproduced by a test** | `gui/scan/page.py` `_ensure_batch`, `add_scan_paths`; `services/batch_store.py` `record_results` | A: write a failing test first; fix is small and finite-mode, but only with that test |
| `ScanJobStatus.PROCESSING` is never written, so "processing" is never observable in the database | `services/batch_store.py` | C (the live counts need it) |
| `duplicate_groups`, `relink_scan`, `mark_for_reprocessing` have no caller in `src/` | `services/scan_provenance.py`, `services/batch_store.py` | B/C reuse them |
| `delete_batch` on a reprocessed batch would, by inference, hit the `batch_scan_history` delete trigger through the cascade | `services/batch_store.py`, migration 7 | A (note; test if touched) |
| `models.py` header says "schema version 1"; a docstring references a nonexistent `services/processing_manifest.py` | `database/models.py` | A (docstring only) |
| `CURRENT_STATE.md` says version `0.1.0.dev0`, updated 2026-09-20 | `development/CURRENT_STATE.md` | Whichever phase next updates it |
| `docs/wiki/Development-Roadmap.md` "Where `0.1.0-alpha.1` stands" was not updated for `alpha.2`, and says the clean-PC smoke test was "not yet performed" beside text saying it was run; test counts differ from the README (4,162 vs 4,659) | the two files | The next documentation pass on `main` — deliberately **not** edited here, to avoid conflicts with the concurrent update |
| `docs/scan_workflow.md` §14 still lists "no manual correction" although Phase 6 added it | `docs/scan_workflow.md` | D (it rewrites that document's session sections anyway) |
| Phase 11B's target is `v0.1.0-beta.1`; this plan moves Beta after the `0.1.1` line | `docs/wiki/Development-Roadmap.md` | Decision recorded in `ROADMAP.md`; canonical page updated when the plan is adopted |

---

## 18. Open design questions, by phase

| Phase | Question |
|---|---|
| A | Ingest-by-copy vs reference-in-place (ADR-0005) |
| A | Session↔batch and asset↔scan links: nullable columns vs link tables |
| A | Single writer thread vs `busy_timeout` + retry |
| A | Template change mid-session: refuse, new session, or audited acknowledgement |
| A | Are sources defined per session, or per project and attached to sessions? |
| B | Stabilisation *K*, *T*, poll interval, stall ceiling — measured |
| B | Recursive sources? Excluded patterns? Multi-page TIFF? |
| B | Whether to add a notification dependency at all in `0.1.1-alpha.0` |
| C | Unit size / trickle timeout; warm pool across units |
| C | Duplicate grouping by identifier or by (set, identifier) — office policy option |
| C | Replacement suggestion rules; whether any association is ever automatic |
| E | Representation of session-scoped reconciliation/scoring/reporting |
| D | How much of the source diagnostics is visible by default |
| F | Real default for the quality decision mapping, from real rejects |
