# Changelog

All notable changes to OMRFlow are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions below 1.0 make no compatibility promises.

## [Unreleased]

Phase 3 - batch scanning and recognition. A user can now read filled answer
sheets against a template: import one or many scans, have them rectified and
recognised - concurrently across CPU cores, if the machine has them - review the
result with an overlay, optionally file the images under
their detected roll numbers, and export CSV. Recognition has been validated
against one real scanned sheet and geometric variants of it, **not** against a
corpus of independently filled papers; this build must not be used for
examination processing.

### Added — Phase 3

- `omr_scanner.imaging.metrics`: per-bubble measurement. An elliptical interior
  sample, a local paper estimate from an annulus around each bubble, and an ink
  threshold placed halfway between that and the page's own ink level - so a
  printed option glyph inside an empty bubble is not read as a mark, and a
  uniformly faint pencil sheet is not read as blank.
- `omr_scanner.recognition`: `models.py` (the `MarkStatus`/`FieldStatus`
  vocabulary), `decide.py` (`decide_group` - one group of bubbles to one
  `Selection`), `fields.py` (`recognise_grid_zone`, `recognise_template`).
  Blank, single, multiple, uncertain and unreadable are five distinct states
  that survive to the CSV; every threshold comes from the template.
- `omr_scanner.services.recognition_service`: `recognise_scan` - one file to a
  `ScanResult` with registration status, field values, overlay geometry and an
  optional bounded-size preview.
- `omr_scanner.services.batch_processor`: `process_batch` with per-file error
  isolation, progress callbacks and cooperative cancellation. One corrupt image
  cannot end a batch.
- `omr_scanner.services.filename_manager`: `FilenameAllocator` - decides output
  names and never touches a file. Duplicates become `_a`, `_b`, ... `_z`,
  `_aa` (bijective base-26); files already in the output directory count as
  taken; an unreliable identifier gets `UNRESOLVED_001` rather than a
  fabricated name. **No scan image is ever overwritten.**
- `omr_scanner.services.scan_import`: `collect_scan_files` - PNG/JPEG/TIFF/BMP,
  folder walking, unrelated files ignored, natural sort (`scan2` before
  `scan10`).
- `omr_scanner.services.scan_export`: deterministic UTF-8 CSV with stable base
  columns and question columns ordered by the template.
- `omr_scanner.gui.scan`: the Scan workflow stage - `page.py` (controls, scan
  list, results panel), `preview.py` (zoom/pan/fit view with a non-destructive
  recognition overlay), `worker.py` (`BatchWorker`, `PreviewWorker` - `QThread`s
  that touch no widget). The package imports no `cv2`, `numpy`, `imaging` or
  `recognition`.
- `omr_scanner.imaging.synthetic`: `AnswerBubbleSpec` and marked-bubble
  rendering, Phase 3's ground-truth generator.
- `examples/templates/ece_0000_sample.omrt` and
  `scripts/build_ece0000_template.py`: a template describing the repository's
  real sample sheet, built through the real generators.
- **Configurable multicore batch recognition.** `process_batch(..., workers=N)`
  reads several sheets at once through
  `omr_scanner.services.parallel_batch.recognise_in_parallel`, a
  `ProcessPoolExecutor` using `spawn` on every platform, with one complete page
  per worker process. Recognition results come back to the main process, which
  keeps naming, copying and recording strictly in batch order behind a single
  `FilenameAllocator` - so duplicate roll numbers, the scan list and the CSV are
  identical on any number of cores, and no two workers can ever choose the same
  output file name. Progress counts completions, so the bar advances steadily
  rather than waiting on the slowest sheet.
- `omr_scanner.config.processing`: `ProcessingMode` (Automatic / Single core /
  Custom) and `ProcessingSettings`, nested into `AppConfig.processing` and
  persisted in `omrflow.config.json`. Automatic leaves one logical CPU free and
  stops at 8 workers - a cap taken from measurement (throughput peaks at the
  physical core count and falls beyond it, while memory keeps climbing by about
  95 MB per worker); no run ever starts more workers than there are scans.
- `File > Settings...` (`omr_scanner.gui.settings_dialog.SettingsDialog`): the
  Processing section, with the detected CPU-thread count and the number of
  workers the current setting will actually use. The Scan page shows the same
  thing for the list in front of you ("124 scans - 8 parallel workers") and
  reports `Completed 46 / 100 - 8 workers` while running.
