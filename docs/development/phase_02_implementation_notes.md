# Phase 2 implementation notes

Written for whoever picks this up next - a human or another agent - since the
conversational context that produced these decisions will not carry forward.
See also `docs/phase_02_plan.md` (written before implementation, the
interpretation of the existing repository) and
`development/PHASE_02_HANDOFF.md` (the formal phase closeout).

## Architectural decisions made during implementation

### The document is `OmrTemplate`, edited by `model_copy`, undone by a snapshot stack

Covered in full in `docs/phase_02_plan.md` §4. The one thing worth adding here:
this turned out to make several other design questions trivial. "Does Save
write exactly what's on screen?" - yes, by construction, there is no second
representation to fall out of sync. "Does Undo correctly restore bubble
overrides / marker positions / zone order?" - yes, the same way saving does,
because it is the same object.

### Detection is per-corner independent, not Phase 1's exhaustive assignment

`omr_scanner.imaging.marker_detection.select_corner_markers` requires a
mathematically valid one-to-one assignment of candidates to all four corners
and raises when one is missing - correct for Phase 1, which has no human to
ask. The designer always has a human present, so
`marker_detection_service.detect_registration_markers` reimplements only the
per-corner *scoring* half (reusing `score_candidate`,
`detect_marker_candidates`, `corner_search_region` verbatim) and reports each
corner's best candidate independently, never raising for a missing one. This
is the one place Phase 2 does not simply call a Phase 1 function directly -
documented at length in the module's own docstring so the reason survives
without this file.

### Marker/orientation provenance (auto/manual/confirmed/confidence) is not persisted

Discussed in `docs/phase_02_plan.md` §2. In hindsight this was the right call:
persisting it would have meant either bumping the format version for no
scanner-relevant reason, or inventing a "designer metadata" side-channel in the
document that Phase 3+ would have had to learn to ignore.

### One additive field: `OmrTemplate.reference_image`

The only change to Phase 0's domain model. Deliberately minimal (a relative
path string, `None` by default) and tested for backward compatibility
(`tests/unit/test_template_authoring.py::TestReferenceImageField`) - a
document written before Phase 2 loads unchanged.

### `WorkflowPage` gained an `expand` flag

`base_page.py`'s original layout gives `body` its size-hint and puts a
stretch spacer below it - correct for every Phase 0 page (a short column of
labels and buttons) and wrong for a full-size canvas editor, which would
render squeezed into a sliver at the top with empty space below. Added
`expand: bool = False` to `WorkflowPage.__init__`; default behaviour for every
existing page is untouched (verified: no existing GUI test needed a change
because of this). This was the one Phase 0/1 file touched for a reason other
than adding a new page.

### Keyboard shortcuts: Ctrl+Shift+N/O instead of Ctrl+N/O

`main_window.py` already binds Ctrl+N/Ctrl+O to project actions with Qt's
default window-wide shortcut context. Binding the same sequences to template
actions on the page would make Qt refuse *both* ("ambiguous shortcut") the
moment the Template page is visible - discovered by reading Qt's shortcut
dispatch rules, not by hitting the bug at runtime, and avoided rather than
worked around.

## Problems encountered and how they were resolved

### Circular import: `gui.pages.__init__` <-> `gui.template_designer.page`

`TemplateDesignerPage` imports `omr_scanner.gui.pages.base_page` and
`.catalog` (submodules). Re-exporting `TemplateDesignerPage` from
`gui/pages/__init__.py` - the natural place, alongside `ProjectPage` - made
importing any submodule of `gui.pages` first run `gui/pages/__init__.py`,
which then imported `gui.template_designer.page`, which imported back into a
`gui.pages` package still mid-import. Resolved by *not* re-exporting it there;
`main_window.py` imports it directly from `omr_scanner.gui.template_designer.page`
instead. Documented in `gui/pages/__init__.py`'s own docstring so nobody
"fixes" this by re-adding the export.

