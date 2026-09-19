"""The reconciliation classification rules (Phase 7).

Scope:
    :mod:`omr_scanner.services.reconciliation` in isolation - a pure function
    over value objects. No database, no GUI, no recognition.

Why this level exists:
    The rules are a table, and a table is best tested as one. Driving these
    cases through storage and an interface would take a hundred times as long
    and prove the same thing less clearly.

The scenario every test builds on is the one the phase brief specifies:

    1001  present  one script      -> matched
    1002  present  two scripts     -> duplicate script
    1003  absent   no script       -> absent, confirmed
    1004  present  no script       -> present without script
    1005  absent   one script      -> absent with script
    9999  -        one script      -> unknown ID
    -     -        unread ID       -> candidate ID not yet resolved
"""

from __future__ import annotations

import pytest

from omr_scanner.domain.reconciliation import (
    AttendanceSource,
    AttendanceState,
    CandidateDecision,
    CandidateRecord,
    ReconciliationIssue,
    ReconciliationStatus,
    ResolutionState,
    ScriptAssignment,
    ScriptDecision,
    ScriptRecord,
    primary_status,
)
from omr_scanner.services.reconciliation import (
    ReconciliationInput,
    count_entries,
    reconcile,
)


def candidate(
    candidate_id: str, attendance: AttendanceState, name: str = ""
) -> CandidateRecord:
    """One roster row."""
    return CandidateRecord(
        candidate_id=candidate_id,
        display_name=name or f"CANDIDATE {candidate_id}",
        imported_attendance=attendance,
        imported_value="ABSENT" if attendance is AttendanceState.ABSENT else "55",
    )


def script(
    scan_id: int, candidate_id: str, *, unresolved: bool = False, machine: str = ""
) -> ScriptRecord:
    """One scanned script."""
    return ScriptRecord(
        scan_id=scan_id,
        source_name=f"scan_{scan_id}.png",
        machine_candidate_id=machine or candidate_id,
        effective_candidate_id=candidate_id,
        identifier_unresolved=unresolved,
        corrected_by_human=bool(machine) and machine != candidate_id,
    )


PRESENT = AttendanceState.PRESENT
ABSENT = AttendanceState.ABSENT


@pytest.fixture
def scenario() -> ReconciliationInput:
    """The brief's acceptance scenario, exactly."""
    return ReconciliationInput(
        candidates=[
            candidate("1001", PRESENT),
            candidate("1002", PRESENT),
            candidate("1003", ABSENT),
            candidate("1004", PRESENT),
            candidate("1005", ABSENT),
        ],
        scripts=[
            script(1, "1001"),
            script(2, "1002"),
            script(3, "1002"),
            script(4, "1005"),
            script(5, "9999"),
            script(6, "", unresolved=True),
        ],
    )


def by_id(entries) -> dict[str, object]:
    """Index entries by the ID they are filed under."""
    return {entry.candidate_id: entry for entry in entries}


