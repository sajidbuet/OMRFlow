"""Reusable content containers: the card, the empty state and the action row.

Purpose:
    Give the interface three presentation primitives it kept rebuilding by
    hand, so that a titled panel, a "there is nothing here yet" panel and a
    clickable list entry look and behave the same everywhere they appear.

Responsibilities:
    * :class:`Card` - a titled surface with a hairline border.
    * :class:`EmptyState` - the large centred "nothing here" panel.
    * :class:`ActionRow` - one clickable entry inside a card: icon, heading,
      supporting line, chevron.

What does NOT belong here:
    * Anything about projects, scans or results. These are containers; what
      goes in them is the caller's business.
    * A card treatment for every panel in the application. Cards are for
      grouping *peer* content on a dashboard. The brief is explicit that not
      every group box should become one, and the editor pages
      (template, calibrate, scan, resolve) deliberately keep their plain
      full-bleed layouts, because a border around a canvas costs workspace and
      buys nothing.

Why :class:`ActionRow` is a `QAbstractButton`:
    It needs hover, press, focus, Space/Enter activation, an accessible name
    and a disabled state - all of which `QAbstractButton` already has and all
    of which would be hand-rolled, and eventually wrong, on a `QFrame` with a
    ``mousePressEvent``. Its labels are ordinary child widgets marked
    transparent to mouse events, so the whole row is one hit target, which is
    what the brief asks for.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QEnterEvent, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.theme import (
    Card as CardMetrics,
)
from omr_scanner.gui.theme import (
    Color,
    FontSize,
    FontWeight,
    IconSize,
    Radius,
    Spacing,
    Stroke,
)

CARD_PROPERTY = "card"
"""Dynamic property the global stylesheet keys the card surface off.

