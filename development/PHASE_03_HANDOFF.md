# Phase 3 handoff — Batch Scanning, Recognition, Renaming and Export

**Completed:** 2026-09-16
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2, OpenCV
5.0.0, NumPy 2.5.3

This document is the entry point for whoever continues the work. It records what
the Scan workflow does, how it was verified, and what it does not do. Read
alongside `docs/scan_workflow.md` (the user-facing conventions),
`docs/IMAGE_PROCESSING.md` §§21-23 (the algorithms) and `docs/ARCHITECTURE.md`
(how the modules fit together).

---

## 1. Phase objective

Turn the Phase 1 alignment engine and the Phase 2 template designer into a
workflow that reads real answer sheets: import one or many scans, rectify them,
map the template onto them, recognise roll numbers, set codes and answers with
explicit uncertainty, review the result visually, optionally file the images
under their roll numbers without ever overwriting one, and export CSV.

---

## 2. Implementation summary

### `src/omr_scanner/imaging/metrics.py` (new)

Per-bubble measurement. `BubbleMetricsConfig`, `BubbleMeasurement`,
`measure_bubble`, `measure_bubbles`, `estimate_ink_level`, `ink_threshold`.

Samples an ellipse at 0.62 of the bubble's half-axes (inside the printed ring),
estimates local paper from the 80th percentile of an annulus at 1.25-1.95
half-axes, and thresholds halfway between that and the page's 1st-percentile ink
level, clamped to 35-130 grey levels. Every constant is a ratio or a grey-level
difference, never a pixel count, so one configuration serves 150 and 300 dpi.

### `src/omr_scanner/recognition/` (new: `models.py`, `decide.py`, `fields.py`)

| Module | Contents |
|---|---|
| `models.py` | The vocabulary: `MarkStatus`, `FieldStatus`, `Selection`, `MarkReading`, and the field/answer result types. |
| `decide.py` | `decide_group` - one group of measured bubbles to one `Selection`, using the template's `RecognitionSettings`. |
| `fields.py` | `recognise_grid_zone`, `recognise_template` - groups into fields, fields into a sheet's values. |

Three modules rather than one because that is what keeps a double mark
expressible at the bottom *and* visible at the top: blank, multiple, uncertain
and unreadable are distinct values all the way out to the CSV.

### `src/omr_scanner/services/` (new modules)

| Module | Contents |
|---|---|
| `recognition_service.py` | `recognise_scan` - one file to a `ScanResult`, including registration status, field values, overlay geometry and an optional bounded-size preview. |
| `batch_processor.py` | `process_scan`, `process_batch`, `BatchOptions`, `BatchProgress`, `BatchReport`, `ProcessedScan`. Per-file error isolation, progress callbacks, cooperative cancellation. |
| `filename_manager.py` | `FilenameAllocator`, `duplicate_suffix`, `sanitise_stem`. Decides names; never touches a file. |
| `scan_import.py` | `collect_scan_files`, `is_supported_scan`, `SUPPORTED_SCAN_SUFFIXES`. Folder walking, format filtering, natural sort. |
| `scan_export.py` | `build_rows`, `export_scan_results`, `render_scan_results`, `question_numbers`. Deterministic UTF-8(-BOM) CSV. |

### `src/omr_scanner/gui/scan/` (new package)

| Module | Contents |
|---|---|
| `page.py` | `ScanPage`, `ScanPageState`, `ScanEntry` - the workflow stage. |
| `preview.py` | `ScanPreviewView`, `OverlayItem` - zoom/pan/fit graphics view with a non-destructive recognition overlay. |
| `worker.py` | `BatchWorker`, `PreviewWorker` - `QThread`s that touch no widget. |

### `src/omr_scanner/imaging/synthetic.py` (extended)

Gained `AnswerBubbleSpec` and the ability to render *marked* bubbles with a
chosen fill fraction and a printed symbol, which is Phase 3's ground-truth
generator.

### Not built, on purpose

Manual correction of recognised values, database persistence of results, PDF
input, rename-in-place, and anything from Phases 4+ (answer keys, scoring,
attendance, reporting).

---

## 3. Public API

```python
from omr_scanner.services import (
    BatchOptions, FilenameAllocator, collect_scan_files,
    export_scan_results, load_template, process_batch, recognise_scan,
)

template = load_template(Path("sheet.omrt"))

# One sheet.
result = recognise_scan(Path("scan.png"), template, with_preview=False)
result.identifier_value      # "2103123", or "21?3123" when a column did not resolve
result.set_code_value        # "10" - a string, always
result.answers[0].display_value   # "B", "", "B-D", "B?" or "?"
result.registration          # REGISTERED | REGISTERED_WITH_WARNING | FAILED

# A batch, renamed into an output folder.
report = process_batch(
    collect_scan_files([Path("scans/")]),
    template,
    options=BatchOptions(output_dir=Path("out"), rename_with_identifier=True),
    allocator=FilenameAllocator(Path("out")),
)
export_scan_results(report.processed, template, Path("results.csv"))
```

The GUI entry point is navigating to the "Scan" stage in `MainWindow`.

---

## 4. Pipeline

