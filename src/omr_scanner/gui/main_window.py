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
from typing import TYPE_CHECKING, Literal

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
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from omr_scanner import APPLICATION_NAME, __version__
from omr_scanner.config import AppConfig, ProcessingSettings, load_app_config, save_app_config
from omr_scanner.errors import ConfigurationError, OMRScannerError
from omr_scanner.gui.about_dialog import DEVELOPER_NAME, AboutDialog
from omr_scanner.gui.answer_key.page import AnswerKeyPage
from omr_scanner.gui.attendance.page import AttendancePage
from omr_scanner.gui.branding import LOGO_ASPECT_RATIO, application_icon, logo_svg_path
from omr_scanner.gui.calibration.page import CalibrationPage
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.health_dialog import ProjectHealthDialog
from omr_scanner.gui.pages import WORKFLOW_PAGES, PlaceholderPage, ProjectPage
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.reports.page import ReportsPage
from omr_scanner.gui.results.page import ResultsPage
from omr_scanner.gui.review.page import ResolvePage
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.gui.settings_dialog import SettingsDialog
from omr_scanner.gui.template_designer.page import TemplateDesignerPage
from omr_scanner.services import (
    ProjectSession,
    create_project,
    open_project,
    recover_interrupted,
    review_store,
)
from omr_scanner.services.project_lock import ProjectLockHeldError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.evaluation.ground_truth import DatasetManifest
    from omr_scanner.gui.devtools import GenerationRequest

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

