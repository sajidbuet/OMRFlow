"""Common behaviour for workflow pages.

Purpose:
    Give every page the same header, the same margins and one hook for reacting
    to a project being opened or closed.

What does NOT belong here:
    * Service calls. A page receives the open session; it does not open one.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from omr_scanner.gui.pages.catalog import WorkflowPageSpec
from omr_scanner.services import ProjectSession

CONTENT_MARGIN_PX = 24
CONTENT_SPACING_PX = 12


class WorkflowPage(QWidget):
    """Base class for every page in the main window's stacked view.

    Subclasses add their own widgets to :attr:`body` and may override
    :meth:`on_project_changed`.

    Args:
        spec: The workflow stage this page represents.
        parent: Optional Qt parent.
        expand: When ``True``, :attr:`body` is given all the page's remaining
            vertical space instead of shrinking to its contents' size hint with
            a trailing spacer below. Every page before Phase 2 was a short
            column of labels and buttons, for which the spacer reads as
            "aligned to the top"; a page whose body *is* a full-size editor
            (the template designer's canvas) needs the opposite, or the canvas
            renders squeezed into a sliver with empty space beneath it.
    """

    def __init__(
        self, spec: WorkflowPageSpec, parent: QWidget | None = None, *, expand: bool = False
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self._expand = expand

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            CONTENT_MARGIN_PX, CONTENT_MARGIN_PX, CONTENT_MARGIN_PX, CONTENT_MARGIN_PX
        )
        layout.setSpacing(CONTENT_SPACING_PX)

        title = QLabel(spec.title)
        title.setObjectName("pageTitle")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 6)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        summary = QLabel(spec.summary)
        summary.setObjectName("pageSummary")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(separator)

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

        The default implementation does nothing; pages that display project data
        override it. Called by the main window for every page, so a page never
        has to poll for the current project.
        """

    def add_note(self, text: str) -> QLabel:
        """Append a wrapped, dimmed note to the page body and return it."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setObjectName("pageNote")
        self.body.addWidget(label)
        return label
