# The template designer

Phase 2 delivers the interactive editor for `.omrt` documents: the "Template"
stage of the main window, previously a placeholder. This document describes
what it does and how it is put together; `docs/phase_02_plan.md` records the
design decisions behind it, and `docs/TEMPLATE_FORMAT.md` is the authoritative
spec of the document it produces.

## What it is for

Loading a reference sheet image, locating its four registration markers,
placing the orientation mark, and drawing the regions (student ID, question
set, question blocks, custom bubble groups) that describe every bubble the
sheet contains - then saving the result as a `.omrt` template Phase 1's
alignment engine and a future Phase 3 recognition engine can both consume.

It does **not** recognise marks, score anything, or process a batch of scans.
Those remain Phase 3 and Phase 5.

## Opening it

Launch the application and select **Template** in the workflow navigation.
Unlike the other placeholder stages, it does not require an open project - it
can create and save a template standalone, though when a project is open its
templates folder (`<project>/templates/`) is offered as the default save
location.

## Layout

```text
+--------------------------------------------------------------+
| [New][Open][Save][Save As] | [Undo][Redo] | Detect | Confirm  |
| Student ID Set Questions Custom Reference | Edit Bubbles Reset|
| Validate | [Zoom-][Zoom+][Fit][Actual Size][Grid]              |
+----------------------+---------------------------------------+
|                      |                                       |
| Registration markers |                                       |
|  TL TR BR BL         |                                       |
| Orientation           |          CANVAS                      |
| Regions               |   (reference image + overlays)       |
|  Student ID           |                                       |
|  Question set         |                                       |
|  Questions 1-25 ...   |                                       |
+----------------------+---------------------------------------+
|                                                | Geometry     |
|                                                | X / Y / W / H|
|                                                | Normalised   |
+--------------------------------------------------------------+
| Title (unsaved *) | Zoom | Image size | Cursor | State        |
+--------------------------------------------------------------+
```

Three panels, left to right: the region list, the canvas, the numeric
properties panel - matching the brief's layout with the properties panel
docked to the canvas's right rather than spanning the full width at the
bottom, so it stays visible regardless of window height.

## Workflow

### 1. Start a template

**New from Image...** opens a file picker for a reference sheet (PNG, JPEG,
TIFF) and asks for a template name and physical page size (defaults to A4).
The canonical page size is taken directly from the image's own pixel
dimensions - the designer works on the reference image at its native
resolution, matching Phase 1's convention that a template's canonical size is
simply "the working resolution", not a privileged constant.

A brand new template starts with four registration markers and an orientation
mark at reasonable default positions (see `build_blank_template` in
`omr_scanner.domain.template_authoring`), drawn with a dashed outline meaning
*unconfirmed* - `.omrt` requires all four markers to exist, so a "not decided
yet" state is expressed as designer-only status, never by omitting them from
the document.

### 2. Locate the registration markers

**Detect** runs Phase 1's own contour-based detector
(`omr_scanner.imaging.marker_detection`, reached through
`omr_scanner.services.marker_detection_service`) against the reference image,
independently for each corner. A found marker is drawn in amber
(auto-detected, unconfirmed); a corner nothing was found for stays dashed red,
with a dialog naming which corner(s) need manual placement.

Detection deliberately does **not** pick the four largest dark shapes on the
page: each corner scores candidates by shape (area, aspect ratio,
rectangularity, solidity, interior ink) and position, and a hollow answer box
or a hand-drawn tick scores far below a solid printed square.

**Drag any marker** to correct it by hand - dragging automatically confirms it
(green). **Confirm** accepts every amber marker in place, without moving it,
once you have eyeballed the overlay against the page.

### 3. Place the orientation mark

The orientation dash (orange) works exactly like a registration marker for
editing purposes: drag it into place over the printed mark, or leave it at its
default position and adjust numerically in the properties panel. Phase 2 does
not run automatic orientation detection (that is Phase 1's job, at scan time,
against a *finished* template); here it is placed by hand.

