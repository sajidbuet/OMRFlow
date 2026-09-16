# Phase 2 manual test checklist - Template Designer

Run this against the real desktop application (not the offscreen Qt platform
used by the automated test suite - see the note at the end) before relying on
a change to the designer. Every item here also has an automated equivalent in
`tests/gui/test_template_designer_page.py` and `tests/unit/test_template_authoring.py`;
this checklist exists for the interaction feel automated tests cannot judge
(does a drag *feel* right, is a cursor sensible, does a dialog read clearly).

```bash
python -m omr_scanner
```

Navigate to the **Template** stage.

## Application and loading

```text
[ ] Application launches and the Template page shows an empty canvas
[ ] File > New from Image opens a file picker
[ ] Selecting a PNG/JPEG reference image loads it at 1:1 into the canvas
[ ] The four registration markers and orientation mark appear, dashed
    (unconfirmed), at their default positions
[ ] Document controls (Save, Detect Markers, + buttons, Validate) become
    enabled only once a template exists
```

## Marker detection and adjustment

```text
[ ] Detect Markers finds all four corner squares on a clean synthetic sheet
    (use `python -m omr_scanner.tools.make_test_sheet` from Phase 1 to
    generate one, or any sheet matching the example template's layout)
[ ] Detected markers are drawn amber and each shows a confidence-like score
[ ] Confirm Detected Markers turns them green without moving them
[ ] Dragging a marker moves it and turns it green (confirmed) immediately
[ ] Resizing a marker by dragging a corner handle changes only that marker
[ ] Detect Markers on a sheet missing one corner reports that corner as not
    found (a message names it) while leaving the other three detected
[ ] The orientation mark can be dragged into place the same way
```

## Region creation

```text
[ ] + Student ID, after dragging a rectangle, opens a dialog for digit count
    and layout; OK creates one numeric region with the requested bubble count
[ ] + Question Set creates one region with one bubble per entered value
[ ] + Questions with "100 questions, 4 columns of 25" creates FOUR separate
    regions (not one), together holding all 400 bubbles
[ ] + Custom creates a region using the entered symbol list
[ ] + Reference creates a region with no bubbles, for logos/instructions
[ ] Cancelling a region dialog creates nothing
[ ] An invalid dialog input (e.g. only one answer choice) shows an inline
    error instead of closing the dialog
```

## Region editing

```text
[ ] Clicking a region selects it (canvas and region list stay in sync)
[ ] Dragging a region's body moves it; the properties panel updates live
[ ] Dragging an edge/corner resizes it; the bubble count is unchanged
[ ] Typing new X/Y/Width/Height in the properties panel moves/resizes the
    canvas selection to match, on Enter
[ ] Duplicate (region list menu, or Ctrl+D) creates an offset copy
[ ] Rename changes the label, not the id
[ ] Delete (or the Delete key) removes a region
[ ] Delete does nothing when a registration marker or the orientation marker
    is selected
[ ] The region list's checkbox hides/shows a region on the canvas
```

## Individual bubble fine-tuning

```text
[ ] Selecting a bubble region and enabling "Edit Individual Bubbles" shows one
    dot per bubble
[ ] Dragging one dot moves only that bubble and turns it pink (overridden)
[ ] "Reset Bubbles in Region" clears every override in the selected region
[ ] Disabling "Edit Individual Bubbles" hides the dots without discarding
    overrides
```

## View controls

```text
[ ] Mouse wheel zooms in/out, anchored under the cursor
[ ] Zoom In / Zoom Out / Fit / 100% buttons behave as labelled
[ ] Ctrl++ / Ctrl+- / Ctrl+0 match the buttons
[ ] Space + drag pans the canvas
[ ] The status row's Zoom percentage updates on every zoom change
[ ] The status row's Cursor position updates while the mouse is over the image,
    and clears when it leaves
[ ] Grid toggle shows/hides a faint alignment grid
```

## Undo / redo

```text
[ ] Ctrl+Z undoes the most recent completed edit (a whole drag, not a
    per-pixel step)
[ ] Ctrl+Y redoes it
[ ] Undo/Redo buttons are disabled exactly when there is nothing to undo/redo
[ ] Making a new edit after undoing discards the redone-away future (redo
    becomes unavailable)
```

## Validation

