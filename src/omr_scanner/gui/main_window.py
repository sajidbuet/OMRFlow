"""The application main window.

Purpose:
    Assemble the application shell, own the project lifecycle from the user
    interface side, and route menu commands to services.

Responsibilities:
    * Assemble the shell: one chrome row, the stacked pages, the status
      footer and the status bar. Three bands, and the first of them is also
      the title bar.
    * Own the File/Tools/Help action hierarchy, and hand it to the chrome
      row's menu button.
    * Own the *window*: it is frameless, so minimising, maximising, restoring,
      closing, moving and resizing are this class's responsibility rather than
      the platform's - see "Why the window is frameless" below.
    * Hold the single open :class:`~omr_scanner.services.ProjectSession` and
      broadcast changes to every page and to the footer.
    * Keep the recent-project list in the application configuration up to
      date, and keep the Project dashboard's copy of it in step.

What does NOT belong here:
    * Any project/database/imaging logic. Every action delegates to
      :mod:`omr_scanner.services`.
    * Long-running work. Batch processing runs in a worker thread and reports
      progress through signals; the main window must never block.
    * Styling, painting and responsive arithmetic. Each shell part is a widget
      in :mod:`omr_scanner.gui.widgets` that decides its own layout from its
      own width, so this module contains no geometry and no colours.

Testability:
    Commands are split in two: ``_prompt_*`` methods own the modal dialogs,
    and :meth:`create_project_at` / :meth:`open_project_at` /
    :meth:`close_project` contain the behaviour. GUI tests drive the second
    group, so no test has to interact with a native file dialog.

Why the menu bar is hidden rather than removed:
    The reference design replaces the permanent File / Tools / Help row with
    one menu button, but the *actions* must keep working exactly as they did,
    shortcuts included. The three menus are therefore still built on
    ``menuBar()`` - which keeps the hierarchy, the nesting and Qt's own
    shortcut context intact - the bar itself is hidden, and the chrome row's
    menu button pops up those same `QMenu` objects as submenus. There is one
    File menu in the application, and nothing is duplicated or reimplemented.

Why the window is frameless:
    The shell now draws its own title bar, because merging it with the
    workflow ribbon is what removed a whole 48-pixel band from the top of
    every stage. That is only worth doing if the window still behaves like a
    Windows window afterwards, so nothing here re-implements what the platform
    already does:

    * Moving is ``QWindow.startSystemMove()``, from the chrome row. The window
      manager performs the drag, which is what keeps Aero Snap, snap-to-edge,
      drag-to-top-to-maximise and cross-monitor DPI handling working.
    * Resizing is ``QWindow.startSystemResize()`` from
      :data:`WINDOW_RESIZE_BORDER` pixels of frame around the central widget -
      again the window manager's own resize, with its own snapping and its own
      cursors, rather than a hand-rolled ``setGeometry`` loop.
    * Minimise, maximise, restore and close call ``showMinimized()``,
      ``showMaximized()``, ``showNormal()`` and ``close()``. Maximised
      geometry, multi-monitor placement, the taskbar entry, Alt+F4, Win+Up,
      Win+Down and system activation are all untouched, because none of them
      was ever the frame's doing.

    One thing is genuinely lost and is not worked around: Windows draws no
    drop shadow around a frameless window. Restoring it means either a DWM
    call or a translucent parent widget, both of which are the brittle
    platform hack the brief rules out; a hairline border stands in for it
    instead. See ``docs/wiki/Developer-Architecture.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from PySide6.QtCore import QEvent, QPoint, Qt, QUrl, qVersion
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QCursor,
    QDesktopServices,
    QKeySequence,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QMenu,
    QMessageBox,
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
from omr_scanner.gui.branding import application_icon
from omr_scanner.gui.calibration.page import CalibrationPage
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.health_dialog import ProjectHealthDialog
from omr_scanner.gui.pages import WORKFLOW_PAGES, PlaceholderPage, ProjectPage
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.project_config_dialog import ProjectConfigDialog
from omr_scanner.gui.reports.page import ReportsPage
from omr_scanner.gui.results.page import ResultsPage
from omr_scanner.gui.review.page import ResolvePage
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.gui.settings_dialog import SettingsDialog
from omr_scanner.gui.template_designer.page import TemplateDesignerPage
from omr_scanner.gui.theme import Chrome
from omr_scanner.gui.widgets import (
    AppChrome,
    AppStatus,
    StatusFooter,
    WorkflowRibbon,
)
from omr_scanner.services import (
    ProjectSession,
    adopt_template_if_unambiguous,
    create_project,
    diagnostics,
    open_project,
    recover_interrupted,
    resolve_active_template,
    review_store,
    set_active_template,
)
from omr_scanner.services.project_lock import ProjectLockHeldError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.evaluation.ground_truth import DatasetManifest
    from omr_scanner.gui.devtools import GenerationRequest

logger = logging.getLogger(__name__)

WINDOW_MIN_WIDTH = 720
WINDOW_MIN_HEIGHT = 560
"""The window's floor, in logical pixels.

