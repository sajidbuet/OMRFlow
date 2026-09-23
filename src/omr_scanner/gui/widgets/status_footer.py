"""The application's bottom status band.

Purpose:
    Say, permanently and compactly, which build is running, which project is
    open, who wrote it, that it is open source, and what the application is
    doing right now:

        OMRFlow v<version> | Project: Recruitment Exam    ...
            ... Developed by Dr. Sajid Muhaimin Choudhury | Open Source (MIT
            License) | * Ready

    The version is spelled ``<version>`` rather than shown, because this
    module must hold no copy of it -
    ``tests/unit/test_version.py`` fails if one appears, which is how a
    release cannot leave a stale number behind in a docstring.

Responsibilities:
    * :class:`AppStatus` - the states the application can actually be in.
    * :class:`StatusFooter` - the band, and its four-tier responsive
      behaviour.

What does NOT belong here:
    * Deciding the status, or knowing what a project is. The footer is *told*;
      the main window, which owns the batch and the session, knows.
      :meth:`StatusFooter.set_status` and
      :meth:`StatusFooter.set_project_title` are the whole interface.
    * Transient messages. Those keep going to `QStatusBar`, which sits below
      this band and is unchanged.

Why the version and licence are read from package metadata:
    :data:`omr_scanner.__version__` and :data:`omr_scanner.LICENSE_NAME` are
    the same values `pyproject.toml` declares and the About dialog shows, and
    the licence name matches the repository's own ``LICENSE`` file. A version
    typed into this module would be wrong the first time one was released.

Why the project title is elided rather than wrapped or truncated:
    A real examination title is a sentence - "Recruitment Exam, Bangladesh
    Submarine Cable Regulatory Authority" - and the footer is one line. Qt's
    own elision puts the ellipsis where the text is cut and the full value
    goes in the tooltip, so nothing is lost and the version beside it never
    gets pushed off the row.

Why the status is a word and a dot, and never a dot alone:
    A coloured dot is unreadable to an operator who cannot distinguish the
    colours, and meaningless on a monochrome display or in a screenshot
    printed in black and white. The dot is a redundant accent on text that
    already says "Ready" or "Processing".
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QResizeEvent
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

NO_PROJECT_TEXT = "No project open"
"""What the footer says with no project open.

A statement, not a blank: an empty space beside the version would read as a
footer that had not finished loading, and the difference between "nothing is
open" and "something is open and I cannot see which" is exactly what an
operator is looking down here to settle.
"""

PROJECT_PREFIX = "Project: "

SEPARATOR = "|"


class AppStatus(Enum):
    """What the application is doing.

    Deliberately short. These two are the states OMRFlow genuinely
    distinguishes - a recognition batch is either running in the Scan stage's
    worker thread or it is not - and inventing a third would put a word in the
    footer that nothing ever sets. In particular there is no ``Error`` and no
    ``Paused`` state, both of which the brief offers as examples: the
    application has no persistent error condition and no pause control, and a
    status word that nothing can produce is a lie told slowly. Failures are
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
    """How much of the footer fits, most complete first.

    The order is the brief's own priority list read backwards: the licence
    text goes first, then the developer credit, and the build version, the
    project and the status word never go at all.

    Attributes:
        FULL: Everything on one row.
        NO_LICENCE: One row; the licence text is dropped.
        NO_CREDIT: One row; the credit goes too, leaving build, project and
            status.
        STACKED: Two rows; the credit and licence move to the second, and the
            build, project and status keep the first to themselves.
    """

    FULL = "full"
    NO_LICENCE = "no_licence"
    NO_CREDIT = "no_credit"
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


