# Development roadmap

Development proceeds in phases. A phase ends only when its exit criteria are met,
`pytest`, `ruff check .` and `mypy` all pass, `development/CURRENT_STATE.md` is
updated and a `PHASE_NN_HANDOFF.md` is written.

Phases are deliberately sequenced so that each one can be validated on its own:
geometry before recognition, recognition before batch processing, conflicts
before reconciliation, and scoring before reporting. Skipping ahead produces work
that cannot be tested.

| Phase | Title | Status |
|---|---|---|
| 0 | Architecture & Repository Foundation | **Complete** |
| 1 | OMR Geometry & Alignment Engine | **Complete** |
| 2 | Template Data Model & Template Designer Core | **Complete** |
| 3 | Bubble Mapping & Recognition Engine | Implemented; testing in progress - see `development/PHASE_03_HANDOFF.md` |
| 4 | Template Calibration & Validation | Implemented; testing in progress - see `development/PHASE_04_HANDOFF.md` |
| 5 | Batch Scan Processing Pipeline | Implemented; testing in progress - see `development/PHASE_05_HANDOFF.md` |
| 6 | Conflict Detection & Human Resolution | Implemented; testing in progress - see `development/PHASE_06_HANDOFF.md` |
| 7 | Candidate & Attendance Reconciliation | Implemented; testing in progress - see `development/PHASE_07_HANDOFF.md` |
| 8 | Answer-Key & Scoring Engine | Implemented; testing in progress - see `development/PHASE_08_HANDOFF.md` |
| 9 | Result Management & Reporting | Implemented; testing in progress - see `development/PHASE_09_HANDOFF.md` |
| 10 | Integration, Recovery & Production Hardening | Not started |
| 11 | Release, User Documentation & Packaging | Not started |

---

## Phase 0 - Architecture & Repository Foundation

**Purpose.** Establish a tested, documented, layered foundation that later phases
can build on without rewriting it.

**Deliverables.** Packaging and tool configuration; layered package skeleton;
exception hierarchy; application configuration; logging; project model and
service; SQLite/SQLAlchemy foundation with migrations; `.omrt` template model and
load/save; minimal PySide6 shell with honest placeholders; test infrastructure;
documentation set and ADRs.

**Major tests.** Configuration loading; project create/open/reopen/invalid;
database initialisation, reopening and migration refusal; template validation and
round trip; GUI startup; executable layering checks.

**Exit criteria.** Application starts; a project can be created, reopened and
rejected cleanly; module boundaries are enforced by a test; the suite, Ruff and
mypy pass; documentation matches the source tree; unimplemented features are
visibly marked as such.

---

## Phase 1 - OMR Geometry & Alignment Engine

**Purpose.** Turn an arbitrary scan into the canonical page the template
describes. This is the contract every later phase depends on.

**Deliverables.** Delivered as `imaging.preprocessing`,
`imaging.marker_detection`, `imaging.orientation`, `imaging.geometry`,
`imaging.alignment`, `imaging.diagnostics`, `imaging.config`, `imaging.models`
and `imaging.synthetic`, plus `services.alignment_service` and the developer
tools in `omr_scanner.tools`. (The module names differ from those planned in
Phase 0; the responsibilities are the same, split one concern per file.)

**Major tests.** Synthetic round trip: apply a known rotation, scale,
translation and perspective, then measure nine interior control points that took
no part in the fit. Degradation sweeps over blur, noise, brightness, illumination
gradient, JPEG compression, cropping and damaged markers. All four page
orientations. Missing markers raise rather than guess.

**Exit criteria - met.** Accuracy is measured (0.062 px mean, 0.394 px maximum
over 360 control-point measurements) with a 1.5 px regression threshold; the
degradation boundary is documented in `docs/IMAGE_PROCESSING.md`; no Qt import
exists anywhere in `imaging`, enforced by test.

**Not done.** No real-world validation - every number is synthetic. See
`development/PHASE_01_HANDOFF.md`.

---

## Phase 2 - Template Data Model & Template Designer Core

