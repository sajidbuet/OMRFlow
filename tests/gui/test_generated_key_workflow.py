"""A generated examination, taken through the real Answer Key and Results stages.

Scope:
    The operator path for the synthetic generator's ``solution/`` folder, in
    the real :class:`MainWindow`: create a project, give it the template and
    three sets, read the generated scans on the Scan stage, import each set's
    generated attendance workbook, enter each set's key the two ways the
    Answer Key stage offers (paste the ``.txt``; *Read From Solution Sheet* on
    the generated solution OMR), verify, and *Calculate Results*.

    Native file choosers and modal confirmations are the only things not
    driven: the methods behind them are, with the same files an operator would
    pick.

Each test here also pins a defect this path exposed:

    D0  the project's template reached the Scan stage and nothing else, so the
        Answer Key stage could never hold a key;
    D1  Results read only the project's *unscoped* candidate list, so a project
        with per-set attendance could not be scored at all;
    D2  Results learned its candidate list only when the project opened;
    D3  reading one set's solution sheet with another set selected said
        nothing, and saving filed the key under the wrong set;
    D4  the key editor kept the previous project's text across a project
        change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QMessageBox
from tests.conftest import build_answer_sheet_template

from omr_scanner.config import AppConfig
from omr_scanner.domain.project import ProjectLayout
from omr_scanner.domain.scoring import (
    ResultStatus,
    ScoringPolicy,
    canonical_answer_string,
    score_answers,
)
from omr_scanner.evaluation.attendance_dataset import ConflictProfile
from omr_scanner.evaluation.ground_truth import load_ground_truth
from omr_scanner.evaluation.synthetic_dataset import CaseFamily, DatasetProfile
from omr_scanner.gui.devtools.generate_dialog import GenerationRequest
from omr_scanner.gui.devtools.generate_worker import DatasetWorker
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.services import project_sets, save_template, scoring_store
from omr_scanner.services.answer_key import plan_for, read_key
from omr_scanner.services.candidate_import import read_roster

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.gui

SETS = ("10", "11", "12")
OPERATOR = "Acceptance Operator"
TIMEOUT_MS = 180_000


def _wait(signal: Any, timeout: int = TIMEOUT_MS) -> tuple[Any, ...]:
    """Block on one emission of ``signal``; a module fixture has no qtbot."""
    loop = QEventLoop()
    received: list[tuple[Any, ...]] = []

    def done(*args: Any) -> None:
        received.append(args)
        loop.quit()

    signal.connect(done)
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()
    signal.disconnect(done)
    assert received, f"timed out waiting for {signal}"
    return received[0]


class Scenario:
    """Everything the tests read back, gathered by one walk through the stages."""

    window: MainWindow
    dataset: Path
    manifest: Any
    template: Any
    messages: list[str]
    template_reached_key_page: bool
    roster_ids_before_reopen: tuple[int, ...]
    mismatch_warning: list[str]


@pytest.fixture(scope="module")
def scenario(qapp, tmp_path_factory) -> Iterator[Scenario]:
    root = tmp_path_factory.mktemp("generated_key_workflow")
    messages: list[str] = []
    patch = pytest.MonkeyPatch()
    patch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.Yes),
    )
    for name in ("information", "warning", "critical"):
        patch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, **_k: messages.append(str(a[2]) if len(a) > 2 else "")),
        )

    template = build_answer_sheet_template(
        name="Generated key workflow",
        roll_digits=8,
        set_symbols=tuple("0123456789"),
        set_positions=2,
        question_blocks=2,
        questions_per_block=12,
    )
    request = GenerationRequest(
        template_path=save_template(template, root / "generated.omrt"),
        output_dir=root / "dataset",
        count=12,
        seed=20260930,
        profile=DatasetProfile.CUSTOM,
        families=(CaseFamily.BASELINE, CaseFamily.ANSWERS),
        with_attendance=True,
        set_codes=SETS,
        conflict_profile=ConflictProfile.NONE,
        include_reconciliation_edge_cases=False,
    )
    produced: list[Any] = []
    worker = DatasetWorker(request)
    worker.finished_dataset.connect(produced.append)
    worker.run()

    state = Scenario()
    state.dataset, state.manifest, state.template, state.messages = (
        request.output_dir, produced[0], template, messages,
    )
    window = MainWindow(AppConfig(reviewer_name=OPERATOR), config_path=root / "config.json")
    state.window = window
    try:
        assert window.create_project_at(root, "Generated Key Workflow")
        session = window.session
        assert session is not None
        for code in SETS:
            project_sets.add_set(session.database, code, f"Set {code}")
        window.set_active_project_template(
            save_template(template, ProjectLayout(session.project.root).templates_dir / "t.omrt")
        )
        state.template_reached_key_page = window._answer_key_page().state.plan is not None

        scan = window._scan_page()
        scan.add_scan_paths(sorted((request.output_dir / "images").glob("*.png")))
        assert scan.process_all()
        _wait(scan.batch_finished)

        attendance = window._attendance_page()
        for exam_set in project_sets.list_sets(session.database):
            assert attendance.select_set(exam_set.set_id)
            workbook = request.output_dir / "attendance" / f"Set_{exam_set.code}_Attendance.xlsx"
            assert attendance.commit_roster(read_roster(workbook), source_path=workbook)
            _wait(attendance.reconciled)

        solution = request.output_dir / "solution"
        key_page = window._answer_key_page()
        # Set 10: open the .txt and paste it into Answers.
        key_page.set_combo.setCurrentText("10")
        key_page.key_edit.setPlainText(
            (solution / "Set_10_Answer_Key.txt").read_text(encoding="utf-8")
        )
        assert key_page.save_key() and key_page.verify_key()
        # Set 11: Read From Solution Sheet.
        key_page.set_combo.setCurrentText("11")
        assert key_page.read_from_scan(solution / "Set_11_Solution.png")
        assert key_page.save_key() and key_page.verify_key()
        # Set 12's sheet, read while Set 11 is still selected.
        before = len(messages)
        assert key_page.read_from_scan(solution / "Set_12_Solution.png")
        state.mismatch_warning = messages[before:]
        key_page.set_combo.setCurrentText("12")
        assert key_page.save_key() and key_page.verify_key()

        results = window._results_page()
        state.roster_ids_before_reopen = results.state.roster_ids
        assert results.score_batch()
        _wait(results.scored)
        yield state
    finally:
        window.close()
        patch.undo()


def _keys(state: Scenario) -> dict[str, Any]:
    plan = plan_for(state.template)
    return {
        code: read_key(
            (state.dataset / "solution" / f"Set_{code}_Answer_Key.txt").read_text(
                encoding="utf-8"
            ),
            plan,
            code,
        ).to_key()
        for code in SETS
    }


class TestTheAnswerKeyStage:
    def test_the_project_template_reaches_it(self, scenario: Scenario):
        """D0: the Scan stage's template is relayed to the scoring stages."""
        assert scenario.template_reached_key_page
        assert scenario.window._results_page().state.template is not None

    def test_every_verified_key_is_the_generated_one(self, scenario: Scenario):
        stored = scoring_store.verified_keys(scenario.window.session.database)
        manifest = {e["set_code"]: e["answers"] for e in scenario.manifest.generator["answer_keys"]}
        assert set(stored) == set(SETS)
        for code, key in _keys(scenario).items():
            assert stored[code].key.answers == key.answers == manifest[code]
            assert stored[code].key.question_count == 24

    def test_a_sheet_for_another_set_is_named_before_it_can_be_saved(
        self, scenario: Scenario
    ):
        """D3."""
        assert any(
            "marked Set 12, but Set 11 is selected" in text
            for text in scenario.mismatch_warning
        )


