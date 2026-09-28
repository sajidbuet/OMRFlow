"""Reject & Rescan's pure rules: reconciliation, suggestions, readiness.

Scope:
    The value-object layer, with no database: what a rejected script means to
    :func:`~omr_scanner.services.reconciliation.reconcile`, to the roster
    suggestion matcher's definition of "already has a valid script", to
    :func:`~omr_scanner.services.scoring.working_script`, and to the readiness
    check's *Export incomplete results* acknowledgement.

Privacy:
    Every identifier here is fictional.
"""

from __future__ import annotations

from omr_scanner.domain.reconciliation import (
    AttendanceState,
    CandidateRecord,
    ReconciliationIssue,
    ReconciliationStatus,
    ScriptRecord,
    ScriptView,
)
from omr_scanner.domain.reporting import ReadinessIssue, ReadinessIssueKind, ReadinessReport
from omr_scanner.domain.scan_lifecycle import (
    FileState,
    LifecycleState,
    RejectionReason,
    RescanCase,
    format_bytes,
)
from omr_scanner.domain.scoring import BlockReason
from omr_scanner.services.reconciliation import ReconciliationInput, count_entries, reconcile
from omr_scanner.services.reconciliation_leads import (
    edit_distance,
    owner_leads,
    similarity,
)
from omr_scanner.services.report_readiness import acknowledge_incomplete_results
from omr_scanner.services.scoring import working_script

PRESENT = AttendanceState.PRESENT
ABSENT = AttendanceState.ABSENT


def candidates(*rows: tuple[str, AttendanceState]) -> list[CandidateRecord]:
    return [CandidateRecord(candidate_id=cid, imported_attendance=state) for cid, state in rows]


def script(scan_id: int, cid: str, *, rejected: bool = False) -> ScriptRecord:
    return ScriptRecord(
        scan_id=scan_id, source_name=f"s{scan_id}.png",
        machine_candidate_id=cid, effective_candidate_id=cid, rejected=rejected,
    )


def entries_of(roster, scripts, **kwargs: object):
    return {
        entry.candidate_id: entry
        for entry in reconcile(ReconciliationInput(roster, scripts, **kwargs))
    }


class TestARejectedScript:
    def test_does_not_count_as_a_script(self):
        view = ScriptView(script=script(1, "100", rejected=True))
        assert not view.counts_as_a_script
        assert ScriptView(script=script(1, "100")).counts_as_a_script

    def test_its_candidate_reads_rescan_required_not_missing(self):
        found = entries_of(candidates(("100", PRESENT)), [script(1, "100", rejected=True)])
        entry = found["100"]
        assert entry.status is ReconciliationStatus.RESCAN_REQUIRED
        assert entry.issues == frozenset({ReconciliationIssue.RESCAN_REQUIRED})
        assert entry.script_count == 0
        assert len(entry.scripts) == 1  # listed, never dropped

    def test_a_valid_script_beside_it_is_simply_matched(self):
        found = entries_of(
            candidates(("100", PRESENT)),
            [script(1, "100", rejected=True), script(2, "100")],
        )
        assert found["100"].status is ReconciliationStatus.MATCHED

    def test_it_is_never_a_duplicate(self):
        found = entries_of(
            candidates(("100", PRESENT)),
            [script(1, "100", rejected=True), script(2, "100"), script(3, "100", rejected=True)],
        )
        assert ReconciliationIssue.DUPLICATE_SCRIPT not in found["100"].issues

    def test_it_never_becomes_an_unknown_id_of_its_own(self):
        found = entries_of(candidates(("100", PRESENT)), [script(1, "999", rejected=True)])
        assert set(found) == {"100"}
        assert found["100"].status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_an_absent_candidate_with_a_rejected_script_still_needs_the_rescan(self):
        found = entries_of(candidates(("100", ABSENT)), [script(1, "100", rejected=True)])
        assert found["100"].status is ReconciliationStatus.RESCAN_REQUIRED
        assert ReconciliationIssue.ABSENT_WITH_SCRIPT not in found["100"].issues

    def test_counts_name_it(self):
        entries = reconcile(
            ReconciliationInput(candidates(("100", PRESENT)), [script(1, "100", rejected=True)])
        )
        counts = count_entries(entries, scripts=0)
        assert counts.rescan_required == 1
        assert counts.present_without_script == 0
        assert counts.outstanding == 1

    def test_scoring_has_no_script_to_mark(self):
        found = entries_of(candidates(("100", PRESENT)), [script(1, "100", rejected=True)])
        scan_id, block = working_script(found["100"])
        assert scan_id is None
        assert block is not None and block.reason is BlockReason.NO_SCRIPT


