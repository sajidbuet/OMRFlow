"""Scan sessions on the real Scan stage (0.1.1 phase 2).

Driven through the methods the buttons and the *Session* menu call (never a
modal), with a real project and the real recognition engine; the database is
the oracle. Covers implicit creation, the header, the menu actions, a later
Process All in the same session, sealed-batch refusal, *Reprocess All* as a
superseding batch, a rescan of a sealed batch, and that the finite workflow
asks nothing new.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.domain.scan_sessions import BatchMembership, BatchRole, ScanSessionState
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services import batch_store, open_project, save_template, scan_sessions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

BATCH_TIMEOUT_MS = 120_000
OPERATOR = "Scan Operator"


def _marks(roll: str) -> dict:
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
def make_scans(tmp_path: Path, template):
    def make(count: int, *, start: int = 0, folder: str = "scans") -> list[Path]:
        directory = tmp_path / folder
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(count):
            path = directory / f"scan_{start + index:03d}.png"
            sheet = render_marked_sheet(template, _marks(f"{100000 + start + index}"))
            cv2.imwrite(str(path), sheet)
            paths.append(path)
        return paths

    return make


def _page(qtbot, project_session: ProjectSession, template_path: Path) -> ScanPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    page = ScanPage(spec)
    qtbot.addWidget(page)
    page.set_reviewer(OPERATOR)
    page.on_project_changed(project_session)
    assert page.load_template_from(template_path) is True
    return page


@pytest.fixture
def page(qtbot, project_session: ProjectSession, template_path: Path) -> ScanPage:
    return _page(qtbot, project_session, template_path)


@pytest.fixture
def no_modals(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record - and refuse - any message box or input dialog."""
    seen: list[str] = []

    def record(*args: object, **_kwargs: object) -> object:
        seen.append(str(args[1]) if len(args) > 1 else "?")
        return QMessageBox.StandardButton.Cancel

    for name in ("information", "warning", "question", "critical"):
        monkeypatch.setattr(QMessageBox, name, record)
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: ("", False))
    return seen


def run(qtbot, page: ScanPage, start) -> None:
    with qtbot.waitSignal(page.batch_finished, timeout=BATCH_TIMEOUT_MS):
        assert start() is True


class TestImplicitSession:
    def test_the_first_process_all_creates_a_session_and_asks_nothing(
        self, qtbot, page: ScanPage, project_session, make_scans, no_modals
    ) -> None:
        assert "a new one starts with the first Process All" in page.session_label.text()
        page.add_scan_paths(make_scans(2))
        run(qtbot, page, page.process_all)
        assert no_modals == []
        active = scan_sessions.active_scan_session(project_session.database)
        assert active is not None and active.origin == "implicit"
        assert active.name in page.session_label.text().replace("<b>", "").replace("</b>", "")
        info = scan_sessions.batch_info(project_session.database, page.state.batch_id)
        assert info.scan_session_id == active.scan_session_id

    def test_a_later_process_all_after_reopening_joins_the_same_session(
        self, qtbot, project_session, template_path, make_scans
    ) -> None:
        page = _page(qtbot, project_session, template_path)
        page.add_scan_paths(make_scans(1))
        run(qtbot, page, page.process_all)
        first = page.state.batch_id
        root = project_session.root
        page.shutdown_batch()
        project_session.close()
        with open_project(root) as reopened:
            page.on_project_changed(reopened)
            assert page.state.batch_id is None
            page.clear_scans()
            page.add_scan_paths(make_scans(1, start=10, folder="later"))
            run(qtbot, page, page.process_all)
            second = page.state.batch_id
            database = reopened.database
            one, two = scan_sessions.batches_info(database, (first, second))
            assert first != second
            assert one.scan_session_id == two.scan_session_id
            assert len(scan_sessions.list_scan_sessions(database)) == 1
            assert one.membership is BatchMembership.SEALED
            page.on_project_changed(None)


class TestSessionMenu:
    def test_new_rename_close_reopen(
        self, qtbot, page: ScanPage, project_session, make_scans
    ) -> None:
        database = project_session.database
        page.add_scan_paths(make_scans(1))
        run(qtbot, page, page.process_all)
        first_session = scan_sessions.active_scan_session(database).scan_session_id
        assert page.rename_active_scan_session("Morning sitting") is True
        assert "Morning sitting" in page.session_label.text()
        assert page.close_active_scan_session() is True
        assert "closed" in page.session_label.text()
        assert scan_sessions.batch_info(database, page.state.batch_id).membership is (
            BatchMembership.SEALED
        )
        assert page.reopen_session_action.isEnabled()
        assert page.reopen_active_scan_session() is True
        assert scan_sessions.active_scan_session(database).state is ScanSessionState.OPEN
        assert page.new_scan_session("Afternoon") is True
        active = scan_sessions.active_scan_session(database)
        assert active.name == "Afternoon" and active.scan_session_id != first_session
        assert page.state.batch_id is None

    def test_combine_into_this_session(
        self, qtbot, page: ScanPage, project_session, make_scans
    ) -> None:
        database = project_session.database
        page.add_scan_paths(make_scans(1))
        run(qtbot, page, page.process_all)
        first_session = scan_sessions.active_scan_session(database).scan_session_id
        assert page.new_scan_session("Second") is True
        page.clear_scans()
        page.add_scan_paths(make_scans(1, start=5, folder="second"))
        run(qtbot, page, page.process_all)
        moved = page.state.batch_id
        assert page.combine_into_active_session([first_session]) is True
        active = scan_sessions.active_scan_session(database)
        assert active.batch_count == 2
        assert scan_sessions.session_of_batch(database, moved) == active.scan_session_id

    def test_a_closed_session_refuses_a_new_run(
        self, qtbot, page: ScanPage, make_scans, no_modals
    ) -> None:
        page.add_scan_paths(make_scans(1))
        run(qtbot, page, page.process_all)
        assert page.close_active_scan_session() is True
        page.add_scan_paths(make_scans(1, start=3, folder="late"))
        assert page.process_all() is False
        assert no_modals == ["Batch sealed"]
        assert "Reopen the scan session" in page.progress_label.text()
        # After reopening, the next Process All registers a new batch.
        assert page.reopen_active_scan_session() is True
        assert page.state.batch_id is None

    def test_the_menu_is_disabled_without_a_writable_project(self, qtbot, page: ScanPage) -> None:
        page.on_project_changed(None)
        assert not page.session_menu_button.isEnabled()