- `scripts/benchmark_batch.py`: measures batch throughput at several worker
  counts on the machine it is run on, and prints what it measured. Results for
  the development machine are recorded in `docs/scan_workflow.md` §10.
- `multiprocessing.freeze_support()` in `main()`, so a frozen Windows build
  starts workers rather than recursive copies of the application.
- 979 tests, including `tests/gui/test_scan_page.py` (the ten Scan workflows),
  `tests/integration/test_sample_sheet_recognition.py` (the real scan),
  `tests/integration/test_parallel_batch.py` (real worker processes: result
  consistency at 1/2/4 workers, ordering, duplicate-roll collisions, failure
  isolation, no orphan processes) and
  `tests/gui/test_processing_settings_gui.py`.
- The `qtguitesting` skill now covers the Scan page and the Processing settings:
  a `ScanHarness`, ten smoke checks, seven screenshot scenarios and four new
  documented scenarios.

### Added — Phase 3 large-batch progress

Batch processing is built for examination-scale runs (10,000+ scripts), and
the Scan page now reports on one without changing shape as it grows.

- **`omr_scanner.services.batch_progress`**: `BatchProgressTracker`,
  `ProgressSnapshot`, `JobStatus`, `BatchState` and the shared duration/rate/
  count formatters. Thread-safe, headless, and unit-testable with an injected
  clock - the estimator has 57 tests including a 10,000-job simulation that
  runs in under a second.
- **A progress panel on the Scan page**: a bar driven by *finished* sheets
  (a failed scan advances it, so a batch of damaged files cannot stall it),
  completed/total counts with thousands separators, percentage to one decimal,
  elapsed time on a monotonic clock, a smoothed estimate of the time
  remaining, live throughput, an estimated finishing time, and
  successful/needs-review/failed tallies in words.
- **A smoothed ETA** from an exponential moving average over half-second
  throughput samples, with a warm-up (about ten completed sheets, or a few
  seconds of steady measurement) during which it says `Calculating...` rather
  than extrapolating from one sample. A stall makes the estimate grow rather
  than freeze; cancellation withdraws it; completion sets it to zero.
- **`Preparing batch...`** as a distinct state, so the moments before the
  first sheet do not read as a stalled bar at 0 / 10,000.
- **Throttled repainting**: the tracker counts every completion, while the
  window pulls a snapshot about five times a second. A machine finishing fifty
  sheets a second no longer asks Qt to repaint fifty times a second. Scan-list
  rows are buffered the same way and located through a path index, which turns
  a long batch from quadratic into linear work in the GUI thread.
- **Cancellation that responds immediately**: the button disables itself and
  the panel says `Cancelling batch processing...` before any worker notices;
  no new sheet starts; sheets already inside a worker finish cleanly rather
  than being killed mid-write; everything already read is kept; and the final
  state reports both halves ("6,342 processed · 3,658 not processed").
- **A completion summary**: total duration and average speed, with the bar at
  exactly 100% only when every sheet has reached a terminal state.
- `BatchProgress` gained an `outcome` field, so a progress display can tally
  successes, reviews and failures *as sheets finish* - which on a multicore
  run is earlier than results are released in batch order.

### Changed

- A batch run no longer keeps the per-bubble evidence on results it retains
  for export: on the repository's 100-question sample that is 6.7 KB per sheet
  instead of 61.6 KB, or about **68 MB rather than 631 MB** across ten
  thousand sheets. Nothing the page shows used it - the overlay comes from the
  preview worker's own result - and switching diagnostics on keeps it, because
  the diagnostic images are drawn from it.
- `RecognitionOptions.keep_bubble_measurements=False` now omits the bubble
  records entirely rather than blanking their fields, which is where the
  memory actually is. No decision changes either way.

### Added — Phase 3 architectural hardening

Phase 3 turned into a *replaceable* recognition subsystem, so that Phases 4 and
5 can be built against it now and a future Recognition Engine v2 can replace it
without rewriting them. Details in `docs/recognition_engine.md`.

- **A stable recognition API.**
  `omr_scanner.services.recognition_service.RecognitionEngine` -
  `engine.process(image_path, template) -> ScanResult` - is the single entry
  point; no caller above it needs to know about thresholds, contours or
  homographies. `recognise_scan()` remains supported and delegates to the same
  pipeline.
