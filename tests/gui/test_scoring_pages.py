"""The Answer Key and Results stages (Phase 8).

Scope:
    The real pages and the real policy dialog with a real project and a real
    database. The dialogs that own a native file chooser are never exercised;
    the methods behind them are.

Assertions are on *state*, not on pixels: a screenshot that looks right proves
nothing about which key revision produced a mark.

Privacy:
    Every identifier and name here is fictional.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from omr_scanner.config import AppConfig
from omr_scanner.domain.reconciliation import AttendanceState, CandidateRecord
from omr_scanner.domain.scoring import (
    AnswerKeyStatus,
    NegativeMarking,
    ResultStatus,
    ScoringPolicy,
)
from omr_scanner.gui.answer_key.page import AnswerKeyPage
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.results.page import ResultsPage
from omr_scanner.gui.results.policy_dialog import ScoringPolicyDialog
from omr_scanner.services import batch_store, reconciliation_store, scoring_store
from omr_scanner.services.answer_key import plan_for, read_key
from omr_scanner.services.candidate_import import ColumnMapping, RosterValidation

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from omr_scanner.services import ProjectSession

OPERATOR = "Dr. A. Rahman"
PRESENT = AttendanceState.PRESENT
ABSENT = AttendanceState.ABSENT


def _ignore(*_args: object, **_kwargs: object) -> None:
    """A QMessageBox stand-in that does nothing."""


def _answer_yes(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
    """A QMessageBox.question stand-in that confirms."""
    return QMessageBox.StandardButton.Yes


def _capture(sink: list[str]) -> Callable[..., None]:
    """A QMessageBox stand-in that records the message it was given."""

    def recorded(*args: object, **_kwargs: object) -> None:
        sink.append(str(args[2]) if len(args) > 2 else "")

    return recorded


@pytest.fixture(scope="module")
def template():
    from tests.conftest import build_answer_sheet_template

    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def plan(template):
    return plan_for(template)


def validation_of(candidates: list[tuple[str, str, AttendanceState]]) -> RosterValidation:
    records = tuple(
        CandidateRecord(
            candidate_id=candidate_id,
            display_name=name,
            source_row=index + 2,
            imported_attendance=state,
            imported_value="ABSENT" if state is ABSENT else "55",
        )
        for index, (candidate_id, name, state) in enumerate(candidates)
    )
    return RosterValidation(
        candidates=records,
        issues=(),
        rows_read=len(records),
        blank_ids=0,
        duplicate_ids=0,
        expected_present=sum(1 for i in records if i.imported_attendance is PRESENT),
        expected_absent=sum(1 for i in records if i.imported_attendance is ABSENT),
        attendance_unknown=0,
        mapping=ColumnMapping(candidate_id=0, name=1, attendance=2),
        source_name="roster.csv",
    )


@pytest.fixture
def prepared(project_session: ProjectSession, plan, tmp_path: Path):
    """A reconciled batch in an open project, ready to be keyed and scored."""
    from omr_scanner.database.models import BatchScan, ScanBatch
    from omr_scanner.services.recognition_models import (
        AnswerView,
        FieldView,
        RecognitionOutcome,
        RegistrationStatus,
        ScanResult,
    )

    database = project_session.database
    roster_id = reconciliation_store.import_roster(
        database,
        validation_of(
            [("10001", "CAND A", PRESENT), ("10002", "CAND B", PRESENT),
             ("10003", "CAND C", ABSENT)]
        ),
        imported_by=OPERATOR,
    )
    rows = [("s1.png", "10001", "A", {}), ("s2.png", "10002", "A", {1: "B"})]
    now = datetime.now(UTC)
    batch_id = batch_store.new_batch_id()
    with database.session() as session:
        session.add(
            ScanBatch(
                batch_id=batch_id, created_at=now, updated_at=now,
                source_folder=str(tmp_path), status="completed", total_scans=len(rows),
            )
        )
        session.flush()
        for index, (name, roll, set_code, answers) in enumerate(rows):
            result = ScanResult(
                source_path=tmp_path / name,
                outcome=RecognitionOutcome.COMPLETE,
                registration=RegistrationStatus.REGISTERED,
                fields=(
                    FieldView(
                        zone_id="set_code", label="Set", field_type="set_code",
                        value=set_code, status="complete", needs_review=False,
                        characters=(),
                    ),
                ),
                answers=tuple(
                    AnswerView(
                        number=number, zone_id="q", value=answers.get(number, "A"),
                        status="resolved", needs_review=False,
                        top_fill=0.9, margin=0.4, confidence=0.9,
                    )
                    for number in plan.numbers
                ),
                set_code_zone_id="set_code",
            )
            session.add(
                BatchScan(
                    batch_id=batch_id, batch_index=index,
                    source_path=str(tmp_path / name), filename=name,
                    status="completed", identifier_value=roll, set_code_value=set_code,
                    result_json=json.dumps(result.to_dict()),
                )
            )
    reconciliation_store.reconcile_batch(database, roster_id, batch_id)
    return roster_id, batch_id


@pytest.fixture
def key_page(qtbot, project_session: ProjectSession, template):
    """An Answer Key page on an open project with a template loaded."""
    spec = next(item for item in WORKFLOW_PAGES if item.key == "answer_key")
    page = AnswerKeyPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    page.set_reviewer(OPERATOR)
    page.set_template(template)
    yield page
    page.close()


@pytest.fixture
def results_page(qtbot, project_session: ProjectSession, template, prepared):
    """A Results page on a reconciled batch."""
    _, batch_id = prepared
    spec = next(item for item in WORKFLOW_PAGES if item.key == "results")
    page = ResultsPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    page.set_reviewer(OPERATOR)
    page.set_template(template)
    page.set_batch(batch_id)
    yield page
    page.close()


def verified_key_for(database, plan, set_code: str = "A", *, wrong=()) -> int:
    """Store and verify a key of all-A answers."""
    stored = scoring_store.save_key(
        database,
        read_key("A" * plan.question_count, plan, set_code, wrong_questions=list(wrong)).to_key(),
    )
    scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)
    return stored.key_id


# ----------------------------------------------------------------------
class TestAnswerKeyEntry:
    def test_a_valid_key_can_be_saved(self, key_page: AnswerKeyPage, plan):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        assert key_page.state.draft.is_valid is True
        assert key_page.save_button.isEnabled() is True
        assert key_page.save_key() is True

    def test_a_short_key_is_refused_with_a_useful_message(
        self, key_page: AnswerKeyPage, plan
    ):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * (plan.question_count - 2))
        assert key_page.state.draft.is_valid is False
        assert key_page.save_button.isEnabled() is False
        text = key_page.validation_label.text()
        assert "cannot be saved" in text
        assert str(plan.question_count) in text

    def test_the_table_and_the_text_show_the_same_key(
        self, key_page: AnswerKeyPage, plan
    ):
        key_page.set_combo.setCurrentText("A")
        answers = ("ABCD" * plan.question_count)[: plan.question_count]
        key_page.key_edit.setPlainText(answers)
        assert key_page.table.rowCount() == plan.question_count
        for row in range(plan.question_count):
            assert key_page.table.item(row, 1).text() == answers[row]

    def test_a_pasted_key_with_line_breaks_reads(self, key_page: AnswerKeyPage, plan):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A\n" * plan.question_count)
        assert key_page.state.draft.is_valid is True

    def test_lowercase_is_normalised(self, key_page: AnswerKeyPage, plan):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("a" * plan.question_count)
        assert key_page.state.draft.answers == "A" * plan.question_count

    def test_a_multi_character_set_code_is_accepted(
        self, key_page: AnswerKeyPage, plan
    ):
        key_page.set_combo.setCurrentText("10")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        assert key_page.save_key() is True
        assert key_page.state.set_code == "10"

    def test_wrong_questions_are_flagged_in_the_table(
        self, key_page: AnswerKeyPage, plan
    ):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.wrong_edit.setText("2, 5")
        assert key_page.state.draft.wrong_questions == {2, 5}
        assert "full credit" in key_page.table.item(1, 2).text()
        assert key_page.table.item(0, 2).text() == ""

    def test_unflagging_a_wrong_question_clears_the_table(
        self, key_page: AnswerKeyPage, plan
    ):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.wrong_edit.setText("2")
        assert "full credit" in key_page.table.item(1, 2).text()
        key_page.wrong_edit.setText("")
        assert key_page.table.item(1, 2).text() == ""

    def test_an_out_of_range_wrong_question_is_reported(
        self, key_page: AnswerKeyPage, plan
    ):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.wrong_edit.setText("999")
        assert "Questions run" in key_page.validation_label.text()

    def test_the_canonical_string_is_shown(self, key_page: AnswerKeyPage, plan):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        assert "A" * plan.question_count in key_page.summary_label.text()

    def test_without_a_template_nothing_can_be_entered(self, key_page: AnswerKeyPage):
        key_page.set_template(None)
        assert key_page.key_edit.isEnabled() is False
        assert key_page.save_button.isEnabled() is False
        assert "No template is loaded" in key_page.validation_label.text()


class TestAnswerKeyVerification:
    def test_verifying_locks_the_revision(
        self, key_page: AnswerKeyPage, plan, monkeypatch, project_session
    ):
        monkeypatch.setattr(QMessageBox, "question", _answer_yes)
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        assert key_page.verify_key() is True
        stored = scoring_store.verified_key(project_session.database, "A")
        assert stored is not None
        assert stored.key.status is AnswerKeyStatus.VERIFIED
        assert stored.verified_by == OPERATOR

    def test_a_verified_revision_cannot_be_verified_again(
        self, key_page: AnswerKeyPage, plan, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "question", _answer_yes)
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        key_page.verify_key()
        assert key_page.verify_button.isEnabled() is False

    def test_verifying_without_a_name_is_refused(
        self, key_page: AnswerKeyPage, plan, monkeypatch
    ):
        warned: list[str] = []
        monkeypatch.setattr(QMessageBox, "question", _answer_yes)
        monkeypatch.setattr(QMessageBox, "warning", _capture(warned))
        key_page.set_reviewer("")
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        assert key_page.verify_key() is False
        assert warned and "Settings" in warned[0]

    def test_no_reviewer_name_is_said_plainly(self, key_page: AnswerKeyPage):
        key_page.set_reviewer("")
        assert "cannot be verified without a name" in key_page.reviewer_label.text()

    def test_a_second_revision_warns_that_results_go_stale(
        self, key_page: AnswerKeyPage, plan, monkeypatch
    ):
        asked: list[str] = []

        def record(*args: object, **_kwargs: object) -> QMessageBox.StandardButton:
            asked.append(str(args[2]) if len(args) > 2 else "")
            return QMessageBox.StandardButton.Yes

        monkeypatch.setattr(QMessageBox, "question", record)
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        key_page.verify_key()
        key_page.key_edit.setPlainText("B" * plan.question_count)
        key_page.save_key()
        key_page.verify_key()
        assert any("needing recomputation" in item for item in asked)

    def test_revisions_are_listed(self, key_page: AnswerKeyPage, plan, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question", _answer_yes)
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        key_page.key_edit.setPlainText("B" * plan.question_count)
        key_page.save_key()
        labels = [
            key_page.revision_combo.itemText(i)
            for i in range(key_page.revision_combo.count())
        ]
        assert any("revision 1" in item for item in labels)
        assert any("revision 2" in item for item in labels)

    def test_choosing_a_revision_loads_it(self, key_page: AnswerKeyPage, plan):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        key_page.key_edit.setPlainText("B" * plan.question_count)
        key_page.save_key()
        first = key_page.revision_combo.findData(
            next(item.key_id for item in key_page.state.revisions if item.revision == 1)
        )
        key_page.revision_combo.setCurrentIndex(first)
        assert key_page.key_edit.toPlainText() == "A" * plan.question_count


class TestScoringPolicyDialog:
    def test_it_loads_and_returns_an_unchanged_policy(self, qtbot):
        policy = ScoringPolicy(
            correct_mark=Fraction(1),
            incorrect_penalty=Fraction(1, 4),
            mode=NegativeMarking.FIXED,
        )
        dialog = ScoringPolicyDialog(policy)
        qtbot.addWidget(dialog)
        try:
            found = dialog.policy()
            assert found.correct_mark == Fraction(1)
            assert found.incorrect_penalty == Fraction(1, 4)
            assert found.mode is NegativeMarking.FIXED
        finally:
            dialog.close()

    @pytest.mark.parametrize(
        ("name", "mode"),
        [
            ("negativeNoneRadio", NegativeMarking.NONE),
            ("negativeFixedRadio", NegativeMarking.FIXED),
            ("negativeOnePerThreeRadio", NegativeMarking.ONE_PER_THREE),
            ("negativeOnePerFourRadio", NegativeMarking.ONE_PER_FOUR),
        ],
    )
    def test_every_mode_can_be_selected(self, qtbot, name, mode):
        dialog = ScoringPolicyDialog(ScoringPolicy())
        qtbot.addWidget(dialog)
        try:
            dialog.findChild(object, name).setChecked(True)
            assert dialog.policy().mode is mode
        finally:
            dialog.close()

    def test_a_custom_deduction_is_read_exactly(self, qtbot):
        dialog = ScoringPolicyDialog(ScoringPolicy())
        qtbot.addWidget(dialog)
        try:
            dialog.negativeFixedRadio = dialog.findChild(object, "negativeFixedRadio")
            dialog.negativeFixedRadio.setChecked(True)
            dialog.incorrect_spin.setValue(0.33)
            assert dialog.policy().incorrect_penalty == Fraction(33, 100)
        finally:
            dialog.close()

    def test_the_penalty_fields_are_disabled_outside_fixed_mode(self, qtbot):
        dialog = ScoringPolicyDialog(ScoringPolicy())
        qtbot.addWidget(dialog)
        try:
            dialog.findChild(object, "negativeOnePerThreeRadio").setChecked(True)
            assert dialog.incorrect_spin.isEnabled() is False
        finally:
            dialog.close()

    def test_a_separate_multiple_penalty_can_be_set(self, qtbot):
        dialog = ScoringPolicyDialog(ScoringPolicy())
        qtbot.addWidget(dialog)
        try:
            dialog.findChild(object, "negativeFixedRadio").setChecked(True)
            dialog.same_penalty_box.setChecked(False)
            dialog.multiple_spin.setValue(0.5)
            assert dialog.policy().multiple_penalty == Fraction(1, 2)
        finally:
            dialog.close()

    def test_the_clamp_can_be_turned_off(self, qtbot):
        dialog = ScoringPolicyDialog(ScoringPolicy())
        qtbot.addWidget(dialog)
        try:
            dialog.clamp_box.setChecked(False)
            assert dialog.policy().clamp_minimum is False
            assert dialog.minimum_spin.isEnabled() is False
        finally:
            dialog.close()

    def test_the_preview_shows_the_rules_and_a_worked_example(self, qtbot):
        dialog = ScoringPolicyDialog(ScoringPolicy())
        qtbot.addWidget(dialog)
        try:
            text = dialog.preview_label.text()
            assert "Correct answer" in text
            assert "Worked example" in text
            assert "10.00" in text, "10 correct, no negative marking"
        finally:
            dialog.close()

    def test_the_preview_follows_the_mode(self, qtbot):
        dialog = ScoringPolicyDialog(ScoringPolicy())
        qtbot.addWidget(dialog)
        try:
            dialog.findChild(object, "negativeOnePerThreeRadio").setChecked(True)
            assert "9.00" in dialog.preview_label.text(), "10 - 3/3"
        finally:
            dialog.close()


class TestResultsPage:
    def test_scoring_needs_a_verified_key(
        self, qtbot, results_page: ResultsPage, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "information", _ignore)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            assert results_page.score_batch() is True
        found = {item.candidate_id: item for item in results_page.state.results}
        assert all(
            item.status is not ResultStatus.SCORED
            for item in found.values()
            if item.status is not ResultStatus.ABSENT
        )

    def test_a_scored_batch_fills_the_table(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        found = {item.candidate_id: item for item in results_page.state.results}
        assert found["10001"].status is ResultStatus.SCORED
        assert found["10001"].final_score == plan.question_count
        assert found["10003"].status is ResultStatus.ABSENT
        assert results_page.table.rowCount() == len(found)

    def test_the_summary_reports_every_count(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        text = results_page.summary_label.text()
        for label in ("Candidates", "Scored", "Absent", "Cannot be scored", "Need recalculating"):
            assert label in text

    def test_an_absent_candidate_shows_a_dash_not_a_zero(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        row = next(
            index
            for index, item in enumerate(results_page.state.results)
            if item.candidate_id == "10003"
        )
        assert results_page.table.item(row, 4).text() == "-"

    def test_the_key_revision_is_shown(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        row = next(
            index
            for index, item in enumerate(results_page.state.results)
            if item.candidate_id == "10001"
        )
        assert results_page.table.item(row, 9).text() == "1"

    def test_selecting_a_candidate_shows_the_question_breakdown(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        row = next(
            index
            for index, item in enumerate(results_page.state.results)
            if item.candidate_id == "10002"
        )
        results_page.table.selectRow(row)
        assert results_page.detail_table.rowCount() == plan.question_count
        assert results_page.detail_table.item(0, 4).text() == "Incorrect"
        assert results_page.detail_table.item(1, 4).text() == "Correct"
        detail = results_page.detail_label.text()
        assert "Answer key: Set A / revision 1" in detail
        assert "Scoring policy: revision" in detail

    def test_a_blocked_candidate_says_why(
        self, qtbot, results_page: ResultsPage, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "information", _ignore)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        blocked = next(
            item
            for item in results_page.state.results
            if item.status is ResultStatus.BLOCKED
        )
        assert "No verified answer key" in blocked.describe_blocks()

    def test_filtering_narrows_the_table(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        results_page.filter_combo.setCurrentText("Absent")
        assert [item.candidate_id for item in results_page.state.results] == ["10003"]

    def test_searching_by_candidate(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        results_page.search_box.setText("10002")
        assert [item.candidate_id for item in results_page.state.results] == ["10002"]


class TestStaleIndication:
    def test_a_policy_change_marks_results_stale(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        assert results_page.apply_policy(
            ScoringPolicy(
                correct_mark=Fraction(1),
                incorrect_penalty=Fraction(1, 4),
                mode=NegativeMarking.FIXED,
            )
        )
        found = {item.candidate_id: item for item in results_page.state.results}
        assert found["10001"].is_stale
        assert "Need recalculating <b>2</b>" in results_page.summary_label.text()

    def test_the_stale_reason_is_shown_in_the_table(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        results_page.apply_policy(ScoringPolicy(correct_mark=Fraction(2)))
        row = next(
            index
            for index, item in enumerate(results_page.state.results)
            if item.candidate_id == "10001"
        )
        assert "scoring configuration has changed" in results_page.table.item(row, 10).text()

    def test_a_new_key_revision_marks_results_stale(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        database = project_session.database
        verified_key_for(database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        assert not any(item.is_stale for item in results_page.state.results)

        stored = scoring_store.save_key(
            database,
            read_key("B" * plan.question_count, plan, "A").to_key(),
        )
        scoring_store.verify_key(database, stored.key_id, verified_by=OPERATOR)
        results_page.refresh_table()

        found = {item.candidate_id: item for item in results_page.state.results}
        assert found["10001"].is_stale
        assert found["10001"].answer_key_revision == 1, "provenance is not rewritten"
        row = next(
            index
            for index, item in enumerate(results_page.state.results)
            if item.candidate_id == "10001"
        )
        assert "answer key has changed" in results_page.table.item(row, 10).text()

    def test_withdrawing_a_question_marks_results_stale(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        # Wrong questions live on the key revision, so flagging one is a new
        # revision - and every mark computed under the old one is out of date.
        database = project_session.database
        verified_key_for(database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        before = {item.candidate_id: item for item in results_page.state.results}

        verified_key_for(database, plan, wrong=(2,))
        results_page.refresh_table()
        after = {item.candidate_id: item for item in results_page.state.results}
        assert after["10002"].is_stale
        assert after["10002"].final_score == before["10002"].final_score

        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        recomputed = {item.candidate_id: item for item in results_page.state.results}
        assert recomputed["10002"].is_stale is False
        assert recomputed["10002"].wrong_question_count == 1

    def test_a_stale_result_keeps_its_mark_until_recalculated(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        before = next(
            item for item in results_page.state.results if item.candidate_id == "10001"
        ).final_score
        results_page.apply_policy(ScoringPolicy(correct_mark=Fraction(2)))
        after = next(
            item for item in results_page.state.results if item.candidate_id == "10001"
        )
        assert after.final_score == before
        assert after.is_stale

    def test_recalculating_clears_staleness_and_changes_the_mark(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        results_page.apply_policy(ScoringPolicy(correct_mark=Fraction(2)))
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        after = next(
            item for item in results_page.state.results if item.candidate_id == "10001"
        )
        assert after.is_stale is False
        assert after.final_score == 2 * plan.question_count

    def test_one_candidate_can_be_recalculated_alone(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        results_page.apply_policy(ScoringPolicy(correct_mark=Fraction(2)))
        row = next(
            index
            for index, item in enumerate(results_page.state.results)
            if item.candidate_id == "10001"
        )
        results_page.table.selectRow(row)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            assert results_page.rescore_selected() is True
        found = {item.candidate_id: item for item in results_page.state.results}
        assert found["10001"].is_stale is False
        assert found["10002"].is_stale is True, "only the chosen one was recalculated"


class TestPreflight:
    def test_it_reports_a_missing_key(self, results_page: ResultsPage):
        issues = results_page.preflight()
        assert any("no verified answer key" in item for item in issues)

    def test_it_is_silent_when_everything_is_ready(
        self, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        assert results_page.preflight() == ()

    def test_the_dialog_shows_the_rules(
        self, results_page: ResultsPage, project_session, plan, monkeypatch
    ):
        shown: list[str] = []
        verified_key_for(project_session.database, plan)
        monkeypatch.setattr(QMessageBox, "information", _capture(shown))
        results_page.show_preflight()
        assert shown and "Correct answer" in shown[0]

    def test_every_blocking_issue_is_collected_not_just_the_first(
        self, results_page: ResultsPage, project_session, plan
    ):
        # An operator who discovers blocking issues one dialog at a time fixes
        # them one at a time, and the batch fails as many times as there are
        # problems. Set B has no key; 10002's answers are still in review.
        from omr_scanner.database.models import BatchScan

        database = project_session.database
        verified_key_for(database, plan)
        with database.session() as session:
            row = session.scalars(select(BatchScan)).first()
            row.set_code_value = "B"
        results_page.refresh_table()

        issues = results_page.preflight()
        assert len(issues) >= 2
        assert any("Set B has no verified answer key" in item for item in issues)
        assert any("10001" in item for item in issues)

    def test_the_policy_bar_lists_verified_keys(
        self, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        results_page.on_project_changed(project_session)
        assert "A rev 1" in results_page.policy_label.text()

    def test_the_policy_bar_notices_a_key_verified_elsewhere(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        # A key is verified on the Answer Key stage, so the Results page must
        # not go on saying "none" until the project is reopened.
        assert "Verified answer keys: none" in results_page.policy_label.text()
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        assert "A rev 1" in results_page.policy_label.text()


class TestResponsiveness:
    def test_the_event_loop_keeps_running_while_scoring(
        self, qtbot, results_page: ResultsPage, project_session, plan
    ):
        verified_key_for(project_session.database, plan)
        ticks: list[int] = []
        heartbeat = QTimer()
        heartbeat.setInterval(5)
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start()
        try:
            with qtbot.waitSignal(results_page.scored, timeout=15_000):
                results_page.score_batch()
        finally:
            heartbeat.stop()
        assert ticks, "the GUI thread was blocked while scoring ran"


class TestPersistenceAcrossPages:
    def test_results_are_still_there_on_a_fresh_page(
        self, qtbot, results_page: ResultsPage, project_session, template, plan, prepared
    ):
        _, batch_id = prepared
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()

        spec = next(item for item in WORKFLOW_PAGES if item.key == "results")
        fresh = ResultsPage(spec)
        qtbot.addWidget(fresh)
        try:
            fresh.on_project_changed(project_session)
            fresh.set_template(template)
            fresh.set_batch(batch_id)
            found = {item.candidate_id: item for item in fresh.state.results}
            assert found["10001"].final_score == plan.question_count
            assert found["10001"].answer_key_revision == 1
        finally:
            fresh.close()


class TestWindowIntegration:
    def test_both_stages_are_no_longer_placeholders(self, qtbot, tmp_path):
        window = MainWindow(AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        try:
            assert isinstance(window._answer_key_page(), AnswerKeyPage)
            assert isinstance(window._results_page(), ResultsPage)
            assert window.show_page("answer_key") is True
            assert window.show_page("results") is True
        finally:
            window.close()

    def test_the_reviewer_name_reaches_both(self, qtbot, tmp_path):
        config = AppConfig().with_reviewer_name("Dr. Configured")
        window = MainWindow(config, config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        try:
            assert window._answer_key_page().state.reviewer == "Dr. Configured"
            assert window._results_page().state.reviewer == "Dr. Configured"
        finally:
            window.close()

    def test_the_template_reaches_both(self, qtbot, tmp_path, template):
        window = MainWindow(AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        try:
            window.broadcast_template(template)
            assert window._answer_key_page().state.plan is not None
            assert window._results_page().state.template is template
        finally:
            window.close()


class TestStableObjectNames:
    def test_the_answer_key_widgets_can_be_found(self, key_page: AnswerKeyPage):
        for name in (
            "answerKeySetCombo",
            "answerKeyRevisionCombo",
            "readKeyFromScanButton",
            "answerKeyTextEdit",
            "wrongQuestionEdit",
            "answerKeyValidationLabel",
            "saveAnswerKeyButton",
            "verifyAnswerKeyButton",
            "answerKeyTable",
            "answerKeySummaryLabel",
            "answerKeyReviewerLabel",
        ):
            assert key_page.findChild(object, name) is not None, name

    def test_the_results_widgets_can_be_found(self, results_page: ResultsPage):
        for name in (
            "scoringPolicyLabel",
            "configureScoringButton",
            "checkBeforeScoringButton",
            "calculateResultsButton",
            "resultsSummaryLabel",
            "scoringProgressBar",
            "resultsFilterCombo",
            "resultsSearchBox",
            "resultsTable",
            "resultsCountLabel",
            "resultsDetailLabel",
            "resultDetailTable",
            "reviewAnswersButton",
            "recalculateCandidateButton",
        ):
            assert results_page.findChild(object, name) is not None, name

    def test_the_policy_dialog_widgets_can_be_found(self, qtbot):
        dialog = ScoringPolicyDialog(ScoringPolicy())
        qtbot.addWidget(dialog)
        try:
            for name in (
                "correctMarkSpin",
                "blankMarkSpin",
                "negativeNoneRadio",
                "negativeFixedRadio",
                "negativeOnePerThreeRadio",
                "negativeOnePerFourRadio",
                "incorrectPenaltySpin",
                "multipleSamePenaltyCheck",
                "multiplePenaltySpin",
                "clampMinimumCheck",
                "minimumScoreSpin",
                "scoringPreviewLabel",
            ):
                assert dialog.findChild(object, name) is not None, name
        finally:
            dialog.close()

# ----------------------------------------------------------------------
# Repairs and coverage added by the Phase 8 audit.
# ----------------------------------------------------------------------
class TestVerifyingFollowsWhatWasSaved:
    """Verification locks a stored revision, never the text on screen."""

    def test_verify_is_refused_while_the_editor_holds_unsaved_edits(
        self, key_page: AnswerKeyPage, plan, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "question", _answer_yes)
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        assert key_page.verify_button.isEnabled() is True

        # One character changed, not saved. The stored revision still says "A".
        key_page.key_edit.setPlainText("B" + "A" * (plan.question_count - 1))
        assert key_page.verify_button.isEnabled() is False
        assert "Save these edits" in key_page.verify_button.toolTip()

    def test_saving_the_edit_makes_verification_available_again(
        self, key_page: AnswerKeyPage, plan, monkeypatch
    ):
        monkeypatch.setattr(QMessageBox, "question", _answer_yes)
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        key_page.key_edit.setPlainText("B" + "A" * (plan.question_count - 1))
        assert key_page.save_key() is True
        assert key_page.verify_button.isEnabled() is True
        assert key_page.verify_key() is True
        stored = key_page.current_revision()
        assert stored.revision == 2
        assert stored.key.answers.startswith("B")

    def test_a_wrong_question_edit_also_counts_as_unsaved(
        self, key_page: AnswerKeyPage, plan
    ):
        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        key_page.wrong_edit.setText("3")
        assert key_page.verify_button.isEnabled() is False


class TestPolicyDialogExpressesEveryRule:
    """What the dialog can express, the scorer must apply - and vice versa."""

    def test_a_separate_multiple_deduction_is_offered_under_one_per_three(
        self, qtbot
    ):
        dialog = ScoringPolicyDialog(
            ScoringPolicy(correct_mark=Fraction(1), mode=NegativeMarking.ONE_PER_THREE)
        )
        qtbot.addWidget(dialog)
        dialog.same_penalty_box.setChecked(False)
        assert dialog.same_penalty_box.isEnabled() is True
        assert dialog.multiple_spin.isEnabled() is True
        # The per-incorrect amount still belongs to the fixed mode alone.
        assert dialog.incorrect_spin.isEnabled() is False

    def test_nothing_is_deducted_under_no_negative_marking(self, qtbot):
        dialog = ScoringPolicyDialog(ScoringPolicy(correct_mark=Fraction(1)))
        qtbot.addWidget(dialog)
        assert dialog.same_penalty_box.isEnabled() is False
        assert dialog.multiple_spin.isEnabled() is False
        assert dialog.policy().effective_multiple_penalty == 0

    def test_a_multiple_deduction_set_here_reaches_the_scorer(self, qtbot):
        dialog = ScoringPolicyDialog(
            ScoringPolicy(correct_mark=Fraction(1), mode=NegativeMarking.ONE_PER_THREE)
        )
        qtbot.addWidget(dialog)
        dialog.same_penalty_box.setChecked(False)
        dialog.multiple_spin.setValue(0.5)
        policy = dialog.policy()
        assert policy.multiple_penalty == Fraction(1, 2)
        assert policy.effective_multiple_penalty == Fraction(1, 2)
        assert policy.effective_incorrect_penalty == Fraction(1, 3)

    def test_opening_and_saving_never_rewrites_an_exact_rule(self, qtbot):
        # A third cannot be shown in four decimal places. Reading the field
        # back as 0.3333 would create a revision nobody asked for and make
        # every result in the project stale under a rule that was never typed.
        original = ScoringPolicy(
            correct_mark=Fraction(1),
            blank_mark=Fraction(1, 3),
            mode=NegativeMarking.ONE_PER_THREE,
        )
        dialog = ScoringPolicyDialog(original)
        qtbot.addWidget(dialog)
        assert dialog.policy().blank_mark == Fraction(1, 3)

    def test_an_edited_field_is_read_exactly_from_its_text(self, qtbot):
        dialog = ScoringPolicyDialog(
            ScoringPolicy(correct_mark=Fraction(1), blank_mark=Fraction(1, 3))
        )
        qtbot.addWidget(dialog)
        dialog.blank_spin.setValue(0.25)
        assert dialog.policy().blank_mark == Fraction(1, 4)


class TestTheSummaryComesFromOneRead:
    """The table and the line above it describe the same read of the batch."""

    def test_the_counts_match_the_rows_that_were_listed(
        self, results_page: ResultsPage, project_session: ProjectSession, plan, qtbot
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        counts = results_page.state.counts
        assert counts is not None
        assert counts.registered == len(results_page.state.results)
        assert counts.scored + counts.absent + counts.blocked == counts.registered

    def test_a_filtered_view_does_not_shrink_the_batch_summary(
        self, results_page: ResultsPage, project_session: ProjectSession, plan, qtbot
    ):
        verified_key_for(project_session.database, plan)
        with qtbot.waitSignal(results_page.scored, timeout=15_000):
            results_page.score_batch()
        everything = results_page.state.counts.registered
        assert everything > 0
        results_page.filter_combo.setCurrentIndex(3)  # Absent only
        assert results_page.state.counts.registered == everything
        assert len(results_page.state.results) < everything


class TestCrossStageWiring:
    """A key written on one stage has to reach the stage that uses it."""

    def test_verifying_a_key_refreshes_the_results_stage(
        self, qtbot, project_session: ProjectSession, template, prepared, plan
    ):
        window = MainWindow(AppConfig(reviewer_name=OPERATOR))
        qtbot.addWidget(window)
        window._adopt_session(project_session)
        window.broadcast_template(template)
        _, batch_id = prepared
        results = window._results_page()
        key_page = window._answer_key_page()
        assert results is not None and key_page is not None
        results.set_batch(batch_id)
        assert "none" in results.policy_label.text()

        key_page.set_combo.setCurrentText("A")
        key_page.key_edit.setPlainText("A" * plan.question_count)
        key_page.save_key()
        stored = key_page.current_revision()
        scoring_store.verify_key(
            project_session.database, stored.key_id, verified_by=OPERATOR
        )
        key_page.key_verified.emit(stored.key_id)

        assert "A rev 1" in results.policy_label.text()
        window.close()

    def test_a_batch_read_during_the_session_reaches_the_results_stage(
        self, qtbot, project_session: ProjectSession, template, prepared
    ):
        _, batch_id = prepared
        window = MainWindow(AppConfig(reviewer_name=OPERATOR))
        qtbot.addWidget(window)
        window._adopt_session(project_session)
        window.broadcast_template(template)
        results = window._results_page()
        scan_page = window._scan_page()
        assert results is not None and scan_page is not None

        results.set_batch("")
        results.state.batch_id = None
        scan_page.state.batch_id = batch_id
        window._on_batch_finished(object())

        assert results.state.batch_id == batch_id
        window.close()

    def test_the_sets_a_batch_contains_are_offered_for_keying(
        self, qtbot, project_session: ProjectSession, template, prepared
    ):
        _, batch_id = prepared
        window = MainWindow(AppConfig(reviewer_name=OPERATOR))
        qtbot.addWidget(window)
        window._adopt_session(project_session)
        window.broadcast_template(template)
        scan_page = window._scan_page()
        key_page = window._answer_key_page()
        assert scan_page is not None and key_page is not None
        scan_page.state.batch_id = batch_id
        window._on_batch_finished(object())

        offered = {
            key_page.set_combo.itemText(i) for i in range(key_page.set_combo.count())
        }
        assert "A" in offered
        window.close()
