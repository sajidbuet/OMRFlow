# Current state

**Updated:** 2026-09-20
**Version:** 0.1.0.dev0
**Current phase:** Phase 3 (Recognition Engine v1) implemented, architecturally hardened, and measurable through developer testing tools (synthetic dataset generator + recognition benchmark); accuracy validation still pending a real dataset. Phase 4 (Template Calibration & Validation) implemented and tested; it makes Phase 3's own real-dataset validation safer and more systematic, but does not itself constitute that validation. Phase 5 (Batch Scan Processing Pipeline) implemented and tested: batches are now durable and resumable, and original scans are provably unmodified - but Phase 5 makes a batch *reliable*, not *accurate*, and says nothing about whether the values it recorded are correct. Phase 6 (Conflict Detection & Human Resolution) implemented and tested: every value the machine was unsure about is now reviewable, and every final value traces back to either the machine or a named human correction with a reason - but Phase 6 makes ambiguity *visible*, not *rarer*; a confidently wrong reading never reaches the queue, and no review session with real operators has been run. Phase 7 (Candidate & Attendance Reconciliation) implemented and tested: a candidate list imports from CSV or Excel, every script maps to exactly one registered candidate or to an explicit reviewable exception, and the imported value, the machine's reading and every human decision stay independently traceable - but no real cohort has been reconciled against a real roster, and Phase 7 accounts for scripts, not answers. Phase 8 (Answer-Key & Scoring Engine) implemented, independently audited and tested: answer keys are written or scanned, verified before use and versioned, and every mark records the exact key and policy revision that produced it and is recomputed - never patched - when an input changes. The audit found nine defects that the green suite had not, three of which produced quietly wrong marks; all are repaired and pinned by tests. But no examination has been marked with it, and a key is not checked for correctness. Phase 9 (Result Management & Reporting) implemented and tested: each set's own result/absentee workbook is the authoritative roster (order, Roll No., name, existing absentee markers), Phase 7/8 supply attendance and marks, generation never opens the original template for writing (proven by SHA-256), every present candidate's rank is an Excel `RANK.EQ` formula the application's own ranking is checked against directly, a readiness check blocks Final Export on any registered/template/score disagreement, and an existing output file is never silently overwritten. During its own testing this phase independently rediscovered the exact "modal dialog opened from an automatic worker-completion callback can hang the application" defect Phase 8's audit had already fixed once, in a brand-new page - both are now fixed. No real examination office's own workbook has been reported on, and PDF export (via LibreOffice) has no real-engine verification in this build environment, since LibreOffice was not installed there. Phase 10 (Integration, Recovery & Production Hardening) implemented and tested: content-hash provenance and duplicate-scan detection, project locking with an explicit never-automatic stale-lock override, a genuine SQLite read-only mode, SQLite-online-API backups with a manifest-written-last completeness guarantee, a database health check (integrity, foreign keys, schema version, unresolved exceptions, missing keys, backup presence, free disk space) with deliberately no repair path, reprocessing primitives that archive a sheet's superseded reading before resetting it, worker recycling and configurable OpenCV threads, a deterministic index-addressable 100,000-sheet synthetic stress-dataset generator, a headless benchmark CLI with telemetry, and a Project Health & Recovery dialog. Real, forced (not simulated) process terminations were run and resumed cleanly at 100, 1,000 and 10,000 sheets; full 100,000-sheet batch *registration* was executed and measured directly. The mandatory full-scale 100,000-sheet *processing* run and its 1%/25%/50%/75%/99% kill-and-resume acceptance matrix were not executed - an explicit, scoped deferral, not a technical limitation - and lazy Qt models for large result tables were not built, both disclosed in `development/PHASE_10_HANDOFF.md`.

Update this file at the end of every phase.

## What works

### Application shell and projects (Phase 0)

- The application starts (`python -m omr_scanner` or `omrflow`) and shows the
  main window with nine workflow stages, a status bar, File and Help menus.
- A project can be created: a folder containing `project.json`,
  `database.sqlite` and the seven standard sub-directories.
- A project can be closed and reopened, with its identity and metadata intact.
- An invalid project (plain folder, damaged or missing `project.json`, missing
  database, newer format version) is refused with a readable message; no
  traceback reaches the user.
- The project database initialises to schema version 1 through a recorded
  migration, and is refused if it was written by a newer build.
- `.omrt` templates can be loaded, validated and saved. The geometry that locates
  a bubble in a grid is implemented and tested.
- The recent-projects list, log level and default project folder persist between
  runs in the per-user configuration file.
- Logging: an application log plus a per-project log while a project is open.

### Geometric normalisation (Phase 1)

- `omr_scanner.imaging.align_sheet(image, config=...)` turns an arbitrary scan
  of an OMR sheet into the canonical page the template describes, at exactly the
  declared canonical size.
- The four printed registration squares are detected by combined shape evidence
  (area ratio, aspect ratio, rectangularity, solidity, interior ink) and chosen
  by a score that also accounts for position, through an exhaustive one-to-one
  assignment to the four scan corners.
- Page orientation (0/90/180/270 degrees) is resolved by rectifying the
  orientation mark's expected window out of the scan under each of the four
  hypotheses, after pruning those whose page geometry is implausible.
- Rotation, translation, scale, skew and perspective are corrected in one
  homography; the inverse is returned too, for future GUI overlays.
- The result carries measured quality metrics and non-fatal warnings, not a
  single opaque confidence.
- Every expected failure raises an `ImagingError` subclass with a stable code
  (`INVALID_IMAGE`, `INSUFFICIENT_MARKERS`, `AMBIGUOUS_MARKERS`,
  `ORIENTATION_NOT_FOUND`, `INVALID_PAGE_GEOMETRY`,
  `ALIGNMENT_TRANSFORM_FAILED`). A missing corner marker is never extrapolated.
- `omr_scanner.services.alignment_service` converts an `OmrTemplate` into an
  `AlignmentConfig` and reads and writes image files.
- Optional diagnostics render a detection overlay, a rectified preview and a
  textual summary; they are off by default and cannot affect the outcome.
- `omr_scanner.imaging.synthetic` generates synthetic canonical sheets with
  interior control points and applies seeded, reproducible distortions.
- Two developer tools: `python -m omr_scanner.tools.align_image` and
  `python -m omr_scanner.tools.make_test_sheet`.

**Measured accuracy** (synthetic, 40 distortion cases x 9 interior control
points, default 1240 x 1754 page): mean 0.062 px, 95th percentile 0.145 px,
maximum 0.394 px, zero failures. Regression threshold in the suite: 1.5 px.
All four page orientations resolved with confidence 1.00 and margin 1.00.
Alignment takes 12 ms for A4 at 150 dpi and 39 ms at 300 dpi.

### Interactive template designer (Phase 2)

- The "Template" workflow stage is a real page: load a reference sheet image;
  detect the four registration markers (reusing Phase 1's detector, scored per
  corner independently so a partially-damaged sheet still reports three good
  corners); drag markers and the orientation mark into place by hand;
  auto-detected geometry is visually distinct (amber) until confirmed (green).
- Draw regions on the canvas and configure them through a dialog: Student ID
  (numeric), Question Set (set-code), Questions (question-block, generated as
  one zone per printed column, so a 100-question/4-column block is four zones
  holding 400 individually-addressable bubbles, never one rectangle), Custom
  (alphanumeric), and a reference/ignored region.
- Select, drag, resize, duplicate, rename, delete, and show/hide any region;
  numeric X/Y/Width/Height editing in the properties panel, in both pixels and
  normalised fractions, kept in sync with the canvas both ways.
- Per-bubble fine-tuning: toggle "Edit Bubbles" on a selected region to drag
  one bubble's centre, recorded as a `BubbleOverride` (the existing Phase 0
  mechanism, not a new concept); reset per region.
- Undo/redo (one entry per completed gesture, not per mouse-move), coalesced
  by construction because the history is a stack of the immutable `OmrTemplate`
  itself, not a separate command hierarchy.
- Validation beyond what the schema already enforces: overlapping regions,
  regions overlapping a marker, duplicate or missing question numbers.
