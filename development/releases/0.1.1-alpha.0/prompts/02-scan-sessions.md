# Prompt 02 — `0.1.1-B`: Scan sessions and finite batches

You are working in the OMRFlow repository. This is **phase 0.1.1-B** of the
`0.1.1-alpha.0` line. Phase A must be merged
(`development/releases/0.1.1-alpha.0/PHASE_A_HANDOFF.md` exists and
`__version__` is `0.1.1-alpha.0`); otherwise stop and say so. Branch:
`feat/0.1.1-b-scan-sessions` from the current `main`.

Plan: `ROADMAP.md` §2 and §5 B; `ARCHITECTURE_NOTES.md` §§1, 2.2, 3 (defects
1, 2, 6), 4 (F1, F2, F3, F11), 5 (especially 5.2 and 5.5), 6, 13.2–13.7, 15,
16 (Q1–Q3); `ACCEPTANCE_CRITERIA.md` §1, §2 B, §3 and §5.4. The code is the
fact; record discrepancies.

**Lifecycle invariants** (semantic; use the project's terminology for the
eventual names): a batch that is OPEN may receive members, SEALED may not,
SUPERSEDED remains auditable but is excluded from effective aggregation; a
session that is OPEN may receive new finite batches, CLOSED may not receive
batches or intake (all its batches sealed), and an explicitly REOPENED session
is open again and invalidates prior final-export status.

**Supersession invariant:** only effective, non-superseded batch/sheet
membership contributes to session-level attendance, reconciliation, scoring,
results and final reports; superseded batches remain retained for provenance
and audit. Nothing is deleted because it is superseded.

**Crash safety is required, not optional:** after a crash, forced kill, OS
restart, power failure or normal close, reopening the project preserves every
committed Scan and Resolve unit of work (ARCHITECTURE_NOTES.md §13.2).

**The model you are building:** `Project → ScanSession → one or more finite
ScanBatch objects → sheets`. `ScanBatch` = finite processing/provenance unit;
`ScanSession` = operational/examination-level aggregation unit; `Project` =
persistent container. A batch is never an indefinitely growing container.

---

## 1. Inspect first

- Phase A handoff; `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md`,
  `docs/decisions/*.md`, `docs/scan_workflow.md`
- `database/models.py` (`ScanBatch`, `BatchScan`, `ProjectSetting`,
  `SettingKey`, `ProcessingManifest`, `ScanRejection`),
  `database/migrations.py`, `database/engine.py`