**Purpose.** Let a user create and edit a template visually instead of by hand.

**Deliverables.** Delivered as `gui.template_designer` (canvas, region list,
properties panel, dialogs, undo/redo history, session state) plus
`domain.template_authoring` (region generation and designer-facing validation)
and `services.marker_detection_service` (the Qt/imaging seam). The template
data model itself needed only one additive field (`reference_image`); Phase 0
already built the rest (`Zone`, `BubbleGrid`, `FieldDefinition`).

**Major tests.** Round trip designer -> `.omrt` -> designer (129 automated
tests, plus a scripted 12-step functional demonstration,
`docs/testing/phase_02_demo.md`); zone geometry edits produce valid documents by
construction (`model_copy` on an already-validated `Zone`); invalid geometry is
refused at the point of dialog entry; 34 GUI smoke tests covering marker
detection, region creation/editing/deletion, fine-tune bubble mode, undo/redo,
validation and save/reload.

**Exit criteria - met.** A complete template (7-digit student ID, A-D question
set, 100 questions in 4 columns, 474 bubbles) is produced entirely by the
designer's own code path and reloaded unchanged
(`examples/templates/100_question_4_choice_example.omrt`); `docs/TEMPLATE_FORMAT.md`
and the implementation agree (one additive field, documented).

**Not done.** No real printed sheet has been used with the designer; no
snap-to-grid or align/distribute tools. See
`development/PHASE_02_HANDOFF.md`.

---

## Phase 3 - Bubble Mapping & Recognition Engine

**Purpose.** Convert a canonical sheet plus a template into field values with
confidence.

**Delivered as** `imaging.metrics` (per-bubble fill measurements),
`recognition.decide` (measurement -> marked/unmarked/ambiguous/multiple),
`recognition.fields` (numeric, alphanumeric, set code, question blocks),
`services.recognition_service` (the one door into the pipeline,
`RecognitionEngine`/`ScanResult`), batch processing with a multicore worker
pool, large-batch progress tracking, and the `gui.scan` workspace. Confidence
is a bounded decision score, never a probability; every threshold comes from
the template. Full detail, including what remains open: `development/PHASE_03_HANDOFF.md`.

**Exit criteria - partially met.** Recognition is architecturally stable and
measured on a large synthetic corpus and one real scanned sheet (plus
geometric variants of it); ambiguity is never silently resolved and every
threshold comes from the template. **Not yet met:** validation against a
broad, independently filled real corpus - the condition under which this
phase will be marked complete.

---

## Phase 4 - Template Calibration & Validation

**Purpose.** Let a user verify and tune a template against real scans before
processing a whole batch.

**Delivered as** `gui.calibration` (the workflow stage: test-scan management,
marker/bubble/region overlays, click-to-inspect, field filtering, the four
`RecognitionSettings` threshold controls with immediate reclassification, a
per-scan and per-sample quality summary), `services.recognition_service.CalibrationSession`
(Phase 3's own measure/decide split, extended so a threshold change never
repeats registration), and `services.calibration_service` (the four-state
validation verdict - passed / passed with warnings / needs review / failed -
and its documented, tested rules). A small additive template field
(`calibration`, `docs/TEMPLATE_FORMAT.md`) records a run and is invalidated by
a later geometry or settings change. Full detail: `development/PHASE_04_HANDOFF.md`;
operator procedure: `docs/calibration_workflow.md`.

**Exit criteria - met.** Overlay geometry matches the recognition engine's own
computed coordinates exactly (asserted, not merely visually checked); a
threshold change propagates to results, the overlay and the quality summary
without repeating registration; a deliberately mismatched template is reported
`CalibrationStatus.FAILED`, with no fields, answers or bubbles at all, rather
than a plausible wrong result - verified against a real scanned sheet as well
as synthetic ones. Calibrating a template against synthetic or even real
representative scans is **not** the same as validating Phase 3's real-world
accuracy at scale; that remains Phase 3's own open item.

---

## Phase 5 - Batch Scan Processing Pipeline

**Purpose.** Process a folder of scans reliably and resumably.

