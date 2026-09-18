# Architecture

This document describes how OMRFlow is structured and, more importantly, which
dependencies are allowed between its parts. The rules here are enforced by
`tests/unit/test_architecture.py`; if you change a rule, change that test too.

## Why the structure looks like this

OMR processing is the kind of work where "it ran without an error" is not the
same as "the result is correct". The architecture is therefore optimised for:

- **testability** - every algorithm must be runnable without a window;
- **reproducibility** - the same scan and template must produce the same values,
  whether invoked from the GUI, a test or a future batch tool;
- **auditability** - a human correction must never silently overwrite what the
  machine decided;
- **agent maintainability** - a developer or coding agent joining at phase 7
  must be able to tell where a change belongs from the module docstrings alone.

## Layers

```text
┌───────────────────────────────┐  ┌───────────────────────────────┐
│  gui    PySide6 windows,      │  │  evaluation   QA: ground truth│
│         pages, dialogs        │  │  + benchmarks over results    │
└───────────────┬───────────────┘  └───────────────┬───────────────┘
                │ calls                            │ drives
┌───────────────▼──────────────────────────────────▼──────────────┐
│  services     application workflows                             │
└───┬───────────┬───────────┬───────────┬──────┘
    │           │           │           │
┌───▼────┐ ┌────▼─────┐ ┌───▼──────┐ ┌──▼────────┐
│ domain │ │ database │ │ imaging  │ │ reporting │
│        │ │          │ │ +        │ │           │
│        │ │          │ │recognition│ │          │
└───┬────┘ └────┬─────┘ └───┬──────┘ └──┬────────┘
    │           │           │           │
    └───────────┴─────┬─────┴───────────┘
                      │
              ┌───────▼────────┐
              │ utils, config,  │
              │ errors          │
              └─────────────────┘
```

Dependencies point **downward only**. Nothing below a layer may import anything
above it.

| Package | Owns | Must never contain |
|---|---|---|
| `omr_scanner.domain` | Data shapes and their validity rules; pure computations (e.g. bubble centre from a grid). | I/O, SQL, Qt, OpenCV, workflow logic. |
| `omr_scanner.imaging` | Pixel algorithms: preprocessing, marker detection, orientation, perspective rectification *(Phase 1)*, bubble metrics *(Phase 3)*. | Qt, database, project layout knowledge, value interpretation, file I/O, `.omrt` knowledge. |
| `omr_scanner.recognition` | Turning measurements into logical values with confidence, missing/multiple-mark handling *(Phase 3)*. Conflict resolution is Phase 6. | Pixel access, OpenCV, persistence, Qt. |
| `omr_scanner.database` | Schema, migrations, engine and session lifetime. | Workflow logic, Qt, OpenCV. |
| `omr_scanner.services` | Multi-step operations: create/open project, process a batch, calculate results, judge a calibration run (`calibration_service`, Phase 4). Owns all side effects. | Widgets, dialogs, Qt imports of any kind. |
| `omr_scanner.reporting` | CSV/XLSX/PDF generation. *(reserved - Phase 9)* | Result calculation, Qt. |
| `omr_scanner.gui` | Windows, pages, dialogs; presenting state and collecting intent. `gui.template_designer` (Phase 2) is the interactive `.omrt` editor; `gui.calibration` (Phase 4) verifies and tunes a template against representative scans before a batch; `gui.scan` (Phase 3) is the batch scanning workspace, including benchmark mode; `gui.devtools` (Phase 3) is the Tools > Developer / Testing front end to `evaluation`. | OpenCV, NumPy, SQLAlchemy, direct database access, any OMR algorithm. |
| `omr_scanner.evaluation` | Judging the engine: the ground-truth schema, the named test cases and dataset planner, the renderer, the benchmark and its error categories, and the benchmark session *(Phase 3)*. Sits *above* services, beside the GUI. | Qt; any recognition of its own - a benchmark that re-implements the engine measures itself. |
| `omr_scanner.tools` | Developer command line utilities that drive one stage against one file. Beside the GUI, not below it. | Qt, and any algorithm of its own - a tool parses arguments, calls a service, and prints. |
| `omr_scanner.config` | Per-user application settings and platform directory resolution. | Project or template settings. |
| `omr_scanner.utils` | Dependency-light helpers (atomic JSON, logging setup). | Domain vocabulary, any other OMRFlow layer. |
| `omr_scanner.errors` | The exception hierarchy. | Logging, presentation. |

