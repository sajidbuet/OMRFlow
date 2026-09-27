# Synthetic datasets

> **Synthetic datasets supplement but do not replace qualification using real
> human-filled examination sheets.** The marks are drawn by arithmetic in both
> rendering modes, and a real candidate's pencil is not an ellipse. They measure
> *regression consistency and controlled edge-case handling*. They are not
> evidence that a threshold is right for real pencil on real paper — that is
> Phase 11B, and it is not done.

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

## Two rendering modes

The generator decides *what each sheet says* in exactly one place, before
anything is drawn. Where the **page** comes from is a separate choice:

```text
    case plan  (answers, identifier, set code, degradation)
          │
  ┌───────┴────────┐
  ▼                ▼
Template-rendered   Real scanned sheet
synthetic           + synthetic markings
  └───────┬────────┘
          ▼
   the same ground truth
```

| | Template-rendered synthetic | Real scanned sheet + synthetic markings |
|---|---|---|
| The page | Drawn from the template | A scan of a real blank form |
| The marks | Drawn | Drawn |
| Paper, print, lighting, scanner noise | Modelled | Real |
| Resolution | From `--dpi` and the template's physical size | The scan's own; `--dpi` is not applied |
| Marker-damage cases | Generated | **Skipped** — see below |
| Needs | A template | A template **and** a blank scan of that form |

**The second mode is the better evidence, and it is still not sufficient
evidence.** The page is real; the marks are not.

### How it works

Only the candidate's ink is rendered — no markers, no bubble rings, no printed
option letters, nothing the real sheet already has. That mark layer is warped
into the reference scan's own pixels and multiplied onto it, so the paper
survives underneath every mark instead of being replaced by a flat disc.

The reference scan is registered **once per run**, through
`omr_scanner.imaging.align_sheet` — the same alignment the Scan stage uses, not
a second registration built for the generator. A scan that will not register is
refused before a single sheet is written, with a message naming the file and
what the engine objected to. A reference fed upside down or a quarter turn
askew is fine: the orientation mark resolves it and the marks still land in the
right bubbles.

The mark layer is rendered at an integer multiple of the template's canonical
page size, chosen to be at least as dense as the scan (capped at 4×), so marks
are as sharp as the paper they land on. The factor used is recorded in the
manifest.

### What the second mode cannot do

Marker and orientation damage — `MARKER_FAINT`, `MARKER_DAMAGED`,
`MARKER_MISSING`, `MARKERS_MISSING_MANY`, `MARKER_EXTRA`,
`ORIENTATION_MISSING`, `ORIENTATION_FAINT`. Those marks were printed and
photographed before the generator ran, and it only draws the candidate's ink.

Such a case is **not faked and not silently mislabelled**. The defect is
dropped, the tag is dropped with it, and `expect_failure` is cleared unless the
sheet still has an unrelated reason to fail (a severe crop does; a missing
marker that has just been put back does not). The sheet's ground truth notes
say it happened. A benchmark therefore never reports a `MARKER_MISSING`
category it did not actually test.

Everything else — rotation, scale, perspective, cropping, blur, noise,
exposure, speckle, streaks, paper tint, JPEG damage — applies in both modes,
to the composited image.

### Validating it against a genuine blank scan

**Status: run once, passed.** The automated tests exercise this mode against
*rendered* stand-in blank pages, because a committed test cannot depend on a
scan nobody has; registration accuracy is measured there against known
homographies (worst case 0.33 px at the bubble centres).

It has since been run once against a real 2526×3417 scan of a 100-question
form, and passed:

| Check | Result |
|---|---|
| Registration | 0.0 px reprojection error, weakest marker 0.88, orientation confidence 1.0, no warnings |
| No duplicated artwork | One mark changed 0.0057 % of the page; all four registration markers and the orientation mark byte-identical |
| Ink placement | Per-option delta on a sheet answering D: `A +0.0, B +0.0, C +0.0, D +56.5` |
| Recognition | 5 sheets, 500 answers: **0 wrong**, 2 correctly flagged, rolls and set codes 5/5 |
| Colour | Grayscale and colour 100/100; black-and-white lost 4 faint marks, as intended |
| Folds | No-marker folds read 100/100; a quarter-covered marker registered with a warning; both fully covered markers were refused, matching `expect_failure` |

That is **one form, one operator, one scanner**. It is a smoke test, not a
corpus, and it is not evidence that a threshold is right in general. Run it
again whenever a new form or a new scanner enters the picture.

#### What it is good at surfacing

The run above turned up a calibration observation that no synthetic page could
have produced: on that form the printed option letter inside an *empty* bubble
already occupied a mean of 0.187 of the measured area, with 3 % of bubbles at
or above the template's `blank_ratio_threshold` of 0.25 **before anything was
written in them**. The two flagged answers were exactly that — the engine
correctly declining to call a bubble blank when the printing alone is near the
threshold.