class TestScriptFoundSetUnresolved:
    def test_a_script_outside_the_set_with_the_exact_id_is_named(self):
        found = entries_of(
            candidates(("100", PRESENT), ("101", PRESENT)), [],
            set_unresolved_ids=frozenset({"100"}),
        )
        assert found["100"].status is ReconciliationStatus.SCRIPT_SET_UNRESOLVED
        assert found["100"].status.label == "Script found — set unresolved"
        assert found["101"].status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_three_different_states_are_three_different_words(self):
        labels = {
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT.label,
            ReconciliationStatus.SCRIPT_SET_UNRESOLVED.label,
            ReconciliationStatus.RESCAN_REQUIRED.label,
        }
        assert len(labels) == 3


class TestSuggestionsSeeOnlyValidScripts:
    """The matcher itself is unchanged; what "has a valid script" means is not."""

    def test_the_edit_distance_rule_is_unchanged(self):
        assert edit_distance("200123", "20123") == 1
        assert similarity("200123", "20123") == 1
        assert similarity("0123", "123") == 1
        assert similarity("123456", "123499") == 2
        assert similarity("123456", "129999") is None
        assert similarity("18050?2", "1805012") == 0

    def test_a_candidate_whose_only_script_was_rejected_is_offered(self):
        entries = list(
            reconcile(
                ReconciliationInput(
                    candidates(("200122", PRESENT)),
                    [script(1, "200122", rejected=True), script(2, "200132")],
                )
            )
        )
        unknown = next(item for item in entries if item.candidate_id == "200132")
        leads = owner_leads(unknown, entries)
        assert [lead.candidate_id for lead in leads] == ["200122"]
        assert "rejected" in leads[0].reason

    def test_a_candidate_with_a_confirmed_replacement_is_not_offered(self):
        # A confirmed replacement is an ordinary active script; the superseded
        # original is not passed to reconciliation at all.
        entries = list(
            reconcile(
                ReconciliationInput(
                    candidates(("200122", PRESENT)),
                    [script(3, "200122"), script(2, "200132")],
                )
            )
        )
        unknown = next(item for item in entries if item.candidate_id == "200132")
        assert owner_leads(unknown, entries) == ()


class TestAcknowledgingIncompleteResults:
    def test_only_outstanding_rescans_are_demoted(self):
        report = ReadinessReport(
            set_code="3",
            issues=(
                ReadinessIssue(ReadinessIssueKind.RESCAN_OUTSTANDING, "rescan", roll="1"),
                ReadinessIssue(ReadinessIssueKind.PRESENT_WITHOUT_SCORE, "no score", roll="2"),
            ),
        )
        assert report.outstanding_rescans == 1
        assert not report.only_acknowledgeable_blocks
        acknowledged = acknowledge_incomplete_results(report)
        assert not acknowledged.is_ready
        assert [item.blocking for item in acknowledged.issues] == [False, True]

    def test_a_set_blocked_only_by_rescans_can_be_acknowledged(self):
        report = ReadinessReport(
            set_code="3",
            issues=(ReadinessIssue(ReadinessIssueKind.RESCAN_OUTSTANDING, "rescan"),),
        )
        assert not report.is_ready
        assert report.only_acknowledgeable_blocks
        assert acknowledge_incomplete_results(report).is_ready


class TestTheCaseValueObject:
    def test_identity_prefers_the_declaration_and_ignores_unread_values(self):
        case = RescanCase(
            scan_id=1, batch_id="b", state=LifecycleState.REJECTED_PENDING_RESCAN,
            recognised_candidate_id="2001?2", recognised_set_code="?",
        )
        assert case.identity == "" and case.set_code == ""
        declared = RescanCase(
            scan_id=1, batch_id="b", state=LifecycleState.REJECTED_PENDING_RESCAN,
            declared_candidate_id="200122", recognised_candidate_id="2001?2",
        )
        assert declared.identity == "200122"

    def test_only_active_is_result_eligible(self):
        assert [state for state in LifecycleState if state.is_result_eligible] == [
            LifecycleState.ACTIVE
        ]

    def test_state_is_said_in_words(self):
        case = RescanCase(
            scan_id=1, batch_id="b", state=LifecycleState.SUPERSEDED_BY_REPLACEMENT,
            reason=RejectionReason.CLIPPED, replacement_name="IMG_1.jpg",
            file_state=FileState.QUARANTINED,
        )
        assert "IMG_1.jpg" in case.status_text
        assert "quarantined" in case.status_text.lower()
        assert case.reason_label == "Clipped page"

    def test_other_needs_a_note(self):
        assert RejectionReason.OTHER.requires_note
        assert not RejectionReason.FOLDED.requires_note

    def test_sizes_read_like_sizes(self):
        assert format_bytes(12) == "12 bytes"
        assert format_bytes(150 * 1024 * 1024) == "150.0 MB"
