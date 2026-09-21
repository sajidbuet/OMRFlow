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
| **11A** | **Alpha Release Infrastructure** | **Implemented — clean-machine validation pending** |
| 11B | Real-Data Qualification & Beta Release | **Pending** |
| 11C | Release Candidate & Stable Release | **Pending** |

Per-phase detail for 0–10: `development/ROADMAP.md` and the
`development/PHASE_NN_HANDOFF.md` documents.

---

## Phase 11A — Alpha Release Infrastructure

**Status: Implemented — clean-machine validation pending.**
Released as [`v0.1.0-alpha.1`](Release-History).

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
- The full automated suite passes.

### Not yet verified

- 🟠 **Clean-machine installation.** Every installer test so far ran on the
  machine that built it, which has a Python development environment. The
  procedure is written (`docs/release/CLEAN_MACHINE_TEST.md`) and has not
  been executed.
- 🟠 **Windows 10.** Built and tested on Windows 11.
- ⚪ **Code signing.** Not configured; SmartScreen warns. A Phase 11C item.

### Exit criterion

> A non-developer can install OMRFlow and evaluate the documented workflow
> using sample/synthetic data, with the software clearly identified as an
> Alpha release.

Met *except* that "a non-developer's machine" has not been used — which is
precisely the clean-machine validation above.

---

## Phase 11B — Real-Data Qualification & Beta Release

**Status: Pending.** Target: `v0.1.0-beta.1`.

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

The clean-PC smoke test is the one Alpha requirement outstanding, which is
why Phase 11A is *Implemented — clean-machine validation pending* rather than
complete.

---

## Related

- [Release Process](Release-Process)
- [Release History](Release-History)
- [Known Limitations](Known-Limitations)
- `development/ROADMAP.md` — per-phase detail for Phases 0–10