class _ElidingLabel(QLabel):
    """A label that shortens its own text instead of demanding room.

    The project title is the only thing in this band whose length the
    application does not control, and it is beside the one thing that must
    never be pushed off the row. So it gives way: it keeps its full value in
    :attr:`full_text` and in its tooltip, and shows as much of it as the width
    it was actually given allows.
    """

    def __init__(self, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.full_text = ""

    def set_full_text(self, text: str) -> None:
        """Set the value this label represents, and show what fits of it."""
        self.full_text = text
        self.setToolTip(text)
        self._apply_elision()

    def _apply_elision(self) -> None:
        metrics = QFontMetrics(self.font())
        available = max(self.width(), 0)
        if available <= 0 or metrics.horizontalAdvance(self.full_text) <= available:
            super().setText(self.full_text)
            return
        super().setText(
            metrics.elidedText(self.full_text, Qt.TextElideMode.ElideRight, available)
        )

    @property
    def is_elided(self) -> bool:
        """Whether what is shown is shorter than what it stands for."""
        return self.text() != self.full_text

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-elide for the width just granted."""
        super().resizeEvent(event)
        self._apply_elision()

    def sizeHint(self) -> QSize:
        """The whole value, which the layout is free not to grant."""
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance(self.full_text), metrics.height())

    def minimumSizeHint(self) -> QSize:
        """Room for an ellipsis and little else, so it never forces a width."""
        metrics = QFontMetrics(self.font())
        return QSize(metrics.horizontalAdvance("..."), metrics.height())


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
    """The bottom band: build, project, credit, licence, status.

    Args:
        developer_name: Who to credit.
        developer_url: Where the credit links to.
        parent: Optional Qt parent.

    Signals:
        developer_link_activated: The credit link was clicked. Carries the
            URL. Emitted rather than opened here, so the one place that hands
            a URL to the operating system stays the main window.

    Attributes:
        version_label: The application name and the running version.
        project_label: ``"Project: <title>"``, or ``"No project open"``.
        credit_label: The developer credit, with its link.
        licence_label: ``"Open Source (MIT License)"``, on the right.
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
        self.project_separator = self._small_label(SEPARATOR, "appFooterSeparator")
        self.project_label = self._eliding_label("appFooterProject")

        self.credit_label = self._small_label(
            f'Developed by <a href="{developer_url}">{developer_name}</a>',
            "appFooterCredit",
        )
        self.credit_label.setTextFormat(Qt.TextFormat.RichText)
        self.credit_label.setOpenExternalLinks(False)
        self.credit_label.linkActivated.connect(self.developer_link_activated.emit)

        self.licence_separator = self._small_label(SEPARATOR, "appFooterSeparator")
        self.licence_label = self._small_label(
            f"Open Source ({LICENSE_NAME} License)", "appFooterLicence"
        )
        self.status_separator = self._small_label(SEPARATOR, "appFooterSeparator")

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

        self.set_project_title(None)
        self._apply_tier(FooterTier.FULL)
        self.set_status(AppStatus.READY)

    def _small_label(self, text: str, object_name: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName(object_name)
        label.setFont(self._small_font(label))
        return label

    def _eliding_label(self, object_name: str) -> _ElidingLabel:
        label = _ElidingLabel(object_name, self)
        label.setFont(self._small_font(label))
        return label

    def _small_font(self, label: QLabel) -> QFont:
        font = QFont(label.font())
        font.setPointSizeF(
            max(font.pointSizeF() + FontSize.FOOTER, FontSize.MIN_POINT_SIZE)
        )
        return font

    # ------------------------------------------------------------------
    # The open project
    # ------------------------------------------------------------------
    def set_project_title(self, title: str | None) -> None:
        """Show which project is open.

        Args:
            title: The project's display title, or ``None``/``""`` when none
                is open.

        The *title*, never the path. A project's folder is somewhere several
        directories deep under a drive letter, which on a one-line footer
        would elide to a middle fragment of a path and tell the operator
        nothing. The display title is what they named the
        examination, and it is what they are checking they have open.
        """
        cleaned = (title or "").strip()
        self.project_label.set_full_text(
            f"{PROJECT_PREFIX}{cleaned}" if cleaned else NO_PROJECT_TEXT
        )
        self.project_label.setAccessibleName(
            f"Open project: {cleaned}" if cleaned else NO_PROJECT_TEXT
        )

    @property
    def project_title(self) -> str | None:
        """The open project's title, or ``None`` when none is."""
        text = self.project_label.full_text
        if text == NO_PROJECT_TEXT:
            return None
        return text.removeprefix(PROJECT_PREFIX)

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

    def _status_group_width(self) -> int:
        return (
            self.status_separator.sizeHint().width()
            + Footer.SEPARATOR_MARGIN
            + self.status_dot.width()
            + Footer.STATUS_DOT_GAP
            + self.status_label.sizeHint().width()
        )

    def _width_for(self, tier: FooterTier) -> int:
        """Width the first row needs in ``tier``.

        The project title is counted at its *minimum* - it elides rather than
        forcing a tier change, which is the brief's own instruction: a very
        long examination name must not push the developer credit onto a second
        line when an ellipsis would do.

        Only the first row is measured, because the second row is only ever
        used when the first has already overflowed - it never decides the
        tier.
        """
        width = (
            2 * Footer.H_PADDING
            + self.version_label.sizeHint().width()
            + Footer.SEPARATOR_MARGIN
            + self.project_separator.sizeHint().width()
            + Footer.SEPARATOR_MARGIN
            + self.project_label.minimumSizeHint().width()
            + Footer.SEPARATOR_MARGIN
            + self._status_group_width()
        )
        if tier in (FooterTier.NO_CREDIT, FooterTier.STACKED):
            return width
        width += self.credit_label.sizeHint().width() + Footer.SEPARATOR_MARGIN
        if tier is FooterTier.FULL:
            width += (
                self.licence_separator.sizeHint().width()
                + self.licence_label.sizeHint().width()
                + 2 * Footer.SEPARATOR_MARGIN
            )
        return width

    def tier_for_width(self, available: int) -> FooterTier:
        """The most complete tier that fits in ``available`` pixels."""
        for tier in (FooterTier.FULL, FooterTier.NO_LICENCE, FooterTier.NO_CREDIT):
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

        # Left: the build and the open project, always, in that order.
        self._primary.addWidget(self.version_label)
        self._primary.addWidget(self.project_separator)
        self._primary.addWidget(self.project_label, 1)
        self._primary.addStretch(1)

        # Right: credit, then licence, then status - the brief's own order.
        if tier in (FooterTier.FULL, FooterTier.NO_LICENCE):
            self._primary.addWidget(self.credit_label)
        if tier is FooterTier.FULL:
            self._primary.addWidget(self.licence_separator)
            self._primary.addWidget(self.licence_label)
        self._primary.addWidget(self.status_separator)
        self._primary.addSpacing(max(Footer.STATUS_DOT_GAP - Footer.SEPARATOR_MARGIN, 0))
        self._primary.addWidget(self.status_dot)
        self._primary.addSpacing(max(Footer.STATUS_DOT_GAP - Footer.SEPARATOR_MARGIN, 0))
        self._primary.addWidget(self.status_label)

        if tier is FooterTier.STACKED:
            self._secondary.addWidget(self.licence_label)
            self._secondary.addStretch(1)
            self._secondary.addWidget(self.credit_label)

        for widget, visible in (
            (self.licence_separator, tier is FooterTier.FULL),
            (self.licence_label, tier in (FooterTier.FULL, FooterTier.STACKED)),
            (self.credit_label, tier is not FooterTier.NO_CREDIT),
        ):
            widget.setVisible(visible)

    def sizeHint(self) -> QSize:
        """Room for everything on one row."""
        return QSize(self._width_for(FooterTier.FULL), Footer.HEIGHT)

    def minimumSizeHint(self) -> QSize:
        """Only the version, the project and the status are mandatory.

        The stacked tier is two rows tall, so the minimum height has to allow
        for that or the second row is clipped exactly when it is needed.
        """
        rows = 2 if self._tier is FooterTier.STACKED else 1
        row_height = max(self.version_label.sizeHint().height(), Footer.STATUS_DOT_SIZE)
        return QSize(
            self._width_for(FooterTier.STACKED),
            rows * row_height + (rows + 1) * Footer.V_PADDING,
        )


__all__ = [
    "NO_PROJECT_TEXT",
    "PROJECT_PREFIX",
    "AppStatus",
    "FooterTier",
    "StatusFooter",
]
