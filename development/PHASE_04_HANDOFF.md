# Phase 4 handoff — Template Calibration & Validation

**Implemented:** 2026-09-18
**Audited and corrected:** 2026-09-18 (independent review pass — see §0)
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2, OpenCV
5.0.0, NumPy 2.5.3

This document is the entry point for whoever continues the work. It records
what the Calibration workflow does, how it was verified, and what it does
not do. Read alongside `docs/calibration_workflow.md` (the user-facing
procedure), `docs/TEMPLATE_FORMAT.md` (the `calibration` field) and
`docs/ARCHITECTURE.md` ("The Calibration workflow (Phase 4)").

**Phase 3 is still not validated against a large real-world dataset.**
Phase 4 makes that validation *safer and more systematic to perform* - it
does not perform it. See §9.

---

## 0. What the audit pass changed, and why

The first implementation passed its own tests and its GUI smoke suite. An
independent review found four defects that those tests could not have caught,
because in each case the test and the code shared the same wrong assumption.
They are recorded here rather than quietly fixed, because three of them are
the kind that recur.

**0.1 The overlay drew a region recognition never measured.** `BubbleView`
carried only the *printed* bubble size, and both the overlay and the
click-target used it. The sampler reads an ellipse at
`sample_radius_ratio` (0.62) of the printed half-axes - on the real sample
sheet, **22.3 px across inside a printed 36.0 px bubble**, roughly 2.6x the
area. An operator checking "does the template line up" was shown a ring 60 per
cent wider than anything that was actually read, and a template whose windows
were half off the printed bubbles could still look enclosed.

Fixed at the source rather than in the GUI: `BubbleMeasurement` now records
`sample_half_width`/`sample_half_height` at the moment it builds the sampling
mask, `BubbleView` carries them through, and the overlay has separate
**Sampling** (measured ellipse) and **Selections** (printed bubble) layers,
plus a **Centres** layer. The regression test is a shared-data-path test, not
an agreement test: run the engine with a non-default `sample_radius_ratio` and
the displayed window must move with it.

**0.2 A displaced template that still registered was reported as passing.**
This is the defect that matters most. Two different failures were being
conflated:

| | Markers displaced | Only the **zones** displaced |
|---|---|---|
| Registration | fails | **succeeds, cleanly** |
| Bubbles off-page | n/a | **none** |
| Alignment warnings | n/a | **none** |
| Every group reads | nothing at all | a **confident BLANK** from bare paper |
| Verdict *before* the audit | `FAILED` (correct) | **`PASSED_WITH_WARNINGS`** |

Measured, before the fix, on a synthetic sheet with every zone shifted by 2
per cent of the page: `status=passed_with_warnings`, Student ID `______`, all
20 answers blank, **zero** review items, zero unusable bubbles. Nothing in the
verdict, and nothing on screen, said the values had been read from the wrong
part of the sheet.

Fixed by adding one rule with no tunable parameter: if the sheet registered
and **not one** of its response positions carried a mark, the verdict is
`NEEDS_REVIEW` with a `NO_MARKS_DETECTED` finding. Deliberately not `FAILED` -
a genuinely blank practice sheet produces identical evidence, and the message
says so and names the overlays to switch on. Partial displacement is caught by
the pre-existing systematic-ambiguity rule. All three offsets tested (2%, 5%,
12%) now report `NEEDS_REVIEW`; the correct template still passes.

**0.3 Two counts re-derived a classification the engine had already made.**
`answers_multiple` was `"-" in answer.value` - a string heuristic that would
count a single answer as a double mark on any template whose option labels
contain a hyphen. `answers_blank` was `value == "" and not needs_review`,
which silently dropped blank answers whose confidence fell below
`min_confidence`. Both now read `AnswerView.status`, which is the engine's own
`MarkStatus`.

