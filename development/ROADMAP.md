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
| 2 | Template Data Model & Template Designer Core | Not started |
| 3 | Bubble Mapping & Recognition Engine | Not started |
| 4 | Template Calibration & Validation | Not started |
| 5 | Batch Scan Processing Pipeline | Not started |
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

**Deliverables.** Template designer canvas: load a reference sheet, draw and
resize zones, place markers, configure field types, edit bubble grids with live
overlay; template save/load through the existing service; template management
inside a project.

**Major tests.** Round trip designer -> `.omrt` -> designer; zone geometry edits
produce valid documents; invalid geometry is refused at the point of editing, not
at save; GUI tests for the canvas interactions that are testable without pixel
comparison.

**Exit criteria.** A complete template for a real sheet can be produced entirely
in the GUI and reloaded unchanged; the `.omrt` specification and its
documentation still agree.

---

## Phase 3 - Bubble Mapping & Recognition Engine

**Purpose.** Convert a canonical sheet plus a template into field values with
confidence.

**Deliverables.** `imaging.metrics` (per-bubble fill measurements);
`recognition.decide` (measurement -> marked/unmarked/ambiguous);
`recognition.fields` (numeric, alphanumeric, set code, question blocks);
explicit missing-mark and multiple-mark representation; confidence scoring.

**Major tests.** Synthetic sheets with known marks, including light, heavy,
partial, crossed-out, multiple and absent marks; threshold behaviour at the
boundaries; relative-darkness handling for uniformly light sheets; confidence
falls where a human would also hesitate.

**Exit criteria.** Recognition accuracy measured on the synthetic corpus;
ambiguity is never silently resolved; all thresholds come from the template, none
from code.

---

## Phase 4 - Template Calibration & Validation

**Purpose.** Let a user verify and tune a template against real scans before
processing a whole batch.

**Deliverables.** Test-scan workflow; diagnostic overlays (detected markers,
bubble windows, per-bubble scores); threshold adjustment with immediate feedback;
recognition quality summary for a sample.

**Major tests.** Overlay geometry matches computed bubble centres; threshold
changes propagate to results; a miscalibrated template is reported as such rather
than producing confident nonsense.

**Exit criteria.** An operator can tell whether a template is correct *before*
processing a batch, and has a documented procedure for doing so.

---

## Phase 5 - Batch Scan Processing Pipeline

**Purpose.** Process a folder of scans reliably and resumably.

**Deliverables.** Scan import that references files in place; worker-thread
execution with progress reporting; per-sheet error isolation; persistence of
scans and recognition results; resume after interruption.

**Major tests.** A batch with deliberately broken sheets completes and reports
them; progress and cancellation behave; results survive an interrupted run; the
GUI never blocks.

**Exit criteria.** A few hundred sheets process end to end with per-sheet status;
a failure in one sheet cannot abort the batch; original scans are provably
unmodified.

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
