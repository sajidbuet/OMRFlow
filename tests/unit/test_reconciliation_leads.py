"""Investigation leads for attendance exceptions: suggestions, never assignments.

Scope:
    :mod:`omr_scanner.services.reconciliation_leads` - which scripts might
    belong to a candidate with none, and which candidates might have written an
    absent candidate's roll number. Pure functions over the same reconciliation
    the Attendance stage shows.
"""

from __future__ import annotations

from omr_scanner.domain.reconciliation import (
    AttendanceState,
    CandidateRecord,
    ReconciliationStatus,
    ScriptRecord,
)
from omr_scanner.services.reconciliation import ReconciliationInput, reconcile
from omr_scanner.services.reconciliation_leads import (
    MAX_DIGIT_DIFFERENCE,
    id_distance,
    owner_leads,
    script_leads,
)

PRESENT = AttendanceState.PRESENT
ABSENT = AttendanceState.ABSENT


def candidate(candidate_id: str, attendance: AttendanceState) -> CandidateRecord:
    return CandidateRecord(
        candidate_id=candidate_id,
        display_name=f"CANDIDATE {candidate_id}",
        imported_attendance=attendance,
        imported_value="ABSENT" if attendance is ABSENT else "55",
    )


def script(scan_id: int, candidate_id: str, *, unresolved: bool = False) -> ScriptRecord:
    return ScriptRecord(
        scan_id=scan_id,
        source_name=f"scan_{scan_id}.png",
        machine_candidate_id=candidate_id,
        effective_candidate_id=candidate_id,
        identifier_unresolved=unresolved,
    )


def entries_for(candidates, scripts):
    return reconcile(ReconciliationInput(candidates=candidates, scripts=scripts))


def entry(entries, candidate_id):
    return next(item for item in entries if item.candidate_id == candidate_id)


class TestIdDistance:
    def test_counts_differing_positions(self):
        assert id_distance("1805012", "1805012") == 0
        assert id_distance("1805012", "1805017") == 1
        assert id_distance("1805012", "1805071") == 2

    def test_unread_positions_are_not_differences(self):
        assert id_distance("1805012", "18050?2") == 0
        assert id_distance("1805012", "1805_1_") == 0

    def test_different_lengths_cannot_be_compared(self):
        assert id_distance("1805012", "180501") is None
        assert id_distance("", "1805012") is None


class TestScriptLeadsForAMissingScript:
    def test_the_script_filed_under_an_absent_neighbour_is_suggested(self):
        # The reported case: 1805012 attended and wrote 1805017, who was absent.
        entries = entries_for(
            [candidate("1805012", PRESENT), candidate("1805017", ABSENT)],
            [script(7, "1805017")],
        )
        missing = entry(entries, "1805012")
        assert missing.status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT
        (lead,) = script_leads(missing, entries)
        assert lead.scan_id == 7
        assert lead.candidate_id == "1805017"
        assert lead.distance == 1
        assert "absent" in lead.reason

    def test_an_unread_script_is_always_worth_a_look(self):
        entries = entries_for(
            [candidate("1805012", PRESENT)], [script(3, "", unresolved=True)]
        )
        leads = script_leads(entry(entries, "1805012"), entries)
        assert [lead.scan_id for lead in leads] == [3]
        assert "not yet resolved" in leads[0].reason

    def test_a_distant_unknown_id_is_not_suggested(self):
        entries = entries_for(
            [candidate("1805012", PRESENT)], [script(4, "9999999")]
        )
        assert script_leads(entry(entries, "1805012"), entries) == ()

    def test_closest_first(self):
        entries = entries_for(
            [candidate("1805012", PRESENT)],
            [script(1, "1805099"), script(2, "1805013")],
        )
        leads = script_leads(entry(entries, "1805012"), entries)
        assert [lead.scan_id for lead in leads] == [2, 1]
        assert all(lead.distance <= MAX_DIGIT_DIFFERENCE for lead in leads)

    def test_a_matched_script_is_never_suggested(self):
        # Another candidate's own, uncontested script is not a lead.
        entries = entries_for(
            [candidate("1805012", PRESENT), candidate("1805013", PRESENT)],
            [script(1, "1805013")],
        )
        assert script_leads(entry(entries, "1805012"), entries) == ()


class TestOwnerLeadsForAnAbsentCandidatesScript:
    def test_a_present_candidate_with_no_script_and_a_similar_id(self):
        entries = entries_for(
            [
                candidate("1805012", PRESENT),
                candidate("1805017", ABSENT),
                candidate("1805099", PRESENT),
            ],
            [script(7, "1805017"), script(8, "1805099")],
        )
        absent = entry(entries, "1805017")
        assert absent.status is ReconciliationStatus.ABSENT_WITH_SCRIPT
        (lead,) = owner_leads(absent, entries)
        assert lead.candidate_id == "1805012"
        assert lead.scan_id is None, "a candidate, not a script"
        assert lead.distance == 1

    def test_no_lead_when_nobody_is_missing(self):
        entries = entries_for(
            [candidate("1805017", ABSENT), candidate("1805012", PRESENT)],
            [script(7, "1805017"), script(8, "1805012")],
        )
        assert owner_leads(entry(entries, "1805017"), entries) == ()

    def test_leads_change_nothing(self):
        # A pure function: the entries are exactly as they were.
        entries = entries_for(
            [candidate("1805012", PRESENT), candidate("1805017", ABSENT)],
            [script(7, "1805017")],
        )
        before = tuple(entries)
        owner_leads(entry(entries, "1805017"), entries)
        script_leads(entry(entries, "1805012"), entries)
        assert tuple(entries) == before
