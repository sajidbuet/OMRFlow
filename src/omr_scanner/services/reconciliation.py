"""Match scanned scripts against the registered candidates.

Purpose:
    The deterministic core of Phase 7: given a roster, the scripts a batch
    produced and whatever an operator has already decided, work out every
    candidate's reconciliation state and every script's home.

Scope:
    A pure function over value objects. No database, no Qt, no file access, no
    recognition. That is what makes the classification rules testable against a
    table of cases rather than through an interface, and what keeps the primary
    algorithm out of a button handler.

The two rules the whole module is arranged around:

1. **Nothing disappears.** Every script in the input appears in exactly one
   entry of the output, including scripts belonging to nobody and scripts an
   operator has set aside. A script that cannot be placed becomes its own
   entry; it is never dropped to make the arithmetic tidy.
2. **Conditions co-occur, so classifications must too.** A candidate recorded
   absent who has two scripts is *both* ``ABSENT_WITH_SCRIPT`` and
   ``DUPLICATE_SCRIPT``. Each entry therefore carries a set of issues and
   derives its headline from them, rather than holding one status that hides
   the rest.

Determinism:
    Same inputs, same output - including ordering, which follows the roster's
    own order and then scan id. :func:`reconcile` is a function of its
    arguments alone; it reads no clock, no configuration and no global state,
    which is what lets a re-run update records in place instead of
    accumulating a second set of exceptions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omr_scanner.domain.reconciliation import (
    AttendanceSource,
    AttendanceState,
    CandidateDecision,
    CandidateRecord,
    ReconciliationCounts,
    ReconciliationEntry,
    ReconciliationIssue,
    ReconciliationStatus,
    ResolutionState,
    ScriptAssignment,
    ScriptDecision,
    ScriptRecord,
    ScriptView,
    primary_status,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "ReconciliationInput",
    "count_entries",
    "reconcile",
]


class ReconciliationInput:
    """Everything :func:`reconcile` needs, gathered in one place.

    A small class rather than five parameters because the store, the GUI and
    the tests all assemble the same five things, and a positional mistake
    between two mappings keyed by string would be silent.

    Attributes:
        candidates: The active roster, in file order.
        scripts: Every script in the batch under reconciliation.
        script_decisions: Standing operator decisions, keyed by scan id.
        candidate_decisions: Standing operator decisions, keyed by candidate ID.
    """

    __slots__ = ("candidate_decisions", "candidates", "script_decisions", "scripts")

    def __init__(
        self,
        candidates: Sequence[CandidateRecord],
        scripts: Sequence[ScriptRecord],
        script_decisions: Mapping[int, ScriptDecision] | None = None,
        candidate_decisions: Mapping[str, CandidateDecision] | None = None,
    ) -> None:
        self.candidates = tuple(candidates)
        self.scripts = tuple(scripts)
        self.script_decisions = dict(script_decisions or {})
        self.candidate_decisions = dict(candidate_decisions or {})


def reconcile(data: ReconciliationInput) -> tuple[ReconciliationEntry, ...]:
    """Classify every candidate and every script.

    Args:
        data: The roster, the scripts and any standing operator decisions.

    Returns:
        One entry per registered candidate, in roster order, followed by one
        entry per script that belongs to no registered candidate, in scan-id
        order. Deterministic.

    The order is part of the contract: a stable order is what lets the
    interface keep a selected row across a re-run, and what makes two runs
    comparable in a test without sorting first.
    """
    by_id = {item.candidate_id: item for item in data.candidates}
    # candidate ID -> scripts. Built once, in one pass: matching each script by
    # walking the roster would be O(scripts x candidates), which is minutes
    # rather than milliseconds at examination scale (brief section 40).
    attributed: dict[str, list[ScriptView]] = {}
    orphans: dict[str, list[ScriptView]] = {}

    for script in sorted(data.scripts, key=lambda item: item.scan_id):
        decision = data.script_decisions.get(script.scan_id, ScriptDecision())
        view = ScriptView(
            script=script,
            assignment=(
                ScriptAssignment.HUMAN
                if decision.assigned_candidate_id
                else ScriptAssignment.MACHINE
            ),
            excluded=decision.excluded,
            primary=decision.primary,
            reason_code=decision.reason_code,
            reason_text=decision.reason_text,
            reviewer=decision.reviewer,
        )
        target = decision.assigned_candidate_id or script.effective_candidate_id
        if decision.assigned_candidate_id and target in by_id:
            # An operator said where this script belongs, and that candidate
            # exists. Their decision outranks recognition, which is the point
            # of making it.
            attributed.setdefault(target, []).append(view)
            continue
        if script.identifier_unresolved:
            # Not unknown - unread. Filing it as UNKNOWN_ID would send an
            # operator looking for a candidate who may be perfectly ordinary
            # once the Resolve stage has been through the sheet.
            orphans.setdefault(_orphan_key(script), []).append(view)
            continue
        if target and target in by_id:
            attributed.setdefault(target, []).append(view)
            continue
        orphans.setdefault(_orphan_key(script), []).append(view)

    entries = [
        _candidate_entry(
            candidate,
            attributed.get(candidate.candidate_id, []),
            data.candidate_decisions.get(candidate.candidate_id, CandidateDecision()),
        )
        for candidate in data.candidates
    ]
    entries.extend(
        _orphan_entry(key, views, data.candidate_decisions.get(key, CandidateDecision()))
        for key, views in sorted(
            orphans.items(), key=lambda item: item[1][0].script.scan_id
        )
    )
    return tuple(entries)


def _orphan_key(script: ScriptRecord) -> str:
    """The key an unplaceable script is filed under.

    Scripts read as the *same* unknown ID group together, because that is one
    problem to investigate rather than several - six sheets all read as
    ``15999999`` is one misprint, not six mysteries. A script whose ID is still
    unread cannot be grouped with anything, so it is keyed by its own scan id.
    """
    if script.identifier_unresolved:
        return f"#unresolved:{script.scan_id}"
    return script.effective_candidate_id or f"#blank:{script.scan_id}"


def _candidate_entry(
    candidate: CandidateRecord,
    views: Sequence[ScriptView],
    decision: CandidateDecision,
) -> ReconciliationEntry:
    """Classify one registered candidate."""
    ordered = tuple(sorted(views, key=lambda item: item.script.scan_id))
    counted = [item for item in ordered if item.counts_as_a_script]

    overridden = decision.attendance_override is not AttendanceState.UNKNOWN
    attendance = (
        decision.attendance_override if overridden else candidate.imported_attendance
    )
    source = AttendanceSource.HUMAN if overridden else AttendanceSource.IMPORTED

    issues: set[ReconciliationIssue] = set()
    if len(counted) > 1:
        issues.add(ReconciliationIssue.DUPLICATE_SCRIPT)
    if counted and attendance is AttendanceState.ABSENT:
        issues.add(ReconciliationIssue.ABSENT_WITH_SCRIPT)
    if not counted and attendance.expects_a_script:
        issues.add(ReconciliationIssue.PRESENT_WITHOUT_SCRIPT)

    return ReconciliationEntry(
        candidate_id=candidate.candidate_id,
        candidate=candidate,
        scripts=ordered,
        issues=frozenset(issues),
        status=primary_status(
            frozenset(issues), attendance=attendance, script_count=len(counted)
        ),
        effective_attendance=attendance,
        attendance_source=source,
        resolution=_resolution_for(issues, decision),
        reason_code=decision.reason_code,
        reason_text=decision.reason_text,
        reviewer=decision.reviewer,
    )


def _orphan_entry(
    key: str, views: Sequence[ScriptView], decision: CandidateDecision
) -> ReconciliationEntry:
    """Classify a group of scripts that belong to no registered candidate."""
    ordered = tuple(sorted(views, key=lambda item: item.script.scan_id))
    unresolved = any(item.script.identifier_unresolved for item in ordered)
    issue = (
        ReconciliationIssue.UNRESOLVED_CANDIDATE_ID
        if unresolved
        else ReconciliationIssue.UNKNOWN_ID
    )
    issues = {issue}
    counted = [item for item in ordered if item.counts_as_a_script]
    if len(counted) > 1:
        # Several sheets read as the same unregistered ID. Still a duplicate,
        # and saying so now saves discovering it after they are assigned.
        issues.add(ReconciliationIssue.DUPLICATE_SCRIPT)

    return ReconciliationEntry(
        candidate_id=_display_key(key, ordered),
        candidate=None,
        scripts=ordered,
        issues=frozenset(issues),
        status=issue.status,
        effective_attendance=AttendanceState.UNKNOWN,
        attendance_source=AttendanceSource.IMPORTED,
        resolution=_resolution_for(issues, decision),
        reason_code=decision.reason_code,
        reason_text=decision.reason_text,
        reviewer=decision.reviewer,
    )


def _display_key(key: str, views: Sequence[ScriptView]) -> str:
    """The candidate ID an orphan entry is shown under.

    Internal keys beginning with ``#`` are bookkeeping - a script with no
    readable ID at all - and must not be shown to an operator as though the
    sheet had that written on it.
    """
    if not key.startswith("#"):
        return key
    if views and views[0].script.effective_candidate_id:
        return views[0].script.effective_candidate_id
    return ""


def _resolution_for(
    issues: set[ReconciliationIssue], decision: CandidateDecision
) -> ResolutionState:
    """Decide whether an entry still needs attention.

    An entry with no issues is ``RESOLVED`` because there is nothing to
    resolve - including an entry whose issues an operator *made* go away by
    assigning a script, which is the behaviour that lets a queue empty as work
    is done rather than staying full of things already dealt with.
    """
    if not issues:
        return ResolutionState.RESOLVED
    if decision.dismissed:
        return ResolutionState.DISMISSED
    # Issues remain. That is true even when a decision has already been
    # recorded against this entry - a reassignment that produced a fresh
    # duplicate, say - and calling such an entry resolved because somebody
    # once touched it is exactly how a cascading exception gets lost.
    return ResolutionState.OPEN


def count_entries(
    entries: Sequence[ReconciliationEntry], *, scripts: int = -1
) -> ReconciliationCounts:
    """Summarise a reconciliation for the operator's overview.

    Args:
        entries: The result of :func:`reconcile`.
        scripts: How many scripts the batch holds. ``-1`` counts them from
            ``entries`` instead, which is the same number whenever every script
            was passed in.

    Returns:
        Counts by state. Exceptions are counted by *issue*, not by headline,
        so a candidate who is both absent-with-script and duplicated appears in
        both tallies - the summary must not hide what the detail shows.
    """
    counts: dict[str, int] = {}

    def bump(name: str, amount: int = 1) -> None:
        counts[name] = counts.get(name, 0) + amount

    by_status: dict[ReconciliationStatus, int] = {}
    total_scripts = 0
    excluded = 0

    for entry in entries:
        by_status[entry.status] = by_status.get(entry.status, 0) + 1
        total_scripts += len(entry.scripts)
        excluded += sum(1 for item in entry.scripts if item.excluded)

        if entry.is_registered:
            bump("registered")
            if entry.effective_attendance is AttendanceState.PRESENT:
                bump("expected_present")
            elif entry.effective_attendance is AttendanceState.ABSENT:
                bump("expected_absent")
            else:
                bump("attendance_unknown")

        if entry.status is ReconciliationStatus.MATCHED:
            bump("matched")
        elif entry.status is ReconciliationStatus.ABSENT_CONFIRMED:
            bump("absent_confirmed")

        for issue in entry.issues:
            bump(issue.value)

        if entry.status.is_exception:
            if entry.resolution is ResolutionState.DISMISSED:
                bump("dismissed")
            else:
                bump("outstanding")
        elif entry.reviewer:
            # No longer an exception, and a person is the reason. Counting
            # these as "resolved" is the only way the summary can show work
            # being done: an entry that a decision *fixed* stops being an
            # exception, so a count gated on `is_exception` would sit at zero
            # however much an operator got through.
            bump("resolved")

    return ReconciliationCounts(
        registered=counts.get("registered", 0),
        expected_present=counts.get("expected_present", 0),
        expected_absent=counts.get("expected_absent", 0),
        attendance_unknown=counts.get("attendance_unknown", 0),
        scripts=total_scripts if scripts < 0 else scripts,
        scripts_excluded=excluded,
        matched=counts.get("matched", 0),
        absent_confirmed=counts.get("absent_confirmed", 0),
        unknown_id=counts.get(ReconciliationIssue.UNKNOWN_ID.value, 0),
        duplicate_script=counts.get(ReconciliationIssue.DUPLICATE_SCRIPT.value, 0),
        present_without_script=counts.get(
            ReconciliationIssue.PRESENT_WITHOUT_SCRIPT.value, 0
        ),
        absent_with_script=counts.get(ReconciliationIssue.ABSENT_WITH_SCRIPT.value, 0),
        unresolved_candidate_id=counts.get(
            ReconciliationIssue.UNRESOLVED_CANDIDATE_ID.value, 0
        ),
        resolved=counts.get("resolved", 0),
        dismissed=counts.get("dismissed", 0),
        outstanding_count=counts.get("outstanding", 0),
        by_status=by_status,
    )
