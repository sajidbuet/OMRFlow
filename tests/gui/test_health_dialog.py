"""GUI tests for the Project Health & Recovery dialog (Phase 10, §6, §7, §57).

Never `.exec()`-ed - the same policy the rest of this GUI suite follows for
modal dialogs: constructed, driven through its testable methods, and its
state read back directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner.config import AppConfig
from omr_scanner.gui.health_dialog import ProjectHealthDialog
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.services import create_project, project_backup


@pytest.fixture
def window(qtbot, tmp_path: Path) -> MainWindow:
    """A main window whose configuration is written into the temp directory."""
    main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "window_config.json")
    qtbot.addWidget(main_window)
    return main_window


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
def project(workspace: Path):
    session = create_project(workspace, "Health Dialog Exam")
    yield session
    if not session.is_closed:
        session.close()


@pytest.fixture
def dialog(qtbot, project) -> ProjectHealthDialog:
    box = ProjectHealthDialog(project.database, project.root)
    qtbot.addWidget(box)
    return box


class TestHealthSection:
    def test_run_full_check_shows_no_issues_for_a_fresh_project_before_a_backup(
        self, dialog: ProjectHealthDialog
    ):
        report = dialog.run_full_check()
        # A freshly created project genuinely has one finding: no backup has
        # ever been taken yet - that is correct, not a test bug.
        codes = {issue.code for issue in report.issues}
        assert "NO_BACKUPS" in codes

    def test_the_summary_label_updates_after_a_check(self, dialog: ProjectHealthDialog):
        dialog.run_full_check()
        assert dialog.health_summary_label.text()

    def test_the_issue_list_is_populated(self, dialog: ProjectHealthDialog):
        dialog.run_full_check()
        assert dialog.health_list.count() >= 1

    def test_last_report_is_accessible_without_a_dialog(self, dialog: ProjectHealthDialog):
        assert dialog.last_report is None
        report = dialog.run_full_check()
        assert dialog.last_report is report

    def test_no_issues_after_creating_a_backup(self, dialog: ProjectHealthDialog):
        dialog.create_backup(reason="test")
        report = dialog.run_full_check()
        codes = {issue.code for issue in report.issues}
        assert "NO_BACKUPS" not in codes


class TestBackupSection:
    def test_creating_a_backup_adds_it_to_the_list(self, dialog: ProjectHealthDialog):
        assert dialog.backup_list.count() == 0
        dialog.create_backup(reason="test")
        assert dialog.backup_list.count() == 1

    def test_no_backup_is_selected_initially(self, dialog: ProjectHealthDialog):
        assert dialog.selected_backup() is None

    def test_selecting_a_backup_returns_its_entry(self, dialog: ProjectHealthDialog):
        dialog.create_backup(reason="test")
        dialog.backup_list.setCurrentRow(0)
        entry = dialog.selected_backup()
        assert entry is not None
        assert isinstance(entry, project_backup.BackupEntry)

    def test_restoring_a_backup_writes_a_new_file(
        self, dialog: ProjectHealthDialog, tmp_path: Path
    ):
        dialog.create_backup(reason="test")
        dialog.backup_list.setCurrentRow(0)
        entry = dialog.selected_backup()
        assert entry is not None

        destination = tmp_path / "restored.sqlite"
        restored = dialog.restore_backup_to(entry, destination)
        assert restored.is_file()

    def test_refresh_backups_reflects_disk_state(self, dialog: ProjectHealthDialog):
        dialog.create_backup(reason="first")
        dialog.create_backup(reason="second")
        entries = dialog.refresh_backups()
        assert len(entries) == 2
        assert dialog.backup_list.count() == 2


class TestMainWindowWiring:
    def test_the_project_health_action_exists_and_is_disabled_without_a_project(
        self, window: MainWindow
    ):
        assert window.project_health_action is not None
        assert window.project_health_action.isEnabled() is False

    def test_the_action_is_enabled_once_a_project_is_open(
        self, window: MainWindow, workspace: Path
    ):
        window.create_project_at(workspace, "Wired Exam")
        assert window.project_health_action.isEnabled() is True

    def test_the_action_is_disabled_again_after_closing(
        self, window: MainWindow, workspace: Path
    ):
        window.create_project_at(workspace, "Wired Exam 2")
        window.close_project()
        assert window.project_health_action.isEnabled() is False


class TestDiagnosticBundleWiring:
    def test_the_action_exists_and_is_always_enabled(self, window: MainWindow):
        assert window.diagnostic_bundle_action is not None
        assert window.diagnostic_bundle_action.isEnabled() is True

    def test_writing_a_bundle_with_no_project_open_succeeds(
        self, window: MainWindow, tmp_path: Path
    ):
        destination = tmp_path / "bundle.zip"
        assert window.create_diagnostic_bundle_at(destination) is True
        assert destination.is_file()

    def test_writing_a_bundle_with_a_project_open_includes_its_schema_version(
        self, window: MainWindow, workspace: Path, tmp_path: Path
    ):
        import json
        import zipfile

        window.create_project_at(workspace, "Diagnostics Exam")
        destination = tmp_path / "bundle.zip"
        assert window.create_diagnostic_bundle_at(destination) is True

        with zipfile.ZipFile(destination) as archive:
            db_info = json.loads(archive.read("database.json"))
        assert db_info["schema_version"] == window.session.database.schema_version

    def test_a_write_failure_is_reported_not_raised(
        self, window: MainWindow, silent_message_boxes: list[tuple[str, str]]
    ):
        # A directory that cannot possibly be created as a file destination.
        impossible = Path(window.__class__.__module__).anchor  # e.g. "C:\\" itself
        result = window.create_diagnostic_bundle_at(Path(impossible))
        assert result is False
        assert silent_message_boxes
