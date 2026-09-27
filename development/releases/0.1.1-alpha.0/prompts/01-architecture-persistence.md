# Prompt 01 — `0.1.1-A`: Architecture & persistence

You are working in the OMRFlow repository. This is **phase 0.1.1-A** of the
`0.1.1-alpha.0` development line: live, multi-scanner intake. Work on a new
branch `feat/0.1.1-a-architecture-persistence` from the current `main`.

The plan you are implementing is in `development/releases/0.1.1-alpha.0/`:
`ROADMAP.md`, `ARCHITECTURE_NOTES.md`, `ACCEPTANCE_CRITERIA.md`. They were
written against commit `2d18ccb`; **the code may have moved since.** Where the
code and the plan disagree, the code is the fact and the plan is the intent —
record the discrepancy in your handoff and adapt.

---

## 1. Inspect first — before writing any code

Read, at minimum:

- `README.md` (Development status, Testing status), `docs/wiki/Development-Roadmap.md`,
  `development/ROADMAP.md`, `development/CURRENT_STATE.md`
- `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md`, `docs/decisions/*.md`
  (especially ADR-0002 and ADR-0003)
- `docs/scan_workflow.md` §12, `docs/conflict_review.md`, `docs/scan_quality.md`,
  `docs/reconciliation.md`, `docs/phase10_qualification.md`
- `development/PHASE_05_HANDOFF.md`, `PHASE_06_HANDOFF.md`, `PHASE_07_HANDOFF.md`,
  `PHASE_10_HANDOFF.md`
- `src/omr_scanner/_version.py`
- `src/omr_scanner/database/models.py`, `database/migrations.py`, `database/engine.py`
- `src/omr_scanner/services/batch_store.py`, `batch_processor.py`,
  `parallel_batch.py`, `scan_provenance.py`, `review_store.py`,
  `conflict_policy.py`, `reconciliation_store.py`, `project_lock.py`,
  `project_service.py`, `project_health.py`
- `src/omr_scanner/gui/scan/page.py` (`_ensure_batch`, `add_scan_paths`,
  `_start_batch`, `_generate_conflicts`)
- `tests/unit/test_architecture.py`, `test_batch_store.py`, `test_review_store.py`,
  `test_scan_provenance.py`, `test_version.py`;
  `tests/integration/test_database.py`, `test_batch_persistence.py`,
  `test_production_hardening.py`, `test_reprocessing.py`

Also run `git status`, `git log --oneline -10`, and the full test suite once
**before changing anything**, and record the baseline counts.

Then re-verify every row of `ARCHITECTURE_NOTES.md` §2 against the code. List
any that are no longer true at the top of your handoff.

## 2. Existing architecture you must preserve

- Phase 5 `ScanBatch` semantics: fixed membership, `UNIQUE(batch_id,
  source_path)`, `pending → … → terminal` states, resume in `batch_index`
  order, `recover_interrupted` (never to `failed`), fingerprint compatibility
  checks, grouped commits (25 sheets / 2 s).
- Workers never touch the database; only the coordinator writes. SQLite
  rollback journal (no WAL) unless you record a reason in an ADR.
- Phase 6: conflict identity key, idempotent `sync_conflicts`, the projected
  (not stored) effective value, append-only `audit_event` with no foreign key
  and its triggers, and the rule that **answer ambiguity is never a conflict**.
- Phase 7–9 per-`(roster_id, batch_id)` behaviour.
- Phase 10: SHA-256 provenance, project lock (never auto-removed), read-only
  mode, `batch_scan_history` append-only, backups, health check, and the
  100,000-sheet harness with its 15 assertions — **unchanged**.
- Forward-only, additive, hand-written migrations (ADR-0003).
- Layering: no Qt below `gui`; no OpenCV/NumPy/SQLAlchemy in `gui`
  (`tests/unit/test_architecture.py`).
- The finite Scan-page workflow must remain the default and behave exactly as
  before.

## 3. Scope — exactly this

1. **Version bump, first commit, alone.** Change `__version__` in
   `src/omr_scanner/_version.py` from `0.1.0-alpha.2` to `0.1.1-alpha.0`.
   Nowhere else defines the version; do not add a second constant. Update the
   version tests if they pin the literal; add an `[Unreleased]` entry to
   `CHANGELOG.md` saying the 0.1.1 line has begun. Do **not** tag, build or
   release. Commit: `chore: begin the 0.1.1-alpha.0 development line`.
2. **ADRs** under `docs/decisions/`:
   - ADR-0005: ingest-by-copy vs reference-in-place for session assets
     (ARCHITECTURE_NOTES.md §5.2) — decide, with disk-space and
     synchronised-folder consequences.
   - ADR-0006: concurrent-writer strategy (§9) — single writer thread vs
     `busy_timeout` + retry; decide and justify.
   - ADR-0007: template/threshold change within an open session (§8).
   Keep ADR numbering consistent with whatever exists by then.