- **`omr_scanner.services.recognition_models`**: the result vocabulary, split
  out of the engine so a consumer can depend on the *shape* of a result without
  importing the engine that fills it. Adds to `ScanResult`: the engine name and
  version, the template's id/name/format version, a UTC timestamp,
  machine-readable `status_codes`, per-stage `timings` and a `ScanQuality`
  record (marker scores, reprojection error, corrected rotation and skew,
  perspective strength, brightness, contrast, sharpness). `to_dict()` /
  `from_dict()` serialise a result to JSON, refusing a newer schema version
  rather than misreading it.
- **Raw per-bubble measurements on every result.** `BubbleView` now carries
  `mean_darkness`, `contrast`, `paper_level`, the `ink_threshold` it was
  compared against, `sample_pixels`, `usable` and its `rank` within its group -
  so a future recalibration can ask "what would a threshold of 0.6 have
  decided?" arithmetically, without re-reading a single image.
- **`StatusCode`**: `OK`, `LOW_CONFIDENCE`, `BLANK`, `MULTIPLE_MARK`,
  `AMBIGUOUS`, `ALIGNMENT_WARNING`, `ALIGNMENT_FAILED`, `ORIENTATION_FAILED`,
  `MARKER_NOT_FOUND`, `ROLL_UNREADABLE`, `SET_UNREADABLE`, `INVALID_TEMPLATE`,
  `IMAGE_LOAD_ERROR`, `PROCESSING_ERROR` - derived from the values, so they can
  never disagree with the result they describe. A consumer branches on these,
  never on an English message.
- **`omr_scanner.services.recognition_settings`**: `RecognitionOptions` and
  `DiagnosticsOptions` - engine-level options (sampling, preview, what evidence
  to keep, debug output) in one immutable, picklable object. Thresholds stay in
  the template, where they belong.
- **`omr_scanner.services.recognition_diagnostics`**: a headless annotated
  overlay (`render_overlay`) and the staged debug dump per scan - the original,
  the rectified page, the decision overlay, a measurement overlay and the
  result as JSON. Off by default; a write failure costs the diagnostics, never
  the scan; and a test asserts that producing them changes no recognised value.
  Switchable from *File > Settings > Diagnostics* or `--diagnostics`.
- **Headless tools.** `python -m omr_scanner.tools.recognise` reads one scan or
  a folder with no GUI, writing JSON results, overlays or diagnostics, on one
  or many workers. A test proves it: recognition runs in a fresh interpreter
  where no `PySide6` module is ever imported.
- **`omr_scanner.evaluation`**, a QA layer *above* services: the ground-truth
  schema shared by synthetic and real datasets
  (`SheetGroundTruth`, `DatasetManifest`), a reproducible synthetic dataset
  generator, and a benchmark harness.
- **Synthetic dataset generator** (`python -m omr_scanner.tools.make_dataset`):
  renders labelled sheets *from a real template*, with five difficulty profiles
  and controlled defects - mark styles (fill, ring, tick, cross, scribble, dot,
  each with its own coverage, darkness, offset and size), geometry, exposure,
  blur, noise, JPEG artefacts, and structural damage such as a missing marker
  or a cropped page. Ground truth is written beside every image and records the
  defects injected; a seed reproduces a dataset byte for byte.
- **Benchmark harness**
  (`python -m omr_scanner.tools.benchmark_recognition`): scores recognition
  against ground truth, classifies every disagreement (`FALSE_MARK`,
  `FALSE_BLANK`, `WRONG_OPTION`, `MISSED_MULTIPLE_MARK`,
  `FALSE_MULTIPLE_MARK`, `ROLL_ERROR`, `SET_ERROR`, `ALIGNMENT_ERROR`,
  `PROCESSING_FAILURE`), writes `summary.json` and `errors.csv`, compares a run
  against a stored baseline, and offers a fill-threshold sweep that reports
  without changing anything.
- **Stored result fixtures** in `tests/fixtures/recognition/`: eleven scenarios
  a later phase must handle, loadable with no recognition engine present, built
  by `scripts/build_recognition_fixtures.py`.
- **`local_test_data/`**: the documented, git-ignored home for a real
  validation corpus, using the same layout and schema as a synthetic dataset so
  one benchmark command serves both. Nothing is uploaded, ever.
