# Phase 3 handoff — Batch Scanning, Recognition, Renaming and Export

**Implemented:** 2026-09-16
**Updated:** 2026-09-17 — configurable multicore batch recognition added
(§§2, 4, 5, 6, 7, 9); the phase stays in testing/stabilisation.
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

A later increment (2026-09-17) added **configurable multicore batch
recognition**: independent sheets are read concurrently, one whole page per CPU
worker process, under a user-chosen processing mode - without changing a single
recognised value, output file name or CSV row.

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
| `batch_processor.py` | `process_scan`, `finalise_scan`, `process_batch`, `BatchOptions`, `BatchProgress`, `BatchReport`, `ProcessedScan`. Per-file error isolation, progress callbacks, cooperative cancellation, and the `workers` parameter that selects the sequential or the multicore strategy. |
| `parallel_batch.py` | `recognise_in_parallel`, `worker_initialise`, `worker_recognise`, `WorkerOutcome`. The `ProcessPoolExecutor` (`spawn`) that reads one whole page per worker process and yields results in completion order. |
| `filename_manager.py` | `FilenameAllocator`, `duplicate_suffix`, `sanitise_stem`. Decides names; never touches a file. |
| `scan_import.py` | `collect_scan_files`, `is_supported_scan`, `SUPPORTED_SCAN_SUFFIXES`. Folder walking, format filtering, natural sort. |
| `scan_export.py` | `build_rows`, `export_scan_results`, `render_scan_results`, `question_numbers`. Deterministic UTF-8(-BOM) CSV. |

### `src/omr_scanner/gui/scan/` (new package)

| Module | Contents |
|---|---|
| `page.py` | `ScanPage`, `ScanPageState`, `ScanEntry` - the workflow stage. |
| `preview.py` | `ScanPreviewView`, `OverlayItem` - zoom/pan/fit graphics view with a non-destructive recognition overlay. |
| `worker.py` | `BatchWorker`, `PreviewWorker` - `QThread`s that touch no widget. `BatchWorker` also carries the run's worker count. |

### `src/omr_scanner/config/processing.py` and `gui/settings_dialog.py` (new)

| Module | Contents |
|---|---|
| `config/processing.py` | `ProcessingMode` (Automatic / Single core / Custom), `ProcessingSettings` with `configured_worker_count` and `resolve_worker_count`, `detected_cpu_count`, `AUTOMATIC_WORKER_LIMIT`. Nested into `AppConfig.processing`, so the choice persists in `omrflow.config.json`. |
| `gui/settings_dialog.py` | `SettingsDialog` - `File > Settings...`, the Processing section, the detected-CPU and active-worker read-outs. |

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

# The same batch, read on several cores. Nothing else changes: the results, the
# output names and the CSV rows are identical, only the elapsed time differs.
from omr_scanner.config import ProcessingSettings, load_app_config

workers = load_app_config().processing.resolve_worker_count(len(paths))
report = process_batch(paths, template, workers=workers)
report.worker_count      # what it actually used (never more than len(paths))
report.elapsed_seconds   # for the diagnostic log and the benchmark script
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

### Where the multicore path forks

```text
services.batch_processor.process_batch(..., workers=N)
  workers == 1  -> _run_sequential      unchanged: recognise, name, copy, repeat
  workers  > 1  -> _run_parallel
                     -> parallel_batch.recognise_in_parallel
                          ProcessPoolExecutor(spawn), one whole page per worker
                          yields (index, ScanResult) in COMPLETION order
                     -> buffer, restore BATCH order
                     -> filename_manager  (one allocator, parent process)
                     -> copy, record, on_result
```

Progress is reported on completion (so the bar advances steadily); naming,
copying and recording happen in batch order (so the output is deterministic).
Those are the only two orders in the system, and keeping them separate is what
makes "8 cores" and "1 core" produce the same files. Workers never decide a file
name: two sheets legitimately recognising to the same roll number would
otherwise both choose `2103123.png` and one would overwrite the other.

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
| `processing.mode` | `automatic` | `AppConfig.processing` **in `omrflow.config.json`** |
| `processing.worker_count` (Custom) | 4 | same |
| `AUTOMATIC_WORKER_LIMIT` | 8 | `config/processing.py` — measured, see §7 |
| `RESERVED_CPU_COUNT` | 1 | same |
| `START_METHOD` | `spawn` | `services/parallel_batch.py` |

Everything describing the *sheet* comes from the template. `BubbleMetricsConfig`
is measurement *tuning*, the same qualified exception `imaging.config` already
is for alignment: every value named, documented and validated.

The worker count is the one Phase 3 setting that belongs to the *machine*, not
the examination, which is why it lives in the per-user application document
rather than the project or the template: the same batch must read identically on
a laptop and a workstation, and only the time taken may differ.

---

## 6. Tests

979 new tests; 1708 in the repository, 1 skipped, all others passing.

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
| `unit/test_processing_settings.py` | 29 | Worker selection in every mode on 1/2/4/8/16/32/128 logical CPUs; batch smaller than the worker count; invalid counts refused; the setting round-tripping through the configuration file, including a file written before the feature existed and one written on a larger machine. |
| `integration/test_parallel_batch.py` | 32 | **Real worker processes.** Identical results and byte-identical CSVs at 1/2/4 workers; batch order preserved against completion order; eight sheets sharing one roll number named `_a`..`_g` exactly as single-core does; existing output files never overwritten; corrupt/missing/failing scans isolated; a dead worker process becoming one failed scan; cancellation; no orphan processes after a run, a cancellation or an abandoned generator. |
| `gui/test_processing_settings_gui.py` | 35 | Settings opens and shows Processing; each mode selectable; the worker selector enabled only for Custom and bounded by the detected CPU count; the choice persisted and still present after "restarting"; a batch running in the selected mode with the window responsive, the progress indicator advancing and completion reported. |