### The GUI/service rule

> Domain logic must not live inside GUI event handlers.

Concretely:

```text
GUI action  ->  service  ->  domain / imaging / recognition / database
```

not

```text
GUI button  ->  OpenCV processing in MainWindow  ->  Excel file
```

A useful test when deciding where code belongs: *would this still be meaningful
in a command line version of OMRFlow?* If yes, it is a service or lower.

In the main window this shows up as a deliberate split: `_prompt_*` methods own
the modal dialogs and contain no logic, while `create_project_at`,
`open_project_at` and `close_project` contain the behaviour and are what the GUI
tests drive.

## Error handling

All deliberate failures derive from `OMRScannerError`:

```text
OMRScannerError
├── ConfigurationError
├── ProjectError
│   ├── ProjectValidationError
│   └── ProjectExistsError
├── DatabaseError
│   └── SchemaVersionError
├── TemplateError
├── ImagingError                      code = "IMAGING_ERROR"
│   ├── ImageValidationError          code = "INVALID_IMAGE"
│   ├── MarkerDetectionError          code = "MARKER_DETECTION_FAILED"
│   │   ├── InsufficientMarkersError  code = "INSUFFICIENT_MARKERS"
│   │   └── AmbiguousMarkerError      code = "AMBIGUOUS_MARKERS"
│   ├── OrientationDetectionError     code = "ORIENTATION_NOT_FOUND"
│   ├── InvalidPageGeometryError      code = "INVALID_PAGE_GEOMETRY"
│   └── AlignmentTransformError       code = "ALIGNMENT_TRANSFORM_FAILED"
├── RecognitionError    (reserved - Phase 3)
└── ReportingError      (reserved - Phase 9)
```

Each carries two messages: `str(exc)` is technical and goes to the log;
`exc.user_message` is plain language and is what a dialog shows. Services raise
them; `omr_scanner.gui.error_reporting.report_error` is the single place that
turns one into a dialog. The GUI never shows a traceback.

Every `ImagingError` additionally carries a stable, machine-readable `code`.
Callers - the future conflict queue, the batch pipeline, tests - branch on the
code without parsing English, and the code is what gets recorded against a sheet
that failed to align. It is deliberately not derived from the class name, so
renaming a class cannot silently change a persisted status.

## Logging and privacy

- Configured once in `omr_scanner.utils.logging_setup`, called from
  `omr_scanner.main` before anything else can log.
- The application log lives in the per-user log directory; a second handler is
  attached to `<project>/logs/project.log` while a project is open.
- Logged: startup/shutdown, project create/open/close, database migrations,
  configuration problems, uncaught exceptions.
- **Never logged**: candidate names, roll numbers, answers or answer keys. Log
  counts, file names and identifiers instead.

## Configuration layers

| Scope | Lives in | Example |
|---|---|---|
| Application, per user | `omrflow.config.json` in the platform config directory | log level, recent projects, CPU workers (`processing`) |
| Project | `<project>/project.json` | project name, id, timestamps |
| Template | the `.omrt` document | page geometry, zones, bubble grids |
| Recognition | nested inside the template (`recognition`) | fill threshold, confidence floor |

The worker count is application configuration rather than project or template
configuration because it describes the *machine*, not the examination: the same
batch read on a laptop and on a workstation must produce the same results, and
only the time taken may differ.

Recognition thresholds belong to the template, not to the application, because
they are only meaningful for the sheet design and print quality they were tuned
against. A module-level threshold constant anywhere in `recognition` or
`imaging` is a bug.

