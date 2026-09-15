# Phase 1 handoff — OMR Geometry & Alignment Engine

**Completed:** 2026-09-15
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, OpenCV 5.0.0, NumPy 2.5.3

This document is the entry point for whoever continues the work. It records what
the alignment engine does, what it was measured at, and what it does not do.

---

## 1. Phase objective

Turn an arbitrary scan of an OMR sheet into the canonical page its template
describes: detect the four printed registration markers, decide which way up the
page is, correct rotation, skew, perspective, scale and translation in one
operation, and produce a rectified image at exactly the canonical size — or fail
with a reason.

Bubble recognition, the template designer and batch processing were explicitly
out of scope and none of them were touched.

---

## 2. Implementation summary

### `src/omr_scanner/imaging/` (new; replaced a documentation-only package)

| Module | Responsibility |
|---|---|
| `models.py` | The pipeline's typed vocabulary: `Point`, `BoundingBox`, `MarkerCandidate`, `RejectedCandidate`, `ScoredCandidate`, `RegistrationMarkerDetection`, `OrientationHypothesis`, `OrientationResult`, `AlignmentMetrics`, `AlignmentDiagnostics`, `AlignmentResult`, `AlignmentWarning`, `ImageCorner`, `CANONICAL_CORNER_ORDER`. |
| `config.py` | `AlignmentConfig` and its four groups; every tunable value named, documented, validated at construction and defaulted in one place. Derived bounds are computed, never stored twice. |
| `preprocessing.py` | `validate_image`, `to_grayscale`, `working_scale`, `binarize`, `prepare_for_detection`. |
| `marker_detection.py` | `measure_candidate`, `detect_marker_candidates`, `corner_search_region`, `score_candidate`, `select_corner_markers`. |
| `orientation.py` | `orientation_windows`, `determine_orientation`. |
| `geometry.py` | Pure functions: ordering, area, convexity, simplicity, aspect ratio, `validate_page_quadrilateral`, `perspective_transform`, `invert_transform`, `apply_transform`, `reprojection_errors`. |
| `alignment.py` | `align_sheet` — the orchestrator — plus metric and warning collection. |
| `diagnostics.py` | `render_detection_overlay`, `render_normalized_preview`, `summarize`. Returns arrays and strings; writes nothing. |
| `synthetic.py` | `render_sheet`, `apply_distortion`, `distortion_homography`, `project`, `control_point_errors`. Development and test utility. |

### `src/omr_scanner/services/alignment_service.py` (new)

`alignment_config_from_template`, `load_scan_image`, `save_image`. The seam that
keeps `imaging` free of `.omrt` knowledge and of the file system.

### `src/omr_scanner/tools/` (new package)

`align_image.py` and `make_test_sheet.py` — developer command line utilities.

### `src/omr_scanner/errors.py` (extended)

Six new `ImagingError` subclasses, each with a stable `code`.

### Not built, on purpose

No GUI work. The optional debug integration mentioned in the phase brief was
skipped: the engine and its tests were the priority, and the Scan and Template
pages remain honest placeholders.

---

## 3. Public API

```python
from omr_scanner.imaging import AlignmentConfig, align_sheet
from omr_scanner.services import alignment_config_from_template, load_template

config = alignment_config_from_template(load_template(template_path))
result = align_sheet(image=scan, config=config)

result.normalized_image           # canonical page, exactly the configured size
result.transform_matrix           # 3x3, scan -> canonical
result.inverse_transform_matrix   # 3x3, canonical -> scan (for GUI overlays)
result.corner_markers             # four detections, in TL TR BR BL order
result.marker(MarkerRole.TOP_LEFT)
result.orientation.quarter_turns  # 0 | 1 | 2 | 3
result.metrics                    # AlignmentMetrics
result.warnings                   # tuple[AlignmentWarning, ...]
result.diagnostics                # only when config.diagnostics is True
```

`config` is optional; omitting it uses defaults describing the example sheet in
`resources/templates`.

Failures raise a subclass of `ImagingError` carrying `code` and `user_message`.
Nothing returns `None` or a blank result.

---

## 4. Algorithm

