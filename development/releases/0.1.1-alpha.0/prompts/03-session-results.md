# Prompt 03 — `0.1.1-C`: Session-level review, reconciliation, scoring and reporting

You are working in the OMRFlow repository. This is **phase 0.1.1-C** of the
`0.1.1-alpha.0` line. Phases A and B must be merged (`PHASE_A_HANDOFF.md`,
`PHASE_B_HANDOFF.md` exist); phase D may or may not be merged — this phase must
not depend on it. Otherwise stop and say so. Branch:
`feat/0.1.1-c-session-results`.

**Why this phase exists:** every Phase 7–8 table is keyed
`(roster_id, batch_id, …)` and Resolve, Attendance, Results and Reports read one
batch. A session of several batches would otherwise reconcile, score and report
one batch — silently. This phase gives Attendance, reconciliation, scoring,
Results and final Reports **one authoritative session-level view**, and keeps
batch-level views for provenance, debugging and monitoring.

Plan: `ROADMAP.md` §5 C; `ARCHITECTURE_NOTES.md` §§2.3, 2.4, 3 (defects 1–4),
4 (F3–F7), 8, 9, 15, 16 (Q4–Q6); `ACCEPTANCE_CRITERIA.md` §2 C, §3, §4. The code
is the fact; record discrepancies.

---

## 1. Inspect first

- Phase A and B handoffs and ADRs; `services/scan_sessions.py`
- `database/models.py`: `reconciliation_run/_entry/_script/_decision`,
  `candidate_result`, `generated_report` and their unique keys and foreign keys
  (note: `batch_id` is a NOT NULL FK with `ON DELETE CASCADE`, and SQLite cannot
  alter a UNIQUE constraint in place)
- `services/reconciliation.py`, `services/reconciliation_store.py`,
  `domain/reconciliation.py`; `services/scoring.py`, `services/scoring_store.py`;
  `services/report_store.py`, `services/report_readiness.py`,
  `reporting/excel.py`
- `services/review_store.py` (`sync_duplicate_identifiers`, effective values),
  `services/conflict_policy.py`, `domain/review.py`
- `services/scan_lifecycle.py` (`adopted_replacements`, `counted_elsewhere`,
  `ineligible_scan_ids`, `processed_sheets`), `services/scan_provenance.py`
  (`hash_file`, `duplicate_groups`), `gui/scan/worker.py` (where hashing runs)
- `gui/review/page.py`, `gui/attendance/page.py`, `gui/results/page.py`,
  `gui/reports/page.py`
- `docs/reconciliation.md`, `docs/scoring.md`, `docs/reporting.md`,
  `docs/conflict_review.md`
- tests: reconciliation, scoring, report, review-store, Reject & Rescan tests (`test_reject_rescan_rules.py`, `integration/test_reject_and_rescan*.py`);
  `integration/test_reconciliation_workflow.py`, `test_multi_set_reports.py`,
  `test_stress_reconciliation_and_reports.py`; GUI tests for these pages

Run the full suite first; record the baseline.

## 2. Preserve

- **A single-batch session gives byte-identical** reconciliation rows and
  workbook cell values to `main` before this phase.
- Phase 7's four-values-apart rule; write-once `registered_candidate`;
  "regenerate, never patch" for scores and reports; key and policy revision on
  every mark; per-set rosters and templates; `RANK.EQ`; never overwriting an
  output file; the template never opened for writing; readiness blocking Final
  Export on disagreements.
- Reject & Rescan semantics: a confirmed replacement counts once.
- Everything in `prompts/README.md` "Rules every prompt repeats".

## 3. Scope — exactly this

1. **ADR on the representation** (ARCHITECTURE_NOTES.md §8.3): (a) generalised
   scope key via table rebuild, (b) parallel session-scoped tables, or (c)
   session as the only stored scope with existing rows migrated once. Evaluate
   (c) first; decide on evidence; record migration size and regression risk.
2. **Migration** (next free number) implementing the ADR; `generated_report`
   gains its scope. Table rebuilds follow the SQLite rebuild procedure inside
   the migration's transaction, with `foreign_key_check` afterwards.
3. **Effective scan set service** (ARCHITECTURE_NOTES.md §8.1): pure, tested,
   with a count per exclusion reason (rejection state, superseded batch,
   exact duplicate). It is the **only** place the supersession invariant is
   evaluated: only effective, non-superseded batch/sheet membership contributes
   to session-level attendance, reconciliation, scoring, results and final
   reports — for every cause (*Reprocess All*, whole-batch rescans, future
   algorithmic reprocessing, recomputation after reopening). Superseded batches
   stay retained and inspectable.
