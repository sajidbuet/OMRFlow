# Development guide

## Requirements

- Python 3.12 or newer (3.12 is what the project is developed and tested on).
- Git.
- A desktop session for running the GUI. Headless machines can still run the
  test suite; see [Testing](#testing).

## Environment setup

```powershell
git clone https://github.com/sajidbuet/OMRflow.git
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
| `N802` for `closeEvent` and friends | `pep8-naming` config | Qt method overrides must keep Qt's camelCase spelling. |
| `D101/D102/D103`, `ANN001/ANN201`, `ARG001` | `tests/*` | Test names describe the test; fixtures are often requested only for their side effect. Module docstrings are still required. |
| `warn_unreachable` off | `omr_scanner.config.paths` | The module branches on `sys.platform`; mypy analyses one platform at a time and reports the other branches as unreachable. |
| `ignore_missing_imports`, `follow_imports = silent` | `PySide6.*` | PySide6's generated stubs are incomplete and Qt's signal/slot machinery is not expressible in the type system. Our own code stays strictly checked. |

## Where settings live

Before adding a constant, decide which of these it is:

| Kind | Home | Module |
|---|---|---|
| Application default, per user | `omrflow.config.json` | `omr_scanner.config.app_config` |
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
