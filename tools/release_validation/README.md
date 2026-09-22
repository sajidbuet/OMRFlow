# Release qualification

A self-contained, unattended qualification run for an OMRFlow release. Open
PowerShell in the repository, start it, and leave:

```powershell
python tools\release_validation\validate_release.py --all
```

It asks nothing while it runs. Every decision is made by the check that made
the observation, recorded with a reason, and folded into an exit code.

---

## Contents

- [What it checks](#what-it-checks)
- [Prerequisites](#prerequisites)
- [Running it](#running-it)
- [Command-line options](#command-line-options)
- [Reports](#reports)
- [Exit codes](#exit-codes)
- [Visual baselines](#visual-baselines)
- [Safety](#safety)
- [What it does not cover](#what-it-does-not-cover)
- [Architecture](#architecture)
- [Troubleshooting](#troubleshooting)

---

## What it checks

| Stage | Blocking | What it answers |
|---|---|---|
| **Environment** | yes | Can this machine run the qualification at all? |
| **Source tests** | yes | Does the repository's own suite pass, invoked the canonical way? |
| **Qt GUI tests** | yes | Does the interface work — navigation, geometry, menus, project lifecycle, dialogs, settings? |
| **Workflow smoke test** | yes | Do the nine stages actually connect, from *Create Project* to a generated workbook? |
| **Accessibility checks** | **no** | Accessible names, keyboard reach, focus, disabled-state explanations — graded ERROR/WARNING/INFO |
| **Visual regression** | **no** | Do the screenshots still match the committed baselines, within tolerance? |
| **Build/package verification** | yes | Is the built bundle complete and correctly versioned? |
| **Packaged application launch** | yes | Does `dist\OMRFlow\OMRFlow.exe` start, show a window, stay responsive and close cleanly? |
| **Installer qualification** | yes | Install → launch → uninstall → **user data survives** → reinstall |
| **Cleanup** | no | Were processes stopped and temporary data removed? |

Two stages are deliberately **non-blocking**. Accessibility: the project's own
checklist requires only an *initial* review at Alpha and a full audit from
Beta, so an unnamed combo box is reported, not made release-blocking — use
`--strict-accessibility` when that is wanted. Visual regression: §6 of the
framework's brief is explicit that a functional failure must not hinge on pixel
equality, and a layout somebody meant to change should not fail a release.

---

## Prerequisites

- Windows 10 1809 or newer, 64-bit
- Python 3.12+
- The repository installed in editable mode
- **A real desktop session** for the GUI, accessibility and packaged stages.
  With `QT_QPA_PLATFORM=offscreen` the GUI stage records itself as SKIPPED
  rather than passing vacuously.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev,validation]"
```

The `validation` group adds only what the qualification itself needs:

| Package | Used for |
|---|---|
| `pytest-qt` | driving Qt widgets in the GUI and accessibility stages |
| `pywinauto` | UI Automation against the **packaged** application |
| `pillow` | comparing screenshots with the baselines |

All three are optional at run time. A missing one **narrows** the run and says
so; it never silently passes a stage. Building (`--rebuild`) additionally needs
`pip install -e ".[packaging]"`, and compiling an installer needs Inno Setup 6
(`winget install --id JRSoftware.InnoSetup`).

---

## Running it

```powershell
# Safe: everything except install/uninstall. The default.
python tools\release_validation\validate_release.py --safe

# Everything, including the installer round trip.
python tools\release_validation\validate_release.py --all

# One stage at a time.
python tools\release_validation\validate_release.py --gui
python tools\release_validation\validate_release.py --packaged
python tools\release_validation\validate_release.py --installer "dist\installer\OMRFlow-0.1.0-alpha.1-Setup-x64.exe"
```

Or through the launcher, which finds the virtual environment and checks the
dependencies before starting:

```powershell
.\tools\release_validation\Run-ReleaseValidation.ps1
.\tools\release_validation\Run-ReleaseValidation.ps1 -All
```

The full `--all` run takes roughly 35–45 minutes, almost all of it the source
suite.

---

## Command-line options

### What to run

| Option | Effect |
|---|---|
| `--all` | Every stage, including the destructive installer round trip |
| `--safe` | Every stage except the installer. **The default** when nothing is named |
| `--source` | The repository's pytest suite |
| `--gui` | Qt GUI functional checks |
| `--workflow` | The end-to-end workflow smoke test |
| `--accessibility` | Accessibility checks against the Qt tree |
| `--visual` | Compare screenshots with the committed baselines |
| `--build` | Verify the built bundle |
| `--packaged` | Launch and drive `dist\OMRFlow\OMRFlow.exe` |
| `--installer [PATH]` | Install/launch/uninstall/reinstall. Without a path, the newest in `dist\installer` |
| `--installed-app-only` | Check an already-installed OMRFlow. Installs nothing |
| `--stress` | Also run the `stress`-marked suite. Slow; excluded from `--all` |

### Behaviour

| Option | Effect |
|---|---|
| `--rebuild` | Run `Build-App.ps1 -Clean` before verifying the bundle |
| `--quick` | Source stage runs `tests/unit` only. For iterating, **not** for a release — the report says so |
| `--fail-on-warning` | Exit non-zero when anything is a warning |
| `--strict-accessibility` | Make accessibility errors release-blocking |
| `--update-visual-baselines` | Overwrite the committed screenshots with this run's |
| `--replace-installation` | Allow the installer stage to remove an OMRFlow that is already installed |
| `--kill-stray-processes` | During cleanup, also stop OMRFlow processes this run did not start |
| `--keep-artifacts` | Keep the temporary workspace (projects, rendered sheets) |
| `--results-dir PATH` | Where reports go. Default `validation-results\<timestamp>` |
| `--verbose` | More console output |

---

## Reports

Every run writes a timestamped directory:

```text
validation-results/2026-09-22_155705/
    validation-summary.md          human-readable, failures first
    validation-results.json        machine-readable, for CI ingestion
    screenshots/                   one PNG per checkpoint
    logs/                          stdout/stderr of everything launched
    artifacts/                     JUnit XML, accessibility findings
```

`validation-results.json` carries, for every check: `name`, `status`
(`PASS`/`FAIL`/`WARNING`/`SKIPPED`), `duration_seconds`, `detail`, `reason`,
`exception` and `artifacts`. The whole directory is git-ignored — it is
evidence *about* a build, not part of one. The single report a release keeps
lives in `docs/release/validation/`.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Every release-blocking stage passed |
| `1` | A release-blocking stage failed, **or nothing was verified** |
| `2` | The run could not start — bad arguments, or an unusable environment |
| `130` | Interrupted (Ctrl-C). Cleanup still ran |

Warnings alone do not change the exit code unless `--fail-on-warning` is given.

**"Nothing was verified"** means every stage you asked for was skipped — no
installer present, no desktop, nothing installed. The console says
`NOTHING VERIFIED` and names the reason for each skip. It exits non-zero
deliberately: a step that was not run is not a pass, and `0` would claim it
was.

---

## Visual baselines

Committed in `tools/release_validation/baselines/`, one PNG per checkpoint.
Comparison allows a per-channel difference of 12/255 and up to 1.5% of pixels
differing, which absorbs font hinting, anti-aliasing and display-scale
variation while still catching a moved button or a missing icon. A change in
image *size* is reported outright — that is a layout change, not rendering
noise.

Baselines never change during an ordinary run. To update them deliberately:

```powershell
python tools\release_validation\validate_release.py --gui --visual --update-visual-baselines
git diff --stat tools/release_validation/baselines/
```

Review the diff before committing.

---

## Safety

- **Isolation.** Every launched OMRFlow gets `OMRFLOW_CONFIG_DIR` and
  `OMRFLOW_LOG_DIR` pointed into the run's workspace, so a qualification run
  cannot touch the operator's real settings, logs or recent-project list. Test
  projects live in `tmp_path` or the workspace and nowhere else.
- **No existing installation is destroyed.** The installer stage refuses to run
  when OMRFlow is already installed unless `--replace-installation` is given,
  and says so in the report.
- **User data is never deleted.** `%LOCALAPPDATA%\OMRFlow` is inspected and
  what remains after an uninstall is *recorded* — checking that the installer
  keeps it means leaving it alone.
- **Only our processes are stopped.** Cleanup stops processes this run
  launched, by process identity. A sweep by name is opt-in
  (`--kill-stray-processes`).
- **Cleanup always runs**, in a `finally`, including after a crash or Ctrl-C.
- **A stage that raises does not kill the run.** It becomes a recorded failure
  with its traceback, and the remaining stages still run.

---

## What it does not cover

Stated plainly, because a qualification harness that implies more than it does
is worse than none:

- **Not WCAG conformance.** The accessibility stage checks Qt's accessibility
  model. It cannot judge whether a name is *meaningful*, whether a reading
  order makes sense, or whether a colour pairing is comfortable. The manual
  review is `docs/release/ACCESSIBILITY_CHECKLIST.md`.
- **Not a clean-machine test.** Everything here runs on the machine that built
  the artifact. A DLL a developer tool left in `System32` is invisible to it.
  That is `docs/release/CLEAN_MACHINE_TEST.md` and Windows Sandbox.
- **Not real-data qualification.** The workflow smoke test uses six synthetic
  candidates. Real scanned cohorts and real attendance workbooks are Phase 11B.
- **Not the 100,000-sheet run.** Deliberately — see `--stress` and
  `run_phase10_100k_qualification.ps1`.
- **SmartScreen, the licence page and the Alpha warning during installation**
  cannot be observed by a silent install. The installer stage records them as
  *not performed* rather than assuming them.

---

## Architecture

```text
tools/release_validation/
    validate_release.py      CLI and orchestration
    config.py                paths, timeouts, per-run settings
    results.py               CheckResult / StageResult / RunResult
    reporting.py             JSON, Markdown and the console summary
    junit.py                 pytest JUnit XML -> result rows
    pytest_runner.py         runs a pytest selection in a subprocess
    process_utils.py         launching, window inspection, safe termination
    environment_checks.py    stage: prerequisites
    source_tests.py          stage: the repository's suite
    gui_tests.py             stage: GUI functional + workflow smoke
    accessibility_tests.py   stage: accessibility, graded by severity
    visual_tests.py          stage: screenshot comparison
    build_tests.py           stage: bundle contents and version
    packaged_app_tests.py    stage: the real .exe, via Win32 and pywinauto
    installer_tests.py       stage: install / uninstall / reinstall
    cleanup.py               always-runs teardown
    suites/                  the pytest files the GUI stages execute
    baselines/               committed screenshots
```

The GUI, accessibility and workflow stages are ordinary pytest files under
`suites/`, run in a **subprocess**. Qt, OpenCV and the worker pool all live in
C, and a fault in any of them takes down its interpreter; in a subprocess that
is a failed stage with a log rather than a destroyed forty-minute run. JUnit
XML is how the results come back.

**CI compatibility.** The source and workflow stages need no desktop and could
be adopted by a GitHub Actions job unchanged. The GUI, accessibility, packaged
and installer stages need an interactive Windows desktop and are local
qualification.

---

## Troubleshooting

**`Qt GUI tests SKIPPED — QT_QPA_PLATFORM is set to an offscreen platform`**
Unset it and run from a real desktop session: `Remove-Item Env:QT_QPA_PLATFORM`.

**`Build/package verification SKIPPED — no application bundle`**
Build one: `.\scripts\release\Build-App.ps1 -Clean`, or pass `--rebuild`.

**`Installer qualification SKIPPED — OMRFlow is already installed`**
Intentional. Uninstall it first, or pass `--replace-installation` if that
installation is expendable.

**`optional: pywinauto — not installed`**
The packaged stage still launches the application and checks it through Win32;
only the UI Automation checks are skipped. `pip install -e ".[validation]"`.

**The workspace could not be removed**
Usually a SQLite handle Windows has not released. Reported as a warning, never
a failure. Delete `validation-results\<run>\workspace` by hand.

**A stage failed with a traceback in the report**
That is a bug in the framework, not necessarily in OMRFlow. The traceback is in
`validation-summary.md` under *Failures*, and the run continued.
