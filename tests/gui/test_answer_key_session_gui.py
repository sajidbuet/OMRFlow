"""The Answer Key stage names the scan session being worked through (revised phase 8, pre-merge).

The heading is **context**, not ownership: answer keys stay project / set
configuration. The session shown is the one the main window selects for the
downstream stages (``scan_sessions.downstream_session_id``) - the Answer Key
page never chooses one - and its wording is Resolve's (``gui/session_heading``).
"""

from __future__ import annotations

import pytest
from tests.engine_rig import EngineRig, readable_sheets
from tests.integration.test_session_finish import BIG, resolve_all_conflicts

from omr_scanner.config import AppConfig
from omr_scanner.gui.answer_key.page import AnswerKeyPage
from omr_scanner.gui.main_window import MainWindow
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.session_heading import read_session_heading
from omr_scanner.services import create_project, scan_sessions, session_finish

pytestmark = pytest.mark.gui

OPERATOR = "Operator"
SHEETS = readable_sheets(4)
NAME = "EEE 415 Final - morning sitting"


@pytest.fixture
def rig(project_session) -> EngineRig:
    made = EngineRig(project_session)
    scan_sessions.rename_scan_session(
        made.database, made.session_id, NAME, renamed_by=OPERATOR
    )
    made.source("a")
    made.write("a", [(f"{i}.png", SHEETS[i]) for i in range(2)])
    made.new_engine(unit_policy=BIG)
    made.make_ready()
    made.run()
    return made


def answer_key_page(qtbot) -> AnswerKeyPage:
    page = AnswerKeyPage(next(item for item in WORKFLOW_PAGES if item.key == "answer_key"))
    qtbot.addWidget(page)
    return page


def label_text(page: AnswerKeyPage) -> str:
    return page.session_label.text() if page.session_label.isVisibleTo(page) else ""


def close_session(rig: EngineRig) -> None:
    resolve_all_conflicts(rig)
    assert rig.engine is not None
    outcome = rig.engine.finish_session(closed_by=OPERATOR)
    assert outcome.closed, outcome.codes


def test_an_open_session_is_named_with_its_provisional_state(qtbot, rig):
    page = answer_key_page(qtbot)
    page.on_project_changed(rig.project)
    # The downstream stages' session, not one the page picked.
    assert page.state.scan_session_id == scan_sessions.downstream_session_id(rig.database)
    assert page.state.scan_session_id == rig.session_id
    text = label_text(page)
    assert NAME in text
    assert "open · results provisional" in text
    # One wording with Resolve's heading.
    assert text == read_session_heading(rig.database, rig.session_id)


def test_closed_then_reopened_follow_the_shared_semantics(qtbot, rig):
    page = answer_key_page(qtbot)
    page.on_project_changed(rig.project)
    close_session(rig)
    page.set_session(rig.session_id)
    assert label_text(page).endswith("· closed")
    session_finish.reopen_session(rig.database, rig.session_id, reopened_by=OPERATOR)
    page.set_session(rig.session_id)
    assert "open (reopened) · results provisional" in label_text(page)


def test_the_key_tooltip_says_keys_are_not_session_owned(qtbot, rig):
    page = answer_key_page(qtbot)
    page.on_project_changed(rig.project)
    assert "not to a session" in page.session_label.toolTip()


def test_switching_project_or_closing_it_leaves_no_stale_session_text(qtbot, rig, tmp_path):
    page = answer_key_page(qtbot)
    page.on_project_changed(rig.project)
    assert NAME in label_text(page)
    other = create_project(tmp_path / "other", "Other examination")
    try:
        page.on_project_changed(other)
        assert label_text(page) == ""  # a project with nothing read has no session to name
        assert page.state.scan_session_id is None
    finally:
        other.close()
    page.on_project_changed(None)
    assert label_text(page) == ""
    assert page.state.scan_session_id is None


def test_a_finite_project_with_nothing_scanned_shows_nothing(qtbot, project_session):
    page = answer_key_page(qtbot)
    page.on_project_changed(project_session)
    assert label_text(page) == ""
    # The rest of the stage is as it was: the sets bar is still the first row.
    assert page.findChild(type(page.session_label), "answerKeyReadinessLabel") is not None


def test_the_window_keeps_the_heading_in_step_with_the_session(qtbot, rig, tmp_path):
    window = MainWindow(config=AppConfig(), config_path=tmp_path / "config.json")
    qtbot.addWidget(window)
    window.apply_reviewer_name(OPERATOR)
    window._session = rig.project
    window._broadcast_project_change()
    page = window._answer_key_page()
    assert page is not None
    assert "open · results provisional" in label_text(page)
    close_session(rig)
    # What the Scan stage emits when a session closes or reopens.
    window._on_active_session_changed()
    assert label_text(page).endswith("· closed")
    session_finish.reopen_session(rig.database, rig.session_id, reopened_by=OPERATOR)
    window._on_active_session_changed()
    assert "reopened" in label_text(page)
    window._session = None
    window._broadcast_project_change()
    assert label_text(page) == ""
