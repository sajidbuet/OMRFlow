# OMRFlow

### Smart Mark Checker — Open-Source OMR Examination Processing

**OMRFlow** is an open-source desktop application for designing OMR templates,
processing scanned answer sheets, resolving recognition conflicts, reconciling
candidate attendance, evaluating MCQ examinations and generating auditable
examination results.

The goal is a flexible, transparent, locally operated alternative to proprietary
OMR examination-processing systems:

**ordinary image scanner + configurable template + transparent recognition +
human verification + reproducible result processing**

---

## Development Status

> **Pre-release. Phases 0-2 of 11 are complete; Phase 3 (Recognition Engine
> v1) and Phase 4 (Template Calibration & Validation) are implemented and
> undergoing testing.**
> OMRFlow manages projects, rectifies a scanned sheet into its template's
> canonical page, has an interactive designer for building that template, can
> now read the marks on a filled-in sheet, name/export the results, and file
> the processed images by roll number, and can calibrate a saved template
> against representative real scans before a batch is run. None of the Phase 3
> recognition pipeline has been validated on more than one real printed sheet.
> **Do not use it for examination processing.**
>
> **Phase 3 v1 is architecturally stabilized but recognition accuracy remains
> under active validation pending a large real-world OMR dataset. Phase 4
> makes that validation safer and more systematic to perform on whatever real
> scans an operator has - it does not perform the validation itself.**

OMRFlow is being developed incrementally, in the defined phases listed in
[`development/ROADMAP.md`](development/ROADMAP.md). Each phase is implemented,
covered with automated tests, and stabilised before the project moves toward a
production-ready release — but **implemented is not the same as complete**: a
phase is only marked complete once both its implementation *and* its required
validation are satisfied. That distinction matters most right now for Phase 3,
which is implemented and passing its automated test suite, but has not yet been
validated against a broad, real-world set of filled sheets.

| Phase | Description | Development | Testing | Status |
|---|---|---|---|---|
| 0 | Architecture & repository foundation | Complete | Complete | ✅ Complete |
| 1 | OMR geometry & alignment engine | Complete | Complete | ✅ Complete |
| 2 | Template data model & template designer core | Complete | Complete | ✅ Complete |
| 3 | Recognition Engine v1: bubble mapping, recognition, batch scanning, renaming & CSV export | Implemented & architecturally hardened | In progress | 🧪 Testing |
| 4 | Template calibration & validation | Implemented | In progress | 🧪 Testing |
| 5 | Batch scan processing pipeline (persistence, resume) | Pending | Not started | ⏳ Pending |
| 6 | Conflict detection & human resolution | Pending | Not started | ⏳ Pending |
| 7 | Candidate & attendance reconciliation | Pending | Not started | ⏳ Pending |
| 8 | Answer-key & scoring engine | Pending | Not started | ⏳ Pending |
| 9 | Result management & reporting | Pending | Not started | ⏳ Pending |
| 10 | Integration, recovery & production hardening | Pending | Not started | ⏳ Pending |
| 11 | Release, user documentation & packaging | Pending | Not started | ⏳ Pending |

Phase titles and descriptions for 4-11 are taken directly from
[`development/ROADMAP.md`](development/ROADMAP.md), which is the authoritative
plan; see that document for each phase's purpose, deliverables and exit
criteria.

### Status legend

- ✅ **Complete** — implementation and its required testing are both finished.
- 🧪 **Testing** — implementation substantially complete; validation and
  stabilisation ongoing.
- 🚧 **In development** — active implementation.
- ⏳ **Pending** — not yet started.

### What works today

- Create, close, reopen and validate a project (a folder with a metadata file,
  a SQLite database and the standard working sub-directories); invalid or
  damaged projects are refused with a readable message.
- Load, validate and save versioned `.omrt` template documents.
- **Geometric normalisation** (Phase 1): detect the four printed registration
  markers on a scan, resolve which way up the page is, and correct rotation,
  translation, scale, skew and perspective into the canonical page the
  template declares.
- **Interactive template designer** (Phase 2): load a reference sheet image,
  detect and adjust its registration markers, draw student ID / question-set /
  question / custom-bubble regions with generated bubble grids, fine-tune
  individual bubbles, undo/redo, validate and save. See
  `docs/template_designer.md`.