If your run produces flagged answers on bubbles that should be plainly empty,
measure the blank sheet's own ink before suspecting the generator.

The smoke test, in full:

1. Scan one **blank, unmarked** copy of the printed form — the same form the
   `.omrt` template describes — at the resolution you normally scan at. Do not
   crop it or straighten it; a slightly crooked scan is a better test.
2. Open the project whose template matches that form.
3. **Tools → Developer / Testing → Generate Synthetic Test Dataset…**
4. Template: the matching `.omrt`. Render mode: **Real scanned sheet +
   synthetic markings**. Reference scan: **Browse…** (opens on the project
   folder) and pick the blank scan.
5. Sheets: **3–5**. Profile: **Baseline**. Turn **attendance off** for a first
   look — it is a separate concern and only adds files to read.
6. Generate. If the scan cannot be registered the run stops immediately with a
   message naming the file and the reason; nothing is written.

Then open the images and check, in this order:

| Check | What you are looking for |
|---|---|
| Registration marks | Exactly **one** marker per corner. A second, drawn one means the mark layer is leaking printed artwork |
| Orientation mark | One, unchanged |
| Bubble rings and option letters | **One** of each. Doubled or offset printing is the failure this mode is most vulnerable to |
| Student ID marks | Inside the right roll-number bubbles, top of the sheet |
| Set-code marks | Inside the right set bubbles |
| Answer marks | Inside their rings, **top and bottom of the page alike** — a registration error shows up as drift that grows down the sheet |
| Paper and texture | The scan's own paper, print quality, shadows and noise still visible *through and around* the marks |

Compare against `ground_truth/SYN_000001.json` — `answers`, `roll` and
`set_code` are what the sheet should read.

**Where to inspect the registration itself:**

- `manifest.json` → `generator.reference_scan` records the file name, the
  scan's pixel size, the supersampling factor and a `registration` block:
  `max_reprojection_error_px`, `min_marker_score`, `orientation_confidence`,
  `quarter_turns` and any alignment warnings. A `min_marker_score` near the
  acceptance floor, or a non-empty `warnings` list, is worth knowing before you
  trust a large run.
- Each sheet's `ground_truth/*.json` → `metadata.render_mode`,
  `metadata.reference_scan`, `metadata.color_mode` and `metadata.render`.
- The application log records one `Reference scan '…' registered:` line per
  run with the same figures — useful when a run is refused, since the refusal
  message carries the alignment engine's own error code.

If the marks land correctly, repeat once with **Colour** set to **Colour (RGB)**
and once with **Black and white (1-bit)**, and expect the faint marks to be
lost in the black-and-white run — that is the setting behaving correctly.

## Physical corner folds

Off by default. **Tools → Developer / Testing → Generate Synthetic Test
Dataset… → Physical page deformation**, or `--folds` on the command line.

A folded corner is the commonest physical accident a sheet suffers between the
candidate's desk and the scanner, and it is worth reproducing because of what
it does to *registration*: a fold deep enough to reach a corner marker takes
that marker out of view, and the engine's response is the behaviour worth
measuring.

### What is actually modelled

```text
   before                    after
+-----------+           +--·        the corner triangle is VACATED
|\          |           |  ‾·.      - its paper has gone, the scanner
| \  fold   |           |     ‾·.     sees the backing through the gap
|  \        |    -->    |  flap  |  the mirror triangle is COVERED
|   crease  |           |        |  - the flap lies face-down on it
+-----------+           +--------+
```

Both triangles together are what the fold takes out of view, and marker
overlap is measured against the **union**. Measuring against the corner
triangle alone would understate every fold by about half — the flap covers as
much page as the gap exposes.

Each fold gets: the gap showing backing, the flap as blank paper with the
printing on its far side ghosting faintly through, a soft shadow where the
raised flap stands off the page, and a hard crease line. It is a lightweight
raster model, not a simulation — no 3-D paper, no light transport. What it
reproduces is the part that changes a recognition outcome.

**It is applied to the composed sheet** — printed artwork, registration marks
and the candidate's own marks together — *after* everything is on the page and
*before* the scanner's own blur, noise and compression. Paper does not fold
selectively.

### Severity

| Class | Depth, as a fraction of the page |
|---|---|
| Micro | 0.3 – 1.5 % |
| Small | 1.5 – 3 % |
| Moderate | 3 – 7 % |
| Severe | 7 – 12 % |

Depths are fractions, so a "small" fold is the same physical thing at 150 and
600 dpi. **The two axes are independent** — a fold is almost never a 45° 
triangle, and a dataset of identical isoceles corners would test one shape
thoroughly and every other shape not at all.

Micro folds can be only a few pixels deep at ordinary scan resolution. They are
drawn through anti-aliased masks so they survive rasterisation, and are *not*
enlarged to look better.

