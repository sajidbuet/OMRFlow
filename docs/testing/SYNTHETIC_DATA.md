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
├── solution/                    the answer key, one set at a time
│   ├── Set_10_Solution.png      a clean solution OMR sheet for set 10
│   ├── Set_10_Answer_Key.txt    the same key as the Answer Key stage reads it
│   ├── Set_10_Solution.json     the solution sheet's own ground truth
│   └── ...                      (the same three for every set)
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

#### Check your reference scan against a real batch scan first

The run above produced two flagged answers on bubbles that should have been
plainly empty. Measured with the engine's own metric, that reference scan's
*empty* bubbles read a mean fill ratio of **0.22** — above the template's
`blank_ratio_threshold` of 0.25 on a third of them.

That looks like a calibration problem with the template, and it is not.
Measuring the **same form's real, human-filled batch scans** through the same
pipeline gives a completely different picture:

| Measured on | Empty bubbles | Marked bubbles | Ink threshold |
|---|---|---|---|
| The reference scan | mean 0.22, max 0.32 | — | 79 |
| Real batch scans | ~0.00 | 0.95 – 1.00 | 12 – 27 |

Across 4,000 bubbles on eight real sheets the separation is total: 3,274 read
blank, 725 read marked, and **one** lands in between. The template's
thresholds are well placed; nothing needs changing.

The difference is the reference image itself. Bubble measurement adapts its ink
threshold to the page it is given, and that reference scan's darkest content is
grey 114 — it contains no true blacks at all, where the batch scans reach 56.
So the adaptive threshold lands at 79 instead of ~20, and the printed option
letters start counting as ink.

**The practical rule:** a reference scan should come off the same scanner at the
same settings as the batch it will stand in for. One number tells you whether
it does — the 1st-percentile darkness of the aligned page, which should be
close to a real scan's. A lighter reference is not useless; it produces a
*harder* dataset than reality, which is worth knowing before reading a
benchmark result off it.

If your run flags answers on bubbles that should be empty, measure a real batch
scan before adjusting a threshold.

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

The dialog has six sections: the source and destination (template, **render
mode**, **reference scan**, output folder), the image contents (profile, case
families, count, seed), **Attendance and reconciliation**, **Answer keys and
candidate performance** (solution sheets, score distribution), **Physical page
deformation** (corner folds), and the image/output settings (format,
**colour**, resolution). The last three start folded away.

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

## Answer keys and solution sheets

Every generated dataset has a `solution/` folder at its root. For every set it
holds:

| File | What it is |
|---|---|
| `Set_<code>_Solution.<png\|jpg>` | A **solution OMR sheet**: the examiner's key, filled in on the real template — same page, registration and orientation marks, bubble geometry, fill renderer, colour mode, resolution and (in the reference-scan mode) the same blank scan as every candidate's script. Every question carries the key's answer; the set code is marked; the Student ID is **left blank** |
| `Set_<code>_Answer_Key.txt` | The same key as **text**, in the format OMRFlow already reads |
| `Set_<code>_Solution.json` | The solution sheet's ground truth, with `metadata.role = "solution"` and the key |

`<code>` is the set code made safe for a file name (the same shape as
`attendance/Set_<code>_Attendance.xlsx`); the unaltered code is in the manifest.
Two set codes that would sanitise to the same file name are refused before
anything is written.

### One canonical key

```text
SyntheticAnswerKey (one per set, drawn once)
        │
        ├── solution OMR sheet     drawn by the ordinary renderer
        ├── answer-key .txt        serialised
        ├── manifest               generator.answer_keys / generator.solutions
        └── candidates' answers    decided against it (below)
```

Nothing re-draws the answers for any of these, so they cannot disagree. The
test suite checks all four against each other, and runs OMRFlow's recognition
engine over every solution sheet to confirm it reads back as its set code and
its key.

### The text format

OMRFlow has no answer-key *file* format: keys are typed or pasted into the
Answer Key stage's **Answers** field and parsed by
`omr_scanner.services.answer_key.read_key` — one option label per question, in
question order, with whitespace, `,`, `;` and `|` ignored and anything else
reported. The `.txt` file is exactly that string, uppercased, on one line,
UTF-8, terminated by a single `\n`:

```text
BDBDBADACBAABCBACABB
```

So it can be pasted into the stage unchanged, and it round-trips through
`read_key` in the test suite. **The set code is not in the file** — `read_key`
would report it as a stray character — it is in the file name and the
manifest. Labels are always single characters; a template whose options are
longer is refused, as is one the Answer Key stage itself would refuse
(non-contiguous numbering, disagreeing option labels, an option spelled `_` or
`?`). A template with no question regions has no key, and writes no
`solution/` files.

### Why the Student ID is blank

The Answer Key stage reads a solution sheet's answers and set code and nothing
else, so no identifier is needed. A blank one is also the safest: a solution
sheet scanned into a candidate batch by mistake resolves to no roll at all and
is flagged, instead of matching somebody. Solution sheets live only in
`solution/` — never in `images/`, `manifest.csv`, `manifest.entries` or the
reconciliation ground truth.

### Clean by default

No rotation, blur, noise, folds or marker damage: a solution sheet is a
reference. Degrading solution sheets on request is **not implemented**.

### Set codes the template cannot show

A roster's set codes and a template's set-code field are chosen separately, and
existing datasets pair `10, 11, 12` with an `A`–`D` field. The candidate sheets
have always carried a blank set code in that case. The solution sheet does the
same rather than drawing some other set, logs a warning, and records
`"set_code_marked": false` in the manifest. Multi-character codes are marked
whenever the field can spell them: `103` on three digit positions, or `10` as
one bubble on a single-position field whose symbols are whole codes.

### Which sets get a key

With attendance on, the roster's sets. Without it, the sets the planned sheets
actually print; a sheet whose set code is a deliberate test case (blank, double
marked) is assigned one of those papers from its own seeded stream.

### Candidates answer their own set's key

Candidate answers used to be drawn independently of any key, so every
candidate scored about 25 % on a four-option paper. Now:

```text
target fraction  ~ truncated normal (mean, SD) on [minimum, maximum]
target correct   = round(fraction × questions), clamped
exactly that many questions, chosen uniformly over the whole paper → the key's answer
every other question → a uniformly chosen option that is not the key's
            ↓
the case plan's own test conditions laid over it
            ↓
rendered
```

- **Truncation** is by inverse transform — `u` uniform on `[F(a), F(b)]`,
  returned as `F⁻¹(u)` — using only `statistics.NormalDist`. Exact, one draw per
  candidate, no rejection loop, and no spike of probability mass at the bounds
  that clamping would create.
- **Exact per candidate.** Before any test condition, the number of answers
  equal to the key *is* the target. Rolling correctness per question would
  instead give a binomial spread fixed by the question count (≈ 4.8 % at 100
  questions), not the configured SD.
- **The key is the candidate's own.** With a roster, the paper the candidate is
  registered for — deliberately *not* the set code marked on the sheet, which is
  a staged conflict for some. Never another set's key.
- **Test conditions survive.** The case planner fills the questions it is not
  testing with `SheetBuilder.answer_all`, which marks those plans `free`. Only
  free plans take the intended option, and only their option changes — style,
  fill, offset and size stay. Blanks, double marks, faint and erased marks, the
  first-option / last-question cases and the intensity sweep are exactly as
  before.
- **Recorded per sheet.** `ground_truth/SYN_*.json` →
  `metadata.performance`: `answer_key_set`, `target_fraction`,
  `target_correct`, `scored_questions` and `intended_answers`. With the key
  from the manifest, that separates *intended answer*, *correct answer* and
  *rendered state* for every question. `answers` remains the rendered state,
  as before. The manifest's `generator.performance.observed` gives the cohort's
  N, mean, median, SD, minimum, maximum and a ten-bin histogram.
- **Fully random (legacy)** skips all of this, and reproduces the pre-key
  answers exactly for the same seed.

No question difficulty, candidate ability or item-response model: every
question is equally likely to be one a candidate gets right.

