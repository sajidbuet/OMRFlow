# Prompt 05 — `0.1.1-E`: Continuous processing, quality decisions and session controls

You are working in the OMRFlow repository. This is **phase 0.1.1-E** of the
`0.1.1-alpha.0` line. Phases A–D must be merged (their handoffs exist);
otherwise stop and say so. Branch: `feat/0.1.1-e-continuous-processing`.

Plan: `ROADMAP.md` §5 E; `ARCHITECTURE_NOTES.md` §§2.2, 2.3, 5.2, 11–14, 16
(Q8); `ACCEPTANCE_CRITERIA.md` §2 E, §3. The code is the fact; record
discrepancies.

Processing runs while files arrive, **always through finite, sealed
`ScanBatch` units** in the session — never by growing a batch.

---

## 1. Inspect first

- Phase A–D handoffs and ADRs; `docs/intake.md`; `services/intake.py`,
  `services/scan_sessions.py`, the effective-set service from C
- `services/batch_store.py` (`create_batch`, `mark_queued`, `record_results`,
  `BatchRecorder`, `finalise_batch`, `recover_interrupted`, `resumable_scans`,
  `check_compatibility`), `services/batch_processor.py`,
  `services/parallel_batch.py`, `services/batch_progress.py`
- `gui/scan/page.py` `_generate_conflicts`, `gui/scan/worker.py`
  `BatchWorker` — the post-batch conflict flow you are moving into a service
- `services/review_store.py`, `services/conflict_policy.py`, `domain/review.py`
- `domain/scan_quality.py`, `services/scan_quality.py`, `docs/scan_quality.md`
- `domain/scan_lifecycle.py` (`RejectionReason`), `services/scan_lifecycle.py`
  (`reject_scan`, `possible_rescans`, `replacement_candidates`,
  `confirm_replacement`, `sync_reimports`)
- `database/engine.py`
- `evaluation/qualification.py` (submission-log technique for measuring
  no-resubmission)
- tests: batch store, conflict policy, review store, Reject & Rescan (`test_reject_rescan_rules.py`, `integration/test_reject_and_rescan*.py`), scan
  quality; `integration/test_batch_persistence.py`, `test_reprocessing.py`,
  `test_stress_kill_resume.py`; `tests/gui/test_scan_page.py`,
  `test_resolve_page.py`

Run the full suite first; record the baseline.

## 2. Preserve

- A processing unit **is** a Phase 5 `ScanBatch`, sealed at creation;
  `process_batch` / `parallel_batch` unchanged in contract; workers never write.
- `sync_conflicts` idempotence and its withdraw-unless-human-touched rule;
  conflict policy meaning (identifier and set-code ambiguity are conflicts;
  answer ambiguity never is).
- `ScanQualityThresholds` unchanged; no fold/displacement cut-off.
- Reject & Rescan: rejection and replacement stay **operator decisions**.
- The finite GUI conflict flow after a run behaves identically.
- Everything in `prompts/README.md` "Rules every prompt repeats".

## 3. Scope — exactly this

1. **ADR** (next free number) on the writer strategy (Q8): single DB-writer
   queue shared by intake, the recorder and conflict sync, or bounded
   `busy_timeout` + retry. Set `PRAGMA busy_timeout` as a safety net either way.
2. **Unit scheduler** (§11): ready, registered files of **one source**, stable
   order, at most *N* or whatever is ready after a trickle timeout → a new
   sealed batch in the session → the unchanged pipeline; one unit at a time;
   session template pinning enforced; measure pool start-up and choose unit
   size / warm pool, with numbers in the handoff.
3. **`ScanJobStatus.PROCESSING`** written when a sheet enters a worker, batched;
   recovery unchanged.
4. **Headless conflict sync** after each unit commit: move the logic of
   `ScanPage._generate_conflicts` into a service both the GUI and the runner
   call; session-wide duplicate sync from C runs incrementally.
5. **Quality decision layer** (§12): pure, versioned, fingerprinted policy pinned
   by the session → ACCEPT / ACCEPT_WITH_WARNING / RESCAN_REQUIRED with
   reasons; starting mapping from §12, labelled **unvalidated** in code, docs
   and UI text; `unreadable` intake files feed it. RESCAN_REQUIRED creates a
   **suggested rejection** with a mapped `RejectionReason`, confirmed by a named
   operator through `reject_scan`. Nothing is rejected automatically.
