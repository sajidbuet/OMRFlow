"""Common behaviour for workflow pages.

Purpose:
    Give every page the same heading, the same margins and one hook for
    reacting to a project being opened or closed.

What does NOT belong here:
    * Service calls. A page receives the open session; it does not open one.
    * The heading's own construction. That is
      :class:`~omr_scanner.gui.widgets.page_header.PageHeader`, which the
      Project page's dashboard also uses - the two have to match, and they do
      because there is one of them.

Why the default margin is smaller than it was:
    Removing the fixed left sidebar returned roughly 190 logical pixels of
    width to every page. Spending part of that on a wider margin would be
    exactly the wrong trade: the brief asks for the reclaimed width to become
    workspace, and the pages that need it most - template, calibrate, scan,
    resolve - are the ones showing sheet images at zoom.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from omr_scanner.gui.pages.catalog import WorkflowPageSpec
from omr_scanner.gui.theme import Spacing
from omr_scanner.gui.widgets.page_header import PageHeader
from omr_scanner.services import ProjectSession

CONTENT_MARGIN_PX = Spacing.PAGE_MARGIN
CONTENT_SPACING_PX = Spacing.MD

COMPACT_MARGIN_PX = Spacing.PAGE_MARGIN_COMPACT
COMPACT_SPACING_PX = Spacing.SM
"""Margins for a page whose body is a full-size editor rather than a short
column of explanatory text. The defaults above frame a paragraph; on the
template designer they were spending roughly a sixth of the window's height
before the canvas began."""


class WorkflowPage(QWidget):
    """Base class for every page in the main window's stacked view.

    Subclasses add their own widgets to :attr:`body` and may override
    :meth:`on_project_changed`.

    Args:
        spec: The workflow stage this page represents.
        parent: Optional Qt parent.
        expand: When ``True``, :attr:`body` is given all the page's remaining
            vertical space instead of shrinking to its contents' size hint
            with a trailing spacer below. A page that is a short column of
            labels and buttons wants the spacer, which reads as "aligned to
            the top"; a page whose body *is* a full-size editor (the template
            designer's canvas) needs the opposite, or the canvas renders as a
            sliver with empty space beneath it.
        show_summary: When ``True`` (the default),
            :attr:`WorkflowPageSpec.summary` gets its own word-wrapped row
            under the title. That row is the *content* of a placeholder page
            and belongs there; on a page whose body is a full-size editor it
            is a permanent band of text the operator reads once and then works
            around forever. ``False`` keeps the sentence as the title's
            tooltip and status tip, so it stays discoverable without costing a
            row.
        compact: Use :data:`COMPACT_MARGIN_PX`/:data:`COMPACT_SPACING_PX`
            instead of the defaults, for the same reason.
        hero: Show the decorative tagline beside the heading. The Project
            page's dashboard uses it; nothing else should.
        show_header: When ``False``, no heading is built here and
            :attr:`header` is ``None``. For a page that puts the heading
            *inside* its own layout rather than above it - the Project page's
            dashboard has the heading in its left-hand column, beside the
            right-hand column rather than spanning over it.

    Attributes:
        header: The :class:`~omr_scanner.gui.widgets.page_header.PageHeader`,
            or ``None`` when ``show_header`` was false.
        title_widget: The heading label, or ``None``.
        summary_widget: The summary label, or ``None``.
        body: Where subclasses add their content.
    """

    def __init__(
        self,
        spec: WorkflowPageSpec,
        parent: QWidget | None = None,
        *,
        expand: bool = False,
        show_summary: bool = True,
        compact: bool = False,
        hero: bool = False,
        show_header: bool = True,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self._expand = expand

        margin = COMPACT_MARGIN_PX if compact else CONTENT_MARGIN_PX
        spacing = COMPACT_SPACING_PX if compact else CONTENT_SPACING_PX

        layout = QVBoxLayout(self)
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(spacing)

        self.header: PageHeader | None = None
        if show_header:
            self.header = PageHeader(
                spec.title,
                spec.summary,
                spec.icon,
                self,
                show_summary=show_summary,
                hero=hero,
            )
            layout.addWidget(self.header)

        self.title_widget = self.header.title_label if self.header else None
        self.summary_widget = self.header.summary_label if self.header else None

        self.body = QVBoxLayout()
        self.body.setSpacing(CONTENT_SPACING_PX)
        if expand:
            layout.addLayout(self.body, 1)
        else:
            layout.addLayout(self.body)
            layout.addStretch(1)

        self._layout = layout

    def on_project_changed(self, session: ProjectSession | None) -> None:
        """React to a project being opened (``session``) or closed (``None``).

        The default implementation does nothing; pages that display project
        data override it. Called by the main window for every page, so a page
        never has to poll for the current project.
        """

    def add_note(self, text: str) -> QLabel:
        """Append a wrapped, dimmed note to the page body and return it."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setObjectName("pageNote")
        self.body.addWidget(label)
        return label
