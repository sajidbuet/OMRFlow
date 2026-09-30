# `0.1.1-alpha.0` — architecture notes

> **Planning document. Nothing here is implemented.** Reconciled on
> 2026-09-30 against `main` at `128512d` (code as of `c30e809`; application
> version `0.1.0-alpha.2`; **schema version 12**). It replaces both earlier
> plans (see [ROADMAP.md](ROADMAP.md) §11). Table, column and class names are
> *illustrations of the required meaning*; each phase chooses the final
> representation after re-inspecting the code, and records any difference in
> its handoff.
>
> Facts marked *(verified)* were checked in the code for this reconciliation.
> Facts marked *(2026-09-29 inspection)* come from the inspection behind the
> superseded `development/ROADMAP_v0.1.1-alpha.0.md` and were not re-checked
> line by line. *By code reading* means not reproduced by running the
> application.

Contents

1. The architecture in one page
2. What exists on `main` today
3. The six latent `0.1.0-alpha.2` defects
4. Further findings from this reconciliation
5. `ScanBatch` — the finite processing and provenance unit
6. `ScanSession` — the operational aggregation unit
7. Set identity
8. The effective scan set and session-level results
9. Duplicates, rescans and supersession
10. Intake: sources, ledger, stabilisation
11. Processing units: from ready files to finite batches
12. The scan-quality decision layer
13. Concurrency, the single writer and crash-safe persistence
14. Session progress, controls and closure
15. Backward compatibility and migrations
16. Open design questions

---

## 1. The architecture in one page

```text
Project                persistent project and configuration container
 │                     (project folder, project.json, template, sets, rosters,
 │                      answer keys, report templates, intake sources)
 └── ScanSession       operational / examination-level aggregation unit
      │                (one examination's scanning run: its batches, its
      │                 pinned template identity, its lifecycle, its results)
      └── ScanBatch    finite processing / provenance unit
           │           (one import or one intake unit from one source;
           │            fixed membership once sealed; resumable; auditable)
           └── BatchScan   one sheet's recognition job and result
```

| Level | Is | Is not |
|---|---|---|
| **Project** | The persistent container: configuration, template, sets, rosters, keys, report templates, the database, and the project-level list of intake sources | A scanning run. A project may hold several sessions over its life (a main sitting, a supplementary sitting, a legacy import) |
| **ScanSession** | The examination-level unit that Attendance, reconciliation, scoring, Results and final Reports aggregate over. May stay open while further batches are created from any number of scanners, folders, computers, network shares or later rescans | A processing unit. Nothing is recognised "in a session" directly; it is always recognised in one of the session's batches |
| **ScanBatch** | A finite, auditable processing/import unit: one import or one intake unit, from one source, with a fixed membership once sealed, resumable and fingerprint-checked exactly as Phase 5 made it | An indefinitely growing container. A batch never becomes "the examination" |

Two workflows, one model:

```text
Traditional finite scanning              Continuous / multi-scanner scanning
───────────────────────────              ───────────────────────────────────
Project                                  Project
 └── ScanSession (created implicitly)     └── ScanSession "Final exam, 2026-10-12"  (open)
      └── ScanBatch  "Add Folder →              ├── ScanBatch  Scanner A · unit 1   sealed
                      Process All"             ├── ScanBatch  Scanner B · unit 1   sealed
                                               ├── ScanBatch  Scanner A · unit 2   sealed
The operator never sees the word               ├── ScanBatch  Manual import         sealed
"session" unless they want to.                 ├── ScanBatch  Rescans · Scanner C   sealed
                                               └── ScanBatch  Scanner B · unit 2   running
```

**Authoritative view:** Attendance, reconciliation, scoring, Results and final
Reports are computed over a **session's effective scan set** (§8).
**Diagnostic views:** every batch stays individually inspectable — its
membership, source, progress, failures, conflicts and processing record — for
provenance, debugging and operational monitoring. A batch-level result is never
the final result of a multi-batch session.

Principles carried from both earlier plans:

1. Preserve raw input; derive effective values in one place.
2. One central definition of set identity (canonical logical code, §7).
3. Discovery is idempotent: seeing the same bytes again never creates a second
   active script.
4. The database is the durable record — never GUI state or filesystem events.
5. Polled reconciliation of each source folder is the source of truth;
   filesystem notifications are hints at most.
6. A rescan is linked to the script it replaces, never treated as a new
   candidate.
7. Extend `scan_batch`, `batch_scan`, `scan_rejection`, `review_conflict` and
   `audit_event`; do not build a parallel pipeline.
8. The finite workflow (*Add Folder → Process All → Resolve → Attendance →
   Answer Key → Results → Reports*) keeps working with no new steps.
9. One writer to SQLite, deliberately; concurrency comes from worker processes
   that never touch the database.
10. Views that grow with a session stay lazy and SQL-filtered.
11. Processing and reprocessing stay deterministic and reproducible.
12. Nothing is deleted to express a decision.

---

## 2. What exists on `main` today

References are to `src/omr_scanner/`.

### 2.1 Schema

`SCHEMA_VERSION = MIGRATIONS[-1].version = 12` *(verified,
`database/migrations.py`)*. **Migration numbers 1–12 are taken and must never
be reused.** The next free number is **13**.

| # | Migration *(verified)* |
|---|---|
| 1 | Initial schema: `schema_migration`, `project_setting` |
| 2 | Phase 5 batch persistence: `scan_batch`, `batch_scan` |
| 3 | Phase 6 conflict review: `review_conflict`, `audit_event` |
| 4 | Phase 7 reconciliation: `candidate_roster`, `registered_candidate`, `reconciliation_run/entry/script/decision`; audit entity columns |
| 5 | Phase 8 scoring: `answer_key_revision`, `scoring_policy_revision`, `candidate_result` |
| 6 | Phase 9 reporting: `report_template_association`, `report_layout_config`, `generated_report` |
| 7 | Phase 10 hardening: `batch_scan` content-hash columns, `batch_scan_history`, `processing_manifest` |
| 8 | Project-level examination sets: `project_set` |
| 9 | Per-set attendance and templates: `candidate_roster.set_id`, `report_template_association.set_id/source_kind` |
| 10 | Attendance file provenance: `candidate_roster.source_path` |
| 11 | Reject & Rescan: `scan_rejection` |
| 12 | Answer-key provenance: `answer_key_revision.created_by`, template identity, source hash, metadata |

The superseded branch plan proposed "migration 10" for sessions against schema
9; that number now belongs to attendance-file provenance.

### 2.2 Batches and processing

| Fact | Where |
|---|---|
| `scan_batch`: id, `source_folder`, template id/name/path, geometry and recognition fingerprints, engine version, settings, `status` (`new/running/interrupted/cancelled/completed/completed_with_errors`), `total_scans`. **No session, source or seal column** *(verified)* | `database/models.py` `ScanBatch` |
| `batch_scan`: unique on `(batch_id, source_path)` only; `content_sha256` and algorithm columns (migration 7) *(verified)* | `database/models.py` `BatchScan` |
| **A batch already accepts more files at any time.** `add_scans_to_batch` appends rows and rewrites `total_scans`; the Scan page calls it whenever it already holds a batch id, so that a rescan is "read into the batch the original belongs to, because reconciliation, scoring and reporting all work per batch" *(verified, `services/batch_store.py:375`, `gui/scan/page.py:925`)* | as cited |
| *Reprocess All* abandons the stored batch and creates a **new** batch that reads the same files again *(verified, `gui/scan/page.py:1304`)* | `gui/scan/page.py` `reprocess_all` |
| Switching or reopening a project sets `state.batch_id = None` on Scan, Resolve, Attendance, Results and Reports *(verified)*; the next *Process All* therefore creates another batch | `gui/*/page.py` |
| `list_batches` orders by `updated_at DESC`; Attendance, Results and Reports take `list_batches(limit=1)` *(verified, `services/batch_store.py:1057`, `gui/attendance/page.py:1220`, `gui/results/page.py:339`, `gui/reports/page.py:367`)* | as cited |
| `recover_interrupted` on open: `queued/processing → pending`, `running → interrupted`, **and bumps `updated_at`** *(verified, `services/batch_store.py:565`)* | as cited |
| Workers never touch the database; a bounded spawn pool; results committed in groups from the batch QThread *(2026-09-29 inspection)* | `services/parallel_batch.py`, `batch_store.BatchRecorder` |
| No pause; *Cancel* discards in-flight results *(2026-09-29 inspection)* | `gui/scan/worker.py` |
| SQLite rollback journal, `PRAGMA foreign_keys=ON`, **no `busy_timeout`** *(verified, `database/engine.py`)* | as cited |
| A backup is taken before a migration runs *(verified, `services/project_service.py` `_backup_before_migration_if_needed`)* | as cited |
| `ProcessingManifest` (migration 7) is read by `evaluation/qualification.py`; nothing in `src/omr_scanner/services` or `gui` writes it *(verified by search)* | as cited |