- **Scan / recognition workflow** (Phase 3, *implemented, testing in
  progress*): load a template, import one scan or a whole folder of them,
  process them in the background without freezing the GUI, and review the
  result. Per the automated test suite and the recorded validation run in
  `development/PHASE_03_HANDOFF.md`, this currently includes:
  - orientation detection and marker-based registration, with a scan that
    cannot be confidently registered flagged rather than guessed at;
  - template-to-scan coordinate mapping, so recognition follows the template's
    own regions rather than hard-coded page positions;
  - roll/ID (numeric), set-code and question-block recognition;
  - explicit handling of blank and multiple marks (e.g. `B-D`) and uncertain
    reads, shown in the GUI and carried into the export rather than silently
    resolved;
  - a recognition preview/overlay over the corrected sheet;
  - **configurable multicore batch recognition**: several independent sheets
    read concurrently, one whole page per CPU worker, with the processing mode
    (**Automatic**, **Single core**, **Custom**) chosen in *File > Settings >
    Processing* and remembered between sessions. Results, output file names and
    CSV row order are identical to a single-core run on any number of cores;
  - **large-batch progress tracking**: a progress bar driven by finished
    sheets, completed/total counts and percentage, elapsed time, a smoothed
    estimate of the time remaining, live throughput, per-outcome tallies
    (successful / needs review / failed) and responsive cancellation — all
    from a fixed-size panel that behaves the same for ten sheets or ten
    thousand;
  - deterministic CSV export;
  - optional renaming of processed scans using the detected roll number, with
    safe duplicate-roll handling (`2103123.jpg`, `2103123_a.jpg`,
    `2103123_b.jpg`, ...) and unresolved/uncertain roll numbers left
    unrenamed rather than misnamed;
  - per-sheet error isolation, so one bad file does not abort a batch.
- **Template calibration & validation** (Phase 4, *implemented, testing in
  progress*): load a saved template, add one or more representative real
  scans, and run them through Phase 3's own registration and recognition in a
  diagnostic mode that shows every intermediate measurement rather than a
  second, separate calculation:
  - marker, registration and bubble-geometry overlays drawn from the exact
    coordinates the recognition engine itself computed - asserted equal by
    test, not merely eyeballed;
  - click-to-inspect on any bubble (field, question, fill score, active
    threshold, classification);
  - the four recognition thresholds (`fill_ratio_threshold`,
    `blank_ratio_threshold`, `ambiguity_margin`, `min_confidence`) adjustable
    by slider or exact value, reclassifying and updating the overlay and
    quality summary immediately, without repeating registration;
  - non-destructive calibration: a working value separate from the template's
    saved value, applied only on an explicit *Save to Template*;
  - a four-state validation verdict per scan and per sample - **Validation
    Passed**, **Validation Passed With Warnings**, **Needs Review**,
    **Calibration Failed** - and a template whose registration markers no
    longer match the scan is reported `Calibration Failed`, with no answers
    at all, rather than a plausible wrong result;
  - the Scan page shows a non-blocking notice when the loaded template has
    never been calibrated, or has been edited since it last was.

  Calibrating against representative scans is **not** the same as validating
  Phase 3's real-world accuracy at scale; see
  [`development/PHASE_04_HANDOFF.md`](development/PHASE_04_HANDOFF.md) and
  [`docs/calibration_workflow.md`](docs/calibration_workflow.md).
- A PySide6 application shell with the nine workflow stages; **Project**,
  **Template**, **Calibrate** and **Scan** are implemented, and the remaining
  stages state which phase will implement them.

### Phase 3 architectural hardening

Phase 3 was subsequently hardened into a **replaceable recognition
subsystem**, so that Phases 4 and 5 can be built against it now and a future
Recognition Engine v2 can replace it without rewriting them:

- **Recognition API established.** One entry point -
  `RecognitionEngine.process(image, template)` - behind which no caller needs
  to know about thresholds, contours or homographies. The older
  `recognise_scan()` function remains supported.
- **Structured `RecognitionResult`.** Plain, versioned, JSON-serialisable data:
  the values, machine-readable status codes, the engine and template identity,
  scan-quality metrics, per-stage timings, and the **raw per-bubble
  measurements** behind every decision - so a future recalibration can re-score
  a batch without re-reading a single image.
- **Measurement separated from decision**, with every threshold still owned by
  the template.
- **Diagnostics.** Optional staged debug images and a headless annotated
  overlay per sheet, switchable from *File > Settings > Diagnostics* or the
  command line, and off by default.
- **Headless operation.** `python -m omr_scanner.tools.recognise` reads a scan
  or a folder with no GUI at all — asserted by a test that runs recognition in
  an interpreter where Qt was never imported.
- **Synthetic test generator.** Reproducible labelled datasets rendered from a
  real template, with controlled mark styles, geometry, exposure and structural
  damage, and ground truth written beside every image.
- **Benchmark framework.** Scores the engine against ground truth, classifies
  every disagreement by kind, writes machine-readable reports and compares a
  run against a stored baseline.
- **Regression fixtures.** Eleven stored recognition results covering the
  scenarios later phases must handle, usable with no engine present.
- **Multicore-ready processing** (see above), with the worker pool reading one
  whole page per process.
- **Real-dataset validation pending.** Everything above is infrastructure;
  accuracy is still a Phase 3 open question.

### Large-batch progress (Phase 3)

The batch architecture is built for examination-scale runs — **10,000+ OMR
scripts in a single batch** — and the Scan page reports on one without
changing shape as it grows:

```text
Processing OMR scans...
████████████████████░░░░░░░░░░░░  63.4%
6,342 / 10,000 processed
Elapsed 00:18:42 · Remaining ~00:10:47
Speed 5.7 scans/sec · 12 workers · Finish ~15:42
Successful 6,301 · Review 28 · Failed 13
```

