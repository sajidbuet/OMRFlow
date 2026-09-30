"""Small, dependency-free charts for the Results dashboard.

Why hand-drawn with ``QPainter``:
    The frozen Windows build deliberately excludes ``PySide6.QtCharts`` (see
    ``packaging/omrflow.spec``), and a browser widget for five bar charts
    would be a far heavier dependency than the charts themselves. What is
    needed - bars, a box plot, guide lines, hover and click - is a few hundred
    lines of painting, in logical pixels, so it scales with Windows display
    scaling the way every other widget does.

What every chart shares (:class:`ChartBase`):
    * Margins measured from the widget's own font, so labels are never
      clipped at 125-175% scaling.
    * Hover: the item under the pointer is highlighted and its exact values
      are shown in a tooltip - a 100-question chart labels every fifth or
      tenth question on the axis and relies on this for the rest.
    * Click: :attr:`ChartBase.activated` carries the item's index, which the
      dashboard uses to select a question.
    * An empty state drawn as a sentence rather than as empty axes.

Nothing here computes a statistic. Every value arrives already calculated by
:mod:`omr_scanner.services.result_analytics`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from omr_scanner.gui.theme import ChartColor, Color, FontSize

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from PySide6.QtGui import QMouseEvent, QPaintEvent

    from omr_scanner.services.result_analytics import BoxSummary


def nice_ticks(low: float, high: float, target: int = 5) -> list[float]:
    """Round axis ticks covering ``[low, high]``, about ``target`` of them."""
    if high <= low:
        high = low + 1
    raw = (high - low) / max(target, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(
        (s * magnitude for s in (1, 2, 2.5, 5, 10) if s * magnitude >= raw), 10 * magnitude
    )
    start = math.floor(low / step) * step
    ticks: list[float] = []
    value = start
    # Up to the first tick at or above `high`, so the tallest bar is always
    # under a labelled line rather than above the last one.
    while True:
        ticks.append(round(value, 10))
        if value >= high - step * 1e-9:
            break
        value += step
    return ticks


def format_value(value: float) -> str:
    """A tick or statistic without spurious decimals."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _small_font(font: QFont) -> QFont:
    """The widget font at the theme's secondary size."""
    small = QFont(font)
    size = small.pointSizeF()
    if size > 0:
        small.setPointSizeF(max(FontSize.MIN_POINT_SIZE, size + FontSize.SECONDARY))
    return small