- Save/Save As/Open with dirty-state tracking and a title bar `*`; a template
  records its reference image's path (new, additive field
  `OmrTemplate.reference_image`) so reopening it restores the canvas exactly.
- Zoom (wheel, in/out, fit, 100%), pan (space+drag), an optional alignment
  grid overlay.
- The GUI never imports `cv2`/`numpy`: `omr_scanner.services.marker_detection_service`
  is the seam, handing the GUI plain bytes and floats.

**Example**: `examples/templates/100_question_4_choice_example.omrt` - a
7-digit student ID, A-D question set and 100 questions in 4 columns (474
bubbles total), built and validated through the real generator functions
(`omr_scanner.domain.template_authoring`), not hand-typed JSON.

### Bubble recognition and the Scan workflow (Phase 3)

- `omr_scanner.imaging.metrics` measures each bubble against a **locally**
  computed ink threshold - halfway between the paper level in an annulus around
  the bubble and the page's own ink level - so a printed option glyph inside an
  empty bubble does not read as a mark and a uniformly faint pencil sheet does
  not read as blank.
- `omr_scanner.recognition` turns those measurements into values with five
  explicit states (resolved, blank, multiple, uncertain, unreadable). Two marks
  are reported as `b-d` with both kept; an uncertain reading as `b?`; an
  unreadable group as `?`. Every threshold comes from the template's
  `RecognitionSettings`.
- Numeric identifiers are read per digit column and only offered as an
  identifier when *every* column resolved - `21?3123` is never silently turned
  into a plausible roll number. Leading zeros survive.
- Set codes are multi-position strings read from the template's own symbols:
  `10` is `"10"`, never `1` or the integer ten.
- `omr_scanner.services.recognition_service` reads one sheet end to end;
  `batch_processor` runs many and isolates per-file errors so one corrupt image
  cannot end a batch; `filename_manager` decides output names; `scan_export`
  writes a deterministic UTF-8 CSV.
- Roll-based renaming **copies** into an output folder, never moves or
  overwrites. Duplicates become `_a`, `_b`, ... `_z`, `_aa` (bijective base-26),
  counting files already in the output folder as taken. An unreliable roll
  number gets `UNRESOLVED_001` instead of a fabricated name.
- The "Scan" workflow stage is a real page (`omr_scanner.gui.scan`): template
  loading, file/folder import with natural sort, background batch processing
  with progress and cancel, a zoomable/pannable preview with a recognition
  overlay, a results panel, and CSV export. It imports no `cv2`, `numpy`,
  `imaging` or `recognition`, enforced by the same executable layering test as
  the rest of the GUI.
- **Multicore batch recognition.** `omr_scanner.services.parallel_batch` reads
  several sheets at once in a `ProcessPoolExecutor` (`spawn` on every platform),
  one complete page per worker; `batch_processor` keeps naming, copying and
  recording in the main process, in batch order, so results, duplicate-roll
  suffixes and CSV rows are identical on any number of cores. The worker count
  is a user setting (`omr_scanner.config.processing`: Automatic / Single core /
  Custom) edited in `File > Settings > Processing` and persisted in
  `omrflow.config.json`; automatic mode leaves one logical CPU free and stops at
  8 workers, a cap taken from measurement, not assumption.

**Measured throughput** (48 copies of the real sample, 16 logical CPUs /
8 physical cores, Windows 11): 3.44 scans/s on one worker, 11.63 on eight
(3.38x), then *down* to 10.02 on sixteen; peak resident memory 127 MB, 855 MB
and 1,607 MB respectively. Reproduce with
`python scripts/benchmark_batch.py --scans 48 --workers 1,2,4,8,12,16`.

### Recognition as a replaceable subsystem (Phase 3 hardening)

- `RecognitionEngine.process(image, template) -> ScanResult` is the one door
  into Phase 3; `recognise_scan()` remains as the function form. Nothing above
  that line touches a threshold, a contour or a homography.
- `ScanResult` is versioned (`engine_version`, `schema_version`), JSON
  round-trips, and carries the **evidence** behind every decision: per-bubble
  fill ratio, darkness, contrast, local paper level, the ink threshold applied,
  sample size and rank. A recalibration can therefore re-score a stored batch
  without re-reading an image.
- `status_codes` gives consumers machine-readable conditions instead of English
  prose; failures are results, never exceptions.
- Diagnostics (staged images, a headless overlay, the result as JSON) are
  opt-in from the GUI or the command line, and provably do not change what was
  recognised.
- `omr_scanner.evaluation` adds a QA layer above services: the ground-truth
  schema, a reproducible synthetic dataset generator driven by a real template,
  and a benchmark that classifies every disagreement and compares against a
  stored baseline.
- Three headless tools: `omr_scanner.tools.recognise`, `...make_dataset`,
  `...benchmark_recognition`. A test runs recognition in an interpreter where
  PySide6 is never imported.
- Eleven stored result fixtures in `tests/fixtures/recognition/` let Phases 4
  and 5 be written and tested with no engine present.

### Large-batch progress (Phase 3)

- `omr_scanner.services.batch_progress` counts terminal jobs and estimates the
  time remaining: an exponential moving average over half-second throughput
  samples, a warm-up during which it says "Calculating..." rather than
  extrapolating from one sheet, and an estimate that grows during a stall
  instead of freezing. Thread-safe, headless, and tested with an injected
  clock - including a 10,000-job simulation that runs in under a second.
- The Scan page shows a fixed-size panel whatever the batch length: bar,
  completed/total, percentage to one decimal, elapsed, remaining, throughput,
  estimated finish, and successful/review/failed tallies. A failed sheet
  advances the bar; 9,999 of 10,000 never reads as 100%.
- Counting happens once, in the parent process, under one lock; the window
  *pulls* a snapshot five times a second rather than being pushed one per
  completion. Scan-list rows are buffered the same way and located through a
  path index, so a long batch is linear rather than quadratic work.
- Cancellation disables its own button immediately, says so, lets sheets in
  flight finish rather than killing them mid-write, and reports both halves of
  what happened.
- Batch runs decline the per-bubble evidence they never read: 6.7 KB per sheet
  instead of 61.6 KB on the 100-question sample, about **68 MB rather than
  631 MB** across ten thousand sheets. Diagnostics keep it, because the
  diagnostic images are drawn from it.

**Measured on a synthetic mixed dataset** (12 sheets from the real sample's
template, seed 20260918): answer accuracy 0.951, roll and set code 1.000, blank
detection 1.000, double marks 1.000, borderline marks handled acceptably 19/19.
Every remaining error was a false blank, and attributing them by the mark style
that was drawn gives ticks 33%, circled bubbles 8%, crosses and scribbles 0% -
a coverage-based measurement under-reading line-shaped marks. That is a finding
for the real-dataset calibration, not a number to tune against.

**Measured on the real sample** (`examples/ECE-0000.png`, an actual scan with
handwritten marks, printed option glyphs and non-white paper): roll `00000000`,
set code `10` and all 100 answers read correctly, with zero fields flagged for
review. The gap between the faintest mark's fill ratio and the darkest unmarked
bubble's exceeds **0.5**. The same sheet still reads correctly after ±3°
rotation, exact 90/180/270° turns, 0.7x and 1.3x rescaling, translation,
perspective distortion, JPEG compression, and all of those combined.

### Developer testing tools (Phase 3)

- *Tools > Developer / Testing* offers **Generate Synthetic Test Dataset** and
  **Run Recognition Benchmark**, both also available as command-line tools and
  both entirely local - nothing is uploaded and no analytics exist.
- The generator renders from a real `.omrt` at **150 DPI derived from the
  template's physical page size** (A4 → 1240 × 1754 px), as PNG or JPEG, into
  `images/` + `ground_truth/` + `manifest.json` + `manifest.csv` +
  `dataset_summary.json`. One sheet is in memory at a time, so ten thousand
  sheets cost one page; generation is cancellable and a cancelled run's manifest
  says how much of the dataset exists.
