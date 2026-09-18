# The recognition engine (Phase 3)

How to call recognition, what it gives back, how to see what it did, and how to
measure whether it is any good.

This is the developer's document. For the user-facing workflow see
[`scan_workflow.md`](scan_workflow.md); for the pixel algorithms see
[`IMAGE_PROCESSING.md`](IMAGE_PROCESSING.md).

---

## 1. The idea in one paragraph

Recognition is a **replaceable subsystem**. Everything above it - the Scan
page, the batch processor, and Phases 4 to 9 when they arrive - depends on two
things only: a way to ask "read this image with this template", and the shape
of the answer. Neither mentions OpenCV, thresholds, contours or bubbles. When
Recognition Engine v2 arrives with better thresholds and a different
measurement, it replaces what is *below* that line and nothing above it has to
change.

```text
    GUI  /  batch processor  /  Phase 4 review  /  Phase 8 scoring
                              │
                              │  RecognitionEngine.process(path, template)
                              ▼
                      RecognitionEngine  (services/recognition_service.py)
                              │
        ┌─────────────┬───────┴────────┬──────────────┬─────────────┐
        ▼             ▼                ▼              ▼             ▼
   load image    registration    bubble measurement  decision   diagnostics
  (services)     (imaging)        (imaging.metrics) (recognition) (services)
                              │
                              ▼
                         ScanResult          ← the contract
                 (services/recognition_models.py)
```

The boundary is enforced, not merely described:
`tests/unit/test_architecture.py` fails the build if the GUI imports `cv2`,
`numpy`, `imaging` or `recognition`, and
`tests/integration/test_recognition_engine.py` starts a fresh interpreter to
prove a sheet can be read with no Qt module loaded at all.

---

## 2. Calling it

```python
from pathlib import Path
from omr_scanner.services import RecognitionEngine, RecognitionOptions, load_template

template = load_template(Path("sheet.omrt"))
engine = RecognitionEngine(RecognitionOptions(with_preview=False))

result = engine.process(Path("scan.png"), template)
result.identifier_value        # "2103123"
result.answer(17).value        # "B", "" or "B-D"
result.status_codes            # ("ALIGNMENT_WARNING",)
```

One engine reads many sheets: it holds no mutable state between calls, which is
what makes it safe to share across threads and to pickle into a worker process.

`recognise_scan(path, template, ...)` is the same operation as a function and
remains supported - most callers read one sheet, and it is the spelling the
rest of the repository already uses.

### Options

`RecognitionOptions` (`services/recognition_settings.py`) covers what the
*engine* does. It deliberately contains no thresholds:

| Option | Default | What it changes |
|---|---|---|
| `metrics` | `BubbleMetricsConfig()` | Where inside the ring a bubble is sampled, how local paper is estimated |
| `with_preview` | `True` | Produce a display image of the rectified page (batches turn this off) |
| `preview_max_dimension` | `1400` | Longest side of that preview |
| `keep_bubble_measurements` | `True` | Keep the per-bubble evidence on the result |
| `keep_quality_metrics` | `True` | Measure brightness, contrast, sharpness, skew |
| `diagnostics` | off | Staged debug images; see §5 |

**Thresholds live in the template**, in `RecognitionSettings` inside the
`.omrt`: how dark a mark must be and how far ahead of its runner-up are
properties of a *sheet design* and its print quality, not of the machine
reading it. Putting them in an application setting would create two sources of
truth and let a preference silently change a recognised answer.

---

## 3. What comes back

`ScanResult` (`services/recognition_models.py`) is plain data - strings,
numbers, booleans and tuples of the same. No NumPy, no Qt, no OpenCV.

```text
ScanResult
├─ identity      source_path, engine_name, engine_version, recognised_at,
│                template_id, template_name, template_version
├─ outcome       outcome, registration, registration_message, warnings,
│                error_code, status_codes
├─ values        fields[] (→ characters[]), answers[],
│                identifier_zone_id, set_code_zone_id
├─ evidence      bubbles[]  one per measured bubble, with its numbers
├─ geometry      zones[], markers[], canonical/source dimensions
├─ quality       ScanQuality: markers, skew, rotation, perspective,
│                brightness, contrast, sharpness ...
└─ timing        StageTimings: load, register, measure, decide, present, total
```

### Two versions, moving independently

- `ENGINE_VERSION` (`"1.0"`) identifies the *behaviour* that produced a result.
  It changes when the engine would read the same sheet differently.
