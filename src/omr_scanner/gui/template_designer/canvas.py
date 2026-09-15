"""The zoomable, pannable canvas showing the reference sheet and its regions.

Purpose:
    Host the reference image and every editable overlay, and translate mouse
    interaction into plain signals the template designer page can act on -
    the canvas itself knows nothing about `.omrt` documents.

Responsibilities:
    * Load a decoded image (:class:`~omr_scanner.services.DecodedImage`) as the
      background, at 1:1 scale in scene coordinates.
    * Rebuild overlay items from a plain list of :class:`RegionSpec` whenever
      the caller's document changes - a full rebuild each time rather than a
      diff, because a template has at most a few dozen regions and this keeps
      "what's on screen" always provably in sync with "what the document says",
      which is worth far more here than the cost of a few dozen widget
      constructions.
    * Zoom (wheel, in/out, fit, 100%), pan (space+drag or middle-button drag),
      an optional grid overlay and snap-to-grid.
    * Report cursor position and selection changes for the rest of the page.

What does NOT belong here:
    * Any `omr_scanner.imaging`/`cv2`/`numpy` import - forbidden for the whole
      `gui` package, and unnecessary: the caller decodes images through
      `omr_scanner.services` and hands this widget plain bytes.
    * Undo/redo bookkeeping and template mutation - `page.py` listens for this
      widget's ``geometry_committed`` signal and calls into
      `state.DesignerState`.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QRubberBand,
    QWidget,
)

from omr_scanner.gui.template_designer.items import BubbleDotItem, HandleState, RegionHandleItem
from omr_scanner.services import DecodedImage

ZOOM_STEP = 1.25
"""Factor applied per wheel notch or per Zoom In/Out action."""

MIN_ZOOM = 0.02
MAX_ZOOM = 16.0

GRID_COLOR_ALPHA = 60
"""Faint enough that the grid never competes visually with the sheet or the
region overlays it is meant to help align."""


@dataclass(frozen=True, slots=True)
class RegionSpec:
    """One overlay rectangle the canvas should display, in image pixels.

    A plain data contract between the page (which knows about `.omrt` zones
    and markers) and the canvas (which does not), so the canvas can be tested
    and reused without constructing a single domain object.

    Attributes:
        item_id: Stable id - a zone id, or a marker role's string value.
        kind: ``"marker"``, ``"orientation"`` or ``"zone"`` - purely
            informational, echoed back on signals so the page knows which
            table to look the id up in.
        x, y, width, height: Geometry in reference-image pixels.
        color: ``#RRGGBB`` overlay colour.
        label: Short text drawn above the rectangle.
        state: Visual state (see :class:`~omr_scanner.gui.template_designer.items.HandleState`).
        resizable: Whether the item exposes resize handles.
        locked: Whether the item can be selected but not moved/resized.
        bubble_points: Bubble centres to preview inside the rectangle, in image
            pixels, or ``None`` for a marker (which has no bubbles).
    """

    item_id: str
    kind: str
    x: float
    y: float
    width: float
    height: float
    color: str
    label: str = ""
    state: HandleState = HandleState.NORMAL
    resizable: bool = True
    locked: bool = False
    bubble_points: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class BubbleDotSpec:
    """One individually editable bubble, for fine-tune mode.

    Attributes:
        row: Zero-based row index within the zone's grid.
        column: Zero-based column index.
        x, y: Centre in reference-image pixels.
        overridden: Whether this cell already has an explicit override.
    """

    row: int
    column: int
    x: float
    y: float
    overridden: bool = False


class TemplateCanvasScene(QGraphicsScene):
    """The `QGraphicsScene` behind :class:`TemplateCanvasView`.

    A thin subclass exists mainly so item construction has a natural home
    (:meth:`rebuild_regions`) rather than living in the view.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.background_item: QGraphicsPixmapItem | None = None
        self.region_items: dict[str, RegionHandleItem] = {}
        self.bubble_items: list[BubbleDotItem] = []

    def set_background(self, pixmap: QPixmap) -> None:
        """Replace the reference image."""
        if self.background_item is not None:
            self.removeItem(self.background_item)
        self.background_item = QGraphicsPixmapItem(pixmap)
        self.background_item.setZValue(-1000)
        self.background_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.addItem(self.background_item)
        self.setSceneRect(QRectF(0, 0, pixmap.width(), pixmap.height()))

    def rebuild_regions(self, specs: list[RegionSpec]) -> dict[str, RegionHandleItem]:
        """Replace every region overlay item with fresh ones built from ``specs``."""
        for item in self.region_items.values():
            self.removeItem(item)
        self.region_items = {}

        for spec in specs:
            rect = QRectF(0, 0, spec.width, spec.height)
            handle = RegionHandleItem(
                spec.item_id,
                rect,
                kind=spec.kind,
                color=QColor(spec.color),
                label=spec.label,
                resizable=spec.resizable,
                locked=spec.locked,
            )
            handle.setPos(QPointF(spec.x, spec.y))
            handle.set_state(spec.state)
            handle.bubble_points = [
                QPointF(px - spec.x, py - spec.y) for px, py in spec.bubble_points
            ]
            handle.setZValue(10 if spec.kind == "zone" else 20)
            self.addItem(handle)
            self.region_items[spec.item_id] = handle

        return self.region_items

    def clear_bubble_dots(self) -> None:
        """Remove every per-bubble fine-tune dot (leaving regions untouched)."""
        for item in self.bubble_items:
            self.removeItem(item)
        self.bubble_items = []

    def show_bubble_dots(self, dots: list[BubbleDotSpec]) -> list[BubbleDotItem]:
        """Replace the fine-tune dots with ones built from ``dots``."""
        self.clear_bubble_dots()
        for dot in dots:
            item = BubbleDotItem(
                dot.row, dot.column, QPointF(dot.x, dot.y), overridden=dot.overridden
            )
            item.setZValue(30)
            self.addItem(item)
            self.bubble_items.append(item)
        return self.bubble_items


