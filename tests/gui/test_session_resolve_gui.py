"""Resolve over a scan session of several batches (0.1.1 phase 4), real widgets.

Two Process All runs in one scan session, the second reading a Student ID the
first already holds. The Resolve stage, opened on either batch, shows the
session's queue - the cross-batch duplicate included - loads a sheet of the
other batch, records a decision against that sheet's own batch, and rebuilds
the same queue after the project is reopened, without visiting Scan. The
database is the oracle; every wait is on a signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import pytest
from PySide6.QtWidgets import QMessageBox
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.database.models import ReviewConflict
from omr_scanner.domain.review import ConflictState, ConflictType
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.review.page import ResolvePage
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services import (
    batch_store,
    open_project,
    review_store,
    save_template,
    scan_sessions,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.services import ProjectSession

pytestmark = pytest.mark.gui

TIMEOUT_MS = 120_000
REVIEWER = "Dr. Rahman"


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
def write(tmp_path: Path, template):
    def make(folder: str, name: str, roll: str, *, compression: int = 3) -> Path:
        directory = tmp_path / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        cv2.imwrite(
            str(path),
            render_marked_sheet(template, _marks(roll)),
            [cv2.IMWRITE_PNG_COMPRESSION, compression],
        )
        return path

    return make


def _resolve(qtbot, project_session, template) -> ResolvePage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "resolve")
    page = ResolvePage(spec)
    qtbot.addWidget(page)
    page.on_project_changed(project_session)
    page.set_reviewer(REVIEWER)
    return page


@pytest.fixture
def two_batches(qtbot, project_session: ProjectSession, template, write, monkeypatch):
    def refuse(*args: object, **_kwargs: object) -> object:
        raise AssertionError(f"unexpected modal: {args[1:3]}")

    for name in ("information", "warning", "question", "critical"):
        monkeypatch.setattr(QMessageBox, name, refuse)
    path = project_session.project.layout.templates_dir / "sheet.omrt"
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = next(item for item in WORKFLOW_PAGES if item.key == "scan")
    scan = ScanPage(spec)
    qtbot.addWidget(scan)
    scan.on_project_changed(project_session)
    template_file = save_template(template, path)
    assert scan.load_template_from(template_file) is True
    scan.add_scan_paths([write("one", "a.png", "170504"), write("one", "b.png", "170505")])
    with qtbot.waitSignal(scan.batch_finished, timeout=TIMEOUT_MS):
        assert scan.process_all() is True
    first = scan.state.batch_id
    # Leaving and returning to the project, as an operator does between two
    # sittings: the next Process All opens a new batch of the same session.
    scan.shutdown_batch()
    scan.on_project_changed(None)
    scan.on_project_changed(project_session)
    assert scan.state.batch_id is None
    assert scan.load_template_from(template_file) is True
    scan.clear_scans()
    # A second, genuinely different scan of the same Student ID: other bytes
    # (an identical file is an exact duplicate and is never read, 0.1.1 phase 4).
    scan.add_scan_paths([write("two", "c.png", "170504", compression=1)])
    with qtbot.waitSignal(scan.batch_finished, timeout=TIMEOUT_MS):
        assert scan.process_all() is True, scan.progress_label.text()
    second = scan.state.batch_id
    assert first != second
    assert scan_sessions.session_of_batch(
        project_session.database, first
    ) == scan_sessions.session_of_batch(project_session.database, second)
    scan.shutdown_batch()
    scan.on_project_changed(None)
    ids = {
        path.name: scan_id
        for batch in (first, second)
        for path, scan_id in batch_store.scan_ids_by_path(
            project_session.database, batch
        ).items()
    }
    return first, second, ids


def duplicate_rows(database) -> dict[int, tuple[str, str]]:
    with database.session() as session:
        return {
            row.scan_id: (row.batch_id, row.state)
            for row in session.query(ReviewConflict)
            .filter(ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value)
            .all()
        }


class TestSessionResolve:
    def test_the_queue_is_the_sessions_from_either_batch(
        self, qtbot, project_session, template, two_batches
    ):
        first, second, ids = two_batches
        rows = duplicate_rows(project_session.database)
        assert rows[ids["a.png"]][0] == first and rows[ids["c.png"]][0] == second
        page = _resolve(qtbot, project_session, template)
        for batch in (first, second):
            assert page.load_batch(batch, template) is True
            queued = {item.scan_id for item in page.state.conflicts}
            assert {ids["a.png"], ids["c.png"]} <= queued
            assert "Scan session" in page.batch_label.text()
        page.close()

    def test_a_decision_on_the_other_batchs_sheet_is_kept_in_that_batch(
        self, qtbot, project_session, template, two_batches
    ):
        first, second, ids = two_batches
        page = _resolve(qtbot, project_session, template)
        assert page.load_batch(first, template) is True
        target = next(
            item
            for item in page.state.conflicts
            if item.scan_id == ids["c.png"]
            and item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        )
        with qtbot.waitSignal(page.sheet_ready, timeout=TIMEOUT_MS):
            assert page.select_conflict_by_id(target.conflict_id) is True
        assert page.state.sheet_conflicts, "the sheet's own records were not loaded"
        assert {item.scan_id for item in page.state.sheet_conflicts} == {ids["c.png"]}
        assert page.accept_machine() is True
        record = review_store.get_conflict(project_session.database, target.conflict_id)
        assert record is not None and record.state is ConflictState.RESOLVED
        assert duplicate_rows(project_session.database)[ids["c.png"]][0] == second
        page.close()

    def test_reopening_rebuilds_the_same_queue_without_the_scan_stage(
        self, qtbot, project_session, template, two_batches
    ):
        first, _second, _ids = two_batches
        page = _resolve(qtbot, project_session, template)
        assert page.load_batch(first, template) is True
        before = sorted(item.conflict_id for item in page.state.conflicts)
        page.close()
        root = project_session.root
        project_session.close()
        with open_project(root) as reopened:
            again = _resolve(qtbot, reopened, template)
            key = scan_sessions.downstream_batch_id(reopened.database)
            assert key == first
            assert again.load_batch(key, template) is True
            assert sorted(item.conflict_id for item in again.state.conflicts) == before
            again.on_project_changed(None)
            again.close()


class TestBatchFilter:
    def test_a_batch_filter_narrows_the_view_and_nothing_else(
        self, qtbot, project_session, template, two_batches
    ):
        first, second, ids = two_batches
        page = _resolve(qtbot, project_session, template)
        assert page.load_batch(first, template) is True
        items = [page.batch_filter.itemData(index) for index in range(page.batch_filter.count())]
        assert items == ["", first, second]
        everything = {item.scan_id for item in page.state.conflicts}
        counts_before = review_store.count_conflicts(
            project_session.database, first, session_wide=True
        )
        page.batch_filter.setCurrentIndex(2)
        narrowed = {item.scan_id for item in page.state.conflicts}
        assert narrowed == {ids["c.png"]} and narrowed < everything
        # The duplicate group still spans both batches; the session's counts
        # are the same whatever is viewed.
        duplicate = next(
            item for item in page.state.conflicts
            if item.conflict_type is ConflictType.IDENTIFIER_DUPLICATE
        )
        assert ids["a.png"] in duplicate.related_scan_ids
        assert review_store.count_conflicts(
            project_session.database, first, session_wide=True
        ) == counts_before
        page.batch_filter.setCurrentIndex(0)
        assert {item.scan_id for item in page.state.conflicts} == everything
        page.close()