- **Real-time completed / total count** and percentage, driven by *finished*
  sheets — a scan that fails to read still advances the bar, so a batch full
  of damaged files cannot stall it.
- **Elapsed time** on a monotonic clock, so a system clock change cannot
  corrupt it.
- **Smoothed ETA** from recent measured throughput (an exponential moving
  average), shown as `Calculating...` until there is enough evidence — never
  an absurd estimate from the first sheet, and never presented as exact.
- **Processing throughput**, and an estimated finishing time once the estimate
  is stable.
- **Success / needs-review / failure counters** that reconcile with the
  processed count.
- **Multicore-safe progress reporting**: worker processes never touch a
  widget; completions are counted centrally, in one place, under one lock.
- **Responsive cancellation**: the button disables itself at once, no new
  sheet is started, sheets already in a worker finish cleanly rather than
  being killed mid-write, and everything already read is kept.

The interface is fixed-size — one progress bar and five labels whatever the
batch length, repainted about five times a second rather than once per sheet —
and a batch queues file paths, never images. Verified by a headless
10,000-job simulation and by real batches of a few dozen sheets; **no
10,000-scan real-world run has been timed**, so no performance limit is
claimed.

Details: [`docs/recognition_engine.md`](docs/recognition_engine.md).

### Developer testing tools (Phase 3)

*Tools > Developer / Testing* puts the two things recognition work needs inside
the application: a labelled dataset to test against, and a score for what the
engine did with it. Both are also available from the command line, and both run
entirely on the local machine — nothing is uploaded, and no analytics are
collected.

**Generate Synthetic Test Dataset** renders a dataset from a real `.omrt`
template, at roughly **150 DPI derived from the template's physical page size**
(A4 → 1240 × 1754 px), as PNG or JPEG:

```text
synthetic_dataset/
├── images/            SYN_000001.png ...
├── ground_truth/      SYN_000001.json ...   (what was actually marked)
├── manifest.json      template, profile, seed, DPI, format — how to regenerate
├── manifest.csv       one row per sheet, with its test-case tags
└── dataset_summary.json  how many sheets of each kind
```

Nothing about the sheet is hard-coded: identifier length, symbol set, option
labels, question count, bubble size and page dimensions all come from the
template, so a nine-digit alphanumeric identifier with six options generates
correctly without a code change. Identifiers are fictional by construction,
derived from the sheet index.

Sheets are **named test cases**, not random noise, and each carries tags that
survive into the benchmark's category table — blank and multiple identifier
columns, faint and erased marks, tick/cross/ring/dot/stroke/slash mark styles,
a ten-step intensity sweep across the decision boundary, rotation, quarter
turns, scale, translation, perspective, cropping, faint/damaged/missing/extra
registration markers, blur, noise, speckle, exposure, JPEG artefacts, paper
tint, scanner streaks, edge shadow, planted duplicate identifiers, and
deliberate combinations. Profiles (Baseline, Recognition, Degradation, Batch,
Stress, Mixed, Custom) choose which families are drawn on; **the interesting
cases are emitted first**, so a twelve-sheet dataset is a spread of edge cases
rather than a random sample that happens to omit the one that would fail.
A seed makes a dataset byte-for-byte reproducible.

**Run Recognition Benchmark** does *not* open a second processing window. It
puts the existing Step 3 (Scan) page into **benchmark mode** — a banner, the
dataset's scans in the ordinary list, and the same *Process All* button, the
same settings and the same worker pool — because a benchmark of a different
pipeline would measure nothing worth knowing. When the run ends it is scored
automatically and reported:

```text
Dataset: synthetic · 120 scans · sheet accuracy 0.9397 · answer accuracy 0.9652

By test case (least accurate first):
  MARK_STYLE_DOT        1 sheet   sheet ok 0.0000   answers 0.0000   100 errors
  MARK_STYLE_SLASH      1 sheet   sheet ok 0.0000   answers 0.0000   100 errors
  ...
  BASELINE             15 sheets  sheet ok 1.0000   answers 1.0000     0 errors
```

The report covers sheet, registration, student-ID, set-code and answer
accuracy, blank and multiple-mark handling, borderline marks, accuracy by
decision-score band, and every disagreement classified by kind
(`FALSE_BLANK`, `FALSE_MARK`, `WRONG_OPTION`, `MISSED_MULTIPLE_MARK`,
`ROLL_ERROR`, `ROLL_AMBIGUITY_MISSED`, `SET_ERROR`, `ALIGNMENT_ERROR`,
`ORIENTATION_ERROR`, `PROCESSING_FAILURE`). **Per-test-case-category metrics**
are the point of the tags: a dataset that is 94% correct overall is far more
useful described as "perfect everywhere except dot-shaped marks". Failing
scans are listed and open in the scan list with their overlay, and each run
writes `summary.json`, `summary.csv`, `errors.csv`, `category_metrics.csv` and
`run_config.json` beside the dataset, comparing itself with the previous run.

Duplicate identifiers are benchmarked **separately** from recognition
correctness: reading the same number on two sheets is *correct*, and whether
the batch layer noticed is a different question from whether the engine read
the digits.

