# Current state

**Updated:** 2026-09-18
**Version:** 0.1.0.dev0
**Current phase:** Phase 3 (Recognition Engine v1) implemented and architecturally hardened; accuracy validation pending a real dataset. Phase 4 not started.

Update this file at the end of every phase.

## What works

### Application shell and projects (Phase 0)

- The application starts (`python -m omr_scanner` or `omrflow`) and shows the
  main window with eight workflow stages, a status bar, File and Help menus.
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

## What does not exist

No conflict resolution, attendance reconciliation, answer-key handling, scoring,
database-backed results, or Excel/PDF reporting. Recognised values cannot yet be
corrected by hand in the GUI, and results are not written to the project
database - a batch's output is the CSV and, optionally, the renamed image
copies.

`omr_scanner.reporting` contains module documentation and no code. The Resolve,
Results and later GUI pages say which phase will implement them and do not
simulate anything.

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
- **No manual correction.** Recognised values cannot be edited in the GUI. The
  data model already keeps the machine's reading separate from a correction, so
  this is additive, but it is not there yet.
- **Results are not persisted.** A batch's output is the CSV and the optional
  renamed copies; nothing is written to the project database (Phase 5).
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

1935 tests passing, 1 skipped (Python 3.12.7, PySide6 6.11.2, OpenCV 5.0.0,
NumPy 2.5.3, Windows 11).

```text
pytest         1935 passed, 1 skipped in 161.8s
ruff check .   All checks passed
mypy           Success: no issues found in 89 source files
```

96 of those are the multicore work: `tests/unit/test_processing_settings.py`
(29, worker-count policy on every plausible machine),
`tests/integration/test_parallel_batch.py` (32, real worker processes: result
consistency at 1/2/4 workers, batch ordering, duplicate-roll collisions, failure
isolation, cancellation, no orphan processes) and
`tests/gui/test_processing_settings_gui.py` (35, the Settings dialog, its
persistence and a multicore batch driven through the GUI).

The skip is structural: `tests/unit/test_qtguitesting_skill.py` parametrises
over the skill's scripts and skips `_harness.py`, which is shared plumbing
rather than a command a user runs.

1206 of those tests are new in Phase 3 (146 in Phase 2, 455 in Phase 1). Phase 3
added **no** new mypy overrides, Ruff ignores or tool configuration changes.

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
`development/PHASE_03_HANDOFF.md` for entry conditions and constraints.

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