```text
validate_image            8-bit, 2-D or 3-D, 1/3/4 channels, minimum size
prepare_for_detection     grayscale copy, downscale to working_max_dimension_px,
                          Gaussian blur, Otsu (or adaptive) threshold; ink = 255
detect_marker_candidates  every external contour measured into a candidate:
                          centroid, area ratio, aspect ratio, rectangularity,
                          solidity, interior ink; each filter that fails is named
select_corner_markers     score each candidate against each scan corner as
                          0.6 * shape + 0.4 * proximity; exhaustive search for
                          the best assignment using four distinct contours
determine_orientation     for each of the four hypotheses: build the candidate
                          homography, prune it if the quadrilateral is not a
                          plausible page, otherwise rectify the orientation
                          mark's expected window out of the scan and measure the
                          ink in it; require min_confidence and min_margin
rotate to canonical order re-anchor the clockwise scan-corner sequence by
                          quarter_turns
validate_page_quadrilateral  convex, simple, large enough, not degenerate,
                          correctly proportioned
perspective_transform     marker centres -> canonical marker-centre targets,
                          plus the inverse
warpPerspective           full source resolution -> canonical size, white border
AlignmentResult           image, transforms, metrics, warnings, diagnostics
```

Three decisions are worth carrying forward:

- **Detection works in scan-corner terms, never canonical ones.** `ImageCorner`
  and `MarkerRole` are distinct types. Before orientation is known, "the
  canonical top-left marker" has no position in the scan.
- **Orientation is decided by rectifying a small window, not by searching for a
  mark.** It tests the template's own claim about the sheet, works for a quarter
  turn plus arbitrary skew, and costs four small warps rather than four full
  rectifications.
- **Marker centres map onto canonical marker centres**, not onto page corners,
  so the printed margin outside the markers is not stretched across the output.

---

## 5. Configuration

`AlignmentConfig` holds the canonical page size, the four expected marker
centres and four groups. The values most likely to need adjustment:

| Field | Default | Effect |
|---|---|---|
| `canonical_width` / `canonical_height` | 1240 x 1754 | Size of the rectified page. From the template in production. |
| `marker_targets` | example sheet's four centres | Where markers land on the canonical page. From the template. |
| `preprocessing.threshold_strategy` | `otsu` | Switch to `adaptive_mean` for a scan with a visible illumination gradient. |
| `preprocessing.working_max_dimension_px` | 3600 | Bounds detection cost; lowering it costs precision roughly in proportion. |
| `marker_detection.corner_search_width` / `_height` | 0.32 | Corner search regions as a fraction of the scan. Set both to 1.0 to detect markers anywhere. |
| `marker_detection.marker_area_tolerance` | 4.0 | Multiplicative tolerance on the expected marker area ratio. Must absorb the dilution a rotated page's larger canvas causes. |
| `marker_detection.min_fill_ratio` | 0.70 | The test that separates a solid marker from a printed frame. |
| `marker_detection.min_candidate_score` | 0.45 | Acceptance floor for a corner. |
| `orientation.min_confidence` / `min_margin` | 0.35 / 0.15 | How firmly the orientation mark must be seen. The margin matters more. |
| `orientation.allow_fallback` | `False` | Guessing the orientation. Off by default. |
| `geometry.max_aspect_ratio_deviation` | 0.30 | Also prunes impossible orientation hypotheses. |
| `diagnostics` | `False` | Retains two working-resolution images per sheet. |

The full list, with units and rationale for each, is in
`docs/IMAGE_PROCESSING.md` § 15 and in the docstrings of
`omr_scanner.imaging.config`.

---

## 6. Tests

455 new tests; 583 in the repository, all passing.

| File | Tests | Covers |
|---|---:|---|
| `unit/test_imaging_geometry.py` | 53 | Ordering (upright, ±30°, translated, perspective, narrow, scrambled), convexity, simplicity, area, aspect, every validation rejection, homography and inverse, reprojection. |
| `unit/test_imaging_config.py` | 35 | Every configuration rejection; derived area and aspect bounds; normalised-to-canonical conversion at three page sizes. |
| `unit/test_imaging_preprocessing.py` | 39 | Every malformed input; grayscale for all layouts; downscaling and the realised scale; three threshold strategies; the adaptive block-size constraint; source immutability. |
| `unit/test_synthetic_sheets.py` | 43 | The generator's ground truth, determinism, each distortion, and two guards on the accuracy metric itself. |
| `integration/test_marker_detection.py` | 40 | Clean pages; eight degradations; decoys inside corner regions; a large logo; noise specks; three resolutions; three threshold strategies; scoring; search regions; centroid stability under damage; selection failures. |
| `integration/test_orientation.py` | 29 | 0/90/180/270 plain, skewed and by confidence; mark reporting; hypothesis pruning; missing and ambiguous marks; fallback and its warning. |
| `integration/test_alignment.py` | 113 | The 40-case accuracy suite; determinism; three resolutions; image-space recovery; colour and BGRA; transforms and inverse; result structure; every metric; every warning. |
| `integration/test_alignment_failures.py` | 41 | Each missing corner; two missing; blank and cluttered pages; heavy cropping; invalid quadrilaterals; every malformed input; the failure contract; damaged and outline-only markers. |
| `integration/test_alignment_diagnostics.py` | 23 | That diagnostics change nothing; their content; overlay and preview rendering; the summary. |
| `integration/test_alignment_service.py` | 20 | Template-to-configuration field by field; an end-to-end alignment driven by a template; image I/O including non-ASCII paths and overwrite refusal. |
| `integration/test_imaging_tools.py` | 17 | Both developer tools: arguments, exit codes, diagnostics output, reproducibility. |
| `unit/test_architecture.py` | +2 | Layering rules extended to the new `tools` package. |