```bash
# Generate a reproducible labelled dataset
python -m omr_scanner.tools.make_dataset out/dataset --template sheet.omrt \
    --count 250 --profile mixed --seed 20260918 --format png

# Score it, printing the per-test-case table
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --report out/benchmark --workers auto --categories 0
```

> **What synthetic numbers mean.** These datasets measure *regression
> consistency* and *controlled edge-case handling*. They do **not** establish
> real-world recognition accuracy — the pages have clean geometry, even paper
> and marks drawn by arithmetic. Every report this tool writes says so in the
> file itself.

One finding already recorded rather than tuned away: on a 100-question
template with the default 0.55 fill threshold, **dot-shaped marks measure a
mean fill ratio of 0.16** and are read confidently as blank, while horizontal
strokes and slashes measure 0.51–0.54 and land in the uncertainty band, where
the engine flags them for review. Filled bubbles measure 0.93. Whether real
candidates' marks behave this way is exactly what the real-dataset corpus is
for.

### Phase 3 testing status

Phase 3's functionality is implemented and exercised by an automated suite
(2,029 tests passing at the time of writing, plus the repository-local
`qtguitesting` Qt GUI harness), but it has only been run end-to-end against
**one real scanned sheet** (`examples/ECE-0000.png`) — validated with correct
roll number, set code and all 100 answers — plus geometrically distorted copies
of that same sheet and synthetic pages. It has not yet been run against a
broad, independently filled corpus (varied handwriting, pencil vs. pen,
erasures, genuinely ambiguous marks, different scanners), which is the
condition under which Phase 3 will be marked complete. Full detail is in
[`development/PHASE_03_HANDOFF.md`](development/PHASE_03_HANDOFF.md).

Confirmed by the current automated suite:

- [x] Recognition-accuracy validation (real sample: 100/100 answers, roll and
  set code correct, 0 flagged; synthetic corpus covering blank/single/multiple/
  uncertain marks)
- [x] Rotation/orientation testing (±3° rotation, exact 90°/180°/270° turns,
  rescaling, translation, perspective distortion, JPEG re-encoding — the real
  sample reads identically under each)
- [x] Duplicate roll-number handling (`_a`/`_b`/`_c`/... and beyond 26 via
  `_aa`)
- [x] Existing-output-filename collision handling (a pre-existing file is
  never overwritten)
- [x] CSV export validation (column order, question ordering, Unicode,
  escaping, duplicate-name recording, determinism)
- [x] Automated Qt GUI validation using `qtguitesting` (27/27 smoke checks,
  including the real sample recognised end-to-end through the GUI, and a
  dataset generated from the real template and benchmarked through the Scan
  page; three real defects were found this way and fixed)
- [x] Multicore recognition validation (a batch read across worker processes
  produces the same results, in the same order, as one read on a single core;
  also checked through the GUI on the real sample)
- [x] Single-core vs multicore result consistency (the same dataset processed
  at 1, 2 and 4 workers exports **byte-identical** CSVs — values, statuses,
  confidences, output names and row order)
- [x] Parallel duplicate-roll collision testing (eight sheets recognising to
  one roll number, read on four workers, produce `2103123.png`, `_a` ... `_g`;
  nothing overwritten, every CSV row naming the file actually written)
- [x] Worker failure isolation (corrupt, missing and unregistrable images fail
  individually; a worker process that dies outright becomes one failed scan,
  not a failed batch; no worker process outlives a run, a cancellation or the
  window)
- [x] Multicore GUI responsiveness (the event loop keeps delivering queued
  progress signals throughout a four-worker run; the progress bar advances
  monotonically to the scan count and the completion summary is shown)
- [x] Performance benchmarking (48 real scans at 1/2/4/8/12/16 workers, with
  peak memory; measured results and the reasoning behind the automatic worker
  cap are recorded in `docs/scan_workflow.md` §10)
- [x] Recognition API and result contract (engine runs headlessly with no Qt
  imported; `ScanResult` serialises, round-trips and refuses a newer schema)
- [x] Synthetic dataset generation (reproducible from a seed; labels verified
  by recognising the generated sheets; baseline profile scores 100%; DPI
  derived from the template's physical page size; PNG and JPEG output;
  cancellable, one sheet in memory at a time)
- [x] Template-independence of the generator (a nine-digit identifier, five
  options and a template with no set code at all each generate correctly with
  no code change)
- [x] Named test cases and their tags (every profile's mandatory edge cases
  present by construction, and still present when the dataset is too small to
  hold them all)
- [x] Benchmark harness and error categorisation (summary metrics, per-error
  CSV, per-test-case-category metrics, baseline and per-category regression
  comparison, run configuration — all exercised by tests)
- [x] Duplicate-identifier benchmarking, scored apart from recognition
  correctness (planted groups detected, missed, and unplanted collisions)
- [x] Developer testing tools through the GUI (menu, generation dialog,
  cancellable generation, benchmark mode in the Scan page, results dialog,
  failing-case review, second-run comparison — 20 pytest-qt tests)
