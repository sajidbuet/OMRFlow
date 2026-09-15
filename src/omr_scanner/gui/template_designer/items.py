"""Graphics items drawn on the template designer canvas.

Purpose:
    Give each editable thing on the canvas (a registration marker, the
    orientation marker, a zone, a single bubble during fine-tuning) its own
    small `QGraphicsItem` that knows how to draw and drag/resize itself, so the
    canvas widget itself only has to wire selection and persistence, never
    per-pixel drawing logic.

Coordinate convention:
    Every item lives directly in **scene coordinates that equal reference-image
    pixel coordinates** - the scene is simply "the image, at 1:1", and
    `QGraphicsView.scale()` supplies zoom on top of that. This is why
    `coordinates.CoordinateMapper` never needs to know about zoom: an item's
    `pos()`/`rect()` in this file *is* an image-pixel position, always, and only
    conversion to/from *normalised* template coordinates is needed anywhere
    else.

Responsibilities:
    * `RegionHandleItem` - a resizable, draggable rectangle shared by markers
      and zones, coloured and labelled by the caller.
    * `BubbleDotItem` - a small draggable dot used only in per-bubble editing
      mode.

What does NOT belong here:
    * Committing a move/resize to the template. Items emit
      ``geometry_changed`` (gesture in progress, for the live properties panel)
      and ``geometry_committed`` (mouse released, for the undo-stack push);
      the canvas listens and calls into `state.DesignerState`.
"""

from __future__ import annotations

from enum import StrEnum

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsRectItem,
    QGraphicsSceneHoverEvent,
    QGraphicsSceneMouseEvent,
    QStyleOptionGraphicsItem,
    QWidget,
)

HANDLE_MARGIN_PX = 8
"""How close the cursor must be to an edge, in *view* pixels, to grab it for
resizing. Kept in view pixels (via the item's ``deviceTransform``) rather than
scene pixels so the grab zone feels the same size at any zoom level."""

MIN_SIZE_PX = 4.0
"""A region or marker can never be resized smaller than this many image
pixels on a side - small enough to never constrain a real bubble, large enough
that a rectangle can never accidentally collapse to nothing and become
impossible to grab again."""


class HandleState(StrEnum):
    """Visual/interaction state of a :class:`RegionHandleItem`.

    Distinct colours for each, per the Phase 2 brief's requirement that
    auto-detected, confirmed, manually-overridden and missing/invalid markers
    be visually distinguishable at a glance.
    """

    NORMAL = "normal"
    """An ordinary, already-accepted region (most zones, confirmed markers)."""

    AUTO_DETECTED = "auto_detected"
    """Placed by detection, not yet confirmed by the user."""

    MANUAL_OVERRIDE = "manual_override"
    """A detected marker the user has since moved by hand."""

    MISSING = "missing"
    """A corner with no marker placed at all - drawn as a dashed placeholder."""

    SELECTED = "selected"
    """Currently selected; takes priority over the states above for the border."""


_STATE_COLORS: dict[HandleState, QColor] = {
    HandleState.NORMAL: QColor(30, 136, 229),
    HandleState.AUTO_DETECTED: QColor(255, 179, 0),
    HandleState.MANUAL_OVERRIDE: QColor(216, 27, 96),
    HandleState.MISSING: QColor(198, 40, 40),
}
_SELECTED_COLOR = QColor(0, 200, 83)
_FILL_ALPHA = 45
"""Overlay fill opacity (of 255) - low enough that the printed sheet under it
stays legible, per the brief's "semi-transparent overlays" requirement."""


class _EditSignals(QObject):
    """Signals for :class:`RegionHandleItem`.

    `QGraphicsItem` is not a `QObject` and cannot have signals directly; every
    interactive item owns one of these instead. A private helper class rather
    than putting signals on the canvas, so each item's own geometry changes are
    identified by the item that emitted them without a lookup table.
    """

    geometry_changed = Signal(object)
    """Emitted continuously during a drag/resize with the item itself."""

    geometry_committed = Signal(object)
    """Emitted once, on mouse release, with the item itself."""

    selected = Signal(object)
    """Emitted when the item is clicked, with the item itself."""


