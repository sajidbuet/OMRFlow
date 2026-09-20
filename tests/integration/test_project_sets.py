"""End-to-end tests for the examination name and the project's set registry.

What these lock down:
    The brief's own persistence requirement - that a project's configuration
    survives being saved, closed and reopened - is tested the way it is
    stated: by closing the session entirely and opening the project again
    from disk, never by reading back an object still held in memory.

    The example examination used throughout is the one from the brief, so a
    failure here reads in the same terms as the manual validation case.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omr_scanner.database import SCHEMA_VERSION
from omr_scanner.domain.project import PROJECT_FORMAT_VERSION
from omr_scanner.errors import ProjectValidationError
from omr_scanner.services import (
    create_project,
    open_project,
    project_sets,
    read_project_metadata,
    update_exam_name,
)
from omr_scanner.services.project_sets import ProjectSetError

EXAM_NAME = "Recruitment Exam, Bangladesh Submarine Cable Regulatory Authority"

SETS = (
    ("10", "Name of Post: Assistant Engineer (Electrical)"),
    ("11", "Name of Post: Assistant Engineer (Civil)"),
    ("12", "Name of Post: Assistant Engineer (Mechanical)"),
)


@pytest.fixture
def project_root(workspace: Path) -> Path:
    """Create the example project, close it, and return its root directory."""
    with create_project(workspace, "BSCRA Recruitment", exam_name=EXAM_NAME) as session:
        for code, description in SETS:
            project_sets.add_set(session.database, code, description)
        return session.root


class TestExamName:
    def test_it_is_stored_in_the_project_document(self, workspace: Path) -> None:
        with create_project(workspace, "Exam A", exam_name=EXAM_NAME) as session:
            payload = json.loads((session.root / "project.json").read_text(encoding="utf-8"))
        assert payload["exam_name"] == EXAM_NAME

    def test_it_defaults_to_the_project_name_when_not_given(self, workspace: Path) -> None:
        with create_project(workspace, "Exam A") as session:
            assert session.project.metadata.exam_name == "Exam A"

    def test_it_survives_close_and_reopen(self, workspace: Path) -> None:
        with create_project(workspace, "Exam A", exam_name=EXAM_NAME) as session:
            root = session.root
        with open_project(root) as reopened:
            assert reopened.project.metadata.exam_name == EXAM_NAME
            assert reopened.exam_name == EXAM_NAME

    def test_it_can_be_changed_and_the_change_survives_reopen(self, workspace: Path) -> None:
        with create_project(workspace, "Exam A", exam_name="Draft title") as session:
            root = session.root
            update_exam_name(session, "  Final Examination Title  ")
            # Visible to everything already holding the session, without a reopen.
            assert session.exam_name == "Final Examination Title"
        with open_project(root) as reopened:
            assert reopened.project.metadata.exam_name == "Final Examination Title"

    def test_a_blank_name_is_refused_and_nothing_is_written(self, workspace: Path) -> None:
        with create_project(workspace, "Exam A", exam_name=EXAM_NAME) as session:
            with pytest.raises(ProjectValidationError, match="blank"):
                update_exam_name(session, "   ")
            assert session.exam_name == EXAM_NAME
            payload = json.loads((session.root / "project.json").read_text(encoding="utf-8"))
        assert payload["exam_name"] == EXAM_NAME

    def test_creating_with_a_blank_exam_name_is_refused(self, workspace: Path) -> None:
        with pytest.raises(ProjectValidationError, match="blank"):
            create_project(workspace, "Exam A", exam_name="   ")

    def test_it_is_mirrored_into_the_database(self, workspace: Path) -> None:
        """A stray database.sqlite should still say which examination it is."""
        from sqlalchemy import select

        from omr_scanner.database.models import ProjectSetting, SettingKey

        with (
            create_project(workspace, "Exam A", exam_name=EXAM_NAME) as session,
            session.database.session() as db,
        ):
            stored = db.execute(
                select(ProjectSetting.value).where(ProjectSetting.key == SettingKey.EXAM_NAME)
            ).scalar_one()
        assert stored == EXAM_NAME

    def test_an_exam_name_may_contain_characters_a_folder_name_cannot(
        self, workspace: Path
    ) -> None:
        title = "Exam: Round 2 (Written/Viva)"
        with create_project(workspace, "Round Two", exam_name=title) as session:
            root = session.root
        with open_project(root) as reopened:
            assert reopened.exam_name == title
            # The folder is still named by the project name, untouched.
            assert reopened.root.name == "Round Two"


class TestSetPersistence:
    def test_a_single_set_saves_and_reloads(self, workspace: Path) -> None:
        with create_project(workspace, "One Set", exam_name=EXAM_NAME) as session:
            root = session.root
            project_sets.add_set(session.database, "10", "Name of Post: AE (Electrical)")
        with open_project(root) as reopened:
            stored = project_sets.list_sets(reopened.database)
        assert len(stored) == 1
        assert stored[0].code == "10"
        assert stored[0].description == "Name of Post: AE (Electrical)"

    def test_multiple_sets_save_and_reload_in_order(self, project_root: Path) -> None:
        with open_project(project_root) as session:
            stored = project_sets.list_sets(session.database)
        assert tuple((item.code, item.description) for item in stored) == SETS

    def test_each_code_keeps_its_own_description(self, project_root: Path) -> None:
        with open_project(project_root) as session:
            by_code = {item.code: item.description for item in project_sets.list_sets(
                session.database
            )}
        assert by_code["11"] == "Name of Post: Assistant Engineer (Civil)"
        assert by_code["12"] == "Name of Post: Assistant Engineer (Mechanical)"

    def test_stable_identifiers_survive_a_reopen(self, workspace: Path) -> None:
        with create_project(workspace, "Ids", exam_name=EXAM_NAME) as session:
            root = session.root
            created = [
                project_sets.add_set(session.database, code, description)
                for code, description in SETS
            ]
            before = [item.set_id for item in created]
        with open_project(root) as reopened:
            after = [item.set_id for item in project_sets.list_sets(reopened.database)]
        assert after == before

    def test_an_identifier_is_not_the_row_position(self, project_root: Path) -> None:
        """The brief forbids the visible position being the identity."""
        with open_project(project_root) as session:
            stored = project_sets.list_sets(session.database)
        identifiers = [item.set_id for item in stored]
        assert identifiers != ["0", "1", "2"]
        assert all(len(value) == 32 for value in identifiers)
        assert len(set(identifiers)) == len(identifiers)

    def test_a_set_can_be_looked_up_by_its_code(self, project_root: Path) -> None:
        with open_project(project_root) as session:
            found = project_sets.set_by_code(session.database, "11")
            assert found is not None
            assert found.description == "Name of Post: Assistant Engineer (Civil)"
            assert project_sets.set_by_code(session.database, "99") is None


class TestSetValidation:
    def test_a_duplicate_code_is_rejected(self, project_session) -> None:
        project_sets.add_set(project_session.database, "10", "First")
        with pytest.raises(ProjectSetError) as excinfo:
            project_sets.add_set(project_session.database, "10", "Second")
        assert "10" in excinfo.value.user_message
        assert "unique" in excinfo.value.user_message.lower()

    def test_a_duplicate_is_still_rejected_after_whitespace_is_trimmed(
        self, project_session
    ) -> None:
        project_sets.add_set(project_session.database, "10", "First")
        with pytest.raises(ProjectSetError):
            project_sets.add_set(project_session.database, "  10  ", "Second")

    def test_a_rejected_duplicate_never_overwrites_the_existing_set(
        self, project_session
    ) -> None:
        original = project_sets.add_set(project_session.database, "10", "First")
        with pytest.raises(ProjectSetError):
            project_sets.add_set(project_session.database, "10", "Second")
        stored = project_sets.list_sets(project_session.database)
        assert len(stored) == 1
        assert stored[0].set_id == original.set_id
        assert stored[0].description == "First"

    def test_an_empty_code_is_rejected(self, project_session) -> None:
        with pytest.raises(ProjectSetError, match="blank"):
            project_sets.add_set(project_session.database, "", "No code")

    def test_a_whitespace_only_code_is_rejected(self, project_session) -> None:
        with pytest.raises(ProjectSetError, match="blank"):
            project_sets.add_set(project_session.database, "   ", "No code")

    def test_editing_one_set_to_another_existing_code_is_rejected(
        self, project_session
    ) -> None:
        first = project_sets.add_set(project_session.database, "10", "First")
        project_sets.add_set(project_session.database, "11", "Second")
        with pytest.raises(ProjectSetError) as excinfo:
            project_sets.update_set(project_session.database, first.set_id, code="11")
        # The operator-facing half of the error is the one that has to read
        # well; `str(exc)` stays terse for the log.
        assert "already exists" in excinfo.value.user_message

    def test_a_set_may_keep_its_own_code_while_being_edited(self, project_session) -> None:
        created = project_sets.add_set(project_session.database, "10", "First")
        updated = project_sets.update_set(
            project_session.database, created.set_id, code="10", description="Changed"
        )
        assert updated.code == "10"
        assert updated.description == "Changed"


class TestEditingPersists:
    def test_adding_a_set_persists(self, workspace: Path) -> None:
        with create_project(workspace, "Add", exam_name=EXAM_NAME) as session:
            root = session.root
            project_sets.add_set(session.database, "10", "Electrical")
        with open_project(root) as reopened:
            project_sets.add_set(reopened.database, "11", "Civil")
        with open_project(root) as again:
            assert [item.code for item in project_sets.list_sets(again.database)] == ["10", "11"]

    def test_editing_a_description_persists(self, project_root: Path) -> None:
        with open_project(project_root) as session:
            target = project_sets.set_by_code(session.database, "11")
            assert target is not None
            project_sets.update_set(
                session.database, target.set_id, description="Name of Post: Deputy Manager"
            )
            unchanged_id = target.set_id
        with open_project(project_root) as reopened:
            after = project_sets.set_by_code(reopened.database, "11")
            assert after is not None
            assert after.description == "Name of Post: Deputy Manager"
            # The identity is what downstream data will link to; editing the
            # description must never reissue it.
            assert after.set_id == unchanged_id

    def test_editing_a_code_persists_and_keeps_the_identifier(
        self, project_root: Path
    ) -> None:
        with open_project(project_root) as session:
            target = project_sets.set_by_code(session.database, "12")
            assert target is not None
            project_sets.update_set(session.database, target.set_id, code="12A")
            original_id = target.set_id
        with open_project(project_root) as reopened:
            renamed = project_sets.set_by_code(reopened.database, "12A")
            assert renamed is not None
            assert renamed.set_id == original_id
            assert project_sets.set_by_code(reopened.database, "12") is None

    def test_deleting_a_set_persists(self, project_root: Path) -> None:
        with open_project(project_root) as session:
            target = project_sets.set_by_code(session.database, "11")
            assert target is not None
            project_sets.delete_set(session.database, target.set_id)
        with open_project(project_root) as reopened:
            assert [item.code for item in project_sets.list_sets(reopened.database)] == [
                "10",
                "12",
            ]

    def test_deleting_an_unknown_set_is_refused(self, project_session) -> None:
        with pytest.raises(ProjectSetError, match="No set with id"):
            project_sets.delete_set(project_session.database, "does-not-exist")

    def test_nothing_blocks_deletion_yet_but_the_check_is_wired_in(
        self, project_session
    ) -> None:
        """Part 1 has no downstream references - and asks the check to exist anyway."""
        created = project_sets.add_set(project_session.database, "10", "Electrical")
        assert project_sets.references_to_set(project_session.database, created.set_id) == ()


class TestOrdering:
    def test_new_sets_are_appended_in_creation_order(self, project_root: Path) -> None:
        with open_project(project_root) as session:
            orders = [item.display_order for item in project_sets.list_sets(session.database)]
        assert orders == [0, 1, 2]

    def test_moving_a_set_up_persists(self, project_root: Path) -> None:
        with open_project(project_root) as session:
            target = project_sets.set_by_code(session.database, "12")
            assert target is not None
            project_sets.move_set(session.database, target.set_id, -1)
        with open_project(project_root) as reopened:
            assert [item.code for item in project_sets.list_sets(reopened.database)] == [
                "10",
                "12",
                "11",
            ]

    def test_moving_the_first_set_up_does_nothing(self, project_root: Path) -> None:
        with open_project(project_root) as session:
            target = project_sets.set_by_code(session.database, "10")
            assert target is not None
            project_sets.move_set(session.database, target.set_id, -1)
            assert [item.code for item in project_sets.list_sets(session.database)] == [
                "10",
                "11",
                "12",
            ]

    def test_reordering_requires_every_current_identifier(self, project_session) -> None:
        created = project_sets.add_set(project_session.database, "10", "Electrical")
        project_sets.add_set(project_session.database, "11", "Civil")
        with pytest.raises(ProjectSetError, match="every current set id"):
            project_sets.reorder_sets(project_session.database, [created.set_id])


class TestManySets:
    def test_fifty_sets_round_trip(self, workspace: Path) -> None:
        """The brief's own upper example: "50+" sets, with nothing hard-coded."""
        with create_project(workspace, "Many", exam_name=EXAM_NAME) as session:
            root = session.root
            for index in range(60):
                project_sets.add_set(session.database, f"S{index:02d}", f"Post number {index}")
        with open_project(root) as reopened:
            stored = project_sets.list_sets(reopened.database)
        assert len(stored) == 60
        assert stored[0].code == "S00"
        assert stored[-1].code == "S59"
        assert len({item.set_id for item in stored}) == 60