**0.4 The quality summary's "single" count was arithmetically wrong.** It was
`total - blank - multiple - needs_review`, but those categories overlap: a
double mark is both `MULTIPLE` *and* flagged for review, so it was subtracted
twice. A sheet with 10 blanks and 3 double marks under-reported its clean
answers by 3, then clamped negatives to zero. Replaced with an explicit
`answers_single` counted from `MarkStatus.RESOLVED`.

Two smaller corrections: a scan finishing while a slider was being dragged was
judged against the new thresholds but displayed against the old ones (it is
now re-decided against the settings in force when it lands), and the page now
states in words whether the working thresholds match the template's saved ones
rather than leaving it to be inferred.

Two requirements were implemented that the first pass had left as dead code or
unused data: the **Original scan** view (`CalibrationSession.original_preview`
existed but nothing called it) and **per-position Student ID / Set Code
diagnostics** (`CharacterView` carried every number needed and none were
displayed).

---

## 1. Phase objective

Let an operator load a saved `.omrt` template, add one or more representative
real scans, run the existing Phase 3 pipeline on them in a diagnostic mode
that shows every intermediate measurement, adjust recognition thresholds and
see the effect immediately, and get an explicit validation status before
trusting the template with a batch of thousands of sheets.

Built as a fourth workflow stage, **Calibrate**, between Template and Scan -
not as a second recognition engine, and not as a second geometry editor.

---

## 2. Implementation summary

### `src/omr_scanner/domain/template.py` (extended)

`CalibrationRecord` (new Pydantic model): `validated_at`, `sample_count`,
`engine_version`, `status`, `geometry_fingerprint`, `recognition_fingerprint`.
`OmrTemplate` gained the additive field `calibration: CalibrationRecord` and
three methods: `geometry_fingerprint()` (hashes `page`, `registration_markers`,
`orientation_marker`, `zones` - excluding `recognition`), `recognition_fingerprint()`
(hashes the template-level and every zone-level `RecognitionSettings`), and
`is_calibration_current()` (compares the stored record's two fingerprints
against freshly computed ones). `with_calibration()` returns a copy carrying a
new record. See `docs/TEMPLATE_FORMAT.md`, "`calibration`".

### `src/omr_scanner/services/recognition_service.py` (extended - see §4)

`_recognise` split into `_measure_sheet` (load, register, measure - everything
a settings change must not repeat) and `_decide_and_build` (decide, build
views, present - everything a settings change is allowed to repeat). New
`CalibrationSession` class and `RecognitionEngine.open_session()`. `MarkerView`
gained `canonical_x/y` (detected marker reprojected through the fitted
homography) and `expected_x/y` (the template's declared centre in canonical
pixels), both additive with a `0.0` default.

### `src/omr_scanner/services/calibration_service.py` (new)

`CalibrationStatus` (four-state enum), `CalibrationFinding`, `CalibrationReport`
(one scan), `CalibrationSampleReport` (a sample, worst-of), `evaluate_calibration()`,
`aggregate_calibration()`, `apply_calibration()`, `write_calibration_report()`,
`separation_label()`. Every rule documented and tested; see §7.

### `src/omr_scanner/gui/calibration/` (new package)

| Module | Contents |
|---|---|
| `page.py` | `CalibrationPage`, `CalibrationPageState`, `TestScanEntry` - the workflow stage. |
| `worker.py` | `CalibrationWorker`, `CalibrationSessionResult` - opens sessions off the GUI thread. |

### `src/omr_scanner/gui/scan/preview.py` (extended)

`OverlayItem` gained marker painting (`show_markers`, `set_content(..., markers=...)`)
and the expected/detected/mismatch colour constants. `ScanPreviewView` gained
`clicked_scene_point` (canonical-page coordinates of a plain left click) and
`set_overlay(..., markers=...)`. Both additive; the Scan page's own behaviour
is unchanged (it never enables `show_markers` and does not connect to the new
signal).

