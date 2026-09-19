"""Vocabulary for reconciling scripts against the registered candidates.

Purpose:
    The words Phase 7 reasons in - attendance states, reconciliation
    classifications, the issues a candidate can carry at once, the actions an
    operator can take and the reasons they give. Pure data: no Qt, no
    SQLAlchemy, no file access, no OpenCV.

Why the vocabulary is its own module:
    The GUI builds a status filter from :class:`ReconciliationStatus`, the
    reconciliation engine classifies into it, the store persists it and a future
    report will read it. Putting it in the domain layer is what lets all four do
    that without importing one another - the same arrangement
    :mod:`omr_scanner.domain.review` established for Phase 6, and the reason
    ``tests/unit/test_architecture.py`` still passes with a reconciliation page
    in the GUI layer.

What does NOT belong here:
    * Reading a roster file, touching the database, or deciding *which*
      classification applies. Those are services
      (:mod:`omr_scanner.services.candidate_import`,
      :mod:`omr_scanner.services.reconciliation`,
      :mod:`omr_scanner.services.reconciliation_store`).
    * Candidate data of any kind. This module describes shapes, never people.

The invariant every type here exists to protect:

    Source data says what was registered. Machine data says what OMR Flow
    recognised. Human resolution says what an operator decided. A value
    changing does not entitle anything to forget the two before it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ----------------------------------------------------------------------
# Attendance
# ----------------------------------------------------------------------
ABSENT_TOKENS: frozenset[str] = frozenset({"absent", "abs"})
"""The cell values that mean a candidate was recorded absent.

Compared as **whole tokens** against ``str(value).strip().casefold()``, never as
substrings: ``ABSENTEE``, ``ABSENCE`` and ``ABS123`` are not absences, and a
``in`` test would silently turn all three into one. The set is small and closed
on purpose - a marks column contains marks, and anything that is not one of
these two words is not a declaration of absence.
"""


def normalise_attendance_token(value: object) -> str:
    """Return the comparable form of a marks/attendance cell.

    Args:
        value: Whatever the importer read - a string, a number, ``None``.

    Returns:
        The stripped, case-folded text. ``None`` becomes ``""``.

    One function so that the importer, the validator and the tests cannot
    disagree about what "the same value" means.
    """
    if value is None:
        return ""
    return str(value).strip().casefold()


def is_absent_token(value: object) -> bool:
    """Whether a marks/attendance cell declares the candidate absent.

    ``ABSENT``, ``absent``, ``Absent``, ``ABS``, ``abs``, ``Abs`` and any of
    them padded with whitespace are absences. Everything else - including a
    blank cell, a mark of zero, and ``ABSENTEE`` - is not.
    """
    return normalise_attendance_token(value) in ABSENT_TOKENS


class AttendanceState(StrEnum):
    """Whether a candidate was expected to sit the examination.

    ``UNKNOWN`` is a real state, not a placeholder: a roster imported without a
    marks/attendance column says nothing about attendance, and pretending such a
    candidate was expected present would manufacture a
    :attr:`ReconciliationStatus.PRESENT_WITHOUT_SCRIPT` exception for every
    candidate who did not sit.
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            AttendanceState.PRESENT: "Expected present",
            AttendanceState.ABSENT: "Marked absent",
            AttendanceState.UNKNOWN: "Not stated",
        }[self]

    @property
    def expects_a_script(self) -> bool:
        """Whether a missing script for this candidate is worth reporting.

        Only a candidate positively expected to attend. A candidate marked
        absent is *expected* to have no script, and one whose attendance was
        never stated cannot be said to owe one.
        """
        return self is AttendanceState.PRESENT