class TestDownstreamThroughTheWindow:
    def test_downstream_stages_read_the_session_batch_not_the_rescan_batch(
        self, qtbot, tmp_path, workspace, template, make_scans
    ) -> None:
        from omr_scanner.config import AppConfig
        from omr_scanner.gui.main_window import MainWindow
        from omr_scanner.services import create_project

        created = create_project(workspace, "Downstream")
        root = created.root
        template_file = save_template(template, created.project.layout.templates_dir / "t.omrt")
        created.close()
        window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
        qtbot.addWidget(window)
        assert window.open_project_at(root) is True
        scan = window._scan_page()
        assert scan is not None and scan.load_template_from(template_file)
        scan.add_scan_paths(make_scans(2))
        run(qtbot, scan, scan.process_all)
        original = scan.state.batch_id
        database = window.session.database
        for stage in (window._attendance_page(), window._results_page(), window._reports_page()):
            assert stage.state.batch_id == original
        scan_sessions.seal_batch(database, original)
        run(qtbot, scan, lambda: scan.import_rescans(original, make_scans(1, start=70, folder="r")))
        assert scan.state.batch_id != original
        for stage in (window._attendance_page(), window._results_page(), window._reports_page()):
            assert stage.state.batch_id == original
        # Reopening the project reads the same batch again (defect 2).
        window.close_project()
        assert window.open_project_at(root) is True
        assert window._results_page().state.batch_id == original
        window.close_project()


class TestReprocessAndRescan:
    def test_reprocess_all_creates_a_superseding_batch(
        self, qtbot, page: ScanPage, project_session, make_scans
    ) -> None:
        database = project_session.database
        page.add_scan_paths(make_scans(2))
        run(qtbot, page, page.process_all)
        original = page.state.batch_id
        run(qtbot, page, page.reprocess_all)
        reprocess = page.state.batch_id
        assert reprocess != original
        old, new = scan_sessions.batches_info(database, (original, reprocess))
        assert new.role is BatchRole.REPROCESS and old.superseded_by == reprocess
        assert batch_store.load_summary(database, original).processed == 2
        assert batch_store.load_summary(database, reprocess).processed == 2
        # 0.1.1 phase 4 changed this on purpose. Phase 2 pointed downstream at
        # the reprocess batch; downstream now reads the *session*, under a key
        # that never moves (its oldest batch), and the population is drawn from
        # the live batches only - the superseded original contributes nothing.
        from omr_scanner.services import session_population

        assert scan_sessions.downstream_batch_id(database) == original
        population = session_population.population(database, original)
        assert population.live_batch_ids == (reprocess,)
        assert {population.batch_of[item] for item in population.effective} == {reprocess}

    def test_a_rescan_for_a_sealed_batch_becomes_a_rescan_batch(
        self, qtbot, page: ScanPage, project_session, make_scans
    ) -> None:
        database = project_session.database
        page.add_scan_paths(make_scans(2))
        run(qtbot, page, page.process_all)
        original = page.state.batch_id
        scan_sessions.seal_batch(database, original, sealed_by=OPERATOR)
        rescans = make_scans(1, start=50, folder="rescans")
        run(qtbot, page, lambda: page.import_rescans(original, rescans))
        rescan_batch = page.state.batch_id
        info = scan_sessions.batch_info(database, rescan_batch)
        assert info.role is BatchRole.RESCAN
        assert info.scan_session_id == scan_sessions.session_of_batch(database, original)
        assert batch_store.load_summary(database, original).total == 2
        # Downstream stays on the original, where a confirmed rescan counts.
        assert scan_sessions.downstream_batch_id(database) == original

    def test_a_rescan_into_the_open_batch_behaves_as_before(
        self, qtbot, page: ScanPage, project_session, make_scans
    ) -> None:
        database = project_session.database
        page.add_scan_paths(make_scans(2))
        run(qtbot, page, page.process_all)
        original = page.state.batch_id
        run(qtbot, page, lambda: page.import_rescans(original, make_scans(1, start=60, folder="r")))
        assert page.state.batch_id == original
        assert batch_store.load_summary(database, original).total == 3
