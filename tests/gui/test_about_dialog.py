"""Tests for the Help > About dialog.

Scope:
    The dialog is never shown modally here (`exec()` blocks until a user
    closes it); it is constructed directly and its widgets are inspected, the
    same policy `docs/TESTING.md` already applies to every other dialog.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QDialog, QLabel, QPushButton

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
from omr_scanner.gui.about_dialog import DEVELOPER_NAME, AboutDialog

pytestmark = pytest.mark.gui


@pytest.fixture
def dialog(qtbot) -> AboutDialog:
    about = AboutDialog()
    qtbot.addWidget(about)
    return about


def _label_texts(widget: QDialog) -> list[str]:
    return [label.text() for label in widget.findChildren(QLabel)]


class TestAboutDialogCanBeInstantiated:
    def test_it_constructs_without_raising(self, qtbot):
        about = AboutDialog()
        qtbot.addWidget(about)
        assert isinstance(about, QDialog)

    def test_it_accepts_a_parent(self, qtbot):
        from PySide6.QtWidgets import QWidget

        parent = QWidget()
        qtbot.addWidget(parent)
        about = AboutDialog(parent)
        qtbot.addWidget(about)
        assert about.parent() is parent

    def test_the_window_title_names_the_application(self, dialog: AboutDialog):
        assert APPLICATION_NAME in dialog.windowTitle()


class TestIdentityContent:
    def test_the_application_name_is_shown(self, dialog: AboutDialog):
        assert APPLICATION_NAME in _label_texts(dialog)

    def test_the_version_is_shown_and_matches_the_package(self, dialog: AboutDialog):
        texts = _label_texts(dialog)
        assert any(__version__ in text for text in texts)

    def test_the_tagline_is_shown(self, dialog: AboutDialog):
        texts = " ".join(_label_texts(dialog))
        assert "OMR Template Design and Scanning Application" in texts


class TestReleaseMaturityIsStated:
    """A prerelease build must say what it is, where a user looks for it.

    The About dialog is the one place an operator goes to find out what they
    are running, so it carries the release channel and the warning. The
    warning is deliberately *not* a start-up modal: one that appears on every
    launch teaches people to dismiss warnings unread.
    """

    def test_the_release_channel_is_shown_beside_the_version(
        self, dialog: AboutDialog
    ):
        texts = " ".join(_label_texts(dialog))
        assert RELEASE_CHANNEL.value in texts
        assert __version__ in texts

    def test_the_prerelease_notice_is_shown(self, dialog: AboutDialog):
        notice = dialog.findChild(QLabel, "aboutPrereleaseNotice")
        assert notice is not None
        assert notice.text() == ALPHA_NOTICE

    def test_the_notice_says_qualification_is_incomplete_and_to_verify(
        self, dialog: AboutDialog
    ):
        """The two things a tester has to take away from an Alpha build."""
        notice = dialog.findChild(QLabel, "aboutPrereleaseNotice")
        assert notice is not None
        assert "qualification" in notice.text()
        assert "verify" in notice.text().lower()

    def test_the_build_identifier_is_available_for_bug_reports(
        self, dialog: AboutDialog
    ):
        """Version plus source commit, without cluttering the dialog."""
        version_label = dialog.findChild(QLabel, "aboutVersion")
        assert version_label is not None
        assert build_identifier() in version_label.toolTip()

    def test_the_notice_is_tied_to_the_version_not_configured_separately(self):
        """So a stable build cannot keep showing an Alpha warning."""
        assert IS_PRERELEASE is RELEASE_CHANNEL.is_prerelease

    def test_no_modal_warning_is_raised_on_construction(self, qtbot):
        """Constructing the dialog must not itself pop anything up.

        Guards the decision above *and* the offscreen test suite: a modal
        here would block the run with nothing to dismiss it.
        """
        about = AboutDialog()
        qtbot.addWidget(about)
        assert about.findChild(QLabel, "aboutPrereleaseNotice") is not None


class TestAttributionContent:
    """Wording must credit the developer and name the assistance tools separately.

    Dr. Sajid Muhaimin Choudhury is the developer; ChatGPT and Claude Code are
    named only as development assistance.
    """

    def test_the_developer_is_named(self, dialog: AboutDialog):
        texts = " ".join(_label_texts(dialog))
        assert DEVELOPER_NAME in texts
        assert "Dr. Sajid Muhaimin Choudhury" in texts

    def test_development_assistance_tools_are_named(self, dialog: AboutDialog):
        texts = " ".join(_label_texts(dialog))
        assert "ChatGPT" in texts
        assert "Claude Code" in texts

    def test_the_wording_distinguishes_developer_from_assistance(self, dialog: AboutDialog):
        # "Developed by <developer>" and "assistance from <tools>" must be two
        # distinct phrases, not a single sentence crediting all three equally.
        texts = " ".join(_label_texts(dialog))
        assert "Developed by" in texts
        assert "with assistance from" in texts


class TestLicenseContent:
    def test_the_license_name_is_shown(self, dialog: AboutDialog):
        texts = " ".join(_label_texts(dialog))
        assert LICENSE_NAME in texts
        assert "MIT License" in texts

    def test_open_source_wording_is_present(self, dialog: AboutDialog):
        texts = " ".join(_label_texts(dialog))
        assert "open-source" in texts.lower()

    def test_the_copyright_year_is_shown(self, dialog: AboutDialog):
        texts = " ".join(_label_texts(dialog))
        assert str(COPYRIGHT_YEAR) in texts

    def test_a_view_license_button_exists(self, dialog: AboutDialog):
        buttons = [button.text() for button in dialog.findChildren(QPushButton)]
        assert any("License" in text for text in buttons)


class TestRepositoryLink:
    def test_a_repository_button_exists_when_a_url_is_configured(self, dialog: AboutDialog):
        assert REPOSITORY_URL, "REPOSITORY_URL must be set for this test to be meaningful"
        buttons = [button.text() for button in dialog.findChildren(QPushButton)]
        assert any("GitHub" in text or "Repository" in text for text in buttons)

    def test_the_repository_button_opens_the_configured_url(
        self, dialog: AboutDialog, monkeypatch: pytest.MonkeyPatch
    ):
        opened: list[str] = []
        monkeypatch.setattr(
            "omr_scanner.gui.about_dialog.QDesktopServices.openUrl",
            staticmethod(lambda url: opened.append(url.toString())),
        )
        dialog._open_repository()
        assert opened == [REPOSITORY_URL]


class TestLicenseViewer:
    def test_view_license_opens_a_dialog_when_the_license_file_exists(
        self, dialog: AboutDialog, monkeypatch: pytest.MonkeyPatch
    ):
        from omr_scanner.gui.about_dialog import _LICENSE_FILE

        if not _LICENSE_FILE.is_file():
            pytest.skip("No local LICENSE file to view in this checkout")

        opened: list[object] = []
        monkeypatch.setattr(
            "omr_scanner.gui.about_dialog.LicenseViewerDialog.exec",
            lambda self: opened.append(self) or QDialog.DialogCode.Accepted,
        )
        dialog._show_licence()
        assert len(opened) == 1

    def test_the_license_viewer_shows_the_file_contents(self, tmp_path, qtbot):
        from omr_scanner.gui.about_dialog import LicenseViewerDialog

        license_file = tmp_path / "LICENSE"
        license_file.write_text("MIT License\n\nSample text.", encoding="utf-8")

        viewer = LicenseViewerDialog(license_file)
        qtbot.addWidget(viewer)

        from PySide6.QtWidgets import QPlainTextEdit

        text_widget = viewer.findChild(QPlainTextEdit)
        assert text_widget is not None
        assert "Sample text." in text_widget.toPlainText()
