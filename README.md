# OMRFlow

**Open-source Optical Mark Recognition (OMR) scanner and examination
processing software for Windows.**

OMRFlow lets educators and examination administrators design OMR
bubble-sheet templates, process scanned answer sheets, review uncertain
marks, reconcile attendance, score MCQ examinations, and generate
auditable Excel results locally.
<div align="center">

<img src="src/omr_scanner/gui/resources/branding/logo.svg" alt="OMRFlow" width="220">

**Design OMR templates, process scanned answer sheets, resolve recognition
conflicts, reconcile candidate attendance, evaluate MCQ examinations and
generate auditable results — entirely on your own machine.**

[![Release](https://img.shields.io/badge/release-0.1.0--alpha.2-AC1F24)](https://github.com/sajidbuet/OMRFlow/releases)
[![Status](https://img.shields.io/badge/status-Alpha-orange)](docs/wiki/Known-Limitations.md)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-blue)](docs/wiki/Installation.md)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
[![Licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![Zenodo](https://img.shields.io/badge/Zenodo-archived-1682D4?logo=zenodo&logoColor=white)](https://doi.org/10.5281/zenodo.22943575)
[![DOI](https://zenodo.org/badge/1371137095.svg)](https://doi.org/10.5281/zenodo.22943575)

</div>

![OMRFlow opening a demonstration project and moving through its workflow: the
compact ribbon across the top, the Template stage with a marked-up answer
sheet, the Scan stage recognising a sheet and listing the roll number, set code
and answers it read, then the Attendance and Reports
stages](docs/images/omrflow-workflow-stages.gif)

<div align="center"><sub>A demonstration project, a real template, and one real
sheet recognised — captured from the running application by
<a href="scripts/generate_readme_demo.py"><code>scripts/generate_readme_demo.py</code></a>.</sub></div>

---

> ## ⚠️ Alpha release
>
> **OMRFlow is currently available as an Alpha release for evaluation and
> testing.** Core workflows are implemented and covered by an automated suite
> of over four thousand tests, but **real examination-data qualification is
> still underway** — no real attendance workbook and no real scanned cohort
> has been processed end to end.
>
> **Generated results should be independently verified before operational
> use.** See **[Known Limitations](docs/wiki/Known-Limitations.md)** for
> exactly what has and has not been validated.

---

## What is OMRFlow?

OMRFlow reads optical-mark-recognition answer sheets and turns them into
examination results. You mark up a blank sheet once to make a template, scan
the completed sheets on an ordinary document scanner, and OMRFlow aligns each
page, reads the marks, tells you what it was unsure about, matches the
scripts against your candidate list, scores them against a verified answer
key, and produces result workbooks built on your own attendance workbook.

It is a flexible, transparent, locally operated alternative to proprietary
OMR systems:

**ordinary image scanner + configurable template + transparent recognition +
human verification + reproducible result processing**

Nothing is uploaded anywhere. A project is a folder on your disk.

## Key features

- **Template designer** — registration markers, bubble grids, per-bubble
  adjustment, validation. Coordinates are normalised, so one template works
  at any scan resolution.
- **Geometric normalisation** — rotation, translation, scale, skew and
  perspective corrected to the template's canonical page before anything is
  measured.
- **Transparent recognition** — a blank, a multiple mark and an uncertain
  read are reported as what they are, never silently resolved into an answer.
- **Multicore batch processing** — one sheet per CPU worker, with identical
  results and output ordering on any number of cores.
- **Durable batches** — an interrupted run resumes without reprocessing what
  it already read. Power loss included.
- **Human resolution with an audit trail** — every correction is recorded
  append-only beside the machine's original reading, attributed to a named
  reviewer with a reason.
- **Attendance reconciliation** — unknown, duplicate and missing scripts,
  absent-with-script and present-without-script, each decided explicitly.
- **Per-set answer keys and scoring** — verification required before use,
  negative marking, defective questions, standard competition ranking.
- **Reports built on your own workbook** — your columns, fonts, merged cells
  and institution logo are preserved, because the report is a copy of your
  attendance workbook with the results written into it.
- **Examination sets** — one project can describe an examination divided into
  several question papers, carried through attendance, keys and reporting.
- **Project health, backup and recovery** — integrity checking that reports
  and never silently repairs.

## The application window

OMRFlow puts everything above your work into **one compact row**, which is
also the window's title bar:

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ ☰  OMRFlow │ − +  ‹  1 Project › 2 Template › … › 9 Reports  ›   _  ☐  ✕ │
├──────────────────────────────────────────────────────────────────────────┤
│  the stage you are on                                                    │
├──────────────────────────────────────────────────────────────────────────┤
│ OMRFlow v… │ Project: …      Developed by … │ Open Source (MIT) │ ● Ready│
└──────────────────────────────────────────────────────────────────────────┘
```

- **One chrome row.** The application menu, the wordmark, the workflow and the
  window buttons share a single line. There is no separate header band and no
  stage heading repeating what the ribbon already says, which leaves the
  Template, Calibrate, Scan, Resolve and Reports stages close to the whole
  window to work in — including on a 1366×768 display.
- **The workflow is always one line.** All nine stages when they fit; a
  horizontally scrolling strip when they do not; and at very narrow widths the
  current stage alone, with the other eight one hover, click or keypress away
  in a selector. It never wraps onto a second row, and no stage is ever
  hidden from navigation.
- **The active stage is always visible.** However you move — a click, the
  `‹`/`›` arrows, a menu command, a keyboard shortcut — the ribbon scrolls it
  into view or becomes it.
- **`−` and `+` set how much room the workflow takes**, not how big the page
  is. They change the ribbon's padding only, within readable limits, and your
  choice is remembered in your own settings — no project file is involved.
- **The footer says which project is open**, by its examination title rather
  than its folder path, and updates the moment you create, open, rename or
  close one.
- **It still behaves like a Windows window.** Drag it by the logo or the empty
  space in the row, double-click there to maximise, resize from any edge or
  corner, and use Aero Snap, Alt+F4, Win+Up and Win+Down as usual — the window
  manager does all of that, not a hand-rolled substitute. The one thing
  framelessness costs is the system drop shadow; a hairline border stands in
  for it.

## Current release status

**`0.1.0-alpha.2`** — the current Alpha build.

| | |
|---|---|
| Release channel | **Alpha** — for evaluation and testing |
| Platform | Windows 10 1809 or newer, 64-bit |
| Installer | Unsigned; SmartScreen will warn |
| Real-data qualification | **Incomplete** |

The release channel is derived from the version string, so a build cannot
claim a maturity its version does not support.

## Download and installation

Get `OMRFlow-0.1.0-alpha.2-Setup-x64.exe` from the
**[Releases page](https://github.com/sajidbuet/OMRFlow/releases)**. No Python
required.

Verify the download against the published `SHA256SUMS.txt` — the installer is
unsigned, so the checksum is how you confirm you have the file that was
built:

```powershell
certutil -hashfile OMRFlow-0.1.0-alpha.2-Setup-x64.exe SHA256
```

Full instructions, including the SmartScreen warning and where your data
lives: **[Installation](docs/wiki/Installation.md)**.

## Quick start

**[Quick Start](docs/wiki/Quick-Start.md)** walks the whole workflow in about
half an hour using **synthetic data OMRFlow generates itself** — so you can
see what it does without needing real answer sheets:

create a project → open the example template → generate synthetic sheets →
process them → resolve what recognition flagged → import a candidate list →
enter an answer key → score → generate reports.

## Documentation

| | |
|---|---|
| **[Documentation home](docs/wiki/Home.md)** | Everything, organised |
| [Installation](docs/wiki/Installation.md) | Requirements, install, upgrade, uninstall |
| [Quick Start](docs/wiki/Quick-Start.md) | The whole workflow, with synthetic data |
| [Synthetic datasets](docs/testing/SYNTHETIC_DATA.md) | Generating test scans — fully drawn, or synthetic marks laid on a real blank scan, optionally with folded corners — **and** attendance workbooks with exact ground truth |
| [User Guide](docs/wiki/User-Guide.md) | The nine stages in detail |
| [Scan quality](docs/scan_quality.md) | How a folded or curled sheet is detected, and what it deliberately does not flag |
| [Known Limitations](docs/wiki/Known-Limitations.md) | **What is and is not trustworthy yet** |
| [Troubleshooting](docs/wiki/Troubleshooting.md) | When something goes wrong |
| [Upgrading](docs/wiki/Upgrading-OMRFlow.md) | And what happens to your projects |
| [Development Roadmap](docs/wiki/Development-Roadmap.md) | Phase status and what is next |
| [Architecture](docs/wiki/Developer-Architecture.md) | For contributors |
| [Release Process](docs/wiki/Release-Process.md) | How a release is made |

If OMRFlow is useful in your work, please consider citing it using the
[Zenodo DOI](https://doi.org/10.5281/zenodo.22943575).

## Development status

**Current release: `0.1.0-alpha.2`** (development baseline; `main` carries
further unreleased work — see below).

**Next development target: `v0.1.1-alpha.0`** — scan sessions made of finite
batches (one scanner or many, finite imports or continuous watched folders,
later rescans), session-level attendance, results and reports, robust rescan
provenance, and set-code identity improvements. **In development, not
released:** [plan](development/releases/0.1.1-alpha.0/ROADMAP.md), built in
ten revised phases.

- **Completed (implemented, tests passing, merged):** phase 1, set identity;
  phase 2, scan sessions and finite batches.
- **Under testing (implemented, tests passing, not merged):** phase 3,
  crash-safe Scan/Resolve persistence, on branch
  `feat/0.1.1-phase3-crash-safe-persistence` - tested with real process kills;
  power loss not tested; not used by an operator.
- **Pending:** phases 4–10 - session-level results (a session of several
  batches is **not** added up until then), intake, continuous processing,
  quality/session controls, operational GUI, automated qualification, SMB /
  installed build / release gate.

| `0.1.1` revised phase | Implemented | Automated tests | Synthetically validated | Real-scan validated | Network-share validated | Production qualified |
|---|---|---|---|---|---|---|
| 1 — Set identity | ✅ merged | ✅ passing | n/a for this phase | ❌ | n/a | ❌ |
| 2 — ScanSession + finite ScanBatch lifecycle | ✅ merged | ✅ passing | n/a for this phase | ❌ | n/a | ❌ |
| 3 — Crash-safe Scan/Resolve persistence | ✅ (branch, unmerged) | ✅ passing, incl. real-process kill matrix | ✅ synthetic crash/restart (40 and 1,000 sheets) | ❌ | ❌ | ❌ |
| 4 — Session-level effective results | ⚪ pending | — | — | — | — | — |
| 5 — Intake sources + ledger | ⚪ pending | — | — | — | — | — |
| 6 — Continuous-processing engine | ⚪ pending | — | — | — | — | — |
| 7 — Quality / rescan / session controls | ⚪ pending | — | — | — | — | — |
| 8 — Operational GUI | ⚪ pending | — | — | — | — | — |
| 9 — Automated qualification | ⚪ pending | — | — | — | — | — |
| 10 — SMB / installed build / Alpha release gate | ⚪ pending | — | — | — | — | — |

Phases 0–10 are implemented; Phase 11 takes OMRFlow from "implemented and
synthetically tested" to a qualified stable release.

| Phase | Status |
|---|---|
| 0–2 — Foundation, geometry, template designer | **Complete** |
| 3–9 — Recognition, calibration, batch, conflicts, attendance, scoring, reporting | Implemented; synthetic testing complete, real-data testing in progress |
| 3/6 addendum — scan-quality (page-geometry) detection | Implemented; **under testing** — synthetic and real-scan validation done, wider real-batch validation pending |
| 10 — Integration, recovery, production hardening | Implemented; 100,000-sheet acceptance run pending |
| **11A — Alpha release infrastructure** | **Implemented — validation pending**; release automation complete and tested, clean-machine test run and passed, its manual steps outstanding |
| 11B — Real-data qualification & Beta | Pending |
| 11C — Release candidate & stable | Pending |

### Testing status

| | |
|---|---|
| Automated suite | Full local run on branch `feat/0.1.1-phase3-crash-safe-persistence` (0.1.1 revised phase 3), 2026-10-01: **6,293 passed, 16 skipped, 0 failed** (56 min 18 s in a worktree, where 13 real-sheet tests skip for want of their untracked fixture; those 13 passed against the branch from the main checkout; 5 `stress` tests deselected, the 1,000-sheet crash series among them, run separately: passed); `ruff` and `mypy` (208 files) clean. Includes a real-process kill / restart matrix (ACCEPTANCE §5.4 cases 1–15). Previously: full local run on branch `feat/0.1.1-phase2-scan-session-lifecycle` (0.1.1 revised phase 2), 2026-10-01: **6,241 passed, 16 skipped, 0 failed** (59 min 00 s; 4 `stress` tests deselected); `ruff` and `mypy` (206 files) clean. Previously: full local run on branch `feat/0.1.1-phase1-set-identity` (0.1.1 phase A), 2026-10-01: **6,168 passed, 16 skipped, 0 failed** (39 min 24 s; 4 `stress` tests deselected); `ruff` and `mypy` (203 files) clean. Previously: full local run after the Results Dashboard work, 2026-10-01: **6,066 passed, 15 skipped, 0 failed** (38 min 29 s; 4 `stress` tests deselected); `ruff` (src, tests, tools, scripts) and `mypy` (201 files) clean. Previously: full local run after the Attendance dispositions work, 2026-09-30: **5,972 passed, 16 skipped, 3 failed** (1 h 05 min; 4 `stress` tests deselected). One failure was an expected enumeration assertion (two new placements), since updated and passing; the other two (`test_developer_tools` small-screen tab focus, `test_stress_kill_resume[100]` read-only database on resume) pass when rerun alone and did not touch this change — recorded as load/environment flakiness, not fixed. `ruff` clean; `mypy` reports the same 2 pre-existing errors as `main`. Previously: full local run after the preview-freeze fix and hang watchdog, 2026-09-30: **5,925 passed, 15 skipped, 0 failed** (36 min 59 s; 4 `stress` tests deselected); `ruff` and `mypy` clean. After the Results button fix: 5,911 passed; after the answer-key verification fix: 5,906; after the Step 7 real-sheet fixes: 5,900; after the Step 7 rework: 5,878. Earlier runs, before merging: 5,617 passed, 15 skipped after the Reject & Rescan hardening (2026-09-29); 5,608 passed, 3 skipped with answer keys and solution sheets (2026-09-29). 4 `stress` tests deselected by default; `ruff` and `mypy` clean on each branch |
| Cross-platform CI | 🟠 Tests and packaging green on Windows and Ubuntu ([run 36210285696](https://github.com/sajidbuet/OMRFlow/actions/runs/36210285696), 2026-09-26); the lint/type gate was red from 2026-09-25, when SQLAlchemy 2.1 respelled a query annotation — corrected, awaiting a confirming run |
| Synthetic end-to-end | ✅ Passing, from source |
| Synthetic qualification data | ✅ Template-driven scans **and** set-specific attendance workbooks with deliberate reconciliation conflicts and exact ground truth — see [Synthetic datasets](docs/testing/SYNTHETIC_DATA.md) |
| Synthetic marks on real paper | 🟠 **Implemented — automated tests passing, validated once on a genuine blank scan.** Marks drawn onto a scan of a real blank form, registered through the production alignment pipeline, in colour / grayscale / black-and-white; attendance and reconciliation generation are unaffected by the choice of mode. Automated coverage passes, including registration measured against known homographies (worst case 0.33 px at the bubble centres). On a real 2526×3417 scan of a 100-question form: registration reprojection error 0.0 px with no warnings, one mark changed 0.0057 % of the page with all five printed marks byte-identical, and 5 sheets / 500 answers read back with **0 wrong** (2 correctly flagged). One form, one operator, one scanner — a smoke test, not a corpus. The marks themselves remain synthetic in both modes |
| Physical corner folds | 🟠 **Implemented — full suite passing.** Micro / small / moderate / severe folds at any of the four corners, in both rendering modes, applied to the composed sheet so paper, printing and marks fold together. Marker interaction is computed from the template's actual marker polygons — per-marker overlap fractions, including the orientation mark — and an interaction the geometry cannot produce is reported as not applicable rather than faked. Off by default, and a disabled policy is byte-identical to a run from before folds existed. 236 new automated tests; see [Synthetic datasets](docs/testing/SYNTHETIC_DATA.md). Exercised once on a real blank scan, where the predicted outcome held at every interaction level: folds reaching no marker read 100/100, a quarter-covered marker registered with a warning, and both fully covered markers were refused exactly as their `expect_failure` said. **The fold itself is still drawn — no photograph of genuinely folded paper has been through it** |
| Packaged application | ✅ Launches, navigates and closes cleanly under UI Automation |
| Installer | ✅ Install → launch → uninstall → **user data preserved** → reinstall |
| Clean machine | ✅ 56/56 automated checks on a pristine Windows image; its manual steps outstanding |
| Accessibility | 🟠 Automated checks pass; 8 controls have no accessible name (recorded, non-blocking at Alpha) |
| Real examination data | ❌ **Not started** — this is Phase 11B |
| 100,000-sheet qualification | ⚪ Harness ready, not run |
| Release automation | ✅ One command prepares a release; GitHub Actions builds and publishes it. 96 tests, no step needs a person or a model — see [Release Checklist](docs/release/RELEASE_CHECKLIST.md) |
| Template Editor interaction | ✅ Debugging pass completed — see below |
| Real-scan registration | 🟠 First real scanned cohort registered and calibrated — 8 sheets, one template. See below |
| Calibration workspace | ✅ Reorganised around the scan preview — see below |
| Project template | ✅ The template is now project state, chosen once and shared by Template, Calibrate and Scan — see below |
| Conflict-resolution semantics | ✅ Updated — Resolve now covers student ID / roll and set code only; ambiguous answers stay in the recognition result. See below |
| Resolve stage UX & reversible decisions | 🟠 **Implemented — automated tests passing, not yet exercised by a real reviewer.** Whole-position lane overlay, keyboard-first operation, auto-advance, and undo / redo / undo-a-whole-sheet as persisted audit events. See below |
| Resolve stage layout & workflow (2nd pass) | 🟠 **Implemented — automated tests passing; inspected in rendered screenshots, not yet worked by a real operator.** Space redistributed 29/71 and 70/30, ROI framing, machine-vs-operator colour semantics, pick-then-confirm, and an action that no longer offers to store an invalid reading. See below |
| Resolve field-level entry & sheet-local progression | 🟠 **Implemented — automated tests passing; driven end to end in a rendered harness, not yet worked by a real operator.** A whole Student ID or Question Set typed once settles every position of it the sheet disputes; the queue is sheet-major and finishes a sheet before moving on. See below |
| Attendance: reconciliation workstation | 🟠 **Implemented — automated tests passing; the 100-candidate acceptance scenario driven in a rendered harness, not yet worked by a real operator.** Missing script and absent-but-script-found are investigated from the original scan; the complete Student ID or set code is corrected through the Resolve stage's own review ledger; suggestions of where to look; a compact layout usable at 1366×768; the Choose / Replace Attendance File defect fixed. See below |
| Resolve: overriding a confident reading | 🟠 **Implemented — automated tests passing; acceptance scenario driven in a rendered harness, not yet worked by a real operator.** Explicit full-field editing of the Student ID or Question Set / Set Code can now overrule a position the machine read confidently, after a warning, as an audited override that one `Ctrl+Z` takes back. Both editors are now **sheet actions**, available whatever record is selected and on a sheet with no conflict on that field; set codes with multi-character symbols (`10`, `11`) are reassembled by symbol. See below |
| Reject & Rescan | 🟠 **Implemented — automated tests passing; acceptance scenario driven in the real window by a script (screenshots inspected), not yet worked by an operator on real sheets.** An unusable scan is rejected on Resolve, stops counting at once, is replaced only by an explicitly confirmed rescan, and its image can later be quarantined or purged. **Follow-up hardening (implemented — automated tests passing; scripted in the real window, not yet worked by an operator):** the rescan may be read in a later batch; a sheet with no conflict can be rejected from *All processed sheets*; Undo Reject and unlinking bring duplicate-ID records back. See below |
| Attendance dispositions (Keep / Reject / Defer / Restore) | 🟠 **Implemented — automated tests passing; acceptance scenario driven in the real window by a script (24/24 checks, screenshots at 1366×768 and 1100×680 inspected), not yet worked by an operator on real sheets.** A duplicate is settled by inspecting each copy and keeping one; an unwanted sheet is rejected / excluded; an undecided one is deferred; each is restorable. One eligibility rule (the Reject & Rescan lifecycle) from reconciliation through scoring, Results, reports and reopening. See below |
| Results Dashboard tab | 🟠 **Implemented — under testing.** Automated tests passing; driven in the real window by a script (49/49 checks, screenshots at 1366×768 and 1100×680 inspected); not yet used by an operator. Read-only statistics for Overall Exam or one set — marks distribution, question difficulty and discrimination, distractors, review flags, set comparison, KR-20 — from exactly the rows the Results table lists. See below |
| Scan-quality / page geometry | 🟠 **Implemented — under testing.** Detects a physically folded, curled or lifted sheet that registers cleanly but whose printing has moved. Validated on synthetic lattices, the committed sample sheet and two real scans; see [Scan quality](docs/scan_quality.md) |
| Synthetic answer keys, solution sheets & candidate performance | 🟠 **Implemented — automated tests passing; taken through the real Answer Key → Results path in the application window (driven offscreen, not yet by a person), where five operator-path defects were found and fixed.** Every generated dataset now has a `solution/` folder with one clean solution OMR sheet and one answer-key text file per set, both derived from one canonical key; candidates answer against their own set's key with a truncated-normal score distribution (default 65 % ± 15 %). OMRFlow's own engine reads every solution sheet back as its set code and key. See below |
| Answer Key stage (Step 7) rework | 🟠 **Implemented — under real-world validation.** One real solution sheet (ECE-0000) and four real scans used as stand-ins read 500/500 against a visual transcription with no false confident read; one dialog defect found and fixed; no operator use yet. The stage reads the project's own template (Scan need not be visited), shows every defined set's key state, keeps the key string and question table in step, reviews a marked solution sheet before anything is saved, records per-revision provenance (migration 12), and never uses a key that no longer fits the template. See below |
| Crash-safe Scan / Resolve persistence (`0.1.1` revised phase 3) | 🟠 **Implemented — automated tests passing, including a real-process kill / restart matrix; not used by an operator; power loss not tested; not production qualified.** A sheet's result and its review conflicts are saved as one unit; progress counts saved sheets only; reopening a project shows the interrupted Scan batch (recognised / failed / pending from the database) and the Resolve queue with every committed decision, without processing anything; nothing saved is read twice. No migration. [Handoff](development/releases/0.1.1-alpha.0/PHASE_C_HANDOFF.md) |
| Scan sessions & finite batches (`0.1.1` revised phase 2) | 🟠 **Implemented — automated tests passing; not used with a real scanner or by an operator; not production qualified.** Every batch belongs to a scan session (created silently by the first Process All); a later Process All - also after reopening the project - adds a new batch to the same session and seals the previous one; Reprocess creates a superseding batch and deletes nothing; minimal Session menu; migration 14 with upgrade backfill tested from schema-13 projects written by the schema-13 build. Downstream stages read one batch of the session until session-level results (phase 4). Crash-safe recovery: phase 3, above. [Handoff](development/releases/0.1.1-alpha.0/PHASE_B_HANDOFF.md) |
| Set identity (`0.1.1` phase A) | 🟠 **Implemented — automated tests passing; not used with a real scanner or real paper; not production qualified.** Set codes compare without regard to case, width or surrounding spaces everywhere (one function, enforced by an architecture test); a set may be *Printed on sheet as* another mark (Set 10 as `A`), translated once after Resolve with the raw reading kept; migration 13, with legacy `A` / `a` pairs kept, reported and blocking only their own set-dependent stages. Upgrade tested from schema-12 projects written by the schema-12 build. Counts and limitations: [handoff](development/releases/0.1.1-alpha.0/PHASE_A_HANDOFF.md) |
| Synthetic written Student ID & set code; used-reference cleanup | 🟠 **Implemented — automated tests passing; inspected visually on generated sheets and a real used sample form, not yet used in a real session.** The *intended* Student ID and set code (not the bubbled ones) are written in the boxes above their bubbles; a used reference form has its old writing **and its old bubble marks** removed first, keeping the printed rings, labels and borders. On the sample: 110/110 filled bubbles removed, none of the 390 unfilled touched; generated sheets read back 100/100. Full suite after the set-code and cleanup work: 5817 passed, 15 skipped, 0 failed. See below |

#### Results Dashboard (2026-10-01)

**Status: 🟠 Implemented — automated tests passing; acceptance scenario driven
in the real window by a script (screenshots at 1366×768 and 1100×680 at 175 %
Windows scaling inspected); not yet used by an operator on a real
examination. Not validated.**

The Results stage now has two tabs: **Results** — the existing stage, whose
controls, table, filters, scoring and behaviour are unchanged (its widgets
moved into the tab, nothing else) — and a read-only **Dashboard**. *Analysis:*
selects **Overall Exam** or a set (the project's own set codes, each with its
script count). For the selected scope: KPI cards (scored scripts, mean,
median, mode, sample SD, min, max, Q1–Q3), a marks histogram with mean and
median lines, a box plot with variance and skewness, and — per set — % correct
for every question with difficulty-band lines and counts, discrimination
(negative bars in red), easiest / hardest questions, a stacked correct /
incorrect / multiple / blank chart, *Questions to review*, a selected-question
option panel with the key highlighted, and KR-20 with its SEM. *Overall Exam*
adds a set comparison (N, mean, median, SD, min, max, KR-20, box plots). Every
bar has an exact-value tooltip; clicking a bar or a review row selects that
question.

- **Same data as the Results table.** `services/result_analytics.py` consumes
  the page's unfiltered `StoredResult` rows (one per candidate, effective set
  and answers after Resolve; rejected, deferred, superseded and duplicate
  sheets already excluded by scoring). Only *Scored* rows with a mark count.
  Per-question outcomes are regenerated with the scorer and each result's own
  key and policy revision — no second marking implementation. Stale results are
  included, as on the table, and announced.
- **Never combined across sets.** Sets have their own keys and OMRFlow records
  no question mapping, so question statistics are per set; *Overall Exam*
  shows them only for a single-set examination.
- **Formulas:** sample SD; linear-interpolation quartiles; adjusted
  Fisher-Pearson skewness; corrected point-biserial (item vs. rest score in
  questions correct); KR-20 with population variances, SEM = SD × √(1 − KR-20).
  Undefined values read *N/A* / *Not available* with the reason, never NaN.
  Thresholds (difficulty bands, review flags, minimum N) are in one
  `AnalyticsSettings` dataclass.
- **Performance:** computed on first opening of the tab, off the GUI thread,
  for every scope at once, and cached by a fingerprint of the result rows; a
  scope change is a lookup. The 210-script, 100-question acceptance set
  computes in about 0.1 s (plus the Results page's existing read).
- **Isolation:** a failure is shown on the Dashboard tab and logged; the Results
  tab is unaffected (tested).
- **Charts** are drawn with `QPainter` — `PySide6.QtCharts` is excluded from the
  frozen build, and no dependency was added.

**Tests.** 53 unit tests (hand-worked statistics, histogram binning, per-set
filtering, discrimination sign and formula, KR-20 normal / zero-variance /
too-few cases, distractors with 4 and 5 options, empty and one-script states,
flags); 9 integration tests on a real project (counts reconcile with the
Results summary; a rejected sheet, a confirmed rescan, an unresolved and a kept
duplicate, and a Resolve set-code override 2 → 3 are each followed); 24 GUI
tests (tabs, Results tab unchanged and still scores, default scope, set list,
scope switching and restoring, histogram, question charts, distractor panel,
click-to-select, empty set, stale notice, failure isolation, caching, resize at
1366×768 / 1100×680, vertical scrolling). Rendered acceptance:
`scripts/acceptance_results_dashboard.py` builds a 3-set, 100-question,
225-candidate project (`tests/analytics_fixtures.py`, with eight designed
questions), opens the real window, compares every scope's KPIs with the
Results rows computed independently, screenshots each panel and captures a
real hover tooltip — 49/49 checks passed.

**Limitations.** No pass mark exists in OMRFlow, so no pass rate is shown; no
export of analytics yet (the service returns structured values for a later
CSV / JSON / PDF step); only the batch the Results stage holds is analysed
(session-level results are 0.1.1-C); the release-validation baseline
`10-stage-results.png` predates the tab bar and will need regenerating;
display scaling other than 175 % was not checked on this machine; interpretation
bands are conventional defaults, not psychometric standards.

#### Answer keys, solution sheets and key-relative candidate performance

A synthetic examination can now test **scoring** end to end, not only
recognition.

**One key per set, stated three ways.** The generator draws one canonical key
per set from its own seeded stream and derives everything else from that one
object:

```text
dataset/
└── solution/
    ├── Set_10_Solution.png      a clean solution OMR sheet: same template, markers,
    ├── Set_10_Answer_Key.txt    bubbles, renderer and colour mode as the candidates
    ├── Set_10_Solution.json     its ground truth (role = "solution")
    ├── Set_11_…                 … one of each per set
    └── Set_12_…
```

The `.txt` file is **OMRFlow's existing answer-key text format** — the answer
string the Answer Key stage's *Answers* field reads through
`services.answer_key.read_key` (one option label per question, in question
order) — on one line, UTF-8, `\n`. It can be pasted into the stage unchanged,
and the solution sheet can be read with **Read From Solution Sheet…**. The
manifest records the same key (`generator.answer_keys`, `generator.solutions`).
Solution sheets are clean — no degradation, no folds — and leave the Student ID
blank, so a solution sheet scanned into a batch by mistake can never match a
candidate.

**Candidates answer their own set's key.** Each candidate's target share of
correct answers is drawn from a truncated normal distribution (inverse-CDF
sampling, standard library only), exactly that many questions — chosen
uniformly over the whole paper — take the key's answer, and the rest take a
uniformly chosen *other* option. The case plan's deliberate test conditions
(blanks, double marks, faint, erased and offset marks, mark styles, the
intensity sweep) are laid over that intended response unchanged, and the
intended answers are recorded per sheet (`metadata.performance`) so scoring and
recognition can be tested separately. **Fully random (legacy)** restores the
old key-independent answers exactly.

**Settings.** *Tools → Developer / Testing → Generate Synthetic Test Dataset… →
Answer keys and candidate performance* (folded by default); on the command line
`--no-solutions`, `--performance-distribution normal|random`, `--mean-correct`,
`--sd-correct`, `--min-correct`, `--max-correct`; programmatically
`generate_dataset(generate_solutions=…, performance=PerformancePolicy(…))`. One
default everywhere: solutions on, normal, mean 65 %, SD 15 %, 0–100 %.

**Isolation.** The keys (`"{seed}:answer-key:{set}"`) and the performance model
(`"{seed}:performance"`) use their own seeded streams, so switching solutions
off leaves every candidate image and ground-truth file byte-identical, and
changing the performance settings moves answers and nothing else. The generator
version is now **2.3**.

**Verified.** On a 3-set, 100-question, 500-candidate dataset (469 scripts):
OMRFlow's scorer gives every candidate exactly its target number of correct
answers (469/469); observed mean 64.4 %, SD 15.3 %, median 65 %, range
16–100 %, against a truncated-normal expectation of 64.6 % / 14.5 %; every
solution sheet read back by the recognition engine as its set code and its key.
Details and limitations: [Synthetic datasets](docs/testing/SYNTHETIC_DATA.md#answer-keys-and-solution-sheets).

**Through the Answer Key and Results stages (2026-09-29).** A generated
examination — 3 sets (`10`, `11`, `12`, marked on a two-digit set field), 24
questions, 45 candidates, seed 20260930 — was taken through the real
application window: project created, template and sets defined, the images
read on **Scan**, each set's generated workbook imported on **Attendance**,
Set 10's key **pasted from its `.txt`**, Sets 11 and 12 read with **Read From
Solution Sheet…**, all three verified, then **Calculate Results**. No file was
edited. All three verified keys equal the manifest and the `.txt`; Results
scored 45/45 (15 per set, none blocked) and each candidate's mark came from
their own set's key; the correct-answer count shown equals the ground truth's
for 44 of 45 — the exception is the deliberate *undersized mark*, which the
engine declined to read as a single answer and marked as a multiple, per the
scoring rules. Keys and results survived closing and reopening the project.
The stages were driven through the window's own methods, offscreen, with the
same files an operator would choose — not clicked through by a person.

That pass found and fixed five defects in the operator path:

- **The project's template never reached the Answer Key, Attendance, Results
  or Reports stages** — only Scan — so no key could be entered in the running
  application. The Scan stage now announces its template and the window
  relays it.
- **Results could not score a project with per-set attendance.** It read only
  the project-wide candidate list, which such a project does not have; it now
  scores every defined set's list (and a pre-sets project's single list, as
  before).
- **Results learned its candidate list only when the project opened**, so a
  list imported during the session was invisible until reopening.
- **Reading one set's solution sheet with another set selected said nothing**,
  and saving filed the key under the wrong set. It now warns, naming both.
- **The key editor kept the previous project's key** across a project change,
  one click from being saved into the new project.

**Pending.** A person working the same path by hand on a real screen, and on
real scans. Degrading solution sheets on request is not implemented — they are
always clean. A logical set code the template's set-code field cannot spell
(`10` on an `A`–`D` field) is left blank on the solution sheet and the
candidates' sheets: nothing crashes, the operator must choose the set, and such
candidates are held as *No question-paper set was read* rather than marked
against another set's key. No logical-to-physical set mapping exists.

#### Answer Key stage (Step 7): project template, set-centric workflow, solution-sheet review

**Root cause fixed.** The stage learned the template only from the Scan
stage's `template_changed` signal (the relay added in the pass above), so a
Scan load that failed, a template re-saved in place on the Template stage, or a
template loaded ad hoc on Scan all left Answer Key wrong or empty — and its
message sent the operator to Scan. It now reads the project's persisted active
template through `services.project_template.load_project_template`, which
distinguishes *no template*, *file missing*, *newer format*, *damaged*, *no
question regions* and *unusable question regions*. The Scan stage no longer
feeds it; an in-place re-save is re-read. Results also takes the project's
template on open.

**Implemented.**

- **Set-centric.** Every set Project Configuration defines (multi-digit and
  multi-character codes included) is a tile with its state in words, glyph and
  colour — Verified, Draft, Missing, Incompatible, Unsaved — and a summary
  *N of M verified*. A project without defined sets can still type a code.
- **Manual entry.** *Enter / Paste Key*; `ABCD…`, `A B C D`, `A,B,C,D` and
  line breaks read the same; a count line (*98 of 100 answers entered.
  Questions 99-100 are missing.* / *101 answers entered; the template defines
  100 questions.*); invalid symbols named with question and allowed options;
  nothing truncated or normalised away.
- **Question table** (Q / Key / Full credit / Note) edits the same model:
  double-click for a choice list, or select a row and type the option; *Full
  credit* checkboxes keep the text list in step; duplicate or out-of-range
  numbers are reported. A blank key answer is accepted only on a full-credit
  question (scoring gives it full credit before the key is consulted).
- **Solution sheets.** *Read Marked Solution Sheet…* (chooser opens in the
  project folder) reads the image with the ordinary engine and the project
  template, then opens a review dialog: sheet preview with the selected
  question outlined, per-question reading (clear / no mark / multiple marks
  B + C / low confidence), registration and set-code status. Blanks and
  multiples stay unanswered until chosen; an unregistered sheet cannot be
  imported; a sheet reading a different defined set cannot be accepted until
  the operator chooses the set; a blank set field, or a set the template's
  field cannot print (Set 10 on an A–D field, or Set B on a digit field), is a
  notice, not a block. The result is an **unsaved draft** — never a verified
  key, never a candidate script, attendance entry or scan count.
- **Revisions, provenance, verification.** Saving always creates a new draft
  revision with creator, template id/name/fingerprint, source file name,
  SHA-256 and read metadata (questions blank/multiple at read, corrected by the
  operator, set-code decision). Older revisions stay viewable without becoming
  active. Verify is enabled only for the newest saved, unedited, complete,
  template-compatible revision with a reviewer name; every blocking reason is
  listed beside the button. Editing a verified key shows *Unsaved changes* and
  saves a new unverified revision; Results keeps using the verified one and the
  stage says so.
- **Template changes.** A key whose question count, numbering or options no
  longer match the active template is shown *Incompatible*, cannot be
  verified, and is never truncated, padded or deleted; Results already refuses
  to mark with it.
- **Unsaved edits** survive leaving the stage; switching set or revision, File
  > Close/Open/New Project, recent projects and Exit offer Save / Discard /
  Cancel.
- **Results.** *Check Before Scoring* now says why a set cannot be marked (no
  key / saved but not verified / does not fit the template). Scoring still
  uses the verified key of each candidate's **effective** set code; this path
  was traced, not changed.
- **Schema migration 12** adds six provenance columns to
  `answer_key_revision`, with no backfill: older revisions show *creator not
  recorded*.

**Tested.** `tests/integration/test_answer_key_stage_services.py` (36 tests)
and `tests/gui/test_answer_key_stage.py` (24 tests) are new;
`tests/gui/test_scoring_pages.py` and `tests/gui/test_generated_key_workflow.py`
were updated where the intended behaviour changed (the review dialog, the
project-template rule, reopening restores the stored key). The generated
three-set examination still scores 45/45 with each candidate marked against
their own set's key. A scripted rendered run (1366×768 and 1100×680, real
Windows platform at 175 % scaling) on the OMR-Scan template (100 questions,
sets A–D) opened the project straight onto Answer Key, entered Set A by hand,
read Set B from a rendered solution sheet with one double mark and one blank,
verified A and D, reopened the project and found every set, source and
full-credit list restored. Full suite afterwards: **5,878 passed, 15 skipped,
0 failed**; ruff and mypy clean.

**Real solution-sheet validation (2026-09-29).** Status: **implemented /
under real-world validation.** Real scanner-produced sheets were taken through
the production solution-sheet path and the rendered review dialog (Windows,
175 % scaling, 1366×768 and 1100×680):

| Sheet | What it is | Template | Set read | Result vs. ground truth |
|---|---|---|---|---|
| `ECE-0000.png` | **Real solution sheet**, ink-filled, 2480×3508 (~300 dpi), no measurable rotation; registered with a *multiple corner candidates* warning | BUET100q | `10` ✓ | 100/100 exact, 0 flagged |
| `C3--01268` | Real candidate script used as a stand-in marked sheet | OMR-Scan | `11` ✓ | 100/100: 32 blanks and 1 double mark (Q53, B + C) all detected |
| `C8--03369` | Stand-in, rotation −0.09°, skew −0.16° | OMR-Scan | `16` ✓ | 100/100: 11 blanks detected |
| `C1--00023` | Stand-in | OMR-Scan | `12` ✓ | 100/100: 18 blanks detected |
| `C5--02293` | Stand-in, rotation −0.14° | OMR-Scan | `13` ✓ | 100/100, 0 flagged |

500 questions in all: **0 false confident reads**, 61 of 61 blanks and 1 of 1
double mark detected, nothing clear flagged as ambiguous. **Ground truth is a
visual transcription of the rectified pages made from crops without the
engine's overlay — not an examiner's key**, none being available.

In the real window: ECE-0000 was read into Set 10, saved, verified and
restored on reopening; C3's unresolved questions kept Save and Verify
disabled; C8 (reads Set 16) with Set 12 selected could not be accepted until a
set was chosen. The dialog highlighted the right bubbles for every question
checked (Q89 and Q97 on ECE-0000, Q2 and Q53 on C3).

**Defect found and fixed:** the review dialog opened with its preview at 1:1 on
the page's top-left corner (marker and name box) instead of on the answers. It
now opens on the first question needing review, or fits the whole page when
nothing needs review. Regression tests: `tests/gui/test_answer_key_stage.py`
(fail without the fix), `tests/integration/test_real_solution_sheet.py`
(ECE-0000, committed sample) and `tests/local/test_real_marked_sheets.py`
(the four scans in git-ignored `Scratch/`, skipped elsewhere).

**Not covered by any real sample:** a second real solution sheet; real
solution sheets with a blank or double mark (only candidate stand-ins); strong
skew (the samples are within ±0.23°); noisy or photographed scans; a blank or
unreadable real set field. Step 7 is therefore **not** called validated.

**Same-path template refresh — fixed.** Scan identified its template by path
alone and the main window returned early when a template was re-saved to the
same path, so Scan (and Results, fed from it) kept the old geometry and
question count; Answer Key was already immune. When the same path is
re-announced the window now compares the file's contents with Scan's copy and
reloads only if they differ and no batch is running. Tests cover the reload,
the unchanged re-announce, and the running-batch case.

**Logical vs. physical set codes — design only, not implemented.** No mapping
exists, active or dormant: every stage compares one bare string. Recommended:
an optional per-set `physical_mark` on `project_set` (migration 13; NULL =
printed as its own code, so existing projects are unchanged), validated
against the template's set field, and translated **once**, where the effective
set code is derived for a scan (`review_store.effective_set_codes` and the
undefined-set check), so Resolve, Attendance, Answer Key, Results and Reports
all see logical codes while `batch_scan.set_code_value` keeps the raw reading
for audit. The solution-sheet set check and the synthetic generator would use
the same mapping. **Also found:** set-code case handling differs between stages
(registry, Resolve, Attendance and Reports compare exactly; scoring
upper-cases before the key lookup) — harmless for upper-case codes, a latent
fault for a lower-case one; recorded as a follow-up, not changed.

**Verification defect found by an operator and fixed (2026-09-29).** Clicking
*Verify Answer Key* and then **Yes** in the real confirmation left the key a
draft. Cause: PySide6 6.11 returns the clicked button from the real
`QMessageBox.question` as a plain `int` (`16384`), equal to but never *the
same object as* `StandardButton.Yes`, and `verify_key` compared by identity
(`is not`). Every existing test replaced the dialog with a function returning
the enum member, so none could see it. The same identity comparison was also
silently refusing six other real confirmations — replacing an attendance
candidate list, replacing the sample attendance template file, and four
stress-campaign prompts (continue / stop safely / force kill / resume); all
now compare by value. Verification also re-reads the stored revision and
announces success only if it is persisted as verified, and reports and logs a
failure. New tests: `tests/gui/test_answer_key_real_verify.py` clicks the real
button and the real dialog's Yes/No (no mocking of `QMessageBox.question`;
confirmed to fail on the old code), and
`tests/unit/test_qt_dialog_result_comparisons.py` keeps identity comparisons of
dialog results out of `src/`. Rendered acceptance at 1366×768, reviewer
*Robin*, sets 10/11/12, the real ECE-0000 sheet: Draft → Verify → Yes →
*Verified*, *1 of 3 verified*, Results shows `10 rev 1`, and it stays verified
after reopening.

**Results: *Calculate Results* failed from the real button — fixed
(2026-09-30).** Clicking *Calculate Results* in a project with three verified
keys reported *Scoring could not complete. ('bool' object is not iterable)*.
`QPushButton.clicked` emits a `checked` bool; the button was connected straight
to `ResultsPage.score_batch(candidates=None)`, so a click became
`score_batch(False)`, `False` travelled through `ScoringWorker` into
`scoring_store.score_batch(candidates=False)`, and `set(False)` raised. Every
test called `score_batch()` directly, never through the button. Now the button
has its own handler, `candidates` is keyword-only, *Recalculate This Candidate*
ignores the bool, and an unexpected scoring failure is logged with its full
traceback and batch/roster context while the dialog names only the exception
type. Audited every `clicked`/`toggled`/`triggered` connection in `src/…/gui`:
no other slot takes the bool as something else. Tests now click the real
buttons (`TestTheRealButtons` in `tests/gui/test_scoring_pages.py`; the
three-set generated workflow uses the real button) and were confirmed to fail
on the old code. Rendered acceptance (1366×768, sets 10/11/12, real button
clicks): *Check Before Scoring* → *Every candidate can be marked*; *Calculate
Results* → 12/12 scored, 4 per set, key revision 1, no error dialog; the same
after reopening. Scripted, not an operator session.

**Resolve: interface froze ("Python is not responding") — investigated
(2026-09-30).** Reported on the Resolve stage, on the Student ID position-1
conflict of `SYN_000033` right after a full Student ID edit on another sheet.
Windows recorded an *Application Hang* (event 1002: "stopped interacting with
Windows and was closed"); the OMRFlow logs simply stop, with no exception —
a GUI-thread freeze, not a crash. **The exact trigger could not be reproduced**
on a copy of the project (every open conflict, sheet row, value choice,
confirmation and full-field edit replayed in the real window, maximised, with
the old code). Two changes result:

- **A real freeze in the same pane was found and fixed.** The zoomed preview
  (`gui/scan/preview.py`) refits its framed region on every resize, sized
  against the *current* viewport. For a region at the page's left edge (Student
  ID position 1) in a wide, short pane, one zoom showed a scroll bar and the
  narrower viewport then gave a zoom that hid it; each refit's resize triggered
  the other, forever. A sweep reproduced it in 46 of 1,386 pane-size/region
  combinations; with the fit sized against the scroll-bar-independent
  `maximumViewportSize()` and a re-entrancy guard, in none.
  `tests/gui/test_preview_refit_stability.py` fails on the old code. Whether
  this is what froze the reported session is **not established**.
- **Freezes now leave evidence.** `gui/hang_watchdog.py`: if the event loop is
  silent for 10 s, every thread's Python stack is written to
  `omrflow-hang-<time>.txt` in the application log folder
  (`%LOCALAPPDATA%\OMRFlow\logs`) and the path is logged. No candidate data is
  written; it observes and never interrupts.

**Pending / limitations.** No operator has used the stage beyond the reports
above. Verification remains a prerequisite for scoring, as before. With many sets the tiles scroll
horizontally (checked with fifteen).

#### Written Student ID and set code, and used reference forms cleaned first

A real script carries its identity twice: **written** in a row of boxes, then
**bubbled** underneath. Synthetic sheets now do the same for both the Student
ID and the set code, because the written value is what an operator reads to
resolve a wrongly bubbled, double-marked or blank field. And because a
"blank" reference form is often a used script, its old writing *and* its old
bubble marks are removed before any sheet is drawn on it.

**Implemented.**

- **The intended value is written, never the bubbles' reading.** Student ID:
  the case's `intended_roll` — the registered roll, or an *unknown candidate*
  script's own number; nothing on a solution sheet. A sheet bubbled
  `13000019`, `1300?016` or `________` still shows `13000016`; leading zeros
  kept. Set code: the case's new `intended_set` — the paper the candidate is
  registered for, so *wrong set* and *blank set* scripts still show it; a
  solution sheet shows its own set. Recorded as `metadata.written_student_id`
  and `metadata.written_set_code`; bubbles, expected readings and every other
  truth field are unchanged, and the recognition engine still reads only the
  bubbles.
- **Set codes as the template spells them.** A field with one position whose
  bubbles are whole codes (`10`, `11`, `12`) has one box and the whole code
  goes in it; a positional field has one box per position (`05` → `0`, `5`).
  A code the field cannot spell (`10` on an `A`–`D` field) is not written.
- **Where the boxes are.** Derived from each grid the template already
  declares — one box per character position, sized in grid pitches — so no
  template field was added and any length works. A template-rendered page
  prints the boxes; on a reference scan they are located on the scan, and a row
  the form does not print gets nothing written in it.
- **A used reference is cleaned once per run, in this order**, inside
  `load_reference_scan`:
  1. *Write-in boxes* — handwriting removed; a printed border a stroke covered
     is rebuilt from its own appearance further along the line.
  2. *Bubbles* — every bubble the template declares (Student ID, set code and
     answers) is compared with a **clean copy of the same printed label** on
     the same form, and only the pixels darker than that copy are replaced by
     it, aligned and matched to the local paper colour. Ring, printed label
     and paper grain therefore come back from the scan itself, not as a white
     disc; unfilled bubbles are left byte-for-byte as scanned, and a genuinely
     blank form comes out unchanged. Overflow beside the ring is filled along
     any table or frame line it crossed. If a label has no unfilled copy
     anywhere, the fill is still removed but the label is lost — and counted.

  Then, for every sheet: the intended Student ID and set code are written and
  the synthetic bubbles drawn (one mark layer), folds applied, the page
  degraded (blur, noise, JPEG…), and the colour mode applied last — so the new
  writing and marks are degraded exactly as if they had been on the paper
  before scanning. The manifest's `reference.write_in` and `reference.bubbles`
  record what was found and removed; generator version **2.5**.

**On the repository's own used sample** (`Sample-Project/1.Template/ECE-0000.png`
— a filled solution sheet: all 100 answers, every Student-ID `0`, both set-code
positions, and `00000000` / `10` handwritten): cleanup removed exactly the
**110** filled bubbles and touched **none** of the 390 unfilled ones, in colour
and in grayscale, with no label lost, measured against an independent
filled/unfilled split. Sheets generated on it and read back by OMRFlow's own
engine gave the synthetic candidate's Student ID, set code and **100/100**
answers — a normal sheet in colour, grayscale and black-and-white; the wrong-ID
and wrong-set anomalies in colour (each read as its *bubbled* value, as
intended); the blank-bubble sheet 100/100 answers, its blank identity fields
read as unresolved (see limitations). With cleaning turned off, the same test
reads the old marks — which is what these sheets carried until now.

**Fixed while completing it (2026-09-29).** The first Student-ID
implementation failed its own stale-text test (12 of 15 borders "found"): the
misses were not handwriting over the Student-ID borders but the set-code row,
which the generator never printed boxes for — one of its borders had been
"found" by borrowing the Student-ID row's edge, which would then have licensed
erasing whatever is printed there. Checking the real sample found three more
defects the clean synthetic fixture could not show: on a blurred/JPEG scan the
rule for strokes crossing a border **faded every black border to grey**; a
stroke touching a border **widened it into the box**; inpainting beside a pink
border **left pink blots**. Developing the bubble cleanup on the same form found
that print weight varies across one page (a heavier-printed unfilled bubble
must not look filled), that registration drifts up to ~4 px across it, and that
a fill can cover a printed frame line — each now handled and covered by a test.

**Testing status.** `test_written_student_id.py` 44 passed;
`test_written_set_code.py` (new) 22 passed; `test_reference_bubble_cleanup.py`
(new) 17 passed; the synthetic-generation and evaluation suites 645 passed; 0
failed, skipped or xfailed in any of them; ruff and mypy clean. Full suite
after the Student-ID fix: **5778 passed, 15 skipped, 0 failed**. Full suite
after the set-code and bubble-cleanup work: **5817 passed, 15 skipped, 0 failed**.

**Visual validation.** Inspected by eye, not measured: template-rendered
normal, wrong-ID, double-marked, blank-bubble and leading-zero sheets; the used
sample cleaned and regenerated with a different candidate in colour, grayscale
and black-and-white, for a normal, wrong-ID, wrong-set and blank-bubble sheet,
and its answer area. Not yet used in a real session.

**Known limitations (accepted, non-blocking).**

- Handwriting *outside* a write-in box's border band is not removed.
- Faint traces can remain: the lightest edge pixels of an old stroke, and a
  few pixels of a fill that overflowed further than its exemplar reaches (on
  the sample, a dot beside two Student-ID `0`s and one beside question 45's
  `a`).
- A stroke merged with a *black* printed border cannot be told from it and is
  left on the line.
- A bubble whose label has no unfilled copy on the form loses its printed
  label (counted in the manifest); a fill in such a bubble is only removed if
  it is unmistakable.
- Not arbitrary form restoration: other handwriting on a used form (a name, a
  signature, "Solution" on the sample) is not touched.
- The written characters are a synthetic font, not handwriting, and only a
  field whose characters run in columns gets write-in boxes.
- Observed, not changed (outside this work): on the sample's pink form the
  engine reads an *entirely blank* Student-ID or set-code column as unresolved
  (`?`) rather than blank (`_`) — its printed labels measure as partial fill.

**Pending.** Use in a real session; a dataset generated from a genuinely
blank scan and from a used one compared end to end.

#### Attendance dispositions: duplicate scripts, unwanted scans, Defer

**Status: implemented — automated tests passing; scripted acceptance in the
real window passed; not yet validated by an operator on a real examination.**

*The gap.* Attendance could identify a duplicate or an unrecognised sheet but
offered no obvious way to dispose of it. *Set Script Aside* was a
reconciliation-only decision: it did not reach Resolve's duplicate-ID
detection, and it could not clear an *Unknown candidate ID* exception, so an
accidental or wrong-exam scan could only be "fixed" by inventing a candidate
ID or accepting the exception as-is.

*What changed.* The disposition controls live in the right-hand panel and are
context-sensitive:

* **Keep This Script** — a duplicate row shows *Script 1 of N* with Previous /
  Next, and each copy's file, batch, scan number, recognised and effective
  Student ID and set, and disposition; selecting a copy shows its scan. Keeping
  one rejects / excludes every other copy in one transaction, after a
  confirmation that names each copy. Three or more copies work the same way.
* **Reject / Exclude…** — with a reason (wrong page / document, accidental
  scan, duplicate, blank / unusable, other + note). The sheet leaves
  reconciliation, duplicate detection, scoring, Results and exports; it is
  kept, counted under **Rejected**, and listed in *Rejected / excluded sheets*.
* **Defer** — kept, marked *Deferred*, counted, not scored while deferred. The
  operator may carry on; Attendance and Results say *N sheet(s) are deferred
  and will not be included in scoring or results*, and a final export needs the
  audited *Export incomplete results* acknowledgement. A registered candidate
  whose only script is deferred reads *Script deferred — decision postponed*,
  not *Missing script*.
* **Restore** / **Undo Last Disposition** — back to active review exactly as it
  was; a restored duplicate is reported again.

*How.* The Reject & Rescan lifecycle (`scan_rejection`) gained two states,
`excluded` and `deferred` — no new table and no migration — so the existing
"only `active` is result-eligible" rule covers them everywhere. Every
disposition is committed and re-reconciled before the call returns, and is
recorded in the append-only audit ledger (`excluded`, `deferred`, `restored`,
`disposition_changed`, `kept_canonical`). A confirmed rescan cannot be
excluded or deferred while its link stands; excluded / deferred sheets never
seed exact-re-import detection. Results no longer lists a *blocked* result
whose entry no longer exists (such a row is removed on the next full
calculation; a result with a mark is never removed). Details:
[reconciliation](docs/reconciliation.md) § *Sheet dispositions*.

*Testing performed.* 35 new service-level integration tests (`tests/integration/test_attendance_dispositions.py`) (duplicates of
two and three copies, keep either copy, all-or-nothing refusal, scoring and
report exclusion, counts, restore re-raising the duplicate, identical-bytes
duplicates, unknown ID rejected and deferred, deferred registered candidate,
persistence across reopen, Reject & Rescan including cross-batch
replacements); 16 new GUI tests (`tests/gui/test_attendance_disposition_controls.py`: both scans exposed, preview follows the
selection and the keyboard, confirmation text, reject / defer / restore /
undo, rejected and deferred views and counts, reopen, controls reachable and
unclipped at 1366×768 and 1100×680). `scripts/acceptance_attendance_dispositions.py`
drives the real `MainWindow` on the platform display with real rendered and
recognised sheets (10000049 scanned twice, stray 10000009, one deferred
candidate): 24/24 checks passed, including scoring from the kept copy, the
stray sheet absent from Results, the deferred warning on Results, and every
disposition intact after closing and reopening the project.

*Still under testing / limitations.* Not yet used by an operator on a real
cohort. Undo Last Disposition covers the last action of the current session
only (Restore works at any time). A non-duplicate row does not open its scan
automatically (press *Inspect*). A sheet's Resolve conflicts leave the Resolve
queue while it is excluded or deferred (they return on Restore). Existing
*Set Script Aside* decisions are honoured but no longer created.

#### Reject & Rescan

A scan can be unusable even when each of its conflicts could in principle be
corrected - folded through the answer grid, clipped, skewed, the wrong page.
It can now be **rejected pending a rescan**, and replaced.

**On Resolve.** *Reject / Rescan…* (toolbar, or `R`; never while typing) opens
a compact dialog naming the scan and its current Student ID and set code, with
a reason (folded, poor quality, registration, clipped, skew, wrong document,
Student ID unreadable, other + note), an optional note, and an **optional**
Student ID / set as read off the paper. Identity is not required - a sheet
with no readable roll number can be rejected. Cancel is the default button.
The sheet leaves the working queue at once and, with auto-advance on, the next
unresolved conflict is selected. Its conflicts are **kept, not withdrawn**, and
come back exactly as they were on *Undo Reject*.

**Rejected / Rescan** is a view of the ordinary status filter, so the same
queue, search and Ctrl+Up / Ctrl+Down apply (walking cases still awaiting a
rescan, passing over completed ones, wrapping with a note). Each row says its
state in words and a glyph - `✖ REJECTED — RESCAN REQUIRED`, `⇄ Rejected —
replaced by rescan: IMG_…` - never colour alone; a banner above the image says
the scan on screen does not count. The case panel (two columns, so every
control fits at 1366×768) lists what is recorded - file, Student ID, set,
reason, who and when, replacement, image state - and offers *Import rescan…*,
**Possible rescan: … · Student ID … · original …** candidates with *Use as
replacement*, *Choose from all scans…* (project-wide, searched) for an unknown-identity case,
*Undo Reject*, *Remove replacement link…* (asks first), *Show replacement /
original* to compare, and *History…*. The summary under the queue counts
*N rescan required* beside the conflicts.

**Rescans.** *Import rescan…* reads the file into **the rejected sheet's own
batch** through the Scan stage, like any scan; a rescan read in **any other
batch of the project** - another day, another scanner - is found just the same
(see *Follow-up hardening* below). (A defect fixed on the way: a
file added to a batch that had already been registered was read but silently
never stored - `batch_store.add_scans_to_batch` now registers it.) Candidates
are suggested only by an **effective** Student ID that is fully read and equals
the case's identity; set code is extra evidence; **file names are never
compared**. Nothing is ever linked automatically: before confirmation the
rescan is an ordinary active scan and the case stays outstanding.

**Lifecycle, in the service layer** (`services/scan_lifecycle.py`, table
`scan_rejection`, migration 11): `active → rejected_pending_rescan →
superseded_by_replacement`, plus `reimport_of_rejected`; the image's
`file_state` (`present / quarantined / purged`) is a separate column. Only
`active` is result-eligible. Enforced where it matters: reconciliation places a
rejected script under its candidate as **Script received but rejected — rescan
required** and never counts it, drops superseded originals, and re-reads
lifecycle when stored entries are read; scoring drops ineligible scans'
answers; a mark made from a scan that is later rejected or replaced is
reported stale; duplicate-ID detection ignores ineligible scans; the review
queue and counts hide them; the recognition CSV leaves them out. Undo Reject
is refused while a replacement is linked - remove the link first, which
returns the original to *awaiting rescan*, never to active; undoing then makes
two active scans with one ID, which the ordinary duplicate detection raises
again on both sheets and scoring refuses to resolve silently. An exact re-import of a rejected scan's
bytes (content hash) is linked back to it as a re-import: never counted, never
offered as a rescan, never able to resurrect it. Every transition is an
append-only audit event (`entity_type='scan_lifecycle'`).

**Export.** Results stay viewable, and Results says *may be incomplete: N
rejected sheet(s) still awaiting rescan*. A **final** export of a set with an
outstanding rescan (including a rejected sheet of unknown identity or set) is
blocked by a new readiness issue that - alone among blocking issues - can be
acknowledged: Reports asks, *Export incomplete results* (Cancel default), the
workbook's Processing Log opens with `INCOMPLETE RESULTS … acknowledged by …`,
and the acknowledgement is audited (`entity_type='results_export'`). Any other
blocking issue still blocks.

**Purge Rejects** (Tools menu) lists superseded originals whose replacement is
still active - never a sheet awaiting rescan - with sizes, and moves their
images to `<project>/quarantine/` (recommended) or deletes them. Only files
inside the project's own `scans_original` / `scans_aligned` / `quarantine`
folders are touched, after rejecting relative paths, `..`, symbolic links and
junctions, paths resolving elsewhere, files another scan still references and
files whose content hash changed since import. Scans read in place from a
scanner share or another folder are **never** removed - they are listed as
left in place. The database row, the rejection, the replacement link and the
history are kept; Project Health no longer reports a purged image as a missing
source.

**Terminology.** Attendance now distinguishes *Missing script* (nothing
matched), **Script found — set unresolved** (a script with exactly this ID
exists but its set code is unsettled) and **Script received but rejected —
rescan required**, each with its own word, glyph, filter and explanation. The
suggestion matcher is unchanged; "already has a valid script" now excludes
rejected and superseded scans and includes a confirmed replacement.

**Testing.** 95 new tests: 57 service-level integration tests on a real
project (every lifecycle transition and refusal, audit fields, persistence
across reopening, reprocessing and resume, scoring and staleness, the
acknowledged / refused / unacknowledgeable final export and its workbook,
declared and unknown identities, candidates and file-name independence,
re-imports and hash provenance, genuine duplicates kept, Undo before and after
replacement, purge eligibility, quarantine, delete, external / shared / changed
files, `..`, junction and symlink paths, Project Health, migration from schema
10 and re-running migration 11), 20 unit tests of the pure rules, and 17 GUI
tests on real rendered sheets (reject, the view and banner, `R` and not while
typing, the dialog, reviewer required, **import a rescan through the Scan
stage → offered → confirmed → compared → CSV excludes the original**, undo,
navigation, 1366×768 and 1024×640 layouts, the Reports question, the window's
Purge action); the combined three-set scenario was extended with the
Reject & Rescan steps and a close/reopen. Two existing tests were updated, both
for intended changes: one set-scoping test now expects *Script found — set
unresolved* where it expected *Missing script* (its lead assertions are
unchanged), and the placement-enumeration test lists the three new placements
it was written to anticipate. **Full suite: 5,574 passed, 15 skipped, 0 failed** (local run of 2026-09-28, 30 min; 4 `stress` tests deselected by default; each of the 15 skips states its reason - 10 are local real-scan fixtures absent from the machine, 1 is the new symlink test, which needs a Windows privilege the machine lacks; its junction counterpart ran and passed). The three presentation fixes below were made after that run; the five affected GUI files were re-run afterwards: 351 passed, 1 skipped. `ruff` and `mypy` clean.

**Acceptance, scripted in the real window** (1366×768, a two-set project,
three rendered sheets, driving the window's own commands; screenshots
inspected): a sheet was rejected and left the queue, read *REJECTED — RESCAN
REQUIRED* in the view and banner, reconciled as *Script received but rejected*
with 0 counted scripts, and was still rejected after closing and restarting the
window; a rendered rescan imported through *Import rescan* was read, offered as
*Possible rescan … set code matches* and not linked, then confirmed; the
candidate became *Matched* with only the rescan counted and scored from the
rescan (17 correct, its answers); the Resolve count fell to 0; Purge Rejects
listed only the original and quarantined it while the rescan stayed; the
history read *Rejected → Replaced → Image moved to quarantine*; all of it held
after reopening. The screenshots found two presentation defects, both fixed:
the case panel's actions were below the fold at 1366×768, and a stale
navigation note carried into the view; a rejected script's declared identity
also read as a correction on Attendance and now reads *(rejected; case …)*.
**Not yet worked by an operator, and not on real damaged sheets.**

**Follow-up hardening.** Three limits of the first version are removed. No
schema change was needed (still schema 11): the replacement link was always
scan-to-scan.

* **A rescan from another batch.** Suggestions are drawn from the whole project
  by effective Student ID, with set code as supporting evidence; file name,
  folder, scanner and batch are never evidence. A candidate from another batch
  says so - *… · batch 1a2b3c4d (2026-09-28)* - and the confirmed case reads
  *SCN_… (read in batch 1a2b3c4d)*. *Choose from all scans…* searches the whole
  project in SQL (file name or Student ID as read, on Enter, capped at 200). All
  refusals stand: the rescan must be active, not the original, not already a
  replacement, not an exact re-import of rejected bytes (in any batch), and in
  this project. Reconciliation, scoring and reports stay per batch: a confirmed
  cross-batch replacement **stands in for its original in the original's
  batch**, and in its own batch is placed *counted elsewhere* - so it is
  counted exactly once. Each audit event is recorded under its own scan's batch.
* **Rejecting a sheet with no conflict.** Resolve's status filter has an
  **All processed sheets** view: every read sheet of the batch, one SQL page at
  a time, searchable, loading only the selected sheet's image. The sheet panel
  shows the file, its effective Student ID and set, and its lifecycle state,
  with the same *Reject / Rescan…* action and dialog. The view counts nothing
  as unresolved and Ctrl+Up / Ctrl+Down do not walk it (a note says so).
* **Duplicates after Undo Reject or unlinking.** Every lifecycle change ends in
  the canonical duplicate pass for each batch concerned. A duplicate-ID record
  the machine had *withdrawn* because its partner stopped counting is
  re-opened by a new machine event, *Detected again*, when both sheets count
  once more; a record a person had decided is never re-opened by the machine.
  Resolve, Attendance (*Duplicate scripts*) and scoring (*blocked*) agree, and
  running the pass again changes nothing.

**Which batch a replacement belongs to.** Physically, a rescan belongs to
the batch it was read in: its recognition result, its review records and any
correction made to it are stored there - including a correction made from the
original's Attendance, whose inspector files it under the rescan's own batch.
Academically it belongs to its original's batch: that is where it is
reconciled, scored and exported. No report combines batches, so a student
has one mark - from the replacement, in the original's batch - and the
replacement's own batch never scores it.

**An original whose image is gone.** Once *Purge Rejects* has quarantined or
deleted a superseded original, *Undo Reject* is not offered, *Remove
replacement link…* is disabled, and the case panel says why: *Original image
is no longer available (…); the rejection cannot be undone and the
replacement link is kept.* The service refuses both actions with the same
sentence whatever calls it, and the replacement is untouched.

Tests: 43 new - 31 service-level (14 cross-batch, 3 exactly-once counting
and export, 3 processed-sheet, 8 duplicate-reconstruction, 3 image-gone Undo
safety), 11 GUI (5 on the new view, 2 cross-batch, 3 on the image-gone case
panel, 1 correcting a cross-batch replacement from Attendance and reopening),
and the three-set scenario extended with Batch A → reject → reopen → Batch B
rescan → offered → confirmed → counted once → results, audit, purge → unlink
→ Undo → reopen. Two existing tests were updated for intended changes (the
same-batch refusal test now checks an unread scan of another batch is still
refused; the placement enumeration lists *counted elsewhere*). **Full suite:
5,617 passed, 15 skipped, 0 failed** (local run of 2026-09-29, 31 min 37 s; 4
`stress` tests deselected by default; the same 15 skips as before). `ruff` and
`mypy` clean.

Scripted in the real window at 1366×768, screenshots inspected: a clean sheet
rejected from *All processed sheets* (conflict counts unchanged, Ctrl+Down
declined with its note); a duplicate pair withdrawn by rejecting one sheet and
both re-opened by Undo Reject, agreeing on Attendance and in scoring; a rescan
read in a fresh window as a new batch, offered with its batch, confirmed,
counted and scored only in the original's batch, and still linked after
reopening. The screenshots found one defect, fixed: the candidate line was cut
off before its batch at 1366 px (it now wraps). A second scripted run: a
rescan with two blank roll positions, read as a new batch, linked by hand
through *Choose from all scans…*, then corrected to its student's ID from the
original batch's Attendance inspector - the correction was stored in the
rescan's batch only, the candidate became *Matched* by the rescan, and both
held after reopening; after a quarantine the case panel offered no Undo,
disabled the unlink, and gave the reason.

**Known limits.** A quarantined image can be restored only by hand, and an
original whose image was quarantined or purged cannot be un-rejected. The
Resolve stage's dividers are still not remembered. Not yet worked by an
operator on real damaged sheets.

#### Attendance as a reconciliation workstation

Step 6 answers, for one set at a time: who was expected, whose script exists,
which records contradict one another, and - new here - what the paper itself
says.

**States.** *Matched*, *Missing script* (expected present, no script),
*Absent but script found* (a contradiction, never taken as proof of
attendance), *Unknown candidate ID*, *Student ID not yet resolved*, *Duplicate
scripts* and *Absent, confirmed* - each a word plus a symbol, never colour
alone. The counts are a row of chips that filter the table.

**Investigating.** Selecting an exception states the problem in labelled
sentences and lists its scripts and **where to look**: for a missing script,
the unread, unknown, duplicated or absent-filed scripts whose ID is within two
digits; for an absent candidate's script, the candidates expected present with
no script and a similar ID. These are suggestions only - nothing is reassigned
by similarity, and attendance never changes a scan or vice versa.

**Inspecting and correcting.** *Inspect / Correct Script* (or Enter) opens the
**original scan** and the sheet as read, inside the Attendance pane, and takes
the **complete** Student ID or set code. It is the Resolve stage's machinery,
not a copy: the field-edit logic now lives in one shared service
(`services/field_edit.py`) that both stages call, the inspector reuses
Resolve's sheet loader, image view, decision lanes and override warning, and
every correction is written by `review_store.correct_field` - machine value
kept, operator value effective for Answer Key, Results and Reports, the reason
(a new *Candidate entered a wrong roll number / ID* reason exists for this
case) and the Attendance context recorded in the audit ledger, one-step undo.
Reconciliation re-runs at once, the selection stays in place, and the table
says where the script went.

**Choose / Replace Attendance File.** Root cause: when a set's `.xlsx`
attendance file was replaced by one that cannot be a result template (a CSV,
or a workbook without a marks column), the **previous workbook stayed
associated as the set's result template** - so the table's *Result template*
column went on showing the old file's name, and Reports would have built the
result on the superseded list. The replacement's blocker message also
overwrote the line naming the new file, and rosters stored only a file name,
so a same-named file from another folder was indistinguishable. Fixed: an
auto-adopted (`attendance`) template is released when its attendance file is
superseded (a template chosen on Reports is kept); migration 10 records the
file's full path, shown as a tooltip; the template is reported on its own line.
The *Result template* column was removed from the set table - it looked like a
second attendance file - and the template is shown in the set's detail line
and Status tooltip.

**Layout.** No group box around every block: a compact set table (scrolls
beyond four sets), one status line, the chips, then a resizable splitter with
the exception table as the main area and a single scrollable detail pane, so
every control needed to finish a correction is reachable at 1366×768
(page minimum 733×322). Ctrl+F searches ID, name or recognised ID.

**Testing.** 45 new tests: 27 Attendance-investigation GUI tests on real rendered scans (states, suggestions, original-scan inspection, full-ID correction with and without an override, cancel, undo, audit contents, selection kept, persistence across reopening, search by recognised ID, chips, Enter and Ctrl+F, every control reachable at 1920×1080 / 1600×900 / 1366×768, set isolation, set switching), 7 Choose / Replace regression tests, and 11 unit tests for the suggestions. Eight existing Attendance tests were updated for the new wording and layout; every existing Resolve-stage test passes unchanged over the shared field-edit service. Manual acceptance, driven in a rendered harness on
a synthetic Set 10 of 100 candidates (95 present, 5 absent) with 95 real
rendered scans: Reconcile gave Matched 93 · Missing script 2 · Absent + script
1 · Unrecognised 1 · Duplicates 0; the absent candidate's script was
inspected on its original scan, its full ID corrected 170596 → 170594 after
the override warning; it moved to 170594 (Matched) and 170596 became Absent,
confirmed; the ledger kept machine `6`, new `4`, the reviewer, the reason and
the Attendance context; the incomplete ID was completed; every required
control was reachable at 1366×768; a replacement attendance file and the
correction both survived closing and reopening the project. **Not yet worked
by an operator on real sheets.**

**Remaining limits** at the time; three of the four were addressed in the
follow-up below.

**Follow-up: set-scoped reconciliation, better suggestions, navigation, a
remembered divider.**

- *Set-scoped reconciliation - fixed.* A set now reconciles only the scripts
  whose **effective** set code is its own; the rule lives in the service
  (`ScriptSetPlacement`), not the GUI. Another set's scripts are outside it
  and never unknown IDs. A script whose set code is unresolved stays on the
  Resolve stage as before; one whose code was read but names no defined set
  now raises a *Set code not a defined set* conflict there
  (`sync_undefined_set_codes`, after every batch and before every
  reconciliation). The Attendance summary says how many scripts the set left
  out and why, and *Missing script* suggestions include those scripts.
- *Suggestions - fixed.* A conservative edit-distance matcher on exact ID
  strings: one wrong, missing or extra digit, up to two wrong digits at the
  same length, unread positions matching anything, the roster as the candidate
  space, at most five, ties shown as *equally close*. The rule is in
  `docs/reconciliation.md`.
- *Next / previous unresolved - fixed.* Ctrl+Down / Ctrl+Up and two buttons
  on the Attendance stage; the same keys added to the Resolve stage's existing
  commands (which keep Ctrl+Enter / Shift+Enter). Both stay within the current
  filter, pass over rows already dealt with, and wrap at the end - the Resolve
  stage's existing convention - with a note saying so.
- *Divider - fixed for the Attendance stage.* Remembered in the ordinary
  application configuration (like the ribbon density), restored on the next
  start, clamped to 30-80 % and never allowed to squeeze the detail pane below
  its minimum. The Resolve stage's own dividers are still not remembered.
- *Reject & Rescan* - not implemented at the time; since implemented, see
  **Reject & Rescan** below, which also completes the combined scenario.

Testing: 53 new tests - 16 set-scoping tests over one batch of three sets (each set sees only its own scripts, unresolved / overridden / undefined set codes, idempotent sync, withdrawal when the set is added, the unscoped legacy path), 13 more suggestion tests (substitution, one missing digit, one extra digit, leading zeros, unrelated IDs, two equally close IDs shown as tied, a short list, out-of-set scripts), 17 navigation and divider tests (Ctrl+Down / Ctrl+Up on both stages, filtered walks, announced wrap, nothing-left, config round trip, clamping, restore through the window), and the 7-test combined three-set scenario including close-and-reopen. Three existing investigation tests were updated: two for set scoping (their sets now carry the sheets' set code) and one for the new suggestion wording. Full suite 5,491 passed, 3 skipped; `ruff` and `mypy` clean.

#### Overriding a confidently read position

The whole-field editor used to refuse a value that disagreed with a position
the engine had read **confidently**: with no conflict at that position there
was nowhere to record a decision. That made it unable to fix the error that
matters most — a clean `9` that the paper shows is an `8` — so `100029`
could not be corrected to `100028`.

The explicit **Edit full Student ID / Question Set** action is now a
human-authoritative field correction. The typed value is compared with the
current field position by position, and each difference is one of: an
unresolved conflict, an earlier manual decision, or a **confident machine
reading with no conflict**. All three can be changed. Nothing else is:
recognition thresholds, detection policy and the single-position controls are
exactly as they were, and no conflict is raised for any position nobody edits.

- **Preview.** The editor shows `100029 → 100028`, the changed positions, and —
  in the warning colour — `Position 6 — confident machine read (9 → 8)`. Apply
  reads **Apply override…** when an override is involved.
- **Confirmation, only when needed.** A *Manual field override* dialog lists
  each confident position with its machine and entered values; **Cancel** is
  the default and changes nothing — no event, no record, the editor stays open
  with the value. Changing only disputed positions shows no dialog.
- **Stored honestly.** An overridden position is recorded on a new
  `manual_override` record ("Operator field override"), **never on a fabricated
  detection**: it has no `DETECTED` event, its machine observation is copied
  verbatim from the re-read, and its correction event carries both an
  `[override]` marker and the edit's shared action token — reviewer, time,
  sheet, field, position, machine value, new value, reason and note, all in the
  existing append-only ledger. No schema migration.
- **Machine / manual / effective.** The machine value stays `9`; the manual and
  effective values become `8`; the effective Student ID becomes `100028`; the
  lane overlay shows it as a manual decision like any other.
- **Undo.** One `Ctrl+Z` reverses the whole edit — disputed and confident
  positions together. A reversed override rests as withdrawn, so the machine's
  `9` stands again and nothing new appears in the working queue. A position
  somebody has decided again since is still not rolled back.
- **History.** Opens with "Not raised by recognition", headings the event
  *Explicit field override*, and shows machine value and source on every
  decision line. Reopening an override restores the machine reading without
  erasing it. Overrides cannot be deferred — there is no open question to put
  off.
- **Downstream.** The effective Student ID feeds reconciliation, so a changed
  ID is re-evaluated for duplicates there (tested: overriding a sheet to
  another sheet's ID raises `DUPLICATE_SCRIPT`; undo clears it).

**Testing.** 21 review-store tests and 18 Resolve-stage GUI tests added for
this, covering: disputed-only edits (no override record, no dialog); a single
confident override; disputed and confident together under one token and one
reason; the preview; the dialog appearing only when needed, defaulting to
Cancel, and its text; Cancel leaving the ledger size and conflict counts
unchanged; machine/manual/effective values; no fabricated `DETECTED`; the
audit fields and marker; grouped undo; later decisions protected; a redo still
marked as an override; re-overriding after undo reusing the record; a re-read
not withdrawing a standing override; defer refused; reopen; the overlay;
same-sheet navigation; the multi-position, multi-character set code
(`10`/`11`/`12`); FieldShape validation; and duplicate detection. One existing
test that asserted the old refusal was rewritten to assert the new warning.
The full suite passes; see the testing-status table above.

**Manual acceptance.** Driven in a rendered harness on the busy synthetic sheet
(`????29`): `100029` applied with no dialog; the resolved Student ID selected
again and `100028` typed; the preview named position 6 as a confident read;
the dialog listed `Position 6  9 → 8`; confirmed; history kept machine `9`,
manual and effective `8`, effective ID `100028`; the set-code conflict on the
same sheet was selected next; a real `Ctrl+Z` keypress restored `100029`. **Not
yet exercised by an operator on a real sheet.**

**Follow-up patch: sheet-level field editors, and set codes rebuilt by symbol.**

*Field editors belong to the sheet.* The editor used to be derived from the
**selected record**: it opened only when that record was a position of the
Student ID or set code. So once every Student ID conflict was resolved the
operator had to switch the queue to *All* and find an old row, and a Student
ID read confidently in every position — no record at all — could not be
edited. The editor row now carries two compact buttons, **Edit full Student
ID…** and **Edit full Set code…** (named from the template), shown for the
sheet being reviewed whatever is selected: a position conflict, a set-code
conflict, a duplicate-ID conflict, or a resolved record under *All* (the same
rule covers a sheet-scope finding on a registered sheet; that case is not
separately tested). The field's zone is the one recognition read it from and its
shape comes from the template. Both buttons are hidden — and the editor
refuses to open — while the sheet is still loading or if it never rectified.
`E` opens the field the selected record belongs to, and the Student ID
otherwise. Moving to another sheet closes an open editor, so a value typed for
one script is never applied to the next. A Student ID with no conflict is
edited through the operator-override path above; nothing is fabricated. A field
disputed *as a whole* (a wholly blank ID) is settled as that one record rather
than as per-position overrides.

A defect found by the new tests and fixed: an override made while the *other*
field's record was selected was stored against that field's kind (a Student ID
override filed as a set-code record), so it never reached the effective ID.
The kind now comes from the field being edited.

*Set codes are rebuilt from symbols.* The effective Student ID / set code (and
the CSV export's) was built by writing each decision into the machine's
assembled string **by character index**. A position printing `10` is two
characters, so correcting position 1 of `10?` to `2` gave `12?` instead of
`102`. The per-position records were already right; the assembly was not.
Decisions are now collected per field and laid over the machine's
**per-position symbols**, read from the stored recognition result, then
joined. One shared set of helpers in `conflict_policy` —
`split_field_value`, `join_field_value`, `machine_field_symbols` — serves the
editor's parsing, the current / proposed preview, the history text,
`effective_identifiers`, `effective_set_codes` and `sheet_resolutions`.

**Testing (this patch).** 10 Resolve-stage GUI tests (one of them the old
"not for a duplicate identifier" test, rewritten to expect the editor) and 5
review-store tests. They cover: the editors offered with a Student ID, set-code
and duplicate-ID record selected; after every Student ID conflict is resolved,
without changing the filter; a sheet with **no** Student ID record overridden
through the sheet action, with no detection fabricated; a wholly blank
Student ID settled as its one record; a never-registered
sheet offering neither; the editor closing on a change of sheet; and, for a
set code printing `0`–`9`, `10`, `11` with two positions, the machine
reading `10?`, a single-position decision giving `102`, parsing (`102` →
`10`+`2`, `112` → `11`+`2`), the preview, the audit text, the CSV export
value, and grouped undo. Also symbols such as `B2` and `X`, and the fallback
for a sheet with no stored result.

**Remaining limits.**

- At narrow window widths the one-line preview can be cut off at the right; the
  full text is in its tooltip, and the confirmation dialog always lists every
  override. Non-blocking.
- An override stores the machine observation of the re-read, without per-bubble
  candidate fill scores. Non-blocking.
- Reassembly falls back to the old character substitution in two cases: a scan
  row with no decodable stored result, and a field with both a whole-field
  decision and positional ones. Both are exact for single-character symbols
  (every numeric Student ID).

#### Correcting a whole field, and finishing a sheet before leaving it

Two workflow problems, both reported from working a real 1,875-sheet batch.

**A candidate who leaves four Student ID positions blank produces four
conflicts**, and the operator usually knows the whole number — it is written on
the script. Deciding four positions separately, each with its own reason, was
work the interface was creating rather than work the examination needed.

**Edit full \<field\>…** (or `E`) opens a one-line editor beside the value
buttons, prefilled with what the sheet currently reads — `??0029`, with `?`
where no value can yet be stated. Typing stages every position the value would
change **on the sheet**, in the pending style, so the number can be seen
landing on the right bubbles before anything is written. One **Apply**, one
reason, one note. The same control handles the Question Set / Set Code,
including multi-position and multi-character codes.

Everything is derived from the template — how many positions, and which symbols
each one prints — so a five-digit identifier or a two-position set code is
validated against *its* field. A set code printed `10`, `11`, `12` is split by
symbol, not by character. Nothing is padded or truncated.

**The position model is untouched.** What an edit writes is ordinary position
corrections, one per position, each against its own conflict with its own audit
event, reviewer and reason — a faster way to reach the existing model, not a
replacement for it. Two deliberate limits:

- **Confidently read positions are left alone.** Six digits typed against four
  disputed positions writes four corrections, not six.
- ~~**A position nobody disputes cannot be overruled here.**~~ *Superseded:*
  the explicit field editor can now overrule a confident reading, as an audited
  override after a warning — see
  [Overriding a confidently read position](#overriding-a-confidently-read-position).

Every event of one edit carries a shared action token in the ledger's detail —
no schema change; `audit_event` is under immutability triggers and is not
altered for a convenience feature — so the history shows the four corrections
as one operator action, and **one `Ctrl+Z` takes the whole edit back**. A
position somebody has since decided again is left alone rather than rolled back
over their work.

**The queue jumped after every resolution.** The root cause was the ordering:
`list_conflicts` sorted by severity across the whole *batch*, so a roll-number
column with two marks (severity 1) and an uncertain one (severity 0) on the
same paper ended up hundreds of rows apart. Resolving one therefore moved the
selection to a different sheet, and selection was then restored **by row
number** — which means a different conflict after every rebuild.

The queue is now **sheet-major**: the sheets in most trouble first, each sheet's
conflicts one contiguous run. Auto-advance selects the next conflict *on the
same sheet* and only hands over to another sheet when this one has nothing left
needing a decision — so a script can be worked to the end while it is in the
operator's hand. Selection is restored by conflict, then by sheet, then by
scrollbar; never by index. The queue shows the grouping by writing the file
name once per sheet and marking its other rows as continuations, and the header
counts each thing separately: `Conflict 2 of 5 on this sheet · 4 left here ·
137 left in batch`, with the denominator taken from the sheet rather than from
the filtered queue so it does not collapse to "1 of 1" as the work proceeds.

**Two defects found while driving it**, both fixed: the whole-field editor was
computed before the sheet finished loading and so stayed hidden for the rest of
that conflict; and closing the editor let Qt move focus into the note box,
where every shortcut on the page correctly refuses to fire — so the next
`Ctrl+Z` did nothing. A third was found by a test written for it: undoing a
field edit reversed a position that had since been decided again, because the
"still standing" walk returns superseded commands as well as current ones.

**Testing.** 170 Resolve-stage GUI tests (41 new) and 42 review-store tests (14
new), covering queue contiguity, sheet-local progression across a five-conflict
sheet, template-derived field length and symbols, validation (length, alphabet,
the `?` marker, no padding, the undisputed-position refusal), one-operation
resolution, untouched positions, shared reason and note, `Other` still needing
a note, the shared action token, grouped undo, undo not rolling over later
work, staging on the sheet, and the duplicate-ID and registration-failure
refusals. Ruff and mypy green.

Driven end to end in a rendered harness — five conflicts on one sheet, whole
roll number typed once, four positions settled, stayed on the sheet for the set
code, one undo took the field edit back. **No operator has yet worked a sitting
on it**; that remains Phase 11B.

#### The Resolve stage, laid out for the work it is actually used for

A second pass over the same screen, driven by watching it carry a 1,875-sheet
batch. Nothing about detection, scoring, persistence or the audit ledger
changed; what changed is where the space goes and what the controls say.

**Space.** The window is now divided **29 / 71** between the queue and the
workspace, and the workspace **70 / 30** between the sheet and the decision —
by ratio, re-applied on every resize, with floors so neither pane can be
dragged into a sliver, and abandoned the moment the operator drags a splitter
themselves. Stretch factors alone did not hold it: a stretch factor governs how
*extra* space is shared, not what the panes start at, and the starting point
came from size hints that handed the queue a third more width than the design
calls for. Within the decision row, the machine's four facts take **37%** and
the operator's controls **63%**, where they used to take half each.

**Framing.** The zoomed field was sized once, when the sheet finished loading —
before the tab had been shown, so against a viewport that did not exist yet,
which is why it appeared as a small image pinned to the corner of a large empty
canvas. The view is now given a *region to keep in frame* and re-fits it on
show and on resize. Two further changes come with that: the region includes the
**neighbouring printed positions**, because a faint mark is judged against the
columns beside it, filled by the same candidate in the same pencil; and the
region is grown to the pane's proportions before fitting, so the spare width of
a wide pane showing a tall column is spent on *more of the sheet* instead of on
blank canvas.

**Colour means one thing each.** On this stage **amber** is "the machine read
this mark" and **red** is "a person decided this" — on the sheet and on the
buttons alike, from one design token. The recognition status palette is turned
off here, so a zone is no longer outlined red merely because its status is
`multiple`, which put a third meaning on the same colour inside the same
rectangle. Every distinction is also carried by line weight or style, so none
of it depends on telling amber from red.

| On the sheet | |
|---|---|
| dashed amber lane | this position is waiting for a person |
| dashed amber ring on a bubble | the engine read this mark |
| dashed **red** lane + heavy solid red ring | picked, not yet recorded |
| solid red lane + heavy solid red ring | recorded |

**Pick, then commit.** Clicking a value — or pressing its digit — now *picks*
it: the ring lands on the bubble, the comparison strip fills in, and nothing is
written until **Confirm** (or `Enter`). A correction changes what a script is
worth, and a single mis-click deciding it, with no moment in between to see
that the ring landed where the reviewer meant, is the accidental edit this
stage exists to prevent. The strip reads `MACHINE 1-7 · MANUAL 1 · EFFECTIVE
Pending` until it is committed, so an uncommitted choice is never displayed as
a stored result.

**"Accept machine value" was offered for readings that are not values.** For a
roll-number position the engine read as `0-5`, that button recorded `0-5` as
the human-decided value of one printed digit — which
`review_store._substitute_position` then substitutes into the identifier,
producing a candidate ID no roster will ever match, from a button that said the
machine was right. **The backend is unchanged and can still store it**; what
changed is that the interface no longer offers it as a resolution. The primary
action is now conflict-aware and says which of three things it would do:
*Confirm '5'*, *Confirm machine reading* when the reading is a symbol the field
can hold (or a blank, or a whole identifier for a duplicate), or a disabled
*Choose a value first* with a tooltip explaining why. **Reopen** is hidden
rather than permanently disabled.

Also: the queue's "Field" and "Conflict" columns — two narrow columns each
eliding half of one sentence — became a single **Issue** column reading
`Student ID - position 5 · More than one mark`, with every cell's full value in
a tooltip; the selected row is an accent *tint* with accent rules rather than a
solid dark red block; the batch summary is one line of counters with the
breakdown beneath in smaller type; the filters are labelled *Status* and
*Type*; the header counter names what each number counts
(`3/8 on sheet · 5 left here · 157 in batch`) so it cannot read as
contradicting the summary; the operator's name is a badge rather than a row;
`(blank)` reads as **Blank**; `0-5` is described as **0 and 5**; and the long
per-batch diagnostics sit behind **Details**.

**Registration failures stay special** — and now stay usable. The two tabs that
are empty by definition of that conflict are disabled and explain themselves,
the original scan is selected, and — the defect this pass found — the tab is
**given back** afterwards. Forcing it without restoring it meant one bad sheet
in a queue pinned every later sheet to the original scan, so the zoomed field
was never seen again. Qt's own tab switch, triggered by disabling the visible
tab, was being recorded as the reviewer's preference.

**Testing.** 129 Resolve-stage GUI tests (38 new in this pass), covering the
proportions at 1366×768 / 1600×900 / 1920×1080, ROI framing and re-framing,
manual zoom taking framing over, the three candidate-button states, pending vs
committed, the invalid-multi-mark action, the decided-conflict action, the
duplicate-ID and registration-failure paths, queue tooltips, toolbar tooltips
and counter scope. The whole suite (5,273 tests), `ruff` and `mypy` are green.

The screen was rendered and inspected at each stage, and swept through the
conflict types — identifier multiple mark, set-code multiple mark,
registration/undecodable sheet, resolved and deferred — at 1366×768 and
1900×980, checking the candidate states, the confirm wording, the tab handling
and the comparison strip in each. Two wording defects that sweep found were
fixed: a decided conflict offered a disabled *Choose a value first* instead of
*Reopen*, and an undecodable file offered *Confirm machine reading* where there
is no reading to confirm (it now says *Acknowledge*). The duplicate-student-ID
case is covered by the automated tests but was not reachable in the rendering
harness, which does not write the identifier column a duplicate is detected
from. **No operator has yet worked a sitting on it**, and the manual
walkthrough on real examination data remains Phase 11B.

#### Resolving a conflict is now reversible, and faster to read

The Resolve stage was correct but slow to work: it marked an uncertain position
with a `?` beside the single bubble the engine nearly chose, which pointed at
where the *machine's* doubt landed rather than at where the reviewer's
attention had to go — and a correction, once saved, could only be undone by
reopening it, which discarded every decision on it at once.

**What the preview says now.** The unit of review is the printed **position** —
a roll-number column is a stack of ten bubbles, and the question is "what is in
this column".

| Outline | Meaning |
|---|---|
| **Amber, dashed**, around the whole 0–9 stack | waiting for a decision |
| **Red, solid**, around the same stack | a person supplied or overrode the value |
| **Heavy red ring** on one bubble | the value they chose |
| **`BLANK`** caption | they decided the position carries no mark |

Every doubtful position on the sheet is outlined, not only the selected one,
with the active one drawn more heavily. State is carried by **line style as
well as colour**, so it survives a colour-vision deficiency and a grey
printout. The geometry comes from the bubbles recognition measured — the
template's own normalised coordinates projected onto the canonical page — so it
sits where the engine looked, at any zoom, and nothing is drawn onto the scan.
A conflict that is *not* a position on the paper (a page that would not
rectify, a corrupt file, a curled corner) gets no rectangle, because inventing
one would put a confident outline somewhere arbitrary. The `?` glyph is gone
from this stage and unchanged on the Scan page, where skimming a sheet for what
went wrong is the actual task.

**Undo is an event, not an edit.** The ledger is append-only and stays that
way. `review_store.standing_commands` folds a conflict's history into the
commands still *in effect* — a decision pushes, an `UNDONE` event pops — and
the effective value, the cached state and the queue's label are all derived
from that one fold. So:

- **`Ctrl+Z` steps back one decision**, not to the machine's value. A position
  corrected by X and then by Y returns to X's value and stays resolved; only
  when nothing is left standing does it return to the machine's reading and to
  *Open*. **Reopen** remains the "discard every decision" command.
- **`Ctrl+Shift+Z` takes back a whole sheet.** A sheet's resolution session is
  the maximal run of consecutive decisions on it in the ledger — the run ends
  exactly where the reviewer moved on — so nothing extra is stored to know it,
  it survives closing the project, and decisions from an earlier sitting are
  left alone. All of it reverses in one transaction, or none of it does.
- **`Ctrl+Y` re-issues the decision** through the ordinary path, recorded as a
  decision by whoever is reviewing now. There is no "redo" ledger entry,
  because a history claiming a value was *restored* would describe something
  nobody did. The redo stack is therefore session-only.
- **Nothing is a screen gesture.** Counters, queue, overlay and downstream
  results follow immediately, and reopening the project shows the undone state.

**Keyboard-first.** `0`–`9` choose the value that digit prints, `B` blank,
`Enter` accepts, `D` defers, `Shift+Enter` / `Ctrl+Enter` walk the unresolved
conflicts. A key and its button are the same command — the digit keys call
exactly what the value buttons call — so a typed correction carries the same
reviewer, reason and audit event as a clicked one. A digit acts only when the
active position genuinely prints that symbol, and every keyboard action refuses
while the focus is in a text box, so searching for roll number `170501` cannot
record `1` as somebody's student ID. Auto-advance (remembered per user) selects
the next undecided conflict after a decision; deferring never advances.

Also: the header reads `Conflict 3 of 8 · 5 unresolved` rather than a count
that did not move as the reviewer worked; the decision panel always states
**Machine result / Manual decision / Effective result**; and every queue row
names its state in words with a glyph, including **Reopened** for a conflict
that is open because somebody put it back.

**The two curved-arrow buttons that existed before this work were *not* undo
and redo.** They were bound to "previous conflict" and "next conflict", so the
one control an operator would reach for to take a mistaken correction back
moved the selection instead. They now mean what they look like; navigation
wears chevrons.

**Four layout defects found by running the real application** on a 1,875-sheet
batch, three of them older than this pass and one introduced by it:

| Defect | Cause | Fix |
|---|---|---|
| The decision panel filled with dozens of slivers reading *"This is not a"* | `_clear_choices` removed the value buttons and nothing else, so the "not a correctable value" note **and** a trailing stretch were appended on every refresh and removed on none. Thirty registration failures meant thirty notes and thirty spacers, which also squeezed the next conflict's value buttons to nothing | The layout is drained rather than a list of known widget kinds being removed, so there is nothing to forget to extend |
| The **State** column was off-screen behind a horizontal scrollbar | Five columns at Qt's default equal width need 638 px in a 484 px panel. The state cue added by this pass was therefore invisible | Only the identifier and the state take their own width; file name, field and conflict type share what is left and elide. Measured: 484 px of 484 |
| The Normalised sheet and Zoomed field tabs were unexplained grey rectangles for a sheet that failed registration | Such a sheet has no rectified page *by definition* — that is the finding — but nothing said so | `ScanPreviewView.set_placeholder`, with a message naming which of the three situations it is: undecodable file, unalignable page, or no preview produced |
| The evidence panel stopped mid-sentence | Its height is fixed by the splitter; its content is not | The evidence is scrolled |

**Testing.** 52 new Resolve-stage GUI tests and 28 new review-store tests, all
run, alongside the 39 existing GUI tests and 53 existing store tests. Assertions
are against **what ended up in the database** — an undo that only repainted the
screen would pass a test that read back the label. Two behavioural defects the
new tests found were fixed: deferring auto-advanced past the conflict the
reviewer had just asked to come back to, and the sheet-level undo target was
wrong when the operator had also decided something on the sheet they moved to.
No migration: `UNDONE` is a new value in an existing column, and a project
written before this build opens unchanged. **No real reviewer has yet worked a
sitting on it** — that remains Phase 11B.

#### Conflict resolution is for identity, not for answers

Conflict Resolution used to receive every value recognition could not decide,
including answers. An examination of a hundred questions could therefore stage
a hundred conflicts per sheet, and the one or two that genuinely stopped a
script being attributed were impossible to find in them. Worse, ambiguity no
human decision could improve — a candidate who filled two bubbles filled two
bubbles — blocked the batch from going on.

The **Resolve** stage now holds only ambiguity that leaves the *record*
unusable:

```text
recognition result
     |
     +-- student ID ambiguity ----> conflict
     +-- set code ambiguity ------> conflict
     +-- sheet unreadable --------> conflict
     |
     +-- answer ambiguity --------> answer result only
```

An ambiguous or multiply-marked **answer is not a conflict**. Everything the
engine measured about it is kept — the status, the fill ratios, `needs_review`,
the value — and it exports exactly as before: `B`, `B-D` for a double mark, `?`
or `B?` for a read too faint or too close to call, empty for a blank. Nothing
is discarded and no answer is resolved algorithmically.

Consequences, all of which are covered by tests:

- **Counts mean something again.** A batch with 100 ambiguous answers, 2
  disputed student IDs and 1 disputed set code reports **3** unresolved
  conflicts, not 103 — on the Scan page badge, in the Resolve summary, in the
  export's `unresolved_conflicts` column and in the project health check.
- **Answer ambiguity no longer blocks.** A batch whose only ambiguity is in its
  answers passes the conflict-resolution step with zero items.
- **Scoring does not silently gain an answer.** A question the engine could not
  reduce to one option is marked as a multiple (`?`), never as the option it
  nearly said and never as a blank, so removing the block did not turn a doubt
  into a mark. An unresolved **set code** still blocks: marking a script against
  the wrong paper's key is the worst available outcome.
- **Older projects open unchanged.** A project scanned before this change keeps
  its stored answer conflicts — they are evidence, and a decision somebody
  recorded on one is still honoured in exports and scoring — but they no longer
  appear in the queue or in any count, and a re-read withdraws the untouched
  ones. Nothing is rewritten on load.

The distinction is enforced where conflicts are *created*, not hidden in the
GUI: `ConflictType.requires_resolution` and `FieldKind.is_record_identity` in
`domain/review.py`, read by `services/conflict_policy.py` and by one SQL filter
in `services/review_store.py`. See
[Conflict detection and human review](docs/conflict_review.md).

**Testing.** 39 Resolve-stage GUI tests, 35 end-to-end conflict-review tests,
53 review-store tests, 40 detection-policy tests and 12 new tests for the
scoring guard, all run. The whole suite (4,659 tests), `ruff`, `mypy` and the
55-check Qt GUI smoke run were re-run green — the smoke run includes a new
check that a sheet carrying both a bad roll-number column and a double-marked
answer stages only the roll number. Real examination data remains Phase 11B.

#### One template per project

The template used to be three independent copies of a path — one held by the
Template screen, one by Calibrate, one by Scan — so the same `.omrt` file had
to be browsed for three times, and nothing stopped the three from disagreeing.

A project now records its template in `project.json`, as a **project-relative**
POSIX path (`templates/OMR-Scan.omrt`), so copying the project folder onto a
memory stick or a marking machine carries the choice with it. `project.json`
moves to format version 3; the field is optional, and a project written before
it existed opens unchanged.

Which template a project uses is decided in one place, when the project opens:

| The project… | What happens |
|---|---|
| names a template that exists | it is used, on all three screens |
| names a template that has gone | the project still opens; Calibrate says so and asks for a replacement |
| names none and owns exactly one | that one is adopted, and written down |
| names none and owns several | nothing is guessed — picking wrong would read the sheets against the wrong geometry and still look plausible |
| names none and owns none | the screens are empty, as before |

Saving a template, or opening one of the project's own templates, on the
Template screen makes it the project's template: the page tells the main
window, which records it and re-broadcasts the session, so Calibrate and Scan
learn about it exactly as they learn about a project being opened. A template
from outside the project is opened for viewing and adopted by nothing.
Switching projects swaps the template on every screen, and a project with no
template clears what the previous one left behind. File dialogs on these
screens now open inside the project rather than at the home directory.

**Testing.** 30 tests, all run: 20 against real project directories on disk,
10 driving a real main window through open, save, switch and delete. Two
defects the tests found — Scan keeping the previous project's template, and
Calibrate's "template missing" notice being overwritten by the label refresh —
were fixed and are covered.

#### Calibration workspace reorganised

The Calibration stage now gives its height to the thing it is about. A
ten-line status paragraph and an always-open results table used to sit
permanently beneath the registered page; with a real result loaded the preview
held **57%** of the workspace (547 px at 1920×1080) and the paragraph alone
took 140 px of it.

| | Before | After (drawer shut) | After (drawer open) |
|---|---|---|---|
| Preview, 1920×1080 | 547 px · 57% | **832 px · 87%** | 680 px · 71% |
| Preview, 1366×768 | — | 499 px · 77% | 347 px · 54% |

The split is decided by **stretch factors**, not fixed pixel heights, so the
preview grows with the window and reclaims the drawer's space when it is shut.
The status paragraph became a single wrapped row of labelled chips —
`✓ Registration 4/4 · ✓ Orientation · ID … · 69 marked · ⚠ 31 review` — each
carrying words as well as colour. Four sections now fold, all shut by default
and each showing a one-line summary on its own header: **Sample results**,
**Selected scan details**, **Recognition thresholds**, **Field diagnostics**.

Nothing was deleted. The response-position counts, near-threshold count and
unusable-window count are the body of *Selected scan details*; the per-question
list is in *Field diagnostics*, scrolled rather than stacked.

**Engine untouched.** No file under `imaging/`, `recognition/` or the
calibration service's scoring was modified by this pass.

**Testing.** 28 new layout tests plus the existing 90 calibration tests, all
run. Each of four layout guarantees was verified load-bearing by reverting it
and confirming its test fails. Screenshots captured at 1920×1080, 1600×900 and
1366×768 across the empty, passed, failed, drawer-open, thresholds-open and
diagnostics-open states, and inspected. The page was **not** driven by hand
through a live calibration run.

#### Real-scan registration debugging

A real project — a real `.omrt` template and eight real scanned sheets — failed
calibration with *"the orientation mark ... was not found"*. The mark was
present, black, and exactly where the template said.

**Root cause.** The orientation confidence was `fill / (1 / window_margin²)`,
which assumes the template's declared orientation box tightly bounds the
printed mark. Nothing enforces that, and the designer lets the box be drawn
with margin — which is the natural way to draw one. This template's box was
about twice the mark in each axis, so the ink was diluted over four times the
area: the correct orientation scored **0.29** against a 0.35 floor, and sat
only **0.01** ahead of its own 180° twin, because a window mostly full of
paper scores much the same wherever it is placed.

**Fix.** The mark is now scored over the best of several concentric windows,
so the measurement no longer depends on how tightly the box was drawn. A
template whose box already fits is unaffected. On the real sheets confidence
went from 0.29 to **1.00** and the margin from 0.00 to **0.63–0.73**.

The four registration markers were never the problem — they were detected all
along, at scores 0.93–0.95. The calibration panel reported *"Markers detected:
0 / 4"* purely because the failure path never recorded what the engine had
found, which sent the investigation after a detector fault that did not exist.
It now says *"not reached"* rather than asserting a count it never measured.

**Verified on the real dataset:** 8/8 sheets register, resolve orientation and
sample all 500 bubble positions, with 0.0 px reprojection error. Bubble
overlays were inspected visually against the printed sheet and are concentric
with the printed bubbles. The scans themselves are examination material and
are not in this repository; the regression tests are synthetic.

#### Template Editor interaction pass

Five interaction defects, all of the same shape — the editor knew the right
answer but did not show it until some later event:

| Defect | Cause | Fix |
|---|---|---|
| Spin-box arrows showed an I-beam and clicking them put the caret in the text | Styling a spin box switches it to `QStyleSheetStyle`, which lays the line edit across the whole padded rect unless the up/down buttons declare a width. They did not, so `childAt()` over an arrow returned the `QLineEdit` — the editor was physically on top of the buttons | Explicit `::up-button` / `::down-button` geometry in the central stylesheet, so the editor stops short of them. Hit-testing, not appearance; Qt's auto-repeat, keyboard stepping and typing are untouched |
| Editing X/Y/W/H in the inspector did not move the canvas | The panel emitted only on `editingFinished`, i.e. on Enter or focus loss | A separate `geometry_preview` signal on `valueChanged` redraws immediately; `geometry_edited` still commits once, so there is no undo entry per keystroke |
| Bubbles did not follow a region being dragged or resized | The mid-drag handler updated the inspector numbers and nothing else; bubbles were recalculated only on release | The same handler now re-fits the layout with `resize_zone` — the *same* pure function the commit path uses, so preview and result are identical and releasing produces no jump |
| Default bubble radius | `DEFAULT_BUBBLE_RADIUS` was the same constant as the legacy fallback for templates predating the field | Split them. New templates get **20 px**, converted per page; the fallback stays where it was, so existing templates keep the geometry they were drawn with |
| Cursor stayed a hand while drawing a region | Three code paths fought: a crosshair on the view, `ScrollHandDrag` putting an open hand on the *viewport*, and `_stop_pan` calling `unsetCursor()` unconditionally | One `_apply_cursor()` derives the cursor from the mode and sets it on the viewport |

Both directions of geometry editing are now synchronised: a canvas drag
updates the inspector and its normalised values, and an inspector edit updates
the canvas, the bubble layout and the normalised values.

**Testing status.** 55 focused tests were added and run. Each of the five
fixes was verified load-bearing by reverting it and confirming its test fails.
The spin-box tests are **skipped on the offscreen platform** — the defect is an
interaction with the *Windows* style, and on Fusion they pass whether or not
the fix is present, so they run on a Windows desktop and report a skip in
headless CI rather than false assurance. Manual GUI walkthroughs (TEST A–F)
have **not** been performed.

#### Cross-platform CI defects fixed

Four Ubuntu-only failures, all of them the suite correctly reporting a genuine
platform difference rather than flakiness:

| Defect | Cause | Fix |
|---|---|---|
| `mypy` reported an unreachable statement in `services/process_containment.py` | The Windows path sat behind an early `return` rather than a `sys.platform` *block*; mypy exempts platform-guarded branch bodies from `--warn-unreachable`, but not code following an early return | Restructured into `if sys.platform == "win32": … else: …`. No `type: ignore`, no change to the mypy configuration |
| Workflow labels elided in layouts chosen *because* the labels fit | `QFontMetrics.horizontalAdvance` sums glyph advances, `elidedText` lays the text out; they disagree by up to a pixel of right bearing, so a step drawn at exactly its measured width elides | One pixel of measured headroom per step (`ELISION_SLACK`), plus a test that pins the invariant at the exact boundary |
| The kill/resume test asserted that workers die with the coordinator | That is a Windows Job Object guarantee; `process_containment` documents that no POSIX equivalent is implemented | The Windows guarantee is still asserted on Windows; every other property is still asserted everywhere; surviving workers are reaped so no run leaks processes |
| A synthetic-dataset test compared ink over a fixed percentage crop of the raw page | Two causes. The sheet it measured was picked from an unsorted `glob`, so a different filesystem chose a different candidate — and a different candidate has a different number of marked bubbles, which *is* the measurement. The crop itself was also meaningless: a rendered sheet is not in canonical coordinates, since it carries a scan margin and the geometry cases rotate and crop it, so the rectangle covered whatever happened to fall in it | The scan is rectified with `align_sheet` and its bubbles measured with `measure_bubbles` — the same pair recognition uses — and the representative sheet comes from a sorted listing. Compared on `fill_ratio`, which is normalised against levels read from the image itself. A second test now also pins that a blank identifier renders *no* marked bubble at all |

The first three were verified locally with `mypy --platform linux` and the GUI
suite under `QT_QPA_PLATFORM=offscreen`, and then **confirmed green on the
Ubuntu runner itself** in
[run 35809212682](https://github.com/sajidbuet/OMRFlow/actions/runs/35809212682).
The fourth was found by the Ubuntu runner afterwards, in
[run 35959715301](https://github.com/sajidbuet/OMRFlow/actions/runs/35959715301),
and is fixed in this release.

### Release qualification infrastructure

Release qualification is a local, unattended framework — one command, a
machine-readable report, a meaningful exit code, and no supervision:

```powershell
python tools\release_validation\validate_release.py --safe   # no install/uninstall
python tools\release_validation\validate_release.py --all    # including the installer
```

It covers source tests, Qt GUI behaviour, accessibility, an end-to-end workflow
smoke test, the built bundle, the **packaged executable**, and the installer
round trip. See
**[`tools/release_validation/README.md`](tools/release_validation/README.md)**.

Current qualification focus:

- real examination datasets and real scanner output;
- real attendance workbooks;
- set-specific end-to-end validation;
- the clean-machine steps that need a person;
- a green Ubuntu CI run **for this release**: the Ubuntu runner has been green
  since the first three cross-platform defects were fixed, but the fourth was
  found after that and its fix has been verified only on Windows so far.

**Implemented, automated tests passing, synthetic dataset validated, real
scanned dataset validated and production validated are five different
things, and OMRFlow currently claims the first three.**

Detail: **[Development Roadmap](docs/wiki/Development-Roadmap.md)** ·
[Known Limitations](docs/wiki/Known-Limitations.md) ·
[Detailed status and capabilities](docs/wiki/Detailed-Development-Status.md)

## Running from source

```powershell
git clone https://github.com/sajidbuet/OMRFlow.git
cd OMRFlow
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m omr_scanner.main
```

Requires Python 3.12 or newer. Also the route to take if your organisation
will not permit an unsigned installer. See
[Development Setup](docs/wiki/Development-Setup.md).

```powershell
.\scripts\release\Invoke-Tests.ps1          # ruff, mypy and the test suite
.\scripts\release\Invoke-Tests.ps1 -Fast    # unit tests only
```

## Contributing

Contributions are welcome. Please read
**[CONTRIBUTING.md](CONTRIBUTING.md)** first — in particular the parts about
tests, database migrations, and never committing real candidate data.

- **Report a problem:** [open an issue](https://github.com/sajidbuet/OMRFlow/issues/new/choose),
  after reading [SUPPORT.md](SUPPORT.md) on how to sanitise a reproduction.
- **Security or privacy:** [SECURITY.md](SECURITY.md) — **not** a public
  issue.

> **OMRFlow processes examination material.** Never attach real candidate
> names, roll numbers, rosters, answer keys, scans or project folders to a
> public issue. OMRFlow can generate synthetic sheets — and synthetic
> attendance workbooks — for exactly this purpose:
> *Tools → Developer / Testing → Generate Synthetic Test Dataset…*

## Privacy

OMRFlow has **no telemetry, no analytics and no network access** during
examination processing. Everything stays in the project folder on your disk.
Treat that folder as confidential examination material — see
[Backup & Data Retention](docs/wiki/Backup-and-Data-Retention.md).

## Citation

If you use OMRFlow in research, teaching, academic work, software, or another
project, please cite the software. Citation helps others discover the project
and supports continued development.

**DOI:** <https://doi.org/10.5281/zenodo.22943575>

[![DOI](https://zenodo.org/badge/1371137095.svg)](https://doi.org/10.5281/zenodo.22943575)

Every release is archived on [Zenodo](https://doi.org/10.5281/zenodo.22943575).
The DOI above is the *concept* DOI: it represents OMRFlow as a project and
always resolves to the most recent archived release, so it stays correct as new
versions appear. Each individual release also receives its own DOI, listed on
the Zenodo record, for when you need to point at one exact version.

GitHub's **Cite this repository** button, on the right of the repository page,
reads [`CITATION.cff`](CITATION.cff) and will generate these for you.

```text
Choudhury, S. M. (2026). OMRFlow [Computer software]. Zenodo.
https://doi.org/10.5281/zenodo.22943575
```

```bibtex
@software{choudhury_omrflow,
  author    = {Choudhury, Sajid Muhaimin},
  title     = {OMRFlow},
  year      = {2026},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.22943575},
  url       = {https://doi.org/10.5281/zenodo.22943575}
}
```

## Licence

[MIT](LICENSE). © 2026 Dr. Sajid Muhaimin Choudhury.

## Developer

Developed by **[Dr. Sajid Muhaimin Choudhury](https://www.sajid.bd)**,
Department of Electrical and Electronic Engineering, Bangladesh University of
Engineering and Technology, with development assistance from ChatGPT and
Claude Code.
