# Phase 0 handoff — Architecture & Repository Foundation

**Completed:** 2026-09-15
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.2

This document is the entry point for whoever continues the work. It is written
so that a developer or coding agent can act on the repository without
reconstructing the development history.

---

## 1. Work completed

### Starting point

The repository contained **only `README.md`** — a detailed vision document with
no source code, tests, configuration, notebooks, images or templates. Nothing
was deleted. The README's content (planned features, workflow, principles,
privacy stance) was carried into the new README and the documentation set; its
proposed structure and phase list were adopted essentially as written.

### Built in this phase

| Area | Outcome |
|---|---|
| Packaging | `pyproject.toml`: dependencies with the phase that first uses each, `dev` extra, `omrflow` entry point, and configuration for pytest, Ruff and mypy. |
| Layering | `src/omr_scanner` split into `domain`, `database`, `services`, `gui`, `config`, `utils`, plus documentation-only `imaging`, `recognition`, `reporting`. |
| Errors | `omr_scanner.errors`: one hierarchy, each exception carrying a technical message *and* a `user_message`. |
| Configuration | Immutable `AppConfig` (Pydantic) with recent projects and log level; platform directory resolution with `OMRFLOW_CONFIG_DIR` / `OMRFLOW_LOG_DIR` overrides. |
| Logging | Console + rotating application log; a second handler attached to `<project>/logs/project.log` while a project is open; privacy rule that candidate data is never logged. |
| Project | `ProjectDirectory` / `ProjectLayout` / `ProjectMetadata` / `Project` in `domain`; create, validate, open and close in `services.project_service`; `ProjectSession` owns the database handle and log handler. |
| Database | SQLAlchemy 2.x over SQLite; schema version 1 (`schema_migration`, `project_setting`); forward-only migration runner; transactional session scope; foreign keys enabled per connection. |
| Template | Full `.omrt` model (page geometry, four registration markers, orientation marker, zones, four field kinds, bubble grid with overrides, recognition settings) + load/save service + validated example resource. |
| GUI | `MainWindow` with eight-stage navigation, status bar, File/Help menus, recent projects; real `ProjectPage`; honest `PlaceholderPage` for the other seven stages. |
| Tests | 128 tests across unit, integration and GUI, including executable architecture checks. |
| Docs | 7 documents, 4 ADRs, roadmap, current state, this handoff. |

---

## 2. Files and directories added

Every file below is new; `README.md` is the only pre-existing file and was
rewritten.

```text
.gitignore                    CHANGELOG.md                  pyproject.toml
README.md (rewritten)

src/omr_scanner/
  __init__.py  __main__.py  main.py  errors.py  py.typed
  config/    __init__.py  app_config.py  paths.py
  domain/    __init__.py  geometry.py  project.py  template.py
  database/  __init__.py  engine.py  migrations.py  models.py
  services/  __init__.py  project_service.py  template_service.py
  gui/       __init__.py  application.py  error_reporting.py  main_window.py
             pages/  __init__.py  base_page.py  catalog.py
                     placeholder_page.py  project_page.py
  imaging/     __init__.py      (documentation only)
  recognition/ __init__.py      (documentation only)
  reporting/   __init__.py      (documentation only)

tests/
  conftest.py
  unit/        test_app_config.py  test_architecture.py  test_geometry.py
               test_logging_setup.py  test_main_entry.py
               test_project_domain.py  test_template_model.py
  integration/ test_database.py  test_project_service.py
               test_template_service.py
  gui/         conftest.py  test_main_window.py
  fixtures/    README.md  images/README.md

resources/  templates/example_answer_sheet.omrt   icons/README.md

docs/       ARCHITECTURE.md  DATA_MODEL.md  DEVELOPMENT_GUIDE.md
            IMAGE_PROCESSING.md  TEMPLATE_FORMAT.md  TESTING.md  USER_GUIDE.md
            decisions/  README.md  ADR-0001..ADR-0004

development/ ROADMAP.md  CURRENT_STATE.md  PHASE_00_HANDOFF.md
```

---

## 3. Architectural decisions

Four ADRs, with full rationale in `docs/decisions/`:

1. **ADR-0001 — Python + PySide6 desktop application.** One language end to end;
   direct file system access to scan batches; the mature CV stack is Python.
   Every layer below `gui` is Qt-free, so a future CLI or web front end could
   reuse the services.
2. **ADR-0002 — A project is a folder** containing `project.json` (discovery) and
   `database.sqlite` (authoritative). Scans are referenced in place, internal
   paths are relative, SQLite stays on the default journal mode because projects
   often live in synchronised folders.
