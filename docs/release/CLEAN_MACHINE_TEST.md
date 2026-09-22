# Clean-Machine Installation Test

> **Status for `0.1.0-alpha.1`: PERFORMED — automated portion passed; the
> steps needing a person are outstanding.**
>
> On **2026-09-22** this procedure was executed for the first time, in
> Windows Sandbox, against
> `OMRFlow-0.1.0-alpha.1-Setup-x64.exe`
> (`3f2cfc62…`, commit `92fe4b2`). **56 checks passed and none failed** —
> including the two failures this test exists to catch, and the ones the
> substitutes could never reach: installation as an unprivileged user,
> first launch, uninstall with user data preserved, and reinstall.
>
> The full result is recorded in
> [`validation/0.1.0-alpha.1-clean-machine.md`](validation/0.1.0-alpha.1-clean-machine.md)
> and in the JSON beside it.
>
> **What is still outstanding** is everything that needs a person to look at
> the screen: steps 5–7, 13–24, 27, 32 and 35 — the Alpha warning during
> installation, the About dialog, the nine stage icons, and the end-to-end
> run from *Create Project* through recognition to a generated result. Those
> are **not performed**, and Phase 11A is *Implemented — validation pending*
> until they are.
>
> **To finish it:**
>
> ```powershell
> .\packaging\sandbox\New-SandboxPayload.ps1 -Launch -Wait
> # then, in the sandbox window, after working through the interface:
> C:\Users\WDAGUtilityAccount\Desktop\OMRFlow\Complete-ManualChecks.ps1
> # back on the host:
> .\scripts\release\New-ValidationReport.ps1
> ```
>
> The first defect this test found was real: a packaged build did not record
> its own source commit, so the build identifier documented in
> [Installation](../wiki/Installation.md) did not exist in any installed
> build. Fixed in `92fe4b2` and confirmed here — the first-launch log now
> reads `0.1.0-alpha.1+92fe4b2`.

## Why it cannot be skipped

Two classes of defect are invisible on a development machine and obvious
here:

1. **A dependency that was not bundled** but is present on the build
   machine — a Qt plugin, a Visual C++ runtime, a codec.
2. **An import that only worked because the source tree was importable.**

Both produce an application that launches perfectly for the developer and
fails immediately for everyone else.

## What "clean" means

A Windows machine — physical, virtual machine, or a fresh Windows Sandbox —
with:

- a supported Windows version (10 1809 / build 17763 or newer, 64-bit);
- **no Python installed**;
- **no Visual Studio, no build tools, no Qt**;
- **no OMRFlow source checkout**;
- a normal user account **without administrator rights**, to confirm the
  per-user install path works.

Windows Sandbox is the cheapest option if the host has it: it is clean every
time it starts, which removes the "was it really clean?" doubt.

### Running it in Windows Sandbox

The scaffolding is in the repository, so the test is two commands once
Sandbox is enabled:

```powershell
.\scripts\release\Build-Installer.ps1          # if not already built
.\scripts\release\New-Checksums.ps1
.\packaging\sandbox\New-SandboxPayload.ps1 -Launch
```

`New-SandboxPayload.ps1` stages **only** the installer, `SHA256SUMS.txt` and
the in-sandbox script into `packaging/sandbox/payload/` — deliberately no
source tree, because an importable source tree is one of the two defects
this test exists to catch. `OMRFlow-CleanMachine.wsb` maps that folder
read-only, disables networking so a missing runtime cannot be quietly
downloaded, and runs `Start-CleanMachineTest.ps1` on logon.

That script performs the mechanical steps — confirming the sandbox really is
clean, verifying the checksum, installing unprivileged, launching — and then
prints the steps that need a person to look at the screen. It does not
replace this procedure; the steps involving judgement stay manual.

If Sandbox is unavailable, copy `packaging/sandbox/payload/` to a separate
clean machine or fresh VM and run `Start-CleanMachineTest.ps1` there.

## Procedure

Record the result of every step. A step that was not run is *not performed*,
not a pass.

### Preparation

1. [ ] Note the Windows version (`winver`) and the machine type.
2. [ ] Copy **only** the installer and `SHA256SUMS.txt` onto the machine.
       Nothing else — no source, no Python.
3. [ ] Verify the checksum:
       `certutil -hashfile OMRFlow-<version>-Setup-x64.exe SHA256`
       and compare it with `SHA256SUMS.txt`.

### Installation

4. [ ] Run the installer as a **non-administrator** user.
5. [ ] Note whether SmartScreen warned, and the exact wording. It is expected
       to, for an unsigned build; confirm the documentation matches what
       actually appeared.