### 2.3 Review, provenance and rescans

| Fact | Where |
|---|---|
| `review_conflict` identity includes `batch_id`; indexes by `(batch_id, state)` and `scan_id` *(verified)* | `database/models.py` |
| `sync_duplicate_identifiers(database, batch_id)` groups **one batch**, reliable identifiers only, result-eligible scans only *(verified, `services/review_store.py:827`)*; per the 2026-09-29 inspection it groups on the machine reading, not the corrected value | as cited |
| Resolve loads **one batch** (`load_batch`); counts and rescan counts are per batch (`count_conflicts(batch)`, `scan_lifecycle.count_cases(batch)`) *(verified, `gui/review/page.py:1363`, `:1606`)* | as cited |
| Content hash is computed at the start of each run by `compute_hashes_for_batch` from the batch worker, not at registration *(verified, `gui/scan/worker.py:224`)* | as cited |
| `duplicate_groups` and `relink_scan` have no caller in `services/` or `gui/` *(verified by search)* | `services/scan_provenance.py` |
| **Reject & Rescan** (migration 11): `scan_rejection`, one row per scan, holding the logical lifecycle `active / rejected_pending_rescan / superseded_by_replacement / reimport_of_rejected`, reason codes (`folded`, `poor_quality`, `registration`, `clipped`, `skew`, `wrong_document`, `id_unreadable`, `other`), declared vs recognised identity, file state and purge record; history in `audit_event` (`entity_type = scan_lifecycle`) *(verified)* | `database/models.py` `ScanRejection`, `domain/scan_lifecycle.py` |
| Rejection is an **operator decision** (`reject_scan`); replacement is **confirmed by a named operator** (`confirm_replacement`), undoable (`remove_replacement`, `undo_reject`); re-imports of rejected bytes detected by hash (`sync_reimports`); purge to quarantine *(verified)* | `services/scan_lifecycle.py` |
| A replacement may come from **any** batch and "counts in the batch of the sheet it replaces": `adopted_replacements(batch)` pulls it in there, `counted_elsewhere(batch)` leaves it out of its own batch *(verified, `services/scan_lifecycle.py:1343`, `:1364`)* | as cited |
| Scan quality `PASS / REVIEW / UNUSABLE` + issue codes, surfaced as one `scan_quality` conflict *(2026-09-29 inspection)*; nothing turns a quality verdict into a rejection automatically *(verified: `reject_scan` has only operator callers)* | `domain/scan_quality.py`, `services/conflict_policy.py` |

### 2.4 Downstream (Phases 7–9)

| Fact | Where |
|---|---|
| `reconciliation_run` unique `(roster_id, batch_id)`; `reconciliation_entry` unique `(roster_id, batch_id, entry_key)`; `reconciliation_script` unique `(roster_id, batch_id, scan_id)`; `reconciliation_decision` unique `(roster_id, batch_id, target_kind, target_key)`; `candidate_result` unique `(roster_id, batch_id, candidate_id)`. `batch_id` is a **NOT NULL foreign key to `scan_batch`** with `ON DELETE CASCADE` *(verified)* | `database/models.py` |
| `generated_report` has **no batch or scope column** — only `set_code`, template, key/policy revisions, counts and output *(verified)*; `report_store` reads results by `(roster_id, batch_id)` | `database/models.py`, `services/report_store.py` |

### 2.5 Set codes

| Fact | Where |
|---|---|
| `find_conflicting_set` compares **exactly**, by design (`"a"` and `"A"` are distinct sets) *(verified, `domain/exam_sets.py:183`)* | as cited |
| Scoring upper-cases before key lookup (`scoring_store.py:374`, `:408`); the Answer Key stage upper-cases in several places (`answer_key.py:651`, `:689`, `:760`, `:795`) *(verified)* | as cited |
| No logical ↔ physical set mapping exists | — |

### 2.6 Names already in use

`ProjectSession` (`services/project_service.py:83`) and the SQLAlchemy ORM
`Session` (the variable `session` appears throughout the services) *(verified)*.
The new entity is named **`ScanSession`** in code and "Scan session" in the UI.
To keep it unambiguous, code must **never** name a `ScanSession` variable
`session`; use `scan_session` / `scan_session_id`.

---

## 3. The six latent `0.1.0-alpha.2` defects

Identified by the 2026-09-29 inspection and retained. Status as of this
reconciliation, and how the reconciled model resolves each:

| # | Defect | Re-checked 2026-09-30 | Resolved by |
|---|---|---|---|
| 1 | **Late scans split the cohort.** After a project is reopened the Scan page holds no batch, so the next *Process All* creates a second batch; Attendance, Results and Reports then read only one batch, and candidates whose scripts are in the other appear **absent** *(by code reading)* | Still true: `state.batch_id = None` on project change; downstream reads `list_batches(limit=1)` | Phase B: the second batch joins the **same open session**. Phase C: downstream aggregates the session, so nobody is absent because their script is in another batch |
| 2 | **What Results reads can change silently.** "Latest" is `ORDER BY updated_at DESC`; retrying failed sheets in an older batch, and `recover_interrupted` on every project open, bump `updated_at` — so an older interrupted batch becomes the one read after opening the project *(by code reading)* | Still true (`batch_store.py:565`, `:1057`) | Phase B: an explicit **active session** pointer; Phase C: pages select a session explicitly. `updated_at` never chooses what is scored |
| 3 | **Duplicate Student IDs are detected on the machine reading, per batch.** A corrected ID that collides is not flagged; the same candidate in two batches is never flagged | Still per batch (`review_store.py:827`) | Phase C: session-wide duplicate detection on **effective** identifiers |
| 4 | **Byte-identical images are processed twice** when added under different paths, producing two active scripts | Still true; hash computed at run start, `duplicate_groups` unused | Phase C: hash at registration and session-wide exact-duplicate handling for manual adds; Phase D: hash at discovery for intake |
| 5 | **Set-code case mismatch.** A lower-case set code cannot find its answer key in Results (key stored as typed, lookup upper-cased) | Mechanism still present (exact set registry, upper-cased key lookup) | Phase A: canonical set identity |
| 6 | **`processing_manifest` is never written**, though documented as the batch's processing record | Still true for `src/omr_scanner/services` and `gui` | Phase B: written at batch seal / run boundaries |

---

## 4. Further findings from this reconciliation

These are implementation consequences of the chosen architecture, found by
checking it against current `main`. Each is assigned to a phase.