- `RESULT_SCHEMA_VERSION` (`1`) identifies the *shape* it was written in. It
  changes when the serialised form does.

Neither is the application's release number, because a benchmark comparing two
engines is meaningless if the version also moves when the About box does.

### Stability promise

Within a schema version, fields are **added, never removed or repurposed**, and
every added field carries a default. A consumer written against an older
version keeps working; a document written by an older version still loads.
`ScanResult.from_dict` refuses a *newer* schema version rather than
misinterpreting it.

### Per-bubble evidence, and why it is kept

Every `BubbleView` carries the numbers behind the decision, not just the
decision:

```json
{
  "zone_id": "questions_2", "row": 3, "column": 1, "label": "B",
  "fill_ratio": 0.78, "selected": true, "leading": true, "rank": 0,
  "mean_darkness": 0.63, "contrast": 0.41,
  "paper_level": 247.0, "ink_threshold": 174.5,
  "sample_pixels": 188, "usable": true,
  "group_status": "resolved"
}
```

This is what makes a future recalibration cheap: given `fill_ratio` and
`ink_threshold` for every bubble of a batch, "what would a threshold of 0.6
have decided?" is arithmetic over stored JSON, not a re-run of the whole
image pipeline. It is also what a conflict-review screen needs to show a human
*why* something was flagged.

Set `keep_bubble_measurements=False` to drop it; the decision is unaffected.

### Status codes

`status_codes` is the machine-readable summary a consumer should branch on -
never the English message, which is free to be rewritten or translated. A
result carries *several* at once, because a sheet is routinely several things
at the same time:

```text
OK · LOW_CONFIDENCE · BLANK · MULTIPLE_MARK · AMBIGUOUS
ALIGNMENT_WARNING · ALIGNMENT_FAILED · ORIENTATION_FAILED · MARKER_NOT_FOUND
ROLL_UNREADABLE · SET_UNREADABLE
INVALID_TEMPLATE · IMAGE_LOAD_ERROR · PROCESSING_ERROR
```

They are *derived* from the values, in `derive_status_codes`, so they can never
disagree with the result they describe.

### Failures are results, not exceptions

An unreadable file, an unregistrable page and an unexpected internal error all
come back as a `ScanResult` with an outcome, a status code and a message,
because a batch of two hundred sheets must survive any one of them. A failed
result is as fully described as a successful one - same engine stamp, same
timestamp, same template identity - so a later phase never has to special-case
it.

The exception hierarchy in `errors.py` is unchanged: `ImagingError` and its
subclasses still carry stable codes, and `IMAGING_ERROR_STATUS` maps them onto
status codes at the boundary.

### JSON

```python
payload = result.to_dict()          # JSON-safe plain data, no preview image
restored = ScanResult.from_dict(payload)
```

Used by the headless tools, the benchmark harness, the diagnostic dumps and the
committed fixtures in `tests/fixtures/recognition/`.

---

## 4. Scan quality

`result.quality` reports what the scan looked like: marker scores,
reprojection error, the rotation and skew that were corrected, perspective
strength, brightness, contrast, sharpness, and the page's share of the image.

**None of it decides anything.** A sheet is failed only when it genuinely
cannot be processed, never because a quality number crossed a threshold nobody
has validated. The metrics exist so that a batch of a thousand can be sorted by
"which scans look worst", and so that a future calibration can ask whether an
error correlates with blur rather than with a threshold.

---

## 5. Diagnostics

Off by default. When enabled, each scan gets its own folder:

```text
diagnostics/
└── scan0042/
    ├── 00_original.png       the scan as loaded
    ├── 01_registered.png     the rectified page
    ├── 02_overlay.png        zones, selections, flags
    ├── 03_measurements.png   every bubble, annotated with its fill ratio
    └── result.json           the full ScanResult
```

```python
from omr_scanner.services import DiagnosticsOptions, RecognitionOptions

options = RecognitionOptions(
    diagnostics=DiagnosticsOptions(
        enabled=True, directory=Path("out/diagnostics"), failures_only=True
    )
)
```

or from the GUI: **File > Settings > Diagnostics**, or from the command line
with `--diagnostics`.

Two rules hold: generating diagnostics **never changes what was recognised**
(asserted by a test), and a write failure - a full disk, a folder somebody
removed mid-batch - costs the diagnostics and not the scan.

`render_overlay(page, result)` draws the overlay on its own, headlessly, into a
NumPy array. A future review screen should display *that* rather than
reimplementing the drawing.

---

## 6. Headless tools

