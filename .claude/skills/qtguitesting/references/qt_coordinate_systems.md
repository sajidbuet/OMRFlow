# Qt coordinate systems in OMRFlow

Read this before diagnosing any position, resize, drag or overlay-alignment bug.
Most of them are one frame being mistaken for another.

## The six frames

| Frame | Units | Origin | Lives in |
| --- | --- | --- | --- |
| **Normalised template** | fractions, `[0, 1]` | canonical page top-left | `.omrt`; `NormalizedRect`, `NormalizedPoint`, `NormalizedSize` |
| **Image pixels** | reference-image pixels | image top-left | `DecodedImage`; every spin box in the designer; every `imaging` result |
| **Scene** | scene units | scene origin | `QGraphicsScene`, `item.scenePos()`, `event.scenePos()` |
| **Item-local** | scene units | the *item's own* `pos()` | `item.rect()`, `item.boundingRect()`, `event.pos()` inside an item |
| **Viewport** | device pixels | viewport widget top-left | `QGraphicsView` mouse events, `QRubberBand`, `widget.grab()` |
| **Screen** | device pixels | the desktop | `QCursor.pos()`. **Never appears in a test.** |

### OMRFlow's two simplifying decisions

**Scene == image pixels.** The scene *is* the reference image at 1:1.
`scene.setSceneRect(0, 0, pixmap.width(), pixmap.height())`, and zoom is applied
only by `QGraphicsView.setTransform`. So:

* scene <-> image needs **no conversion at all**;
* zoom is in exactly one place (the view's matrix) and can never be applied twice;
* `CoordinateMapper` therefore only converts image pixels <-> normalised, and
  never needs to know the zoom level.

**Normalised is the document.** Everything persisted is normalised, so a
template is resolution independent. Conversion happens at the GUI boundary only,
using the *reference image's* size.

```
.omrt  --normalised--> CoordinateMapper --image px--> scene (1:1) --view transform--> viewport
```

## `pos()` vs `boundingRect()` vs `sceneBoundingRect()`

These are three different things and conflating them is the single most common
`QGraphicsItem` bug.

```python
item.pos()                # QPointF - the item's origin, in its PARENT's frame
                          #           (the scene, for a top-level item)
item.rect()               # QRectF  - QGraphicsRectItem's rectangle, ITEM-LOCAL
item.boundingRect()       # QRectF  - everything the item paints, ITEM-LOCAL,
                          #           including half the pen width
item.sceneBoundingRect()  # QRectF  - boundingRect() mapped into the SCENE
item.mapToScene(p)        # item-local -> scene
item.mapFromScene(p)      # scene -> item-local
view.mapToScene(p)        # viewport -> scene   (undoes zoom and scroll)
view.mapFromScene(p)      # scene -> viewport   (applies zoom and scroll)
```

`boundingRect()` is **not** `sceneBoundingRect()` minus `pos()`: the scene
version also carries the pen's outline. That is why OMRFlow reports geometry from
its own `RegionHandleItem.scene_rect()` rather than from `sceneBoundingRect()` -
the pen must not leak into the saved template.

## `RegionHandleItem`'s contract

Every region overlay keeps this invariant, enforced by its constructor and by
`set_scene_rect()`:

```
item.pos()               == (x, y)          scene coordinates
item.rect()              == (0, 0, w, h)    item-local
item.scene_rect()        == (x, y, w, h)    scene coordinates
```

**Position lives in `pos()` and only there. Size lives in `rect()` and only
there.** Storing the position in both is what a `setRect(x, y, w, h)` on a
positioned item does, and it is how the resize bug happened:

```python
# THE BUG (fixed):
self._press_rect = QRectF(self.rect())   # item-local, origin always (0, 0)
delta = event.scenePos() - self._press_pos   # scene
rect = self._press_rect.translated(delta)    # a meaningless mixture
self.set_scene_rect(rect)                    # read as scene -> pos() := delta
# The region teleported to the scene origin, losing exactly its old pos().
```

```python
# THE FIX: one frame throughout.
self._press_rect = self.scene_rect()     # scene
delta = event.scenePos() - self._press_pos   # scene
rect = QRectF(self._press_rect); rect.setRight(rect.right() + delta.x())
self.set_scene_rect(rect)                # scene
```

Note what falls out with no special cases: moving only `setRight` cannot change
`x`, `y` or `height`. The resize anchoring semantics *are* the arithmetic.

## Invariants worth asserting

Write the invariant, not the symptom. A test that says "does not jump to (0, 0)"
passes as soon as it jumps to (0, 1) instead.

### Resize anchoring

| Handle dragged | x | y | width | height |
| --- | --- | --- | --- | --- |
| right | unchanged | unchanged | changes | unchanged |
| bottom | unchanged | unchanged | unchanged | changes |
| bottom-right | unchanged | unchanged | changes | changes |
| left | changes | unchanged | changes | unchanged |
| top | unchanged | changes | unchanged | changes |
| top-left | changes | changes | changes | changes |

The top/left handles move the top-left *boundary*; that is what they are for.
Any other handle changing the position is a coordinate-frame bug.

### Numeric edit (properties panel)

Editing **Width** changes width. Not x, not y, not height. Likewise Height.

### Question region container

The rectangle the user drew is the union of the generated column zones' bounds.
Changing the column count, the questions per column, the choice count, the column
gap, the row/choice pitch or the bubble size reflows the *inside* and leaves that
union identical (see `ColumnLayoutMode.FIT_CONTAINER`).

```
BEFORE  4 columns:  x=X, y=Y, w=W, h=H
AFTER   1 column:   x=X, y=Y, w=W, h=H     <- and the internal layout DID change
```

Assert both halves. A fix that freezes the whole region, contents included,
satisfies the first and is useless.

### Bubble radius

Radius is defined in **image pixels**, so zoom cannot affect it:

```
radius = 8 image px  ->  the same stored geometry at 50%, 100% and 200% zoom
```

Changing only the radius changes only `grid.bubble_size`. Every bubble centre,
every override and the parent rectangle are unchanged (`set_zone_bubble_size`).

### Pan

Middle-drag and right-drag scroll the viewport. `view.horizontalScrollBar()`
changes; **no** model value and **no** `item.scene_rect()` does.

## Update direction

There is one authority and one refresh path:

```
user gesture (canvas drag/resize, dialog, properties panel)
      -> DesignerState mutation on the immutable OmrTemplate   <- the authority
      -> TemplateDesignerPage._refresh_all()
            -> canvas.rebuild_regions(specs)   scene items rebuilt from the model
            -> region list, properties panel, status row
```

Graphics items are never a source of truth; they are rebuilt from the document.
Loops are prevented in exactly one place: `PropertiesPanel.set_geometry()` blocks
its spin boxes' signals, so a value flows canvas -> panel *or* panel -> canvas
for one user action, never both.

When a test finds the scene item and the model disagreeing, the bug is a missing
`_refresh_all()`, not a stale widget to poke.

## Zoom, DPI and the things that are not geometry

* **Zoom** is `view.setTransform(QTransform.fromScale(f, f))`. It changes
  rendering only. Anything that changes stored geometry when you zoom is a bug.
* **Grab margins** (`HANDLE_MARGIN_PX`) are in *view* pixels, deliberately, so
  the handle feels the same size at any zoom - converted through
  `view.transform().m11()` in `_local_margin()`. This is the one place a view
  quantity legitimately enters an item.
* **Device pixel ratio** affects `widget.grab()` output size on a HiDPI display.
  Compare screenshots by structure, never by absolute pixel count.
* **Cosmetic pens** (`pen.setCosmetic(True)`) keep a 1 px outline 1 px wide at
  any zoom. Used for the bubble preview so a large zoom does not turn the outline
  into a blob.

## Quick diagnostic snippet

```python
def describe(item, zone=None) -> dict:
    r, b, s = item.rect(), item.boundingRect(), item.sceneBoundingRect()
    return {
        "pos":                 [item.pos().x(), item.pos().y()],
        "rect":                list(r.getRect()),
        "bounding_rect":       list(b.getRect()),
        "scene_bounding_rect": list(s.getRect()),
        "scene_rect":          list(item.scene_rect().getRect()),
        "model":               None if zone is None else list(
            (zone.bounds.x, zone.bounds.y, zone.bounds.width, zone.bounds.height)
        ),
    }
```

`scripts/dump_gui_geometry.py` is this, for every region, as a JSON file.