```text
Input scan
  -> services.scan_import.collect_scan_files      formats, folders, natural sort
  -> services.template_service.load_template
  -> imaging.marker_detection                     four registration squares
  -> imaging.orientation(_marker)                 which way up (0/90/180/270)
  -> imaging.geometry + imaging.alignment         one homography; rotation,
                                                  translation, scale, skew,
                                                  perspective
  -> canonical page image
  -> services.recognition_service                 template normalised coords
                                                  -> canonical pixels
  -> imaging.metrics                              per-bubble fill ratio
  -> recognition.decide                           one group -> a Selection
  -> recognition.fields                           fields, answers, statuses
  -> services.filename_manager                    a non-colliding output name
  -> services.batch_processor                     copy, isolate errors, report
  -> gui.scan.preview                             review with overlay
  -> services.scan_export                         CSV
```

Four decisions worth carrying forward:

- **The ink threshold is local and relative.** Halfway between the paper level
  in an annulus around *that* bubble and the page's own ink level. An absolute
  threshold reads a printed option glyph as a mark and a uniformly faint pencil
  sheet as blank; this reads neither.
- **Five explicit states, never a value plus a boolean.** Flattening "two marks"
  into one answer is how a double mark silently becomes a wrong result.
- **Naming is separated from copying.** `filename_manager` decides,
  `batch_processor` acts. The duplicate rule is therefore unit-testable without
  a disk, and the GUI can preview an output name before committing.
- **An unreliable identifier gets a review name.** `UNRESOLVED_001`, not a
  plausible-looking roll number, because a fabricated name is indistinguishable
  from a correct one.

---

## 5. Configuration

| Value | Default | Where |
|---|---|---|
| `fill_ratio_threshold`, `blank_ratio_threshold`, `ambiguity_margin` | per template | `RecognitionSettings` **in the `.omrt`** |
| `BubbleMetricsConfig.sample_radius_ratio` | 0.62 | `imaging/metrics.py` |
| `background_inner_ratio` / `background_outer_ratio` | 1.25 / 1.95 | same |
| `paper_percentile` / `ink_percentile` | 80.0 / 1.0 | same |
| `ink_fraction` | 0.5 | same |
| `min_ink_margin` / `max_ink_margin` | 35.0 / 130.0 | same |
| `min_sample_pixels` | 9 | same |
| `DEFAULT_PREVIEW_MAX_DIMENSION` | see module | `services/recognition_service.py` |
| `PREVIEW_CACHE_SIZE` | 6 | `gui/scan/page.py` |

Everything describing the *sheet* comes from the template. `BubbleMetricsConfig`
is measurement *tuning*, the same qualified exception `imaging.config` already
is for alignment: every value named, documented and validated.

---

## 6. Tests

883 new tests; 1612 in the repository, 1 skipped, all others passing.

| File | Tests | Covers |
|---|---:|---|
| `unit/test_bubble_metrics.py` | 38 | Sampling geometry, local paper estimation, ink thresholds, coverage-to-fill-ratio, edge bubbles, unusable samples. |
| `unit/test_recognition_decide.py` | 27 | Candidate ordering, blank, single, multiple, ambiguous-margin and faint-mark decisions. |
| `unit/test_recognition_fields.py` | 41 | Grid fields, blank/double/unreadable digit columns, multi-character set codes, alphanumeric symbol order, whole-template recognition. |
| `unit/test_filename_manager.py` | 41 | Unique roll, `_a`/`_b`/`_c`, beyond 26 (`_aa`), existing-file collisions, extension preservation, unsafe characters, invalid-roll fallback, case-insensitive collision. |
| `unit/test_scan_export.py` | 31 | Column order, question ordering from the template, Unicode, escaping, duplicate output names, determinism, BOM. |
| `unit/test_scan_import.py` | 33 | Supported suffixes, folder walking, unrelated files ignored, natural sort, de-duplication. |
| `integration/test_recognition_pipeline.py` | 50 | End-to-end on synthetic sheets: every mark convention, under rotation/scale/translation/perspective, template-coordinate mapping after transformation. |
| `integration/test_batch_processor.py` | 26 | Batch runs, per-file error isolation, renaming, duplicates, unresolved identifiers, shared allocators, cancellation. |
| `integration/test_sample_sheet_recognition.py` | 19 | **The real scan** and geometrically distorted copies of it. |
| `gui/test_scan_page.py` | 50 | Workflows A-J: launch, template load, import, process, preview interaction, batch, renaming, duplicate rolls, existing-file collision, CSV export. |

All GUI tests run under `QT_QPA_PLATFORM=offscreen`, wait on
`ScanPage.batch_finished` rather than sleeping, and never `exec()` a modal.

---

## 7. Quantitative results

On `examples/ECE-0000.png` — an actual scan, with handwritten marks, printed
option glyphs inside every bubble, non-white paper and scanner ringing around
the registration squares:

| Measure | Result |
|---|---|
| Roll number | `00000000` — correct, leading zeros intact |
| Set code | `10` — correct, two positions, not reduced |
| Answers | 100/100 correct against the paper |
| Fields flagged for review | 0 |
| Bubbles measured | 500 (8x10 roll + 2x10 set + 100x4 answers) |
| Bubbles read as marked | 110 (8 + 2 + 100) |
| Separation, faintest mark vs darkest unmarked bubble | **> 0.5 fill ratio** |