| # | Finding | Consequence | Phase |
|---|---|---|---|
| F1 | Batches are **not** finite today: `add_scans_to_batch` extends any batch, and the Scan page uses it for rescans because downstream is per batch | "Finite" must be made explicit: a batch gains a **sealed** state after which its membership never changes (§5). Rescans go into a new batch of the same session instead. `add_scans_to_batch` stays valid only for an **unsealed** batch | B |
| F2 | *Reprocess All* creates a new batch that reads **the same files** again | Aggregating a session naively would count every sheet twice. Supersession must be a **first-class concept**, not a *Reprocess All* special case: a reprocessing batch supersedes the batch it re-reads, and the same mechanism covers whole-batch rescans, future algorithmic reprocessing and recomputation after reopening. Only effective, non-superseded membership contributes to session-level results (§5.5, §8.1) | B (model), C (effective set) |
| F3 | The rescan rule "a replacement counts in the batch of the sheet it replaces" (`adopted_replacements`, `counted_elsewhere`) exists only because results are per batch | Inside one session the rule becomes simply "counts once, in the session". The functions remain for batch-level diagnostic views and for replacement links that cross **sessions** (legacy data, §9.3) | C |
| F4 | Every Phase 7–8 table is unique on `(roster_id, batch_id, …)` with `batch_id` a NOT NULL FK to `scan_batch`. SQLite cannot alter a UNIQUE constraint or a column's nullability in place | Session scope needs either **table-rebuild migrations** (copy into new tables with a scope key, the standard SQLite 12-step procedure) or **parallel session-scoped tables** beside the batch-scoped ones. Both are large; Phase C decides by ADR (§8.3). This is the single largest migration of the line | C |
| F5 | `generated_report` records no batch or scope | A report cannot say which session (or batch) it was generated from. Phase C adds a scope column (additive) | C |
| F6 | Resolve is per batch (`load_batch`, `count_conflicts(batch)`, `count_cases(batch)`) | Session-level Resolve queue needed; batch filter kept as a diagnostic | C |
| F7 | `review_conflict` identity includes `batch_id` | Fits session scope unchanged: each member keeps its own `batch_id`/`scan_id`, only the grouping query's scope widens (§9.2) | C |
| F8 | `numeric_version` maps the prerelease *number* only, ignoring the channel: `0.1.0-beta.1 → (0,1,0,1)` sorts **below** the released `0.1.0-alpha.2 → (0,1,0,2)`, and `0.1.0-alpha.N` and `0.1.0-beta.N` collide *(verified, `_version.py:160`)*. **That is the bug**: within one base version a Beta must follow its Alphas. By contrast, `0.1.0-beta.x < 0.1.1-alpha.0` is **normal** semantic-version ordering (0.1.1 is newer than 0.1.0) and not a defect | **Decided:** F8 must be fixed and tested before any Beta release. The Beta that follows the `0.1.1-alpha.x` line is **`v0.1.1-beta.x`**; `v0.1.0-beta.x` is **not** used as a successor to `v0.1.1-alpha.0` (§16 Q10) | G / 11B |
| F9 | The superseded branch plan's "late-added scans are never persisted" defect | **No longer present**: fixed on `main` by `add_scans_to_batch` (its docstring records the old bug). Not carried forward | — |
| F10 | The superseded branch plan stated batch membership is "fixed at creation" and there is "no API to add scans" | **No longer true** (see F1). Not carried forward | — |
| F11 | Current `main` provides substantial crash-resilient persistence **primitives**, but the complete Scan/Resolve recovery workflow does **not** yet meet the `0.1.1` requirement: committed sheets lose their conflicts after a crash (S1), nothing re-adopts an interrupted batch on reopen (S2), the GUI counts uncommitted sheets as done (S3), and Resolve cannot reach saved work after reopen (R1) | Crash-safe persistence and resume made an explicit requirement with a durable-completion invariant and defined work units (§13.2–13.7). **S1, S2, S3 and R1 are required defects to close before `0.1.1-alpha.0` is released** | B (S1–S3, R1), C, E, G |

---

## 5. `ScanBatch` — the finite processing and provenance unit

### 5.1 Meaning

A `ScanBatch` is one import or one intake unit: a set of files from **one
source**, recognised with one template identity, recorded durably, resumable
and fingerprint-checked. Every Phase 5 and Phase 10 property is kept:
`pending → queued → processing → completed | warning | failed | cancelled`,
resume in `batch_index` order, `recover_interrupted`, compatibility checks,
grouped commits, reprocessing history, the Phase 10 harness.

### 5.2 Lifecycle invariants

These are **semantic requirements, not mandated identifiers**. Where the
eventual enum or column names differ (for example a `sealed_at` timestamp
rather than a state value), the existing project terminology wins; the
meanings below do not change. Membership state is orthogonal to the existing
processing `status` (`new/running/interrupted/…`).