class TestRequiredClassifications:
    """Every classification the brief names, from controlled data."""

    def test_a_present_candidate_with_one_script_is_matched(self, scenario):
        entry = by_id(reconcile(scenario))["1001"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert entry.issues == frozenset()
        assert entry.status.is_exception is False

    def test_two_scripts_for_one_candidate_is_a_duplicate(self, scenario):
        entry = by_id(reconcile(scenario))["1002"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert entry.script_count == 2

    def test_an_absent_candidate_with_no_script_is_confirmed(self, scenario):
        entry = by_id(reconcile(scenario))["1003"]
        assert entry.status is ReconciliationStatus.ABSENT_CONFIRMED
        # Explicitly not an exception: it is the expected outcome.
        assert entry.status.is_exception is False

    def test_a_present_candidate_with_no_script_is_reported(self, scenario):
        entry = by_id(reconcile(scenario))["1004"]
        assert entry.status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_an_absent_candidate_with_a_script_is_reported(self, scenario):
        entry = by_id(reconcile(scenario))["1005"]
        assert entry.status is ReconciliationStatus.ABSENT_WITH_SCRIPT
        assert entry.script_count == 1

    def test_an_unregistered_id_becomes_its_own_entry(self, scenario):
        entry = by_id(reconcile(scenario))["9999"]
        assert entry.status is ReconciliationStatus.UNKNOWN_ID
        assert entry.is_registered is False
        assert entry.scripts[0].script.scan_id == 5

    def test_an_unread_id_is_not_called_unknown(self, scenario):
        # The distinction that stops an operator hunting for a candidate who
        # may turn out to be perfectly ordinary once the sheet is reviewed.
        entries = reconcile(scenario)
        unresolved = [
            e
            for e in entries
            if e.status is ReconciliationStatus.UNRESOLVED_CANDIDATE_ID
        ]
        assert len(unresolved) == 1
        assert unresolved[0].scripts[0].script.scan_id == 6
        assert not any(
            e.status is ReconciliationStatus.UNKNOWN_ID
            and e.scripts[0].script.scan_id == 6
            for e in entries
        )

    def test_all_seven_outcomes_appear_at_once(self, scenario):
        found = {entry.status for entry in reconcile(scenario)}
        assert found == {
            ReconciliationStatus.MATCHED,
            ReconciliationStatus.DUPLICATE_SCRIPT,
            ReconciliationStatus.ABSENT_CONFIRMED,
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,
            ReconciliationStatus.ABSENT_WITH_SCRIPT,
            ReconciliationStatus.UNKNOWN_ID,
            ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
        }


class TestNothingDisappears:
    """The invariant the whole phase exists to keep."""

    def test_every_script_appears_exactly_once(self, scenario):
        seen = [
            view.script.scan_id
            for entry in reconcile(scenario)
            for view in entry.scripts
        ]
        assert sorted(seen) == [1, 2, 3, 4, 5, 6]
        assert len(seen) == len(set(seen))

    def test_every_registered_candidate_has_an_entry(self, scenario):
        entries = by_id(reconcile(scenario))
        for candidate_id in ("1001", "1002", "1003", "1004", "1005"):
            assert candidate_id in entries

    def test_an_excluded_script_is_still_listed(self, scenario):
        data = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=scenario.scripts,
            script_decisions={3: ScriptDecision(excluded=True, reviewer="Dr. X")},
        )
        entry = by_id(reconcile(data))["1002"]
        assert entry.script_count == 1, "it stops counting"
        assert len(entry.scripts) == 2, "but it is still there"
        assert entry.scripts[1].excluded is True

    def test_scripts_read_as_the_same_unknown_id_group_together(self):
        # One misprint, not six mysteries.
        data = ReconciliationInput(
            candidates=[candidate("1001", PRESENT)],
            scripts=[script(1, "8888"), script(2, "8888"), script(3, "8888")],
        )
        unknown = [
            e for e in reconcile(data) if e.status is ReconciliationStatus.UNKNOWN_ID
        ]
        assert len(unknown) == 1
        assert len(unknown[0].scripts) == 3

    def test_several_unread_scripts_stay_separate(self):
        # Two unread sheets are two unknowns; they cannot be grouped, because
        # nothing yet says they are the same candidate.
        data = ReconciliationInput(
            candidates=[candidate("1001", PRESENT)],
            scripts=[script(1, "", unresolved=True), script(2, "", unresolved=True)],
        )
        unresolved = [
            e
            for e in reconcile(data)
            if e.status is ReconciliationStatus.UNRESOLVED_CANDIDATE_ID
        ]
        assert len(unresolved) == 2


class TestCoOccurringConditions:
    """A second problem must never hide behind the first."""

    def test_absent_with_two_scripts_carries_both_issues(self):
        data = ReconciliationInput(
            candidates=[candidate("1005", ABSENT)],
            scripts=[script(1, "1005"), script(2, "1005")],
        )
        entry = by_id(reconcile(data))["1005"]
        assert entry.issues == {
            ReconciliationIssue.ABSENT_WITH_SCRIPT,
            ReconciliationIssue.DUPLICATE_SCRIPT,
        }
        # The headline is one of them...
        assert entry.status is ReconciliationStatus.ABSENT_WITH_SCRIPT
        # ...and the other is still on the record.
        assert ReconciliationIssue.DUPLICATE_SCRIPT in entry.issues

    def test_several_unregistered_scripts_are_unknown_and_duplicated(self):
        data = ReconciliationInput(
            candidates=[candidate("1001", PRESENT)],
            scripts=[script(1, "7777"), script(2, "7777")],
        )
        entry = by_id(reconcile(data))["7777"]
        assert entry.issues == {
            ReconciliationIssue.UNKNOWN_ID,
            ReconciliationIssue.DUPLICATE_SCRIPT,
        }

    @pytest.mark.parametrize(
        ("issues", "expected"),
        [
            (
                {
                    ReconciliationIssue.UNRESOLVED_CANDIDATE_ID,
                    ReconciliationIssue.UNKNOWN_ID,
                },
                ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
            ),
            (
                {
                    ReconciliationIssue.UNKNOWN_ID,
                    ReconciliationIssue.DUPLICATE_SCRIPT,
                },
                ReconciliationStatus.UNKNOWN_ID,
            ),
            (
                {
                    ReconciliationIssue.ABSENT_WITH_SCRIPT,
                    ReconciliationIssue.DUPLICATE_SCRIPT,
                },
                ReconciliationStatus.ABSENT_WITH_SCRIPT,
            ),
        ],
    )
    def test_precedence_is_documented_and_total(self, issues, expected):
        found = primary_status(
            frozenset(issues), attendance=PRESENT, script_count=1
        )
        assert found is expected


class TestAttendanceSemantics:
    def test_a_candidate_with_unknown_attendance_is_not_owed_a_script(self):
        # A roster imported with no marks column says nothing about who sat the
        # paper; reporting everyone as missing would be noise, not information.
        data = ReconciliationInput(
            candidates=[candidate("1001", AttendanceState.UNKNOWN)], scripts=[]
        )
        entry = by_id(reconcile(data))["1001"]
        assert entry.status is ReconciliationStatus.ABSENT_CONFIRMED
        assert entry.issues == frozenset()

    def test_an_override_changes_the_outcome_without_changing_the_import(self):
        data = ReconciliationInput(
            candidates=[candidate("1005", ABSENT)],
            scripts=[script(1, "1005")],
            candidate_decisions={
                "1005": CandidateDecision(
                    attendance_override=PRESENT, reviewer="Dr. X"
                )
            },
        )
        entry = by_id(reconcile(data))["1005"]
        assert entry.status is ReconciliationStatus.MATCHED
        assert entry.effective_attendance is PRESENT
        assert entry.attendance_source is AttendanceSource.HUMAN
        # The roster is untouched.
        assert entry.candidate.imported_attendance is ABSENT
        assert entry.attendance_was_overridden is True

    def test_overriding_to_absent_creates_the_opposite_exception(self):
        data = ReconciliationInput(
            candidates=[candidate("1004", PRESENT)],
            scripts=[],
            candidate_decisions={
                "1004": CandidateDecision(
                    attendance_override=ABSENT, reviewer="Dr. X"
                )
            },
        )
        entry = by_id(reconcile(data))["1004"]
        assert entry.status is ReconciliationStatus.ABSENT_CONFIRMED

    def test_withdrawing_an_override_restores_the_roster(self):
        data = ReconciliationInput(
            candidates=[candidate("1005", ABSENT)],
            scripts=[script(1, "1005")],
            candidate_decisions={
                "1005": CandidateDecision(
                    attendance_override=AttendanceState.UNKNOWN, reviewer="Dr. X"
                )
            },
        )
        entry = by_id(reconcile(data))["1005"]
        assert entry.effective_attendance is ABSENT
        assert entry.attendance_source is AttendanceSource.IMPORTED


class TestHumanAssignment:
    def test_assigning_a_script_moves_it_and_keeps_the_machine_value(self, scenario):
        data = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=scenario.scripts,
            script_decisions={
                5: ScriptDecision(assigned_candidate_id="1004", reviewer="Dr. X")
            },
        )
        entries = by_id(reconcile(data))
        assert entries["1004"].status is ReconciliationStatus.MATCHED
        moved = entries["1004"].scripts[0]
        assert moved.script.machine_candidate_id == "9999"
        assert moved.assignment is ScriptAssignment.HUMAN
        assert "9999" not in entries

    def test_assigning_onto_an_occupied_candidate_surfaces_the_duplicate(
        self, scenario
    ):
        # The cascade the brief insists must never be hidden.
        data = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=scenario.scripts,
            script_decisions={
                5: ScriptDecision(assigned_candidate_id="1001", reviewer="Dr. X")
            },
        )
        entry = by_id(reconcile(data))["1001"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert entry.script_count == 2

    def test_an_assignment_to_a_candidate_who_is_not_registered_is_ignored(self):
        # The store refuses this; the engine must not silently invent an entry
        # for the named candidate either.
        data = ReconciliationInput(
            candidates=[candidate("1001", PRESENT)],
            scripts=[script(1, "6666")],
            script_decisions={
                1: ScriptDecision(assigned_candidate_id="nobody", reviewer="Dr. X")
            },
        )
        entries = by_id(reconcile(data))
        assert "nobody" not in entries
        assert entries["6666"].status is ReconciliationStatus.UNKNOWN_ID

    def test_an_assignment_outranks_recognition(self):
        data = ReconciliationInput(
            candidates=[candidate("1001", PRESENT), candidate("1002", PRESENT)],
            scripts=[script(1, "1001")],
            script_decisions={
                1: ScriptDecision(assigned_candidate_id="1002", reviewer="Dr. X")
            },
        )
        entries = by_id(reconcile(data))
        assert entries["1002"].script_count == 1
        assert entries["1001"].status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_an_assignment_also_rescues_an_unread_script(self):
        data = ReconciliationInput(
            candidates=[candidate("1001", PRESENT)],
            scripts=[script(1, "", unresolved=True)],
            script_decisions={
                1: ScriptDecision(assigned_candidate_id="1001", reviewer="Dr. X")
            },
        )
        entry = by_id(reconcile(data))["1001"]
        assert entry.status is ReconciliationStatus.MATCHED


class TestExclusion:
    def test_setting_one_of_two_aside_resolves_the_duplicate(self, scenario):
        data = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=scenario.scripts,
            script_decisions={3: ScriptDecision(excluded=True, reviewer="Dr. X")},
        )
        entry = by_id(reconcile(data))["1002"]
        assert entry.status is ReconciliationStatus.MATCHED

    def test_setting_the_only_script_aside_leaves_the_candidate_owing_one(self):
        data = ReconciliationInput(
            candidates=[candidate("1001", PRESENT)],
            scripts=[script(1, "1001")],
            script_decisions={1: ScriptDecision(excluded=True, reviewer="Dr. X")},
        )
        entry = by_id(reconcile(data))["1001"]
        assert entry.status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT
        assert len(entry.scripts) == 1


