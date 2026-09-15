# UI polish manual test — Template Designer toolbar

Run this against the real desktop application (a genuine windowed session,
not the `offscreen` Qt platform automated tests use) after any change to
`template_designer/page.py`'s toolbar, `gui/icons.py` or `gui/theme.py`.
Every item here also has an automated equivalent in
`tests/gui/test_template_designer_toolbar.py` and `tests/gui/test_icons.py`;
this checklist exists for what an automated test cannot judge - does a label
actually *look* clipped, is a colour actually legible to a human eye.

```powershell
.\.venv\Scripts\python.exe -m omr_scanner
```

Navigate to **2. Template**.

## Toolbar readability - the original complaint

```text
[ ] No toolbar label is clipped, cut off, or shows only partial text, at the
    window's default size
[ ] New / Open / Save / Save As show clear, distinct icons (no text - hover
    each and confirm the tooltip names it)
[ ] Undo / Redo show clear icons
[ ] Detect / Confirm show an icon plus their full text ("Detect", "Confirm")
[ ] Student ID / Set / Questions / Custom / Reference each show an icon plus
    their full text, and are visually distinguishable from one another
[ ] Edit Bubbles / Reset show an icon plus their full text
[ ] Validate shows an icon plus its full text
[ ] Zoom Out / Zoom In / Fit to Window / Actual Size / Grid show clear,
    distinguishable icons at the right-hand end of the toolbar
```

## Icon and colour quality

```text
[ ] Every icon is a crisp vector line drawing (not a blurry raster image),
    including after changing Windows display scaling (see below)
[ ] No icon is an emoji
[ ] Icons are a consistent size and visual weight across the whole toolbar
```

## Disabled-state readability

With no template open (Save, Detect, Student ID, etc. are disabled):

```text
[ ] Disabled toolbar buttons are clearly greyed out
[ ] Disabled buttons' icons and any text are still readable, not
    "nearly invisible" against the toolbar background
[ ] New and Open remain fully enabled and normal-coloured (they do not need
    an open template)
```

After loading a template (**New from Image...**, pick any reference image):

```text
[ ] The previously disabled buttons (Save, Detect, Student ID, Set,
    Questions, Custom, Reference, Edit Bubbles, Reset, Validate) become
    normal-coloured and enabled
```

## Hover, pressed and checked states

```text
[ ] Hovering any toolbar button shows a visible but subtle background change
[ ] Pressing and holding a button shows a slightly darker "pressed" state
[ ] Clicking Grid toggles a clearly distinguishable highlighted/"on"
    appearance, and the alignment grid appears/disappears on the canvas to
    match
[ ] Clicking Edit Bubbles (with a bubble region selected) shows the same kind
    of obvious checked appearance
```

## Tooltips and status bar

```text
[ ] Hovering any toolbar icon shows a tooltip describing what it does
[ ] A tooltip for an action with a keyboard shortcut names that shortcut,
    e.g. "Save the template (Ctrl+S)"
[ ] Hovering a toolbar icon updates the OMRFlow window's own status bar (the
    outer application status bar at the very bottom, from Help > About or any
    other menu hover - Qt shows QAction status tips there automatically)
```

## Keyboard shortcuts

```text
[ ] Ctrl+Shift+N still opens "New from Image"
[ ] Ctrl+Shift+O still opens "Open"
[ ] Ctrl+S still saves
[ ] Ctrl+Shift+S still opens "Save As"
[ ] Ctrl+Z / Ctrl+Y still undo/redo
[ ] Delete still deletes the selected region
[ ] Ctrl+D still duplicates the selected region
[ ] Ctrl++ / Ctrl+- / Ctrl+0 still zoom in/out/fit
```

## Region list (Duplicate / Delete)

```text
[ ] The Duplicate button below the region list shows a copy icon plus text
[ ] The Delete button shows a trash icon plus text
[ ] Delete does not look more visually prominent than Duplicate (same size,
    same styling - only the icon differs)
```

## Geometry panel

```text
[ ] With nothing selected, "X (px)", "Y (px)", "Width (px)", "Height (px)"
    and "Normalised" labels are all clearly readable even though the fields
    are disabled
[ ] Selecting a region enables the fields and shows readable, non-greyed
    values
```

## Window resizing and toolbar overflow

```text
[ ] Narrow the main window significantly (drag its right edge left)
[ ] No toolbar label starts clipping or shrinking as the window narrows
[ ] Instead, a "»" overflow indicator appears at the right end of the
    toolbar once it runs out of room
[ ] Clicking "»" shows the overflowed actions in a menu, and they still work
    from there
[ ] Widen the window back out - the overflowed actions return to the toolbar
```

## High-DPI / Windows display scaling

If your machine's Display settings can be changed (Settings > System >
Display > Scale):

```text
[ ] At 100% scaling, icons are crisp and toolbar text is not clipped
[ ] At 125% scaling (if available), icons remain crisp (not blurry) and
    nothing clips
[ ] At 150% scaling (if available), the same holds
```

## Left navigation (should be unaffected by this change)

```text
[ ] The eight workflow stages (Project, Template, Scan, Resolve, Attendance,
    Answer Key, Results, Reports) still list in the same order
[ ] The current stage is still highlighted the same way as before
[ ] No icons were added to the left navigation (out of scope for this pass)
```

## Regression check - Phase 2 functionality unaffected

```text
[ ] Detect Markers still finds registration markers on a sample sheet
[ ] Creating a Student ID / Set / Questions / Custom / Reference region still
    works exactly as before, through the same drag-a-rectangle-then-dialog
    flow
[ ] Undo/redo still works after adding, moving or resizing a region
[ ] Validate still reports the same errors/warnings as before this change
[ ] Save and Open still round-trip a template correctly
```