- 227 further tests (1,935 in the repository), covering the result contract,
  the fixtures, the evaluation harness, the engine as a subsystem, the dataset
  generator and the three command line tools.

### Fixed — Phase 3

- `ScanPage` no longer starts a second `PreviewWorker` for a scan already being
  rendered. Finishing a batch re-selects the current row, so a user who also
  clicked that row got two workers, and the late one re-applied the preview -
  resetting a zoom they had just set.
- The Settings dialog no longer reverts other preferences. It returned a whole
  `AppConfig` rebuilt from the snapshot taken when it opened, so accepting it
  discarded anything that had changed meanwhile - a project opened while the
  dialog was up disappeared from the recent list. It now returns only its own
  `ProcessingSettings`, which the main window merges into the current
  configuration.

### Added — Phase 2

- `omr_scanner.gui.template_designer`: the interactive template designer.
  - `page.py` - the workflow page: file actions with dirty tracking, marker
    detection, region creation, undo/redo, keyboard shortcuts, validation.
  - `canvas.py` - zoomable/pannable `QGraphicsView` canvas: wheel zoom, pan,
    an alignment grid overlay, rubber-band region drawing, per-bubble
    fine-tune dots.
  - `items.py` - draggable/resizable region overlays with four visual states
    (normal, auto-detected, manually overridden, missing) and individually
    draggable bubble dots.
  - `state.py` - `DesignerState`: the template plus its undo/redo history plus
    session-only marker detection provenance (never persisted).
  - `history.py` - `SnapshotHistory`, a generic undo/redo stack over immutable
    snapshots.
  - `coordinates.py` - `CoordinateMapper` and `snap`, the one place
    pixel/normalised arithmetic happens.
  - `region_list.py`, `properties_panel.py`, `dialogs.py` - the region list,
    the numeric geometry editor, and one dialog per region kind plus New
    Template and the validation report.
- `omr_scanner.domain.template_authoring`: pure region-generation and
  designer-validation functions - `generate_character_grid_zone`,
  `generate_question_columns` (one zone per printed column),
  `generate_ignored_zone`, `translate_zone`, `resize_zone`,
  `build_blank_template`, `validate_template_for_designer`.
- `omr_scanner.services.marker_detection_service`: the seam that lets the
  designer decode images and run Phase 1's marker detector without the `gui`
  layer ever importing `cv2`/`numpy`. Detection is scored per corner
  independently (never Phase 1's stricter all-or-nothing four-corner
  assignment), so a sheet with three good corners and one damaged one is
  reported accurately rather than rejected outright.
- `OmrTemplate.reference_image`: one additive, optional field recording the
  reference sheet's path relative to the `.omrt` file, so a template can be
  reopened for further editing. Does not bump `format_version`; documents
  written before Phase 2 load unchanged.
- `examples/templates/100_question_4_choice_example.omrt`: a 7-digit student
  ID, A-D question set and 100 questions in 4 columns (474 bubbles total),
  built and validated through the real generator functions, with its
  reference image shipped alongside it.
- 146 new tests (729 total): region generation and validation, coordinate
  conversion, undo/redo, designer state mutations, the marker detection
  service against synthetic sheets, and GUI smoke tests covering the full
  create-detect-draw-adjust-undo-validate-save-reload workflow.
- Set Code regions now support both **enumerated values** (arbitrary
  multi-character tokens - `"10,11,12"`, `"01,02,03"` - never split into
  digits or coerced to numbers) and a **positional code** mode (each code
  position its own bubble column); both were already representable by the
  existing `set_code` field, so this is a dialog-only addition with no schema
  change.
- Question Answer columns are independently positionable: the Questions
  dialog exposes bubble width/height, choice spacing, question row spacing
  and Column Gap as explicit image-pixel fields (defaulting to reproduce the
  original auto-fit geometry exactly), with a live dashed preview on the
  canvas; `QuestionBlockFieldDefinition.group_id` (additive, optional) ties
  sibling columns of one Question Region together.
- **Create Question Column Array** and **Distribute Columns Evenly** toolbar
  actions (`omr_scanner.domain.template_authoring.generate_column_array`,
  `.distribute_columns_evenly`, `.measure_column_gap`): generate a full set of
  calibrated columns from one reference column, or re-space a group's
  intermediate columns evenly between a fixed first and last - each applying
  as one undo step (`DesignerState.apply_zones`).