``"true"`` is the ordinary white card; ``"sunken"`` is the dashed placeholder
surface an empty state uses. A property rather than an object name, because
several unrelated widgets need the same surface and object names have to be
unique to be useful for anything else.
"""


def _titled_font(base: QFont, delta: int, weight: int) -> QFont:
    font = QFont(base)
    font.setPointSizeF(max(base.pointSizeF() + delta, FontSize.MIN_POINT_SIZE))
    font.setWeight(QFont.Weight(weight))
    return font


class Card(QFrame):
    """A titled surface for a group of related content.

    Args:
        title: Heading shown at the top. Omit for an untitled surface.
        parent: Optional Qt parent.
        trailing: A widget placed on the title's right - a "View All" button,
            typically.

    Attributes:
        body: Add content here. A `QVBoxLayout` inside the card's padding.
        title_label: The heading, or ``None`` when untitled.
    """

    def __init__(
        self,
        title: str = "",
        parent: QWidget | None = None,
        *,
        trailing: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setProperty(CARD_PROPERTY, "true")
        self.setFrameShape(QFrame.Shape.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            CardMetrics.PADDING, CardMetrics.PADDING, CardMetrics.PADDING, CardMetrics.PADDING
        )
        outer.setSpacing(CardMetrics.TITLE_GAP)

        self.title_label: QLabel | None = None
        if title or trailing is not None:
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(Spacing.SM)
            if title:
                self.title_label = QLabel(title, self)
                self.title_label.setObjectName("cardTitle")
                self.title_label.setFont(
                    _titled_font(self.font(), FontSize.SECTION_TITLE, FontWeight.BOLD)
                )
                header.addWidget(self.title_label)
            header.addStretch(1)
            if trailing is not None:
                header.addWidget(trailing)
            outer.addLayout(header)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(CardMetrics.GAP)
        outer.addLayout(self.body)

    def add_divider(self) -> QFrame:
        """Append a hairline rule to :attr:`body` and return it."""
        divider = QFrame(self)
        divider.setObjectName("cardDivider")
        divider.setFrameShape(QFrame.Shape.NoFrame)
        divider.setFixedHeight(Stroke.HAIRLINE)
        self.body.addWidget(divider)
        return divider

    def add_empty_note(self, headline: str, detail: str = "") -> QWidget:
        """Append a small centred "nothing here yet" note and return it.

        For an *empty card*, as distinct from :class:`EmptyState`, which fills
        a whole page region. A card that is merely empty should say so
        quietly, not turn into a billboard.
        """
        host = QWidget(self)
        host.setObjectName("cardEmptyNote")
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, Spacing.LG, 0, Spacing.LG)
        layout.setSpacing(Spacing.XS)

        icon = QLabel(host)
        icon.setObjectName("cardEmptyIcon")
        icon.setPixmap(
            load_icon("folder").pixmap(
                QSize(IconSize.EMPTY_STATE // 2, IconSize.EMPTY_STATE // 2),
                QIcon.Mode.Disabled,
            )
        )
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)

        title = QLabel(headline, host)
        title.setObjectName("emptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setFont(_titled_font(self.font(), FontSize.BODY, FontWeight.MEDIUM))
        layout.addWidget(title)

        if detail:
            body = QLabel(detail, host)
            body.setObjectName("cardEmptyBody")
            body.setAlignment(Qt.AlignmentFlag.AlignCenter)
            body.setWordWrap(True)
            layout.addWidget(body)

        self.body.addWidget(host)
        return host


class EmptyState(QFrame):
    """The large centred panel shown when a page has nothing to display.

    Args:
        icon_name: Bundled Lucide icon name for the illustration.
        headline: The one-line statement of fact - "No project is open."
        detail: What to do about it.
        parent: Optional Qt parent.

    Attributes:
        actions_layout: A `QHBoxLayout`, already centred, for the
            call-to-action buttons. Not named ``actions``: `QWidget` already
            has an ``actions()`` method, and shadowing it with a layout is
            how a widget ends up with a `QAction` list that is a
            `QHBoxLayout`.

    Sized to its content and centred vertically by its parent's stretch, not
    stretched to fill: a panel inflated to fill a 1080-pixel-tall window is
    the "excessive blank region" the brief asks to avoid, and it makes the
    buttons drift apart from the text they belong to.
    """

    def __init__(
        self,
        icon_name: str,
        headline: str,
        detail: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        self.setProperty(CARD_PROPERTY, "sunken")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        outer.setSpacing(Spacing.MD)
        outer.addStretch(1)

        self.icon_label = QLabel(self)
        self.icon_label.setObjectName("emptyStateIcon")
        self.icon_label.setPixmap(
            load_icon(icon_name).pixmap(
                QSize(IconSize.EMPTY_STATE, IconSize.EMPTY_STATE), QIcon.Mode.Disabled
            )
        )
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.icon_label)

        self.headline_label = QLabel(headline, self)
        self.headline_label.setObjectName("emptyStateTitle")
        self.headline_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.headline_label.setFont(
            _titled_font(self.font(), FontSize.PAGE_TITLE - 3, FontWeight.BOLD)
        )
        self.headline_label.setWordWrap(True)
        outer.addWidget(self.headline_label)

        self.detail_label = QLabel(detail, self)
        self.detail_label.setObjectName("emptyStateBody")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)
        self.detail_label.setVisible(bool(detail))
        outer.addWidget(self.detail_label)

        self.actions_layout = QHBoxLayout()
        self.actions_layout.setContentsMargins(0, Spacing.SM, 0, 0)
        self.actions_layout.setSpacing(Spacing.MD)
        self.actions_layout.addStretch(1)
        self._action_tail = self.actions_layout.count()
        self.actions_layout.addStretch(1)
        outer.addLayout(self.actions_layout)

        outer.addStretch(1)

    def add_action(self, widget: QWidget) -> None:
        """Add a call-to-action button, keeping the group centred.

        Inserted between the two stretches rather than appended, so the
        buttons stay centred as a group however many are added.
        """
        self.actions_layout.insertWidget(self._action_tail, widget)
        self._action_tail += 1


class ActionRow(QAbstractButton):
    """One clickable entry in a card: icon, heading, supporting line, chevron.

    Args:
        icon_name: Bundled Lucide icon name, drawn on a pale accent plate.
        heading: What the entry does.
        detail: One supporting line.
        parent: Optional Qt parent.

    Every visible entry must do something: the brief rules out decorative
    controls that look clickable and are not. An entry that is not currently
    meaningful is *disabled with an explanation*
    (:meth:`set_unavailable_reason`), never shown as live.
    """

    def __init__(
        self,
        icon_name: str,
        heading: str,
        detail: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"actionRow_{icon_name}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(CardMetrics.ROW_HEIGHT)
        self._unavailable_reason = ""
        self._detail = detail

        layout = QHBoxLayout(self)
        layout.setContentsMargins(Spacing.SM, Spacing.SM, Spacing.SM, Spacing.SM)
        layout.setSpacing(Spacing.MD)

        self.plate = _IconPlate(icon_name, self)
        layout.addWidget(self.plate)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(1)

        self.heading_label = QLabel(heading, self)
        self.heading_label.setObjectName("actionRowHeading")
        self.heading_label.setFont(
            _titled_font(self.font(), FontSize.CARD_TITLE, FontWeight.SEMIBOLD)
        )
        text_column.addWidget(self.heading_label)

        self.detail_label = QLabel(detail, self)
        self.detail_label.setObjectName("actionRowDetail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setVisible(bool(detail))
        font = QFont(self.font())
        font.setPointSizeF(
            max(font.pointSizeF() + FontSize.SECONDARY, FontSize.MIN_POINT_SIZE)
        )
        self.detail_label.setFont(font)
        text_column.addWidget(self.detail_label)
        layout.addLayout(text_column, 1)

        self.chevron = QLabel(self)
        self.chevron.setObjectName("actionRowChevron")
        self.chevron.setFixedSize(IconSize.MD, IconSize.MD)
        layout.addWidget(self.chevron)

        # One hit target: without this, a click that happens to land on the
        # heading is delivered to the label and the row never fires.
        for child in (self.plate, self.heading_label, self.detail_label, self.chevron):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._refresh_palette()
        self._refresh_accessibility(heading)

    def _refresh_accessibility(self, heading: str) -> None:
        self.setAccessibleName(heading)
        parts = [part for part in (self._detail, self._unavailable_reason) if part]
        self.setAccessibleDescription(" ".join(parts))
        self.setToolTip(self._unavailable_reason or self._detail)
        self.setStatusTip(self._detail or heading)

    def set_unavailable_reason(self, reason: str) -> None:
        """Disable the row and say why, or re-enable it when ``reason`` is ``""``."""
        self._unavailable_reason = reason
        self.setEnabled(not reason)
        self.detail_label.setText(reason or self._detail)
        self._refresh_accessibility(self.heading_label.text())
        self._refresh_palette()

    @property
    def unavailable_reason(self) -> str:
        """Why this row is disabled, or ``""``."""
        return self._unavailable_reason

    def _refresh_palette(self) -> None:
        enabled = self.isEnabled()
        heading = Color.TEXT_PRIMARY if enabled else Color.TEXT_DISABLED
        detail = Color.TEXT_SECONDARY if enabled else Color.TEXT_DISABLED
        self.heading_label.setStyleSheet(f"color: {heading};")
        self.detail_label.setStyleSheet(f"color: {detail};")
        self.plate.set_enabled_look(enabled)
        self.chevron.setPixmap(
            load_icon("chevron-right").pixmap(
                QSize(IconSize.MD, IconSize.MD),
                QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled,
            )
        )

    def changeEvent(self, event: QEvent) -> None:
        """Restyle the child labels when the enabled state changes.

        The labels carry their own colour, so Qt's automatic disabled palette
        does not reach them - without this, disabling the row would grey the
        icon and the chevron but leave the text looking live.
        """
        super().changeEvent(event)
        self._refresh_palette()

    def sizeHint(self) -> QSize:
        """Room for the plate, the wider text line, and the chevron."""
        return QSize(
            IconSize.GETTING_STARTED_PLATE
            + Spacing.MD
            + max(
                self.heading_label.sizeHint().width(),
                self.detail_label.sizeHint().width(),
            )
            + Spacing.MD
            + IconSize.MD
            + 2 * Spacing.SM,
            max(CardMetrics.ROW_HEIGHT, super().sizeHint().height()),
        )

    def paintEvent(self, _event: object) -> None:
        """Paint only the interaction background; the children draw the rest."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rounded = self.rect().adjusted(0, 0, -1, -1).toRectF()

        if self.isEnabled() and (self.isDown() or self.underMouse()):
            fill = Color.SURFACE_PRESSED if self.isDown() else Color.SURFACE_HOVER
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(rounded, Radius.MD, Radius.MD)

        if self.hasFocus():
            pen = QPen(QColor(Color.FOCUS), float(Stroke.FOCUS_RING))
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rounded, Radius.MD, Radius.MD)
        painter.end()

    def enterEvent(self, event: QEnterEvent) -> None:
        """Repaint so the hover background appears."""
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """Repaint so the hover background goes away."""
        self.update()
        super().leaveEvent(event)