class RegionHandleItem(QGraphicsRectItem):
    """A draggable, resizable rectangle representing a marker or a zone.

    Args:
        item_id: Stable identifier the canvas uses to find the corresponding
            domain object (a zone id, or a marker role's string value).
        rect: Initial geometry, in scene (= image-pixel) coordinates.
        kind: ``"marker"``, ``"orientation"`` or ``"zone"`` - echoed back on
            every signal so the page knows which table to look ``item_id`` up
            in without guessing from its format.
        color: Base outline/fill colour; overridden visually while
            :attr:`state` is anything other than :attr:`HandleState.NORMAL`.
        label: Short text drawn above the rectangle.
        resizable: Whether edge/corner dragging resizes rather than the whole
            shape being a fixed size (bubbles-in-a-group markers are usually
            resizable; a single bubble dot in fine-tune mode is not - it uses
            :class:`BubbleDotItem` instead).
        locked: When true, the item can be selected but not moved or resized -
            the designer's "lock" feature (§10).
    """

    def __init__(
        self,
        item_id: str,
        rect: QRectF,
        *,
        kind: str = "zone",
        color: QColor,
        label: str = "",
        resizable: bool = True,
        locked: bool = False,
    ) -> None:
        super().__init__(rect)
        self.item_id = item_id
        self.kind = kind
        self.label = label
        self.base_color = color
        self.state = HandleState.NORMAL
        self.resizable = resizable
        self.locked = locked
        self.signals = _EditSignals()

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, not locked)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self._resize_edge: str | None = None
        self._press_rect: QRectF | None = None
        self._press_pos: QPointF | None = None
        self.bubble_points: list[QPointF] = []
        """Bubble centres to preview inside this region, in item-local
        coordinates. Painted directly rather than as child items: a
        400-bubble question block would otherwise mean 400 extra
        `QGraphicsItem` instances just to *look* at, for every zone, all the
        time."""

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paint(
        self,
        painter: QPainter,
        _option: QStyleOptionGraphicsItem,
        _widget: QWidget | None = None,
    ) -> None:
        """Draw the rectangle, its fill, and its label.

        Overridden (rather than relying on `QGraphicsRectItem`'s own pen/brush)
        so the colour can react to :attr:`state` and to selection without the
        canvas having to reach in and restyle the item on every state change.
        """
        color = _SELECTED_COLOR if self.isSelected() else _STATE_COLORS.get(
            self.state, self.base_color
        )
        pen = QPen(color, 2 if not self.isSelected() else 3)
        if self.state is HandleState.MISSING:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)

        fill = QColor(color)
        fill.setAlpha(_FILL_ALPHA)
        painter.setBrush(QBrush(fill))
        painter.drawRect(self.rect())

        if self.label:
            painter.setPen(QPen(color))
            text_point = self.rect().topLeft()
            text_point.setY(text_point.y() - 4)
            painter.drawText(text_point, self.label)

        if self.bubble_points:
            dot_pen = QPen(color)
            dot_pen.setWidth(1)
            painter.setPen(dot_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            radius = 3.0
            for point in self.bubble_points:
                painter.drawEllipse(point, radius, radius)

    # ------------------------------------------------------------------
    # Mouse interaction: drag to move, drag near an edge to resize
    # ------------------------------------------------------------------
    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Start a move or, near an edge, a resize gesture."""
        self.signals.selected.emit(self)
        if self.locked:
            event.ignore()
            return
        self._press_rect = QRectF(self.rect())
        self._press_pos = event.scenePos()
        self._resize_edge = (
            self._edge_at(event.pos()) if self.resizable else None
        )
        if self._resize_edge is None:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Continue the drag: either Qt's own move, or a manual resize."""
        if self._resize_edge is not None and self._press_rect is not None:
            self._apply_resize(event)
            self.signals.geometry_changed.emit(self)
            return
        super().mouseMoveEvent(event)
        self.signals.geometry_changed.emit(self)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """End the gesture and commit it - exactly one undo-stack entry."""
        super().mouseReleaseEvent(event)
        self._resize_edge = None
        self._press_rect = None
        self._press_pos = None
        self.signals.geometry_committed.emit(self)

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        """Show a resize cursor near an edge, otherwise the default cursor."""
        if self.resizable and not self.locked:
            edge = self._edge_at(event.pos())
            self.setCursor(_cursor_for_edge(edge))
        super().hoverMoveEvent(event)

    def scene_rect(self) -> QRectF:
        """Return this item's rectangle in scene coordinates.

        `QGraphicsRectItem.rect()` is in the item's own local coordinates,
        which differ from scene coordinates once the item has been moved by a
        Qt-handled drag (`pos()` shifts, `rect()` stays put at the origin);
        this is the one place that combines the two, so every caller gets an
        answer already in scene space.
        """
        return self.rect().translated(self.pos())

    def set_scene_rect(self, rect: QRectF) -> None:
        """Set this item's geometry from a scene-coordinate rectangle."""
        self.prepareGeometryChange()
        self.setPos(rect.topLeft())
        self.setRect(QRectF(0, 0, rect.width(), rect.height()))

    def set_state(self, state: HandleState) -> None:
        """Change the visual state and repaint."""
        self.state = state
        self.update()

    # ------------------------------------------------------------------
    # Resize internals
    # ------------------------------------------------------------------
    def _edge_at(self, local_pos: QPointF) -> str | None:
        """Return which edge/corner ``local_pos`` (item-local) is near, if any."""
        rect = self.rect()
        margin = self._local_margin()
        near_left = abs(local_pos.x() - rect.left()) <= margin
        near_right = abs(local_pos.x() - rect.right()) <= margin
        near_top = abs(local_pos.y() - rect.top()) <= margin
        near_bottom = abs(local_pos.y() - rect.bottom()) <= margin

        if near_left and near_top:
            return "top_left"
        if near_right and near_top:
            return "top_right"
        if near_left and near_bottom:
            return "bottom_left"
        if near_right and near_bottom:
            return "bottom_right"
        if near_left:
            return "left"
        if near_right:
            return "right"
        if near_top:
            return "top"
        if near_bottom:
            return "bottom"
        return None

    def _local_margin(self) -> float:
        """Convert the fixed on-screen grab margin into this item's local units."""
        views = self.scene().views() if self.scene() else []
        if not views:
            return HANDLE_MARGIN_PX
        scale = views[0].transform().m11()
        return HANDLE_MARGIN_PX / max(scale, 0.01)

    def _apply_resize(self, event: QGraphicsSceneMouseEvent) -> None:
        """Grow or shrink the item according to which edge is being dragged."""
        if self._press_rect is None or self._press_pos is None or self._resize_edge is None:
            return
        delta = event.scenePos() - self._press_pos
        rect = QRectF(self._press_rect)

        if "left" in self._resize_edge:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - MIN_SIZE_PX))
        if "right" in self._resize_edge:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + MIN_SIZE_PX))
        if "top" in self._resize_edge:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - MIN_SIZE_PX))
        if "bottom" in self._resize_edge:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + MIN_SIZE_PX))

        self.set_scene_rect(rect)


