"""One step of the workflow ribbon, painted as a chevron or a tile.

Purpose:
    Render a single workflow stage as a real, scalable, clickable shape with a
    distinct appearance for each of its states, and report the width it needs
    at any candidate density so that the ribbon can decide which layout fits
    before committing to one.

Responsibilities:
    * :class:`StepGeometry` - the dimensional rules of a step, as a value
      object. Separated from the widget so the ribbon can ask "how wide would
      this step be one density level tighter?" without mutating anything.
    * :class:`WorkflowStep` - the widget: paints the geometry, confines the
      click target to the painted shape, and carries the accessible name,
      the tooltip and the disabled explanation.

What does NOT belong here:
    * Which step is active, which are enabled, and where in the row each one
      sits. A step knows nothing about its siblings; the ribbon owns all of
      that.
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
from omr_scanner.gui.theme import Color, Density, FontWeight, Navigator, Radius, Stroke

TILE_EXTRA_H_PADDING = 2
"""A tile has no arrow to clear, so it can afford a touch more breathing room
at the same density without the row growing."""

ELISION_SLACK = 1
"""One logical pixel of headroom added to every measured label width.

Not a fudge factor. `QFontMetrics.horizontalAdvance` sums glyph advances,
while `QFontMetrics.elidedText` asks the text engine to lay the string out and
compares against *that* - and the two disagree by up to a pixel on the last
glyph's right bearing. A step given exactly its measured advance therefore
elides: the ribbon picks the layout in which every label fits, and then the
first label is drawn one character short with an ellipsis after it.

Found by looking at a 1366-pixel screenshot, where "1. Project" rendered as
"1. Proje..." in the layout chosen precisely because all nine fitted. One
pixel per step is nine pixels across the whole ribbon, which is a price worth
paying for a row that says what it means.
"""

MENU_INDICATOR_WIDTH = 15
"""Room reserved at a step's right end for the "there is more here" caret.

Only the narrow layout's single current-step control shows one, and it is the
difference between a lone step that looks like a dead label and one that
looks like it opens something. Reserved in the width rather than painted over
the label, or the caret would sit on top of "7. Answer Key"."""

_MENU_CARET_HALF_WIDTH = 4.0
_MENU_CARET_HEIGHT = 3.0

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
        CHEVRON: Interlocking arrow. The ribbon's own shape, in every one of
            its layouts, because the steps are adjacent there and should read
            as one connected process.
        TILE: Rounded rectangle, for a step drawn on its own - the narrow
            layout's single current-step control. A chevron only means
            anything when the next chevron continues it, and there is no next
            chevron there.
    """

    CHEVRON = auto()
    TILE = auto()


