# Testing strategy

## Principles

1. **A test asserts behaviour, not implementation.** Tests that restate the code
   (`assert True is True`, or mocking the thing under test) are worse than no
   test: they cost maintenance and catch nothing.
2. **Tests never touch real user data.** An autouse fixture redirects the
   per-user configuration and log directories into the test's temporary folder.
   Projects are created under `tmp_path`.
3. **Failures are tested as carefully as successes.** For examination software,
   "rejects a damaged project file cleanly" matters as much as "opens a good
   one".
4. **Architecture is testable too.** `tests/unit/test_architecture.py` parses
   every source file and asserts the layering rules from
   `docs/ARCHITECTURE.md` - the GUI imports no OpenCV or SQLAlchemy, `imaging`
   imports no Qt, `domain` imports nothing above it, and every module has a
   docstring.
5. **A measurement, not an assertion that something happened.** `assert result
   is not None` is not a test of an alignment engine. Every geometric test
   compares a recovered position against a known one and reports the distance,
   and every tolerance is derived from measurement with its magnitude justified
   where it is defined.

## Layout

```text
tests/
├── conftest.py        shared fixtures (isolation, workspace, project session,
│                      synthetic canonical sheet and its alignment configuration)
├── unit/              pure logic: config, geometry, template model, domain,
│                      imaging geometry/config/preprocessing, the synthetic
│                      generator, layering
├── integration/       service + database + file system working together;
│                      the whole alignment engine end to end
├── gui/               pytest-qt smoke tests of the PySide6 shell
└── fixtures/          test data (see the fixture policy below)
```

## Running

```bash
pytest                 # everything
pytest tests/unit      # fast, no Qt
pytest -m gui          # GUI only
pytest --cov=omr_scanner
```

GUI tests require a Qt platform plugin; on a headless runner the offscreen
plugin is selected automatically (`tests/gui/conftest.py`), and can be forced
with `QT_QPA_PLATFORM=offscreen`.

## What is covered today (Phases 0-1)

583 tests, all passing. The Phase 1 additions are listed under "Testing the
alignment engine" below; this table covers the Phase 0 foundation.


| Area | Examples |
|---|---|
| Configuration | defaults, round trip, damaged file falls back, strict mode raises, recent-project list is unique/capped, sandbox isolation holds |
| Project lifecycle | directory structure created, metadata readable, identity mirrored into the database, project log started, invalid name rejected, non-empty folder refused, reopen preserves identity, missing sub-directories repaired |
| Invalid projects | plain folder, damaged `project.json`, newer format version, missing database, metadata failing validation |
| Database | schema created and recorded, reopen applies nothing, data survives a reopen, newer schema refused, failed transaction rolls back fully, foreign keys enabled |
| Domain | normalised geometry, bubble-centre arithmetic and overrides, field row/column derivation, zone/grid overflow rejection, template round trip, shipped example stays valid |
| Templates | load, save, suffix handling, damaged file, unrelated JSON, newer version, semantically invalid document |
| GUI | window starts, navigation switches pages, placeholders state their phase, create/close/reopen updates the window, invalid folder reports an error instead of crashing, recent projects persisted, closing the window releases the database |
| Entry point | argument parsing, `--version`, GUI start-up failure becomes an exit code |

## GUI testing policy

Modal dialogs are never exercised. The main window deliberately separates
`_prompt_*` methods (which own `QFileDialog`/`QInputDialog` and contain no logic)
from `create_project_at` / `open_project_at` / `close_project` (which contain the
behaviour). Tests drive the second group. Where an error dialog is unavoidable,
`QMessageBox.warning` is patched and its arguments asserted.

Keep GUI tests to a smoke layer: that widgets exist, that state propagates, that
failures do not escape into the event loop. Logic worth testing thoroughly does
not belong in a widget in the first place.

One exception earns its keep: **geometry**. A `QGraphicsItem` position or resize
bug lives nowhere but the widget layer, and cannot be moved out of it, so
`tests/gui/test_template_designer_region_geometry.py` drives real Qt mouse events
and asserts invariants (dragging the right edge changes only the width; a region
never lands near the scene origin). Those tests use Qt-native interaction at
*viewport-relative* points derived from an item's own scene rectangle - never
absolute screen coordinates, which depend on DPI, window placement and the
operating system.

