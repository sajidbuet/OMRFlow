# Development Roadmap

**This page is the canonical roadmap.** `development/ROADMAP.md` in the
repository holds the full per-phase detail — deliverables, tests and exit
criteria as each phase was written — and remains the working document for
Phases 0–10. This page is the current status, and the definitive statement of
Phase 11.

Development proceeds in numbered phases. A phase ends only when its exit
criteria are met, `pytest`, `ruff` and `mypy` all pass,
`development/CURRENT_STATE.md` is updated and a handoff document is written.

## Status legend

| Status | Meaning |
|---|---|
| **Complete** | Implemented, tested, exit criteria met |
| **Implemented; testing in progress** | Built and covered by automated tests; validation continuing |
| **Implemented; synthetic testing only** | Tested, but only against data OMRFlow generated itself |
| **Implemented — validation pending** | Built; a required validation has not been performed |
| **Pending** | Not started |

## Phases

| Phase | Title | Status |
|---|---|---|
| 0 | Architecture & Repository Foundation | **Complete** |
| 1 | OMR Geometry & Alignment Engine | **Complete** |
| 2 | Template Data Model & Template Designer Core | **Complete** |
| 3 | Bubble Mapping & Recognition Engine | Implemented; testing in progress |
| 4 | Template Calibration & Validation | Implemented; testing in progress |
| 5 | Batch Scan Processing Pipeline | Implemented; testing in progress |
| 6 | Conflict Detection & Human Resolution | Implemented; testing in progress |
| 7 | Candidate & Attendance Reconciliation | Implemented; testing in progress |
| 8 | Answer-Key & Scoring Engine | Implemented; testing in progress |
| 9 | Result Management & Reporting | Implemented; testing in progress |
| 10 | Integration, Recovery & Production Hardening | Implemented; 100,000-sheet acceptance run pending |
| **11A** | **Alpha Release Infrastructure** | **Implemented — validation pending** (clean-machine test run; its manual steps outstanding) |
| 11B | Real-Data Qualification & Beta Release | **Pending** |
| 11C | Release Candidate & Stable Release | **Pending** |

Per-phase detail for 0–10: `development/ROADMAP.md` and the
`development/PHASE_NN_HANDOFF.md` documents.

---

## Next development target — v0.1.1-alpha.0

**Status: in development (not released).** Source builds report
`0.1.1-alpha.0` since 2026-10-01. Baseline: `v0.1.0-alpha.2` plus the
unreleased work on `main` (Reject & Rescan, the Attendance workstation, the
Answer Key rework; schema 12). `0.1.0-alpha.2` remains the released
finite-batch Alpha.

Implemented in the revised ten-phase sequence (release `ROADMAP.md` §5.1):

| Revised phase | Implemented | Automated tests | Synthetic campaign | Network share | Real scanners | Production |
|---|---|---|---|---|---|---|
| 1 — Set identity (migration 13) | ✅ merged into `main` | ✅ passing | n/a | n/a | ❌ not performed | ❌ not performed |
| 2 — ScanSession + finite ScanBatch lifecycle (migration 14) | ✅ merged into `main` | ✅ passing | n/a | n/a | ❌ not performed | ❌ not performed |
| 3 — Crash-safe Scan/Resolve persistence (no migration) | ✅ merged | ✅ passing, incl. a real-process kill matrix (40 sheets; 1,000-sheet percentage series) | n/a | ❌ not performed | ❌ not performed | ❌ not performed |
| 4 — Session-level effective results (migration 15) | ✅ merged into `main` (`fd063f8`) | ✅ passing, incl. a 3-batch / 100+-script acceptance scenario, generated-cohort ground truth and a golden one-batch regression | ✅ synthetic only | ❌ not performed | ❌ not performed | ❌ not performed |
| 5 — Intake sources + ledger (migration 16; headless) | ✅ merged into `main` (`59ba8df`) | ✅ passing, incl. a three-source acceptance scenario, real temporary directories, real writer / engine process kills and a 2,400-arrival soak | 🟠 local temp-directory soak only, not the campaign | ❌ not performed | ❌ not performed | ❌ not performed |
| 6 — Continuous-processing engine (no migration; headless) | ✅ merged into `main` (`141d703`) | ✅ passing, incl. a kill-boundary matrix, real process kills, 1/25/50/75/99 % restart series, finite-path equivalence and a 3,000-file endurance run | 🟠 local and fake-filesystem scenarios only, not the campaign | ❌ not performed | ❌ not performed | ❌ not performed |
| 7–10 — Quality/session controls, operational GUI, automated qualification, SMB / installed build / release gate | Pending | — | — | — | — | — |