- Nothing about the sheet is hard-coded. `FieldLayout` reads identifier length
  and symbols, set-code structure, question count, option labels, bubble size
  and page dimensions from the template, and tests assert that a nine-digit
  identifier, five options and a template with no set code all work unchanged.
- Sheets are **named test cases** carrying tags (about sixty, in thirteen
  families) that survive into the ground truth and then into the benchmark's
  category table. Profiles select families; each family's edge cases are emitted
  first, and a dataset too small to hold them all takes a spread across families
  rather than a prefix.
- Ground truth is derived from the marks in one place, and records the drawn
  marks alongside the expected string, so an ambiguous column can be re-judged
  without regenerating anything. Identifiers are fictional by construction.
- Benchmarking runs **inside the existing Scan page**, in benchmark mode - same
  batch architecture, same settings, same worker pool - and scores the run
  automatically when it ends. There is no second processing window.
- The report adds sheet-level and registration accuracy, per-test-case-category
  metrics, duplicate-identifier metrics kept separate from recognition
  correctness, a run configuration recording what produced the numbers, and a
  comparison against the previous run of the same dataset (headline metrics and
  every category).

**Measured on a 120-sheet mixed dataset** (100-question example template, seed
424242, 150 dpi PNG, 8 workers): 120 scans in 23.6 s, sheet accuracy 0.940,
answer accuracy 0.965, registration 116/116, blank detection 1.000, double
marks 1.000, borderline marks 34/34 handled acceptably, 4/4 planted duplicate
groups found with no unplanted collisions. Every one of the 402 remaining errors
was a false blank, and the category table attributes all of them to three
sheets: dot, slash and stroke mark styles. Measured fill ratios: filled bubble
0.93, stroke/slash 0.51-0.54 (flagged uncertain at the 0.55 threshold), dot 0.16
(read confidently as blank, below the 0.25 blank threshold). **A finding for the
real-dataset calibration, not a number to tune against**, and not evidence of
real-world accuracy.

### Template Calibration & Validation (Phase 4)

- A ninth workflow stage, **Calibrate**, between Template and Scan: load a
  saved template, add one or more representative real scans, and run the
  existing Phase 3 pipeline against them in diagnostic mode - never a second
  recognition engine.
- `RecognitionEngine.open_session()` returns a `CalibrationSession` that
  registers and measures a scan once; `session.recompute(template)` re-decides
  the cached measurements against a different `RecognitionSettings` with no
  file read, no marker detection and no perspective warp. A threshold slider
  therefore reclassifies, updates the overlay and updates the quality summary
  synchronously, on the GUI thread - measured at roughly 9 ms per recompute on
  a 100-question synthetic sheet, against 70-80 ms for the registration it
  does not repeat.
- The overlay draws `ScanResult.bubbles` and `ScanResult.markers` unchanged;
  `MarkerView` gained `canonical_x/y` (the detected marker reprojected through
  the fitted homography) and `expected_x/y` (the template's own declared
  centre), both already in canonical pixels, so "expected vs detected" is
  drawn from the same computation the engine used to rectify the page, not a
  second one.
- `services.calibration_service.evaluate_calibration` derives an explicit
  four-state verdict - passed / passed with warnings / needs review / failed -
  from signals Phase 3 already computes: registration status, alignment
  warnings (split into geometry-class and cosmetic), the fraction of bubbles
  whose sampling window could not be measured, and the fraction of checked
  positions needing review. A sample's status is the worst of its scans, never
  an average.
- **The fail-safe, proven rather than assumed.** A template whose registration
  markers are shifted past their `search_radius` fails to register; `ScanResult`
  for a failed registration carries no fields, answers or bubbles at all (true
  since Phase 3, by construction), so the calibration verdict can only ever be
  `FAILED` for it - never a plausible-looking wrong answer. Checked at the unit
  level, the integration level (synthetic), the GUI level, and against the real
  sample and template through `qtguitesting`.
- A small additive template field records a calibration run
  (`docs/TEMPLATE_FORMAT.md`, "`calibration`") and is invalidated the moment
  the template's geometry *or* its recognition settings move - tracked as two
  separate content hashes so the two kinds of change are distinguished. Every
  template saved before Phase 4 loads unchanged, simply "never calibrated".
- The Scan page shows a small, non-blocking warning when a loaded template has
  never been calibrated or has gone stale; it never blocks *Process All*.

- The overlay draws the **sampled** ellipse and the **printed** bubble as two
  separate, separately-labelled layers, plus a centre cross. They are
  genuinely different regions - the sampler reads the interior at
  `sample_radius_ratio` (0.62) of the printed half-axes, which on the real
  sample is 22.3 px across inside a printed 36.0 px bubble - and
  `BubbleMeasurement` now records the sampled extent at the moment it builds
  the mask so the two cannot drift apart. An audit pass corrected an earlier
  version that drew only the printed size, meaning an operator checking
  alignment was shown a region 2.6x the area of anything actually measured.

**Measured on the real sample** (`examples/ECE-0000.png`, its own template):
calibration status `Validation passed with warnings` (a pre-existing marker
alignment warning, not a Phase 4 finding), 4/4 markers detected within 0 px of
their expected canonical position, all 100 answers single-marked, 0 flagged,
marks detected in 110 of 110 response positions, 0 of 500 sampling windows
unusable.

**The two miscalibration shapes, both measured on that same real scan:**

- *Markers displaced* (every marker shifted 0.3 normalised): `Registration:
  FAILED`, `Markers detected: 0 / 4`, 0 fields/answers/bubbles, calibration
  status `FAILED` - not a degraded pass.
- *Only the zones displaced* (every zone and grid origin shifted 0.02
  normalised): registration **succeeds** cleanly, 0 of 500 sampling windows
  unusable, no alignment problem at all - and the status is `Needs review`
  with "marks detected in 2 of 110 response positions". This is the case that
  no registration-level check can see, and the one an audit pass found
  reporting `Validation passed with warnings`; it is now covered by an
  explicit rule (`NO_MARKS_DETECTED`) with no tunable constant, plus the
  pre-existing systematic-ambiguity rule for partial displacement.

### Durable batch processing (Phase 5)

- Two additive tables, created by **migration 2**: `scan_batch` (one run: its
  folder, template, both template fingerprints, engine version, settings and
  lifecycle status) and `batch_scan` (one row per sheet: status, attempt count,
  recognised roll and set code, output name, failure reason and machine-readable
  category, timings, and the full `ScanResult` as JSON). Field-by-field:
  `docs/DATA_MODEL.md`.
- `services.batch_store` is the repository layer. It contains no Qt and no
  recognition, and is attached to a run through `process_batch`'s **existing**
  `on_result` hook - so a caller with no project open (a test, the benchmark,
  `python -m omr_scanner.tools.recognise`) runs exactly the code path it always
  did. `batch_processor` still knows nothing about a database.
- Results are committed in **groups** - 25 sheets or 2 seconds, whichever comes
  first - because one `fsync` per sheet dominates a run on a spinning disk or a
  synchronised folder. That bounds what an abrupt termination costs to a second
  or two of finished work, and the bound is asserted by a test rather than
  merely intended.
- **Only the coordinating process writes.** Workers return recognition results
  and nothing else - the same rule that already stopped them naming files. SQLite
  is single-writer and the architecture keeps it that way by construction.
- **Resume** processes only `pending`/`queued`/`processing`/`cancelled` rows, in
  batch order, so duplicate-identifier suffixes stay stable across an
  interruption. **Retry Failed** targets failures and only those, incrementing
  each scan's attempt count.
- Opening a project runs `recover_interrupted`: rows left `queued` or
  `processing` can only be in those states while some process owns them, and a
  project being *opened* proves none does. They return to `pending` - never to
  `failed`, because "we do not know what happened to this sheet" is not "this
  sheet is bad", and marking it failed would silently exclude exactly the sheets
  a crash caught.
- Resuming with an edited template or retuned thresholds compares the stored
  fingerprints against the current ones and says precisely what changed before
  asking. The fingerprints are the same ones Phase 4 uses for calibration
  staleness.
