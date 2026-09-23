"""The minimise, maximise/restore and close buttons of the custom chrome row.

Purpose:
    Replace the three buttons a native Windows title bar would have provided,
    once that title bar has been removed in favour of one integrated chrome
    row - and replace them convincingly enough that nobody has to think about
    it.

Responsibilities:
    * :class:`WindowButtonKind` - which of the four glyphs a button draws.
    * :class:`WindowButton` - the widget: the glyph, the hover and pressed
      fills, the accessible name, and nothing else.

What does NOT belong here:
    * Actually minimising, maximising or closing anything. A button emits
      ``clicked``; the main window owns the window state. That separation is
      what lets a test press Close without a window disappearing from under
      it.

Why the glyphs are painted rather than loaded as icons:
    They are three straight lines, a rectangle and two diagonals. A bundled
    SVG per glyph would be four assets to keep aligned with each other, and a
    font glyph ("🗕", "✕") would be a font dependency that renders at a
    different weight and baseline on every machine and is missing outright
    from some. A `QPainter` path is exact at 100%, 125%, 150%, 175% and 200%
    scaling from one source, and takes the button's own foreground colour, so
    the close button's glyph can turn white on its red hover fill without a
    second asset.

Why the metrics copy Windows':
    :data:`~omr_scanner.gui.theme.Chrome.WINDOW_BUTTON_WIDTH` is the width
    Windows' own title-bar buttons use. The point of matching it is muscle
    memory: the close button has to be where the operator's hand already
    goes, and has to be the same size their pointer is already used to
    hitting.
"""

from __future__ import annotations

from enum import Enum, auto

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from omr_scanner.gui.theme import Chrome, Color, Stroke

_GLYPH_PEN_WIDTH = 1.0
"""Hairline, like the platform's own. Wider reads as a toolbar icon rather
than as window chrome."""

_RESTORE_OFFSET = 2.5
"""How far the two rectangles of the restore glyph are separated."""


class WindowButtonKind(Enum):
    """Which glyph a :class:`WindowButton` draws.

    Attributes:
        MINIMISE: A single horizontal rule.
        MAXIMISE: An open rectangle.
        RESTORE: Two overlapping rectangles - the state a maximised window
            shows, because from there the button un-maximises.
        CLOSE: A cross.
    """

    MINIMISE = auto()
    MAXIMISE = auto()
    RESTORE = auto()
    CLOSE = auto()


_ACCESSIBLE_NAMES = {
    WindowButtonKind.MINIMISE: "Minimise",
    WindowButtonKind.MAXIMISE: "Maximise",
    WindowButtonKind.RESTORE: "Restore Down",
    WindowButtonKind.CLOSE: "Close",
}
"""What a screen reader calls each button.

Spelled out because every one of them shows a glyph and no text, and an
icon-only control must never be announced as an unnamed button. The wording
is Windows' own, so a user of a screen reader hears the same name here as
they do on every other window on the machine.
"""


