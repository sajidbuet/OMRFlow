"""The real Scan stage holds the project's coordinator lease for exactly its run (revised phase 7).

*Add Folder -> Process All* is unchanged for the operator: one batch, read and
settled as before. Underneath, the run takes the lease before it registers or
claims anything and gives it back once the batch has left ``running``; while
it holds it, the continuous engine is refused, and while the engine holds it,
*Process All* is refused with the typed message and writes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import pytest
from sqlalchemy import func, select
from tests.conftest import build_answer_sheet_template, render_marked_sheet
from tests.gui.test_exact_duplicates_gui import TIMEOUT_MS, _marks

from omr_scanner.database.models import BatchScan, ScanBatch
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services import coordinator, save_template, scan_sessions
from omr_scanner.services.continuous_engine import ContinuousEngine
from omr_scanner.services.coordinator import CoordinatorBusyError, CoordinatorKind
from omr_scanner.services.recognition_pool import InlineRecogniser

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui


class Watch:
    """RunHooks that look at the lease from inside the run (worker thread)."""

    def __init__(self, database) -> None:
        self.database = database
        self.holder_during_run: CoordinatorKind | None = None
        self.engine_refused = False

    def submitted(self, batch_id, paths) -> None:
        holder = coordinator.holder_of(self.database)
        self.holder_during_run = holder.kind if holder is not None else None
        try:
            coordinator.acquire(
                self.database, CoordinatorKind.CONTINUOUS_ENGINE, owner=self
            ).release()
        except CoordinatorBusyError:
            self.engine_refused = True

    def started(self, path) -> None:
        pass

    def committed(self, batch_id, outcomes) -> None:
        pass

    def run_recognised(self, batch_id) -> None:
        pass

    def review_state_completed(self, batch_id) -> None:
        pass


def _page(qtbot, project_session: ProjectSession, tmp_path: Path) -> ScanPage:
    template = build_answer_sheet_template()
    path = project_session.project.layout.templates_dir / "sheet.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    template_file = save_template(template, path)
    folder = tmp_path / "exam scans"
    folder.mkdir()
    for index, roll in enumerate(("170501", "170502")):
        cv2.imwrite(str(folder / f"{index:06d}.png"), render_marked_sheet(template, _marks(roll)))
    page = ScanPage(next(item for item in WORKFLOW_PAGES if item.key == "scan"))
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    assert page.load_template_from(template_file) is True
    page.add_scan_paths([folder])
    return page


def test_the_finite_run_holds_the_lease_for_its_run_only(qtbot, project_session, tmp_path):
    page = _page(qtbot, project_session, tmp_path)
    watch = Watch(project_session.database)
    page.run_hooks = watch
    with qtbot.waitSignal(page.batch_finished, timeout=TIMEOUT_MS) as finished:
        assert page.process_all() is True
    assert len(finished.args[0].processed) == 2
    assert watch.holder_during_run is CoordinatorKind.FINITE_SCAN
    assert watch.engine_refused
    assert coordinator.holder_of(project_session.database) is None  # released after settling
    with project_session.database.session() as session:
        assert session.scalar(
            select(func.count()).select_from(BatchScan).where(
                BatchScan.status.in_(["completed", "warning"])
            )
        ) == 2
    page.shutdown_batch()
    page.on_project_changed(None)


def test_process_all_is_refused_while_the_engine_runs_and_writes_nothing(
    qtbot, project_session, tmp_path
):
    page = _page(qtbot, project_session, tmp_path)
    database = project_session.database
    scan_session = scan_sessions.create_scan_session(database, name="Live", created_by="op")
    template = build_answer_sheet_template()
    engine = ContinuousEngine(
        database, scan_session_id=scan_session.scan_session_id, template=template,
        recogniser=InlineRecogniser(template),
    )
    engine.start()
    try:
        assert page.process_all() is False
        assert "already being processed" in page.progress_label.text()
        with database.session() as session:
            assert session.scalar(select(func.count()).select_from(ScanBatch)) == 0
    finally:
        engine.shutdown()
    with qtbot.waitSignal(page.batch_finished, timeout=TIMEOUT_MS):
        assert page.process_all() is True
    page.shutdown_batch()
    page.on_project_changed(None)
