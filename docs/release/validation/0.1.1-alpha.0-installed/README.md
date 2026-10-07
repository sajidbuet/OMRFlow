# `0.1.1-alpha.0` installed-build evidence (revised phase 10)

Recorded 2026-10-07 on the build machine (Windows 11 Education 10.0.26200,
AMD64, 16 logical CPUs, 31.4 GiB; PowerShell 7.6.6; Python 3.12.7 for the
harness). **Not released; not tagged.** Handoff:
[`PHASE_J_HANDOFF.md`](../../../../development/releases/0.1.1-alpha.0/PHASE_J_HANDOFF.md).

## The candidate

| | |
|---|---|
| Source commit | `73e364b` (branch `feat/0.1.1-phase10-release-gate`, clean tree; the build recorded it, no dirty-tree warning) |
| Version / channel / build identifier | `0.1.1-alpha.0` / Alpha / `0.1.1-alpha.0+73e364b` (first-launch log line) |
| Installer | `OMRFlow-0.1.1-alpha.0-Setup-x64.exe`, 85,862,153 bytes, SHA-256 `f69a02e1dd5eb81d0cdc09d3ac3b86b0b873b70e360e8874f0c75362c2724328` (`packaging/candidate-SHA256SUMS.txt`); **unsigned** (Authenticode: NotSigned) |
| Application | `OMRFlow.exe` 12,785,528 bytes, SHA-256 `ac453449bfc258fa16b9f9b1ced9ee89ec86d0331b0168f119ca618a29c23a78`; bundle 957 files, 298,845,460 bytes; Windows metadata `0.1.1-alpha.0` |
| Built by | `scripts/release/Build-App.ps1 -Clean`, `Build-Installer.ps1` (Inno Setup 6.7.3), `New-Checksums.ps1` |
| Installed as | per-user, `%LOCALAPPDATA%\Programs\OMRFlow`, **in place over an installed `v0.1.0-alpha.2`** (silent: `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER`) |

The installers GitHub's *Release* workflow builds from a tag are separate
artifacts; their hashes will differ from this local candidate's.

## What was run, and the result

| Check | Folder | Result |
|---|---|---|
| Packaging smoke: `Test-PackagedApp.ps1`; `audit_dependencies.py`; `verify_frozen_imports.py` | `packaging/packaging-checks.log` | **PASS** - 16/16; 0 unresolved imports, C/C++ runtime bundled; every imported dependency frozen |
| Installer: `Test-InstallerRoundTrip.ps1`, `Test-SelfContained.ps1`, `Invoke-ReleaseVerification.ps1` | `packaging/installer-checks.log` | **PASS** - 15/15, 14/14, 14/14 |
| Release-validation framework `--build --packaged --installer` | `packaging/release-validation/` | **PASS** - 78 checks, 0 failed, 3 skipped (licence page, Alpha warning, SmartScreen: not observable silently). Its "RELEASE QUALIFIED" line covers those stages only, not the release gate. A first run failed 2 checks through a framework defect (relative `--results-dir`), fixed in `87d9fa5` |
| In-place upgrade install over `v0.1.0-alpha.2` | `packaging/install-*.excerpt.log` | **PASS** - one uninstall entry afterwards (`0.1.1-alpha.0`), user data kept, installed `OMRFlow.exe` byte-identical to the built one. Three DLLs of the old build remain (`libcrypto-3`, `libssl-3`, `libffi-8`; unused) |
| **Installed-build live intake** (continuous engine *inside the installed `OMRFlow.exe`*, two local sources) | `installed-intake/` | **ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION** (the best verdict a small run can have): 249 files, 178 written partially, 108 names at both scanners, a source outage, **5 real `TerminateProcess` kills of the installed executable + 1 clean close** (6 restarts, 8 incarnations, all `frozen`), 29 files written while it was down; all 16 assertions, crash cases 1–15, endurance A–E **PASS**; results and report cells equal an independent reference |
| — first attempt | `installed-intake/attempts/…-FAILED-case13-not-exercised/` | **FAILED** - crash case 13 (S1) *not exercised*: its pause point (armed at 10 %, one duplicate-ID target) was not reached within the harness's 180 s deferral on this small two-source run with the production stability policy; every other assertion and case passed. Harness-configuration issue, not a product defect; the rerun armed it at 45 % |
| **Installed GUI after a real kill** (S2, S3, R1) | `gui-recovery/` | **PASS** - the installed executable killed with 43 sheets committed and 3 in a worker; the installed GUI then showed Resolve (opened first, Scan never visited) with the persisted session's queue `9 total 4 unresolved 4 resolved 1 rescan required 2 suggested rescan` = the production services' summary of the committed rows (R1), and Scan `43 / 58 read` = 43 committed, one session, "not running in this window" (S2, S3) |
| **Upgrade: a `v0.1.0-alpha.2` project** | `upgrade/v0.1.0-alpha.2-project/` | **PASS** - written by the `v0.1.0-alpha.2` code (its own release-validation workflow suite run from the `v0.1.0-alpha.2` tag: 5 sheets recognised, 6-candidate roster, reconciliation, verified key, 6 results, 1 generated report; schema 9), opened in the **installed `v0.1.0-alpha.2`** (stays schema 9, shows 6 / 5 scored / 1 absent), then in the installed candidate: migrations 10–17, backup `before-migration-9-to-17` (schema 9, every row equal to the original), every old row and value unchanged, one backfilled session, integrity and Health clean, all nine stages walked; the same 6 / 5 / 1 shown |
| **Upgrade: schema-12 fixtures** | `upgrade/schema12-*` | **PASS** - `unique_sets` and `colliding_sets` (written by the schema-12 build `0ed96ed`): migrations 13–17, backups, every old value kept; `colliding_sets` keeps `a` without a canonical code and Health reports `SET_CODE_COLLISION`, as designed |

Every application launch used `OMRFLOW_CONFIG_DIR` / `OMRFLOW_LOG_DIR` in the
work folder (none touched the operator's profile) and UI Automation's
*Invoke* (no mouse).

## Packaged SQLite (from every installed coordinator incarnation)

```text
SQLite library 3.45.3 (the bundled sqlite3.dll; same as the build venv)
journal_mode   delete   (rollback journal - not WAL)
synchronous    2        (FULL)
busy_timeout   5000 ms
foreign_keys   1
```

## Forward-only consequence (checked)

After the installed candidate opened the `v0.1.0-alpha.2` project, the
`v0.1.0-alpha.2` code refuses it, writable and read-only, with *"The project
file is not valid and could not be opened."* - `project.json` gained
`active_template`, which its strict reader rejects. The pre-migration backup
covers the database only, so **a copy of the whole project folder** is what
lets a project go back.

## What this does not prove

Genuine SMB (local folders only; the outage is a folder link), real scanners
or paper, power loss (process termination), a clean machine (Windows Sandbox
is not available on this machine; not repeated for this build), a person
operating the installed GUI (UI Automation walked it), other machines or
Windows 10, recognition accuracy on real scans, production readiness. The
supervisor, scanner writers, finite control and evaluator of the intake
campaign run from source; only the application under test is the installed
executable.