### Screenshots could not be captured meaningfully in this environment

The Phase 2 brief asks for `docs/images/template_designer_main.png` and
similar. Screenshots were captured (`QWidget.grab()` under
`QT_QPA_PLATFORM=offscreen`) and did show every widget in its correct
position, size and colour - but every piece of *text* rendered as a small
placeholder box rather than a glyph, a font-backend limitation of Qt's
offscreen platform plugin in this sandboxed environment, not a bug in the
application. Shipping a screenshot where every label reads as "□□□□□□□□"
would misrepresent what the real, windowed application looks like, so none
were kept. `docs/testing/phase_02_manual_test.md` documents running the real
checklist on a real desktop session instead, and
`docs/testing/phase_02_demo.md` records the twelve-step functional demo as
executed output rather than a picture.

### A test asserted a canvas state that only a real mouse gesture sets

One GUI test called `page._on_region_drawn(...)` directly (bypassing an actual
mouse drag) and then asserted `canvas.is_drawing is False` - but that flag is
only cleared by `canvas.mouseReleaseEvent`, which the direct call skips.
Fixed by calling `canvas.cancel_draw_mode()` explicitly in the test, with a
comment explaining why: the test was wrong, not the code under test - `git
blame`/this note exists so a future reader doesn't "fix" the production code
to make the shortcut-driven test pass.

### QMessageBox hangs the test suite under the offscreen platform

Several page methods show a `QMessageBox` (a missing-marker notice, the
unsaved-changes prompt). Under `QT_QPA_PLATFORM=offscreen` there is no user to
click it, so `exec()` blocks forever rather than raising or timing out. Fixed
with an autouse `monkeypatch` fixture in
`tests/gui/test_template_designer_page.py` that stubs `QMessageBox.information`
and `.warning` for every test in that file, mirroring the existing
`silent_message_boxes` pattern in `tests/gui/test_main_window.py`.

## Deliberately deferred (not overlooked)

| Deferred | Why | Where the seam is |
|---|---|---|
| Snap-to-grid while dragging | `coordinates.snap()` exists and is unit-tested; wiring it into `RegionHandleItem`'s drag/resize math is a small, isolated change once the interaction feel is validated against a real sheet. | `items.py: RegionHandleItem.mouseMoveEvent` / `_apply_resize` |
| Align left/right/top/bottom, distribute | Needs a real multi-select UX decision (which item is the reference?) that is easier to get right after the single-select flow has been used for real work. | Would live in `page.py`, operating on `canvas._scene.selectedItems()` |
| Per-bubble override reset (vs. per-region) | "Reset Bubbles in Region" covers the common case; a right-click-one-dot reset needs a context menu on `BubbleDotItem`, deferred for time, not difficulty. | `items.py: BubbleDotItem` |
| Marker `shape` (circle/rectangle) aware detection UI | Phase 1 itself only exercises `filled_square`; the designer inherits that scope boundary rather than exceeding it. | `marker_detection_service.py` |
| Real-scan validation of the whole designer | No anonymised sample sheet exists yet - the same open item Phase 1 recorded. | N/A - needs an actual scan |

## APIs introduced for later phases

- `omr_scanner.domain.template_authoring` is a stable, Qt-free surface for
  *generating* and *validating* zones - Phase 4 (template calibration) can
  reuse `validate_template_for_designer` directly rather than reinventing
  template sanity checks, and any future headless/CLI template tool can reuse
  the generator functions without any GUI dependency.
- `omr_scanner.services.marker_detection_service.DecodedImage` is a
  general-purpose "image as plain bytes for Qt" contract; any future GUI
  feature that needs to display a scan (Phase 4's calibration preview, Phase 6's
  conflict review) can reuse it instead of inventing a second image-to-QImage
  path.
- `gui.template_designer.history.SnapshotHistory` is a generic undo/redo stack
  over any immutable snapshot type, not template-specific; it is written and
  tested generically for exactly this reason.
