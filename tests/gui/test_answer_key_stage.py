"""The redesigned Answer Key stage (Step 7), driven through its real widgets.

Scope:
    The page inside a real :class:`MainWindow` on a real project, and the
    solution-sheet review dialog on real rendered sheets. Modal dialogs are
    never ``exec()``-ed: the unsaved-changes prompt and the review dialog are
    replaced at their one seam (``ask_unsaved`` / ``_run_solution_dialog``),
    and the dialog itself is constructed and driven directly.

What is asserted is state - which set is shown, what the table holds, what
was stored - not pixels.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

import cv2
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QMessageBox
from tests.conftest import build_answer_sheet_template

from omr_scanner.config import AppConfig
from omr_scanner.domain.scoring import AnswerKeySource, AnswerKeyStatus
from omr_scanner.evaluation.answer_keys import generate_answer_keys, solution_case
from omr_scanner.evaluation.synthetic_dataset import render_case
from omr_scanner.evaluation.test_cases import FieldLayout, SheetBuilder
from omr_scanner.gui.answer_key.page import AnswerKeyPage, UnsavedChoice
from omr_scanner.gui.answer_key.solution_dialog import SolutionSheetDialog
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.services import (
    create_project,
    project_sets,
    save_template,
    scoring_store,
    set_active_template,
)
from omr_scanner.services.answer_key import plan_for
from omr_scanner.services.solution_sheet import read_solution_sheet

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.gui

OPERATOR = "Key Checker"
SETS = ("A", "B", "C", "D")


def _yes(*_a: object, **_k: object) -> QMessageBox.StandardButton:
    return QMessageBox.StandardButton.Yes


def _make_project(root: Path, template, sets=SETS) -> Path:
    """A project on disk with a saved template and defined sets - then closed."""
    session = create_project(root, "Answer Key Stage")
    try:
        for code in sets:
            project_sets.add_set(session.database, code)
        path = save_template(template, session.project.layout.templates_dir / "exam.omrt")
        set_active_template(session, path)
        return session.root
    finally:
        session.close()


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch) -> Iterator[MainWindow]:
    monkeypatch.setattr(QMessageBox, "question", _yes)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *_a, **_k: None)
    win = MainWindow(AppConfig(reviewer_name=OPERATOR), config_path=tmp_path / "c.json")
    qtbot.addWidget(win)
    yield win
    win.close()


def _open(window: MainWindow, root: Path) -> AnswerKeyPage:
    assert window.open_project_at(root)
    page = window._answer_key_page()
    assert page is not None
    return page


# ----------------------------------------------------------------------
# Part 1: the template comes from the project
# ----------------------------------------------------------------------
class TestWithoutVisitingScan:
    def test_opening_a_project_is_enough(self, window, tmp_path, template):
        root = _make_project(tmp_path, template)
        page = _open(window, root)
        # Straight to Answer Key: Scan is never shown.
        assert window.show_page("answer_key")
        assert page.state.plan == plan_for(template)
        assert page.state.plan.question_count == 20
        assert page.set_codes() == SETS
        assert [page.set_combo.itemText(i) for i in range(page.set_combo.count())] == list(SETS)
        assert page.table.rowCount() == 20
        assert page.key_edit.isEnabled()
        assert "does not yet have" not in page.validation_label.text()

    def test_a_project_without_a_template_says_where_to_make_one(self, window, tmp_path):
        session = create_project(tmp_path, "No Template")
        root = session.root
        session.close()
        page = _open(window, root)
        text = page.validation_label.text()
        assert "does not yet have an active OMR template" in text
        assert "Template stage" in text and "Scan" not in text
        assert not page.key_edit.isEnabled()
        assert not page.scan_button.isEnabled()
        assert "Template stage" in page.scan_button.toolTip()

    def test_a_template_with_no_questions_is_named(self, window, tmp_path):
        root = _make_project(tmp_path, build_answer_sheet_template(question_blocks=0))
        page = _open(window, root)
        assert "contains no question regions" in page.validation_label.text()

    def test_a_template_resaved_in_place_is_reread(self, window, tmp_path, template):
        root = _make_project(tmp_path, template)
        page = _open(window, root)
        path = window.session.project.layout.templates_dir / "exam.omrt"
        save_template(build_answer_sheet_template(questions_per_block=15), path)
        # What the Template stage does after saving the same file again.
        window.set_active_project_template(path.resolve())
        assert page.state.plan.question_count == 30
        # The Scan stage (and Results, fed from it) re-read it too.
        assert plan_for(window._scan_page().state.template).question_count == 30
        assert plan_for(window._results_page().state.template).question_count == 30


class TestSamePathTemplateRefreshOnScan:
    def _loaded(self, window: Any, tmp_path: Path, template: Any) -> tuple[Any, Path]:
        root = _make_project(tmp_path, template)
        assert window.open_project_at(root)
        path = (window.session.project.layout.templates_dir / "exam.omrt").resolve()
        return window._scan_page(), path

    def test_an_unchanged_file_is_not_reloaded(self, window, tmp_path, template):
        scan, path = self._loaded(window, tmp_path, template)
        before = scan.state.template
        window.set_active_project_template(path)  # re-announced, as opening does
        assert scan.state.template is before

    def test_no_reload_while_a_batch_is_running(self, window, tmp_path, template, monkeypatch):
        scan, path = self._loaded(window, tmp_path, template)
        before = scan.state.template
        save_template(build_answer_sheet_template(questions_per_block=15), path)
        monkeypatch.setattr(window, "batch_is_running", lambda: True)
        window.set_active_project_template(path)
        assert scan.state.template is before


# ----------------------------------------------------------------------
# Parts 3-6: sets, entry, table, full credit
# ----------------------------------------------------------------------
class TestSetsAndEntry:
    def test_multi_digit_sets_and_their_states(self, window, tmp_path, template):
        root = _make_project(tmp_path, template, sets=("10", "11", "12"))
        page = _open(window, root)
        assert page.set_codes() == ("10", "11", "12")
        assert not page.set_combo.isEditable()  # defined sets are chosen, not typed
        assert all(page.set_state(code) == "missing" for code in ("10", "11", "12"))
        assert "0 of 3 verified" in page.readiness_label.text()
        assert page.select_set("11")
        assert page.state.set_code == "11"
        assert page._tiles["11"].isChecked()
        assert "Missing" in page._tiles["12"].text()

    def test_pasting_fills_the_table_and_counts(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A B C D " * 4 + "ABC")
        assert "19 of 20 answers entered" in page.count_label.text()
        assert "Question 20 is missing" in page.count_label.text()
        assert page.table.item(18, 1).text() == "C"
        assert page.table.item(19, 1).text() == "—"
        assert page.table.item(19, 3).text() == "No answer"
        assert not page.save_button.isEnabled()
        assert "Cannot save" in page.save_button.toolTip()

    def test_too_many_is_reported_without_dropping_any(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A" * 21)
        assert "21 answers entered; the template defines 20 questions" in page.count_label.text()
        assert len(page.state.draft.answers) == 21

    def test_an_invalid_symbol_is_flagged_on_its_row(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A" * 16 + "E" + "A" * 3)
        assert "Question 17" in page.validation_label.text()
        assert "Not an answer choice" in page.table.item(16, 3).text()

    def test_one_question_is_corrected_in_the_table(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A" * 20)
        page.set_answer_at_row(4, "C")
        assert page.key_edit.toPlainText() == "AAAACAAAAAAAAAAAAAAA"
        assert page.table.item(4, 1).text() == "C"

    def test_typing_an_option_on_a_row_answers_it_and_moves_on(
        self, window, tmp_path, template
    ):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A" * 20)
        page.table.setCurrentCell(2, 1)
        event = QKeyEvent(
            QKeyEvent.Type.KeyPress, Qt.Key.Key_D, Qt.KeyboardModifier.NoModifier, "d"
        )
        QApplication.sendEvent(page.table, event)
        assert page.key_edit.toPlainText()[2] == "D"
        assert page.table.currentRow() == 3

    def test_ticking_full_credit_updates_the_list(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A" * 20)
        page.table.item(16, 2).setCheckState(Qt.CheckState.Checked)
        page.table.item(3, 2).setCheckState(Qt.CheckState.Checked)
        assert page.wrong_edit.text() == "4, 17"
        assert page.state.draft.wrong_questions == {4, 17}
        page.table.item(3, 2).setCheckState(Qt.CheckState.Unchecked)
        assert page.wrong_edit.text() == "17"

    def test_a_duplicate_full_credit_number_is_reported(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A" * 20)
        page.wrong_edit.setText("17, 17")
        assert "more than once" in page.validation_label.text()
        assert not page.save_button.isEnabled()


# ----------------------------------------------------------------------
# Parts 8-9: revisions, verification, dirty state
# ----------------------------------------------------------------------
class TestRevisionsAndVerification:
    def test_save_verify_and_edit_a_verified_key(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("ABCD" * 5)
        assert page.set_state("A") == "unsaved"
        assert page.save_key()
        assert page.set_state("A") == "draft"
        assert page.verify_button.isEnabled()
        assert page.verify_key()
        assert page.set_state("A") == "verified"
        assert "Verified by Key Checker" in page.provenance_label.text()
        assert "1 of 4 verified" in page.readiness_label.text()

        # Editing a verified key never leaves the edit looking verified.
        page.set_answer(1, "D")
        assert page.set_state("A") == "unsaved"
        assert "Unsaved changes" in page.status_label.text()
        assert not page.verify_button.isEnabled()
        assert page.save_key()
        stored = page.current_revision()
        assert stored.revision == 2 and stored.key.status is AnswerKeyStatus.DRAFT
        first = scoring_store.list_keys(window.session.database, set_code="A")[-1]
        assert first.key.status is AnswerKeyStatus.VERIFIED  # history kept, still in use
        assert "Results use verified revision 1" in page.provenance_label.text()

    def test_verify_explains_why_it_is_unavailable(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A" * 20)
        assert "Save the key as a revision first" in page.verify_button.toolTip()
        assert page.action_hint_label.text()
        page.set_reviewer("")
        page.save_key()
        assert "reviewer name" in page.verify_button.toolTip()

    def test_switching_sets_with_unsaved_edits_asks(self, window, tmp_path, template):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("B" * 20)
        asked: list[str] = []
        page.ask_unsaved = lambda action: (asked.append(action), UnsavedChoice.CANCEL)[1]  # type: ignore[method-assign]
        assert page.select_set("B") is False
        assert page.state.set_code == "A" and page.key_edit.toPlainText() == "B" * 20
        assert asked == ["switch to Set B"]

        page.ask_unsaved = lambda _action: UnsavedChoice.SAVE  # type: ignore[method-assign]
        assert page.select_set("B")
        saved = scoring_store.list_keys(window.session.database, set_code="A")
        assert saved[0].key.answers == "B" * 20
        assert page.key_edit.toPlainText() == ""  # Set B has no key yet

        page.key_edit.setPlainText("C" * 20)
        page.ask_unsaved = lambda _action: UnsavedChoice.DISCARD  # type: ignore[method-assign]
        assert page.select_set("A")
        assert page.key_edit.toPlainText() == "B" * 20  # Set A's stored key
        assert scoring_store.list_keys(window.session.database, set_code="B") == ()

    def test_a_historical_revision_is_viewable_without_becoming_active(
        self, window, tmp_path, template
    ):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("A" * 20)
        page.save_key()
        page.verify_key()
        page.key_edit.setPlainText("B" * 20)
        page.save_key()
        first = page.revision_combo.findData(page.state.revisions[-1].key_id)
        page.revision_combo.setCurrentIndex(first)
        assert page.key_edit.toPlainText() == "A" * 20
        assert "newer revision" in page.verify_button.toolTip()
        assert scoring_store.verified_key(window.session.database, "A").revision == 1

    def test_leaving_the_stage_keeps_unsaved_edits_and_the_window_offers_to_save(
        self, window, tmp_path, template
    ):
        page = _open(window, _make_project(tmp_path, template))
        page.key_edit.setPlainText("D" * 20)
        assert window.show_page("results")
        assert window.show_page("answer_key")
        assert page.key_edit.toPlainText() == "D" * 20
        assert "unsaved changes" in page.unsaved_changes_summary()
        page.ask_unsaved = lambda _action: UnsavedChoice.CANCEL  # type: ignore[method-assign]
        assert window.confirm_unsaved_work("close the project") is False
        page.ask_unsaved = lambda _action: UnsavedChoice.SAVE  # type: ignore[method-assign]
        assert window.confirm_unsaved_work("close the project") is True
        assert scoring_store.list_keys(window.session.database, set_code="A")


# ----------------------------------------------------------------------
# Part 7: the solution sheet
# ----------------------------------------------------------------------
def _sheet(template, path: Path, *, set_code: str, blank=(), double=(), seed=5) -> tuple[Path, Any]:
    layout = FieldLayout.of(template)
    key = generate_answer_keys(template, layout, (set_code,), seed=seed)[set_code]
    if not blank and not double:
        case = solution_case(key, layout, index=1)
    else:
        builder = SheetBuilder(layout, 1, random.Random(0))
        builder.set_code(set_code)
        for number, label in key.answers.items():
            if number in blank:
                continue
            others = [item for item in layout.labels_for(number) if item != label]
            builder.answer(number, (label, others[0]) if number in double else (label,))
        case = builder.build()
    cv2.imwrite(str(path), render_case(template, case).image)
    return path, key


class TestSolutionSheetWorkflow:
    def test_an_ambiguous_sheet_is_reviewed_corrected_and_saved(
        self, window, tmp_path, template, monkeypatch
    ):
        page = _open(window, _make_project(tmp_path / "p", template))
        path, key = _sheet(
            template, tmp_path / "Set_A_key.png", set_code="A", blank=(4,), double=(9,)
        )
        seen: dict[str, Any] = {}

        def review(_page: object, dialog: SolutionSheetDialog) -> bool:
            seen["unanswered"] = dialog.unanswered()
            seen["chips"] = dialog.chips.chip_texts()
            seen["filtered"] = dialog.table.isRowHidden(0)
            seen["highlight"] = dialog.highlight_question(9)
            dialog.set_answer(4, key.key_string[3])
            return True

        monkeypatch.setattr(AnswerKeyPage, "_run_solution_dialog", review)
        assert page.read_from_scan(path)
        assert seen["unanswered"] == (4, 9)
        assert "1 blank" in seen["chips"] and "1 multiple" in seen["chips"]
        assert seen["filtered"] is True  # clean rows hidden by default
        assert seen["highlight"] is True
        # Q9 was not decided: it stays missing, and the key cannot be saved.
        assert page.state.source is AnswerKeySource.SCANNED
        assert not page.save_button.isEnabled()
        assert "Question 9" in page.validation_label.text()
        assert "Multiple marks" in page.table.item(8, 3).text()
        page.set_answer(9, key.key_string[8])
        assert page.save_key()
        stored = page.current_revision()
        assert stored.key.answers == key.key_string
        assert stored.key.source is AnswerKeySource.SCANNED
        assert stored.source_scan == "Set_A_key.png"
        assert len(stored.source_sha256) == 64
        assert stored.source_metadata["blank_at_read"] == [4]
        assert stored.source_metadata["multiple_at_read"] == [9]
        assert stored.source_metadata["corrected_by_operator"] == [4, 9]
        assert stored.created_by == OPERATOR
        assert stored.template_name == template.name

    def test_a_scanned_key_does_not_overwrite_a_verified_one(
        self, window, tmp_path, template, monkeypatch
    ):
        page = _open(window, _make_project(tmp_path / "p", template))
        page.key_edit.setPlainText("A" * 20)
        page.save_key()
        page.verify_key()
        path, key = _sheet(template, tmp_path / "a.png", set_code="A")
        monkeypatch.setattr(AnswerKeyPage, "_run_solution_dialog", lambda _p, _d: True)
        assert page.read_from_scan(path)
        assert scoring_store.verified_key(window.session.database, "A").key.answers == "A" * 20
        assert page.set_state("A") in ("unsaved", "verified")
        assert page.is_dirty() == (key.key_string != "A" * 20)

    def test_the_source_file_may_disappear_after_saving(
        self, window, tmp_path, template, monkeypatch
    ):
        page = _open(window, _make_project(tmp_path / "p", template))
        path, key = _sheet(template, tmp_path / "gone.png", set_code="A")
        monkeypatch.setattr(AnswerKeyPage, "_run_solution_dialog", lambda _p, _d: True)
        assert page.read_from_scan(path)
        assert page.save_key() and page.verify_key()
        path.unlink()
        root = window.session.root
        window.close_project()
        page = _open(window, root)
        assert page.key_edit.toPlainText() == key.key_string
        assert "gone.png" in page.provenance_label.text()
        assert page.set_state("A") == "verified"

    def test_the_dialog_blocks_a_mismatched_set_until_chosen(self, qtbot, tmp_path, template):
        plan = plan_for(template)
        path, _key = _sheet(template, tmp_path / "b.png", set_code="B")
        reading = read_solution_sheet(path, template, plan, selected_set="A", defined_sets=SETS)
        dialog = SolutionSheetDialog(reading, template)
        qtbot.addWidget(dialog)
        assert not dialog.accept_button.isEnabled()
        assert "appears to be Set B" in dialog.set_label.text()
        dialog.choose_set("A")
        assert dialog.accept_button.isEnabled() and dialog.target_set() == "A"
        assert "kept the selected set" in dialog.set_decision()
        dialog.choose_set("B")
        assert dialog.target_set() == "B"

    def test_the_dialog_opens_on_the_first_question_needing_review(
        self, qtbot, tmp_path, template
    ):
        """Found on a real scan: it used to open at 1:1 on the top-left corner."""
        path, _key = _sheet(template, tmp_path / "amb.png", set_code="A", blank=(6,), double=(9,))
        reading = read_solution_sheet(path, template, plan_for(template), selected_set="A")
        dialog = SolutionSheetDialog(reading, template)
        qtbot.addWidget(dialog)
        assert dialog.table.currentRow() == -1
        dialog.show()
        qtbot.waitExposed(dialog)
        assert dialog.table.currentRow() == dialog._rows[6]
        assert dialog.preview.focus_rect is not None

    def test_a_clean_sheet_opens_on_the_whole_page(self, qtbot, tmp_path, template):
        path, _key = _sheet(template, tmp_path / "clean.png", set_code="A")
        reading = read_solution_sheet(path, template, plan_for(template), selected_set="A")
        dialog = SolutionSheetDialog(reading, template)
        qtbot.addWidget(dialog)
        dialog.show()
        qtbot.waitExposed(dialog)
        assert dialog.table.currentRow() == -1
        assert dialog.preview.focus_rect is None
        assert dialog.preview.zoom < 1.0  # fitted, not shown at 1:1

    def test_an_unregistered_sheet_offers_nothing(self, qtbot, tmp_path, template):
        import numpy as np

        path = tmp_path / "white.png"
        cv2.imwrite(str(path), np.full((1400, 1000), 255, dtype=np.uint8))
        reading = read_solution_sheet(path, template, plan_for(template), selected_set="A")
        dialog = SolutionSheetDialog(reading, template)
        qtbot.addWidget(dialog)
        assert not dialog.accept_button.isEnabled()
        assert "could not be registered" in dialog.outcome_label.text()


# ----------------------------------------------------------------------
# Part 13: persistence across a reopen, mixed sources
# ----------------------------------------------------------------------
class TestReopening:
    def test_every_set_is_restored(self, window, tmp_path, template, monkeypatch):
        root = _make_project(tmp_path / "p", template)
        page = _open(window, root)
        monkeypatch.setattr(AnswerKeyPage, "_run_solution_dialog", lambda _p, _d: True)
        typed = {"A": "ABCD" * 5, "C": "DCBA" * 5}
        scanned: dict[str, str] = {}
        for code in SETS:
            assert page.select_set(code)
            if code in typed:
                page.key_edit.setPlainText(typed[code])
                if code == "C":
                    page.wrong_edit.setText("3, 18")
            else:
                path, key = _sheet(
                    template, tmp_path / f"Set_{code}.png", set_code=code, seed=ord(code)
                )
                assert page.read_from_scan(path)
                scanned[code] = key.key_string
            assert page.save_key()
            if code in ("A", "B"):
                assert page.verify_key()

        window.close_project()
        page = _open(window, root)
        assert window.show_page("answer_key")
        assert [page.set_state(code) for code in SETS] == ["verified", "verified", "draft", "draft"]
        for code in SETS:
            assert page.select_set(code)
            stored = page.current_revision()
            expected = typed.get(code) or scanned[code]
            assert page.key_edit.toPlainText() == expected
            assert stored.key.source is (
                AnswerKeySource.MANUAL if code in typed else AnswerKeySource.SCANNED
            )
            if code not in typed:
                assert stored.source_scan == f"Set_{code}.png"
        assert page.select_set("C")
        assert page.wrong_edit.text() == "3, 18"
        assert "2 of 4 verified" in page.readiness_label.text()

    def test_a_key_for_an_older_template_shows_as_incompatible(
        self, window, tmp_path, template
    ):
        root = _make_project(tmp_path / "p", template)
        page = _open(window, root)
        page.key_edit.setPlainText("A" * 20)
        page.save_key()
        page.verify_key()
        window.close_project()
        # The template is replaced with a 30-question one while closed.
        from omr_scanner.services import open_project

        with open_project(root) as session:
            save_template(
                build_answer_sheet_template(questions_per_block=15),
                session.project.layout.templates_dir / "exam.omrt",
            )
        page = _open(window, root)
        assert page.set_state("A") == "stale"
        assert "Incompatible" in page.status_label.text()
        assert not page.verify_button.isEnabled()
        assert "the active template defines 30 questions" in page.validation_label.text()
        assert scoring_store.verified_key(window.session.database, "A").key.answers == "A" * 20
