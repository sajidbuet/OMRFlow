# Phase 2 handoff — Template Data Model & Template Designer Core

**Completed:** 2026-09-15
**Version:** 0.1.0.dev0
**Environment verified on:** Windows 11, Python 3.12.7, PySide6 6.11.2, OpenCV
5.0.0, NumPy 2.5.3

This document is the entry point for whoever continues the work. It records
what the interactive template designer does, how it was verified, and what it
does not do. Read alongside `docs/phase_02_plan.md` (written *before*
implementation - the interpretation of the existing repository and the
decisions that followed from it) and
`docs/development/phase_02_implementation_notes.md` (decisions and problems
encountered *during* implementation).

---

## 1. Phase objective

Let a user build a complete `.omrt` template visually instead of by hand:
load a reference sheet image, detect and adjust its registration markers,
place the orientation mark, draw and configure the regions a sheet contains
(student ID, question set, question blocks, custom bubble groups), fine-tune
individual bubbles, and save/reload the result - without touching bubble
recognition, batch processing, or any later-phase concern.

## 2. Implementation summary

### `src/omr_scanner/domain/template_authoring.py` (new)

Pure region-generation and designer-validation functions, no Qt, no I/O:
`fit_grid_to_bounds`, `generate_character_grid_zone`, `generate_question_columns`
(one `Zone` per printed column), `generate_ignored_zone`, `translate_zone`,
`resize_zone`, `build_blank_template`, `validate_template_for_designer`,
`DesignerValidationReport`.

### `src/omr_scanner/domain/template.py` (extended)

One additive field: `OmrTemplate.reference_image: str | None`. Does not bump
`format_version`; a document without it loads with `None`.

### `src/omr_scanner/domain/geometry.py` (extended)

`NormalizedRect.overlaps()`, used by the designer's overlap warnings and
generically useful geometry.

### `src/omr_scanner/services/marker_detection_service.py` (new)

The seam between the designer and Phase 1's pixel algorithms:
`DecodedImage` (plain bytes + dimensions), `decode_image_file`,
`detect_registration_markers` (per-corner independent scoring, reusing Phase
1's `detect_marker_candidates`/`score_candidate`/`corner_search_region`
directly), `MarkerSearchConfig`, `DetectedMarker`, `MarkerDetectionOutcome`.

### `src/omr_scanner/gui/template_designer/` (new package)

| Module | Contents |
|---|---|
| `coordinates.py` | `CoordinateMapper`, `snap` - pixel/normalised conversion, no Qt. |
| `history.py` | `SnapshotHistory[T]` - generic undo/redo stack over immutable snapshots. |
| `state.py` | `DesignerState`, `MarkerStatus`, `DetectionMethod` - the template, its history, session-only marker provenance. |
| `items.py` | `RegionHandleItem`, `BubbleDotItem`, `HandleState` - draggable/resizable `QGraphicsItem`s. |
| `canvas.py` | `TemplateCanvasView`, `TemplateCanvasScene`, `RegionSpec`, `BubbleDotSpec` - zoom, pan, grid, rubber-band drawing, signal plumbing. |
| `region_list.py` | `RegionListPanel`, `RegionListEntry` - grouped marker/orientation/region list. |
| `properties_panel.py` | `PropertiesPanel` - numeric X/Y/W/H editor. |
| `dialogs.py` | `StudentIdDialog`, `QuestionSetDialog`, `QuestionBlockDialog`, `CustomBubbleDialog`, `IgnoredRegionDialog`, `NewTemplateDialog`, `ValidationReportDialog`. |
| `page.py` | `TemplateDesignerPage` - assembles everything into the workflow page. |

### `src/omr_scanner/gui/pages/base_page.py` and `catalog.py` (extended)

`WorkflowPage.__init__` gained `expand: bool = False` (default-unchanged) so a
page's body can fill available space. `WorkflowPageSpec` gained an explicit
`implemented` field; the Template stage sets it `True` while keeping
`phase=2` for the placeholder-message history.

### `src/omr_scanner/gui/main_window.py` (extended)

Wires `TemplateDesignerPage` in place of the placeholder for the "template"
key, and updates the About text.

### Not built, on purpose

Snap-to-grid interaction, align/distribute tools, per-bubble override reset
(only per-region), and anything from §36 of the brief (recognition, grading,
batch processing, database-backed results, ML, camera perspective correction,
auth/server infrastructure).

---

## 3. Public API