```bash
# Read one scan, or a folder, with no GUI anywhere
python -m omr_scanner.tools.recognise scans/ --template sheet.omrt \
    --json-dir out/results --overlay-dir out/overlays --workers auto

# Everything about one sheet that went wrong
python -m omr_scanner.tools.recognise scan047.jpg --template sheet.omrt \
    --diagnostics out/diagnostics --verbose
```

Exit code `0` when every sheet was read, `1` when at least one failed. The tool
prints one line per sheet and a summary; `--quiet` prints only the summary.

---

## 7. Synthetic datasets

Available two ways, from the same code: *Tools > Developer / Testing > Generate
Synthetic Test Dataset* in the application, and
`python -m omr_scanner.tools.make_dataset` on the command line. Both render
labelled sheets **from a real template**, so a dataset exercises the coordinate
mapping the engine actually uses:

```bash
python -m omr_scanner.tools.make_dataset out/dataset \
    --template examples/templates/ece_0000_sample.omrt \
    --count 250 --profile mixed --seed 20260918
```

```text
out/dataset/
├── images/SYN_000001.png ...
├── ground_truth/SYN_000001.json ...
├── manifest.json           template, seed, profile, count, DPI, format
├── manifest.csv            one row per sheet, with its tags
└── dataset_summary.json    how many sheets of each kind
```

### Resolution and format

Sheets are rendered at **150 DPI derived from the template's physical page
size** (`page.width_mm` / `page.height_mm`), not at its canonical pixel size:
A4 → 1240 × 1754 px, Letter → 1275 × 1650. That is the honest thing to do - a
real scan is whatever the scanner produced, and rendering at the canonical size
would hand the engine a page that needed no rescaling and quietly stop testing
one. `--dpi` changes it; a template with no millimetres falls back to its
canonical size and says so in the manifest.

`--format png` (the default, lossless and therefore byte-reproducible) or
`--format jpg`. JPEG quality defaults to 92: compression *damage* is its own
test case with its own tag, and should not arrive uninvited in every dataset
that happens to be written as JPEG.

### Nothing is hard-coded

Identifier length and symbols, set-code structure, question count, option
labels, bubble size and page dimensions all come from the template
(`evaluation.test_cases.FieldLayout`). A nine-digit identifier with six options
and no set code generates correctly with no code change; an optional field that
is absent simply produces no cases of that kind rather than an error.
`--describe` prints what a template offers before anything is generated.

### Named test cases, not random noise

Every sheet is a *named case* carrying tags (`evaluation.test_cases.TestCaseTag`)
that survive into the ground truth and then into the benchmark's category table.
The families (`CaseFamily`):

| Family | What it contains |
|---|---|
| `baseline` | Clean valid sheets, first/last question, first/last option, repeated-digit and boundary identifiers |
| `student_id` | Blank, partial, multiply-marked, faint, erased and offset identifier columns |
| `set_code` | Blank, multiple, faint and erased set codes |
| `answers` | Blank questions, all-blank sheets, double and triple marks, all options marked, strong-plus-weak pairs, faint, erased, offset, oversized, undersized and between-bubble marks |
| `mark_styles` | Fill, tick, cross, ring, scribble, dot, horizontal/vertical stroke, slash |
| `intensity` | A ten-step sweep across the template's own fill and blank thresholds |
| `geometry` | Mild/moderate/severe rotation, quarter turns, scale, translation, perspective |
| `cropping` | Mildly and severely cropped pages |
| `markers` | Faint, damaged, missing and extra registration markers; missing and faint orientation mark |
| `image_quality` | Blur, noise, speckle, brightness, contrast, illumination gradient, JPEG artefacts |
| `paper` | Paper tint, scanner streaks, edge shadow |
| `duplicates` | Planted duplicate identifiers: adjacent, separated, different set codes, one low-confidence |
| `mixed` | Deliberate combinations of defects that really co-occur |

Profiles select families: `baseline`, `recognition`, `degradation`, `batch`,
`stress`, `mixed` (the default), and `custom` with `--families`.

**Mandatory cases come first.** Each family emits its edge cases
unconditionally, and when a dataset is too small to hold them all the planner
takes a *spread across families* rather than a prefix - so twelve sheets of a
Mixed dataset are twelve different kinds of sheet, not twelve baselines. A
randomly sampled dataset has a real chance of containing no blank identifier at
all, and then the headline number looks fine while the case that would have
failed was never generated.

### Reproducibility

