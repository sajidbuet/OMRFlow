"""The Help > About dialog.

Purpose:
    Show the application's identity, authorship and licensing information in a
    dedicated, clean dialog appropriate for an open-source desktop application.

Responsibilities:
    * Read the application name, version, repository URL and licence name from
      :mod:`omr_scanner` - the single source already used for the project's
      identity elsewhere (``project.json`` metadata, log headers) - rather than
      duplicating any of them here.
    * Offer a clickable link to the public repository and a way to read the
      full licence text, either from the repository's local ``LICENSE`` file
      or, failing that, the repository's licence page online.

What does NOT belong here:
    * Any application logic. This dialog only presents static text and two
      links; :class:`~omr_scanner.gui.main_window.MainWindow` owns nothing more
      than the menu action that opens it, per the project's rule that
      substantial GUI behaviour does not live inline in the main window.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner import APPLICATION_NAME, COPYRIGHT_YEAR, LICENSE_NAME, REPOSITORY_URL, __version__

DEVELOPER_NAME = "Dr. Sajid Muhaimin Choudhury"
"""The project's author. ChatGPT and Claude Code assisted with development;
they are not credited as authors."""

DEVELOPMENT_ASSISTANCE = "ChatGPT and Claude Code"

TAGLINE = "OMR Template Design and Scanning Application"

_LICENSE_FILE = Path(__file__).resolve().parents[3] / "LICENSE"
"""The repository's licence file: ``<repo root>/LICENSE``, three directories
above this one (``src/omr_scanner/gui/about_dialog.py``)."""


class AboutDialog(QDialog):
    """A clean, professional About box: identity, authorship, licence.

    Args:
        parent: Owning widget, typically the main window.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {APPLICATION_NAME}")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        name_label = QLabel(APPLICATION_NAME)
        name_label.setObjectName("aboutApplicationName")
        name_font = name_label.font()
        name_font.setPointSize(name_font.pointSize() + 8)
        name_font.setBold(True)
        name_label.setFont(name_font)
        layout.addWidget(name_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        version_label = QLabel(f"Version {__version__}")
        version_label.setObjectName("aboutVersion")
        layout.addWidget(version_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        layout.addSpacing(10)

        tagline_label = QLabel(TAGLINE)
        tagline_label.setObjectName("aboutTagline")
        tagline_label.setWordWrap(True)
        tagline_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(tagline_label)

        layout.addSpacing(10)

        attribution_label = QLabel(
            f"Developed by<br><b>{DEVELOPER_NAME}</b><br>"
            f"with assistance from {DEVELOPMENT_ASSISTANCE}"
        )
        attribution_label.setObjectName("aboutAttribution")
        attribution_label.setTextFormat(Qt.TextFormat.RichText)
        attribution_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        attribution_label.setWordWrap(True)
        layout.addWidget(attribution_label)

        layout.addSpacing(10)

        licence_label = QLabel(
            f"© {COPYRIGHT_YEAR} {DEVELOPER_NAME}<br>"
            f"This is an open-source project released under the {LICENSE_NAME} License."
        )
        licence_label.setObjectName("aboutLicenseSummary")
        licence_label.setTextFormat(Qt.TextFormat.RichText)
        licence_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        licence_label.setWordWrap(True)
        layout.addWidget(licence_label)

        layout.addSpacing(14)
        layout.addLayout(self._build_link_row())
        layout.addSpacing(6)
        layout.addWidget(self._build_button_box())

        self.setMinimumWidth(360)

    def _build_link_row(self) -> QHBoxLayout:
        """Build the GitHub Repository / View License button row."""
        row = QHBoxLayout()
        if REPOSITORY_URL:
            repository_button = QPushButton("GitHub Repository")
            repository_button.setObjectName("aboutRepositoryButton")
            repository_button.clicked.connect(self._open_repository)
            row.addWidget(repository_button)

        licence_button = QPushButton("View License")
        licence_button.setObjectName("aboutLicenseButton")
        licence_button.clicked.connect(self._show_licence)
        row.addWidget(licence_button)
        return row

    def _build_button_box(self) -> QDialogButtonBox:
        """Build the closing button row."""
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        return buttons

    def _open_repository(self) -> None:
        """Open the public repository in the user's default browser."""
        QDesktopServices.openUrl(QUrl(REPOSITORY_URL))

    def _show_licence(self) -> None:
        """Show the full licence text, locally if possible, online otherwise."""
        if _LICENSE_FILE.is_file():
            LicenseViewerDialog(_LICENSE_FILE, self).exec()
        elif REPOSITORY_URL:
            QDesktopServices.openUrl(QUrl(f"{REPOSITORY_URL}/blob/main/LICENSE"))
        else:
            QMessageBox.information(
                self,
                "License",
                f"This project is released under the {LICENSE_NAME} License.",
            )


class LicenseViewerDialog(QDialog):
    """A small read-only viewer for the repository's ``LICENSE`` file.

    Args:
        license_path: Path to the licence text file to display.
        parent: Owning widget, typically :class:`AboutDialog`.
    """

    def __init__(self, license_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{LICENSE_NAME} License")
        self.resize(560, 480)

        layout = QVBoxLayout(self)

        text_view = QPlainTextEdit()
        text_view.setObjectName("licenseText")
        text_view.setReadOnly(True)
        text_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        text_view.setPlainText(license_path.read_text(encoding="utf-8"))
        layout.addWidget(text_view)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


__all__ = ["AboutDialog", "LicenseViewerDialog"]
