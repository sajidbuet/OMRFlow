"""One step of the workflow navigator, painted as a chevron or a tile.

Purpose:
    Render a single workflow stage as a real, scalable, clickable shape with a
    distinct appearance for each of its states, and report the width it needs
    in any candidate geometry so that the navigator can decide which layout
    fits before committing to one.

Responsibilities:
    * :class:`StepGeometry` - the dimensional rules of a step, as a value
      object. Separated from the widget so the navigator can ask "how wide
      would this step be as a tile?" without mutating anything.
    * :class:`WorkflowStep` - the widget: paints the geometry, confines the
      click target to the painted shape, and carries the accessible name,
      the tooltip and the disabled explanation.

What does NOT belong here:
    * Which step is active, which are enabled, and how many rows the row of
      them occupies. A step knows nothing about its siblings; the navigator
      owns all of that.
    * Any project or workflow logic. A step is a button.

Why `QPainterPath` rather than an SVG or a text glyph:
    The brief rules out faking the shape with ``>`` characters, and rightly:
    that cannot interlock, cannot fill, and reflows with the font. An SVG
    would need one asset per width. A path is computed from the widget's own
    size on every repaint, so it is exactly right at any width, any height and
    any device pixel ratio - Qt renders it through the same transform as the
    rest of the widget, which is what keeps the arrowheads sharp and unclipped
    at 125%, 150% and 200% scaling.

Why the hit test is the path and not the rectangle:
    Consecutive chevrons *overlap*: step N's point occupies the same
    horizontal band as step N+1's notch, which is what makes the row read as
    one connected process rather than as nine separate tiles. Two overlapping
    rectangular buttons would give the later one's rectangle priority over the
    earlier one's arrowhead, so clicking the visible point of step 4 would
    activate step 5. Testing the path makes the clickable area identical to
    the visible area.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.theme import Color, FontWeight, IconSize, Navigator, Radius, Stroke

ICON_TEXT_GAP = 8
"""Between a chevron's icon and its label, in logical pixels."""

TILE_H_PADDING = 10
TILE_ICON_TEXT_GAP = 7

_REMEASURE_EVENTS = frozenset(
    {
        QEvent.Type.FontChange,
        QEvent.Type.ApplicationFontChange,
        QEvent.Type.StyleChange,
        QEvent.Type.EnabledChange,
        QEvent.Type.PaletteChange,
    }
)
"""Changes after which a step's own width or appearance may differ.

`FontChange` is the load-bearing one: a Windows text-scaling change reaches a
widget as a font change, and a step that did not re-measure would keep the
width it computed at the old scale and clip its own label.
"""

_DISABLED_DASH_PATTERN = (3.0, 2.5)
"""Dash pattern for a disabled step's outline, in units of the pen width.

A disabled step is subdued *and* dashed. The dash is the point: colour alone
must not be what tells an operator a stage is unavailable, and a dashed edge
survives both a monochrome display and the common forms of colour blindness.
"""


class StepShape(Enum):
    """How a step draws itself.

    Attributes:
        CHEVRON: Interlocking arrow, for the one-row and two-row layouts where
            steps are adjacent and should read as a connected process.
        TILE: Rounded rectangle, for the two-column layout. A chevron only
            means anything when the next chevron continues it; in a
            two-column grid the arrow would point at a column break, so the
            geometry changes rather than being squeezed.
    """

    CHEVRON = auto()
    TILE = auto()


class StepSize(Enum):
    """The two metric sets a step can be drawn at.

    The *font* is never reduced between them - only the padding, the icon and
    the arrow depth. Shrinking text to fit is what the brief forbids, so this
    enum deliberately has no effect on type size.
    """

    REGULAR = auto()
    COMPACT = auto()


