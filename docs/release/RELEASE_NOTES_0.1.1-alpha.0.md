<!--
DRAFT, prepared by revised phase 10 (2026-10-07). NOT RELEASED, NOT TAGGED.
Publishing is the owner's decision. Before publishing: every "NOT PERFORMED"
gate that ACCEPTANCE_CRITERIA.md section 8 requires must have passed - at the
time of writing the real SMB qualification has not - and the Validation
Status table must be re-checked against what was actually run for the build
being published. Delete this comment when publishing.
-->

# OMRFlow 0.1.1-alpha.0

**Release status: Alpha**

> ⚠️ OMRFlow is currently undergoing real examination-data qualification.
> **Generated results should be independently verified before operational
> use.** See [Known Limitations](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Known-Limitations.md).

`0.1.1-alpha.0` turns one examination into a **scan session** made of finite
batches - one scanner or several, a finite folder or folders watched while
scanning continues, later rescans - with session-level attendance, results and
reports, crash-safe scanning and review, and case-insensitive set identity.

**Not yet qualified, and said plainly:**

- **Real scanning-room qualification has not been performed** - no real
  scanner, operator or paper has been through this build.
- **The scan-quality decision defaults are unvalidated** - when OMRFlow
  *suggests* a rescan it uses an uncalibrated default policy; nothing is ever
  rejected without a named operator's confirmation.
- **Genuine network-share (SMB) intake has not been qualified.** <!-- If the
  SMB qualification has passed, replace this bullet with its result and link. -->
- **A fresh 100,000-sheet qualification on the new session architecture has
  not been run.** It is required before the first Beta, not for this Alpha;
  the 10,189-file intake campaign below is not a substitute for it.
- Power-loss durability is untested (process termination is).

## Highlights

- **Scan sessions and finite batches.** Reopening a project and scanning more
  adds a batch to the same session; nothing scored changes behind your back.
- **Several scanners at once** (*Session → Add Scanner Source…*): watched
  folders, stable-file detection (a file being written is never read),
  duplicate images recognised, three progress lines, *Finish Scan Session*
  with every blocker listed.
- **Crash-safe Scan and Resolve.** A sheet shown as read is already saved with
  its review state; after a crash the project reopens on the same session with
  the saved work shown before you press anything; Resolve opens directly.
- **Session-level results.** Attendance, Results and Reports read one
  effective sheet set per session; Final Export needs a closed session.
- **Set identity.** Set codes are case-insensitive (`a` is `A`); a printed
  mark can map to a logical set (`A` on the sheet → *Set 10*).

## Installation

Download `OMRFlow-0.1.1-alpha.0-Setup-x64.exe` below and run it.

Windows 10 1809 (build 17763) or newer, 64-bit. No Python required. Built and
tested on Windows 11 only.

> The installer is **unsigned**, so Windows SmartScreen will warn that the
> publisher is unknown. Click *More info* → *Run anyway*, after verifying the
> checksum below. See
> [Installation](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Installation.md).

New to OMRFlow? Start with the
[Quick Start](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Quick-Start.md).

## Added

- Scan sessions (schema 14), the session-level effective sheet set (15),
  intake sources and the intake ledger (16), quality decisions and persisted
  operator controls (17); set identity (13).
- The continuous-processing engine and the operational GUI (session mode on
  the Scan stage; live Resolve with *Suggested rescans* and *Files awaiting
  decision*).
- The intake qualification harness (`python -m omr_scanner.tools.intake_qualification`)
  and the SMB qualification tooling (`python -m omr_scanner.tools.smb_qualification`,
  with a PowerShell scanner-PC writer).

## Changed

- Attendance, Results and Reports read the active scan session, never "the
  most recently updated batch".
- Results of an open session are labelled provisional; Final Export requires a
  closed session and becomes stale when the session is reopened.

## Fixed

