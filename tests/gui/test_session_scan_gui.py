"""The Scan stage with and without session mode, and the window around it (revised phase 8).

* **Finite mode is unchanged** (ACCEPTANCE_CRITERIA F1): a project processed
  with *Add Folder -> Process All* never sees session mode - no panel, no
  list chooser, no engine, the same Process All - and no polling thread.
* Session mode appears only for a session with an intake source, and the
  sources section is compact and folded by default (F4).
* The window: closing or switching a project while continuous scanning runs
  stops the engine safely first (no thread left running, no coordinator lease
  held, the session left open); the relayed snapshot refreshes Resolve while
  Resolve is open, including a cross-batch duplicate from another scanner.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication
from tests.gui.session_gui_rig import OPERATOR, SHEETS, SessionGuiRig

from omr_scanner.config import AppConfig
from omr_scanner.domain.review import ConflictType
from omr_scanner.domain.session_snapshot import SessionActivity
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.gui.scan.session_panel import (
    activity_text,
    counts_text,
    progress_text,
    secondary_counts_text,
)
from omr_scanner.services import coordinator, save_template, scan_sessions, set_active_template

pytestmark = pytest.mark.gui


def _qthreads() -> list[QThread]:
    app = QApplication.instance()
    return [item for item in app.findChildren(QThread) if item.isRunning()] if app else []


class TestFiniteModeIsUnchanged:
    def test_a_finite_project_shows_no_session_mode(self, qtbot, project_session):
        spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
        page = ScanPage(spec)
        qtbot.addWidget(page)
        page.set_reviewer(OPERATOR)
        page.on_project_changed(project_session)
        assert not page.session_mode.active
        assert not page.session_panel.isVisibleTo(page)
        assert not page.list_scope_row.isVisibleTo(page)
        assert page.list_stack.currentIndex() == 0  # the batch list, as before
        assert page.session_mode.poller.watching == ""
        assert page.session_mode.poller._thread is None  # no polling thread at all
        # No session progress lines either: the finite stage keeps its one bar
        # (test_batch_progress_gui counts them; it caught this in the phase 8 gate).
        assert page.session_panel.progress_bars == {}
        assert not page.session_mode.running
        # The Session menu offers the way in, without requiring it.
        assert page.configure_sources_action.isEnabled()
        assert page.process_all_button.toolTip() == page._finite_tooltips["processAllButton"]

    def test_an_implicit_finite_session_still_shows_no_session_mode(self, qtbot, project_session):
        scan_sessions.create_scan_session(project_session.database, name="Implicit")
        spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
        page = ScanPage(spec)
        qtbot.addWidget(page)
        page.on_project_changed(project_session)
        assert not page.session_mode.active


class TestSessionMode:
    def test_a_session_with_a_source_shows_the_compact_panel(
        self, qtbot, project_session, monkeypatch
    ):
        rig = SessionGuiRig(project_session, monkeypatch)
        rig.source("a")
        page = rig.build_page(qtbot)
        assert page.session_mode.active
        assert page.session_panel.isVisibleTo(page)
        assert page.list_scope_row.isVisibleTo(page)
        assert page.list_stack.currentIndex() == 1  # the paged session list
        # Sources are secondary: folded by default, summarised in one line.
        assert not page.session_panel.sources_section.is_expanded
        assert "1 source(s)" in page.session_panel.sources_section.summary_label.text()
        # The three lines are separate and none is a percentage.
        for line in ("recognition", "conflicts", "rescans"):
            bar = page.session_panel.progress_bars[line]
            assert not bar.isTextVisible()
            assert "%" not in page.session_panel.progress_labels[line].text()

    def test_adding_a_source_from_the_menu_creates_the_session_only_when_none_exists(
        self, qtbot, project_session, tmp_path
    ):
        from omr_scanner.gui.scan.session_dialogs import SourceDraft

        spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
        page = ScanPage(spec)
        qtbot.addWidget(page)
        page.set_reviewer(OPERATOR)
        page.on_project_changed(project_session)
        assert scan_sessions.active_scan_session(project_session.database) is None
        folder = tmp_path / "scanner-a"
        folder.mkdir()
        page.session_mode.add_source(SourceDraft("Scanner A", str(folder)))
        page.session_mode.add_source(
            SourceDraft("Scanner B", r"\\scanner-pc-b\scans")  # typed UNC, kept even unreachable
        )
        sessions = scan_sessions.list_scan_sessions(project_session.database)
        assert len(sessions) == 1
        assert page.session_mode.active

        def listed() -> set[str]:
            view = page.session_mode.view
            return {item.label for item in view.sources} if view is not None else set()

        qtbot.waitUntil(lambda: listed() == {"Scanner A", "Scanner B"}, timeout=30_000)
        view = page.session_mode.view
        assert view is not None
        stored = {item.label: item.root_path for item in view.sources}
        assert stored["Scanner B"] == r"\\scanner-pc-b\scans"


class TestWordsForValues:
    """The panel's words come from the snapshot's values (pure functions)."""

    def test_recognition_can_fall_and_is_never_a_percentage(self):
        from omr_scanner.domain.session_snapshot import Progress

        assert progress_text(Progress(100, 100), "recognition") == "100 / 100 read"
        assert progress_text(Progress(100, 120), "recognition") == "100 / 120 read"
        assert progress_text(Progress(0, 0), "conflicts") == "none"
        assert "%" not in progress_text(Progress(3, 6), "rescans")


class TestWindowIntegration:
    def test_closing_the_window_while_scanning_stops_safely_and_leaves_the_session_open(
        self, qtbot, project_session, monkeypatch, tmp_path
    ):
        rig = SessionGuiRig(project_session, monkeypatch)
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(3)])
        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        window.apply_reviewer_name(OPERATOR)
        window._session = rig.project  # the rig's project, as the window would hold it
        scan = window._scan_page()
        assert scan is not None
        scan.state.template = rig.template
        scan.session_mode.engine_factory = rig.factory()  # type: ignore[assignment]
        scan.session_mode.poller.clock = rig.clock
        window._broadcast_project_change()
        with qtbot.waitSignal(scan.session_mode.runner.engine_started, timeout=30_000):
            assert scan.session_mode.start()
        assert window.continuous_scan_is_running()
        rig.page = scan
        rig.wait_for(rig.processed(1), tick=3)
        # A programmatic close asks nothing (no modal) and stops safely.
        window._settle_background_work()
        assert not scan.session_mode.running
        assert coordinator.holder_of(rig.database) is None
        assert not any(item.objectName() == "continuousEngineThread" for item in _qthreads())
        info = scan_sessions.get_scan_session(rig.database, rig.session_id)
        assert info is not None and info.state.value == "open"

    def test_the_relayed_snapshot_refreshes_resolve_with_a_cross_scanner_duplicate(
        self, qtbot, project_session, monkeypatch, tmp_path
    ):
        rig = SessionGuiRig(project_session, monkeypatch)
        rig.source("a")
        rig.source("b")
        # SHEETS 17 and 18 of the seed share a Student ID on different images.
        from tests.engine_rig import readable_sheets

        pair = readable_sheets(20)
        # The project's own template, as a real project has it.
        template_file = rig.project.project.layout.templates_dir / "rig.omrt"
        template_file.parent.mkdir(parents=True, exist_ok=True)
        save_template(rig.template, template_file)
        set_active_template(rig.project, template_file)
        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        window.apply_reviewer_name(OPERATOR)
        window._session = rig.project
        scan = window._scan_page()
        resolve = window._resolve_page()
        assert scan is not None and resolve is not None
        scan.state.template = rig.template
        scan.session_mode.engine_factory = rig.factory()  # type: ignore[assignment]
        scan.session_mode.poller.clock = rig.clock
        window._broadcast_project_change()
        window.resize(1366, 768)
        window.show()
        window.show_page("resolve")
        rig.write("a", [("first.png", pair[17])])
        with qtbot.waitSignal(scan.session_mode.runner.engine_started, timeout=30_000):
            assert scan.session_mode.start()
        rig.page = scan
        rig.wait_for(rig.processed(1), tick=3)
        # Resolve anchored itself to the session from the relayed snapshot.
        deadline = time.monotonic() + 60
        while resolve.state.scan_session_id != rig.session_id and time.monotonic() < deadline:
            QApplication.processEvents()
        assert resolve.state.scan_session_id == rig.session_id
        # A late duplicate at the *other* scanner, while Resolve is open.
        scan.session_mode.pause_intake()
        rig.write("b", [("second.png", pair[18])])
        scan.session_mode.resume_intake()

        def duplicate_listed() -> bool:
            rig.clock.advance(3)
            return any(
                item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
                for item in resolve.state.conflicts
            )

        deadline = time.monotonic() + 120
        while not duplicate_listed() and time.monotonic() < deadline:
            scan.session_mode.poller.refresh()
            for _ in range(10):
                QApplication.processEvents()
            time.sleep(0.05)
        duplicates = [
            item for item in resolve.state.conflicts
            if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        ]
        assert duplicates, "the cross-scanner duplicate reached Resolve without visiting Scan"
        # Navigable across batches: its related sheet is the other scanner's.
        assert any(item.related_scan_ids for item in duplicates)
        assert resolve.current_conflict() is not None
        # Anchored by the snapshot, not handed a batch by Scan: the project's
        # template still re-reads the sheet, so its evidence is shown (a defect
        # found in the phase 8 screenshots - Resolve stayed blank).
        assert resolve.state.template is not None
        deadline = time.monotonic() + 60
        while resolve.state.bundle is None and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        assert resolve.state.bundle is not None
        window._settle_background_work()


OUR_THREADS = {
    "continuousEngineThread",
    "sessionSnapshotThread",
    "sessionSheetListThread",
    "resolveLiveQueueThread",
    "finishSessionThread",
}


class TestShutdownOrder:
    def test_rapid_project_switches_while_scanning_leave_no_thread_or_lease(
        self, qtbot, tmp_path, monkeypatch
    ):
        from omr_scanner.services import create_project, open_project

        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        window.apply_reviewer_name(OPERATOR)
        project = create_project(tmp_path, "Switching")
        root = project.root
        rig = SessionGuiRig(project, monkeypatch)
        rig.source("a")
        rig.write("a", [(f"{i}.png", SHEETS[i]) for i in range(4)])
        window._session = project
        scan = window._scan_page()
        assert scan is not None
        scan.state.template = rig.template
        scan.session_mode.engine_factory = rig.factory()  # type: ignore[assignment]
        scan.session_mode.poller.clock = rig.clock
        window._broadcast_project_change()
        for _round in range(2):
            with qtbot.waitSignal(scan.session_mode.runner.engine_started, timeout=30_000):
                assert scan.session_mode.start()
            # Close the project under the running engine: it must settle first.
            window.close_project()
            assert not scan.session_mode.running
            assert coordinator.holder_of(rig.database) is None
            assert not [item for item in _qthreads() if item.objectName() in OUR_THREADS]
            reopened = open_project(root)
            window._adopt_session(reopened)
            scan.state.template = rig.template
            rig.project, rig.database = reopened, reopened.database
            scan.session_mode.engine_factory = rig.factory()  # type: ignore[assignment]
        window.close_project()
        assert not [item for item in _qthreads() if item.objectName() in OUR_THREADS]

    def test_quitting_while_paused_or_with_an_unreachable_source_is_clean(
        self, qtbot, project_session, monkeypatch, tmp_path
    ):
        rig = SessionGuiRig(project_session, monkeypatch)
        rig.source("a")
        rig.source("b")
        rig.fs.unreachable.add(rig.root("b"))
        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        window.apply_reviewer_name(OPERATOR)
        window._session = rig.project
        scan = window._scan_page()
        assert scan is not None
        scan.state.template = rig.template
        scan.session_mode.engine_factory = rig.factory()  # type: ignore[assignment]
        scan.session_mode.poller.clock = rig.clock
        window._broadcast_project_change()
        with qtbot.waitSignal(scan.session_mode.runner.engine_started, timeout=30_000):
            assert scan.session_mode.start()
        assert scan.session_mode.pause_processing()
        rig.page = scan
        rig.wait_for(
            lambda: scan.session_mode.view is not None
            and scan.session_mode.view.snapshot.activity is SessionActivity.PROCESSING_PAUSED
        )
        window.close()  # programmatic: no question, a safe stop
        QApplication.processEvents()
        assert not scan.session_mode.running
        assert not [item for item in _qthreads() if item.objectName() in OUR_THREADS]

    def test_the_exit_question_defaults_to_cancel_and_says_the_session_stays_open(
        self, qtbot, tmp_path, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        seen: dict[str, str] = {}

        def answer(box: QMessageBox) -> int:
            seen["default"] = box.defaultButton().text()
            seen["text"] = box.text()
            return 0  # nothing clicked: Escape / closed

        monkeypatch.setattr(QMessageBox, "exec", answer)
        assert window.confirm_exit_while_scanning() is False
        assert seen["default"] == "Cancel"
        assert "stays OPEN" in seen["text"]
        assert "Finish Scan Session" in seen["text"]


def test_activity_words_name_the_unreachable_scanner():
    from datetime import UTC, datetime

    from omr_scanner.domain.session_controls import SessionControls
    from omr_scanner.domain.session_snapshot import Partition, Progress, SessionSnapshot
    from omr_scanner.gui.scan.session_poller import SessionView
    from omr_scanner.services.intake import SourceInfo

    snapshot = SessionSnapshot(
        scan_session_id="s", session_state="open", taken_at=datetime.now(UTC),
        activity=SessionActivity.WAITING_FOR_SOURCE, caught_up=False, partition=Partition(),
        discovered_excluding_ignored=0, ignored=0, recognition=Progress(0, 0),
        conflicts=Progress(0, 0), rescans=Progress(0, 0), outstanding_suggestions=0,
        retry_processing=0, pending_decisions=0, controls=SessionControls("s"), sources=(),
        disabled_sources=(), unreachable_sources=("Scanner B",), running_batches=0,
    )
    since = datetime(2026, 10, 4, 10, 42, tzinfo=UTC)
    info = SourceInfo.__new__(SourceInfo)
    object.__setattr__(info, "label", "Scanner B")
    object.__setattr__(info, "reachability_changed_at", since)
    view = SessionView(snapshot=snapshot, session=None, sources=(info,))
    text = activity_text(view)
    assert text.startswith("Waiting for Scanner B (unreachable since ")
    assert text.endswith(f"{since.astimezone():%H:%M})")
    assert counts_text(snapshot).startswith("Discovered 0")
    assert secondary_counts_text(Partition(duplicate=2)) == "Duplicate 2"
