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

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from omr_scanner import APPLICATION_NAME, __version__
from omr_scanner.config import AppConfig, load_app_config, save_app_config
from omr_scanner.errors import ConfigurationError, OMRScannerError
from omr_scanner.gui.about_dialog import DEVELOPER_NAME, AboutDialog
from omr_scanner.gui.branding import LOGO_ASPECT_RATIO, application_icon, logo_svg_path
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.pages import WORKFLOW_PAGES, PlaceholderPage, ProjectPage
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.gui.template_designer.page import TemplateDesignerPage
from omr_scanner.services import ProjectSession, create_project, open_project

logger = logging.getLogger(__name__)

WINDOW_MIN_WIDTH = 960
WINDOW_MIN_HEIGHT = 640
NAVIGATION_WIDTH = 190

NO_PROJECT_STATUS = "No project open"
STATUS_MESSAGE_MS = 5000
"""How long transient status bar messages stay visible."""

LOGO_DISPLAY_WIDTH = 130
"""Logo width in the sidebar, in logical (DPI-independent) pixels - within the
110-150px range a small application-branding mark should occupy without
crowding the fixed-width sidebar. Height follows from `LOGO_ASPECT_RATIO` so
the original artwork is never stretched."""

SIDEBAR_LOGO_BOTTOM_MARGIN = 20
"""Gap between the logo and the bottom of the sidebar, in logical pixels."""

DEVELOPER_URL = "https://www.sajid.bd"


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
        # In addition to `QApplication.setWindowIcon()` (the taskbar/dock
        # default), this window's own title bar reads its icon from here.
        self.setWindowIcon(application_icon())

        self._build_central_widget()
        self._build_menus()
        self._build_status_bar()
        self._broadcast_project_change()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_central_widget(self) -> None:
        """Assemble the navigation/workflow area and the footer.

        No separate header row: the logo lives inside the sidebar (see
        `_build_sidebar`), so the workflow area starts directly below the
        menu bar and the main page content starts near the top of the
        window, not below a dedicated branding strip.
        """
        central = QWidget(self)
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        outer_layout.addLayout(self._build_workflow_area(), stretch=1)
        outer_layout.addWidget(self._build_footer())

        self.setCentralWidget(central)

    def _build_workflow_area(self) -> QHBoxLayout:
        """Create the navigation sidebar and the stacked workflow pages."""
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()

        for index, spec in enumerate(WORKFLOW_PAGES):
            page: WorkflowPage
            if spec.key == "project":
                project_page = ProjectPage(spec)
                project_page.create_requested.connect(self._prompt_create_project)
                project_page.open_requested.connect(self._prompt_open_project)
                page = project_page
            elif spec.key == "template":
                page = TemplateDesignerPage(spec)
            elif spec.key == "scan":
                page = ScanPage(spec)
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

        layout.addWidget(self.stack, stretch=1)
        return layout

    def _build_sidebar(self) -> QWidget:
        """Build the fixed-width sidebar: the navigation list, then the logo.

        ``navigation`` (unchanged - same widget, same object name, same row
        content/behaviour, still `NAVIGATION_WIDTH` wide via the sidebar
        container) is given the layout's whole stretch factor, so it claims
        every bit of vertical space the fixed-size logo below it does not
        need - equivalent to "navigation, then an expanding spacer, then the
        logo" without an extra invisible widget. A `QSvgWidget` renders the
        vector logo directly rather than a pre-rasterised bitmap, so it stays
        crisp at any Windows display scaling (100/125/150/200%).
        """
        sidebar = QWidget()
        sidebar.setObjectName("workflowSidebar")
        sidebar.setFixedWidth(NAVIGATION_WIDTH)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, SIDEBAR_LOGO_BOTTOM_MARGIN)
        layout.setSpacing(0)

        self.navigation = QListWidget()
        self.navigation.setObjectName("workflowNavigation")
        self.navigation.setAlternatingRowColors(True)
        layout.addWidget(self.navigation, stretch=1)

        logo_height = round(LOGO_DISPLAY_WIDTH / LOGO_ASPECT_RATIO)
        self.logo_widget = QSvgWidget(str(logo_svg_path()))
        self.logo_widget.setObjectName("appLogo")
        self.logo_widget.setFixedSize(LOGO_DISPLAY_WIDTH, logo_height)
        # A transparent background lets the logo sit directly on the
        # sidebar's own background rather than inside a coloured box.
        self.logo_widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout.addWidget(self.logo_widget, alignment=Qt.AlignmentFlag.AlignHCenter)

        return sidebar

    def _build_footer(self) -> QWidget:
        """Build the small, centred developer-credit footer.

        A separate widget from `statusBar()` (which keeps showing transient
        workflow messages and the permanent project indicator, untouched) -
        this row sits just above it, inside the central widget, so it is
        always the last thing before the native status bar rather than a
        second competing status bar.
        """
        footer = QWidget()
        footer.setObjectName("appFooter")
        footer.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addStretch(1)

        self.footer_label = QLabel(
            f'Developed by <a href="{DEVELOPER_URL}">{DEVELOPER_NAME}</a>'
        )
        self.footer_label.setObjectName("appFooterLabel")
        self.footer_label.setTextFormat(Qt.TextFormat.RichText)
        self.footer_label.setOpenExternalLinks(False)  # routed through the opener below instead
        self.footer_label.linkActivated.connect(self._open_developer_site)
        # `QLabel` already shows a pointing-hand cursor over an embedded `<a>`
        # link by default (its `textInteractionFlags()` include
        # `LinksAccessibleByMouse` out of the box) - nothing extra needed here.
        small_font = self.footer_label.font()
        small_font.setPointSizeF(max(small_font.pointSizeF() - 1.0, 7.0))
        self.footer_label.setFont(small_font)
        layout.addWidget(self.footer_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(1)
        return footer

    def _open_developer_site(self, url: str) -> None:
        """Open the developer's site in the system's default browser.

        Never navigates inside the application itself - `QDesktopServices`
        hands the URL straight to the OS, exactly as clicking it in any other
        desktop application would.
        """
        QDesktopServices.openUrl(QUrl(url))

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
        """Show the About dialog: identity, authorship and licence."""
        AboutDialog(self).exec()

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
