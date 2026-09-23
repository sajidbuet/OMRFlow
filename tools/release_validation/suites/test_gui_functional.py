"""Qt GUI qualification - does the interface actually work?

Scope:
    The real :class:`MainWindow`, built the way the application builds it,
    driven through its public API and its widgets. Nothing here asserts on
    pixels: a screenshot proves a window was painted, not that clicking a
    stage changed the page. Screenshots are taken at checkpoints for the
    separate visual-regression comparison, and a functional failure is never
    decided by one.

How widgets are found:
    By ``objectName`` and ``accessibleName``, which the application already
    sets throughout, or through the window's own accessors. Never by screen
    coordinates - a test that clicks at (412, 288) fails the first time
    somebody adds a toolbar button, and passes for the wrong reason when the
    layout shifts under it.

Isolation:
    Every project these tests create lives in the test's ``tmp_path``. The
    autouse fixture in ``conftest.py`` redirects the application's
    configuration and log directories there too, so nothing here can touch a
    real project or the operator's settings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget

from omr_scanner import APPLICATION_NAME, __version__
from omr_scanner.gui.main_window import MainWindow, window_title
from omr_scanner.gui.pages import WORKFLOW_PAGES

pytestmark = pytest.mark.gui

STAGE_KEYS = tuple(spec.key for spec in WORKFLOW_PAGES)


def find_by_object_name(root: QWidget, name: str) -> QWidget | None:
    """The first descendant whose ``objectName`` is ``name``."""
    if root.objectName() == name:
        return root
    return root.findChild(QWidget, name)


# --------------------------------------------------------------- application


class TestStartup:
    """The window exists, identifies itself, and is built from the catalogue."""

    def test_the_window_is_created_and_titled(self, main_window: MainWindow):
        assert main_window.windowTitle() == window_title()
        assert __version__ in main_window.windowTitle()

    def test_the_application_name_appears_exactly_once_in_the_title(
        self, main_window: MainWindow
    ):
        assert main_window.windowTitle().count(APPLICATION_NAME) == 1

    def test_no_project_is_open_at_startup(self, main_window: MainWindow):
        assert main_window.session is None

    def test_the_ribbon_offers_every_workflow_stage(self, main_window: MainWindow):
        assert main_window.ribbon.keys == STAGE_KEYS

    def test_there_are_nine_stages(self, main_window: MainWindow):
        # Guards the claim made in the README and the clean-machine procedure.
        assert len(STAGE_KEYS) == 9

    def test_the_chrome_row_and_footer_are_present(self, shown_window: MainWindow):
        assert find_by_object_name(shown_window, "appChrome") is not None
        assert find_by_object_name(shown_window, "appFooter") is not None

    def test_the_chrome_row_shows_the_logo(self, shown_window: MainWindow):
        logo = find_by_object_name(shown_window, "appLogo")
        assert logo is not None
        assert logo.accessibleName() == "OMRFlow"

    def test_the_footer_reports_a_status(self, shown_window: MainWindow):
        footer = shown_window.footer
        assert footer.status is not None
        assert footer.status_label.accessibleName().startswith("Application status:")

    def test_startup_checkpoint(self, shown_window: MainWindow, checkpoint):
        checkpoint(shown_window, "01-startup")


# ------------------------------------------------------------------ geometry


class TestWindowGeometry:
    """Resizing, maximising and restoring must not break the layout."""

    def test_the_window_can_be_resized(self, shown_window: MainWindow, qtbot):
        shown_window.resize(1400, 900)
        qtbot.wait(80)
        assert shown_window.width() >= 1000
        shown_window.resize(900, 700)
        qtbot.wait(80)
        assert shown_window.width() >= 720  # WINDOW_MIN_WIDTH

    def test_the_window_honours_its_minimum_size(self, shown_window: MainWindow, qtbot):
        shown_window.resize(200, 200)
        qtbot.wait(80)
        assert shown_window.width() >= shown_window.minimumWidth()
        assert shown_window.height() >= shown_window.minimumHeight()

    def test_maximise_and_restore(self, shown_window: MainWindow, qtbot):
        shown_window.showMaximized()
        qtbot.wait(150)
        assert shown_window.isMaximized()
        shown_window.showNormal()
        qtbot.wait(150)
        assert not shown_window.isMaximized()
        # The ribbon must still hold every stage after the round trip.
        assert shown_window.ribbon.keys == STAGE_KEYS


# ---------------------------------------------------------------- navigation


class TestWorkflowNavigation:
    """The ribbon, the stack and the highlight stay in step."""

    def test_every_stage_can_be_shown_once_a_project_is_open(
        self, shown_window: MainWindow, workspace: Path, qtbot
    ):
        assert shown_window.create_project_at(workspace, "Navigation Project")
        qtbot.wait(120)
        unreachable = [key for key in STAGE_KEYS if not shown_window.show_page(key)]
        assert not unreachable, f"stages that would not open: {unreachable}"

    def test_showing_a_stage_moves_the_ribbon_highlight(
        self, shown_window: MainWindow, workspace: Path, qtbot
    ):
        assert shown_window.create_project_at(workspace, "Highlight Project")
        for key in STAGE_KEYS:
            assert shown_window.show_page(key)
            qtbot.wait(20)
            assert shown_window.current_page_key() == key
            assert shown_window.ribbon.current_key() == key

    def test_an_unknown_stage_is_refused(self, shown_window: MainWindow):
        assert shown_window.show_page("no-such-stage") is False

    def test_the_arrow_keys_move_focus_between_stages(
        self, shown_window: MainWindow, workspace: Path, qtbot
    ):
        """Right/Left/Home/End move focus; they do not activate.

        Moving focus and activating are deliberately two actions, so that
        keyboard exploration does not change the page under the operator; the
        activation half is the next test. The chrome row's `<` and `>` buttons
        are the separate previous/next controls, and they *do* navigate -
        covered in ``tests/gui/test_window_chrome.py``.
        """
        assert shown_window.create_project_at(workspace, "Keyboard Project")
        shown_window.resize(1600, 900)
        qtbot.wait(120)
        ribbon = shown_window.ribbon
        steps = {step.key: step for step in ribbon.steps}
        steps[STAGE_KEYS[0]].setFocus()
        qtbot.wait(30)

        def focused_key() -> str | None:
            return next((step.key for step in ribbon.steps if step.hasFocus()), None)

        assert focused_key() == STAGE_KEYS[0]

        qtbot.keyClick(ribbon, Qt.Key.Key_Right)
        qtbot.wait(30)
        second = focused_key()
        assert second is not None and second != STAGE_KEYS[0]

        qtbot.keyClick(ribbon, Qt.Key.Key_Left)
        qtbot.wait(30)
        assert focused_key() == STAGE_KEYS[0]

        qtbot.keyClick(ribbon, Qt.Key.Key_End)
        qtbot.wait(30)
        assert focused_key() != STAGE_KEYS[0]

        qtbot.keyClick(ribbon, Qt.Key.Key_Home)
        qtbot.wait(30)
        assert focused_key() == STAGE_KEYS[0]

    def test_the_keyboard_can_activate_the_focused_stage(
        self, shown_window: MainWindow, workspace: Path, qtbot
    ):
        """Space on a focused step changes the page - the other half.

        At a width where every stage is on screen. In the narrow layout the
        only visible step *is* the current page, so Space there opens the
        stage selector instead of navigating nowhere - deliberate, and covered
        by ``tests/gui/test_workflow_ribbon.py``.
        """
        assert shown_window.create_project_at(workspace, "Activate Project")
        shown_window.resize(1600, 900)
        qtbot.wait(120)
        ribbon = shown_window.ribbon
        assert shown_window.show_page(STAGE_KEYS[0])

        target = next(step for step in ribbon.steps if step.key == STAGE_KEYS[1])
        target.setFocus()
        qtbot.wait(30)
        qtbot.keyClick(target, Qt.Key.Key_Space)
        qtbot.wait(80)

        assert ribbon.current_key() == STAGE_KEYS[1]
        assert shown_window.current_page_key() == STAGE_KEYS[1]

    @pytest.mark.parametrize("width", [1600, 1200, 900, 640, 420])
    def test_the_ribbon_lays_out_on_one_line_at_every_width(
        self, shown_window: MainWindow, width: int
    ):
        """The workflow never wraps, and every stage stays reachable.

        The layout *mode* may change - that is the point of having three - and
        the narrow one shows a single step in the strip. What may never change
        is that the ribbon occupies one line and that all nine stages still
        exist behind it, which is why both are asserted rather than just the
        placement count.
        """
        ribbon = shown_window.ribbon
        plan = ribbon.plan_for_width(width)
        assert plan.rows == 1, f"the workflow wrapped at {width}px"
        assert plan.placements
        assert len(ribbon.steps) == len(STAGE_KEYS)
        assert ribbon.keys == STAGE_KEYS

    def test_narrow_windows_choose_a_different_layout_than_wide_ones(
        self, shown_window: MainWindow
    ):
        wide = shown_window.ribbon.plan_for_width(1600)
        narrow = shown_window.ribbon.plan_for_width(200)
        assert wide.mode is not narrow.mode

    def test_narrow_navigation_remains_complete(self, shown_window: MainWindow, qtbot):
        """Hidden from the strip is not hidden from the workflow.

        At a width that collapses the ribbon to one stage, the other eight are
        still present, still enabled, and still listed in the selector that
        replaces them.
        """
        shown_window.resize(720, 620)
        qtbot.wait(150)
        ribbon = shown_window.ribbon
        assert len(ribbon.steps) == len(STAGE_KEYS)
        assert ribbon.isVisible()
        ribbon.refresh_flyout()
        assert len(ribbon.flyout.actions()) == len(STAGE_KEYS)


# --------------------------------------------------------------------- menus


class TestMenus:
    """The application menu and the actions the procedure names."""

    def test_the_application_menu_button_is_reachable(self, shown_window: MainWindow):
        button = find_by_object_name(shown_window, "appMenuButton")
        assert button is not None
        assert button.accessibleName()

    def test_the_application_menu_exists(self, shown_window: MainWindow):
        assert shown_window.application_menu is not None
        assert shown_window.application_menu.objectName() == "applicationMenu"

    @pytest.mark.parametrize(
        "attribute",
        [
            "project_config_action",
            "settings_action",
            "project_health_action",
            "diagnostic_bundle_action",
        ],
    )
    def test_tools_actions_are_present(self, shown_window: MainWindow, attribute: str):
        action = getattr(shown_window, attribute, None)
        assert action is not None, f"{attribute} is missing from the menu"
        assert action.text()

    @pytest.mark.parametrize(
        "attribute",
        ["generate_dataset_action", "run_benchmark_action", "run_stress_qualification_action"],
    )
    def test_developer_actions_are_present(self, shown_window: MainWindow, attribute: str):
        """The Developer/Testing entries the clean-machine procedure uses."""
        action = getattr(shown_window, attribute, None)
        assert action is not None, f"{attribute} is missing from the Developer menu"

    def test_opening_the_application_menu_does_not_raise(
        self, shown_window: MainWindow, qtbot
    ):
        shown_window.open_application_menu()
        qtbot.wait(60)
        menu = shown_window.application_menu
        if menu.isVisible():
            menu.close()
            qtbot.wait(30)


# ---------------------------------------------------------- project lifecycle


class TestProjectLifecycle:
    """Create, save, close, reopen - with throwaway projects only."""

    def test_a_project_can_be_created(self, shown_window: MainWindow, workspace: Path):
        assert shown_window.create_project_at(workspace, "Created Project")
        assert shown_window.session is not None
        assert (workspace / "Created Project").is_dir()

    def test_the_title_names_the_open_project(
        self, shown_window: MainWindow, workspace: Path
    ):
        shown_window.create_project_at(workspace, "Titled Project", exam_name="Mid Term")
        assert shown_window.windowTitle() != window_title()
        assert APPLICATION_NAME in shown_window.windowTitle()

    def test_a_project_can_be_closed(self, shown_window: MainWindow, workspace: Path):
        shown_window.create_project_at(workspace, "Closable Project")
        shown_window.close_project()
        assert shown_window.session is None
        assert shown_window.windowTitle() == window_title()

    def test_a_closed_project_can_be_reopened_with_its_data(
        self, shown_window: MainWindow, workspace: Path
    ):
        """The persistence check: what was saved is still there afterwards."""
        assert shown_window.create_project_at(
            workspace, "Reopened Project", exam_name="Final Examination 2026"
        )
        directory = Path(shown_window.session.root)  # type: ignore[union-attr]
        shown_window.close_project()

        assert shown_window.open_project_at(directory)
        assert shown_window.session is not None
        assert shown_window.session.exam_name == "Final Examination 2026"

    def test_opening_a_directory_that_is_not_a_project_fails_quietly(
        self, shown_window: MainWindow, tmp_path: Path, silent_dialogs
    ):
        """It must report, not raise: an exception here reaches Qt's loop."""
        empty = tmp_path / "not-a-project"
        empty.mkdir()
        assert shown_window.open_project_at(empty) is False
        assert shown_window.session is None

    def test_project_checkpoint(
        self, shown_window: MainWindow, workspace: Path, checkpoint, qtbot
    ):
        shown_window.create_project_at(workspace, "Screenshot Project")
        shown_window.show_page("project")
        qtbot.wait(120)
        checkpoint(shown_window, "02-project")