class TestResolutionState:
    def test_an_entry_with_no_issues_is_resolved(self, scenario):
        assert by_id(reconcile(scenario))["1001"].resolution is (
            ResolutionState.RESOLVED
        )

    def test_an_entry_with_issues_needs_review(self, scenario):
        entry = by_id(reconcile(scenario))["1004"]
        assert entry.resolution is ResolutionState.OPEN
        assert entry.needs_attention is True

    def test_dismissing_stops_it_counting_as_outstanding(self, scenario):
        data = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=scenario.scripts,
            candidate_decisions={
                "1004": CandidateDecision(dismissed=True, reviewer="Dr. X")
            },
        )
        entry = by_id(reconcile(data))["1004"]
        assert entry.resolution is ResolutionState.DISMISSED
        assert entry.needs_attention is False
        # ...but it keeps its classification rather than looking fixed.
        assert entry.status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT

    def test_a_decision_that_did_not_fix_the_problem_stays_open(self):
        # A reassignment that produced a fresh duplicate is still open work.
        data = ReconciliationInput(
            candidates=[candidate("1001", PRESENT)],
            scripts=[script(1, "1001"), script(2, "5555")],
            script_decisions={
                2: ScriptDecision(assigned_candidate_id="1001", reviewer="Dr. X")
            },
        )
        entry = by_id(reconcile(data))["1001"]
        assert entry.status is ReconciliationStatus.DUPLICATE_SCRIPT
        assert entry.resolution is ResolutionState.OPEN