- A **storage** failure is handled separately from a **recognition** failure:
  the batch continues, the buffer is kept for a later retry, and the page
  reports it in a dialog. A run whose results could not be written is never
  presented as a clean success.
- `parallel_batch` gained bounded submission (4 tasks per worker) so a
  ten-thousand-sheet batch no longer builds ten thousand futures before reading
  the first page. Nothing else about the pool changed.

**Measured** (24 copies of `examples/ECE-0000.png`, 16 logical CPUs):
1 worker 8.02 s / 2.99 scans-s; 2 workers 6.15 s / 1.30x; 4 workers 4.47 s /
1.79x; 8 workers 4.05 s / 1.98x. All 24 read at every worker count -
multiprocessing has not silently fallen back to sequential.

### Conflict detection and human review (Phase 6)

- Two additive tables, created by **migration 3**: `review_conflict` (one thing
  on one sheet to look at, with the machine's observation snapshotted for
  querying) and `audit_event` (the append-only ledger). Identity is
  `(batch_id, scan_id, conflict_type, zone_id, group_key)` under a unique
  constraint, so Phase 5's resume and retry **update** conflicts rather than
  duplicating them. Field-by-field: `docs/DATA_MODEL.md`.
- `domain.review` holds the vocabulary — 22 conflict types, four states, seven
  actions, nine reason codes, `FieldRef`, `MachineObservation`, `Provenance` —
  with no Qt, no SQLAlchemy and no OpenCV in it. The enums carry their own rules
  as properties (`is_processing_failure`, `allows_value_correction`,
  `sets_effective_value`, `requires_text`), which is what lets the GUI build a
  type filter and the exporter read a provenance without either importing the
  other.
- `services.conflict_policy` is the single deterministic place a result becomes
  conflicts. **It contains no thresholds.** It reads the `needs_review` flag
  `recognition/decide.py` already computed from the template's own
  `ambiguity_margin` and `min_confidence`, so calibrating a template in Phase 4
  moves the conflict queue with it.
- Blank answers are **not** flagged by default (a candidate may leave a question
  blank, and one row per unanswered question would bury the real conflicts);
  alignment warnings are **not** (the repository's own sample raises one on every
  sheet); an assumed orientation **is** (an inverted sheet read as upright
  produces a full set of confidently wrong answers). All three are policy flags,
  not constants.
- `services.review_store` has **one write path for events** and no update or
  delete for them. Every human action is one transaction: the audit event and
  the state change commit together or neither does.
- **The final value is projected, not stored.** There is no `resolved_value`
  column; `provenance_for` folds a conflict's ordered events over the machine's
  reading. Reopening a decision restores the machine's value while keeping the
  superseded correction, its reviewer, its reason and its timestamp in the
  record. `ReviewConflict.state` *is* cached for the queue's sake, and
  `recompute_state()` proves it equal to the fold.
- **Append-only, enforced three ways**: nothing on the service surface, nothing
  in the application, and two SQLite triggers that `RAISE(ABORT)` on any
  `UPDATE` or `DELETE` of `audit_event`. The table carries **no foreign key**, so
  a decision outlives the row it was about and Phases 7-9 can audit into it
  without a schema change.
- The **Resolve** stage: a queue filtered by state, type and a search over
  student ID and file name; a workspace showing the disputed bubbles zoomed, the
  whole normalised sheet, and the original scan; an evidence panel reporting
  each option's measured **fill score** (a coverage measurement, labelled as
  such, never a "probability"); and Accept / Correct / Defer / Reopen, each
  requiring a named reviewer and each correction a reason.
- `ScanResult.source_transform` carries the engine's own inverse homography, so
  the original scan can be located from the engine's geometry rather than a
  second calculation in the GUI — which may not import OpenCV or NumPy anyway.
  The original view therefore carries a **note**, not an overlay.
- Review re-reads the one selected sheet in a `QThread`, because
  `keep_bubble_measurements` is off for batches (most of a gigabyte over ten
  thousand sheets). Recognition is deterministic, so the evidence reproduces
  exactly; the worker starts only when the sheet actually changes.
- Detection runs **in the coordinator after the batch**, never in a worker: a
  worker must not open the single-writer database, and a duplicate identifier is
  not a property of one sheet. **Phase 5's pipeline is unchanged.**
- CSV export gained `value_source` and `unresolved_conflicts`, appended after
  the existing columns. Exporting with conflicts open warns and states the
  count; it does not block.

**Measured**: 10,000 conflicts in one batch — a queue page costs a bounded
number of SQL statements and the summary a bounded number of grouped queries,
asserted by counting statements rather than by timing one machine.

### Candidate & attendance reconciliation (Phase 7)

- **Four values kept apart on purpose**: what the roster file said
  (`registered_candidate`, write-once), what recognition read
  (`machine_candidate_id`, never overwritten), what an operator decided
  (`reconciliation_decision` + `audit_event`), and the effective value, which
  is the only one computed. Overriding an attendance or reassigning a script
  can never cost the record of what it changed *from*.
- Six additive tables, created by **migration 4**, plus `entity_type` and
  `entity_id` on `audit_event` so a decision about a candidate or a script goes
  into the **same append-only ledger**, under the same triggers, as a decision
  about a recognition conflict. Existing audit rows were **deliberately not
  backfilled** - an `UPDATE` there is aborted by those triggers, so the new
  column's default was chosen to be already correct for them. Field-by-field:
  `docs/DATA_MODEL.md`.
- `services.candidate_import` reads CSV and `.xlsx` (via `openpyxl`; **pandas
  deliberately not used** for one pass over a spreadsheet). Worksheet
  selection, a preview of the real file, and a column mapping the operator
  confirms - **Candidate ID** required, **Name** and **Marks / Attendance**
  optional.
- A marks column doubles as attendance: `ABSENT`/`ABS` in any case and spacing
  means absent, anything else - including a blank cell - does not. Compared as
  **whole tokens**, so `ABSENTEE`, `ABSENCE` and `ABS123` are not absences, and
  the column name is never hard-coded.
- **Candidate IDs are identifiers, not quantities.** `15000001` imports as
  `"15000001"`, never `"15000001.0"`; a non-integral value is not rounded,
  because rounding is how two candidates become one.
- **It refuses to guess.** Two columns that equally name a candidate ID stop
  the import and ask; a repeated candidate ID stops it with the ID and both row
  numbers. A failed import leaves nothing behind.
- `services.reconciliation` is **one pure deterministic function** - no clock,
  no config, no database - so the rules are testable as a table and a re-run is
  idempotent. The stored entry and script rows are a **cache** of it, rewritten
  wholesale so a stale classification cannot survive a roster change; operator
  decisions are the **input** and live in their own table.
- Seven classifications, five of them exceptions, and **an entry carries a set
  of issues rather than one status** - a candidate marked absent with two
  scripts is both, and a schema holding one would make a physical script
  invisible.
- **`UNRESOLVED_CANDIDATE_ID` is its own state**, so a roll number still
  awaiting Phase 6 review is never reported as an unknown candidate. The two
  need different actions.
- Resolution never destroys: a script set aside as an accidental re-scan keeps
  its scan row, its recognition result, its reason and its audit trail. Every
  decision needs a named operator and a reason, and **re-runs reconciliation
  immediately** so a cascading duplicate is surfaced rather than discovered at
  export time.
- `review_store.effective_identifiers` is the single place Phase 7 learns which
  candidate a sheet is now believed to belong to, so reconciling against the
  machine's own reading is impossible by construction.
- The **Attendance** stage: roster bar, live summary, a table filtered and
  counted in SQL, a detail panel listing every script including set-aside ones,
  the entry's history, and the decision panel. Import and reconciliation both
  run off the GUI thread.
- The candidate/attendance sample workbook is packaged **inside** the
  application (`src/omr_scanner/resources/templates/`) and read through
  `importlib.resources`, so *Download Sample Template…* works in a wheel and a
  frozen build. The top-level `resources/` directory is dev fixtures and is
  never shipped.
- **Phases 5 and 6 are untouched.**

