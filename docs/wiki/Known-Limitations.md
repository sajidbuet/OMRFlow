# Known Limitations

**For `0.1.0-alpha.1`.** This page is the honest account of what OMRFlow can
be trusted with. It distinguishes five things that are often run together:

| Status | Meaning |
|---|---|
| ✅ **Implemented and tested** | Built, and validated by automated tests |
| 🟡 **Synthetically tested** | Built and tested, but only against data OMRFlow generated itself |
| 🟠 **Not yet qualified** | Built, but a required validation has not been performed |
| 🔴 **Known defect** | Behaves incorrectly, and is known to |
| ⚪ **Planned** | Not built |

A 🟠 is **not a defect**. It means nobody has yet proved the thing works on
real material, which is a different and equally important statement.

---

## The one that matters most

> 🟠 **No real examination has been processed end to end.**
>
> Every workflow in OMRFlow has been validated against synthetic data:
> sheets OMRFlow rendered itself, from templates it already understood, with
> answers it already knew. No real attendance workbook and no real scanned
> cohort has been through the pipeline.
>
> **Independently verify every generated result before acting on it.** Check
> a sample of sheets by hand against what OMRFlow read, and check a sample of
> computed marks against your own arithmetic.

Closing this is the whole purpose of
[Phase 11B](Development-Roadmap#phase-11b--real-data-qualification--beta-release).

---

## By area

### Project and configuration

| | Area |
|---|---|
| ✅ | Create, open, close, reopen and validate a project |
| ✅ | Refusing damaged or incompatible projects with a readable message |
| ✅ | Examination name and question-paper sets (*examination sets, Part 1*) |
| ✅ | A second OMRFlow process cannot open a project this one has open |
| 🟠 | Opening a project created by an *earlier released version* — there has not yet been an earlier release |

### Template design

| | Area |
|---|---|
| ✅ | Registration markers, orientation mark, region drawing, bubble grids, per-bubble adjustment, undo/redo, validation, save and reload |
| 🟡 | Templates for sheet layouts other than the bundled example. The designer is general, but only a few layouts have been exercised |

### Recognition

| | Area |
|---|---|
| ✅ | Registration, orientation detection, coordinate mapping, roll/ID, set code, answer blocks |
| ✅ | Blank and multiple marks reported explicitly rather than resolved silently |
| ✅ | Identical results on any number of CPU cores |
| 🟡 | Accuracy on real scanner output. Measured against synthetic sheets with deliberate degradations, not against paper |
| 🟠 | Behaviour across scanner makes, resolutions and paper stocks |

### Batch processing

| | Area |
|---|---|
| ✅ | Multicore processing, progress reporting, cancellation |
| ✅ | Durable batches: an interrupted run resumes without reprocessing completed sheets |
| ✅ | Worker processes die with the coordinator (no orphans) |
| 🟠 | **The 100,000-sheet qualification campaign has not been run.** The harness is complete and validated at 250–2,000 sheets, including real forced kills, a real orchestrator crash and recovery. The full-scale run — roughly a day of machine time — has not been executed |

### Review and resolution

| | Area |
|---|---|
| ✅ | Conflict queue, side-by-side original/corrected/zoomed views, append-only audit of every decision |
| ✅ | A correction cannot be saved without a named reviewer and a reason |

### Attendance and reconciliation

| | Area |
|---|---|
| ✅ | Importing a candidate list from CSV or Excel, including a header row below a title |
| ✅ | Detecting unknown, duplicate and missing scripts, and absentees with a script |
| 🟡 | **Per-set attendance** (*examination sets, Part 2*). Implemented and tested — with synthetic rosters only |
| 🟠 | Real attendance workbooks. Institutional workbooks vary in ways synthetic ones do not: merged cells, multiple header rows, trailing totals, unexpected columns |

### Answer keys and scoring

| | Area |
|---|---|
| ✅ | Manual entry, recognition from a solution sheet, revisions, verification before use |
| ✅ | Independent keys per set; questions markable as defective |
| ✅ | Marks for correct/incorrect/blank, negative marking, standard competition ranking |
| ✅ | Recalculation after a key change, a scoring change or a resolved conflict |

### Results and reports

| | Area |
|---|---|
| ✅ | Roll-wise workbook built on the set's own attendance workbook, preserving its formatting and logo |
| ✅ | Merit-wise sheet with absentees removed and the rest ordered by result |
| ✅ | Summary, Answer Key and Processing Log sheets; Excel rank formulas |
| 🟡 | **Set-aware reporting** (*examination sets, Part 2*). Synthetic testing only |
| 🟠 | Real institutional result templates |
| 🟠 | Report generation at stress scale. The stress dataset has no roster, answer key or result template, and inventing them would measure a fabricated scenario |
| ⚪ | PDF export requires **LibreOffice** installed separately. Without it the XLSX is still generated |

### Interface

| | Area |
|---|---|
| ✅ | Nine-stage navigation, responsive down to a 720-pixel window, keyboard accessible with visible focus |
| ✅ | Text contrast verified against WCAG AA; no state carried by colour alone |
| 🟠 | **Results, Resolve and Attendance tables are not lazily loaded.** The backend has been verified against 100,000 rows; those three *displays* have not been optimised for it and may be slow with a very large cohort. The Scan table *is* lazy |
| 🟠 | Screen-reader testing. Accessible names and descriptions are set throughout, but no screen reader has been used to work through the application |
| ⚪ | No dark theme |
| ⚪ | English only |

### Packaging and installation

| | Area |
|---|---|
| ✅ | Windows installer; install, launch and uninstall verified, with user data preserved |
| ✅ | No Python needed on the target machine |
| 🟠 | **Clean-machine installation has not been verified.** Every installer test so far ran on the machine that built it, which has a Python development environment. See [the procedure](https://github.com/sajidbuet/OMRflow/blob/main/docs/release/CLEAN_MACHINE_TEST.md) |
| 🟠 | **Windows 10 has not been tested.** Built and tested on Windows 11; Windows 10 1809 is the floor the bundled runtime supports |
| ⚪ | **The installer is unsigned**, so SmartScreen warns. Signing is a Phase 11C item |
| ⚪ | Windows only. No macOS or Linux package |
| ⚪ | 64-bit only |

### Upgrades

| | Area |
|---|---|
| ✅ | Database migrations are append-only and refuse to open a newer schema |
| 🟠 | **Upgrading between released versions is untested** — `0.1.0-alpha.1` is the first release, so there is nothing to upgrade from. **Back up projects before installing a later Alpha.** See [Upgrading OMRFlow](Upgrading-OMRFlow) |

---

## Examination sets: the exact status

The examination-sets enhancement runs across several phases and its two
parts are at different maturities. Stated precisely because this is the most
likely thing to be misread:

### Part 1 — Project configuration · ✅ Implemented and tested

An examination name, and a variable number of persistent, uniquely
identified sets each with an operator-visible code and a description.
Covered by 116 automated tests plus an end-to-end GUI check.

### Part 2 — Per-set attendance and set-aware reporting · 🟡 Synthetically tested

One attendance workbook per set; that workbook becoming the set's result
template; a roll-wise result built on it; and a merit-wise sheet derived from
the completed roll-wise sheet. Implemented and covered by automated tests.

**Remaining:** validation with at least one representative real attendance
workbook and one real scanned cohort. Until that is done, treat set-specific
attendance and reporting as the least proven part of OMRFlow.

---

## Known defects

🔴 **None currently known.**

This is a statement about knowledge, not about correctness: it means no
incorrect behaviour has been identified and left unfixed. Given that real-data
qualification is incomplete, the absence of known defects should not be read
as evidence that there are none.

Found one? [Report it](https://github.com/sajidbuet/OMRflow/issues/new/choose).

---

## Not features, by design

These are deliberate and are not planned to change:

- **No telemetry and no network access.** OMRFlow makes no requests during
  examination processing and uploads nothing, ever.
- **No cloud storage or sync.** A project is a folder on your disk.
- **No automatic repair.** The health check reports problems and does not
  silently fix them, because a silent fix to examination data is
  indistinguishable from data loss.
- **No "resolve this for me" for ambiguous marks.** A mark OMRFlow cannot
  read confidently goes to a person. Guessing would be faster and wrong.
