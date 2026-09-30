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
    * :class:`FieldLane` - one response group outlined as a whole, for the
      Resolve stage's "this position needs a person / a person decided this"
      display. See :mod:`omr_scanner.gui.review.lanes`, which builds them.

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
from dataclasses import dataclass
from enum import StrEnum
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
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QStyleOptionGraphicsItem,
    QWidget,
)

from omr_scanner.domain.scan_quality import ScanQualityStatus
from omr_scanner.gui.theme.tokens import Color

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.scan_quality import ScanQualityAssessment
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

_SCAN_QUALITY = QColor(230, 120, 0)
"""Amber: this region's template-to-paper mapping is in doubt."""

_SCAN_QUALITY_SEVERE = QColor(190, 30, 140)
"""Magenta: the same, where what is in doubt is what identifies the script.

Deliberately not the red the overlay already uses for a multiply-marked bubble.
Red there means "this value is disputed"; the whole point of a scan-quality
finding is that it is *not* a dispute about a value, and re-using the colour
would merge the two categories the feature exists to keep apart."""

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

LANE_UNRESOLVED_COLOR = QColor(Color.ATTENTION)
"""Amber: this response position is waiting for a person to decide it.

From the design system, so that the outline on the sheet and the value buttons
for the symbols the engine read are the same amber by construction rather than
by two people having typed the same hex twice."""

LANE_MANUAL_COLOR = QColor(Color.PRIMARY)
"""The application accent, red: a person supplied or overrode this value.

Taken from the design system rather than written out again, because the accent
already means "this is the thing you are acting on" everywhere else in the
application, and a second hand-typed red would be a second thing to keep in
step.

Deliberately *not* :data:`MULTIPLE_COLOR`. That red means "the paper carries
more than one mark"; this one means "a human decided", which is the opposite
kind of statement - and the lane it outlines is drawn in a different style, so
the distinction survives a monochrome print and a colour-blind reader."""

LANE_PADDING_RATIO = 0.35
"""Clearance around a lane, as a fraction of the group's mean bubble width.

Proportional rather than a pixel constant: the same overlay has to sit
correctly on a 100-question sheet's small bubbles and on a wide identifier
grid's large ones, and a fixed padding would crowd one and float around the
other. The bubble geometry itself comes from the template, projected onto the
canonical page by recognition - nothing here measures a printed bubble."""

LANE_BORDER_PX = 1.6
LANE_ACTIVE_BORDER_PX = 3.0
"""Lane outline widths, cosmetic - constant on screen at any zoom, so a lane
stays visible when the reviewer zooms out and does not swallow the bubbles when
they zoom in. The active lane is the wider one, which is the second way (after
the dash pattern) that state is carried other than by colour."""

LANE_CHOICE_RING_PX = 3.2
"""The ring drawn round the bubble a reviewer chose. Heavier than every
template outline on the page on purpose: the whole point of the manual overlay
is that "which value did they pick" is answerable by looking, not by reading
the decision panel."""

LANE_MACHINE_RING_PX = 2.0
"""The ring drawn round a bubble the *engine* read as marked.

Deliberately lighter than :data:`LANE_CHOICE_RING_PX` and dashed rather than
solid. Both facts are on the page at once for a disputed position - what the
machine read, and what the person decided - and the heavier, solid, accented
one has to be the person's, because that is the value the script will carry."""

LANE_CHOICE_RING_RATIO = 0.72
"""How much larger than the printed bubble the choice ring is drawn, so it
reads as an annotation around the mark rather than as another bubble."""

NEUTRAL_OUTLINE = Color.BORDER_STRONG
"""The colour zones and bubbles wear when status colouring is turned off.

The Scan page colours every zone and bubble by what recognition made of it -
green for resolved, red for a multiple, amber for uncertain - which is exactly
right when the question is *what happened to this sheet*. On the Resolve stage
it is not: there, red means "a person decided this" and amber means "the
machine read this mark", and a zone outlined red because its status is
``multiple`` puts a third meaning on the same colour in the same rectangle.

Turning the status palette off leaves the geometry visible - a reviewer still
sees the field boundary and every printed bubble - while the only coloured
things on the page are the two the decision is about."""

