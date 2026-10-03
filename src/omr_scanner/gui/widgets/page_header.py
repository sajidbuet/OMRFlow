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
from PySide6.QtGui import QColor, QIcon, QPainter
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
from omr_scanner.gui.ui_scale import scale_layout, scale_widget, set_relative_font


class _PageIcon(QWidget):
    """The stage's glyph, tinted with the brand accent."""

    def __init__(self, icon_name: str, extent: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageHeaderIcon")
        scale_widget(self, fixed_width=extent, fixed_height=extent)
        self._icon = load_icon(icon_name)

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        ratio = self.devicePixelRatioF()
        extent = self.width()
        pixmap = self._icon.pixmap(QSize(extent, extent) * ratio, ratio, QIcon.Mode.Normal)
        tint = QPainter(pixmap)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint.fillRect(pixmap.rect(), QColor(Color.PRIMARY))
        tint.end()
        painter.drawPixmap(0, 0, extent, extent, pixmap)
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
        scale_layout(outer, spacing=Spacing.SM)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        scale_layout(row, spacing=Spacing.MD)

        self.icon = _PageIcon(icon_name, IconSize.PAGE_HEADER, self)
        row.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignTop)

        text_column = QVBoxLayout()
        text_column.setContentsMargins(0, 0, 0, 0)
        scale_layout(text_column, spacing=Spacing.XXS)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("pageTitle")
        set_relative_font(self.title_label, FontSize.PAGE_TITLE, weight=FontWeight.BOLD)
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
        scale_widget(self.rule, fixed_height=Stroke.HAIRLINE)
        outer.addWidget(self.rule)


__all__ = ["PageHeader"]
