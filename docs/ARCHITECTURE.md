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
┌──────────────────────────────────────────────┐
│  gui          PySide6 windows, pages, dialogs│
└───────────────────────┬──────────────────────┘
                        │ calls
┌───────────────────────▼──────────────────────┐
│  services     application workflows          │
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
| `omr_scanner.recognition` | Turning measurements into logical values with confidence, missing/multiple-mark handling. *(reserved - Phase 3/6)* | Pixel access, OpenCV, persistence, Qt. |
| `omr_scanner.database` | Schema, migrations, engine and session lifetime. | Workflow logic, Qt, OpenCV. |
| `omr_scanner.services` | Multi-step operations: create/open project, process a batch, calculate results. Owns all side effects. | Widgets, dialogs, Qt imports of any kind. |
| `omr_scanner.reporting` | CSV/XLSX/PDF generation. *(reserved - Phase 9)* | Result calculation, Qt. |
| `omr_scanner.gui` | Windows, pages, dialogs; presenting state and collecting intent. | OpenCV, NumPy, SQLAlchemy, direct database access, any OMR algorithm. |
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
| Application, per user | `omrflow.config.json` in the platform config directory | log level, recent projects |
| Project | `<project>/project.json` | project name, id, timestamps |
| Template | the `.omrt` document | page geometry, zones, bubble grids |
| Recognition | nested inside the template (`recognition`) | fill threshold, confidence floor |

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

Everything up to the canonical page image exists (Phase 1); everything after it
does not. The detail is in `docs/IMAGE_PROCESSING.md`.

```text
raw scan
  -> imaging.preprocessing      grayscale, downscale, denoise, threshold
  -> imaging.marker_detection   candidate measurement, filtering, corner choice
  -> imaging.orientation        which scan corner is the sheet's top-left
  -> imaging.geometry           ordering, validation, homography
  -> imaging.alignment          warp; the align_sheet entry point
  -> canonical page image       (matches the template's canonical geometry)
  -> imaging.metrics            per-bubble fill measurements       (Phase 3)
  -> recognition.fields         values with confidence             (Phase 3)
  -> services.scan_service      persistence, conflicts, progress   (Phase 5)
```

`omr_scanner.services.alignment_service` is the seam between the template and
the engine: it turns an `OmrTemplate` into an `AlignmentConfig` and reads scans
from disk, so that `imaging` depends on neither `.omrt` nor the file system.

Each arrow is a function boundary that can be tested with synthetic data. The
canonical page image is the contract between geometry (Phase 1) and recognition
(Phase 3): everything after it works in normalised template coordinates, so a
recognition change can never silently depend on scanner resolution.

## Concurrency

Phase 0 is single-threaded. From Phase 5, batch processing runs in worker
threads; the rules that keep that safe are set now:

- Services must not touch Qt objects, so they can run off the GUI thread.
- A `ProjectDatabase` session is short-lived and scoped to one operation.
- The main window must never block on a long operation; progress arrives through
  signals.
