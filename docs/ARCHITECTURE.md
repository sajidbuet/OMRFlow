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
| `omr_scanner.services` | Multi-step operations: create/open project, process a batch, calculate results, judge a calibration run (`calibration_service`, Phase 4). Owns all side effects. Phase 10 adds `scan_provenance` (content-hash provenance and duplicate detection), `project_lock` (plain-file project locking, deliberately not `QLockFile`, to stay Qt-free), `project_backup` (SQLite-online-API snapshots), `project_health` (integrity/health checking, no repair path), and `telemetry` (sampling for a long batch run). | Widgets, dialogs, Qt imports of any kind. |
| `omr_scanner.reporting` | XLSX/PDF generation mechanics *(Phase 9)*: `excel.py` populates a copied result template and builds the supporting sheets; `pdf.py` is the LibreOffice-backed exporter abstraction. CSV export is `services.scan_export`. | Result calculation, Qt, the database. |
| `omr_scanner.gui` | Windows, pages, dialogs; presenting state and collecting intent. `gui.theme` is the design system (tokens, then the stylesheets composed from them) and `gui.widgets` the application shell's parts and shared presentation primitives - see [The application shell](#the-application-shell) below; `gui.template_designer` (Phase 2) is the interactive `.omrt` editor; `gui.calibration` (Phase 4) verifies and tunes a template against representative scans before a batch; `gui.scan` (Phase 3) is the batch scanning workspace, including benchmark mode; `gui.devtools` (Phase 3) is the Tools > Developer / Testing front end to `evaluation`; `gui.health_dialog` (Phase 10) is Project Health & Recovery. | OpenCV, NumPy, SQLAlchemy, direct database access, any OMR algorithm. |
| `omr_scanner.evaluation` | Judging the engine: the ground-truth schema, the named test cases and dataset planner, the renderer, the benchmark and its error categories, and the benchmark session *(Phase 3)*. Sits *above* services, beside the GUI. Phase 10 adds `stress_dataset` (a deterministic, index-addressable synthetic-sheet generator reusing these same rendering primitives, for the 100,000-sheet production-hardening stress test) and `stress_runner` (orchestration that reuses `services.batch_processor` unchanged), plus `qualification` (the 100,000-sheet release-qualification campaign: it supervises `tools.benchmark_stress` as child processes from outside, kills one deliberately, and judges what survived). | Qt - which is why `qualification.describe_environment()` records NumPy and SQLAlchemy versions and deliberately not a Qt version; also any recognition of its own, since a benchmark that re-implements the engine measures itself. |
| `omr_scanner.tools` | Developer command line utilities that drive one stage against one file. Beside the GUI, not below it. Phase 10 adds `benchmark_stress` (the headless stress-test/kill-resume CLI) and `phase10_qualification` (the unattended 100,000-sheet qualification campaign, which drives `benchmark_stress` as child processes - see [`phase10_qualification.md`](phase10_qualification.md)). | Qt, and any algorithm of its own - a tool parses arguments, calls a service, and prints. |
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

## The application shell

The window is three bands - one chrome row, the stacked pages, and a status
footer - and `MainWindow` assembles them and owns nothing about how any of
them looks.

```text
+----------------------------------------------------------------------+
| = [OMRFlow] | - +  <  [1 Project > 2 Template > ... > 9 Reports]  >   |
|                                                          _   []   X   |
+----------------------------------------------------------------------+
|  the current stage's page, starting here                              |
+----------------------------------------------------------------------+
| OMRFlow v… | Project: …      Developed by … | Open Source … | * Ready |
+----------------------------------------------------------------------+
```

Qt's own `QStatusBar` sits below the footer and carries transient messages
only ("Project opened read-only", "Recovered 3 scan(s)"). Its permanent
project indicator was removed with this redesign: it read
`"<folder name>  (<absolute path>)"` two rows beneath a footer that now names
the examination, so it was simultaneously a duplicate and the long filesystem
path the footer's own rule exists to avoid showing.