```python
from omr_scanner.domain.template_authoring import (
    build_blank_template, generate_character_grid_zone, generate_question_columns,
    validate_template_for_designer,
)
from omr_scanner.services import (
    decode_image_file, detect_registration_markers, MarkerSearchConfig,
)
from omr_scanner.gui.template_designer.state import DesignerState
from omr_scanner.gui.template_designer.page import TemplateDesignerPage

# Programmatic use (what the GUI itself does under the hood):
template = build_blank_template(name="Sheet", canonical_width_px=1240, canonical_height_px=1754)
state = DesignerState(template, reference_image_path=Path("sheet.png"))
zone = generate_character_grid_zone(
    zone_id="sid", label="Student ID", field_type=FieldType.NUMERIC,
    symbols=tuple("0123456789"), character_count=7,
    bounds=NormalizedRect(x=0.1, y=0.1, width=0.3, height=0.4),
    bubble_size=NormalizedSize(width=0.02, height=0.015),
)
state.add_zones((zone,))
report = validate_template_for_designer(state.template)
```

The GUI entry point is simply navigating to the "Template" stage in
`MainWindow`; there is no separate launch command.

---

## 4. Algorithm / data flow

```text
File > New from Image
  -> services.marker_detection_service.decode_image_file   (plain bytes)
  -> canvas builds a QImage/QPixmap directly from those bytes
  -> domain.template_authoring.build_blank_template          (placeholder markers)

Detect Markers
  -> services.marker_detection_service.detect_registration_markers
       (Phase 1's preprocessing + marker_detection, scored per corner
        independently - never Phase 1's stricter all-or-nothing assignment)
  -> DesignerState.set_marker(...) per found corner, status = auto/unconfirmed

Draw a region (rubber-band drag) -> a dialog collects parameters
  -> domain.template_authoring.generate_*                    (one or more Zones)
  -> DesignerState.add_zones(...)

Drag/resize/move on the canvas, or edit the properties panel
  -> DesignerState.move_zone / resize_zone / set_marker / set_orientation_marker
  -> each pushes exactly one OmrTemplate.model_copy(...) onto SnapshotHistory

Save
  -> domain.template_authoring.validate_template_for_designer (advisory)
  -> OmrTemplate.model_copy(update={"reference_image": <relative path>, "modified_at": now})
  -> services.template_service.save_template                 (unchanged since Phase 0)
```

Three decisions worth carrying forward (full reasoning in
`docs/phase_02_plan.md` and the implementation notes):

- **One document, no parallel model.** Every edit is
  `template.model_copy(update=...)`; undo/redo is a stack of the resulting
  `OmrTemplate`s. Save always writes the exact object being edited.
- **Detection is per-corner independent**, not Phase 1's exhaustive
  one-to-one assignment - correct here because a human is always present to
  confirm or override, unlike Phase 1's unattended alignment.
- **Detection provenance is session state, never persisted.** The domain model
  stores geometry; whether it came from detection or a drag is designer-only
  bookkeeping.

---

## 5. Configuration

Nothing user-facing to configure beyond what the dialogs collect per region.
Developer-level tuning:

| Value | Default | Where |
|---|---|---|
| `MarkerSearchConfig.expected_marker_width/height` | 0.03 / 0.021 | `services/marker_detection_service.py` |
| `MarkerSearchConfig.corner_search_fraction` | 0.32 | same |
| `MarkerSearchConfig.min_candidate_score` | 0.45 | same |
| `SnapshotHistory.max_depth` | 100 | `gui/template_designer/history.py` |
| `DEFAULT_GRID_SIZE_NORMALIZED` | 0.05 | `gui/template_designer/page.py` |

---

## 6. Tests

146 new tests; 729 in the repository, all passing.

| File | Tests | Covers |
|---|---:|---|
| `unit/test_template_authoring.py` | 43 | Grid fitting, all four region generators, translate/resize, `build_blank_template`, designer validation (overlaps, duplicate/missing question numbers), the `reference_image` field round trip and backward compatibility. |
| `unit/test_template_designer_coordinates.py` | 15 | `CoordinateMapper` construction, conversions and their inverses, `snap`. |
| `unit/test_template_designer_history.py` | 15 | Push/undo/redo, redo-future discarding on a new push, no-op pushes, max-depth eviction, reset. |
| `unit/test_template_designer_state.py` | 23 | Every `DesignerState` mutation method, dirty tracking, bubble overrides, marker/orientation status, `load()`. |
| `integration/test_marker_detection_service.py` | 15 | Image decoding to plain bytes and back, detection on clean/degraded/blank synthetic sheets, per-corner independence on missing markers, a custom search config. |
| `gui/test_template_designer_page.py` | 34 | Construction, opening a reference image, marker detection and confirmation, region CRUD, geometry editing (canvas drag, numeric panel), undo/redo, fine-tune bubble mode, validation, save/reload including a moved reference image, status display. |
| `gui/test_main_window.py` | +1/updated | The Template page is confirmed no longer a placeholder; the "still a placeholder" smoke test now points at Scan. |