**Delivered as** two additive tables (`scan_batch`, `batch_scan`, migration 2)
and `services.batch_store` - the durable state, its recorder, the resume
selection, the crash repair and the template/settings compatibility guard -
plus the Scan page's Resume, Retry Failed, status filter and batch-state line,
and the main window's crash recovery on project open and stop-and-wait on close.
Scan import, the multiprocessing pool, per-sheet error isolation, progress and
the ETA already existed from Phase 3 and were **preserved unchanged**; the pool
gained only bounded submission. Full detail: `development/PHASE_05_HANDOFF.md`;
operator description: `docs/scan_workflow.md` §11-§13.

**Exit criteria - met.** Several hundred synthetic sheets process end to end
with per-sheet status; one broken sheet cannot abort a batch; original scans are
**provably** unmodified (hashed before and after, at two levels, including with
renaming on and a corrupt file in the list); results are persisted incrementally
and an interrupted or cancelled batch resumes without redoing finished work;
single-worker and multi-worker runs persist identical results in identical
order; the GUI stays responsive throughout, asserted by a timer that could not
tick if it were blocked.

**Not done.** No real examination-scale run - the largest measured batch is 48
real scans, and the ten-thousand figure remains a simulation of the progress
path. Storage failures are tested by injection, not against a real network
share or full disk; crash recovery is tested by simulating the state a crash
leaves rather than by killing a live process; Windows only. And Phase 5 makes a
batch *reliable*, not *accurate* - whether the values it durably recorded are
correct remains Phase 3's open item.

---

## Phase 6 - Conflict Detection & Human Resolution

**Purpose.** Put a human in the loop wherever the machine is unsure - without
losing what the machine saw.

**Delivered as** `domain.review` (the pure vocabulary - conflict types, states,
actions, reason codes, provenance), `services.conflict_policy` (the single
deterministic place where a recognition result becomes conflicts, reading the
engine's own `needs_review` and the template's own thresholds rather than
inventing new ones), `services.review_store` (the conflict and audit repository,
with one write path and provenance *projected* from the event fold rather than
stored), two additive tables and two immutability triggers (migration 3), and
the `gui.review` Resolve page - queue, three-view workspace, evidence panel,
decision panel and audit history dialog. The Scan page gained conflict detection
after a batch, a Review Conflicts button and an unresolved-export warning; the
CSV gained `value_source` and `unresolved_conflicts`; `ScanResult` gained
`source_transform`, which is how the original scan can be highlighted using the
engine's own inverse homography rather than a second calculation in the GUI.
Phase 5's multiprocessing, cancellation, resume and progress are **unchanged** -
detection runs in the coordinator, after the batch, never in a worker. Full
detail: `development/PHASE_06_HANDOFF.md`; operator description:
`docs/conflict_review.md`.

**Exit criteria - met.** Every conflict kind in the taxonomy is raised from a
real rendered sheet and is resolvable; a correction never overwrites the machine
value (including the special multi-mark form `B-D`); the audit history is
complete and append-only, enforced at the service surface, in the application
and by database triggers that abort an `UPDATE` or `DELETE` outright; a decision
survives a reopen with the superseded correction, its reviewer and its reason
intact; resolution state survives closing and reopening the project; a
correction without a named reviewer is refused; duplicate identifiers are
detected across the whole batch; and original scans remain byte-for-byte
unchanged by review.

**Not done.** No review session with real operators on a real batch - the
workflow has been exercised by automated GUI tests, a smoke suite and screenshot
inspection, all against synthetic sheets and the repository's single real
sample. The queue's performance at examination scale is reasoned from its SQL
(filtered, ordered and counted in the database; no image loaded for an
unselected row) and asserted on a synthetic ten-thousand-conflict batch, not
measured on a real one. And Phase 6 makes what the machine was *unsure* about
visible; a confidently wrong reading never reaches the queue at all, which
remains Phase 3's and Phase 4's problem.

---

## Phase 7 - Candidate & Attendance Reconciliation

