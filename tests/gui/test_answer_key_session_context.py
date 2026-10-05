"""The Answer Key stage names the scan session it is working through (revised phase 8).

Scope:
    The page's one-line session context - the same words Resolve uses
    (:mod:`omr_scanner.gui.session_context`) - for an open, closed and reopened
    session; that it follows the selection the main window hands every
    downstream stage; that it clears on a project switch and with no project;
    and that a finite (implicit) session gets the same single line, nothing
    more.

    The context is presentation only: answer keys stay the project's, per set,
    and showing another session - or none - changes no key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from PySide6.QtWidgets import QAbstractButton, QMessageBox
from sqlalchemy import update
from tests.integration.test_reject_and_rescan import OPERATOR, build_world
from tests.integration.test_session_population import SessionWorld

from omr_scanner.config import AppConfig
from omr_scanner.database.models import ScanBatch
from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.gui.answer_key.page import AnswerKeyPage
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.services import create_project, scan_lifecycle, scan_sessions, scoring_store

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.gui


@pytest.fixture
def sw(workspace: Path, tmp_path: Path) -> Iterator[SessionWorld]:
    """A project whose one read batch is in the open scan session 'Midterm'."""
    session = create_project(workspace, "Answer Key Context")
    try:
        world = SessionWorld(build_world(session, tmp_path))
        # The two disputed sheets are set aside, so the session can be closed.
        for name in ("s_x.png", "s_blur.png"):
            scan_lifecycle.exclude_scan(
                world.database, world.ids[name], reviewer=OPERATOR,
                reason=RejectionReason.FOLDED,
            )
        yield world
    finally:
        if not session.is_closed:
            session.close()


def make_page(qtbot, project) -> AnswerKeyPage:
    spec = next(item for item in WORKFLOW_PAGES if item.key == "answer_key")
    page = AnswerKeyPage(spec)
    qtbot.addWidget(page)
    page.set_reviewer(OPERATOR)
    page.on_project_changed(project)
    return page


def shown(page: AnswerKeyPage) -> str:
    """The context line as the operator reads it ('' when hidden)."""
    label = page.session_label
    return label.text() if not label.isHidden() else ""


def close(sw: SessionWorld) -> None:
    assert scan_sessions.closure_blockers(sw.database, sw.scan_session_id) == ()
    scan_sessions.close_scan_session(sw.database, sw.scan_session_id, closed_by=OPERATOR)


class TestLifecycleWording:
    def test_an_open_session_is_named_and_provisional(self, qtbot, sw):
        page = make_page(qtbot, sw.world.session)
        assert page.state.scan_session_id == sw.scan_session_id
        text = shown(page)
        assert "Scan session <b>Midterm</b>" in text
        assert "open · results provisional" in text
        assert "reopened" not in text and "closed" not in text

    def test_a_closed_session_says_closed(self, qtbot, sw):
        page = make_page(qtbot, sw.world.session)
        close(sw)
        page.refresh_session_context()
        text = shown(page)
        assert "<b>Midterm</b> · closed" in text
        assert "provisional" not in text

    def test_a_reopened_session_says_so_and_is_provisional_again(self, qtbot, sw):
        page = make_page(qtbot, sw.world.session)
        close(sw)
        scan_sessions.reopen_scan_session(sw.database, sw.scan_session_id, reopened_by=OPERATOR)
        page.refresh_session_context()
        assert "<b>Midterm</b> · open (reopened) · results provisional" in shown(page)

    def test_a_close_made_elsewhere_shows_when_the_stage_is_shown_again(self, qtbot, sw):
        # Reports' "close and export" closes the session while this stage is
        # off screen; showing the stage re-reads it.
        page = make_page(qtbot, sw.world.session)
        page.show()
        qtbot.waitExposed(page)
        page.hide()
        close(sw)
        page.show()
        qtbot.waitExposed(page)
        assert "· closed" in shown(page)


class TestSelection:
    def test_no_project_shows_no_session(self, qtbot):
        page = make_page(qtbot, None)
        assert shown(page) == ""
        assert page.state.scan_session_id is None

    def test_closing_the_project_clears_the_name(self, qtbot, sw):
        page = make_page(qtbot, sw.world.session)
        assert "Midterm" in shown(page)
        page.on_project_changed(None)
        assert shown(page) == "" and page.session_label.text() == ""

    def test_switching_project_drops_the_old_session(self, qtbot, sw, tmp_path):
        page = make_page(qtbot, sw.world.session)
        assert "Midterm" in shown(page)
        other = create_project(tmp_path / "other", "Other Exam")
        try:
            page.on_project_changed(other)
            assert "Midterm" not in page.session_label.text()
            assert shown(page) == ""  # the other project has no session yet
        finally:
            page.on_project_changed(None)
            other.close()

    def test_it_follows_the_window_like_every_downstream_stage(
        self, qtbot, sw, tmp_path, monkeypatch
    ):
        root = sw.world.session.root
        sw.world.session.close()
        monkeypatch.setattr(
            QMessageBox, "question", lambda *_a, **_k: QMessageBox.StandardButton.Yes
        )
        window = MainWindow(AppConfig(reviewer_name=OPERATOR), config_path=tmp_path / "c.json")
        qtbot.addWidget(window)
        try:
            assert window.open_project_at(root)
            page, results = window._answer_key_page(), window._results_page()
            assert page is not None and results is not None
            # One selection: the same session Results marks.
            assert page.state.scan_session_id == results.state.scan_session_id
            assert page.state.scan_session_id == sw.scan_session_id
            assert "Midterm" in shown(page)

            # The active session changes (Scan stage): Answer Key follows.
            database = window._session.database
            later = scan_sessions.create_scan_session(database, name="Resit", created_by=OPERATOR)
            with database.session() as db:
                db.execute(
                    update(ScanBatch)
                    .where(ScanBatch.batch_id == sw.batches[0])
                    .values(scan_session_id=later.scan_session_id)
                )
            window._on_active_session_changed()
            assert page.state.scan_session_id == results.state.scan_session_id
            assert page.state.scan_session_id == later.scan_session_id
            assert "<b>Resit</b>" in shown(page) and "Midterm" not in shown(page)
        finally:
            window.close()


class TestKeysStayProjectOwned:
    def test_the_shown_session_changes_no_key(self, qtbot, sw):
        page = make_page(qtbot, sw.world.session)
        before = scoring_store.list_keys(sw.database)
        assert before  # the world verified a key per set
        page.set_session(None)
        page.set_session(sw.scan_session_id)
        close(sw)
        page.refresh_session_context()
        assert scoring_store.list_keys(sw.database) == before
        # The sets the page offers are the project's, whichever session is shown.
        assert set(page.set_codes()) >= {"1", "2", "3"}


class TestFiniteWorkflow:
    def test_an_implicit_session_gets_the_same_single_line(self, qtbot, workspace, tmp_path):
        # The finite "Process All" path: no session was ever made by hand; the
        # first batch created one, named after the exam and the date.
        project = create_project(workspace, "Finite Exam")
        try:
            world = build_world(project, tmp_path)
            implicit = scan_sessions.create_scan_session(world.database, origin="implicit")
            with world.database.session() as db:
                db.execute(
                    update(ScanBatch)
                    .where(ScanBatch.batch_id == world.batch_id)
                    .values(scan_session_id=implicit.scan_session_id)
                )
            page = make_page(qtbot, project)
            page.resize(1366, 768)
            page.show()
            qtbot.waitExposed(page)
            label = page.session_label
            assert f"<b>{implicit.name}</b>" in shown(page)
            # Context, not a panel: one wrapped label - no buttons, no list.
            assert label.findChildren(QAbstractButton) == []
            assert label.height() <= 2 * label.fontMetrics().lineSpacing() + 8
            # And no new step: what the operator can do is what it was without
            # the line.
            enabled = (page.entry_button.isEnabled(), page.set_combo.isEnabled())
            page.set_session(None)
            assert label.isHidden()
            assert (page.entry_button.isEnabled(), page.set_combo.isEnabled()) == enabled
        finally:
            project.close()

    def test_a_batch_outside_any_session_shows_nothing(self, qtbot, workspace, tmp_path):
        project = create_project(workspace, "Legacy Exam")
        try:
            build_world(project, tmp_path)  # one completed batch, no session
            page = make_page(qtbot, project)
            assert shown(page) == ""
        finally:
            project.close()
