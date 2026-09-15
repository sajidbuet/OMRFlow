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
from omr_scanner.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


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
    app.setApplicationName(APPLICATION_NAME)
    app.setApplicationDisplayName(APPLICATION_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    app.setApplicationVersion(__version__)

    window = MainWindow(config=config)
    window.show()

    if initial_project is not None:
        window.open_project_at(initial_project)

    logger.info("Entering Qt event loop")
    return int(app.exec())