```text
[ ] Validate on a template with no regions shows a warning, no error
[ ] Validate on a template with two regions covering the same question number
    shows an error
[ ] Validate on a template with a region overlapping a marker shows a warning
[ ] Saving with validation errors present still succeeds, after a
    confirmation prompt
```

## Save / reload

```text
[ ] Save (with no path yet) behaves as Save As
[ ] The title bar shows the template name, file name, and a trailing "*"
    while there are unsaved changes; the "*" disappears immediately after Save
[ ] Save As lets you pick a new file and switches the open document to it
[ ] Closing the page (or opening another template) with unsaved changes
    prompts Save / Discard / Cancel
[ ] Open reloads a saved template: every region, its exact geometry, and its
    bubble overrides are unchanged after the round trip
[ ] Opening a template whose reference image has been moved/deleted falls
    back to a blank canvas at the template's canonical size, without crashing
```

## Error handling

```text
[ ] Opening a non-image file as a reference image shows a readable error, no
    traceback
[ ] Opening a corrupted or unrelated .omrt file shows a readable error
[ ] Opening a .omrt file from a newer format version is refused with a
    readable message (Phase 0 behaviour, unchanged)
```

## Template Designer correction pass (GUI fix pass)

These checks exist because each one was a reported bug. The root cause of every
one is recorded in `docs/development/template_gui_fix_diagnosis.md`; the
automated equivalents are named beside each block.

Use the repository's own real OMR page for all of them:

```text
examples/ECE-0000.png
```

It is 2480 x 3508, with four magenta corner squares, an 86 x 44 orientation dash
near the top-left, and five printed answer columns of 20 (Q1-100). Never modify
it.

### Question region geometry

The rectangle you drag is the *container*. Nothing you change inside the dialog
may move or resize it.

```text
[ ] Load examples/ECE-0000.png
[ ] Drag a Question Region over the five printed answer columns
[ ] Note the region's outer position and size (the properties panel, or the
    left/right edges against the printed columns)
[ ] The dialog opens showing 4 columns
[ ] Change it to 1 column
[ ] The outer rectangle is visually UNCHANGED - same left edge, same right
    edge, same top, same bottom
[ ] Change it to 5 columns
[ ] The outer rectangle is still unchanged
[ ] Only the internal layout changed: 1 column spreads its bubbles over the
    whole width; 5 columns divides it into five strips
[ ] The same holds when you change questions per column, the choice labels,
    the column gap and the bubble radius
```

Automated: `tests/unit/test_question_region_container.py`,
`tests/gui/test_template_designer_bubble_and_layout.py`.

### Bubble radius

```text
[ ] The toolbar's second row shows "Bubble radius" in reference-image pixels
[ ] Set it to 5 px - the preview bubbles become small dots
[ ] Set it to 10 px - they become visibly twice the diameter
[ ] The bubble CENTRES do not move; only the circles grow around them
[ ] The parent region does not move or resize
[ ] Zoom to 50%, 100% and 200% - the radius reading and the geometry are
    unchanged; only the rendering scales
[ ] Select a region: the properties panel shows its own radius and a
    "Use template bubble size" checkbox, ticked
[ ] Clear the checkbox, set that region's radius to something distinct
[ ] Change the toolbar radius again - the overridden region keeps its own size
    and every other region follows the new default
[ ] Ctrl+Z undoes a radius change in ONE step, however many regions it touched
```

Automated: `tests/gui/test_template_designer_bubble_and_layout.py::TestBubbleRadius`,
`tests/unit/test_question_region_container.py::TestSetZoneBubbleSize`.

### Region resize

Place the region well away from the top-left corner first; a region at the
origin cannot show this bug.

```text
[ ] Select a region away from (0, 0)
[ ] Note its X and Y in the properties panel
[ ] Drag the bottom-right handle
[ ] X and Y are UNCHANGED; width and height changed
[ ] The region does not jump towards the top-left corner
[ ] Drag the right edge only: X, Y and Height unchanged, Width changed
[ ] Drag the bottom edge only: X, Y and Width unchanged, Height changed
[ ] Drag the top-left handle: the position changes by exactly the drag, and
    the bottom-right corner stays put
[ ] Type a new Width in the properties panel: X, Y, Height unchanged
[ ] Type a new Height: X, Y, Width unchanged
[ ] Ctrl+Z restores the previous geometry in one step, on screen and in the
    panel
```

