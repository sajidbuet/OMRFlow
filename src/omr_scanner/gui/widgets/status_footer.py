"""The application's bottom status band.

Purpose:
    Say, permanently and compactly, which build is running, that it is open
    source, who wrote it, and what the application is doing right now.

Responsibilities:
    * :class:`AppStatus` - the states the application can actually be in.
    * :class:`StatusFooter` - the band, and its three-tier responsive
      behaviour.

What does NOT belong here:
    * Deciding the status. The footer is told; the main window, which owns the
      batch, knows. :meth:`StatusFooter.set_status` is the whole interface.
    * Transient messages. Those keep going to `QStatusBar`, which sits below
      this band and is unchanged.

Why the version and licence are read from package metadata:
    :data:`omr_scanner.__version__` and :data:`omr_scanner.LICENSE_NAME` are
    the same values `pyproject.toml` declares and the About dialog shows, and
    the licence name matches the repository's own ``LICENSE`` file. A version
    typed into this module would be wrong the first time one was released.

Why the status is a word and a dot, and never a dot alone:
    A coloured dot is unreadable to an operator who cannot distinguish the
    colours, and meaningless on a monochrome display or in a screenshot
    printed in black and white. The dot is a redundant accent on text that
    already says "Ready" or "Processing".
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QResizeEvent
from PySide6.QtWidgets import (
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from omr_scanner import APPLICATION_NAME, LICENSE_NAME, __version__
from omr_scanner.gui.theme import Color, FontSize, Footer


class AppStatus(Enum):
    """What the application is doing.

    Deliberately short. These two are the states OMRFlow genuinely
    distinguishes - a recognition batch is either running in the Scan stage's
    worker thread or it is not - and inventing a third would put a word in the
    footer that nothing ever sets. In particular there is no ``Error`` state:
    the application has no persistent error condition, and failures are
    reported where they happen, not summarised here.

    Attributes:
        READY: No long-running work in progress.
        PROCESSING: A recognition batch is running.
    """

    READY = "Ready"
    PROCESSING = "Processing"

    @property
    def colour(self) -> str:
        """The accent for this state's dot."""
        return {
            AppStatus.READY: Color.STATUS_READY,
            AppStatus.PROCESSING: Color.STATUS_BUSY,
        }[self]


class FooterTier(Enum):
    """How much of the footer fits.

    Attributes:
        FULL: Everything on one row.
        NO_LICENCE: One row; the licence text is dropped.
        STACKED: Two rows; the licence and the credit move to the second, and
            the build version and status keep the first to themselves.
    """

    FULL = "full"
    NO_LICENCE = "no_licence"
    STACKED = "stacked"