3. **ADR-0003 — Hand-written forward-only migrations**, not Alembic. Thousands of
   small databases migrated automatically, no revision graph needed; a newer
   schema is refused rather than downgraded.
4. **ADR-0004 — Normalised template coordinates and a stored bubble pitch.**
   Resolution independence; small, diffable, calibratable templates; one shared
   `bubble_center` function.

Decisions not covered by an ADR but load-bearing:

- **Layering is enforced by a test**, not just documented
  (`tests/unit/test_architecture.py`).
- **Recognition thresholds live in the template**, never in code.
- **The machine value is never overwritten** by a human correction — this shapes
  the Phase 3/6 data model from the start (`docs/DATA_MODEL.md`).
- **Dialogs are separated from behaviour** in the main window, so GUI tests never
  touch a modal dialog.
- **`opencv-python-headless`** is used to avoid OpenCV's bundled Qt plugins
  shadowing PySide6's.

---

## 4. Tests created

| File | Tests | Covers |
|---|---:|---|
| `tests/unit/test_app_config.py` | 10 | Defaults, round trip, damaged file fallback vs strict mode, unknown keys, recent-project uniqueness/cap, sandbox isolation. |
| `tests/unit/test_geometry.py` | 8 | Normalised point/size/rect, pixel projection, edge containment, page overflow. |
| `tests/unit/test_template_model.py` | 18 | Bubble-centre arithmetic and overrides, row/column derivation, zone and grid validation, marker completeness, duplicate zone ids, JSON round trip, shipped example validity. |
| `tests/unit/test_project_domain.py` | 16 | Layout resolution, relative-path storage, metadata round trip, name validation, timezone requirement. |
| `tests/unit/test_architecture.py` | 21 | Per-layer forbidden imports, no OpenCV calls in the GUI, every module has a docstring. |
| `tests/unit/test_logging_setup.py` | 4 | File logging, no duplicate handlers, project handler attach/detach. |
| `tests/unit/test_main_entry.py` | 4 | Argument parsing, `--version`, GUI start-up failure becomes an exit code. |
| `tests/integration/test_project_service.py` | 18 | Directory structure, metadata, database mirroring, project log, invalid names, non-empty folder, reopen, repaired sub-directories, five invalid-project cases, session lifecycle, handle release. |
| `tests/integration/test_database.py` | 10 | Initialisation and ledger, reopen applies nothing, persistence, missing file/dir, newer schema refused, migration numbering, rollback, foreign keys. |
| `tests/integration/test_template_service.py` | 10 | Load/save round trip, suffix handling, missing/damaged/unrelated/newer/invalid documents, project template listing. |
| `tests/gui/test_main_window.py` | 9 | Startup state, navigation, placeholder honesty, create/close/reopen, error dialog on invalid folder, recent-project persistence, database released on close. |

All tests use temporary directories. An autouse fixture redirects the per-user
configuration and log directories into `tmp_path`, so no test can touch real user
data.

---

## 5. Results

```text
pytest         128 passed in 4.4s
ruff check .   All checks passed!
mypy           Success: no issues found in 33 source files   (strict mode)
```

### Tool exceptions, all narrow and documented

Nothing was silenced in bulk. The complete list, with reasons, is in
`docs/DEVELOPMENT_GUIDE.md`:

- `D107` project-wide — constructor arguments are documented in the class
  docstring's `Args:` section (Google style).
- `ANN401` project-wide — `Any` is unavoidable at the Qt/DBAPI boundary.
- `pep8-naming` ignores Qt override names (`closeEvent`, …).
- `tests/*` relax per-test docstrings, test signature annotations and unused
  fixture arguments; module docstrings remain required.
- mypy: `warn_unreachable` off for `config.paths` only (it branches on
  `sys.platform`); relaxed import handling for PySide6's generated stubs; the
  `gui` package may subclass the untyped Qt classes.

### Manual smoke test

Performed by driving the real `MainWindow` and capturing screenshots:

1. Application starts, main window visible, "No project open" in the status bar.
2. Created *Physics Midterm 2026*; title, status bar and Project page updated;
   the folder contained `project.json`, `database.sqlite` and all seven
   sub-directories.
3. Navigated to the Template and Resolve pages: both state "Not implemented yet —
   planned for development phase N" and list the planned functionality.
4. Closed the project, then reopened it from disk: metadata restored correctly.
5. Window closed cleanly; the database handle was released.

Installation instructions in the README were executed verbatim from a clean
`.venv` to produce the environment used above.

---

## 6. Known issues