@dataclass(frozen=True)
class StepGeometry:
    """The dimensional rules for drawing a step, as plain data.

    Frozen and Qt-free apart from the font metrics it is asked to measure
    against, which is what lets the ribbon evaluate every candidate layout -
    "how wide is the whole row at this density? at the next one down?" -
    before it moves a single widget. Measuring by temporarily reconfiguring
    the real widgets would emit a layout request per probe and make the
    decision depend on the order the probes ran in.

    Attributes:
        shape: :class:`StepShape`.
        density: A :class:`~omr_scanner.gui.theme.Density` level, 0 (tightest)
            to :data:`~omr_scanner.gui.theme.Density.MAXIMUM`. Clamped on
            read, so an out-of-range level is a tight or roomy step rather
            than an `IndexError` in a paint event.
        has_left_notch: Whether the left edge is notched to receive the
            previous step's point. False for the first step of a row, which
            has nothing to interlock with.
        has_right_point: Whether the right edge comes to a point.
        has_menu_indicator: Whether to reserve room for, and paint, the caret
            that says this control opens the workflow selector.
    """

    shape: StepShape
    density: int
    has_left_notch: bool
    has_right_point: bool
    has_menu_indicator: bool = False

    @property
    def arrow_depth(self) -> int:
        """How far the point projects, and how deep the notch is.

        Zero for a tile, which has neither.
        """
        if self.shape is StepShape.TILE:
            return 0
        return Density.level(self.density).arrow_depth

    @property
    def height(self) -> int:
        """The step's height - the same at every density; see `Density`."""
        return Navigator.STEP_HEIGHT

    @property
    def icon_extent(self) -> int:
        """Edge length of the step's icon."""
        return Density.level(self.density).icon_extent

    @property
    def h_padding(self) -> int:
        """Padding inside each end of the step, clear of the arrow geometry."""
        padding = Density.level(self.density).h_padding
        return padding + TILE_EXTRA_H_PADDING if self.shape is StepShape.TILE else padding

    @property
    def icon_text_gap(self) -> int:
        """Gap between the icon and the label."""
        return Density.level(self.density).icon_text_gap

    @property
    def min_label_width(self) -> int:
        """The narrowest the label itself may be drawn at this density."""
        return Density.level(self.density).min_label_width

    @property
    def left_inset(self) -> int:
        """Where the content starts, clear of the notch."""
        return self.h_padding + (self.arrow_depth if self.has_left_notch else 0)

    @property
    def right_inset(self) -> int:
        """How much room the point and the caret need to the content's right."""
        return (
            self.h_padding
            + (self.arrow_depth if self.has_right_point else 0)
            + (MENU_INDICATOR_WIDTH if self.has_menu_indicator else 0)
        )

    @property
    def chrome_width(self) -> int:
        """Everything a step needs *besides* its label: insets, icon, gap."""
        return self.left_inset + self.icon_extent + self.icon_text_gap + self.right_inset

    def width_for(self, text: str, font: QFont) -> int:
        """The width needed to show ``text`` in full at ``font``.

        Includes :data:`ELISION_SLACK`, without which "in full" is off by one
        pixel and therefore not in full at all.
        """
        return (
            self.chrome_width
            + QFontMetrics(font).horizontalAdvance(text)
            + ELISION_SLACK
        )

    def minimum_width(self) -> int:
        """The narrowest this step can be drawn and still say something.

        Its label is elided to this density's ``min_label_width``. The ribbon
        treats needing this as the signal to change layout rather than as a
        size to draw, so in practice no layout renders a step this narrow.
        """
        return self.chrome_width + self.min_label_width

    def advance_for(self, width: int) -> int:
        """How far the *next* step starts to the right of this one's left edge.

        For a chevron this is less than ``width``: consecutive chevrons
        overlap by the arrow depth, less the visible seam. That overlap is the
        whole reason a row of these reads as one process.
        """
        if self.shape is StepShape.TILE:
            return width + Navigator.STEP_GAP
        return width - self.arrow_depth + Navigator.STEP_GAP


CHEVRON_DEFAULT = StepGeometry(
    shape=StepShape.CHEVRON,
    density=Density.DEFAULT,
    has_left_notch=True,
    has_right_point=True,
)
"""A middle chevron at the default density - the shape most steps have, and
what a step is constructed with before the ribbon places it."""


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
        self._icon_name = icon_name
        self._icon = load_icon(icon_name)
        self._geometry = CHEVRON_DEFAULT
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
    def icon_name(self) -> str:
        """The bundled Lucide icon name this step was built with.

        Exposed so the narrow layout's stage selector can show the same glyph
        beside the same label, from the same asset, rather than keeping a
        second mapping of stage to icon that could drift from this one.
        """
        return self._icon_name

    @property
    def stage_icon(self) -> QIcon:
        """The loaded icon, for a control that shows this stage elsewhere."""
        return self._icon

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

        if self._geometry.has_menu_indicator:
            self._paint_menu_caret(painter, foreground)
        if self.hasFocus():
            self._paint_focus_ring(painter, path)
        painter.end()

    def _paint_menu_caret(self, painter: QPainter, colour: QColor) -> None:
        """A small downward caret at the right end, in the reserved width.

        Painted rather than loaded as an icon so it inherits the step's own
        foreground - white on the active accent, charcoal otherwise - without
        a second tinted pixmap for a shape that is three lines long.
        """
        centre_x = (
            self.width()
            - self._geometry.h_padding
            - (self._geometry.arrow_depth if self._geometry.has_right_point else 0)
            - MENU_INDICATOR_WIDTH / 2.0
        )
        centre_y = self.height() / 2.0
        pen = QPen(colour, float(Stroke.FOCUS_RING))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        caret = QPainterPath()
        caret.moveTo(centre_x - _MENU_CARET_HALF_WIDTH, centre_y - _MENU_CARET_HEIGHT / 2.0)
        caret.lineTo(centre_x, centre_y + _MENU_CARET_HEIGHT / 2.0)
        caret.lineTo(centre_x + _MENU_CARET_HALF_WIDTH, centre_y - _MENU_CARET_HEIGHT / 2.0)
        painter.drawPath(caret)

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
    "CHEVRON_DEFAULT",
    "ELISION_SLACK",
    "MENU_INDICATOR_WIDTH",
    "TILE_EXTRA_H_PADDING",
    "StepGeometry",
    "StepShape",
    "WorkflowStep",
]


