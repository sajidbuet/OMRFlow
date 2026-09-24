# Synthetic datasets

> **Synthetic datasets supplement but do not replace qualification using real
> human-filled examination sheets.** These pages have clean geometry, even
> paper and marks drawn by arithmetic. They measure *regression consistency and
> controlled edge-case handling*. They are not evidence that a threshold is
> right for real pencil on real paper — that is Phase 11B, and it is not done.

## What it generates

One command produces a complete synthetic examination:

```text
SyntheticDataset/
├── images/                      the scans
│   ├── SYN_000001.jpg
│   └── ...
├── attendance/                  the paperwork, one workbook per set
│   ├── Set_10_Attendance.xlsx
│   ├── Set_11_Attendance.xlsx
│   └── Set_12_Attendance.xlsx
├── ground_truth/
│   ├── SYN_000001.json          per-sheet truth: answers, roll, set, defects
│   ├── candidates.csv           the authoritative roster
│   └── reconciliation.csv       true vs. workbook vs. scan, and the expected state
├── manifest.json
├── manifest.csv
└── dataset_summary.json
```

The images and the workbooks describe the *same cohort* — and deliberately
disagree about it, in the ways a real examination office's paperwork does.

## Where it is

**Tools → Developer / Testing → Generate Synthetic Test Dataset…**

The dialog has four sections: the template and destination, the image contents
(profile, case families, count, seed), **Attendance and reconciliation**, and
the image/output settings.

Each section folds. **Images and output** starts folded, because format, JPEG
quality and resolution are the settings that are changed least often — click
its header to open it. A folded section shows its current values beside the
title, and folding one never changes a setting: everything inside keeps its
value and is still used when you press **Generate**. The form itself scrolls;
the caveat and the **Generate** / **Cancel** buttons stay put at the bottom.

It needs a `.omrt` template. Everything about the sheets — page size,
registration markers, orientation mark, zone geometry, bubble grids, roll-number
and set-code fields — is read from that template, so a template for a different
form generates the corresponding different form. No coordinates are hard-coded.

## The two halves, and why they are separate

```text
candidate population          case plan
(who exists, who attended,    (answers, mark styles,
 what is marked on the sheet)  rotation, blur, banding)
          └───────────┬───────────┘
                      ▼
               rendered sheet
```

The population owns **identity**; the case plan owns the **image**. They meet in
one small function, `attendance_dataset.bind_case`, which overlays the
candidate's roll and set onto a planned case. Neither has to know how the other
decides.

## Ground truth comes first

```text
roster → attendance → conflicts → workbooks → sheets → images
```

Never:

```text
image → recognition → "ground truth"
```

The expected reconciliation state for every candidate is derived from the plan.
Nothing in the generator runs the recognition engine or the reconciliation
service to decide what the answer should be — a ground truth computed by the
code under test is not a ground truth.

## The three states

For every candidate, `reconciliation.csv` keeps apart:

| Column group | Means |
|---|---|
| `true_*` | What actually happened |
| `attendance_*` | What the workbook claims |
| `omr_*`, `scan_present` | What is marked on the scan, if there is one |
| `expected_reconciliation_state` | Where Phase 7 should land |

`candidates.csv` holds **only** the truth. It never contains the deliberately
corrupted attendance — a truth file carrying the errors is not a truth file.

## Conflicts

Every one maps to a state that already exists in `ReconciliationStatus`; none
was invented for the generator.

| Conflict | Expected state |
|---|---|
| `none` | `matched` |
| `true_absentee` | `absent_confirmed` |
| `marked_absent_but_present` | `absent_with_script` |
| `marked_present_but_absent` | `present_without_script` |
| `blank_candidate_id` | `unresolved_candidate_id` |
| `partial_candidate_id` | `unresolved_candidate_id` |
| `candidate_id_multiple_mark` | `unresolved_candidate_id` |
| `wrong_candidate_id` | `present_without_script` (plus a stray sheet) |
| `unknown_candidate_id` | `unknown_id` |
| `duplicate_script` | `duplicate_script` |
| `missing_scan` | `present_without_script` |
| `wrong_set` | `matched`, review required |
| `blank_set` | `matched`, review required |

The three identifier defects share one state on purpose: the engine cannot tell
a blank field from an over-marked one and must not guess. Which defect it was
stays in `conflict_type`.

