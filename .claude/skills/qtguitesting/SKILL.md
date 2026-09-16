---
name: qtguitesting
description: Use when modifying or debugging OMRFlow's PySide6/Qt user interface - the graphics canvas, region geometry, mouse interaction, zoom/pan, resize handles, property panels, dialogs, toolbar layout or visual rendering. Provides a repeatable workflow combining pytest-qt, QTest, geometry diagnostics, deterministic screenshots and real-image verification against examples/ECE-0000.png.
---

# Testing OMRFlow's Qt GUI

> **Screenshot similarity is not proof of GUI correctness.**
>
> Behaviour - position invariants, model synchronisation, signal emission,
> serialisation, question numbering - must be asserted through **program state**.
> Screenshots are for the things state cannot show: clipping, spacing,
> visibility, overlay alignment, icon presence, obvious layout regressions.

Applies to every Qt surface in OMRFlow, not only the Template page. Scan,
Resolve, Results and the rest arrive in later phases and will use the same
canvas, the same coordinate systems and the same testing hierarchy.

## Workflow

Follow this whenever you change GUI code. Do not skip from step 1 to step 12.

1. **Identify the affected behaviours.** Which widget, which signal, which
   model field.
2. **Identify the invariants** - what must *not* change. See
   `references/qt_coordinate_systems.md` § "Invariants worth asserting".
3. **Run the focused existing tests** for that area first (see *Commands*).
4. **Add a regression test for every reproduced bug**, written as the invariant
   rather than as the symptom.
5. **Make the smallest architecture-correct fix.** Never an ad-hoc offset
   (`x += ...`), never a special case (`if columns == 1:`). If the fix needs
   one, the model is wrong - fix the model.
6. **Run the focused tests again.**
7. **Run the Qt GUI tests** (`pytest -m gui`).
8. **Run the smoke test** (`scripts/run_gui_smoke_tests.py`).
9. **Load `examples/ECE-0000.png`** - the repository's real OMR page.
10. **Capture screenshots** (`scripts/capture_gui_states.py`).
11. **Dump geometry** (`scripts/dump_gui_geometry.py`) for anything
    geometry-sensitive.
12. **Read the screenshots and the dumps.** Actually open them.
13. **Fix any regression** you find, and return to step 6.
14. **Run the full suite**, plus `ruff check .` and `mypy`.
15. **Report what actually ran**, with real pass/fail counts.

## The testing hierarchy

Work up it. A bug reachable at a lower level is tested there.

| # | Level | Where | Use for |
|---|-------|-------|---------|
| 1 | Pure domain/geometry | `tests/unit/` | layout maths, invariants, model rules - no `QApplication` needed |
| 2 | Qt widget | `tests/gui/` | a dialog's fields, a panel's signals |
| 3 | `QGraphicsScene`/`View` | `tests/gui/` | drag, resize, selection, pan, zoom |
| 4 | Application smoke | `scripts/run_gui_smoke_tests.py` | "does it still start and load an image" |
| 5 | Screenshots | `scripts/capture_gui_states.py` | layout, clipping, overlay alignment |
| 6 | Real image | `examples/ECE-0000.png` | everything synthetic fixtures cannot expose |
| 7 | Visual inspection | you, reading the PNGs | the rest |

Most OMRFlow geometry bugs are level 1 or 3. The container-invariance bug was a
pure function's contract (level 1); the resize-jumps-to-origin bug was a
scene/local confusion (level 3). Neither needed a screenshot to find.

## Interaction rules

**Qt-native only.** `qtbot.mouseClick/mousePress/mouseMove/mouseRelease`, or
`QTest.*` against `view.viewport()`. Never PyAutoGUI, never OS screenshot tools.

**Never absolute screen coordinates.** `click at (1342, 716)` breaks on a
different DPI, window position, screen size, OS or CI runner. State the point in
the coordinate system that *means* something and let Qt map it:

```python
point = canvas.mapFromScene(item.scene_rect().center())   # scene -> viewport
QTest.mousePress(canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
```

**Find items semantically.** `scene.items()[3]` is not the question region; it
is whatever Qt happened to sort third, and adding one overlay breaks it. Use the
domain id:

```python
item = canvas._scene.region_items["questions_0"]      # keyed by zone id
zone = state.template.zone_by_id("questions_0")
```

For widgets, prefer `findChild(QDoubleSpinBox, "bubble_radius")` and the
`objectName`s listed in `references/omrflow_gui_test_scenarios.md`.

**Test dialogs directly.** Never `exec()` a modal in a test - offscreen there is
nothing to click and it blocks forever. Construct it, set its fields, call its
build method, assert the model (`docs/TESTING.md` already requires this):