The workflow, the coordinate-system reference and the diagnostic scripts for this
live in the repository-local `qtguitesting` skill
(`.claude/skills/qtguitesting/`); see `docs/DEVELOPMENT_GUIDE.md`.

## Fixture policy

`tests/fixtures/` is reserved and currently almost empty. Three categories, kept
apart on purpose:

| Category | Directory | Purpose |
|---|---|---|
| **Synthetic** | `fixtures/images/synthetic/` | Generated by code from a *known* transform. Preferred, because the ground truth is known, so accuracy can be measured rather than eyeballed. |
| **Anonymized real-world** | `fixtures/images/anonymized/` | Real scans with every candidate identifier removed. They capture what synthetic data cannot: pencil texture, scanner shadows, crossed-out bubbles, imperfect printing. |
| **Regression** | `fixtures/images/regression/` | The minimal input that once reproduced a specific defect, committed with the fix and a one-line note naming the defect. |

### Rules

1. **Never commit confidential examination or candidate data.** No real roll
   numbers, no names, no live answer keys, no identifiable handwriting - not in
   fixtures, not in bug reports, not in screenshots.
2. Prefer synthetic fixtures. Add a real scan only for a property synthetic data
   cannot express.
3. Keep files small (target under 200 KB): downscale and crop to the region under
   test.
4. Index every committed fixture with one line in its directory's `README.md`
   saying what it is for.

### Anonymizing a real scan

Before committing anything under `anonymized/`:

1. Obtain permission from the institution that owns the sheet.
2. Blank the candidate identity regions (roll number, name, signature, any
   barcode or sticker) in the image itself, not by cropping the view.
3. Confirm no identifier survives in the file name or in EXIF metadata.
4. Verify the file has the property it is being added for, and record that
   property in the index line.

If step 1 is not possible, the scan does not go into the repository. Keep it in a
private local fixture directory instead and mark the test that uses it as
skipped when the file is absent.

## Testing the alignment engine (Phase 1)

The synthetic round trip is the reason the pipeline is designed the way it is:

```text
canonical sheet with known interior control points
   -> apply a KNOWN rotation / scale / translation / perspective / blur / noise
   -> synthetic scan
   -> OMRFlow alignment
   -> map the control points through the recovered transform
   -> compare with where they were drawn
```

Because the applied transform is known, alignment accuracy is a measured number
with a threshold that can regress.

### The synthetic sheet generator

`omr_scanner.imaging.synthetic` renders a canonical page and distorts it
reproducibly. It lives in the package rather than under `tests/` so the
developer tools can use it too, but it is a test and development utility.

`render_sheet(SyntheticSheetSpec(...))` produces a page carrying:

- four solid registration squares at the configured normalised centres;
- an orientation dash;
- nine interior control points, whose exact canonical positions come back with
  the sheet;
- **competing graphics on purpose**: hollow answer frames the same size as a
  marker, a grid of unfilled bubbles, text-like bars, and optional solid decoy
  squares placed inside the corner search regions.

The clutter is not decoration. A page that is white paper plus four black
squares makes any detector look good; the shapes above are the ones that defeat
a detector relying on outline alone, and several tests exist only to confirm
they are rejected for the right reason.

Markers and the orientation mark can be omitted individually
(`omit_markers`, `omit_orientation_marker`), which is how the failure paths are
exercised without hand-editing pixels.

### Why interior control points

The homography is fitted through the four marker centres, so those four points
map onto their targets to numerical precision no matter how wrong detection was.
Reprojection error over them therefore measures arithmetic, not geometry.

The control points take no part in the fit, so the distance between where one
lands and where it was drawn is an honest measure of how much of the page was
actually recovered. `synthetic.control_point_errors` computes it, using its own
arithmetic rather than `imaging.geometry`, so the ground truth is never produced
by the code under test.

Two tests guard the metric itself: mapping through the exact inverse of the
applied distortion must give ~0 error, and mapping through the identity must
give a large one.

### Deterministic distortions

`DistortionSpec` describes a degradation physically rather than as OpenCV
arguments, so a failing case reads as "10 degrees plus 6 per cent perspective":

| Field | Effect |
|---|---|
| `rotation_degrees` | Clockwise rotation about the page centre; the canvas grows to fit, as a scanner's output does. |
| `scale_x`, `scale_y` | Uniform or non-uniform scale. |
| `translate_x_px`, `translate_y_px`, `margin_px` | Translation, platen margin; a negative margin crops into the page. |
| `perspective_strength` | Each corner displaced by up to this fraction of the shorter page side. |
| `brightness_gain`, `brightness_offset` | Uniform exposure change. |
| `illumination_gradient` | A diagonal ramp - the degradation a global threshold is actually vulnerable to. |
| `blur_kernel_px`, `noise_sigma`, `jpeg_quality` | Optical and sensor degradation. |
| `seed` | Seeds every random choice. |