**Measured**: 10,000 candidates against 10,000 scripts reconcile in well under
a second - an indexed match, not a per-script walk of the roster.

### Answer keys and scoring (Phase 8)

- **`domain.scoring` is pure arithmetic** - the canonical answer string, the
  key, the policy, five question outcomes and `score_answers`, which reads no
  clock, no configuration and no database. That is what makes "recompute,
  never patch" a property a test can assert.
- **Every mark is a `fractions.Fraction`**, never a float. `Decimal` was
  rejected because `1/3` has no terminating decimal expansion and the 1-per-3
  rule is a published marking scheme; three hundred thirds sum to exactly 100,
  which a test asserts. Rounding happens once, at the end, for display.
- **One independent answer key per set**, typed, pasted or read off a solution
  sheet through the existing recognition engine. Set codes are not assumed to
  be one character, validation names the question, and a stray character is
  reported rather than dropped.
- **Verification before scoring, and revisions that are never edited.**
  Correcting a verified key creates the next revision and supersedes the old
  one - which is kept, because results point at it. A key read off a solution
  sheet is still a draft: recognition completing does not make a key right.
- **Wrong questions**, flagged per set, pay every response in full - right,
  wrong, multiple or blank - with no deduction. The rule is checked first,
  above the blank and multiple rules.
- **Four negative-marking modes**, with fractional penalties never truncated:
  one wrong answer under the 1-per-3 rule costs exactly `1/3`.
- **A result stores its inputs, not just its mark**: the answer string, the
  machine's own string, the set, the key revision and the policy revision. The
  per-question breakdown is **regenerated** by the same pure function, so a
  detail view and a total cannot disagree - and a million-row breakdown table
  that could is avoided.
- **An absent candidate has no mark**, not a zero.
- **Staleness and recomputation.** Changing a key, a rule, an answer, a set or
  a reconciliation makes a result stale; it keeps its mark and says so, and
  recalculating runs the whole scorer again. A test corrupts a stored score and
  asserts the rescore ignores it. A cancelled run writes nothing.
- Three additive tables, created by **migration 5**, with marks stored as exact
  rational strings. Field-by-field: `docs/DATA_MODEL.md`.
- **Phases 5, 6 and 7 are untouched.**

### Result management and reporting (Phase 9)

- **A set's own result/absentee template is authoritative for its roster** -
  order, Roll No., name and existing absentee markers are the template's;
  Phase 7/8 supply attendance and marks. Nothing in Phases 1-8 otherwise
  records which set an absent candidate (who has no script) was assigned to,
  which is why the template is mandatory rather than a convenience.
- **`domain.reporting` is pure**: standard competition ranking
  (`compute_ranks`), a dynamic `RANK.EQ` formula generator (the marks column
  and the row range are always derived, never hard-coded), spreadsheet-
  injection-safe text, and the readiness-issue vocabulary.
- **`services.report_template`** reads a real workbook - header-row detection
  tolerant of decorative title rows above the real header, column mapping
  that reports ambiguity (two equally plausible Roll No. or Marks columns)
  rather than guessing, reusing Phase 7's own identifier normalisation.
- **`services.report_readiness`** cross-checks the template's roster against
  Phase 7's reconciliation and Phase 8's scoring: a template candidate not in
  the project, a registered candidate missing from the template, a duplicate
  Roll No., an absentee-status mismatch, a present candidate with no score, an
  unresolved exception - every one a named, addressable issue, never silently
  dropped.
- **The original template is never opened for writing.** Generation copies
  its bytes first; a test hashes the file with SHA-256 before and after and
  asserts they match.
- **`reporting.excel`** builds Rollwise (populated in the copy, in place),
  Meritwise (mark descending, Roll No. ascending as a deterministic
  tie-break), Summary, Answer Key and Processing Log, plus layout
  (header/logo/font/page setup) that changes nothing when left at its
  defaults.
- **`reporting.pdf`** is a dependency-injected exporter abstraction over
  LibreOffice's headless conversion - the engine that actually recalculates
  the `RANK.EQ` formulas before rendering. Reports plainly, never pretends to
  succeed, when no engine is available.
- **`services.report_store`** persists per-set templates and layout,
  records an append-only `GeneratedReport` audit row per attempt (including
  failures), and enforces **regenerate, never patch**: every generation
  rebuilds the whole workbook from Phase 7/8's current stored state.
- **An existing output file is never silently overwritten** - a second
  generation writes `..._1`.
- One additive migration (**migration 6**): `report_template_association`,
  `report_layout_config`, `generated_report`. Field-by-field:
  `docs/DATA_MODEL.md`.
- **An adversarial defect found and fixed during this phase's own testing**:
  `ReportsPage._on_generated` - a worker-completion callback - opened a modal
  `QMessageBox` for the routine case of a blocked or failed set, which no
  automated or headless context could dismiss. The identical defect class
  Phase 8's own audit had already fixed once (`ResultsPage._on_scored`),
  rediscovered independently in a new page. Fixed with an inline status
  label.
- **Phases 1-8 are untouched.**

## What does not exist

There is no batch-browser dialog: `adopt_batch` and `list_batches` exist and
are tested, but nothing in the UI lists previous batches to pick from yet. The
conflict queue and the reconciliation are both **per batch** — there is no
project-wide view of either, and a cohort split across two batches must be
reconciled twice; reporting is per batch and per roster for the same reason.
Reviewer identity is a name, not an account: there is no authentication, so the
ledger records who *said* they made a decision. There is no Windows Excel COM
PDF adapter - PDF export requires LibreOffice. The Scan, Results, Resolve and
Attendance tables are item-based `QTableWidget`s, not lazy Qt models - a
disclosed Phase 10 gap against a 100,000-row *display* specifically (see
`development/PHASE_10_HANDOFF.md`); the backend was verified at that scale
independently of the GUI. A forced kill of the batch coordinator leaves its
worker processes running, orphaned, until found and terminated by hand -
data is never affected (workers never had database write access), but
nothing cleans them up automatically yet.

**This build must not be used for examination processing.** Its recognition has
been validated against one real sheet and geometric variants of it, not against
a corpus of independently filled papers.

## Known limitations

### Alignment (the ones that matter)

- **No real-world validation has been performed.** Every accuracy number above
  is synthetic. No real scan has ever been processed. This is the largest open
  risk in the project.
- Arbitrary rotation is corrected up to ±15 degrees, plus exact quarter turns.
  Beyond that the markers leave the corner search regions; widening
  `corner_search_width`/`_height` to 1.0 handles 20-75 degrees at the cost of no
  positional filtering.
- The default Otsu threshold fails on an illumination gradient beyond 0.45;
  `adaptive_mean` survives to 0.95 but produces many more spurious contours.
- Cropping beyond about 5 per cent of the page width into the margin fails, by
  design.
- Only square markers are exercised; `filled_circle` would pass the filters by
  accident rather than by design, and `shape` is not consulted.
- One sheet per call, single-threaded. Batch parallelism is Phase 5.

The measured degradation boundary for blur, noise, brightness, illumination,
perspective, JPEG compression and cropping is tabulated in
`docs/IMAGE_PROCESSING.md` § 20.

### Template designer

- **No real printed sheet has been used with the designer.** Every functional
  check (including the required 12-step demonstration,
  `docs/testing/phase_02_demo.md`) used a synthetic reference image.
- No snap-to-grid while dragging (the pure function exists and is tested; not
  wired into interactive dragging yet).
- No align-left/right/top/bottom or distribute tools for multiple selected
  regions.
- Individual-bubble override reset is per-region (the "Reset" action), not
  per-bubble.
- Documentation screenshots could not be captured meaningfully in this
  environment: the offscreen Qt platform plugin used here renders every label
  as a placeholder box rather than a glyph (a font-backend limitation of that
  plugin, not of the application). See
  `docs/development/phase_02_implementation_notes.md`.

### Scan and recognition