**Purpose.** Reconcile scripts against who was registered and who attended.

**Delivered as** `domain.reconciliation` (the vocabulary - seven
classifications, five co-occurring issues, nine actions, nine reason codes, and
the absence-token rule), `services.candidate_import` (CSV and XLSX, column
mapping that reports ambiguity rather than guessing, and identifier
normalisation that cannot merge two candidates), `services.reconciliation` (one
pure deterministic function), `services.reconciliation_store` (roster import,
the idempotent reconciliation, the six operator actions), six additive tables
and two audit columns (migration 4), and the `gui.attendance` stage - roster
bar, summary, filterable table, detail panel and resolution. `review_store`
gained `effective_identifiers`, the single place Phase 7 learns what candidate
a sheet is now believed to belong to. The candidate/attendance sample workbook
is packaged inside the application and offered by a Save dialog. Phase 5's
multiprocessing and Phase 6's ledger are **unchanged** - the ledger was
extended with `entity_type`/`entity_id` rather than duplicated, with no
existing row rewritten. Full detail: `development/PHASE_07_HANDOFF.md`;
operator description: `docs/reconciliation.md`.

**Exit criteria - met.** Every script maps to exactly one registered candidate
or to an explicit reviewable exception, and every registered candidate has an
understandable state; unknown IDs and duplicate scripts are never dropped, and
a script set aside as a re-scan keeps its scan row, its result, its reason and
its audit trail; the imported attendance value, the machine's recognised ID and
every human decision remain independently traceable; a decision without a named
operator is refused; resolution state survives save/close/reopen; messy imports
produce usable corrective messages rather than crashes; and **no candidate
name, ID or mark appears in an application log**, asserted by 18 dedicated
tests, a grep and a smoke check.

**Not done.** No reconciliation of a real cohort against a real roster - every
test is synthetic or uses the repository's one real sheet, and a genuine
examination roster has its own column names, its own spelling of absence and
candidates who really are missing. No examination-scale run: matching is
asserted at 10,000 candidates but the largest real batch anywhere in the
project is 48 scans. Reconciliation is per batch, with no project-wide view.
The operator's identity is a name, not an account. And one privacy exposure is
documented rather than fixed: the Phase 3 pipeline logs each scan's *file
name*, so an office whose files are named by roll number has roll numbers in
its log.

---

## Phase 8 - Answer-Key & Scoring Engine

**Purpose.** Produce marks that can be defended.

**Delivered as** `domain.scoring` (the arithmetic, as pure functions over
value objects - the canonical answer string, the key, the policy, the five
question outcomes and `score_answers`), `services.answer_key` (reading a key
from text or from a scanned solution sheet through the *existing* recognition
engine, with validation that names the question), `services.scoring`
(eligibility - who can be marked, and why not), `services.scoring_store` (key
and policy revisions, verification, scoring and staleness), three additive
tables (migration 5), and the `gui.answer_key` and `gui.results` stages.
`review_store` gained `effective_set_codes` and `effective_answers`, so a paper
is never marked against the set or the answers the *machine* read when a
reviewer has said otherwise. **Every mark is an exact rational**, never a
float; rounding happens once, for display. Full detail:
`development/PHASE_08_HANDOFF.md`; operator description: `docs/scoring.md`.

**Exit criteria - met.** Results are reproducible from stored inputs: a result
records the answer string, the set, the exact **answer-key revision** and the
exact **scoring-policy revision**, and closing and reopening the project and
recomputing reproduces the same mark. The key used for each candidate is
recorded on the result itself and never inferred from whichever key is current
- an earlier revision is superseded rather than deleted, precisely so it can
still be named. Changing any score-affecting rule makes affected results
**stale** and recomputes them from authoritative inputs rather than adjusting
the stored number, which a test asserts by corrupting a stored score and
checking it is ignored. Every hand-calculated case in the brief passes exactly,
fractional penalties are never truncated, a withdrawn question pays every
response with no deduction, an absent candidate gets no mark rather than a
zero, and an unverified key produces no marks at all.