### Marker interaction

Computed from the template's **actual marker geometry**. There is no "corner
marker" or "side marker" concept anywhere: every registration marker and the
orientation mark become polygons, and the question "is this marker affected"
becomes "do these two polygons intersect, and by how much". A template whose
registration mark sits well along an edge is handled by the same arithmetic,
with no new type.

Every affected marker is recorded **individually, with its own overlap
fraction** — never one boolean for "a marker was hit".

The generator aims to include one of each:

| Interaction | Meaning |
|---|---|
| `no_marker` | Reaches nothing. The commonest real fold |
| `marker_touch` | Clips a marker (≥ 0.5 %) |
| `partial_marker` | ≥ 5 % of one marker |
| `substantial_marker` | ≥ 45 % |
| `full_marker` | ≥ 90 % |
| `two_markers` | Two markers each ≥ 5 % under one fold |

**An interaction this template's geometry cannot produce is skipped, never
faked.** Two markers under one fold needs two markers near one corner; on most
sheets only the top-left corner has that (a registration marker plus the
orientation mark), and the other three corners correctly report the case as
not applicable.

### Expected failures

A fold is allowed to assert an outcome in exactly one case: when a
**registration** marker is ≥ 90 % covered. At that point there is nothing at
the corner to detect, which is the same situation the dataset already treats as
an expected refusal when a marker was never printed. That is a *geometric*
statement, not a calibration one.

Below it the dataset deliberately asserts **nothing**. A fold covering a third
of a marker may or may not still register, and which of those happens is a
measurement of the detector rather than a property of the sheet. Those sheets
carry their interaction tag and no expectation, so a benchmark reports them as
their own category instead of scoring them. Read a result in that band as a
calibration measurement.

### Several corners

`Max per sheet` allows 1–4; the default is 1, because more than one folded
corner on the same page is genuinely uncommon. Folds are computed independently
against the page rather than against each other's output, and three or more on
one sheet are capped at **moderate** so they cannot between them consume the
page.

### Metadata

Per sheet, in `ground_truth/SYN_*.json` under `metadata.physical_augmentation`:

```json
{
  "corner_folds": [
    {
      "corner": "top_left",
      "severity": "severe",
      "depth_x": 0.0797,
      "depth_y": 0.1126,
      "crease": [[0.0797, 0.0], [0.0, 0.1126]],
      "affected_markers": [
        {"marker_id": "top_left", "marker_type": "registration",
         "overlap_fraction": 1.0},
        {"marker_id": "orientation", "marker_type": "orientation",
         "overlap_fraction": 0.052}
      ]
    }
  ],
  "marker_interaction": "two_markers",
  "registration_marker_lost": true
}
```

The crease is normalised so the record means the same thing at any resolution.
Nothing about a fold is encoded in a filename. The manifest's
`generator.folds` records the policy, and is `null` when nothing was folded.

**The logical ground truth — answers, roll number, set code, attendance — is
unchanged by folding.** A fold changes the paper and nothing else.

### Both rendering modes

Folding works with a template-rendered page and with a real reference scan. In
the reference mode the fold follows the page's own corner through the
registration homography Checkpoint A already computed, so it lands on the
*paper* rather than on the image corner — including when the sheet was fed
crooked or at a different scale on each axis. There is no second registration
path and no single scalar standing in for two axis scales.

### Command line

```powershell
python -m omr_scanner.tools.make_dataset D:\OMRFlow-Folded `
    --template examples\templates\ece_0000_sample.omrt `
    --count 500 --folds --fold-frequency 0.2 `
    --fold-corners top_left top_right --fold-severity random
