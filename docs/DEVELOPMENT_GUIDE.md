# Development guide

## Requirements

- Python 3.12 or newer (3.12 is what the project is developed and tested on).
- Git.
- A desktop session for running the GUI. Headless machines can still run the
  test suite; see [Testing](#testing).

## Environment setup

```powershell
git clone https://github.com/sajidbuet/OMRFlow.git
cd OMRflow
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
pip install -e ".[dev]"
```

On Linux/macOS the activation line is `source .venv/bin/activate`; everything
else is identical.

`pip install -e ".[dev]"` installs the application in editable mode plus pytest,
pytest-qt, pytest-cov, Ruff and mypy.

### Why the dependency list contains packages nothing imports yet

`pyproject.toml` declares the full committed stack (OpenCV, NumPy, pandas,
openpyxl) even though Phase 0 uses none of them, so that one install command
produces an environment that works for every phase. Each entry is annotated with
the phase that first uses it. OpenCV is pinned to `opencv-python-headless`
deliberately: the regular wheel bundles its own Qt plugins, which can shadow
PySide6's and break GUI startup.

## Running the application

```bash
python -m omr_scanner            # from a source checkout
omrflow                          # console entry point after installation
omrflow "C:/exams/midterm"       # open a project on start-up
omrflow --log-level DEBUG
```

## Testing

```bash
pytest                           # whole suite
pytest tests/unit                # fast, no Qt
pytest -m gui                    # GUI tests only
pytest --cov=omr_scanner         # with coverage
```

GUI tests need a Qt platform plugin. On a headless Linux runner the test
configuration selects the offscreen plugin automatically; you can force it with
`QT_QPA_PLATFORM=offscreen`.

Tests never touch real user data: an autouse fixture redirects the per-user
configuration and log directories into the test's temporary folder through the
`OMRFLOW_CONFIG_DIR` and `OMRFLOW_LOG_DIR` environment variables.

See `docs/TESTING.md` for the strategy and the fixture policy.

## Working on the Qt GUI: the `qtguitesting` skill

`.claude/skills/qtguitesting/` is a repository-local Claude Code skill covering
every Qt surface in OMRFlow - the graphics canvas, region geometry, mouse
interaction, zoom and pan, resize handles, property panels, dialogs, toolbar
layout and visual rendering. It is committed with the project and applies to the
Scan, Resolve and Results pages as they arrive, not only to the Template page.

**What it is.** A workflow (identify the invariants, test at the lowest level
that can reach the bug, fix the model rather than the symptom, verify against the
real sheet, look at the evidence), plus four runnable scripts and two reference
documents. Claude loads it automatically when a task touches the GUI; a human
reads `SKILL.md` and runs the same commands.

**When it is worth reaching for.** Any position, resize, drag or overlay
alignment problem - those are almost always one Qt coordinate system mistaken for
another, and
`.claude/skills/qtguitesting/references/qt_coordinate_systems.md` lays out the
six of them, which OMRFlow uses where, and the invariants to assert.

**The same commands a human runs:**

```bash
# Fast sanity check: app starts, page builds, sample loads, controls exist.
python .claude/skills/qtguitesting/scripts/run_gui_smoke_tests.py

# Deterministic screenshots of the states worth looking at.
python .claude/skills/qtguitesting/scripts/capture_gui_states.py

# Model / item.pos() / boundingRect() / sceneBoundingRect(), side by side.
python .claude/skills/qtguitesting/scripts/dump_gui_geometry.py --scenario columns
python .claude/skills/qtguitesting/scripts/dump_gui_geometry.py --scenario resize

# Compare two captures with tolerances rather than byte equality.
python .claude/skills/qtguitesting/scripts/compare_gui_images.py a.png b.png
```

**Where the diagnostics go.** `test-output/gui/` - screenshots at the top level,
JSON geometry dumps under `geometry/`, and failure artefacts under `failures/`.
The whole directory is git-ignored: it is regenerated on demand and is never a
committed baseline.

**The real sample.** `examples/ECE-0000.png` is a real scanned OMR page and the
project's standard GUI regression image. Never modify it, and never hard-code its
coordinates into `src/` - `tests/unit/test_qtguitesting_skill.py` asserts both.

**One rule worth repeating here:** screenshot similarity is not proof of GUI
correctness. Position invariants, model synchronisation, signal emission and
serialisation are asserted through program state; screenshots catch clipping,
spacing, overlay alignment and obvious layout regressions, and nothing else.

## Linting and type checking

```bash
ruff check .                     # lint
ruff check . --fix               # apply safe fixes
mypy                             # strict type check of src/omr_scanner
```

All three commands must pass before a phase is considered complete.

### Configured exclusions, and why

Nothing is silenced in bulk. The complete list of exceptions:

| Rule | Where | Reason |
|---|---|---|
| `D107` (missing `__init__` docstring) | project-wide | Constructor arguments are documented in the class docstring's `Args:` section, per the Google style. A second docstring would duplicate and drift. |
| `ANN401` (`Any` disallowed) | project-wide | Unavoidable at the Qt and SQLAlchemy DBAPI boundaries. |
| `N802` for `closeEvent` and friends | `pep8-naming` config | Qt method overrides must keep Qt's camelCase spelling. The list was extended in Phase 2 to cover the mouse/hover/wheel/key/drag event overrides the template designer's canvas and graphics items implement (`mousePressEvent`, `wheelEvent`, `drawBackground`, `itemChange`, and similar). |
| `D101/D102/D103`, `ANN001/ANN201`, `ARG001` | `tests/*` | Test names describe the test; fixtures are often requested only for their side effect. Module docstrings are still required. |
| `warn_unreachable` off | `omr_scanner.config.paths` | The module branches on `sys.platform`; mypy analyses one platform at a time and reports the other branches as unreachable. |
| `ignore_missing_imports`, `follow_imports = silent` | `PySide6.*` | PySide6's generated stubs are incomplete and Qt's signal/slot machinery is not expressible in the type system. Our own code stays strictly checked. |

## Where settings live

Before adding a constant, decide which of these it is:

| Kind | Home | Module |
|---|---|---|
| Application default, per user | `omrflow.config.json` | `omr_scanner.config.app_config` |
| Machine capability, per user | `omrflow.config.json` (`processing`) | `omr_scanner.config.processing` |
| Project metadata | `<project>/project.json` | `omr_scanner.domain.project` |
| Sheet design | the `.omrt` document | `omr_scanner.domain.template` |
| Recognition threshold | `recognition` block inside the template | `omr_scanner.domain.template` |
| Genuinely local constant | module-level constant next to its only user | - |

Scattering tunable values across modules is the failure mode this table exists
to prevent.

## Contribution conventions

**Code**

- Type hints on every public function; `mypy` runs in strict mode.
- Every module starts with a docstring stating its purpose, its
  responsibilities, what does **not** belong in it, and any invariant.
- Comments explain *why*, not *what*. Do not comment obvious syntax.
- `pathlib.Path` everywhere; no string path concatenation (Ruff enforces `PTH`).
- Small cohesive modules over large ones.
- No global mutable state. Configuration is immutable and passed explicitly.

**Changes**

- One concern per pull request. Do not mix an architectural change with a
  feature.
- Add or update tests with the change. A bug fix gets a regression test.
- Update the documentation that the change invalidates, in the same commit -
  especially `docs/ARCHITECTURE.md` when a boundary moves, and
  `docs/DATA_MODEL.md` when an entity changes.
- Never claim a feature works before it does. Placeholder pages say which phase
  will implement them.

**Phase discipline**

Development proceeds in the phases listed in `development/ROADMAP.md`. At the
end of a phase:

1. `pytest`, `ruff check .` and `mypy` all pass;
2. `development/CURRENT_STATE.md` is updated;
3. a `development/PHASE_NN_HANDOFF.md` is written;
4. `CHANGELOG.md` gains an entry.

## Adding a database migration

1. Add the table or column to `omr_scanner/database/models.py`.
2. Append a `Migration` with the next consecutive version to `MIGRATIONS` in
   `omr_scanner/database/migrations.py`.
3. Add a test that opens a database created at the previous version and asserts
   the upgrade succeeds.

Migrations are forward-only; `SCHEMA_VERSION` is derived from the tuple and is
never edited by hand. Rationale: `docs/decisions/ADR-0003-schema-migrations.md`.

**Adding a column to `audit_event`** needs one extra thought, because that
table carries triggers that abort any `UPDATE`. `ALTER TABLE ... ADD COLUMN` is
a schema change and does not fire them, but a statement that *backfills* the
new column does — correctly. Choose a `DEFAULT` that is already true for every
existing row, as migration 4 did (`entity_type DEFAULT 'conflict'`), so no
backfill is needed. If you genuinely cannot, you are proposing to rewrite
history; stop and reconsider.
