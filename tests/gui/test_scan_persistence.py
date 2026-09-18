"""The Scan page's durable batches, through the real widgets (Phase 5).

Scope:
    Drives :class:`~omr_scanner.gui.scan.page.ScanPage` through the same
    public commands its buttons call, with a real project, a real
    ``QThread`` and a real recognition engine, and asserts against what ended
    up in the project database.

Mapping to the Phase 5 test plan:
    ==== ======================================================
    Test Covered by
    ==== ======================================================
    D    :class:`TestDCancellation`
    E    :class:`TestEResumeThroughThePage`
    L    :class:`TestLGuiResponsiveness`
    ==== ======================================================

    A/B/C/F/G/H/I/J/K are headless and live in
    ``tests/integration/test_batch_persistence.py``.

Why every wait is on a signal:
    ``ScanPage.batch_finished`` fires when a run ends, cancelled or not. No
    test here sleeps for a fixed time - a machine slower than the developer's
    would turn that into a flake, and a machine faster into a test that passes
    without having waited for anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.config import AppConfig
from omr_scanner.database.models import BatchStatus, ScanJobStatus
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import (
    FILTER_ALL,
    FILTER_COMPLETED,
    FILTER_FAILED,
    ScanPage,
)
from omr_scanner.services import batch_store, save_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

BATCH_TIMEOUT_MS = 120_000
"""A ceiling, not a delay - every wait returns as soon as its signal arrives."""


def sheet_marks(roll: str) -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def template_path(project_session: ProjectSession, template) -> Path:
    path = project_session.project.layout.templates_dir / "sheet.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return save_template(template, path)


@pytest.fixture
def scans_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "batch_scans"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def make_scans(scans_dir: Path, template):
    def make(count: int, *, start: int = 0) -> list[Path]:
        paths = []
        for index in range(count):
            path = scans_dir / f"scan_{start + index:03d}.png"
            cv2.imwrite(
                str(path),
                render_marked_sheet(template, sheet_marks(f"{100000 + start + index}")),
            )
            paths.append(path)
        return paths

    return make


@pytest.fixture
def page(qtbot, project_session: ProjectSession, template_path: Path) -> ScanPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    scan_page = ScanPage(spec)
    qtbot.addWidget(scan_page)
    scan_page.on_project_changed(project_session)
    assert scan_page.load_template_from(template_path) is True
    return scan_page


def run_and_wait(qtbot, page: ScanPage, start) -> None:
    """Start a run and wait for ``batch_finished``."""
    with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS):
        assert start() is True


class TestBatchIsRecorded:
    def test_processing_creates_a_stored_batch(
        self, qtbot, page: ScanPage, project_session, make_scans
    ):
        page.add_scan_paths(make_scans(3))
        run_and_wait(qtbot, page, page.process_all)

        assert page.state.batch_id is not None
        summary = batch_store.load_summary(project_session.database, page.state.batch_id)
        assert summary.total == 3
        assert summary.processed == 3
        assert summary.status == BatchStatus.COMPLETED.value

    def test_the_page_reports_the_stored_state(self, qtbot, page: ScanPage, make_scans):
        page.add_scan_paths(make_scans(2))
        run_and_wait(qtbot, page, page.process_all)
        text = page.batch_state_label.text()
        assert "all processed" in text
        assert "2 ok" in text

    def test_no_project_means_no_batch_but_the_run_still_works(
        self, qtbot, template_path: Path, make_scans
    ):
        # Running without a project is supported: recognition, renaming and
        # CSV export all work, they are simply not durable. The page says so
        # rather than refusing.
        spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
        standalone = ScanPage(spec)
        qtbot.addWidget(standalone)
        standalone.on_project_changed(None)
        assert standalone.load_template_from(template_path) is True
        standalone.add_scan_paths(make_scans(2))

        run_and_wait(qtbot, standalone, standalone.process_all)

        assert standalone.state.batch_id is None
        assert standalone.database is None
        assert "not be saved" in standalone.batch_state_label.text()
        assert all(entry.processed is not None for entry in standalone.state.entries)
        standalone.shutdown_batch()


# ----------------------------------------------------------------------
# Test D - cancellation
# ----------------------------------------------------------------------
class TestDCancellation:
    def test_cancelling_keeps_finished_work_and_leaves_the_rest_resumable(
        self, qtbot, page: ScanPage, project_session, make_scans
    ):
        page.add_scan_paths(make_scans(8))

        # Cancel as soon as the first sheet lands, so the run genuinely stops
        # part-way rather than racing to completion.
        def cancel_on_first(_processed: object) -> None:
            page.cancel_processing()

        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert page.process_all() is True
            # Connected after starting: the worker only exists from here on.
            page._worker.scan_done.connect(cancel_on_first)

        report = blocker.args[0]
        summary = batch_store.load_summary(project_session.database, page.state.batch_id)

        assert summary.total == 8
        # Whatever was read is kept ...
        assert summary.processed == report.total
        assert summary.processed >= 1
        # ... and everything else is still to do.
        assert summary.pending == 8 - summary.processed
        assert summary.status == BatchStatus.CANCELLED.value
        assert (
            len(batch_store.resumable_scans(project_session.database, page.state.batch_id))
            == summary.pending
        )

    def test_cancelling_leaves_no_worker_running(
        self, qtbot, page: ScanPage, make_scans
    ):
        page.add_scan_paths(make_scans(6))
        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            page.process_all()
            page._worker.scan_done.connect(lambda _p: page.cancel_processing())

        assert page.is_processing is False
        # And the controls come back, rather than staying disabled forever.
        assert page.process_all_button.isEnabled() is True

    def test_no_worker_processes_are_left_behind(self, qtbot, page: ScanPage, make_scans):
        import multiprocessing

        page.add_scan_paths(make_scans(4))
        run_and_wait(qtbot, page, page.process_all)
        page.shutdown_batch()
        assert multiprocessing.active_children() == []


# ----------------------------------------------------------------------
# Test E - resume through the page
# ----------------------------------------------------------------------
class TestEResumeThroughThePage:
    def test_resume_processes_only_the_remainder(
        self, qtbot, page: ScanPage, project_session, make_scans
    ):
        paths = make_scans(6)
        page.add_scan_paths(paths)

        # Process the first three by selecting them explicitly - a partial run
        # with the same shape as a cancelled one.
        page.scan_table.selectRow(0)
        page.scan_table.selectRow(1)
        page.scan_table.selectRow(2)
        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert page._start_batch(paths[:3]) is True

        database = project_session.database
        batch_id = page.state.batch_id
        assert batch_store.load_summary(database, batch_id).pending == 3

        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert page.resume_batch() is True

        # The resumed run read three sheets, not six.
        assert blocker.args[0].total == 3
        summary = batch_store.load_summary(database, batch_id)
        assert summary.processed == 6
        assert summary.pending == 0

    def test_resume_is_disabled_when_nothing_is_left(
        self, qtbot, page: ScanPage, make_scans
    ):
        page.add_scan_paths(make_scans(2))
        run_and_wait(qtbot, page, page.process_all)
        assert page.resume_button.isEnabled() is False

    def test_reopening_a_batch_restores_its_results(
        self, qtbot, page: ScanPage, project_session, make_scans
    ):
        page.add_scan_paths(make_scans(3))
        run_and_wait(qtbot, page, page.process_all)
        batch_id = page.state.batch_id

        # A fresh page, as if the application had been restarted.
        spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
        reopened = ScanPage(spec)
        qtbot.addWidget(reopened)
        reopened.on_project_changed(project_session)

        assert reopened.adopt_batch(batch_id) is True
        assert len(reopened.state.entries) == 3
        # The results came back with the scans, so the list is not just a set
        # of file names waiting to be read again.
        assert all(entry.processed is not None for entry in reopened.state.entries)
        assert {entry.identifier for entry in reopened.state.entries} == {
            "100000",
            "100001",
            "100002",
        }
        reopened.shutdown_batch()

    def test_an_incompatible_resume_asks_before_mixing_results(
        self, qtbot, monkeypatch, page: ScanPage, project_session, make_scans, template_path
    ):
        from PySide6.QtWidgets import QMessageBox

        paths = make_scans(4)
        page.add_scan_paths(paths)
        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            page._start_batch(paths[:2])

        # Retune the template underneath the batch, then try to resume.
        retuned = page.state.template.model_copy(
            update={
                "recognition": page.state.template.recognition.model_copy(
                    update={"fill_ratio_threshold": 0.72}
                )
            }
        )
        save_template(retuned, template_path)
        page.load_template_from(template_path)
        page.state.batch_id = batch_store.list_batches(project_session.database)[0].batch_id

        asked: list[str] = []

        def decline(
            _parent: object, _title: str, text: str, *_args: object, **_kwargs: object
        ) -> QMessageBox.StandardButton:
            asked.append(text)
            return QMessageBox.StandardButton.Cancel

        monkeypatch.setattr(
            "omr_scanner.gui.scan.page.QMessageBox.warning", staticmethod(decline)
        )
        assert page.resume_batch() is False
        assert asked, "the operator was never asked"
        assert "threshold" in asked[0].lower()


# ----------------------------------------------------------------------
# Retry and filtering
# ----------------------------------------------------------------------
class TestRetryAndFiltering:
    def test_retry_targets_only_the_failed_scans(
        self, qtbot, page: ScanPage, project_session, make_scans, scans_dir
    ):
        good = make_scans(2)
        corrupt = scans_dir / "corrupt.png"
        corrupt.write_bytes(b"not an image")
        page.add_scan_paths([*good, corrupt])
        run_and_wait(qtbot, page, page.process_all)

        database = project_session.database
        batch_id = page.state.batch_id
        summary = batch_store.load_summary(database, batch_id)
        assert summary.failed == 1
        assert page.retry_failed_button.isEnabled() is True

        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert page.retry_failed() is True

        # One sheet re-read, and the two good results untouched.
        assert blocker.args[0].total == 1
        after = batch_store.load_summary(database, batch_id)
        assert after.completed + after.warning == 2
        assert after.failed == 1

    def test_the_failed_filter_shows_only_failures(
        self, qtbot, page: ScanPage, make_scans, scans_dir
    ):
        page.add_scan_paths(make_scans(2))
        corrupt = scans_dir / "bad.png"
        corrupt.write_bytes(b"still not an image")
        page.add_scan_paths([corrupt])
        run_and_wait(qtbot, page, page.process_all)

        page.status_filter_combo.setCurrentText(FILTER_FAILED)
        visible = page.visible_rows()
        assert len(visible) == 1
        assert page.state.entries[visible[0]].path == corrupt

        page.status_filter_combo.setCurrentText(FILTER_COMPLETED)
        assert len(page.visible_rows()) == 2

        page.status_filter_combo.setCurrentText(FILTER_ALL)
        assert len(page.visible_rows()) == 3

    def test_filtering_never_changes_what_was_recognised(
        self, qtbot, page: ScanPage, make_scans
    ):
        page.add_scan_paths(make_scans(3))
        run_and_wait(qtbot, page, page.process_all)
        before = [entry.identifier for entry in page.state.entries]

        page.status_filter_combo.setCurrentText(FILTER_COMPLETED)
        page.status_filter_combo.setCurrentText(FILTER_ALL)

        assert [entry.identifier for entry in page.state.entries] == before


# ----------------------------------------------------------------------
# Test L - GUI responsiveness
# ----------------------------------------------------------------------
class TestLGuiResponsiveness:
    def test_the_event_loop_keeps_running_throughout_a_batch(
        self, qtbot, page: ScanPage, make_scans
    ):
        from PySide6.QtCore import QTimer

        page.add_scan_paths(make_scans(8))

        # A timer that can only fire if the GUI thread is actually returning
        # to the event loop while the batch runs. If recognition were happening
        # on this thread, it would not tick.
        ticks: list[int] = []
        heartbeat = QTimer()
        heartbeat.setInterval(20)
        heartbeat.timeout.connect(lambda: ticks.append(1))
        heartbeat.start()

        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert page.process_all() is True
        heartbeat.stop()

        assert len(ticks) > 3, "the GUI thread was blocked while the batch ran"

    def test_progress_advances_and_reaches_the_total(
        self, qtbot, page: ScanPage, make_scans
    ):
        page.add_scan_paths(make_scans(6))
        run_and_wait(qtbot, page, page.process_all)

        snapshot = page._last_snapshot
        assert snapshot.total == 6
        # `completed` is every job that reached a terminal state, however it
        # ended - the same definition the progress bar uses.
        assert snapshot.completed == 6
        assert page.progress_bar.value() == page.progress_bar.maximum()

    def test_the_page_stays_usable_after_a_run(self, qtbot, page: ScanPage, make_scans):
        page.add_scan_paths(make_scans(3))
        run_and_wait(qtbot, page, page.process_all)

        assert page.is_processing is False
        assert page.process_all_button.isEnabled() is True
        assert page.export_csv_button.isEnabled() is True
        assert page.cancel_button.isEnabled() is False


# ----------------------------------------------------------------------
# Closing the window mid-batch
# ----------------------------------------------------------------------
class TestClosingDuringProcessing:
    def test_the_window_reports_a_running_batch(
        self, qtbot, tmp_path, project_session, template_path, make_scans
    ):
        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        assert window.batch_is_running() is False

        scan_page = window._pages["scan"]
        scan_page.on_project_changed(project_session)
        scan_page.load_template_from(template_path)
        scan_page.add_scan_paths(make_scans(6))

        with qtbot.waitSignal(scan_page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert scan_page.process_all() is True
            assert window.batch_is_running() is True
            scan_page.cancel_processing()

        assert window.batch_is_running() is False

    def test_shutting_down_mid_batch_leaves_the_batch_resumable(
        self, page: ScanPage, project_session, make_scans
    ):
        page.add_scan_paths(make_scans(8))
        assert page.process_all() is True
        # The hard path: stop without waiting for `batch_finished`, exactly as
        # the main window does when the operator confirms "stop and exit".
        page.shutdown_batch()

        assert page.is_processing is False
        summary = batch_store.load_summary(project_session.database, page.state.batch_id)
        assert summary.status == BatchStatus.CANCELLED.value
        # Nothing is left claiming to be in progress, so the next open resumes
        # cleanly rather than skipping the sheets that were in flight.
        remaining = batch_store.resumable_scans(
            project_session.database, page.state.batch_id
        )
        assert len(remaining) == summary.pending
        assert summary.processed + summary.pending == 8


class TestCrashRecoveryThroughTheWindow:
    def test_opening_a_project_repairs_stale_processing_rows(
        self, qtbot, tmp_path, workspace, template
    ):
        from omr_scanner.services import create_project, open_project

        session = create_project(workspace, "Interrupted Examination")
        root = session.root
        database = session.database
        paths = [tmp_path / f"scan{i}.png" for i in range(4)]
        for path in paths:
            path.write_bytes(b"placeholder")
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)
        )
        # The shape a crash leaves behind.
        batch_store.mark_queued(database, batch_id, paths[:2])
        batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)
        session.close()

        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        assert window.open_project_at(root) is True

        reopened = open_project(root)
        try:
            summary = batch_store.load_summary(reopened.database, batch_id)
            assert summary.status == BatchStatus.INTERRUPTED.value
            assert summary.pending == 4
            assert summary.failed == 0
        finally:
            reopened.close()
        window.close_project()

    def test_a_stale_row_is_never_recovered_as_a_failure(
        self, tmp_path, workspace, template
    ):
        # "We do not know what happened to this sheet" must not become "this
        # sheet is bad": that would quietly exclude it from every resume.
        from omr_scanner.services import create_project

        session = create_project(workspace, "Another Examination")
        try:
            paths = [tmp_path / "one.png"]
            paths[0].write_bytes(b"placeholder")
            batch_id = batch_store.create_batch(
                session.database, paths, identity=batch_store.BatchIdentity.of(template)
            )
            batch_store.mark_queued(session.database, batch_id, paths)
            batch_store.recover_interrupted(session.database)
            assert batch_store.resumable_scans(session.database, batch_id) == tuple(paths)
            assert batch_store.failed_scans(session.database, batch_id) == ()
            assert ScanJobStatus.PENDING.is_resumable is True
        finally:
            session.close()