class TestTheResultsStage:
    def test_every_set_is_scored(self, scenario: Scenario):
        """D1 and D2: per-set lists, imported during this session, are scored."""
        results = scenario.window._results_page()
        assert len(scenario.roster_ids_before_reopen) == 3
        counts = results.state.counts
        assert counts is not None
        assert counts.blocked == 0
        assert counts.scored == len(scenario.manifest.entries)
        assert set(counts.by_set) == set(SETS)

    def test_each_mark_uses_the_candidates_own_sets_key(self, scenario: Scenario):
        results = scenario.window._results_page()
        keys = _keys(scenario)
        plan = plan_for(scenario.template)
        by_roll = {item.candidate_id: item for item in results.state.results}
        distinguishing = 0
        for name in scenario.manifest.entries:
            truth = load_ground_truth(scenario.dataset / "ground_truth" / name)
            own = truth.metadata["performance"]["answer_key_set"]
            stored = by_roll[truth.roll]
            assert stored.status is ResultStatus.SCORED
            assert stored.set_code == own
            answers = canonical_answer_string(truth.answers, plan.numbers, plan.labels)
            expected = score_answers(answers, keys[own], ScoringPolicy())
            if not truth.ambiguous and not set(truth.tags) & {
                "UNDERSIZED_MARK", "OFFSET_MARK", "MARK_BETWEEN_BUBBLES",
            }:
                assert stored.correct_count == expected.correct_count, truth.scan
            others = [
                score_answers(answers, keys[code], ScoringPolicy()).correct_count
                for code in SETS
                if code != own
            ]
            if all(abs(value - expected.correct_count) >= 4 for value in others):
                distinguishing += 1
        assert distinguishing > 0, "no candidate whose mark depends on which key is used"