The chrome row **is** the title bar: the window is frameless and the three
window buttons live at its right end. That merge is the point of the design -
it removed a branded header band, a separate navigator band and a per-stage
heading, and gave roughly a hundred logical pixels of every screen back to
the workspace. There is no left sidebar either; the horizontal ribbon
replaced one.

```text
gui/theme/      tokens.py       colours, spacing, radii, type, shell metrics,
                                the ribbon's density levels
                                (no Qt import at all)
                stylesheet.py   the Qt stylesheets, composed from tokens
gui/widgets/    app_chrome      the single chrome row: menu button, wordmark,
                                density and previous/next controls, the
                                ribbon, the window buttons, window dragging
                window_buttons  minimise / maximise / restore / close
                workflow_ribbon / workflow_step
                                the one-line responsive chevron ribbon and
                                its narrow-mode stage selector
                status_footer   build, project, credit, licence, status
                page_header     a heading a page may give itself (no stage
                                does; the ribbon already names it)
                card            card, empty state, clickable action row
                buttons         primary / secondary / destructive roles
```

Four rules hold this together.

**Every visual constant is named once.** A colour, a gap or a radius lives in
`theme.tokens` and nowhere else; `tests/unit/test_theme_tokens.py` fails if a
hex literal appears in a stylesheet, and computes the WCAG contrast ratios
rather than trusting a comment about them. `theme.tokens` imports no Qt, which
is what lets those checks run without a display.

**Each part decides its own layout from its own width.** The ribbon picks one
of three layouts by measuring nine labels in the font currently in use; the
Project dashboard picks one or two columns by asking whether both columns'
minimum readable widths still fit. Neither consults the window, and neither
consults the other - the brief requires those two transitions to be
independent, and they genuinely need different thresholds. There is no
resolution constant anywhere in the shell.

**The workflow is always one line.** Never two rows, never a grid. A workflow
is an ordered sequence, and a second row breaks the one thing the chevrons
exist to show. When the nine no longer fit, the strip scrolls; when scrolling
would show barely one stage at a time, it collapses to the current stage plus
a selector listing all nine. Neither hides a stage - every one keeps its
widget, its enabled state and its place in the order, and stays reachable.

**Layout changes move widgets; they never rebuild them.** A responsive
transition repositions the same nine step widgets and re-parents the same two
dashboard columns. Rebuilding would discard page state and, on the editor
stages, reload sheet images for no reason.

### Why the menu bar is hidden rather than removed

The chrome row's menu button replaces the permanent *File / Tools / Help* row, but
the menus are still built on `menuBar()` and are then added to one application
menu as submenus. That keeps the hierarchy, the nesting, the actions and Qt's
own shortcut context exactly as they were, with nothing duplicated or
reimplemented. Each shortcut-bearing action is additionally added to the
window, because Qt deactivates the shortcuts of actions that live only in a
hidden widget -
`tests/gui/test_app_shell.py::TestDShortcutsStillFire` presses the keys to
prove it.

### Why the chevron is painted rather than composed

A chevron is a `QPainterPath` computed from the widget's own size each repaint,
so it is exact at any width and any device pixel ratio. Two consequences follow
and are easy to get wrong:

* Consecutive steps **overlap** by the arrow depth, so `hitButton` tests the
  path, not the rectangle. A rejected press is *ignored* and goes to the
  parent, never to the sibling underneath, so without this a click on step 4's
  visible arrowhead would do nothing at all. For the same reason the steps are
  stacked with step 1 on top.
* The scrolled strip has **no visible scrollbar at all**. The ribbon is one
  34-pixel line inside a 46-pixel chrome row, and a horizontal scrollbar would
  take a third of that line from the chevrons - which is exactly what clipped
  them in the previous design. Scrolling is the wheel (either axis, with or
  without Shift), the previous/next buttons beside the strip, and the
  automatic scroll that brings the active stage into view after every
  navigation.
* Measurement has to agree with the renderer. `QFontMetrics.horizontalAdvance`
  sums glyph advances while `elidedText` lays the string out, and they differ
  by up to a pixel; a step drawn at exactly its measured advance therefore
  elides. `ELISION_SLACK` is that pixel, and without it the first label
  rendered as "1. Proje..." in the layout chosen precisely because all nine
  fitted.