**Every randomised distortion is seeded.** There are no flaky geometry tests:
reproducibility is asserted directly, by applying the same spec twice and
requiring byte-identical images and identical homographies.

### The accuracy suite

`tests/integration/test_alignment.py` runs 40 distortion cases - every class
singly, plus four combined cases including an upside-down and a quarter-turn one
- and measures nine control points in each.

```text
mean               0.062 px
median             0.059 px
95th percentile    0.145 px
maximum            0.394 px
failures           0
```

`CONTROL_POINT_TOLERANCE_PX = 1.5` is the regression threshold. It is derived
from the measurement, not chosen for convenience: roughly a tenth of a printed
marker's width and two orders of magnitude below a bubble pitch, with enough
headroom that a regression tripling the error fails the suite rather than
passing quietly.

A second test re-detects each control point **in the rectified image** rather
than through the matrix, at a looser 3 px tolerance that also absorbs bilinear
interpolation. It exists so that a warp using the right matrix with the wrong
output size or a flipped axis cannot pass unnoticed.

### Orientation tests

All four cardinal feed orientations are tested three ways: alone, with 4 degrees
of skew and 1.5 per cent perspective on top, and for confidence and margin. The
failure paths are tested as carefully: a sheet with no orientation mark must
raise `ORIENTATION_NOT_FOUND`, and a sheet carrying a *second* mark where the
inverted hypothesis would sample it must be refused for insufficient margin
rather than decided by a coin toss.

### Test layout

| File | Tests | Covers |
|---|---:|---|
| `unit/test_imaging_geometry.py` | 53 | Point ordering (upright, rotated ±30°, translated, perspective, narrow, scrambled input), convexity, simplicity, area, aspect ratio, every quadrilateral validation rejection, homography and its inverse, reprojection. |
| `unit/test_imaging_config.py` | 35 | Every configuration rejection, the derived area/aspect bounds, normalised-to-canonical conversion, canonical marker targets at three page sizes. |
| `unit/test_imaging_preprocessing.py` | 39 | Validation of every malformed input shape, grayscale for all channel layouts, downscaling and the realised scale factor, all three threshold strategies, the adaptive block-size constraint, source immutability. |
| `unit/test_synthetic_sheets.py` | 43 | The generator's own ground truth, determinism, each distortion's effect, and the two guards on the accuracy metric itself. |
| `integration/test_marker_detection.py` | 40 | Clean pages, eight degradations, decoys inside the corner regions, a large logo, noise specks, three resolutions, three threshold strategies, scoring, search regions, centroid stability under damage, and the selection failures. |
| `integration/test_orientation.py` | 29 | 0/90/180/270 plain, skewed and by confidence; mark reporting; hypothesis pruning; missing and ambiguous marks; the fallback and its warning. |
| `integration/test_alignment.py` | 113 | The 40-case accuracy suite (error and output size), determinism, three resolutions, image-space recovery, colour and BGRA input, transforms and their inverse, the result structure, every metric, every warning. |
| `integration/test_alignment_failures.py` | 41 | Each missing corner, two missing, blank and cluttered pages, heavy cropping, invalid quadrilaterals, every malformed input, the failure contract (code, user message, no sentinel), damaged and outline-only markers. |
| `integration/test_alignment_diagnostics.py` | 23 | That diagnostics change nothing, their content, overlay and preview rendering, the textual summary. |
| `integration/test_alignment_service.py` | 20 | Template-to-configuration conversion field by field, an end-to-end alignment driven by a template, image I/O including non-ASCII paths and overwrite refusal. |
| `integration/test_imaging_tools.py` | 17 | Both developer tools: arguments, exit codes, diagnostics output, reproducibility. |

### Shared fixtures

`canonical_sheet` and `canonical_config` in `tests/conftest.py` are session
scoped. Both are immutable - the sheet is only ever warped into a new array, and
the configuration is frozen dataclasses all the way down - so sharing them
cannot leak state between tests, and it saves the suite several hundred renders.

### Files