- [x] Diagnostics (staged images and overlay produced on demand, nothing
  written by default, and generating them provably does not change a result)
- [x] Stored result fixtures for Phases 4-5 (11 scenarios, loadable with no
  recognition engine present)
- [x] Large-batch progress tracking (headless 10,000-job simulation reaching
  exactly 10,000/10,000 and 100%; ETA warm-up, smoothing, stall and
  cancellation behaviour; counters reconciling with the batch report)
- [x] Progress GUI validation (bar advances monotonically to exactly 100%, a
  failed scan still advances it, no dialog per failed scan, cancellation is
  immediate and honest, ten thousand rows create exactly one progress bar)

Still open, and why Phase 3 is not marked complete:

- [ ] Validation against a broad corpus of independently, genuinely filled
  sheets (different handwriting, pencil/pen, erasures, real ambiguous marks) —
  currently one real sheet plus geometric variants of it
- [ ] Registration/alignment edge cases beyond Phase 1's documented limits
  (e.g. illumination gradients, rotation beyond ±15°, heavy cropping)
- [ ] Batch-processing stress testing at realistic exam volumes. The largest
  *measured* run is 48 real scans; the 10,000-scan figure the architecture
  targets has been exercised as a simulation of the progress and counting
  path, not as ten thousand real recognitions
- [ ] Multicore validation on hardware other than the 16-thread Windows
  development machine (core counts, memory limits and `spawn` behaviour all
  differ; Linux and macOS are untested)
- [ ] **Threshold calibration against real marks.** The defaults come from one
  real sheet and synthetic pages
- [ ] **Confidence calibration.** What the engine reports is a bounded
  *decision score*, not a probability; the benchmark reports accuracy by band
  so it can be checked once there is data to check it against
- [ ] Difficult handwriting and mark-shape analysis. Synthetic datasets already
  show the shape of the problem: on one template tick-shaped marks produced a
  ~33% false-blank rate against 0% for crosses and scribbles, and on another
  dot-shaped marks measured a mean fill ratio of 0.16 (read confidently as
  blank) while strokes and slashes measured 0.51–0.54 (flagged as uncertain),
  against 0.93 for a filled bubble — all because the measurement is
  coverage-based. Whether real marks behave this way is exactly what the corpus
  is for, and the thresholds are deliberately **not** being tuned against
  synthetic pages
- [ ] Recognition accuracy and performance optimisation, once there is real
  data to optimise against
- [ ] Final Phase 3 regression sign-off once the above are addressed

### Phase 4 testing status

Phase 4 (Template Calibration & Validation) is implemented and covered by 63
automated tests (27 unit, 10 integration, 26 GUI), plus targeted
`qtguitesting` smoke checks against the real sample sheet. Full detail is in
[`development/PHASE_04_HANDOFF.md`](development/PHASE_04_HANDOFF.md).

Confirmed by the current automated suite:

- [x] Overlay geometry matches the recognition engine's own computed
  coordinates exactly (asserted, not merely visually checked)
- [x] Threshold changes propagate to results, the overlay and the quality
  summary without repeating registration (asserted via unchanged measured
  fill values and unchanged registration output on a re-decide)
- [x] A deliberately mismatched template is reported `Calibration Failed`,
  with no fields, answers or bubbles at all - verified against the real
  scanned sheet as well as synthetic ones
- [x] Small marker offsets are tolerated and large ones are flagged, using
  tolerances derived from the template's own measured geometry rather than
  assumed numbers
- [x] Non-destructive threshold adjustment (working value vs. saved value;
  reset to template, reset to defaults, explicit save)
- [x] Template staleness detection (an edited template's saved calibration is
  correctly invalidated; the Scan page shows the resulting warning)
- [x] Per-scan and per-sample (aggregate) quality summaries, worst-status-wins
  aggregation
- [x] Backward compatibility: a template saved before this phase loads with no
  recorded calibration, rather than failing to load
- [x] Automated Qt GUI validation using `qtguitesting` (30/30 smoke checks,
  including the real sample sheet scoring `passed_with_warnings` and a
  deliberately mismatched template scoring `failed`)
- [x] Full pre-existing Phase 1-3 recognition, batch and benchmark suites pass
  unchanged after the one isolated, behaviour-preserving refactor this phase
  made to `recognition_service` (see
  [`development/PHASE_03_HANDOFF.md`](development/PHASE_03_HANDOFF.md))

Still open, and why Phase 4 is not marked complete:

- [ ] Calibrating a template against representative scans has **not** been
  shown to make Phase 3's underlying recognition accurate - it makes
  mis-registration and mis-calibration visible and correctable, which is a
  different, narrower claim
- [ ] No real corpus of *deliberately* miscalibrated templates exists yet to
  validate the calibration-judgement thresholds (5% unusable bubbles, 30%
  systematic ambiguity) against; they are reasoned defaults, documented as
  such
- [ ] A score-distribution histogram was not built; a simpler, documented
  textual separation label is used instead, per the phase's own brief
  permitting a simpler alternative where a fitted statistical measure could
  not be justified on the data available

### Development philosophy