- Canvas panning via middle-button drag (always) and right-button drag (past
  Qt's own standard drag-distance threshold, leaving a plain right-click free
  for a future context menu), in addition to the existing space+left-drag;
  neither ever reaches a region item, so it cannot be mistaken for selecting
  or moving one.
- `docs/testing/question_layout_manual_test.md`: manual verification steps
  for the above.
- Official OMR Flow branding: the wordmark logo (`omr_scanner.gui.branding`)
  anchored at the bottom of the left navigation sidebar, below the workflow
  step list and horizontally centred - not a dedicated header row, so no
  vertical space is reserved above the page content - a multi-resolution
  application/window icon derived from it (`gui/resources/branding/icon.ico`,
  generated by `scripts/generate_branding_assets.py`), and a small
  developer-credit footer with a clickable link to https://www.sajid.bd
  opened via `QDesktopServices` in the system browser. Purely presentational;
  no template, scan or workflow behaviour changed.

### Changed

- `omr_scanner.gui.pages.base_page.WorkflowPage` gained an `expand: bool`
  constructor flag so a page's body can fill all available vertical space
  instead of shrinking to its content with a trailing spacer - needed for the
  designer's full-size canvas. Default behaviour for every other page is
  unchanged.
- `omr_scanner.gui.pages.catalog.WorkflowPageSpec` gained an explicit
  `implemented` field, decoupling "is this a working page" from "which phase's
  number is shown to the user" - the Template stage keeps `phase=2` for
  documentation purposes while `implemented=True`.
- The Template workflow stage is a real page instead of a placeholder.
- `docs/TEMPLATE_FORMAT.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT_GUIDE.md`
  updated for the above.
- `pyproject.toml`: the Ruff `pep8-naming` Qt-override allowlist extended to
  cover the mouse/hover/wheel/key/drag event handlers the designer's canvas
  and graphics items implement.

### Fixed

Template Designer correction pass. Root causes and the reasoning behind each
correction are recorded in `docs/development/template_gui_fix_diagnosis.md`.

- **A Question Region changed size when its column count changed.** The drawn
  rectangle was never a container: `generate_question_columns`' explicit-pitch
  branch read only `bounds.x`/`bounds.y` and derived each strip's size from the
  pitch, so the block's outer extent was proportional to the column count.
  `ColumnLayoutMode` now names the two behaviours - `FIT_CONTAINER` (the
  default; the strips tile the user's rectangle exactly, for any column count,
  gap, pitch or bubble size) and `FROM_PITCH` (which `generate_column_array`
  opts into, because "make N more like this calibrated column" is meant to
  extend past it).
- **Resizing a region threw it to the scene's top-left corner.**
  `RegionHandleItem` captured its press rectangle in *item-local* coordinates,
  added a *scene-space* mouse delta to it, and passed the result to
  `set_scene_rect`, which reads scene coordinates - so the first mouse-move set
  the item's position to the drag delta alone. The gesture is now computed
  entirely in scene coordinates, and the constructor enforces the class's
  invariant (`pos()` holds the position, `rect()` holds the size, never both).
  Resize anchoring falls out of the arithmetic with no special cases.
- **Bubble size could not be adjusted, and changing it altered nothing.**
  `RegionHandleItem.paint` drew every preview bubble at a hard-coded 3 px radius
  and `RegionSpec` carried no size at all, so no value a user could type would
  change what was drawn; separately, three of the four bubble region kinds had
  no bubble-sizing control.
- **The Template page spent a sixth of the window's height above the canvas.**
  `WorkflowPage` unconditionally gave every page a word-wrapped summary row and
  24 px margins - right for a placeholder page whose content *is* explanatory
  text, wrong for one whose body is a full-size editor.

### Added

- **Orientation-mark detection inside a user-drawn region.**
  `omr_scanner.imaging.orientation_marker` crops the rectangle, thresholds it on
  its own statistics and scores each dark shape on darkness, solidity, aspect
  ratio, size and position, with dash-shaped defaults rather than the
  registration-square criteria (which reject a 2:1 dash on aspect ratio alone).
  Reached from the designer through the toolbar's **Orientation** action and
  `services.detect_orientation_marker_in_region`. Every reported coordinate is
  in full-image pixels; a mark wholly inside the rectangle is never rejected for
  being inside it. Previously there was no orientation detection in the designer
  at all - `imaging.orientation` answers a different question (*which way up* a
  scan was fed, from four detected corners and a homography) and cannot answer
  this one. Setting `OMRFLOW_ORIENTATION_DEBUG_DIR` writes an annotated overlay
  of the search region, every candidate and each rejection reason.
