# Phase 9 handoff — Result Management & Reporting

**Implemented:** 2026-09-20
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2,
openpyxl 3.1.5, SQLAlchemy 2.x, 16 logical CPUs. LibreOffice **not** installed
in the verification environment (see §10).

Read alongside `docs/reporting.md` (the operator-facing description),
`docs/DATA_MODEL.md` and `docs/ARCHITECTURE.md`.

**Phase 9 turns a mark into a filed document, without becoming a second place
marks are decided.** Every number on a generated report is read from a Phase 8
`CandidateResult`; every attendance fact is read from Phase 7's reconciliation.
Nothing here recognises a scan, computes a score, or decides who was present.

---

## 1. What already existed

| Capability | Where | Verdict |
|---|---|---|
| Stored, reproducible marks with exact-key/policy provenance | `services.scoring_store` (Phase 8) | Read, never re-derived |
| Reconciled attendance, per registered candidate | `services.reconciliation_store` (Phase 7) | Read as the roster/absence source |
| Verified per-set answer keys | `services.scoring_store.verified_keys` (Phase 8) | Read for the Answer Key sheet |
| Reading an Excel workbook, robust to messy real files | `services.candidate_import` (Phase 7) | Column-matching algorithm reused, extended for a fifth field (Merit) |
| A `<project>/exports` directory | `domain.project.ProjectDirectory.EXPORTS` | Reserved since Phase 0; used for the first time |
| `openpyxl` as a declared dependency | `pyproject.toml` (marked "Phase 9" since Phase 0) | Used for the first time, well beyond the single call site its mypy override used to describe (updated) |
| Background-worker pattern (`QThread` + one signal) | `gui/scan`, `gui/results` | Reused for `ReportGenerationWorker` |
| `reporting/` package, reserved | `reporting/__init__.py` | Filled in, following its own documented plan almost exactly (`excel.py`, `pdf.py`) |

**Nothing was rewritten.** No second scoring engine, no second reconciliation,
no second candidate-ID normalisation - `normalise_candidate_id` and the header-
matching algorithm from `services/candidate_import.py` were promoted to public
names and reused, not duplicated with a second, possibly-diverging idea of what
a Roll No. header looks like.

## 2. What was missing

1. No way to say which template belongs to which set.
2. No ranking - the brief's "1224" competition ranking, checked against
   `RANK.EQ` semantics, did not exist anywhere.
3. No Excel generation of any kind.
4. No PDF export.
5. No readiness/validation gate before a report could be called final.
6. No traceability from a generated file back to the inputs that produced it.

## 3. What was built

### `domain/reporting.py` — the vocabulary

Pure. `compute_ranks` (competition ranking), `rank_formula` (the dynamic
`RANK.EQ` text, never hard-coding a column or a row range), `column_letter`,
`safe_cell_text` (spreadsheet-injection mitigation), `safe_filename_component`,
`total_header_for`/`header_matches_maximum`, and the readiness vocabulary
(`ReadinessIssue`, `ReadinessIssueKind`, `ReadinessReport`).

`compute_ranks` is a function of its argument and nothing else - the same
reason `domain.scoring.score_answers` is pure. It has to be, because the
brief's own exit criterion is that the application's rank and the Excel
formula's rank must provably agree.

### `services/report_template.py` — reading a result template

`preview_template`, `read_template`, `suggest_mapping`, `TemplateRoster`.
Header-row detection scans the first fifteen rows rather than assuming row 1,
because a real institution's workbook carries a title above its table - the
brief is explicit about this (§38) and Phase 7's own importer does not need
it, since a roster CSV rarely does.

### `services/report_readiness.py` — is this set ready?

One pure function, `evaluate`, cross-checking a read `TemplateRoster` against
Phase 7's `ReconciliationEntry` list and Phase 8's `StoredResult`s. Ten
distinct issue kinds, every one naming a Roll No. and a reason. Never resolves
ambiguity - every disagreement becomes an issue a person reads.

### `reporting/excel.py` — the mechanics

`copy_into` (template preservation - the *only* function that ever touches the
original file, and only to copy its bytes), `populate_rollwise`,
`add_meritwise_sheet`, `add_summary_sheet`, `add_answer_key_sheet`,
`add_processing_log_sheet`, `apply_layout`. No database, no Qt - everything it
needs arrives as plain dataclasses (`CandidateReportRow`, `MeritCandidate`,
`SummaryData`, `LayoutSettings`).

### `reporting/pdf.py` — PDF, through whichever engine exists

