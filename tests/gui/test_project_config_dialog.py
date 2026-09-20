"""GUI tests for the Project Configuration dialog.

Scope:
    These drive the real dialog - its widgets, its table, its enabled/disabled
    state and the methods its buttons are wired to - not the service layer
    underneath it, which has its own tests in
    ``tests/integration/test_project_sets.py``. What is deliberately never
    driven here is a modal: the ``_prompt_*`` methods own those, and the
    methods beside them do the work, which is the split the rest of this
    GUI suite follows.

    Persistence is asserted by closing the project and opening it again from
    disk, so a test cannot pass on a value that only ever existed in a widget.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialog

from omr_scanner.config import AppConfig
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.project_config_dialog import (
    EXAM_NAME_SAVED_TEXT,
    ProjectConfigDialog,
    SetEditorDialog,
)
from omr_scanner.services import ProjectSession, create_project, open_project, project_sets

pytestmark = pytest.mark.gui

EXAM_NAME = "Recruitment Exam, Bangladesh Submarine Cable Regulatory Authority"

SETS = (
    ("10", "Name of Post: Assistant Engineer (Electrical)"),
    ("11", "Name of Post: Assistant Engineer (Civil)"),
    ("12", "Name of Post: Assistant Engineer (Mechanical)"),
)


@pytest.fixture
def session(workspace: Path):
    """An open project with no sets defined yet."""
    created = create_project(workspace, "BSCRA Recruitment", exam_name=EXAM_NAME)
    yield created
    if not created.is_closed:
        created.close()


@pytest.fixture
def dialog(qtbot, session: ProjectSession) -> ProjectConfigDialog:
    box = ProjectConfigDialog(session)
    qtbot.addWidget(box)
    return box


def _codes(box: ProjectConfigDialog) -> list[str]:
    """The set codes the table is actually showing, read off the widget."""
    return [box.sets_table.item(row, 0).text() for row in range(box.sets_table.rowCount())]


def _descriptions(box: ProjectConfigDialog) -> list[str]:
    return [box.sets_table.item(row, 1).text() for row in range(box.sets_table.rowCount())]


class TestExamNameField:
    def test_it_opens_showing_the_stored_name(self, dialog: ProjectConfigDialog) -> None:
        assert dialog.exam_name_edit.text() == EXAM_NAME

    def test_saving_an_edited_name_reports_success(
        self, dialog: ProjectConfigDialog
    ) -> None:
        dialog.exam_name_edit.setText("Revised Examination Title")
        assert dialog.save_exam_name() is True
        assert dialog.exam_name_status_label.text() == EXAM_NAME_SAVED_TEXT

    def test_a_saved_name_survives_close_and_reopen(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        dialog.exam_name_edit.setText("Revised Examination Title")
        assert dialog.save_exam_name() is True
        root = session.root
        session.close()
        with open_project(root) as reopened:
            assert reopened.exam_name == "Revised Examination Title"

    def test_a_blank_name_is_refused_with_a_message_beside_the_field(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        dialog.exam_name_edit.setText("   ")
        assert dialog.save_exam_name() is False
        assert "blank" in dialog.exam_name_status_label.text().lower()
        assert session.exam_name == EXAM_NAME

    def test_surrounding_whitespace_is_trimmed_in_the_field_too(
        self, dialog: ProjectConfigDialog
    ) -> None:
        dialog.exam_name_edit.setText("   Padded Title   ")
        assert dialog.save_exam_name() is True
        assert dialog.exam_name_edit.text() == "Padded Title"

    def test_the_project_folder_name_is_shown_separately(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        """The two must be visibly different things, not one field reused."""
        from PySide6.QtWidgets import QLabel

        folder_label = dialog.findChild(QLabel, "projectFolderNameLabel")
        assert folder_label is not None
        assert folder_label.text() == session.name == "BSCRA Recruitment"
        assert dialog.exam_name_edit.text() == EXAM_NAME != session.name


class TestAddSet:
    def test_adding_a_set_shows_it_in_the_table(self, dialog: ProjectConfigDialog) -> None:
        assert dialog.add_set("10", "Name of Post: Assistant Engineer (Electrical)") is True
        assert _codes(dialog) == ["10"]
        assert _descriptions(dialog) == ["Name of Post: Assistant Engineer (Electrical)"]

    def test_the_three_example_sets_all_appear(self, dialog: ProjectConfigDialog) -> None:
        for code, description in SETS:
            assert dialog.add_set(code, description) is True
        assert _codes(dialog) == ["10", "11", "12"]

    def test_an_added_set_survives_close_and_reopen(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        for code, description in SETS:
            dialog.add_set(code, description)
        root = session.root
        session.close()
        with open_project(root) as reopened:
            stored = project_sets.list_sets(reopened.database)
        assert tuple((item.code, item.description) for item in stored) == SETS

    def test_a_blank_code_is_refused_with_a_message(
        self, dialog: ProjectConfigDialog
    ) -> None:
        assert dialog.add_set("", "No code") is False
        assert "blank" in dialog.sets_status_label.text().lower()
        assert dialog.sets_table.rowCount() == 0

    def test_a_duplicate_code_is_refused_with_a_useful_message(
        self, dialog: ProjectConfigDialog
    ) -> None:
        dialog.add_set("10", "Name of Post: Assistant Engineer (Electrical)")
        assert dialog.add_set("10", "Something else") is False
        message = dialog.sets_status_label.text()
        assert "10" in message
        assert "unique" in message.lower()

    def test_a_refused_duplicate_leaves_the_original_untouched(
        self, dialog: ProjectConfigDialog
    ) -> None:
        dialog.add_set("10", "Original description")
        dialog.add_set("10", "Replacement attempt")
        assert _descriptions(dialog) == ["Original description"]
        assert dialog.sets_table.rowCount() == 1

    def test_the_newly_added_set_is_selected(self, dialog: ProjectConfigDialog) -> None:
        dialog.add_set("10", "Electrical")
        dialog.add_set("11", "Civil")
        selected = dialog.selected_set()
        assert selected is not None
        assert selected.code == "11"


class TestEditSet:
    def test_editing_a_description_updates_the_table(
        self, dialog: ProjectConfigDialog
    ) -> None:
        for code, description in SETS:
            dialog.add_set(code, description)
        target = dialog.sets[1]
        assert dialog.edit_set(target.set_id, description="Name of Post: Deputy Manager") is True
        assert _descriptions(dialog)[1] == "Name of Post: Deputy Manager"

    def test_an_edited_description_survives_close_and_reopen(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        for code, description in SETS:
            dialog.add_set(code, description)
        target = dialog.sets[1]
        dialog.edit_set(target.set_id, description="Name of Post: Deputy Manager")
        root = session.root
        session.close()
        with open_project(root) as reopened:
            stored = project_sets.set_by_code(reopened.database, "11")
        assert stored is not None
        assert stored.description == "Name of Post: Deputy Manager"
        assert stored.set_id == target.set_id

    def test_editing_a_code_to_an_existing_one_is_refused(
        self, dialog: ProjectConfigDialog
    ) -> None:
        dialog.add_set("10", "Electrical")
        dialog.add_set("11", "Civil")
        first = dialog.sets[0]
        assert dialog.edit_set(first.set_id, code="11") is False
        assert "already exists" in dialog.sets_status_label.text()
        assert _codes(dialog) == ["10", "11"]

    def test_editing_keeps_the_stable_identifier(self, dialog: ProjectConfigDialog) -> None:
        dialog.add_set("1O", "Typed with a letter O by mistake")
        before = dialog.sets[0].set_id
        assert dialog.edit_set(before, code="10") is True
        assert dialog.sets[0].set_id == before
        assert dialog.sets[0].code == "10"


class TestDeleteSet:
    def test_deleting_removes_the_row(self, dialog: ProjectConfigDialog) -> None:
        for code, description in SETS:
            dialog.add_set(code, description)
        target = dialog.sets[1]
        assert dialog.delete_set(target.set_id) is True
        assert _codes(dialog) == ["10", "12"]

    def test_a_deletion_survives_close_and_reopen(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        for code, description in SETS:
            dialog.add_set(code, description)
        dialog.delete_set(dialog.sets[1].set_id)
        root = session.root
        session.close()
        with open_project(root) as reopened:
            stored = project_sets.list_sets(reopened.database)
        assert [item.code for item in stored] == ["10", "12"]

    def test_deleting_an_unknown_set_reports_rather_than_raises(
        self, dialog: ProjectConfigDialog
    ) -> None:
        assert dialog.delete_set("no-such-id") is False
        assert "no longer exists" in dialog.sets_status_label.text()


class TestReordering:
    def test_moving_a_set_up_changes_the_displayed_order(
        self, dialog: ProjectConfigDialog
    ) -> None:
        for code, description in SETS:
            dialog.add_set(code, description)
        assert dialog.move_set(dialog.sets[2].set_id, -1) is True
        assert _codes(dialog) == ["10", "12", "11"]

    def test_a_new_order_survives_close_and_reopen(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        for code, description in SETS:
            dialog.add_set(code, description)
        dialog.move_set(dialog.sets[2].set_id, -1)
        root = session.root
        session.close()
        with open_project(root) as reopened:
            stored = project_sets.list_sets(reopened.database)
        assert [item.code for item in stored] == ["10", "12", "11"]

    def test_moving_with_nothing_selected_does_nothing(
        self, dialog: ProjectConfigDialog
    ) -> None:
        dialog.add_set("10", "Electrical")
        dialog.sets_table.clearSelection()
        dialog.sets_table.setCurrentCell(-1, -1)
        assert dialog.move_selected_set(-1) is False

    def test_move_buttons_are_disabled_at_the_ends_of_the_list(
        self, dialog: ProjectConfigDialog
    ) -> None:
        for code, description in SETS:
            dialog.add_set(code, description)
        dialog.select_set(dialog.sets[0].set_id)
        assert dialog.move_up_button.isEnabled() is False
        assert dialog.move_down_button.isEnabled() is True
        dialog.select_set(dialog.sets[-1].set_id)
        assert dialog.move_up_button.isEnabled() is True
        assert dialog.move_down_button.isEnabled() is False


class TestButtonState:
    def test_edit_and_delete_are_disabled_without_a_selection(
        self, dialog: ProjectConfigDialog
    ) -> None:
        assert dialog.edit_set_button.isEnabled() is False
        assert dialog.delete_set_button.isEnabled() is False

    def test_they_enable_once_a_set_is_selected(self, dialog: ProjectConfigDialog) -> None:
        dialog.add_set("10", "Electrical")
        assert dialog.edit_set_button.isEnabled() is True
        assert dialog.delete_set_button.isEnabled() is True

    def test_adding_is_always_available(self, dialog: ProjectConfigDialog) -> None:
        assert dialog.add_set_button.isEnabled() is True


class TestManySets:
    def test_the_table_stays_usable_with_sixty_sets(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        """Nothing in the dialog may assume a fixed or small number of sets."""
        for index in range(60):
            assert dialog.add_set(f"S{index:02d}", f"Post number {index}") is True
        assert dialog.sets_table.rowCount() == 60
        assert len(dialog.sets) == 60

        # Still individually addressable: the last row is selectable, movable
        # and identified by its own id rather than its position.
        last = dialog.sets[-1]
        assert dialog.select_set(last.set_id) is True
        assert dialog.move_set(last.set_id, -1) is True
        assert _codes(dialog)[-1] == "S58"

        root = session.root
        session.close()
        with open_project(root) as reopened:
            assert len(project_sets.list_sets(reopened.database)) == 60


class TestALegacyProjectWithNoStoredExamName:
    def test_the_field_opens_blank_rather_than_claiming_a_name_was_set(
        self, qtbot, workspace: Path
    ) -> None:
        """A project that predates `exam_name` has none - and should say so."""
        import json

        with create_project(workspace, "Legacy Exam") as writer:
            root = writer.root
        path = root / "project.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("exam_name", None)
        payload["project_format_version"] = 1
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        with open_project(root) as session:
            box = ProjectConfigDialog(session)
            qtbot.addWidget(box)
            assert box.exam_name_edit.text() == ""
            # ...while everything that has to *show* a name still has one.
            assert session.exam_name == "Legacy Exam"

            box.exam_name_edit.setText(EXAM_NAME)
            assert box.save_exam_name() is True

        with open_project(root) as reopened:
            assert reopened.project.metadata.exam_name == EXAM_NAME


class TestSuggestionsForALegacyProject:
    """A project that predates the set registry can be offered what it mentions."""

    def _record_an_answer_key(self, session: ProjectSession, set_code: str) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import insert

        from omr_scanner.database.models import AnswerKeyRevision

        with session.database.session() as db:
            db.execute(
                insert(AnswerKeyRevision).values(
                    set_code=set_code,
                    revision=1,
                    answers="ABCD",
                    question_count=4,
                    first_question=1,
                    status="verified",
                    source="manual",
                    created_at=datetime.now(UTC),
                )
            )

    def test_the_button_is_hidden_when_there_is_nothing_to_suggest(
        self, dialog: ProjectConfigDialog
    ) -> None:
        assert dialog.suggested_codes() == ()
        # `isVisibleTo`, not `isVisible`: the dialog is never shown in a test,
        # so `isVisible()` is False for every widget and would assert nothing.
        assert dialog.suggest_button.isVisibleTo(dialog) is False

    def test_the_button_appears_when_there_is_something_to_suggest(
        self, qtbot, session: ProjectSession
    ) -> None:
        self._record_an_answer_key(session, "10")
        box = ProjectConfigDialog(session)
        qtbot.addWidget(box)
        assert box.suggest_button.isVisibleTo(box) is True

    def test_a_code_from_an_existing_answer_key_is_offered(
        self, qtbot, session: ProjectSession
    ) -> None:
        self._record_an_answer_key(session, "10")
        box = ProjectConfigDialog(session)
        qtbot.addWidget(box)
        assert box.suggested_codes() == ("10",)

    def test_adopting_them_defines_sets_without_inventing_descriptions(
        self, qtbot, session: ProjectSession
    ) -> None:
        self._record_an_answer_key(session, "10")
        self._record_an_answer_key(session, "11")
        box = ProjectConfigDialog(session)
        qtbot.addWidget(box)

        assert box.adopt_suggested_codes() == 2
        assert _codes(box) == ["10", "11"]
        # Blank, on purpose: only the operator knows what each set was for.
        assert _descriptions(box) == ["", ""]

    def test_nothing_is_offered_twice(self, qtbot, session: ProjectSession) -> None:
        self._record_an_answer_key(session, "10")
        box = ProjectConfigDialog(session)
        qtbot.addWidget(box)
        box.adopt_suggested_codes()
        assert box.suggested_codes() == ()
        assert box.suggest_button.isVisibleTo(box) is False

    def test_adopted_sets_survive_close_and_reopen(
        self, qtbot, session: ProjectSession
    ) -> None:
        self._record_an_answer_key(session, "10")
        box = ProjectConfigDialog(session)
        qtbot.addWidget(box)
        box.adopt_suggested_codes()
        root = session.root
        session.close()
        with open_project(root) as reopened:
            assert [item.code for item in project_sets.list_sets(reopened.database)] == ["10"]


class TestReadOnlyProject:
    def test_every_editing_action_is_disabled(self, qtbot, workspace: Path) -> None:
        with create_project(workspace, "Read Only Exam", exam_name=EXAM_NAME) as writer:
            root = writer.root
        with open_project(root, read_only=True) as reader:
            box = ProjectConfigDialog(reader)
            qtbot.addWidget(box)
            assert box.exam_name_edit.isReadOnly() is True
            assert box.save_exam_name_button.isEnabled() is False
            assert box.add_set_button.isEnabled() is False
            assert box.edit_set_button.isEnabled() is False
            assert box.delete_set_button.isEnabled() is False


class TestSetEditorDialog:
    def test_it_returns_what_was_typed(self, qtbot) -> None:
        editor = SetEditorDialog(code="10", description="Electrical")
        qtbot.addWidget(editor)
        assert editor.values() == ("10", "Electrical")

    def test_it_starts_empty_when_adding(self, qtbot) -> None:
        editor = SetEditorDialog()
        qtbot.addWidget(editor)
        assert editor.values() == ("", "")

    def test_it_does_not_trim_because_the_service_layer_does(self, qtbot) -> None:
        editor = SetEditorDialog(code="  10  ")
        qtbot.addWidget(editor)
        assert editor.values()[0] == "  10  "


class TestMainWindowWiring:
    @pytest.fixture
    def window(self, qtbot, tmp_path: Path) -> MainWindow:
        main_window = MainWindow(
            config=AppConfig(), config_path=tmp_path / "window_config.json"
        )
        qtbot.addWidget(main_window)
        return main_window

    def test_the_action_exists_and_is_disabled_without_a_project(
        self, window: MainWindow
    ) -> None:
        assert window.project_config_action is not None
        assert window.project_config_action.isEnabled() is False

    def test_it_is_enabled_once_a_project_is_open(
        self, window: MainWindow, workspace: Path
    ) -> None:
        window.create_project_at(workspace, "Wired Exam")
        assert window.project_config_action.isEnabled() is True

    def test_it_is_disabled_again_after_closing(
        self, window: MainWindow, workspace: Path
    ) -> None:
        window.create_project_at(workspace, "Wired Exam")
        window.close_project()
        assert window.project_config_action.isEnabled() is False

    def test_creating_a_project_defaults_the_exam_name_to_the_project_name(
        self, window: MainWindow, workspace: Path
    ) -> None:
        window.create_project_at(workspace, "Wired Exam")
        assert window.session is not None
        assert window.session.exam_name == "Wired Exam"

    def test_creating_a_project_can_set_a_distinct_exam_name(
        self, window: MainWindow, workspace: Path
    ) -> None:
        window.create_project_at(workspace, "Wired Exam", exam_name=EXAM_NAME)
        assert window.session is not None
        assert window.session.exam_name == EXAM_NAME
        assert window.session.name == "Wired Exam"

    def test_the_project_page_shows_the_exam_name_and_set_summary(
        self, window: MainWindow, workspace: Path
    ) -> None:
        window.create_project_at(workspace, "Wired Exam", exam_name=EXAM_NAME)
        assert window.session is not None
        for code, description in SETS:
            project_sets.add_set(window.session.database, code, description)
        # Redisplay, exactly as returning from the configuration dialog does.
        window._broadcast_project_change()

        page = window._pages["project"]
        assert page._value_labels["Exam name"].text() == EXAM_NAME
        summary = page._value_labels["Sets"].text()
        assert "3 defined" in summary
        assert "Set 10" in summary


class TestDialogLifecycle:
    def test_it_is_modal_and_titled(self, dialog: ProjectConfigDialog) -> None:
        assert dialog.isModal() is True
        assert dialog.windowTitle() == "Project Configuration"

    def test_closing_it_needs_no_save_because_edits_are_already_stored(
        self, dialog: ProjectConfigDialog, session: ProjectSession
    ) -> None:
        dialog.add_set("10", "Electrical")
        dialog.reject()
        assert dialog.result() == QDialog.DialogCode.Rejected
        # Rejecting the dialog does not undo anything: each action was
        # committed when it was made, which is the documented behaviour.
        assert len(project_sets.list_sets(session.database)) == 1