class TestReopening:
    def test_keys_and_results_survive_and_the_editor_starts_empty(
        self, scenario: Scenario
    ):
        """D4, and persistence."""
        window = scenario.window
        root = window.session.project.root
        before = window._results_page().state.counts
        window.close_project()
        assert window.open_project_at(root)
        key_page = window._answer_key_page()
        assert key_page.key_edit.toPlainText() == ""
        assert key_page.state.plan is not None
        assert set(scoring_store.verified_keys(window.session.database)) == set(SETS)
        results = window._results_page()
        results.refresh_table()
        assert results.state.counts == before


# ----------------------------------------------------------------------
# Each defect on its own, at the lowest layer that shows it
# ----------------------------------------------------------------------
def _roster(candidate_ids: tuple[str, ...]) -> Any:
    from omr_scanner.domain.reconciliation import AttendanceState, CandidateRecord
    from omr_scanner.services.candidate_import import ColumnMapping, RosterValidation

    records = tuple(
        CandidateRecord(
            candidate_id=candidate_id,
            display_name=f"Candidate {index}",
            source_row=index + 2,
            imported_attendance=AttendanceState.PRESENT,
            imported_value="---",
        )
        for index, candidate_id in enumerate(candidate_ids)
    )
    return RosterValidation(
        candidates=records, issues=(), rows_read=len(records), blank_ids=0,
        duplicate_ids=0, expected_present=len(records), expected_absent=0,
        attendance_unknown=0, mapping=ColumnMapping(candidate_id=0, name=1, attendance=2),
        source_name="roster.csv",
    )