### Why the window is frameless, and what that cost

Drawing the title bar inside the chrome row is what made the merge possible,
and it is only worth doing if the window still behaves like a Windows window.
Nothing re-implements what the platform already does:

| Behaviour | Mechanism |
|---|---|
| Move, and with it Aero Snap | `QWindow.startSystemMove()` from the chrome row |
| Resize from edges and corners | `QWindow.startSystemResize()` from a 5px border |
| Minimise / maximise / restore | `showMinimized()` / `showMaximized()` / `showNormal()` |
| Maximised geometry, multi-monitor, DPI | Qt and the window manager, untouched |
| Taskbar, Alt+F4, Win+Up/Down, activation | unaffected - it is still an ordinary top-level window |

A press on the chrome row is classified before it can drag: `is_drag_area`
answers "is there an interactive child here?", so adding a control to the row
cannot silently make its area draggable. The logo and the separator opt *in*
to dragging by being transparent to the mouse.

One thing is genuinely lost and is not worked around: **Windows draws no drop
shadow around a frameless window.** Restoring it means a DWM call or a
translucent parent widget, both of which are the brittle platform hack the
design brief rules out. A hairline border on the central widget stands in for
it.

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
└── ReportingError      Excel/PDF generation failures (Phase 9)
    ├── ExcelReportError  A workbook could not be built
    └── PdfExportError    A PDF could not be produced
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

## Durable batches (Phase 5)

Phase 5 adds persistence *around* the Phase 3 pipeline rather than inside it.
`services.batch_processor` still knows nothing about a database; the store is
attached through the `on_result` hook it already had:

```text
ScanPage._start_batch
  -> services.batch_store.create_batch        (one row per scan, all PENDING)
  -> services.batch_store.mark_queued / set_batch_status(RUNNING)
  -> gui.scan.worker.BatchWorker(recorder=BatchRecorder)   (a QThread)
       -> services.batch_processor.process_batch
            -> services.parallel_batch          (worker processes: results only)
            -> on_result -> BatchRecorder.record  (buffered)
                              -> batch_store.record_results  (one transaction)
       -> BatchWorker.run flushes the recorder before emitting finished_report
  -> ScanPage._settle_batch_state
       -> batch_store.mark_cancelled | finalise_batch
```

Four decisions worth carrying forward:

- **Only the coordinating process writes.** Worker processes return recognition
  results and nothing else: they never open the database, never assign an
  output name, never touch shared state. SQLite is a single-writer store and
  the architecture keeps it that way by construction rather than by locking
  discipline. This is the same reason the workers do not name files (see
  "Multicore batch recognition" below) - one rule, two consequences.
- **Persistence is optional, and its absence is visible.** With no project
  open there is no recorder, `process_batch` runs exactly the code path it
  always did, and the page says the run will not be saved. That is what keeps
  the benchmark, the command line tools and most tests free of a database.
- **Results are committed in groups**, not one transaction per sheet. One
  `fsync` per sheet dominates a run on a spinning disk or a synchronised
  folder; buffering to whichever of 25 sheets or 2 seconds comes first bounds
  what an abrupt termination costs to a second or two of finished work. The
  bound is a documented trade and is asserted by a test.
- **A storage failure is not a recognition failure.** `BatchRecorder` records
  the first one, keeps the buffer so a later flush can retry, lets the batch
  continue, and the page reports it in a dialog at the end. A run whose results
  could not be written is never presented as a clean success.

The one piece of state that needs repairing rather than reading is a row left
`QUEUED` or `PROCESSING`: those states are only valid while some process owns
the row, so a project being *opened* proves nobody does.
`batch_store.recover_interrupted` is called once, from `MainWindow._adopt_session`,
before any page sees the session.

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

## Conflict review (Phase 6)

`omr_scanner.gui.review` puts a person in front of every value recognition was
unsure about. It adds no recognition, no thresholds and no geometry of its own:

```text
ScanPage._generate_conflicts       -> services.conflict_policy.detect_conflicts   (pure)
   (after the batch, in the           -> services.review_store.sync_conflicts
    coordinating process)          -> conflict_policy.detect_duplicate_identifiers
                                      -> review_store.sync_duplicate_identifiers

ResolvePage.refresh_queue          -> review_store.list_conflicts / count_conflicts   (SQL)
ResolvePage._load_sheet_for        -> gui.review.worker.SheetWorker  (a QThread)
                                        -> services.recognition_service.RecognitionEngine.process
ResolvePage.accept / correct       -> review_store.accept_machine_value / correct_value
   / defer / reopen                     -> one transaction: audit event + state
ResolvePage._refresh_provenance    -> review_store.provenance_for   (fold over the ledger)
ScanPage._export_resolutions       -> review_store.sheet_resolutions -> scan_export
```

Four decisions worth carrying forward:

- **Detection reads the engine's own judgement.** `conflict_policy` maps each
  group's `MarkStatus` and the `needs_review` flag `recognition/decide.py`
  already computed — from **the template's own** `ambiguity_margin` and
  `min_confidence` — into conflict types. There is no threshold in the module
  and no second opinion about the pixels. Calibrating a template in Phase 4
  therefore moves the conflict queue with it, which is the same
  one-source-of-truth argument as the overlay above.
- **The final value is projected, never stored.** There is no `resolved_value`
  column. `review_store.provenance_for` folds a conflict's ordered audit events
  over the machine's reading, so the machine's observation, every superseded
  correction, its reviewer and its reason all survive — and no application code
  has to keep a third copy of the truth in step. `ReviewConflict.state` *is*
  cached, for the queue's sake, and `recompute_state()` proves it equal to the
  fold.
- **The append-only ledger is enforced below the code that uses it**: no update
  or delete on the service surface, none in the application, and two SQLite
  triggers that abort either statement. Correctness here does not depend on a
  future contributor remembering. `audit_event` carries no foreign key, so a
  decision outlives the row it was about and Phases 7-9 can audit into the same
  table without a migration.
- **Highlighting the original scan uses the engine's own inverse homography.**
  `ScanResult.source_transform` carries it, and
  `recognition_service.map_canonical_to_source()` is the one place it is
  applied. The GUI may not import OpenCV or NumPy, and a second implementation
  of the engine's geometry is a second thing to drift — the failure Phase 4's
  audit named as the most serious possible.

Detection runs **in the coordinator after the batch finishes**, never in a
worker: a worker process must not open the single-writer project database, and a
duplicate identifier is not a property of one sheet. Phase 5's pool,
cancellation, resume and progress are unchanged. See `docs/conflict_review.md`.

## Candidate reconciliation (Phase 7)

`omr_scanner.gui.attendance` matches the scripts a batch produced against the
candidates an examination office registered. It adds no recognition and no
image handling at all:

```text
AttendancePage.import_from      -> gui.attendance.import_dialog.RosterImportDialog
                                     -> gui.attendance.worker.RosterReadWorker  (a QThread)
                                        -> services.candidate_import.read_roster   (pure)
AttendancePage.commit_roster    -> services.reconciliation_store.import_roster    (one transaction)
AttendancePage.reconcile        -> gui.attendance.worker.ReconcileWorker  (a QThread)
                                     -> services.reconciliation_store.reconcile_batch
                                          -> review_store.effective_identifiers   (Phase 6)
                                          -> services.reconciliation.reconcile     (pure)
AttendancePage.assign_selected_script
   / toggle_selected_exclusion  -> reconciliation_store.assign_script / set_script_excluded
   / toggle_attendance             / override_attendance / dismiss_entry
                                     -> one transaction: decision + audit event
                                     -> re-reconcile, so consequences are visible
```

Four decisions worth carrying forward:

- **Four values are kept apart on purpose**: what the roster file said, what
  recognition read, what an operator decided, and what follows from the three.
  Only the last is computed. `registered_candidate` is write-once and the
  machine's reading is never overwritten, so a change of effective value can
  never cost the record of what it changed *from*. This is Phase 6's invariant
  extended to imported data.