3. **Domain vocabulary** (Qt-free, SQLAlchemy-free, in `domain/`): intake
   session lifecycle (`open`, `closing`, `closed`, reopen), source kind
   (`watched`, `manual`), source reachability, asset intake states
   (ARCHITECTURE_NOTES.md §6.1), asset relationships (duplicate-of,
   replaces/superseded-by), operational outcome enum (§10.1) — as enums with
   their rules as properties, in the style of `domain/review.py`. Name the
   container `IntakeSession` (or better) in code, not "Session", to avoid
   `ProjectSession`/ORM collisions.
4. **Persistence — migration 10**, additive only: the session, source and asset
   representation; an optional session link on `scan_batch`; an optional asset
   link on `batch_scan`; indexes for the queries later phases need
   (asset by `(source, relative_path)`, by content hash, by state; batch by
   session). Choose columns vs link tables after inspection and justify in
   `DATA_MODEL.md`. **Specify semantics first** (ARCHITECTURE_NOTES.md §5);
   don't copy the illustrative names blindly.
5. **Repository service** (e.g. `services/intake_store.py`), Qt-free:
   create/open/close/reopen a session; add/edit/enable/disable a source;
   register an asset; transition an asset's state with a reason; record
   duplicate-of; record and undo a supersession; link an asset to a
   `BatchScan`; list/count by state with bounded, grouped SQL. Every
   operator-meaningful transition (session open/close/reopen, source changes,
   supersession, undo) writes an `audit_event` in the same transaction.
6. **Defect found while planning** (ARCHITECTURE_NOTES.md §17, first row):
   write a test that shows whether scans added to the Scan list after a batch
   was registered are persisted. If the defect is real, fix it minimally in
   the finite workflow, or mark the test `xfail(strict=True)` with a reason and
   defer it explicitly. Do not change unrelated behaviour.
7. Docstring-only corrections noted in §17 for `database/models.py`.

## 4. Out of scope — do not do

- No watcher, poller, stabilisation, hashing pipeline or unit scheduler (B, C).
- No GUI beyond what the version bump shows automatically.
- No change to recognition, alignment, scan-quality thresholds, conflict
  policy, reconciliation, scoring or reporting behaviour.
- No change to the Phase 10 harness or its assertions.
- No release, tag, installer or packaging change.

## 5. Persistence and migration expectations

- Migration 10 is additive: new tables, nullable columns; no existing row is
  rewritten; guard any `ALTER` with `PRAGMA table_info` as the existing
  migrations do.
- Upgrade test: a committed **schema-9 project fixture** (created by the
  current code, not hand-written SQL) upgrades to 10, and every existing
  query used by Scan, Resolve, Attendance, Results and Reports returns
  identical results before and after.
- A schema-10 database is refused by a schema-9 build (existing behaviour);
  document that in `docs/wiki/Upgrade-Compatibility.md`.
- `project_health.full_check` still passes on upgraded and fresh projects.

## 6. Tests required

- Unit: domain enums and their rules; repository operations and every
  transition, including illegal ones refused; audit events written atomically
  with the change; bounded statement counts for list/count queries (count
  statements, as Phase 6 does).
- Integration: migration 9 → 10 on the fixture; round-trip of a session with
  three sources, same filenames, duplicate and supersession relationships,
  surviving close/reopen of the project; read-only mode refuses writes.
- Regression: the **entire existing suite passes unchanged**; finite batches
  have `session = NULL` and behave identically.

## 7. Verification — run and report output

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
```

No GUI tests are expected to change in this phase; run the GUI suite anyway
(it is part of `pytest -q`) and report it.

## 8. Documentation updates

- `docs/DATA_MODEL.md`: new entities, relationships, schema version 10.
- `docs/ARCHITECTURE.md`: a short "Intake sessions (0.1.1)" section linking
  the ADRs.
- `development/releases/0.1.1-alpha.0/ROADMAP.md` §5 status table.
- `development/releases/0.1.1-alpha.0/PHASE_A_HANDOFF.md` (new): what was
  built, decisions, discrepancies from the plan, test counts, what is next.
- `development/CURRENT_STATE.md`: version, date, new capability, limitations.
- `docs/wiki/Development-Roadmap.md`: the `0.1.1` entry's phase A status.
- **README `Development status` and `Testing status`**: add a `0.1.1-A` row;
  state phases completed, under testing, automated-test status, synthetic-
  testing status (n/a for A), real-data status, and pending phases.
- `CHANGELOG.md` `[Unreleased]`.

## 9. How to report status — do not overclaim

Report each track separately (ROADMAP.md §5): implementation complete;
automated tests complete; synthetic validation (n/a for A); network-share
validation (n/a); real scanner validation (n/a); production qualification
(n/a). Use the repository's vocabulary: *implemented*, *automated tests
passing*, *synthetically validated*, *real-scan validated*, *operationally
qualified*, *released* — never as synonyms. Phase A may be reported as
"Implemented; automated tests passing". **Passing tests alone never make a
phase "Complete"** in the canonical sense.

Commit in small, reviewable commits. Push the branch. Do not merge to `main`
unless the user asks.