4. **Resolve**: one queue per session with batch and source filters; counts
   per session; cross-batch duplicate groups navigable.
5. **Duplicate-ID conflicts session-wide on effective identifiers**
   (§9.2), incremental and bounded (only affected groups, indexed), triggered
   after each commit and after each decision that changes an effective
   identifier; detail text says "in this session"; the (set, identifier)
   grouping option, default unchanged.
6. **Exact duplicates**: hash at **registration** for manual adds (move or add
   to the hashing now done at run start), session-wide duplicate handling;
   `duplicate_groups` finally used.
7. **Reconciliation, scoring, reports over the session**; Attendance, Results
   and Reports select a session explicitly (default: the active session), name
   it, and never choose by `updated_at`; batch-level diagnostic views,
   labelled as such.
8. **Provisional vs final** (§8.2, decided): provisional labels while open, in
   every display and export; Final Export requires a CLOSED session;
   `report_readiness` checks it. If the session is open, Final Export offers
   one step, **"Close session and generate final export"**: run the closure
   checks — on any blocker list them and change nothing; otherwise close
   (sealing the batches, audited) and generate from the authoritative state.
   A normal finite import never needs a separate session-management screen.
   Final outputs record the session and the close they came from; after a
   **reopen** they are shown **stale** and must be regenerated after any
   subsequent change.
9. **Rescans in session terms** (§9.3): counted once in the session; new
   cross-session replacement links refused with a clear message; legacy links
   honoured.
10. **Renamed-copy export over the effective set** for multi-batch sessions,
    using `FilenameAllocator`, so a superseded original never takes the plain
    roll name.

## 4. Out of scope

- Watched folders and the intake ledger (D); continuous processing (E); the
  operational session GUI (F).
- Changes to scoring rules, reconciliation classification, ranking or report
  layout.

## 5. Persistence expectations

- Upgrade tests from schema-12 fixtures through phases A and B's migrations to
  this one: every existing Phase 7–9 row readable; results unchanged.
- Reconciliation decisions keyed so a decision made against a session is never
  silently applied elsewhere, and survive the session's growth and closure;
  re-reconciliation after new arrivals is idempotent and explains what changed.

## 6. Tests required

- Unit: effective-set rules (every rejection state, supersession, reprocess,
  exact duplicate, chains, undo); scope handling in reconciliation, scoring,
  readiness; duplicate grouping incl. the (set, identifier) option.
- Integration: the 10:03 / 10:47 / 11:05 scenario across two batches; a
  synthetic **multi-set** session over several batches with replacements and
  duplicates matching the generator's ground truth, and matching the same cohort
  processed as one batch; defect-1 fixture (two batches of one exam, combined)
  → nobody absent; defect-2 scenario → what is scored does not change.
- **Golden regression**: single-batch project, byte-identical rows and cells.
- GUI: session selector and header, batch filter, provisional labels, Final
  Export blocked while open, the one-step close-and-export (blocked path and
  success path), stale outputs after reopen.
- Crash safety: forced termination during a session-level reconciliation,
  scoring and report generation leaves no partial run or half-recorded report;
  repeated restart creates no duplicate attendance or scoring rows; session
  results after interruption equal an uninterrupted run
  (ACCEPTANCE_CRITERIA.md §5.4 cases 9, 10, 12).
- Whole suite unchanged; Phase 10 self-test passes.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
```

## 8. Documentation updates

`docs/reconciliation.md`, `docs/scoring.md`, `docs/reporting.md`,
`docs/conflict_review.md` (session scope, provisional results, effective set);
`docs/wiki/Attendance.md`, `Answer-Keys-and-Scoring.md`,
`Results-and-Reports.md`, `Review-and-Resolution.md`, `Known-Limitations.md`;
`docs/DATA_MODEL.md`; the new ADR; `docs/ARCHITECTURE.md`; `ROADMAP.md` §7; new
`PHASE_C_HANDOFF.md`; `CURRENT_STATE.md`; README Development/Testing status;
`docs/wiki/Development-Roadmap.md`; `CHANGELOG.md`.

## 9. Status reporting

Separately: implemented; tested; synthetic validation — the multi-set
ground-truth run, **with its size**, which is not the ACCEPTANCE_CRITERIA.md §5
campaign; network share n/a; real scanners / real data not performed (no real
roster or cohort); production not performed. Synthetic ground truth checks
consistency, not recognition accuracy — say so.

Commit in small commits, push the branch, do not merge unless asked.