Narrower than it was, and deliberately: the fixed 190-pixel navigation
sidebar this shell replaced was most of the old 960-pixel minimum, and the
workflow ribbon has layouts for widths far below that. A floor wider than the
shell needs would make those layouts unreachable, which is the opposite of why
they exist. The height floor came down with the chrome: one 46-pixel row
replaced a 48-pixel header plus a navigator band, and the stages no longer
spend a further row on a heading naming themselves.
"""

WINDOW_RESIZE_BORDER = Chrome.RESIZE_BORDER
"""The frame, in logical pixels, that belongs to the window rather than to the
central widget.

A frameless window has no non-client area, so there is nothing for the
platform's resize cursors to appear over. Reserving a few pixels as the
central widget's margin leaves a strip that no child widget occupies, which
means a press there arrives at this window and can start a native resize. It
is zeroed while maximised: there is no edge to drag, and the margin would show
as a hairline gap against the screen edge.
"""

STATUS_MESSAGE_MS = 5000
"""How long transient status bar messages stay visible."""

DEVELOPER_URL = "https://www.sajid.bd"

def window_title(project_name: str = "") -> str:
    """The window's title, always carrying the running version.

    Args:
        project_name: The open project's name, or ``""`` when none is open.

    Returns:
        ``"OMRFlow <version>"``, or ``"<project> - OMRFlow <version>"`` with a
        project open. The version is spelled out rather than shown here, so
        this module holds no copy of it -
        ``tests/unit/test_version.py`` fails if one appears.

    The version is in the title on purpose: during a prerelease the first
    question asked of any bug report is which build produced it, and a
    screenshot of the window answers that without the reporter having to
    find the About dialog. It costs one short suffix in a bar that is
    otherwise mostly empty.
    """
    application = f"{APPLICATION_NAME} {__version__}"
    return f"{project_name} - {application}" if project_name else application


STAGE_NOT_IMPLEMENTED = "Planned for phase {phase}; not available in this build."
"""Why a workflow stage cannot be opened, shown in its tooltip.