A set disagreement does not stop a script being matched to its owner — it stops
it being scored against the right key, which is a separate decision. So those
two are `matched` with `manual_review_required` set.

**One conflict per candidate.** Conflicts are assigned from a single quota-based
draw, not by rolling each probability independently, so nobody comes out
simultaneously a true absentee, missing a scan and the owner of a duplicate
script. Real datasets do contain compound failures, but a compound failure that
arose *by accident* has no defensible expected state.

### Why quotas rather than dice

At 100 candidates a 0.25 % rate produces a duplicate script about a fifth of the
time, so most runs would silently omit a case the dataset claims to cover.
Filling exact quotas and shuffling gives the same expected composition with none
of the variance. **Guarantee one of every reconciliation conflict** additionally
forces one of each, whatever the rates work out to at that roster size.

## Count means candidates, not images

With attendance on, **Sheets** is the size of the candidate roster. Absentees
and missing scans mean fewer images than candidates — a 30-candidate roster
typically renders 27 sheets. The dialog's summary line states both before you
generate anything, computed from the real plan rather than from the rates.

## The workbook format

Not invented here. `services/candidate_import.py` defines what OMRFlow accepts,
and `resources/templates/candidate_attendance_sample.xlsx` shows the shape:
worksheet `Rollwise(All)`, headers `Sl.No. | Roll No. | Name | Total (90) |
Merit`, with `ABS` in the marks column meaning absent and anything else —
including a blank — meaning not absent.

Generated workbooks are round-tripped through OMRFlow's own `read_roster()` in
the test suite. A workbook the application cannot import is a generator defect.

## Determinism

The same **template + configuration + generator version + seed** reproduces the
same dataset, byte for byte, images included. The roster, the set assignment,
the attendance, the conflicts and the rendering parameters are all decided from
the seed before any sheet is drawn, so nothing depends on the order sheets
happen to be rendered in.

A different seed changes *who* is affected but not *how many* — the composition
is fixed by the quotas.

## Command line

The GUI and the CLI use the same generator.

```powershell
python -m omr_scanner.tools.make_dataset D:\OMRFlow-Synthetic `
    --template examples\templates\ece_0000_sample.omrt `
    --count 1000 `
    --sets 10,11,12 `
    --seed 20260923 `
    --dpi 300 `
    --format jpg `
    --attendance-conflict-profile normal `
    --true-absentee-rate 0.05
```

Omit `--sets` and no attendance is generated — images and their ground truth
only, exactly as before this feature existed.

| Option | Effect |
|---|---|
| `--sets` | Comma-separated set codes. Given, enables attendance generation |
| `--attendance-conflict-profile` | `none`, `low`, `normal`, `high`, `custom` |
| `--true-absentee-rate` | Genuine non-attendance, as a fraction |
| `--no-reconciliation-edge-cases` | Do not force one of every conflict |

Set codes are never assumed to be a single character.

## Memory and large datasets

Generation is streaming: one sheet is rendered, encoded, written and released
before the next begins. A hundred thousand sheets costs one page of memory, not
a hundred thousand. Roughly 60 GB of disk at A4/300 dpi, so check the
destination before starting a run that size.

## Cancellation

Cancelling stops after the sheet being drawn. Completed images stay, and the
manifest records `cancelled: true` with the number actually produced — it never
claims a partial dataset is complete.

## Using a dataset for reconciliation qualification

1. Generate with attendance on.
2. Create a project, load the same template, import `images/`.
3. Import the matching `attendance/Set_NN_Attendance.xlsx` on the Attendance
   stage, per set.
4. Reconcile.
5. Compare the result against `ground_truth/reconciliation.csv`.

The comparison key is `candidate_uid`, which is stable regardless of what
identifier ended up marked on the scan.

## Known limitations

- **Colour mode is not selectable.** Output follows the renderer's existing
  behaviour; Color/Grayscale/Black-&-White as a user choice is not implemented.
- **No preview, no resume, no debug overlays**, and no disk/time estimate
  before a large run.
- **No single-sheet reproduce command.** A sheet is reproducible from the seed
  by regenerating the dataset, but there is no `--reproduce --sheet N`.
- **Attendance conflicts are not combined with each other.** One per candidate,
  by design; combined defects would need a named stress case.
- **The workbook is generated, not copied from a project's own attendance
  template.** Preserving a user-supplied workbook's formatting, logos and
  merged cells is not implemented.
