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

from omr_scanner import (
    ALPHA_NOTICE,
    APPLICATION_NAME,
    COPYRIGHT_YEAR,
    IS_PRERELEASE,
    LICENSE_NAME,
    RELEASE_CHANNEL,
    REPOSITORY_URL,
    __version__,
    build_identifier,
)
from omr_scanner.gui.theme import Color, FontWeight
from omr_scanner.gui.ui_scale import (
    add_scaled_spacing,
    current_scale,
    resize_scaled,
    scale_layout,
    scale_widget,
    set_relative_font,
)

ABOUT_TITLE_DELTA = 8
"""How many points the application name stands above the body text."""

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
        scale_layout(layout, spacing=6)

        name_label = QLabel(APPLICATION_NAME)
        name_label.setObjectName("aboutApplicationName")
        set_relative_font(name_label, ABOUT_TITLE_DELTA, weight=FontWeight.BOLD)
        layout.addWidget(name_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        version_label = QLabel(f"Version {__version__}  ·  {RELEASE_CHANNEL.value}")
        version_label.setObjectName("aboutVersion")
        version_label.setToolTip(f"Build {build_identifier()}")
        layout.addWidget(version_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        if IS_PRERELEASE:
            add_scaled_spacing(layout, 8)
            layout.addWidget(self._build_prerelease_notice())

        add_scaled_spacing(layout, 10)

        tagline_label = QLabel(TAGLINE)
        tagline_label.setObjectName("aboutTagline")
        tagline_label.setWordWrap(True)
        tagline_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(tagline_label)

        add_scaled_spacing(layout, 10)

        attribution_label = QLabel(
            f"Developed by<br><b>{DEVELOPER_NAME}</b><br>"
            f"with assistance from {DEVELOPMENT_ASSISTANCE}"
        )
        attribution_label.setObjectName("aboutAttribution")
        attribution_label.setTextFormat(Qt.TextFormat.RichText)
        attribution_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        attribution_label.setWordWrap(True)
        layout.addWidget(attribution_label)

        add_scaled_spacing(layout, 10)

        licence_label = QLabel(
            f"© {COPYRIGHT_YEAR} {DEVELOPER_NAME}<br>"
            f"This is an open-source project released under the {LICENSE_NAME} License."
        )
        licence_label.setObjectName("aboutLicenseSummary")
        licence_label.setTextFormat(Qt.TextFormat.RichText)
        licence_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        licence_label.setWordWrap(True)
        layout.addWidget(licence_label)

        add_scaled_spacing(layout, 14)
        layout.addLayout(self._build_link_row())
        add_scaled_spacing(layout, 6)
        layout.addWidget(self._build_button_box())

        scale_widget(self, minimum_width=360)

    def _build_prerelease_notice(self) -> QLabel:
        """The release-maturity warning, shown only on a prerelease build.

        Here and in the documentation, and deliberately nowhere else. A modal
        on every launch would teach an operator to dismiss warnings without
        reading them, which is worse than not warning at all; the About
        dialog is where someone looks to find out what they are running.

        Driven by :data:`omr_scanner.IS_PRERELEASE`, which is derived from the
        version string - so a stable build cannot accidentally keep showing
        an Alpha warning, and an Alpha build cannot accidentally hide one.
        """
        notice = QLabel(ALPHA_NOTICE)
        notice.setObjectName("aboutPrereleaseNotice")
        notice.setWordWrap(True)
        notice.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        scale = current_scale()
        notice.setStyleSheet(
            f"color: {Color.TEXT_PRIMARY};"
            f"background: {Color.PRIMARY_SOFT};"
            f"border: {scale.stroke(1)}px solid {Color.PRIMARY};"
            f"border-radius: {scale.px(6)}px;"
            f"padding: {scale.px(8)}px;"
        )
        return notice

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
        resize_scaled(self, 560, 480)

        layout = QVBoxLayout(self)

        text_view = QPlainTextEdit()
        text_view.setObjectName("licenseText")
        text_view.setReadOnly(True)
        # The platform's monospace font at its own size, times the interface
        # zoom: the licence is a fixed-width document, so it keeps its own
        # family and size relationship rather than the UI font's.
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        if fixed.pointSizeF() > 0:
            fixed.setPointSizeF(fixed.pointSizeF() * current_scale().factor)
        text_view.setFont(fixed)
        text_view.setPlainText(license_path.read_text(encoding="utf-8"))
        layout.addWidget(text_view)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


__all__ = ["AboutDialog", "LicenseViewerDialog"]