The ground truth is the deliberate demonstration pattern printed on the sheet -
four of each option, then `abcd` repeating - which is what makes it usable:
a decode that drifted by one column would break the pattern visibly rather than
producing another plausible-looking string.

The same sheet still reads identically after: ±3° rotation; exact 90°, 180° and
270° turns; 0.7x and 1.3x rescaling; an 80x-55 px translation; perspective
distortion; JPEG compression; and all of those combined in one image.

---

## 8. Quality checks

```text
pytest         1612 passed, 1 skipped in 45.6s
ruff check .   All checks passed!
mypy           Success: no issues found in 76 source files   (strict mode)
```

Baseline before Phase 3: 729 passed, ruff clean, mypy clean on 58 files.
**No pre-existing failures, and Phase 3 introduced none.**

Phase 3 added **no** new mypy overrides, Ruff ignores, `# type: ignore`s or
`pyproject.toml` tool configuration changes.

---

## 9. GUI validation

Beyond `tests/gui/test_scan_page.py`, the repository-local `qtguitesting` skill
was extended to cover the Scan page and then actually run:

- `scripts/_harness.py` gained `ScanHarness` and `build_scan_page`, which drive
  the real page with the real sample and its real template, wait on
  `batch_finished`, and shut worker threads down cleanly.
- `scripts/run_gui_smoke_tests.py` gained five Scan checks — **16/16 pass**,
  including recognising the real sample end to end through the GUI.
- `scripts/capture_gui_states.py` gained five Scan scenarios, producing
  `scan_empty`, `scan_template_loaded`, `scan_processed`, `scan_overlay_zoom`,
  `scan_overlay_all_bubbles`, `scan_duplicate_rolls` and `scan_exported`.
- `references/omrflow_gui_test_scenarios.md` gained the Scan `objectName` table
  and Scenarios 11-13.

Two defects were found by this process and fixed, not worked around:

1. **A second `PreviewWorker` for the same scan.** Finishing a batch re-selects
   the current row, so a user who also clicks that row produced two workers; the
   late one re-applied the preview and reset a zoom the user had just set.
   `ScanPage._show_preview_for` now declines to start a worker for a file
   already being rendered. Regression test:
   `test_reselecting_the_same_scan_does_not_start_a_second_preview_worker`.
2. **Capture scripts exiting `0xC0000409`.** A `QThread` still running at
   interpreter shutdown makes Qt abort *after* the work succeeded.
   `ScanHarness.shutdown()` closes the page first.

---

## 10. Known limitations

1. **Validation rests on one real sheet.** One scan plus geometric variants of
   it, and synthetic pages. Light pencil, erasures, crossed-out answers,
   overflowing marks, other candidates' handwriting and other scanners have
   never been seen. **This is the largest open risk in Phase 3.**
2. **No manual correction** of recognised values in the GUI. The data model
   already separates the machine's reading from a correction, so this is
   additive.
3. **Results are not persisted** to the project database; a batch's output is
   the CSV and the optional renamed copies (Phase 5).
4. **No PDF input** — deliberate; the brief ruled out a new dependency for it.
5. **Renaming only copies**; there is no move/rename-in-place mode.
6. Cancellation stops after the sheet in progress, not instantly.
7. No parallelism across cores.
8. Phase 1's alignment limits still bound everything: ±15° arbitrary rotation
   plus quarter turns, illumination gradients to ~0.45 with the default Otsu
   threshold, and ~5% margin cropping.
9. `MULTIPLE_CORNER_CANDIDATES` is reported on both the real sample and the
   synthetic page. It is honest — more than one marker-like shape is genuinely
   present near a corner — but it means the warning is not, in practice, a
   useful discriminator between a good and a suspect sheet.

---

## 11. Phase 4 entry criteria

**Already in place**

- A `ProcessedScan` per sheet carrying the result, the chosen output name, what
  was written and why, ready to be persisted.
- `RecognitionResult`-shaped values with explicit statuses, so a later phase can
  ask "which sheets need a human?" without re-deriving it.
- A deterministic CSV whose base columns are stable and documented.
- Ground-truth generation (`imaging.synthetic` with marked bubbles) and a real
  validated sample, so a later phase can test against known answers.

**Constraints to respect**

1. `omr_scanner.recognition` must not import Qt; `gui` must not import `cv2`,
   `numpy`, `imaging` or `recognition`. Both are enforced by
   `tests/unit/test_architecture.py`.
2. Recognition thresholds live in `RecognitionSettings`, in the template.
3. A missing, multiple or uncertain reading is represented explicitly and never
   guessed at — and an unreliable identifier never becomes a file name.
4. No scan image may ever be overwritten.

**The one thing to do before trusting this**

Process a stack of genuinely filled sheets and compare the CSV against the
papers. Count two numbers separately: how many sheets needed review, and how
many were confidently *wrong*. The second decides whether this is usable for
examination processing; nothing measured so far bounds it.
