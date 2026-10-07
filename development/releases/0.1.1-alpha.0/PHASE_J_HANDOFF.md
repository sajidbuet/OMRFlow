> `PHASE_J_HANDOFF.md` is the handoff for revised Phase 10, implementing the SMB / installed-build / Alpha-release-gate half of roadmap phase 0.1.1-G.

# Revised phase 10 handoff — SMB, installed build, upgrades and the Alpha release gate (roadmap 0.1.1-G, second part)

Date: 2026-10-07. Branch `feat/0.1.1-phase10-release-gate` (worktree
`C:\Research\OMRflow-p10`), from `main` at `acad683`. **Not merged, not
tagged, not released. No migration: schema stays 17.** `PHASE_I_HANDOFF.md`
and the earlier handoffs are not modified.

## 1. Phase verdict

```text
NOT READY FOR RELEASE — BLOCKERS REMAIN
```

Computed, not typed: `python -m tools.release_validation.release_gate` over
`docs/release/validation/0.1.1-alpha.0-release-gate/release-gate.json`.
Blocking: **§8 gate 3, the real SMB qualification - NOT PERFORMED** (the only
machine available is one laptop; no second Windows machine, no share), and
**gate 9, the release checklist - NOT PERFORMED** (its clean-machine and
person-operated items and the SMB gate are open; publication is the owner's).

Everything Phase 10 could do on one machine was done and passed: the
installed-build live intake with real kills of the installed executable,
S1/S2/S3/R1 on the installed build, upgrades from a real `v0.1.0-alpha.2`
project and from schema 12, every packaging and installer check, and the SMB
tooling and procedure (rehearsed locally - never SMB evidence).

## 2. Git state

```text
branch:       feat/0.1.1-phase10-release-gate
baseline:     main acad683 (docs: record revised phase 9 as merged (459c65b))
candidate:    73e364b (the installer was built from it; clean tree)
tip:          the commit carrying this handoff
commits:      <<COMMITS>>
working tree: clean
pushed:       <<PUSHED>>
merged:       no
tagged:       no
released:     no
schema:       17 (no migration)
```

```
d75c760 feat(qualification): run the intake coordinator inside the packaged executable
1e7d493 feat(smb-qualification): genuine-SMB intake qualification tooling and rehearsal
73e364b feat(release-validation): installed-build upgrade and GUI recovery checks      <- candidate
87d9fa5 fix(release-validation): hand launched applications absolute profile paths
ef8961b feat(release-validation): computed release-gate verdict; set-collision rule in the upgrade check
54e549a docs(release): SMB qualification procedure - not performed, real infrastructure required
a5713cd docs(release): installed-build evidence for the 0.1.1-alpha.0 candidate
cd79270 docs: revised phase 10 status - release gate not met (SMB not performed)
<<LATER>>
```

Product code changed: `src/omr_scanner/main.py` only - a hidden first argument
`--intake-qualification-coordinator` (not in `--help`) that runs the
intake-qualification coordinator instead of the GUI - and one hidden import in
`packaging/omrflow.spec`. Everything else is evaluation / release tooling,
tests and documentation. Commits after `73e364b` change only release tooling
(`tools/release_validation/`), tests and documentation - nothing in the
packaged application.

## 3. Incoming post-Phase-9 gate

The canonical gate the owner started on `main` after the phase 9 merge,
monitored (2 h wait, then checked): `Scratch\Log\2026-10-07_071006` in the main
checkout.

```text
main commit: acad683e7dbc03ac34275bd34ecc379a6b81e92f
pytest:      7,197 passed, 16 skipped, 9 deselected (stress), 0 failed - exit 0 (1 h 34 min)
ruff:        All checks passed - exit 0
mypy:        Success: no issues found in 261 source files - exit 0
overall:     OVERALL RESULT: PASS; git state unchanged during testing
log:         C:\Research\OMRflow\Scratch\Log\2026-10-07_071006\summary.log
```

## 4. Phase 9 evidence carried forward

`p9-release-20261006T151124Z` at `542b2af`: **`QUALIFIED`** - 3 scanner-writer
processes, 10,189 files, 4 sets, all 16 §5.2 assertions, crash cases 1–15 with
the 1 / 25 / 50 / 75 / 99 % series, endurance A–E, stress 9/9
(`docs/release/validation/0.1.1-alpha.0-phase9-intake/`, `PHASE_I_HANDOFF.md`).
**Kept valid, not re-run:** phase 10 changed no correctness-critical
production semantics (intake, registration, deduplication, sessions, the
engine, recovery, persistence, conflicts, rescans, scoring, reports are
untouched); the one product change is an entry-point branch that the GUI path
never takes.

## 5. SMB environment

```text
OMRFlow machine:   SAJID-ASUS-LAPT (DNS name Sajid-Asus-Laptop), Windows 11 Education 10.0.26200
remote machine(s): none available
Windows versions:  -
SMB/share details: none - no mapped drives, no configured UNC source, only the administrative shares
                   (ADMIN$, C$, IPC$); one Wi-Fi adapter on a network profile categorised Public;
                   Get-SmbConnection needs elevation here
database location: -
```

Discovery was limited to this machine (host name, adapters and addresses, SMB
mappings and shares, `net use`, environment); no LAN scan. **SMB status: NOT
PERFORMED — REAL INFRASTRUCTURE REQUIRED.**

## 6. SMB workload (prepared, not run)

```text
sources:          2 scanner PCs (\\HOST\share\...), written by Invoke-OmrflowScannerWriter.ps1 on each
files/source:     >= 1,000 (the Phase 9 cohort, grown until every source has its minimum)
partial writes:   stepped, long pause, header first, held open, .part + rename
same-name cases:  names written at both scanners (Phase 9 cohort)
outage:           a person disconnects scanner B's share (adapter / cable / LanmanServer) at ~60 %,
                  observed unreachable here and in the project, kept away >= 120 s, observed back
restart:          OMRFlow killed (TerminateProcess) at ~35 % with a sheet in a worker; restarted
                  after >= 10 files were written while it was down
```

Procedure: [`docs/release/SMB_QUALIFICATION.md`](../../../docs/release/SMB_QUALIFICATION.md).

## 7. SMB results

| Assertion | Status |
|---|---|
| all 15 (`genuine_smb_topology` … `project_health`) | **NOT PERFORMED** |

**Rehearsal (tooling only, local folders, not SMB):** source build `1e7d493`
and the `stress`-marked `tests/integration/test_smb_rehearsal.py`: two local
PowerShell writers (50 + 81 files), one real kill with a sheet in a worker and
restart, a simulated outage by removing a folder link; every assertion that can
run on one machine `PASS`, `genuine_smb_topology` and `scale_and_workload`
*not exercised*, verdict **`NOT SMB QUALIFICATION`**. The first rehearsal found
a real defect in the tooling: the writer reports its NetBIOS name
(`SAJID-ASUS-LAPT`), which differs from the DNS host name, so a writer on the
OMRFlow machine would not have been caught - fixed before commit, with a test.

## 8. SMB performance

```text
listing median/p95/max:        not measured on SMB (rehearsal, local folder: 0.0003 / 0.0009 / 0.0022 s)
stabilization median/p95/max:  not measured on SMB (rehearsal, local, fast policy: 2.64 / 10.78 / 22.91 s)
```

The network stability defaults (`NETWORK_POLICY`: quiet 15 s, poll 30 s) are
neither confirmed nor changed: there is no SMB measurement to base either on.

## 9. Packaged build

```text
artifact:          OMRFlow-0.1.1-alpha.0-Setup-x64.exe (Inno Setup 6.7.3); bundle dist\OMRFlow (957 files)
version:           0.1.1-alpha.0 (installer and OMRFlow.exe metadata); channel Alpha
build identifier:  0.1.1-alpha.0+73e364b (first-launch log; commit baked by Build-App.ps1, no dirty warning)
size:              85,862,153 bytes (installer); OMRFlow.exe 12,785,528 bytes; bundle 298,845,460 bytes
SHA-256:           f69a02e1dd5eb81d0cdc09d3ac3b86b0b873b70e360e8874f0c75362c2724328 (installer)
                   ac453449bfc258fa16b9f9b1ced9ee89ec86d0331b0168f119ca618a29c23a78 (OMRFlow.exe;
                   the installed one is byte-identical)
signing:           none (Authenticode NotSigned) - SmartScreen will warn
```

Built with the official scripts (`Build-App.ps1 -Clean`, `Build-Installer.ps1`,
`New-Checksums.ps1`) from the clean committed tree. A development build at
`d75c760` was used first to find out whether the frozen coordinator works
(it did: a self-test campaign with real kills of `dist\OMRFlow\OMRFlow.exe`
passed - diagnostic only).

## 10. Installed-build live intake

```text
sources:     2 local scanner folders (Scanner B behind a folder link for its outage), separate writer processes
files:       249 written (243 distinct images; 178 partially: 32 held open, 44 .part + rename, 25 header first,
             16 longer than the quiet period, the rest stepped; 108 names at both scanners; 5 byte copies)
restart:     5 real TerminateProcess kills of the installed OMRFlow.exe (1 at 25 % with 16 in flight, after a
             commit, with a duplicate-ID pass owed, after operator decisions, at 75 % with 4 in flight) and
             1 clean close: 6 restarts, 8 frozen incarnations (interrupted + control); 29 files written while
             it was down; restart 0.06-0.17 s; no orphan worker; 0 lock errors
recognition: the frozen worker pool (4 workers, <= 16 sheets in flight), production local stability policy
recovery:    every kill: committed kept, in-flight returned to pending, integrity and Health clean
Resolve:     each restart's resolve view read the persisted session's queue (case 15)
result:      ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION (16/16 assertions, crash 1–15, endurance A–E)
```

Campaign `p9-custom-20261007T051349Z-20261006`, 1,089 s, configuration
`installed-intake/config.json` (custom: 2 sources, 60 candidates × 4 sets, 240 s
timeline, workers 4, production `LOCAL_POLICY`), coordinator
`%LOCALAPPDATA%\Programs\OMRFlow\OMRFlow.exe` (SHA-256 above). The supervisor,
writers, finite control and evaluator ran from source; the application under
test was the installed executable. The closure, held late file, reopen and
re-close, attendance, scoring and four final reports all ran **inside the
installed executable**; Results and report cells equal the independent
reference.

**First attempt** (`p9-custom-20261007T045426Z`, same build): `FAILED` - every
assertion, every other crash case and every endurance case passed, but crash
case 13 was *not exercised*: its pause point was armed at 10 % on one
duplicate-ID target that had not been committed within the 180 s deferral
(small two-source cohort, production stability policy). Harness configuration,
not a product defect; rerun with the case-13 kill at 45 %, where it landed.
Both reports are committed.

## 11. Installed crash-safety

| Defect | Installed-build evidence | Result |
|---|---|---|
| **S1** recognition and its conflicts one durable unit | crash case 13 in the installed campaign: the installed executable paused right after a commit whose duplicate-ID pass was owed and was killed; on restart recovery created exactly the 2 owed `identifier_duplicate` conflicts (sheets 39 and 120), nothing re-read | **PASS** |
| **S2** interrupted session / batch re-adopted | every installed restart reopened the same session and units (one session, no new or superseding batch; crash cases 3, 9, 14); and the installed GUI after a kill showed the same single session, *"not running in this window"*, nothing started by itself | **PASS** |
| **S3** shown progress = committed work | installed GUI after a kill with 43 committed and 3 in a worker: Scan showed `43 / 58 read`; the database: 43 committed | **PASS** |
| **R1** Resolve reaches persisted work after reopen | installed GUI, Resolve opened first (Scan never visited): `9 total · 4 unresolved · 4 resolved · 1 rescan required · 2 suggested rescan`, equal to the production services' summary of the committed rows, naming the persisted session; crash case 15 in the campaign | **PASS** |

Process termination only - **not a power-loss test.**

## 12. Packaged SQLite

Recorded by every installed coordinator incarnation from its own project
connection:

```text
SQLite version:  3.45.3 (library and connection)
journal_mode:    delete (rollback journal; not WAL - unchanged)
synchronous:     2 (FULL)
busy_timeout:    5000 ms
foreign_keys:    1
```

The project database stayed on the local disk throughout.

## 13. Clean-machine validation

```text
automated:  NOT PERFORMED for this build - Windows Sandbox is not installed on this machine
            (no WindowsSandbox.exe, no wsb CLI; enabling needs elevation and a restart), and
            no other clean machine or VM was available. Substitutes passed: audit_dependencies
            (0 unresolved, C/C++ runtime bundled), verify_frozen_imports, Test-SelfContained 14/14
            - not a clean-machine pass.
manual:     NOT PERFORMED (SmartScreen wording, licence page, Alpha warning, icons, About dialog)
end-to-end: the installed build, headless: recognition by the frozen worker pool, conflicts,
            operator decisions, attendance, scoring and report generation in the installed
            campaign; the installed GUI's nine stages walked by UI Automation on three upgraded
            projects and opened after a kill. NOT PERFORMED: a person driving Create Project ->
            template -> synthetic sheets -> Process All -> Resolve -> Attendance -> Answer Key ->
            Results -> Reports in the installed GUI.
```

## 14. Schema-12 upgrade

Fixtures `tests/fixtures/schema12/unique_sets` and `colliding_sets`, written
by the schema-12 build `0ed96ed` (`PROVENANCE.json`), opened in the installed
candidate by `installed_checks upgrade`: **PASS** both. Migrations 13–17 on
opening (~7 s to the window); backup `before-migration-12-to-17` (schema 12,
every row equal to the original); every row and value the old build wrote
unchanged; one backfilled *Legacy batch* session; canonical set codes; Health:
`SOURCE_SCANS_MISSING` (the fixtures have no images) and, for `colliding_sets`,
`UNRESOLVED_RECONCILIATION_EXCEPTIONS` and `SET_CODE_COLLISION` - set `a` kept
without a canonical code, as designed (A2). All nine stages walked, no dialog,
clean close, no lock left, no ERROR logged.

## 15. `v0.1.0-alpha.2` upgrade

- **The release:** `OMRFlow-0.1.0-alpha.2-Setup-x64.exe` downloaded from the
  GitHub release `v0.1.0-alpha.2`; SHA-256
  `480a7100e39eb633d226a66fe8ac5e7610686c50a9d24f7fd0f84fe5672d9429` equal to
  its `SHA256SUMS.txt`; installed silently per-user (reports `0.1.0-alpha.2`).
- **The project:** written by the `v0.1.0-alpha.2` code - its own
  `tools/release_validation/suites/test_workflow_smoke.py` (12 passed) run from
  a checkout of the tag `v0.1.0-alpha.2` (`809296e`): real recognition of 5
  sheets, a 6-candidate roster, reconciliation, a verified key, 6 results, 1
  generated report; schema 9 (database SHA-256 `d1e8eb49…f56612`). Opened in
  the **installed `v0.1.0-alpha.2`**: title `OMRFlow 0.1.0-alpha.2`, stays
  schema 9, shows 6 candidates / 5 scored / 1 absent. It was *not* created
  through the alpha.2 GUI by a person.
- **The upgrade:** the candidate installer run silently **over** the installed
  `v0.1.0-alpha.2` (no uninstall): one uninstall entry, now `0.1.1-alpha.0`;
  settings and logs kept; installed `OMRFlow.exe` byte-identical to the build.
  Three DLLs of the old build remain unused (`libcrypto-3`, `libssl-3`,
  `libffi-8`); the uninstaller's records cover them.
- **The project in the installed candidate: PASS** - migrations 10–17 on
  opening, backup `before-migration-9-to-17` (schema 9, rows equal), every old
  row and value unchanged, one backfilled session, integrity and Health clean,
  all nine stages walked and showing the same 6 / 5 / 1 and a verified key.

## 16. Migration backup / forward-only behaviour

- Pre-migration backup: created before every migration seen here (12→17,
  9→17), with a manifest (reason, schema, size, SHA-256), schema and rows equal
  to the original; nothing deleted it.
- **Forward-only, and more than the database:** after the installed candidate
  opened the `v0.1.0-alpha.2` project, the `v0.1.0-alpha.2` code refuses it
  writable *and* read-only with *"The project file is not valid and could not
  be opened."* `project.json` gained `"active_template"`, which its strict
  reader rejects before any schema check. Restoring the database backup does
  not change `project.json`, so it does not make the project usable by
  `v0.1.0-alpha.2` again; a copy of the whole folder does. *Upgrade
  Compatibility* had attributed the schema-12 build's "created with a newer
  version" message to `0.1.0-alpha.2` (checked only with `0ed96ed`) -
  corrected; the release notes and Known Limitations say to copy the folder.
- A migration-failure path (a migration that cannot complete leaves the
  project as it was) was **not** exercised on the installed build; it is
  covered by the existing automated migration tests from source only.

## 17. Release-gate matrix

`ACCEPTANCE_CRITERIA.md` §8, evaluated by `tools/release_validation/release_gate.py`:

<<GATEMATRIX>>

## 18. Product defects discovered

| # | Class | Finding | Action | Phase 9 evidence |
|---|---|---|---|---|
| 1 | documentation | *Upgrade Compatibility* said `0.1.0-alpha.2` refuses a migrated project with *"created with a newer version"*; the released build actually says *"The project file is not valid"* (strict `project.json`), and the database backup alone does not undo the change | docs corrected; release notes / Known Limitations: copy the folder | unaffected |
| 2 | documentation | *Installation* placed the settings under `%LOCALAPPDATA%\OMRFlow\`; they are in `%APPDATA%\OMRFlow\omrflow.config.json` | corrected | unaffected |
| 2a | **release process** | **`scripts/release.ps1 -Version 0.1.1-alpha.0` cannot release this version.** `__version__` has been `0.1.1-alpha.0` since phase A (ACCEPTANCE A1 bumped it at the start of the line), and `release.py` `_validate_forward` refuses a target equal to the current version (*"0.1.1-alpha.0 is already the current version. A release must move it forward."* - reproduced with the real `parse_version`; the dry run stops earlier, on the branch check) | **not changed** - the release path is the owner's; options in §24 | unaffected |
| 3 | packaging (low) | installing over `v0.1.0-alpha.2` leaves `libcrypto-3.dll`, `libssl-3.dll`, `libffi-8.dll` (unused by 0.1.1; removed by the uninstaller) - Inno Setup does not delete files a newer package no longer ships | recorded (an `[InstallDelete]` entry would remove them); not changed | unaffected |
| 4 | test / harness | release-validation framework: a relative `--results-dir` gave the app a relative log directory; "a log was written" failed although the log existed | fixed `87d9fa5`, regression tests | unaffected |
| 5 | test / harness | my upgrade check first required a canonical code on every set; migration 13 deliberately keeps a case-collision without one | fixed in `ef8961b` | unaffected |
| 6 | test / harness | SMB tooling: writer host compared with the DNS host name, not the NetBIOS name | fixed before commit, test | unaffected |
| 7 | test / harness (configuration) | installed campaign attempt 1: crash case 13 not exercised (pause deferral expired) | rerun with the kill at 45 % | unaffected |
| 8 | environment / process | my first UI Automation exploration launched the app without a redirected profile and added a scratch project to the operator's *Recent Projects* (and moved `default_projects_root`) | both restored by hand (the previous root inferred from the most recent project, as the application sets it); every later launch redirected | - |
| 9 | environment / process | the canonical gate script needs a branch name; a detached-HEAD worktree made it stop at once | gate worktree given a local branch at the same commit, relaunched | - |
| - | **product** | **none found** | - | **Phase 9 qualification retained**: no correctness-critical production code changed |

No assertion was demoted, no threshold changed, no failure turned into a
warning. F8 (version ordering) is confirmed still open - see §23.

## 19. Final automated validation

```text
targeted:                 tests/unit/test_main_entry.py, test_intake_qualification.py,
                          test_smb_qualification.py (46), test_installed_checks.py,
                          test_release_validation_config.py, test_release_gate.py,
                          test_architecture.py, test_version.py, test_release_automation.py - passed
full pytest:              <<GATE>>
stress:                   <<STRESS>>
ruff:                     <<RUFF>>
fresh-cache mypy:         <<MYPY>>
packaging tests:          Test-PackagedApp 16/16; audit_dependencies 0 unresolved; verify_frozen_imports ok;
                          Test-SelfContained 14/14
release-automation tests: Test-InstallerRoundTrip 15/15; Invoke-ReleaseVerification 14/14;
                          validate_release --build --packaged --installer: 78 passed, 0 failed, 3 skipped;
                          tests/unit/test_release_automation.py in the suite
native crash:             <<NATIVE>>
```

## 20. Candidate artifacts

```text
OMRFlow-0.1.1-alpha.0-Setup-x64.exe  85,862,153 bytes  f69a02e1dd5eb81d0cdc09d3ac3b86b0b873b70e360e8874f0c75362c2724328
OMRFlow.exe (in the bundle)          12,785,528 bytes  ac453449bfc258fa16b9f9b1ced9ee89ec86d0331b0168f119ca618a29c23a78
source commit 73e364b; version 0.1.1-alpha.0; channel Alpha; local, not committed (dist/ is ignored)
```

A published release would carry installers built by the *Release* workflow
from the tag, whose hashes differ.

## 21. Documentation / release notes

Updated: README (phase 10 status and a track-by-track table; packaged /
installer / clean-machine rows), `CHANGELOG.md` (`[Unreleased]`: phase 10
tooling, two documentation fixes, the framework fix - kept under
`[Unreleased]`, no date invented), `CURRENT_STATE.md`, `ROADMAP.md`,
`ACCEPTANCE_CRITERIA.md` §6 / §8, `docs/release/RELEASE_CHECKLIST.md` (the
candidate worked through, item by item), `CLEAN_MACHINE_TEST.md`,
`SMB_QUALIFICATION.md` (new), the wiki (Installation, Scanning, Processing,
Review and Resolution, Attendance, Results and Reports, User Guide, Known
Limitations, Upgrade Compatibility, Development Roadmap). **Release notes
draft:** `docs/release/RELEASE_NOTES_0.1.1-alpha.0.md` - not published; it
states plainly that real scanning-room qualification is not performed, the
quality-decision defaults are unvalidated, SMB is not qualified, a fresh 100k
run is not performed, the installer is unsigned, and that opening a project in
`0.1.1-alpha.0` is one-way.

## 22. Known limitations

- **Real SMB qualification not performed** (required for this Alpha).
- Real scanning-room qualification not performed; quality-decision defaults
  unvalidated.
- Clean-machine test not performed for this build; the installed GUI not
  operated by a person (UI Automation only); icons / SmartScreen / licence /
  Alpha warning pages not looked at.
- Installed-build campaign at small scale (2 sources, 249 files) on one
  machine; source-build supervisor; local folders.
- Power-loss durability untested; process-kill crash safety tested.
- Forward-only upgrade that also changes `project.json`; the database backup
  alone cannot take a project back to `v0.1.0-alpha.2`.
- Three stale DLLs after installing over `v0.1.0-alpha.2`.
- A migration failure on the installed build was not provoked.
- Windows 10 not tested; Windows 11 only.
- Unsigned installer (SmartScreen).
- Project Health after a closed session still warns about history (phase 9
  limitation, unchanged).

## 23. Pre-Beta gates still open

- **A fresh 100,000-sheet qualification on the new architecture** (sessions,
  finite units, continuous processing; 1/25/50/75/99 % kills) - not run.
- **F8, prerelease version ordering** - confirmed open on this branch:
  `numeric_version("0.1.1-beta.1") = (0, 1, 1, 1)` sorts below
  `numeric_version("0.1.1-alpha.3") = (0, 1, 1, 3)`, and stable `0.1.1` gives
  `(0, 1, 1, 0)`, the same as `0.1.1-alpha.0`. Harmless for this Alpha (it
  sorts above `0.1.0-alpha.2`, as the in-place upgrade showed); no Beta may be
  tagged until it is fixed and tested.
- **Real scanning-room qualification** (ACCEPTANCE_CRITERIA §7).

None of these blocks `0.1.1-alpha.0`; the roadmap is unchanged.

## 24. Owner action required

**Not ready.** The exact blockers:

1. **§8 gate 3 - real SMB qualification.** On two Windows machines (or more):
   follow `docs/release/SMB_QUALIFICATION.md` - `smb_qualification prepare`
   with the real UNC paths, copy the packages to the scanner PCs, `run`
   (optionally `--coordinator-exe` the installed build), make the outage when
   told, commit the compact report under
   `docs/release/validation/0.1.1-alpha.0-smb/`, set gate 3 in
   `release-gate.json` to the report's verdict.
2. **§8 gate 9 - the release checklist.** The clean-machine test
   (`.\packaging\sandbox\New-SandboxPayload.ps1 -Launch -Wait` on a machine with
   Windows Sandbox, then `Complete-ManualChecks.ps1` and
   `New-ValidationReport.ps1`) - or, if it genuinely cannot be run, say so in
   the release notes as the checklist allows; the person-only checks (icons,
   SmartScreen wording, a hand-driven Quick Start on the installed build); then
   set gate 9 accordingly.
3. **Decide how `0.1.1-alpha.0` is tagged** (defect 2a): `release.ps1` refuses
   a version equal to `__version__`, which is already `0.1.1-alpha.0`. Either
   (a) teach `scripts/release.py` to accept *target == current* when the tag
   `v<target>` does not exist yet and `_version.py` already says it (skip the
   version edit, keep every other check and gate) - a change to the
   publication path, with tests; or (b) tag by hand on the release commit, the
   way the script would (annotated `v0.1.1-alpha.0`, pushed with the commit),
   after running the gates yourself.
4. Re-run `python -m tools.release_validation.release_gate`; only
   `READY FOR OWNER RELEASE APPROVAL` means the gate is met.

When - and only when - it says so, the release procedure (not executed here):

```powershell
# merge the branch, then on an up-to-date, clean main:
git switch main; git pull --ff-only
git merge --no-ff feat/0.1.1-phase10-release-gate; git push origin main
# CHANGELOG: rename [Unreleased] -> [0.1.1-alpha.0] - <release date>, fold in the
# release-notes draft; commit and push.
# then, with option (a) in place:
.\scripts\release.ps1 -Version 0.1.1-alpha.0 -DryRun     # what it would do
.\scripts\release.ps1 -Version 0.1.1-alpha.0             # gates, annotated tag v0.1.1-alpha.0, push
# or, option (b), after .\pytest-ruff-mypy.ps1 passes on that main:
#   git tag -a v0.1.1-alpha.0 -m "OMRFlow 0.1.1-alpha.0"; git push origin main v0.1.1-alpha.0
# GitHub Actions 'Release' then builds, verifies and publishes the pre-release
# (it refuses a tag that disagrees with _version.py); paste
# docs/release/RELEASE_NOTES_0.1.1-alpha.0.md with the published SHA256SUMS and
# work through RELEASE_CHECKLIST.md sections 6-7.
```

No tag or release has been created.
