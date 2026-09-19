"""The Scan page's image preview, with the recognition overlay drawn on top.

Purpose:
    Show the rectified sheet and, over it, exactly what the recogniser measured:
    where each zone was read, which bubble in each group was taken as the
    answer, which groups need a human, and - when asked - where the
    registration markers were expected and found (Phase 4 calibration).

Responsibilities:
    * :class:`ScanPreviewView` - a zoomable, pannable view of one page.
    * :class:`OverlayItem` - one graphics item that paints every zone rectangle,
      every bubble and, optionally, the registration markers, rather than
      several hundred separate items.

What does NOT belong here:
    * Any recognition logic. This widget is handed plain
      :class:`~omr_scanner.services.recognition_service.ZoneView` and
      :class:`~omr_scanner.services.recognition_service.BubbleView` values and
      draws them; it never decides what a bubble means.
    * Modifying the image. Overlays are painted by the *view*, so the underlying
      scan is never altered - the preview and the file on disk always agree.

Coordinates:
    The scene is the canonical page at 1:1, so every overlay coordinate can be
    used exactly as the service reported it. The preview pixmap is usually
    smaller than the canonical page (see
    :data:`~omr_scanner.services.recognition_service.DEFAULT_PREVIEW_MAX_DIMENSION`),
    so it is scaled *up* into that frame: zooming past the preview's own
    resolution shows a soft image rather than a misaligned overlay, which is the
    right trade - the overlay is the thing being checked.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.services import BubbleView, DecodedImage, MarkerView, ZoneView

ZOOM_STEP = 1.25
MIN_ZOOM = 0.02
MAX_ZOOM = 16.0

SELECTED_COLOR = QColor(0, 150, 60)
"""Green: the bubble the engine took as the answer."""

MULTIPLE_COLOR = QColor(200, 30, 40)
"""Red: one of several marks in a group that should have had one."""

UNCERTAIN_COLOR = QColor(230, 145, 0)
"""Amber: too faint, or too close to its runner-up, to accept."""

UNREADABLE_COLOR = QColor(150, 40, 150)
"""Purple: the bubble could not be sampled at all."""

EMPTY_COLOR = QColor(120, 140, 170, 130)
"""Muted blue-grey: a bubble that was measured and found empty."""

_STATUS_COLORS: dict[str, QColor] = {
    "resolved": SELECTED_COLOR,
    "blank": EMPTY_COLOR,
    "multiple": MULTIPLE_COLOR,
    "uncertain": UNCERTAIN_COLOR,
    "unreadable": UNREADABLE_COLOR,
}

_STATUS_SYMBOLS: dict[str, str] = {
    "multiple": "!",
    "uncertain": "?",
    "unreadable": "x",
}
"""A glyph drawn beside a group that needs attention.

Colour alone is not enough - it fails for a colour-blind user, in a screenshot
printed in grey, and in the review workflow's own "what is wrong with this
sheet" question. The symbol says which."""

SAMPLE_WINDOW_COLOR = QColor(0, 110, 200, 200)
"""Blue: the elliptical interior the sampler actually read for a bubble.

Drawn from :attr:`~omr_scanner.services.recognition_models.BubbleView.sample_half_width`
and its ``height`` counterpart - the region ``fill_ratio`` was computed over -
never from the printed bubble size, which is larger (the printed ring is ink,
so it is deliberately excluded; see :mod:`omr_scanner.imaging.metrics`). The
distinction matters: an operator checking alignment must see where the engine
looked, not where the bubble is printed."""

BUBBLE_CENTER_COLOR = QColor(200, 0, 140)
"""Magenta: the exact sampled centre of a bubble.

The single most useful thing for spotting a systematically displaced template -
a whole page of centres sitting consistently off the printed bubbles is
immediately visible, where a ring that merely overlaps is not."""

CENTER_MARK_PX = 2.5
"""Half-length of the centre cross's arms, in canonical pixels."""

EXPECTED_MARKER_COLOR = QColor(30, 100, 220)
"""Blue: where the template says a registration marker's centre should sit."""

DETECTED_MARKER_CLOSE_COLOR = QColor(0, 150, 60)
"""Green: the detected marker, reprojected into canonical pixels, landed close
to where it was expected - registration behaving the way it should."""

DETECTED_MARKER_FAR_COLOR = QColor(200, 30, 40)
"""Red: the detected marker landed a noticeable distance from where it was
expected, which is worth a look even though registration itself succeeded."""