OMRFlow follows an incremental development process. Major functionality is
introduced in phases, followed by automated testing, GUI validation, regression
testing and stabilisation before a phase is considered complete. This does not
mean every phase must be completely frozen before work on the next begins —
Phase 4 (template calibration) and Phase 5 (persistent batch processing) both
build on Phase 3's recognition engine, for example — but a phase's own status
in the table above reflects its own testing state, not merely whether its code
exists.

### Pending development

Phases 4-11 have not started. Their titles, purposes and deliverables as
currently planned are documented in
[`development/ROADMAP.md`](development/ROADMAP.md); this project does not
promise a delivery date for any of them, and scopes may be refined as earlier
phases surface real requirements.

Current detail: [`development/CURRENT_STATE.md`](development/CURRENT_STATE.md).
Plan: [`development/ROADMAP.md`](development/ROADMAP.md).
Phase 3 handoff: [`development/PHASE_03_HANDOFF.md`](development/PHASE_03_HANDOFF.md).
Phase 4 handoff: [`development/PHASE_04_HANDOFF.md`](development/PHASE_04_HANDOFF.md).

---

## Installation

Requires **Python 3.12 or newer**.

```powershell
git clone https://github.com/sajidbuet/OMRflow.git
cd OMRflow
python -m venv .venv
.venv\Scripts\Activate.ps1          # Linux/macOS: source .venv/bin/activate
pip install -e .                    # add ".[dev]" for the development tools
```

## Running

```bash
python -m omr_scanner                  # from a source checkout
omrflow                                # console entry point
omrflow "C:/Exams/Physics Midterm"     # open a project on start-up
```

## Testing

```bash
pip install -e ".[dev]"
pytest                   # 2,000+ tests
ruff check .
mypy
```