```python
dialog = QuestionBlockDialog(bounds=..., existing_zone_ids=[], image_width=..., ...)
dialog.columns_box.setValue(1)
zones = dialog._build_zones()
```

**Let Qt process events** before reading geometry or grabbing a pixmap:
`qtbot.waitUntil(...)`, `qtbot.wait(...)` or `QApplication.processEvents()`.
Never a long `sleep`.

## Geometry debugging

When something moves, resizes or lands in the wrong place, dump before and
after. Load `references/qt_coordinate_systems.md` first - the distinction between
`pos()`, `boundingRect()` and `sceneBoundingRect()` is the cause of most of these
bugs, and the module explains which OMRFlow uses where.

```
BEFORE:  model geometry | item.pos() | boundingRect() | sceneBoundingRect()
         <perform the GUI operation>
AFTER:   model geometry | item.pos() | boundingRect() | sceneBoundingRect()
```

`scripts/dump_gui_geometry.py` produces exactly that as JSON. A disagreement
between the four columns *is* the bug.

## Scripts

All are run from the repository root with the project's virtualenv.

```bash
# Quick sanity check: app starts, page builds, sample loads, controls exist.
python .claude/skills/qtguitesting/scripts/run_gui_smoke_tests.py

# Deterministic screenshots into test-output/gui/.
python .claude/skills/qtguitesting/scripts/capture_gui_states.py

# Geometry of the template's regions, as JSON.
python .claude/skills/qtguitesting/scripts/dump_gui_geometry.py --scenario question-region

# Compare two captures with tolerance, not byte equality.
python .claude/skills/qtguitesting/scripts/compare_gui_images.py a.png b.png
```

Output goes to `test-output/gui/` (git-ignored). Failures additionally write
`test-output/gui/failures/` with a screenshot, a geometry dump and the
traceback - preserve those rather than re-running until something passes.

## Commands

```bash
pytest                                            # everything
pytest -m gui                                     # Qt tests only
pytest -m "not gui"                               # headless logic only
pytest tests/gui/test_template_designer_region_geometry.py -q     # focused
pytest tests/unit/test_question_region_container.py -q            # focused
pytest tests/integration/test_orientation_marker_detection.py -q  # focused
ruff check . && mypy                              # lint and types
```

Use a focused path while iterating; run the full suite before reporting.

## Headless vs. visible

`tests/gui/conftest.py` selects Qt's `offscreen` platform automatically when
there is no display. Both modes matter and they answer different questions:

* **headless functional** - CI, and every assertion about state. Force it with
  `QT_QPA_PLATFORM=offscreen` if needed.
* **visible interactive** - what a human sees. Do *not* force `offscreen` during
  ordinary development on Windows or macOS; it changes font metrics and
  rendering, which is precisely what you are trying to look at.

Screenshots are captured with `QWidget.grab()`, which works in both.

## Real-image verification

`examples/ECE-0000.png` is the repository's standard real OMR page: 2480x3508,
four magenta corner squares, an 86x44 orientation dash near the top-left, and
five question columns of 20 (Q1-100).

* Never modify, crop, resize or overwrite it.
* Never hard-code coordinates from it into `src/`. It is a validation sample, not
  a template. (Expected values may appear in *tests* as ground truth - clearly
  labelled as such.)
* Resolve it with `pathlib` from a known anchor, never from the working
  directory:
  ```python
  REPOSITORY_ROOT = Path(__file__).resolve().parents[N]
  SAMPLE = REPOSITORY_ROOT / "examples" / "ECE-0000.png"
  ```

## When a GUI test fails

Preserve the evidence. Do not retry until it happens to pass.

Keep: the traceback, the geometry dump, a screenshot of the failing state, the
scenario name, the input image, and the relevant model state. Name them for the
scenario - `question_resize_anchor_failure.png`,
`question_resize_anchor_geometry.json`.

## Never do this

* Test-only branches in production code (`if TEST_MODE: ...`). Tests must
  exercise the real path. Small diagnostic accessors that expose existing state
  are fine.
* Redesigning the application to suit a test. Adding a stable `objectName` is
  fine; restructuring a widget hierarchy is not.
* Asserting pixel-perfect widget positions. Fonts, DPI and Qt styles differ
  across machines; `docs/TESTING.md` rules this out.
* Byte-equality screenshot comparison.

## References

Load these when the task needs them - not up front.

* **`references/qt_coordinate_systems.md`** - the six coordinate systems,
  `pos()` vs `boundingRect()` vs `sceneBoundingRect()`, the invariants worth
  asserting, and the conversion helpers. Read this before diagnosing *any*
  position or resize bug.
* **`references/omrflow_gui_test_scenarios.md`** - ten concrete scenarios
  against `examples/ECE-0000.png`, with the expected invariant for each, plus
  the stable `objectName` table.