class WindowButton(QAbstractButton):
    """One window-control button.

    Args:
        kind: Which glyph to draw, and therefore what the button is called.
        parent: Optional Qt parent.

    Attributes:
        kind: As passed in, and changeable through :meth:`set_kind` - the
            maximise button becomes the restore button and back again as the
            window's state changes.
    """

    def __init__(
        self, kind: WindowButtonKind, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self.setObjectName(f"windowButton_{kind.name.lower()}")
        self.setFixedSize(Chrome.WINDOW_BUTTON_WIDTH, Chrome.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        # Reachable from the keyboard, which a native title bar's buttons are
        # not. Window state has other keyboard routes (Alt+F4, Win+Up,
        # Win+Down), so this is an addition rather than the only way in - but
        # a focusable control with a visible ring costs nothing and is one
        # less thing that needs a pointer.
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._apply_kind()

    @property
    def kind(self) -> WindowButtonKind:
        """Which glyph this button currently draws."""
        return self._kind

    def set_kind(self, kind: WindowButtonKind) -> None:
        """Switch glyph - and name, and tooltip - to ``kind``."""
        if kind is self._kind:
            return
        self._kind = kind
        self._apply_kind()
        self.update()

    def _apply_kind(self) -> None:
        name = _ACCESSIBLE_NAMES[self._kind]
        self.setAccessibleName(name)
        self.setToolTip(name)
        self.setStatusTip(name)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def _background(self) -> QColor | None:
        if not self.isEnabled():
            return None
        if self._kind is WindowButtonKind.CLOSE:
            if self.isDown() or self.underMouse():
                return QColor(Color.WINDOW_CLOSE_HOVER)
            return None
        if self.isDown():
            return QColor(Color.SURFACE_PRESSED)
        if self.underMouse():
            return QColor(Color.WINDOW_BUTTON_HOVER)
        return None

    def _foreground(self) -> QColor:
        if not self.isEnabled():
            return QColor(Color.TEXT_DISABLED)
        if self._kind is WindowButtonKind.CLOSE and (self.isDown() or self.underMouse()):
            return QColor(Color.WINDOW_CLOSE_HOVER_GLYPH)
        return QColor(Color.TEXT_PRIMARY)

    def paintEvent(self, _event: object) -> None:
        """Fill the hover state, then stroke the glyph."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        background = self._background()
        if background is not None:
            painter.fillRect(self.rect(), background)

        pen = QPen(self._foreground(), _GLYPH_PEN_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.SquareCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self._glyph_path())

        if self.hasFocus():
            self._paint_focus_ring(painter)
        painter.end()

    def _glyph_box(self) -> QRectF:
        """The square the glyph is drawn inside, centred in the button.

        Offset by half a pixel so a one-pixel stroke lands *on* a device pixel
        rather than across two of them, which is the difference between a
        crisp line and a grey smear at 100% scaling.
        """
        extent = float(Chrome.WINDOW_BUTTON_GLYPH)
        left = round((self.width() - extent) / 2.0) + 0.5
        top = round((self.height() - extent) / 2.0) + 0.5
        return QRectF(left, top, extent, extent)

    def _glyph_path(self) -> QPainterPath:
        box = self._glyph_box()
        path = QPainterPath()
        if self._kind is WindowButtonKind.MINIMISE:
            middle = box.center().y()
            path.moveTo(box.left(), middle)
            path.lineTo(box.right(), middle)
        elif self._kind is WindowButtonKind.MAXIMISE:
            path.addRect(box)
        elif self._kind is WindowButtonKind.RESTORE:
            front = QRectF(
                box.left(),
                box.top() + _RESTORE_OFFSET,
                box.width() - _RESTORE_OFFSET,
                box.height() - _RESTORE_OFFSET,
            )
            path.addRect(front)
            # The window behind, drawn as the two edges of it that are not
            # covered by the front one - a full second rectangle would show
            # through and read as a grid.
            path.moveTo(front.left() + _RESTORE_OFFSET, front.top())
            path.lineTo(front.left() + _RESTORE_OFFSET, box.top())
            path.lineTo(box.right(), box.top())
            path.lineTo(box.right(), box.bottom() - _RESTORE_OFFSET)
            path.lineTo(front.right(), box.bottom() - _RESTORE_OFFSET)
        else:
            path.moveTo(box.topLeft())
            path.lineTo(box.bottomRight())
            path.moveTo(box.topRight())
            path.lineTo(box.bottomLeft())
        return path

    def _paint_focus_ring(self, painter: QPainter) -> None:
        """An inset accent outline, so keyboard focus is unmistakable."""
        ring = QPen(QColor(Color.FOCUS), float(Stroke.FOCUS_RING))
        ring.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = Stroke.FOCUS_RING
        painter.drawRect(self.rect().adjusted(inset, inset, -inset, -inset))

    # ------------------------------------------------------------------
    # Repaint triggers a plain QAbstractButton does not give us
    # ------------------------------------------------------------------
    def enterEvent(self, event: object) -> None:
        """Repaint on hover - the fill is the whole button, not its glyph."""
        self.update()
        super().enterEvent(event)  # type: ignore[arg-type]

    def leaveEvent(self, event: object) -> None:
        """Repaint when the pointer leaves - see :meth:`enterEvent`."""
        self.update()
        super().leaveEvent(event)  # type: ignore[arg-type]

    def sizeHint(self) -> QSize:
        """A title-bar button's width, at the chrome row's full height."""
        return QSize(Chrome.WINDOW_BUTTON_WIDTH, Chrome.HEIGHT)


__all__ = ["WindowButton", "WindowButtonKind"]