Automated: `tests/gui/test_template_designer_region_geometry.py`.

### Orientation marker

```text
[ ] Drag the ORIENT rectangle so it contains the printed dash near the sheet's
    top-left corner (it need not be tight or centred)
[ ] Press Orientation on the toolbar's first row
[ ] The mark is found; the status row reports its position and a score
[ ] The ORIENT overlay now sits exactly on the printed dash
[ ] The properties panel reports roughly x=157, y=308, w=86, h=44
[ ] A mark wholly inside the rectangle is never rejected for being inside it
[ ] Move the rectangle over blank paper and press Orientation: it reports that
    nothing was found, with a reason, and changes nothing
[ ] Move it over a corner registration square alone: also not found (the
    squares are 1:1; the orientation mark is not)
```

To see what the detector saw, set `OMRFLOW_ORIENTATION_DEBUG_DIR` to a
directory before launching; each attempt writes
`orientation_detection_latest.png` there with the search region, every
candidate and each rejection reason.

Automated: `tests/integration/test_orientation_marker_detection.py`.

### Page layout

```text
[ ] The sentence "Design and calibrate the OMR sheet template." no longer
    occupies a full-width row (it is the Template title's tooltip instead)
[ ] The toolbar is two compact rows: file/undo/detection/validation above,
    region tools/bubble radius/zoom/grid below
[ ] More vertical space is available for the canvas than before
[ ] Narrow the window to about 1000 px: controls overflow into Qt's own "»"
    menu rather than overlapping or clipping their labels
[ ] Widen it again: everything returns
[ ] Repeat at Windows display scaling 100%, 125% and 150% - icons stay
    visible, labels stay legible, nothing overlaps
```

Automated: `tests/gui/test_template_designer_toolbar.py`,
`tests/gui/test_template_designer_bubble_and_layout.py::TestPageHeaderIsCompact`.

### Canvas panning on the real sheet

```text
[ ] Zoom to about 200%
[ ] Middle-button drag pans the viewport smoothly
[ ] Right-button drag pans the viewport
[ ] A plain right-click (no movement) does not pan
[ ] No region is selected or moved by either
[ ] Left-drag still moves the selected region
[ ] Region geometry in the properties panel is identical before and after
```

Automated: `tests/gui/test_template_designer_canvas_panning.py`,
`tests/gui/test_template_designer_bubble_and_layout.py::TestPanningDoesNotAlterTheDocument`.

### Real question-column layout

```text
[ ] Draw a Question Region over the sample's answer area
[ ] Configure Q1-100, choices a,b,c,d, 5 columns of 20
[ ] Five regions appear, labelled Questions 1-20 ... 81-100
[ ] Together they hold 400 bubbles
[ ] Adjust the column gap and watch the columns move while the outer
    rectangle stays put
[ ] Drag one column sideways; the other four do not move
[ ] Save, reload, and confirm every offset came back
```

Automated: `tests/unit/test_template_authoring.py`,
`tests/integration/test_template_service.py`.

### Screenshots and geometry dumps

Rather than reading these by eye alone, generate the evidence:

```bash
python .claude/skills/qtguitesting/scripts/run_gui_smoke_tests.py
python .claude/skills/qtguitesting/scripts/capture_gui_states.py
python .claude/skills/qtguitesting/scripts/dump_gui_geometry.py --scenario columns
python .claude/skills/qtguitesting/scripts/dump_gui_geometry.py --scenario resize
```

Screenshots land in `test-output/gui/`; the geometry dumps check the container
and resize-anchor invariants for you and exit non-zero if either is violated.

---

## A note on how this checklist was actually exercised for Phase 2

The development environment used for this phase runs Qt through the
`offscreen` platform plugin, which - on this machine - renders every label as
a placeholder box rather than real text (a font-backend limitation of that
plugin, not of the application; see
`development/phase_02_implementation_notes.md`). Every behavioural item above
was therefore verified by driving the page's real methods and signals
end-to-end (see `tests/gui/test_template_designer_page.py`, and the worked
walkthrough in `docs/testing/phase_02_demo.md`) rather than by visually
reading an offscreen screenshot. **This checklist should be run against the
real windowed application on a development machine before the designer is
trusted for a real sheet** - nothing in this phase constitutes that visual
sign-off.