**Not done.** No examination has been marked with it - every test is synthetic
or uses the repository's one real sheet, and no operator has checked a mark
against a paper in front of them. No examination-scale run: the largest real
batch anywhere in the project is 48 scans. A key is not checked for
*correctness*; verification records who looked at it, which is a much weaker
claim. Scoring is per batch and per roster, so a cohort split across two
batches is marked twice. One policy applies to the whole paper - no
section-wise or per-question weights. And there is no result export: reports
are Phase 9.

---

## Phase 9 - Result Management & Reporting

**Purpose.** Deliver the outputs an examination office actually files.

**Delivered as** `domain.reporting` (competition ranking, the dynamic
`RANK.EQ` formula generator, spreadsheet-injection safety, readiness
vocabulary), `services.report_template` (reading a real institution's own
result/absentee workbook - header-row detection tolerant of decorative rows,
column mapping that reports ambiguity rather than guessing, reusing Phase 7's
own identifier normalisation), `services.report_readiness` (cross-checking a
template's roster against Phase 7/8's canonical state), the `reporting`
package's `excel.py` (Rollwise/Meritwise/Summary/Answer-Key/Processing-Log
generation into a *copy* of the template, never the original) and `pdf.py`
(a dependency-injected exporter abstraction over LibreOffice's headless
conversion), `services.report_store` (per-set template/layout persistence,
the append-only generation audit, and "regenerate, never patch"
orchestration), one additive migration (`report_template_association`,
`report_layout_config`, `generated_report`), and the `gui.reports` stage.
Full detail: `development/PHASE_09_HANDOFF.md`; operator description:
`docs/reporting.md`.

**Exit criteria - met.** Both Rollwise and Meritwise reports are produced from
a complete, reconciled and scored project and verified against the database -
every candidate the template lists appears, absentees are retained with their
canonical marker, present candidates carry the exact stored `final_score`, and
every rank is an Excel `RANK.EQ` formula whose semantics the application's own
`compute_ranks` is checked against directly, including the brief's own
`90, 88, 88, 85 -> 1, 2, 2, 4` example. Exports never overwrite an existing
file silently - a second generation writes `..._1`, proven by a dedicated
test. The original template is proven byte-for-byte unmodified by SHA-256
before/after every generation. Two independent sets, given deliberately
different templates and keys, are proven to produce workbooks that cannot
cross-contaminate.

**Not done.** No real examination office's own workbook has been reported on -
every test is synthetic or reuses this project's rendered sheets. PDF export
has no real-LibreOffice verification in the environment this phase was built
in (LibreOffice was not installed there); the exporter abstraction, its
availability detection and its orchestration are fully tested by dependency
injection, and the one test needing a real engine is present and correctly
skipped, not deleted or weakened. No Windows Excel COM PDF adapter. Reporting
remains per batch and per roster, following Phase 8's own limitation. No
examination-scale run - the largest real cohort in this project remains under
fifty candidates.

---

## Phase 10 - Integration, Recovery & Production Hardening

**Purpose.** Make the whole thing survive real, imperfect use.

**Deliverables.** Autosave and crash recovery; reprocessing of individual sheets
and whole batches; performance work on large batches; end-to-end validation from
template to report; database integrity checks and repair guidance.

**Major tests.** Full end-to-end run on a synthetic examination; kill-and-resume
at each stage; performance benchmarks on a large batch; integrity check detects a
deliberately damaged project.

**Exit criteria.** An interrupted examination can be resumed without data loss; a
complete run is reproducible; performance limits are documented.

---

## Phase 11 - Release, User Documentation & Packaging

**Purpose.** Ship something an institution can install and trust.

**Deliverables.** Complete user documentation with screenshots; Windows
installer/packaged build; release checklist; licence selection; upgrade and data
retention guidance; contribution guide for outside contributors.

**Major tests.** Installer produces a working application on a clean Windows
machine; documented procedures reproduce documented results; upgrade from the
previous version opens existing projects.

**Exit criteria.** A non-developer can install OMRFlow and complete an
examination following the user guide alone.
