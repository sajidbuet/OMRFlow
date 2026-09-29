"""Reject & Rescan through the real widgets: Resolve, Scan, Reports, the window.

Scope:
    Real rendered sheets, read by the real recognition engine through the Scan
    stage; the Resolve stage's Reject / Rescan action, its *Rejected / Rescan*
    view, the case panel, next/previous navigation in that view, the Scan
    stage's rescan import and CSV export, the Reports stage's *Export
    incomplete results* question and the window's *Purge Rejects*. Every
    assertion that matters is made against the database, not a label.

Why every wait is on a signal:
    Recognition and image loading run in real threads; nothing here sleeps.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.config import AppConfig
from omr_scanner.database.models import BatchScan
from omr_scanner.domain.reporting import ReadinessIssue, ReadinessIssueKind, ReadinessReport
from omr_scanner.domain.scan_lifecycle import LifecycleState, PurgeMode, RejectionReason
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.reports.page import ReportsPage, SetRow
from omr_scanner.gui.review.page import FILTER_OPEN, FILTER_RESCAN, ResolvePage
from omr_scanner.gui.review.rescan import RejectScanDialog
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services import review_store, save_template, scan_lifecycle

if TYPE_CHECKING:  # pragma: no cover - typing only

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

TIMEOUT_MS = 120_000
REVIEWER = "Dr. Rahman"
STATE_COLUMN = 3


def marks(roll: str, *, double_first: bool = False) -> dict:
    found = {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }
    if double_first:
        found["roll_number"] = {**found["roll_number"], 0: ["1", "7"]}
    return found


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def template_path(project_session: ProjectSession, template) -> Path:
    path = project_session.project.layout.templates_dir / "sheet.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return save_template(template, path)


@pytest.fixture
def write_sheet(tmp_path: Path, template):
    folder = tmp_path / "scanner"
    folder.mkdir()

    def write(name: str, roll: str, *, double_first: bool = False) -> Path:
        path = folder / name
        image = render_marked_sheet(template, marks(roll, double_first=double_first))
        cv2.imwrite(str(path), image)
        return path

    return write


@pytest.fixture
def scan_page(qtbot, project_session: ProjectSession, template_path: Path) -> ScanPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    assert page.load_template_from(template_path) is True
    return page


@pytest.fixture
def processed(qtbot, scan_page: ScanPage, write_sheet):
    """A batch of three read sheets: one with a disputed Student ID, two clean."""
    paths = [
        write_sheet("damaged.png", "170503", double_first=True),
        write_sheet("clean_a.png", "170504"),
        write_sheet("clean_b.png", "170505"),
    ]
    scan_page.add_scan_paths(paths)
    with qtbot.waitSignal(scan_page.batch_finished, timeout=TIMEOUT_MS):
        assert scan_page.process_all() is True
    assert scan_page.state.batch_id is not None
    return scan_page.state.batch_id


@pytest.fixture
def resolve(qtbot, project_session: ProjectSession, template, processed):
    spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
    page = ResolvePage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    page.set_reviewer(REVIEWER)
    assert page.load_batch(processed, template) is True
    yield page
    page.close()


def select_first_conflict(qtbot, page: ResolvePage) -> None:
    with qtbot.waitSignal(page.sheet_ready, timeout=TIMEOUT_MS):
        page.queue_table.selectRow(0)


def scan_id_of(project_session, batch_id: str, name: str) -> int:
    from omr_scanner.services import batch_store

    return next(
        scan_id
        for path, scan_id in batch_store.scan_ids_by_path(
            project_session.database, batch_id
        ).items()
        if path.name == name
    )


def open_rescan_view(qtbot, page: ResolvePage) -> None:
    with qtbot.waitSignal(page.sheet_ready, timeout=TIMEOUT_MS):
        page.state_filter.setCurrentText(FILTER_RESCAN)


# ----------------------------------------------------------------------
class TestRejectingOnResolve:
    def test_the_damaged_sheet_is_in_the_queue(self, resolve: ResolvePage):
        assert {item.scan_name for item in resolve.state.conflicts} == {"damaged.png"}

    def test_reject_removes_the_sheet_from_the_working_queue(
        self, qtbot, resolve: ResolvePage, project_session, processed
    ):
        select_first_conflict(qtbot, resolve)
        with qtbot.waitSignal(resolve.lifecycle_changed, timeout=TIMEOUT_MS):
            assert resolve.reject_current_sheet(
                RejectionReason.FOLDED, note="Folded", declared_candidate_id="170503"
            )
        assert resolve.state.conflicts == []
        assert "1</b> rescan required" in resolve.summary_label.text()
        damaged = scan_id_of(project_session, processed, "damaged.png")
        assert scan_lifecycle.state_of(project_session.database, damaged) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )
        # Kept, not withdrawn: its conflicts are still in the database.
        kept = review_store.list_conflicts(
            project_session.database, processed,
            filters=review_store.ConflictFilter(scan_id=damaged, include_rejected=True),
        )
        assert kept and all(item.state.is_open for item in kept)

    def test_the_rejected_view_says_so_in_words(self, qtbot, resolve: ResolvePage):
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.POOR_QUALITY)
        open_rescan_view(qtbot, resolve)
        assert resolve.queue_table.rowCount() == 1
        state = resolve.queue_table.item(0, STATE_COLUMN).text()
        assert "REJECTED — RESCAN REQUIRED" in state
        assert "REJECTED — RESCAN REQUIRED" in resolve.lifecycle_banner.text()
        assert resolve.lifecycle_banner.isVisibleTo(resolve)
        assert resolve.decision_stack.currentWidget() is resolve.rescan_panel
        assert "REJECTED — RESCAN REQUIRED" in resolve.rescan_panel.state_label.text()
        assert resolve.original_view.has_page
        assert not resolve.type_filter.isEnabled()

    def test_returning_to_the_queue_restores_the_workspace(self, qtbot, resolve):
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.CLIPPED)
        open_rescan_view(qtbot, resolve)
        resolve.state_filter.setCurrentText(FILTER_OPEN)
        assert resolve.decision_stack.currentIndex() == 0
        assert not resolve.lifecycle_banner.isVisibleTo(resolve)

    def test_r_opens_the_dialog_but_never_while_typing(
        self, qtbot, resolve: ResolvePage, monkeypatch
    ):
        select_first_conflict(qtbot, resolve)
        opened: list[bool] = []

        def fake_exec(dialog: RejectScanDialog) -> int:
            opened.append(True)
            return RejectScanDialog.DialogCode.Accepted

        monkeypatch.setattr(RejectScanDialog, "exec", fake_exec)
        from PySide6.QtWidgets import QApplication

        resolve.show()
        resolve.activateWindow()
        resolve.raise_()
        QApplication.processEvents()
        if not resolve.isActiveWindow():
            pytest.skip("this Qt platform plugin never activates a window")
        resolve.search_box.setFocus()
        QTest.keyClick(resolve.search_box, Qt.Key.Key_R)
        assert opened == []
        assert resolve.search_box.text() == "r"  # typed, not a command
        resolve.search_box.clear()
        resolve.queue_table.setFocus()
        resolve.refresh_queue()
        resolve.queue_table.selectRow(0)
        QTest.keyClick(resolve.queue_table, Qt.Key.Key_R)
        assert opened == [True]
        assert resolve.state.conflicts == []

    def test_the_dialog_requires_a_note_for_other(self, qtbot):
        dialog = RejectScanDialog("x.png", "", "", ["1", "2"])
        qtbot.addWidget(dialog)
        dialog.reason_combo.setCurrentIndex(
            dialog.reason_combo.findData(RejectionReason.OTHER.value)
        )
        dialog._accept_if_valid()
        assert "Other" in dialog.problem_label.text()
        dialog.note_edit.setText("Coffee stain across the grid")
        dialog.identity_edit.setText("170503")
        dialog.set_combo.setCurrentIndex(dialog.set_combo.findData("2"))
        assert dialog.values() == (
            RejectionReason.OTHER, "Coffee stain across the grid", "170503", "2"
        )
        # Cancel, not Reject, is what Enter presses.
        assert dialog.cancel_button.isDefault()

    def test_a_reviewer_is_required(self, qtbot, resolve: ResolvePage):
        select_first_conflict(qtbot, resolve)
        resolve.set_reviewer("")
        assert not resolve.reject_button.isEnabled()


class TestRescanWorkflow:
    def test_import_confirm_compare_and_export(
        self, qtbot, resolve: ResolvePage, scan_page: ScanPage, project_session,
        processed, write_sheet, tmp_path,
    ):
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.FOLDED, declared_candidate_id="170503")
        open_rescan_view(qtbot, resolve)

        # Asking to import only asks: the window routes it to the Scan stage.
        rescan = write_sheet("IMG_7777.png", "170503")
        with qtbot.waitSignal(resolve.rescan_import_requested) as asked:
            assert resolve.request_rescan_import([rescan])
        assert asked.args == [processed, [rescan]]

        with qtbot.waitSignal(scan_page.batch_finished, timeout=TIMEOUT_MS):
            assert scan_page.import_rescans(processed, [rescan]) is True
        new_id = scan_id_of(project_session, processed, "IMG_7777.png")
        damaged = scan_id_of(project_session, processed, "damaged.png")
        # Read, registered, and not linked.
        assert scan_lifecycle.state_of(project_session.database, damaged) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )

        resolve.refresh_queue()
        panel = resolve.rescan_panel
        texts = [panel.candidates_list.item(i).text() for i in range(panel.candidates_list.count())]
        assert len(texts) == 1 and texts[0].startswith("Possible rescan: IMG_7777.png")
        assert "170503" in texts[0] and "damaged.png" in texts[0]
        assert not panel.use_button.isEnabled()  # nothing chosen yet
        assert panel.select_candidate(new_id)
        assert panel.use_button.isEnabled()
        with qtbot.waitSignal(resolve.lifecycle_changed, timeout=TIMEOUT_MS):
            panel.use_button.click()
        case = scan_lifecycle.get_case(project_session.database, damaged)
        assert case is not None
        assert case.state is LifecycleState.SUPERSEDED_BY_REPLACEMENT
        assert case.replacement_scan_id == new_id
        assert "replaced by rescan" in resolve.queue_table.item(0, STATE_COLUMN).text()

        # Compare: the image switches to the replacement, and the banner says so.
        with qtbot.waitSignal(resolve.sheet_ready, timeout=TIMEOUT_MS):
            panel.compare_button.click()
        assert "Showing the replacement" in resolve.lifecycle_banner.text()
        assert resolve.original_view.has_page

        # The recognition CSV leaves the rejected scan out.
        written = scan_page.export_csv_to(tmp_path / "out.csv")
        assert written is not None
        text = written.read_text(encoding="utf-8")
        assert "IMG_7777.png" in text and "damaged.png" not in text
        assert "rejected scan(s) left out" in scan_page.progress_label.text()

    def test_a_file_already_in_the_batch_is_not_a_rescan(
        self, qtbot, scan_page: ScanPage, processed, write_sheet, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        shown: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "information", lambda *args, **_kw: shown.append(args[2])
        )
        again = scan_page.state.entries[0].path
        assert scan_page.import_rescans(processed, [again]) is False
        assert shown and "already part of this batch" in shown[0]

    def test_undo_reject_from_the_panel(self, qtbot, resolve: ResolvePage, project_session):
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.FOLDED)
        open_rescan_view(qtbot, resolve)
        assert resolve.rescan_panel.undo_button.isEnabled()
        with qtbot.waitSignal(resolve.lifecycle_changed, timeout=TIMEOUT_MS):
            resolve.rescan_panel.undo_button.click()
        assert resolve.state.rescan_cases == []
        resolve.state_filter.setCurrentText(FILTER_OPEN)
        assert {item.scan_name for item in resolve.state.conflicts} == {"damaged.png"}


class TestNavigationInTheRescanView:
    def test_ctrl_down_walks_outstanding_cases_only_and_wraps(
        self, qtbot, resolve: ResolvePage, project_session, processed
    ):
        database = project_session.database
        ids = {
            name: scan_id_of(project_session, processed, name)
            for name in ("damaged.png", "clean_a.png", "clean_b.png")
        }
        for name in ids:
            scan_lifecycle.reject_scan(
                database, ids[name], reviewer=REVIEWER, reason=RejectionReason.SKEW
            )
        # Make clean_a a completed case by linking clean_b... no: a
        # replacement must be active. Undo clean_b and use it to replace clean_a.
        scan_lifecycle.undo_reject(database, ids["clean_b.png"], reviewer=REVIEWER)
        scan_lifecycle.confirm_replacement(
            database, ids["clean_a.png"], ids["clean_b.png"], reviewer=REVIEWER
        )
        resolve.refresh_queue()
        open_rescan_view(qtbot, resolve)
        cases = resolve.state.rescan_cases
        assert [case.source_name for case in cases] == ["damaged.png", "clean_a.png"]
        assert cases[0].is_outstanding and not cases[1].is_outstanding

        # The Ctrl+Down / Ctrl+Up bindings are the same shortcuts the Resolve
        # stage's own tests press; here the commands behind them are driven.
        # Start on the completed case, which navigation must pass over.
        with qtbot.waitSignal(resolve.sheet_ready, timeout=TIMEOUT_MS):
            resolve.queue_table.selectRow(1)
        assert resolve.select_next_unresolved() is True
        assert resolve.current_case().source_name == "damaged.png"
        assert "continued from the top" in resolve.navigation_note.text()
        assert resolve.select_previous_unresolved() is True
        assert resolve.current_case().source_name == "damaged.png"

    def test_nothing_outstanding_is_said(self, qtbot, resolve, project_session, processed):
        database = project_session.database
        damaged = scan_id_of(project_session, processed, "damaged.png")
        clean = scan_id_of(project_session, processed, "clean_a.png")
        scan_lifecycle.reject_scan(database, damaged, reviewer=REVIEWER,
                                   reason=RejectionReason.SKEW)
        scan_lifecycle.confirm_replacement(database, damaged, clean, reviewer=REVIEWER)
        resolve.refresh_queue()
        open_rescan_view(qtbot, resolve)
        assert resolve.select_next_unresolved() is False
        assert "awaiting a rescan" in resolve.navigation_note.text()


class TestLayout:
    @pytest.mark.parametrize("size", [(1366, 768), (1024, 640)])
    def test_every_rescan_control_is_reachable(self, qtbot, resolve: ResolvePage, size):
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.FOLDED)
        resolve.resize(*size)
        resolve.show()
        qtbot.waitExposed(resolve)
        open_rescan_view(qtbot, resolve)
        panel = resolve.rescan_panel
        for button in (panel.import_button, panel.undo_button, panel.history_button):
            assert button.isVisibleTo(resolve)
            assert button.width() >= button.minimumSizeHint().width() - 1
        assert resolve.reject_button.isVisibleTo(resolve) or resolve.findChild(
            object, "qt_toolbar_ext_button"
        )


class TestReportsAskBeforeAnIncompleteExport:
    def _page(self, qtbot, monkeypatch, report: ReadinessReport) -> ReportsPage:
        spec = next(item for item in WORKFLOW_PAGES if item.key == "reports")
        page = ReportsPage(spec)
        qtbot.addWidget(page)
        monkeypatch.setattr(page, "_readiness_for", lambda _row, **_kw: report)
        return page

    def test_only_rescans_blocking_asks_and_cancel_generates_nothing(
        self, qtbot, monkeypatch
    ):
        report = ReadinessReport(
            set_code="3",
            issues=(ReadinessIssue(ReadinessIssueKind.RESCAN_OUTSTANDING, "rescan"),),
        )
        page = self._page(qtbot, monkeypatch, report)
        asked: list[str] = []
        monkeypatch.setattr(
            page, "confirm_incomplete_export", lambda text: asked.append(text) or False
        )
        row = SetRow(set_id="s3", set_code="3")
        assert page._acknowledge_if_needed([row]) is None
        assert "Set 3: 1 rejected sheet(s) awaiting rescan" in asked[0]
        monkeypatch.setattr(page, "confirm_incomplete_export", lambda _text: True)
        assert page._acknowledge_if_needed([row]) is True

    def test_other_blocks_are_not_offered_the_acknowledgement(self, qtbot, monkeypatch):
        report = ReadinessReport(
            set_code="3",
            issues=(
                ReadinessIssue(ReadinessIssueKind.RESCAN_OUTSTANDING, "rescan"),
                ReadinessIssue(ReadinessIssueKind.NO_VERIFIED_KEY, "no key"),
            ),
        )
        page = self._page(qtbot, monkeypatch, report)
        monkeypatch.setattr(
            page, "confirm_incomplete_export",
            lambda _text: pytest.fail("must not ask"),
        )
        assert page._acknowledge_if_needed([SetRow(set_id="s3", set_code="3")]) is False


class TestAnOriginalWhoseImageIsGone:
    """After *Purge Rejects*, nothing on the panel offers to reactivate the original."""

    @pytest.mark.parametrize(
        ("mode", "where"),
        [(PurgeMode.QUARANTINE, "quarantine"), (PurgeMode.DELETE, "permanently deleted")],
    )
    def test_undo_and_unlink_are_unavailable_and_say_why(
        self, qtbot, resolve: ResolvePage, scan_page: ScanPage, project_session,
        processed, write_sheet, mode, where,
    ):
        database = project_session.database
        damaged = scan_id_of(project_session, processed, "damaged.png")
        # The original lives in project storage, so a purge really removes it.
        stored = project_session.project.layout.scans_original_dir / "damaged.png"
        stored.parent.mkdir(parents=True, exist_ok=True)
        with database.session() as session:
            row = session.get(BatchScan, damaged)
            stored.write_bytes(Path(row.source_path).read_bytes())
            row.source_path = str(stored)
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.FOLDED, declared_candidate_id="170503")
        rescan_path = write_sheet("IMG_8888.png", "170503")
        with qtbot.waitSignal(scan_page.batch_finished, timeout=TIMEOUT_MS):
            assert scan_page.import_rescans(processed, [rescan_path]) is True
        rescan = scan_id_of(project_session, processed, "IMG_8888.png")
        scan_lifecycle.confirm_replacement(database, damaged, rescan, reviewer=REVIEWER)
        outcome = scan_lifecycle.execute_purge(
            database, project_session.root, mode=mode, reviewer=REVIEWER
        )
        assert outcome.files_moved == 1 and not stored.exists()

        # No image to decode, so no loader and no sheet_ready: the view shows
        # a placeholder at once.
        resolve.state_filter.setCurrentText(FILTER_RESCAN)
        assert resolve.current_case().scan_id == damaged
        assert "The scan's record, its rejection" in resolve.original_note.text()
        panel = resolve.rescan_panel
        # Undo is not offered at all for a replaced sheet, and unlinking -
        # the only road back - is disabled, with the reason in words.
        assert not panel.undo_button.isVisibleTo(resolve)
        assert panel.unlink_button.isVisibleTo(resolve)
        assert not panel.unlink_button.isEnabled()
        assert panel.unavailable_label.isVisibleTo(resolve)
        note = panel.unavailable_label.text()
        assert note.startswith("Original image is no longer available")
        assert where in note and "cannot be undone" in note
        assert panel.unlink_button.toolTip() == note
        # The service refuses the same way, whatever calls it.
        with pytest.raises(scan_lifecycle.LifecycleError):
            scan_lifecycle.undo_reject(database, damaged, reviewer=REVIEWER)
        # The replacement is untouched.
        assert scan_lifecycle.state_of(database, rescan) is LifecycleState.ACTIVE
        case = scan_lifecycle.get_case(database, damaged)
        assert case is not None and case.replacement_scan_id == rescan

    def test_no_note_while_the_image_is_present(self, qtbot, resolve: ResolvePage):
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.FOLDED)
        open_rescan_view(qtbot, resolve)
        assert not resolve.rescan_panel.unavailable_label.isVisibleTo(resolve)
        assert resolve.rescan_panel.undo_button.isEnabled()


class TestAllProcessedSheets:
    """Rejecting a sheet that raised no conflict, from the inspection view."""

    def open_sheets(self, qtbot, page: ResolvePage) -> None:
        with qtbot.waitSignal(page.sheet_ready, timeout=TIMEOUT_MS):
            page.show_processed_sheets()

    def test_every_read_sheet_is_listed_including_clean_ones(self, qtbot, resolve):
        self.open_sheets(qtbot, resolve)
        names = [resolve.queue_table.item(row, 0).text() for row in range(3)]
        assert sorted(names) == ["clean_a.png", "clean_b.png", "damaged.png"]
        assert resolve.decision_stack.currentWidget() is resolve.sheet_panel
        assert resolve.original_view.has_page  # the scan as it arrived
        assert resolve.state.conflicts == []

    def test_a_clean_sheet_shows_its_identity_and_can_be_rejected(
        self, qtbot, resolve: ResolvePage, project_session, processed
    ):
        self.open_sheets(qtbot, resolve)
        clean = scan_id_of(project_session, processed, "clean_a.png")
        if resolve.current_sheet().scan_id != clean:
            with qtbot.waitSignal(resolve.sheet_ready, timeout=TIMEOUT_MS):
                assert resolve.select_sheet(clean)
        panel = resolve.sheet_panel
        assert "170504" in panel.details_label.text()
        assert "No open conflict" in resolve.queue_table.item(
            resolve.queue_table.currentRow(), 2
        ).text()
        assert panel.reject_button.isEnabled() and resolve.reject_button.isEnabled()
        before = review_store.count_conflicts(project_session.database, processed)

        with qtbot.waitSignal(resolve.lifecycle_changed, timeout=TIMEOUT_MS):
            assert resolve.reject_current_sheet(
                RejectionReason.WRONG_DOCUMENT, declared_candidate_id="170504"
            )
        assert scan_lifecycle.state_of(project_session.database, clean) is (
            LifecycleState.REJECTED_PENDING_RESCAN
        )
        # Still selected, now saying so; and not rejectable twice.
        assert resolve.current_sheet().scan_id == clean
        assert "REJECTED — RESCAN REQUIRED" in resolve.lifecycle_banner.text()
        assert not panel.reject_button.isEnabled()
        assert not resolve.reject_button.isEnabled()
        # The conflict counts are unchanged: a clean sheet had none.
        after = review_store.count_conflicts(project_session.database, processed)
        assert (after.total, after.unresolved) == (before.total, before.unresolved)
        # And the case is in Rejected / Rescan like any other.
        open_rescan_view(qtbot, resolve)
        assert [case.scan_id for case in resolve.state.rescan_cases] == [clean]

    def test_ctrl_navigation_does_not_walk_the_sheet_list(self, qtbot, resolve):
        self.open_sheets(qtbot, resolve)
        row = resolve.queue_table.currentRow()
        assert resolve.select_next_unresolved() is False
        assert resolve.select_previous_unresolved() is False
        assert resolve.queue_table.currentRow() == row
        assert "nothing in it is unresolved" in resolve.navigation_note.text()
        # Back in the queue, Ctrl+Down means what it always meant.
        resolve.state_filter.setCurrentText(FILTER_OPEN)
        assert resolve.select_next_unresolved() is True
        assert resolve.current_conflict().scan_name == "damaged.png"

    def test_the_view_is_searched_in_sql_and_loads_one_image(
        self, qtbot, resolve: ResolvePage, monkeypatch
    ):
        loaded: list[str] = []
        original = ResolvePage._load_image

        def spy(page: ResolvePage, path: Path) -> None:
            loaded.append(path.name)
            return original(page, path)

        monkeypatch.setattr(ResolvePage, "_load_image", spy)
        self.open_sheets(qtbot, resolve)
        assert len(loaded) == 1  # the selected row only, not every sheet
        seen: list[str] = []
        real = scan_lifecycle.processed_sheets
        monkeypatch.setattr(
            scan_lifecycle, "processed_sheets",
            lambda *args, **kw: seen.append(kw.get("search", "")) or real(*args, **kw),
        )
        resolve.search_box.setText("clean_b")
        resolve.refresh_queue()
        assert seen and seen[-1] == "clean_b"
        assert resolve.queue_table.rowCount() == 1

    def test_the_summary_is_not_inflated(self, qtbot, resolve: ResolvePage):
        before = resolve.summary_label.text()
        self.open_sheets(qtbot, resolve)
        resolve.state_filter.setCurrentText(FILTER_OPEN)
        assert resolve.summary_label.text() == before


class TestCrossBatchRescan:
    def test_a_rescan_read_in_a_later_batch_is_offered_and_confirmed(
        self, qtbot, resolve: ResolvePage, project_session, processed,
        template_path, write_sheet,
    ):
        database = project_session.database
        damaged = scan_id_of(project_session, processed, "damaged.png")
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.FOLDED, declared_candidate_id="170503")

        # Days later, on another scanner: a new batch, not an import into this one.
        spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
        later_page = ScanPage(spec)
        qtbot.addWidget(later_page)
        later_page.on_project_changed(project_session)
        assert later_page.load_template_from(template_path) is True
        later_page.add_scan_paths([write_sheet("SCN_000913.tif", "170503")])
        with qtbot.waitSignal(later_page.batch_finished, timeout=TIMEOUT_MS):
            assert later_page.process_all() is True
        later = later_page.state.batch_id
        assert later is not None and later != processed
        rescan = scan_id_of(project_session, later, "SCN_000913.tif")

        open_rescan_view(qtbot, resolve)
        panel = resolve.rescan_panel
        texts = [panel.candidates_list.item(i).text() for i in range(panel.candidates_list.count())]
        assert len(texts) == 1
        assert "SCN_000913.tif" in texts[0] and f"batch {later[:8]}" in texts[0]
        # Offered, not linked.
        assert scan_lifecycle.state_of(database, damaged) is LifecycleState.REJECTED_PENDING_RESCAN
        assert panel.select_candidate(rescan)
        with qtbot.waitSignal(resolve.lifecycle_changed, timeout=TIMEOUT_MS):
            panel.use_button.click()
        case = scan_lifecycle.get_case(database, damaged)
        assert case is not None and case.replacement_batch_id == later
        assert f"batch {later[:8]}" in panel.details_label.text()
        with qtbot.waitSignal(resolve.sheet_ready, timeout=TIMEOUT_MS):
            panel.compare_button.click()
        assert "Showing the replacement" in resolve.lifecycle_banner.text()

    def test_manual_association_searches_the_whole_project(
        self, qtbot, resolve: ResolvePage, project_session, processed, monkeypatch
    ):
        select_first_conflict(qtbot, resolve)
        resolve.reject_current_sheet(RejectionReason.FOLDED)
        open_rescan_view(qtbot, resolve)
        panel = resolve.rescan_panel
        assert not panel.association_search.isVisibleTo(resolve)
        seen: list[str] = []
        real = scan_lifecycle.association_choices
        monkeypatch.setattr(
            scan_lifecycle, "association_choices",
            lambda *args, **kw: seen.append(kw.get("search", "")) or real(*args, **kw),
        )
        panel.show_all_button.setChecked(True)
        assert panel.association_search.isVisibleTo(resolve)
        panel.association_search.setText("clean_b")
        panel.association_search.editingFinished.emit()  # queried on Enter, not per key
        assert seen[-1] == "clean_b"
        texts = [panel.candidates_list.item(i).text() for i in range(panel.candidates_list.count())]
        assert len(texts) == 1 and "clean_b.png" in texts[0]


class TestTheWindow:
    def test_purge_rejects_is_offered_with_a_project(self, qtbot, tmp_path, project_session):
        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        assert not window.purge_rejects_action.isEnabled()
        window._session = project_session
        window._broadcast_project_change()
        assert window.purge_rejects_action.isEnabled()
        window._resolve_page().set_reviewer(REVIEWER)
        outcome = window.purge_rejects(PurgeMode.QUARANTINE)
        assert outcome is not None and outcome.processed == 0
        window._session = None