| Membership state | Invariant |
|---|---|
| **OPEN** | The batch **may receive members** (today's behaviour on the Scan page while the operator is building a list) |
| **SEALED** | The batch **may not receive new members**, ever. `total_scans` is final. Processing, resume, retry, review, rejection and replacement of its existing members continue |
| **SUPERSEDED** | The batch **remains fully auditable** — rows, results, conflicts, history, files — but is **excluded from effective session aggregation** (§5.5). Only a sealed batch can be superseded |

**SEALED is not COMPLETED.** Sealing freezes membership only; a sealed batch
may still have pending, queued, failed or interrupted sheets, and resumes that
unfinished processing normally. Recovery after an interruption **preserves** the
batch's lifecycle state — an interrupted OPEN batch stays OPEN, an interrupted
SEALED batch stays SEALED and resumes — and recovery itself never creates a
ScanSession, a ScanBatch or a superseding batch (§13.5).

Who seals: the operator starting another batch; the intake scheduler at unit
creation (a scheduled unit is sealed when created); closing the session
(§6.1); or, in the finite workflow, the trigger Phase B defines test-first
against today's Scan-page behaviour (§16 Q2). Once sealed, a batch never grows;
a rescan, a late file or a new folder goes into a **new batch of the same
session**. Every seal is an audit event.

### 5.3 Role and lineage

| Role | Created by | Contribution |
|---|---|---|
| `scan` | An import (*Add Folder → Process All*) or an intake unit | Its effective members count |
| `rescan` | Importing replacements for rejected sheets | Members count through a confirmed `scan_rejection` link, or as ordinary new scripts if unlinked |
| `reprocess` | *Reprocess All*, or any later re-reading of the same files (§5.5) | Supersedes the batch it re-reads |
| `legacy` | Backfill of a batch that existed before the upgrade | As `scan` |

A batch carries a **source** reference (manual import, or a watched source;
§10). One batch = one source keeps provenance simple: "which scanner produced
this sheet" is always answered by the batch.

### 5.4 What a batch-level view still offers

Membership, source, per-sheet status and attempts, timings, failures, the
processing manifest, its conflicts filtered from the session queue, its
rejections, its supersession lineage, and — as a diagnostic, never as the
final result — a batch-scoped reconciliation/score preview if Phase C keeps one
(§8.3). Superseded batches stay browsable here, clearly marked.

### 5.5 Supersession — a first-class concept

Supersession is **not** a special case of *Reprocess All*. It is the general
mechanism by which a later reading takes the place of an earlier one, at two
granularities that share one meaning:

| Granularity | Record | Exists today? |
|---|---|---|
| **Sheet** | `scan_rejection` link: original `superseded_by_replacement`, replacement effective (Reject & Rescan) | Yes — preserved unchanged |
| **Batch** | A batch-level supersession record: which batch supersedes which, the reason, by whom, when; undone only by an audited reversal | New (Phase B) |

**The invariant, binding on every phase:**

> Only effective, non-superseded batch/sheet membership contributes to
> session-level attendance, reconciliation, scoring, results and final
> reports. Superseded batches remain retained for provenance and audit but do
> not contribute to effective totals.

It must cover at least:

- ***Reprocess All*** — the new `reprocess` batch supersedes the batch it
  re-reads.
- **Replacement / rescan batches, where appropriate** — supersession of a
  rescanned sheet is normally per sheet (`scan_rejection`), so a `rescan` batch
  does *not* supersede the original batch as a whole; batch-level supersession
  applies only when an operator deliberately rescans an entire batch to replace
  it.
- **Future algorithmic reprocessing** — a re-read with a newer engine, template
  calibration or policy is a new `reprocess` batch that supersedes the old one;
  the old reading is never overwritten in place.
- **Reopening or recomputing a session** — recomputation always derives the
  effective set from the supersession records as they stand at that moment; it
  never resurrects a superseded batch or sheet, and an undone supersession
  takes effect only through its own audited reversal.

Rules: supersession **never deletes** a batch, a sheet, its results or its
records; chains are allowed (the effective member is the latest non-superseded
one); a batch cannot supersede a batch of another session; contradictory
relationships (cycles, a batch superseded by two live batches, a sheet
effective through two paths) are refused when recorded and reported by the
health check. The effective-set service (§8.1) is the **only** place these
rules are evaluated.

---

## 6. `ScanSession` — the operational aggregation unit

### 6.1 Lifecycle invariants

Semantic requirements, not mandated identifiers (as §5.2).

| State | Invariant |
|---|---|
| **OPEN** | **May receive new finite batches** (imports, intake units, rescan and reprocess batches). Results are **provisional** |
| **CLOSED** | **May not receive new batches or intake.** Every contributing batch is **sealed**. Final reconciliation, scoring, results and reports represent the closed session's **authoritative state**. A change that would alter an effective value requires reopening first (Phase C fixes the exact boundary, e.g. whether viewing and annotating remain allowed) |
| **REOPENED** | An explicit operator action returns the session to an **editable, open** state (semantically OPEN). Reopening **invalidates prior final-export status**: every final report generated while closed is marked stale, and final outputs must be regenerated after subsequent changes. Reopening and re-closing are each audit events |

Closing runs the checks of §14.3 and seals any still-open batch of the session
(audited). Files arriving from watched sources for a closed session are
**held**, never silently added (§10.2).

A session **pins** the template identity and fingerprints of its first batch.
Every later batch must match, or the operator explicitly acknowledges a change
(audited, modelled on today's `check_compatibility` prompt), or starts a new
session — Phase B decides which by ADR.

### 6.2 Implicit sessions keep the finite workflow simple

- The first *Process All* in a project with no open session **creates one
  silently** (named after the exam and date). The operator is not asked
  anything.
- Reopening the project re-adopts the **active session** (a `project_setting`
  key; no migration needed for the pointer). The next *Process All* creates a
  new batch **in the same session** — which is exactly what fixes defect 1.
- Session management (name, close, reopen, new session, combine) lives in one
  compact place on the Scan stage and is optional. A project with one session
  and one batch looks and behaves as today.
- **Closing does not need that place.** At Final Export, if the session is
  still open, OMRFlow offers one step, **"Close session and generate final
  export"**: it runs the closure checks, closes (sealing the batches) and
  generates the final outputs — or lists the blockers and closes nothing
  (§8.2).
- Downstream stages default to the active session and name it in their header.
  They never pick "the latest batch".

### 6.3 Multi-scanner sessions

A session with watched sources (§10) keeps creating sealed batches, one or more
per source, while open. Late files, rescans and manual imports join as further
batches. Sessions do not share batches; a batch belongs to exactly one session.

### 6.4 Existing projects: backfill and combining

**Decided.** Migrations change structure, not data (`migrations.py` module
rule), so the session backfill is the **upgrade step that runs on the first
read-write open after the migration** — part of the upgrade as far as the
operator is concerned, and audited:

- **Each existing batch becomes its own one-batch `ScanSession`** by default
  (role `legacy`). Every existing per-batch result stays identical.
- **Batches connected by a confirmed rescan/replacement relationship** (a
  `scan_rejection` link in state `superseded_by_replacement` whose original and
  replacement are in different batches) are placed **in the same migrated
  session where the relationship is unambiguous**. Where grouping would be
  ambiguous — for example it would chain otherwise unrelated batches together
  through several links, or the linked batches cannot share one session
  consistently (different template identity) — they stay in separate sessions,
  the existing "counts where the original is" rule keeps the result unchanged
  (§9.3), and the upgrade report lists the case. Phase B defines "unambiguous"
  precisely, test-first.
- **Unrelated historical batches are never combined automatically**, even when
  they belong to the same project and look like one examination.
- Read-only mode backfills nothing; a batch with no session is presented as a
  virtual one-batch session.

**"Combine into one session"** (operator action; repairs a defect-1 project).
It must:

- operate only on batches (or sessions) **of the same project**;
- be **explicit**, started by a named operator — never automatic;
- be **audit-logged** (which sessions and batches, by whom, when, why), and be
  undone only by an audited reversal;
- **validate before committing** that the combination introduces **no duplicate
  effective sheets** (exact-content duplicates, or two effective scripts for one
  candidate that would not surface as a conflict) and **no contradictory
  supersession relationships** (§5.5); on failure, refuse and list the reasons;
- respect template pinning (§6.1) and session state: combining into a CLOSED
  session is refused (reopen first).

---

## 7. Set identity

Carried from the superseded `main` plan, unchanged in substance.

### 7.1 Canonical, case-insensitive logical codes

- Canonical form: `unicodedata.normalize("NFKC", code).strip().upper()`.
  The stored `code` keeps the operator's spelling for display.
- One module (proposed `services/set_identity.py`: `canonical_code()`,
  `logical_for_physical()`, `physical_for_logical()`). **Every** set-code
  comparison goes through it: `exam_sets.find_conflicting_set` (defining `a`
  when `A` exists is refused), `review_store.sync_undefined_set_codes`,
  `reconciliation_store` placements, `scan_lifecycle` declared-set validation,
  `scoring.usable_set_code`, `scoring_store` key lookups and verified keys,
  `report_store` associations, `report_readiness`, the Answer Key stage.
- An architecture test forbids direct comparisons of `set_code` fields outside
  the module, in the style of `tests/unit/test_architecture.py`.
- This **reverses a documented design decision** in `find_conflicting_set`'s
  docstring; the change must update that docstring and `docs/DATA_MODEL.md`.

### 7.2 Optional logical ↔ physical mapping

- `project_set.physical_mark` (NULL = the sheet prints the logical code).
  Validated with `services/answer_key.can_print_set_code` and unique among sets.
- **Raw is kept**: `batch_scan.set_code_value` and the review ledger keep what
  was read or corrected **on the paper** (physical).
- **One translation point**: `review_store`'s effective set derivation via
  `set_identity`. Reconciliation, scoring and reports already read effective
  codes and become correct without their own mapping.
- Physical-aware consumers: Resolve's set editor (offers marks, shows the
  logical set), the Answer Key solution-sheet check (`check_sheet_set`), the
  synthetic generator (marks the physical symbol). Rejection's
  `declared_set_code` stays logical. Exports show *Set (as read)* and *Set*.

### 7.3 Collisions in existing projects

If two existing sets canonicalise alike (`A`, `a`), the migration fills
`canonical_code` for the first by `display_order` and leaves the other NULL;
Project Health and Project Configuration report the collision, and set-dependent
stages refuse to score until the operator renames or merges. **No automatic
merge.** Stored `answer_key_revision.set_code`, `batch_scan.set_code_value`,
`candidate_result.set_code`, `generated_report.set_code` are **not rewritten**;
they are compared through `canonical_code()`.

---

## 8. The effective scan set and session-level results

### 8.1 Definition

This is where the supersession invariant (§5.5) is enforced: only effective,
non-superseded batch/sheet membership contributes to session-level attendance,
reconciliation, scoring, results and final reports.

The **effective scan set** of a session is the set of `BatchScan` rows that:

- belong to a batch of the session that is **not superseded** (§5.5 — whatever
  the cause: *Reprocess All*, algorithmic reprocessing, a whole-batch rescan);
- are result-eligible in the Reject & Rescan lifecycle (`active` only —
  `rejected_pending_rescan`, `superseded_by_replacement` and
  `reimport_of_rejected` are excluded, as today);
- are not an exact-content duplicate of an earlier effective scan (§9.1);
- plus confirmed replacements for the session's originals, wherever they were
  read (§9.3).

A pure, tested service computes it, with counts of each exclusion reason.

### 8.2 What aggregates over it

| Stage | Authoritative (session) | Diagnostic (batch) |
|---|---|---|
| Resolve | One queue over the session; cross-batch duplicate groups navigable | Filter by batch or source |
| Attendance / reconciliation | Session effective set against the roster | Which batch each script came from, shown per row |
| Answer Key | Unchanged (project-level keys) | — |
| Results / scoring | Session | Batch preview only if kept (§8.3) |
| Reports | Session; `generated_report` records the scope | — |

Rules:

- While the session is **open**, results are **provisional** and labelled so on
  screen and in every export. **Final Export requires a CLOSED session**
  (`report_readiness` gains that check).
- **One-step close at Final Export (decided, §16 Q4).** If the session is still
  open, Final Export offers **"Close session and generate final export"**:
  run the closure checks (§14.3); if any fail, list the blockers and change
  nothing; otherwise close the session (sealing its batches, audited) and
  generate the final outputs from the now-authoritative state. A normal finite
  import never needs a separate session-management screen.
- **Final-export status follows the session.** Final outputs are recorded
  against the session and the close they were generated from. **Reopening the
  session marks them stale**; they are shown as such, and Final Export must be
  regenerated after any subsequent change. Re-closing does not revive stale
  outputs — only regeneration does.
- Reconciliation decisions made while open survive growth and closure;
  re-reconciling after new arrivals is idempotent and explains what changed.
- A single-batch session must produce **byte-identical** reconciliation rows
  and workbook cell values to today's per-batch run on the same data (golden
  test).

