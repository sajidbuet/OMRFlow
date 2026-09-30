# Prompt 03 — `0.1.1-C`: Incremental processing, review & rescan

You are working in the OMRFlow repository. This is **phase 0.1.1-C** of the
`0.1.1-alpha.0` line. Phases A and B must be merged
(`PHASE_A_HANDOFF.md`, `PHASE_B_HANDOFF.md` exist); otherwise stop and say so.
Branch: `feat/0.1.1-c-incremental-review-rescan`.

Plan: `development/releases/0.1.1-alpha.0/ARCHITECTURE_NOTES.md` §§8–12 and
§14, `ACCEPTANCE_CRITERIA.md` §2-C. The code is the fact; record discrepancies.

---

## 1. Inspect first

- Phase A and B handoffs, ADRs 0005–0007, `docs/intake.md`
- `services/batch_store.py` (`create_batch`, `mark_queued`, `record_results`,
  `BatchRecorder`, `finalise_batch`, `recover_interrupted`, `resumable_scans`,
  `check_compatibility`, `mark_for_reprocessing`)
- `services/batch_processor.py`, `services/parallel_batch.py`,
  `services/batch_progress.py`
- `services/conflict_policy.py` (`detect_conflicts`,
  `detect_duplicate_identifiers`, `ConflictPolicy`), `services/review_store.py`
  (`sync_conflicts`, `sync_duplicate_identifiers`, `_resolution_only`,
  `provenance_for`, `effective_identifiers`), `domain/review.py`
- `domain/scan_quality.py`, `services/scan_quality.py`, `docs/scan_quality.md`
- `gui/scan/page.py` `_generate_conflicts` and `gui/scan/worker.py`
  `BatchWorker` — the current post-batch conflict flow you are generalising
- `evaluation/qualification.py` (the submission-log technique for measuring
  no-resubmission)
- tests: `test_batch_store.py`, `test_conflict_policy.py`,
  `test_review_store.py`, `integration/test_conflict_review.py`,
  `test_batch_persistence.py`, `test_reprocessing.py`,
  `test_stress_kill_resume.py`, `test_scan_quality*.py`

Run the full suite first; record the baseline.

## 2. Preserve

Everything in prompt 01 §2, and specifically:

- A processing unit **is** a Phase-5 `ScanBatch`: fixed membership, resume,
  retry, recovery, fingerprint check. Do not add scans to an existing batch.
- `process_batch`/`parallel_batch` unchanged in contract; workers never write.
- `sync_conflicts` idempotence and its withdraw-unless-human-touched rule.
- **Conflict policy meaning**: identifier and set-code ambiguity are
  conflicts; **answer ambiguity never is**. No new thresholds in
  `conflict_policy`.
- `ScanQualityThresholds` unchanged; no fold/displacement cut-off introduced.
- Finite batches outside a session: per-batch duplicate IDs exactly as today.
- Nothing is deleted to express a decision.

## 3. Scope

1. **Unit scheduler**: takes READY assets in stable order (ready time, then
   asset id), creates a `ScanBatch` linked to the session with at most *N*
   assets or whatever is ready after a trickle timeout; submits it through the
   existing pipeline; one unit at a time. Pinned session template and
   fingerprints per ADR-0007. Measure pool start-up and choose unit size /
   whether to keep a warm pool; record the numbers.
2. **Write `ScanJobStatus.PROCESSING`** when a sheet enters a worker (today it
   is never written), batched like other writes; recovery unchanged.
3. **Sheet-local conflict sync after each unit commit**, headless (move the
   logic now in `ScanPage._generate_conflicts` into a service the GUI and the
   session runner both call; the finite GUI path must behave identically).
4. **Session-scoped cross-sheet conflicts**: duplicate identifiers grouped
   over the session's **effective, reliable** identifiers across units;
   member rows keep their own `batch_id`/`scan_id`; `related_scan_ids` span
   units; detail text says "session". Re-evaluated incrementally after each
   commit **and** after each review decision that changes an effective
   identifier; bounded (indexed, only affected groups). Superseded and
   duplicate-content assets excluded. Test the 10:03/10:47 scenario and the
   dissolve-on-correction scenario (ARCHITECTURE_NOTES.md §11).
5. **(set, identifier) grouping** as a `ConflictPolicy` option, default
   unchanged, documented as an examination-office decision.