The same seed, count, profile, template, DPI and format give the same dataset,
byte for byte. The plan is pure data, so one sheet can be re-rendered on its own
without regenerating the ones before it. Generation streams: one sheet is
rendered, encoded, written and released before the next begins, so ten thousand
sheets cost one page of memory. It is cancellable, and a cancelled run writes a
manifest that says how much of the dataset actually exists.

### Ground truth is generated, never inferred

Every expected value is derived from the marks the case will draw, by one rule,
in one place (`SheetBuilder`). A generator that decided "this sheet answers B"
separately from "draw a mark on B" will eventually disagree with itself and the
benchmark will blame the engine - which has already happened once in this
repository.

Each document records the value *and* the marks behind it (`roll_marks`,
`set_marks`), the tags, the degradation parameters that were applied, the
duplicate group if any, and which questions were deliberately drawn *borderline*
(`ambiguous`) - for those, flagging is correct behaviour and is scored as such.

**Identifiers are fictional by construction**, derived from the sheet index.
Nothing resembling a real institution's numbering is ever generated, so a
synthetic dataset can be committed or shared freely.

> Synthetic accuracy is not real accuracy. These pages have clean geometry,
> even paper and marks drawn by arithmetic. They are excellent at catching
> regressions and coordinate bugs, and nearly useless as evidence that a
> threshold is right for real pencil on real paper.

---

## 8. Benchmarking

Two ways, one implementation. In the application, *Tools > Developer / Testing >
Run Recognition Benchmark* puts the existing **Scan page into benchmark mode**:
a banner, the dataset's scans in the ordinary list, and the same *Process All*
button, the same settings and the same worker pool. There is deliberately no
second processing window - a benchmark of a different pipeline would measure
nothing worth knowing.

```bash
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --report out/benchmark --workers auto --categories 0
```

```text
out/benchmark/
├── summary.json           dataset-level metrics, with the categories
├── summary.csv            the same metrics, one per row
├── errors.csv             one row per disagreement
├── category_metrics.csv   accuracy per kind of test case
└── run_config.json        dataset, template, engine, workers, settings, seed
```

In the GUI the report is written to `<dataset>/benchmark_report/`, with the
previous run kept in `benchmark_report/previous/` so every run compares itself
with the one before it.

Metrics: sheets read with nothing wrong at all, registration rate, roll and set
exact-match accuracy, question accuracy, blank-detection accuracy,
multiple-mark accuracy, how borderline marks were handled, flagged questions,
accuracy by decision-score band, and timing.

Every disagreement is **classified**, because "94% accurate" is not actionable:

| Category | What it usually means |
|---|---|
| `FALSE_MARK` | Threshold too low, or ink bleeding from a neighbour |
| `FALSE_BLANK` | Threshold too high, or a mark that covers little of the bubble |
| `WRONG_OPTION` | A geometry bug - one column out - not a threshold problem |
| `MISSED_MULTIPLE_MARK` | A candidate's second mark silently discarded. The dangerous one |
| `FALSE_MULTIPLE_MARK` | Noisy, but safe: it is flagged |
| `ROLL_ERROR`, `SET_ERROR` | The identifier or set code did not match |
| `ROLL_AMBIGUITY_MISSED`, `SET_AMBIGUITY_MISSED` | A deliberately unreadable column was resolved confidently to something never drawn. The engine did not misread a digit - it failed to notice it was guessing |
| `ALIGNMENT_ERROR` | Should have registered and did not, or vice versa |
| `ORIENTATION_ERROR` | Registration failed specifically on the orientation mark |
| `PROCESSING_FAILURE` | Could not be processed at all |

### Per-test-case categories

The reason the generator tags its sheets. A dataset that is 94% correct overall
is far more useful described as "perfect everywhere except dot-shaped marks":

```text
By test case (least accurate first):
  category                    sheets  sheet ok   answers  errors
  MARK_STYLE_DOT                   1    0.0000    0.0000     100
  MARK_STYLE_SLASH                 1    0.0000    0.0000     100
  BASELINE                        15    1.0000    1.0000       0
```

A sheet counts in full towards each of its tags rather than being divided
between them. A category made entirely of deliberate failures has no accuracy
to report and shows a dash, not `0.0000`, and sorts to the end - it behaved
perfectly, and a zero there would be a lie with a decimal point. Sheets with no
tags at all, as a real dataset will have, appear under `UNTAGGED`.

### Duplicate identifiers