class TestReplaceAllSets:
    def test_it_defines_a_whole_list_at_once(self, project_session) -> None:
        stored = project_sets.replace_all_sets(project_session.database, SETS)
        assert tuple((item.code, item.description) for item in stored) == SETS

    def test_it_rejects_a_duplicate_within_the_list(self, project_session) -> None:
        with pytest.raises(ProjectSetError) as excinfo:
            project_sets.replace_all_sets(
                project_session.database, [("10", "First"), ("10", "Second")]
            )
        assert "more than once" in excinfo.value.user_message
        assert project_sets.list_sets(project_session.database) == ()

    def test_it_refuses_to_run_against_a_project_that_already_has_sets(
        self, project_session
    ) -> None:
        project_sets.add_set(project_session.database, "10", "Electrical")
        with pytest.raises(ProjectSetError, match="already defines sets"):
            project_sets.replace_all_sets(project_session.database, SETS)


class TestLegacyProjects:
    """A project created before this feature existed must still open."""

    def _wind_back_to_schema_7(self, root: Path) -> None:
        """Make an existing project look like one created by the previous build.

        Drops the set table and the migration-8 ledger row, which is what a
        project database written by the previous version genuinely looks
        like. Done with raw `sqlite3` so that nothing in the application
        under test participates in constructing the fixture.
        """
        connection = sqlite3.connect(root / "database.sqlite")
        try:
            connection.execute("DROP TABLE IF EXISTS project_set")
            connection.execute("DELETE FROM schema_migration WHERE version >= 8")
            connection.commit()
        finally:
            connection.close()

    def _strip_exam_name(self, root: Path) -> None:
        """Rewrite project.json as a format-version-1 document."""
        path = root / "project.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("exam_name", None)
        payload["project_format_version"] = 1
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @pytest.fixture
    def legacy_root(self, workspace: Path) -> Path:
        with create_project(workspace, "Legacy Exam") as session:
            root = session.root
        self._wind_back_to_schema_7(root)
        self._strip_exam_name(root)
        return root

    def test_a_legacy_project_still_opens(self, legacy_root: Path) -> None:
        with open_project(legacy_root) as session:
            assert session.name == "Legacy Exam"

    def test_opening_it_upgrades_the_database(self, legacy_root: Path) -> None:
        with open_project(legacy_root) as session:
            assert session.database.schema_version == SCHEMA_VERSION

    def test_a_legacy_project_starts_with_no_sets(self, legacy_root: Path) -> None:
        """Deliberately empty: nothing in the old data says what the sets were."""
        with open_project(legacy_root) as session:
            assert project_sets.list_sets(session.database) == ()

    def test_its_exam_name_falls_back_to_the_project_name(self, legacy_root: Path) -> None:
        with open_project(legacy_root) as session:
            assert session.project.metadata.exam_name == ""
            assert session.exam_name == "Legacy Exam"

    def test_its_stored_format_version_is_left_at_one_until_something_changes(
        self, legacy_root: Path
    ) -> None:
        metadata = read_project_metadata(legacy_root)
        assert metadata.project_format_version == 1

    def test_setting_an_exam_name_upgrades_the_document(self, legacy_root: Path) -> None:
        with open_project(legacy_root) as session:
            update_exam_name(session, EXAM_NAME)
        metadata = read_project_metadata(legacy_root)
        assert metadata.exam_name == EXAM_NAME
        assert metadata.project_format_version == PROJECT_FORMAT_VERSION

    def test_sets_can_be_defined_on_an_upgraded_legacy_project(
        self, legacy_root: Path
    ) -> None:
        with open_project(legacy_root) as session:
            project_sets.add_set(session.database, "10", "Electrical")
        with open_project(legacy_root) as reopened:
            assert [item.code for item in project_sets.list_sets(reopened.database)] == ["10"]