### `src/omr_scanner/gui/template_designer/page.py`, `gui/main_window.py`,
### `gui/scan/page.py`, `gui/pages/catalog.py` (extended)

- `TemplateDesignerPage.open_template_at(path)` - the "Edit Template" shortcut,
  a public wrapper around the existing dialog-driven open path.
- `MainWindow.edit_template()`, `MainWindow.start_calibration()` - navigation
  glue, mirroring `start_benchmark()` from the developer tools.
- `catalog.py`: a `"calibration"` `WorkflowPageSpec` inserted between
  `"template"` and `"scan"` - the navigation list is now nine stages.
- `ScanPage`: a small, non-blocking `calibrationWarningLabel` shown when the
  loaded template has never been calibrated or has gone stale. It never
  blocks *Process All*.

### Not built, on purpose

A score-distribution histogram (`separation_label()` reports a plain
well-separated / some-overlap / poorly-separated / not-enough-data string
instead - see its docstring for why); a raw homography matrix in the default
view (folded into "Advanced Diagnostics"); a second geometry editor; mandatory
calibration before batch processing (the Scan page warns, it does not block).

---

## 3. Public API

```python
from omr_scanner.services import (
    RecognitionEngine, RecognitionOptions,
    CalibrationStatus, evaluate_calibration, aggregate_calibration,
    apply_calibration, write_calibration_report, separation_label,
    load_template, save_template,
)

template = load_template(Path("sheet.omrt"))
engine = RecognitionEngine(RecognitionOptions(with_preview=False, keep_bubble_measurements=True))

# One scan, registered and measured once.
session = engine.open_session(Path("scan.png"), template)
result = session.recompute(template)          # decide against the saved settings
report = evaluate_calibration(result, template)
report.status                                   # CalibrationStatus.PASSED | ...

# Retune a threshold - no file read, no registration.
looser = template.model_copy(update={
    "recognition": template.recognition.model_copy(update={"fill_ratio_threshold": 0.45})
})
result2 = session.recompute(looser)              # same measurements, re-decided

# A sample of several scans.
sample = aggregate_calibration([report, ...])
sample.status                                    # worst of the sample

# Record and save a run.
stamped = apply_calibration(looser, status=sample.status, sample_count=len(sample.reports))
save_template(stamped, Path("sheet.omrt"))
stamped.is_calibration_current()                 # True, until geometry or settings move again
```

The GUI entry point is navigating to the "Calibrate" stage in `MainWindow`, or
`CalibrationPage.load_template_from(path)` / `add_scan_paths([...])` /
`run_selected()` / `run_all()` directly, exactly as `ScanPage` is driven in
tests.

---

## 4. Calibration architecture: where each thing comes from

| Question | Answer |
|---|---|
| Registration geometry | `omr_scanner.imaging.alignment.align_sheet` (Phase 1), reached through `_measure_sheet` - unchanged. |
| Bubble geometry (position, size) | `template.zones[*].grid.bubble_center(row, column)`, projected to canonical pixels inside `_measure_sheet` - unchanged. |
| Raw per-bubble scores | `omr_scanner.imaging.metrics.measure_bubble` (fill ratio, mean darkness, contrast, paper level, ink threshold) - unchanged, cached in `_MeasuredSheet.measurements`. |
| Classification (filled/blank/multiple/uncertain) | `omr_scanner.recognition.decide.decide_group`, called from `recognise_template` inside `_decide_and_build` - unchanged in behaviour, callable repeatedly. |
| Marker "detected" position, canonical pixels | The detected marker's source-pixel centre, mapped through `alignment.transform_matrix` via `imaging.geometry.apply_transform` - the *same* transform that rectified the page. |
| Marker "expected" position, canonical pixels | `template.marker_by_role(role).center`, scaled by the canonical page size - no image involved at all. |
| Calibration verdict | `services.calibration_service.evaluate_calibration`, reading only fields already on `ScanResult`/`CalibrationReport` - never a pixel. |