MARKER_MISMATCH_PX = 3.0
"""How far a detected marker's canonical position may sit from its expected
one, in canonical pixels, before the calibration overlay calls it out in
:data:`DETECTED_MARKER_FAR_COLOR` rather than :data:`DETECTED_MARKER_CLOSE_COLOR`.

With exactly four correspondences the fitted homography reproduces its own
four points almost exactly (``docs/IMAGE_PROCESSING.md`` -
:attr:`~omr_scanner.services.recognition_models.ScanQuality.mean_reprojection_error_px`
is typically a small fraction of a pixel), so any visible gap here is a
property of the fit being *forced* through markers that do not sit quite where
printed - not of measurement noise. A few pixels is generous against that
baseline and still catches a genuinely displaced marker."""


class OverlayItem(QGraphicsItem):
    """Paints every zone rectangle and every bubble of one recognition result.

    One item rather than several hundred: a sheet has 500 bubbles, and a
    `QGraphicsItem` each would cost more in scene bookkeeping than the drawing
    itself. The whole overlay is static between results, so there is nothing to
    gain from individual items.

    Args:
        parent: Optional parent item.
    """

    def __init__(self, parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self._page = QRectF(0, 0, 1, 1)
        self._zones: tuple[ZoneView, ...] = ()
        self._bubbles: tuple[BubbleView, ...] = ()
        self._markers: tuple[MarkerView, ...] = ()
        self.show_zones = True
        self.show_bubbles = True
        self.show_empty_bubbles = False
        self.show_markers = False
        self.show_sample_windows = False
        self.show_centers = False
        self.setZValue(10)

    def set_page_size(self, width: float, height: float) -> None:
        """Set the canonical page extent this overlay covers."""
        self.prepareGeometryChange()
        self._page = QRectF(0, 0, max(width, 1.0), max(height, 1.0))

    def set_content(
        self,
        zones: Sequence[ZoneView],
        bubbles: Sequence[BubbleView],
        markers: Sequence[MarkerView] = (),
    ) -> None:
        """Replace what the overlay draws.

        Args:
            zones: Zone rectangles.
            bubbles: Measured bubbles.
            markers: Registration markers, with their canonical and expected
                positions (:attr:`~omr_scanner.services.recognition_models.MarkerView.canonical_x`
                and :attr:`~omr_scanner.services.recognition_models.MarkerView.expected_x`,
                and their ``y`` counterparts). Empty for a page that never
                registered - there is nothing in canonical pixels to draw a
                marker at.
        """
        self._zones = tuple(zones)
        self._bubbles = tuple(bubbles)
        self._markers = tuple(markers)
        self.update()

    def boundingRect(self) -> QRectF:
        """Return the canonical page rectangle."""
        return self._page

    def paint(
        self,
        painter: QPainter,
        _option: QStyleOptionGraphicsItem,
        _widget: QWidget | None = None,
    ) -> None:
        """Draw zone rectangles, then bubbles, then the attention glyphs."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.show_zones:
            self._paint_zones(painter)
        if self.show_bubbles:
            self._paint_bubbles(painter)
        if self.show_sample_windows:
            self._paint_sample_windows(painter)
        if self.show_centers:
            self._paint_centers(painter)
        if self.show_markers:
            self._paint_markers(painter)

    def _paint_zones(self, painter: QPainter) -> None:
        """Outline each zone in its template colour, tinted by its status."""
        for zone in self._zones:
            status_color = _STATUS_COLORS.get(zone.status)
            base = QColor(zone.color)
            pen = QPen(status_color if status_color is not None else base, 3.0)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(zone.x, zone.y, zone.width, zone.height))

            label = zone.label
            symbol = _STATUS_SYMBOLS.get(zone.status, "")
            if symbol:
                label = f"{label}  {symbol}"
            font = QFont(painter.font())
            font.setPointSizeF(max(zone.height * 0.035, 11.0))
            painter.setFont(font)
            painter.drawText(QPointF(zone.x, zone.y - 6.0), label)

    def _paint_bubbles(self, painter: QPainter) -> None:
        """Ring every bubble, filling the ones the engine chose."""
        for bubble in self._bubbles:
            interesting = bubble.selected or bubble.group_status in _STATUS_SYMBOLS
            if not interesting and not self.show_empty_bubbles:
                continue

            color = _STATUS_COLORS.get(bubble.group_status, EMPTY_COLOR)
            rect = QRectF(
                bubble.x - bubble.width / 2.0,
                bubble.y - bubble.height / 2.0,
                bubble.width,
                bubble.height,
            )
            if bubble.selected:
                painter.setPen(QPen(color, 3.0))
                fill = QColor(color)
                fill.setAlpha(70)
                painter.setBrush(QBrush(fill))
            else:
                painter.setPen(QPen(color, 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect)

            symbol = _STATUS_SYMBOLS.get(bubble.group_status, "")
            if symbol and bubble.leading:
                # One glyph per group, anchored to its darkest bubble, so a row
                # of four options is not decorated four times over - and so the
                # glyph points at what the sheet nearly said.
                font = QFont(painter.font())
                font.setBold(True)
                font.setPointSizeF(max(bubble.height * 0.8, 10.0))
                painter.setFont(font)
                painter.setPen(QPen(color))
                painter.drawText(
                    QPointF(rect.left() - bubble.width * 1.1, rect.bottom()), symbol
                )

    def _paint_sample_windows(self, painter: QPainter) -> None:
        """Outline the ellipse the sampler actually read, for every bubble.

        Deliberately a *different* shape from the one
        :meth:`_paint_bubbles` draws: that one is the printed bubble, this one
        is the interior the fill ratio was measured over, and it is smaller.
        Both come straight off the
        :class:`~omr_scanner.services.recognition_models.BubbleView` - nothing
        here recomputes either.
        """
        pen = QPen(SAMPLE_WINDOW_COLOR, 1.0)
        pen.setStyle(Qt.PenStyle.DotLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for bubble in self._bubbles:
            half_x = bubble.sample_half_width
            half_y = bubble.sample_half_height
            if half_x <= 0.0 or half_y <= 0.0:
                # The caller kept no per-bubble evidence, so there is no
                # sampled region to draw. Drawing the printed bubble instead
                # would be a plausible-looking lie.
                continue
            painter.drawEllipse(
                QRectF(bubble.x - half_x, bubble.y - half_y, half_x * 2.0, half_y * 2.0)
            )

    def _paint_centers(self, painter: QPainter) -> None:
        """Mark each bubble's sampled centre with a small cross."""
        painter.setPen(QPen(BUBBLE_CENTER_COLOR, 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for bubble in self._bubbles:
            x, y = bubble.x, bubble.y
            painter.drawLine(
                QPointF(x - CENTER_MARK_PX, y), QPointF(x + CENTER_MARK_PX, y)
            )
            painter.drawLine(
                QPointF(x, y - CENTER_MARK_PX), QPointF(x, y + CENTER_MARK_PX)
            )

    def _paint_markers(self, painter: QPainter) -> None:
        """Draw each registration marker's expected and detected position.

        Both are drawn in canonical pixels - the same frame every zone and
        bubble is drawn in - because both already are: ``expected_x/y`` is the
        template's own declared marker centre and ``canonical_x/y`` is the
        detected marker reprojected through the fitted transform
        (:mod:`omr_scanner.services.recognition_service`). Neither is
        recomputed here.
        """
        half = 9.0
        for marker in self._markers:
            expected = QPointF(marker.expected_x, marker.expected_y)
            ex, ey = expected.x(), expected.y()
            painter.setPen(QPen(EXPECTED_MARKER_COLOR, 2.0))
            painter.drawLine(QPointF(ex - half, ey), QPointF(ex + half, ey))
            painter.drawLine(QPointF(ex, ey - half), QPointF(ex, ey + half))

            if marker.canonical_x == 0.0 and marker.canonical_y == 0.0:
                continue  # No successful registration to compare against.
            detected = QPointF(marker.canonical_x, marker.canonical_y)
            distance = math.hypot(detected.x() - expected.x(), detected.y() - expected.y())
            color = (
                DETECTED_MARKER_CLOSE_COLOR
                if distance <= MARKER_MISMATCH_PX
                else DETECTED_MARKER_FAR_COLOR
            )
            painter.setPen(QPen(color, 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(detected.x() - half, detected.y() - half, half * 2, half * 2))

            if distance > MARKER_MISMATCH_PX:
                painter.drawLine(expected, detected)

            font = QFont(painter.font())
            font.setPointSizeF(max(half * 1.1, 10.0))
            painter.setFont(font)
            painter.setPen(QPen(color))
            painter.drawText(
                QPointF(detected.x() + half + 2.0, detected.y() - half),
                f"{marker.role.replace('_', ' ')} ({distance:.1f}px)",
            )


class ScanPreviewView(QGraphicsView):
    """A zoomable, pannable view of one rectified scan and its overlay.

    Navigation matches the Template Designer's canvas so the two pages feel the
    same: the wheel zooms, the middle button pans always, and the right button
    pans once the drag passes Qt's own drag-distance threshold - below which it
    is still an ordinary right-click.

    Signals:
        clicked_scene_point: ``(float, float)`` canonical-page coordinates of a
            plain left click - the same frame every zone, bubble and marker is
            drawn in. Emitted for the calibration viewer's click-to-inspect
            (``docs/calibration_workflow.md``); the Scan page does not connect
            to it and is unaffected by its existence.

    Args:
        parent: Optional Qt parent.
    """

    clicked_scene_point = Signal(float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setObjectName("scanPreview")
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor(238, 238, 240)))

        self._background: QGraphicsPixmapItem | None = None
        self._overlay = OverlayItem()
        self._scene.addItem(self._overlay)
        self._page_size = (0, 0)
        self._zoom = 1.0

        self._pan_active = False
        self._pan_last: QPoint | None = None
        self._pan_button: Qt.MouseButton | None = None
        self._right_press: QPoint | None = None
        self._space_panning = False

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Remove the image and the overlay, leaving an empty view."""
        if self._background is not None:
            self._scene.removeItem(self._background)
            self._background = None
        self._overlay.set_content((), ())
        self._page_size = (0, 0)
        self._scene.setSceneRect(QRectF(0, 0, 1, 1))
        self.viewport().update()

    def set_page(
        self,
        image: DecodedImage | None,
        *,
        canonical_width: int,
        canonical_height: int,
        preview_scale: float = 1.0,
    ) -> None:
        """Show one rectified page.

        Args:
            image: The decoded preview, or ``None`` to show an empty page of the
                right size (which is what a failed registration gets).
            canonical_width: Canonical page width in pixels - the scene's width.
            canonical_height: Canonical page height in pixels.
            preview_scale: ``image`` pixels per canonical pixel. The pixmap is
                scaled by its reciprocal so that overlay coordinates need no
                adjustment anywhere else.
        """
        if self._background is not None:
            self._scene.removeItem(self._background)
            self._background = None

        width = max(canonical_width, 1)
        height = max(canonical_height, 1)
        self._page_size = (width, height)
        self._scene.setSceneRect(QRectF(0, 0, width, height))
        self._overlay.set_page_size(width, height)

        if image is not None:
            fmt = (
                QImage.Format.Format_BGR888
                if image.channels == 3
                else QImage.Format.Format_Grayscale8
            )
            # QImage does not copy the buffer; the DecodedImage's bytes may be
            # released when the result is replaced, so an explicit copy is
            # required to keep the pixmap valid.
            qimage = QImage(image.data, image.width, image.height, image.stride, fmt)
            item = QGraphicsPixmapItem(QPixmap.fromImage(qimage.copy()))
            item.setZValue(-100)
            if preview_scale > 0.0 and preview_scale != 1.0:
                item.setScale(1.0 / preview_scale)
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            self._scene.addItem(item)
            self._background = item

        self.viewport().update()

    def set_overlay(
        self,
        zones: Sequence[ZoneView],
        bubbles: Sequence[BubbleView],
        markers: Sequence[MarkerView] = (),
    ) -> None:
        """Replace the overlay content."""
        self._overlay.set_content(zones, bubbles, markers)

    def set_overlay_visible(
        self,
        *,
        zones: bool,
        bubbles: bool,
        empty: bool,
        markers: bool = False,
        sample_windows: bool = False,
        centers: bool = False,
    ) -> None:
        """Choose which overlay layers are drawn."""
        self._overlay.show_zones = zones
        self._overlay.show_bubbles = bubbles
        self._overlay.show_empty_bubbles = empty
        self._overlay.show_markers = markers
        self._overlay.show_sample_windows = sample_windows
        self._overlay.show_centers = centers
        self._overlay.update()

    @property
    def has_page(self) -> bool:
        """Whether a page is currently shown."""
        return self._page_size != (0, 0)

    # ------------------------------------------------------------------
    # Zoom
    # ------------------------------------------------------------------
    @property
    def zoom(self) -> float:
        """Current zoom factor; ``1.0`` is one canonical pixel per screen pixel."""
        return self._zoom

    def zoom_in(self) -> None:
        """Zoom in one step."""
        self._apply_zoom(self._zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        """Zoom out one step."""
        self._apply_zoom(self._zoom / ZOOM_STEP)

    def zoom_to_actual_size(self) -> None:
        """Show the page at 100 per cent."""
        self._apply_zoom(1.0)

    def set_zoom(self, factor: float) -> None:
        """Zoom to ``factor``, clamped to the view's own limits.

        The public form of an absolute zoom, for a caller that has computed the
        magnification it wants - the conflict review workspace fitting one
        disputed group to the viewport, for instance. Clamping happens here, so
        a caller never has to know :data:`MIN_ZOOM` and :data:`MAX_ZOOM`.
        """
        self._apply_zoom(factor)

    def fit_to_window(self) -> None:
        """Zoom so the whole page fits the viewport."""
        width, height = self._page_size
        if width <= 0 or height <= 0:
            return
        viewport = self.viewport().size()
        factor = min(viewport.width() / width, viewport.height() / height)
        self._apply_zoom(max(MIN_ZOOM, min(MAX_ZOOM, factor)))
        self.centerOn(width / 2.0, height / 2.0)

    def _apply_zoom(self, factor: float) -> None:
        factor = max(MIN_ZOOM, min(MAX_ZOOM, factor))
        self._zoom = factor
        self.setTransform(self.transform().fromScale(factor, factor))

    # ------------------------------------------------------------------
    # Navigation events
    # ------------------------------------------------------------------
    def wheelEvent(self, event: QWheelEvent) -> None:
        """Zoom on the wheel instead of scrolling."""
        if event.angleDelta().y() > 0:
            self.zoom_in()
        else:
            self.zoom_out()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """Hold space for Qt's own hand-drag panning."""
        if event.key() == Qt.Key.Key_Space and not self._space_panning:
            self._space_panning = True
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        """Leave space-pan mode."""
        if event.key() == Qt.Key.Key_Space:
            self._space_panning = False
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Middle button pans immediately; the right button waits for a drag.

        A plain left click emits :attr:`clicked_scene_point` in canonical-page
        coordinates before being passed on - Qt's own click handling on an
        empty scene does nothing with it, so nothing about the Scan page's
        behaviour changes; only a listener that actually connects (the
        calibration viewer's bubble inspector) sees anything.
        """
        if event.button() == Qt.MouseButton.MiddleButton:
            self._start_pan(event.position().toPoint(), event.button())
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self._right_press = event.position().toPoint()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.has_page:
            point = self.mapToScene(event.position().toPoint())
            self.clicked_scene_point.emit(point.x(), point.y())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Continue a pan, or start one once a right-drag clears the threshold."""
        position = event.position().toPoint()
        if self._pan_active:
            self._update_pan(position)
            event.accept()
            return
        if self._right_press is not None:
            moved = position - self._right_press
            if moved.manhattanLength() > QApplication.startDragDistance():
                self._start_pan(self._right_press, Qt.MouseButton.RightButton)
                self._update_pan(position)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """End a pan; a right-click that never moved stays an ordinary click."""
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            if self._pan_active and self._pan_button == event.button():
                self._stop_pan()
            if event.button() == Qt.MouseButton.RightButton:
                self._right_press = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _start_pan(self, position: QPoint, button: Qt.MouseButton) -> None:
        self._pan_active = True
        self._pan_last = position
        self._pan_button = button
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _update_pan(self, position: QPoint) -> None:
        if self._pan_last is None:
            return
        delta = position - self._pan_last
        self._pan_last = position
        horizontal = self.horizontalScrollBar()
        vertical = self.verticalScrollBar()
        horizontal.setValue(horizontal.value() - delta.x())
        vertical.setValue(vertical.value() - delta.y())

    def _stop_pan(self) -> None:
        self._pan_active = False
        self._pan_last = None
        self._pan_button = None
        self.unsetCursor()


__all__ = [
    "BUBBLE_CENTER_COLOR",
    "CENTER_MARK_PX",
    "DETECTED_MARKER_CLOSE_COLOR",
    "DETECTED_MARKER_FAR_COLOR",
    "EMPTY_COLOR",
    "EXPECTED_MARKER_COLOR",
    "MARKER_MISMATCH_PX",
    "MULTIPLE_COLOR",
    "SAMPLE_WINDOW_COLOR",
    "SELECTED_COLOR",
    "UNCERTAIN_COLOR",
    "UNREADABLE_COLOR",
    "OverlayItem",
    "ScanPreviewView",
]
