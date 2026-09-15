"""The Project page: the only stage with real functionality in Phase 0.

Purpose:
    Show the metadata of the open project, or an invitation to create/open one.

Responsibilities:
    * Render :class:`~omr_scanner.services.ProjectSession` state.
    * Emit :attr:`ProjectPage.create_requested` / :attr:`ProjectPage.open_requested`
      so the main window - which owns the project lifecycle - performs the action.

What does NOT belong here:
    * Calling the project service. A page that opened projects itself would give
      the application two places where a project can change.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.pages.catalog import WorkflowPageSpec
from omr_scanner.services import ProjectSession

NO_PROJECT_TEXT = "No project is open. Create a new project or open an existing one."

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M UTC"


class ProjectPage(WorkflowPage):
    """Displays the open project and offers the create/open commands."""

    create_requested = Signal()
    """Emitted when the user asks to create a project."""

    open_requested = Signal()
    """Emitted when the user asks to open an existing project."""

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent)

        self._empty_label = QLabel(NO_PROJECT_TEXT)
        self._empty_label.setWordWrap(True)
        self.body.addWidget(self._empty_label)

        buttons = QHBoxLayout()
        self.create_button = QPushButton("Create project...")
        self.create_button.clicked.connect(self.create_requested.emit)
        self.open_button = QPushButton("Open project...")
        self.open_button.clicked.connect(self.open_requested.emit)
        buttons.addWidget(self.create_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        self.body.addLayout(buttons)

        self._details = QWidget()
        self._form = QFormLayout(self._details)
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.body.addWidget(self._details)
        self._details.setVisible(False)

        self._value_labels: dict[str, QLabel] = {}
        for caption in ("Name", "Description", "Location", "Project ID", "Created", "Modified"):
            value = QLabel("")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value.setWordWrap(True)
            self._form.addRow(f"{caption}:", value)
            self._value_labels[caption] = value

    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Show the open project's metadata, or the empty-state message."""
        if session is None:
            self._details.setVisible(False)
            self._empty_label.setVisible(True)
            return

        metadata = session.project.metadata
        self._value_labels["Name"].setText(metadata.name)
        self._value_labels["Description"].setText(metadata.description or "-")
        self._value_labels["Location"].setText(str(session.root))
        self._value_labels["Project ID"].setText(metadata.project_id)
        self._value_labels["Created"].setText(metadata.created_at.strftime(TIMESTAMP_FORMAT))
        self._value_labels["Modified"].setText(metadata.modified_at.strftime(TIMESTAMP_FORMAT))

        self._empty_label.setVisible(False)
        self._details.setVisible(True)
