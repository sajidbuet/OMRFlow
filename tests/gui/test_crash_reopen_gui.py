"""Scan and Resolve after an interrupted session, through the real window (0.1.1 phase 3).

Scope:
    What an operator sees when a project is reopened after a crash, before
    pressing anything: the Scan stage holding the interrupted batch with its
    committed results and the honest counts, and the Resolve stage holding
    every committed decision and exactly the unresolved queue - without a
    visit to Scan. And, during a run, that a sheet is not counted done before
    its durable work unit commits (S3).

    The database states are the ones a real kill leaves; that a real kill
    leaves them is ``tests/crash/test_crash_matrix.py``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.config import AppConfig
from omr_scanner.database.models import BatchStatus
from omr_scanner.domain.review import ReasonCode
from omr_scanner.domain.scan_sessions import BatchMembership
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.gui.scan.worker import BatchWorker
from omr_scanner.services import (
    BatchOptions,
    batch_store,
    create_project,
    review_store,
    save_template,
    scan_sessions,
    set_active_template,
)
from omr_scanner.services.batch_processor import process_batch

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.template import OmrTemplate

pytestmark = pytest.mark.gui

BATCH_TIMEOUT_MS = 120_000
REVIEWER = "Reopen Tester"


def _marks(roll: str | None) -> dict:
    marks: dict = {
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }
    if roll is not None:
        marks["roll_number"] = dict(enumerate(roll))
    return marks


@pytest.fixture(scope="module")
def template() -> OmrTemplate:
    return build_answer_sheet_template()


@pytest.fixture(scope="module")
def sheets(tmp_path_factory: pytest.TempPathFactory, template: OmrTemplate) -> list[Path]:
    directory = tmp_path_factory.mktemp("reopen_sheets")
    rolls: list[str | None] = [f"40000{index}" for index in range(6)] + ["500000", "500000", None]
    paths = []
    for index, roll in enumerate(rolls):
        path = directory / f"scan_{index:02d}.png"
        cv2.imwrite(str(path), render_marked_sheet(template, _marks(roll)))
        paths.append(path)
    corrupt = directory / "scan_99.png"
    corrupt.write_bytes(b"not an image")
    paths.append(corrupt)
    return paths


@pytest.fixture(scope="module")
def outcomes(sheets: list[Path], template: OmrTemplate):  # type: ignore[no-untyped-def]
    return list(process_batch(sheets, template, options=BatchOptions(), workers=1).processed)


def _interrupted_project(  # type: ignore[no-untyped-def]
    workspace: Path, template, sheets, outcomes, *, committed: int
) -> tuple[Path, str]:
    """A project left exactly as a killed Scan run leaves it."""
    session = create_project(workspace, "Interrupted Examination")
    try:
        path = session.project.layout.templates_dir / "sheet.omrt"
        path.parent.mkdir(parents=True, exist_ok=True)
        save_template(template, path)
        set_active_template(session, path)
        database = session.database
        batch_id = batch_store.create_batch(
            database, sheets, identity=batch_store.BatchIdentity.of(template, path)
        )
        batch_store.mark_queued(database, batch_id, sheets)
        batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)
        batch_store.record_results(database, batch_id, outcomes[:committed], template=template)
        return session.root, batch_id
    finally:
        session.close()


def _window(qtbot, tmp_path: Path) -> MainWindow:  # type: ignore[no-untyped-def]
    window = MainWindow(
        config=AppConfig(reviewer_name=REVIEWER), config_path=tmp_path / "config.json"
    )
    qtbot.addWidget(window)
    return window


class TestScanAfterReopen:
    def test_the_interrupted_batch_is_shown_from_committed_rows_before_resume(
        self, qtbot, tmp_path, workspace, template, sheets, outcomes
    ):
        root, batch_id = _interrupted_project(workspace, template, sheets, outcomes, committed=4)
        window = _window(qtbot, tmp_path)
        assert window.open_project_at(root) is True
        scan = window._scan_page()
        assert scan is not None

        assert scan.state.batch_id == batch_id
        assert scan.is_processing is False  # opening never starts a run
        assert len(scan.state.entries) == len(sheets)
        assert sum(1 for entry in scan.state.entries if entry.processed is not None) == 4
        pending = len(sheets) - 4
        expected = f"4 / {len(sheets)} recognised · {pending} pending"
        assert scan.progress_counts_label.text() == expected
        assert "press Resume" in scan.progress_label.text()
        assert scan.progress_bar.value() == 4
        assert scan.resume_button.isEnabled() is True
        summary = batch_store.load_summary(window.session.database, batch_id)
        assert summary is not None and summary.status == BatchStatus.INTERRUPTED.value
        window.close_project()

    def test_the_same_session_and_batch_are_kept(
        self, qtbot, tmp_path, workspace, template, sheets, outcomes
    ):
        root, batch_id = _interrupted_project(workspace, template, sheets, outcomes, committed=4)
        window = _window(qtbot, tmp_path)
        assert window.open_project_at(root) is True
        database = window.session.database
        sessions = scan_sessions.list_scan_sessions(database)
        assert len(sessions) == 1
        batches = scan_sessions.batches_of(database, sessions[0].scan_session_id)
        assert [item.batch_id for item in batches] == [batch_id]
        assert scan_sessions.live_supersessions(database) == {}
        window.close_project()

    def test_resume_reads_only_the_uncommitted_sheets(
        self, qtbot, tmp_path, workspace, template, sheets, outcomes
    ):
        root, batch_id = _interrupted_project(workspace, template, sheets, outcomes, committed=4)
        window = _window(qtbot, tmp_path)
        assert window.open_project_at(root) is True
        scan = window._scan_page()
        with qtbot.waitSignal(scan.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert scan.resume_batch() is True
        assert blocker.args[0].total == len(sheets) - 4
        summary = batch_store.load_summary(window.session.database, batch_id)
        assert summary is not None and summary.pending == 0
        window.close_project()

    def test_process_all_on_a_restored_batch_skips_what_was_committed(
        self, qtbot, tmp_path, workspace, template, sheets, outcomes
    ):
        root, batch_id = _interrupted_project(workspace, template, sheets, outcomes, committed=6)
        window = _window(qtbot, tmp_path)
        assert window.open_project_at(root) is True
        scan = window._scan_page()
        with qtbot.waitSignal(scan.batch_finished, timeout=BATCH_TIMEOUT_MS) as blocker:
            assert scan.process_all() is True
        assert blocker.args[0].total == len(sheets) - 6
        rows = {
            Path(path).name: attempts
            for path, attempts in _attempts(window.session.database, batch_id).items()
        }
        assert set(rows.values()) == {1}, rows
        window.close_project()

    def test_a_sealed_interrupted_batch_is_shown_sealed_and_resumes_its_members(
        self, qtbot, tmp_path, workspace, template, sheets, outcomes
    ):
        root, batch_id = _interrupted_project(workspace, template, sheets, outcomes, committed=4)
        from omr_scanner.services import open_project

        session = open_project(root)
        try:
            scan_sessions.seal_batch(session.database, batch_id, sealed_by=REVIEWER)
            batch_store.set_batch_status(session.database, batch_id, BatchStatus.RUNNING)
        finally:
            session.close()
        window = _window(qtbot, tmp_path)
        assert window.open_project_at(root) is True
        scan = window._scan_page()
        database = window.session.database
        assert scan.state.batch_id == batch_id
        info = scan_sessions.batch_info(database, batch_id)
        assert info is not None and info.membership is BatchMembership.SEALED
        with qtbot.waitSignal(scan.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert scan.resume_batch() is True
        info = scan_sessions.batch_info(database, batch_id)
        assert info is not None and info.membership is BatchMembership.SEALED
        assert info.total_scans == len(sheets)
        window.close_project()


def _attempts(database, batch_id: str) -> dict[str, int]:  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from omr_scanner.database.models import BatchScan

    with database.session() as session:
        return {
            str(path): int(attempts)
            for path, attempts in session.execute(
                select(BatchScan.source_path, BatchScan.attempt_count).where(
                    BatchScan.batch_id == batch_id
                )
            ).all()
        }


class TestResolveAfterReopen:
    def test_resolve_shows_committed_decisions_and_exactly_the_unresolved_queue(
        self, qtbot, tmp_path, workspace, template, sheets, outcomes
    ):
        root, batch_id = _interrupted_project(
            workspace, template, sheets, outcomes, committed=len(outcomes)
        )
        from omr_scanner.services import open_project, scan_recovery

        session = open_project(root)
        try:
            database = session.database
            scan_recovery.recover_on_open(database, templates=[template])
            batch_store.finalise_batch(database, batch_id)
            queue = review_store.list_conflicts(database, batch_id)
            assert len(queue) >= 3
            corrected = next(item for item in queue if item.allows_value_correction)
            review_store.correct_value(
                database,
                corrected.conflict_id,
                value="9",
                reviewer=REVIEWER,
                reason=ReasonCode.OTHER,
                reason_text="read from the paper",
            )
            accepted = next(item for item in queue if item.conflict_id != corrected.conflict_id)
            review_store.accept_machine_value(database, accepted.conflict_id, reviewer=REVIEWER)
            decided = {corrected.conflict_id, accepted.conflict_id}
            expected_queue = [item.conflict_id for item in queue if item.conflict_id not in decided]
        finally:
            session.close()

        window = _window(qtbot, tmp_path)
        assert window.open_project_at(root) is True
        resolve = window._resolve_page()
        assert resolve is not None
        # No visit to Scan, no Review Conflicts click: the stage loaded itself.
        assert resolve.state.batch_id == batch_id
        assert [item.conflict_id for item in resolve.state.conflicts] == expected_queue
        text = resolve.summary_label.text()
        assert f"<b>{len(expected_queue)}</b> unresolved" in text
        assert "<b>2</b> resolved" in text
        provenance = review_store.provenance_for(window.session.database, corrected.conflict_id)
        assert provenance.value == "9"
        window.close_project()


class TestClosingJoinsTheScanWorkers:
    def test_closing_the_window_waits_for_a_preview_still_being_read(
        self, qtbot, tmp_path, workspace, template, sheets, outcomes
    ):
        # Found by the crash harness: with no batch running, the window's
        # close did not shut the Scan page down, so a preview worker still
        # reading a sheet outlived the window and aborted the process at exit.
        root, _batch_id = _interrupted_project(
            workspace, template, sheets, outcomes, committed=4
        )
        window = _window(qtbot, tmp_path)
        assert window.open_project_at(root) is True
        scan = window._scan_page()
        assert scan is not None and not scan.is_processing
        scan.select_scan(0)
        preview = scan._preview_worker
        assert preview is not None, "selecting a row starts the preview worker"
        window.close()
        assert not preview.isRunning()


class TestCompletedCountIsCommitted:
    def test_a_read_but_uncommitted_sheet_is_in_flight_not_processed(
        self, qtbot, project_session, template, sheets, monkeypatch
    ):
        database = project_session.database
        batch_id = batch_store.create_batch(
            database, sheets[:3], identity=batch_store.BatchIdentity.of(template)
        )
        gate = threading.Event()
        entered = threading.Event()
        real = batch_store.record_results

        def held(*args: object, **kwargs: object) -> None:
            entered.set()
            assert gate.wait(30), "the test never released the store"
            real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(batch_store, "record_results", held)
        recorder = batch_store.BatchRecorder(
            database=database, batch_id=batch_id, template=template, flush_every=1
        )
        # Two workers: a read is reported as it lands, before it is released
        # in batch order to the store - the window S3 is about.
        worker = BatchWorker(sheets[:3], template, BatchOptions(), workers=2, recorder=recorder)
        done: list[object] = []
        worker.scan_done.connect(done.append)
        with qtbot.waitSignal(worker.finished_report, timeout=BATCH_TIMEOUT_MS):
            worker.start()
            assert entered.wait(30)
            qtbot.wait(200)
            snapshot = worker.progress_snapshot()
            # Read by the worker, held by the store: not done, and not shown done.
            assert snapshot.completed == 0
            assert snapshot.in_flight >= 1
            assert done == []
            gate.set()
        worker.wait()
        final = worker.progress_snapshot()
        assert final.completed == 3 and final.in_flight == 0
        assert len(done) == 3

    def test_a_store_that_cannot_commit_counts_nothing_and_names_it_not_saved(
        self, qtbot, project_session, template, sheets, monkeypatch
    ):
        from omr_scanner.errors import OMRScannerError

        def broken(*_args: object, **_kwargs: object) -> None:
            raise OMRScannerError("disk full", user_message="disk full")

        spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
        page = ScanPage(spec)
        qtbot.addWidget(page)
        page.on_project_changed(project_session)
        template_path = project_session.project.layout.templates_dir / "sheet.omrt"
        template_path.parent.mkdir(parents=True, exist_ok=True)
        save_template(template, template_path)
        assert page.load_template_from(template_path)
        page.add_scan_paths(sheets[:3])
        monkeypatch.setattr(batch_store, "record_results", broken)
        monkeypatch.setattr(
            "omr_scanner.gui.scan.page.QMessageBox.warning",
            staticmethod(lambda *_a, **_k: None),
        )
        with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS):
            assert page.process_all() is True
        assert page._last_snapshot.completed == 0
        assert page.progress_counts_label.text() == "0 / 3 processed · 3 not saved"
        # Still listed (and exportable), never counted.
        assert all(entry.processed is not None for entry in page.state.entries)
        page.shutdown_batch()
