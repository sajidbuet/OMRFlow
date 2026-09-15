# Current state

**Updated:** 2026-09-15
**Version:** 0.1.0.dev0
**Current phase:** Phase 2 complete. Phase 3 not started.

Update this file at the end of every phase.

## What works

### Application shell and projects (Phase 0)

- The application starts (`python -m omr_scanner` or `omrflow`) and shows the
  main window with eight workflow stages, a status bar, File and Help menus.
- A project can be created: a folder containing `project.json`,
  `database.sqlite` and the seven standard sub-directories.
- A project can be closed and reopened, with its identity and metadata intact.
- An invalid project (plain folder, damaged or missing `project.json`, missing
  database, newer format version) is refused with a readable message; no
  traceback reaches the user.
- The project database initialises to schema version 1 through a recorded
  migration, and is refused if it was written by a newer build.
- `.omrt` templates can be loaded, validated and saved. The geometry that locates
  a bubble in a grid is implemented and tested.
- The recent-projects list, log level and default project folder persist between
  runs in the per-user configuration file.
- Logging: an application log plus a per-project log while a project is open.

### Geometric normalisation (Phase 1)

- `omr_scanner.imaging.align_sheet(image, config=...)` turns an arbitrary scan
  of an OMR sheet into the canonical page the template describes, at exactly the
  declared canonical size.
- The four printed registration squares are detected by combined shape evidence
  (area ratio, aspect ratio, rectangularity, solidity, interior ink) and chosen
  by a score that also accounts for position, through an exhaustive one-to-one
  assignment to the four scan corners.
- Page orientation (0/90/180/270 degrees) is resolved by rectifying the
  orientation mark's expected window out of the scan under each of the four
  hypotheses, after pruning those whose page geometry is implausible.
- Rotation, translation, scale, skew and perspective are corrected in one
  homography; the inverse is returned too, for future GUI overlays.
- The result carries measured quality metrics and non-fatal warnings, not a
  single opaque confidence.
- Every expected failure raises an `ImagingError` subclass with a stable code
  (`INVALID_IMAGE`, `INSUFFICIENT_MARKERS`, `AMBIGUOUS_MARKERS`,
  `ORIENTATION_NOT_FOUND`, `INVALID_PAGE_GEOMETRY`,
  `ALIGNMENT_TRANSFORM_FAILED`). A missing corner marker is never extrapolated.
- `omr_scanner.services.alignment_service` converts an `OmrTemplate` into an
  `AlignmentConfig` and reads and writes image files.
- Optional diagnostics render a detection overlay, a rectified preview and a
  textual summary; they are off by default and cannot affect the outcome.
- `omr_scanner.imaging.synthetic` generates synthetic canonical sheets with
  interior control points and applies seeded, reproducible distortions.
- Two developer tools: `python -m omr_scanner.tools.align_image` and
  `python -m omr_scanner.tools.make_test_sheet`.

**Measured accuracy** (synthetic, 40 distortion cases x 9 interior control
points, default 1240 x 1754 page): mean 0.062 px, 95th percentile 0.145 px,
maximum 0.394 px, zero failures. Regression threshold in the suite: 1.5 px.
All four page orientations resolved with confidence 1.00 and margin 1.00.
Alignment takes 12 ms for A4 at 150 dpi and 39 ms at 300 dpi.

### Interactive template designer (Phase 2)

- The "Template" workflow stage is a real page: load a reference sheet image;
  detect the four registration markers (reusing Phase 1's detector, scored per
  corner independently so a partially-damaged sheet still reports three good
  corners); drag markers and the orientation mark into place by hand;
  auto-detected geometry is visually distinct (amber) until confirmed (green).
- Draw regions on the canvas and configure them through a dialog: Student ID
  (numeric), Question Set (set-code), Questions (question-block, generated as
  one zone per printed column, so a 100-question/4-column block is four zones
  holding 400 individually-addressable bubbles, never one rectangle), Custom
  (alphanumeric), and a reference/ignored region.
- Select, drag, resize, duplicate, rename, delete, and show/hide any region;
  numeric X/Y/Width/Height editing in the properties panel, in both pixels and
  normalised fractions, kept in sync with the canvas both ways.