### Settings

| | Dialog | Command line | API |
|---|---|---|---|
| Solutions | **Generate solution sheets and answer keys** (on) | `--no-solutions` | `generate_solutions=True` |
| Distribution | Normal (Gaussian) / Fully random (legacy) | `--performance-distribution normal\|random` | `PerformancePolicy(distribution=…)` |
| Mean | 65 % | `--mean-correct 0.65` | `mean=0.65` |
| SD | 15 % | `--sd-correct 0.15` | `stddev=0.15` |
| Minimum / maximum | 0 % / 100 % | `--min-correct 0` / `--max-correct 1` | `minimum=0.0`, `maximum=1.0` |

One default everywhere (`omr_scanner.evaluation.performance.DEFAULT_PERFORMANCE`).
`0 ≤ minimum ≤ mean ≤ maximum ≤ 100 %` and `SD ≥ 0` are enforced in all three,
before anything is written.

### Reproducibility and isolation

The keys use `Random(f"{seed}:answer-key:{set code}")` and the performance
model `Random(f"{seed}:performance")`, following the fold planner's
`f"{seed}:folds"`. So:

- the same seed gives byte-identical solution files;
- switching solutions off leaves every candidate image and ground-truth file
  byte-identical — the keys are drawn either way;
- adding a set leaves the other sets' keys unchanged;
- changing the performance settings moves answers and nothing else — rolls,
  set codes, tags, degradation and the solution files are unchanged.

A stale `Set_*_Solution.*`, `Set_*_Answer_Key.txt` from an earlier run into the
same folder is removed first (even with solutions off), so a four-set run
followed by a three-set run does not leave `Set_4` looking valid. Other files
in `solution/` are left alone.

### Validation run

3 sets, 100 questions (`ece_0000_sample.omrt`), 500 registered candidates, seed
20260929, default settings — 469 scripts after absentees and missing scans:

| | Configured | Truncated-normal expectation | Observed |
|---|---|---|---|
| Mean | 65 % | 64.6 % | 64.4 % |
| SD | 15 % | 14.5 % | 15.3 % |
| Median | | | 65 % |
| Range | 0–100 % | | 16–100 % |

```text
 0-10%  |
10-20%  | 1
20-30%  |###  6
30-40%  |##########  24
40-50%  |####################  47
50-60%  |########################################  96
60-70%  |############################################  105
70-80%  |###############################################  114
80-90%  |######################  54
90-100% |#########  22
```

Scored against the `.txt` keys with OMRFlow's own `score_answers`, every
candidate's intended answers scored exactly their `target_correct` (469/469),
and all three solution sheets were read by the recognition engine as their set
code and key.

### Through the Answer Key and Results stages

Validated on 2026-09-29 in the real application window
(`tests/gui/test_generated_key_workflow.py` repeats it on every run). The
stages were driven through the window's own methods, offscreen, with the same
files an operator would pick; native file choosers and confirmation boxes were
the only things not clicked.

| | |
|---|---|
| Dataset | seed 20260930, sets `10` `11` `12` (two-digit set field, so every code is physically marked), 24 questions, 45 candidates, PNG grayscale, families *baseline* + *answers*, no attendance conflicts, normal 65 % ± 15 % |
| Scan | 45 read, 0 conflicts |
| Attendance | Each `attendance/Set_<code>_Attendance.xlsx` imported for its own set; 15 matched per set |
| Answer Key | Set 10 pasted from `Set_10_Answer_Key.txt`; Sets 11 and 12 read with **Read From Solution Sheet…** from `Set_<code>_Solution.png`; each 24/24, valid, saved and verified with no editing |
| Keys | manifest = `.txt` = verified key, for all three sets |
| Results | 45 scored, 0 blocked, 15 per set; every mark from the candidate's own set's key |
| Reopen | All three keys still verified at revision 1; results unchanged, none stale |