Nothing here is a second implementation of anything Phase 1-3 already does.
The one new *capability* is `CalibrationSession`, and it is a capability, not
an algorithm: it is `_measure_sheet` plus `_decide_and_build` called through a
seam that lets the second one repeat on its own.

---

## 5. Features implemented

Against the acceptance criteria (all met):

- Load a saved template; add one or more representative scans (PNG, JPEG,
  TIFF, BMP - whatever `scan_import.collect_scan_files` already accepts).
- Run the existing pipeline in diagnostic mode (`keep_bubble_measurements=True`,
  `with_preview=True`) via `CalibrationWorker`.
- Inspect detected registration markers, their expected positions, and the
  distance between them, as an overlay layer.
- Inspect orientation resolution (quality summary: "resolved" vs.
  "assumed/unresolved").
- Bubble sampling geometry overlaid on the scan, from the engine's own
  coordinates.
- Individual bubble scores inspectable by clicking.
- The current threshold visible and editable (slider + exact spin box), for
  all four `RecognitionSettings` fields.
- Threshold changes propagate to results, the overlay and the quality summary,
  with no worker run.
- Student ID, Set Code and Question diagnostics (the quality summary and the
  field filter).
- Multiple and ambiguous marks visible (competing bubble scores stay on the
  overlay; the summary reports counts, never a grading judgement).
- Per-scan and per-sample (aggregate) summaries.
- A deliberately miscalibrated template is flagged `FAILED`, not a false pass.
- Reset to template / reset to defaults.
- Save to template, with an explicit confirmation naming the status about to
  be recorded.
- Existing templates remain compatible (additive field, defaulted).
- Existing Phase 1-3 tests still pass (§8).
- Qt GUI tests exist and `qtguitesting` was used (§8, §9).

---

## 6. Phase 3 changes

Three changes, all additive or behaviour-preserving - see
`development/PHASE_03_HANDOFF.md`'s own 2026-09-18 update for the pointer back
here.

0. **`imaging.metrics.BubbleMeasurement` gained `sample_half_width` /
   `sample_half_height`** (audit pass, §0.1), defaulting to `0.0`, populated on
   every return path of `measure_bubble` including the unusable ones. The
   values were already being computed to build the sampling mask; they are now
   reported rather than discarded. No decision reads them - they exist so that
   anything drawing "what was measured" cannot draw something else. The
   interior mask is now built from the same two locals, so the recorded value
   and the sampled region cannot drift apart.

1. **`recognition_service._recognise` split** into `_measure_sheet` and
   `_decide_and_build`, joined by a small `_MeasuredSheet` value and an
   internal `_SheetMeasurementError` control-flow exception. Pure extraction:
   every statement moved, none rewritten. `StageTimings` is reassembled from
   the two halves (`load`/`register`/`measure` frozen from the first call;
   `decide`/`present` fresh on every `recompute()`), so `elapsed_seconds` on a
   `recompute()` result honestly reports only the (small) work that call
   actually did.
2. **`MarkerView` gained `canonical_x/y` and `expected_x/y`**, both `0.0` by
   default. Additive; `ScanResult.to_dict()`/`from_dict()` needed no changes
   because their serialisation walks a dataclass's declared fields generically.

**Evidence that no recognised value moved:** the full pre-existing recognition,
batch-processing, parallel-batch and benchmark test suites
(`tests/unit/test_recognition_contract.py`, `test_recognition_fixtures.py`,
`tests/integration/test_recognition_engine.py`, `test_recognition_pipeline.py`,
`test_sample_sheet_recognition.py`, `test_recognition_tools.py`,
`test_batch_processor.py`, `test_parallel_batch.py` - 282 tests) pass unchanged
after the refactor, with no test edited to accommodate it.

---

## 7. Diagnostic parameters exposed

Exactly the four fields `omr_scanner.domain.template.RecognitionSettings`
already declares - no new threshold was invented for this phase:

| Control | Field | Range |
|---|---|---|
| Bubble fill threshold | `fill_ratio_threshold` | `[0, 1]` |
| Blank threshold | `blank_ratio_threshold` | `[0, 1]`, must stay below the fill threshold |
| Ambiguity margin | `ambiguity_margin` | `[0, 1]` |
| Minimum confidence | `min_confidence` | `[0, 1]` |

The calibration-*judgement* constants (`UNUSABLE_BUBBLE_FAILURE_FRACTION = 0.05`,
`SYSTEMATIC_AMBIGUITY_REVIEW_FRACTION = 0.30`, `NEAR_THRESHOLD_BAND = 0.05`,
`MARKER_MISMATCH_PX = 3.0`, `GEOMETRY_WARNING_CODES`) are not user-facing
controls; they are documented, named module constants in
`calibration_service.py` and `gui/scan/preview.py`, each with the reasoning
for its value in its own docstring, each covered by a boundary test.

---

## 8. Automated tests added

| Level | File | Count | Covers |
|---|---|---|---|
| Unit | `tests/unit/test_calibration_service.py` | 34 | Every judgement rule and its documented boundary, against hand-built `ScanResult`s. Audit pass added `TestNothingMarkedAnywhere` (4) and `TestAnswerCountsComeFromMarkStatus` (3), the latter including the hyphenated-label and the blank-needing-review regressions and an explicit assertion that the four answer counts are *not* related by subtraction. |
| Unit | `tests/unit/test_bubble_metrics.py` | +4 | `TestReportedSampleGeometry`: the recorded window is the configured fraction of the printed one, is strictly smaller than it, follows a custom ratio, and is still reported for an unusable measurement. |
| Integration | `tests/integration/test_calibration_workflow.py` | 18 | The real `RecognitionEngine` against real templates and synthetic sheets: registration-failure-is-never-a-pass, small-vs-large marker offsets, threshold propagation through a real `CalibrationSession`, apply/save/reload, staleness after an edit, pre-Phase-4 document compatibility. Audit pass added `TestADisplacedTemplateThatStillRegistersIsNeverAPass` (3, parametrised over three offsets) and `TestOverlayGeometryComesFromTheSampler` (3, covering an identifier, a set-code and a question bubble, plus the shared-data-path proof). |
| GUI | `tests/gui/test_calibration_page.py` | 37 | A-J: launch/navigation, template/scan management, running a scan, overlay geometry exactness, click-to-inspect (including a cross-check that the question-number label formatter agrees with the engine's own numbering), field filtering, threshold controls (asserting **no** worker starts), save/staleness, multi-scan aggregation, the mismatched-template failure path end to end. Audit pass added the sampled-vs-printed overlay tests, an overlay/image alignment test across three zoom levels, the original-scan-view tests, per-position field diagnostics (§20-22) and the displaced-template end-to-end case. |
| Regression | (existing suites, unmodified) | 282 (recognition/batch) + 556 (GUI) | Confirmed passing after every change in this phase - see §6. |

**On test quality.** The audit's first three findings were each invisible to
the original tests because the test asserted the same thing the code did. The
replacements are written to fail if the *source* changes: the geometry test
varies the sampler's configuration rather than comparing two defaults, and the
miscalibration test asserts on the operator-visible verdict rather than on the
internal signals that happened to be clean.

`tests/gui/test_main_window.py` was updated (not added to): two tests that
hard-coded a navigation row index were rewritten to look the row up by key,
since inserting the Calibrate stage shifted every index after it - this is
the correct fix, not a workaround, and is exactly what those tests' own
existing comments already recommended doing for the *next* stage insertion.

---

## 9. Tests actually executed

All figures below are from the **audited** tree, run to completion on the
environment named at the top of this document.

```text
Baseline before any audit change:
pytest -q -m "not gui"                                            → 1576 passed, 1 skipped

After the audit changes:
pytest tests/unit/test_calibration_service.py
       tests/unit/test_bubble_metrics.py -q                        → 76 passed
pytest tests/integration/test_calibration_workflow.py -q           → 18 passed
pytest tests/gui/test_calibration_page.py -q -m gui                → 37 passed
pytest -q -m "not gui"                                             → 1595 passed, 1 skipped  (95s)
pytest -q -m gui                                                   → 593 passed              (109s)
ruff check .                                                       → All checks passed
mypy                                                                → Success: no issues found in 102 source files
.claude/skills/qtguitesting/scripts/run_gui_smoke_tests.py          → 32/32 checks passed
.claude/skills/qtguitesting/scripts/capture_gui_states.py --only calibration → 10 screenshots written
```

Totals: **2188 passed, 1 skipped**, across both markers.

Nothing was skipped except the one pre-existing, structural skip in
`test_qtguitesting_skill.py` (shared plumbing, not a command a user runs -
unrelated to this phase). No test was deleted or weakened to make a number
look better; the two tests that changed
(`tests/gui/test_main_window.py`, §8) were made index-agnostic because a new
navigation stage shifted every row after it.

**Correction to the pre-audit version of this document**, which recorded the
GUI suite as taking "~92 min": it takes **109 seconds**. The earlier figure was
never measured.

---

## 10. GUI validation

`qtguitesting` scenario 17 ("Calibration (Phase 4)") added to
`.claude/skills/qtguitesting/references/omrflow_gui_test_scenarios.md`, with
its own stable-object-name table. Smoke checks added to
`run_gui_smoke_tests.py`: object names present, a full run against the real
sample (`examples/ECE-0000.png`) and its real template scoring
`passed_with_warnings` with 4/4 markers and 100/100 questions single-marked,
and the mismatched-marker check against the same real template and scan,
scoring `FAILED` with zero fields/answers/bubbles.

Screenshots captured and inspected (`test-output/gui/`, git-ignored). Ten,
after the audit pass:

- `calibration_loaded.png`, `calibration_run_clean.png` - the page as loaded
  and after a clean run against the real sample.
- `calibration_overlay_markers.png` - the top-left marker at 100% zoom: a
  green square on the detected position, a blue cross on the expected one,
  nearly coincident, labelled "top left (0.0px)".
- `calibration_sampling_top.png`, `_middle.png`, `_bottom.png` - the sampling
  and centre overlays at 100% zoom at three points down the page. Captured at
  three heights on purpose: a scale or perspective error accumulates
  downwards, so checking one region proves nothing about the others.
  **Inspected: the dotted sampled ellipses and the magenta centres sit
  precisely on the printed bubbles at all three, with no visible drift.**
- `calibration_threshold_ambiguous.png` - the fill threshold lowered to 0.30.
- `calibration_mismatched_failed.png` - displaced *markers*: a blank preview,
  "Calibration failed" in red, "Markers detected: 0 / 4", "Questions: 0
  total". Inspected and confirmed to show no plausible wrong result of any
  kind.
- `calibration_displaced_zones.png`, `calibration_displaced_zoomed.png` -
  displaced *zones* on the same real scan. **Inspected: the status reads
  "Needs review" in amber, the summary reads "Marks detected in 2 of 110
  response positions", the Student ID reads `______`, and at 100% zoom the
  sampled ellipses are visibly below and right of the printed bubbles - the
  operator can see the cause, not just the verdict.** This is the state that
  reported "Validation passed with warnings" before the audit.

A real bug was found and fixed during the original work: the qtguitesting
harness's `CalibrationHarness.run_all()` connected `run_finished` (a
zero-argument signal) to `received.append` the way `ScanHarness.run_batch()`
connects `batch_finished` (which *does* carry a payload) to it - a `TypeError`
inside Qt's signal dispatch that was silently swallowed, leaving the wait loop
spinning until its 120-second timeout. Fixed by connecting to a zero-argument
callback instead; documented in the scenario reference so the next signal
added to either harness does not repeat it.