### 8.3 Representation — an ADR for Phase C

Because of F4, choose one after inspection, and record it:

- **(a) Generalise the scope key.** Rebuild the Phase 7–8 tables with a scope
  (`scan_session_id`, and for diagnostic previews optionally `batch_id`),
  copying existing rows into the scope of the session each batch was
  backfilled into. One code path; biggest migration; highest regression risk.
- **(b) Session-scoped tables beside batch-scoped ones.** New tables keyed
  `(roster_id, scan_session_id, …)`; the existing tables stay for legacy
  batch-level data and previews. Smallest migration; two code paths to keep in
  step.
- **(c) Session as the only scope from now on**, existing batch-keyed rows
  migrated once into their backfilled sessions, batch-level reconciliation
  dropped as a stored concept (batch views become filters over the session's
  rows). Likely the cleanest long-term answer; needs (a)'s rebuild.

The recommendation to evaluate first is **(c)** — since every batch belongs to
a session after backfill, a stored batch-level result has no remaining
authoritative use — but the decision is Phase C's, on evidence.

---

## 9. Duplicates, rescans and supersession

### 9.1 The cases

| Case | Detection | Outcome |
|---|---|---|
| Exact duplicate image (same bytes, any source, any batch of the session) | SHA-256, computed at registration (manual) or discovery (intake) | Not registered as a second active script: recorded, linked to the first, listed for the operator; zero recognition cost. **Session-wide**, not per batch |
| Same physical sheet scanned twice (different bytes) | Same **effective** Student ID (optional answer-similarity hint, P2) | `identifier_duplicate` conflict over the session; reconciliation `DUPLICATE_SCRIPT`; operator marks primary or rejects one |
| Rejected sheet later rescanned (any scanner) | Existing *Rescan* flow; suggestions from `replacement_candidates`, now also by source and arrival time | `scan_rejection` link; original `superseded_by_replacement` (existing, audited, undoable) |
| Re-import of a rejected image | Existing `sync_reimports` by hash | Unchanged; moves to registration/discovery time |
| Reprocessing the same files | Batch role `reprocess` | Earlier batch superseded as a whole (§5.3) |
| Same Student ID on two different physical scripts | Indistinguishable from case 2 by machine | Conflict; human decision with write-in box and answers as evidence |
| Candidate in two **sessions** | Not a conflict by itself (different examinations/sittings) | Reported on request; never merged |

### 9.2 Session-wide duplicate-ID conflicts

- Grouped over the session's **effective, reliable** identifiers (corrections
  included), not one batch's machine readings.
- Each member row keeps its own `batch_id`/`scan_id`; `group_key` carries the
  identifier; `related_scan_ids` may span batches; detail text says "in this
  session". The identity key of `review_conflict` does not change (F7).
- Re-evaluated incrementally after each commit and after each decision that
  changes an effective identifier; bounded (only affected groups, indexed).
- Worked example (from the branch plan): 10:03 sheet X reads `1705123`, unique;
  10:47 sheet Y in another batch reads `1705123` → conflicts on **both**; 11:05
  Y corrected to `1705132` → X's conflict withdrawn unless a human touched it.
- **Grouping by identifier or by (set, identifier)** is an examination-office
  policy question (rosters are per set). Offered as a documented
  `ConflictPolicy` option, **default unchanged**.

### 9.3 Replacements and sessions

- Within a session: a confirmed replacement counts **once, in the session**,
  whichever batch read it (F3). No new mechanism.
- Across sessions: new cross-session links are **refused** in `0.1.1-alpha.0`
  (a replacement is read into the session of the original). Legacy
  cross-batch links are kept intra-session by the backfill rule (§6.4); if one
  cannot be, the existing "counts where the original is" rule applies at
  session level.
- Nothing is deleted; undo remains reopening; chains are allowed; every step is
  an `audit_event`.

---

## 10. Intake: sources, ledger, stabilisation

### 10.1 Sources

- **Project-level** rows (a source outlives sessions), attached to a session
  when it is used: label, folder (local or UNC), kind (`watched` / `manual`),
  recursive flag, exclusions, enabled, reachability (`online`, `unreachable`,
  `permission_denied`, `disabled`) with detail and time, last successful
  reconciliation, last file seen. A built-in `manual` source is created on first
  use by the service, not by the migration.
- **Identity is not provenance.** The source that produced a file is recorded,
  never used to decide whose script it is.

### 10.2 The intake ledger

One row per file observed in a source: source, relative path, absolute path
seen, size, `mtime_ns`, content hash, state, first seen, last seen, stable
since, attempts, last error, the `BatchScan` it became (or the effective scan
it duplicates). Unique on `(source, relative path, content hash)`; indexed on
hash and state. `(source, relative path, size, mtime)` only avoids re-hashing
unchanged files on each poll — **path is never identity**.

```text
intake file:  discovered → stabilizing → ready → registered (→ its BatchScan)
                   │            ▲   │        └→ duplicate_content   (terminal)
                   │   (changed)└───┘
                   ├→ ignored     (unsupported / excluded; terminal)
                   ├→ vanished    (gone before ready; reopenable if it reappears)
                   ├→ unreadable  (never decodes after retries → rescan required)
                   └→ held        (arrived for a closed session; operator decides)
batch_scan:   pending → queued → processing → completed | warning | failed   (unchanged)
```

Once registered, the file's processing state **is** its `BatchScan.status` —
never duplicated on the ledger row.

### 10.3 Collision rules

| Situation | Treatment |
|---|---|
| Same filename, different sources | Different files. Three scanners all writing `000001.jpg` is normal |
| Same relative path, same observation | Already known; nothing happens (idempotence) |
| Same relative path, **different content** after registration (counter reset, overwrite) | A new ledger row flagged `path_reused`; the earlier scan is untouched |
| Same hash as an effective scan in the session | `duplicate_content`, linked, not recognised |
| File disappears before ready | `vanished`, kept as a record |
| File disappears after registration | Existing availability check / relink; if ingested by copy (§10.5), nothing downstream is affected |

### 10.4 Stabilisation

A file becomes `ready` only when **all** hold:

1. Supported suffix, non-empty, not a temporary name (`*.tmp`, `*.part`, `~*`,
   `.*`, configured exclusions).