class _IconPlate(QWidget):
    """A Lucide glyph centred on a pale circular accent plate."""

    def __init__(self, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("actionRowPlate")
        self.setFixedSize(
            IconSize.GETTING_STARTED_PLATE, IconSize.GETTING_STARTED_PLATE
        )
        self._icon = load_icon(icon_name)
        self._enabled_look = True

    def set_enabled_look(self, enabled: bool) -> None:
        self._enabled_look = enabled
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(
            QColor(Color.PRIMARY_SOFT if self._enabled_look else Color.SURFACE_DISABLED)
        )
        painter.drawEllipse(self.rect())

        extent = IconSize.GETTING_STARTED
        ratio = self.devicePixelRatioF()
        pixmap = self._icon.pixmap(
            QSize(extent, extent) * ratio,
            ratio,
            QIcon.Mode.Normal if self._enabled_look else QIcon.Mode.Disabled,
        )
        tint = QPainter(pixmap)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint.fillRect(
            pixmap.rect(),
            QColor(Color.PRIMARY if self._enabled_look else Color.TEXT_DISABLED),
        )
        tint.end()
        offset = (self.width() - extent) // 2
        painter.drawPixmap(offset, offset, extent, extent, pixmap)
        painter.end()


__all__ = ["CARD_PROPERTY", "ActionRow", "Card", "EmptyState"]