- **Classification is a pure function.** `services.reconciliation.reconcile`
  takes value objects and returns them; it reads no clock, no configuration and
  no database. That is what lets the rules be tested as a table rather than
  through an interface, and what makes a re-run idempotent - the stored entry
  and script rows are a *cache* of it, rewritten wholesale, so a stale
  classification cannot survive a change of roster. Operator decisions are the
  **input** to that function, which is why they live in their own table and
  survive the rewrite.
- **An entry carries a set of issues, not one status.** A candidate recorded
  absent with two scripts is both `ABSENT_WITH_SCRIPT` and `DUPLICATE_SCRIPT`;
  a schema or interface that could hold one would make a physical script
  invisible. The headline is derived by documented precedence and never
  replaces the set.
- **Candidate identity is resolved through Phase 6, never around it.**
  `review_store.effective_identifiers` is the single place that answers "which
  candidate does this sheet say it belongs to" after review, so reconciling
  against `BatchScan.identifier_value` - the machine's own reading - is
  impossible by construction. An identifier still awaiting review is its own
  state rather than an unknown candidate.

Reconciliation runs after recognition, off the GUI thread, and touches nothing
about how a batch runs. Candidate data is displayed and stored, never logged;
see `docs/reconciliation.md`.

## Answer keys and scoring (Phase 8)

`omr_scanner.gui.answer_key` and `omr_scanner.gui.results` turn recognised
answers into marks. Neither does any arithmetic of its own:

```text
AnswerKeyPage.read_from_scan  -> services.RecognitionEngine.process      (the existing engine)
                                   -> services.answer_key.key_from_scan  (pure)
AnswerKeyPage.save_key        -> services.scoring_store.save_key         (a new revision, always)
AnswerKeyPage.verify_key      -> services.scoring_store.verify_key       (supersedes the previous)

ResultsPage.configure_policy  -> services.scoring_store.save_policy      (a revision only if a rule changed)
ResultsPage.score_batch       -> gui.results.worker.ScoringWorker  (a QThread)
                                   -> services.scoring_store.score_batch
                                        -> review_store.effective_answers / effective_set_codes
                                        -> services.scoring.score_candidate      (pure: eligibility)
                                             -> domain.scoring.score_answers     (pure: arithmetic)
ResultsPage._show_result      -> scoring_store.breakdown_for             (regenerated, not stored)
```

Four decisions worth carrying forward:

- **Marks are exact rationals, and rounding happens once.** Every mark is a
  `fractions.Fraction`; a decimal typed into the configuration is read with
  `Fraction(Decimal(text))` and never through `float`. `0.1 + 0.2` is not
  `0.3` in binary floating point, and a hundred questions at `-1/3` accumulate
  an error that depends on the order they were added. Formatting to two places
  is presentation and never feeds back.
- **A result stores its inputs, not just its mark.** The answer string, the
  key revision and the policy revision are all on the row, and the per-question
  breakdown is *regenerated* by the same pure function that produced the total.
  A detail view and a total therefore cannot disagree - and a million-row
  breakdown table that could is avoided entirely. This is the same
  "one calculation, not two copies" argument as Phase 6's projected provenance.
- **Changing an input makes a result stale; it never changes the number.**
  `stale_reasons_for` compares what a result *was computed from* against what
  is current, and recomputation runs the whole scorer again. Nothing anywhere
  adds a delta to an existing mark - asserted by a test that corrupts a stored
  score and checks the rescore ignores it.
- **A revision is never edited.** Correcting a verified key creates the next
  revision and supersedes the old one, which is kept because results point at
  it. "Which key produced this mark" is answered from the result, never
  inferred from whichever key happens to be current.

Eligibility is separate from arithmetic: `services.scoring` knows about
candidates, scripts and the four phases underneath and produces either a mark
or a list of blocks; `domain.scoring` knows only about strings and fractions.
That split is why the marking rules can be tested as a table. See
`docs/scoring.md`.

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