class TestSuggestionsFromExistingData:
    def test_a_project_with_no_history_suggests_nothing(self, project_session) -> None:
        assert project_sets.suggest_sets_from_existing_data(project_session.database) == ()

    def test_codes_already_named_by_an_answer_key_are_offered(self, project_session) -> None:
        """Offered, never applied - the operator decides what the sets are."""
        from datetime import UTC, datetime

        from sqlalchemy import insert

        from omr_scanner.database.models import AnswerKeyRevision

        with project_session.database.session() as db:
            db.execute(
                insert(AnswerKeyRevision).values(
                    set_code="10",
                    revision=1,
                    answers="ABCD",
                    question_count=4,
                    first_question=1,
                    status="verified",
                    source="manual",
                    created_at=datetime.now(UTC),
                )
            )
        assert project_sets.suggest_sets_from_existing_data(project_session.database) == ("10",)

    def test_a_code_already_defined_is_not_offered_again(self, project_session) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import insert

        from omr_scanner.database.models import AnswerKeyRevision

        with project_session.database.session() as db:
            db.execute(
                insert(AnswerKeyRevision).values(
                    set_code="10",
                    revision=1,
                    answers="ABCD",
                    question_count=4,
                    first_question=1,
                    status="verified",
                    source="manual",
                    created_at=datetime.now(UTC),
                )
            )
        project_sets.add_set(project_session.database, "10", "Already defined")
        assert project_sets.suggest_sets_from_existing_data(project_session.database) == ()
