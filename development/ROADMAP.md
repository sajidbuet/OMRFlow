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
| 6 | Conflict Detection & Human Resolution | Not started |
| 7 | Candidate & Attendance Reconciliation | Not started |
| 8 | Answer-Key & Scoring Engine | Not started |
| 9 | Result Management & Reporting | Not started |
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

**Deliverables.** Conflict queue; review interface showing the original sheet,
the normalised sheet, the highlighted field and the zoomed region with
alternatives; manual correction; append-only `AuditEvent` history.

**Major tests.** Every conflict kind is raised and resolvable; a correction never
overwrites the machine value; audit history is complete and append-only;
resolution state survives a reopen.

**Exit criteria.** Every ambiguous value is reviewable, and any final value can
be traced back to either the machine or a named correction with a reason.

---

## Phase 7 - Candidate & Attendance Reconciliation

**Purpose.** Reconcile scripts against who was registered and who attended.

**Deliverables.** Candidate/absentee import from CSV and Excel; matching of
recognised candidate ids to registered candidates; classification of unknown ids,
duplicate scripts, present-without-script and absent-with-script; resolution
workflow for each.

**Major tests.** Each classification is produced for a constructed data set;
duplicates and unknown ids are never silently dropped; imports with messy columns
fail with a usable message.

**Exit criteria.** Every script maps to exactly one candidate or to an explicit,
reviewable exception; no candidate data appears in logs.

---

## Phase 8 - Answer-Key & Scoring Engine

**Purpose.** Produce marks that can be defended.

**Deliverables.** Answer-key entry and recognition from solution sheets;
independent keys per question paper set; key verification before scoring;
scoring configuration (correct/incorrect/blank marks, negative marking toggle);
the scoring engine and `CandidateResult`.

**Major tests.** Scoring arithmetic against hand-computed cases including blanks,
multiples and absentees; negative marking on and off; per-set keys applied to the
right candidates; recomputation is deterministic.

**Exit criteria.** Results are reproducible from stored inputs; the key used for
each candidate is recorded; changing the scoring configuration recomputes rather
than patches.

---

## Phase 9 - Result Management & Reporting

**Purpose.** Deliver the outputs an examination office actually files.

**Deliverables.** Result review; ranking; roll-wise workbook including absentees;
merit-wise workbook; summary, answer-key and processing-log sheets;
user-editable Excel layout (headers, logo, fonts, page setup); PDF export.

**Major tests.** Generated workbooks open and contain every registered candidate;
ranking matches the stored ranks; absentees appear correctly; a customised
template produces the customised layout.

**Exit criteria.** Both report types are produced from a complete project and
verified against the database; exports never overwrite an existing file
silently.

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