@dataclass(frozen=True)
class StepGeometry:
    """The dimensional rules for drawing a step, as plain data.

    Frozen and Qt-free apart from the font metrics it is asked to measure
    against, which is what lets the navigator evaluate every candidate layout
    - "how wide is the whole row as chevrons? as two rows? as tiles?" -
    before it moves a single widget. Measuring by temporarily reconfiguring
    the real widgets would emit a layout request per probe and make the
    decision depend on the order the probes ran in.

    Attributes:
        shape: :class:`StepShape`.
        size: :class:`StepSize`.
        has_left_notch: Whether the left edge is notched to receive the
            previous step's point. False for the first step of a row, which
            has nothing to interlock with.
        has_right_point: Whether the right edge comes to a point.
    """

    shape: StepShape
    size: StepSize
    has_left_notch: bool
    has_right_point: bool

    @property
    def arrow_depth(self) -> int:
        """How far the point projects, and how deep the notch is.

        Zero for a tile, which has neither.
        """
        if self.shape is StepShape.TILE:
            return 0
        return (
            Navigator.ARROW_DEPTH
            if self.size is StepSize.REGULAR
            else Navigator.ARROW_DEPTH_COMPACT
        )

    @property
    def height(self) -> int:
        """The step's height."""
        return (
            Navigator.STEP_HEIGHT
            if self.size is StepSize.REGULAR
            else Navigator.STEP_HEIGHT_COMPACT
        )

    @property
    def icon_extent(self) -> int:
        """Edge length of the step's icon."""
        if self.shape is StepShape.TILE or self.size is StepSize.COMPACT:
            return IconSize.NAV_STEP_COMPACT
        return IconSize.NAV_STEP

    @property
    def h_padding(self) -> int:
        """Padding inside each end of the step, clear of the arrow geometry."""
        return TILE_H_PADDING if self.shape is StepShape.TILE else Navigator.STEP_H_PADDING

    @property
    def icon_text_gap(self) -> int:
        """Gap between the icon and the label."""
        return TILE_ICON_TEXT_GAP if self.shape is StepShape.TILE else ICON_TEXT_GAP

    @property
    def left_inset(self) -> int:
        """Where the content starts, clear of the notch."""
        return self.h_padding + (self.arrow_depth if self.has_left_notch else 0)

    @property
    def right_inset(self) -> int:
        """How much room the point needs to the right of the content."""
        return self.h_padding + (self.arrow_depth if self.has_right_point else 0)

    @property
    def chrome_width(self) -> int:
        """Everything a step needs *besides* its label: insets, icon, gap."""
        return self.left_inset + self.icon_extent + self.icon_text_gap + self.right_inset

    def width_for(self, text: str, font: QFont) -> int:
        """The width needed to show ``text`` in full at ``font``."""
        return self.chrome_width + QFontMetrics(font).horizontalAdvance(text)

    def minimum_width(self) -> int:
        """The narrowest this step can be drawn and still say something.

        Its label is elided to :data:`Navigator.MIN_LABEL_WIDTH`. The
        navigator treats needing this as the signal to change layout, so in
        practice only the last-resort scrolling layout draws a step this
        narrow.
        """
        return self.chrome_width + Navigator.MIN_LABEL_WIDTH

    def advance_for(self, width: int) -> int:
        """How far the *next* step starts to the right of this one's left edge.

        For a chevron this is less than ``width``: consecutive chevrons
        overlap by the arrow depth, less the visible seam. That overlap is the
        whole reason a row of these reads as one process.
        """
        if self.shape is StepShape.TILE:
            return width + Navigator.STEP_GAP
        return width - self.arrow_depth + Navigator.STEP_GAP


CHEVRON_REGULAR = StepGeometry(
    shape=StepShape.CHEVRON, size=StepSize.REGULAR, has_left_notch=True, has_right_point=True
)
"""A middle chevron at full size - the shape most steps have in the wide
layout, and the one the navigator measures the wide layout against."""