`PdfExporter` (a `Protocol`), `LibreOfficePdfExporter`,
`UnavailablePdfExporter`, `detect_exporter`. Dependency-injected everywhere it
is used, so the whole orchestration layer is tested without LibreOffice being
installed (§44) - only one test genuinely needs it, and it is skipped, loudly,
when absent (§44's own instruction).

### `services/report_store.py` — persistence and orchestration

Template associations, layout configuration (project-wide default + optional
per-set override), the append-only `GeneratedReport` audit trail,
`generate_xlsx`, `generate_pdf`. This is where "regenerate, never patch" is
enforced: every generation reads the roster, reconciliation and scoring fresh
and rebuilds the whole workbook. Nothing here opens a previous report and
edits a cell.

### Migration 6

`report_template_association`, `report_layout_config`, `generated_report`.
Purely additive, following migration 5's own pattern exactly.

### `gui/reports/`

`page.py` (the Result Management stage), `worker.py`
(`ReportGenerationWorker`), `layout_dialog.py`, `template_dialog.py`.

## 4. Why the template is authoritative for set membership

Recognition only knows a candidate's set once their script has been read - an
absentee has no script and therefore no set anywhere in Phase 1-8's data. The
office's own result template is the one place "this Roll No. sat Set A" is
recorded independently of a script, which is exactly why §3 and §8 of the
brief make it authoritative for roster membership, order and existing
absentee markers, while Phase 7/8 remain authoritative for *whether* someone
was present and *what* they scored. `docs/reporting.md` §1 documents this
explicitly, because it is the single most important design decision in the
phase and the least obvious one from the code alone.

## 5. Why ranking is exact and pure

`compute_ranks` never touches a float. Marks are `fractions.Fraction`
throughout (Phase 8's own rule, carried forward), sorted and compared exactly,
so a tie is a tie and never an artefact of floating-point comparison. The
function is checked directly against the brief's own worked example
(`90, 88, 88, 85 → 1, 2, 2, 4`) and against a hand-built `RANK.EQ` truth table
in the integration suite - not merely "it compiles and returns numbers that
look plausible".

## 6. Why the rank formula is generated, never hard-coded

`rank_formula` takes the marks column letter and the first/last data row as
arguments, derived from the template's own mapping and the template's own row
count - never `"D"`, never `230`. A test builds a ten-row template and a
230-row template and asserts the formula text differs correctly in both, and
that the row range never contains the header row.

## 7. Why a template association is not revisioned

Unlike an answer key or a scoring policy, selecting a different template for a
set is not score-affecting - it changes how a report looks, not what a
candidate is worth. `ReportTemplateAssociation` is therefore one row per set,
updated in place. What *is* preserved per generation is a full snapshot on
`GeneratedReport` - the template path, its SHA-256 at that moment, the exact
key and policy revisions used - so a filed report is traceable even after the
association has since changed.

## 8. Why the processing log is built from typed entries, not a log line

`ProcessingLogEntry` is a plain `(label, value)` pair, and every value that
reaches it in `services/report_store.py` is already a count or a hash - never
a candidate ID, name or answer string. This mirrors Phase 7's own privacy
discipline (`docs/reconciliation.md` §9) rather than reinventing it, and is
tested the same way: synthetic, deliberately distinctive candidate values are
run through generation and asserted absent from both the ordinary application
log *and* the Processing Log sheet's own text.

## 9. Two defects an adversarial pass against this phase's own tests found

Both were caught before this handoff was written, by running the phase's own
GUI test suite and qtguitesting smoke checks - not discovered later by a user.

### A test template with a candidate on the wrong set

The qtguitesting smoke harness for Set A originally listed all four synthetic
candidates, including one whose script was genuinely read as **Set Z** (a set
with no answer key at all - deliberately, to exercise "no verified key").
Because a set's template is authoritative for who belongs to it, that
candidate correctly tripped `ReadinessIssueKind.SET_MISMATCH`/
`PRESENT_WITHOUT_SCORE` and blocked generation - a real, working readiness
check catching a genuinely inconsistent fixture, not a bug in `report_store`.
Fixed by giving the harness's Set A template only the candidates actually on
Set A's own roster (§3's own rule, applied to the test fixture that was
violating it).

### An automatic callback that could hang forever

`ReportsPage._on_generated` - the slot a background worker's `ready` signal
invokes whenever a run happens to finish - originally opened a
`QMessageBox.information` summary whenever more than one job ran or any job
was blocked. Discovered by running the new qtguitesting smoke checks: a
routine "one set was blocked" result opened a modal with nothing to close it,
hanging the process indefinitely. This is the same defect class fixed in
Phase 8's `ResultsPage._on_scored` during that phase's own audit, for the same
reason: a modal raised from a callback that can fire at any time - including
while nobody is watching, or from an automated driver - blocks the whole
interface until a human clicks it. Fixed by replacing the modal with an inline
status label (`generation_status_label`); a genuine worker exception is still
shown, as coloured text, never as a dialog that has to be dismissed before the
page is usable again.

## 10. Testing

| Suite | Count | What it holds |
|---|---|---|
| `tests/unit/test_reporting.py` | 65 | Ranking (including the brief's own worked example and a 20,000-candidate timing check), the `RANK.EQ` formula generator, spreadsheet-injection safety, safe filenames, the total-marks header, readiness vocabulary |
| `tests/unit/test_report_template.py` | 35 | Reading the sample's own structure, every variation §38 lists (different sheet name, header on row 3+, decorative rows, extra columns, merged header cells, `ABS`/lowercase `absent`, leading-zero rolls, Unicode names, empty names, duplicate rolls, malformed workbooks, missing columns) |
| `tests/unit/test_excel_report.py` | 34 | Template preservation (byte-for-byte, proven by SHA-256), Rollwise population, Meritwise sorting/ties/injection-safety, Summary's `N/A` handling, Answer Key per-set content, Processing Log privacy, layout page setup and graceful degradation, Unicode, missing/real logo degradation |
| `tests/unit/test_pdf_export.py` | 8 (1 conditionally skipped) | The exporter protocol, the unavailable-engine fallback, dependency-injected orchestration; one real-LibreOffice test, skipped honestly where absent |
| `tests/integration/test_report_generation.py` | 25 | The phase brief's six acceptance scenarios (§54) end to end, against real Phase 7/8 services and real workbooks, plus the schema-6 migration onto an existing Phase 8 project |
| `tests/gui/test_reports_page.py` | 12 | The real page, the real dialogs (never `.exec()`'d in a test - see §11), the real worker thread |
| **Total new** | **179** | |

Plus 3 new `qtguitesting` smoke checks (**52/52 passing**, up from 48).

Full regression suite: run at the end of this phase; see the final report for
the exact count. `ruff check .` clean. `mypy` clean (141 source files, up from
136).

### The assertions that matter most

```python
assert compute_ranks([("a", F(90)), ("b", F(88)), ("c", F(88)), ("d", F(85))]) \
    == {"a": 1, "b": 2, "c": 2, "d": 4}                       # the brief's own example
assert "$D$2:$D$97" in formula and "230" not in formula        # never hard-coded
assert before_hash == after_hash                                # template untouched
assert outcome.status == "blocked" and outcome.output_path is None  # readiness gates final export
assert second.output_path != first.output_path                 # never silently overwritten
assert set_a_workbook_rolls != set_b_workbook_rolls             # per-set isolation
```

## 11. A rule this phase reinforced, not invented

**A modal dialog must never be opened from an automatic callback.** Phase 8's
own audit fixed this once (`ResultsPage._on_scored`); Phase 9 rediscovered the
identical defect independently in a brand-new page, which is itself evidence
that this needs to be a standing rule for every future `QThread.ready` handler
in this codebase, not a one-off patch. Every `TestCase` in
`tests/gui/test_reports_page.py` that drives a dialog-owning command (template
selection, layout configuration) calls the split, non-modal method underneath
it instead of the command that shows the dialog - `associate_template` beside
`prompt_select_template`, `apply_layout_settings` beside `configure_layout` -
the same split Phase 3 onward already uses for every other modal command on
this project.

## 12. What is not done

- **No examination has been reported on.** Every test is synthetic or reuses
  the repository's rendered sheets; no real examination office's own workbook
  has been fed through this pipeline.
- **PDF export is untested against a real LibreOffice installation** in this
  environment - `soffice` was not present. The abstraction, the availability
  detection and the orchestration around it are fully tested with a
  dependency-injected fake; the one test that needs a real engine is present,
  correctly guarded, and will run the moment LibreOffice is available.
- **No Windows Excel COM adapter.** Deliberately not built - see
  `reporting/pdf.py`'s own docstring for why an untested COM path would be
  worse than an honest "install LibreOffice" message.
- **Reporting is per batch and per roster**, following Phase 8's own
  limitation - a cohort split across two batches would be reported on twice.
- **No examination-scale run.** Report generation is arithmetic and file I/O,
  fast by construction, but the largest *real* cohort anywhere in this project
  remains under fifty candidates.
- **The report layout editor's font controls apply to sheets this application
  creates** (Meritwise, Summary, Answer Key, Processing Log, and any inserted
  title text); the Rollwise sheet's existing cell fonts are deliberately left
  exactly as the template had them, since preserving the template is the
  point.

## 13. For whoever picks this up

**Phase 10 (Integration, Recovery & Production Hardening)** is next.

- `report_store.set_overview` and `check_readiness` already give a project-
  wide view per set; a future project-wide *reporting* dashboard (across
  multiple batches/rosters) would build on them rather than re-deriving
  per-set counts.
- `GeneratedReport` already carries everything a future "resend this exact
  report" or "prove what was filed on this date" feature would need - the
  template hash, the exact key/policy revisions, the output hash.
- If a genuine multi-batch cohort becomes a requirement, `gather_set_inputs`
  is the one place that would need to read more than one batch's results;
  everything downstream of it already operates on the `SetGenerationInputs`
  it returns.

**If you add a report sheet**, keep it a pure function taking plain data in
(mirror `add_summary_sheet`'s shape) - no database access inside
`reporting/excel.py`, ever.

**If you touch readiness**, keep every issue naming a `ReadinessIssueKind` and
a message; a check that returns `bool` instead of a `ReadinessIssue` cannot be
shown in the "Cannot export Set B" checklist the brief's own §32 example
requires.

**The one thing not to do** is add a delta-patch path for a generated report,
or open a `QMessageBox` from a slot connected to a worker's completion signal.
Both have already cost this project a defect once each.