6. **Rescan suggestions** by source and arrival time (extend
   `replacement_candidates` / `possible_rescans`); re-import detection at
   registration; files for a **closed** session held for the operator.
7. **Controls as services** (the buttons come in F; §14.3): intake on/paused
   (global, per source), processing running/paused (in-flight sheets finish and
   are recorded), finish-current-and-stop as the default stop, cancel-queued;
   pause flags persisted.
8. **Session snapshot** (§14.1–14.2): immutable value from bounded grouped
   queries; partitioning counts; three progress lines; caught-up predicate;
   per-source counts and a rate alarm.
9. **Finish-session validation** (ACCEPTANCE_CRITERIA.md §3): final
   reconciliation of every source, then the blocker list or an audited close.
10. **Restart sequence and crash safety under continuous processing**
    (ARCHITECTURE_NOTES.md §13): `recover_interrupted` → conflict-sync
    recovery pass → reconcile every source → resume if it was running.
    Recovery resumes the **same session and sealed unit**; it never creates a
    session or a superseding batch (recovery is not reprocessing). A sheet is
    reported completed only when its result and its required conflict state
    are durably committed — one transaction, or a recorded step that recovery
    deterministically completes before the sheet is Resolve-ready (the Phase B
    work unit). An interrupted SEALED unit stays SEALED and resumes. Progress is always derived from committed
    rows.

## 4. Out of scope

- GUI (F); automatic rejection or replacement; recognition or quality
  measurement changes; scheduling across machines; the Phase 10 harness.

## 5. Persistence expectations

- Prefer existing schema; any addition is an additive migration with an upgrade
  test.
- A forced kill at any point — mid-unit, mid-conflict-sync, mid-rejection —
  leaves a consistent state; tested with real process kills at small scale.
- Supersession, rejection and their undo are single transactions with their
  audit events.

## 6. Tests required

- Unit: scheduler ordering and sizing; every quality-mapping row incl.
  not-evaluated and not-verified; suggested-rejection mapping; snapshot
  partition; each finish blocker; pause semantics.
- Integration: a three-source session of ~500 synthetic sheets (existing
  generator, folds for RESCAN_REQUIRED) → sealed units → conflicts per unit;
  late duplicate across units; correction dissolves; suggested rejection
  confirmed; replacement from a different source counted once; undo; chain;
  restart at each stage; kill mid-unit with a submission log proving no
  resubmission.
- Contention test for concurrent writes; GUI-thread write latency bounded
  (headless simulated caller).
- Crash safety with intake running: ACCEPTANCE_CRITERIA.md §5.4 cases 1–4 and
  9–12 (case 11 at small scale), including a real kill between a unit commit
  and its conflict sync, and repeated restarts proving no duplicate rows, no new
  session and no superseding batch.
- Endurance (mark `stress`): the ACCEPTANCE_CRITERIA.md §5.3 set at a scale
  this machine allows — multiple batches, continuous intake,
  supersession/reprocessing mid-session, restart/resume, session-level
  aggregation equal to an uninterrupted run.
- Regression: finite GUI conflict flow identical (`test_scan_page.py`,
  `test_resolve_page.py` unchanged); plus a GUI test proving the finite path
  still syncs conflicts after a run. Whole suite unchanged; Phase 10 self-test
  passes.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest -q -m stress -k "intake or session"
```

## 8. Documentation updates

`docs/intake.md` (units, controls, finish validation); `docs/scan_quality.md`
(the decision layer above the evidence; defaults unvalidated);
`docs/conflict_review.md` (incremental sync); a new `docs/rescan.md` or the
existing Reject & Rescan documentation extended (suggestions, session rules);
the new ADR; `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md` as touched;
`ROADMAP.md` §7; new `PHASE_E_HANDOFF.md`; `CURRENT_STATE.md`; README
Development/Testing status; `docs/wiki/Development-Roadmap.md`; `CHANGELOG.md`.

## 9. Status reporting

Separately: implemented; tested; synthetic validation — the ~500-sheet run is
**not** the ACCEPTANCE_CRITERIA.md §5 campaign; network share not performed;
real scanners not performed; production not performed. State that the quality
decision defaults are unvalidated.

Commit in small commits, push the branch, do not merge unless asked.