class TestDeterminism:
    def test_the_same_input_produces_the_same_output(self, scenario):
        first = reconcile(scenario)
        second = reconcile(scenario)
        assert first == second

    def test_script_order_does_not_change_the_result(self, scenario):
        shuffled = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=list(reversed(scenario.scripts)),
        )
        assert reconcile(scenario) == reconcile(shuffled)

    def test_registered_candidates_come_first_in_roster_order(self, scenario):
        entries = reconcile(scenario)
        registered = [e.candidate_id for e in entries if e.is_registered]
        assert registered == ["1001", "1002", "1003", "1004", "1005"]
        assert all(not e.is_registered for e in entries[len(registered) :])


class TestCounts:
    def test_the_summary_matches_the_entries(self, scenario):
        entries = reconcile(scenario)
        counts = count_entries(entries, scripts=6)
        assert counts.registered == 5
        assert counts.expected_present == 3
        assert counts.expected_absent == 2
        assert counts.scripts == 6
        assert counts.matched == 1
        assert counts.absent_confirmed == 1
        assert counts.unknown_id == 1
        assert counts.duplicate_script == 1
        assert counts.present_without_script == 1
        assert counts.absent_with_script == 1
        assert counts.unresolved_candidate_id == 1

    def test_co_occurring_issues_are_counted_in_both_tallies(self):
        data = ReconciliationInput(
            candidates=[candidate("1005", ABSENT)],
            scripts=[script(1, "1005"), script(2, "1005")],
        )
        counts = count_entries(reconcile(data))
        # One entry, two problems, and the summary says so rather than picking.
        assert counts.absent_with_script == 1
        assert counts.duplicate_script == 1

    def test_outstanding_work_falls_as_exceptions_are_dealt_with(self, scenario):
        counts = count_entries(reconcile(scenario))
        assert counts.is_clear is False
        before = counts.outstanding

        data = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=scenario.scripts,
            candidate_decisions={
                "1004": CandidateDecision(dismissed=True, reviewer="Dr. X")
            },
        )
        after = count_entries(reconcile(data))
        assert after.outstanding == before - 1

    def test_an_exception_a_decision_fixed_is_counted_as_resolved(self, scenario):
        """Otherwise the summary shows no sign of an operator's work.

        An exception a decision *fixes* stops being an exception, so a
        "resolved" count gated on `status.is_exception` would sit at zero
        however much got done - which is what it did until a screenshot showed
        "Resolved 0" beside a decision that had plainly just been made.
        """
        before = count_entries(reconcile(scenario))
        assert before.resolved == 0

        data = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=scenario.scripts,
            candidate_decisions={
                "1005": CandidateDecision(
                    attendance_override=PRESENT, reviewer="Dr. X"
                )
            },
        )
        after = count_entries(reconcile(data))
        assert after.resolved == 1
        assert after.absent_with_script == 0
        assert after.outstanding == before.outstanding - 1

    def test_a_normal_outcome_nobody_touched_is_not_resolved(self, scenario):
        # 1001 was matched from the start; nobody resolved anything.
        counts = count_entries(reconcile(scenario))
        assert counts.matched == 1
        assert counts.resolved == 0

    def test_outstanding_counts_entries_not_issues(self):
        # One entry with two issues is one piece of work, not two.
        data = ReconciliationInput(
            candidates=[candidate("1005", ABSENT)],
            scripts=[script(1, "1005"), script(2, "1005")],
        )
        counts = count_entries(reconcile(data))
        assert counts.exceptions == 2, "two issues raised"
        assert counts.outstanding == 1, "on one entry"
        assert counts.is_clear is False

    def test_excluded_scripts_are_counted_separately_not_lost(self, scenario):
        data = ReconciliationInput(
            candidates=scenario.candidates,
            scripts=scenario.scripts,
            script_decisions={3: ScriptDecision(excluded=True, reviewer="Dr. X")},
        )
        counts = count_entries(reconcile(data), scripts=6)
        assert counts.scripts == 6
        assert counts.scripts_excluded == 1


class TestScale:
    def test_matching_is_not_quadratic(self):
        # Ten thousand candidates and ten thousand scripts. A per-script walk
        # of the roster would be 100 million comparisons; an indexed match is
        # instant. Asserted by wall clock only as a coarse guard - the shape of
        # the algorithm is what matters, and a regression to O(n^2) here would
        # take minutes rather than milliseconds.
        import time

        size = 10_000
        candidates = [candidate(str(100000 + i), PRESENT) for i in range(size)]
        scripts = [script(i, str(100000 + i)) for i in range(size)]
        started = time.perf_counter()
        entries = reconcile(ReconciliationInput(candidates, scripts))
        elapsed = time.perf_counter() - started
        assert len(entries) == size
        assert all(e.status is ReconciliationStatus.MATCHED for e in entries)
        assert elapsed < 5.0