FOCUS_MARGIN_RATIO = 0.22
"""Breathing room around a framed region, as a fraction of its longest side.

Enough that the reviewer sees what the disputed position is *next to* - a
roll-number column is judged against its neighbours, not in isolation - and
little enough that the bubbles stay large. Proportional rather than a pixel
constant for the same reason as :data:`LANE_PADDING_RATIO`."""

LANE_TINT_ALPHA = 28
"""Alpha of a lane's translucent fill. Faint on purpose - the reviewer is
judging graphite against paper underneath it, and a wash heavy enough to be
obvious is heavy enough to change that judgement."""

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


class LaneState(StrEnum):
    """What a highlighted response group is saying."""

    UNRESOLVED = "unresolved"
    """Nobody has decided this position yet."""

    PENDING = "pending"
    """A reviewer has picked a value but has not committed it.

    Drawn in the manual colour so the preview answers "what did I just choose",
    and in the *unresolved* line style so it cannot be mistaken for a decision
    that has been recorded. Nothing has been written to the ledger while a lane
    is in this state."""

    MANUAL = "manual"
    """A named reviewer supplied or overrode the value here, and it is stored."""

    @property
    def is_manual(self) -> bool:
        """Whether a person, rather than the machine, decided this value."""
        return self in (LaneState.PENDING, LaneState.MANUAL)


@dataclass(frozen=True, slots=True)
class LaneMark:
    """One bubble called out inside a lane, in canonical pixels.

    Attributes:
        x / y: The bubble's measured centre.
        width / height: Its printed size.
        label: The symbol it stands for, for an accessible description.
    """

    x: float
    y: float
    width: float
    height: float
    label: str = ""