6. [ ] The licence page shows the MIT licence.
7. [ ] The Alpha warning is shown during installation.
8. [ ] Accept the default (per-user) location. Installation completes without
       requesting elevation.
9. [ ] Start menu entry created.

### First launch

10. [ ] OMRFlow launches from the Start menu.
11. [ ] **No error about a missing DLL, Python, Qt or a Visual C++ runtime.**
        This is the single most important observation in this document.
12. [ ] The window title reads `OMRFlow <version>`.
13. [ ] The header shows the logo and the workflow navigator; **all nine
        stage icons render** — a missing icon means the bundled assets did
        not come along.
14. [ ] **Help → About OMRFlow** shows the version, `Alpha`, the MIT licence,
        and the Alpha warning. Hovering the version shows the build
        identifier with its commit.
15. [ ] Move between all nine stages. Each renders without error.
16. [ ] The footer shows the version, the licence and `Ready`.

### A real piece of work

17. [ ] **Create Project** — a project is created and opens.
18. [ ] **Project Configuration** opens automatically; set an examination
        name and one set, and save.
19. [ ] Open the bundled example template on the **Template** stage, and
        **Validate** it.
20. [ ] **Tools → Developer / Testing → Generate Synthetic Test Dataset…** —
        generate ~10 sheets. *(This exercises the imaging and rendering
        stack, which is where a missing native dependency would surface.)*
21. [ ] On the **Scan** stage, load the template, add the folder, and
        **Process All**. Recognition completes.
22. [ ] Recognised values appear in the table and a preview renders.
23. [ ] **Tools → Create Diagnostic Bundle…** produces a zip file.
24. [ ] **Tools → Project Health / Recovery…** opens and reports.

### Shutdown and restart

25. [ ] Close OMRFlow. It exits cleanly, with no error and no lingering
        process in Task Manager.
26. [ ] Relaunch it. The project appears under **Recent Projects**.
27. [ ] Reopen it. The previously recognised results are still there.
28. [ ] Close it again.

### Uninstall

29. [ ] Uninstall through **Settings → Apps → Installed apps**.
30. [ ] The application directory is gone.
31. [ ] The Start menu entry is gone.
32. [ ] **The project folder still exists, with its database intact.**
33. [ ] **`%LOCALAPPDATA%\OMRFlow\` still exists**, with settings and logs.

### Reinstall

34. [ ] Install again. It succeeds.
35. [ ] The previously created project opens, with its results.
36. [ ] Uninstall, and remove the leftover data by hand if the machine is
        being kept.

## The substitutes, and what they add

These run on the build machine and were **also** executed for
`0.1.0-alpha.1`. Before the clean-machine test could be run they stood in
for it, which is why they are described below in those terms; now that it has
been run, they are a faster pre-flight rather than a stand-in. Both passed
again against the rebuilt candidate, and `Test-SelfContained.ps1` additionally
passed with the application installed under
`…\Parikşa Dosyası ২০২৬\OMRFlow Alpha Test` — a path with spaces and
characters outside ASCII.

### `packaging/audit_dependencies.py` — static import audit

Parses the PE import and delay-import tables of every binary in the bundle
and classifies each imported DLL as bundled, satisfied by Windows, or
unresolved.

| | |
|---|---|
| Binaries parsed | 162 |
| Distinct imported DLLs | 127 |
| Satisfied from the bundle | 76 |
| Satisfied by Windows | 51 (46 of them non-API-set, all standard OS DLLs) |
| **Unresolved** | **0** |
| VC++ runtime | `msvcp140*`, `vcruntime140*` — **bundled**, not taken from `System32` |

That last row is the classic clean-machine failure, and it is covered: the
C/C++ runtime ships inside the bundle.

### `scripts/release/Test-SelfContained.ps1` — sanitised-environment launch

Installs the release installer into a scratch directory and launches what it
installed with the environment a machine without a development toolchain
would present: `PATH` reduced to the three Windows directories, and
`PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, `CONDA_PREFIX`, `QT_PLUGIN_PATH`,
`QT_QPA_PLATFORM_PLUGIN_PATH`, `QML*_IMPORT_PATH` and the rest cleared, from
a working directory nowhere near the source tree.

**All 13 checks passed:** the installer succeeded unprivileged; no Python
interpreter is inside the installation; the application started with no
Python on the `PATH`; the window appeared with the title `OMRFlow
0.1.0-alpha.1`; it was responsive; it loaded its Qt platform plugin from the
bundle; it closed with exit code 0; it wrote a log under the user profile
during that run and wrote nothing into the installation directory; and it
uninstalled.

### What the substitute proves

