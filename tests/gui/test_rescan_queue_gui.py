"""Resolve's Suggested rescans and Rejected / Rescan views in a scanning session (revised phase 8).

Real sessions from the engine rig: a sheet whose identifier block is
displaced is suggested for a rescan by the (unvalidated) quality policy; its
rescan arrives at *another* scanner. The page lists and explains the
suggestion, never rejects by itself, confirms or dismisses only for a named
reviewer through ``quality_decisions``, and offers the cross-scanner rescan
- ranked, with its provenance - for an explicit, undoable confirmation.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select
from tests.engine_rig import EngineRig, digest
from tests.integration.test_session_finish import BIG, resolve_all_conflicts_except_evidence
from tests.quality_rig import displaced_id, rescan_of, roll_of

from omr_scanner.database.models import AuditEvent, BatchScan
from omr_scanner.domain.processing import UnitPolicy
from omr_scanner.domain.scan_lifecycle import LifecycleState, RejectionReason
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.review.page import FILTER_RESCAN, FILTER_SUGGESTED, ResolvePage
from omr_scanner.gui.scan.session_poller import read_session_view
from omr_scanner.services import quality_decisions, scan_lifecycle

pytestmark = pytest.mark.gui

OPERATOR = "Operator"


@pytest.fixture
def rig(project_session) -> EngineRig:
    made = EngineRig(project_session)
    made.source("a")
    made.source("b")
    made.write("a", [("moved3.png", displaced_id(3)), ("moved4.png", displaced_id(4))])
    made.new_engine(unit_policy=BIG)
    made.make_ready()
    made.run()
    resolve_all_conflicts_except_evidence(made)
    return made


def resolve_page(qtbot, rig: EngineRig, *, reviewer: str = OPERATOR) -> ResolvePage:
    page = ResolvePage(next(item for item in WORKFLOW_PAGES if item.key == "resolve"))
    qtbot.addWidget(page)
    page.on_project_changed(rig.project)
    page.set_reviewer(reviewer)
    assert page.load_session(rig.session_id, rig.template)
    # The session snapshot the window relays: this session is scanning.
    page.on_session_view(read_session_view(rig.database, rig.session_id, now=rig.clock()))
    return page


def lifecycle(rig: EngineRig, scan_id: int) -> LifecycleState:
    return scan_lifecycle.state_of(rig.database, scan_id)


class TestSuggestedRescans:
    def test_suggestions_are_listed_with_evidence_and_the_unvalidated_policy(self, qtbot, rig):
        page = resolve_page(qtbot, rig)
        page.show_view("suggestions")
        assert page.state_filter.currentText() == FILTER_SUGGESTED
        assert len(page.state.suggestions) == 2
        assert page.queue_table.rowCount() == 2
        assert "Suggested rescan" in page.queue_table.item(0, 3).text()
        panel = page.suggestion_panel
        assert "unvalidated" in panel.policy_note.text()
        assert "Operator confirmation is always required" in panel.policy_note.text()
        details = panel.details_label.text()
        assert "UNVALIDATED DEFAULT" in details
        assert "Scanner a" in details
        assert "moved" in details
        # Listing a suggestion never rejects anything.
        for item in page.state.suggestions:
            assert lifecycle(rig, item.scan_id) is LifecycleState.ACTIVE
        assert "suggested rescan" in page.summary_label.text()

    def test_without_a_reviewer_nothing_can_be_confirmed_or_dismissed(self, qtbot, rig):
        page = resolve_page(qtbot, rig, reviewer="")
        page.show_view("suggestions")
        assert not page.suggestion_panel.confirm_button.isEnabled()
        assert not page.suggestion_panel.dismiss_button.isEnabled()
        assert "reviewer name" in page.suggestion_panel.reviewer_note.text()
        assert page.prompt_confirm_suggestion() is False

    def test_confirm_rejects_through_reject_and_rescan_by_name(self, qtbot, rig):
        page = resolve_page(qtbot, rig)
        page.show_view("suggestions")
        chosen = page.state.suggestions[0]
        with qtbot.waitSignal(page.lifecycle_changed, timeout=30_000):
            assert page.confirm_suggestion(chosen.scan_id, reason=RejectionReason.FOLDED)
        assert lifecycle(rig, chosen.scan_id) is LifecycleState.REJECTED_PENDING_RESCAN
        case = scan_lifecycle.get_case(rig.database, chosen.scan_id)
        assert case is not None and case.rejected_by == OPERATOR
        assert case.reason is RejectionReason.FOLDED
        assert [item.scan_id for item in page.state.suggestions] != [chosen.scan_id]

    def test_dismiss_keeps_the_sheet_and_its_evidence_on_record(self, qtbot, rig):
        page = resolve_page(qtbot, rig)
        page.show_view("suggestions")
        chosen = page.state.suggestions[0]
        assert page.dismiss_suggestion(chosen.scan_id, note="looks fine to me")
        assert lifecycle(rig, chosen.scan_id) is LifecycleState.ACTIVE
        stored = quality_decisions.decision_of(rig.database, chosen.scan_id)
        assert stored is not None and stored.dismissed_by == OPERATOR
        assert stored.reasons  # the evidence is not removed
        with rig.database.session() as session:
            audit = session.scalars(
                select(AuditEvent).where(AuditEvent.entity_id == str(chosen.scan_id))
            ).all()
        assert any(item.reviewer == OPERATOR for item in audit)
        assert chosen.scan_id not in [item.scan_id for item in page.state.suggestions]

    def test_a_refresh_keeps_the_selected_suggestion(self, qtbot, rig):
        page = resolve_page(qtbot, rig)
        page.show_view("suggestions")
        second = page.state.suggestions[1]
        assert page.select_suggestion(second.scan_id)
        page.refresh_queue()
        assert page.current_suggestion() is not None
        assert page.current_suggestion().scan_id == second.scan_id  # type: ignore[union-attr]


class TestCrossScannerReplacement:
    def test_a_rescan_from_another_scanner_is_offered_with_provenance_and_is_undoable(
        self, qtbot, rig, monkeypatch
    ):
        rejected = quality_decisions.outstanding_suggestions(rig.database, rig.session_id)
        with rig.database.session() as session:
            content = dict(
                session.execute(
                    select(BatchScan.scan_id, BatchScan.content_sha256).where(
                        BatchScan.scan_id.in_([item.scan_id for item in rejected])
                    )
                ).all()
            )
        target = next(
            item for item in rejected if content[item.scan_id] == digest(displaced_id(3))
        )
        # The displaced identifier block cannot be read: the operator names the
        # Student ID the case is known by (as the phase 7 scenario does).
        quality_decisions.confirm_suggestion(
            rig.database, target.scan_id, reviewer=OPERATOR,
            declared_candidate_id=roll_of(rescan_of(3)),
        )
        assert rig.engine is not None
        rig.clock.advance(60)
        rig.write("b", [("rescan3.png", rescan_of(3)), ("rescan4.png", rescan_of(4))])
        rig.engine._policy = UnitPolicy(max_unit_size=50, trickle_seconds=0)
        rig.make_ready()
        rig.run()
        page = resolve_page(qtbot, rig)
        page.state_filter.setCurrentText(FILTER_RESCAN)
        assert page.select_case(target.scan_id)
        panel = page.rescan_panel
        texts = [panel.candidates_list.item(i).text() for i in range(panel.candidates_list.count())]
        assert texts, "the rescan from Scanner b is offered"
        # Provenance: the scanner and the arrival (before / after the
        # rejection - the rig's fake clock makes that order arbitrary here).
        assert any("from Scanner b" in text and "arrived" in text for text in texts), texts
        # The names the sheets arrived with, never the content-addressed copies'.
        assert not any(re.search(r"\b[0-9a-f]{32,}\.png", text) for text in texts), texts
        assert "not a certainty" in panel.candidates_list.item(0).toolTip()
        # Offered, never linked by itself.
        assert lifecycle(rig, target.scan_id) is LifecycleState.REJECTED_PENDING_RESCAN
        candidate = page.state.rescan_candidates[target.scan_id][0]
        with qtbot.waitSignal(page.lifecycle_changed, timeout=30_000):
            assert page.confirm_replacement_for_case(candidate.scan_id)
        assert lifecycle(rig, target.scan_id) is LifecycleState.SUPERSEDED_BY_REPLACEMENT
        # Undo, after the confirmation question (Cancel is its default).
        monkeypatch.setattr(page, "confirm_remove_replacement", lambda _case: True)
        assert page.select_case(target.scan_id)
        assert page.remove_replacement_for_case()
        assert lifecycle(rig, target.scan_id) is LifecycleState.REJECTED_PENDING_RESCAN
        history = scan_lifecycle.lifecycle_history(rig.database, target.scan_id)
        assert len(history) >= 3  # rejected, replaced, replacement removed - all kept
