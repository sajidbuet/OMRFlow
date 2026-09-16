# Changelog

All notable changes to OMRFlow are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions below 1.0 make no compatibility promises.

## [Unreleased]

Phase 2 - interactive template designer. A user can build a complete `.omrt`
template visually: load a reference sheet, detect and adjust registration
markers, draw and configure regions, fine-tune individual bubbles, undo/redo,
validate and save. Bubble recognition still does not exist; this build must
not be used for examination processing.

### Added

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

### Known limitations

- No snap-to-grid while dragging yet (the pure function exists and is tested;
  wiring it into interactive dragging is deferred).
- Distribute is per Question-Region group only (Distribute Columns Evenly);
  no general align/distribute tool for arbitrary multi-selected regions.
- Individual-bubble override reset is per-region, not per-bubble.
- The designer has not been used against a real printed sheet.

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