GENERATION_SHUTDOWN_TIMEOUT_MS = 30_000
"""How long the window waits for a cancelled dataset generation to finish.

A cancel stops between sheets, not inside one, so the wait only ever covers the
page currently being drawn. Generous because a 600-dpi page on a slow disk is
still a fraction of a second, and a thread outliving its window is not."""


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
        self._broadcast_config_change()

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
            elif spec.key == "calibration":
                calibration_page = CalibrationPage(spec)
                calibration_page.edit_template_requested.connect(self.edit_template)
                page = calibration_page
            elif spec.key == "scan":
                scan_page = ScanPage(spec)
                scan_page.review_requested.connect(self.review_batch)
                scan_page.batch_finished.connect(self._on_batch_finished)
                page = scan_page
            elif spec.key == "resolve":
                page = ResolvePage(spec)
            elif spec.key == "attendance":
                page = AttendancePage(spec)
            elif spec.key == "answer_key":
                answer_key_page = AnswerKeyPage(spec)
                # A key is written on one stage and used on another. Without
                # these the Results stage goes on reporting "verified answer
                # keys: none" - and its stored results go on looking current -
                # until something else happens to rebuild its table.
                answer_key_page.key_saved.connect(self._on_answer_key_changed)
                answer_key_page.key_verified.connect(self._on_answer_key_changed)
                page = answer_key_page
            elif spec.key == "results":
                results_page = ResultsPage(spec)
                # A recomputed result must be reflected the next time the
                # Reports stage is opened - the same "one stage changes what
                # another stage shows" rule as the answer-key wiring above.
                results_page.scored.connect(self._on_results_changed)
                page = results_page
            elif spec.key == "reports":
                page = ReportsPage(spec)
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
        """Create the File, Tools and Help menus."""
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

        self.settings_action = QAction("&Settings...", self)
        self.settings_action.setObjectName("settingsAction")
        self.settings_action.setShortcut(QKeySequence.StandardKey.Preferences)
        self.settings_action.setStatusTip(
            "Application preferences, including how many CPU workers batch processing uses"
        )
        self.settings_action.triggered.connect(self.show_settings)
        file_menu.addAction(self.settings_action)

        file_menu.addSeparator()

        self.exit_action = QAction("E&xit", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

        tools_menu = self.menuBar().addMenu("&Tools")

        self.project_health_action = QAction("&Project Health / Recovery...", self)
        self.project_health_action.setObjectName("projectHealthAction")
        self.project_health_action.setEnabled(False)
        self.project_health_action.setStatusTip(
            "Check database integrity and create or restore a project backup"
        )
        self.project_health_action.triggered.connect(self._prompt_project_health)
        tools_menu.addAction(self.project_health_action)
        tools_menu.addSeparator()

        developer_menu = tools_menu.addMenu("&Developer / Testing")

        self.generate_dataset_action = QAction("&Generate Synthetic Test Dataset...", self)
        self.generate_dataset_action.setObjectName("generateDatasetAction")
        self.generate_dataset_action.setStatusTip(
            "Render a labelled synthetic dataset from a template, for testing recognition"
        )
        self.generate_dataset_action.triggered.connect(self.generate_dataset)
        developer_menu.addAction(self.generate_dataset_action)

        self.run_benchmark_action = QAction("Run Recognition &Benchmark...", self)
        self.run_benchmark_action.setObjectName("runBenchmarkAction")
        self.run_benchmark_action.setStatusTip(
            "Score recognition against a labelled dataset, in the Scan stage"
        )
        self.run_benchmark_action.triggered.connect(self._prompt_run_benchmark)
        developer_menu.addAction(self.run_benchmark_action)

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

    @property
    def config(self) -> AppConfig:
        """The application configuration this window is working from."""
        return self._config

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

    def open_project_resolving_lock(
        self, directory: Path, *, action: Literal["read_only", "force"]
    ) -> bool:
        """Open a project whose lock conflict has already been shown to a human.

        Args:
            directory: Project root directory.
            action: The choice already made - ``"read_only"`` or ``"force"``
                (remove the existing lock and open for editing). There is
                deliberately no ``"cancel"`` value: a cancel is simply not
                calling this method at all.

        Returns:
            Whether the project was opened.

        Testable on its own, with no dialog involved - the dialog that
        produces ``action`` is :meth:`_prompt_open_project`'s job. Splitting
        the two is the same convention this codebase already uses wherever a
        choice requires a modal dialog but the resulting action must still be
        exercised directly by a test (see, for Phase 9,
        `gui.reports.page.ReportsPage.associate_template` beside its
        dialog-owning `prompt_select_template`) - a modal opened from inside
        a method a test calls directly hangs a headless run indefinitely,
        which is exactly the defect class this split exists to prevent.
        """
        try:
            session = open_project(
                directory,
                read_only=(action == "read_only"),
                force_lock=(action == "force"),
            )
        except OMRScannerError as exc:
            report_error(self, exc, context="Open project")
            return False
        self._adopt_session(session)
        if action == "read_only":
            self.statusBar().showMessage(
                "Project opened read-only - no changes can be saved.", STATUS_MESSAGE_MS
            )
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
        """Ask for a project folder, then open it.

        A lock conflict gets its own follow-up dialog here, rather than
        inside :meth:`open_project_at`, precisely so that method stays free
        of modal dialogs and safe to call directly from a test (see the
        module docstring's "Testability" section).
        """
        start_dir = self._config.default_projects_root or Path.home()
        directory = QFileDialog.getExistingDirectory(
            self, "Open project folder", str(start_dir)
        )
        if not directory:
            return
        path = Path(directory)
        try:
            session = open_project(path)
        except ProjectLockHeldError as exc:
            self._prompt_resolve_lock_conflict(path, exc)
            return
        except OMRScannerError as exc:
            report_error(self, exc, context="Open project")
            self._forget_recent_project(path)
            return
        self._adopt_session(session)

    def _prompt_resolve_lock_conflict(
        self, directory: Path, conflict: ProjectLockHeldError
    ) -> None:
        """Show the lock-conflict choices and act on whichever one is picked.

        Never removes the lock or opens read-only on its own (Phase 10, §3):
        every choice here is the explicit human decision the phase brief
        requires before a lock - even one this process judges likely stale -
        is disturbed.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Project already open")
        staleness = (
            "It looks like that process is no longer running, but this cannot "
            "be confirmed with certainty."
            if conflict.holder.likely_stale
            else "That process appears to still be running."
        )
        box.setText(
            f"{conflict.holder.describe()}\n\n{staleness}\n\n"
            "Opening for editing anyway could let two processes write to the "
            "same project database at once."
        )
        read_only_button = box.addButton("Open Read-Only", QMessageBox.ButtonRole.ActionRole)
        remove_button = box.addButton(
            "Remove Lock and Open", QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()

        if clicked is read_only_button:
            self.open_project_resolving_lock(directory, action="read_only")
        elif clicked is remove_button:
            self.open_project_resolving_lock(directory, action="force")

    def _show_about(self) -> None:
        """Show the About dialog: identity, authorship and licence."""
        AboutDialog(self).exec()

    def _prompt_project_health(self) -> None:
        """Open Project Health & Recovery for the current project."""
        if self._session is None:
            return
        ProjectHealthDialog(self._session.database, self._session.root, self).exec()

    def show_settings(self) -> None:
        """Open the Settings dialog and apply whatever the user accepted.

        Both sections are applied in one configuration update, so accepting the
        dialog writes the file once rather than twice.
        """
        dialog = SettingsDialog(self._config, self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self.apply_config(
                self._config.with_processing(dialog.processing_settings()).with_reviewer_name(
                    dialog.reviewer_name()
                )
            )

    def apply_processing_settings(self, processing: ProcessingSettings) -> None:
        """Adopt an edited Processing section, leaving every other setting alone."""
        self.apply_config(self._config.with_processing(processing))

    def apply_reviewer_name(self, name: str) -> None:
        """Adopt the reviewer identity conflict decisions are recorded against."""
        self.apply_config(self._config.with_reviewer_name(name))

    def _resolve_page(self) -> ResolvePage | None:
        """The Resolve page, when this window built a real one."""
        page = self._pages.get("resolve")
        return page if isinstance(page, ResolvePage) else None

    def _attendance_page(self) -> AttendancePage | None:
        """The Attendance page, when this window built a real one."""
        page = self._pages.get("attendance")
        return page if isinstance(page, AttendancePage) else None

    def _answer_key_page(self) -> AnswerKeyPage | None:
        """The Answer Key page, when this window built a real one."""
        page = self._pages.get("answer_key")
        return page if isinstance(page, AnswerKeyPage) else None

    def _results_page(self) -> ResultsPage | None:
        """The Results page, when this window built a real one."""
        page = self._pages.get("results")
        return page if isinstance(page, ResultsPage) else None

    def _reports_page(self) -> ReportsPage | None:
        """The Reports page, when this window built a real one."""
        page = self._pages.get("reports")
        return page if isinstance(page, ReportsPage) else None

    def broadcast_template(self, template: object | None) -> None:
        """Tell the scoring stages which template the batch was read with.

        Phase 8 needs the template for its question count and answer labels,
        and the Scan stage is where one is loaded. Routed through the window
        rather than page-to-page, for the same reason every other cross-page
        message is: a page that reached into another would have to know it
        exists.
        """
        answer_key = self._answer_key_page()
        if answer_key is not None:
            answer_key.set_template(template)  # type: ignore[arg-type]
        results = self._results_page()
        if results is not None:
            results.set_template(template)  # type: ignore[arg-type]
        reports = self._reports_page()
        if reports is not None:
            reports.set_template(template)  # type: ignore[arg-type]

    def _on_answer_key_changed(self, _key_id: int) -> None:
        """Tell the Results and Reports stages that this project's keys have moved on.

        Verifying a key changes which candidates can be marked and makes every
        result computed under the previous revision stale, and changes which
        sets are "ready" for the Reports stage's readiness check. Neither
        stage can see that happen - each is a different page - so both are
        told, and re-read. Which key changed is deliberately not used: each
        stage re-reads everything rather than patching one row, which is the
        rule the scoring engine itself follows.
        """
        results = self._results_page()
        if results is not None:
            results.refresh_table()
        reports = self._reports_page()
        if reports is not None:
            reports.refresh_table()

    def _on_results_changed(self) -> None:
        """Tell the Reports stage that scoring has been (re)calculated.

        A recomputed mark, a newly scored candidate, or a batch that just
        went stale all change what the Reports stage's readiness check and
        set overview should show.
        """
        reports = self._reports_page()
        if reports is not None:
            reports.refresh_table()

    def _on_batch_finished(self, report: object) -> None:
        """Point the scoring stages at a batch that has just been processed.

        All three stages pick up a batch when a project is *opened*. A batch
        scanned during the session would otherwise be invisible to them until
        the project was closed and reopened, and "Calculate Results" - or
        "Generate XLSX" - would go on saying it had nothing to work with, with
        a freshly read cohort sitting on disk.

        The batch id comes from the Scan stage rather than from ``report``,
        which summarises what was read and does not name the batch it was
        written to.
        """
        if getattr(report, "cancelled", False):
            return
        scan_page = self._scan_page()
        batch_id = scan_page.state.batch_id if scan_page is not None else None
        if not batch_id:
            return
        results = self._results_page()
        if results is not None:
            results.set_batch(batch_id)
        reports = self._reports_page()
        if reports is not None:
            reports.set_batch(batch_id)
        answer_key = self._answer_key_page()
        session = self._session
        if answer_key is None or session is None:
            return
        # The sets the batch actually contains, so an operator writing keys is
        # offered the papers that were sat rather than having to remember them.
        try:
            found = review_store.effective_set_codes(session.database, batch_id)
        except OMRScannerError:  # pragma: no cover - defensive
            return
        answer_key.offer_set_codes(
            [item.value for item in found.values() if item.value and not item.unresolved]
        )

    def reconcile_batch(self, batch_id: str) -> bool:
        """Open a batch's candidate reconciliation in the Attendance stage.

        Args:
            batch_id: The batch to reconcile.

        Returns:
            Whether the stage could be opened with that batch.
        """
        page = self._attendance_page()
        if page is None:
            return False
        page.set_batch(batch_id)
        return self.show_page("attendance")

    def review_batch(self, batch_id: str) -> bool:
        """Open a batch's conflicts in the Resolve stage.

        Args:
            batch_id: The batch to review.

        Returns:
            Whether the stage could be opened with that batch.

        The main window owns cross-page navigation, so the Scan page asks
        rather than reaching into another stage - and the template travels with
        the request, because the review workspace needs it to re-read a sheet
        and to know which labels a reviewer may choose from.
        """
        page = self._resolve_page()
        scan_page = self._scan_page()
        if page is None:
            return False
        template = scan_page.state.template if scan_page is not None else None
        if not page.load_batch(batch_id, template):
            return False
        return self.show_page("resolve")

    def apply_config(self, config: AppConfig) -> None:
        """Adopt an edited configuration: persist it and tell the pages.

        Separate from :meth:`show_settings` for the reason every command on this
        window is: the dialog is untestable offscreen, the behaviour is not.
        """
        self._config = config
        self._persist_config()
        self._broadcast_config_change()

    # ------------------------------------------------------------------
    # Developer / testing tools
    # ------------------------------------------------------------------
    def show_page(self, key: str) -> bool:
        """Bring one workflow stage to the front by its key.

        Returns whether that stage exists. Driven through the navigation list
        rather than the stack so the sidebar's highlight stays in step with
        what is on screen.
        """
        for row in range(self.navigation.count()):
            if self.navigation.item(row).data(Qt.ItemDataRole.UserRole) == key:
                self.navigation.setCurrentRow(row)
                return True
        return False

    def edit_template(self, path: Path) -> bool:
        """Open ``path`` in the Template Designer stage.

        The "Edit Template" shortcut the calibration page offers when a
        region is physically misplaced - calibration tunes recognition
        settings, it does not edit geometry (``docs/calibration_workflow.md``).

        Returns:
            ``True`` when the Template stage now shows ``path``.
        """
        template_page = self._pages.get("template")
        if not isinstance(template_page, TemplateDesignerPage):
            return False
        opened = template_page.open_template_at(path)
        if opened:
            self.show_page("template")
        return opened

    def start_calibration(self, template_path: Path | None = None) -> bool:
        """Show the Calibration stage, optionally loading a template first.

        Args:
            template_path: Template to load. When omitted, the stage keeps
                whatever it already had loaded.

        Returns:
            ``True`` unless a ``template_path`` was given and failed to load.
        """
        calibration_page = self._pages.get("calibration")
        if not isinstance(calibration_page, CalibrationPage):
            return False
        self.show_page("calibration")
        if template_path is not None:
            return calibration_page.load_template_from(template_path)
        return True

    def generate_dataset(self) -> None:
        """Ask what synthetic dataset to make, then make it.

        Split the same way every other command on this window is: the dialogs
        live here, and everything they decide is carried out by
        :meth:`generate_dataset_from`, which a test drives directly.
        """
        from omr_scanner.gui.devtools import GenerateDatasetDialog

        scan_page = self._pages.get("scan")
        template_path = (
            scan_page.state.template_path if isinstance(scan_page, ScanPage) else None
        )
        dialog = GenerateDatasetDialog(
            self,
            template_path=template_path,
            output_dir=self._config.default_projects_root,
        )
        if dialog.exec() != GenerateDatasetDialog.DialogCode.Accepted:
            return
        request = dialog.request()
        if request is not None:
            self.generate_dataset_from(request)

    def generate_dataset_from(self, request: GenerationRequest) -> bool:
        """Generate a dataset, showing progress and then a summary.

        Args:
            request: What to generate.

        Returns:
            ``True`` when a dataset was written - including a cancelled run,
            which writes fewer sheets and a manifest that says so.
        """
        from omr_scanner.gui.devtools import (
            DatasetWorker,
            GenerationProgressDialog,
            GenerationSummaryDialog,
        )

        produced: list[DatasetManifest] = []
        worker = DatasetWorker(request, self)
        worker.finished_dataset.connect(produced.append)
        progress = GenerationProgressDialog(worker, request.count, self)
        worker.start()
        progress.exec()
        # The thread owns file handles and a template; letting the window carry
        # on while it is still writing would leave a half-written dataset behind
        # a dialog that has already closed.
        worker.wait(GENERATION_SHUTDOWN_TIMEOUT_MS)

        if not produced:
            return False

        manifest = produced[0]
        summary = GenerationSummaryDialog(manifest, request.output_dir, self)
        wants_benchmark = [False]
        summary.benchmark_requested.connect(lambda: wants_benchmark.__setitem__(0, True))
        summary.exec()

        self.statusBar().showMessage(
            f"Generated {len(manifest.entries)} synthetic sheet(s)",
            STATUS_MESSAGE_MS,
        )
        if wants_benchmark[0] or request.run_benchmark:
            self.start_benchmark(request.output_dir, template_path=request.template_path)
        return True

    def _prompt_run_benchmark(self) -> None:
        """Ask which labelled dataset to benchmark, then start."""
        start = self._config.default_projects_root or Path.home()
        directory = QFileDialog.getExistingDirectory(
            self, "Select a labelled dataset folder", str(start)
        )
        if directory:
            self.start_benchmark(Path(directory))

    def start_benchmark(self, dataset_dir: Path, template_path: Path | None = None) -> bool:
        """Put the Scan stage into benchmark mode over ``dataset_dir``.

        Args:
            dataset_dir: The labelled dataset to score against.
            template_path: Template to load first. When omitted, whatever the
                Scan page already has is used - and a benchmark against the
                wrong template is a benchmark of nothing, so the dataset's own
                manifest names one when the caller does not.

        Returns:
            ``True`` when the Scan stage is ready to process the dataset.

        Deliberately *not* a second processing window. The user ends up on the
        page they already know, with the same buttons, and presses Process All.
        """
        scan_page = self._pages.get("scan")
        if not isinstance(scan_page, ScanPage):
            return False

        self.show_page("scan")
        if template_path is not None and not scan_page.load_template_from(template_path):
            return False
        if scan_page.state.template is None:
            report_error(
                self,
                ConfigurationError(
                    "Load the template this dataset was generated from before "
                    "benchmarking it."
                ),
                context="Benchmark",
            )
            return False

        started = scan_page.enter_benchmark_mode(dataset_dir)
        if started:
            self.statusBar().showMessage(
                "Benchmark mode: press Process All to score this dataset",
                STATUS_MESSAGE_MS,
            )
        return started

    def _broadcast_config_change(self) -> None:
        """Push settings that pages act on down to the pages that act on them."""
        scan_page = self._scan_page()
        if scan_page is not None:
            scan_page.set_processing_settings(self._config.processing)
        resolve_page = self._resolve_page()
        if resolve_page is not None:
            resolve_page.set_reviewer(self._config.reviewer_name)
        attendance_page = self._attendance_page()
        if attendance_page is not None:
            # The same name: the person reconciling a script against a roster
            # is the same kind of named authority as the one correcting a
            # recognised value, and asking for two would invite one to be left
            # blank.
            attendance_page.set_operator(self._config.reviewer_name)
        answer_key_page = self._answer_key_page()
        if answer_key_page is not None:
            answer_key_page.set_reviewer(self._config.reviewer_name)
        results_page = self._results_page()
        if results_page is not None:
            results_page.set_reviewer(self._config.reviewer_name)
        reports_page = self._reports_page()
        if reports_page is not None:
            reports_page.set_reviewer(self._config.reviewer_name)

    # ------------------------------------------------------------------
    # Internal state propagation
    # ------------------------------------------------------------------
    def _adopt_session(self, session: ProjectSession) -> None:
        """Replace the current session and refresh the whole window."""
        if self._session is not None:
            self._session.close()
        self._session = session
        if not session.read_only:
            self._recover_interrupted_batches(session)
        self._remember_recent_project(session.root)
        self._broadcast_project_change()
        self.statusBar().showMessage(f"Project '{session.name}' is open", STATUS_MESSAGE_MS)

    def _recover_interrupted_batches(self, session: ProjectSession) -> None:
        """Repair batch state left behind by a run that never finished.

        Done here, once, immediately after the database is opened and before
        any page sees the session. A scan row can only be QUEUED or PROCESSING
        while some process owns it; this application is only just starting, so
        none does, and those rows are stale by definition. Leaving them would
        make a resumed batch skip exactly the sheets that were in flight when
        the crash happened - the ones most likely to be missing.

        Never fatal: a project that cannot be repaired still opens, because
        the operator can do plenty with it that has nothing to do with batches.
        """
        try:
            batches, scans = recover_interrupted(session.database)
        except OMRScannerError:
            logger.exception("Could not recover interrupted batch state")
            return
        if scans:
            self.statusBar().showMessage(
                f"Recovered {scans} scan(s) from {batches} interrupted batch(es)",
                STATUS_MESSAGE_MS,
            )

    def _broadcast_project_change(self) -> None:
        """Push the current session to every page and update chrome."""
        for page in self._pages.values():
            page.on_project_changed(self._session)

        has_project = self._session is not None
        self.close_project_action.setEnabled(has_project)
        self.project_health_action.setEnabled(has_project)

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
    def _scan_page(self) -> ScanPage | None:
        """The Scan page, when this window built a real one.

        It is a :class:`~omr_scanner.gui.pages.PlaceholderPage` in a build
        where the stage is not implemented, so the type is checked rather than
        assumed.
        """
        page = self._pages.get("scan")
        return page if isinstance(page, ScanPage) else None

    def batch_is_running(self) -> bool:
        """Whether the Scan page is in the middle of processing a batch."""
        page = self._scan_page()
        return page is not None and page.is_processing

    def closeEvent(self, event: QCloseEvent) -> None:
        """Stop any running batch, then close the project, then disappear.

        The order matters. A batch writes recognition results into the project
        database as it goes, so closing that database underneath a running
        worker would abort the very writes that make the run resumable - and
        on Windows it would also leave the SQLite handle open until the worker
        noticed. The batch is therefore stopped and waited for *first*, and
        only then is the project released.

        Releasing the handle here rather than in ``__del__`` is what guarantees
        the project folder is not locked once the window is gone.
        """
        if self.batch_is_running():
            answer = QMessageBox.question(
                self,
                "Batch in progress",
                "A batch is currently being processed.\n\n"
                "Stop processing and exit? Scans already read are saved and the "
                "batch can be resumed next time this project is opened.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            logger.info("Window closing: stopping the running batch first")
            page = self._scan_page()
            if page is not None:
                # Cancel *and wait*: the pool has to be torn down and the last
                # results flushed before the database goes away, or the run is
                # neither finished nor properly resumable.
                page.shutdown_batch()

        # The review page may be part-way through decoding a sheet. Same rule,
        # same reason: no thread may outlive the window, and none may still be
        # reading when the project's database handle is released.
        review_page = self._resolve_page()
        if review_page is not None:
            review_page.shutdown()
        attendance_page = self._attendance_page()
        if attendance_page is not None:
            # Joined before the project closes for the same reason the batch
            # is: a reconciliation worker still writing when the database goes
            # away would abort mid-transaction.
            attendance_page.shutdown()
        results_page = self._results_page()
        if results_page is not None:
            results_page.shutdown()
        reports_page = self._reports_page()
        if reports_page is not None:
            reports_page.shutdown()

        self.close_project()
        logger.info("Main window closed")
        super().closeEvent(event)