- The example template's coordinates are illustrative and have never been
  calibrated against a printed sheet. It is a format demonstration, not a usable
  sheet design.
- `project.json` `modified_at` is written at creation and never updated, because
  nothing else writes to a project yet.
- The GUI has no icons, no theming and does not remember window geometry.
- Single-threaded; the worker infrastructure for long operations is Phase 5.
- GUI tests cover the window's behaviour, not its appearance; there is no visual
  regression testing.
- No packaging or installer (Phase 11).

None of these block Phase 1.

---

## 7. Technical debt deliberately deferred

Each of these was a conscious choice, not an oversight:

| Deferred | Why | When |
|---|---|---|
| The rest of the database schema | A speculative table is a table nobody knows the rules for. Only schema version 1 exists; the intended evolution is documented instead. | Each phase adds its own tables + migration |
| `imaging` / `recognition` / `reporting` implementations | Scope limit of Phase 0. The packages carry their contracts as documentation so later work starts from an agreed design. | Phases 1, 3, 9 |
| Template designer | Writing an editor before the format was settled would have coupled the two. | Phase 2 |
| Qt resource system and icons | No icons exist yet; adding the machinery first would be speculative. | Phase 2 |
| Window geometry / session persistence | `QSettings` duplicates part of `AppConfig`; the split needs one decision, not two half-implementations. | Phase 10 |
| Internationalisation | Strings are plain English literals. Retrofitting `tr()` later is mechanical; designing for it now would add noise. | Phase 11 |
| Config schema migration | `AppConfig` has `config_version` but no upgrade path, because there is no version 0 to upgrade from. | First time the schema changes |
| Test fixture images | No pipeline exists to consume them. The categories and the anonymization policy are reserved and documented. | Phase 1 |

---

## 8. Entry conditions for Phase 1

Phase 1 is **OMR Geometry & Alignment Engine** (`development/ROADMAP.md`).

**Already in place**

- `omr_scanner.imaging` exists with its contract in the module docstring.
- `ImagingError` is defined in the exception hierarchy.
- `omr_scanner.domain.geometry` defines the coordinate convention (origin
  top-left, `y` downward, values in `[0, 1]`).
- The template supplies each marker's expected centre, size and `search_radius`,
  and the canonical page size that alignment must produce.
- `tests/fixtures/images/` is reserved with a documented policy, and
  `docs/IMAGE_PROCESSING.md` records the intended pipeline stage by stage.
- OpenCV (headless) and NumPy are already declared dependencies.

**Constraints Phase 1 must respect**

1. No PySide6/Qt import anywhere under `omr_scanner/imaging` —
   `tests/unit/test_architecture.py` fails the build otherwise.
2. No threshold constants in code; values come from the template.
3. Functions take NumPy arrays and plain data, not an `OmrTemplate`, so they stay
   unit-testable with synthetic input.
4. Failures raise `ImagingError`; never return a sentinel or a blank result.
5. The output contract is the canonical page image at exactly
   `page.canonical_width_px` × `page.canonical_height_px`.
6. Original scans are never modified; aligned images are derived artefacts
   written to `<project>/scans_aligned`.
7. Every new module starts with the standard docstring (purpose,
   responsibilities, what does not belong, invariants).

**Suggested first steps**

1. Build the synthetic sheet generator *first*: render a canonical page from a
   template, then apply a known rotation/scale/translation/perspective. Ground
   truth makes every later assertion a measurement rather than an opinion.
2. Implement preprocessing, then marker detection within each marker's
   `search_radius`, then corner ordering, then the homography.
3. Add orientation resolution using the orientation marker, and test the
   upside-down case explicitly.
4. Add the degradation sweep (blur, noise, brightness, damaged markers) and
   record the failure boundary in `docs/IMAGE_PROCESSING.md`.
5. Keep the Scan page a placeholder — Phase 1 delivers the engine, not the
   workflow.

**Suggested next prompt**

> Begin Phase 1 — OMR Geometry & Alignment Engine, following
> `development/ROADMAP.md` and the constraints in
> `development/PHASE_00_HANDOFF.md` section 8. Implement the synthetic sheet
> generator first, then preprocessing, marker detection, orientation resolution
> and the perspective transform, with measured accuracy tests. Do not implement
> bubble recognition or the template designer.

**Definition of done for Phase 1**

`pytest`, `ruff check .` and `mypy` pass; alignment accuracy is a measured number
with a regression threshold; the degradation limit is documented;
`CURRENT_STATE.md` is updated and `PHASE_01_HANDOFF.md` is written.