class AttendanceSource(StrEnum):
    """Where an effective attendance state came from."""

    IMPORTED = "imported"
    HUMAN = "human"


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------
class ReconciliationStatus(StrEnum):
    """The headline state of one reconciliation entry.

    Two of these are normal outcomes and five are exceptions; see
    :attr:`is_exception`. The headline is chosen by documented precedence
    (:func:`primary_status`) and never *replaces* the full issue list - a
    candidate who is marked absent and has two scripts carries both facts, and
    :class:`ReconciliationIssue` is where they both live.
    """

    MATCHED = "matched"
    ABSENT_CONFIRMED = "absent_confirmed"
    UNKNOWN_ID = "unknown_id"
    DUPLICATE_SCRIPT = "duplicate_script"
    PRESENT_WITHOUT_SCRIPT = "present_without_script"
    ABSENT_WITH_SCRIPT = "absent_with_script"
    UNRESOLVED_CANDIDATE_ID = "unresolved_candidate_id"

    @property
    def label(self) -> str:
        """Operator-facing wording.

        Examination staff, not database readers: "Marked absent but a script
        was found" says what happened; ``ABSENT_WITH_SCRIPT`` says what the
        column contains.
        """
        return {
            ReconciliationStatus.MATCHED: "Matched",
            ReconciliationStatus.ABSENT_CONFIRMED: "Absent, confirmed",
            ReconciliationStatus.UNKNOWN_ID: "Unknown candidate ID",
            ReconciliationStatus.DUPLICATE_SCRIPT: "Duplicate script",
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT: (
                "Present but no script found"
            ),
            ReconciliationStatus.ABSENT_WITH_SCRIPT: (
                "Marked absent but script found"
            ),
            ReconciliationStatus.UNRESOLVED_CANDIDATE_ID: (
                "Candidate ID not yet resolved"
            ),
        }[self]

    @property
    def description(self) -> str:
        """A sentence explaining the state, for a tooltip or detail panel."""
        return {
            ReconciliationStatus.MATCHED: (
                "This candidate was expected to attend and exactly one script "
                "was received for them. Nothing to do."
            ),
            ReconciliationStatus.ABSENT_CONFIRMED: (
                "This candidate was recorded absent and no script was "
                "received. Nothing to do."
            ),
            ReconciliationStatus.UNKNOWN_ID: (
                "A script was read as a candidate ID that is not in the "
                "imported candidate list. The script is kept; assign it to the "
                "correct candidate, or record why it cannot be."
            ),
            ReconciliationStatus.DUPLICATE_SCRIPT: (
                "More than one script maps to this candidate. Both are kept. "
                "Reassign a misidentified script, or mark one as an accidental "
                "re-scan - nothing is deleted either way."
            ),
            ReconciliationStatus.PRESENT_WITHOUT_SCRIPT: (
                "This candidate was expected to attend but no script maps to "
                "them. The script may be missing, unread, or filed under "
                "another candidate's ID."
            ),
            ReconciliationStatus.ABSENT_WITH_SCRIPT: (
                "This candidate was recorded absent, yet a script maps to "
                "them. Either the attendance record or the recognised ID is "
                "wrong, and which one matters."
            ),
            ReconciliationStatus.UNRESOLVED_CANDIDATE_ID: (
                "This script's candidate ID is still awaiting review on the "
                "Resolve stage. It is not an unknown candidate - it is a "
                "candidate nobody has read yet."
            ),
        }[self]

    @property
    def is_exception(self) -> bool:
        """Whether this state needs a human to look at it."""
        return self not in (
            ReconciliationStatus.MATCHED,
            ReconciliationStatus.ABSENT_CONFIRMED,
        )

    @property
    def concerns_a_script(self) -> bool:
        """Whether resolving this state means acting on a script.

        ``PRESENT_WITHOUT_SCRIPT`` is the odd one out: there is no script to
        act on, which is the whole complaint.
        """
        return self in (
            ReconciliationStatus.UNKNOWN_ID,
            ReconciliationStatus.DUPLICATE_SCRIPT,
            ReconciliationStatus.ABSENT_WITH_SCRIPT,
            ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
        )