- The six latent `0.1.0-alpha.2` defects in the `0.1.1` plan (a late scan
  splitting an examination; downstream reading a retried old batch;
  duplicate-ID detection across batches; byte-identical images read twice; the
  `10 → A` set mapping; the processing manifest) and the crash-safety defects
  S1, S2, S3 and R1. See [CHANGELOG.md](https://github.com/sajidbuet/OMRFlow/blob/main/CHANGELOG.md).

## Known Limitations

- The bullets at the top of these notes.
- **Opening a project in `0.1.1-alpha.0` is one-way.** The database is
  migrated (a backup of it is taken first) *and* `project.json` changes;
  `v0.1.0-alpha.2` then refuses the project as *"not valid"*, even after the
  database backup is restored. **Copy the whole project folder first** if you
  may need `v0.1.0-alpha.2` again.
- Network-share (UNC) sources: the stability defaults are unmeasured on a real
  share; keep the project itself on a local disk.
- Nobody has operated the session GUI in a real scanning room; the installed
  GUI was walked by UI Automation, not by a person.
- Version ordering for a future Beta (finding F8) is wrong in
  `numeric_version`; harmless for this Alpha; fixed before any Beta.
- Full list: [Known Limitations](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Known-Limitations.md).

## Compatibility

| | |
|---|---|
| Project format version | 3 |
| Database schema version | 17 |
| Supported Windows versions | Windows 10 1809+ 64-bit (supported); tested on Windows 11 only |
| Upgrades from | `v0.1.0-alpha.2` (schema 9), verified on the installed candidate; schema-12 projects; anything older through the same forward-only migrations (not separately tested) |

## Upgrade Notes

> **Back up your projects before upgrading - copy the whole project folder.**
> Prerelease versions make no compatibility promises to each other, and a
> database migration is applied in place and cannot be undone. Opening a
> project in this version also changes `project.json`, so the automatic
> database backup alone does not take it back to `v0.1.0-alpha.2`. See
> [Upgrade Compatibility](https://github.com/sajidbuet/OMRFlow/blob/main/docs/wiki/Upgrade-Compatibility.md).

Install over `v0.1.0-alpha.2` without uninstalling it: settings, logs and
projects are kept; existing batches each become a one-batch scan session.

## Validation Status

For the candidate built from commit `73e364b` (installer SHA-256 below).
Details: `development/releases/0.1.1-alpha.0/PHASE_J_HANDOFF.md` and
`docs/release/validation/`.

| Check | Status |
|---|---|
| Full automated suite | <!-- fill from the final canonical gate --> |
| Synthetic intake qualification (10,189 files, 3 simulated scanners, real kills; source build) | **QUALIFIED** (revised phase 9, `542b2af`) |
| Crash safety, process termination (cases 1–15, 1/25/50/75/99 %) | Passed (source build, phase 9); S1/S2/S3/R1 also on the **installed** build (phase 10) |
| Installed-build continuous intake (2 local sources, ~250 files, real kills) | <!-- fill --> |
| Packaged application smoke test | Passed (16/16) |
| Installer install / launch / uninstall / reinstall | Passed (round trip 15/15; release-validation stages 78 checks, 0 failed) |
| Clean-machine installation | **Not performed** for this build (Windows Sandbox unavailable on the build machine) |
| — substitute: bundle import audit | Passed (0 unresolved imports; every imported dependency frozen) |
| — substitute: sanitised-environment launch | Passed (14/14) |
| Upgrade from the previous release | Passed on the installed candidate: in place over installed `v0.1.0-alpha.2`; a `v0.1.0-alpha.2` project migrated 9 → 17 with every value kept |
| Genuine SMB network-share qualification | **Not performed** <!-- required for this Alpha --> |
| Real attendance workbook | Not performed |
| Real scanned cohort / scanning room | **Not performed** |
| Quality-decision defaults | **Unvalidated** |
| Golden-result verification | Synthetic golden one-batch regression only |
| 100,000-sheet qualification (new architecture) | **Not performed** (required before Beta) |
| Power loss | Not performed |
| Accessibility review | Initial (automated checks; non-blocking at Alpha) |
| Code signing | Not signed |

## Downloads

| File | Description |
|---|---|
| `OMRFlow-0.1.1-alpha.0-Setup-x64.exe` | Windows installer, 64-bit |
| `SHA256SUMS.txt` | SHA-256 checksums |

## SHA-256 Verification

The installer is unsigned, so this checksum is how you confirm you have the
file that was built here.

```powershell
certutil -hashfile OMRFlow-0.1.1-alpha.0-Setup-x64.exe SHA256
```

Compare the result with the matching line in `SHA256SUMS.txt`. If they
differ, **do not install it** — download it again.

On a system with `sha256sum`:

```bash
sha256sum --check SHA256SUMS.txt
```

<!-- The published installer is built by the Release workflow from the tag,
so its hash will differ from the local candidate's
(f69a02e1dd5eb81d0cdc09d3ac3b86b0b873b70e360e8874f0c75362c2724328). Quote the
published SHA256SUMS.txt. -->

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