---

## 11. Miscalibration handling - the exact mechanism

Stated once, precisely, because it is the one thing this phase must not get
wrong. There are **two** distinct ways a template can be wrong, and only the
first announces itself through registration.

### Shape 1 - the markers do not match (registration fails)

1. `align_sheet` (Phase 1, unchanged) either finds all four registration
   markers and resolves orientation, or raises `ImagingError`.
2. `_measure_sheet` catches that and raises `_SheetMeasurementError` carrying
   a `ScanResult` with `registration=RegistrationStatus.FAILED`,
   `fields=()`, `answers=()`, `bubbles=()` - **by construction**, because
   the code path that builds a successful result (`_decide_and_build`) is
   never reached.
3. `CalibrationSession` catches the same exception at `open_session()` time
   and caches the failed result; every later `recompute()` call returns it
   unchanged, because no recognition threshold could change *why* a marker
   was not found.
4. `evaluate_calibration` checks `result.registration is RegistrationStatus.FAILED`
   **first, before any other rule**, and returns `CalibrationStatus.FAILED`
   with a `REGISTRATION_FAILED` finding naming the actual reason
   (`result.registration_message`). No other check in that function can ever
   run for such a result.
5. The GUI shows the failure status in red, the quality summary states
   "Registration: FAILED" and the actual reason, the sample table's Status
   column reads "Calibration failed", and the preview shows nothing to look
   at - because there is nothing rectified to show.

