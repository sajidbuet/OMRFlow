# OMRFlow <VERSION>

**Release status: <Alpha | Beta | Release Candidate | Stable>**

<!--
Keep the warning below for any prerelease. Delete it only for a stable
release, and only once the stable exit criterion has actually been met.
-->

> ⚠️ OMRFlow is currently undergoing real examination-data qualification.
> **Generated results should be independently verified before operational
> use.** See [Known Limitations](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Known-Limitations.md).

## Highlights

<!-- Three to six bullets. What would make someone want this version? -->

-

## Installation

Download `OMRFlow-<VERSION>-Setup-x64.exe` below and run it.

Windows 10 1809 (build 17763) or newer, 64-bit. No Python required.

<!-- Keep while releases are unsigned. -->
> The installer is **unsigned**, so Windows SmartScreen will warn that the
> publisher is unknown. Click *More info* → *Run anyway*, after verifying the
> checksum below. See
> [Installation](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Installation.md).

New to OMRFlow? Start with the
[Quick Start](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Quick-Start.md).

## Added

-

## Changed

-

## Fixed

-

## Known Limitations

<!--
Real ones only, discovered or still outstanding. Never invent a limitation
to look thorough, and never omit one to look finished. The full list lives in
docs/wiki/Known-Limitations.md; repeat here the ones that most affect
someone deciding whether to use this build.
-->

-

## Compatibility

| | |
|---|---|
| Project format version | |
| Database schema version | |
| Supported Windows versions | |
| Upgrades from | |

## Upgrade Notes

<!--
What happens to an existing project. Keep the backup warning for any
prerelease - a migration is applied in place and cannot be reversed.
-->

> **Back up your projects before upgrading.** Prerelease versions make no
> compatibility promises to each other, and a database migration is applied
> in place and cannot be undone. See
> [Upgrading OMRFlow](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Upgrading-OMRFlow.md).

## Validation Status

<!--
State exactly what was run for THIS build. Do not claim a test that was not
performed. "Not performed" is a valid and useful entry.

The two substitute rows exist so that a build where the clean-machine test
could not be run still shows what evidence there is. They do not upgrade
"Clean-machine installation" to a pass - that row stays "Not performed".
-->

| Check | Status |
|---|---|
| Unit and integration tests | |
| Full automated suite | |
| Synthetic end-to-end walkthrough | |
| Packaged application smoke test | |
| Installer install / launch / uninstall | |
| Clean-machine installation | |
| — substitute: bundle import audit | |
| — substitute: sanitised-environment launch | |
| Upgrade from the previous release | |
| Real attendance workbook | |
| Real scanned cohort | |
| Golden-result verification | |
| 100,000-sheet qualification | |
| Accessibility review | |
| Code signing | |

## Downloads

| File | Description |
|---|---|
| `OMRFlow-<VERSION>-Setup-x64.exe` | Windows installer, 64-bit |
| `SHA256SUMS.txt` | SHA-256 checksums |

## SHA-256 Verification

The installer is unsigned, so this checksum is how you confirm you have the
file that was built here.

```powershell
certutil -hashfile OMRFlow-<VERSION>-Setup-x64.exe SHA256
```

Compare the result with the matching line in `SHA256SUMS.txt`. If they
differ, **do not install it** — download it again.

On a system with `sha256sum`:

```bash
sha256sum --check SHA256SUMS.txt
```

## Reporting problems

[Open an issue](https://github.com/sajidbuet/OMRFlow/issues/new/choose).
Please read
[SUPPORT.md](https://github.com/sajidbuet/OMRFlow/blob/main/SUPPORT.md)
first — in particular, **do not attach real candidate data, rosters, answer
keys or scans** to a public issue.

Security or privacy problems go through
[SECURITY.md](https://github.com/sajidbuet/OMRFlow/blob/main/SECURITY.md),
not the issue tracker.

---

**Full changelog:**
[CHANGELOG.md](https://github.com/sajidbuet/OMRFlow/blob/main/CHANGELOG.md)
