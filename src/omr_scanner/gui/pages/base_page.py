"""Common behaviour for workflow pages.

Purpose:
    Give every page the same margins and one hook for reacting to a project
    being opened or closed.

What does NOT belong here:
    * Service calls. A page receives the open session; it does not open one.
    * A heading naming the stage. See below.

Why no page has a stage heading any more:
    The workflow ribbon at the top of the window already says, in accent
    colour, which of the nine stages is open. A large "Reports" underneath it
    repeated that in a full row of vertical space - on every stage, on every
    screen, forever - and on a 1366x768 display those rows are the ones the
    Scan preview and the Template canvas most need back. ``show_header`` is
    therefore ``False`` by default. It is not removed, because a page may
    still have a genuine reason to head *itself* (the Project dashboard puts
    a heading inside a card, beside its hero artwork), and because the
    distinction worth keeping is between a heading that repeats the ribbon and
    one that says something the ribbon does not.

    Functional section headings inside a page - "Result Management",
    "Scoring configuration", "Conflict queue" - are untouched by this. They
    describe parts of a page rather than the page itself.

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
        show_summary: Only consulted when ``show_header`` is ``True``. When
            ``True``, :attr:`WorkflowPageSpec.summary` gets its own
            word-wrapped row under the title; when ``False`` the sentence
            survives as the title's tooltip and status tip.
        compact: Use :data:`COMPACT_MARGIN_PX`/:data:`COMPACT_SPACING_PX`
            instead of the defaults, for a page whose body is a full-size
            editor rather than a short column of explanatory text.
        show_header: Whether to build a heading naming the stage. ``False``
            by default - see the module docstring. A page that passes ``True``
            is asserting that its heading says something the workflow ribbon
            does not.

    Attributes:
        header: The :class:`~omr_scanner.gui.widgets.page_header.PageHeader`,
            or ``None`` - which it is on every stage unless the page opted in.
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
        show_header: bool = False,
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