6. **Quality decision layer** (§10.2): pure function, versioned and
   fingerprinted policy pinned by the session, outputs ACCEPT /
   ACCEPT_WITH_WARNING / RESCAN_REQUIRED with reasons. Starting mapping from
   the table in §10.2, labelled **unvalidated** in code, docs and UI text.
   Undecodable (from B's UNREADABLE) feeds the same decision.
7. **Operational outcome projection** (§10.1): ACCEPTED / CONFLICT /
   RESCAN_REQUIRED / SUPERSEDED / DUPLICATE / in-flight, derived from stored
   facts. Any cache must have a recompute-equals-cache test.
8. **Rescan and replacement** (§12): rescan items; replacement suggestions
   (reliable identity match against an open item's partial identity; or
   "replacement mode" on a chosen source); **confirmation by a named
   operator**; on confirmation and once the replacement is recognised and not
   itself RESCAN_REQUIRED: original SUPERSEDED, replacement EFFECTIVE; undo
   (reopen) restores; chains; unmatched replacements listed. Every step is an
   `audit_event`. The original's conflicts leave the active queue by an
   exclusion rule, not deletion.
9. **Session snapshot service** (§14): immutable value from a bounded number
   of grouped queries; counts partition; the three progress lines; the
   "caught up" predicate; per-source counts and a rate alarm.
10. **Concurrency** per ADR-0006: intake, unit recording, conflict sync and a
    review decision writing concurrently without `database is locked`
    failures; bounded GUI-thread write latency (tested headlessly with a
    simulated GUI caller).
11. **Pause / resume processing and finish-session validation** as
    services (the buttons come in D): pause stops new units after the running
    one; finish runs a final reconciliation then returns the list of blockers
    (ACCEPTANCE_CRITERIA.md §3) or closes, audited.

## 4. Out of scope

- GUI (D). Session-scoped reconciliation/scoring/reporting (E).
- Automatic, unconfirmed replacement.
- Changing recognition or scan-quality measurement.
- Changing the Phase 10 harness.

## 5. Persistence expectations

- Prefer representing new facts with Phase A's schema. Any addition is an
  additive migration with an upgrade test.
- A forced kill at any point — mid-unit, mid-conflict-sync, mid-supersession —
  leaves a consistent state; recovery on open completes or rolls back each
  operation atomically. Test with real process kills, as Phase 10 does, at
  small scale.
- Supersession and its undo are single transactions with their audit events.

## 6. Tests required

- Unit: scheduler ordering and sizing; quality decision mapping (every row,
  plus not-evaluated and not-verified cases); outcome projection;
  replacement suggestion rules; snapshot partition; finish-blocker list.
- Integration: a session with three sources producing ~500 synthetic sheets
  (existing generator, including folds for RESCAN_REQUIRED) → units →
  conflicts appear per unit; late duplicate creates conflicts on both sheets;
  correction dissolves; replacement from a *different* source supersedes;
  undo; chain; restart at each stage; kill mid-unit and verify
  no-resubmission with a submission log.
- Contention test for concurrent writes (item 10).
- Regression: finite-batch conflict flow in the GUI identical
  (`tests/gui/test_scan_page.py`, `test_resolve_page.py` unchanged and
  passing); whole suite unchanged; Phase 10 self-test passes.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q -m stress -k "intake or session"
```

GUI tests: the existing Scan/Resolve GUI tests must pass unchanged after the
conflict-sync refactor (item 3); add a GUI test proving the finite path still
syncs conflicts after a run.

## 8. Documentation updates

- `docs/conflict_review.md`: session-scoped cross-sheet conflicts; the
  (set, identifier) option.
- `docs/scan_quality.md`: the decision layer above the evidence, and that its
  defaults are unvalidated.
- New `docs/rescan.md`: rescan lifecycle, suggestion, confirmation,
  supersession, undo, audit.
- `docs/intake.md`: units, pause, finish validation.
- `docs/DATA_MODEL.md`, `docs/ARCHITECTURE.md` as touched.
- `PHASE_C_HANDOFF.md`, ROADMAP.md §5, `CURRENT_STATE.md`,
  `docs/wiki/Development-Roadmap.md`, `CHANGELOG.md`.
- **README Development status / Testing status**: `0.1.1-C` row with phases
  completed / under testing, automated, synthetic, network-share, real-data
  status and pending work.

## 9. Status reporting

Report separately: implementation complete; automated tests complete;
synthetic validation (the ~500-sheet integration run is **not** the §4
campaign — say so); network-share validation not performed; real scanner
validation not performed; production qualification not performed. State that
the quality-decision defaults are unvalidated. Never call the phase complete
on unit tests alone.

Commit in small commits, push the branch, do not merge unless asked.