Every randomised distortion is seeded, and reproducibility is asserted directly
rather than assumed: the same specification applied twice must produce
byte-identical images and identical homographies.

No test writes into the repository; the tools and diagnostics tests use
`tmp_path`.

---

## 7. Quantitative accuracy

Measured on the default 1240 x 1754 canonical page. The nine control points per
case are interior features that take **no part** in fitting the homography, so
these numbers measure recovered geometry rather than the fit's own residual.

```text
Synthetic test set:  40 transformations x 9 control points = 360 measurements

Mean control-point error:        0.062 px
Median:                          0.059 px
95th percentile:                 0.145 px
Maximum:                         0.394 px
Failures:                        0
Regression threshold in the suite: 1.5 px
```

Worst five cases:

```text
scale 0.5             0.394 px
perspective 1%        0.234 px
rotate +1             0.218 px
perspective 3%        0.205 px
non-uniform scale     0.164 px
```

The suite covers: no distortion; rotation ±1, ±3, ±5, ±10, +15, 90, 180, 270;
scale 0.5, 0.75, 1.5 and non-uniform; translation; perspective 1, 3, 6 and 10
per cent; dark, bright and low-contrast scans; an illumination gradient; blur 3,
7 and 13; noise σ 5, 15 and 25; JPEG 60 and 25; no margin; slight cropping; and
four combined cases including an upside-down and a quarter-turn one.

### Reprojection error is separately reported and is not accuracy

`mean_reprojection_error_px` and `max_reprojection_error_px` measure how well
the transform reproduces its own four correspondences. Four correspondences
determine a homography exactly, so the value is ~1e-4 px regardless of whether
detection was right. It is recorded because a large value means the solve went
wrong, and the distinction is documented in `docs/IMAGE_PROCESSING.md` § 12.

### Resolution independence and timing

One configuration (scaled canonical size only), 4° rotation plus 2 per cent
perspective. Best of five runs of the whole `align_sheet` call:

| Canonical page | Scan | `working_scale` | Max error | Time |
|---|---|---|---|---|
| 620 x 877 | 753 x 1014 | 1.000 | 0.178 px | 5.9 ms |
| 1240 x 1754 | 1427 x 1947 | 1.000 | 0.087 px | 12.2 ms |
| 1748 x 2480 | 1979 x 2720 | 1.000 | 0.150 px | 17.7 ms |
| 2480 x 3508 | 2773 x 3815 | 0.944 | 0.486 px | 39.4 ms |

At roughly 40 ms per A4 sheet at 300 dpi, a 500-sheet batch spends about 20
seconds in alignment. Phase 5 does not need to optimise this.

---

## 8. Orientation tests

All four cardinal feed orientations, on the default sheet:

| Feed orientation | `quarter_turns` | Expected | Confidence | Margin | Max control-point error |
|---|---|---|---|---|---|
| 0° | 0 | 0 | 1.00 | 1.00 | 0.071 px |
| 90° | 1 | 1 | 1.00 | 1.00 | 0.071 px |
| 180° | 2 | 2 | 1.00 | 1.00 | 0.071 px |
| 270° | 3 | 3 | 1.00 | 1.00 | 0.071 px |

Each is also tested with 4 degrees of skew and 1.5 per cent perspective on top,
and the 180° case is additionally verified by checking that the orientation mark
ends up in the template's declared top-left region of the rectified page.

