# Clean-Machine Installation Test

> **Status for `0.1.0-alpha.1`: NOT PERFORMED.**
>
> The procedure below has **not** been executed. No clean machine was
> available: Windows Sandbox is not installed on the build machine, and
> enabling it needs administrator rights and a reboot.
>
> A **substitute** was run instead, and passed — see
> [What was run instead](#what-was-run-instead) below. It is a substitute,
> not this test: it proves the build does not depend on Python, on the
> source tree or on Qt environment variables, and it does not prove the
> bundle is self-contained.
>
> This remains outstanding in
> [Known Limitations](../wiki/Known-Limitations.md) and is the reason Phase
> 11A is *Implemented — clean-machine validation pending* rather than
> complete.
>
> **To run the real test:** enable Windows Sandbox, then
> `.\packaging\sandbox\New-SandboxPayload.ps1 -Launch`.

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

## What was run instead

On **2026-09-21**, for `0.1.0-alpha.1`, a clean machine was unavailable, so
the strongest substitute that a development machine can offer was run and
**passed**. Recorded here so the release's evidence is exactly what was
executed, no more.

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

### What it does not prove — why the real test is still required

- ❌ **A DLL sitting in `System32` because a developer tool put it there** is
  indistinguishable, on this machine, from one Windows ships. This is the
  single biggest gap, and only a genuinely fresh Windows install closes it.
- ❌ **Dependencies loaded at run time** — `ctypes`, `LoadLibrary`, a Qt
  plugin discovered by path — appear in no import table and so in no audit.
- ❌ **Code paths the launch did not reach.** Starting the window exercises
  Qt, the imaging stack and the icon resources; it does not exercise
  recognition, Excel generation or PDF rendering, any of which could need
  something the bundle lacks.
- ❌ **Everything a person has to look at**: SmartScreen's wording, the
  licence page, the Alpha warning during installation, all nine stage icons
  rendering, the About dialog, and the full end-to-end run through
  recognition to a generated report.
- ❌ **Data preservation across uninstall and reinstall** on a machine that
  never had OMRFlow before.

Accordingly the sign-off table below records **NOT PERFORMED**, and the
release notes must say so rather than leaving it ambiguous.

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
| Date | 2026-09-21 |
| Windows version | — (no clean machine available) |
| Machine type | **none** — Windows Sandbox not installed; enabling it needs administrator rights and a reboot |
| Administrator rights available | no |
| Installer version tested | `OMRFlow-0.1.0-alpha.1-Setup-x64.exe` (on the build machine only) |
| Checksum verified | yes, on the build machine |
| Steps passed | **0 of 36** — this procedure was not executed |
| Failures | none observed, because nothing was run |
| Result | **NOT PERFORMED** |
| Substitute run | `audit_dependencies.py` (0 unresolved imports) and `Test-SelfContained.ps1` (13/13) — both passed; see [What was run instead](#what-was-run-instead) |

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
