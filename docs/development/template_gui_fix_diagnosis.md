# Template Designer - GUI correction pass: diagnosis

Written before the fixes in this pass, from reading the Phase 2b code as
committed at `0d4a0a6`. Every root cause below was confirmed by reading the
implementation, not inferred from the symptom.

## Coordinate systems in play

The designer moves geometry between five frames. Naming them precisely is what
makes three of the five bugs below obvious rather than mysterious.

| Frame | Where it lives | Units |
| --- | --- | --- |
| **Normalised template** | `.omrt` (`NormalizedRect`, `NormalizedPoint`) | fractions of the canonical page, `[0, 1]` |
| **Image pixels** | `DecodedImage`, the properties panel, every dialog spin box | reference-image pixels |
| **Scene** | `QGraphicsScene` | *identical to image pixels* - the scene is "the image at 1:1" |
| **Item-local** | `QGraphicsItem.rect()`, `boundingRect()`, `event.pos()` | scene units, origin at the item's own `pos()` |
| **Viewport** | `QGraphicsView` mouse events, `QRubberBand` | device pixels; zoom lives *only* in the view transform |

The intended invariant for a region overlay item, already used by
`TemplateCanvasScene.rebuild_regions`, is:

```
item.pos()               == (x, y)          scene coordinates
item.rect()              == (0, 0, w, h)    item-local
item.sceneBoundingRect() == (x, y, w, h)    scene coordinates
```

`CoordinateMapper` converts image pixels <-> normalised only; scene <-> image
needs no conversion by construction, and zoom is never applied by hand.

---

## 1. A question region changes size when the column count changes

**Observed symptom.** The user drags out a rectangle, the Question Block dialog
opens with `Columns = 4`, and changing it to `1` makes the region on the canvas
about a quarter as wide. Changing it to `5` makes it wider than the rectangle
that was drawn.

**Root cause.** There is no container. A Question Region is stored as *N sibling
`Zone`s*, one per printed column (`docs/TEMPLATE_FORMAT.md`, by design - one
`BubbleGrid` expresses one pitch pair). `QuestionBlockDialog._build_zones` always
passes an explicit `row_pitch`/`column_pitch` (read from its spin boxes), which
puts `generate_question_columns` into its *explicit-pitch* branch:

```python
strip_width = bubble_size.width + max(nominal_field.columns - 1, 0) * column_pitch
strip_height = bubble_size.height + max(nominal_field.rows - 1, 0) * row_pitch
```

In that branch `bounds.width` / `bounds.height` are **not read at all** - the
drawn rectangle only anchors `bounds.x` / `bounds.y`. The block's outer extent is
therefore `columns * strip_width + (columns - 1) * column_gap`, a quantity
proportional to the column count. The drawn rectangle is discarded as soon as the
dialog opens.

**Files/classes involved.**
`domain/template_authoring.py:generate_question_columns`;
`gui/template_designer/dialogs.py:QuestionBlockDialog._build_zones`,
`._seed_spacing_defaults`.

**Chosen correction.** Make the container authoritative, and say so in the API.
`generate_question_columns` gains an explicit `layout_mode`:

* `ColumnLayoutMode.FIT_CONTAINER` (the new default) - `bounds` *is* the user's
  container. The strips tile it exactly:
  `strip_width = (bounds.width - column_gap * (columns - 1)) / columns`,
  `strip_height = bounds.height`. The union of the generated zones' bounds
  reproduces `bounds` to floating-point tolerance for any column count, any gap,
  any pitch and any bubble size. An explicit pitch, when given, positions the
  lattice *inside* its strip (top-left anchored, single-bubble axes centred,
  matching `fit_grid_to_bounds`) instead of defining the strip's size; a pitch
  that would not fit is rejected with a readable message rather than silently
  growing the region.
* `ColumnLayoutMode.FROM_PITCH` - the previous behaviour, kept because
  `generate_column_array` genuinely means "extend this calibrated column into an
  array", where growing outward is the point of the gesture.

The dialog additionally gains a "Fit bubble spacing to the region" checkbox (on
by default). While it is on, the pitch is derived from the container on every
change, so `4 -> 1` reflows the bubbles across the full width instead of leaving
them bunched at the left. Unchecking it restores the manual pitch fields.

**Regression risks.** Two unit tests in `TestGenerateQuestionColumnsExplicitPitch`
described geometry that only the `FROM_PITCH` branch produces; they now name that
mode explicitly, and a `FIT_CONTAINER` twin was added beside them.
`generate_column_array` is unchanged in behaviour because it opts into
`FROM_PITCH`.

---

## 2. Bubble size cannot be adjusted, and changing it does not alter the preview

**Observed symptom.** Nothing in the designer offers a bubble radius; the Question
Block dialog's two "Bubble width/height (px)" fields are the only bubble sizing
anywhere, and editing them changes no visible circle on the canvas.

**Root cause.** Two independent causes.

*Rendering.* `RegionHandleItem.paint` draws every bubble preview with a hard-coded
radius:

```python
radius = 3.0
for point in self.bubble_points:
    painter.drawEllipse(point, radius, radius)
```

`RegionSpec` carries only `bubble_points` - centres, no size - so the zone's real
`grid.bubble_size` never reaches the canvas at all. No value the user could type
would change what is drawn.

*Model/UI.* There is no template-level bubble size. Each dialog hard-codes the
module constants `DEFAULT_BUBBLE_WIDTH = 0.022` / `DEFAULT_BUBBLE_HEIGHT = 0.016`
(Student ID, Question Set, Custom), so three of the four bubble region kinds have
no bubble sizing control whatsoever.

**Files/classes involved.**
`gui/template_designer/items.py:RegionHandleItem.paint`;
`gui/template_designer/canvas.py:RegionSpec`;
`gui/template_designer/dialogs.py` (all four region dialogs);
`domain/template.py:OmrTemplate`.

**Chosen correction.**

* `RegionSpec` gains `bubble_size` (width, height in image pixels) and
  `RegionHandleItem` paints each preview bubble at that size. Radius is defined in
  image/scene pixels, so it is unaffected by zoom by construction: the view
  transform scales the rendering, never the geometry.
* `OmrTemplate` gains an additive, optional `default_bubble_radius` - normalised
  to the **page width**, the same additive-optional-field convention
  `reference_image` already set, so no `format_version` bump and older documents
  load unchanged. `OmrTemplate.default_bubble_size` converts it to the
  `NormalizedSize` the grid model already stores, using `page.aspect_ratio` for
  the vertical axis, so a radius that is circular in pixels stays circular.
* A "Bubble radius (px)" spin box on toolbar row 2 edits that default, and a
  per-region radius (with a "Use template default" checkbox) appears in the
  properties panel for any selected bubble region.
* Inheritance is expressed without a schema change: a region **inherits** the
  template radius while its stored bubble size still equals what that radius
  produces. Setting a region radius makes it differ, and later changes to the
  template default then leave it alone. This survives save/reload, unlike a
  session-only flag would.
* Changing a radius uses `set_zone_bubble_size`, which rewrites **only**
  `grid.bubble_size` - `origin`, both pitches, every override and `bounds` are
  untouched - so every bubble centre and the parent rectangle are preserved
  exactly, which is what makes calibration predictable.

**Regression risks.** The Question Block dialog's two size fields become one
radius field, so a deliberately *elliptical* bubble can no longer be authored from
that dialog (the `.omrt` model still stores width and height independently, and a
template authored elsewhere round-trips unchanged). One dialog test that
hard-coded the old default bubble size now derives it from the dialog's own
default radius.

---

## 3. Resizing a region moves it to the top-left

**Observed symptom.** Grabbing any edge or corner handle of a region teleports it
to (roughly) the scene origin on the first mouse-move.

**Root cause.** A scene/local coordinate confusion in exactly the shape the brief
predicted. `RegionHandleItem.mousePressEvent` captures

```python
self._press_rect = QRectF(self.rect())      # ITEM-LOCAL
self._press_pos  = event.scenePos()         # SCENE
```

`_apply_resize` then adds a **scene-space** delta to that **item-local** rectangle
and hands the result to `set_scene_rect`, which interprets it as **scene**
coordinates:

```python
def set_scene_rect(self, rect):
    self.setPos(rect.topLeft())             # treats a local rect as a scene rect
    self.setRect(QRectF(0, 0, rect.width(), rect.height()))
```

Because `rebuild_regions` builds every item as `setRect(0, 0, w, h)` +
`setPos(x, y)`, `self.rect().topLeft()` is always `(0, 0)`. The first resize
therefore sets `pos()` to the mouse delta alone - the region lands at the top-left
corner of the scene, having lost its entire position. The magnitude of the jump is
exactly the item's old `pos()`, which is why it reads as "snaps to the origin"
rather than "drifts".

**Files/classes involved.** `gui/template_designer/items.py:RegionHandleItem`
(`mousePressEvent`, `_apply_resize`, `set_scene_rect`, `__init__`).

**Chosen correction.** Do the whole gesture in one frame. `mousePressEvent`
captures `self.scene_rect()` (scene), `_apply_resize` edits that scene rectangle
with the scene delta, and `set_scene_rect` receives a genuine scene rectangle.
`__init__` additionally normalises whatever rectangle it is given to the
`pos() = top-left, rect() = (0, 0, w, h)` invariant, so the class can no longer be
constructed into the inconsistent state that made the confusion possible.

Resize anchoring then falls out of the arithmetic with no special cases: dragging
`right` moves only `rect.setRight`, so `x`, `y` and `height` are untouched;
`bottom_right` moves only right and bottom; `top_left` moves the top-left
boundary, which is the one case where position *should* change.

No correction offsets are added anywhere. The numeric path (`PropertiesPanel` ->
`_apply_geometry` -> `DesignerState.resize_zone`) was already correct and is
unchanged - it never routed through `_apply_resize`.