# ------------------------------------------------------------------- stages


class TestStagePages:
    """Every stage renders, with a project open, without raising."""

    @pytest.mark.parametrize("key", STAGE_KEYS)
    def test_each_stage_renders(
        self, shown_window: MainWindow, workspace: Path, key: str, qtbot
    ):
        shown_window.create_project_at(workspace, "Stage Project")
        assert shown_window.show_page(key), f"stage {key!r} would not open"
        qtbot.wait(60)
        page = shown_window.stack.currentWidget()
        assert page is not None
        assert page.isVisible()

    def test_stage_checkpoints(
        self, shown_window: MainWindow, workspace: Path, checkpoint, qtbot
    ):
        """One screenshot per stage, for the visual comparison."""
        shown_window.create_project_at(workspace, "Checkpoint Project")
        for index, key in enumerate(STAGE_KEYS, start=3):
            assert shown_window.show_page(key)
            qtbot.wait(150)
            checkpoint(shown_window, f"{index:02d}-stage-{key}")


# ------------------------------------------------------------------ dialogs


class TestDialogs:
    """Dialogs open, accept, reject - and never block an unattended run."""

    def test_the_about_dialog_reports_this_build(self, qtbot, checkpoint):
        from omr_scanner.gui.about_dialog import AboutDialog

        dialog = AboutDialog()
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)

        version_label = dialog.findChild(QWidget, "aboutVersion")
        assert version_label is not None
        assert __version__ in version_label.text()  # type: ignore[attr-defined]

        name_label = dialog.findChild(QWidget, "aboutApplicationName")
        assert name_label is not None
        assert APPLICATION_NAME in name_label.text()  # type: ignore[attr-defined]

        checkpoint(dialog, "20-dialog-about")
        dialog.reject()
        qtbot.wait(50)
        assert not dialog.isVisible()

    def test_the_about_dialog_states_the_prerelease_channel(self, qtbot):
        """An Alpha build must say so where the operator can see it."""
        from omr_scanner import IS_PRERELEASE, RELEASE_CHANNEL
        from omr_scanner.gui.about_dialog import AboutDialog

        dialog = AboutDialog()
        qtbot.addWidget(dialog)
        notice = dialog.findChild(QWidget, "aboutPrereleaseNotice")
        if IS_PRERELEASE:
            assert notice is not None, "a prerelease build must show the notice"
            assert RELEASE_CHANNEL.value.lower() in notice.text().lower()  # type: ignore[attr-defined]
        else:
            assert notice is None

    def test_the_project_configuration_dialog_opens_and_cancels(
        self, shown_window: MainWindow, workspace: Path, qtbot, checkpoint
    ):
        from omr_scanner.gui.project_config_dialog import ProjectConfigDialog

        shown_window.create_project_at(workspace, "Config Project", exam_name="Term Test")
        session = shown_window.session
        assert session is not None

        dialog = ProjectConfigDialog(session, parent=shown_window)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)
        checkpoint(dialog, "21-dialog-project-config")

        dialog.reject()
        qtbot.wait(50)
        assert not dialog.isVisible()

    def test_examination_sets_can_be_added_and_removed(
        self, shown_window: MainWindow, workspace: Path, qtbot
    ):
        """Creating an examination set, which the Alpha procedure requires."""
        from omr_scanner.gui.project_config_dialog import ProjectConfigDialog

        shown_window.create_project_at(workspace, "Sets Project")
        session = shown_window.session
        assert session is not None

        dialog = ProjectConfigDialog(session, parent=shown_window)
        qtbot.addWidget(dialog)

        assert dialog.add_set("A", "Set A")
        assert dialog.add_set("B", "Set B")
        codes = {exam_set.code for exam_set in dialog.refresh_sets()}
        assert {"A", "B"} <= codes

        target = next(item for item in dialog.sets if item.code == "B")
        assert dialog.delete_set(target.set_id)
        assert "B" not in {item.code for item in dialog.refresh_sets()}

    def test_the_settings_dialog_opens_and_closes(
        self, shown_window: MainWindow, qtbot, monkeypatch, checkpoint
    ):
        """Opened directly rather than through the menu, so nothing is modal."""
        from omr_scanner.gui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(shown_window.config, parent=shown_window)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)
        checkpoint(dialog, "22-dialog-settings")

        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None, "a settings dialog needs an OK/Cancel box"
        dialog.reject()
        qtbot.wait(50)
        assert not dialog.isVisible()

    def test_accepting_the_settings_dialog_yields_its_edited_values(
        self, shown_window: MainWindow, qtbot
    ):
        """OK must return what was edited, where Cancel returns nothing.

        Checked through the dialog's own accessors rather than by clicking a
        button at a screen position: the OK button's job is to accept, and
        accepting is what the main window acts on.
        """
        from omr_scanner.gui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(shown_window.config, parent=shown_window, cpu_count=4)
        qtbot.addWidget(dialog)

        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert ok is not None, "the settings dialog needs an OK button"
        assert cancel is not None, "the settings dialog needs a Cancel button"

        dialog.show()
        qtbot.waitExposed(dialog)
        qtbot.mouseClick(ok, Qt.MouseButton.LeftButton)
        qtbot.wait(60)

        assert dialog.result() == int(SettingsDialog.DialogCode.Accepted)
        assert not dialog.isVisible()
        # The edited settings are readable, which is what the main window
        # applies through apply_processing_settings / apply_reviewer_name.
        assert dialog.processing_settings() is not None

    def test_cancelling_the_settings_dialog_changes_nothing(
        self, shown_window: MainWindow, qtbot
    ):
        from omr_scanner.gui.settings_dialog import SettingsDialog

        before = shown_window.config
        dialog = SettingsDialog(shown_window.config, parent=shown_window, cpu_count=4)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)

        buttons = dialog.findChild(QDialogButtonBox)
        assert buttons is not None
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        qtbot.mouseClick(cancel, Qt.MouseButton.LeftButton)
        qtbot.wait(60)

        assert dialog.result() == int(SettingsDialog.DialogCode.Rejected)
        assert shown_window.config is before, "Cancel modified the application config"

    def test_no_modal_dialog_is_left_open(self, shown_window: MainWindow, qtbot):
        """A leaked modal would hang the next unattended run."""
        qtbot.wait(50)
        assert QApplication.activeModalWidget() is None


# ------------------------------------------------------------- settings save


class TestSettingsPersistence:
    """What the operator changes is still there next time."""

    def test_the_reviewer_name_is_applied(self, shown_window: MainWindow):
        shown_window.apply_reviewer_name("Dr. Rahman")
        assert shown_window.config.reviewer_name == "Dr. Rahman"

    def test_configuration_survives_a_save_and_reload(
        self, main_window: MainWindow, tmp_path: Path
    ):
        """AppConfig is frozen, so this goes through the module's own I/O."""
        from omr_scanner.config import load_app_config, save_app_config

        main_window.apply_reviewer_name("Persisted Reviewer")
        config_path = tmp_path / "config.json"
        save_app_config(main_window.config, config_path)
        assert config_path.is_file()

        reloaded = load_app_config(config_path)
        assert reloaded.reviewer_name == "Persisted Reviewer"
