"""A single wrapped row of small status labels.

Purpose:
    Say how something went in one line. A page that spends ten wrapped lines
    on "registration passed, four of four markers, orientation resolved" has
    used a tenth of its height restating a result the reader took in at a
    glance - and taken that height from whatever the page is actually for.

Responsibilities:
    * :class:`StatusChip` - one labelled state.
    * :class:`StatusChipStrip` - a row of them that wraps.
    * :class:`FlowLayout` - the wrapping itself, which Qt does not provide.

What does NOT belong here:
    * Any knowledge of what is being reported. A strip is handed
      ``(tone, text)`` pairs; the page decides what deserves a chip.

Why each chip carries words:
    The tone picks the colour, but the text always names the state - "4/4",
    "Orientation assumed", "31 review". Colour is the fastest way to read a
    status for people who can see the difference and no way at all for people
    who cannot, so it is never the only carrier.

Why it wraps rather than elides:
    How many chips there are depends on what the engine found. A fixed row
    would clip the last ones, and a chip elided to "31 re..." is worse than
    absent. Wrapping costs one extra row at worst and keeps every chip
    readable.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLayout,
    QLayoutItem,
    QSizePolicy,
    QWidget,
)

from omr_scanner.gui.theme import Color, FontSize, Radius, Spacing, Stroke

TONES: dict[str, str] = {
    "ok": Color.STATUS_READY,
    "warn": Color.STATUS_BUSY,
    "fail": Color.STATUS_ERROR,
    "neutral": Color.TEXT_SECONDARY,
}
"""``tone -> foreground colour``, from the application's existing status
palette rather than a new one. Four tones, matching the four calibration
outcomes the engine already distinguishes; a fifth colour would mean inventing
a status that does not exist.

Every chip shares one muted background and takes its tone from the text and
border only. A row of filled colour blocks reads as a dashboard, which is the
opposite of what a technical page under a scan preview should look like."""


class StatusChip(QLabel):
    """One small labelled state."""

    def __init__(
        self, text: str, *, tone: str = "neutral", parent: QWidget | None = None
    ) -> None:
        """Create a chip.

        Args:
            text: What the chip says. Always meaningful without the colour.
            tone: One of ``ok``, ``warn``, ``fail``, ``neutral``. An unknown
                tone falls back to neutral rather than raising: a chip is a
                label, and a page should not fail to draw because a status
                string was unexpected.
            parent: Optional Qt parent.
        """
        super().__init__(text, parent)
        foreground = TONES.get(tone, TONES["neutral"])
        self.setObjectName(f"statusChip_{tone if tone in TONES else 'neutral'}")
        self.setStyleSheet(
            f"color: {foreground};"
            f"background: {Color.SURFACE_MUTED};"
            f"border: {Stroke.HAIRLINE}px solid {foreground};"
            f"border-radius: {Radius.SM}px;"
            f"padding: {Spacing.XXS}px {Spacing.SM}px;"
        )
        font = self.font()
        font.setPointSizeF(
            max(font.pointSizeF() + FontSize.SECONDARY, FontSize.MIN_POINT_SIZE)
        )
        self.setFont(font)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        # What a screen reader announces. The tone is decoration on top of it.
        self.setAccessibleName(text)


class FlowLayout(QLayout):
    """Left to right, wrapping onto a new row when the width runs out.

    Qt ships no flow layout. This is the minimal one, and it exists for a
    concrete reason: the chip count varies with what the engine found, so a
    `QHBoxLayout` would either clip the last chips or push the page wider than
    the window.
    """

    def __init__(self, parent: QWidget | None = None, *, spacing: int = Spacing.XS) -> None:
        """Create the layout."""
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._space = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:
        """Take ownership of ``item``."""
        self._items.append(item)

    def count(self) -> int:
        """How many items the layout holds."""
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        """The item at ``index``, or ``None`` when out of range."""
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        """Remove and return the item at ``index``."""
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        """The strip never asks to grow on its own."""
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        """Height depends on width - that is what wrapping means."""
        return True

    def heightForWidth(self, width: int) -> int:
        """The height the chips need at ``width``."""
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        """Lay the chips out inside ``rect``."""
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        """One unwrapped row."""
        width = sum(item.sizeHint().width() + self._space for item in self._items)
        height = max((item.sizeHint().height() for item in self._items), default=0)
        return QSize(max(0, width - self._space), height)

    def minimumSize(self) -> QSize:
        """Wide enough for the widest single chip, tall enough for one row."""
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, *, apply: bool) -> int:
        """Place the items, or measure where they would go.

        Args:
            rect: The area to lay out in.
            apply: Whether to actually move the widgets.

        Returns:
            The total height used.
        """
        x, y, row_height = rect.x(), rect.y(), 0
        right = rect.x() + rect.width()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._space
            if next_x - self._space > right and row_height > 0:
                x = rect.x()
                y += row_height + self._space
                next_x = x + hint.width() + self._space
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y()


class StatusChipStrip(QFrame):
    """A row of :class:`StatusChip` widgets that wraps when it runs out of width."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """Create an empty strip."""
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._flow = FlowLayout(self)
        self.setLayout(self._flow)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def set_chips(self, chips: list[tuple[str, str]]) -> None:
        """Replace the strip's contents.

        Args:
            chips: ``(tone, text)`` pairs, in the order they should read.
        """
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for tone, text in chips:
            self._flow.addWidget(StatusChip(text, tone=tone, parent=self))
        self.updateGeometry()

    def chip_texts(self) -> list[str]:
        """What the strip currently says, in order. For tests and diagnostics."""
        texts: list[str] = []
        for index in range(self._flow.count()):
            item = self._flow.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QLabel):
                texts.append(widget.text())
        return texts


__all__ = ["TONES", "FlowLayout", "StatusChip", "StatusChipStrip"]
