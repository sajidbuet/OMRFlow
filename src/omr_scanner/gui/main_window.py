"""The application main window.

Purpose:
    Host the workflow navigation, own the project lifecycle from the user
    interface side, and route menu commands to services.

Responsibilities:
    * Build the navigation list, the stacked pages, the menus and the status bar.
    * Hold the single open :class:`~omr_scanner.services.ProjectSession` and
      broadcast changes to every page.
    * Keep the recent-project list in the application configuration up to date.

What does NOT belong here:
    * Any project/database/imaging logic. Every action delegates to
      :mod:`omr_scanner.services`.
    * Long-running work. Batch processing (Phase 5) runs in a worker thread and
      reports progress through signals; the main window must never block.

Testability:
    Commands are split in two: ``_prompt_*`` methods own the modal dialogs, and
    :meth:`create_project_at` / :meth:`open_project_at` / :meth:`close_project`
    contain the behaviour. GUI tests drive the second group, so no test has to
    interact with a native file dialog.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from omr_scanner import APPLICATION_NAME, __version__
from omr_scanner.config import AppConfig, load_app_config, save_app_config
from omr_scanner.errors import ConfigurationError, OMRScannerError
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.pages import WORKFLOW_PAGES, PlaceholderPage, ProjectPage
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.services import ProjectSession, create_project, open_project

logger = logging.getLogger(__name__)

WINDOW_MIN_WIDTH = 960
WINDOW_MIN_HEIGHT = 640
NAVIGATION_WIDTH = 190

NO_PROJECT_STATUS = "No project open"
STATUS_MESSAGE_MS = 5000
"""How long transient status bar messages stay visible."""

ABOUT_TEXT = (
    f"<b>{APPLICATION_NAME}</b> {__version__}<br><br>"
    "Template-driven OMR examination processing and result management.<br><br>"
    "Development status: Phase 0 - architecture and repository foundation. "
    "Recognition, reconciliation, scoring and reporting are not implemented yet."
)


class MainWindow(QMainWindow):
    """Main application window.

    Args:
        config: Application configuration. Loaded from disk when omitted.
        config_path: Where configuration changes are written. Defaults to the
            per-user location; tests pass a temporary path.
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        config_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._config = config if config is not None else load_app_config()
        self._config_path = config_path
        self._session: ProjectSession | None = None
        self._pages: dict[str, WorkflowPage] = {}

        self.setWindowTitle(APPLICATION_NAME)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        self._build_central_widget()
        self._build_menus()
        self._build_status_bar()
        self._broadcast_project_change()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_central_widget(self) -> None:
        """Create the navigation list and the stacked workflow pages."""
        central = QWidget(self)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.navigation = QListWidget()
        self.navigation.setObjectName("workflowNavigation")
        self.navigation.setFixedWidth(NAVIGATION_WIDTH)
        self.navigation.setAlternatingRowColors(True)

        self.stack = QStackedWidget()

        for index, spec in enumerate(WORKFLOW_PAGES):
            page: WorkflowPage
            if spec.key == "project":
                project_page = ProjectPage(spec)
                project_page.create_requested.connect(self._prompt_create_project)
                project_page.open_requested.connect(self._prompt_open_project)
                page = project_page
            else:
                page = PlaceholderPage(spec)

            self._pages[spec.key] = page
            self.stack.addWidget(page)

            item = QListWidgetItem(f"{index + 1}. {spec.title}")
            item.setData(Qt.ItemDataRole.UserRole, spec.key)
            if not spec.is_implemented:
                item.setToolTip(f"Planned for phase {spec.phase}")
            self.navigation.addItem(item)

        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.navigation.setCurrentRow(0)

        layout.addWidget(self.navigation)
        layout.addWidget(self.stack, stretch=1)
        self.setCentralWidget(central)

    def _build_menus(self) -> None:
        """Create the File and Help menus."""
        file_menu = self.menuBar().addMenu("&File")

        self.new_project_action = QAction("&New Project...", self)
        self.new_project_action.setShortcut(QKeySequence.StandardKey.New)
        self.new_project_action.triggered.connect(self._prompt_create_project)
        file_menu.addAction(self.new_project_action)

        self.open_project_action = QAction("&Open Project...", self)
        self.open_project_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_project_action.triggered.connect(self._prompt_open_project)
        file_menu.addAction(self.open_project_action)

        self.recent_menu = file_menu.addMenu("Open &Recent")
        self._rebuild_recent_menu()

        file_menu.addSeparator()

        self.close_project_action = QAction("&Close Project", self)
        self.close_project_action.setEnabled(False)
        self.close_project_action.triggered.connect(self.close_project)
        file_menu.addAction(self.close_project_action)

        file_menu.addSeparator()

        self.exit_action = QAction("E&xit", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

        help_menu = self.menuBar().addMenu("&Help")
        self.about_action = QAction(f"&About {APPLICATION_NAME}", self)
        self.about_action.triggered.connect(self._show_about)
        help_menu.addAction(self.about_action)

    def _build_status_bar(self) -> None:
        """Create the status bar and its permanent project indicator."""
        self._project_status = QLabel(NO_PROJECT_STATUS)
        self.statusBar().addPermanentWidget(self._project_status)
        self.statusBar().showMessage(f"{APPLICATION_NAME} {__version__} ready")

    # ------------------------------------------------------------------
    # Project lifecycle (no dialogs - driven by tests and by the prompts below)
    # ------------------------------------------------------------------
    @property
    def session(self) -> ProjectSession | None:
        """The open project session, or ``None`` when no project is open."""
        return self._session

    def create_project_at(self, parent_directory: Path, name: str) -> bool:
        """Create a project and open it, replacing any currently open project.

        Args:
            parent_directory: Folder that will contain the new project folder.
            name: Display name of the examination.

        Returns:
            ``True`` when the project was created and opened, ``False`` when the
            attempt failed. Failures are reported to the user and logged; they
            never propagate into Qt's event loop.
        """
        try:
            session = create_project(parent_directory, name)
        except OMRScannerError as exc:
            report_error(self, exc, context="Create project")
            return False
        self._adopt_session(session)
        return True

    def open_project_at(self, directory: Path) -> bool:
        """Open an existing project, replacing any currently open project.

        Args:
            directory: Project root directory.

        Returns:
            ``True`` on success, ``False`` when the directory is not a valid
            project or its database could not be opened.
        """
        try:
            session = open_project(directory)
        except OMRScannerError as exc:
            report_error(self, exc, context="Open project")
            self._forget_recent_project(directory)
            return False
        self._adopt_session(session)
        return True

    def close_project(self) -> None:
        """Close the open project, releasing its database and log handler."""
        if self._session is None:
            return
        self._session.close()
        self._session = None
        self._broadcast_project_change()
        self.statusBar().showMessage("Project closed", STATUS_MESSAGE_MS)

    # ------------------------------------------------------------------
    # Dialog-owning commands
    # ------------------------------------------------------------------
    def _prompt_create_project(self) -> None:
        """Ask for a location and a name, then create the project."""
        start_dir = self._config.default_projects_root or Path.home()
        parent_directory = QFileDialog.getExistingDirectory(
            self, "Select the folder that will contain the project", str(start_dir)
        )
        if not parent_directory:
            return

        name, accepted = QInputDialog.getText(self, "New project", "Project name:")
        if not accepted or not name.strip():
            return

        self.create_project_at(Path(parent_directory), name.strip())

    def _prompt_open_project(self) -> None:
        """Ask for a project folder, then open it."""
        start_dir = self._config.default_projects_root or Path.home()
        directory = QFileDialog.getExistingDirectory(
            self, "Open project folder", str(start_dir)
        )
        if not directory:
            return
        self.open_project_at(Path(directory))

    def _show_about(self) -> None:
        """Show the About box."""
        QMessageBox.about(self, f"About {APPLICATION_NAME}", ABOUT_TEXT)

    # ------------------------------------------------------------------
    # Internal state propagation
    # ------------------------------------------------------------------
    def _adopt_session(self, session: ProjectSession) -> None:
        """Replace the current session and refresh the whole window."""
        if self._session is not None:
            self._session.close()
        self._session = session
        self._remember_recent_project(session.root)
        self._broadcast_project_change()
        self.statusBar().showMessage(f"Project '{session.name}' is open", STATUS_MESSAGE_MS)

    def _broadcast_project_change(self) -> None:
        """Push the current session to every page and update chrome."""
        for page in self._pages.values():
            page.on_project_changed(self._session)

        has_project = self._session is not None
        self.close_project_action.setEnabled(has_project)

        if self._session is None:
            self.setWindowTitle(APPLICATION_NAME)
            self._project_status.setText(NO_PROJECT_STATUS)
        else:
            self.setWindowTitle(f"{self._session.name} - {APPLICATION_NAME}")
            self._project_status.setText(f"{self._session.name}  ({self._session.root})")

    # ------------------------------------------------------------------
    # Recent projects
    # ------------------------------------------------------------------
    def _remember_recent_project(self, directory: Path) -> None:
        """Promote ``directory`` in the recent list and persist the change."""
        self._config = self._config.with_recent_project(directory)
        self._persist_config()
        self._rebuild_recent_menu()

    def _forget_recent_project(self, directory: Path) -> None:
        """Drop a project that could not be opened from the recent list."""
        if directory.resolve() not in self._config.recent_projects:
            return
        self._config = self._config.without_recent_project(directory)
        self._persist_config()
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        """Rebuild the "Open Recent" submenu from the configuration."""
        self.recent_menu.clear()
        if not self._config.recent_projects:
            empty = QAction("(none)", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return

        for directory in self._config.recent_projects:
            action = QAction(str(directory), self)
            action.triggered.connect(
                lambda _checked=False, path=directory: self.open_project_at(path)
            )
            self.recent_menu.addAction(action)

    def _persist_config(self) -> None:
        """Save the configuration, treating failure as non-fatal."""
        try:
            save_app_config(self._config, self._config_path)
        except ConfigurationError as exc:
            logger.warning("Could not save application configuration: %s", exc)

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        """Close the open project before the window disappears.

        Releasing the SQLite handle here (rather than in ``__del__``) guarantees
        the project folder is not locked after the window closes, which matters
        on Windows.
        """
        self.close_project()
        logger.info("Main window closed")
        super().closeEvent(event)