```

| Option | Effect |
|---|---|
| `--folds` | Enable corner folds. Off without it |
| `--fold-frequency` | Fraction of sheets to fold, e.g. `0.2` |
| `--fold-corners` | Which corners are eligible. All four when omitted |
| `--fold-severity` | `micro`, `small`, `moderate`, `severe` or `random` |
| `--fold-max-per-sheet` | 1–4; default 1 |
| `--no-fold-coverage` | Fold purely at random, with no guaranteed cases |

As with the reconciliation conflicts, the deliberate coverage cases are filled
first, so a small dataset may exceed the requested frequency rather than omit a
case it claims to contain.

## Colour

| Mode | Output |
|---|---|
| `grayscale` (default) | One channel, full range. What the generator has always produced |
| `color` | Three channels, BGR. Degradations act per channel, as on a real colour scan |
| `bw` | One channel holding only 0 and 255, thresholded by Otsu's method |

Colour is applied in two places on purpose, because that is the order a scanner
works in: the channel layout is chosen **before** degradation, so noise and
blur act per channel; bilevel quantisation happens **last**, so the blur cannot
put back the grey levels a one-bit scan does not contain.

**`bw` is expected to lose faint marks.** A bilevel scan has thrown away the
grey levels a light pencil lives in before recognition ever sees the page. That
is the honest behaviour of the setting, not a defect in the engine — treat a
`bw` dataset's faint-mark results accordingly.

## Where it is

**Tools → Developer / Testing → Generate Synthetic Test Dataset…**

The dialog has five sections: the source and destination (template, **render
mode**, **reference scan**, output folder), the image contents (profile, case
families, count, seed), **Attendance and reconciliation**, **Physical page
deformation** (corner folds), and the image/output settings (format,
**colour**, resolution). The last two start folded away.

Choosing **Real scanned sheet + synthetic markings** enables the reference-scan
field and disables **Resolution (dpi)**, which no longer applies. **Generate**
is refused until a reference scan is chosen and exists. Whether it *registers*
is reported by the run itself, immediately, before any sheet is written — the
dialog cannot check that without importing the imaging layer, which the
architecture forbids it.

The reference scan is an input to the run, not a saved preference: nothing
persists it between runs. Browse opens on the current project's folder.

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
| `--render-mode` | `template` (default) or `reference_scan` |
| `--reference-scan` | The blank scan to lay marks on. Required by `reference_scan` |
| `--color-mode` | `grayscale` (default), `color` or `bw` |

Set codes are never assumed to be a single character.

Rendering onto a real blank scan:

```powershell
python -m omr_scanner.tools.make_dataset D:\OMRFlow-RealPaper `
    --template examples\templates\ece_0000_sample.omrt `
    --render-mode reference_scan `
    --reference-scan D:\scans\blank_form.png `
    --color-mode color `
    --count 500
```

`--dpi` is accepted but not applied in that mode, and the manifest records the
resolution as `null` rather than echoing back a setting that was never used.

## Memory and large datasets

Generation is streaming: one sheet is rendered, encoded, written and released
before the next begins. A hundred thousand sheets costs one page of memory, not
a hundred thousand. Roughly 60 GB of disk at A4/300 dpi, so check the
destination before starting a run that size.

### Why it stays bounded, including in the reference-scan mode

Structurally, what a run holds at any moment is:

| Held | How many | For how long |
|---|---|---|
| The planned cases | all of them | the run — but they are plain data (marks, flags, a seed), not pixels |
| The rendered image | **one** | rendered → encoded → written → released, inside one loop iteration |
| The manifest rows | all of them | the run — file names and tags, no arrays |
| The reference scan | **one**, in reference mode only | the run |
| The mark layer | **one** | one loop iteration |

`generate_dataset` never appends a `GeneratedSheet` or an image array to a
list. `entries` holds file names, `rows` holds strings. The reference scan is
loaded and registered **once, before the loop**, and reused — it is the one
thing deliberately kept for the whole run, and the second page of memory the
reference mode costs over the template mode. Two pages, not two per sheet.

This is asserted rather than assumed, and without a fragile resident-set
measurement. `tests/integration/test_real_scan_dataset.py` holds a `weakref` to
every image the generator produces and checks, after the run returns, that none
of them is still reachable — which is exactly the property "does not accumulate
images" means, and it is checked in both rendering modes. The same file proves
the reference is decoded once and aligned once per job, however many sheets are
generated.

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

- **The marks are still synthetic in both modes.** A real candidate's pencil is
  not an ellipse. The reference-scan mode makes the *page* real; it does not
  make the dataset a substitute for scans of real human-filled sheets.
- **Marker and orientation damage is not available in the reference-scan
  mode.** Those cases are dropped, along with their tags — see *What the second
  mode cannot do* above.
- **A reference scan is not checked for being blank.** A scan that already has
  marks on it will have more marks composited over them, and the ground truth
  will describe only the ones this generator drew.
- **Corner folds are a raster model, not a simulation.** No 3-D paper, no
  bending stiffness, no light transport. The flap is flat, the shadow is a
  blurred rim, and the crease is a line. It reproduces what changes a
  recognition outcome, not what a photograph would look like.
- **Only corner folds.** A crease across the middle of a sheet, a curled edge
  or a torn page are not modelled.
- **A fold between "touched" and "substantial" asserts no outcome**, by
  design — see *Expected failures* above. A benchmark result in that band is a
  calibration measurement, not a pass or a fail.
- **No preview, no resume, no debug overlays**, and no disk/time estimate
  before a large run.
- **No single-sheet reproduce command.** A sheet is reproducible from the seed
  by regenerating the dataset, but there is no `--reproduce --sheet N`.
- **Attendance conflicts are not combined with each other.** One per candidate,
  by design; combined defects would need a named stress case.
- **The workbook is generated, not copied from a project's own attendance
  template.** Preserving a user-supplied workbook's formatting, logos and
  merged cells is not implemented.
