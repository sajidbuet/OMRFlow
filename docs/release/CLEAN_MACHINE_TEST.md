# Clean-Machine Installation Test

> **Status for `0.1.0-alpha.1`: NOT PERFORMED.**
>
> Every installer test so far has run on the machine that built OMRFlow,
> which has Python, PySide6, OpenCV and the build tooling installed. That
> machine cannot prove the bundle is self-contained, because anything the
> bundle forgot may still be found on the system.
>
> This procedure is written and has not been executed. It is recorded as
> outstanding in [Known Limitations](../wiki/Known-Limitations.md) and is the
> reason Phase 11A is *Implemented — clean-machine validation pending*
> rather than complete.

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
