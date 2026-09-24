# OMRFlow 0.1.0-alpha.1

**Release status: Alpha**

> ⚠️ OMRFlow is currently undergoing real examination-data qualification.
> **Generated results should be independently verified before operational
> use.** See [Known Limitations](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Known-Limitations.md).

The first installable release. OMRFlow's workflow is implemented end to end —
design a template, process scanned sheets, resolve what recognition could not
decide, reconcile attendance, score against an answer key and generate result
workbooks — and is covered by 4,162 automated tests against synthetic data.

**No real examination data has been processed end to end.** That is Phase 11B,
and it is not done. This build is for evaluation.

## Highlights

- **A Windows installer**, so OMRFlow can be evaluated without a Python
  development environment.
- **Template designer** for `.omrt` documents: registration markers, bubble
  grids, per-bubble adjustment, validation.
- **Geometric normalisation** of a scan to the template's canonical page —
  rotation, translation, scale, skew and perspective.
- **Multicore batch recognition**, with identical results and output ordering
  on any number of cores, and batches that survive interruption and resume.
- **Conflict review**, with every manual correction recorded append-only
  beside the machine's original reading.
- **Attendance reconciliation**, including absent-with-script and
  present-without-script, and scoring with optional negative marking and
  standard competition ranking.
- **Result workbooks built from the examination office's own attendance
  workbook**, preserving its formatting and logo.

## Installation

Download `OMRFlow-0.1.0-alpha.1-Setup-x64.exe` below and run it.

Windows 10 1809 (build 17763) or newer, 64-bit. No Python required.

> The installer is **unsigned**, so Windows SmartScreen will warn that the
> publisher is unknown. Click *More info* → *Run anyway*, after verifying the
> checksum below. Code signing is a Phase 11C item. See
> [Installation](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Installation.md).

Verify your download before installing:

```powershell
certutil -hashfile OMRFlow-0.1.0-alpha.1-Setup-x64.exe SHA256
```

```
54efb360603d28934e2880d9e408c153237199ddef08f5a8e8d800a32bb8b502  OMRFlow-0.1.0-alpha.1-Setup-x64.exe
```

New to OMRFlow? Start with the
[Quick Start](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Quick-Start.md).

Installs per-user to `%LOCALAPPDATA%\Programs\OMRFlow` and needs no
administrator rights. Your projects, settings and logs live in your user
profile and are **not** removed by uninstalling.

## Validation Status

| Check | Status |
|---|---|
| Unit and integration tests | ✅ 4,162 passed, 2 skipped; `ruff` and `mypy` clean |
| Full automated suite | ✅ Passed |
| Synthetic end-to-end walkthrough | ✅ Passed, **from source** |
| Packaged application smoke test | ✅ 16/16 |
| Installer install / launch / uninstall | ✅ 15/15, user data preserved |
| Clean-machine installation | ✅ **56/56** on a pristine Windows 11 image (no Python, Qt or build tools): checksum verified there, per-user install without elevation, first launch, uninstall with data intact, reinstall. [Record](https://github.com/sajidbuet/OMRFlow/blob/main/docs/release/validation/0.1.0-alpha.1-clean-machine.md) |
| — substitute: bundle import audit | ✅ 0 unresolved imports; the C/C++ runtime is bundled, not borrowed |
| — substitute: sanitised-environment launch | ✅ 14/14, including installation under a path with spaces and non-ASCII characters |
| **End-to-end workflow on the *installed* build** | ⚠️ **Not performed.** Recognition, Excel reporting and the parallel worker path are covered by the automated suite, but every one of those runs from source, not from the installer |
| Upgrade from the previous release | ➖ Not applicable — first release |
| Real attendance workbook | ❌ Not performed |
| Real scanned cohort | ❌ Not performed |
| Golden-result verification | ❌ Not performed |
| 100,000-sheet qualification | ➖ Harness ready, not run |
| Accessibility review | 🟠 Initial only — keyboard, focus, contrast and high-DPI verified; no screen reader used |

## Known Limitations

The full list is in
[Known Limitations](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Known-Limitations.md).
The ones most likely to affect a decision to use this build:

- **Real examination-data qualification is incomplete.** No real attendance
  workbook and no real scanned cohort has been processed end to end. Verify
  every generated result independently.
- **The installed build has never been driven through a recognition run.**
  Nothing suggests it behaves differently once frozen; nobody has checked.
- **The installer is unsigned**, so SmartScreen warns.
- **Windows 10 is untested.** Built and validated on Windows 11; Windows 10
  1809 is the floor the bundled runtime supports.
- **Results, Resolve and Attendance tables are not lazily loaded**, and may
  be slow with a very large cohort. The Scan table is lazy.
- No dark theme; English only; Windows x64 only.

## Compatibility

| | |
|---|---|
| Project format version | 2 |
| Database schema version | 9 |
| Supported Windows versions | Windows 10 1809 (build 17763) or newer, 64-bit |
| Upgrades from | None — this is the first release |

## Upgrade Notes

> **Back up your projects before upgrading.** Prerelease versions make no
> compatibility promises to each other, and a database migration is applied
> in place and cannot be undone. See
> [Upgrading OMRFlow](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Upgrading-OMRFlow.md).

## Reporting problems

Please include the **build identifier** from **Help → About OMRFlow** (hover
the version line): it reads `0.1.0-alpha.1+<commit>` and identifies the exact
revision this installer was built from. A diagnostic bundle from
**Tools → Create Diagnostic Bundle…** is the most useful thing to attach.

[Issues](https://github.com/sajidbuet/OMRFlow/issues) ·
[Support](https://github.com/sajidbuet/OMRFlow/blob/main/SUPPORT.md) ·
[Security policy](https://github.com/sajidbuet/OMRFlow/blob/main/SECURITY.md)
