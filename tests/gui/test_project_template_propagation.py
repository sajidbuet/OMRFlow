"""One project, one template, three screens.

These tests drive a real :class:`MainWindow` against real project directories,
because the behaviour under test is the wiring between the window, the session
and the three pages - the part that a page-level unit test cannot see.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.config import AppConfig
from omr_scanner.gui.calibration.page import CalibrationPage
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.gui.template_designer.page import TemplateDesignerPage
from omr_scanner.services import create_project, save_template

pytestmark = pytest.mark.gui


@pytest.fixture
def window(qtbot, tmp_path: Path) -> MainWindow:
    """A main window whose configuration is written into the temp directory."""
    main_window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(main_window)
    return main_window


def _project_with_templates(workspace: Path, name: str, *stems: str) -> Path:
    """Create a closed project owning a template per stem, and return its root."""
    with create_project(workspace, name) as session:
        for stem in stems:
            save_template(
                build_answer_sheet_template(),
                session.project.layout.templates_dir / f"{stem}.omrt",
            )
        return session.root


def _pages(window: MainWindow) -> tuple[TemplateDesignerPage, CalibrationPage, ScanPage]:
    designer = window._pages.get("template")
    calibration = window._pages.get("calibration")
    scan = window._pages.get("scan")
    if not isinstance(designer, TemplateDesignerPage):
        pytest.skip("this build has no Template screen")
    if not isinstance(calibration, CalibrationPage):
        pytest.skip("this build has no Calibrate screen")
    if not isinstance(scan, ScanPage):
        pytest.skip("this build has no Scan screen")
    return designer, calibration, scan


# ---------------------------------------------------------------------------
# Opening a project
# ---------------------------------------------------------------------------
def test_opening_a_project_gives_all_three_screens_the_same_template(
    window: MainWindow, tmp_path: Path
):
    root = _project_with_templates(tmp_path / "workspace", "Physics", "Midterm")

    assert window.open_project_at(root)
    designer, calibration, scan = _pages(window)
    expected = root / "templates" / "Midterm.omrt"

    assert designer._designer_state is not None
    assert designer._designer_state.template_path == expected
    assert calibration.state.template_path == expected
    assert scan.state.template_path == expected
    # ...and the choice was written down, so the next open does not re-derive it.
    assert window.session is not None
    assert window.session.project.metadata.active_template == "templates/Midterm.omrt"


def test_a_project_with_two_templates_loads_neither(window: MainWindow, tmp_path: Path):
    """Guessing would read the sheets against the wrong geometry, silently."""
    root = _project_with_templates(tmp_path / "workspace", "Physics", "Midterm", "Final")

    assert window.open_project_at(root)
    designer, calibration, scan = _pages(window)

    assert designer._designer_state is None
    assert calibration.state.template_path is None
    assert scan.state.template_path is None


# ---------------------------------------------------------------------------
# Changing the template from the Template screen
# ---------------------------------------------------------------------------
def test_saving_a_template_makes_it_the_project_template_everywhere(
    window: MainWindow, tmp_path: Path
):
    root = _project_with_templates(tmp_path / "workspace", "Physics", "Midterm", "Final")
    assert window.open_project_at(root)
    designer, calibration, scan = _pages(window)

    chosen = root / "templates" / "Final.omrt"
    designer.open_template_at(chosen)
    assert designer.save()

    assert window.session is not None
    assert window.session.project.metadata.active_template == "templates/Final.omrt"
    assert calibration.state.template_path == chosen
    assert scan.state.template_path == chosen


def test_opening_one_of_the_projects_templates_settles_the_choice(
    window: MainWindow, tmp_path: Path
):
    root = _project_with_templates(tmp_path / "workspace", "Physics", "Midterm", "Final")
    assert window.open_project_at(root)
    designer, calibration, scan = _pages(window)

    chosen = root / "templates" / "Midterm.omrt"
    assert designer.open_template_at(chosen)

    assert window.session is not None
    assert window.session.project.metadata.active_template == "templates/Midterm.omrt"
    assert calibration.state.template_path == chosen
    assert scan.state.template_path == chosen


def test_a_template_outside_the_project_is_not_adopted(window: MainWindow, tmp_path: Path):
    """Looking at someone else's template is not a decision about this project."""
    root = _project_with_templates(tmp_path / "workspace", "Physics", "Midterm")
    outside = save_template(build_answer_sheet_template(), tmp_path / "elsewhere.omrt")

    assert window.open_project_at(root)
    designer, _calibration, _scan = _pages(window)
    assert designer.open_template_at(outside)

    assert window.session is not None
    assert window.session.project.metadata.active_template == "templates/Midterm.omrt"


# ---------------------------------------------------------------------------
# Switching projects
# ---------------------------------------------------------------------------
def test_switching_projects_swaps_the_template_on_every_screen(
    window: MainWindow, tmp_path: Path
):
    workspace = tmp_path / "workspace"
    physics = _project_with_templates(workspace, "Physics", "PhysicsSheet")
    chemistry = _project_with_templates(workspace, "Chemistry", "ChemistrySheet")

    assert window.open_project_at(physics)
    assert window.open_project_at(chemistry)
    designer, calibration, scan = _pages(window)
    expected = chemistry / "templates" / "ChemistrySheet.omrt"

    assert designer._designer_state is not None
    assert designer._designer_state.template_path == expected
    assert calibration.state.template_path == expected
    assert scan.state.template_path == expected


def test_switching_projects_does_not_leave_the_previous_template_behind(
    window: MainWindow, tmp_path: Path
):
    workspace = tmp_path / "workspace"
    physics = _project_with_templates(workspace, "Physics", "PhysicsSheet")
    empty = _project_with_templates(workspace, "Chemistry")

    assert window.open_project_at(physics)
    assert window.open_project_at(empty)
    _designer, calibration, scan = _pages(window)

    assert calibration.state.template_path is None
    assert scan.state.template_path is None


# ---------------------------------------------------------------------------
# A template that has gone missing
# ---------------------------------------------------------------------------
def test_a_missing_template_is_reported_rather_than_loaded(
    window: MainWindow, tmp_path: Path
):
    root = _project_with_templates(tmp_path / "workspace", "Physics", "Midterm")
    assert window.open_project_at(root)
    window.close_project()
    (root / "templates" / "Midterm.omrt").unlink()

    assert window.open_project_at(root)
    _designer, calibration, _scan = _pages(window)

    assert calibration.state.template_path is None
    assert "missing" in calibration.template_name_label.text().casefold()


# ---------------------------------------------------------------------------
# File dialogs start where the project is
# ---------------------------------------------------------------------------
def test_file_dialogs_start_inside_the_open_project(window: MainWindow, tmp_path: Path):
    root = _project_with_templates(tmp_path / "workspace", "Physics", "Midterm")
    assert window.open_project_at(root)
    designer, calibration, scan = _pages(window)

    assert root in calibration._template_dialog_directory().parents or (
        calibration._template_dialog_directory() == root
    )
    assert calibration._scan_dialog_directory() == root / "scans_original"
    assert scan._default_template_dir() == root / "templates"
    assert scan._default_scan_dir() == root / "scans_original"
    assert root in designer._default_browse_dir().parents


def test_file_dialogs_fall_back_to_home_with_no_project(window: MainWindow):
    _designer, calibration, scan = _pages(window)

    assert calibration._scan_dialog_directory() == Path.home()
    assert scan._default_scan_dir() == Path.home()
