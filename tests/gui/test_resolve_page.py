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
    FieldKind,
    ReasonCode,
    ReviewAction,
    ValueSource,
)
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.review.history_dialog import render_history
from omr_scanner.gui.review.lanes import BLANK_NOTE
from omr_scanner.gui.review.page import (
    BLANK_BUTTON_TEXT,
    BLANK_CHOICE,
    CHOICE_BUTTON_MIN_WIDTH,
    FILTER_ALL,
    FILTER_RESOLVED,
    ORIGINAL_TAB_INDEX,
    QUEUE_PANEL_WIDTH,
    ZOOM_TAB_INDEX,
    ResolvePage,
)
from omr_scanner.gui.scan.preview import LaneState
from omr_scanner.gui.theme import (
    CANDIDATE_CHOSEN,
    CANDIDATE_MACHINE,
    CANDIDATE_STATE_PROPERTY,
)
from omr_scanner.services import batch_store, review_store, save_template
from omr_scanner.services.batch_processor import process_batch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

SHEET_TIMEOUT_MS = 120_000
REVIEWER = "Dr. Rahman"

ISSUE_COLUMN = 2
STATE_COLUMN = 3
"""Queue columns, named so an assertion does not read as a bare index."""


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
    """A batch holding exactly the conflicts this stage is now for.

    One multiply-marked roll-number column and one duplicated identifier -
    plus, on the same sheet as the bad roll number, a double-marked answer
    that must never reach the queue.
    """
    scans = tmp_path / "scans"
    scans.mkdir(parents=True, exist_ok=True)

    ambiguous = sheet_marks("170503")
    # Two bad columns on one sheet, so that walking a sheet's conflicts is
    # exercised rather than skipped.
    ambiguous["roll_number"] = {
        **ambiguous["roll_number"],
        0: ["1", "7"],
        2: ["0", "5"],
    }
    ambiguous["questions_0"] = {**ambiguous["questions_0"], 0: ["B", "D"]}
    paths = []
    for name, marks in (
        ("ambiguous_id.png", ambiguous),
        ("dup_a.png", sheet_marks("170501")),
        ("dup_b.png", sheet_marks("170501")),
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
        # One compact line of counters, with the per-type breakdown beneath it
        # in smaller type - a workstation, not a dashboard.
        text = page.summary_label.text()
        assert "total" in text
        assert "unresolved" in text
        assert "resolved" in text
        assert page.summary_label.toolTip()
        assert page.summary_breakdown.text()

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

    def test_the_queue_holds_only_identification_conflicts(self, page: ResolvePage):
        # The sheet with the bad roll number also carries a double-marked
        # answer. It is in the recognition result and in the export; it is not
        # here, and no row of this queue is about a question.
        assert page.state.conflicts
        assert {item.conflict_type for item in page.state.conflicts} == {
            ConflictType.IDENTIFIER_MULTIPLE,
            ConflictType.IDENTIFIER_DUPLICATE,
        }
        assert all(
            item.field.kind is not FieldKind.QUESTION for item in page.state.conflicts
        )

    def test_the_type_filter_offers_no_answer_conflict(self, page: ResolvePage):
        offered = {
            page.type_filter.itemText(index)
            for index in range(page.type_filter.count())
        }
        assert ConflictType.IDENTIFIER_MULTIPLE.label in offered
        assert ConflictType.SET_CODE_MULTIPLE.label in offered
        assert ConflictType.ANSWER_MULTIPLE.label not in offered

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

    def test_previous_unresolved_walks_back(self, qtbot, page: ResolvePage):
        page.state_filter.setCurrentText(FILTER_ALL)
        select_first(qtbot, page)
        page.queue_table.selectRow(len(page.state.conflicts) - 1)
        assert page.select_previous_unresolved() is True
        assert page.current_conflict().state is ConflictState.OPEN

    def test_the_state_column_is_visible_without_scrolling(self, page: ResolvePage):
        # The defect: five equal default columns ran past the panel's edge and
        # put **State** off-screen behind a horizontal scrollbar. A state cue a
        # reviewer has to scroll to find is not a state cue.
        from PySide6.QtWidgets import QHeaderView

        table = page.queue_table
        table.resize(QUEUE_PANEL_WIDTH, 400)
        header = table.horizontalHeader()
        assert header.sectionResizeMode(STATE_COLUMN) == (
            QHeaderView.ResizeMode.ResizeToContents
        )

        total = sum(table.columnWidth(column) for column in range(table.columnCount()))
        assert total <= table.viewport().width() + 1, (
            f"the columns need {total}px in a {table.viewport().width()}px viewport"
        )

    def test_the_issue_column_reads_as_one_phrase(self, page: ResolvePage):
        # Two narrow columns each eliding half of "Student ID - position 5 /
        # Student ID multiple marks" told a reviewer neither half.
        assert page.queue_table.rowCount()
        conflict = next(
            item
            for item in page.state.conflicts
            if item.conflict_type is ConflictType.IDENTIFIER_MULTIPLE
        )
        row = page.state.conflicts.index(conflict)
        text = page.queue_table.item(row, ISSUE_COLUMN).text()
        assert conflict.field.describe() in text
        assert "·" in text
        # And the whole value is reachable where it is elided.
        assert page.queue_table.item(row, ISSUE_COLUMN).toolTip() == text

    def test_every_cell_carries_its_full_value_in_a_tooltip(self, page: ResolvePage):
        assert page.queue_table.rowCount()
        for row in range(page.queue_table.rowCount()):
            for column in range(page.queue_table.columnCount()):
                assert page.queue_table.item(row, column).toolTip()

    def test_every_row_says_its_state_in_words_and_a_glyph(self, page: ResolvePage):
        # Not by colour alone: the row tint is unreadable to a colour-blind
        # reviewer and gone in a printed screenshot.
        assert page.queue_table.rowCount()
        for row, conflict in enumerate(page.state.conflicts):
            cell = page.queue_table.item(row, STATE_COLUMN)
            assert conflict.state_label in cell.text()
            assert conflict.state_marker in cell.text()
            assert cell.toolTip()

    def test_a_reopened_row_is_named_as_reopened(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        page.undo_last_decision()

        page.state_filter.setCurrentText(FILTER_ALL)
        record = next(
            item for item in page.state.conflicts if item.conflict_id == conflict_id
        )
        assert record.state is ConflictState.OPEN
        assert record.state_label == "Reopened"


# ----------------------------------------------------------------------
# The review workspace
# ----------------------------------------------------------------------
class TestWorkspace:
    def test_selecting_a_conflict_loads_all_three_views(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.normalised_view.has_page is True
        assert page.original_view.has_page is True
        assert page.zoom_view.has_page is True

    def test_the_zoom_view_is_focused_on_the_disputed_field(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        # Magnified well past "fit the whole page", which is what makes the
        # bubbles legible enough to judge.
        assert page.zoom_view.zoom > page.normalised_view.zoom

    def test_only_the_disputed_group_is_ringed(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        conflict = page.current_conflict()
        drawn = page.zoom_view._overlay._bubbles
        assert drawn
        # Exactly one response group - here one roll-number column's ten
        # digits - not all five hundred bubbles on the sheet.
        assert all(item.zone_id == conflict.field.zone_id for item in drawn)
        assert len(drawn) <= 12

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
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.original_view._overlay._bubbles == ()
        assert page.original_view._overlay.show_bubbles is False

    def test_the_original_view_says_where_the_field_is(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        note = page.original_note.text()
        assert "never modified" in note
        assert "x=" in note and "y=" in note

    def test_the_evidence_panel_shows_measured_fill_scores(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        text = page.evidence_label.text()
        assert "Machine value" in text
        # Labelled as what it is. The engine measures coverage, not likelihood.
        assert "probability" not in text.lower()

    def test_the_sheet_progress_label_says_where_in_the_sheet_we_are(
        self, qtbot, page: ResolvePage
    ):
        # "Conflict 1 of 2 - 2 unresolved", not "this sheet has 2 conflicts":
        # a reviewer part-way through a sheet needs a figure that moves as they
        # work, and the old wording did not move at all.
        select_first(qtbot, page)
        text = page.sheet_progress_label.text()
        # Every number says what it counts, so the header and the batch summary
        # cannot read as contradicting each other.
        assert "on sheet" in text
        assert "left here" in text
        assert "in batch" in text
        assert page.sheet_progress_label.toolTip()

    def test_the_choice_buttons_come_from_the_template(
        self, qtbot, page: ResolvePage, template
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        labels = [button.text() for button in page._choice_buttons]
        zone = next(item for item in template.zones if item.id == "roll_number")
        # "Blank" reads as a choice among digits; "(blank)" reads as the
        # absence of one. The stored value is still the empty string.
        assert labels == [*zone.field.symbols, BLANK_BUTTON_TEXT]

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

    def test_the_choice_row_never_accumulates_widgets(
        self, qtbot, page: ResolvePage
    ):
        # The defect: the "not a value that can be corrected" note and the
        # trailing stretch were added on every refresh and removed on none, so
        # a reviewer walking thirty registration failures ended up with thirty
        # slivers of wrapped text reading "This is not a" across the panel, and
        # the value buttons of the next conflict squeezed to nothing.
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        first = page.choice_layout.count()
        for _ in range(5):
            page._refresh_choices(page.current_conflict())
        assert page.choice_layout.count() == first

    def test_revisiting_a_sheet_conflict_leaves_one_note(
        self, qtbot, project_session, template, tmp_path
    ):
        from PySide6.QtWidgets import QLabel

        spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
        review_page = ResolvePage(spec)
        qtbot.addWidget(review_page)
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

        for _ in range(6):
            review_page.queue_table.clearSelection()
            review_page.queue_table.selectRow(0)

        notes = [
            index
            for index in range(review_page.choice_layout.count())
            if isinstance(review_page.choice_layout.itemAt(index).widget(), QLabel)
        ]
        assert len(notes) == 1
        review_page.close()

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
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
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
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        machine_value = page.current_conflict().observation.value
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)

        assert page.correct("1") is True

        found = review_store.provenance_for(page.database, conflict_id)
        assert found.value == "1"
        assert found.machine_value == machine_value
        assert found.reviewer == REVIEWER
        assert found.reason == ReasonCode.DOMINANT_MARK.value

    def test_a_correction_without_a_reviewer_is_refused(
        self, qtbot, monkeypatch, page: ResolvePage
    ):

        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.set_reviewer("")
        shown: list[str] = []
        monkeypatch.setattr(
            "omr_scanner.gui.error_reporting.QMessageBox.warning",
            staticmethod(_recording_warning(shown)),
        )
        assert page.correct("1") is False
        # Nothing was written.
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_other_without_an_explanation_is_refused(
        self, qtbot, monkeypatch, page: ResolvePage
    ):

        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.OTHER.label)
        page.reason_text.setPlainText("   ")
        monkeypatch.setattr(
            "omr_scanner.gui.error_reporting.QMessageBox.warning",
            staticmethod(_accept_warning),
        )
        assert page.correct("1") is False
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
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")

        # Resolving removes it from the default "Unresolved" view, so a
        # reviewer coming back to change their mind has to widen the filter -
        # which is exactly what reopening is for.
        page.state_filter.setCurrentText(FILTER_ALL)
        assert page.select_conflict_by_id(conflict_id) is True
        assert page.reopen_conflict() is True

        assert page.select_conflict_by_id(conflict_id) is True
        page.reason_combo.setCurrentText(ReasonCode.MISCLASSIFICATION.label)
        page.correct("7")

        history = review_store.history_for(page.database, conflict_id)
        assert [item.action for item in history] == [
            ReviewAction.DETECTED,
            ReviewAction.CORRECTED,
            ReviewAction.REOPENED,
            ReviewAction.CORRECTED,
        ]
        assert history[1].new_value == "1"
        assert history[3].new_value == "7"
        assert review_store.provenance_for(page.database, conflict_id).value == "7"

    def test_the_queue_shows_the_new_state_after_a_decision(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.accept_machine()
        page.state_filter.setCurrentText(FILTER_ALL)
        record = next(
            item for item in page.state.conflicts if item.conflict_id == conflict_id
        )
        assert record.state is ConflictState.RESOLVED

    def test_reopen_is_disabled_until_something_has_been_decided(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
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
        page.type_filter.setCurrentText(ConflictType.IDENTIFIER_MULTIPLE.label)
        while page.state.conflicts:
            # The sheet is only re-read when the selection moves to a
            # different one, so waiting unconditionally would hang on the
            # second conflict of the same sheet.
            if page.state.conflicts[0].scan_id == page._loaded_scan_id:
                page.queue_table.selectRow(0)
            else:
                with qtbot.waitSignal(page.sheet_ready, timeout=SHEET_TIMEOUT_MS):
                    page.queue_table.selectRow(0)
            page.accept_machine()

        assert page.current_conflict() is None
        assert page.confirm_button.isEnabled() is False
        assert "Select a conflict" in page.machine_summary_label.text()


# ----------------------------------------------------------------------
# Machine / manual / effective
# ----------------------------------------------------------------------
class TestEffectiveValueIsSpelledOut:
    def test_an_untouched_conflict_names_all_three(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        machine, manual, effective = page.provenance_summary()
        assert machine == page.current_conflict().observation.value
        assert manual == "—"
        assert effective == machine

    def test_a_pending_choice_is_not_shown_as_a_result(
        self, qtbot, page: ResolvePage
    ):
        # The distinction §15 exists for: a value has been picked, nothing has
        # been written, and the strip must not claim otherwise.
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.choose_label("1") is True

        _, manual, effective = page.provenance_summary()
        assert manual == "1"
        assert effective == "Pending"
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_an_override_shows_the_machine_value_beside_the_decision(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        machine_value = page.current_conflict().observation.value
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")

        # Auto-advance has moved on, so come back to what was decided.
        page.state_filter.setCurrentText(FILTER_ALL)
        assert page.select_conflict_by_id(conflict_id) is True
        assert page.provenance_summary() == (machine_value, "1", "1")

    def test_a_manual_blank_reads_as_blank_and_not_as_absent(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.ERASED_RESPONSE.label)
        page.correct("")

        page.state_filter.setCurrentText(FILTER_ALL)
        assert page.select_conflict_by_id(conflict_id) is True
        _, manual, effective = page.provenance_summary()
        assert manual == "(blank)"
        assert effective == "(blank)"


# ----------------------------------------------------------------------
# Undo, redo and taking a sheet back
# ----------------------------------------------------------------------
class TestUndoThroughThePage:
    def test_the_curved_arrows_are_undo_and_redo(self, page: ResolvePage):
        # They were "previous conflict" and "next conflict" before this work,
        # which is why navigation now wears chevrons.
        assert page.undo_action.objectName() == "undoDecisionButton"
        assert page.redo_action.objectName() == "redoDecisionButton"
        assert page.undo_sheet_action.objectName() == "undoResolvedSheetButton"

    def test_undo_is_disabled_until_something_has_been_decided(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.undo_action.isEnabled() is False
        assert page.redo_action.isEnabled() is False
        assert page.undo_last_decision() is False

    def test_undo_reverses_the_persisted_decision(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        machine = page.current_conflict().observation.value
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        assert review_store.provenance_for(page.database, conflict_id).value == "1"

        assert page.undo_last_decision() is True

        # The database, not the label: an undo that only repainted the screen
        # would lose the correction on the next refresh and keep it in the
        # exported results.
        found = review_store.provenance_for(page.database, conflict_id)
        assert found.value == machine
        assert found.source is ValueSource.MACHINE
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_undo_restores_the_counters_immediately(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        before = page.summary_label.text()
        page.correct("1")
        assert page.summary_label.text() != before

        page.undo_last_decision()
        assert page.summary_label.text() == before

    def test_undo_follows_the_decision_wherever_it_was_made(
        self, qtbot, page: ResolvePage
    ):
        # With auto-advance on, the conflict just decided is not the one on
        # screen any more. Undo must still take back what was decided, and
        # show the reviewer where it happened.
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        assert page.current_conflict().conflict_id != conflict_id

        assert page.undo_last_decision() is True
        assert page.current_conflict().conflict_id == conflict_id

    def test_undo_keeps_the_reversed_decision_in_the_history(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        page.undo_last_decision()
        page.reason_combo.setCurrentText(ReasonCode.MISCLASSIFICATION.label)
        page.select_conflict_by_id(conflict_id)
        page.correct("7")

        assert [
            item.action for item in review_store.history_for(page.database, conflict_id)
        ] == [
            ReviewAction.DETECTED,
            ReviewAction.CORRECTED,
            ReviewAction.UNDONE,
            ReviewAction.CORRECTED,
        ]

    def test_undo_survives_a_fresh_page_on_the_same_project(
        self, qtbot, project_session, template, prepared, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        page.undo_last_decision()
        page.close()

        spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
        reopened = ResolvePage(spec)
        qtbot.addWidget(reopened)
        reopened.on_project_changed(project_session)
        reopened.set_reviewer(REVIEWER)
        reopened.load_batch(prepared, template)

        record = next(
            item for item in reopened.state.conflicts if item.conflict_id == conflict_id
        )
        assert record.state is ConflictState.OPEN
        assert record.state_label == "Reopened"
        reopened.close()

    def test_redo_makes_the_decision_again(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        page.undo_last_decision()
        assert page.redo_action.isEnabled() is True

        assert page.redo_last_decision() is True

        found = review_store.provenance_for(page.database, conflict_id)
        assert found.value == "1"
        assert found.source is ValueSource.HUMAN
        assert found.reason == ReasonCode.DOMINANT_MARK.value
        assert page.redo_action.isEnabled() is False

    def test_a_new_decision_clears_what_could_be_redone(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        page.undo_last_decision()
        assert page.state.redo

        page.correct("7")
        assert page.state.redo == []
        assert page.redo_action.isEnabled() is False


class TestSheetUndoThroughThePage:
    def resolve_a_whole_sheet(self, qtbot, page: ResolvePage) -> tuple[int, list[int]]:
        """Decide every conflict on the busiest sheet, then move on from it.

        The shape the feature exists for: an operator works one sheet to the
        end, the application carries them on to the next, and only then do they
        realise they were reading the wrong column.
        """
        counts: dict[int, int] = {}
        for item in page.state.conflicts:
            counts[item.scan_id] = counts.get(item.scan_id, 0) + 1
        scan_id = max(counts, key=lambda key: counts[key])
        decided: list[int] = []

        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        while True:
            row = next(
                (
                    index
                    for index, item in enumerate(page.state.conflicts)
                    if item.scan_id == scan_id
                ),
                None,
            )
            if row is None:
                break
            if page.state.conflicts[row].scan_id == page._loaded_scan_id:
                page.queue_table.selectRow(row)
            else:
                with qtbot.waitSignal(page.sheet_ready, timeout=SHEET_TIMEOUT_MS):
                    page.queue_table.selectRow(row)
            decided.append(page.current_conflict().conflict_id)
            assert page.correct("1") is True

        # Moving on is enough to end the session; nothing is decided on the
        # next sheet, because then *it* would be the last one finished.
        assert page.current_conflict() is not None
        assert page.current_conflict().scan_id != scan_id
        return scan_id, decided

    def test_a_finished_sheet_can_be_taken_back_whole(self, qtbot, page: ResolvePage):
        scan_id, decided = self.resolve_a_whole_sheet(qtbot, page)
        assert len(decided) >= 2, "this fixture should give one busy sheet"
        counts = review_store.count_conflicts_for_scan(
            page.database, page.state.batch_id, scan_id
        )
        assert counts.unresolved == 0

        assert page.undo_last_resolved_sheet() is True

        after = review_store.count_conflicts_for_scan(
            page.database, page.state.batch_id, scan_id
        )
        assert after.unresolved == len(decided)
        for conflict_id in decided:
            found = review_store.provenance_for(page.database, conflict_id)
            assert found.source is ValueSource.MACHINE
            assert review_store.get_conflict(page.database, conflict_id).state is (
                ConflictState.OPEN
            )

    def test_the_page_returns_to_the_sheet_it_restored(self, qtbot, page: ResolvePage):
        scan_id, _ = self.resolve_a_whole_sheet(qtbot, page)
        assert page.undo_last_resolved_sheet() is True
        assert page.current_conflict() is not None
        assert page.current_conflict().scan_id == scan_id

    def test_the_restored_sheets_history_records_every_reversal(
        self, qtbot, page: ResolvePage
    ):
        _, decided = self.resolve_a_whole_sheet(qtbot, page)
        page.undo_last_resolved_sheet()
        for conflict_id in decided:
            actions = [
                item.action
                for item in review_store.history_for(page.database, conflict_id)
            ]
            assert actions.count(ReviewAction.CORRECTED) == 1
            assert actions.count(ReviewAction.UNDONE) == 1

    def test_sheet_undo_is_disabled_when_no_sheet_has_been_finished(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.undo_sheet_action.isEnabled() is False
        assert page.undo_last_resolved_sheet() is False


# ----------------------------------------------------------------------
# Auto-advance
# ----------------------------------------------------------------------
class TestAutoAdvance:
    def test_deciding_moves_to_the_next_unresolved_conflict(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)

        assert page.correct("1") is True

        landed = page.current_conflict()
        assert landed is not None
        assert landed.conflict_id != conflict_id
        assert landed.state is ConflictState.OPEN

    def test_turning_it_off_leaves_the_selection_alone(self, qtbot, page: ResolvePage):
        page.set_auto_advance(False)
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")

        # The decided conflict has left the "Unresolved" queue, so what matters
        # is that nothing deliberately jumped past the next row.
        landed = page.current_conflict()
        page.set_auto_advance(True)
        assert landed is None or landed.state is not ConflictState.RESOLVED

    def test_deferring_does_not_skip_past_the_deferred_conflict(
        self, qtbot, page: ResolvePage
    ):
        # Deferring is "come back to this", so advancing past it would make the
        # thing the reviewer asked to see again the thing they cannot find.
        page.state_filter.setCurrentText(FILTER_ALL)
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.defer_conflict() is True
        assert page.current_conflict().conflict_id == conflict_id

    def test_advancing_does_not_record_a_second_event(
        self, qtbot, page: ResolvePage
    ):
        # Auto-advance selects a row; selecting is not deciding. A second
        # `resolution_recorded` here would double-count the decision in every
        # listener, and a second *audit* event would invent a decision nobody
        # made.
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        seen: list[int] = []
        page.resolution_recorded.connect(seen.append)

        page.correct("1")

        assert seen == [conflict_id]
        actions = [
            item.action for item in review_store.history_for(page.database, conflict_id)
        ]
        assert actions.count(ReviewAction.CORRECTED) == 1

    def test_the_toggle_reports_the_change_for_the_window_to_remember(
        self, qtbot, page: ResolvePage
    ):
        with qtbot.waitSignal(page.auto_advance_changed) as blocker:
            page.auto_advance_action.setChecked(False)
        assert blocker.args == [False]
        assert page.state.auto_advance is False
        page.auto_advance_action.setChecked(True)

    def test_adopting_the_stored_preference_does_not_echo_it_back(
        self, qtbot, page: ResolvePage
    ):
        seen: list[bool] = []
        page.auto_advance_changed.connect(seen.append)
        page.set_auto_advance(False)
        page.set_auto_advance(True)
        assert seen == [], "the page must not tell the window what the window told it"


# ----------------------------------------------------------------------
# Keyboard-first operation
# ----------------------------------------------------------------------
class TestKeyboard:
    def bound(self, page: ResolvePage) -> set[str]:
        return {item.key().toString() for item in page._shortcuts}

    def test_every_promised_shortcut_is_bound(self, page: ResolvePage):
        keys = self.bound(page)
        for expected in (
            "0", "1", "5", "9", "B", "D", "Return",
            "Ctrl+Z", "Ctrl+Y", "Ctrl+Shift+Z",
            "Shift+Return", "Ctrl+Return", "Left", "Right",
        ):
            assert expected in keys, expected

    def test_redo_and_sheet_undo_do_not_share_a_shortcut(self, page: ResolvePage):
        # Qt resolves an ambiguous shortcut by firing neither, so Ctrl+Shift+Z
        # must not also be Redo - which is what QKeySequence.StandardKey.Redo
        # would have made it on Windows.
        keys = [item.key().toString() for item in page._shortcuts]
        assert len(keys) == len(set(keys))

    def test_a_digit_picks_the_value_it_prints_and_enter_records_it(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)

        assert page.choose_label("1") is True
        # Picked, not written: the reviewer still has to see the ring land on
        # the bubble they meant before it becomes the value.
        assert review_store.provenance_for(page.database, conflict_id).source is (
            ValueSource.MACHINE
        )

        assert page.confirm_resolution() is True

        found = review_store.provenance_for(page.database, conflict_id)
        assert found.value == "1"
        assert found.reviewer == REVIEWER
        assert found.reason == ReasonCode.DOMINANT_MARK.value

    def test_the_blank_key_records_a_blank(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.ERASED_RESPONSE.label)

        assert page.choose_label(BLANK_CHOICE) is True
        assert page.confirm_resolution() is True

        found = review_store.provenance_for(page.database, conflict_id)
        assert found.value == ""
        assert found.source is ValueSource.HUMAN

    def test_a_symbol_this_sheet_does_not_print_does_nothing(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        labels = page._offered_labels(page.current_conflict())
        missing = next(str(digit) for digit in range(10) if str(digit) not in labels) \
            if len(labels) < 10 else None
        if missing is None:
            pytest.skip("this template prints every digit")
        assert page.choose_label(missing) is False
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_typing_in_the_search_box_decides_nothing(self, qtbot, page: ResolvePage):
        # The defect this exists for: a reviewer searching for roll number
        # 170501 silently recording 1, 7, 0, 5, 0 and 1 as somebody's corrected
        # student ID.
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.show()
        page.search_box.setFocus()
        # `focusWidget`, not `hasFocus`: the latter is false whenever the window
        # is inactive, which it is under a headless platform plugin - and the
        # guard itself reads `focusWidget`, so this asserts the real condition.
        assert page.focusWidget() is page.search_box

        assert page.choose_label("1") is False
        assert page.accept_machine() is False
        assert page.defer_conflict() is False
        assert page.undo_last_decision() is False
        assert page.undo_last_resolved_sheet() is False
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_typing_in_the_reason_box_decides_nothing(self, qtbot, page: ResolvePage):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.show()
        page.reason_text.setFocus()
        assert page.focusWidget() is page.reason_text
        assert page.choose_label("1") is False
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_a_real_keypress_reaches_the_decision(self, qtbot, page: ResolvePage):
        # The binding table above says what is bound; this says a real key
        # event actually gets through the queue table - which eats digits for
        # its own type-ahead search unless the shortcut takes them first.
        #
        # Skipped where the platform plugin never activates a window, because
        # Qt does not deliver shortcuts to an inactive one and the test would
        # then be asserting the platform rather than the page.
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.show()
        page.activateWindow()
        page.raise_()
        page.queue_table.setFocus()
        QApplication.processEvents()
        if not page.isActiveWindow():
            pytest.skip("this Qt platform plugin never activates a window")

        QTest.keyClick(page.queue_table, Qt.Key.Key_1)
        assert page.state.pending == "1"
        QTest.keyClick(page.queue_table, Qt.Key.Key_Return)

        assert review_store.provenance_for(page.database, conflict_id).value == "1"


# ----------------------------------------------------------------------
# History
# ----------------------------------------------------------------------
class TestHistory:
    def test_the_history_view_renders_real_persisted_events(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")

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
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
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
        assert record.observation.value != "1", "the machine value must not be the correction"
        found = review_store.provenance_for(reopened.database, conflict_id)
        assert found.value == "1"
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
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
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


# ----------------------------------------------------------------------
# The lane overlay
# ----------------------------------------------------------------------
def lanes_of(page: ResolvePage):
    """The lane rectangles the zoomed view is currently drawing."""
    return page.zoom_view._overlay._lanes


class TestLaneOverlay:
    def test_an_unresolved_position_is_outlined_as_a_whole_lane(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        drawn = lanes_of(page)
        assert drawn
        assert any(item.active for item in drawn)
        assert all(item.state is LaneState.UNRESOLVED for item in drawn)

    def test_the_lane_spans_the_whole_bubble_stack(self, qtbot, page: ResolvePage):
        # The rectangle is the *position*, not the bubble the engine nearly
        # chose: every bubble of the disputed group has to fall inside it.
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        conflict = page.current_conflict()
        bubbles = page._conflict_bubbles(conflict)
        lane = next(item for item in lanes_of(page) if item.active)

        assert len(bubbles) >= 10, "a roll-number column is a stack of digits"
        for bubble in bubbles:
            assert lane.x <= bubble.x <= lane.x + lane.width
            assert lane.y <= bubble.y <= lane.y + lane.height
        # Taller than it is wide - it is a column of ten, drawn as one.
        assert lane.height > lane.width

    def test_the_question_mark_glyph_is_gone(self, qtbot, page: ResolvePage):
        # The `?` said where the machine's doubt landed. The lane says which
        # part of the field a person has to look at, which is the question the
        # reviewer is actually answering.
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        for view in (page.zoom_view, page.normalised_view):
            assert view._overlay.show_status_symbols is False

    def test_the_scan_page_keeps_its_status_glyphs(self, qtbot):
        # Turning the glyphs off is a Resolve-stage decision, not a change to
        # the overlay everything else shares.
        from omr_scanner.gui.scan.preview import ScanPreviewView

        view = ScanPreviewView()
        qtbot.addWidget(view)
        assert view._overlay.show_status_symbols is True

    def test_every_unresolved_position_on_the_sheet_gets_a_lane(
        self, qtbot, page: ResolvePage
    ):
        # The prepared sheet has two bad roll-number columns. A reviewer
        # deciding the first has to be able to see that the second is waiting.
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        conflict = page.current_conflict()
        on_sheet = [
            item
            for item in page.state.sheet_conflicts
            if item.scan_id == conflict.scan_id
        ]
        assert len(on_sheet) >= 2
        drawn = lanes_of(page)
        assert len(drawn) == len(on_sheet)
        # Distinct rectangles, not two copies of the same one.
        assert len({(item.x, item.y) for item in drawn}) == len(drawn)
        assert sum(1 for item in drawn if item.active) == 1

    def test_a_manual_decision_turns_its_lane_red_and_rings_the_choice(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        before = next(item for item in lanes_of(page) if item.active)

        assert page.correct("1") is True

        manual = [item for item in lanes_of(page) if item.state is LaneState.MANUAL]
        assert len(manual) == 1
        lane = manual[0]
        # The same rectangle, restated - not a new one somewhere else.
        assert (lane.x, lane.y) == (before.x, before.y)
        # ...and the chosen bubble is identifiable inside it.
        assert lane.has_choice is True
        assert lane.choice is not None
        assert lane.x <= lane.choice.x <= lane.x + lane.width
        assert lane.y <= lane.choice.y <= lane.y + lane.height
        assert lane.choice.label == "1"
        assert lane.note == ""

    def test_the_overlay_updates_without_reloading_the_sheet(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        loaded = page._loaded_scan_id

        page.correct("1")

        assert page._loaded_scan_id == loaded, "the sheet was decoded again"
        assert any(item.state is LaneState.MANUAL for item in lanes_of(page))

    def test_a_manual_blank_is_captioned_rather_than_ringed(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.ERASED_RESPONSE.label)

        assert page.correct("") is True

        lane = next(item for item in lanes_of(page) if item.state is LaneState.MANUAL)
        # No bubble stands for "blank", so the caption is what distinguishes
        # "I checked; it is empty" from "nobody has checked".
        assert lane.has_choice is False
        assert lane.note == BLANK_NOTE

    def test_undoing_returns_the_lane_to_unresolved(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        assert any(item.state is LaneState.MANUAL for item in lanes_of(page))

        assert page.undo_last_decision() is True

        drawn = lanes_of(page)
        assert drawn
        assert all(item.state is LaneState.UNRESOLVED for item in drawn)
        assert all(item.has_choice is False for item in drawn)
        assert all(item.note == "" for item in drawn)

    def test_the_lane_geometry_does_not_move_when_the_view_zooms(
        self, qtbot, page: ResolvePage
    ):
        # Lanes are canonical-page coordinates, so zooming re-renders them and
        # never re-computes them. A lane that drifted with the zoom would be
        # pointing at a different position every time the reviewer looked.
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        before = lanes_of(page)
        page.zoom_view.set_zoom(page.zoom_view.zoom * 3.0)
        page.zoom_view.zoom_out()
        assert lanes_of(page) == before

    def test_both_overlay_views_show_the_same_lanes(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.normalised_view._overlay._lanes == page.zoom_view._overlay._lanes

    def test_switching_preview_mode_keeps_the_conflict_and_its_overlay(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.correct("1")
        page.state_filter.setCurrentText(FILTER_ALL)
        chosen = page.current_conflict().conflict_id

        for index in (1, 2, 0):
            page.view_tabs.setCurrentIndex(index)
            assert page.current_conflict().conflict_id == chosen

        assert any(item.state is LaneState.MANUAL for item in lanes_of(page))
        assert any(
            item.state is LaneState.MANUAL
            for item in page.normalised_view._overlay._lanes
        )

    def test_a_sheet_level_conflict_gets_no_rectangle(
        self, qtbot, project_session, template, tmp_path
    ):
        # "This JPEG will not decode" is not a place on a page. Drawing a
        # confident outline somewhere for it would be an invention.
        from omr_scanner.gui.review.lanes import lane_for

        spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
        review_page = ResolvePage(spec)
        qtbot.addWidget(review_page)
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

        conflict = review_page.current_conflict()
        assert conflict.conflict_type.scope.value == "sheet"
        assert lane_for(report.processed[0].result, template, conflict, None) is None
        assert lanes_of(review_page) == ()
        review_page.close()


class TestEmptyViewsExplainThemselves:
    def test_a_sheet_that_never_registered_says_so_in_the_view(
        self, qtbot, project_session, template, tmp_path
    ):
        # The defect: a registration failure left the Normalised sheet and
        # Zoomed field tabs as unexplained grey rectangles. There is no
        # rectified page for such a sheet *by definition* - that is the finding
        # itself - and a reviewer must be told that rather than left wondering
        # whether the application is still loading.
        spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
        review_page = ResolvePage(spec)
        qtbot.addWidget(review_page)
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
        with qtbot.waitSignal(review_page.sheet_ready, timeout=SHEET_TIMEOUT_MS):
            review_page.queue_table.selectRow(0)

        for view in (review_page.normalised_view, review_page.zoom_view):
            assert view.has_page is False
            assert view._placeholder.text()

        # The only tab with anything on it is selected, and the two that are
        # empty by definition of the conflict say so rather than inviting a
        # click through two blank panes.
        assert review_page.view_tabs.currentIndex() == ORIGINAL_TAB_INDEX
        assert review_page.view_tabs.isTabEnabled(ZOOM_TAB_INDEX) is False
        assert review_page.view_tabs.tabToolTip(ZOOM_TAB_INDEX)
        # ...and there is no digit selector for a page that was never measured.
        assert review_page._choice_buttons == []
        review_page.close()

    def test_a_registration_failure_does_not_pin_later_sheets_to_it(
        self, qtbot, project_session, template, tmp_path, prepared, page: ResolvePage
    ):
        # The defect: forcing the tab without restoring it meant one bad sheet
        # in a queue moved every later sheet to the original scan, so the
        # zoomed field - the view this stage is built around - was never seen
        # again.
        page.view_tabs.setCurrentIndex(ORIGINAL_TAB_INDEX)
        page._offer_the_useful_tab(registered=False)
        assert page.view_tabs.currentIndex() == ORIGINAL_TAB_INDEX

        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)

        assert page.view_tabs.isTabEnabled(ZOOM_TAB_INDEX) is True
        assert page.view_tabs.currentIndex() == ORIGINAL_TAB_INDEX, (
            "a tab the reviewer chose should be kept"
        )

    def test_a_forced_tab_is_given_back(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.view_tabs.currentIndex() == ZOOM_TAB_INDEX

        page._offer_the_useful_tab(registered=False)
        assert page.view_tabs.currentIndex() == ORIGINAL_TAB_INDEX

        page._offer_the_useful_tab(registered=True)
        assert page.view_tabs.currentIndex() == ZOOM_TAB_INDEX

    def test_a_placeholder_never_covers_a_page(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.normalised_view.set_placeholder("should not be shown")
        assert page.normalised_view.has_page is True
        assert page.normalised_view._placeholder.isVisible() is False


class TestFraming:
    def test_the_zoomed_field_frames_the_disputed_position(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        conflict = page.current_conflict()
        rect = page.zoom_view.focus_rect
        assert rect is not None
        # Every bubble of the disputed group is inside the framed region...
        for bubble in page._conflict_bubbles(conflict):
            assert rect.left() <= bubble.x <= rect.right()
            assert rect.top() <= bubble.y <= rect.bottom()
        # ...and so is the whole page nowhere near it: this is a region, not a
        # fit-to-window in disguise.
        assert rect.width() < page.state.bundle.result.canonical_width

    def test_the_framing_keeps_neighbouring_positions_in_view(
        self, qtbot, page: ResolvePage
    ):
        # A faint mark is judged against the columns beside it, filled by the
        # same candidate in the same pencil.
        from omr_scanner.gui.review.lanes import context_bubbles, group_bubbles

        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        conflict = page.current_conflict()
        result = page.state.bundle.result
        own = group_bubbles(result, page.state.template, conflict)
        context = context_bubbles(result, page.state.template, conflict)
        assert len(context) > len(own)
        assert set(own).issubset(set(context))

    def test_switching_conflict_reframes(self, qtbot, page: ResolvePage):
        # No pan or zoom may survive from the position before.
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        first = page.zoom_view.focus_rect
        rows = [
            index
            for index, item in enumerate(page.state.conflicts)
            if item.conflict_type is ConflictType.IDENTIFIER_MULTIPLE
        ]
        if len(rows) < 2:
            pytest.skip("this batch has one disputed position per sheet")
        page.queue_table.selectRow(rows[1])
        second = page.zoom_view.focus_rect
        assert second is not None
        assert second != first

    def test_a_manual_zoom_takes_the_framing_over(self, qtbot, page: ResolvePage):
        # Once the reviewer has moved the view it is theirs; a resize must not
        # snatch it back.
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.zoom_view.focus_rect is not None
        before = page.zoom_view.zoom

        page.zoom_view.zoom_in()

        assert page.zoom_view.focus_rect is None
        assert page.zoom_view.zoom > before
        page.zoom_view.resize(page.zoom_view.width() + 40, page.zoom_view.height())
        assert page.zoom_view.focus_rect is None

    def test_a_tall_region_is_widened_to_fill_the_pane(self, qtbot):
        # The reason the zoomed field used to sit in a sea of blank canvas: a
        # roll-number position is tall and narrow, the pane is wide and short,
        # and fitting one inside the other leaves two thirds of the width
        # empty. The region is grown to the pane's shape instead, and the spare
        # width is spent on more of the sheet.
        from PySide6.QtCore import QRectF

        from omr_scanner.gui.scan.preview import ScanPreviewView

        tall = QRectF(100, 100, 50, 400)
        widened = ScanPreviewView._widened_to_viewport(tall, 800, 400)
        assert widened.height() == tall.height()
        assert widened.width() > tall.width()
        assert widened.center() == tall.center()
        assert widened.width() / widened.height() == pytest.approx(800 / 400)

    def test_widening_never_shrinks_the_region(self):
        from PySide6.QtCore import QRectF

        from omr_scanner.gui.scan.preview import ScanPreviewView

        wide = QRectF(0, 0, 400, 50)
        widened = ScanPreviewView._widened_to_viewport(wide, 400, 400)
        assert widened.width() >= wide.width()
        assert widened.height() >= wide.height()


class TestCandidateButtonStates:
    def machine_buttons(self, page: ResolvePage) -> list[str]:
        return [
            item.text()
            for item in page._choice_buttons
            if item.property(CANDIDATE_STATE_PROPERTY) == CANDIDATE_MACHINE
        ]

    def chosen_buttons(self, page: ResolvePage) -> list[str]:
        return [
            item.text()
            for item in page._choice_buttons
            if item.property(CANDIDATE_STATE_PROPERTY) == CANDIDATE_CHOSEN
        ]

    def test_the_symbols_the_machine_read_are_marked(self, qtbot, page: ResolvePage):
        # Eleven identical buttons made the reviewer carry "it was 1 and 7" in
        # their head from the panel beside them.
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        conflict = page.current_conflict()
        expected = sorted(conflict.observation.value.split("-"))
        assert len(expected) == 2, "this fixture stages a double mark"
        assert sorted(self.machine_buttons(page)) == expected
        assert self.chosen_buttons(page) == []

    def test_a_pick_is_marked_differently_from_a_machine_reading(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        machine = sorted(self.machine_buttons(page))

        page.choose_label("3")

        assert self.chosen_buttons(page) == ["3"]
        # Choosing something else leaves the machine's own readings marked.
        assert sorted(self.machine_buttons(page)) == machine

    def test_picking_a_machine_reading_becomes_the_pick(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        first = sorted(self.machine_buttons(page))[0]

        page.choose_label(first)

        assert self.chosen_buttons(page) == [first]
        assert first not in self.machine_buttons(page)

    def test_no_pick_survives_a_change_of_conflict(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.choose_label("3")
        assert page.state.pending == "3"

        page.select_next()

        assert page.state.pending is None
        assert self.chosen_buttons(page) == []

    def test_a_pick_shows_on_the_sheet_before_it_is_committed(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.choose_label("3")

        lane = next(item for item in lanes_of(page) if item.active)
        # Manual colour, unresolved line style: what was picked, without
        # claiming it has been stored.
        assert lane.state is LaneState.PENDING
        assert lane.choice is not None
        assert lane.choice.label == "3"

    def test_the_machine_marks_are_ringed_on_the_sheet(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        conflict = page.current_conflict()
        lane = next(item for item in lanes_of(page) if item.active)
        assert {mark.label for mark in lane.machine_marks} == set(
            conflict.observation.value.split("-")
        )


class TestTheMachineReadingIsNotOfferedWhenItIsNotAValue:
    def test_a_double_mark_cannot_be_confirmed_as_a_digit(
        self, qtbot, page: ResolvePage
    ):
        # "0-5" is not a value one printed roll-number position can hold, and
        # storing it would substitute into the identifier and produce a
        # candidate ID no roster will match - from a button that said the
        # machine was right.
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert "-" in page.current_conflict().observation.value

        assert page.confirm_button.isEnabled() is False
        assert "Choose a value" in page.confirm_button.text()
        assert page.confirm_resolution() is False
        assert review_store.get_conflict(page.database, conflict_id).state is (
            ConflictState.OPEN
        )

    def test_choosing_a_value_makes_the_action_available(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.choose_label("3")
        assert page.confirm_button.isEnabled() is True
        assert "3" in page.confirm_button.text()

    def test_a_reading_the_field_can_hold_is_still_confirmable(
        self, qtbot, page: ResolvePage
    ):
        # A duplicate student ID is a perfectly legible ID that two sheets
        # share, so the machine's reading *is* a value and confirming it is a
        # real decision.
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_DUPLICATE)
        assert page.confirm_button.isEnabled() is True
        assert "machine" in page.confirm_button.text().lower()

        assert page.confirm_resolution() is True
        found = review_store.provenance_for(page.database, conflict_id)
        assert found.source is ValueSource.HUMAN

    def test_a_decided_conflict_offers_reopen_instead_of_confirm(
        self, qtbot, page: ResolvePage
    ):
        # Nothing to confirm once it is decided, and a button reading "Choose a
        # value first" there tells the operator to fix something not broken.
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.choose_label("1")
        page.confirm_resolution()

        page.state_filter.setCurrentText(FILTER_ALL)
        assert page.select_conflict_by_id(conflict_id) is True
        assert page.confirm_button.isVisibleTo(page) is False
        assert page.reopen_button.isVisibleTo(page) is True

    def test_picking_again_on_a_decided_conflict_offers_to_commit(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        page.reason_combo.setCurrentText(ReasonCode.DOMINANT_MARK.label)
        page.choose_label("1")
        page.confirm_resolution()
        page.state_filter.setCurrentText(FILTER_ALL)
        page.select_conflict_by_id(conflict_id)

        assert page.choose_label("3") is True

        assert page.confirm_button.isVisibleTo(page) is True
        assert "3" in page.confirm_button.text()
        assert page.confirm_resolution() is True
        assert review_store.provenance_for(page.database, conflict_id).value == "3"

    def test_a_sheet_level_conflict_is_acknowledged_not_confirmed(
        self, qtbot, project_session, template, tmp_path
    ):
        # A file that will not decode is not a value, so "confirm the machine
        # reading" would be confirming nothing.
        spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
        review_page = ResolvePage(spec)
        qtbot.addWidget(review_page)
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

        assert review_page.confirm_button.text() == "Acknowledge"
        assert review_page.confirm_button.isEnabled() is True
        review_page.close()

    def test_a_duplicate_is_not_given_digit_buttons(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_DUPLICATE)
        assert page._choice_buttons == []
        assert page.free_value_row.isVisibleTo(page) is True
        # And the panel says how many other sheets are involved.
        assert "Also on" in page.machine_summary_label.text()


class TestMachineObservationIsCompact:
    def test_it_states_the_position_the_issue_and_the_marks(
        self, qtbot, page: ResolvePage
    ):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        text = page.machine_summary_label.text()
        conflict = page.current_conflict()
        assert conflict.field.describe() in text
        assert "Issue" in text and "Detected" in text and "Sheet" in text
        # "1 and 7", not "1-7": at a glance a hyphen is as easily a range or a
        # minus sign.
        assert " and " in text

    def test_the_long_diagnostics_are_behind_details(self, qtbot, page: ResolvePage):
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        assert page.evidence_scroll.isVisibleTo(page) is False
        assert page.details_button.isChecked() is False

        page.details_button.setChecked(True)

        assert page.evidence_scroll.isVisibleTo(page) is True
        assert "fill score" in page.evidence_label.text().lower()

    def test_the_stored_value_is_not_rewritten_for_display(
        self, qtbot, page: ResolvePage
    ):
        conflict_id = select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        record = review_store.get_conflict(page.database, conflict_id)
        assert "-" in record.observation.value
        # The strip shows the machine value exactly as it is stored.
        assert page.provenance_summary()[0] == record.observation.value


class TestSpaceDistribution:
    def ratios(self, splitter) -> list[int]:
        sizes = splitter.sizes()
        return [round(100 * size / sum(sizes)) for size in sizes]

    @pytest.mark.parametrize(
        ("width", "height"), [(1366, 768), (1600, 900), (1920, 1080)]
    )
    def test_the_designed_proportions_hold_at_every_size(
        self, qtbot, page: ResolvePage, width: int, height: int
    ):
        page.resize(width, height)
        page.show()
        qtbot.waitExposed(page)

        queue, workspace = self.ratios(page.main_splitter)
        assert 27 <= queue <= 32, f"queue is {queue}% at {width}x{height}"
        assert 68 <= workspace <= 73

        preview, resolution = self.ratios(page.workspace_splitter)
        assert 67 <= preview <= 73, f"preview is {preview}% at {width}x{height}"
        assert 27 <= resolution <= 33

    def test_the_operators_own_split_is_not_overridden(
        self, qtbot, page: ResolvePage
    ):
        # Once a reviewer has dragged a splitter the sizes are theirs, and a
        # window resize must not take them back.
        page.resize(1600, 900)
        page.show()
        qtbot.waitExposed(page)
        assert page._splitters_adjusted is False

        page.main_splitter.setSizes([900, 700])
        page.main_splitter.splitterMoved.emit(900, 1)
        assert page._splitters_adjusted is True
        theirs = page.main_splitter.sizes()

        page._apply_split_ratios()

        assert page.main_splitter.sizes() == theirs

    def test_neither_pane_can_be_collapsed(self, page: ResolvePage):
        assert page.main_splitter.childrenCollapsible() is False
        assert page.workspace_splitter.childrenCollapsible() is False

    def test_the_machine_panel_is_the_smaller_half(self, qtbot, page: ResolvePage):
        # 37/63, not 50/50: four facts against eleven buttons, a reason, a
        # note, a comparison strip and the commit.
        page.resize(1600, 900)
        page.show()
        qtbot.waitExposed(page)
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)
        machine = page.machine_summary_label.parentWidget().width()
        decision = page.choice_row.parentWidget().width()
        assert machine < decision

    @pytest.mark.parametrize(
        ("width", "height"), [(1366, 768), (1920, 1080)]
    )
    def test_nothing_is_clipped_at_a_supported_size(
        self, qtbot, page: ResolvePage, width: int, height: int
    ):
        page.resize(width, height)
        page.show()
        qtbot.waitExposed(page)
        select_first(qtbot, page, ConflictType.IDENTIFIER_MULTIPLE)

        for widget in (page.confirm_button, page.defer_button, page.reason_combo):
            assert widget.width() >= widget.minimumSizeHint().width(), widget
            assert widget.height() >= widget.minimumSizeHint().height(), widget
        # Every value button is reachable, not squeezed to nothing.
        for button in page._choice_buttons:
            assert button.width() >= CHOICE_BUTTON_MIN_WIDTH - 1
        # And the queue never needs a horizontal scrollbar.
        table = page.queue_table
        total = sum(table.columnWidth(column) for column in range(table.columnCount()))
        assert total <= table.viewport().width() + 1


class TestLaneRendering:
    def test_an_unresolved_lane_paints_its_amber_outline(self, qtbot):
        # Rendered, not merely configured: the overlay is the one part of this
        # stage whose whole job is to be looked at.
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QStyleOptionGraphicsItem

        from omr_scanner.gui.scan.preview import (
            LANE_MANUAL_COLOR,
            LANE_UNRESOLVED_COLOR,
            FieldLane,
            LaneState,
            OverlayItem,
        )

        def render(state: LaneState) -> set[tuple[int, int, int]]:
            image = QImage(120, 200, QImage.Format.Format_RGB32)
            image.fill(0xFFFFFFFF)
            overlay = OverlayItem()
            overlay.set_page_size(120, 200)
            overlay.set_lanes(
                [FieldLane(x=20, y=20, width=60, height=150, state=state, active=True)]
            )
            painter = QPainter(image)
            overlay.paint(painter, QStyleOptionGraphicsItem(), None)
            painter.end()
            assert QRectF(0, 0, 120, 200) == overlay.boundingRect()
            return {
                (
                    image.pixelColor(x, y).red(),
                    image.pixelColor(x, y).green(),
                    image.pixelColor(x, y).blue(),
                )
                for x in range(120)
                for y in range(200)
            }

        unresolved = render(LaneState.UNRESOLVED)
        manual = render(LaneState.MANUAL)
        amber = (
            LANE_UNRESOLVED_COLOR.red(),
            LANE_UNRESOLVED_COLOR.green(),
            LANE_UNRESOLVED_COLOR.blue(),
        )
        red = (
            LANE_MANUAL_COLOR.red(),
            LANE_MANUAL_COLOR.green(),
            LANE_MANUAL_COLOR.blue(),
        )
        assert amber in unresolved
        assert red in manual
        assert red not in unresolved


class TestToolbar:
    def test_every_icon_explains_itself(self, page: ResolvePage):
        # The toolbar is icon-only; an icon whose meaning has to be guessed is
        # an icon an operator finds out about by pressing it, on a stage where
        # pressing things changes what a script is worth.
        from PySide6.QtWidgets import QToolBar

        toolbar = page.findChild(QToolBar, "reviewToolbar")
        # Actions carrying an icon are the icon-only buttons; the rest are the
        # embedded widgets (the History button, the spacer, the counter), which
        # carry their own text.
        actions = [
            item
            for item in toolbar.actions()
            if not item.isSeparator() and not item.icon().isNull()
        ]
        assert len(actions) >= 10
        missing = [item.text() for item in actions if not item.toolTip()]
        assert not missing, missing
        assert page.history_button.toolTip()

    def test_the_groups_are_separated(self, page: ResolvePage):
        # Reversal / navigation / view are three different kinds of thing.
        from PySide6.QtWidgets import QToolBar

        toolbar = page.findChild(QToolBar, "reviewToolbar")
        assert sum(item.isSeparator() for item in toolbar.actions()) >= 3

    def test_the_undo_arrows_are_undo_and_redo(self, page: ResolvePage):
        from PySide6.QtWidgets import QToolBar

        toolbar = page.findChild(QToolBar, "reviewToolbar")
        names = [item.objectName() for item in toolbar.actions() if not item.isSeparator()]
        assert names[:3] == [
            "undoDecisionButton",
            "redoDecisionButton",
            "undoResolvedSheetButton",
        ]


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
            "deferConflictButton",
            "reopenConflictButton",
            "correctionReasonCombo",
            "correctionReasonText",
            "conflictHistoryButton",
            "conflictProvenanceStrip",
            "provenanceMachineValue",
            "provenanceManualValue",
            "provenanceEffectiveValue",
            "machineSummaryLabel",
            "machineDetailsToggle",
            "confirmResolutionButton",
            "reviewSummaryLabel",
            "resolveOperatorBadge",
            "resolveMainSplitter",
            "resolveWorkspaceSplitter",
            "sheetConflictProgressLabel",
        ]
        missing = [name for name in required if page.findChild(QObject, name) is None]
        assert not missing, missing