- Per-bubble fine-tuning: toggle "Edit Bubbles" on a selected region to drag
  one bubble's centre, recorded as a `BubbleOverride` (the existing Phase 0
  mechanism, not a new concept); reset per region.
- Undo/redo (one entry per completed gesture, not per mouse-move), coalesced
  by construction because the history is a stack of the immutable `OmrTemplate`
  itself, not a separate command hierarchy.
- Validation beyond what the schema already enforces: overlapping regions,
  regions overlapping a marker, duplicate or missing question numbers.
- Save/Save As/Open with dirty-state tracking and a title bar `*`; a template
  records its reference image's path (new, additive field
  `OmrTemplate.reference_image`) so reopening it restores the canvas exactly.
- Zoom (wheel, in/out, fit, 100%), pan (space+drag), an optional alignment
  grid overlay.
- The GUI never imports `cv2`/`numpy`: `omr_scanner.services.marker_detection_service`
  is the seam, handing the GUI plain bytes and floats.

**Example**: `examples/templates/100_question_4_choice_example.omrt` - a
7-digit student ID, A-D question set and 100 questions in 4 columns (474
bubbles total), built and validated through the real generator functions
(`omr_scanner.domain.template_authoring`), not hand-typed JSON.

## What does not exist

No bubble recognition, batch processing, conflict resolution, attendance
reconciliation, answer-key handling, scoring, or Excel/PDF reporting.

`omr_scanner.recognition` and `omr_scanner.reporting` contain module
documentation and no code. The corresponding GUI pages say which phase will
implement them and do not simulate anything. The Scan page is still a
placeholder: Phases 1 and 2 delivered the alignment engine and the template
designer, not the batch scanning workflow.

**This build must not be used for examination processing.**

## Known limitations

### Alignment (the ones that matter)

- **No real-world validation has been performed.** Every accuracy number above
  is synthetic. No real scan has ever been processed. This is the largest open
  risk in the project.
- Arbitrary rotation is corrected up to ±15 degrees, plus exact quarter turns.
  Beyond that the markers leave the corner search regions; widening
  `corner_search_width`/`_height` to 1.0 handles 20-75 degrees at the cost of no
  positional filtering.
- The default Otsu threshold fails on an illumination gradient beyond 0.45;
  `adaptive_mean` survives to 0.95 but produces many more spurious contours.
- Cropping beyond about 5 per cent of the page width into the margin fails, by
  design.
- Only square markers are exercised; `filled_circle` would pass the filters by
  accident rather than by design, and `shape` is not consulted.
- One sheet per call, single-threaded. Batch parallelism is Phase 5.

The measured degradation boundary for blur, noise, brightness, illumination,
perspective, JPEG compression and cropping is tabulated in
`docs/IMAGE_PROCESSING.md` § 20.

### Template designer

- **No real printed sheet has been used with the designer.** Every functional
  check (including the required 12-step demonstration,
  `docs/testing/phase_02_demo.md`) used a synthetic reference image.
- No snap-to-grid while dragging (the pure function exists and is tested; not
  wired into interactive dragging yet).
- No align-left/right/top/bottom or distribute tools for multiple selected
  regions.
- Individual-bubble override reset is per-region (the "Reset" action), not
  per-bubble.
- Documentation screenshots could not be captured meaningfully in this
  environment: the offscreen Qt platform plugin used here renders every label
  as a placeholder box rather than a glyph (a font-backend limitation of that
  plugin, not of the application). See
  `docs/development/phase_02_implementation_notes.md`.

### Elsewhere

- The example template in `resources/templates` is illustrative. Its coordinates
  have never been calibrated against a printed sheet. A second, larger example
  (`examples/templates/100_question_4_choice_example.omrt`, Phase 2) is built
  and validated through the real generator code, but is likewise synthetic.
- Project metadata is written once at creation; nothing updates `modified_at`
  yet for a *project* (a template's `modified_at` is bumped on every save,
  since Phase 2 added that).
- The GUI is functional but visually plain: no icons, no theming, no window
  geometry persistence.