All three must pass before a development phase is considered complete. Passing
tests are a precondition for completion, not proof of it by themselves — see
[Development Status](#development-status) above for what "complete" requires
for a given phase.

## Developer tools

Not user-facing, but the quickest way to see the alignment engine work:

```bash
# Render a synthetic sheet, distorted by a known transform
python -m omr_scanner.tools.make_test_sheet scan.png --rotate 6 --perspective 0.02 --seed 7

# Align it, print what was measured, and write diagnostic overlays
python -m omr_scanner.tools.align_image scan.png --output aligned.png --debug debug/

# Measure batch throughput on this machine at several worker counts
python scripts/benchmark_batch.py --scans 48 --workers 1,2,4,8
```

Phase 3 recognition, entirely without the GUI:

```bash
# Read one scan or a folder; write JSON results, overlays, or a full debug dump
python -m omr_scanner.tools.recognise scans/ --template sheet.omrt \
    --json-dir out/results --overlay-dir out/overlays --workers auto

# Generate a reproducible labelled test dataset from a real template
python -m omr_scanner.tools.make_dataset out/dataset --template sheet.omrt \
    --count 250 --profile mixed --seed 20260918

# ...or just the cases you are working on, as JPEG
python -m omr_scanner.tools.make_dataset out/answers --template sheet.omrt \
    --profile custom --families answers mark_styles intensity --format jpg

# See what a template offers the generator before generating anything
python -m omr_scanner.tools.make_dataset out/x --template sheet.omrt --describe

# Score recognition against that dataset, classifying every disagreement
# and reporting accuracy per kind of test case
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --report out/benchmark --categories 0

# ...and compare a change against the run before it
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template sheet.omrt --baseline out/benchmark/summary.json
```

Both are in the application as well, under *Tools > Developer / Testing* — see
[Developer testing tools](#developer-testing-tools-phase-3) above.

Diagnostics (the corrected page, an annotated overlay, every bubble's
measurement, and the result as JSON) come from `--diagnostics out/diagnostics`
on the command line, or *File > Settings > Diagnostics* in the application.
Both are off by default. Full detail:
[`docs/recognition_engine.md`](docs/recognition_engine.md).

---

## Repository structure

```text
OMRflow/
├── pyproject.toml            packaging + pytest/Ruff/mypy configuration
├── CHANGELOG.md
│
├── src/omr_scanner/
│   ├── main.py               entry point: CLI, logging, start-up
│   ├── errors.py             application exception hierarchy
│   ├── config/               per-user settings, platform paths and the
│   │                         CPU-worker policy (Phase 3)
│   ├── domain/               pure models: project, geometry, template,
│   │                         template_authoring (region generation, Phase 2)
│   ├── database/             SQLite schema, migrations, sessions
│   ├── services/             workflows the GUI calls: alignment, template,
│   │                         recognition, batch processing (incl. the
│   │                         multicore worker pool), filename allocation,
│   │                         scan import/export (Phase 3), calibration
│   │                         verdicts (Phase 4)
│   ├── gui/                  PySide6 window and workflow pages
│   │   ├── template_designer/  interactive .omrt editor (Phase 2)
│   │   ├── calibration/         calibrate a saved template against real
│   │   │                        scans before a batch (Phase 4, testing in
│   │   │                        progress)
│   │   ├── scan/                scan/recognition workflow page, incl.
│   │   │                        benchmark mode (Phase 3, testing in progress)
│   │   └── devtools/            Tools > Developer / Testing: dataset
│   │                            generation and benchmark results (Phase 3)
│   ├── imaging/              pixel algorithms: alignment, plus per-bubble
│   │                         fill-metric measurement (Phase 3)
│   ├── tools/                developer command line utilities
│   ├── recognition/          value interpretation: decide, fields (Phase 3,
│   │                         testing in progress; conflict resolution is
│   │                         Phase 6)
│   ├── evaluation/           QA above the engine: ground-truth schema, named
│   │                         test cases, dataset planner and renderer,
│   │                         benchmark, error categories and the benchmark
│   │                         session (Phase 3)
│   ├── reporting/            RESERVED - XLSX/PDF export (Phase 9; CSV export
│   │                         already exists in services/scan_export.py)
│   └── utils/                logging setup, atomic JSON
│
├── tests/
│   ├── unit/                 logic, domain models, layering rules
│   ├── integration/          services + database + file system
│   ├── gui/                  pytest-qt smoke tests, incl. the Scan workflow
│   └── fixtures/             test data (policy in docs/TESTING.md), incl.
│                             recognition/ - stored results for Phases 4-5
│
├── local_test_data/          where a real validation corpus goes; ignored by
│                             git except its README (Phase 3)
│
├── resources/
│   ├── templates/            illustrative .omrt example
│   └── icons/                RESERVED
│
├── examples/
│   ├── templates/            worked .omrt examples (Phase 2), incl. the
│   │                         template for the real sample sheet (Phase 3)
│   └── ECE-0000.png          the one real scanned sheet Phase 3 has been
│                             validated against
├── docs/                     architecture, data model, formats, ADRs
└── development/              roadmap, current state, phase handoffs
```

## Documentation

| Document | Contents |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layers, dependency direction, GUI/service separation, error and logging rules |
| [`docs/DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md) | Setup, commands, conventions, where each kind of setting belongs |
| [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) | Entities across all phases and their relationships |
| [`docs/TEMPLATE_FORMAT.md`](docs/TEMPLATE_FORMAT.md) | The `.omrt` format, with a worked example |
| [`docs/template_designer.md`](docs/template_designer.md) | The interactive template designer: workflow, shortcuts, architecture |
| [`docs/IMAGE_PROCESSING.md`](docs/IMAGE_PROCESSING.md) | The alignment and recognition pipeline: algorithms, accuracy, failure modes and limits |
| [`docs/scan_workflow.md`](docs/scan_workflow.md) | The Scan / recognition workflow: importing, batch processing, renaming and CSV export (Phase 3) |
| [`docs/recognition_engine.md`](docs/recognition_engine.md) | The recognition subsystem for developers: the engine API, the `ScanResult` contract, diagnostics, synthetic datasets and the benchmark harness (Phase 3) |
| [`docs/calibration_workflow.md`](docs/calibration_workflow.md) | The Calibration workflow: procedure, overlay layers, thresholds, validation status and how to recognise a bad calibration (Phase 4) |
| [`docs/TESTING.md`](docs/TESTING.md) | Testing strategy and the test-fixture policy |
| [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | How to use what currently exists |
| [`docs/decisions/`](docs/decisions/) | Architecture decision records |

---

## Capabilities

The following describes the finished system. Each capability is tagged with
the phase that delivers it and its current state; anything tagged **planned**
does not exist yet. Phase 3 and Phase 4 items are implemented but still
"testing in progress" in the sense described in
[Development Status](#development-status) above.

**Template designer** *(Phase 2, implemented)*. Load a reference sheet, draw
recognition zones, define the four registration markers and the orientation
marker, and configure field types: numeric, alphanumeric, set code, candidate
id, MCQ question blocks and ignored regions. Templates use
resolution-independent normalised coordinates and are stored as versioned
`.omrt` documents.

**Automatic scan alignment** *(Phase 1, implemented)*. Registration markers
drive rotation, skew, perspective and scale correction, transforming every raw
scan into the canonical coordinate system of the template. Original scans are
never modified.

**Confidence-aware recognition** *(Phase 3, implemented; testing in
progress)*. Bubbles are measured (local fill ratio against a locally estimated
paper/ink level, relative darkness among competing candidates), not merely
thresholded against a fixed value, so uncertain responses are flagged instead
of silently accepted.

**Missing and multiple marks** *(Phase 3, implemented; testing in progress)*.
Ambiguity is represented explicitly rather than guessed: an unmarked response
is blank, and multiple marks on one question are reported with both kept (for
example `B-D`), never collapsed to a single answer. A roll number or set code
that could not be read reliably is flagged rather than filed under a guessed
value.

**Multicore batch processing** *(Phase 3, implemented; testing in progress)*.
Independent sheets are read concurrently, one complete page per CPU worker
process, while the Qt interface stays in the main process and stays responsive.
The processing mode is a user setting - **Automatic** (OMRFlow chooses,
leaving the machine room to breathe), **Single core** (deterministic
troubleshooting, low-memory machines, benchmark baselines) or **Custom** (a
worker count of your own, up to the CPU threads detected). Parallel execution
never changes what is recognised: output names are assigned centrally, in batch
order, by a single allocator in the main process, so duplicate roll numbers,
the scan list and the CSV come out identically however the work was divided.

**Batch scanning, export and safe renaming** *(Phase 3, implemented; testing
in progress)*. Import one scan or a whole folder; process in the background
without freezing the GUI; export a deterministic CSV; optionally copy
processed scans into an output folder named after the detected roll number,
with duplicate rolls safely suffixed (`2103123.jpg`, `2103123_a.jpg`,
`2103123_b.jpg`, ...) so no file is ever overwritten, and unresolved rolls left
unrenamed rather than misnamed. See `docs/scan_workflow.md`.

**Template calibration & validation** *(Phase 4, implemented; testing in
progress)*. Verify a saved template against representative real scans before
running a batch, reusing Phase 3's own registration and recognition rather
than a second engine: marker/registration/bubble-geometry overlays drawn from
the engine's own coordinates, per-bubble score inspection, non-destructive
threshold tuning with immediate reclassification, and a four-state validation
verdict (Passed / Passed with Warnings / Needs Review / Calibration Failed)
that reports a mis-registered or mis-calibrated template as failed rather than
producing a confident-looking wrong result. See `docs/calibration_workflow.md`.

**Conflict resolution** *(planned, Phase 6)*. A review interface showing the
original sheet, the normalised sheet, the highlighted field, the zoomed region
and the detected alternatives. A manual correction never overwrites the
machine value - it is recorded alongside it with a timestamp and a reason.

**Candidate and attendance reconciliation** *(planned, Phase 7)*. Compare
registered candidates, recorded attendance and detected scripts; surface
unknown roll numbers, duplicate scripts, absent-with-script and
present-without-script cases.

**Answer keys and scoring** *(planned, Phase 8)*. Keys entered manually or read
from solution sheets, independent per question paper set, verified before use.
Configurable marks for correct, incorrect and blank answers, with negative
marking optional.

**Reports** *(planned, Phase 9)*. Roll-wise (including absentees) and
merit-wise Excel workbooks with a user-editable layout, plus PDF export. (CSV
export of raw recognition results already exists, from Phase 3.)

**Auditability.** Anything that can change a result is recorded append-only:
machine value, corrected value, timestamp, operation, reason.

---

## Technology

| Component | Technology |
|---|---|
| Language | Python 3.12+ |
| Desktop GUI | PySide6 / Qt |
| Image processing | OpenCV (headless), NumPy |
| Database | SQLite via SQLAlchemy 2.x |
| Validation | Pydantic 2 |
| Tabular / Excel | pandas, openpyxl |
| Testing | pytest, pytest-qt |
| Lint / types | Ruff, mypy (strict) |

Initial target platform is Windows; the application core avoids
platform-specific logic, so Linux and macOS support remains open.

## Design principles

> **Readable over clever** · **Explicit over implicit** · **Configurable over
> hard-coded** · **Modular over monolithic** · **Testable over tightly coupled**
> · **Human-verifiable over black-box automation**

OMR algorithms must never live inside GUI event handlers:

```text
GUI  ->  Service  ->  Alignment / Recognition  ->  Database
```

The layering is not merely documented - `tests/unit/test_architecture.py` fails
the build if the GUI imports OpenCV or SQLAlchemy, or if the imaging layer
imports Qt.

## Data privacy and examination integrity

OMRFlow runs locally. Candidate identities, answer sheets, attendance,
answer keys and results never need to leave the machine, and candidate data is
kept out of application logs by policy.

Successful processing is not the same as a correct result. For high-stakes
examinations, institutions should retain independent procedures for template
validation, answer-key verification, unresolved-conflict review, attendance
reconciliation, result verification, output approval and archival of original
scans. The software supports such controls; it does not replace institutional
responsibility.

Never include confidential candidate or examination data in bug reports or test
fixtures.

## Contributing

Contributions are welcome once the architecture has stabilised. Before opening a
pull request, run `pytest`, `ruff check .` and `mypy`, and read
[`docs/DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md). Please keep unrelated
architectural changes and features in separate pull requests.

### Development status maintenance

The [Development Status](#development-status) table and its testing checklist
should be updated whenever a phase is started, implemented, enters testing, or
is completed — including moving individual checklist items from unchecked to
checked only once repository evidence (a passing automated test, a documented
validation run) actually confirms them, never merely because related code or a
test file now exists. Testing status and known pending work should stay
synchronised with `development/CURRENT_STATE.md` and the relevant
`development/PHASE_NN_HANDOFF.md`, so this README remains an accurate
high-level status page rather than a second, drifting source of truth.

## License

Intended to be released as open-source software under a permissive licence
(Apache 2.0 or MIT). The final choice will be made before the first public
release.

## Developer

OMRFlow is developed by [Dr. Sajid Muhaimin Choudhury](https://www.sajid.bd).

---

## OMRFlow

**From scanned marks to verified results.**