- **Validation rests on one real sheet.** `examples/ECE-0000.png` plus
  geometrically distorted copies of it, and synthetic pages. A corpus of
  independently filled papers - light pencil, crossed-out answers, erasures,
  smudges, different candidates' handwriting - has never been processed. This is
  the largest open risk in Phase 3, and it is the same category of risk Phases 1
  and 2 recorded.
- **Thresholds are uncalibrated and `confidence` is uncalibrated.** What the
  engine reports is a bounded *decision score*, not a probability. The benchmark
  reports accuracy by score band so that it can be checked - once there is data
  to check it against.
- **Line-shaped marks are under-read.** A tick covers little of a bubble's
  interior and the measurement is coverage-based; on synthetic data that is a
  33% false-blank rate for ticks against 0% for crosses. Whether real ticks
  behave the same way, and whether the answer is a darkness term or a different
  threshold, is a question for the real corpus.
- **Manual correction covers flagged values only** (Phase 6). Anything the
  engine was unsure about is reviewable and correctable; a value it read
  *confidently and wrongly* never enters the queue, so there is no way to
  correct one except by noticing it some other way. Narrowing that gap is a
  recognition problem, not a review one.
- **No PDF input.** Deliberate - the brief ruled out adding a dependency for it
  in this phase.
- **Renaming only copies.** There is no move/rename-in-place mode.
- Recognition is single-threaded *within* a sheet; parallelism is across sheets
  only, so a batch of one gains nothing from extra workers.
- "Cancel" stops after the sheets in progress, not instantly: OpenCV will not be
  interrupted part-way through a warp, so with N workers up to N more sheets
  finish after Cancel is pressed.
- Worker start-up is not free. Each worker is a fresh interpreter importing
  NumPy and OpenCV, so a handful of sheets can be *slower* on four workers than
  on one; the benefit begins at a few dozen.
- The multicore path has only been exercised on the 16-thread Windows
  development machine. Other core counts, memory limits and platforms
  (`spawn` on Linux and macOS) are untested.
- **No ten-thousand-scan run has been timed end to end.** The architecture
  targets that scale, and the counting, estimation and interface paths are
  exercised by a 10,000-job simulation, but the largest *real* batch measured
  is 48 scans. No performance limit is claimed.

### Conflict review

- **No review session with real operators on a real batch.** Everything is
  automated: 147 tests, three smoke checks and four screenshots. The workflow
  has never been driven by someone reviewing sheets they cared about, which is
  the only way to learn whether the queue is *usable* rather than merely
  correct.
- **Queue performance is asserted, not measured at scale.** 10,000 conflicts,
  by counting SQL statements rather than timing a machine. The per-sheet re-read
  on selection has been measured only on the development machine.
- **The policy defaults are reasoned, not evidenced.** Whether an examination
  office wants blank answers flagged, or alignment warnings surfaced, is not yet
  known; both are `ConflictPolicy` flags rather than constants, so the question
  is answerable without a code change.
- **Reviewer identity is a name, not an account.** No authentication, so the
  ledger records who *said* they made a decision. Deliberate - the brief ruled
  out building an auth system - but a real limitation for a high-stakes
  deployment.
- **The ledger is append-only, not tamper-proof.** Triggers stop the
  application and a careless hand-written statement; a database administrator
  with file access can still alter the file. Cryptographic chaining was
  explicitly out of scope.
- **The queue is per batch.** There is no project-wide "every unresolved
  conflict" view, and no way to review two batches together.
- **Phase 6 does not bound the error rate.** It makes the machine's *uncertainty*
  actionable. Its usefulness is therefore capped by how honest that uncertainty
  is, which is Phase 3's open item and Phase 4's calibration tooling.

### Candidate reconciliation

- **No real cohort has been reconciled against a real roster.** Every test is
  synthetic or uses the repository's one real sheet. A genuine examination
  roster has its own column names, its own spelling of absence and candidates
  who really are missing; none of that has been seen.
- **No examination-scale run.** Matching is asserted at 10,000 candidates
  against 10,000 scripts, but the largest *real* batch anywhere in this project
  is 48 scans.