Alignment tuning is the one qualified case, and the qualification is explicit:
`omr_scanner.imaging.config` holds the engine's tolerances, score weights and
acceptance limits as named, documented, validated fields with defaults. Every
value that describes the *sheet* - canonical page size, marker centres, marker
size, the orientation mark - comes from the template, and
`services.alignment_config_from_template` is where they cross over.

## The recognition pipeline

Complete from raw scan to CSV as of Phase 3; database persistence and conflict
resolution remain Phases 5 and 6. The detail is in `docs/IMAGE_PROCESSING.md`,
and the user-facing conventions in `docs/scan_workflow.md`.

```text
raw scan
  -> imaging.preprocessing      grayscale, downscale, denoise, threshold
  -> imaging.marker_detection   candidate measurement, filtering, corner choice
  -> imaging.orientation        which scan corner is the sheet's top-left
  -> imaging.geometry           ordering, validation, homography
  -> imaging.alignment          warp; the align_sheet entry point
  -> canonical page image       (matches the template's canonical geometry)
  -> imaging.metrics            per-bubble fill measurements       (Phase 3)
  -> recognition.decide         one group's marks -> a Selection   (Phase 3)
  -> recognition.fields         values with explicit status        (Phase 3)
  -> services.recognition_service   one sheet, end to end          (Phase 3)
  -> services.filename_manager  a non-colliding output name        (Phase 3)
  -> services.batch_processor    many sheets, errors isolated      (Phase 3)
  -> services.scan_export       deterministic CSV                  (Phase 3)
  -> services.scan_service      persistence, conflicts, progress   (Phase 5)
```

### Phase 3 in the brief's terms

The sequence the Phase 3 brief asks to be documented, and the module that owns
each step:

| Step | Owner |
|---|---|
| Input scan | `services.scan_import.collect_scan_files` (formats, folders, natural sort) |
| Load template | `services.template_service.load_template` |
| Detect registration markers | `imaging.marker_detection` |
| Determine orientation | `imaging.orientation`, `imaging.orientation_marker` |
| Geometric correction | `imaging.geometry`, `imaging.alignment` |
| Transform template coordinates | `services.recognition_service` (normalised -> canonical px) |
| Extract answer regions | `domain.template` `Zone` / `BubbleGrid.bubble_center` |
| Measure bubbles | `imaging.metrics` |
| Interpret responses | `recognition.decide`, `recognition.fields` |
| Validate roll / set / questions | `recognition.fields`, `recognition.models` |
| Assign output filename | `services.filename_manager.FilenameAllocator` |
| Preview / review | `gui.scan.preview`, `gui.scan.page` |
| CSV export | `services.scan_export` |

`gui.scan.worker` is the only piece that is neither: it exists solely to run
`services.batch_processor` on a `QThread` and re-emit its callbacks as Qt
signals, so that no widget is ever touched from a worker thread and no
recognition code ever imports Qt.

### Recognition is a replaceable subsystem

Phase 3 is deliberately reachable through one door and describable in one type:

```text
RecognitionEngine.process(image_path, template) -> ScanResult
```

Everything above that line - the Scan page, the batch processor, Phase 4's
review queue, Phase 8's scoring - depends on the *call* and the *shape of the
answer*, and on nothing else. No consumer thresholds a pixel, walks a contour
or knows what a homography is.

Three properties make the substitution real rather than aspirational:

- **The result is plain data.** Strings, numbers, booleans, tuples. That is why
  the GUI can consume it while being forbidden `cv2`, `numpy`, `imaging` and
  `recognition`, and why a result can be written to JSON, stored as a fixture
  and read back by a phase that has no engine at all.
- **The result is versioned.** `engine_version` says which behaviour produced
  it; `schema_version` says which shape it was written in. A benchmark can put
  v1 and v2 side by side on one dataset; a stored result stays readable.
- **Measurement is separated from decision.** `imaging.metrics` measures a
  bubble; `recognition.decide` decides what it means; the thresholds come from
  the template. Recalibrating means changing the third of those, and the
  per-bubble evidence kept on every result means a new threshold can be
  *evaluated* without re-reading a single image.