Reported on their own, never folded into identifier accuracy: reading the same
number twice is **correct**, and whether the batch layer noticed is a different
question from whether the engine read the digits. A planted group counts as
detected when every sheet in it came back carrying the same identifier; a set of
sheets that share a recognised identifier without having been planted together
is an *unplanted collision*, which is a recognition error showing up as a
spurious duplicate. A group member whose identifier was drawn deliberately
unreadable is left out of the reckoning - the engine is right to decline it.

### Comparing against a baseline

```bash
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --baseline out/benchmark_before/summary.json
```

Each headline metric, and each test-case category, is reported as `improved`,
`unchanged` or `regressed`, with a default tolerance of 0.5 percentage points -
small datasets move by more than that when one sheet differs, and a comparison
that shouts every run is ignored within a week. A category present in only one
of the two runs is compared against zero rather than skipped: a family that has
silently stopped being generated is exactly what a regression comparison exists
to surface.

`run_config.json` records what produced the numbers - dataset, template, engine
version, worker count, the recognition settings in force and the dataset's own
seed and profile. A number without its conditions is not a measurement.

**Nothing here fails a build.** Whether a regression on synthetic data should
block a change depends on what changed, and encoding that here would make the
answer look objective when it is not.

### Threshold sweeps

```bash
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --sweep-fill 0.25 0.70 0.05
```

Re-scores the dataset at each fill threshold and prints the result. It is a
*hook*, not a calibration: nothing is written back into the template or the
defaults. Production thresholds must not be tuned on synthetic sheets.

---

## 9. Stored results as fixtures

`tests/fixtures/recognition/*.json` holds one serialised result per scenario -
a perfect scan, blank answers, double marks, a faint mark, an unreadable roll,
a duplicate-roll pair, an alignment warning, an orientation failure.

They exist so that **Phase 4 and Phase 5 can be built and tested without
running recognition**: load a fixture, render it, assert against it, with no
OpenCV, no template and no scan. That is also the test of this whole
architecture - if a later phase cannot be written against these, recognition is
not as replaceable as it claims.

Regenerate with `python scripts/build_recognition_fixtures.py`, and read the
diff: these files are a published contract.

---

## 10. Real datasets

`local_test_data/` and `private_test_data/` are where a real corpus goes.
Everything inside them is git-ignored; only `local_test_data/README.md` is
committed. They use the same layout and the same ground-truth schema as a
synthetic dataset, so the same benchmark command and the Scan page's benchmark
mode work on either. Nothing is uploaded; no network request is made anywhere in
recognition, benchmarking or reporting.

Writing the ground truth by hand is the whole job, and the schema is built for
it: set `human_verified` and a `reviewer`, and add `tags` describing what makes
each sheet interesting - `FAINT_MARK`, `ERASED_MARK`, `MULTIPLE_ANSWER`, and so
on from `TestCaseTag`. Those tags put a real sheet in the same category table as
its synthetic equivalent, which is where the two can finally be compared.
Sheets with no tags appear under `UNTAGGED` rather than vanishing.

**Never commit real candidate identifiers.** Synthetic datasets use fictional
identifiers derived from the sheet index precisely so that they *can* be
committed; a real one cannot.

---

## 11. What is deliberately still open

Phase 3 v1 is architecturally stable. Its *accuracy* is not settled, and these
are the questions a real dataset has to answer:

- **Thresholds are uncalibrated against real marks.** The defaults come from
  one real sheet and synthetic pages.
- **`confidence` is a decision score, not a probability.** It is bounded,
  interpretable and monotone in the evidence, and it has never been checked
  against observed correctness. The benchmark reports accuracy by band so that
  it *can* be, once there is data.
- **Line-shaped and small marks are under-read.** Measured on synthetic
  datasets, two ways. On one template, ticks produced a false-blank rate of
  about 33% and circled bubbles about 8%, against 0% for crosses and scribbles.
  On another (100 questions, default 0.55 fill / 0.25 blank thresholds), the
  mean leading fill ratio was **0.93** for a filled bubble, **0.51-0.54** for a
  horizontal stroke or a slash - inside the uncertainty band, so the engine
  flagged them - and **0.16** for a dot, below the blank threshold, so the
  engine read it confidently as blank.

  All of this follows from a coverage-based measurement, and is recorded rather
  than tuned away. Whether real candidates' ticks, dots and slashes behave the
  same way, and whether the fix is a darkness term, a stroke-aware measurement
  or a lower threshold, is exactly what the real corpus is for. The benchmark's
  category table is how the answer will be read: each mark style is its own row.
- **Registration limits** are Phase 1's: ±15° plus quarter turns, illumination
  gradients to about 0.45, roughly 5% margin cropping.