- **A template-wide bubble radius.** `OmrTemplate.default_bubble_radius` -
  additive and optional, normalised to the page width, no `format_version` bump
  - with `default_bubble_size` deriving the width/height a grid stores from the
  page aspect ratio, so a bubble circular in pixels stays circular. Edited from
  toolbar row 2 in reference-image pixels, with a per-region override in the
  properties panel. A region inherits while its stored size matches what the
  default produces, so the relationship survives save and reload without a new
  schema field. `set_zone_bubble_size` changes only `grid.bubble_size`: every
  bubble centre, every hand-placed override and the parent rectangle are
  preserved exactly.
- `place_grid_in_bounds` - the counterpart to `fit_grid_to_bounds`: positions a
  lattice of a *given* pitch inside a fixed rectangle, rejecting one that does
  not fit rather than silently enlarging the region.
- A "Fit bubble spacing to the region" option in the Question Block dialog (on
  by default), so changing the column count reflows the bubbles across the
  container instead of leaving them at a pitch a different count needed.
- `.claude/skills/qtguitesting/` - a repository-local Claude Code skill for
  OMRFlow's Qt GUI: the testing hierarchy and workflow, a Qt coordinate-system
  reference, ten real-image scenarios, and four scripts
  (`run_gui_smoke_tests.py`, `capture_gui_states.py`, `dump_gui_geometry.py`,
  `compare_gui_images.py`). Documented in `docs/DEVELOPMENT_GUIDE.md`.
- Regression tests for every bug above:
  `tests/unit/test_question_region_container.py`,
  `tests/gui/test_template_designer_region_geometry.py`,
  `tests/gui/test_template_designer_bubble_and_layout.py`,
  `tests/integration/test_orientation_marker_detection.py` (seven synthetic
  scenarios plus six search-rectangle shapes on `examples/ECE-0000.png`), and
  `tests/unit/test_qtguitesting_skill.py`.

### Changed (correction pass)

- The Template toolbar is two rows - file/undo/detection/validation above,
  region tools/bubble radius/zoom/grid below. One row pushed most of the region
  tools into Qt's overflow menu on any window narrower than about 1400 px.
  `TemplateDesignerPage.toolbar_actions()` returns both rows' actions.
- `WorkflowPage` gained `show_summary` and `compact`; the designer passes both,
  keeping the stage description as the title's tooltip. Every other page is
  unchanged.
- `DesignerState.apply_template` replaces the whole document in one history
  entry, so a template-level change that also touches zones (a radius change) is
  one undo step.
- `test-output/` is git-ignored - generated screenshots and geometry dumps, never
  committed baselines.

### Known limitations

- No snap-to-grid while dragging yet (the pure function exists and is tested;
  wiring it into interactive dragging is deferred).
- Distribute is per Question-Region group only (Distribute Columns Evenly);
  no general align/distribute tool for arbitrary multi-selected regions.
- Individual-bubble override reset is per-region, not per-bubble.
- The Question Block dialog exposes one bubble *radius* rather than independent
  width and height, so a deliberately elliptical bubble cannot be authored from
  that dialog. The `.omrt` model still stores the two axes independently and a
  template authored elsewhere round-trips unchanged.
- The designer has been exercised against the repository's real sample sheet
  (`examples/ECE-0000.png`) but has not been used to process a real examination.

---

Phase 1 - OMR geometry and alignment engine. An arbitrary scan of a sheet can
now be normalised into the canonical page its template describes. Bubble
recognition still does not exist; this build must not be used for examination
processing.

### Added

