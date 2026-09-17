"""Smoke tests for the PySide6 shell.

Scope:
    These tests drive the main window's behaviour, never its modal dialogs. The
    dialog-owning ``_prompt_*`` methods are deliberately not exercised; the
    commands they call (:meth:`MainWindow.create_project_at` and friends) are.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel

from omr_scanner import APPLICATION_NAME
from omr_scanner.config import AppConfig, load_app_config
from omr_scanner.gui.main_window import NO_PROJECT_STATUS, MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES

pytestmark = pytest.mark.gui


@pytest.fixture
def silent_message_boxes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture error dialogs instead of showing them, so tests never block."""
    shown: list[tuple[str, str]] = []

    def fake_warning(_parent: object, title: str, text: str, *_args: object) -> None:
        shown.append((title, text))

    monkeypatch.setattr(
        "omr_scanner.gui.error_reporting.QMessageBox.warning", staticmethod(fake_warning)
    )
    return shown


@pytest.fixture
def window(qtbot, tmp_path: Path) -> MainWindow:
    """A main window whose configuration is written into the temp directory."""
    main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(main_window)
    return main_window


def test_window_starts_without_a_project(window: MainWindow):
    assert window.windowTitle() == APPLICATION_NAME
    assert window.session is None
    assert window.navigation.count() == len(WORKFLOW_PAGES)
    assert window.close_project_action.isEnabled() is False
    status_texts = [label.text() for label in window.statusBar().findChildren(QLabel)]
    assert NO_PROJECT_STATUS in status_texts


def test_navigation_switches_the_visible_page(window: MainWindow):
    window.navigation.setCurrentRow(2)

    assert window.stack.currentIndex() == 2
    assert window.stack.currentWidget().spec.key == WORKFLOW_PAGES[2].key


def test_unimplemented_pages_say_so(window: MainWindow):
    # Index 3 ("Resolve"): Phase 2 replaced the Template placeholder and Phase 3
    # replaced the Scan one, so this check walks forward to whichever stage is
    # still honestly unimplemented.
    resolve_page = window.stack.widget(3)
    texts = [label.text() for label in resolve_page.findChildren(QLabel)]

    assert resolve_page.spec.key == "resolve"
    assert resolve_page.spec.phase > 0
    assert not resolve_page.spec.is_implemented
    assert any("Not implemented yet" in text for text in texts)
    assert any(f"phase {resolve_page.spec.phase}" in text for text in texts)


def test_template_page_is_no_longer_a_placeholder(window: MainWindow):
    template_page = window.stack.widget(1)

    assert template_page.spec.key == "template"
    assert template_page.spec.is_implemented


def test_scan_page_is_no_longer_a_placeholder(window: MainWindow):
    scan_page = window.stack.widget(2)

    assert scan_page.spec.key == "scan"
    assert scan_page.spec.is_implemented
    assert scan_page.objectName() == "scanPage"


class TestAboutMenuAction:
    """The Help > About action.

    The dialog's own content is tested in `tests/gui/test_about_dialog.py`.
    """

    def test_the_about_action_exists_under_help(self, window: MainWindow):
        from PySide6.QtWidgets import QMenu

        help_menu = next(
            menu
            for menu in window.menuBar().findChildren(QMenu)
            if menu.title() == "&Help"
        )
        assert window.about_action in help_menu.actions()
        assert APPLICATION_NAME in window.about_action.text()

    def test_triggering_about_opens_the_about_dialog(
        self, window: MainWindow, monkeypatch: pytest.MonkeyPatch
    ):
        from omr_scanner.gui.about_dialog import AboutDialog

        opened: list[AboutDialog] = []
        monkeypatch.setattr(
            "omr_scanner.gui.main_window.AboutDialog.exec",
            lambda self: opened.append(self) or None,
        )

        window.about_action.trigger()

        assert len(opened) == 1
        assert isinstance(opened[0], AboutDialog)


def test_creating_a_project_updates_the_window(window: MainWindow, workspace: Path):
    created = window.create_project_at(workspace, "GUI Exam")

    assert created
    assert window.session is not None
    assert window.windowTitle() == f"GUI Exam - {APPLICATION_NAME}"
    assert window.close_project_action.isEnabled()
    assert (workspace / "GUI Exam" / "project.json").is_file()


def test_project_page_shows_the_open_project(window: MainWindow, workspace: Path):
    project_page = window.stack.widget(0)

    window.create_project_at(workspace, "Visible Exam")
    texts = [label.text() for label in project_page.findChildren(QLabel)]

    assert project_page.spec.key == "project"
    assert "Visible Exam" in texts
    assert any(str(workspace / "Visible Exam") in text for text in texts)


def test_closing_a_project_resets_the_window(window: MainWindow, workspace: Path):
    window.create_project_at(workspace, "Closable Exam")

    window.close_project()

    assert window.session is None
    assert window.windowTitle() == APPLICATION_NAME
    assert window.close_project_action.isEnabled() is False


def test_opening_an_invalid_folder_reports_an_error(
    window: MainWindow, tmp_path: Path, silent_message_boxes: list[tuple[str, str]]
):
    opened = window.open_project_at(tmp_path / "not_a_project")

    assert opened is False
    assert window.session is None
    assert silent_message_boxes
    assert silent_message_boxes[0][0] == "Open project"


def test_recent_projects_are_recorded_in_the_configuration(
    window: MainWindow, workspace: Path, tmp_path: Path
):
    window.create_project_at(workspace, "Remembered Exam")

    stored = load_app_config(tmp_path / "config.json", strict=True)

    assert stored.recent_projects[0] == (workspace / "Remembered Exam").resolve()
    assert window.recent_menu.actions()[0].text() == str(
        (workspace / "Remembered Exam").resolve()
    )


def test_closing_the_window_releases_the_project(window: MainWindow, workspace: Path):
    window.create_project_at(workspace, "Released Exam")
    session = window.session
    assert session is not None

    window.close()

    assert session.is_closed