### 4. Add regions

Click **Student ID**, **Set**, **Questions** or **Custom**, then drag out a
rectangle on the canvas (the cursor becomes a crosshair). A dialog collects
the specifics:

| Button | Dialog | Produces |
|---|---|---|
| Student ID | digit count, layout | one `numeric` zone |
| Set | set values (comma separated), layout | one `set_code` zone |
| Questions | first question, count, choice labels, columns, questions per column, layout | **one `question_block` zone per column** |
| Custom | symbols, position count, layout | one `alphanumeric` zone |
| Reference | name only | one `ignored` zone (logos, printed instructions) |

A 100-question, 4-column, 25-per-column block produces four separate regions
(`questions_0` .. `questions_3`), each a valid, independently-editable zone -
never one rectangle standing in for 400 bubbles. This mirrors what
`docs/TEMPLATE_FORMAT.md` already specified for question blocks before Phase 2
began.

The generated bubble grid is fitted evenly inside the rectangle you drew (see
`fit_grid_to_bounds`): the first and last bubble sit half a bubble-width in
from the edges, and every bubble in between is spaced evenly.

### 5. Adjust regions

Click a region to select it (on the canvas or in the region list) - handles
appear at its edges and corners. Drag the body to move it; drag an edge or
corner to resize it (the bubble grid is refitted to the new size, keeping the
same row/column count). The properties panel shows exact X/Y/Width/Height in
both image pixels and normalised fractions, editable directly; typing a value
and pressing Enter applies it immediately, and the canvas updates to match.

**Duplicate** (region list context menu, or Ctrl+D) copies a region with a
small offset. **Rename** changes its label without touching its `id` (so any
data already keyed to that id is unaffected). **Delete** (or the Delete key)
removes it - not available for registration or orientation markers, which are
never optional in the format; move them instead.

The region list's checkbox toggles a region's visibility on the canvas without
deleting it, useful for decluttering a busy sheet while working on one area.

### 6. Fine-tune individual bubbles

Select a region and toggle **Edit Bubbles**: every bubble in that region
appears as a small draggable dot (blue normally, pink once moved). Dragging
one records an explicit `BubbleOverride` for that cell - the existing
mechanism `docs/TEMPLATE_FORMAT.md` already specifies for "genuinely irregular
sheets" - so a template can describe printing that departs from a perfectly
even grid without abandoning the grid for the rest of the region. **Reset**
clears every override in the selected region, reverting it to the computed
grid.

Individual editing is opt-in per region and only shows dots for the one
selected region, so a 400-bubble question block never floods the canvas with
handles unless you specifically ask to fine-tune it.

### 7. Validate

**Validate** runs `omr_scanner.domain.template_authoring.validate_template_for_designer`,
which checks what the document schema *cannot* reject by construction:

- **Errors** (template should not be relied on): a question number defined by
  two different regions.
- **Warnings** (unusual, possibly intentional): no regions defined yet, a
  region overlapping a registration or orientation marker, two regions
  overlapping each other, a gap in question numbering.

Saving with errors present is still permitted - after a confirmation - because
a half-finished template is a normal thing to save and resume later.

### 8. Save and reload

**Save** / **Save As** write the document through
`omr_scanner.services.template_service.save_template`, exactly as Phase 0
defined it, plus the reference image's path recorded relative to the saved
file (see `docs/TEMPLATE_FORMAT.md`, `reference_image`). The title bar shows
the template name, the file name (or "unsaved"), and a trailing `*` while there
are unsaved changes. Closing or opening another template with unsaved changes
prompts to save, discard, or cancel.

**Open...** reloads a `.omrt` file; if its recorded reference image is found
next to it, the canvas shows it exactly as before. If the image cannot be
found (moved, deleted, or the template was written by hand with no image at
all), the canvas falls back to a plain white page at the template's own
canonical size - regions remain fully editable, just without the printed sheet
underneath them.

## Undo and redo