def _cursor_for_edge(edge: str | None) -> QCursor:
    """Return the resize cursor matching ``edge``, or the default arrow."""
    shapes = {
        "left": Qt.CursorShape.SizeHorCursor,
        "right": Qt.CursorShape.SizeHorCursor,
        "top": Qt.CursorShape.SizeVerCursor,
        "bottom": Qt.CursorShape.SizeVerCursor,
        "top_left": Qt.CursorShape.SizeFDiagCursor,
        "bottom_right": Qt.CursorShape.SizeFDiagCursor,
        "top_right": Qt.CursorShape.SizeBDiagCursor,
        "bottom_left": Qt.CursorShape.SizeBDiagCursor,
    }
    return QCursor(shapes[edge] if edge is not None else Qt.CursorShape.ArrowCursor)


class BubbleDotItem(QGraphicsRectItem):
    """A small draggable dot marking one bubble centre, for per-bubble fine-tuning.

    Deliberately not a :class:`RegionHandleItem`: a bubble dot only ever
    moves (there is nothing to resize), and hundreds of these may exist at
    once while a group is in fine-tune mode, so painting is kept as cheap as
    possible.

    Args:
        row: Zero-based row index within the zone's grid.
        column: Zero-based column index.
        center: Initial centre, in scene (image-pixel) coordinates.
        radius: Half the dot's on-screen size, in scene pixels.
        overridden: Whether this cell currently has a
            :class:`~omr_scanner.domain.template.BubbleOverride` (drawn in a
            different colour, so a fine-tuned bubble is visible at a glance).
    """

    def __init__(
        self,
        row: int,
        column: int,
        center: QPointF,
        *,
        radius: float = 6.0,
        overridden: bool = False,
    ) -> None:
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.row = row
        self.column = column
        self.overridden = overridden
        self.signals = _EditSignals()
        self.setPos(center)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self._update_style()

    def _update_style(self) -> None:
        color = QColor(216, 27, 96) if self.overridden else QColor(30, 136, 229)
        self.setPen(QPen(color, 1))
        fill = QColor(color)
        fill.setAlpha(160)
        self.setBrush(QBrush(fill))

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """Commit the new position once the drag ends."""
        super().mouseReleaseEvent(event)
        self.signals.geometry_committed.emit(self)


__all__ = ["BubbleDotItem", "HandleState", "RegionHandleItem"]