### Shape 2 - the markers match but the bubble geometry does not

Registration succeeds perfectly. This is the dangerous one, and it has two
sub-cases:

**2a. The windows fall off the page.** If more than 5% of a sheet's bubbles
come back `usable=False` (their sampling window fell off the rectified page or
could not gather enough pixels - `imaging.metrics.measure_bubble`'s own,
unmodified rule), `evaluate_calibration` returns `FAILED` with an
`UNUSABLE_BUBBLES` finding naming the exact count and fraction.

**2b. The windows stay on the page but land on blank paper.** Nothing above
fires: the markers are found, the transform is exact, no window leaves the
page, and every response group reads a *confident* `BLANK`. This was the
audit's most serious finding (§0.2); before the fix it reported
`PASSED_WITH_WARNINGS`. Two rules now cover it:

- `NO_MARKS_DETECTED` - the sheet registered and **not one** of its response
  positions carried a mark. `NEEDS_REVIEW`, with a message naming both
  possible causes and the overlays to switch on. The threshold is literally
  zero, so there is no tuned constant to defend; and the verdict is
  deliberately not `FAILED`, because a genuinely blank sheet produces
  identical evidence and claiming otherwise would be the same overconfidence
  pointing the other way.
- `SYSTEMATIC_AMBIGUITY` (pre-existing) - partial displacement, where the
  windows catch the edges of neighbouring bubbles, drives more than 30% of
  positions into review.

Measured on the real sample sheet with every zone shifted by 2% of the page:
`registration=registered_with_warning`, `status=needs_review`, marks detected
in 2 of 110 positions, `bubbles_unusable=0`. On the same sheet with the
correct template: `passed_with_warnings`, 110 of 110 positions marked. Both
are `qtguitesting` smoke checks, and both are captured as screenshots.

---

## 12. Known limitations

- **Phase 3 recognition remains pending validation with a sufficiently large
  real-world dataset.** This phase makes that validation safer and more
  systematic to carry out; it is not a substitute for it, and no calibration
  run in this codebase - synthetic or against a handful of real scans - should
  be read as such.
- The "geometry problems are fixed in the Template Designer" workflow has been
  exercised as a navigation round trip (`open_template_at`, confirmed by test),
  not as a full manual "notice a displacement, jump to the designer, fix it,
  return, re-run" session by a human operator.