Ctrl+Z / Ctrl+Y (or the Undo/Redo toolbar actions) step through every completed edit -
one entry per finished gesture (a drag, a resize, a dialog's OK, a delete),
never per intermediate mouse-move. See `docs/phase_02_plan.md` §4 for why this
is implemented as a snapshot stack over the immutable `OmrTemplate` rather than
a command-object hierarchy: it makes undo/redo automatically consistent with
whatever the document happens to contain, with no separate `undo()` method to
keep in sync as the schema evolves.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Ctrl+Shift+N | New template from image |
| Ctrl+Shift+O | Open template |
| Ctrl+S | Save |
| Ctrl+Shift+S | Save As |
| Ctrl+Z / Ctrl+Y | Undo / Redo |
| Delete | Delete selected region |
| Ctrl+D | Duplicate selected region |
| Ctrl++ / Ctrl+- | Zoom in / out |
| Ctrl+0 | Fit to window |
| Mouse wheel | Zoom |
| Space + drag | Pan |

**New/Open use Ctrl+Shift+N/O, not Ctrl+N/O.** The main window already binds
Ctrl+N/Ctrl+O to *project* actions with a window-wide shortcut context; reusing
them here for *template* actions would make Qt refuse both (an "ambiguous
shortcut", where neither fires) whenever the Template page is visible. Every
other shortcut in the Phase 2 brief was free at the window level and is bound
exactly as specified.

## Grid and alignment

The **Grid** toggle overlays a faint reference grid on the canvas (spacing is
a fraction of the image's shorter side); it is visual only in the current
build and does not yet snap dragged geometry to it (`coordinates.snap` exists
and is unit-tested, but the canvas does not call it yet - see the deferred
items in `development/phase_02_implementation_notes.md`).

## Architecture

```text
omr_scanner/gui/template_designer/
├── coordinates.py     Pure pixel <-> normalised conversion (no Qt)
├── history.py         Undo/redo snapshot stack (no Qt)
├── state.py            DesignerState: the template + its edit history +
│                        session-only marker provenance (no Qt)
├── items.py            QGraphicsItem subclasses: draggable/resizable regions,
│                        individual bubble dots
├── canvas.py            QGraphicsView/Scene: zoom, pan, grid, rubber-band
│                        region drawing, signal plumbing
├── region_list.py       Grouped list of markers/orientation/regions
├── properties_panel.py  Numeric X/Y/W/H editor for the current selection
├── dialogs.py            One dialog per region kind + New Template +
│                          validation report
└── page.py               Assembles everything into the workflow page;
                           the only place a user action becomes a
                           `DesignerState` mutation
```

Three rules the layering above exists to enforce:

1. **No `cv2`/`numpy` import anywhere under `gui`** - marker detection and
   image decoding happen in
   `omr_scanner.services.marker_detection_service`, which hands the GUI plain
   `bytes`/`int`/`float` (a `DecodedImage` dataclass), never a NumPy array.
   Enforced by `tests/unit/test_architecture.py`.
2. **`OmrTemplate` is the only document.** No parallel mutable model exists;
   every edit is `template.model_copy(update=...)`, which is what makes
   round-trip equality with the saved file automatic rather than something a
   test has to hope for.
3. **Detection provenance is session state, never persisted.** Whether a
   marker was auto-detected, confirmed, or hand-placed matters while editing
   and means nothing to a scanner reading the saved geometry - see
   `docs/phase_02_plan.md` §2.

## Known limitations

- No snap-to-grid yet (see *Grid and alignment* above).
- Individual-bubble reset is per-region (the **Reset** action), not
  per-bubble; clearing one specific override means dragging it back or
  clearing the whole region and re-adjusting the others.
- No automatic layout tools (align left/right/top/bottom, distribute) - out of
  scope for Phase 2, deferred alongside the rest of the brief's optional
  polish (`development/phase_02_implementation_notes.md`).
- The designer has not been used against a real printed sheet; every
  screenshot attempt and functional check in this phase used a synthetic
  reference image from `omr_scanner.imaging.synthetic`.
