"""Qt application bootstrap.

Purpose:
    Create the :class:`QApplication`, show the main window and run the event
    loop.

What does NOT belong here:
    * Logging configuration and command line parsing; those happen in
      :mod:`omr_scanner.main` before Qt is touched, so that a failure during
      start-up is logged even if Qt never initialises.
    * Any widget construction beyond the main window.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtWidgets import QApplication

from omr_scanner import APPLICATION_NAME, ORGANIZATION_NAME, __version__
from omr_scanner.config import AppConfig
from omr_scanner.gui.branding import application_icon
from omr_scanner.gui.error_reporting import install_global_exception_handler
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.theme import application_stylesheet

logger = logging.getLogger(__name__)


def configure_application(app: QApplication) -> None:
    """Apply OMRFlow's identity, icon and stylesheet to ``app``.

    Args:
        app: The application object to configure.

    Separated from :func:`run_gui` so it can be tested: `run_gui` ends in
    ``app.exec()``, which does not return until the user closes the window,
    so nothing about the configuration it performs could otherwise be
    asserted.

    Note what is *not* set here: ``applicationDisplayName``. Qt appends that
    to every window title automatically, which produced
    "OMRFlow <version> - OMRFlow" on the packaged build - the application
    name twice, once from the title the window composed and once from Qt.
    The window builds its own title
    (:func:`omr_scanner.gui.main_window.window_title`), and Qt's standard
    dialogs fall back to ``applicationName``, so nothing is lost by leaving
    it unset.
    """
    app.setApplicationName(APPLICATION_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    app.setApplicationVersion(__version__)
    # Sets the default icon for every top-level window and is what the
    # platform (taskbar, dock, alt-tab switcher) reads, in addition to each
    # window's own `setWindowIcon()` for its title bar.
    app.setWindowIcon(application_icon())
    # On the application rather than on each window: `QMessageBox`,
    # `QFileDialog` and `QInputDialog` are constructed by Qt itself or by its
    # static convenience methods, so there is no constructor of theirs to
    # style. This is the only place from which their buttons and labels can be
    # made to match the rest of the interface.
    app.setStyleSheet(application_stylesheet())


def run_gui(config: AppConfig, *, initial_project: Path | None = None) -> int:
    """Run the desktop application until the user closes it.

    Args:
        config: Application configuration loaded by the caller.
        initial_project: Project directory to open on start-up. A failure to
            open it is reported to the user but does not stop the application.

    Returns:
        The Qt exit code, suitable as a process exit status.
    """
    # A QApplication may already exist when the GUI is started from a test.
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([])
    configure_application(app)

    window = MainWindow(config=config)
    # Installed with the window as parent, and before `show()`, so that any
    # exception escaping a slot - even one triggered while the window is
    # first laying itself out - has somewhere to attach its fallback dialog.
    install_global_exception_handler(window)
    window.show()

    if initial_project is not None:
        window.open_project_at(initial_project)

    logger.info("Entering Qt event loop")
    return int(app.exec())