- **A scan file named after a roll number still reaches the log.** The Phase 3
  pipeline logs each scan's file name - documented, and the only way to tell
  which sheet failed - so an office whose files are named by roll number (which
  is what OMRFlow's own rename step produces) has roll numbers in its
  application log. Phase 7 puts none there itself. Recorded in
  `docs/reconciliation.md` §9 and pinned by a test; fixing it means logging a
  scan id instead and costs the diagnostic for headless runs.
- **Reconciliation is per batch**, with no project-wide view.
- **`.xls` is not supported**, by decision - it would need another dependency
  for a format Excel has discouraged for fifteen years.
- **Leading zeros in a *numeric* Excel cell cannot be recovered.** Documented;
  the column must be formatted as Text before saving.
- **Phase 7 says nothing about whether an answer is right.** It accounts for
  scripts and candidates; scoring is Phase 8.

### Scoring

- **No examination has been marked with it.** Every test is synthetic or uses
  the repository's one real sheet. No real cohort, no real answer key, and no
  operator checking a mark against a paper in front of them.
- **No examination-scale run.** Scoring is arithmetic and fast, but the largest
  *real* batch anywhere in this project is 48 scans.
- **A key is not checked for correctness.** Verification records who looked at
  it - a much weaker claim, and deliberately so, because nothing in software
  can do better.
- **Scoring is per batch and per roster.** A cohort split across two batches is
  marked twice, with no combined view.
- **No result export.** Reports are Phase 9.
- **One policy per paper.** Section-wise or per-question mark weights are not
  supported; the model would take a policy attached to a question range without
  changing the result shape.
- **No manual mark override**, by design: a mark is recomputed, never edited,
  so there is no human decision about a *mark* to audit. The decisions that
  affect one are already audited where they are made.

### Elsewhere

- The example template in `resources/templates` is illustrative. Its coordinates
  have never been calibrated against a printed sheet. A second, larger example
  (`examples/templates/100_question_4_choice_example.omrt`, Phase 2) is built
  and validated through the real generator code, but is likewise synthetic.
- Project metadata is written once at creation; nothing updates `modified_at`
  yet for a *project* (a template's `modified_at` is bumped on every save,
  since Phase 2 added that).
- The GUI is functional but visually plain: no icons, no theming, no window
  geometry persistence.
- No packaging or installer; the application runs from a source checkout.
- `resources/icons` is reserved and empty.

## Test status

3332 tests passing, 2 skipped, plus 1 explicitly-run large-scale test not
part of the default suite (Python 3.12.7, PySide6 6.11.2, OpenCV 5.0.0,
NumPy 2.5.3, psutil 7.2.2, Windows 11).

```text
pytest (default set)     3332 passed, 2 skipped, 1 deselected
pytest -m stress         1 passed (10,000-sheet real forced-kill/resume)
ruff check .             All checks passed
mypy                     Success: no issues found in 150 source files
run_gui_smoke_tests      52/52 checks passed
```

314 of those are Phase 8 (Answer-Key & Scoring Engine):
`tests/unit/test_scoring.py` (96, the arithmetic as a table - the brief's
hand-calculated cases, every negative-marking mode, wrong-question precedence,
clamping, exactness, whole-paper extremes and determinism field by field),
`tests/unit/test_answer_key.py` (43, reading a key and every refusal *with its
message*), `tests/unit/test_scoring_store.py` (77, revisions, verification,
staleness, recomputation-never-patching, the Phase 7 eligibility matrix,
provenance across a reopen, set-key contamination and the migration),
`tests/integration/test_scoring_workflow.py` (23, the acceptance scenario with
real recognition over real rendered sheets) and
`tests/gui/test_scoring_pages.py` (72), plus three in
`tests/unit/test_candidate_privacy.py` keeping marks and answer strings out of
the log. Confirmed end-to-end through `scripts/run_gui_smoke_tests.py` (48/48,
including a draft key producing no marks, the acceptance outcomes with the key
revision recorded, a rule change making results stale, recomputation ignoring a
deliberately corrupted mark, and a withdrawn question paying every response).

Four defects were found by inspecting the four screenshots
`scripts/capture_gui_states.py --only scoring` produces and by an **independent
adversarial audit** of the phase against its brief. The audit found nine, none
of which any existing test caught - the suite was green before and after it.
The worst were silent: a key written for a differently numbered paper withdrew
no questions at all, a template edited after recognition cost candidates the
multiple-answer deduction, and a candidate recorded absent whose script had
turned up was filed as a settled "Absent". Detail in
`development/PHASE_08_HANDOFF.md` §11a.

179 of those are Phase 9 (Result Management & Reporting):
`tests/unit/test_reporting.py` (65, ranking - including the brief's own worked
example and a 20,000-candidate timing check - the `RANK.EQ` formula generator,
spreadsheet-injection safety, safe filenames, the total-marks header,
readiness vocabulary), `tests/unit/test_report_template.py` (35, the sample's
own structure and every documented variation - a different sheet name, a
header on row 3+ with decorative rows above it, `ABS`/lowercase `absent`,
leading-zero rolls, Unicode names, duplicate rolls, malformed workbooks),
`tests/unit/test_excel_report.py` (34, template preservation by SHA-256,
Rollwise/Meritwise/Summary/Answer-Key/Processing-Log content, layout page
setup and graceful degradation, missing/real logo degradation),
`tests/unit/test_pdf_export.py` (8, one
conditionally skipped - the exporter abstraction, fully testable without a
PDF engine, plus a real-LibreOffice test that runs only where one is
installed), `tests/integration/test_report_generation.py` (25, the phase
brief's six acceptance scenarios end to end against real Phase 7/8 services,
plus the schema-6 migration onto an existing Phase 8 project)
and `tests/gui/test_reports_page.py` (12). Confirmed end-to-end through
`scripts/run_gui_smoke_tests.py` (52/52, including associating a template,
generating an XLSX with every candidate row and a working rank formula, and a
second generation never overwriting the first).

During its own testing this phase independently rediscovered the exact "a
modal dialog opened from a worker-completion callback can hang the
application indefinitely" defect Phase 8's own audit had already fixed once
(`ResultsPage._on_scored`) - this time in `ReportsPage._on_generated`, for the
routine case of a blocked or failed set in a multi-set generation run.

130 of those are Phase 10 (Integration, Recovery & Production Hardening):
`tests/unit/test_scan_provenance.py` (18, streaming SHA-256 hashing, exact-
duplicate detection, availability classification, verified relinking),
`tests/unit/test_project_lock.py` (10), `tests/unit/test_project_backup.py`
(11, manifest-written-last completeness, tamper detection, never-overwrite
restore), `tests/unit/test_project_health.py` (13, including a database
damaged at the byte level, not only a synthetic flag),
`tests/unit/test_readonly_defaults.py` (2), `tests/unit/test_stress_dataset.py`
(18, determinism, distribution, the exact-duplicate and duplicate-roll case
kinds, a Windows-path round-trip regression test),
`tests/unit/test_telemetry.py` (9), `tests/integration/test_production_hardening.py`
(2), `tests/integration/test_reprocessing.py` (8, real recognition, archived
history, one-sheet isolation), `tests/integration/test_stress_runner.py` (8,
bounded chunk materialisation, permanent-identity rewriting, resume),
`tests/integration/test_stress_kill_resume.py` (2 in the default run - 100
and 1,000 real forced-kill sheets - plus 1 marked `stress`, 10,000 sheets,
also passing), `tests/gui/test_health_dialog.py` (13), and 15 additions to
existing files (locking/read-only in `test_project_service.py`, the
lock-conflict dialog split in `test_main_window.py`, worker recycling and
OpenCV threads in `test_parallel_batch.py`, the Advanced settings section in
`test_processing_settings_gui.py`). Confirmed end-to-end through
`scripts/run_gui_smoke_tests.py` (52/52, unchanged - Phase 10 added no new
smoke checks of its own this pass) and by genuine, forced process
terminations of the real CLI at 100, 1,000 and 10,000 sheets, each verified
to lose nothing and duplicate nothing on resume.

This phase's own testing found and fixed two genuinely new defects (a
Windows-incompatible virtual-path scheme, and a project-lock never released
by the benchmark CLI) and one platform limitation that changed the worker-
recycling design entirely: `ProcessPoolExecutor`'s own `max_tasks_per_child`
parameter was found, in a minimal reproduction with no OMRFlow code
involved, to hang permanently on this platform after one recycle
generation. Detail in `development/PHASE_10_HANDOFF.md` §8. The mandatory
full-scale 100,000-sheet kill/resume acceptance matrix was not executed -
an explicit, scoped deferral, not a technical limitation; see the handoff
for the exact commands to run it.
Detail in `development/PHASE_09_HANDOFF.md` §9.

279 of those are Phase 7 (Candidate & Attendance Reconciliation):
`tests/unit/test_candidate_import.py` (87, parsing, identifier normalisation,
column detection and every refusal, against real CSV fixtures and real
generated workbooks), `tests/unit/test_reconciliation.py` (44, the seven
classifications as a table, co-occurrence, determinism and that matching is not
quadratic), `tests/unit/test_reconciliation_store.py` (55, roster storage,
idempotence, the six operator actions, the shared ledger, persistence and the
migration from a Phase 6 project), `tests/unit/test_candidate_privacy.py` (18,
the mandatory privacy criterion, including a grep that fails if a Phase 7
module formats candidate data into a log call),
`tests/integration/test_reconciliation_workflow.py` (19, the acceptance
scenario with real recognition over real rendered sheets, Phase 6 integration
and a 1-vs-4-worker comparison) and `tests/gui/test_attendance_page.py` (56).
Confirmed end-to-end through `scripts/run_gui_smoke_tests.py` (43/43,
including every classification, the machine-and-imported-values check, the
missing-operator refusal, the sample download and a log-capture check).

149 of those are Phase 6 (Conflict Detection & Human Resolution):
`tests/unit/test_conflict_policy.py` (40, detection in isolation - the
taxonomy, the policy defaults, determinism and identity, and that every label
comes from the template), `tests/unit/test_review_store.py` (48, the ledger
rules: the machine value never overwritten, reviewer and reason required,
append-only enforced by the database, reopening superseding without erasing,
the cached state equal to the fold, one transaction per decision, and the queue
paged and counted in SQL at 10,000 conflicts),
`tests/integration/test_conflict_review.py` (24, the real engine and real
rendered sheets: scenarios A-E, every conflict kind raised from marks on a
page, migration onto an existing Phase 5 project, and source-file integrity)
and `tests/gui/test_resolve_page.py` (37, the real page with a real project and
a real `QThread`). Confirmed end-to-end through
`scripts/run_gui_smoke_tests.py` (38/38, including a named decision recorded
against the real sample, both tamper refusals and the missing-reviewer refusal)
and by inspecting the four screenshots `scripts/capture_gui_states.py --only
review` produces - which is how a real defect was found and fixed: a
duplicate-identifier conflict names no zone, so the zoomed view had been
falling back to the whole page at fit scale instead of magnifying the roll
number a reviewer has to read.