class ReconciliationIssue(StrEnum):
    """One fact that is wrong with a reconciliation entry.

    An entry carries a *set* of these, because conditions genuinely co-occur: a
    candidate recorded absent who has two scripts is both
    :attr:`ABSENT_WITH_SCRIPT` and :attr:`DUPLICATE_SCRIPT`, and an
    architecture that could only hold one would make a physical script
    invisible. The headline :class:`ReconciliationStatus` is derived from this
    set, never a substitute for it.
    """

    UNKNOWN_ID = "unknown_id"
    DUPLICATE_SCRIPT = "duplicate_script"
    PRESENT_WITHOUT_SCRIPT = "present_without_script"
    ABSENT_WITH_SCRIPT = "absent_with_script"
    UNRESOLVED_CANDIDATE_ID = "unresolved_candidate_id"

    @property
    def status(self) -> ReconciliationStatus:
        """The headline status this issue corresponds to."""
        return ReconciliationStatus(self.value)

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return self.status.label


_STATUS_PRECEDENCE: tuple[ReconciliationIssue, ...] = (
    # Ordered by how much a human needs to see it first, not alphabetically.
    #
    # UNRESOLVED_CANDIDATE_ID leads because every classification below it is
    # provisional while the ID is unread - calling such a script "unknown"
    # would send an operator looking for a candidate who may not be missing.
    #
    # UNKNOWN_ID next: a script filed under nobody is the case where a
    # candidate's paper is most likely to be lost outright.
    #
    # ABSENT_WITH_SCRIPT before DUPLICATE_SCRIPT because it questions the
    # roster itself rather than the scanning, and PRESENT_WITHOUT_SCRIPT last
    # because it is very often the *consequence* of one of the others.
    ReconciliationIssue.UNRESOLVED_CANDIDATE_ID,
    ReconciliationIssue.UNKNOWN_ID,
    ReconciliationIssue.ABSENT_WITH_SCRIPT,
    ReconciliationIssue.DUPLICATE_SCRIPT,
    ReconciliationIssue.PRESENT_WITHOUT_SCRIPT,
)
"""Which issue becomes the headline when an entry carries several.

Documented and total, so two runs over the same data cannot disagree. This
decides only what the *Status* column says; every issue remains attached to the
entry and visible in its detail, which is the requirement that matters.
"""


def primary_status(
    issues: frozenset[ReconciliationIssue] | set[ReconciliationIssue],
    *,
    attendance: AttendanceState,
    script_count: int,
) -> ReconciliationStatus:
    """Choose the headline status for an entry.

    Args:
        issues: Everything wrong with the entry. Empty for a normal outcome.
        attendance: The *effective* attendance state.
        script_count: How many scripts are attributed to the entry, excluding
            any an operator has set aside as an accidental re-scan.

    Returns:
        The headline. With no issues, a candidate with a script is
        :attr:`~ReconciliationStatus.MATCHED` and one without is
        :attr:`~ReconciliationStatus.ABSENT_CONFIRMED`.
    """
    for issue in _STATUS_PRECEDENCE:
        if issue in issues:
            return issue.status
    if script_count > 0:
        return ReconciliationStatus.MATCHED
    if attendance is AttendanceState.PRESENT:
        # Defensive: a present candidate with no script should already carry
        # PRESENT_WITHOUT_SCRIPT. Reaching here would mean the caller built the
        # issue set wrongly, and silently reporting "absent, confirmed" would
        # hide a missing script - the one outcome this phase forbids.
        return ReconciliationStatus.PRESENT_WITHOUT_SCRIPT
    return ReconciliationStatus.ABSENT_CONFIRMED


# ----------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------
class ResolutionState(StrEnum):
    """Whether a human has dealt with an entry's exception."""

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            ResolutionState.OPEN: "Needs review",
            ResolutionState.RESOLVED: "Resolved",
            ResolutionState.DISMISSED: "Accepted as-is",
        }[self]

    @property
    def needs_attention(self) -> bool:
        """Whether this entry still counts as outstanding work."""
        return self is ResolutionState.OPEN