class WorkflowStep(QAbstractButton):
    """A single workflow stage.

    Args:
        key: The stage's stable identifier, matching
            :attr:`~omr_scanner.gui.pages.catalog.WorkflowPageSpec.key`.
        number: 1-based position in the workflow, shown to the user.
        label: The stage's name.
        icon_name: A bundled Lucide icon name.
        summary: One line describing the stage, used as the accessible
            description and in the tooltip.
        parent: Optional Qt parent.

    Attributes:
        key: As passed in. The navigator routes on this.
    """

    def __init__(
        self,
        key: str,
        number: int,
        label: str,
        icon_name: str,
        summary: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self._number = number
        self._label = label
        self._summary = summary
        self._icon = load_icon(icon_name)
        self._geometry = CHEVRON_REGULAR
        self._disabled_reason = ""
        self._elided = False

        self.setObjectName(f"workflowStep_{key}")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setText(self.display_text)
        self._refresh_accessibility()

    # ------------------------------------------------------------------
    # What the step says
    # ------------------------------------------------------------------
    @property
    def display_text(self) -> str:
        """``"4. Scan"`` - the number and the label, as one string.

        The number is part of the label rather than a separate badge because
        it has to survive elision: an operator who can only read *part* of a
        step still needs to know which step it is.
        """
        return f"{self._number}. {self._label}"

    @property
    def label(self) -> str:
        """The stage's name, without its number."""
        return self._label

    @property
    def number(self) -> int:
        """The stage's 1-based position in the workflow."""
        return self._number

    @property
    def summary(self) -> str:
        """The one-line description of what this stage is for."""
        return self._summary

    @property
    def is_label_elided(self) -> bool:
        """Whether the last repaint had to shorten the label to fit.

        The navigator does not let this happen in the chevron or tile layouts
        - it changes layout instead - so a true value means the last-resort
        scrolling layout is in use, where the tooltip carries the full name.
        """
        return self._elided

    def set_disabled_reason(self, reason: str) -> None:
        """Explain, in the tooltip, why this stage cannot be opened yet.

        Args:
            reason: A sentence, or ``""`` to clear it.

        A disabled control that gives no reason is a dead end. Shown in
        addition to - never instead of - the visual disabled state.
        """
        if reason == self._disabled_reason:
            return
        self._disabled_reason = reason
        self._refresh_accessibility()

    @property
    def disabled_reason(self) -> str:
        """Why this stage cannot be opened yet, or ``""``."""
        return self._disabled_reason

    def _refresh_accessibility(self) -> None:
        self.setAccessibleName(self.display_text)
        parts = [part for part in (self._summary, self._disabled_reason) if part]
        self.setAccessibleDescription(" ".join(parts))
        tooltip = f"<b>{self.display_text}</b>"
        if self._summary:
            tooltip += f"<br>{self._summary}"
        if self._disabled_reason:
            tooltip += f"<br><i>{self._disabled_reason}</i>"
        self.setToolTip(tooltip)
        self.setStatusTip(self._summary or self.display_text)

    # ------------------------------------------------------------------
    # Geometry, set by the navigator
    # ------------------------------------------------------------------
    def apply_geometry(self, geometry: StepGeometry) -> None:
        """Adopt ``geometry``, re-measuring only if it actually changed.

        The navigator applies the same geometry to most steps on most passes;
        calling ``updateGeometry()`` on each of nine widgets regardless would
        schedule nine layout passes for no change.
        """
        if geometry == self._geometry:
            return
        self._geometry = geometry
        self.updateGeometry()
        self.update()

    @property
    def step_geometry(self) -> StepGeometry:
        """The geometry this step is currently drawing itself with."""
        return self._geometry

    def natural_width(self, geometry: StepGeometry | None = None) -> int:
        """Width needed to show the whole label, in ``geometry``.

        Args:
            geometry: The candidate geometry. Defaults to the one currently
                applied.

        This is the measurement the navigator's layout decision is built on,
        which is why it takes the geometry as an argument: the decision has to
        compare layouts this step is *not* currently drawn in.
        """
        return (geometry or self._geometry).width_for(self.display_text, self._text_font())

    def sizeHint(self) -> QSize:
        """The width this step needs to show its whole label, and its height."""
        return QSize(self.natural_width(), self._geometry.height)

    def minimumSizeHint(self) -> QSize:
        """The narrowest useful size - see :meth:`StepGeometry.minimum_width`."""
        return QSize(self._geometry.minimum_width(), self._geometry.height)

    def _text_font(self) -> QFont:
        """The label's font: the widget's own, bolder when this step is active.

        The weight change is deliberate and is not decoration. It means the
        active step is identifiable without relying on its accent fill, which
        is what stops "which stage am I on" from being a colour-only signal.

        Measured against the *checked* weight in both states, because a width
        that changed when a step became active would make the row's geometry
        shift on every navigation - which the brief explicitly rules out.
        """
        font = QFont(self.font())
        font.setWeight(QFont.Weight(FontWeight.SEMIBOLD))
        return font

    # ------------------------------------------------------------------
    # Hit testing
    # ------------------------------------------------------------------
    def hitButton(self, pos: QPoint) -> bool:
        """Whether ``pos`` is inside the painted shape.

        See the module docstring: overlapping chevrons make the default
        rectangular test wrong, not merely imprecise.
        """
        return self._shape_path().contains(QPointF(pos))

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def _shape_path(self) -> QPainterPath:
        """The step's outline, in widget coordinates."""
        if self._geometry.shape is StepShape.TILE:
            path = QPainterPath()
            path.addRoundedRect(
                QRect(0, 0, self.width() - 1, self.height() - 1).toRectF(),
                float(Radius.MD),
                float(Radius.MD),
            )
            return path
        return self._chevron_path()

    def _chevron_path(self) -> QPainterPath:
        """The interlocking arrow.

        Built as one explicit polygon rather than as a union of a rectangle
        and a triangle: a union leaves a hairline seam where the two fills
        meet, which on an accented step shows as a pale line down its middle.
        """
        depth = float(self._geometry.arrow_depth)
        mid = (self.height() - 1) / 2.0
        right = float(self.width()) - 1.0
        bottom = float(self.height()) - 1.0
        # A flat end still gets a small radius, so the row's outer corners are
        # not sharp against the band behind them.
        radius = float(Radius.SM)

        path = QPainterPath()
        path.moveTo(0.0 if self._geometry.has_left_notch else radius, 0.0)

        if self._geometry.has_right_point:
            path.lineTo(right - depth, 0.0)
            path.lineTo(right, mid)
            path.lineTo(right - depth, bottom)
        else:
            path.lineTo(right - radius, 0.0)
            path.quadTo(right, 0.0, right, radius)
            path.lineTo(right, bottom - radius)
            path.quadTo(right, bottom, right - radius, bottom)

        if self._geometry.has_left_notch:
            path.lineTo(0.0, bottom)
            path.lineTo(depth, mid)
        else:
            path.lineTo(radius, bottom)
            path.quadTo(0.0, bottom, 0.0, bottom - radius)
            path.lineTo(0.0, radius)
            path.quadTo(0.0, 0.0, radius, 0.0)

        path.closeSubpath()
        return path

    def _fill_colour(self) -> QColor:
        if not self.isEnabled():
            return QColor(Color.SURFACE_DISABLED)
        if self.isChecked():
            return QColor(Color.PRIMARY_PRESSED if self.isDown() else Color.PRIMARY)
        if self.isDown():
            return QColor(Color.SURFACE_PRESSED)
        if self.underMouse():
            return QColor(Color.SURFACE_MUTED)
        return QColor(Color.SURFACE_SUNKEN)

    def _foreground_colour(self) -> QColor:
        if not self.isEnabled():
            return QColor(Color.TEXT_DISABLED)
        if self.isChecked():
            return QColor(Color.TEXT_ON_PRIMARY)
        return QColor(Color.TEXT_PRIMARY)

    def _border_pen(self) -> QPen:
        if not self.isEnabled():
            pen = QPen(QColor(Color.BORDER_STRONG), float(Stroke.BORDER))
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern(list(_DISABLED_DASH_PATTERN))
            return pen
        if self.isChecked():
            return QPen(QColor(Color.PRIMARY), float(Stroke.BORDER))
        return QPen(QColor(Color.BORDER), float(Stroke.BORDER))

    def paintEvent(self, _event: object) -> None:
        """Draw the shape, then the icon, then the label."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        path = self._shape_path()
        painter.fillPath(path, self._fill_colour())
        painter.strokePath(path, self._border_pen())

        foreground = self._foreground_colour()
        left = self._geometry.left_inset
        content_width = max(self.width() - left - self._geometry.right_inset, 0)
        extent = self._geometry.icon_extent

        icon_rect = QRect(left, (self.height() - extent) // 2, extent, extent)
        self._paint_icon(painter, icon_rect, foreground)

        text_left = icon_rect.right() + 1 + self._geometry.icon_text_gap
        text_width = max(left + content_width - text_left, 0)
        font = QFont(self.font())
        if self.isChecked():
            font.setWeight(QFont.Weight(FontWeight.SEMIBOLD))
        painter.setFont(font)
        painter.setPen(foreground)
        shown = QFontMetrics(font).elidedText(
            self.display_text, Qt.TextElideMode.ElideRight, text_width
        )
        self._elided = shown != self.display_text
        painter.drawText(
            QRect(text_left, 0, text_width, self.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            shown,
        )

        if self.hasFocus():
            self._paint_focus_ring(painter, path)
        painter.end()

    def _paint_icon(self, painter: QPainter, rect: QRect, colour: QColor) -> None:
        """Render the Lucide glyph in ``colour``.

        The bundled SVGs stroke with ``currentColor``, which Qt resolves from
        the icon's own palette rather than from the painter's pen, so the
        colour has to be applied by masking the rendered pixmap. This is what
        lets one asset serve a white glyph on the active accent and a charcoal
        glyph on the inactive fill, instead of shipping two of each.
        """
        ratio = self.devicePixelRatioF()
        mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
        pixmap = self._icon.pixmap(rect.size() * ratio, ratio, mode)
        if pixmap.isNull():  # pragma: no cover - a missing asset raises at load
            return
        tint_painter = QPainter(pixmap)
        tint_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint_painter.fillRect(pixmap.rect(), colour)
        tint_painter.end()
        painter.drawPixmap(rect, pixmap)

    def _paint_focus_ring(self, painter: QPainter, path: QPainterPath) -> None:
        """Outline the shape so keyboard focus is unmistakable.

        Two strokes: a light halo just outside the accent ring, so the ring
        stays visible on the active step, whose fill is that same accent. A
        single-colour ring would disappear exactly where it matters most.
        """
        painter.setBrush(Qt.BrushStyle.NoBrush)
        halo = QPen(QColor(Color.FOCUS_HALO), float(Stroke.FOCUS_RING) + 1.5)
        halo.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(halo)
        painter.drawPath(path)
        ring = QPen(QColor(Color.FOCUS), float(Stroke.FOCUS_RING))
        ring.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(ring)
        painter.drawPath(path)

    # ------------------------------------------------------------------
    # Repaint triggers a checkable QAbstractButton does not give us
    # ------------------------------------------------------------------
    def enterEvent(self, event: QEnterEvent) -> None:
        """Repaint on hover.

        `QAbstractButton` repaints its own rectangle on hover, which for a
        chevron is the wrong region - the notch and the neighbouring point
        both fall inside it. Repainting the whole widget is correct and, for a
        44-pixel-tall shape, not worth optimising.
        """
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """Repaint when the pointer leaves - see :meth:`enterEvent`."""
        self.update()
        super().leaveEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        """Re-measure when the font, the style or the enabled state changes."""
        super().changeEvent(event)
        if event.type() in _REMEASURE_EVENTS:
            self.updateGeometry()
            self.update()


__all__ = [
    "CHEVRON_REGULAR",
    "ICON_TEXT_GAP",
    "TILE_H_PADDING",
    "StepGeometry",
    "StepShape",
    "StepSize",
    "WorkflowStep",
]