The `evaluation` package exists to hold that honest: it consumes the same
public `ScanResult` a later phase will consume. If a benchmark ever needed a
private detail of the engine, that detail would belong on the result - and the
pressure to notice is exactly why the layer is separate. See
`docs/recognition_engine.md`.

### Why the recognition modules are split three ways

`recognition.models` holds the vocabulary (`MarkStatus`, `FieldStatus`,
`Selection`), `recognition.decide` turns one group of measured bubbles into one
`Selection`, and `recognition.fields` assembles groups into fields and a whole
template's worth of values. The split is what makes "two marks were found"
expressible at the bottom and still visible at the top: a blank, a double mark
and an unreadable group are distinct values all the way out to the CSV, rather
than being flattened into a single answer plus a boolean.

`omr_scanner.services.alignment_service` is the seam between the template and
the engine: it turns an `OmrTemplate` into an `AlignmentConfig` and reads scans
from disk, so that `imaging` depends on neither `.omrt` nor the file system.

Each arrow is a function boundary that can be tested with synthetic data. The
canonical page image is the contract between geometry (Phase 1) and recognition
(Phase 3): everything after it works in normalised template coordinates, so a
recognition change can never silently depend on scanner resolution.

## The template designer (Phase 2)

`omr_scanner.gui.template_designer` produces the `.omrt` documents the
pipeline above consumes. It sits entirely inside the `gui` layer's rules -
no `cv2`/`numpy` import anywhere in the package - by keeping pixel work behind
one more service seam:

```text
reference image file
  -> services.marker_detection_service.decode_image_file    plain bytes, not an array
  -> gui.template_designer.canvas                           QImage from those bytes
  -> services.marker_detection_service.detect_registration_markers
  -> gui.template_designer.state.DesignerState               marker geometry (session)
  -> domain.template_authoring                               region generation, validation
  -> gui.template_designer.state.DesignerState               OmrTemplate.model_copy(...)
  -> services.template_service.save_template                 unchanged since Phase 0
```

`marker_detection_service` reuses Phase 1's `imaging.preprocessing` and
`imaging.marker_detection` directly rather than re-detecting markers by other
means - it only replaces Phase 1's *all-or-nothing* four-corner assignment
(correct when there is no human to ask) with an independent per-corner result
(correct when there is): see the module's own docstring for why, and
`docs/phase_02_plan.md` §5 for the layering reasoning.

`domain.template_authoring` holds the region-generation and designer-facing
validation logic in `domain`, not `gui`, because it is pure computation over
the existing `Zone`/`BubbleGrid` model with no Qt or file dependency - the same
placement rule `domain.geometry` and `domain.template` already follow.

The designer edits by producing a new `OmrTemplate` via `model_copy(update=...)`
and pushing it onto `gui.template_designer.history.SnapshotHistory`; there is
no second, mutable template representation to keep in sync with the persisted
format. See `docs/template_designer.md` for the user-facing description.

## The Scan workspace (Phase 3)

`omr_scanner.gui.scan` consumes those `.omrt` documents. It obeys the same
layering rule by the same means - the services hand it plain strings, floats and
bytes, so the package imports neither `cv2`, `numpy`, `imaging` nor
`recognition`:

```text
ScanPage.load_template_from   -> services.template_service.load_template
ScanPage.add_scan_paths       -> services.scan_import.collect_scan_files
ScanPage.process_all          -> gui.scan.worker.BatchWorker  (a QThread)
                                   -> services.batch_processor.process_batch
                                        -> services.parallel_batch (worker processes)
                                        -> services.recognition_service.RecognitionEngine
                                        -> services.filename_manager.FilenameAllocator
                                   -> Qt signals back to the GUI thread
ScanPage.select_scan          -> gui.scan.worker.PreviewWorker
                                   -> gui.scan.preview.ScanPreviewView
ScanPage.export_csv_to        -> services.scan_export.export_scan_results
```