No test writes into the repository. The tools tests and the diagnostics tests
write into pytest's `tmp_path`; everything else works in memory.

### Adding anonymised real-world scans later

`tests/fixtures/images/anonymized/` is reserved and empty. Real-world regression
testing is **deferred**: no anonymised sample sheets exist yet, and Phase 1's
accuracy numbers are therefore entirely synthetic. That is the largest open risk
in the alignment engine.

To add one, follow the anonymisation procedure above, then:

1. Put the file in `tests/fixtures/images/anonymized/` and index it in that
   directory's `README.md` with the property it is there for.
2. Add a test that aligns it and asserts what can be asserted without ground
   truth: that it succeeds, which warnings it carries, and that the metrics stay
   within recorded bounds. Pin those bounds to the values measured when the
   fixture is added, so a regression is visible.
3. If the sheet's true marker positions can be measured by hand, record them
   beside the fixture and assert the detected centres against them; that turns
   the fixture into a real accuracy measurement rather than a smoke test.
4. Mark the test skipped when the file is absent, so a developer without the
   fixture can still run the suite.

---

## Testing multicore batch processing (Phase 3)

Multiprocessing is tested in three layers, because the three questions it raises
are answered at different levels and only one of them needs a process at all.

| Layer | File | Answers |
|---|---|---|
| Pure logic | `unit/test_processing_settings.py` | How many workers does each mode ask for - on 1, 2, 4, 8, 16, 32, 128 logical CPUs, for a batch of 0, 1, 3 or 500 scans? Does the setting survive the configuration file? |
| Real processes | `integration/test_parallel_batch.py` | Do 1, 2 and 4 workers read the same thing? Does the batch order survive out-of-order completion? Do eight sheets claiming one roll number all keep their file? Does one corrupt image fail alone? Is any worker process left behind? |
| The GUI | `gui/test_processing_settings_gui.py` | Can each mode be selected, does the worker selector refuse invalid values, is the choice still there after a restart, does a batch run in the chosen mode while the window stays responsive? |

Three rules keep this suite honest and fast:

- **Pass `cpu_count` explicitly** in the logic and GUI tests. The offered range
  and the automatic count depend on the machine; a test that read the real CPU
  count would assert something different on every runner, which is the same as
  asserting nothing.
- **Never assert a duration.** A throughput assertion fails when the machine is
  busy and teaches nobody anything. `scripts/benchmark_batch.py` measures
  instead, prints what it measured, and leaves the judgement to a person; its
  recorded results are in `docs/scan_workflow.md` §10.
- **Keep the datasets small.** Six to eight synthetic sheets is enough to expose
  a coordination bug, and every worker is a fresh interpreter importing NumPy
  and OpenCV - a large dataset would make the suite slow without making it
  stricter.

The consistency check is the one that matters most: the same dataset is
processed at one, two and four workers and the exported CSVs are compared **byte
for byte**. If parallel execution ever changed a recognised value, a confidence,
an output name or a row order, that comparison is what fails.

---

## Testing Phase 3 as a subsystem

The recognition engine is tested at four levels, and which level a test belongs
at is decided by what it is actually asserting.

| Level | Where | Asserts |
|---|---|---|
| The contract | `unit/test_recognition_contract.py` | The shape of a `ScanResult`: serialisation, status-code derivation, accessors, the engine/schema versions. Imports no engine, no OpenCV. |
| Stored results | `unit/test_recognition_fixtures.py` | That `tests/fixtures/recognition/*.json` loads, round-trips, and still represents its scenario. |
| The harness itself | `unit/test_evaluation_harness.py` | Ground-truth schema, error categorisation, summary arithmetic, baseline verdicts. A benchmark that is itself untested will eventually report an improvement that did not happen. |
| The engine | `integration/test_recognition_engine.py` | The documented entry point, the quality metrics, the timings, diagnostics on and off, and determinism. |
| The data | `integration/test_synthetic_dataset.py` | That generated labels match what was drawn - checked by *recognising* the generated sheets - that a seed reproduces a dataset exactly, that each profile's edge cases are present by construction, and that nothing about the sheet is hard-coded. |
| The tools | `integration/test_recognition_tools.py` | `recognise`, `make_dataset` and `benchmark_recognition` driven through `main(argv)`: arguments, exit codes, the files they write. |
| The developer tools | `gui/test_developer_tools.py` | The Tools menu, the generation dialog and its validation, generation and its cancellation, benchmark mode in the Scan page, the results dialog, failing-case review, and the second-run comparison. |