class TestEachDefect:
    def test_d0_the_project_template_reaches_answer_key_and_results(
        self, qtbot, tmp_path: Path
    ):
        window = MainWindow(AppConfig(reviewer_name=OPERATOR), config_path=tmp_path / "c.json")
        qtbot.addWidget(window)
        try:
            assert window.create_project_at(tmp_path, "Template Relay")
            layout = ProjectLayout(window.session.project.root)
            window.set_active_project_template(
                save_template(build_answer_sheet_template(), layout.templates_dir / "t.omrt")
            )
            assert window._answer_key_page().state.plan is not None
            assert window._results_page().state.template is not None
            assert window._attendance_page().state.template is not None
            window.close_project()
            assert window._answer_key_page().state.plan is None
        finally:
            window.close()

    def test_d1_d2_per_set_lists_imported_after_opening_are_scored(
        self, qtbot, project_session
    ):
        from omr_scanner.gui.pages import WORKFLOW_PAGES
        from omr_scanner.gui.results.page import ResultsPage
        from omr_scanner.services import reconciliation_store

        spec = next(item for item in WORKFLOW_PAGES if item.key == "results")
        page = ResultsPage(spec)
        qtbot.addWidget(page)
        page.on_project_changed(project_session)
        assert page.state.roster_ids == ()

        database = project_session.database
        first = project_sets.add_set(database, "10")
        second = project_sets.add_set(database, "11")
        ids = [
            reconciliation_store.import_roster(
                database, _roster((f"{code}001", f"{code}002")), set_id=exam_set.set_id
            )
            for code, exam_set in (("10", first), ("11", second))
        ]
        page.refresh_table()
        assert page.state.roster_ids == tuple(ids)

    def test_d3_a_solution_sheet_for_another_set_warns(
        self, qtbot, tmp_path: Path, project_session, monkeypatch
    ):
        import cv2

        from omr_scanner.evaluation.answer_keys import generate_answer_keys, solution_case
        from omr_scanner.evaluation.synthetic_dataset import render_case
        from omr_scanner.evaluation.test_cases import FieldLayout
        from omr_scanner.gui.answer_key.page import AnswerKeyPage
        from omr_scanner.gui.pages import WORKFLOW_PAGES

        template = build_answer_sheet_template()
        layout = FieldLayout.of(template)
        key = generate_answer_keys(template, layout, ("B",), seed=3)["B"]
        path = tmp_path / "Set_B_Solution.png"
        cv2.imwrite(str(path), render_case(template, solution_case(key, layout, index=1)).image)

        warned: list[str] = []
        monkeypatch.setattr(
            "omr_scanner.gui.answer_key.page.QMessageBox.warning",
            staticmethod(lambda *a, **_k: warned.append(str(a[2]))),
        )
        spec = next(item for item in WORKFLOW_PAGES if item.key == "answer_key")
        page = AnswerKeyPage(spec)
        qtbot.addWidget(page)
        page.on_project_changed(project_session)
        page.set_template(template)

        page.set_combo.setCurrentText("A")
        assert page.read_from_scan(path)
        assert page.state.set_code == "A"  # never switched behind the operator's back
        assert any("marked Set B, but Set A is selected" in text for text in warned)

        warned.clear()
        page.set_combo.setCurrentText("B")
        assert page.read_from_scan(path)
        assert warned == []
        assert page.state.draft is not None and page.state.draft.answers == key.key_string

    def test_d4_the_editor_is_cleared_for_a_new_project_only(
        self, qtbot, tmp_path: Path, project_session
    ):
        from omr_scanner.gui.answer_key.page import AnswerKeyPage
        from omr_scanner.gui.pages import WORKFLOW_PAGES
        from omr_scanner.services import create_project

        spec = next(item for item in WORKFLOW_PAGES if item.key == "answer_key")
        page = AnswerKeyPage(spec)
        qtbot.addWidget(page)
        page.set_template(build_answer_sheet_template())
        page.on_project_changed(project_session)
        page.set_combo.setCurrentText("A")
        page.key_edit.setPlainText("ABCD")

        page.on_project_changed(project_session)  # the same project, re-announced
        assert page.key_edit.toPlainText() == "ABCD"

        other = create_project(tmp_path / "other_parent", "Other Examination")
        try:
            page.on_project_changed(other)
            assert page.key_edit.toPlainText() == ""
            assert page.state.set_code == ""
        finally:
            page.on_project_changed(None)
            other.close()
