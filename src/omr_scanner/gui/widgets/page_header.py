"""A heading a page may give itself: icon, title, summary, hairline rule.

Purpose:
    Provide one heading shape for the rare page that genuinely needs to head
    itself, so that two such pages cannot drift apart.

Responsibilities:
    * :class:`PageHeader` - the heading row and its rule.

What does NOT belong here:
    * Page content. This is the band above it.

Why this is no longer above every stage:
    It used to be, and the workflow ribbon made it redundant: the ribbon
    names the current stage in accent colour at the top of the window, so a
    large "Reports" immediately beneath it said the same thing twice and cost
    a row of vertical space on all nine stages. ``WorkflowPage`` therefore
    builds no heading unless a page asks for one - see
    :mod:`omr_scanner.gui.pages.base_page`. Nothing in the application
    currently does; this stays because "heading that repeats the ribbon" and
    "heading that says something else" are different things, and only the
    first was wrong.

Why the summary can be turned off:
    On a stage whose body is a full-size editor - the template designer's
    canvas, the scan preview - a permanent sentence of explanatory text is a
    row the operator reads once and then works around for the rest of the
    session. With ``show_summary=False`` the sentence survives as the title's
    tooltip and status tip, so it is still discoverable, without costing a row
    of the workspace those pages need most.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.theme import (
    Color,
    FontSize,
    FontWeight,
    IconSize,
    Spacing,
    Stroke,
)


class _PageIcon(QWidget):
    """The stage's glyph, tinted with the brand accent."""

    def __init__(self, icon_name: str, extent: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageHeaderIcon")
        self.setFixedSize(extent, extent)
        self._icon = load_icon(icon_name)
        self._extent = extent

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ratio = self.devicePixelRatioF()
        pixmap = self._icon.pixmap(
            QSize(self._extent, self._extent) * ratio, ratio, QIcon.Mode.Normal
        )
        tint = QPainter(pixmap)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint.fillRect(pixmap.rect(), QColor(Color.PRIMARY))
        tint.end()
        painter.drawPixmap(0, 0, self._extent, self._extent, pixmap)
        painter.end()


class PageHeader(QWidget):
    """A page's heading: icon, title, summary, and a hairline rule beneath.

    Args:
        title: The heading.
        summary: One line describing what the page is for.
        icon_name: Bundled Lucide icon name.
        parent: Optional Qt parent.
        show_summary: Whether the summary gets its own row - see the module
            docstring.

    Attributes:
        title_label: The heading.
        summary_label: The summary row, or ``None`` when not shown.
    """

    def __init__(
        self,
        title: str,
        summary: str,
        icon_name: str,
        parent: QWidget | None = None,
        *,
        show_summary: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("pageHeader")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(Spacing.SM)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Spacing.MD)

        self.icon = _PageIcon(icon_name, IconSize.PAGE_HEADER, self)
        row.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignTop)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(Spacing.XXS)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("pageTitle")
        title_font = QFont(self.title_label.font())
        title_font.setPointSizeF(title_font.pointSizeF() + FontSize.PAGE_TITLE)
        title_font.setWeight(QFont.Weight(FontWeight.BOLD))
        self.title_label.setFont(title_font)
        text_column.addWidget(self.title_label)

        self.summary_label: QLabel | None = None
        if show_summary:
            self.summary_label = QLabel(summary, self)
            self.summary_label.setObjectName("pageSummary")
            self.summary_label.setWordWrap(True)
            text_column.addWidget(self.summary_label)
        else:
            self.title_label.setToolTip(summary)
            self.title_label.setStatusTip(summary)

        row.addLayout(text_column, 1)
        outer.addLayout(row)

        self.rule = QFrame(self)
        self.rule.setObjectName("pageHeaderRule")
        self.rule.setFrameShape(QFrame.Shape.NoFrame)
        self.rule.setFixedHeight(Stroke.HAIRLINE)
        outer.addWidget(self.rule)


__all__ = ["PageHeader"]