Three decisions worth carrying forward:

- **Naming is separated from copying.** `filename_manager` only *decides* a
  name; `batch_processor` performs the file side effect. That is what makes the
  duplicate rule (`2103123`, `_a`, `_b`, ...) unit-testable without a disk, and
  what lets the GUI show an output name as a preview before anything is written.
- **The allocator consults the output directory, not only its own history**, so
  a second run over the same folder cannot replace the first run's output.
- **A batch never keeps its previews.** A hundred rectified pages is most of a
  gigabyte, so `BatchOptions.with_preview` is off for batch runs and the page
  re-renders only the sheet being looked at, through `PreviewWorker`, caching a
  handful.

`ScanPage` follows the main window's testability split: a `_prompt_*` method
owns each modal file dialog and contains no logic, while the command beside it
(`load_template_from`, `add_scan_paths`, `export_csv_to`) does the work. GUI
tests and the `qtguitesting` scripts drive the second group, so nothing has to
interact with a native file dialog. See `docs/scan_workflow.md` for the
user-facing description.

## The Calibration workflow (Phase 4)

`omr_scanner.gui.calibration` verifies a template against representative
scans and tunes its recognition thresholds - it is deliberately **not** a
second recognition engine:

```text
CalibrationPage.load_template_from  -> services.template_service.load_template
CalibrationPage.add_scan_paths      -> services.scan_import.collect_scan_files
CalibrationPage.run_all             -> gui.calibration.worker.CalibrationWorker  (a QThread)
                                          -> services.recognition_service.RecognitionEngine.open_session
                                               -> CalibrationSession (load + register + measure, once)
CalibrationPage._on_threshold_changed -> CalibrationSession.recompute()   (GUI thread, no worker)
                                          -> services.calibration_service.evaluate_calibration
CalibrationPage.save_to_template     -> services.calibration_service.apply_calibration
                                          -> services.template_service.save_template
```

Two decisions worth carrying forward:

- **Measurement is separated from decision, one level below where Phase 3
  already separates them.** `RecognitionEngine.process()` does load, register,
  measure, decide and present in one call; `open_session()` splits it at the
  measure/decide boundary, returning a `CalibrationSession` that caches the
  measured-but-undecided bubbles. `recompute()` re-runs only decide-and-present
  against a *new* `RecognitionSettings` - never against new pixels - which is
  what makes a threshold slider's "immediate feedback" genuinely immediate
  rather than a smaller re-registration. This is a Phase 3 change (see below),
  not a Phase 4 duplicate of Phase 3.
- **The overlay draws exactly what the engine measured, never an
  approximation.** `MarkerView` carries the detected marker reprojected
  through the fitted homography (`canonical_x/y`) and the template's own
  declared centre (`expected_x/y`), both already in canonical pixels; the
  bubble overlay draws `ScanResult.bubbles` unchanged. The GUI layer performs
  no geometry of its own beyond a click-to-bubble hit test against those same
  coordinates.

A template that fails to register produces a `ScanResult` with no fields,
answers or bubbles at all - by construction, in the one code path every
registration failure goes through - so `evaluate_calibration` cannot describe
such a scan as anything but `CalibrationStatus.FAILED`. There is no separate
"is this template safe" heuristic to keep in sync with the engine's own
failure handling. See `docs/calibration_workflow.md`.

## Concurrency

Phase 0 is single-threaded. From Phase 3, batch recognition runs off the GUI
thread and, when the user's settings allow it, across several CPU cores; the
rules that keep that safe are:

- Services must not touch Qt objects, so they can run off the GUI thread - and,
  for the same reason, inside a worker *process*.
- A `ProjectDatabase` session is short-lived and scoped to one operation.
- The main window must never block on a long operation; progress arrives through
  signals.

### Multicore batch recognition (Phase 3)