Since revised phase 4, a scan session with several batches is added up: every
downstream stage reads the session's one effective sheet set
([ADR-0007](../decisions/ADR-0007-session-effective-sheet-set.md)). Revised
phase 5 adds the **headless** intake layer - sources, the intake ledger,
stabilisation, verified copy ingest and a registration API
([intake](../intake.md), [ADR-0008](../decisions/ADR-0008-intake-copy-or-reference.md)).
Revised phase 6 adds the **headless** continuous-processing engine - finite
sealed units per source, durable claims, one coordinator writing, crash-safe
restart in the same session and batches
([ADR-0009](../decisions/ADR-0009-continuous-engine-single-writer.md)); watched
folders are still not scanned for an operator (the GUI is phase 8).

**Architecture:** `Project → ScanSession → one or more finite ScanBatch
objects → sheets`. A *scan batch* stays a finite, auditable processing and
provenance unit; a *scan session* is the examination-level unit that may stay
open while further batches arrive from several scanners, computers, network
shares or later rescans; the *project* stays the persistent container.
Attendance, reconciliation, scoring, Results and final Reports get one
authoritative session-level view; batch-level views remain for provenance and
monitoring. A normal finite import is a session with one batch and needs no
new steps.

**Scope:** canonical, case-insensitive set identity with optional logical ↔
physical set marks; scan sessions and genuinely finite batches; session-level
results; multi-source intake (watched and manual) with stable-file detection
and idempotent registration; continuous processing through finite units; a
scan-quality decision layer that suggests rejections to the existing Reject &
Rescan workflow; an operator GUI; synthetic intake and real network-share
qualification. It fixes six latent `0.1.0-alpha.2` defects found while
planning — among them that late scans can split an examination into a second
batch that Attendance and Results then read alone.

Two further requirements are explicit: **supersession** is first-class — only
effective, non-superseded batch and sheet membership counts towards
session-level results, while superseded batches are kept for provenance and
audit — and **crash-safe persistence and resume**: after a crash, forced
termination, power failure or normal close, reopening the project keeps every
committed Scan and Resolve step and resumes the same session. A fresh
100,000-sheet run is optional for this Alpha; before the first Beta, a fresh
100,000-sheet qualification on the new architecture and a fix for the
installer's prerelease version ordering are mandatory gates.