class ScriptAssignment(StrEnum):
    """Who decided which candidate a script belongs to."""

    MACHINE = "machine"
    HUMAN = "human"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            ScriptAssignment.MACHINE: "Recognised",
            ScriptAssignment.HUMAN: "Assigned by reviewer",
        }[self]


class ReconciliationAction(StrEnum):
    """What was done to a candidate or a script.

    Written into the Phase 6 audit ledger under
    ``entity_type`` of ``candidate`` or ``script``. :attr:`RECONCILED` is the
    machine's; every other member requires a named operator.
    """

    RECONCILED = "reconciled"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"
    EXCLUDED = "excluded"
    INCLUDED = "included"
    MARKED_PRIMARY = "marked_primary"
    ATTENDANCE_OVERRIDDEN = "attendance_overridden"
    DISMISSED = "dismissed"
    REOPENED = "reopened"

    @property
    def is_human(self) -> bool:
        """Whether this action requires a named operator."""
        return self is not ReconciliationAction.RECONCILED

    @property
    def label(self) -> str:
        """Operator-facing wording, for the history view."""
        return {
            ReconciliationAction.RECONCILED: "Reconciled",
            ReconciliationAction.ASSIGNED: "Script assigned",
            ReconciliationAction.UNASSIGNED: "Assignment withdrawn",
            ReconciliationAction.EXCLUDED: "Script set aside",
            ReconciliationAction.INCLUDED: "Script brought back",
            ReconciliationAction.MARKED_PRIMARY: "Marked as the working script",
            ReconciliationAction.ATTENDANCE_OVERRIDDEN: "Attendance overridden",
            ReconciliationAction.DISMISSED: "Accepted as-is",
            ReconciliationAction.REOPENED: "Reopened",
        }[self]


class ReconciliationReason(StrEnum):
    """Why an operator decided what they decided.

    A closed list plus ``OTHER``, for the same reason Phase 6 has one: free text
    alone produces a ledger nobody can count, and a list alone produces one
    nobody believes.
    """

    MISREAD_IDENTIFIER = "misread_identifier"
    ACCIDENTAL_RESCAN = "accidental_rescan"
    SHEET_SWAPPED = "sheet_swapped"
    CANDIDATE_ATTENDED = "candidate_attended"
    CANDIDATE_DID_NOT_ATTEND = "candidate_did_not_attend"
    SCRIPT_MISSING = "script_missing"
    ROSTER_ERROR = "roster_error"
    CONFIRMED_CORRECT = "confirmed_correct"
    OTHER = "other"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            ReconciliationReason.MISREAD_IDENTIFIER: (
                "Candidate ID was misread on the sheet"
            ),
            ReconciliationReason.ACCIDENTAL_RESCAN: (
                "The same sheet was scanned twice"
            ),
            ReconciliationReason.SHEET_SWAPPED: (
                "The candidate wrote another candidate's ID"
            ),
            ReconciliationReason.CANDIDATE_ATTENDED: (
                "Candidate did attend; the roster is wrong"
            ),
            ReconciliationReason.CANDIDATE_DID_NOT_ATTEND: (
                "Candidate did not attend after all"
            ),
            ReconciliationReason.SCRIPT_MISSING: (
                "The script is known to be missing"
            ),
            ReconciliationReason.ROSTER_ERROR: (
                "The imported candidate list is wrong"
            ),
            ReconciliationReason.CONFIRMED_CORRECT: (
                "Checked and correct as it stands"
            ),
            ReconciliationReason.OTHER: "Other (explain)",
        }[self]

    @property
    def requires_text(self) -> bool:
        """Whether an explanation must accompany this reason."""
        return self is ReconciliationReason.OTHER