- The systematic-ambiguity and unusable-bubble fractions
  (`SYSTEMATIC_AMBIGUITY_REVIEW_FRACTION = 0.30`,
  `UNUSABLE_BUBBLE_FAILURE_FRACTION = 0.05`) are reasoned, documented defaults,
  not values tuned against a corpus of real miscalibrated templates - there is
  no such corpus yet. They may need adjustment once one exists.
  (`NO_MARKS_REVIEW_THRESHOLD` is exempt: it is zero, and is a statement about
  what the evidence can support rather than a tuned fraction.)
- **A partially displaced template is caught, but not always explained.** The
  displacement range between "everything blank" and "a third of positions need
  review" is covered by the systematic-ambiguity rule, which reports *that*
  something is wrong without distinguishing a geometry problem from genuinely
  faint sheets. The overlays make the difference obvious to a human in
  seconds; the verdict alone does not. Closing that gap properly needs a
  measure of whether a sampling window sits on printed bubble at all, which
  would be new pixel analysis and was deliberately not invented for this
  phase.
- **Calibration cannot distinguish a blank sheet from a displaced template.**
  It says so, and refuses to pass either. That is the honest answer given the
  evidence, not a limitation that can be engineered away without ground truth.
- `separation_label()` is a simple, documented heuristic (a mean-gap-to-spread
  ratio over leading vs. non-leading bubbles), explicitly not a fitted
  statistical model - see its docstring for why a simpler alternative was
  chosen over a histogram.
- The Calibration page's "Advanced Diagnostics" panel reports the same
  `ScanQuality` numbers the Scan page's diagnostics mode already computes; it
  does not expose the raw homography matrix, per the brief's own guidance that
  the default interface stay usable without computer-vision background.
- No dedicated performance measurement of `CalibrationSession.recompute()` at
  scale (many scans, many rapid slider movements) beyond the informal
  per-call timing already recorded in `CURRENT_STATE.md` (~9 ms on a
  100-question synthetic sheet). It is expected to remain fast because it is
  pure Python over already-measured, in-memory data, but this has not been
  stress-tested.

---

## 13. Phase 3 validation status

Explicitly, as required:

**Phase 3 recognition remains pending validation with a sufficiently large
real-world dataset.**

---

## 14. README

`README.md`'s development-status table, feature description and testing
checklist were updated as part of this work to add Phase 4 as *Implemented;
testing in progress*, alongside a description of the workflow and its
verified behaviour on the real sample. Phase 3's own status line was left
exactly as accurate as it already was - not upgraded, not claimed complete.

---

## 15. Phase 5 entry criteria

**Already in place**

- A documented, tested way to verify a template against real scans before a
  batch - the operational gap Phase 5's persistence work would otherwise
  inherit unfilled.
- `CalibrationSession` demonstrates that Phase 3's measure/decide split
  generalises: any future phase that needs "the same measurements, evaluated
  under a different setting" (a resume-after-interruption re-score, for
  instance) has a working precedent to extend rather than invent.
- A small, additive template-versioning pattern (`CalibrationRecord`, two
  content fingerprints, `is_calibration_current()`) that Phase 5's own
  "has this batch's template changed since it ran" question can reuse
  directly.

**Constraints to respect**

1. Everything in `development/PHASE_03_HANDOFF.md` §15 still applies.
2. `omr_scanner.gui.calibration` must not import `cv2`, `numpy`, `imaging` or
   `recognition`, exactly as every other `gui` package - enforced by
   `tests/unit/test_architecture.py`.
3. A `CalibrationSession` is only valid for `recompute()` calls against
   templates sharing the geometry it was opened with; nothing enforces this
   at runtime (it is a documented precondition, not a checked one) - a future
   phase reusing the class should either keep that discipline or add the
   check.
4. Do not let a future phase quietly redefine "calibrated" as "accurate".
   The distinction in §1 and §12 is load-bearing for how this feature may be
   described anywhere in the project.