- `services/batch_store.py` — especially `create_batch`, `add_scans_to_batch`
  (and its docstring on why rescans extend the original's batch),
  `recover_interrupted`, `list_batches`, `finalise_batch`, `check_compatibility`,
  `mark_for_reprocessing`
- `services/scan_lifecycle.py` — `adopted_replacements`, `counted_elsewhere`,
  `confirm_replacement`, `sync_reimports`
- `services/project_service.py` (`ProjectSession`, backup before migration)
- `gui/scan/page.py` (`on_project_changed`, `_ensure_batch`, `process_all`,
  `reprocess_all`, `resume_batch`, `retry_failed`), `gui/scan/worker.py`,
  `gui/main_window.py` (where `recover_interrupted` runs)
- how Attendance, Results, Reports and Resolve choose a batch
  (`gui/attendance/page.py`, `gui/results/page.py`, `gui/reports/page.py`,
  `gui/review/page.py`)
- `evaluation/stress_runner.py`, `evaluation/qualification.py` (they create
  batches directly; they must keep working)
- `gui/scan/page.py` `_generate_conflicts` (conflicts built from the in-memory
  report after a run — gap S1), `adopt_batch` (no caller on project open — gap
  S2), `shutdown_batch`; `gui/scan/worker.py` `_emit_result` (buffered sheets
  shown as done — gap S3); `gui/review/page.py` `on_project_changed`,
  `load_batch` (gap R1); `gui/main_window.py` `closeEvent`,
  `_recover_interrupted_batches`
- tests: `test_batch_store.py`, `test_reject_rescan_rules.py`, `integration/test_reject_and_rescan*.py`, `tests/gui/test_reject_rescan_gui.py`,
  `integration/test_batch_persistence.py`, `test_reprocessing.py`,
  `tests/gui/test_scan_page.py`, `test_scan_persistence.py`

Run the full suite first; record the baseline.

## 2. Preserve

- Phase 5 batch semantics: states, resume in `batch_index` order,
  `recover_interrupted` (never to `failed`), compatibility checks, grouped
  commits, `UNIQUE(batch_id, source_path)`.
- Reject & Rescan exactly as it behaves today, including cross-batch
  replacements counted once — now expressed through sessions.
- The finite workflow: *Add Folder → Process All → Resolve → Attendance →
  Answer Key → Results → Reports* with **no new operator step and no new
  question**.
- The Phase 10 harness and stress runner (which create batches directly).
- Everything in `prompts/README.md` "Rules every prompt repeats".

## 3. Scope — exactly this

1. **ADR** (next free number after ADR-0004) recording the three-level model,
   sealing, batch roles, implicit sessions, the active-session pointer,
   template pinning (ARCHITECTURE_NOTES.md §16 Q3) and the legacy backfill rule
   (Q1).
2. **Domain vocabulary** (Qt-free, SQLAlchemy-free, in `domain/`): session
   state (`open`, `closed`, reopen as a transition), batch role (`scan`,
   `rescan`, `reprocess`, `legacy`), sealing — enums with their rules as
   properties, in the style of `domain/review.py`. Name the entity
   `ScanSession`; **never** name a variable holding one `session` (that means
   the ORM session here); use `scan_session`.
3. **Migration** (next free number — 14 if A took 13): `scan_session` table
   (id, name, state, pinned template identity and fingerprints, created/closed/
   reopened times and by whom, notes); `scan_batch.scan_session_id` (nullable
   at DB level), membership state (OPEN / SEALED, e.g. `sealed_at`), `role`;
   a **first-class batch-supersession record** (superseding and superseded
   batch, reason, by whom, when, audited reversal) — not only a
   `supersedes_batch_id` column. Indexes for batch by session.
4. **`services/scan_sessions.py`** (Qt-free): create, rename, close, reopen;
   the active-session pointer in `project_setting` (new `SettingKey`);
   implicit creation on the first *Process All*; seal a batch; record and
   reverse a supersession (refusing cycles, double supersession and
   cross-session supersession); list a session's batches; every transition
   writes an `audit_event` in the same transaction.
   - **Close** runs the closure checks (ACCEPTANCE_CRITERIA.md §3), seals every
     open batch, and moves the session to CLOSED; a CLOSED session refuses new
     batches and intake.
   - **Reopen** is explicit and audited, returns the session to OPEN, and marks
     every final output generated while closed as **stale** (the stale marker
     may live on `generated_report` when phase C adds its scope — record where);
     re-close is audited.
   - **Combine into one session**: batches/sessions of the **same project**
     only; explicit and operator-initiated, never automatic; audited; refused,
     with reasons, if it would introduce a duplicate effective sheet or a
     contradictory supersession relationship; refused into a CLOSED session.
5. **Upgrade backfill** — the step that runs on the first read-write open
   after the migration (migrations change structure, not data):
   ARCHITECTURE_NOTES.md §6.4, **decided** — each existing batch becomes its
   own one-batch session; batches connected by a confirmed rescan/replacement
   share one migrated session **where the relationship is unambiguous** (define
   "unambiguous" precisely, test-first); ambiguous cases stay separate with
   results unchanged and are listed in an upgrade report; unrelated historical
   batches are **never** combined automatically; audited; read-only mode
   backfills nothing and presents a virtual one-batch session.
6. **Finite batches**: `add_scans_to_batch` refuses a sealed batch; decide and
   implement the finite-mode seal trigger (Q2) **test-first against today's
   Scan-page behaviour**; a rescan for a sheet in a sealed batch goes into a new
   `rescan` batch of the same session.
7. ***Reprocess All*** creates a `reprocess` batch that supersedes the batch it
   re-reads through the supersession record; the superseded batch stays
   inspectable and nothing is deleted. Design the record so the same mechanism
   serves future algorithmic reprocessing and whole-batch rescans
   (ARCHITECTURE_NOTES.md §5.5).
8. **Scan page**: re-adopt the active session on project open; a later
   *Process All* creates a new batch in the same session (defect 1's cause); the
   session name in the Scan stage header; a minimal *Scan session* menu (new,
   rename, close, reopen, combine). Nothing else visible.
9. **Downstream, until phase C lands**: Attendance, Results and Reports stop
   using `list_batches(limit=1)` / `updated_at`; they read the active session's
   most recent **sealed or completed** batch explicitly and say so in their
   header (defect 2's cause). Phase C replaces this with session aggregation.
10. **`processing_manifest`** written at seal and run boundaries (defect 6).
11. **Template pinning**: a batch whose identity does not match the session's
    pinned identity follows the ADR (refuse by default; audited
    acknowledgement).
12. **Crash-safe persistence and resume — close the required pre-Alpha
    defects S1, S2, S3 and R1** (ARCHITECTURE_NOTES.md §13.2–13.8). Current
    `main` has persistence primitives, **not** a complete recovery workflow; do
    not assume the existing Resume satisfies the requirement. Before changing
    anything, write failing tests that reproduce S1–S3 and R1 with **real
    process kills** (or record, with evidence, that one is not reproducible).
    The invariant: *once OMRFlow reports a sheet as successfully
    recognised/completed, its recognition result and all review/conflict state
    required for Resolve are already durably committed.* Then:
    - **S1** — make recognition and its required conflict state one
      crash-consistent work unit: one transaction if practical; otherwise
      record the step reached, and have recovery deterministically detect and
      finish the missing conflict generation, idempotently and from stored
      results, before the sheet counts as completed or Resolve-ready;
    - **S3** — the completed count and every "done" mark reflect committed
      state only;
    - **S2** — on project open, after `recover_interrupted`, discover
      interrupted session/batch state from persisted data; completed sheets
      visible immediately, e.g. *637 / 1,000 recognised · 4 failed · 359
      pending*; unfinished work available for Resume; counts reconstructed from
      committed rows, never from a stored progress value;
    - **R1** — Resolve loads the persisted session/batch directly on open
      (e.g. *47 / 120 conflicts resolved*, only unresolved items in the active
      queue), without the operator visiting Scan; the selected row need not
      survive;
    - recovery preserves lifecycle state — an interrupted OPEN batch stays
      OPEN, an interrupted SEALED batch stays SEALED and resumes its unfinished
      processing — and never creates a session, a batch or a superseding batch,
      nor duplicates sheet, conflict, audit, attendance or scoring rows.
    - **Commit granularity** — measure per-sheet commits with the existing
      single-writer SQLite setup at realistic throughput; prefer per-sheet
      durability if practical; if grouped commits stay, they are an
      implementation detail and the GUI must count only committed sheets.
      Record the numbers and the decision in the handoff.

## 4. Out of scope

- Session-level reconciliation, scoring, reporting, Resolve queue (phase C).
- Watched folders, intake ledger (D); continuous processing (E); the session
  panel and per-source UI (F).
- Any change to recognition, conflict policy, scoring rules or report layout.

## 5. Persistence expectations

- Additive migration with upgrade tests from committed schema-12 fixtures:
  (a) one batch; (b) two batches of one examination (defect 1); (c) two
  batches linked by a confirmed cross-batch replacement. After upgrade and
  backfill every existing query returns what it returned before.
- `project_health.full_check` gains the invariant "every batch belongs to a
  session; a sealed batch's `total_scans` equals its row count".

## 6. Tests required

- Unit: domain rules; session transitions incl. illegal ones; sealing refusal;
  reprocess supersession; backfill rules; audit events atomic with changes;
  bounded statement counts for list queries.
- Integration: reopen-and-process lands in the same session; retrying an old
  batch and `recover_interrupted` do not change what downstream reads; rescan
  into a new `rescan` batch counted once; the backfill fixtures incl. an
  ambiguous case; close / reopen / re-close with stale final-output status;
  combine refused across projects, on duplicate effective sheets and on
  contradictory supersession; supersession cycles refused; manifests written;
  read-only mode.
- **Crash safety** — ACCEPTANCE_CRITERIA.md §5.4 cases 1–10 and 12–15 for the
  finite workflow, including case 13 (kill after recognition persistence but
  before conflict generation) (case 11 at small scale, e.g. 1,000 sheets at ≈ 1/25/50/75/99 %),
  with real process termination for every "forced" case and evidence from
  outside the killed process.
- GUI: finite workflow unchanged (`test_scan_page.py`,
  `test_scan_persistence.py` unchanged and passing); session name shown;
  session menu actions.
- Phase 10 harness self-test passes unchanged. Whole suite unchanged.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
```

## 8. Documentation updates

`docs/DATA_MODEL.md`; `docs/ARCHITECTURE.md` (a "Scan sessions" section linking
the ADR); `docs/scan_workflow.md` (sessions, sealing, reprocess); the new ADR;
`docs/wiki/Scanning.md`, `Processing.md`, `Upgrade-Compatibility.md`;
`ROADMAP.md` §7; new `PHASE_B_HANDOFF.md` (including which downstream stages
still read one batch until C); `CURRENT_STATE.md`; README Development/Testing
status; `docs/wiki/Development-Roadmap.md` `0.1.1` entry; `CHANGELOG.md`.

## 9. Status reporting

Separately: implemented; tested; synthetic validation n/a; network share n/a;
real scanners not performed; production not performed. State plainly that
downstream stages **do not yet aggregate a multi-batch session** — that is
phase C — so a multi-batch session is not yet safe to score. Report the crash
safety cases individually (which passed, at what scale, with real kills) and
list any gap from ARCHITECTURE_NOTES.md §13.5 that remains open. Power-failure
behaviour is inferred from SQLite durability, not tested — say so.

Commit in small commits, push the branch, do not merge unless asked.
