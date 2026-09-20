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
from omr_scanner.gui.pages.placeholder_page import PlaceholderPage

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


def test_a_placeholder_page_says_so(qtbot):
    # This used to walk WORKFLOW_PAGES looking for whichever real stage was
    # still honestly unimplemented - by design, so it would "fail loudly once
    # every stage is built rather than quietly passing on nothing" (its own
    # former docstring). Phase 9 implementing Reports was that moment: no
    # catalog entry is unimplemented any longer (see
    # test_every_workflow_stage_is_no_longer_a_placeholder below), so the
    # thing this test exists to check - that PlaceholderPage itself renders
    # its notice correctly - is now verified against a page built directly
    # from a synthetic spec instead of depending on the live catalog having
    # one left. PlaceholderPage's own contract is what matters here, not
    # which phase currently happens to be incomplete.
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec

    spec = WorkflowPageSpec(
        key="_test_unimplemented",
        title="Future Stage",
        summary="A stage a later phase will implement.",
        phase=99,
        details=("Do the thing.",),
    )
    page = PlaceholderPage(spec)
    qtbot.addWidget(page)
    texts = [label.text() for label in page.findChildren(QLabel)]

    assert spec.is_implemented is False
    assert any("Not implemented yet" in text for text in texts)
    assert any("phase 99" in text for text in texts)
    assert any("Do the thing." in text for text in texts)


def test_every_workflow_stage_is_no_longer_a_placeholder(window: MainWindow):
    # The positive statement test_a_placeholder_page_says_so's own docstring
    # points to: as of Phase 9, every stage in the real navigation is a real
    # page. A future phase that adds a new WORKFLOW_PAGES entry (Phase 10/11
    # add no new GUI stage per development/ROADMAP.md) would need to update
    # this alongside it.
    for index, spec in enumerate(WORKFLOW_PAGES):
        assert spec.is_implemented, f"{spec.key} is still a placeholder"
        page = window.stack.widget(index)
        assert not isinstance(page, PlaceholderPage), (
            f"{spec.key} is still rendering the placeholder widget"
        )


def test_the_resolve_page_is_no_longer_a_placeholder(window: MainWindow):
    index = next(
        index for index, spec in enumerate(WORKFLOW_PAGES) if spec.key == "resolve"
    )
    page = window.stack.widget(index)
    assert page.objectName() == "resolvePage"
    assert page.spec.is_implemented is True


def test_template_page_is_no_longer_a_placeholder(window: MainWindow):
    template_page = window.stack.widget(1)

    assert template_page.spec.key == "template"
    assert template_page.spec.is_implemented


def test_scan_page_is_no_longer_a_placeholder(window: MainWindow):
    scan_index = next(index for index, spec in enumerate(WORKFLOW_PAGES) if spec.key == "scan")
    scan_page = window.stack.widget(scan_index)

    assert scan_page.spec.key == "scan"
    assert scan_page.spec.is_implemented
    assert scan_page.objectName() == "scanPage"


def test_calibration_page_is_no_longer_a_placeholder(window: MainWindow):
    calibration_index = next(
        index for index, spec in enumerate(WORKFLOW_PAGES) if spec.key == "calibration"
    )
    calibration_page = window.stack.widget(calibration_index)

    assert calibration_page.spec.key == "calibration"
    assert calibration_page.spec.is_implemented
    assert calibration_page.objectName() == "calibrationPage"


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


def test_opening_a_locked_project_directly_reports_an_error(
    window: MainWindow, workspace: Path, silent_message_boxes: list[tuple[str, str]]
):
    """Confirm `open_project_at` reports a lock conflict as a plain error.

    It never shows the resolution dialog (Phase 10, §3) - only the ordinary
    error report, so it stays a plain, hang-free, testable method. The
    three-choice follow-up belongs to `_prompt_open_project` alone.
    """
    from omr_scanner.services import create_project

    with create_project(workspace, "Locked Exam") as first:
        root = first.root

        opened = window.open_project_at(root)

        assert opened is False
        assert window.session is None
        assert silent_message_boxes
        assert silent_message_boxes[0][0] == "Open project"


def test_open_project_resolving_lock_can_open_read_only(
    window: MainWindow, workspace: Path
):
    from omr_scanner.services import create_project

    with create_project(workspace, "Read Only Via Resolve") as first:
        root = first.root

        opened = window.open_project_resolving_lock(root, action="read_only")

        assert opened is True
        assert window.session is not None
        assert window.session.read_only is True


def test_open_project_resolving_lock_can_force_open(window: MainWindow, workspace: Path):
    from omr_scanner.services import create_project

    with create_project(workspace, "Force Via Resolve") as first:
        root = first.root

        opened = window.open_project_resolving_lock(root, action="force")

        assert opened is True
        assert window.session is not None
        assert window.session.read_only is False


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