class _StatusDot(QWidget):
    """A small filled circle, painted rather than typed.

    A text bullet would change size, weight and baseline with the UI font;
    this is exactly :data:`Footer.STATUS_DOT_SIZE` across at every font and
    every display scale.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("appFooterStatusDot")
        self.setFixedSize(Footer.STATUS_DOT_SIZE, Footer.STATUS_DOT_SIZE)
        self._colour = QColor(Color.STATUS_READY)

    def set_colour(self, colour: str) -> None:
        self._colour = QColor(colour)
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(self._colour)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self.rect())
        painter.end()


def _clear(layout: QBoxLayout) -> None:
    """Empty ``layout`` without destroying the widgets that were in it.

    ``takeAt`` hands ownership of each item back to the caller; a widget item
    releases the widget, which stays alive because it is still parented to the
    footer. This is what lets :meth:`StatusFooter._apply_tier` rebuild both
    rows declaratively instead of computing insertion indices - the version
    that did compute them was one refactor away from putting the credit
    between the separator and the licence.
    """
    while layout.count():
        layout.takeAt(0)


class StatusFooter(QWidget):
    """The bottom band: build, licence, credit, status.

    Args:
        developer_name: Who to credit.
        developer_url: Where the credit links to.
        parent: Optional Qt parent.

    Signals:
        developer_link_activated: The credit link was clicked. Carries the
            URL. Emitted rather than opened here, so the one place that hands
            a URL to the operating system stays the main window.

    Attributes:
        version_label: ``"OMRFlow v0.1.0.dev0"``.
        licence_label: ``"Open Source (MIT License)"``.
        credit_label: The developer credit, with its link.
        status_label: The status word.
    """

    developer_link_activated = Signal(str)

    def __init__(
        self,
        developer_name: str,
        developer_url: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("appFooter")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self._status = AppStatus.READY
        self._tier: FooterTier | None = None

        self.version_label = self._small_label(
            f"{APPLICATION_NAME} v{__version__}", "appFooterVersion"
        )
        self.licence_separator = self._small_label("|", "appFooterSeparator")
        self.licence_label = self._small_label(
            f"Open Source ({LICENSE_NAME} License)", "appFooterVersion"
        )

        self.credit_label = self._small_label(
            f'Developed by <a href="{developer_url}">{developer_name}</a>',
            "appFooterCredit",
        )
        self.credit_label.setTextFormat(Qt.TextFormat.RichText)
        self.credit_label.setOpenExternalLinks(False)
        self.credit_label.linkActivated.connect(self.developer_link_activated.emit)

        self.status_dot = _StatusDot(self)
        self.status_label = self._small_label(self._status.value, "appFooterStatus")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            Footer.H_PADDING, Footer.V_PADDING, Footer.H_PADDING, Footer.V_PADDING
        )
        outer.setSpacing(Footer.V_PADDING)
        self._primary = QHBoxLayout()
        self._primary.setContentsMargins(0, 0, 0, 0)
        self._primary.setSpacing(Footer.SEPARATOR_MARGIN)
        self._secondary = QHBoxLayout()
        self._secondary.setContentsMargins(0, 0, 0, 0)
        self._secondary.setSpacing(Footer.SEPARATOR_MARGIN)
        outer.addLayout(self._primary)
        outer.addLayout(self._secondary)

        self._apply_tier(FooterTier.FULL)
        self.set_status(AppStatus.READY)

    def _small_label(self, text: str, object_name: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName(object_name)
        font = QFont(label.font())
        font.setPointSizeF(
            max(font.pointSizeF() + FontSize.FOOTER, FontSize.MIN_POINT_SIZE)
        )
        label.setFont(font)
        return label

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def set_status(self, status: AppStatus) -> None:
        """Show ``status``.

        Sets the word *and* the dot's colour; the word is what carries the
        meaning.
        """
        self._status = status
        self.status_label.setText(status.value)
        self.status_dot.set_colour(status.colour)
        self.status_dot.setToolTip(status.value)
        self.status_label.setAccessibleName(f"Application status: {status.value}")

    @property
    def status(self) -> AppStatus:
        """The status currently shown."""
        return self._status

    # ------------------------------------------------------------------
    # Responsive behaviour
    # ------------------------------------------------------------------
    @property
    def tier(self) -> FooterTier | None:
        """Which tier is currently applied."""
        return self._tier

    def _width_for(self, tier: FooterTier) -> int:
        """Width the first row needs in ``tier``.

        Only the first row is measured, because the second row is only ever
        used when the first has already overflowed - it never decides the
        tier.
        """
        width = (
            2 * Footer.H_PADDING
            + self.version_label.sizeHint().width()
            + Footer.SEPARATOR_MARGIN
            + self.status_dot.width()
            + Footer.STATUS_DOT_GAP
            + self.status_label.sizeHint().width()
        )
        if tier is FooterTier.STACKED:
            return width
        width += self.credit_label.sizeHint().width() + 2 * Footer.SEPARATOR_MARGIN
        if tier is FooterTier.FULL:
            width += (
                self.licence_separator.sizeHint().width()
                + self.licence_label.sizeHint().width()
                + 2 * Footer.SEPARATOR_MARGIN
            )
        return width

    def tier_for_width(self, available: int) -> FooterTier:
        """The most complete tier that fits in ``available`` pixels.

        Tried in the brief's own priority order: the licence text goes before
        the credit does, and the build version and the status word never go at
        all - they are the two things an operator glances down here to read.
        """
        for tier in (FooterTier.FULL, FooterTier.NO_LICENCE):
            if available >= self._width_for(tier):
                return tier
        return FooterTier.STACKED

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-pick the tier for the new width."""
        super().resizeEvent(event)
        self._apply_tier(self.tier_for_width(self.width()))

    def _apply_tier(self, tier: FooterTier) -> None:
        """Rebuild both rows for ``tier``. Idempotent and index-free."""
        if tier is self._tier:
            return
        self._tier = tier
        _clear(self._primary)
        _clear(self._secondary)

        self._primary.addWidget(self.version_label)
        if tier is FooterTier.FULL:
            self._primary.addWidget(self.licence_separator)
            self._primary.addWidget(self.licence_label)
        self._primary.addStretch(1)
        if tier is not FooterTier.STACKED:
            self._primary.addWidget(self.credit_label)
            self._primary.addStretch(1)
        self._primary.addWidget(self.status_dot)
        self._primary.addSpacing(max(Footer.STATUS_DOT_GAP - Footer.SEPARATOR_MARGIN, 0))
        self._primary.addWidget(self.status_label)

        if tier is FooterTier.STACKED:
            self._secondary.addWidget(self.licence_label)
            self._secondary.addStretch(1)
            self._secondary.addWidget(self.credit_label)

        for widget, visible in (
            (self.licence_separator, tier is FooterTier.FULL),
            (self.licence_label, tier is not FooterTier.NO_LICENCE),
            (self.credit_label, True),
        ):
            widget.setVisible(visible)

    def sizeHint(self) -> QSize:
        """Room for everything on one row."""
        return QSize(self._width_for(FooterTier.FULL), Footer.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        """Only the version and the status are mandatory.

        The stacked tier is two rows tall, so the minimum height has to allow
        for that or the second row is clipped exactly when it is needed.
        """
        rows = 2 if self._tier is FooterTier.STACKED else 1
        row_height = max(
            self.version_label.sizeHint().height(), Footer.STATUS_DOT_SIZE
        )
        return QSize(
            self._width_for(FooterTier.STACKED),
            rows * row_height + (rows + 1) * Footer.V_PADDING,
        )


__all__ = ["AppStatus", "FooterTier", "StatusFooter"]
