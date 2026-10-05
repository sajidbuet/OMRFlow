"""Resolve's *Files awaiting decision* view (0.1.1 revised phase 8).

Held (arrived for a closed session), unreadable and unsupported intake files
are listed with their provenance; the actions offered are exactly
``intake_decisions.OPTIONS`` for the file's state, each applied through
``intake_decisions.decide_file`` for a named reviewer - never by the page
changing the ledger. A file that arrived for a closed session cannot be
released until the session is reopened.
"""

from __future__ import annotations

import pytest
from tests.engine_rig import EngineRig, readable_sheets, sheet_bytes
from tests.integration.test_session_finish import BIG, resolve_all_conflicts

from omr_scanner.domain.intake import IntakeState
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.review.page import FILTER_FILES, ResolvePage
from omr_scanner.services import intake, intake_decisions, session_finish
from omr_scanner.services.intake_decisions import FileDecision

pytestmark = pytest.mark.gui

OPERATOR = "Operator"
SHEETS = readable_sheets(6)


@pytest.fixture
def rig(project_session) -> EngineRig:
    made = EngineRig(project_session)
    made.source("a")
    made.write("a", [(f"{i}.png", SHEETS[i]) for i in range(2)] + [
        ("broken.png", sheet_bytes(36)[30])
    ])
    made.new_engine(unit_policy=BIG)
    made.make_ready()
    made.run()
    for _ in range(5):  # the malformed file exhausts its decode attempts
        made.clock.advance(60)
        assert made.engine is not None
        made.engine.poll_intake(force=True)
    return made


def page_for(qtbot, rig: EngineRig, *, reviewer: str = OPERATOR) -> ResolvePage:
    page = ResolvePage(next(item for item in WORKFLOW_PAGES if item.key == "resolve"))
    qtbot.addWidget(page)
    page.on_project_changed(rig.project)
    page.set_reviewer(reviewer)
    assert page.load_session(rig.session_id, rig.template)
    page.show_view("files")
    return page


def test_an_unreadable_file_is_listed_with_exactly_the_service_options(qtbot, rig):
    page = page_for(qtbot, rig)
    assert page.state_filter.currentText() == FILTER_FILES
    assert [item.file_name for item in page.state.pending_files] == ["broken.png"]
    item = page.state.pending_files[0]
    assert item.state is IntakeState.UNREADABLE
    assert page.queue_table.item(0, 1).text() == "Scanner a"
    assert page.queue_table.item(0, 3).text() == "Unreadable"
    panel = page.file_panel
    offered = {decision for decision, button in panel.buttons.items() if button.isVisibleTo(panel)}
    assert offered == set(intake_decisions.OPTIONS[IntakeState.UNREADABLE])
    assert "broken.png" in panel.details_label.text()


def test_without_a_reviewer_no_decision_is_possible(qtbot, rig):
    page = page_for(qtbot, rig, reviewer="")
    assert not any(button.isEnabled() for button in page.file_panel.buttons.values())
    assert page.prompt_decide_file(FileDecision.RETRY) is False


def test_retry_and_dismiss_go_through_the_service(qtbot, rig, monkeypatch):
    page = page_for(qtbot, rig)
    item = page.state.pending_files[0]
    seen: list[tuple[int, FileDecision, str]] = []
    real = intake_decisions.decide_file

    def spy(
        database: object, intake_file_id: int, decision: FileDecision, **kwargs: str
    ) -> object:
        seen.append((intake_file_id, decision, kwargs["reviewer"]))
        return real(database, intake_file_id, decision, **kwargs)

    monkeypatch.setattr(intake_decisions, "decide_file", spy)
    assert page.decide_file(item.intake_file_id, FileDecision.RETRY)
    assert seen == [(item.intake_file_id, FileDecision.RETRY, OPERATOR)]
    row = intake.ledger_row(rig.database, item.intake_file_id)
    assert row is not None and row.state is IntakeState.STABILIZING
    assert page.state.pending_files == []  # no longer waiting


def test_a_file_arriving_for_a_closed_session_is_held_and_not_releasable_until_reopened(
    qtbot, rig
):
    page = page_for(qtbot, rig)
    broken = page.state.pending_files[0]
    assert page.decide_file(broken.intake_file_id, FileDecision.DISMISS, note="bad file")
    resolve_all_conflicts(rig)
    assert rig.engine is not None
    outcome = rig.engine.finish_session(closed_by=OPERATOR)
    assert outcome.closed, outcome.codes
    rig.write("a", [("late.png", SHEETS[4])])
    rig.make_ready()
    rig.run()
    page.refresh_queue()
    held = [item for item in page.state.pending_files if item.file_name == "late.png"]
    assert held and held[0].state is IntakeState.HELD
    assert page.select_file(held[0].intake_file_id)
    release = page.file_panel.buttons[FileDecision.RELEASE]
    assert release.isVisibleTo(page.file_panel) and not release.isEnabled()
    assert "Reopen the session first" in page.file_panel.note_label.text()
    # Nothing was added to the closed session's population by the arrival.
    assert intake.ledger_row(rig.database, held[0].intake_file_id).batch_scan_id is None  # type: ignore[union-attr]
    session_finish.reopen_session(rig.database, rig.session_id, reopened_by=OPERATOR)
    page.refresh_queue()
    assert page.select_file(held[0].intake_file_id)
    assert page.file_panel.buttons[FileDecision.RELEASE].isEnabled()
    assert page.decide_file(held[0].intake_file_id, FileDecision.RELEASE)
    row = intake.ledger_row(rig.database, held[0].intake_file_id)
    assert row is not None and row.state is IntakeState.READY