All GUI tests run under `QT_QPA_PLATFORM=offscreen`, wait on
`ScanPage.batch_finished` rather than sleeping, and never `exec()` a modal. The
multiprocessing tests pass an explicit `cpu_count` wherever the machine would
otherwise decide the answer, and assert no durations — throughput is measured by
`scripts/benchmark_batch.py`, not asserted.

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

### Multicore throughput and memory

48 copies of `examples/ECE-0000.png` (2480x3508), development machine: 16
logical CPUs / 8 physical cores, Windows 11, Python 3.12.7. Memory is the peak
resident set of *all* Python processes together, sampled at 150 ms.

| Workers | Seconds | Scans/s | Speed-up | Peak RAM |
|---:|---:|---:|---:|---:|
| 1 | 13.95 | 3.44 | 1.00x | 127 MB |
| 2 | 8.81 | 5.45 | 1.58x | — |
| 4 | 5.54 | 8.67 | 2.52x | — |
| 8 | 4.13 | 11.63 | **3.38x** | 855 MB |
| 12 | 4.44 | 10.80 | 3.14x | — |
| 16 | 4.79 | 10.02 | 2.91x | 1,607 MB |

Two conclusions, both acted on:

1. **`AUTOMATIC_WORKER_LIMIT = 8`.** Throughput peaks at the physical core count
   and *falls* beyond it, while memory keeps rising by roughly 95 MB per worker.
   Past eight, a batch costs more memory and finishes no sooner, so automatic
   mode stops there; Custom mode still allows more for anyone who has measured
   their own machine.
2. **Small batches do not benefit.** At 16 scans the same comparison gives only
   1.77x at eight workers, because each worker is a fresh interpreter importing
   NumPy and OpenCV. The gain is real from a few dozen sheets upward, which is
   the size a real examination batch has.

Reproduce with
`python scripts/benchmark_batch.py --scans 48 --workers 1,2,4,8,12,16`.

---

## 8. Quality checks

```text
pytest         1708 passed, 1 skipped in 114.6s
ruff check .   All checks passed!
mypy           Success: no issues found in 79 source files   (strict mode)
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
- `scripts/run_gui_smoke_tests.py` gained five Scan checks, then four more for
  the multicore work (the Settings dialog's Processing section, the Scan page's
  worker plan, multicore-equals-single-core on the real sample, and an
  orphan-process check) — **20/20 pass**, including recognising the real sample
  end to end through the GUI.
- `scripts/capture_gui_states.py` gained five Scan scenarios and two more
  (`settings`, `scan-multicore`), producing `scan_empty`,
  `scan_template_loaded`, `scan_processed`, `scan_overlay_zoom`,
  `scan_overlay_all_bubbles`, `scan_duplicate_rolls`, `scan_exported`,
  `settings_processing_{automatic,single_core,custom}`,
  `scan_multicore_four` and `scan_multicore_single`.
- `references/omrflow_gui_test_scenarios.md` gained the Scan and Settings
  `objectName` tables and Scenarios 11-14.

Three defects were found by this process and fixed, not worked around:

1. **A second `PreviewWorker` for the same scan.** Finishing a batch re-selects
   the current row, so a user who also clicks that row produced two workers; the
   late one re-applied the preview and reset a zoom the user had just set.
   `ScanPage._show_preview_for` now declines to start a worker for a file
   already being rendered. Regression test:
   `test_reselecting_the_same_scan_does_not_start_a_second_preview_worker`.
2. **Capture scripts exiting `0xC0000409`.** A `QThread` still running at
   interpreter shutdown makes Qt abort *after* the work succeeded.
   `ScanHarness.shutdown()` closes the page first.
3. **The Settings dialog silently reverting other settings.** It first returned
   a whole rebuilt `AppConfig` from the snapshot taken when it opened, so
   accepting it discarded anything changed meanwhile - a project opened while
   the dialog was up vanished from the recent list. It now returns only its own
   `ProcessingSettings` and the window merges that into the current
   configuration. Found by
   `test_changing_processing_does_not_disturb_the_recent_project_list`, which
   was written as the invariant before the bug was understood.

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
6. Cancellation stops after the sheets in progress, not instantly — with N
   workers, up to N sheets finish after Cancel is pressed.
7. **Parallelism is across sheets only.** One sheet takes as long as it always
   did; a batch of one gains nothing, and a handful of sheets can be slower on
   several workers than on one because each worker is a fresh interpreter.
8. **The multicore path has run on one machine.** 16 threads, Windows 11. Other
   core counts, memory-constrained machines and `spawn` on Linux/macOS are
   untested, as is a frozen build (`multiprocessing.freeze_support()` is called,
   but nothing has been packaged yet to prove it).
9. Phase 1's alignment limits still bound everything: ±15° arbitrary rotation
   plus quarter turns, illumination gradients to ~0.45 with the default Otsu
   threshold, and ~5% margin cropping.
10. `MULTIPLE_CORNER_CANDIDATES` is reported on both the real sample and the
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