Failure paths: a sheet with no orientation mark raises `ORIENTATION_NOT_FOUND`
with the confidence of all four hypotheses in the message; a sheet carrying a
second mark where the inverted hypothesis would sample it is refused for
insufficient margin rather than decided arbitrarily; with `allow_fallback`
enabled the result is produced but carries `assumed = True` and the
`ORIENTATION_ASSUMED` warning.

---

## 9. Quality checks

```text
pytest         583 passed in 12.5s
ruff check .   All checks passed!
mypy           Success: no issues found in 46 source files   (strict mode)
```

Baseline before Phase 1: 128 passed, ruff clean, mypy clean on 33 files. **There
were no pre-existing failures.**

### New tool exceptions

**None.** No ignore, no `noqa`, no mypy override was added in this phase. The
Phase 0 exception list in `docs/DEVELOPMENT_GUIDE.md` is unchanged.

One annotation is deliberately loose and carries its reason in the docstring:
`marker_detection.measure_candidate` takes `NDArray[Any]`, because OpenCV's own
stubs declare a contour as either integer or floating point and narrowing it
would only add a cast.

### Environment note

The repository had no virtual environment when Phase 1 began. One was created at
`.venv` (already git-ignored) with `pip install -e ".[dev]"`, which resolved to
OpenCV 5.0.0 and NumPy 2.5.3 — both newer than the `>=4.9` and `>=1.26` floors
in `pyproject.toml`, and both working without changes.

---

## 10. Manual smoke test

Run end to end with the developer tools, writing into a scratch directory:

```bash
python -m omr_scanner.tools.make_test_sheet scan.png \
    --rotate 6 --scale 0.9 --perspective 0.02 --blur 3 --noise 4 --seed 7

python -m omr_scanner.tools.align_image scan.png \
    --template resources/templates/example_answer_sheet.omrt \
    --output aligned.png --debug debug/
```

What was inspected:

1. **Ground truth against detection.** The generator printed the four true
   marker centres; the engine reported them to within 0.2 px — e.g. true
   `(283.69, 98.43)` against detected `(283.7, 98.5)`.
2. **The detection overlay** (`debug/detection.png`). The four green boxes sit
   on the four printed squares with the correct TL/TR/BR/BL labels, the
   orientation dash is boxed in orange, and the magenta quadrilateral joins the
   four marker centres. Every hollow answer frame, every bubble ring, every text
   bar and every control dot is drawn grey — rejected.
3. **The rectified page** (`debug/normalized.png`). Upright, 1240 x 1754, and
   each green cross marking an expected canonical marker centre sits in the
   middle of its printed square.
4. **The reported metrics.** `quarter turns 0`, orientation confidence 1.00 with
   margin 1.00, aspect ratio 0.683 against an expected 0.684 (−0.2 per cent),
   no warnings, 16.6 ms.
5. **A refusal.** A sheet rendered without an orientation mark exits 1 and
   prints `Alignment failed [ORIENTATION_NOT_FOUND]` to stderr.

The same flow is asserted automatically in
`tests/integration/test_imaging_tools.py`, so the smoke test cannot silently
stop working.

---

## 11. Known limitations

Measured, not estimated. The full table is in `docs/IMAGE_PROCESSING.md` § 20.

1. **No real-world validation.** Every number above comes from synthetic sheets.
   Pencil texture, scanner shadows, print registration error and paper texture
   are not represented anywhere in the suite. **This is the largest open risk in
   Phase 1.**
2. **Arbitrary rotation is corrected to ±15°**, plus exact quarter turns. Beyond
   that the page's bounding canvas grows enough that the markers leave the
   corner search regions; at 45° they sit at the canvas edge midpoints, where no
   corner region can reach them. Setting `corner_search_width` and
   `corner_search_height` to 1.0 aligns 20°–75° correctly, at the cost of no
   positional filtering. A scanner does not feed pages at 45°, so this was not
   pursued further.
