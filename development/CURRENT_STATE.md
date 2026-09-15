# Current state

**Updated:** 2026-09-15
**Version:** 0.1.0.dev0
**Current phase:** Phase 0 complete. Phase 1 not started.

Update this file at the end of every phase.

## What works

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

## What does not exist

Everything else. Specifically, there is **no** image processing, marker
detection, orientation handling, perspective correction, bubble recognition,
template designer, batch processing, conflict resolution, attendance
reconciliation, answer-key handling, scoring, or Excel/PDF reporting.

`omr_scanner.imaging`, `omr_scanner.recognition` and `omr_scanner.reporting`
contain module documentation and no code. The corresponding GUI pages say which
phase will implement them and do not simulate anything.

**This build must not be used for examination processing.**

## Known limitations

- The example template in `resources/templates` is illustrative. Its coordinates
  have never been calibrated against a printed sheet.
- Project metadata is written once at creation; nothing updates `modified_at`
  yet, because nothing else writes to a project.
- The GUI is functional but visually plain: no icons, no theming, no window
  geometry persistence.
- No packaging or installer; the application runs from a source checkout.
- Single-threaded. The worker-thread infrastructure arrives with Phase 5.
- `resources/icons` is reserved and empty.

## Test status

128 tests, all passing (Python 3.12.2, Windows 11).

```text
pytest         128 passed
ruff check .   All checks passed
mypy           Success: no issues found in 33 source files
```

Documented tool exceptions (all narrow, all justified in
`docs/DEVELOPMENT_GUIDE.md`): `D107` and `ANN401` project-wide, Qt camelCase
overrides for `pep8-naming`, test-only relaxations, `warn_unreachable` off for
the one module that branches on `sys.platform`, and relaxed import handling for
PySide6's generated stubs.

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
  GUI imports OpenCV or SQLAlchemy, if `imaging` imports Qt, or if a module lacks
  a docstring.
- Recognition thresholds live in the template, never in code.
- The machine's recognised value is never overwritten by a correction; that
  constraint shapes the data model from the start.

## Next recommended action

Begin **Phase 1 - OMR Geometry & Alignment Engine** (`development/ROADMAP.md`).
Entry conditions and the suggested starting prompt are in
`development/PHASE_00_HANDOFF.md`.