Four rules keep this suite meaningful:

- **No accuracy assertion on synthetic data beyond the baseline profile.** A
  clean synthetic sheet must read perfectly, because anything else is a defect
  in the generator or the engine. Degraded profiles are *measured* by
  `benchmark_recognition`, never asserted in a test: a threshold pinned to
  synthetic performance would be optimising for the wrong data and would fail
  the first time the generator improved.
- **The generator must not assume a template.** Three tests build templates the
  rest of the suite never uses - nine identifier digits, five options, no set
  code at all - and assert that generation adapts rather than crashing or
  quietly producing nonsense. Those are the tests that keep "one template
  definition" true rather than aspirational.
- **Headless means headless.** `test_recognition_engine.py` starts a fresh
  interpreter and asserts that a sheet can be read with no `PySide6` module
  loaded. Checking `sys.modules` in-process would prove nothing, because
  pytest-qt has already imported Qt.
- **Fixtures are generated, never hand-written.** A hand-written result drifts
  from what the engine emits, and the tests written against it then verify a
  fiction. Regenerate with `scripts/build_recognition_fixtures.py` and read the
  diff.

### Running the Phase 3 QA tools

```bash
# A small labelled dataset, reproducibly
python -m omr_scanner.tools.make_dataset out/dataset \
    --template examples/templates/ece_0000_sample.omrt --count 250 --seed 20260918

# Score the engine against it, classify every disagreement, and report
# accuracy per kind of test case
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template examples/templates/ece_0000_sample.omrt \
    --report out/benchmark --categories 0

# Compare a change against the run before it, headline metrics and categories
python -m omr_scanner.tools.benchmark_recognition out/dataset \
    --template examples/templates/ece_0000_sample.omrt \
    --baseline out/benchmark/summary.json
```

Both are in the application too, under *Tools > Developer / Testing*; the
benchmark runs inside the Scan page rather than in a window of its own, so it
exercises the pipeline users actually get.

Generated datasets, reports and diagnostics are git-ignored: the generator plus
its seed reproduces them exactly, so the seed is worth committing and the
gigabytes are not.

**What a synthetic number is worth.** These datasets measure regression
consistency and controlled edge-case handling. They are *not* evidence of
real-world accuracy, and no report, changelog entry or commit message should
present them as such. Every report the tools write carries that sentence in the
file itself, because a `summary.json` with a 0.999 in it will eventually be
pasted into a slide.

### Large-batch progress and the ETA estimator

`BatchProgressTracker` is tested headlessly with an injected clock
(`tests/unit/test_batch_progress.py`, 57 tests), because an estimator that can
only be exercised by watching a progress bar is an estimator nobody tests. The
fake clock turns "does the estimate converge over twenty minutes" into a test
that runs in microseconds and gives the same answer every time.

Covered: warm-up (no estimate from one sample), multicore start-up (a slow
first sheet must not dominate), steady and variable throughput, a sustained
slowdown, a stall (the estimate grows rather than freezing, and never divides
by zero), completion, cancellation, an empty batch, a single-sheet batch, a
clock that goes backwards, and eight threads recording completions at once.

The **10,000-job simulation** lives there too, and runs in well under a second:
ten thousand completions with a realistic mixture of successes, reviews and
failures, asserting that the counts reconcile exactly, the fraction reaches
1.0, the estimate falls monotonically, and the tracker's memory does not grow
with the job count.