3. **Otsu fails on an illumination gradient beyond 0.45** (one corner at 55 per
   cent of the other's brightness). `adaptive_mean` survives to 0.95 but
   produces many more spurious contours, so it is not the default.
4. **Cropping beyond about 5 per cent of the page width** into the margin fails,
   which is the intended behaviour, not a defect.
5. **Only square markers are exercised.** `MarkerShape.FILLED_CIRCLE` would pass
   the default filters (a disc's rectangularity is π/4 ≈ 0.785, above the 0.75
   floor) but nothing tests it, and `shape` is not consulted by the detector.
6. **`working_max_dimension_px` trades precision for speed.** At its default of
   3600 a 300 dpi A4 scan runs essentially at full resolution; a 600 dpi scan
   would be downscaled and lose precision proportionally.
7. **One sheet per call, single-threaded.** Batch parallelism is Phase 5.
8. **No GUI integration.** The Scan and Template pages are still placeholders.

---

## 12. Deferred technical debt

Only genuine items; each was a decision, not an oversight.

| Deferred | Why | When |
|---|---|---|
| Anonymised real-scan fixtures | None exist. The directory, the policy and the procedure for adding one are in place; inventing a "realistic" scan would have produced confidence without evidence. | As soon as a sheet can be obtained and anonymised — ideally before Phase 4 calibration |
| Sub-pixel marker refinement at full resolution | Would remove the `working_max_dimension_px` precision trade-off entirely, but the current error is already two orders of magnitude below a bubble pitch. | Only if a real scan shows it is needed |
| Marker `shape` dispatch (circle, rectangle detectors) | The format declares three shapes; Phase 1's brief assumed squares and no sheet design needs the others yet. Circles would pass the current filters by accident, not by design. | When a sheet design uses one |
| A fallback for a missing corner | Deliberately excluded: reliable refusal is worth more than heroic recovery. | A later phase, if real batches show it is needed, and only with a loud warning |
| GUI preview of an alignment | Out of scope; would have consumed Phase 1 time for no test coverage. | Phase 4 (template calibration) is its natural home |
| `scans_aligned` persistence | Writing a rectified sheet into a project and recording it is scan-service work, and there is no scan service yet. | Phase 5 |

---

## 13. Phase 2 entry criteria

**Phase 2 can begin safely.** The contract it depends on is in place and
measured.

**Already in place**

- `align_sheet(image, config)` produces the canonical page at exactly the
  template's declared size, callable headlessly with no Qt anywhere in
  `imaging` (enforced by `tests/unit/test_architecture.py`).
- `alignment_config_from_template` means the designer can hand a work-in-progress
  template straight to the engine to see whether its marker geometry works.
- `result.inverse_transform_matrix` maps canonical coordinates back onto the
  original scan — what a designer canvas needs to show a zone over a real sheet.
- `imaging.diagnostics` renders the detection overlay and the rectified preview
  as plain arrays, which the GUI can turn into a `QImage` without importing
  OpenCV.
- `imaging.synthetic.render_sheet` gives the designer a reference page to draw
  on without a scanner.

**Constraints Phase 2 must respect**

1. The `.omrt` format is `TEMPLATE_FORMAT_VERSION = 1`. A designer change that
   needs a breaking format change must bump it and say so.
2. Marker centres in a template are canonical **centres**, not page corners.
   A designer that lets a user drag a marker must store the centre.
3. The canonical page size the designer writes is what alignment will produce;
   changing it changes every normalised coordinate's pixel meaning, not the
   coordinates themselves.
4. No OpenCV in the GUI layer. If the designer needs pixels, it calls a service.

**Suggested first steps**

1. Load a reference image into the designer canvas — either a real scan run
   through `align_sheet`, or a synthetic sheet from `imaging.synthetic`.
2. Place and drag the four registration markers and the orientation mark, then
   round-trip the document through `save_template`/`load_template`.
3. Use `align_image --debug` on a scan of the sheet being designed to confirm the
   marker geometry is detectable *before* zones are drawn on top of it.

**Suggested next prompt**

> Begin Phase 2 — Template Data Model & Template Designer Core, following
> `development/ROADMAP.md` and the constraints in
> `development/PHASE_01_HANDOFF.md` section 13. Build the designer canvas on the
> existing `.omrt` model and the Phase 1 alignment engine. Do not modify the
> alignment engine, and do not implement bubble recognition.

**Definition of done for Phase 2**

A complete template for a real sheet can be produced entirely in the GUI and
reloaded unchanged; the `.omrt` specification and `docs/TEMPLATE_FORMAT.md`
still agree; `pytest`, `ruff check .` and `mypy` pass; `CURRENT_STATE.md` is
updated and `PHASE_02_HANDOFF.md` is written.

---

## 14. The one thing to do before trusting this

Obtain a printed sheet, scan it, anonymise it, and run
`python -m omr_scanner.tools.align_image` on it with `--debug`. Every accuracy
number in this document is synthetic. The engine may well work on real paper —
the degradation margins are wide — but nobody has checked, and an examination is
not the place to find out.