@dataclass(frozen=True, slots=True)
class FieldLane:
    """One response group outlined as a whole, in canonical page pixels.

    The unit a reviewer actually works in. A conflict is about a *printed
    position* - one column of a roll number, one position of a set code - and
    that position is a vertical stack of ten bubbles, not one of them. An
    earlier build marked the single bubble the engine nearly chose with a ``?``,
    which told a reviewer where the machine's doubt landed rather than which
    part of the field needed their attention; every rectangle here is the whole
    group.

    Attributes:
        x / y / width / height: The group's extent on the canonical page,
            including :data:`LANE_PADDING_RATIO` clearance. Derived from the
            bubbles recognition measured, which are the template's own geometry
            projected onto the page - never a pixel constant.
        state: Unresolved, chosen but not committed, or decided by a person.
        active: Whether this is the conflict currently being reviewed. Drawn
            more heavily; the others stay visible so that a field with three
            doubtful positions shows three.
        machine_marks: The bubbles the engine actually read as marked in this
            group. Ringed in the unresolved colour, so a reviewer can see *what
            the machine saw* on the paper rather than only being told about it
            in a panel - and so a double mark shows as two rings rather than as
            the string ``"0-5"``.
        choice: The bubble a reviewer chose, when they chose one. Ringed more
            heavily and in the manual colour, so the preview answers both
            "which position was edited" and "what value was put there".
        note: A short caption drawn beside the lane - ``"BLANK"`` for a
            reviewer who decided the position carries no mark, or the value
            itself where no single bubble stands for it (a whole identifier
            typed into the free-text box). Never the only signal that a lane is
            manual; the outline already says that.
    """

    x: float
    y: float
    width: float
    height: float
    state: LaneState = LaneState.UNRESOLVED
    active: bool = False
    machine_marks: tuple[LaneMark, ...] = ()
    choice: LaneMark | None = None
    note: str = ""

    @property
    def has_choice(self) -> bool:
        """Whether a specific bubble inside this lane was chosen."""
        return self.choice is not None


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
        self._lanes: tuple[FieldLane, ...] = ()
        self._doubtful_zone_ids: frozenset[str] = frozenset()
        self._doubtful_is_severe = False
        self.show_zones = True
        self.show_bubbles = True
        self.show_empty_bubbles = False
        self.show_markers = False
        self.show_sample_windows = False
        self.show_centers = False
        self.show_scan_quality = True
        self.show_status_symbols = True
        self.show_status_colors = True
        self.show_lanes = True
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

    def set_lanes(self, lanes: Sequence[FieldLane]) -> None:
        """Replace the highlighted response groups.

        Empty for every page outside conflict review, which is why the Scan
        page is unaffected by this layer existing: nothing draws a lane it was
        not given one for.
        """
        self._lanes = tuple(lanes)
        self.update()

    def set_scan_quality(self, assessment: ScanQualityAssessment | None) -> None:
        """Mark the zones whose geometry the page-geometry check doubted.

        Drawn only when there is something to draw. A clean sheet - which is
        almost every sheet - gets no extra ink at all, because an overlay that
        decorates every page teaches the operator to stop seeing it.
        """
        if assessment is None or not assessment.needs_attention:
            self._doubtful_zone_ids = frozenset()
            self._doubtful_is_severe = False
        else:
            self._doubtful_zone_ids = frozenset(assessment.affected_zone_ids)
            self._doubtful_is_severe = (
                assessment.status is ScanQualityStatus.UNUSABLE
            )
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
        if self.show_scan_quality:
            self._paint_scan_quality(painter)
        if self.show_lanes:
            self._paint_lanes(painter)

    def _paint_lanes(self, painter: QPainter) -> None:
        """Outline each highlighted response group, and any chosen bubble in it.

        Drawn last, over everything else: a lane is the one thing on this
        overlay that says *a person has to act*, and a zone rectangle crossing
        it would be the thing a reviewer noticed first.

        The inactive lanes are drawn before the active one so that overlapping
        neighbours - two doubtful columns of the same roll number are adjacent
        by construction - cannot hide the one being decided.
        """
        for lane in sorted(self._lanes, key=lambda item: item.active):
            self._paint_lane(painter, lane)

    def _paint_lane(self, painter: QPainter, lane: FieldLane) -> None:
        """Draw one lane: its outline, its tint, what was read, what was chosen.

        Three things are said, and each is said twice - once in colour and once
        in line style - so none of them depends on a reviewer distinguishing
        amber from red:

        ====================== ============== ================================
        what                   colour         style
        ====================== ============== ================================
        position needs a human amber          dashed lane outline
        machine read this mark amber          medium dashed ring on the bubble
        the operator chose it  accent red     heavy solid ring on the bubble
        chosen, not yet saved  accent red     dashed lane outline
        ====================== ============== ================================
        """
        manual = lane.state.is_manual
        colour = QColor(LANE_MANUAL_COLOR if manual else LANE_UNRESOLVED_COLOR)
        pen = QPen(colour, LANE_ACTIVE_BORDER_PX if lane.active else LANE_BORDER_PX)
        pen.setCosmetic(True)
        # Solid only for a decision that has been **recorded**. A pending
        # choice is drawn in the manual colour but the unresolved line style,
        # so the preview can show what the reviewer has picked without claiming
        # it has been saved.
        pen.setStyle(
            Qt.PenStyle.SolidLine
            if lane.state is LaneState.MANUAL
            else Qt.PenStyle.DashLine
        )
        painter.setPen(pen)

        tint = QColor(colour)
        tint.setAlpha(LANE_TINT_ALPHA)
        painter.setBrush(QBrush(tint))
        painter.drawRect(QRectF(lane.x, lane.y, lane.width, lane.height))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        for mark in lane.machine_marks:
            self._paint_lane_mark(
                painter,
                mark,
                QColor(LANE_UNRESOLVED_COLOR),
                width=LANE_MACHINE_RING_PX,
                style=Qt.PenStyle.DashLine,
            )
        if lane.choice is not None:
            self._paint_lane_mark(
                painter,
                lane.choice,
                colour,
                width=LANE_CHOICE_RING_PX,
                style=Qt.PenStyle.SolidLine,
                fill=True,
            )
        if lane.note:
            self._paint_lane_note(painter, lane, colour)

    @staticmethod
    def _paint_lane_mark(
        painter: QPainter,
        mark: LaneMark,
        colour: QColor,
        *,
        width: float,
        style: Qt.PenStyle,
        fill: bool = False,
    ) -> None:
        """Ring one bubble, larger than the bubble itself.

        Larger on purpose: the ring has to read as an annotation *about* the
        mark rather than as another printed bubble, and it must not cover the
        graphite the reviewer is judging.
        """
        grow_x = mark.width * LANE_CHOICE_RING_RATIO / 2.0
        grow_y = mark.height * LANE_CHOICE_RING_RATIO / 2.0
        pen = QPen(colour, width)
        pen.setCosmetic(True)
        pen.setStyle(style)
        painter.setPen(pen)
        if fill:
            tint = QColor(colour)
            tint.setAlpha(LANE_TINT_ALPHA * 2)
            painter.setBrush(QBrush(tint))
        painter.drawEllipse(
            QRectF(
                mark.x - mark.width / 2.0 - grow_x,
                mark.y - mark.height / 2.0 - grow_y,
                mark.width + grow_x * 2.0,
                mark.height + grow_y * 2.0,
            )
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)

    @staticmethod
    def _paint_lane_note(painter: QPainter, lane: FieldLane, colour: QColor) -> None:
        """Write a lane's caption beside it - ``BLANK``, or a typed value.

        Beside rather than across: a reviewer deciding that a position carries
        no mark still has to be able to see that it carries no mark.
        """
        font = QFont(painter.font())
        font.setBold(True)
        font.setPointSizeF(max(lane.width * 0.45, 9.0))
        painter.setFont(font)
        painter.setPen(QPen(colour))
        painter.drawText(
            QPointF(lane.x, lane.y - max(lane.width * 0.12, 3.0)), lane.note
        )

    def _paint_scan_quality(self, painter: QPainter) -> None:
        """Outline the regions whose template-to-paper mapping is in doubt.

        A hatched fill rather than another coloured border: the zone outlines
        already carry a colour each and a status tint, and a seventh border
        colour would be one distinction too many to read at a glance. Hatching
        says "do not trust what is under here" without competing with them.
        """
        if not self._doubtful_zone_ids:
            return
        colour = QColor(_SCAN_QUALITY_SEVERE if self._doubtful_is_severe else _SCAN_QUALITY)
        brush = QBrush(colour, Qt.BrushStyle.BDiagPattern)
        pen = QPen(colour, 2.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(brush)
        for zone in self._zones:
            if zone.zone_id not in self._doubtful_zone_ids:
                continue
            painter.drawRect(QRectF(zone.x, zone.y, zone.width, zone.height))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_zones(self, painter: QPainter) -> None:
        """Outline each zone in its template colour, tinted by its status."""
        for zone in self._zones:
            status_color = (
                _STATUS_COLORS.get(zone.status) if self.show_status_colors else None
            )
            base = QColor(zone.color if self.show_status_colors else NEUTRAL_OUTLINE)
            pen = QPen(status_color if status_color is not None else base, 3.0)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(zone.x, zone.y, zone.width, zone.height))

            label = zone.label
            symbol = _STATUS_SYMBOLS.get(zone.status, "") if self.show_status_symbols else ""
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

            color = (
                _STATUS_COLORS.get(bubble.group_status, EMPTY_COLOR)
                if self.show_status_colors
                else QColor(NEUTRAL_OUTLINE)
            )
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

            symbol = (
                _STATUS_SYMBOLS.get(bubble.group_status, "")
                if self.show_status_symbols
                else ""
            )
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
        self._focus_rect: QRectF | None = None
        self._refitting = False

        self._pan_active = False
        self._pan_last: QPoint | None = None
        self._pan_button: Qt.MouseButton | None = None
        self._right_press: QPoint | None = None
        self._space_panning = False

        # Shown only when there is no page *and* the caller has said why. A
        # view with nothing in it is otherwise an unexplained grey rectangle,
        # which on the Resolve stage is exactly the wrong thing to hand
        # somebody: a sheet that would not register has no rectified page by
        # definition, and the reviewer needs to be told that rather than left
        # to wonder whether the application is still loading.
        self._placeholder = QLabel("", self)
        self._placeholder.setObjectName("previewPlaceholder")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._placeholder.setVisible(False)

    # ------------------------------------------------------------------
    # Content
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Remove the image and the overlay, leaving an empty view."""
        if self._background is not None:
            self._scene.removeItem(self._background)
            self._background = None
        self._overlay.set_content((), ())
        self._overlay.set_lanes(())
        self._overlay.set_scan_quality(None)
        self._page_size = (0, 0)
        self._scene.setSceneRect(QRectF(0, 0, 1, 1))
        self._update_placeholder()
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

        self._update_placeholder()
        self.viewport().update()

    def set_overlay(
        self,
        zones: Sequence[ZoneView],
        bubbles: Sequence[BubbleView],
        markers: Sequence[MarkerView] = (),
    ) -> None:
        """Replace the overlay content."""
        self._overlay.set_content(zones, bubbles, markers)

    def set_lanes(self, lanes: Sequence[FieldLane]) -> None:
        """Highlight whole response groups needing, or carrying, a decision."""
        self._overlay.set_lanes(lanes)

    def set_scan_quality(self, assessment: ScanQualityAssessment | None) -> None:
        """Show which regions the page-geometry check could not vouch for."""
        self._overlay.set_scan_quality(assessment)

    def set_overlay_visible(
        self,
        *,
        zones: bool,
        bubbles: bool,
        empty: bool,
        markers: bool = False,
        sample_windows: bool = False,
        centers: bool = False,
        scan_quality: bool = True,
        status_symbols: bool = True,
        status_colors: bool = True,
        lanes: bool = True,
    ) -> None:
        """Choose which overlay layers are drawn.

        ``status_symbols`` turns off the ``?``/``!``/``x`` glyphs. The Scan page
        keeps them - they are how an operator skimming a whole sheet sees what
        was wrong with it - while conflict review turns them off, because there
        the same fact is carried by a lane outline around the entire group
        rather than by a mark beside one bubble.
        """
        self._overlay.show_zones = zones
        self._overlay.show_bubbles = bubbles
        self._overlay.show_empty_bubbles = empty
        self._overlay.show_markers = markers
        self._overlay.show_sample_windows = sample_windows
        self._overlay.show_centers = centers
        self._overlay.show_scan_quality = scan_quality
        self._overlay.show_status_symbols = status_symbols
        self._overlay.show_status_colors = status_colors
        self._overlay.show_lanes = lanes
        self._overlay.update()

    def set_placeholder(self, message: str) -> None:
        """Say why this view is empty, or clear the explanation.

        Args:
            message: What to show while there is no page; ``""`` to show
                nothing.

        Never covers an image: the moment a page is set the message is hidden
        again, so a caller can leave one in place without having to remember to
        take it down.
        """
        self._placeholder.setText(message)
        self._update_placeholder()

    def _update_placeholder(self) -> None:
        """Position the placeholder over the viewport and show or hide it."""
        self._placeholder.setGeometry(self.viewport().geometry())
        self._placeholder.setVisible(bool(self._placeholder.text()) and not self.has_page)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the placeholder centred and the framed region framed."""
        super().resizeEvent(event)
        self._update_placeholder()
        self._apply_focus()

    def showEvent(self, event: QShowEvent) -> None:
        """Re-frame on becoming visible.

        A view on a tab that has never been shown has no meaningful viewport
        size, so the framing computed while it was hidden was arithmetic
        against a placeholder rectangle. This is where it becomes real.
        """
        super().showEvent(event)
        self._update_placeholder()
        self._apply_focus()

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
        self.clear_focus()
        self._apply_zoom(self._zoom * ZOOM_STEP)

    def zoom_out(self) -> None:
        """Zoom out one step."""
        self.clear_focus()
        self._apply_zoom(self._zoom / ZOOM_STEP)

    def zoom_to_actual_size(self) -> None:
        """Show the page at 100 per cent."""
        self.clear_focus()
        self._apply_zoom(1.0)

    def set_zoom(self, factor: float) -> None:
        """Zoom to ``factor``, clamped to the view's own limits.

        The public form of an absolute zoom, for a caller that has computed the
        magnification it wants. Clamping happens here, so a caller never has to
        know :data:`MIN_ZOOM` and :data:`MAX_ZOOM`.
        """
        self.clear_focus()
        self._apply_zoom(factor)

    def fit_to_window(self) -> None:
        """Zoom so the whole page fits the viewport."""
        self.clear_focus()
        self._fit_page()

    def _fit_page(self) -> None:
        """Fit the whole page without disturbing the focus rectangle."""
        width, height = self._page_size
        if width <= 0 or height <= 0:
            return
        # Scroll-bar-independent, for the same reason as `_apply_focus`.
        viewport = self.maximumViewportSize()
        factor = min(viewport.width() / width, viewport.height() / height)
        self._apply_zoom(max(MIN_ZOOM, min(MAX_ZOOM, factor)))
        self.centerOn(width / 2.0, height / 2.0)

    # ------------------------------------------------------------------
    # Framing one region
    # ------------------------------------------------------------------
    def focus_on(self, rect: QRectF, *, margin_ratio: float = FOCUS_MARGIN_RATIO) -> None:
        """Frame one region of the page, and keep it framed.

        Args:
            rect: The region of interest, in canonical page pixels.
            margin_ratio: Breathing room around it, as a fraction of its
                longest side.

        **Sticky, which is the point.** An earlier version computed the
        magnification once, at the moment the sheet finished loading - before
        the tab had been shown and therefore before the viewport had its real
        size. The arithmetic was correct and the result was a small image
        pinned to a corner of a large empty canvas. The rectangle is now
        remembered and re-fitted whenever the view is shown or resized, so the
        framing is computed against the viewport the reviewer is actually
        looking at.

        Any manual zoom or pan clears it: once a reviewer has moved the view,
        it is theirs, and a resize must not snatch it back.
        """
        if rect.isEmpty() or not self.has_page:
            self._focus_rect = None
            self._fit_page()
            return
        # Per axis, not a single margin from the longest side: a roll-number
        # column is four times as tall as it is wide, and one margin would
        # double its width while barely touching its height.
        self._focus_rect = rect.adjusted(
            -rect.width() * margin_ratio,
            -rect.height() * margin_ratio,
            rect.width() * margin_ratio,
            rect.height() * margin_ratio,
        )
        self._apply_focus()

    def clear_focus(self) -> None:
        """Stop re-framing a region; the view keeps whatever it shows now."""
        self._focus_rect = None

    @property
    def focus_rect(self) -> QRectF | None:
        """The region being kept in frame, or ``None``."""
        return self._focus_rect

    def _apply_focus(self) -> None:
        """Fit the remembered region to the viewport, centred and filling it.

        Sized against :meth:`maximumViewportSize` - the viewport *without*
        scroll bars - never the current viewport. Found as a real freeze on the
        Resolve stage: a region at the page's left edge (Student ID position 1)
        widened to a wide, short pane runs past the page, so one zoom showed a
        scroll bar, the narrower viewport gave a zoom that hid it, and each
        refit's resize triggered the other, forever, on the GUI thread. The
        scroll-bar-independent size gives the same zoom either way, so the
        refit is stable; the guard stops a refit re-entering itself.
        """
        rect = self._focus_rect
        if rect is None or not self.has_page or self._refitting:
            return
        viewport = self.maximumViewportSize()
        if viewport.width() <= 0 or viewport.height() <= 0:
            return
        shown = self._widened_to_viewport(rect, viewport.width(), viewport.height())
        factor = min(
            viewport.width() / max(shown.width(), 1.0),
            viewport.height() / max(shown.height(), 1.0),
        )
        self._refitting = True
        try:
            self._apply_zoom(factor)
            self.centerOn(shown.center())
        finally:
            self._refitting = False

    @staticmethod
    def _widened_to_viewport(
        rect: QRectF, view_width: int, view_height: int
    ) -> QRectF:
        """Grow ``rect`` to the viewport's shape, without ever shrinking it.

        The reason the zoomed field used to sit in a sea of empty canvas. A
        roll-number position is a tall, narrow region; the pane it is shown in
        is wide and short. Fitting one inside the other preserving aspect - the
        obvious thing, and what the previous code did - fills the height and
        leaves two thirds of the width blank.

        Growing the region to the pane's proportions first means the same
        magnification is reached and the spare width is spent on *more of the
        sheet*: the neighbouring columns a reviewer compares against. The
        region is only ever enlarged, so nothing that had to be visible stops
        being visible.
        """
        wanted = view_width / max(view_height, 1)
        have = rect.width() / max(rect.height(), 1.0)
        if have < wanted:
            grow = rect.height() * wanted - rect.width()
            return rect.adjusted(-grow / 2.0, 0.0, grow / 2.0, 0.0)
        grow = rect.width() / wanted - rect.height()
        return rect.adjusted(0.0, -grow / 2.0, 0.0, grow / 2.0)

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
    "FOCUS_MARGIN_RATIO",
    "LANE_MANUAL_COLOR",
    "LANE_PADDING_RATIO",
    "LANE_UNRESOLVED_COLOR",
    "MARKER_MISMATCH_PX",
    "MULTIPLE_COLOR",
    "SAMPLE_WINDOW_COLOR",
    "SELECTED_COLOR",
    "UNCERTAIN_COLOR",
    "UNREADABLE_COLOR",
    "FieldLane",
    "LaneMark",
    "LaneState",
    "OverlayItem",
    "ScanPreviewView",
]