- ✅ No DLL named in any import table is missing.
- ✅ The Visual C++ runtime is bundled rather than borrowed from the system.
- ✅ The application does not need Python on the `PATH`, or a Python
  installation at all.
- ✅ It does not rely on `PYTHONPATH`/`PYTHONHOME`, or on the source tree
  being importable.
- ✅ It does not rely on Qt environment variables to find its plugins.
- ✅ It does not rely on its working directory.

### What it does not prove — and which of those the real test closed

| | Closed by the 2026-09-22 clean-machine run? |
|---|---|
| **A DLL sitting in `System32` because a developer tool put it there** is indistinguishable, on the build machine, from one Windows ships | ✅ **Closed.** A pristine Sandbox image has no developer tooling, and the application launched there |
| **Data preservation across uninstall and reinstall** on a machine that never had OMRFlow before | ✅ **Closed.** Uninstalled, user data intact, reinstalled, data still reachable |
| **Dependencies loaded at run time** — `ctypes`, `LoadLibrary`, a Qt plugin found by path — appear in no import table | 🟠 **Partly.** Starting the window on a clean machine proves the Qt platform plugin loads; anything reached only by recognition or reporting is still untested |
| **Code paths the launch did not reach** — recognition, Excel generation, PDF rendering | ❌ **Open.** Steps 17–24 need a person |
| **Everything a person has to look at** — SmartScreen's wording, the licence page, the Alpha warning, the nine stage icons, the About dialog, the end-to-end run | ❌ **Open.** Steps 5–7, 13–24 |

A separate gap the substitutes never claimed to cover, now also closed:
`Test-SelfContained.ps1` was run with the application installed under a path
containing spaces and non-ASCII characters, and passed all 14 checks.

Accordingly the sign-off table below records the automated portion as a pass
and the remaining steps as **not performed**. The release notes must say so
rather than leaving it ambiguous — an installed build that has never been
driven through a recognition run has not been shown to be usable for the
evaluation the Alpha exists to invite.

## Recording the result

Enter this in the release's sign-off table, and in the release notes'
*Validation Status*:

| | |
|---|---|
| Date | |
| Windows version | |
| Machine type | physical / VM / Windows Sandbox |
| Administrator rights available | yes / no |
| Installer version tested | |
| Checksum verified | yes / no |
| Steps passed | of 36 |
| Failures | |
| Result | **PASS / FAIL / NOT PERFORMED** |

### Recorded result for `0.1.0-alpha.1`

| | |
|---|---|
| Date | 2026-09-22 |
| Windows version | Microsoft Windows 11 Enterprise, build 26100 |
| Machine type | **Windows Sandbox** — a pristine image, discarded afterwards |
| Administrator rights available | the sandbox account is an administrator; the install was nevertheless per-user and requested no elevation, and the installer's manifest was read back as `asInvoker` |
| Installer version tested | `OMRFlow-0.1.0-alpha.1-Setup-x64.exe`, commit `92fe4b2` |
| Checksum verified | yes, **on the machine under test**, against `SHA256SUMS.txt`: `3f2cfc62fef612549c2dccd855ad309a11e4bba0879538cdc41b835565e3ac20` |
| Automated checks | **56 passed, 0 failed, 1 recorded-not-judged** |
| Steps passed | preparation and installation (1–4, 8–9), first launch (10–12), shutdown and restart (25–26, 28), uninstall (29–31, 33) and reinstall (34) |
| Steps not performed | **5–7, 13–24, 27, 32, 35** — every step that needs a person to look at the screen |
| Failures | none |
| Result | **PASS (automated portion). The procedure as a whole is incomplete** until the steps above are performed |
| Defect found and fixed | a packaged build did not record its source commit; fixed in `92fe4b2`, re-validated here |
| Full record | [`validation/0.1.0-alpha.1-clean-machine.md`](validation/0.1.0-alpha.1-clean-machine.md) |

The one check recorded rather than judged is the Visual C++ runtime in
`System32`: current Windows images ship it themselves, so its presence no
longer means a developer tool put it there. What matters — that OMRFlow
carries its own copy rather than borrowing that one — is checked separately
and passed.

## If it fails

A failure here is release-blocking for any channel — it means the artifact
does not work for the people it is for. Record the exact error, capture
`%LOCALAPPDATA%\OMRFlow\logs\`, and treat it as a packaging defect:
the fix belongs in `packaging/omrflow.spec` or `packaging/omrflow.iss`, not
in a documentation note telling users to install something extra.

## Related

- [Release Checklist](RELEASE_CHECKLIST.md)
- [Release Process](../wiki/Release-Process.md)
- [Installation](../wiki/Installation.md)
- [Known Limitations](../wiki/Known-Limitations.md)