class TemplateCanvasView(QGraphicsView):
    """The interactive view: zoom, pan, grid, and signal plumbing.

    Signals:
        selection_changed: ``(item_id: str | None, kind: str)`` - ``item_id``
            is ``None`` when the selection is cleared.
        geometry_changed: ``(item_id: str, kind: str, x, y, width, height)`` in
            image pixels, emitted continuously during a drag/resize.
        geometry_committed: Same signature, emitted once per completed gesture.
        bubble_moved: ``(row: int, column: int, x: float, y: float)`` emitted
            once when a fine-tune bubble dot is dropped.
        cursor_moved: ``(x: float, y: float)`` in image pixels, or ``(-1, -1)``
            when the cursor leaves the image bounds.
        zoom_changed: The new zoom factor, ``1.0`` meaning 100%.
        region_drawn: ``(x, y, width, height)`` in image pixels, emitted once
            when the user finishes dragging out a new region rectangle in
            draw mode (see :meth:`start_draw_mode`).
    """

    selection_changed = Signal(object, str)
    geometry_changed = Signal(str, str, float, float, float, float)
    geometry_committed = Signal(str, str, float, float, float, float)
    bubble_moved = Signal(int, int, float, float)
    cursor_moved = Signal(float, float)
    zoom_changed = Signal(float)
    region_drawn = Signal(float, float, float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = TemplateCanvasScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setMouseTracking(True)

        self._zoom = 1.0
        self._grid_visible = False
        self._grid_size_px = 0.0
        self._space_panning = False
        self._image_size: tuple[int, int] | None = None

        self._draw_mode = False
        self._draw_origin: QPoint | None = None
        self._rubber_band: QRubberBand | None = None

        self._scene.selectionChanged.connect(self._on_selection_changed)

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    def set_reference_image(self, image: DecodedImage) -> None:
        """Show a newly decoded reference image and reset the view to fit it."""
        qt_format = (
            QImage.Format.Format_BGR888 if image.channels == 3 else QImage.Format.Format_Grayscale8
        )
        qimage = QImage(image.data, image.width, image.height, image.stride, qt_format)
        # QImage does not copy the buffer by default; the caller's `bytes`
        # object may be garbage collected once this method returns, so an
        # explicit copy is required to keep the pixmap valid afterwards.
        self._scene.set_background(QPixmap.fromImage(qimage.copy()))
        self._image_size = (image.width, image.height)
        self.fit_to_window()

    def rebuild_regions(self, specs: list[RegionSpec]) -> None:
        """Replace every region overlay from a fresh list of specs."""
        self._scene.rebuild_regions(specs)
        self._wire_region_signals()

    def show_bubble_dots(self, dots: list[BubbleDotSpec]) -> None:
        """Show fine-tune dots for one zone's bubbles."""
        items = self._scene.show_bubble_dots(dots)
        for item in items:
            item.signals.geometry_committed.connect(self._on_bubble_committed)

    def clear_bubble_dots(self) -> None:
        """Hide fine-tune dots (leaving region overlays alone)."""
        self._scene.clear_bubble_dots()

    def selected_region_id(self) -> str | None:
        """Return the id of the single selected region, if exactly one is selected."""
        selected = [
            item for item in self._scene.selectedItems() if isinstance(item, RegionHandleItem)
        ]
        return selected[0].item_id if len(selected) == 1 else None

    def select_region(self, item_id: str | None) -> None:
        """Select a region programmatically (e.g. from the region-list panel)."""
        for region_id, item in self._scene.region_items.items():
            item.setSelected(region_id == item_id)

    def set_region_visible(self, item_id: str, visible: bool) -> None:
        """Show or hide one region overlay without removing it from the document."""
        item = self._scene.region_items.get(item_id)
        if item is not None:
            item.setVisible(visible)

    def set_blank_canvas(self, width: int, height: int) -> None:
        """Show a plain white background of the given size (no reference image).

        Used when a loaded template's reference image is unavailable, so the
        canonical page itself still gives the user something to draw regions
        onto, sized and positioned exactly as the template declares.
        """
        pixmap = QPixmap(max(1, width), max(1, height))
        pixmap.fill(QColor("white"))
        self._scene.set_background(pixmap)
        self._image_size = (width, height)
        self.fit_to_window()

    # ------------------------------------------------------------------
    # "Draw a new region" mode
    # ------------------------------------------------------------------
    def start_draw_mode(self) -> None:
        """Enter rubber-band draw mode: the next left-drag defines a new region.

        Existing items are temporarily not selectable while drawing, so
        dragging over an existing region does not move it instead of starting
        a new rectangle.
        """
        self._draw_mode = True
        self.setCursor(Qt.CursorShape.CrossCursor)
        for item in self._scene.region_items.values():
            item.setEnabled(False)

    def cancel_draw_mode(self) -> None:
        """Leave draw mode without emitting :attr:`region_drawn`."""
        self._draw_mode = False
        self._draw_origin = None
        if self._rubber_band is not None:
            self._rubber_band.hide()
        self.unsetCursor()
        for item in self._scene.region_items.values():
            item.setEnabled(True)

    @property
    def is_drawing(self) -> bool:
        """Whether the canvas is currently waiting for a rubber-band drag."""
        return self._draw_mode

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------
    @property
    def zoom(self) -> float:
        """Current zoom factor; ``1.0`` is 100%."""
        return self._zoom

    def zoom_in(self) -> None:
        """Zoom in by one step, anchored under the mouse."""
        self._apply_zoom(self._zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        """Zoom out by one step."""
        self._apply_zoom(self._zoom / ZOOM_STEP)

    def zoom_to_actual_size(self) -> None:
        """Show the image at exactly 100%."""
        self._apply_zoom(1.0)

    def fit_to_window(self) -> None:
        """Zoom so the whole reference image fits the viewport."""
        if self._image_size is None:
            return
        width, height = self._image_size
        if width <= 0 or height <= 0:
            return
        viewport = self.viewport().size()
        factor = min(viewport.width() / width, viewport.height() / height)
        self._apply_zoom(max(MIN_ZOOM, min(MAX_ZOOM, factor)))

    def _apply_zoom(self, factor: float) -> None:
        factor = max(MIN_ZOOM, min(MAX_ZOOM, factor))
        self._zoom = factor
        self.setTransform(self.transform().fromScale(factor, factor))
        self.zoom_changed.emit(self._zoom)

    # ------------------------------------------------------------------
    # Grid
    # ------------------------------------------------------------------
    def set_grid_visible(self, visible: bool) -> None:
        """Toggle the alignment grid overlay."""
        self._grid_visible = visible
        self.viewport().update()

    def set_grid_size(self, normalized_size: float) -> None:
        """Set the grid spacing as a fraction of the image's shorter side."""
        if self._image_size is None:
            self._grid_size_px = 0.0
            return
        self._grid_size_px = normalized_size * min(self._image_size)
        self.viewport().update()

    def drawBackground(self, painter: QPainter, rect: QRectF | QRect) -> None:
        """Draw the alignment grid over the (already-drawn) background image."""
        super().drawBackground(painter, rect)
        if not self._grid_visible or self._grid_size_px <= 0.0 or self._image_size is None:
            return
        width, height = self._image_size
        pen_color = self.palette().text().color()
        pen_color.setAlpha(GRID_COLOR_ALPHA)
        painter.setPen(pen_color)

        step = self._grid_size_px
        x = 0.0
        while x <= width:
            painter.drawLine(QPointF(x, 0.0), QPointF(x, float(height)))
            x += step
        y = 0.0
        while y <= height:
            painter.drawLine(QPointF(0.0, y), QPointF(float(width), y))
            y += step

    # ------------------------------------------------------------------
    # Qt event overrides: wheel zoom, space-to-pan, cursor tracking
    # ------------------------------------------------------------------
    def wheelEvent(self, event: QWheelEvent) -> None:
        """Zoom on the mouse wheel instead of scrolling."""
        if event.angleDelta().y() > 0:
            self.zoom_in()
        else:
            self.zoom_out()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Enter pan mode while space is held, mirroring common editor conventions."""
        if event.key() == Qt.Key.Key_Space and not self._space_panning:
            self._space_panning = True
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        """Leave pan mode when space is released."""
        if event.key() == Qt.Key.Key_Space:
            self._space_panning = False
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Start a rubber-band drag in draw mode; otherwise the default behaviour."""
        if self._draw_mode and event.button() == Qt.MouseButton.LeftButton:
            self._draw_origin = event.pos()
            if self._rubber_band is None:
                self._rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
            self._rubber_band.setGeometry(QRect(self._draw_origin, QSize()))
            self._rubber_band.show()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Report cursor position and, in draw mode, grow the rubber band."""
        scene_pos = self.mapToScene(event.pos())
        if self._image_size is not None and (
            0 <= scene_pos.x() <= self._image_size[0] and 0 <= scene_pos.y() <= self._image_size[1]
        ):
            self.cursor_moved.emit(scene_pos.x(), scene_pos.y())
        else:
            self.cursor_moved.emit(-1.0, -1.0)

        if self._draw_mode and self._draw_origin is not None and self._rubber_band is not None:
            self._rubber_band.setGeometry(QRect(self._draw_origin, event.pos()).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Finish a rubber-band drag in draw mode; otherwise the default behaviour."""
        if self._draw_mode and self._draw_origin is not None:
            view_rect = QRect(self._draw_origin, event.pos()).normalized()
            if self._rubber_band is not None:
                self._rubber_band.hide()
            top_left = self.mapToScene(view_rect.topLeft())
            bottom_right = self.mapToScene(view_rect.bottomRight())
            self.cancel_draw_mode()
            width = abs(bottom_right.x() - top_left.x())
            height = abs(bottom_right.y() - top_left.y())
            if width > 1.0 and height > 1.0:
                self.region_drawn.emit(
                    min(top_left.x(), bottom_right.x()),
                    min(top_left.y(), bottom_right.y()),
                    width,
                    height,
                )
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the fit-to-window behaviour stable across a window resize."""
        super().resizeEvent(event)

    # ------------------------------------------------------------------
    # Wiring item signals up to widget-level signals
    # ------------------------------------------------------------------
    def _wire_region_signals(self) -> None:
        for item in self._scene.region_items.values():
            item.signals.geometry_changed.connect(self._on_region_changed)
            item.signals.geometry_committed.connect(self._on_region_committed)

    def _on_region_changed(self, item: RegionHandleItem) -> None:
        kind = self._kind_of(item)
        rect = item.scene_rect()
        self.geometry_changed.emit(
            item.item_id, kind, rect.x(), rect.y(), rect.width(), rect.height()
        )

    def _on_region_committed(self, item: RegionHandleItem) -> None:
        kind = self._kind_of(item)
        rect = item.scene_rect()
        self.geometry_committed.emit(
            item.item_id, kind, rect.x(), rect.y(), rect.width(), rect.height()
        )

    def _on_bubble_committed(self, item: BubbleDotItem) -> None:
        self.bubble_moved.emit(item.row, item.column, item.pos().x(), item.pos().y())

    def _on_selection_changed(self) -> None:
        selected = [
            item for item in self._scene.selectedItems() if isinstance(item, RegionHandleItem)
        ]
        if len(selected) == 1:
            item = selected[0]
            self.selection_changed.emit(item.item_id, self._kind_of(item))
        else:
            self.selection_changed.emit(None, "")

    @staticmethod
    def _kind_of(item: RegionHandleItem) -> str:
        """Return the ``kind`` a region item was tagged with at construction."""
        return item.kind


__all__ = ["BubbleDotSpec", "RegionSpec", "TemplateCanvasScene", "TemplateCanvasView"]
