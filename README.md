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

**Current release: `0.1.0-alpha.2`**

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
| Automated suite | 5,438 tests passing in the full local run of 2026-09-28 (3 skipped; 4 `stress` tests deselected by default); `ruff` and `mypy` clean |
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
| Scan-quality / page geometry | 🟠 **Implemented — under testing.** Detects a physically folded, curled or lifted sheet that registers cleanly but whose printing has moved. Validated on synthetic lattices, the committed sample sheet and two real scans; see [Scan quality](docs/scan_quality.md) |

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

**Remaining limits.** A set's reconciliation still reads every script of the
batch, so in a batch mixing papers another set's scripts appear as unknown IDs
(pre-existing). Suggestions compare IDs position by position. The app does not
persist splitter positions, so neither does this page. See
[Known Limitations](docs/wiki/Known-Limitations.md).

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