class ChartBase(QWidget):
    """Axes, hover, click and the empty state; subclasses draw the data."""

    activated = Signal(int)
    """Emitted with an item's index when it is clicked."""

    MIN_HEIGHT = 200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.empty_text = "No data."
        self.hovered: int | None = None
        self.selected: int | None = None
        self.y_title = ""
        self.x_title = ""

    # -- geometry ---------------------------------------------------------
    def sizeHint(self) -> QSize:
        """Wide and moderately tall; the layout decides the width."""
        return QSize(480, max(self.MIN_HEIGHT, 240))

    def item_count(self) -> int:
        """How many hoverable items the chart has."""
        return 0

    def y_label_width(self) -> int:
        """Room for the widest y tick label."""
        return QFontMetrics(_small_font(self.font())).horizontalAdvance("0000") + 6

    def plot_rect(self) -> QRectF:
        """The data area, inside the axis labels."""
        metrics = QFontMetrics(_small_font(self.font()))
        line = metrics.height()
        left = self.y_label_width() + (line + 4 if self.y_title else 0) + 4
        bottom = line + 8 + (line + 2 if self.x_title else 0)
        return QRectF(left, line // 2 + 6, self.width() - left - 10, self.height() - bottom - 12)

    # -- interaction ------------------------------------------------------
    def index_at(self, point: QPointF) -> int | None:
        """The item under ``point``, if any. Subclasses override."""
        del point
        return None

    def tooltip_for(self, index: int) -> str:
        """The exact values of one item. Subclasses override."""
        del index
        return ""

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Highlight and describe the item under the pointer."""
        index = self.index_at(event.position())
        if index != self.hovered:
            self.hovered = index
            self.update()
        if index is None:
            QToolTip.hideText()
        else:
            QToolTip.showText(event.globalPosition().toPoint(), self.tooltip_for(index), self)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: object) -> None:
        """Drop the highlight when the pointer leaves."""
        self.hovered = None
        self.update()
        super().leaveEvent(event)  # type: ignore[arg-type]

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Clicking an item activates it."""
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.index_at(event.position())
            if index is not None:
                self.activated.emit(index)
        super().mousePressEvent(event)

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:
        """Draw the empty state or the chart."""
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(Color.SURFACE))
        if self.item_count() == 0:
            painter.setPen(QColor(Color.TEXT_TERTIARY))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self.empty_text,
            )
        else:
            painter.setFont(_small_font(self.font()))
            self.draw(painter, self.plot_rect())
        painter.end()

    def draw(self, painter: QPainter, plot: QRectF) -> None:
        """Draw the data. Subclasses override."""

    def draw_y_axis(
        self, painter: QPainter, plot: QRectF, low: float, high: float, ticks: Sequence[float],
        *, suffix: str = "",
    ) -> None:
        """Grid lines and labels for a value axis spanning ``[low, high]``."""
        metrics = painter.fontMetrics()
        for tick in ticks:
            if tick < low - 1e-9 or tick > high + 1e-9:
                continue
            y = self.y_for(plot, tick, low, high)
            painter.setPen(QPen(QColor(ChartColor.GRID), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(Color.TEXT_SECONDARY))
            text = format_value(tick) + suffix
            width = metrics.horizontalAdvance(text)
            painter.drawText(
                QPointF(plot.left() - width - 6, y + metrics.ascent() / 2 - 1), text
            )
        painter.setPen(QPen(QColor(ChartColor.AXIS), 1))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        if self.y_title:
            painter.save()
            painter.setPen(QColor(Color.TEXT_SECONDARY))
            painter.translate(metrics.height() / 2 + 2, plot.center().y())
            painter.rotate(-90)
            width = metrics.horizontalAdvance(self.y_title)
            painter.drawText(QPointF(-width / 2, metrics.ascent() / 2), self.y_title)
            painter.restore()
        if self.x_title:
            painter.setPen(QColor(Color.TEXT_SECONDARY))
            width = metrics.horizontalAdvance(self.x_title)
            painter.drawText(
                QPointF(plot.center().x() - width / 2, self.height() - 4), self.x_title
            )

    @staticmethod
    def y_for(plot: QRectF, value: float, low: float, high: float) -> float:
        """The pixel row of a value."""
        span = high - low or 1.0
        return plot.bottom() - (value - low) / span * plot.height()


# ----------------------------------------------------------------------
# Marks histogram
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HistogramBar:
    """One bin, as the chart needs it."""

    lower: float
    upper: float
    count: int
    tooltip: str


class HistogramChart(ChartBase):
    """Students per mark range, with the mean and median marked."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardHistogram")
        self.bars: list[HistogramBar] = []
        self.mean: float | None = None
        self.median: float | None = None
        self.y_title = "Students"
        self.x_title = "Total mark"

    def set_data(
        self, bars: Sequence[HistogramBar], *, mean: float | None, median: float | None
    ) -> None:
        """Replace the bins and the markers."""
        self.bars = list(bars)
        self.mean, self.median = mean, median
        self.hovered = None
        self.update()

    def item_count(self) -> int:
        """One item per bin."""
        return len(self.bars)

    def _range(self) -> tuple[float, float]:
        return self.bars[0].lower, self.bars[-1].upper

    def _x(self, plot: QRectF, value: float) -> float:
        low, high = self._range()
        return plot.left() + (value - low) / ((high - low) or 1.0) * plot.width()

    def index_at(self, point: QPointF) -> int | None:
        """The bin under the pointer, anywhere in its column."""
        if not self.bars:
            return None
        plot = self.plot_rect()
        if not plot.contains(point):
            return None
        for index, bar in enumerate(self.bars):
            if self._x(plot, bar.lower) <= point.x() <= self._x(plot, bar.upper):
                return index
        return None

    def tooltip_for(self, index: int) -> str:
        """The bin's range, count and share."""
        return self.bars[index].tooltip

    def draw(self, painter: QPainter, plot: QRectF) -> None:
        """Bars, axes, and the mean and median lines."""
        top = max(bar.count for bar in self.bars)
        ticks = [t for t in nice_ticks(0, max(top, 1), 4) if float(t).is_integer()]
        high = max(ticks[-1] if ticks else 1, top, 1)
        self.draw_y_axis(painter, plot, 0, high, ticks)
        for index, bar in enumerate(self.bars):
            left = self._x(plot, bar.lower) + 1
            right = self._x(plot, bar.upper) - 1
            y = self.y_for(plot, bar.count, 0, high)
            colour = ChartColor.BAR_HOVER if index == self.hovered else ChartColor.BAR
            painter.fillRect(QRectF(left, y, max(right - left, 1), plot.bottom() - y),
                             QColor(colour))
        # x labels: every bin edge that fits.
        metrics = painter.fontMetrics()
        edges = [bar.lower for bar in self.bars] + [self.bars[-1].upper]
        widest = max(metrics.horizontalAdvance(format_value(e)) for e in edges) + 8
        step = max(1, math.ceil(widest / max(plot.width() / len(self.bars), 1)))
        painter.setPen(QColor(Color.TEXT_SECONDARY))
        for position, edge in enumerate(edges):
            if position % step and position != len(edges) - 1:
                continue
            text = format_value(edge)
            x = self._x(plot, edge)
            painter.drawText(
                QPointF(x - metrics.horizontalAdvance(text) / 2,
                        plot.bottom() + metrics.ascent() + 3),
                text,
            )
        self._marker(painter, plot, self.mean, "Mean", ChartColor.MEAN, Qt.PenStyle.SolidLine, 0)
        self._marker(
            painter, plot, self.median, "Median", ChartColor.MEDIAN, Qt.PenStyle.DashLine, 1
        )

    def _marker(
        self, painter: QPainter, plot: QRectF, value: float | None, name: str, colour: str,
        style: Qt.PenStyle, row: int,
    ) -> None:
        """A labelled vertical line at one statistic."""
        if value is None:
            return
        x = self._x(plot, value)
        painter.setPen(QPen(QColor(colour), 2, style))
        painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        metrics = painter.fontMetrics()
        text = f"{name} {format_value(round(value, 2))}"
        width = metrics.horizontalAdvance(text)
        left = x + 4 if x + 4 + width < plot.right() else x - 4 - width
        y = plot.top() + metrics.ascent() + row * (metrics.height() + 1)
        painter.fillRect(QRectF(left - 2, y - metrics.ascent(), width + 4, metrics.height()),
                         QColor(Color.SURFACE))
        painter.setPen(QColor(colour))
        painter.drawText(QPointF(left, y), text)


# ----------------------------------------------------------------------
# Box plots
# ----------------------------------------------------------------------
class BoxPlotChart(ChartBase):
    """One horizontal box-and-whisker per row, on a shared mark scale."""

    MIN_HEIGHT = 110

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardBoxPlot")
        self.rows: list[tuple[str, BoxSummary]] = []
        self.scale: tuple[float, float] = (0.0, 100.0)
        self.x_title = "Total mark"

    def set_data(self, rows: Sequence[tuple[str, BoxSummary]], scale: tuple[float, float]) -> None:
        """Replace the boxes; the height grows with the number of rows."""
        self.rows = list(rows)
        self.scale = scale
        line = QFontMetrics(self.font()).height()
        self.setMinimumHeight(max(self.MIN_HEIGHT, 3 * line + len(self.rows) * (2 * line + 8)))
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        """Tall enough for every row."""
        return QSize(420, self.minimumHeight())

    def item_count(self) -> int:
        """One item per box."""
        return len(self.rows)

    def y_label_width(self) -> int:
        """Room for the row labels."""
        metrics = QFontMetrics(_small_font(self.font()))
        return max((metrics.horizontalAdvance(label) for label, _ in self.rows), default=0) + 8

    def _x(self, plot: QRectF, value: float) -> float:
        low, high = self.scale
        return plot.left() + (value - low) / ((high - low) or 1.0) * plot.width()

    def _row_band(self, plot: QRectF, index: int) -> QRectF:
        height = plot.height() / max(len(self.rows), 1)
        return QRectF(plot.left(), plot.top() + index * height, plot.width(), height)

    def index_at(self, point: QPointF) -> int | None:
        """The row under the pointer."""
        plot = self.plot_rect()
        if not plot.contains(point):
            return None
        for index in range(len(self.rows)):
            if self._row_band(plot, index).contains(point):
                return index
        return None

    def tooltip_for(self, index: int) -> str:
        """The five numbers of one box."""
        label, box = self.rows[index]
        lines = [
            f"<b>{label}</b>",
            f"Minimum: {format_value(round(box.minimum, 2))}",
            f"Q1: {format_value(round(box.q1, 2))}",
            f"Median: {format_value(round(box.median, 2))}",
            f"Q3: {format_value(round(box.q3, 2))}",
            f"Maximum: {format_value(round(box.maximum, 2))}",
        ]
        if box.outliers:
            lines.append(f"Outliers (beyond 1.5 x IQR): {len(box.outliers)}")
        return "<br>".join(lines)

    def draw(self, painter: QPainter, plot: QRectF) -> None:
        """Scale, then one box per row."""
        low, high = self.scale
        metrics = painter.fontMetrics()
        for tick in nice_ticks(low, high, 5):
            if tick < low - 1e-9 or tick > high + 1e-9:
                continue
            x = self._x(plot, tick)
            painter.setPen(QPen(QColor(ChartColor.GRID), 1))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(QColor(Color.TEXT_SECONDARY))
            text = format_value(tick)
            painter.drawText(
                QPointF(x - metrics.horizontalAdvance(text) / 2,
                        plot.bottom() + metrics.ascent() + 3),
                text,
            )
        if self.x_title:
            width = metrics.horizontalAdvance(self.x_title)
            painter.drawText(QPointF(plot.center().x() - width / 2, self.height() - 4),
                             self.x_title)
        for index, (label, box) in enumerate(self.rows):
            band = self._row_band(plot, index)
            mid = band.center().y()
            half = min(band.height() * 0.3, metrics.height())
            painter.setPen(QColor(Color.TEXT_PRIMARY))
            painter.drawText(
                QPointF(plot.left() - metrics.horizontalAdvance(label) - 6,
                        mid + metrics.ascent() / 2 - 1),
                label,
            )
            outline = QColor(ChartColor.BAR_HOVER if index == self.hovered else ChartColor.BAR)
            painter.setPen(QPen(outline, 1.5))
            lw, uw = self._x(plot, box.lower_whisker), self._x(plot, box.upper_whisker)
            q1, q3 = self._x(plot, box.q1), self._x(plot, box.q3)
            painter.drawLine(QPointF(lw, mid), QPointF(q1, mid))
            painter.drawLine(QPointF(q3, mid), QPointF(uw, mid))
            painter.drawLine(QPointF(lw, mid - half / 2), QPointF(lw, mid + half / 2))
            painter.drawLine(QPointF(uw, mid - half / 2), QPointF(uw, mid + half / 2))
            painter.setBrush(QColor(ChartColor.BOX_FILL))
            painter.drawRect(QRectF(q1, mid - half, max(q3 - q1, 1.5), 2 * half))
            median = self._x(plot, box.median)
            painter.setPen(QPen(QColor(ChartColor.MEDIAN), 2.5))
            painter.drawLine(QPointF(median, mid - half), QPointF(median, mid + half))
            painter.setPen(QPen(outline, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for value in box.outliers:
                painter.drawEllipse(QPointF(self._x(plot, value), mid), 2.5, 2.5)


# ----------------------------------------------------------------------
# One bar (or stack) per question
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QuestionBar:
    """One question's bar: a value, or a stack of shares that sum to one."""

    number: int
    value: float | None
    """``None`` for "not available" - drawn as a gap, never as zero."""
    tooltip: str
    stack: tuple[float, ...] = ()
    flagged: bool = False


class QuestionChart(ChartBase):
    """Q1..Qn along the x axis, for any number of questions.

    Three modes share the axis logic: ``"percent"`` (0-100%, with the
    difficulty band lines), ``"stack"`` (correct / incorrect / multiple /
    blank shares) and ``"signed"`` (-1..1 discrimination, negative bars in
    the warning colour).
    """

    STACK_COLOURS = (
        ChartColor.CORRECT, ChartColor.INCORRECT, ChartColor.MULTIPLE, ChartColor.BLANK,
    )

    def __init__(self, mode: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.bars: list[QuestionBar] = []
        self.bands: tuple[float, ...] = ()
        self.x_title = "Question"
        self.y_title = {"percent": "% correct", "stack": "% of scripts",
                        "signed": "Discrimination"}.get(mode, "")

    def set_data(self, bars: Sequence[QuestionBar], *, bands: Sequence[float] = ()) -> None:
        """Replace the questions. ``bands`` are guide lines, as proportions."""
        self.bars = list(bars)
        self.bands = tuple(bands)
        self.hovered = None
        self.update()

    def set_selected(self, index: int | None) -> None:
        """Outline one question - the one the distractor panel shows."""
        self.selected = index
        self.update()

    def item_count(self) -> int:
        """One item per question."""
        return len(self.bars)

    def y_label_width(self) -> int:
        """Room for ``100%`` or ``-1.0``."""
        return QFontMetrics(_small_font(self.font())).horizontalAdvance("100%") + 6

    def _slot(self, plot: QRectF) -> float:
        return plot.width() / max(len(self.bars), 1)

    def index_at(self, point: QPointF) -> int | None:
        """The question column under the pointer."""
        plot = self.plot_rect()
        if not self.bars or not (plot.left() <= point.x() <= plot.right()):
            return None
        if not (plot.top() - 4 <= point.y() <= plot.bottom() + 4):
            return None
        index = int((point.x() - plot.left()) // self._slot(plot))
        return index if 0 <= index < len(self.bars) else None

    def tooltip_for(self, index: int) -> str:
        """The question's exact values."""
        return self.bars[index].tooltip

    def _range(self) -> tuple[float, float]:
        if self.mode == "signed":
            return -1.0, 1.0
        return 0.0, 1.0

    def draw(self, painter: QPainter, plot: QRectF) -> None:
        """Axis, guide lines, then the bars."""
        low, high = self._range()
        if self.mode == "signed":
            ticks = [-1.0, -0.5, 0.0, 0.5, 1.0]
            self.draw_y_axis(painter, plot, low, high, ticks)
            zero = self.y_for(plot, 0.0, low, high)
            painter.setPen(QPen(QColor(ChartColor.AXIS), 1))
            painter.drawLine(QPointF(plot.left(), zero), QPointF(plot.right(), zero))
        else:
            ticks = [0.0, 0.25, 0.5, 0.75, 1.0]
            self._draw_percent_axis(painter, plot, ticks)
        painter.setPen(QPen(QColor(ChartColor.BAND), 1, Qt.PenStyle.DashLine))
        for band in self.bands:
            y = self.y_for(plot, band, low, high)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        slot = self._slot(plot)
        gap = 1.0 if slot >= 4 else 0.0
        for index, bar in enumerate(self.bars):
            left = plot.left() + index * slot + gap / 2
            width = max(slot - gap, 1.0)
            if self.mode == "stack" and bar.stack:
                bottom = plot.bottom()
                for share, colour in zip(bar.stack, self.STACK_COLOURS, strict=False):
                    height = share * plot.height()
                    painter.fillRect(QRectF(left, bottom - height, width, height), QColor(colour))
                    bottom -= height
            elif bar.value is not None:
                if self.mode == "signed":
                    zero = self.y_for(plot, 0.0, low, high)
                    y = self.y_for(plot, bar.value, low, high)
                    colour = ChartColor.NEGATIVE if bar.value < 0 else ChartColor.BAR
                    if index == self.hovered and bar.value >= 0:
                        colour = ChartColor.BAR_HOVER
                    painter.fillRect(QRectF(left, min(y, zero), width, abs(zero - y) or 1),
                                     QColor(colour))
                else:
                    y = self.y_for(plot, bar.value, low, high)
                    colour = ChartColor.BAR_HOVER if index == self.hovered else ChartColor.BAR
                    painter.fillRect(QRectF(left, y, width, plot.bottom() - y), QColor(colour))
            if index == self.hovered and (self.mode == "stack" or bar.value is None):
                painter.fillRect(QRectF(left, plot.top(), width, plot.height()),
                                 QColor(26, 26, 26, 28))
            if index == self.selected:
                painter.setPen(QPen(QColor(ChartColor.BAR_SELECTED_OUTLINE), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(left - 1, plot.top(), width + 2, plot.height()))
        self._draw_question_labels(painter, plot, slot)

    def _draw_percent_axis(self, painter: QPainter, plot: QRectF, ticks: Sequence[float]) -> None:
        metrics = painter.fontMetrics()
        for tick in ticks:
            y = self.y_for(plot, tick, 0.0, 1.0)
            painter.setPen(QPen(QColor(ChartColor.GRID), 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(Color.TEXT_SECONDARY))
            text = f"{round(tick * 100)}%"
            painter.drawText(
                QPointF(plot.left() - metrics.horizontalAdvance(text) - 6,
                        y + metrics.ascent() / 2 - 1),
                text,
            )
        painter.setPen(QPen(QColor(ChartColor.AXIS), 1))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        if self.y_title:
            painter.save()
            painter.setPen(QColor(Color.TEXT_SECONDARY))
            painter.translate(metrics.height() / 2 + 2, plot.center().y())
            painter.rotate(-90)
            painter.drawText(QPointF(-metrics.horizontalAdvance(self.y_title) / 2,
                                     metrics.ascent() / 2), self.y_title)
            painter.restore()

    def _draw_question_labels(self, painter: QPainter, plot: QRectF, slot: float) -> None:
        """Label every 1st, 2nd, 5th or 10th question - whatever fits."""
        metrics = painter.fontMetrics()
        widest = metrics.horizontalAdvance(f"Q{self.bars[-1].number}") + 6
        every = next((n for n in (1, 2, 5, 10, 20, 25, 50, 100) if n * slot >= widest), 100)
        painter.setPen(QColor(Color.TEXT_SECONDARY))
        first = self.bars[0].number
        # The first question is labelled only when the next labelled one is
        # far enough away not to collide with it.
        label_first = every == 1 or (-first % every) * slot >= widest
        for index, bar in enumerate(self.bars):
            if bar.number % every and not (index == 0 and label_first):
                continue
            text = str(bar.number)
            x = plot.left() + (index + 0.5) * slot
            painter.drawText(
                QPointF(x - metrics.horizontalAdvance(text) / 2,
                        plot.bottom() + metrics.ascent() + 3),
                text,
            )
        width = metrics.horizontalAdvance(self.x_title)
        painter.drawText(QPointF(plot.center().x() - width / 2, self.height() - 4),
                         self.x_title)


# ----------------------------------------------------------------------
# Options of one question
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OptionBar:
    """One option of the selected question."""

    label: str
    count: int
    share: float
    is_key: bool = False


class OptionChart(ChartBase):
    """Horizontal bars: how many chose each option, the key highlighted."""

    MIN_HEIGHT = 150

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dashboardOptionChart")
        self.bars: list[OptionBar] = []
        self.empty_text = "Choose a question."

    def set_data(self, bars: Sequence[OptionBar]) -> None:
        """Replace the options; the height follows their number."""
        self.bars = list(bars)
        line = QFontMetrics(self.font()).height()
        self.setMinimumHeight(max(self.MIN_HEIGHT, len(self.bars) * (line + 10) + 16))
        self.updateGeometry()
        self.hovered = None
        self.update()

    def sizeHint(self) -> QSize:
        """Tall enough for every option."""
        return QSize(320, self.minimumHeight())

    def item_count(self) -> int:
        """One item per option."""
        return len(self.bars)

    def _label_width(self) -> int:
        metrics = QFontMetrics(self.font())
        return max((metrics.horizontalAdvance(bar.label) for bar in self.bars), default=0) + 12

    def _row(self, index: int) -> QRect:
        height = (self.height() - 12) // max(len(self.bars), 1)
        return QRect(0, 6 + index * height, self.width(), height)

    def index_at(self, point: QPointF) -> int | None:
        """The option row under the pointer."""
        for index in range(len(self.bars)):
            if self._row(index).contains(point.toPoint()):
                return index
        return None

    def tooltip_for(self, index: int) -> str:
        """The option's count and share."""
        bar = self.bars[index]
        key = " (the key)" if bar.is_key else ""
        return f"<b>{bar.label}</b>{key}<br>{bar.count} script(s) · {bar.share * 100:.1f}%"

    def paintEvent(self, event: QPaintEvent) -> None:
        """Rows of label, bar and percentage."""
        if not self.bars:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(Color.SURFACE))
        metrics = painter.fontMetrics()
        label_width = self._label_width()
        value_width = metrics.horizontalAdvance("100.0% (0000)") + 8
        track = self.width() - label_width - value_width - 8
        for index, bar in enumerate(self.bars):
            row = self._row(index)
            mid = row.center().y()
            bold = QFont(self.font())
            bold.setBold(bar.is_key)
            painter.setFont(bold)
            painter.setPen(QColor(Color.TEXT_PRIMARY))
            painter.drawText(QPointF(4, mid + metrics.ascent() / 2 - 1), bar.label)
            height = min(row.height() - 6, metrics.height() + 2)
            painter.fillRect(QRectF(label_width, mid - height / 2, track, height),
                             QColor(ChartColor.GRID))
            colour = ChartColor.KEY if bar.is_key else (
                ChartColor.BAR_HOVER if index == self.hovered else ChartColor.BAR
            )
            painter.fillRect(
                QRectF(label_width, mid - height / 2, max(track * bar.share, 0), height),
                QColor(colour),
            )
            painter.setPen(QColor(Color.TEXT_PRIMARY))
            text = f"{bar.share * 100:.1f}% ({bar.count})"
            painter.drawText(QPointF(label_width + track + 8, mid + metrics.ascent() / 2 - 1),
                             text)
        painter.end()