**Marks.** The correct-answer count Results shows equals the ground truth's
(the rendered sheet scored against its own set's `.txt` key) for **44 of 45**
candidates, and for all 9 sheets with no answer test condition it also equals
the drawn target exactly. The one difference is the deliberate
`UNDERSIZED_MARK` sheet, whose half-size mark the engine did not accept as a
single answer; scoring counts an undecided answer as a multiple, so that
candidate has one correct answer fewer. Everywhere else the gap between the
intended target and the mark is the test condition itself — an all-blank sheet
scores 0, a sheet answering only its first or last question scores 0 or 1, a
double mark is a multiple.

| | Configured | Intended (ground truth) | Rendered (ground truth) | Results |
|---|---|---|---|---|
| Mean | 65 % | 62.7 % | 51.7 % | 51.6 % |
| SD | 15 % | 10.4 % | 22.6 % | 22.6 % |
| Median | | 62.5 % | 58.3 % | 58.3 % |
| Range | 0–100 % | 41.7–87.5 % | 0–83.3 % | 0–83.3 % |

The intended SD at N = 45 is a genuinely low draw for this seed (its raw
uniform draws have SD 10.2 %), not a defect: 20,000 draws on the same template
give mean 64.6 % and SD 14.5 %, the truncated-normal expectation. The rendered
and Results columns are lower and wider because six of the 45 sheets are
blank-dominated test cases.

**Which key.** Candidate `10000016` (Set 10) scores 20 against its own key and
5 or 10 against Sets 11 and 12's; Results shows 20.

**Defects found and fixed on the way** — the project template not reaching
the scoring stages, Results unable to score per-set attendance or to see a list
imported during the session, a solution sheet read under the wrong set without
warning, and the key editor carrying the previous project's text. See
`CHANGELOG.md`.

**Logical set codes the template cannot spell** (`10` on an `A`–`D` field)
remain a limitation: the solution sheet reads back as its key with no set, the
operator must choose the set, and nothing downstream fails. No
logical-to-physical set mapping exists in OMRFlow.

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
| `--no-solutions` | Do not write `solution/`. The keys are still drawn |
| `--performance-distribution` | `normal` (default) or `random` (legacy) |
| `--mean-correct`, `--sd-correct` | Score distribution, as fractions (`0.65`, `0.15`) |
| `--min-correct`, `--max-correct` | Truncation bounds, as fractions (`0`, `1`) |

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

## Intake qualification cohort (revised phase 9)

The revised phase 9 intake qualification harness
(`omr_scanner.evaluation.intake_qualification`, documented in
[`docs/intake_qualification.md`](../intake_qualification.md)) does not use the
`generate_synthetic_omr` dataset layout above. It plans its own cohort in
`intake_qualification/cohort.py` and renders it with the same `SheetBuilder`
and distortion code:

- The plan is a pure function of the campaign configuration and the template
  (seed `20261006`, timing seed `6102026`). Every content item (script,
  rescan, folded page, blank page), every arrival (source, per-source filename,
  write pattern, time) and every scripted operator task is fixed by the plan,
  and its SHA-256 digest is recorded in the manifest.
- Rescans are rendered with a small page offset so their bytes differ from the
  original; the renderer refuses a pool in which two different contents have
  identical bytes. Byte duplicates are deliberate copies of an already planned
  file, written by the same or a different writer.
- Rendered images live only under `Scratch/Qualification/...` and are never
  committed. The committed evidence keeps the plan digest, the manifest digest
  and the reports, from which the cohort can be regenerated.
- The ground truth (expected scores, ranks, statuses, conflicts and
  replacement chains) is computed from the plan by
  `intake_qualification/reference.py`, independently of the application's
  scoring and reconciliation code.

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
- **Solution sheets are always clean.** There is no option to degrade them.
- **The answer-key text file carries no set code or header.** It is the bare
  answer string the Answer Key stage reads; the set is in the file name and the
  manifest. Wrong questions (full credit) are never generated.
- **Candidate performance has no question difficulty or candidate ability.**
  Every question is equally likely to be answered correctly.
- **A set code the template's set-code field cannot spell** is left blank on
  the solution sheet (and, as before, on the candidates' sheets) rather than
  refused; the manifest says so.