Seven phases, A–G. Full plan, architecture notes, acceptance criteria and
implementation prompts:
[`development/releases/0.1.1-alpha.0/`](https://github.com/sajidbuet/OMRFlow/blob/main/development/releases/0.1.1-alpha.0/ROADMAP.md)
— the single authoritative plan for this release (it supersedes
`development/ROADMAP_v0.1.1-alpha.0.md`).

**Phase 11B and this line:** Phase 11B real-data qualification **continues
alongside** development of the `0.1.1` line. It is not replaced by `0.1.1`, and
it is not moved entirely before or after it. Phase 11B's definition below is
unchanged; its Beta target is now `v0.1.1-beta.x`.

---

## Phase 11A — Alpha Release Infrastructure

**Status: Implemented — validation pending.**
The `0.1.0-alpha.1` candidate is built, and the clean-machine test has now
been run: its automated portion passed on a pristine Windows image, 56 checks
to none. What remains are the steps of that procedure that need a person to
look at the screen — above all an end-to-end run through recognition on the
*installed* build. Published as
[`v0.1.0-alpha.1`](https://github.com/sajidbuet/OMRFlow/releases/tag/v0.1.0-alpha.1)
on 2026-09-22 with that limitation stated in the release notes.

### Purpose

Create a safe, installable and clearly identified Alpha distribution suitable
for external evaluation while production qualification remains incomplete.

### Delivered

- **Centralised versioning.** `src/omr_scanner/_version.py` is the single
  place the version is written down; `pyproject.toml` reads it from there.
  The release channel is derived from the version string, so a build cannot
  claim a maturity its version does not support.
- **`v0.1.0-alpha.1` release convention**, valid as both Semantic Versioning
  and PEP 440.
- **Alpha-labelled About information**, including the build identifier with
  its source commit; the version in the window title.
- **Windows packaging**: a PyInstaller bundle and an Inno Setup installer,
  `OMRFlow-<version>-Setup-x64.exe`, requiring no Python on the target
  machine.
- **A concise public README** with a screenshot and an animated tour.
- **This documentation set** under `docs/wiki/`: installation, quick start,
  user guide, known limitations, upgrade and data-retention guidance.
- **Release infrastructure**: a checklist, a release-notes template, a
  clean-machine test procedure, SHA-256 checksums, and build/verify scripts
  under `scripts/release/`.
- **Repository governance**: contribution guide, security policy, support
  guide, six structured issue forms, a pull-request template.
- **Continuous integration** on Windows and Linux, plus a packaging smoke
  test; and a release workflow that produces a *draft* release and never
  publishes on its own.
- **Compatibility metadata** in the diagnostic bundle: release channel, build
  identifier and expected schema version.

### Verified

- The packaged application launches, reports the correct version, stays
  responsive and closes cleanly — 16 automated checks.
- The installer installs, the installed application launches, the uninstaller
  removes it, and **user configuration and logs survive** — 15 automated
  checks.
- Release artifacts are named for their version, agree with each other, and
  match their published checksums.
- The full automated suite passes — 4162 tests, plus `ruff` and `mypy`.
- **On a pristine Windows image** (Windows Sandbox, no Python, no Qt, no
  build tools): checksum verified on the machine under test, per-user
  install with no elevation, first launch with no missing DLL or Qt plugin,
  a log written under the user profile and **nothing written into the
  installation directory**, clean exit, relaunch, uninstall with user data
  intact, and reinstall — 56 checks, none failed. Recorded in
  [`docs/release/validation/`](https://github.com/sajidbuet/OMRFlow/tree/main/docs/release/validation).
- The installed application works from a path containing spaces and
  characters outside ASCII.
- Every dependency the application imports survives the freeze, including the
  pure-Python ones that live inside the archive rather than on disk.

### Not yet verified

- 🟠 **The clean-machine steps that need a person.** The mechanical portion
  passed (above). Not performed: the SmartScreen warning and its wording, the
  licence page, the Alpha warning shown during installation, the nine stage
  icons actually rendering, the About dialog, and — the substantial one —
  **the end-to-end run on the installed build**: create a project, validate
  the example template, generate synthetic sheets, *Process All*, and reach a
  result. Until that is done, recognition, Excel reporting and the
  multiprocessing worker path have been exercised only from source, never
  from the installer. `packaging/sandbox/Complete-ManualChecks.ps1` walks
  through these and records the answers into the same report.
- 🟠 **Windows 10.** Built and tested on Windows 11; the clean-machine run
  was on a Windows 11 image.
- ⚪ **Code signing.** Not configured; SmartScreen warns. A Phase 11C item.
  Note that the sandbox has networking disabled, so SmartScreen could not
  appear there at all and its wording remains unverified.

### Exit criterion

> A non-developer can install OMRFlow and evaluate the documented workflow
> using sample/synthetic data, with the software clearly identified as an
> Alpha release.

A non-developer's machine **has** now been used: a pristine Windows image
installed OMRFlow without administrator rights, launched it, and removed it
again without touching user data. What has not been shown there is the middle
of that sentence — *evaluate the documented workflow*. Nobody has yet driven
the installed build through a recognition run, so the criterion is met for
"can install" and "clearly identified as an Alpha release", and outstanding
for "evaluate the documented workflow".

---

## Phase 11B — Real-Data Qualification & Beta Release

**Status: Pending.** Target: `v0.1.1-beta.1` — the first Beta of the `0.1.1`
line, following `v0.1.1-alpha.x` (changed from `v0.1.0-beta.1` on 2026-09-30:
`v0.1.0-beta.x` is not used as a successor to `v0.1.1-alpha.0`). No Beta tag is
created until the installer's prerelease version ordering is fixed and tested,
and a fresh 100,000-sheet qualification on the 0.1.1 architecture has passed.

Phase 11B runs **alongside** the `0.1.1-alpha.0` development line — not
replaced by it, and not moved entirely before or after it. Real-data evidence
from `0.1.0-alpha.2` stays valid for what `0.1.1` does not change (recognition,
calibration, scan-quality evidence, scoring rules); evidence about scan
sessions, multi-source intake and session-level results can only come from
`0.1.1` builds.

### Purpose

Validate OMRFlow with representative real examination material, and move from
Alpha to Beta once the core workflows are proven with real data rather than
synthetic data.

This is the phase that closes the single largest gap in OMRFlow's current
claims — see [Known Limitations](Known-Limitations).

### Required qualification

Representative **real**: OMR answer sheets, scanner outputs, attendance
workbooks, candidate rosters, answer keys, examination sets and generated
reports.

Including these cases:

| Category | Cases |
|---|---|
| Sets | Multiple examination sets; wrong or unknown set codes |
| Attendance | Absent candidates; absent-with-script; present-without-script; duplicate roll numbers; duplicate scripts; unknown candidate IDs |
| Marks | Blank answers; multiple marks; ambiguous bubbles; erased and corrected marks |
| Images | Skewed; rotated; varying resolutions; low quality |
| Change | Answer-key changes; scoring-configuration changes; recomputation |
| Output | Roll-wise; merit-wise; workbook logos and formatting |
| Robustness | Interrupted processing; project reopen; migration from a previous version |

### Golden reference datasets

Sanitised reference datasets with **independently known expected outputs**,
machine-verifiable for: recognised candidate ID, recognised set, recognised
answer string, ambiguity and blank symbols, attendance state, score, negative
marks, final marks, rank, roll-wise output, merit-wise ordering, and the
exclusion of absentees from the merit-wise sheet.

> **The Beta is not approved by visual inspection.** A person looking at a
> report and finding it plausible is not qualification; a stored result
> matching an independently computed expected result is.

### Exit criterion

> Representative real examination datasets can be processed end to end and
> OMRFlow's stored and generated results match independently verified
> expected results.

---

## Phase 11C — Release Candidate & Stable Release

**Status: Pending.** Targets: `v1.0.0-rc.1`, then `v1.0.0`.

### Purpose

Freeze functionality, qualify the **packaged** application, validate upgrades
and documentation, and promote OMRFlow to its first stable release.

### During the release candidate

- **Feature freeze.** Only defect, compatibility, accessibility and
  documentation corrections, unless a critical exception is justified and
  recorded.
- Regression tests rerun after **every** release-blocking change.

### Required qualification

- The full automated suite; synthetic end-to-end; real-data qualification
  from 11B.
- **Clean Windows installation**; uninstall and reinstall; upgrade from the
  previous supported version; project migration; backward compatibility
  where promised.
- Data-retention verification; recovery after interrupted processing.
- **The 100,000-sheet stress qualification** — required before stable unless
  formally waived and documented.
- A documentation walkthrough by someone who did not write it.
- An accessibility review, including screen-reader testing.
- A security and dependency review.
- **Code signing**, so SmartScreen no longer warns.
- Final report verification; release artifact checksums.

> Qualification must test **the actual packaged artifact**, not only the
> development environment.

### Stable exit criterion

> A non-developer can install OMRFlow and safely complete a representative
> examination following the user guide alone, with independently verified
> recognition, scoring, attendance and reporting results and no known
> release-blocking defects.

---

## Examination sets — a cross-phase enhancement

Not a numbered phase: an enhancement running alongside them, letting one
project describe an examination divided into several sets and carry that
division through attendance and reporting. Split so each part is
independently testable.

### Part 1 — Project configuration · **Implemented and tested**

An examination name, and a variable number of persistent, uniquely
identified sets each with an operator-visible code and a description.
`project.json` gained `exam_name` (format version 1 → 2, read backward
compatibly); the `project_set` table (migration 8) holds each set's stable
identifier, code, description and display order.

Covered by 116 automated tests plus an end-to-end GUI check.

### Part 2 — Per-set attendance and set-aware reporting · **Implemented; synthetic testing only**

One attendance workbook per set; that workbook becoming the set's result
template; a roll-wise result built on it; and a merit-wise sheet copied from
the completed roll-wise sheet with absentees removed and the rest ordered by
merit.

**Pending:** real-data validation using representative attendance
workbook(s) and scanned cohort(s). No real attendance workbook and no real
scanned cohort has been processed end to end.

See [Examination Sets](Examination-Sets).

---

## Release qualification matrix

What each release channel requires. Used by
[Release Process](Release-Process) and the release checklist.

| Requirement | Alpha | Beta | RC | Stable |
|---|---|---|---|---|
| Unit tests | Required | Required | Required | Required |
| Integration tests | Required | Required | Required | Required |
| Synthetic end-to-end | Required | Required | Required | Required |
| Windows installer | Required | Required | Required | Required |
| Clean-PC smoke test | Required | Required | Required | Required |
| Real attendance workbook | Not blocking | Required | Required | Required |
| Real scanned cohort | Not blocking | Required | Required | Required |
| Golden-result verification | Not blocking | Required | Required | Required |
| Upgrade compatibility | Basic | Required | Required | Required |
| Complete documentation | Partial acceptable | Near-complete | Required | Required |
| Accessibility audit | Initial | Required | Required | Required |
| 100k-sheet qualification | Optional | Recommended | Required unless formally waived and documented | Required, or waived with justification |
| Code signing | Not blocking | Recommended | Required | Required |
| Feature freeze | No | No | Yes | Yes |
| Known release-blocking defects | Allowed if documented | None | None | None |

### Where `0.1.0-alpha.1` stands against it

| Requirement | Status |
|---|---|
| Unit, integration, synthetic end-to-end | ✅ Passing |
| Windows installer | ✅ Built, install/uninstall verified |
| Clean-PC smoke test | 🟠 **Required for Alpha and not yet performed** |
| Upgrade compatibility (basic) | ✅ Migrations tested; no prior release to upgrade from |
| Documentation | ✅ Partial is acceptable at Alpha |
| Accessibility (initial) | ✅ Keyboard, focus, contrast, high-DPI verified; screen reader not tested |
| 100k-sheet qualification | ⚪ Optional at Alpha; harness ready, not run |
| Known release-blocking defects | ✅ None known, and documented as such |

The clean-machine test has been run and its automated portion passed. What
keeps Phase 11A at *Implemented — validation pending* rather than complete is
the remainder of that same procedure: the steps a person has to perform,
chiefly an end-to-end workflow run on the installed build.

---

## Related

- [Release Process](Release-Process)
- [Release History](Release-History)
- [Known Limitations](Known-Limitations)
- `development/ROADMAP.md` — per-phase detail for Phases 0–10