# ----------------------------------------------------------------------
# Value objects
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """One candidate as the roster file declared them.

    **Never modified after import.** An operator who establishes that a
    candidate recorded absent actually attended does not make the roster say
    something else; the decision is recorded separately and
    :class:`ReconciliationEntry` reports both.

    Attributes:
        candidate_id: The roll/candidate number, normalised as an *identifier*
            (see :func:`omr_scanner.services.candidate_import.normalise_candidate_id`).
        display_name: The candidate's name, or ``""`` when the roster had no
            name column. Candidate data: shown in the interface, never logged.
        source_row: The 1-based row of the source file this came from, header
            included - so a validation message can name a row without naming a
            person.
        imported_attendance: What the file said.
        imported_value: The raw marks/attendance cell, kept verbatim so an
            operator can see what was actually written.
    """

    candidate_id: str
    display_name: str = ""
    source_row: int = 0
    imported_attendance: AttendanceState = AttendanceState.UNKNOWN
    imported_value: str = ""


@dataclass(frozen=True, slots=True)
class ScriptRecord:
    """One scanned script, as reconciliation sees it.

    Attributes:
        scan_id: The Phase 5 durable scan id.
        source_name: The scan's file name, for display.
        machine_candidate_id: What recognition read. **Never overwritten.**
        effective_candidate_id: What it reads as *after* any Phase 6
            correction - the value reconciliation actually matches on.
        identifier_unresolved: Whether an identifier conflict for this sheet is
            still open on the Resolve stage. Such a script is
            :attr:`ReconciliationStatus.UNRESOLVED_CANDIDATE_ID`, never
            ``UNKNOWN_ID``.
        corrected_by_human: Whether :attr:`effective_candidate_id` differs from
            the machine's because a named reviewer said so.
    """

    scan_id: int
    source_name: str = ""
    machine_candidate_id: str = ""
    effective_candidate_id: str = ""
    identifier_unresolved: bool = False
    corrected_by_human: bool = False


@dataclass(frozen=True, slots=True)
class ScriptDecision:
    """An operator's standing decision about one script.

    The *input* to reconciliation, not an output: re-running reconciliation
    recomputes every classification from scratch but must honour what a human
    has already settled.

    Attributes:
        assigned_candidate_id: The candidate this script was assigned to by
            hand, or ``""`` to leave recognition's answer in force.
        excluded: Whether the script has been set aside as an accidental
            re-scan. **Set aside, never deleted** - the scan row, its
            recognition result, the reason and the audit trail all remain.
        primary: Whether this is the working script among several for one
            candidate.
        reason_code / reason_text / reviewer: Why, and who.
    """

    assigned_candidate_id: str = ""
    excluded: bool = False
    primary: bool = False
    reason_code: str = ""
    reason_text: str = ""
    reviewer: str = ""


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    """An operator's standing decision about one candidate.

    Attributes:
        attendance_override: The attendance state an operator established, or
            :attr:`AttendanceState.UNKNOWN` to leave the roster's in force. The
            roster's own value is never touched.
        dismissed: Whether the entry's exception has been accepted as-is.
        reason_code / reason_text / reviewer: Why, and who.
    """

    attendance_override: AttendanceState = AttendanceState.UNKNOWN
    dismissed: bool = False
    reason_code: str = ""
    reason_text: str = ""
    reviewer: str = ""


@dataclass(frozen=True, slots=True)
class ScriptView:
    """One script inside a reconciliation entry, ready to display."""

    script: ScriptRecord
    assignment: ScriptAssignment = ScriptAssignment.MACHINE
    excluded: bool = False
    primary: bool = False
    reason_code: str = ""
    reason_text: str = ""
    reviewer: str = ""

    @property
    def counts_as_a_script(self) -> bool:
        """Whether this script counts towards the entry's script total.

        An excluded script does not - that is what excluding it means - but it
        is still listed, still stored and still auditable.
        """
        return not self.excluded


