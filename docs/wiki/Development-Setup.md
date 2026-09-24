# Development Setup

Requires **Python 3.12 or newer**. Windows for the GUI and packaging work;
the engine and its tests are platform-neutral.

```powershell
git clone https://github.com/sajidbuet/OMRFlow.git
cd OMRFlow
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Run it:

```powershell
python -m omr_scanner.main
```

This is also the way to run OMRFlow if your organisation will not allow an
unsigned installer.

## The quality gates

All three must pass. There is no "the linter is wrong" exemption — if a rule
is genuinely wrong, change the rule in `pyproject.toml` in its own commit and
say why.

```powershell
ruff check src tests
mypy src/omr_scanner
pytest
```

Or in one step, with a summary:

```powershell
.\scripts\release\Invoke-Tests.ps1              # everything
.\scripts\release\Invoke-Tests.ps1 -Fast        # unit only, a couple of minutes
```

The full suite is roughly half an hour on an idle machine and considerably
longer on a busy one: it processes real images, opens real databases and
drives real Qt widgets.

## Building a distributable

Needs the extra packaging dependencies, and Inno Setup for the installer:

```powershell
pip install -e ".[dev,packaging]"
winget install --id JRSoftware.InnoSetup
```

See [Release Process](Release-Process).

## Where to read next

- [Architecture](Developer-Architecture) — and `docs/ARCHITECTURE.md` before
  adding a module
- [Testing](Testing) — especially the GUI testing policy
- [Contribution Guide](Contribution-Guide)
- `docs/DEVELOPMENT_GUIDE.md` — environment detail, settings locations, and
  how to add a database migration