This is the *only* rule that disables a stage, and it is the rule the
navigation sidebar already used - it showed the same sentence as a tooltip.
Notably there is no "needs an open project" rule: every stage has always been
reachable without one, each page showing its own empty state, and inventing a
lock here would both change long-standing behaviour and put a disabled control
in front of an operator who is simply looking ahead at the workflow.
"""

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
        frameless: Whether to draw the title bar inside the chrome row. The
            application always does. ``False`` keeps the platform's own frame
            and is here for one reason: a test that needs to isolate a shell
            behaviour from the window-management behaviour should be able to,
            and a diagnostic run on a platform whose compositor cannot start a
            system move should still produce a movable window.
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        config_path: Path | None = None,
        parent: QWidget | None = None,
        *,
        frameless: bool = True,
    ) -> None:
        super().__init__(parent)

        self._config = config if config is not None else load_app_config()
        self._config_path = config_path
        self._session: ProjectSession | None = None
        self._pages: dict[str, WorkflowPage] = {}
        self._frameless = frameless
        self._resize_edges = Qt.Edge(0)

        self.setWindowTitle(window_title())
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        # Still set, and still meaningful without a native title bar to draw
        # it: this is what the taskbar button, the alt-tab switcher and every
        # dialog parented to this window read, alongside
        # `QApplication.setWindowIcon()`.
        self.setWindowIcon(application_icon())

        if self._frameless:
            self._make_frameless()

        self._build_central_widget()
        self._build_menus()
        self._build_status_bar()
        self._broadcast_project_change()
        self._broadcast_config_change()

    def _make_frameless(self) -> None:
        """Drop the platform frame and take over what it used to provide.

        ``FramelessWindowHint`` alone would leave a window that cannot be
        moved or resized, so it is never alone: the chrome row starts a system
        move, and :meth:`mousePressEvent` starts a system resize from the
        border this reserves. ``WA_Hover`` is what lets
        :meth:`mouseMoveEvent` see the pointer crossing that border while no
        button is held, which is how the resize cursors appear.
        """
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_central_widget(self) -> None:
        """Assemble the shell, top to bottom.

        Three bands and nothing else: the chrome row - which is also the title
        bar - the stacked pages, and the status footer. The native status bar
        sits below all of it, unchanged.

        There is no left column and no second chrome band. The width the
        navigation sidebar used to occupy, and the height the header band and
        the per-page heading used to, now belong to the pages.
        """
        central = QWidget(self)
        central.setObjectName("appCentralWidget")
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        self.chrome = AppChrome(
            WORKFLOW_PAGES, density=self._config.ribbon_density, parent=central
        )
        self.chrome.step_activated.connect(self._on_step_activated)
        self.chrome.previous_requested.connect(self.go_to_previous_stage)
        self.chrome.next_requested.connect(self.go_to_next_stage)
        self.chrome.density_changed.connect(self._on_ribbon_density_changed)
        self.chrome.minimise_requested.connect(self.showMinimized)
        self.chrome.maximise_toggled.connect(self.toggle_maximised)
        self.chrome.close_requested.connect(self.close)
        outer_layout.addWidget(self.chrome)

        outer_layout.addWidget(self._build_pages(), stretch=1)

        self.footer = StatusFooter(DEVELOPER_NAME, DEVELOPER_URL, parent=central)
        self.footer.developer_link_activated.connect(self._open_developer_site)
        outer_layout.addWidget(self.footer)

        self.setCentralWidget(central)
        self._apply_resize_margin()

    @property
    def ribbon(self) -> WorkflowRibbon:
        """The workflow ribbon inside the chrome row.

        A shortcut, because "which stage is showing" is a main-window question
        that the chrome row merely hosts the answer to. Everything that
        navigates goes through :meth:`show_page`, never through here.
        """
        return self.chrome.ribbon

    def _build_pages(self) -> QStackedWidget:
        """Create the stacked workflow pages and wire their signals."""
        self.stack = QStackedWidget()

        for spec in WORKFLOW_PAGES:
            page: WorkflowPage
            if spec.key == "project":
                project_page = ProjectPage(spec)
                project_page.create_requested.connect(self._prompt_create_project)
                project_page.open_requested.connect(self._prompt_open_project)
                project_page.project_info_requested.connect(
                    self._prompt_project_configuration
                )
                project_page.recent_project_requested.connect(self.open_project_at)
                project_page.view_all_recent_requested.connect(
                    self.show_recent_projects_menu
                )
                page = project_page
            elif spec.key == "template":
                designer_page = TemplateDesignerPage(spec)
                designer_page.active_template_changed.connect(self.set_active_project_template)
                page = designer_page
            elif spec.key == "calibration":
                calibration_page = CalibrationPage(spec)
                calibration_page.edit_template_requested.connect(self.edit_template)
                page = calibration_page
            elif spec.key == "scan":
                scan_page = ScanPage(spec)
                scan_page.review_requested.connect(self.review_batch)
                scan_page.batch_finished.connect(self._on_batch_finished)
                scan_page.processing_changed.connect(self._on_processing_changed)
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

            if not spec.is_implemented:
                self.ribbon.set_step_enabled(
                    spec.key,
                    enabled=False,
                    reason=STAGE_NOT_IMPLEMENTED.format(phase=spec.phase),
                )

        self.stack.setCurrentIndex(0)
        self.ribbon.set_current_key(WORKFLOW_PAGES[0].key)
        self.chrome.sync_navigation_buttons()
        return self.stack

    def _on_step_activated(self, key: str) -> None:
        """A workflow step was clicked, or chosen from the narrow flyout."""
        self.show_page(key)

    def go_to_previous_stage(self) -> bool:
        """Open the stage immediately before the current one, if permitted."""
        return self._go_to_adjacent_stage(-1)

    def go_to_next_stage(self) -> bool:
        """Open the stage immediately after the current one, if permitted."""
        return self._go_to_adjacent_stage(1)

    def _go_to_adjacent_stage(self, delta: int) -> bool:
        """Step one stage along the workflow.

        Routed through :meth:`show_page` like every other navigation, so the
        arrows cannot reach a stage a click could not: the same disabled check
        applies, the ribbon's highlight moves with the stack, and the active
        step is scrolled back into view by the same code.
        """
        key = self.ribbon.adjacent_key(delta)
        return self.show_page(key) if key is not None else False

    def _on_ribbon_density_changed(self, level: int) -> None:
        """Remember how compact the operator wants the workflow ribbon.

        A display preference, persisted through the ordinary application
        configuration - no project file is touched, so a project saved by an
        older build opens unchanged and one saved now opens in an older build
        unchanged.
        """
        self.apply_config(self._config.with_ribbon_density(level))

    def _on_processing_changed(self, running: bool) -> None:
        """Reflect the Scan stage's batch in the footer's status.

        The only two states the application genuinely has. Nothing else sets
        this, which is why there is no third word in
        :class:`~omr_scanner.gui.widgets.status_footer.AppStatus`.
        """
        self.footer.set_status(AppStatus.PROCESSING if running else AppStatus.READY)

    def _open_developer_site(self, url: str) -> None:
        """Open the developer's site in the system's default browser.

        Never navigates inside the application itself - `QDesktopServices`
        hands the URL straight to the OS, exactly as clicking it in any other
        desktop application would.
        """
        QDesktopServices.openUrl(QUrl(url))

    def _build_menus(self) -> None:
        """Create the File, Tools and Help menus, and hand them to the header.

        The menus are built on ``menuBar()`` exactly as before - same actions,
        same order, same nesting, same shortcuts - and the bar is then hidden.
        Nothing about the hierarchy changes; only where it is opened from.
        """
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

        self.project_config_action = QAction("Project &Configuration...", self)
        self.project_config_action.setObjectName("projectConfigAction")
        self.project_config_action.setEnabled(False)
        self.project_config_action.setStatusTip(
            "Set this project's examination name and the sets it is divided into"
        )
        self.project_config_action.triggered.connect(self._prompt_project_configuration)
        file_menu.addAction(self.project_config_action)

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

        self.diagnostic_bundle_action = QAction("Create &Diagnostic Bundle...", self)
        self.diagnostic_bundle_action.setObjectName("diagnosticBundleAction")
        self.diagnostic_bundle_action.setStatusTip(
            "Save a support bundle (version, settings, health check) - never candidate data"
        )
        self.diagnostic_bundle_action.triggered.connect(self._prompt_create_diagnostic_bundle)
        tools_menu.addAction(self.diagnostic_bundle_action)
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

        self.run_stress_qualification_action = QAction(
            "Run &100,000-Sheet Stress Test...", self
        )
        self.run_stress_qualification_action.setObjectName("runStressQualificationAction")
        self.run_stress_qualification_action.setStatusTip(
            "Launch the unattended Phase 10 qualification - runs for many hours "
            "and deliberately force-kills processing runs to test recovery"
        )
        self.run_stress_qualification_action.triggered.connect(
            self._prompt_run_stress_qualification
        )
        developer_menu.addAction(self.run_stress_qualification_action)

        help_menu = self.menuBar().addMenu("&Help")
        self.about_action = QAction(f"&About {APPLICATION_NAME}", self)
        self.about_action.triggered.connect(self._show_about)
        help_menu.addAction(self.about_action)

        self._install_application_menu(file_menu, tools_menu, help_menu)

    def _install_application_menu(self, *menus: QMenu) -> None:
        """Move the menu bar behind the header's menu button.

        Args:
            menus: The top-level menus, in the order they should appear.

        The same `QMenu` objects are added as submenus of one application
        menu, so there is still exactly one File menu and its actions,
        callbacks and enabled state are untouched. The bar itself is hidden
        rather than left in place, because the reference design replaces that
        row and showing both would be the duplicate the brief rules out.

        Each shortcut-bearing action is *also* added to the window. Qt
        deactivates the shortcuts of actions that live only in a hidden
        widget, and a shortcut that silently stopped working would be the
        worst kind of regression here - invisible until someone reached for
        Ctrl+O. Adding the same `QAction` to the window gives it window
        shortcut context without creating a second action or a second
        handler.
        """
        self.application_menu = QMenu(self)
        self.application_menu.setObjectName("applicationMenu")
        for menu in menus:
            self.application_menu.addMenu(menu)
        self.chrome.set_menu(self.application_menu)

        menu_bar = self.menuBar()
        menu_bar.setVisible(False)

        for action in self._shortcut_actions():
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            self.addAction(action)

    def _shortcut_actions(self) -> tuple[QAction, ...]:
        """Every action that carries a keyboard shortcut."""
        return tuple(
            action
            for action in (
                self.new_project_action,
                self.open_project_action,
                self.settings_action,
                self.exit_action,
            )
            if not action.shortcut().isEmpty()
        )

    def open_application_menu(self) -> None:
        """Pop up the application menu, as clicking the chrome button does.

        Exposed so a test can open it without synthesising a mouse press on
        the button, and so the menu can be reached from code paths that are
        not the button itself.

        Uses ``popup()`` rather than `QToolButton.showMenu`, which is
        documented not to return until the menu has been closed *by the
        user* - the same blocking-modal trap as ``QMenu.exec()``, and it hung
        the shell's own validation run before this was changed. The button's
        own click still goes through Qt's `InstantPopup` handling; only this
        programmatic entry point needed to be non-blocking.
        """
        button = self.chrome.menu_button
        self.application_menu.popup(button.mapToGlobal(button.rect().bottomLeft()))

    def show_recent_projects_menu(self, at: QPoint | None = None) -> None:
        """Show the whole recent-projects list, at the cursor.

        Args:
            at: Where to place the menu. Defaults to the cursor position.

        The *same* "Open Recent" submenu that lives under File, popped up
        where the operator is looking. The Project dashboard lists the first
        few entries and defers the rest to this, rather than owning a second
        copy of the list or a dialog that would have to be kept in step with
        it.

        ``popup()`` and deliberately not ``exec()``. ``exec()`` spins a nested
        modal event loop that only returns when the menu is dismissed, so
        nothing offscreen ever dismisses it - the first version of this method
        used ``exec()`` and hung the test suite outright. That is the
        modal-in-a-testable-method defect this project has now met four times
        (Phases 8, 9, the project-lock dialog, and here); ``popup()`` shows the
        menu and returns, which is both correct for a menu and testable.
        """
        self.recent_menu.popup(at if at is not None else QCursor.pos())

    def _build_status_bar(self) -> None:
        """Create the status bar, for transient messages only.

        It no longer carries a permanent project indicator. That indicator
        read ``"<name>  (<full path>)"``, which the footer now states better
        in two respects: it shows the examination's *title* rather than its
        folder name, and it does not put an absolute filesystem path
        permanently on screen - something the brief for the footer explicitly
        asks against, and which the status bar was doing two rows below it.

        The bar itself stays. It is where every ``showMessage`` in this window
        goes - "Project opened read-only", "Recovered 3 scan(s)", "Diagnostic
        bundle saved to ..." - and those are transient notifications with
        nowhere else to appear.
        """
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

    def create_project_at(
        self, parent_directory: Path, name: str, *, exam_name: str | None = None
    ) -> bool:
        """Create a project and open it, replacing any currently open project.

        Args:
            parent_directory: Folder that will contain the new project folder.
            name: Name of the project workspace; also its folder name.
            exam_name: Title of the examination. Defaults to ``name``, which
                an operator can replace with a fuller title - one that need
                not be a legal folder name - in *Project Configuration*.

        Returns:
            ``True`` when the project was created and opened, ``False`` when the
            attempt failed. Failures are reported to the user and logged; they
            never propagate into Qt's event loop.
        """
        try:
            session = create_project(parent_directory, name, exam_name=exam_name)
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
        """Ask for a location and a name, create the project, then configure it.

        The folder name is asked for first and on its own because it is the
        one answer that cannot be changed afterwards without moving files on
        disk. Everything that *can* be changed later - the examination's real
        title, the sets it is divided into - is collected in *Project
        Configuration*, which opens straight afterwards so that a new project
        is configured in one continuous flow rather than left half-described
        until somebody finds the menu item.
        """
        start_dir = self._config.default_projects_root or Path.home()
        parent_directory = QFileDialog.getExistingDirectory(
            self, "Select the folder that will contain the project", str(start_dir)
        )
        if not parent_directory:
            return

        name, accepted = QInputDialog.getText(
            self, "New project", "Project name (used as the folder name):"
        )
        if not accepted or not name.strip():
            return

        if self.create_project_at(Path(parent_directory), name.strip()):
            self._prompt_project_configuration()

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

    def _prompt_project_configuration(self) -> None:
        """Open Project Configuration for the current project.

        The dialog writes every change as it is made, so there is nothing to
        apply here; the window only has to redisplay the project afterwards,
        because the examination name it shows may have changed.
        """
        if self._session is None:
            return
        ProjectConfigDialog(self._session, self).exec()
        self._broadcast_project_change()

    def create_diagnostic_bundle_at(self, output_path: Path) -> bool:
        """Write a diagnostic bundle to ``output_path``. No dialog - testable directly.

        Works with or without an open project: without one, the bundle
        still carries version/environment information and a "no project
        open" health status.
        """
        try:
            diagnostics.build_diagnostic_bundle(
                output_path,
                database=self._session.database if self._session is not None else None,
                project_root=self._session.root if self._session is not None else None,
                processing=self._config.processing,
                extra_environment={"qt_version": qVersion()},
            )
        except OSError as exc:
            report_error(
                self,
                OMRScannerError(
                    f"Could not write the diagnostic bundle: {exc}",
                    user_message=f"Could not write the diagnostic bundle: {exc}",
                ),
                context="Create diagnostic bundle",
            )
            return False
        return True

    def _prompt_create_diagnostic_bundle(self) -> None:
        """Ask where to save the diagnostic bundle, then write it."""
        default_name = f"omrflow_diagnostics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        start = str((self._config.default_projects_root or Path.home()) / default_name)
        destination, _filter = QFileDialog.getSaveFileName(
            self, "Save Diagnostic Bundle", start, "Zip files (*.zip)"
        )
        if not destination:
            return
        if self.create_diagnostic_bundle_at(Path(destination)):
            self.statusBar().showMessage(
                f"Diagnostic bundle saved to {destination}", STATUS_MESSAGE_MS
            )

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

        Args:
            key: The stage to show.

        Returns:
            ``True`` when that stage exists and is available. A stage that is
            disabled - no project open, or not implemented in this build -
            is not shown, because the ribbon has already told the operator
            why and silently switching to it anyway would contradict that.

        The single entry point for cross-page navigation, and the reason the
        brief's "the active stage must always be visible" requirement needs no
        special case anywhere else. It moves the stack, the ribbon's highlight
        (which scrolls itself into view, or becomes the narrow layout's sole
        step) and the previous/next buttons' enabled state together, however
        the navigation was started: a click, an arrow button, the narrow
        flyout, a keyboard shortcut, a menu command or another page asking.
        """
        page = self._pages.get(key)
        if page is None:
            return False
        step = self.ribbon.step(key)
        if step is not None and not step.isEnabled():
            return False
        self.stack.setCurrentWidget(page)
        self.ribbon.set_current_key(key)
        self.chrome.sync_navigation_buttons()
        return True

    def current_page_key(self) -> str | None:
        """The key of the stage currently on screen."""
        current = self.stack.currentWidget()
        return next(
            (key for key, page in self._pages.items() if page is current), None
        )

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

    def _prompt_run_stress_qualification(self) -> None:
        """Open the 100,000-sheet qualification launcher, or reconnect to a run.

        Owns every dialog, as the ``_prompt_*`` convention on this window
        requires - :meth:`open_stress_qualification_monitor` is the part a
        headless test drives, and it shows nothing modal.

        Reopening this while a campaign is already running goes straight to
        that campaign's monitor rather than offering to start a second one in
        the same directory, which would have two orchestrators overwriting
        each other's evidence.
        """
        from omr_scanner.gui.stress_qualification_dialog import (
            CampaignSituation,
            StressQualificationDialog,
            campaign_situation,
            default_output_dir,
        )

        default_dir = default_output_dir()
        situation = campaign_situation(default_dir)
        if situation is CampaignSituation.RUNNING:
            self.open_stress_qualification_monitor(default_dir)
            return
        if situation is CampaignSituation.RESUMABLE:
            # `run` refuses to start over an existing campaign without
            # --restart, so offering the launch form here would produce a
            # child process that exits 2 into a log file nobody reads. The
            # monitor is where Resume Campaign lives.
            answer = QMessageBox.question(
                self,
                "Continue the campaign already here?",
                f"{default_dir} holds a campaign that stopped before "
                "finishing.\n\nOpen its monitor, where you can resume it? "
                "Runs already verified are not repeated.\n\n"
                "Choose No to set up a campaign somewhere else instead.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer is QMessageBox.StandardButton.Yes:
                self.open_stress_qualification_monitor(default_dir)
                return

        dialog = StressQualificationDialog(self, output_dir=default_dir)
        if dialog.exec() != StressQualificationDialog.DialogCode.Accepted:
            return
        request = dialog.launched_request
        if request is None:
            return
        self.statusBar().showMessage(
            "Qualification campaign started as its own process - closing "
            "OMRFlow will not stop it",
            STATUS_MESSAGE_MS,
        )
        self.open_stress_qualification_monitor(request.output_dir)

    def open_stress_qualification_monitor(self, output_dir: Path) -> object:
        """Show the read-only monitor for the campaign in ``output_dir``.

        Args:
            output_dir: The campaign directory to watch.

        Returns:
            The monitor window, so a test can assert on what it rendered.

        Kept separate from :meth:`_prompt_run_stress_qualification` and free of
        modals so that a headless test can call it directly. The monitor is
        non-modal and owns nothing: closing it never stops the campaign.
        """
        from omr_scanner.gui.stress_qualification_monitor import (
            StressQualificationMonitor,
        )

        monitor = StressQualificationMonitor(output_dir, self)
        monitor.show()
        return monitor

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
        # Before any page sees the session: a project that names no template
        # but owns exactly one adopts it here, once, so Template, Calibrate
        # and Scan all receive it already resolved. Doing it per page would
        # mean three disk scans and three chances to disagree.
        self._adopt_project_template(session)
        self._remember_recent_project(session.root)
        self._broadcast_project_change()
        self.statusBar().showMessage(f"Project '{session.name}' is open", STATUS_MESSAGE_MS)

    def _adopt_project_template(self, session: ProjectSession) -> None:
        """Settle which template this project uses, once per open.

        Only when the project names none: a recorded choice is never
        second-guessed, and a project with two templates is left alone rather
        than guessed at. Failure here is logged and otherwise ignored - a
        project whose template cannot be settled must still open, because the
        Template screen is where it gets fixed.
        """
        try:
            adopted = adopt_template_if_unambiguous(session)
        except OMRScannerError:
            logger.warning(
                "Could not record the active template for %s", session.name, exc_info=True
            )
            return
        if adopted is not None:
            logger.info("Adopted %s as the active template for %s", adopted.name, session.name)

    def set_active_project_template(self, template_path: Path) -> None:
        """Make ``template_path`` this project's template and tell every page.

        The one way the active template changes. Called when the Template
        screen saves, imports or creates one, and when a missing template is
        replaced, so the three screens cannot drift apart: they are not told
        individually, they are all re-broadcast from the same session.
        """
        if self._session is None:
            return
        # A page that opens the template it was just handed announces it right
        # back. Recognising that as a no-op both saves a pointless rewrite of
        # project.json and stops broadcast -> open -> announce -> broadcast
        # from running forever.
        if resolve_active_template(self._session.project) == template_path:
            return
        try:
            set_active_template(self._session, template_path)
        except OMRScannerError as exc:
            logger.warning("Could not record the active template: %s", exc)
            QMessageBox.warning(self, "Project template", exc.user_message)
            return
        self._broadcast_project_change()

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
        """Push the current session to every page, the footer and the chrome.

        The single place that reacts to a project being created, opened,
        reconfigured or closed, which is what makes the footer's project name
        correct by construction rather than by remembering to update it in
        four places. Reconfiguration matters as much as opening does: renaming
        the examination in *Project Configuration* calls back here, so the
        footer never shows the title the project used to have.
        """
        for page in self._pages.values():
            page.on_project_changed(self._session)

        has_project = self._session is not None
        self.close_project_action.setEnabled(has_project)
        self.project_health_action.setEnabled(has_project)
        self.project_config_action.setEnabled(has_project)

        if self._session is None:
            self.setWindowTitle(window_title())
            self.footer.set_project_title(None)
        else:
            self.setWindowTitle(window_title(self._session.name))
            # The examination's own title, not the folder name and never the
            # path: `exam_name` is what an operator typed in Project
            # Configuration, falls back to the project name when they have not
            # yet, and is the thing they are checking they have open.
            self.footer.set_project_title(self._session.exam_name)

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
        """Rebuild the "Open Recent" submenu, and the dashboard's copy of it.

        Both read the same tuple from the application configuration, and both
        are rebuilt here, so the menu and the Project page can never disagree
        about what was opened recently.
        """
        self.recent_menu.clear()
        if not self._config.recent_projects:
            empty = QAction("(none)", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
        else:
            for directory in self._config.recent_projects:
                action = QAction(str(directory), self)
                action.triggered.connect(
                    lambda _checked=False, path=directory: self.open_project_at(path)
                )
                self.recent_menu.addAction(action)

        project_page = self._pages.get("project")
        if isinstance(project_page, ProjectPage):
            project_page.set_recent_projects(self._config.recent_projects)

    def _persist_config(self) -> None:
        """Save the configuration, treating failure as non-fatal."""
        try:
            save_app_config(self._config, self._config_path)
        except ConfigurationError as exc:
            logger.warning("Could not save application configuration: %s", exc)

    # ------------------------------------------------------------------
    # Window state and the frame the platform no longer draws
    # ------------------------------------------------------------------
    def toggle_maximised(self) -> None:
        """Maximise the window, or restore it if it already is.

        ``showNormal()``/``showMaximized()`` and nothing else. Qt and the
        platform between them own what "maximised" means - which monitor's
        work area to fill, where the taskbar is, what the restored geometry
        was - and reimplementing any of that from a saved rectangle is how a
        window ends up restoring onto a monitor that is no longer attached.
        """
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _apply_resize_margin(self) -> None:
        """Reserve (or release) the border the window resizes from.

        Zero while maximised: there is no edge to drag there, and a margin
        would show as a hairline strip of window background between the
        content and the screen edge.
        """
        margin = 0 if (not self._frameless or self.isMaximized()) else WINDOW_RESIZE_BORDER
        self.setContentsMargins(margin, margin, margin, margin)

    def _edges_at(self, position: QPoint) -> Qt.Edge:
        """Which window edges ``position`` is close enough to grab.

        Args:
            position: A point in this window's own coordinates.

        Returns:
            The edges, OR-ed together - so a corner returns two of them and
            resizes in both axes, as every other Windows window does. Empty
            away from the border, and empty while maximised.
        """
        if not self._frameless or self.isMaximized():
            return Qt.Edge(0)
        border = WINDOW_RESIZE_BORDER
        edges = Qt.Edge(0)
        if position.x() <= border:
            edges |= Qt.Edge.LeftEdge
        elif position.x() >= self.width() - border - 1:
            edges |= Qt.Edge.RightEdge
        if position.y() <= border:
            edges |= Qt.Edge.TopEdge
        elif position.y() >= self.height() - border - 1:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges: Qt.Edge) -> Qt.CursorShape:
        """The resize cursor that belongs to ``edges``."""
        horizontal = bool(edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge))
        vertical = bool(edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge))
        if horizontal and vertical:
            falling = bool(edges & Qt.Edge.LeftEdge) == bool(edges & Qt.Edge.TopEdge)
            return (
                Qt.CursorShape.SizeFDiagCursor if falling else Qt.CursorShape.SizeBDiagCursor
            )
        if horizontal:
            return Qt.CursorShape.SizeHorCursor
        if vertical:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Show the resize cursor while the pointer is over the border."""
        edges = self._edges_at(event.position().toPoint())
        if edges != self._resize_edges:
            self._resize_edges = edges
            if edges:
                self.setCursor(self._cursor_for(edges))
            else:
                self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Start a native resize when the press lands on the border.

        ``startSystemResize`` and not a hand-rolled geometry loop, for the
        same reasons the chrome row uses ``startSystemResize``'s sibling for
        dragging: the window manager's resize snaps to screen edges, respects
        the minimum size, handles a display with a different DPI correctly,
        and cannot be left stuck on because a release event went missing.
        """
        edges = self._edges_at(event.position().toPoint())
        handle = self.windowHandle()
        if (
            event.button() is Qt.MouseButton.LeftButton
            and edges
            and handle is not None
            and handle.startSystemResize(edges)
        ):
            event.accept()
            return
        super().mousePressEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        """Keep the maximise button and the resize border in step with the state.

        ``WindowStateChange`` arrives for every route into and out of
        maximised - the button in the chrome row, a double-click on it,
        Win+Up, Win+Down, Aero Snap, and the taskbar's own context menu - so
        handling it here covers all of them rather than only the one this
        application initiates.
        """
        super().changeEvent(event)
        if event.type() is QEvent.Type.WindowStateChange:
            self.chrome.set_maximised(self.isMaximized())
            self._apply_resize_margin()

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