93 of those are Phase 4 (Template Calibration & Validation):
`tests/unit/test_calibration_service.py` (34, the four-state judgement rules
against hand-built results, including the nothing-marked-anywhere rule and the
answer counts that must come from `MarkStatus` rather than from the value
string), `tests/unit/test_bubble_metrics.py` (+4, the reported sampling
geometry), `tests/integration/test_calibration_workflow.py` (18, the real
engine against real templates and synthetic sheets, including *both*
miscalibration shapes, the small-vs-large-offset pair, and a shared-data-path
proof that varies the sampler's own configuration) and
`tests/gui/test_calibration_page.py` (37, A-J plus overlay/image alignment
across three zoom levels, the original-scan view, and per-position field
diagnostics). Confirmed end-to-end against the real sample and its real
template through `scripts/run_gui_smoke_tests.py` (32/32 checks) and by
inspecting the ten screenshots `scripts/capture_gui_states.py --only
calibration` produces - including the sampling overlay at the top, middle and
bottom of the page, since a scale error accumulates downwards and one region
proves nothing about the others.

96 of those are the multicore work, and a further 94 the large-batch
progress work (`tests/unit/test_batch_progress.py`, 58, including a
10,000-job simulation; `tests/gui/test_batch_progress_gui.py`, 33;
progress reconciliation across workers in the parallel integration tests): `tests/unit/test_processing_settings.py`
(29, worker-count policy on every plausible machine),
`tests/integration/test_parallel_batch.py` (32, real worker processes: result
consistency at 1/2/4 workers, batch ordering, duplicate-roll collisions, failure
isolation, cancellation, no orphan processes) and
`tests/gui/test_processing_settings_gui.py` (35, the Settings dialog, its
persistence and a multicore batch driven through the GUI).

The skip is structural: `tests/unit/test_qtguitesting_skill.py` parametrises
over the skill's scripts and skips `_harness.py`, which is shared plumbing
rather than a command a user runs.

A further 94 are the developer testing tools: `integration/test_synthetic_dataset.py`
(48, including template-independence and the presence of each profile's
mandatory cases), `unit/test_evaluation_harness.py` (26 of its 62 are new -
category metrics, grid-field judgement, duplicate scoring, the run
configuration) and `gui/test_developer_tools.py` (20, driving the menu, the
dialogs, cancellation and benchmark mode).

1365 of those tests are new in Phase 3 (146 in Phase 2, 455 in Phase 1). Phase 3
added **no** new mypy overrides, Ruff ignores or tool configuration changes.

Phase 6 added one tool configuration change, and it is a scope extension rather
than a suppressed check: `ARG002` joined `ARG001` in the existing **test-only**
per-file ignores in `pyproject.toml`. The reason is identical to the one
already documented for `ARG001` - a pytest fixture requested purely for its side
effect ("make a conflict exist") is indistinguishable from a dead parameter to
ruff - and `ARG002` is that same case for a test written as a method of a
`Test*` class, which most of Phase 6's are. No source-tree rule was relaxed, and
no mypy override was added.

Phase 2 added **one** tool configuration change, not a suppressed check: the
`pep8-naming` Qt-override allowlist in `pyproject.toml` was extended to cover
the additional Qt event-handler names the designer's canvas and graphics items
override (`mousePressEvent`, `wheelEvent`, `drawBackground`, and similar). The
documented exception list in `docs/DEVELOPMENT_GUIDE.md` is otherwise
unchanged from Phase 0 (`D107` and `ANN401` project-wide, test-only
relaxations, `warn_unreachable` off for the one module that branches on
`sys.platform`, and relaxed import handling for PySide6's generated stubs).

## Important architectural decisions

- Python 3.12 + PySide6 desktop application; every layer below `gui` is free of
  Qt, so the services remain usable headlessly ([ADR-0001](../docs/decisions/ADR-0001-desktop-python-pyside6.md)).
- A project is a folder with `project.json` plus `database.sqlite`; scans are
  referenced in place, internal paths are relative ([ADR-0002](../docs/decisions/ADR-0002-project-on-disk-layout.md)).
- Forward-only hand-written migrations with a ledger table; a newer schema is
  refused rather than downgraded ([ADR-0003](../docs/decisions/ADR-0003-schema-migrations.md)).
- Template coordinates are normalised to the canonical page; bubble centres are
  derived from a stored pitch ([ADR-0004](../docs/decisions/ADR-0004-normalized-template-coordinates.md)).
- Layering rules are executable: `tests/unit/test_architecture.py` fails if the
  GUI imports OpenCV or SQLAlchemy, if `imaging` imports Qt, if the new `tools`
  package imports a widget, or if a module lacks a docstring.
- Recognition thresholds live in the template, never in code. Alignment *tuning*
  is the one qualified case and is confined to `omr_scanner.imaging.config`,
  where every value is named, documented and validated; everything describing
  the *sheet* still comes from the template.
- Alignment maps detected marker centres onto canonical **marker centres**, not
  onto page corners, so the printed margin outside the markers is not stretched
  across the output.
- Detection speaks in scan corners (`ImageCorner`); only orientation resolution
  assigns canonical roles (`MarkerRole`). The two are distinct types, because
  conflating them is how an upside-down sheet becomes a confident wrong answer.
- An alignment failure is reported, never worked around. Three real corners and
  one invented one would produce a plausible rectification and a complete set of
  wrong answers.
- The machine's recognised value is never overwritten by a correction; that
  constraint shapes the data model from the start.
- A bubble's ink threshold is **local and relative**, not a page-wide constant:
  halfway between the paper level measured in an annulus around that bubble and
  the page's own ink level. An absolute threshold fails in both directions - it
  reads a printed option glyph as a mark, and a uniformly faint pencil sheet as
  blank.
- Recognition reports five explicit states, never a value plus a boolean. A
  blank, a double mark, an uncertain reading and an unmeasurable group stay
  distinguishable all the way to the CSV, because flattening them is how a
  double mark silently becomes one answer.
- Naming is separated from copying: `filename_manager` only decides a name and
  `batch_processor` performs the side effect. That makes the duplicate rule
  testable without a disk and lets the GUI preview an output name before
  anything is written.
- The filename allocator treats files **already in the output directory** as
  taken, not only the names it issued this session, so a second run cannot
  replace the first run's output.
- An unreliable identifier gets a review name, never a plausible one. A
  fabricated file name is worse than an obviously-for-review one, because it is
  indistinguishable from a correct result.
- The template designer edits by producing a new `OmrTemplate` via
  `model_copy(update=...)` and pushing it onto a snapshot-stack undo history -
  there is no second, mutable template representation to keep in sync with the
  persisted format (`docs/phase_02_plan.md` §4).
- Marker/orientation *detection provenance* (auto-detected, confirmed,
  confidence) is designer session state, never persisted: a scanner only needs
  a marker's geometry, which the domain model already stores.
- The designer never imports `cv2`/`numpy`: `services.marker_detection_service`
  hands it plain bytes and floats, enforced by the same executable layering
  test as everything else in `gui`.

## Next recommended action

Consult `development/ROADMAP.md` for the next phase, and
`development/PHASE_06_HANDOFF.md` for entry conditions and constraints.

Independently of whichever phase comes next, and now the single most valuable
thing anyone can do for this project: **process a stack of genuinely filled
sheets.** Phase 3 reads the one real sample perfectly, and geometric variants of
it, but one sheet is not a corpus. What has never been seen: light pencil,
erasures, crossed-out answers, marks that overflow the bubble, different
candidates' handwriting, a scanner other than the one that produced the sample,
and a sheet with a genuinely ambiguous double mark on it. Every accuracy claim
in this document is bounded by that.

Concretely: scan 20-30 filled sheets, anonymise them, build a template in the
designer, run them through the Scan page, and compare the CSV against what the
papers actually say. The measurement that matters is how many sheets needed
review and how many were confidently *wrong* - the second number is the one that
decides whether this is usable.

The machinery for that now exists and needs no new code. Write a
`ground_truth/*.json` per sheet by hand (`SheetGroundTruth`, `human_verified`
set true, a `reviewer`), put the images in `images/`, and point benchmark mode
at the folder: the same report, the same categories and the same
previous-run comparison apply to real sheets, and tagging a sheet
`FAINT_MARK` or `ERASED_MARK` by hand puts it in the category table beside its
synthetic equivalent. Keep that data in `private_test_data/` or
`local_test_data/`, which are git-ignored - real candidate identifiers must
never reach this repository.

Phase 6 changes what that exercise can now measure. Previously the only outputs
were a CSV and a benchmark report; now the same batch produces a **conflict
queue**, so the run yields three numbers rather than one: how many sheets the
engine flagged, how many a reviewer agreed with on inspection (Accept), and how
many it got wrong. The third is still the one that decides whether this is
usable — a confidently wrong reading never enters the queue — but the first two
now tell you whether the review workload is realistic for an examination office,
which no synthetic dataset can.

Run it with a real reviewer name set, and afterwards read the audit ledger: it
is a complete record of what a human had to overrule and why, which is exactly
the evidence needed to decide whether the thresholds want recalibrating.