`tests/gui/test_batch_progress_gui.py` then checks what a person sees: the
panel renders a constructed snapshot correctly (including "9,999 / 10,000 is
not 100%"), a real twelve-sheet batch drives it to exactly 100%, a failed scan
still advances it, no dialog appears for a bad file, cancellation is immediate
in the interface and honest in the final state, and ten thousand rows produce
exactly one progress bar.

Do not generate ten thousand real images to test a progress bar. The
simulation covers the counting and the estimate; a few dozen synthetic sheets
cover the wiring.

## Testing durable batches (Phase 5)

| Level | Where | Asserts |
|---|---|---|
| The state rules | `tests/unit/test_batch_store.py` (35 tests) | Resume selection, cancellation, crash repair, final-status derivation, the four compatibility differences, recorder buffering and storage-failure handling - against a real SQLite file with hand-built results, so the *rules* are tested without the engine that ordinarily drives them. |
| The pipeline | `tests/integration/test_batch_persistence.py` (17 tests) | The real engine, real rendered sheets and a real project database: a normal batch, broken images, an unregistrable page, resume, a simulated interruption, one vs. many workers, duplicate identifiers, source integrity and reopening. |
| The GUI | `tests/gui/test_scan_persistence.py` (20 tests) | The real page, a real project and a real `QThread`: cancellation, resume, retry, filtering, responsiveness, close-during-processing, and crash recovery through `MainWindow`. |

**Use a real database, not a mock.** Every interesting property of the store is
a property of the database - the foreign key, the unique constraint, the
transaction boundary, the grouped count. A mocked session asserts that the code
calls the functions it calls, which is the one thing worth nothing. The unit
tests open a real SQLite file in `tmp_path`; they still run in about two
seconds.

**The mandatory exit criterion is a hash comparison.** Nothing else proves the
originals were untouched:

```python
before = {path: sha256(path.read_bytes()).hexdigest() for path in paths}
run_batch(..., options=BatchOptions(output_dir=out, rename_with_identifier=True))
after = {path: sha256(path.read_bytes()).hexdigest() for path in paths}
assert after == before, "a source scan was modified by processing"
```

Run with renaming **on** and a corrupt file in the list - those are the two
paths most likely to touch a source file, and testing the quiet case proves
nothing.

**Assert that resume did less work, not just that the total came out right.**
A resume that silently re-read everything produces exactly the same final
counts as one that did not:

```python
report = harness.resume()          # or page.resume_batch()
assert report.total == 2           # the two that were left, not all four
assert summary_after.processed == 4
```

**Test the GUI event loop with a timer, not with a feeling.** A `QTimer` that
could not tick if recognition were happening on the GUI thread is the honest
form of "the window stayed responsive":

```python
heartbeat = QTimer(); heartbeat.setInterval(20)
heartbeat.timeout.connect(lambda: ticks.append(1)); heartbeat.start()
with qtbot.waitSignal(page.batch_finished, timeout=...):
    page.process_all()
assert len(ticks) > 3, "the GUI thread was blocked while the batch ran"
```

**Simulating a crash.** There is no way to kill a live process mid-write from
a test, so the state a crash *leaves* is constructed instead - rows marked
`queued`/`processing`, the database closed without a finalise - and recovery is
asserted against that. The limitation is recorded in
`development/PHASE_05_HANDOFF.md` §7 rather than papered over.

## Testing the Calibration workflow (Phase 4)

Three levels, and the reason for each mirrors the recognition-testing table
above:

| Level | Where | Asserts |
|---|---|---|
| The judgement rules | `tests/unit/test_calibration_service.py` (34 tests) | Every threshold and boundary `evaluate_calibration` uses - unusable-bubble fractions, geometry-class vs. cosmetic alignment warnings, the systematic-ambiguity fraction, the near-threshold band, the nothing-marked-anywhere rule - against hand-built `ScanResult`s, so the *rules* are tested independently of anything that would ordinarily produce one. Also that the answer counts come from `MarkStatus` and not from the value string. |
| The real pipeline | `tests/integration/test_calibration_workflow.py` (10 tests) | Real templates, real synthetic renders, the real `RecognitionEngine`: a deliberately displaced-marker template fails to register and produces `CalibrationStatus.FAILED`; a small marker offset (inside the default `search_radius`) still registers; a threshold change on a real `CalibrationSession` flips a real classification while the raw score and the registration stay untouched; saving and reloading a calibration round-trips; editing recognition settings afterwards invalidates it; a template document with no `calibration` key (pre-Phase-4) still loads. |
| The GUI | `tests/gui/test_calibration_page.py` (37 tests, A-J) | The whole workflow through `CalibrationPage`'s public commands - load, add scans, run, overlay geometry matching the engine's own bubble and marker coordinates exactly, click-to-inspect, field filtering, threshold controls (including that a threshold change starts **no** worker), reset/defaults, save-and-stale-detection, the multi-scan sample summary, per-position field diagnostics, the original-scan view, overlay/image alignment across zoom levels, and both miscalibration paths end to end. |

**The major Phase 4 geometry test.** Overlay coordinates must equal the
engine's own, not merely be close to them:

```python
overlay._bubbles  ==  {(b.zone_id, b.row, b.column) for b in result.bubbles}
one.x == matching_result_bubble.x   # exact, not approximate
```

**Test the data path, not the agreement.** A test that asserts two
computations agree passes just as happily when both are wrong in the same way -
which is exactly how an earlier build shipped an overlay that drew the printed
bubble while claiming to show the sampled region. The replacement varies the
*source* and requires the display to follow:

```python
narrow = RecognitionEngine(RecognitionOptions(
    metrics=BubbleMetricsConfig(sample_radius_ratio=0.4), ...))
bubble = narrow.process(path, template).bubbles[0]
assert bubble.sample_half_width == pytest.approx(bubble.width / 2.0 * 0.4)
```

A GUI-side reimplementation using the default ratio passes an agreement test
and fails this one.

**Overlay and image must stay locked together at every zoom.** A double
transform - coordinates converted once by the engine and again by the viewer -
produces a display that looks nearly right and drifts as you magnify it:

```python
pixmap_point = QPointF(bubble.x * result.preview_scale, bubble.y * result.preview_scale)
for zoom in (0.25, 1.0, 4.0):
    preview._apply_zoom(zoom)
    assert (preview.mapFromScene(QPointF(bubble.x, bubble.y))
            == preview.mapFromScene(background.mapToScene(pixmap_point)))
```

and a dedicated cross-check
(`test_the_question_number_formatter_agrees_with_the_engines_own_numbering`)
walks every bubble the page would ever label "Question N" and asserts that
`N` names a question the engine actually recognised - the safety net for the
one place the calibration page *does* do a presentation-level calculation
(reading `QuestionBlockFieldDefinition`'s own row/column convention to build
a label), so that calculation can never silently drift from what `zone_groups`
means by the same convention.

**The load-bearing test.** A template whose registration markers are shifted
well past their `search_radius` must come back `CalibrationStatus.FAILED`
with **no** fields, answers or bubbles at all - never a plausible wrong
answer:

```python
result.registration is RegistrationStatus.FAILED
result.fields == ()
result.answers == ()
result.bubbles == ()
report.status is CalibrationStatus.FAILED
```

Exercised at the unit level (hand-built), the integration level (a real
mismatched template against a real rendered sheet) and the GUI level (the
same, through the page), and again in
`scripts/run_gui_smoke_tests.py` against the **real** sample and template
(`examples/ECE-0000.png` / `examples/templates/ece_0000_sample.omrt`).

**The load-bearing test's harder sibling.** Displacing the *markers* makes
registration fail, which is loud. Displacing only the **zones** does not: the
markers are still found, the transform is still exact, no sampling window
leaves the page, and every group reads a *confident blank* from bare paper.
Every registration-level assertion above is satisfied by a sheet that has been
completely misread, so the test must assert on the operator-visible verdict
instead:

```python
displaced = shifted_zones(template, dx=0.02, dy=0.02)   # markers untouched
result = engine.process(path, displaced)
report = evaluate_calibration(result, displaced)

assert result.registration is not RegistrationStatus.FAILED   # genuinely fine
assert report.bubbles_unusable == 0                            # all on-page
assert report.status in (CalibrationStatus.NEEDS_REVIEW, CalibrationStatus.FAILED)
```

Parametrised over three displacements (2%, 5%, 12% of the page) because they
fail differently: the smallest produces *no* marks anywhere and is caught by
`NO_MARKS_DETECTED`, the larger ones catch the edges of neighbouring bubbles
and are caught by `SYSTEMATIC_AMBIGUITY`. Both shapes are also smoke-checked
against the genuine scanned sheet, which is the only place in the suite where
either failure mode is proven on real paper rather than a synthetic render.

**Threshold propagation, without repeating registration.** The same synthetic
sheet is measured once (`RecognitionEngine.open_session`), then decided twice
against two different `RecognitionSettings`:

```python
before = session.recompute(template).answer(N)      # e.g. "uncertain", faint mark
after  = session.recompute(looser_template).answer(N)
after.top_fill == before.top_fill                   # raw score unchanged
after.value != before.value                          # classification changed
after.canonical_width == before.canonical_width       # never re-registered
```

**`.isVisible()` and waiting on a run.** See the qtguitesting scenario
reference (`.claude/skills/qtguitesting/references/omrflow_gui_test_scenarios.md`,
"Calibration page") for two gotchas specific to this page: `.isVisible()` is
unreliable on a page that was never `.show()`n, and `CalibrationPage.run_finished`
carries no payload, unlike `ScanPage.batch_finished`.
