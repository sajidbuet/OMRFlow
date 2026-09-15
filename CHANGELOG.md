# Changelog

All notable changes to OMRFlow are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions below 1.0 make no compatibility promises.

## [Unreleased]

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