```text
              Qt main process
                     │
   ScanPage ── BatchWorker (QThread) ── batch_processor.process_batch
                     │                            │
              progress/results                    │  workers > 1
              back as Qt signals                  ▼
                                        parallel_batch.recognise_in_parallel
                                                   │
                                      ProcessPoolExecutor (spawn)
                                    ┌──────┬──────┬──────┬──────┐
                                    W1     W2     W3     W4  ... WN
                                     │      │      │      │
                                    OMR    OMR    OMR    OMR     ← one page each
                                     └──────┴──┬───┴──────┘
                                               ▼
                                     RecognitionResult objects
                                    (completion order, indexed)
                                               │
                                               ▼
                               batch_processor, in the main process:
                                 re-orders into batch order
                                        │
                                 FilenameAllocator  ← the single allocator
                                        │
                                 copy into the output folder
                                        │
                                 BatchReport -> CSV / scan list
```

Four properties follow from that shape, and each is the reason for it:

- **One page per worker.** The whole pipeline for one sheet - load, register,
  measure, interpret - runs inside one process. Nothing is shared and nothing is
  mutated across processes, so the answer cannot depend on how the work was
  divided.
- **Filename assignment is centralised.** Workers return recognition results and
  nothing else. If each worker named its own file, two sheets that legitimately
  recognise to the same roll number would both choose `2103123.jpg` and one
  would silently overwrite the other - a lost script, the worst failure this
  application has. One allocator, in one process, called in batch order, makes
  that impossible rather than unlikely.
- **Order is restored before anything is named or recorded.** Results arrive out
  of order and are buffered until their predecessor has landed, so the duplicate
  suffixes (`_a`, `_b`, ...), the scan list and the CSV come out in scan-list
  order on any number of cores. Progress, by contrast, counts *completions*, so
  the bar advances steadily instead of waiting on the slowest sheet.
- **Only serialisable data crosses the boundary.** The template (a Pydantic
  model) is sent to each worker once, when the pool starts; each task carries an
  index and a path; each result is plain data - no Qt objects, no open handles,
  no NumPy arrays (batch runs discard previews anyway).

The pool uses the `spawn` start method on every platform, not the Linux default:
forking a process that already owns a Qt event loop and OpenCV thread pools is
unsafe, and a code path only exercised on Linux is not the one that runs on
Windows. Spawn re-imports the package in each child, which is why every entry
point is guarded by `if __name__ == "__main__":` and why `main()` calls
`multiprocessing.freeze_support()` - without either, a worker would start a
second copy of the application.

How many workers is a *user setting* (`AppConfig.processing`, see
[Configuration layers](#configuration-layers)), resolved by one pure function so
the Settings dialog's "workers this setting uses" and the run itself can never
disagree. See `docs/scan_workflow.md` §10 for the modes, the measured
throughput and the reasoning behind the automatic cap.

### Progress: counted centrally, drawn on a timer

```text
worker processes ──► batch_processor ──► BatchWorker (QThread)
   one page each        one progress            │
                        event per sheet         │ tracker.record(status)
                                                ▼
                                    BatchProgressTracker   (thread-safe)
                                    counters · EMA rate · ETA
                                                │
                                                │ snapshot()   ← pulled
                                                ▼
                                    ScanPage QTimer, every 200 ms
                                                │
                                                ▼
                                       progress panel widgets
```

Three decisions, each aimed at the ten-thousand-sheet case:

- **Counting happens in one place, under one lock.** Workers report; they
  never increment a shared counter and never touch a widget. Eight sheets
  finishing in the same instant is then an ordinary case rather than a race,
  and the totals always reconcile with the report the batch produces.
- **The GUI pulls, it does not get pushed.** A batch finishing fifty sheets a
  second would otherwise ask Qt to repaint fifty times a second. The tracker
  is updated on every completion - the counts are exact - and the window reads
  a snapshot five times a second. Row updates are buffered the same way, and
  rows are found through a path index rather than by searching the list, which
  is what keeps a long batch linear rather than quadratic.
- **A snapshot is a value.** `ProgressSnapshot` is immutable, so a slow repaint
  can never show a half-updated batch, and the whole estimator can be tested
  headlessly with a fake clock.
