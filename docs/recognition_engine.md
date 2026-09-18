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

`python -m omr_scanner.tools.make_dataset` renders labelled sheets **from a real
template**, so a dataset exercises the coordinate mapping the engine actually
uses:

```bash
python -m omr_scanner.tools.make_dataset out/dataset \
    --template examples/templates/ece_0000_sample.omrt \
    --count 24 --profile mixed --seed 20260918
```

```text
out/dataset/
├── images/SYN_000001.png ...
├── ground_truth/SYN_000001.json ...
└── manifest.json          seed, profile, count, generator version
```

| Profile | What it produces |
|---|---|
| `clean` | No distortion. Anything failing here is a bug, not a tolerance question |
| `normal` | A decent office scanner: ±2°, mild exposure variation, light noise |
| `difficult` | A tired photocopier: skew, perspective, uneven illumination, blur, JPEG, and marks in every style candidates actually use |
| `stress` | At or past the documented limits, including a missing marker or a cropped page |
| `mixed` | A realistic batch: mostly normal, some difficult, a few broken |

Mark styles (`imaging.synthetic.MarkStyle`): a shaded disc, a circled bubble, a
tick, a cross, a scribble, a stray dot - each with its own coverage, darkness,
offset and size. Real candidates do not all shade neatly inside the ring, and an
engine that has only ever been tested on concentric discs has been tested on the
easy case.

**Reproducibility.** The same seed, count, profile and template give the same
dataset, byte for byte. Each sheet derives its own seed, so sheet 7 can be
regenerated alone and growing a dataset does not reshuffle the sheets already
in it.

**Ground truth is generated, never inferred.** Each document records what was
drawn, which defects were injected and with what parameters, and which
questions were deliberately marked *borderline* (`ambiguous`) - for those,
flagging the question is correct behaviour and is scored as such.

> Synthetic accuracy is not real accuracy. These pages have clean geometry,
> even paper and marks drawn by arithmetic. They are excellent at catching
> regressions and coordinate bugs, and nearly useless as evidence that a
> threshold is right for real pencil on real paper.

---

## 8. Benchmarking

```bash
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --report out/benchmark --workers auto
```

```text
out/benchmark/
├── summary.json    dataset-level metrics
└── errors.csv      one row per disagreement
```

Metrics: scans processed and failed, roll and set exact-match accuracy,
question accuracy, blank-detection accuracy, multiple-mark accuracy, how
borderline marks were handled, flagged questions, accuracy by decision-score
band, and timing.

Every disagreement is **classified**, because "94% accurate" is not actionable:

| Category | What it usually means |
|---|---|
| `FALSE_MARK` | Threshold too low, or ink bleeding from a neighbour |
| `FALSE_BLANK` | Threshold too high, or a mark that covers little of the bubble |
| `WRONG_OPTION` | A geometry bug - one column out - not a threshold problem |
| `MISSED_MULTIPLE_MARK` | A candidate's second mark silently discarded. The dangerous one |
| `FALSE_MULTIPLE_MARK` | Noisy, but safe: it is flagged |
| `ROLL_ERROR`, `SET_ERROR` | The identifier or set code did not match |
| `ALIGNMENT_ERROR` | Should have registered and did not, or vice versa |
| `PROCESSING_FAILURE` | Could not be processed at all |

### Comparing against a baseline

```bash
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --baseline out/benchmark_before/summary.json
```

Each metric is reported as `improved`, `unchanged` or `regressed`, with a
default tolerance of 0.5 percentage points - small datasets move by more than
that when one sheet differs, and a comparison that shouts every run is ignored
within a week.

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

`local_test_data/` is where a real corpus goes. Everything inside it is
git-ignored; only its README is committed. It uses the same layout and the same
ground-truth schema as a synthetic dataset, so the same benchmark command works
on either. Nothing is uploaded; no network request is made anywhere in
recognition, benchmarking or reporting.

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
- **Line-shaped marks are under-read.** Measured on a synthetic mixed dataset:
  ticks produced a false-blank rate of about 33% and circled bubbles about 8%,
  against 0% for crosses and scribbles, because a tick covers little of the
  bubble's interior and the measurement is coverage-based. Whether real
  candidates' ticks behave the same way, and whether the fix is a darkness term
  or a lower threshold, is exactly what the real corpus is for.
- **Registration limits** are Phase 1's: ±15° plus quarter turns, illumination
  gradients to about 0.45, roughly 5% margin cropping.
