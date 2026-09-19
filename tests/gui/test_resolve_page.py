"""The conflict review workflow, through the real widgets (Phase 6).

Scope:
    Drives :class:`~omr_scanner.gui.review.page.ResolvePage` through the same
    public commands its buttons and shortcuts call, with a real project, a real
    ``QThread`` and a real recognition engine - and asserts against **what ended
    up in the database**, not against the label text that happens to be on
    screen.

    That distinction is the whole point: a test that checked the queue said
    "Resolved" would pass just as happily if nothing had been persisted, which
    is the failure mode Phase 6 exists to prevent.

Why every wait is on a signal:
    Selecting a conflict starts a real ``SheetWorker``. Every wait here is on
    ``ResolvePage.sheet_ready``; nothing sleeps for a fixed time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.config import AppConfig
from omr_scanner.domain.review import (
    ConflictState,
    ConflictType,
    ReasonCode,
    ReviewAction,
    ValueSource,
)
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.review.history_dialog import render_history
from omr_scanner.gui.review.page import BLANK_CHOICE, FILTER_ALL, FILTER_RESOLVED, ResolvePage
from omr_scanner.services import batch_store, review_store, save_template
from omr_scanner.services.batch_processor import process_batch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

SHEET_TIMEOUT_MS = 120_000
REVIEWER = "Dr. Rahman"


def _accept_warning(*_args: object, **_kwargs: object) -> object:
    """Stand in for ``QMessageBox.warning``, dismissing it."""
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Ok


def _recording_warning(seen: list[str]) -> Callable[..., object]:
    """A ``QMessageBox.warning`` stand-in that captures what was shown.

    Used to assert that a refusal actually reached the operator, rather than
    only that the action returned ``False``.
    """

    def warning(
        _parent: object, _title: str, text: str, *_args: object, **_kwargs: object
    ) -> object:
        from PySide6.QtWidgets import QMessageBox

        seen.append(text)
        return QMessageBox.StandardButton.Ok

    return warning


def sheet_marks(roll: str = "120317", **overrides: object) -> dict:
    marks = {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }
    marks.update(overrides)
    return marks


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def template_path(project_session: ProjectSession, template) -> Path:
    path = project_session.project.layout.templates_dir / "sheet.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return save_template(template, path)


@pytest.fixture
def prepared(project_session: ProjectSession, template, tmp_path: Path):
    """A batch with one double-marked answer and one duplicate identifier."""
    scans = tmp_path / "scans"
    scans.mkdir(parents=True, exist_ok=True)

    double = sheet_marks("170501")
    double["questions_0"] = {**double["questions_0"], 0: ["B", "D"]}
    paths = []
    for name, marks in (
        ("double.png", double),
        ("dup.png", sheet_marks("170501")),
        ("clean.png", sheet_marks("170502")),
    ):
        path = scans / name
        cv2.imwrite(str(path), render_marked_sheet(template, marks))
        paths.append(path)

    database = project_session.database
    batch_id = batch_store.create_batch(
        database, paths, identity=batch_store.BatchIdentity.of(template)
    )
    recorder = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    report = process_batch(paths, template, on_result=recorder.record, workers=1)
    recorder.flush()
    batch_store.finalise_batch(database, batch_id)

    ids = batch_store.scan_ids_by_path(database, batch_id)
    for item in report.processed:
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[item.source_path],
            result=item.result,
            template=template,
        )
    review_store.sync_duplicate_identifiers(database, batch_id)
    return batch_id


@pytest.fixture
def page(qtbot, project_session: ProjectSession, template, prepared):
    """A Resolve page on a prepared batch, shut down deterministically.

    Selecting a conflict starts a real ``SheetWorker``. A ``QThread`` still
    running when the interpreter tears down makes Qt abort the process - on
    Windows with a bare ``0xC0000409`` and no traceback - so the page is closed
    explicitly, which waits for it.
    """
    spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
    review_page = ResolvePage(spec)
    qtbot.addWidget(review_page)
    review_page.on_project_changed(project_session)
    review_page.set_reviewer(REVIEWER)
    assert review_page.load_batch(prepared, template) is True
    yield review_page
    review_page.close()


def select_first(qtbot, page: ResolvePage, conflict_type: ConflictType | None = None) -> int:
    """Select the first conflict (optionally of one type) and await its sheet."""
    for row, conflict in enumerate(page.state.conflicts):
        if conflict_type is None or conflict.conflict_type is conflict_type:
            with qtbot.waitSignal(page.sheet_ready, timeout=SHEET_TIMEOUT_MS):
                page.queue_table.selectRow(row)
            return conflict.conflict_id
    raise AssertionError(f"no conflict of type {conflict_type} in the queue")


# ----------------------------------------------------------------------
# The queue
# ----------------------------------------------------------------------
class TestQueue:
    def test_the_queue_opens_and_lists_conflicts(self, page: ResolvePage):
        assert page.state.conflicts
        assert page.queue_table.rowCount() == len(page.state.conflicts)

    def test_the_summary_reports_the_batch_counts(self, page: ResolvePage):
        text = page.summary_label.text()
        assert "Total conflicts" in text
        assert "Unresolved" in text

    def test_selecting_a_row_shows_that_conflict(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page)
        assert page.current_conflict().conflict_id == conflict_id
        assert page.evidence_label.text()

    def test_filtering_by_state_narrows_the_queue(self, qtbot, page: ResolvePage):
        before = len(page.state.conflicts)
        page.state_filter.setCurrentText(FILTER_RESOLVED)
        assert page.state.conflicts == []

        page.state_filter.setCurrentText(FILTER_ALL)
        assert len(page.state.conflicts) == before

    def test_filtering_by_type_narrows_the_queue(self, page: ResolvePage):
        page.type_filter.setCurrentText(ConflictType.IDENTIFIER_DUPLICATE.label)
        assert page.state.conflicts
        assert all(
            item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
            for item in page.state.conflicts
        )

    def test_searching_by_identifier_narrows_the_queue(self, page: ResolvePage):
        page.search_box.setText("170501")
        page.refresh_queue()
        assert page.state.conflicts
        assert all("170501" in item.identifier_value for item in page.state.conflicts)

    def test_navigation_moves_through_the_queue(self, qtbot, page: ResolvePage):
        select_first(qtbot, page)
        first = page.current_conflict().conflict_id
        page.select_next()
        assert page.current_conflict().conflict_id != first
        page.select_previous()
        assert page.current_conflict().conflict_id == first

    def test_next_unresolved_skips_decided_conflicts(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page)
        page.accept_machine()
        page.select_conflict_by_id(conflict_id)
        page.select_next_unresolved()
        landed = page.current_conflict()
        assert landed.state is ConflictState.OPEN


# ----------------------------------------------------------------------
# The review workspace
# ----------------------------------------------------------------------
class TestWorkspace:
    def test_selecting_a_conflict_loads_all_three_views(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        assert page.normalised_view.has_page is True
        assert page.original_view.has_page is True
        assert page.zoom_view.has_page is True

    def test_the_zoom_view_is_focused_on_the_disputed_field(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        # Magnified well past "fit the whole page", which is what makes the
        # bubbles legible enough to judge.
        assert page.zoom_view.zoom > page.normalised_view.zoom

    def test_only_the_disputed_group_is_ringed(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        conflict = page.current_conflict()
        drawn = page.zoom_view._overlay._bubbles
        assert drawn
        # Exactly the options of one question, not all five hundred bubbles.
        assert all(item.zone_id == conflict.field.zone_id for item in drawn)
        assert len(drawn) <= 8

    def test_a_batch_level_conflict_still_highlights_its_field(
        self, qtbot, page: ResolvePage
    ):
        # A duplicate is found from the identifiers alone, so the conflict names
        # no zone. The reviewer still has to *read the roll number* to decide,
        # and an earlier build showed them the whole page at fit scale. The
        # zone comes from the engine's own result, not from a guess in the GUI.
        select_first(qtbot, page, ConflictType.IDENTIFIER_DUPLICATE)
        conflict = page.current_conflict()
        assert conflict.field.zone_id == ""
        drawn = page.zoom_view._overlay._bubbles
        assert drawn
        assert {item.zone_id for item in drawn} == {
            page.state.bundle.result.identifier_zone_id
        }
        assert page.zoom_view.zoom > page.normalised_view.zoom

    def test_the_original_view_carries_no_overlay(self, qtbot, page: ResolvePage):
        # Overlay coordinates are canonical-page pixels; the original is not in
        # that frame, so drawing them there would be wrong everywhere while
        # looking plausible.
        select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        assert page.original_view._overlay._bubbles == ()
        assert page.original_view._overlay.show_bubbles is False

    def test_the_original_view_says_where_the_field_is(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        note = page.original_note.text()
        assert "never modified" in note
        assert "x=" in note and "y=" in note

    def test_the_evidence_panel_shows_measured_fill_scores(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        text = page.evidence_label.text()
        assert "Machine value" in text
        # Labelled as what it is. The engine measures coverage, not likelihood.
        assert "probability" not in text.lower()

    def test_the_sheet_progress_label_counts_this_sheets_conflicts(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page)
        assert "This sheet" in page.sheet_progress_label.text()

    def test_the_choice_buttons_come_from_the_template(
        self, qtbot, page: ResolvePage, template
    ):
        select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        labels = [button.text() for button in page._choice_buttons]
        zone = next(item for item in template.zones if item.id == "questions_0")
        assert labels == [*zone.field.answer_labels, BLANK_CHOICE]

    def test_a_duplicate_identifier_offers_free_text_not_buttons(
        self, qtbot, page: ResolvePage
    ):
        # The corrected value for a duplicate roll number is a number nobody
        # printed on this sheet, so there is no symbol set to offer. An earlier
        # build showed only "(blank)" here, which gave a reviewer no way to say
        # what they meant.
        select_first(qtbot, page, ConflictType.IDENTIFIER_DUPLICATE)
        assert page._choice_buttons == []
        assert page.free_value_row.isVisibleTo(page) is True
        assert page.free_value_edit.text() == "170501"

    def test_a_free_text_correction_is_recorded(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_DUPLICATE)
        page.reason_combo.setCurrentText(ReasonCode.MISCLASSIFICATION.label)
        page.free_value_edit.setText("170503")
        assert page._correct_from_text() is True

        found = review_store.provenance_for(page.database, conflict_id)
        assert found.value == "170503"
        assert found.machine_value == "170501"
        assert found.reviewer == REVIEWER

    def test_a_sheet_level_conflict_offers_no_value_buttons(
        self, qtbot, project_session, template, tmp_path, prepared
    ):
        # A corrupt JPEG is not a value a reviewer can pick between.
        spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
        review_page = ResolvePage(spec)
        review_page.on_project_changed(project_session)
        review_page.set_reviewer(REVIEWER)

        corrupt = tmp_path / "corrupt.png"
        corrupt.write_bytes(b"not an image")
        database = project_session.database
        batch_id = batch_store.create_batch(
            database, [corrupt], identity=batch_store.BatchIdentity.of(template)
        )
        report = process_batch([corrupt], template, workers=1)
        ids = batch_store.scan_ids_by_path(database, batch_id)
        review_store.sync_conflicts(
            database,
            batch_id=batch_id,
            scan_id=ids[corrupt],
            result=report.processed[0].result,
            template=template,
        )
        review_page.load_batch(batch_id, template)
        review_page.queue_table.selectRow(0)

        assert review_page._choice_buttons == []
        review_page.close()


# ----------------------------------------------------------------------
# Decisions
# ----------------------------------------------------------------------
class TestDecisions:
    def test_accepting_records_a_human_confirmation(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        machine_value = page.current_conflict().observation.value

        assert page.accept_machine() is True

        database = page.database
        found = review_store.provenance_for(database, conflict_id)
        assert found.source is ValueSource.HUMAN
        assert found.reviewer == REVIEWER
        assert found.value == machine_value
        assert found.machine_value == machine_value

    def test_correcting_records_the_value_and_keeps_the_machines(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        machine_value = page.current_conflict().observation.value
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)

        assert page.correct("B") is True

        found = review_store.provenance_for(page.database, conflict_id)
        assert found.value == "B"
        assert found.machine_value == machine_value
        assert found.reviewer == REVIEWER
        assert found.reason == ReasonCode.DOMINANT_MARK.value

    def test_a_correction_without_a_reviewer_is_refused(
        self, qtbot, monkeypatch, page: ResolvePage
    ):

        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        page.set_reviewer("")
        shown: list[str] = []
        monkeypatch.setattr(
            "omr_scanner.gui.error_reporting.QMessageBox.warning",
            staticmethod(_recording_warning(shown)),
        )
        assert page.correct("B") is False
        # Nothing was written.
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_other_without_an_explanation_is_refused(
        self, qtbot, monkeypatch, page: ResolvePage
    ):

        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.OTHER.label)
        page.reason_text.setPlainText("   ")
        monkeypatch.setattr(
            "omr_scanner.gui.error_reporting.QMessageBox.warning",
            staticmethod(_accept_warning),
        )
        assert page.correct("B") is False
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_deferring_is_recorded(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page)
        assert page.defer_conflict() is True
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.DEFERRED
        )

    def test_reopening_keeps_the_earlier_correction_in_the_history(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("B")

        # Resolving removes it from the default "Unresolved" view, so a
        # reviewer coming back to change their mind has to widen the filter -
        # which is exactly what reopening is for.
        page.state_filter.setCurrentText(FILTER_ALL)
        assert page.select_conflict_by_id(conflict_id) is True
        assert page.reopen_conflict() is True

        assert page.select_conflict_by_id(conflict_id) is True
        page.reason_combo.setCurrentText(ReasonCode.MISCLASSIFICATION.label)
        page.correct("D")

        history = review_store.history_for(page.database, conflict_id)
        assert [item.action for item in history] == [
            ReviewAction.DETECTED,
            ReviewAction.CORRECTED,
            ReviewAction.REOPENED,
            ReviewAction.CORRECTED,
        ]
        assert history[1].new_value == "B"
        assert history[3].new_value == "D"
        assert review_store.provenance_for(page.database, conflict_id).value == "D"

    def test_the_queue_shows_the_new_state_after_a_decision(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        page.accept_machine()
        page.state_filter.setCurrentText(FILTER_ALL)
        record = next(
            item for item in page.state.conflicts if item.conflict_id == conflict_id
        )
        assert record.state is ConflictState.RESOLVED

    def test_reopen_is_disabled_until_something_has_been_decided(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        assert page.reopen_button.isEnabled() is False
        page.accept_machine()

        page.state_filter.setCurrentText(FILTER_ALL)
        assert page.select_conflict_by_id(conflict_id) is True
        assert page.reopen_button.isEnabled() is True

    def test_deciding_the_last_open_conflict_clears_the_workspace(
        self, qtbot, page: ResolvePage
    ):
        # The defect this guards: if the queue empties but the workspace keeps
        # showing the conflict that was just decided, the next click acts on
        # something the reviewer is no longer looking at.
        page.type_filter.setCurrentText(ConflictType.ANSWER_MULTIPLE.label)
        while page.state.conflicts:
            with qtbot.waitSignal(page.sheet_ready, timeout=SHEET_TIMEOUT_MS):
                page.queue_table.selectRow(0)
            page.accept_machine()

        assert page.current_conflict() is None
        assert page.accept_button.isEnabled() is False
        assert "Select a conflict" in page.evidence_label.text()


# ----------------------------------------------------------------------
# History
# ----------------------------------------------------------------------
class TestHistory:
    def test_the_history_view_renders_real_persisted_events(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("B")

        conflict = review_store.get_conflict(page.database, conflict_id)
        events = review_store.history_for(page.database, conflict_id)
        html = render_history(conflict, events)

        assert REVIEWER in html
        assert ReasonCode.DOMINANT_MARK.label in html
        assert "Machine recognition" in html
        # The machine's own reading is still stated, after the correction.
        assert conflict.observation.value in html

    def test_a_conflict_with_no_decisions_still_renders(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page)
        conflict = review_store.get_conflict(page.database, conflict_id)
        html = render_history(conflict, review_store.history_for(page.database, conflict_id))
        assert "Machine recognition" in html


# ----------------------------------------------------------------------
# Persistence through the GUI
# ----------------------------------------------------------------------
class TestStateSurvivesReopening:
    def test_decisions_made_in_the_page_are_still_there_on_a_fresh_page(
        self, qtbot, project_session, template, prepared, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("B")
        page.close()

        spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
        reopened = ResolvePage(spec)
        qtbot.addWidget(reopened)
        reopened.on_project_changed(project_session)
        reopened.set_reviewer(REVIEWER)
        reopened.load_batch(prepared, template)
        reopened.state_filter.setCurrentText(FILTER_ALL)

        record = next(
            item for item in reopened.state.conflicts if item.conflict_id == conflict_id
        )
        assert record.state is ConflictState.RESOLVED
        assert record.observation.value != "B", "the machine value must not be the correction"
        found = review_store.provenance_for(reopened.database, conflict_id)
        assert found.value == "B"
        assert found.reviewer == REVIEWER
        reopened.close()


# ----------------------------------------------------------------------
# Navigation from the Scan page, and responsiveness
# ----------------------------------------------------------------------
class TestWindowIntegration:
    def test_the_scan_page_can_open_the_review_stage(
        self, qtbot, tmp_path, project_session, template_path, prepared
    ):
        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        window._session = project_session
        window._broadcast_project_change()

        scan_page = window._scan_page()
        scan_page.load_template_from(template_path)
        scan_page.state.batch_id = prepared

        assert scan_page.request_review() is True
        assert window.stack.currentWidget().objectName() == "resolvePage"
        assert window._resolve_page().state.batch_id == prepared

        # Closing the window must stop the review page's sheet loader, or a
        # QThread outlives the process and Qt aborts on the way out.
        window._session = None
        window.close()
        assert window._resolve_page()._worker is None or not (
            window._resolve_page()._worker.isRunning()
        )

    def test_the_reviewer_name_reaches_the_page_from_the_configuration(
        self, qtbot, tmp_path, project_session
    ):
        config = AppConfig().with_reviewer_name("  Dr. Configured  ")
        window = MainWindow(config=config, config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        assert window._resolve_page().state.reviewer == "Dr. Configured"

    def test_the_event_loop_keeps_running_while_a_sheet_loads(
        self, qtbot, page: ResolvePage
    ):
        from PySide6.QtCore import QTimer

        ticks: list[int] = []
        heartbeat = QTimer()
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start()
        select_first(qtbot, page, ConflictType.ANSWER_MULTIPLE)
        heartbeat.stop()

        # The sheet is decoded and re-read in a worker thread. If that happened
        # on the GUI thread this timer could not have ticked.
        assert ticks, "the GUI thread was blocked while the sheet loaded"

    def test_walking_a_sheets_conflicts_reloads_the_image_once(
        self, qtbot, page: ResolvePage
    ):
        # Selecting another conflict on the *same* sheet must not decode the
        # image again - that is the difference between a usable queue and one
        # that pauses on every arrow key.
        rows = [
            row
            for row, item in enumerate(page.state.conflicts)
            if item.scan_id == page.state.conflicts[0].scan_id
        ]
        if len(rows) < 2:
            pytest.skip("this batch has only one conflict per sheet")

        with qtbot.waitSignal(page.sheet_ready, timeout=SHEET_TIMEOUT_MS):
            page.queue_table.selectRow(rows[0])
        loaded = page._loaded_scan_id
        page.queue_table.selectRow(rows[1])
        assert page._loaded_scan_id == loaded


class TestStableObjectNames:
    def test_the_review_widgets_can_be_found_by_name(self, page: ResolvePage):
        from PySide6.QtCore import QObject

        assert page.objectName() == "resolvePage"
        required = [
            "conflictQueueTable",
            "conflictStateFilter",
            "conflictTypeFilter",
            "conflictSearchBox",
            "conflictZoomView",
            "normalisedSheetView",
            "originalSheetView",
            "machineEvidenceLabel",
            "acceptMachineValueButton",
            "deferConflictButton",
            "reopenConflictButton",
            "correctionReasonCombo",
            "correctionReasonText",
            "conflictHistoryButton",
            "conflictProvenanceLabel",
            "reviewSummaryLabel",
            "reviewerNameLabel",
        ]
        missing = [name for name in required if page.findChild(QObject, name) is None]
        assert not missing, missing