**Regression risks.** Any caller that relied on `RegionHandleItem(rect)` leaving a
non-origin rectangle in `rect()` would see different local coordinates.
`rebuild_regions` and `show_preview` already used the origin form; nothing else
constructs the class.

---

## 4. Orientation-marker auto-detection fails inside a search region

**Observed symptom.** Drawing a rectangle around the printed orientation dash and
asking for automatic detection never finds it.

**Root cause.** The feature does not exist. There is no orientation-detection
action on the toolbar, no ROI-scoped detector in `imaging`, and no service entry
point. `imaging/orientation.py` is Phase 1's *alignment-time* routine: it decides
which of four quarter-turns a scan was fed in, needs four already-detected corner
markers and a candidate homography to do it, and reports through
`OrientationResult`. It cannot answer "where is the dash inside this rectangle".
`marker_detection_service.detect_registration_markers` only searches the four
fixed corner regions for square registration markers, and its filters
(`MarkerDetectionConfig`) are tuned for a near-square marker - they would reject a
2:1 dash on aspect ratio alone even if it were asked.

So the reported "rejected because it is inside the ROI" is not a bad containment
test; nothing was running at all.

**Files/classes involved.** New: `imaging/orientation_marker.py`,
`services/marker_detection_service.detect_orientation_marker`. Touched:
`gui/template_designer/page.py` (a "Orientation" detect action).

**Chosen correction.** A dedicated, ROI-scoped dash detector:

* The caller passes the ROI in **image pixels**. The detector crops
  `image[y:y+h, x:x+w]`, thresholds the crop on its own statistics (a local ROI of
  mostly paper thresholds far more reliably than the whole page), finds external
  contours, and scores each candidate on darkness, fill of its own bounding box
  (rectangularity), aspect ratio against the expected dash shape, size relative to
  the ROI, and distance from the ROI centre.
* Every reported coordinate is translated back by the ROI origin in one place, so
  a candidate is always returned in full-image pixels.
* A candidate is never rejected for being *inside* the ROI - being inside is the
  whole point of the ROI. The only containment rule is the one the crop already
  enforces.
* Thresholds live in one `OrientationMarkerConfig` dataclass, not as literals in
  GUI code, and default to a dash (`expected_aspect_ratio = 2.0`, accepted range
  1.2-6.0) rather than to registration-square criteria.
* When `debug_dir` is given, an annotated overlay (the ROI, every candidate, the
  accepted one, each rejection reason) is written, so a failed calibration can be
  looked at rather than guessed at.

**Regression risks.** None to existing behaviour - this is purely additive. The
risk is in the detector itself: a sheet whose orientation mark is *not*
dash-shaped will be rejected, by design, with a stated reason rather than by
accepting the darkest blob.

---

## 5. Wasted vertical space at the top of the Template page

**Observed symptom.** The canvas starts a long way down the page.

**Root cause.** `WorkflowPage.__init__` unconditionally builds a header for every
page: a title label, a **word-wrapped full-width summary label** ("Design and
calibrate the OMR sheet template."), and a horizontal separator, inside 24 px
margins with 12 px spacing. That header is right for the six placeholder pages,
whose entire content *is* a short column of explanatory text. It is wrong for a
page whose body is a full-size editor. Combined with a single, crowded toolbar row
that overflows into a "»" menu on a narrow window, roughly 110 px of vertical
space is spent before the canvas begins.

**Files/classes involved.** `gui/pages/base_page.py:WorkflowPage`;
`gui/template_designer/page.py:_build_toolbar`.

**Chosen correction.**

* `WorkflowPage` gains `show_summary` and `compact` options. The designer page
  passes `show_summary=False` (the sentence becomes the title's tooltip and status
  tip, so the information is not lost) and `compact=True` (8 px margins, 6 px
  spacing). Every other page is untouched.
* The toolbar becomes two `QToolBar` rows in a `QVBoxLayout`: row 1 file /
  undo-redo / detection / validation, row 2 region tools / bubble radius / zoom /
  grid. Both are ordinary Qt toolbars in an ordinary layout - no fixed positioning
  - so overflow, wrapping and DPI scaling keep working.

**Regression risks.** `tests/gui/test_template_designer_toolbar.py` asserted that
every action is in `page.toolbar.actions()`; it now checks both rows through a
`page.toolbar_actions()` accessor.

---

## Update direction (single source of truth)

Unchanged in principle, and now stated so it can be tested:

```
user gesture (canvas drag/resize, dialog, properties panel)
      -> DesignerState mutation on the immutable OmrTemplate   <- the authority
      -> _refresh_all()
            -> canvas.rebuild_regions(specs)   (scene items rebuilt from the model)
            -> region list, properties panel, status row
```

Graphics items are never a source of truth; they are rebuilt from the document on
every change. The one loop-prevention rule is `PropertiesPanel.set_geometry`,
which blocks its spin boxes' signals - a canvas drag can flow canvas -> panel, or
a typed edit can flow panel -> canvas, never both for one user action.
