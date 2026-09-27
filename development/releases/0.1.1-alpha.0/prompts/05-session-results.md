# Prompt 05 — `0.1.1-E`: Session-scoped reconciliation, scoring & reporting

You are working in the OMRFlow repository. This is **phase 0.1.1-E** of the
`0.1.1-alpha.0` line. Phases A–C must be merged (D may be merged or not; this
phase must not depend on D's widgets). Otherwise stop and say so. Branch:
`feat/0.1.1-e-session-results`.

Why this phase exists: Phases 7–9 are keyed by `(roster_id, batch_id)` and the
Attendance, Results and Reports pages default to the **latest batch**. A
session made of many processing units would otherwise reconcile, score and
report a single unit, silently. See `ARCHITECTURE_NOTES.md` §13 and
`ACCEPTANCE_CRITERIA.md` §2-E. The code is the fact; record discrepancies.

---

## 1. Inspect first

- Phase A–C (and D if present) handoffs; `docs/rescan.md`
- `services/reconciliation.py`, `services/reconciliation_store.py`
  (`reconcile_batch`, `batch_scripts`, decisions), `domain/reconciliation.py`
  (`ACCIDENTAL_RESCAN`, `counts_as_a_script`)
- `services/scoring.py`, `services/scoring_store.py`, `domain/scoring.py`
- `services/report_store.py`, `services/report_readiness.py`,
  `services/report_template.py`, `reporting/excel.py`, `reporting/pdf.py`
- `services/review_store.py` (`effective_identifiers`, `provenance_for`)
- `database/models.py`: `reconciliation_*`, `candidate_result`,
  `generated_report` and their unique keys
- `gui/attendance/page.py`, `gui/results/page.py`, `gui/reports/page.py`
  (how they pick a batch)
- `docs/reconciliation.md`, `docs/scoring.md`, `docs/reporting.md`,
  `docs/wiki/Examination-Sets.md`
- tests: `tests/unit/test_reconciliation*.py`, scoring and report tests,
  `integration/test_reconciliation_workflow.py`,
  `test_multi_set_reports.py`, `test_stress_reconciliation_and_reports.py`,
  `tests/gui/test_attendance_page.py`, `test_scoring_pages.py`,
  `test_reports_page.py`

Run the full suite first; record the baseline.

## 2. Preserve

Everything in prompt 01 §2, and specifically:

- **Per-batch reconciliation, scoring and reporting for finite projects are
  byte-for-byte unchanged** — same rows, same workbook contents.
- The four-values-apart rule of Phase 7 (roster value, machine value, human
  decision, effective value); write-once `registered_candidate`.
- "Regenerate, never patch" for scores and reports; key and policy revision
  recorded on every mark.
- Per-set rosters and result templates; `RANK.EQ` ranking; never overwriting
  an existing output file; the template never opened for writing.
- Report readiness blocks Final Export on disagreements.

## 3. Scope

1. **Effective scan set** of a session as a service: recognised, not
   superseded, not duplicate-content, latest effective reading per asset;
   rescan-outstanding assets excluded and counted as such.
2. **Session scope for Phase 7–9**: reconciliation, scoring and reporting
   accept either a batch (today) or a session. Choose the representation after
   inspection — e.g. generalising the key to a scope, or a session-level run
   beside the batch-level one — and record it in an ADR. Existing rows stay
   valid without rewrite.
3. **Provisional vs final**: results on an open session are labelled
   provisional everywhere they are shown or exported; Final Export requires a
   closed session (`report_readiness` gains that check).
4. **Page selection**: Attendance, Results and Reports select a session or a
   batch explicitly; when a session exists, "latest batch" is never chosen
   implicitly. Minimal GUI: a selector and the provisional label, following
   existing page patterns.
5. **Renamed output copies for a session** (ARCHITECTURE_NOTES.md §8): an
   explicit export over the effective set using `FilenameAllocator`, so a
   superseded original never takes the plain roll name.
6. Reconciliation decisions made while the session was open survive the
   session's growth and closure; re-reconciliation after new arrivals is
   idempotent and explains what changed.

## 4. Out of scope

- Changes to scoring rules, reconciliation classification, ranking or report
  layout.
- The operational Scan/Resolve/Rescan GUI (D).
- Qualification (F).

## 5. Persistence expectations

- Additive migration if needed, with an upgrade test from the prior schema
  and a schema-9 fixture; every existing Phase 7–9 row readable and
  unchanged.
- Decision rows remain keyed so that a decision made against a session is not
  silently applied to a batch, or vice versa.

## 6. Tests required

- Unit: effective-set rules (superseded, duplicate, rescan-outstanding,
  chains, undo); scope handling in reconciliation, scoring, readiness.
- Integration: a synthetic **multi-set** session (existing generator with
  paired attendance workbooks and answer keys) spread over many units, with
  replacements and duplicates → results match the generator's independently
  computed ground truth; the same cohort processed as one finite batch gives
  identical marks.
- **Golden regression**: a finite project from `0.1.0-alpha.2`-era fixtures
  produces byte-identical reconciliation rows and workbook cell values.
- GUI: selectors, provisional labels, Final Export blocked on an open session.
- Whole existing suite unchanged; Phase 10 self-test passes.

## 7. Verification

```powershell
.venv\Scripts\python.exe -m ruff check src tests tools scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest -q
```

## 8. Documentation updates

- `docs/reconciliation.md`, `docs/scoring.md`, `docs/reporting.md`: session
  scope, provisional results, effective set.
- `docs/wiki/Attendance.md`, `Answer-Keys-and-Scoring.md`,
  `Results-and-Reports.md`, `Known-Limitations.md`.
- `docs/DATA_MODEL.md`, the new ADR, `docs/ARCHITECTURE.md`.
- `PHASE_E_HANDOFF.md`, ROADMAP.md §5, `CURRENT_STATE.md`,
  `docs/wiki/Development-Roadmap.md`, `CHANGELOG.md`.
- **README Development status / Testing status**: `0.1.1-E` row with phases
  completed / under testing, automated, synthetic, real-data status and
  pending work.

## 9. Status reporting

Report separately: implementation complete; automated tests complete;
synthetic validation (the multi-set ground-truth run — state its size);
network-share validation n/a; real scanner / real-data validation **not
performed** (no real roster or cohort); production qualification not
performed. Synthetic ground truth checks consistency, not recognition
accuracy — say so.

Commit in small commits, push the branch, do not merge unless asked.
