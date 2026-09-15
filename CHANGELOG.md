# Changelog

All notable changes to OMRFlow are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions below 1.0 make no compatibility promises.

## [Unreleased]

Nothing yet. The next entry will be Phase 1 (OMR geometry and alignment engine).

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