All GUI tests run under `QT_QPA_PLATFORM=offscreen`; modal dialogs are never
exercised directly (per `docs/TESTING.md`'s existing GUI policy) - an autouse
fixture stubs `QMessageBox` so a headless run cannot hang on an unclickable
modal, mirroring the pattern already used in `test_main_window.py`.

---

## 7. Quantitative results

Not applicable in the Phase 1 sense (there is no accuracy number for a UI),
but the two required, verifiable outputs:

- **474 bubbles** generated for the example template (70 student ID + 4
  question set + 400 questions), matching exactly across generation, the
  designer's live state, the saved file, and an independent reload - asserted
  in `TestSaveAndReload` and in the scripted demo.
- **12/12 demonstration steps completed** with real, logged output (not
  narrated) - see `docs/testing/phase_02_demo.md`.

---

## 8. Quality checks

```text
pytest         729 passed in 12.9s
ruff check .   All checks passed!
mypy           Success: no issues found in 58 source files   (strict mode)
```

Baseline before Phase 2: 583 passed, ruff clean, mypy clean on 46 files.
**No pre-existing failures**, and Phase 2 introduced none.

### New tool configuration

One: the `pep8-naming` Qt-override allowlist in `pyproject.toml` was extended
to cover the additional Qt event-handler names the canvas and graphics items
implement (`mousePressEvent`, `mouseMoveEvent`, `mouseReleaseEvent`,
`hoverMoveEvent`, `wheelEvent`, `keyReleaseEvent`, `drawBackground`, and a few
others declared for future use). This is a naming-convention allowance, not a
suppressed check - every one of those methods is still fully type-checked.

No new mypy overrides, no new Ruff ignores, no `# type: ignore` outside two
narrow, commented cases (`_apply_geometry`'s marker/orientation branches and
one Qt-stub optional-return case already present in Phase 1's style).

---

## 9. Manual smoke test

Documented as a checklist in `docs/testing/phase_02_manual_test.md`, to be run
against the real windowed application (not the offscreen platform this
development environment used - see below).

What substitutes for it in this handoff: the full 12-step functional
demonstration required by the phase brief was executed against the actual
`TemplateDesignerPage` code path (every step calls the same methods the
toolbar/canvas/dialogs call) and its real, logged output is reproduced
verbatim in `docs/testing/phase_02_demo.md` - new template from a synthetic
image, marker detection and confirmation, student ID / question set / 100-question
region creation, a region move, an individual bubble drag, validation, save,
reload in a second independent page instance, and a full equality check
against the file on disk.

**Environment caveat**: this development environment's Qt platform plugin
(`offscreen`) renders every text label as a placeholder box rather than a
glyph. Screenshots were attempted, confirmed structurally correct (every
widget in the right place, size and colour), and then discarded rather than
shipped, because a screenshot full of "□□□□□□□□" would misrepresent the real,
windowed application. See
`docs/development/phase_02_implementation_notes.md` for detail. **The manual
checklist has not been run against a real windowed session and should be
before the designer is trusted for a real sheet.**

---

## 10. Known limitations

1. **No real printed sheet has been used with the designer.** Every check in
   this phase - automated and the scripted demonstration - used a synthetic
   reference image from `omr_scanner.imaging.synthetic`. This is the same
   category of open risk Phase 1 recorded for the alignment engine, now also
   true of the tool used to build templates for it.
2. **No snap-to-grid while dragging.** `coordinates.snap()` exists and is
   unit-tested; it is not called from `items.py`'s drag/resize handlers yet.
3. **No align/distribute tools** for multiple selected regions.
4. **Individual-bubble override reset is per-region**, not per-bubble (no
   context menu on a single `BubbleDotItem` yet).
5. **No documentation screenshots** - see the environment caveat above.
6. **Marker `shape` is not designer-aware.** Only `filled_square` is
   exercised, inheriting Phase 1's own scope boundary rather than exceeding
   it.
7. Manual mouse-drag and keyboard-shortcut behaviour is verified by driving
   the same Qt signals a real gesture produces, not by an actual OS-level
   mouse/keyboard event in this environment (see §9). This is the standard
   limitation of GUI smoke testing already documented in `docs/TESTING.md`,
   not new to Phase 2.

---

## 11. Deferred technical debt

| Deferred | Why | When |
|---|---|---|
| Snap-to-grid wiring | The pure function is ready; wiring it into interactive dragging is best validated against real sheet usage first, so the snap granularity is chosen from experience rather than a guess. | As soon as a real sheet is available, or on user request |
| Align/distribute tools | Needs a real decision about which selected item is the alignment reference once multi-select is used in practice. | After single-select workflows have seen real use |
| Per-bubble override reset | "Reset Bubbles in Region" covers the common case; a per-dot context menu is a small, isolated addition to `BubbleDotItem`. | Low priority, on request |
| Anonymised real-scan validation of the designer | No anonymised sample sheet exists yet - the same open item Phase 1 recorded, now doubled: both the alignment engine and the designer need it. | As soon as a sheet can be obtained and anonymised |
| Documentation screenshots | The offscreen Qt platform in this environment cannot render text; capturing them needs a real windowed session. | Whenever this is next run interactively on a desktop |

---

## 12. Phase 3 entry criteria

**Phase 3 (Bubble Mapping & Recognition Engine) can begin safely.** The
template a recognition engine needs to consume is fully described by the
existing `.omrt` format (unchanged by Phase 2 except the additive
`reference_image` field, which recognition does not need), and can now be
produced without hand-writing JSON.

**Already in place**

- Every recognition-relevant piece of geometry (`Zone`, `BubbleGrid`,
  `FieldDefinition`, `RecognitionSettings`) is exactly what Phase 0 defined;
  Phase 2 only added tooling to produce it, never changed its shape.
- `Zone.field.rows`/`.columns` and `BubbleGrid.bubble_center(row, column)` -
  what Phase 3 needs to locate every bubble - are exercised directly by the
  designer's own bubble-preview and fine-tune code, so they are proven correct
  under real generated templates, not just Phase 0's hand-written example.
- `examples/templates/100_question_4_choice_example.omrt` is a second,
  larger, generator-produced fixture Phase 3's tests can use alongside
  `resources/templates/example_answer_sheet.omrt`.

**Constraints Phase 3 must respect**

1. `omr_scanner.recognition` must not import Qt, matching every other
   non-`gui` layer (already true structurally; Phase 3 must keep it true).
2. Recognition thresholds live in `RecognitionSettings`, inside the template -
   never a new module-level constant.
3. A missing or multiple mark must be represented explicitly, never guessed
   at - the same "fail visibly, never silently" convention Phase 1 established
   for alignment failures.

**Suggested first steps**

1. Render a synthetic sheet with known, marked bubbles (extending
   `omr_scanner.imaging.synthetic`, which currently draws only *unmarked*
   bubbles for visual clutter) as Phase 3's ground truth generator, the same
   way Phase 1's synthetic distortion suite was the foundation of its tests.
2. Implement `imaging.metrics` (per-bubble fill measurement) against a
   template produced by the Phase 2 designer, not a hand-written one, to catch
   any assumption the designer's generators make that a hand-written template
   would not have exercised.
3. Keep the Scan page a placeholder - Phase 3 delivers the recognition engine,
   not the batch workflow (that remains Phase 5).

**Suggested next prompt**

> Begin Phase 3 — Bubble Mapping & Recognition Engine, following
> `development/ROADMAP.md` and the constraints in
> `development/PHASE_02_HANDOFF.md` section 12. Build the synthetic marked-sheet
> generator first, then per-bubble fill measurement, then measurement-to-value
> recognition with explicit confidence and ambiguity handling. Use a template
> produced by `omr_scanner.domain.template_authoring`, not a hand-written one,
> for at least one test fixture. Do not implement the template designer's
> remaining polish items (snap-to-grid, align/distribute) unless asked.

**Definition of done for Phase 3**

Recognition accuracy is a measured number on a synthetic corpus with known
marks (light, heavy, partial, crossed-out, multiple, absent); ambiguity is
never silently resolved; every threshold comes from the template; `pytest`,
`ruff check .` and `mypy` pass; `CURRENT_STATE.md` is updated and
`PHASE_03_HANDOFF.md` is written.

---

## 13. The one thing to do before trusting this

Run `docs/testing/phase_02_manual_test.md` against the real windowed
application on a development machine - every item in it has an automated
equivalent, but none of them has been visually confirmed to *feel* right
(drag responsiveness, cursor behaviour, dialog clarity) in this offscreen
environment. Then, as soon as a printed sheet can be obtained: build a
template for it in the designer, scan the printed sheet, and run
`python -m omr_scanner.tools.align_image` against that template. Neither the
designer nor the alignment engine has ever seen real paper.
