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
    DEFAULT_LEAD_LIMIT,
    MAX_DIGIT_DIFFERENCE,
    OutOfSetScript,
    edit_distance,
    id_distance,
    owner_leads,
    script_leads,
    similarity,
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


class TestSimilarity:
    """The documented rule: Levenshtein on exact strings, unread = wildcard."""

    def test_substitutions(self):
        assert similarity("1805012", "1805012") == 0
        assert similarity("1805012", "1805017") == 1
        assert similarity("1805012", "1805071") == 2
        assert similarity("1805012", "1895071") is None, "three wrong digits"

    def test_one_missing_digit(self):
        assert similarity("1805012", "180512") == 1
        assert similarity("1805012", "805012") == 1

    def test_one_extra_digit(self):
        assert similarity("1805012", "18050122") == 1
        assert similarity("1805012", "11805012") == 1

    def test_two_missing_digits_are_not_similar(self):
        assert similarity("1805012", "18012") is None

    def test_a_missing_digit_plus_a_wrong_one_is_not_similar(self):
        # Lengths one apart are similar only at distance exactly one.
        assert similarity("1805012", "180519") is None

    def test_leading_zeros_are_characters(self):
        assert similarity("0012345", "0012345") == 0
        assert similarity("0012345", "012345") == 1, "one leading zero missing"
        assert similarity("0012345", "12345") is None, "two missing"
        assert edit_distance("007", "7") == 2

    def test_unread_positions_match_anything(self):
        assert similarity("1805012", "18050?2") == 0
        assert similarity("1805012", "1805_1_") == 0

    def test_unrelated_ids(self):
        assert similarity("1805012", "2299876") is None
        assert similarity("1805012", "") is None
        assert id_distance("1805012", "2299876") is None


class TestRankingAndAmbiguity:
    def test_the_closer_candidate_wins_and_is_not_marked_tied(self):
        entries = entries_for(
            [
                candidate("1805012", PRESENT),
                candidate("1809999", PRESENT),
            ],
            [script(7, "9999999"), script(8, "1805013")],
        )
        unknown = entry(entries, "1805013")
        leads = owner_leads(unknown, entries)
        assert [lead.candidate_id for lead in leads] == ["1805012"]
        assert leads[0].tied is False

    def test_two_equally_close_candidates_are_both_shown_as_tied(self):
        # 180512 is one deletion away from both 1805012 and 1805112.
        entries = entries_for(
            [candidate("1805012", PRESENT), candidate("1805112", PRESENT)],
            [script(9, "180512")],
        )
        leads = owner_leads(entry(entries, "180512"), entries)
        assert {lead.candidate_id for lead in leads} == {"1805012", "1805112"}
        assert all(lead.tied for lead in leads)
        assert all("equally close" in lead.describe for lead in leads)

    def test_a_missing_script_is_offered_the_script_missing_a_digit(self):
        entries = entries_for(
            [candidate("1805012", PRESENT)], [script(4, "180512")]
        )
        (lead,) = script_leads(entry(entries, "1805012"), entries)
        assert (lead.scan_id, lead.distance) == (4, 1)

    def test_the_list_stays_short(self):
        # Many candidates one digit away: only the handful best are offered.
        candidates = [candidate(f"18050{digit}2", PRESENT) for digit in range(10)]
        entries = entries_for(candidates, [script(1, "1805_02")])
        unread = next(
            item
            for item in entries
            if item.status is ReconciliationStatus.UNKNOWN_ID
            or item.status is ReconciliationStatus.UNRESOLVED_CANDIDATE_ID
            or not item.is_registered
        )
        leads = owner_leads(unread, entries)
        assert len(leads) <= DEFAULT_LEAD_LIMIT

    def test_a_candidate_with_their_own_script_is_not_offered(self):
        entries = entries_for(
            [candidate("1805012", PRESENT), candidate("1805013", PRESENT)],
            [script(1, "1805012"), script(2, "1805019")],
        )
        leads = owner_leads(entry(entries, "1805019"), entries)
        assert [lead.candidate_id for lead in leads] == ["1805013"]

    def test_an_absent_candidate_is_offered_after_a_missing_one(self):
        entries = entries_for(
            [candidate("1805012", ABSENT), candidate("1805014", PRESENT)],
            [script(1, "1805013")],
        )
        leads = owner_leads(entry(entries, "1805013"), entries)
        assert [lead.candidate_id for lead in leads] == ["1805014", "1805012"]


class TestScriptsOutsideTheSet:
    def test_a_script_with_an_unresolved_set_code_is_suggested(self):
        entries = entries_for([candidate("1805012", PRESENT)], [])
        outside = (
            OutOfSetScript(
                scan_id=5, source_name="s5.png", recognised_id="1805012",
                reason="Set code not yet resolved",
            ),
        )
        (lead,) = script_leads(entry(entries, "1805012"), entries, outside=outside)
        assert lead.scan_id == 5
        assert lead.distance == 0

    def test_another_sets_script_is_suggested_only_for_the_same_id(self):
        entries = entries_for([candidate("1805012", PRESENT)], [])
        outside = (
            OutOfSetScript(6, "s6.png", "1805012", "Read as set 3", exact_only=True),
            OutOfSetScript(7, "s7.png", "1805013", "Read as set 3", exact_only=True),
        )
        leads = script_leads(entry(entries, "1805012"), entries, outside=outside)
        assert [lead.scan_id for lead in leads] == [6]


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
