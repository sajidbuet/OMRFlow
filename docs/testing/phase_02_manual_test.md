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
