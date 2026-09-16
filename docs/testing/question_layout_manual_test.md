# Manual test — Set codes, question column layout, and canvas panning

Run this against the real desktop application (a genuine windowed session) to
verify the Set Code generalisation, multi-column question layout, and
middle/right-button panning added after Phase 2. Every scenario here also has
an automated equivalent (`tests/unit/test_template_authoring.py`,
`tests/gui/test_template_designer_dialogs.py`,
`tests/gui/test_template_designer_canvas_panning.py`,
`tests/gui/test_template_designer_page.py`); this checklist exists for what an
automated test cannot judge - does a dragged column visually line up with the
printed sheet, does panning feel smooth.

```powershell
.\.venv\Scripts\python.exe -m omr_scanner
```

Navigate to **2. Template**, then **New from Image...** and pick any
reference sheet (a real 100-question sheet with five printed blocks of
1-20/21-40/41-60/61-80/81-100 is ideal; the bundled
`examples/templates/100_question_4_choice_example.png` also works).

## Set code scenarios

```text
[ ] Click Set, leave "Set code mode" at "Enumerated values", enter
    "A,B,C,D" - accept - one region with 4 bubbles appears
[ ] Click Set again, enter "10,11,12" - accept - exactly 3 bubbles appear
    (not split into digits); select the region and confirm (in the
    properties panel or by checking the saved file) the values are the
    strings "10"/"11"/"12"
[ ] Click Set again, enter "01,02,03" - accept - confirm the saved values
    are "01"/"02"/"03", not the numbers 1/2/3
[ ] Click Set again, switch "Set code mode" to "Positional code", set
    "Code length" to 2, leave symbols at 0-9 - accept - a two-column grid of
    10 bubbles per column appears
[ ] Repeat with "Code length" = 3 - a three-column grid appears
[ ] Delete the three Set regions created above before continuing (keep the
    template tidy for the next section)
```

## Question Block Calibration Test

```text
[ ] Click Questions, drag a rectangle roughly where the first printed
    question block sits
[ ] In the dialog, set: Questions = 1-100, Choices = A,B,C,D, Number of
    columns = 5, Questions per column = 20
[ ] Adjust the Column Gap field and confirm the dashed canvas preview moves
    the later columns immediately, before clicking OK
[ ] Accept - 5 separate regions appear (questions_0 .. questions_4), one per
    printed column
[ ] Select Column 1 (questions_0) and align it precisely against the
    printed block by dragging/nudging
[ ] Select a different column (e.g. questions_2) and drag it independently -
    confirm Column 1 and every other column do NOT move
[ ] Check the region list / properties panel: the dragged column's question
    numbers (e.g. "Questions 41-60") are unchanged - only its position moved
[ ] Save the template
[ ] Reopen it (Open...) - confirm every column's manually-adjusted position
    is exactly where you left it, not snapped back to an even grid
```

## Array Calibration Test

```text
[ ] Delete the 5 columns from the previous section (or start a fresh
    template) and create only ONE 20-question column (Questions dialog,
    Number of columns = 1, Questions per column = 20)
[ ] Align that one column precisely with the first printed block
[ ] Select it, click Array in the toolbar
[ ] Set: Columns = 5, Questions per column = 20, Direction = Left to Right,
    Starting question = 1 - accept
[ ] Confirm 5 columns now exist, with logical ranges 1-20 / 21-40 / 41-60 /
    61-80 / 81-100 (visible in the region list labels)
[ ] Confirm exactly 400 bubbles total across the 5 columns
[ ] Drag one or two of the generated columns slightly to line up exactly
    with the printed sheet, if the automatic spacing does not already match
[ ] Select any one column and click Distribute in the toolbar - confirm the
    first and last column do not move, and any columns between them that
    were previously misaligned re-space evenly
[ ] Press Ctrl+Z once after Array or Distribute - confirm the entire
    operation reverses in one step, not column-by-column
```

## Canvas Navigation Test

```text
[ ] Zoom to 200% or higher (mouse wheel, or the Zoom In toolbar button)
[ ] Middle-click and drag on the image - confirm the canvas pans smoothly
    and the cursor shows a closed-hand icon while dragging
[ ] Right-click and drag on the image - confirm the canvas pans the same way
[ ] Right-click WITHOUT dragging (press and release in place) - confirm
    nothing pans and no region becomes selected
[ ] Select a region, then middle-drag or right-drag across it - confirm the
    region does NOT move and stays selected
[ ] Left-click and drag a selected region's body - confirm the region still
    moves normally, and the canvas viewport does not shift
[ ] Repeat the middle/right-drag checks at 25%, 50%, 100% and 400% zoom -
    panning should feel identical at every level
[ ] Hold Space and left-drag - confirm the original space+drag panning still
    works exactly as before
```