- `omr_scanner.imaging`: the geometric normalisation engine, entry point
  `align_sheet(image, config=...)`.
  - `preprocessing` - validation, grayscale, working-resolution downscale,
    denoising, and Otsu or adaptive thresholding.
  - `marker_detection` - contour measurement (centroid, area ratio, aspect
    ratio, rectangularity, solidity, interior ink), combined shape filtering
    with a named reason per rejection, per-corner scoring and an exhaustive
    one-to-one assignment of candidates to the four page corners.
  - `orientation` - resolves 0/90/180/270 degree page orientation by rectifying
    the orientation mark's expected window out of the scan under each of the
    four hypotheses, after pruning those whose geometry is implausible.
  - `geometry` - point ordering, quadrilateral validation, homography and its
    inverse, as pure functions.
  - `alignment` - the orchestrator, quality metrics and warnings.
  - `diagnostics` - optional detection overlay, rectified-page preview and
    textual summary; inert by construction.
  - `synthetic` - synthetic canonical sheets with interior control points, and a
    seeded distortion engine (rotation, scale, translation, perspective,
    brightness, illumination gradient, blur, noise, JPEG, cropping).
  - `config` - every tunable value, named, documented, validated and defaulted
    in one place.
  - `models` - explicit runtime types for candidates, detections, orientation,
    metrics, diagnostics and the result.
- `omr_scanner.services.alignment_service`: converts an `OmrTemplate` into an
  `AlignmentConfig`, and reads and writes image files (through
  `fromfile`/`imdecode`, so non-ASCII paths work on Windows).
- `omr_scanner.tools`: developer command line utilities `align_image` and
  `make_test_sheet`.
- Six `ImagingError` subclasses, each carrying a stable machine-readable `code`:
  `ImageValidationError`, `MarkerDetectionError`, `InsufficientMarkersError`,
  `AmbiguousMarkerError`, `OrientationDetectionError`,
  `InvalidPageGeometryError`, `AlignmentTransformError`.
- 455 new tests (583 total), including a 40-case synthetic distortion suite
  measured against nine interior control points per case.

### Changed

- `docs/IMAGE_PROCESSING.md` rewritten to describe the implemented engine, its
  measured accuracy and its measured degradation limits.
- `docs/TESTING.md` documents the synthetic generator, the distortion engine,
  the accuracy metric and the tolerances.
- `docs/ARCHITECTURE.md` records the `tools` layer, the expanded error
  hierarchy and the template/imaging seam.
- `tests/unit/test_architecture.py` enforces the layering rules for `tools`.

### Known limitations

- No real-world validation: every accuracy number is synthetic.
- Arbitrary rotation is corrected up to ±15 degrees, plus exact quarter turns.
  Beyond that the corner search regions must be widened.
- A missing registration marker fails the sheet; no corner is ever
  extrapolated.

## [0.1.0.dev0] - 2026-09-15

Phase 0 - architecture and repository foundation. The application starts and
manages projects; no OMR processing exists yet.

### Added

- Packaging (`pyproject.toml`) with pinned tool configuration for pytest, Ruff
  and mypy, and the `omrflow` console entry point.
- Layered package skeleton under `src/omr_scanner`: `domain`, `database`,
  `services`, `gui`, `config`, `utils`, plus documented-but-empty `imaging`,
  `recognition` and `reporting` packages.
- Application exception hierarchy (`omr_scanner.errors`) with separate technical
  and user-facing messages.
- Per-user application configuration (`AppConfig`) with recent-project tracking,
  stored in the platform's standard configuration directory.
- Structured logging: an application log plus a per-project log attached while a
  project is open.
- Project model and service: create, validate, open and close a project
  directory containing `project.json`, `database.sqlite` and the standard
  sub-directories.
- SQLite/SQLAlchemy 2.x foundation with a forward-only migration ledger
  (schema version 1: `schema_migration`, `project_setting`).
- Versioned `.omrt` template document model (page geometry, registration and
  orientation markers, zones, fields, bubble grids, recognition settings) with
  load/save support and an illustrative example in `resources/templates`.
- Minimal PySide6 shell: main window, workflow navigation with eight stages,
  status bar, File and Help menus, and honest "not implemented yet" placeholder
  pages naming the phase that will implement each stage.
- Test suite (128 tests) covering configuration, project lifecycle, database
  initialisation/migration, template validation, GUI startup, and an executable
  check of the architectural layering rules.
- Documentation set: architecture, development guide, data model, template
  format, image-processing plan, testing strategy, user guide and four ADRs.

[Unreleased]: https://github.com/sajidbuet/OMRflow/compare/v0.1.0.dev0...HEAD
[0.1.0.dev0]: https://github.com/sajidbuet/OMRflow/releases/tag/v0.1.0.dev0