2. `(size, mtime_ns)` unchanged across ≥ *K* observations spanning ≥ *T*
   seconds.
3. Openable for reading (a Windows sharing violation means "not yet", not an
   error).
4. Hashed **and fully decoded from the same read** (a truncated JPEG/TIFF with
   a valid header must fail).
5. Re-stat unchanged after the read.
6. Not an exact duplicate of an effective scan (§10.3) — diverted instead.

Starting values to **measure, not ship blindly**: *K* = 2; *T* = 3–5 s local,
15 s network; poll interval 10 s local, 30 s network; 3 decode attempts with
backoff; a stall ceiling that surfaces a never-stable file. Observations are
not trusted across a restart: unstable files re-stabilise; ready-but-unregistered
files are re-verified.

### 10.5 Reading in place or ingesting a copy — an ADR for Phase D

ADR-0002 references scans in place. For a live session that is risky: Resolve
re-reads the sheet, and a scanner PC's share can go offline or be cleared. To
decide: **ingest by copy** for watched sources (copy into the project after
ready, verify the copy's hash, recognise the copy, keep the original path as
provenance; the source is never modified), reference-in-place for manual local
imports (today's behaviour). The ADR must record disk-space and synchronised-
folder consequences.

### 10.6 Watching

Periodic reconciliation (`os.scandir`) of each enabled source is sufficient and
authoritative. Notifications may only trigger an earlier reconciliation;
`QFileSystemWatcher` is excluded from services by the layering test, and any
new dependency needs the packaging dependency audit. **Shipping with
reconciliation only is a complete implementation.** A listing failure marks the
source unreachable, never its files vanished, and never stops other sources;
on return a full reconciliation runs. Files written while OMRFlow was closed are
found by the first reconciliation after start.

---

## 11. Processing units: from ready files to finite batches

- A **unit scheduler** in the coordinator takes ready, registered files of one
  source in a stable order (ready time, then ledger id) and creates a **sealed**
  `ScanBatch` in the session of at most *N* files, or whatever is ready after a
  trickle timeout, whichever first. Each unit is an ordinary Phase 5 batch
  processed by the unchanged `process_batch` / `parallel_batch`.
- One unit processes at a time. Units are the granularity of pause and crash
  recovery; `recover_interrupted` and resume apply unchanged.
- Pool start-up is not free: keeping one warm pool across units, or sizing
  units to amortise start-up, is measured in Phase E.
- Renamed output copies (`_a`, `_b` suffixes) are not produced per unit in a
  multi-batch session (arrival order is not reproducible, and a superseded
  original would take the plain name); they become an explicit export over the
  **effective set**, reusing `FilenameAllocator`. A single-batch finite session
  keeps today's behaviour.
- **Manual intake converges**: *Add Folder* into a session is a manual source
  reconciled once, through the same hashing, duplicate and registration path.

---

## 12. The scan-quality decision layer

A pure function — evidence in, decision out — with its policy as versioned,
fingerprinted data pinned by the session:

```text
inputs:  recognition outcome, decode failure, ScanQualityAssessment
         (status, issue codes, affected regions), alignment warnings
output:  ACCEPT | ACCEPT_WITH_WARNING | RESCAN_REQUIRED, with reasons
```

| Evidence | Suggested initial decision (unvalidated) |
|---|---|
| Image never decodes | RESCAN_REQUIRED |
| Registration failed | RESCAN_REQUIRED — plus a per-source **rate alarm**, because a wrong template fails every sheet |
| `UNUSABLE` | RESCAN_REQUIRED |
| `REVIEW` | ACCEPT_WITH_WARNING (the existing `scan_quality` conflict remains) |
| `GEOMETRY_NOT_VERIFIED` | ACCEPT_WITH_WARNING |
| Processing error | Neither — retry; a software fault is not a paper fault |

**It feeds the existing Reject & Rescan workflow; it does not replace it.**
RESCAN_REQUIRED produces a *suggested* rejection with a mapped
`RejectionReason` (e.g. `registration`, `folded`, `poor_quality`); a named
operator confirms it through `reject_scan`, as today. No automatic rejection
and no automatic replacement in `0.1.1-alpha.0`. No new geometric threshold is
introduced; `ScanQualityThresholds` stay the evidence thresholds. Defaults are
labelled unvalidated in code, docs and UI until real-data qualification sets
them. **Recognition ambiguity is not physical failure**: an ambiguous roll or
set code is a conflict; answer ambiguity is never a conflict.

---

## 13. Concurrency, the single writer and crash-safe persistence

### 13.1 Concurrency and the single writer

In a live session intake, recording, conflict sync and human review write at
the same time. Requirements (Phase E decides by ADR, tests under contention):

- Workers never open the database. No transaction spans file I/O (listing,
  hashing, copying).
- Either one DB-writer thread/queue shared by intake, the batch recorder and
  conflict sync, or a bounded `busy_timeout` with retry — chosen explicitly.
  `PRAGMA busy_timeout` is set as a safety net in either case.
- The GUI thread never blocks longer than a frame on a write.
- Rollback journal (no WAL, ADR-0002) stands unless an ADR records a reason.
- **One OMRFlow coordinator per project.** Scanner PCs only write image files;
  the project lock keeps it so.

### 13.2 Crash-safe persistence and resume — the requirement

**Required behaviour, not an enhancement.** If OMRFlow is interrupted at any
point — application crash, Python/process crash, forced termination,
operating-system restart, power failure, the operator closing OMRFlow normally,
or closing the project or window — reopening the same project preserves
**all work that had already been durably completed**, in particular in Scan and
Resolve. It must survive **abnormal termination**; saving state during a normal
shutdown is not enough.

- **Scan.** Recognition results are durable incrementally, as sheets complete —
  never only when the batch finishes. Example: 1,000 sheets, 637 committed,
  crash on sheet 638 → after reopening, the 637 results exist and are **not**
  recognised again; sheet 638 is retried safely; the remaining sheets continue
  from the saved state; progress is reconstructed from persisted records.
- **Resolve.** Every completed operator action is durable when confirmed.
  Example: 120 conflicts, 47 resolved, then a close or crash → after reopening,
  the 47 remain, only the rest appear in the active queue, and machine values,
  overrides, effective values and audit history are intact. Navigation position
  need not be preserved; resolved/unresolved state is authoritative from the
  database.
- **Durable-completion invariant.** *Once OMRFlow reports a sheet to the
  operator as successfully recognised/completed, that sheet's recognition
  result and all review/conflict state required for subsequent Resolve
  processing must already be durably committed.* Therefore a sheet never
  appears as Done merely because a worker finished it in memory; Scan progress
  shown as completed represents durable committed state; after a crash, sheets
  already reported completed remain completed; only genuinely in-flight or
  uncommitted sheets may be recognised again.
- **Sessions.** Recovery resumes the existing `ScanSession` and the relevant
  finite `ScanBatch`. It **never creates a new session** because the
  application restarted, and **never creates a superseding batch** — recovery is
  not *Reprocess All*; only an explicit operator request reprocesses.

### 13.3 Transaction boundaries

| Unit of work | Atomic transaction | A unit is "done" only when |
|---|---|---|
| **Recognition of one sheet** — one crash-consistent **work unit** | The sheet's result, status, outcome, readings, timings, attempt count **and the review/conflict state required to interpret it**. Preferably one database transaction. If they cannot literally be one transaction, the unit records which step it reached, and recovery **deterministically detects and finishes** the missing conflict-generation step before the sheet is treated as completed or Resolve-ready (§13.5 S1). Whether sheets commit one at a time or in groups is an implementation choice evaluated in Phase B (§13.8) | The whole unit is durable. Only then may the GUI count it as completed |
| **Content hash / provenance** | Its own short transaction (it is evidence about the file, not about recognition) | Committed; a missing hash is recomputed, never assumed |
| **Manual Resolve correction / acceptance / defer / reopen / undo** | The conflict's new state, the effective value and its `audit_event`, in one transaction — one logical operation, one committed event | The operator's confirmation has committed |
| **Reject & Rescan step** (reject, confirm replacement, undo, purge) | `scan_rejection` change and its `audit_event` together (as today) | Committed |
| **Seal, supersede, close, reopen, combine** | The state change and its audit event together | Committed |
| **Reconciliation / scoring / report generation** | Regenerate-never-patch runs, each committed as a unit (as today); a report file is recorded only after it is completely written | Committed |
| **Batch and session progress** | **Not stored as a counter.** Always derived from committed sheet, conflict and rejection rows | — |

Durability assumes SQLite's default `synchronous=FULL` with the rollback journal
*(verified: the engine sets no `synchronous` or `journal_mode` pragma; the
development environment's SQLite 3.43.1 defaults to `synchronous=2` (FULL) and
`journal_mode=delete`; the packaged build's SQLite is not re-verified here —
Phase G checks it)* and a
storage device that honours flushes. A project on a synchronised or network
folder weakens this (ADR-0002 already warns); the power-failure claim is only
as strong as that storage.

### 13.4 What exists today (audit of `main`, 2026-09-30)

Current `main` already provides **substantial crash-resilient persistence
primitives**: recognised results are checkpointed during scanning, in-progress
sheets return to a retryable state, retries operate on unfinished or failed
sheets, Resolve decisions are transactional with audit events, and conflict
identity prevents ordinary duplicate creation. **However, the complete
Scan/Resolve recovery workflow does NOT yet meet the `0.1.1` requirement**,
because of S1, S2, S3 and R1 (§13.5). The table lists the primitives; it is not
a claim that the requirement is met.

| Behaviour | Status on `main` | Survives hard termination? | Evidence |
|---|---|---|---|
| Sheets registered as `pending` before a run; the run's sheets marked `queued` in one transaction at start | Exists | Yes | `batch_store.create_batch`, `mark_queued` *(verified)* |
| Recognition results checkpointed in groups (25 sheets or 2 s), one transaction per group; a failed write keeps the buffer and is reported | Exists (primitive) | Yes, **up to the last committed group** — but the GUI has already shown the buffered sheets as done, which violates the durable-completion invariant (S3) | `BatchRecorder`, `record_results` *(verified)* |
| On open, `queued/processing → pending`, `running → interrupted`, never to `failed` | Exists | Yes — this is the "was in progress" detection. `PROCESSING` itself is never written, so in-flight sheets are found as `queued` | `recover_interrupted` *(verified)*, defect note in §3 |
| Resume processes only `pending/queued/processing/cancelled`; retry only `failed` | Exists | Yes (state is in the database) | `resumable_scans` *(verified)* |
| Real forced process kills resumed without resubmitting committed sheets, at 100 / 1,000 / 10,000 sheets | Measured | Yes — **headless harness only**, not the GUI path; the 1 / 25 / 50 / 75 / 99 % matrix at 100,000 was never run; power loss never tested | Phase 10 handoff, `evaluation/qualification.py` |
| Normal window close mid-batch: asks, cancels, waits, flushes the recorder, settles stored state | Exists | n/a (graceful only) | `MainWindow.closeEvent`, `ScanPage.shutdown_batch` *(verified)* |
| Resolve decisions (accept, correct, field edit, defer, reopen, undo) each in one transaction with their audit event | Exists | Yes | `review_store` — one `database.session()` per decision *(verified)* |
| Reject & Rescan changes with their audit event in one transaction | Exists | Yes | `scan_lifecycle` *(verified by structure)* |
| Idempotent conflict identity `(batch, scan, type, zone, group)`; unchanged sync writes nothing | Exists | Yes | `review_store.sync_conflicts` *(verified docstring and key)* |

### 13.5 Gaps

**S1, S2, S3 and R1 are required defects to close before `0.1.1-alpha.0` is
released — not observations.** Phase B closes them for the finite workflow;
the release gate (ACCEPTANCE_CRITERIA.md §8) checks them explicitly.

**Scan**

| # | Gap | Only graceful? | Phase |
|---|---|---|---|
| S1 | **Required pre-Alpha defect.** Sheet-local conflicts are generated only after the run finishes, from the in-memory report (`ScanPage._generate_conflicts` iterates `report.processed`). After a crash the committed sheets have results but **no conflicts**, and resume processes only the remainder — so their conflicts are never created *(by code reading, `gui/scan/page.py:1693`; not reproduced)*. The state "recognition saved but required review conflicts not generated" must **never** count as a completed sheet | Graceful close delivers the report only if `finished_report` arrives; hard kill never | **B**: conflict state committed with the result, or a recorded step that recovery deterministically completes from stored results before the sheet is Resolve-ready; **E** keeps it per unit commit |
| S2 | **Required pre-Alpha defect.** After reopening a project nothing re-adopts the interrupted batch. `on_project_changed` clears the batch; `adopt_batch` has no caller on open; *Resume* then reports "Nothing to resume", although the close dialog promises the batch "can be resumed next time this project is opened" *(by code reading; tests call `adopt_batch` directly)* | Both | **B** (re-adopt the active session and its interrupted batch on open) |
| S3 | **Required pre-Alpha defect.** The GUI shows a sheet as done when it is **buffered**, not committed (`_emit_result` buffers, then signals) | Both | **B**: the completed count and every "done" mark reflect committed state only (a buffered tail may be shown as "saving") |
| S4 | Progress on reopen is not reconstructed on the Scan page (no batch adopted); counts exist only via `load_summary` | Both | **B** |
| S5 | `updated_at` is bumped by every flush and by recovery — the defect-2 selector | Both | **B** (downstream stops selecting by it) |
| S6 | Content hash is computed once at run start; a crash before it finishes leaves hashes missing | Both | **C** (hash at registration) |
| S7 | The GUI kill/resume path has never been exercised end to end with a real kill | — | **B** (test), **G** (matrix) |

**Resolve**

| # | Gap | Phase |
|---|---|---|
| R1 | **Required pre-Alpha defect.** After reopening a project Resolve holds no batch (`on_project_changed` clears it); it is loaded only when the Scan page navigates to it *(by code reading)*. Committed decisions are safe, but the operator cannot reach them without first re-adopting the batch in Scan | **B**: Resolve loads the persisted session/batch directly on open, without a visit to Scan; **C** (session queue) |
| R2 | No test proves decisions survive a **hard** kill (only that they commit per action) | **B** |
| R3 | Resolve's in-memory redo list is cleared on project change — acceptable (navigation-like), but must be documented | **B** (docs) |

**Required for the ScanSession architecture**

- Recovery re-adopts the **active session** and its interrupted batch; it
  never creates a session or a batch.
- An interrupted batch keeps its lifecycle state: an interrupted **OPEN** batch
  stays OPEN; an interrupted **SEALED** batch stays SEALED and its unfinished
  processing resumes — sealing freezes membership, it does not mean processing
  finished. Recovery never creates a ScanSession, a ScanBatch or a superseding
  batch.
- **Project reopen restores the workflow context** (Phase B, closing S2 and
  R1): interrupted session and batch state is discovered from persisted data;
  already completed sheets are visible immediately; unfinished work is
  available for Resume; Resolve loads the appropriate persisted session/batch
  directly — the operator never has to visit Scan to make saved Resolve work
  reachable. The previously selected GUI row need not survive; the
  authoritative workflow state must.
- Session and batch counts are derived from committed rows (§14.1), so they are
  correct after any restart without a stored progress value.
- The session-level recovery sequence (Phase E): `recover_interrupted` →
  conflict-sync recovery pass (S1) → reconcile every source (intake) → resume
  processing if it was running.

### 13.6 Idempotence on restart and retry

Restarting or retrying must not create: duplicate sheet rows
(`UNIQUE(batch_id, source_path)`, intake ledger uniqueness, content hash);
duplicate attendance entries or scoring rows (regenerate-never-patch over unique
keys); duplicate conflicts (conflict identity key); double counting at session
level (the effective-set service, §8.1); or duplicate audit events for one
logical operation (each operation commits its event with its change, once; a
recovery pass writes events only for changes it actually makes).

### 13.7 GUI on reopening interrupted work

The Scan stage reconstructs and shows the persisted state **before** any Resume
is pressed, e.g. *637 / 1,000 recognised · 4 failed (retryable) · 359 pending*;
Resolve shows *47 / 120 conflicts resolved* and only the unresolved items in its
active queue. A Resume action is offered where appropriate. No separately
stored progress-bar value is used.

### 13.8 Commit granularity

The acceptance criterion is **durability of operator-visible completed work**,
not a particular transaction batch size. Phase B **measures** per-sheet commits
under the existing single-writer / SQLite (rollback-journal, `synchronous=FULL`)
architecture at realistic throughput and decides:

- if per-sheet durability is practical, prefer it: each sheet's work unit is
  then its own transaction;
- if grouped commits are kept for throughput, the 25-sheet / 2-second buffer
  is an implementation detail only, and the GUI's completed count must reflect
  committed state, never buffered state.

Grouped commits are not required in advance; the measurement and the decision
go into the Phase B handoff.

---

## 14. Session progress, controls and closure

### 14.1 Counts that partition

```text
discovered (excluding ignored)
  = stabilizing + ready + queued + processing
  + accepted + conflict + rescan_required + superseded + duplicate
  + vanished + unreadable_pending_decision
```

The partition is an assertion in tests and in the qualification campaign. A
session snapshot is an immutable value from a bounded number of grouped queries,
pulled by the GUI on a timer (the existing `BatchProgressTracker` pattern),
never pushed. The per-batch tracker stays for the running unit.

### 14.2 Three progress lines, never one percentage

| Line | Numerator / denominator |
|---|---|
| Recognition | processed / (discovered − duplicate − vanished − ignored) — may **decrease** when more files arrive |
| Conflict resolution | resolved / required conflicts on effective scans |
| Rescan | replaced / ever required |

States: *Processing*; **Caught up — watching for new scans** (open, nothing in
flight, every enabled source reconciled within its interval and reachable);
*Waiting for Scanner B (unreachable since 10:42)*; *Processing paused*;
*Closed*. "Caught up" never means "examination complete".

### 14.3 Controls

| Control | Effect |
|---|---|
| Intake on / paused (global, per source) | Paused: no polling or registration; nothing lost |
| Processing running / paused | Paused: no new unit; in-flight sheets finish **and are recorded** |
| Finish current and stop | Replaces today's destructive *Cancel* as the default stop |
| Cancel queued work | Explicit, confirmed; in-flight still recorded |
| **Finish scan session** | Final reconciliation of every source, then closes only if: nothing stabilising, ready, queued or processing; no unresolved required conflict; no outstanding rescan; no unmatched replacement; no held file undecided; every enabled source reachable. Otherwise lists each blocker. On success seals every open batch and moves the session to CLOSED (§6.1). Audited. A temporary absence of files never closes a session. Also reachable as the one-step "Close session and generate final export" (§8.2) |
| Reopen session | Explicit and audited; session editable/open again; results provisional; prior final outputs marked stale (§6.1) |
| Re-close session | Same checks as Finish; audited; final outputs must be regenerated |

---

## 15. Backward compatibility and migrations

### 15.1 Proposed migrations (numbers assigned in merge order; 1–12 taken)

| Proposed # | Phase | Content |
|---|---|---|
| 13 | A | Set identity: `project_set.canonical_code` (+ unique index where not NULL), `project_set.physical_mark` |
| 14 | B | Sessions: `scan_session` table; `scan_batch.scan_session_id` (nullable at DB level), membership state (e.g. `sealed_at`), `role`; a batch-supersession record (reason, by, when, reversal) rather than only a `supersedes_batch_id` column — Phase B chooses; a final-export stale marker if `generated_report` cannot express it (C may own it). The batch's source link is added by D |
| 15 | C | Session-scoped downstream per the Phase C ADR (§8.3), `generated_report` scope |
| 16 | D | Intake: `intake_source`, `intake_file`; `batch_scan.intake_file_id`, `registered_at`; `scan_batch.source_id` |

If a phase needs a further additive change, it takes the next free number at
merge time and says so in its handoff. **Never renumber a merged migration.**

### 15.2 Rules

- Additive and forward-only (ADR-0003); each `ALTER` guarded by
  `PRAGMA table_info` as migrations 4–12 are; table rebuilds (Phase C) follow
  the SQLite rebuild procedure inside the migration's transaction, with
  `foreign_key_check` afterwards.
- The pre-migration backup (`_backup_before_migration_if_needed`) must be
  verified to run before 13–16.
- Upgrade tests from a **real schema-12 project fixture** created by current
  code (the Phase 10 upgrade-test pattern): opens, migrates, backfills sessions,
  and scores identically.
- A schema-16 project is refused by an `0.1.0-alpha.2` build with the existing
  "created with a newer version" message; the upgrade documentation says so and
  recommends the backup.
- Finite single-batch projects: identical results, no new operator step.

---

## 16. Open design questions

Each has a recommendation; the owning phase confirms or changes it by ADR or in
its handoff. Q1, Q4, Q9 and Q10 were decided by the project owner on
2026-09-30.

| # | Question | Recommendation | Phase |
|---|---|---|---|
| Q1 | How are existing batches backfilled into sessions? | **Decided (2026-09-30):** each existing batch becomes its own one-batch session; batches connected by a confirmed rescan/replacement share one migrated session where unambiguous; unrelated batches never combined automatically; explicit, audited, validated *Combine into one session* (§6.4) | B |
| Q2 | Exact trigger that seals a finite-workflow batch | Seal when the operator starts a batch from a different folder, runs *Reprocess All*, or closes the session; test-first against today's Scan-page behaviour | B |
| Q3 | Template change inside a session | Refuse by default; explicit audited acknowledgement as the escape hatch; never silent | B |
| Q4 | Final Export gate for a finite session | **Decided (2026-09-30):** Final Export requires CLOSED; one-step "Close session and generate final export"; reopening marks final outputs stale (§6.1, §8.2) | C |
| Q5 | Session-scoped downstream representation | Evaluate §8.3 (c) first; decide by ADR | C |
| Q6 | Duplicate grouping by identifier or (set, identifier) | Option, default unchanged; office decision | C |
| Q7 | Ingest by copy vs reference in place | Copy for watched sources, reference for manual local imports; ADR | D |
| Q8 | Single writer thread vs `busy_timeout` + retry | Single writer queue, `busy_timeout` as safety net; ADR | E |
| Q9 | 100,000-sheet qualification | **Decided (2026-09-30):** a fresh 100k run is **optional for `0.1.1-alpha.0`** (canonical matrix). Alpha must instead pass **targeted endurance tests** (multiple batches, continuous intake, supersession/reprocessing, restart/resume, session-level aggregation — ACCEPTANCE_CRITERIA.md §5.3). A **fresh 100,000-sheet qualification on the ScanSession / finite-ScanBatch / continuous-processing architecture is a mandatory gate before the first Beta** (ACCEPTANCE_CRITERIA.md §9) | G / Beta |
| Q10 | Beta version line, given F8 | **Decided (2026-09-30):** F8 must be fixed and tested before any Beta release. After the `0.1.1-alpha.x` line the corresponding Beta line is **`v0.1.1-beta.x`**, unless a later deliberate versioning decision changes the target release; `v0.1.0-beta.x` is not used as the successor to `v0.1.1-alpha.0`. The canonical roadmap's Phase 11B target is updated accordingly | 11B / release tooling |
| Q11 | Are sources per project or per session? | Per project, attached to sessions | D |
| Q12 | Recursive sources, multi-page TIFF | Recursive as a per-source option; multi-page TIFF refused with a clear file-level message in `alpha.0` | D |