@dataclass(frozen=True, slots=True)
class ReconciliationEntry:
    """One candidate's reconciliation state, or one script that matched nobody.

    Attributes:
        candidate: The roster record, or ``None`` for an entry that exists only
            because a script arrived - an unknown ID, or one still unread.
        candidate_id: The key this entry is filed under: the registered
            candidate's ID, or the ID a script was read as.
        scripts: Every script attributed to this entry, **including excluded
            ones**. A script is never dropped from this list to make a count
            look tidy.
        issues: Everything wrong, as a set.
        status: The headline, derived from :attr:`issues`.
        effective_attendance: What attendance is now believed to be.
        attendance_source: Whether that came from the file or from a person.
        resolution: Whether a human has dealt with it.
    """

    candidate_id: str
    candidate: CandidateRecord | None = None
    scripts: tuple[ScriptView, ...] = ()
    issues: frozenset[ReconciliationIssue] = frozenset()
    status: ReconciliationStatus = ReconciliationStatus.MATCHED
    effective_attendance: AttendanceState = AttendanceState.UNKNOWN
    attendance_source: AttendanceSource = AttendanceSource.IMPORTED
    resolution: ResolutionState = ResolutionState.OPEN
    reason_code: str = ""
    reason_text: str = ""
    reviewer: str = ""

    @property
    def is_registered(self) -> bool:
        """Whether this entry corresponds to a candidate on the roster."""
        return self.candidate is not None

    @property
    def script_count(self) -> int:
        """How many scripts count towards this candidate, excluding set-aside ones."""
        return sum(1 for item in self.scripts if item.counts_as_a_script)

    @property
    def display_name(self) -> str:
        """The candidate's name, or ``""`` when unknown. Candidate data."""
        return self.candidate.display_name if self.candidate else ""

    @property
    def attendance_was_overridden(self) -> bool:
        """Whether a person changed the attendance the roster declared."""
        return self.attendance_source is AttendanceSource.HUMAN

    @property
    def needs_attention(self) -> bool:
        """Whether this entry is outstanding work for an operator."""
        return self.status.is_exception and self.resolution.needs_attention


@dataclass(frozen=True, slots=True)
class ReconciliationCounts:
    """The summary an operator reads before deciding the batch is finished.

    Every count is of *entries* except :attr:`scripts`, which counts physical
    scripts - so "scripts imported" reconciles against the batch, and a script
    that has been set aside still appears in :attr:`scripts_excluded` rather
    than vanishing from the arithmetic.
    """

    registered: int = 0
    expected_present: int = 0
    expected_absent: int = 0
    attendance_unknown: int = 0
    scripts: int = 0
    scripts_excluded: int = 0
    matched: int = 0
    absent_confirmed: int = 0
    unknown_id: int = 0
    duplicate_script: int = 0
    present_without_script: int = 0
    absent_with_script: int = 0
    unresolved_candidate_id: int = 0
    resolved: int = 0
    """Entries a human decision turned *into* a normal outcome.

    Counted separately from :attr:`matched` because a summary that could only
    say "matched" would show no sign of an operator's work: an exception a
    decision fixed stops being an exception.
    """

    dismissed: int = 0
    """Exceptions an operator accepted as-is. Still exceptions, not outstanding."""

    outstanding_count: int = 0
    """Exceptions nobody has dealt with. Counted, never derived - see
    :attr:`outstanding`."""

    by_status: dict[ReconciliationStatus, int] = field(default_factory=dict)

    @property
    def exceptions(self) -> int:
        """How many issues were raised, across every entry.

        Counted by *issue*, so an entry that is both absent-with-script and
        duplicated contributes to both tallies - the summary must not hide what
        the detail shows. It is therefore **not** a count of entries, and
        subtracting resolutions from it would be meaningless.
        """
        return (
            self.unknown_id
            + self.duplicate_script
            + self.present_without_script
            + self.absent_with_script
            + self.unresolved_candidate_id
        )

    @property
    def outstanding(self) -> int:
        """How many entries still need an operator's attention."""
        return self.outstanding_count

    @property
    def is_clear(self) -> bool:
        """Whether every exception has been dealt with."""
        return self.outstanding_count == 0