- No packaging or installer; the application runs from a source checkout.
- `resources/icons` is reserved and empty.

## Test status

729 tests, all passing (Python 3.12.7, PySide6 6.11.2, OpenCV 5.0.0, NumPy
2.5.3, Windows 11).

```text
pytest         729 passed in 12.9s
ruff check .   All checks passed
mypy           Success: no issues found in 58 source files
```

146 of those tests are new in Phase 2 (455 were new in Phase 1). Phase 2 added
**one** new tool configuration change, not a suppressed check: the
`pep8-naming` Qt-override allowlist in `pyproject.toml` was extended to cover
the additional Qt event-handler names the designer's canvas and graphics items
override (`mousePressEvent`, `wheelEvent`, `drawBackground`, and similar). The
documented exception list in `docs/DEVELOPMENT_GUIDE.md` is otherwise
unchanged from Phase 0 (`D107` and `ANN401` project-wide, test-only
relaxations, `warn_unreachable` off for the one module that branches on
`sys.platform`, and relaxed import handling for PySide6's generated stubs).

## Important architectural decisions

- Python 3.12 + PySide6 desktop application; every layer below `gui` is free of
  Qt, so the services remain usable headlessly ([ADR-0001](../docs/decisions/ADR-0001-desktop-python-pyside6.md)).
- A project is a folder with `project.json` plus `database.sqlite`; scans are
  referenced in place, internal paths are relative ([ADR-0002](../docs/decisions/ADR-0002-project-on-disk-layout.md)).
- Forward-only hand-written migrations with a ledger table; a newer schema is
  refused rather than downgraded ([ADR-0003](../docs/decisions/ADR-0003-schema-migrations.md)).
- Template coordinates are normalised to the canonical page; bubble centres are
  derived from a stored pitch ([ADR-0004](../docs/decisions/ADR-0004-normalized-template-coordinates.md)).
- Layering rules are executable: `tests/unit/test_architecture.py` fails if the
  GUI imports OpenCV or SQLAlchemy, if `imaging` imports Qt, if the new `tools`
  package imports a widget, or if a module lacks a docstring.
- Recognition thresholds live in the template, never in code. Alignment *tuning*
  is the one qualified case and is confined to `omr_scanner.imaging.config`,
  where every value is named, documented and validated; everything describing
  the *sheet* still comes from the template.
- Alignment maps detected marker centres onto canonical **marker centres**, not
  onto page corners, so the printed margin outside the markers is not stretched
  across the output.
- Detection speaks in scan corners (`ImageCorner`); only orientation resolution
  assigns canonical roles (`MarkerRole`). The two are distinct types, because
  conflating them is how an upside-down sheet becomes a confident wrong answer.
- An alignment failure is reported, never worked around. Three real corners and
  one invented one would produce a plausible rectification and a complete set of
  wrong answers.
- The machine's recognised value is never overwritten by a correction; that
  constraint shapes the data model from the start.
- The template designer edits by producing a new `OmrTemplate` via
  `model_copy(update=...)` and pushing it onto a snapshot-stack undo history -
  there is no second, mutable template representation to keep in sync with the
  persisted format (`docs/phase_02_plan.md` §4).
- Marker/orientation *detection provenance* (auto-detected, confirmed,
  confidence) is designer session state, never persisted: a scanner only needs
  a marker's geometry, which the domain model already stores.
- The designer never imports `cv2`/`numpy`: `services.marker_detection_service`
  hands it plain bytes and floats, enforced by the same executable layering
  test as everything else in `gui`.

## Next recommended action

Begin **Phase 3 - Bubble Mapping & Recognition Engine**
(`development/ROADMAP.md`). Entry conditions, constraints and the suggested
starting prompt are in `development/PHASE_02_HANDOFF.md`.

Independently of Phase 3, and worth doing as soon as a printed sheet can be
obtained: scan one, anonymise it, build a template for it in the designer, and
run `python -m omr_scanner.tools.align_image <scan> --debug <dir>` against
that template. Neither the alignment engine nor the template designer has ever
seen real paper - this is the largest open risk carried forward from both
Phase 1 and Phase 2.
