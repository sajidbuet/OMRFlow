"""Where to look next: evidence-based leads for an attendance exception.

Purpose:
    Help an operator investigate the two exceptions that most often mean a
    roll number was written or read wrongly:

    * **Missing script** - a candidate expected to attend has no script. Their
      sheet may exist under another ID: unread, incomplete, filed under an
      absent candidate, or one of a duplicate pair.
    * **Absent but script found** - a candidate marked absent has a script.
      Another candidate may have filled in this roll number; the likeliest are
      candidates expected present who have no script and a similar ID.

What this module will never do:
    Reassign anything. A lead is a *suggestion of where to look*, ranked by
    how many digits differ, and nothing else in the application acts on one.
    Matching roll numbers by similarity is exactly the kind of guess that
    must be confirmed by a person looking at the paper; the operator inspects
    the scan and, if it is wrong, corrects the Student ID through the review
    ledger like any other correction.

A pure function over reconciliation entries - no database, no Qt - so it can
be tested exhaustively and run on every selection without cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from omr_scanner.domain.reconciliation import ReconciliationStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.reconciliation import ReconciliationEntry

UNREAD_POSITION_MARKERS = frozenset({"?", "_"})
"""Characters recognition writes where it could not state a digit. A lead
comparison treats them as unknown rather than as a mismatch."""

MAX_DIGIT_DIFFERENCE = 2
"""How different two IDs may be and still count as "similar"."""

DEFAULT_LEAD_LIMIT = 8
"""How many leads to offer. A long list is no longer a lead, it is a search."""

_SCRIPT_SOURCES: dict[ReconciliationStatus, tuple[int, str]] = {
    ReconciliationStatus.UNRESOLVED_CANDIDATE_ID: (0, "Student ID not yet resolved"),
    ReconciliationStatus.UNKNOWN_ID: (1, "Recognised ID is not on the candidate list"),
    ReconciliationStatus.ABSENT_WITH_SCRIPT: (2, "Filed under a candidate marked absent"),
    ReconciliationStatus.DUPLICATE_SCRIPT: (3, "One of several scripts under one ID"),
}
"""Which entries hold scripts worth suggesting, their rank, and the reason."""


@dataclass(frozen=True, slots=True)
class InvestigationLead:
    """One place an operator might look.

    Attributes:
        candidate_id: The entry the lead points at - the ID a script is
            currently filed under, or a candidate who might own a script.
        reason: Why it is suggested, in an operator's words.
        scan_id: The script to inspect, or ``None`` when the lead is a
            candidate rather than a script.
        source_name: That script's file name.
        recognised_id: What the script currently reads as.
        distance: How many known digits differ from the ID being investigated,
            or ``None`` when the two cannot be compared position by position.
    """

    candidate_id: str
    reason: str
    scan_id: int | None = None
    source_name: str = ""
    recognised_id: str = ""
    distance: int | None = None

    @property
    def describe(self) -> str:
        """One line for a list."""
        closeness = ""
        if self.distance == 0:
            closeness = " · same ID"
        elif self.distance is not None:
            plural = "digit" if self.distance == 1 else "digits"
            closeness = f" · differs by {self.distance} {plural}"
        if self.scan_id is None:
            return f"{self.candidate_id} - {self.reason}{closeness}"
        shown = self.recognised_id or "(not read)"
        name = self.source_name or f"scan {self.scan_id}"
        return f"{name} · read as {shown} - {self.reason}{closeness}"


def id_distance(first: str, second: str) -> int | None:
    """How many known positions of two IDs differ, or ``None``.

    ``None`` when the IDs are of different lengths or either is empty: then no
    position-by-position comparison means anything. A position either ID could
    not read (``?``, ``_``) is not counted as a difference - an incomplete
    ``18050?2`` may well be ``1805012``.
    """
    if not first or not second or len(first) != len(second):
        return None
    return sum(
        1
        for left, right in zip(first, second, strict=True)
        if left != right
        and left not in UNREAD_POSITION_MARKERS
        and right not in UNREAD_POSITION_MARKERS
    )


def script_leads(
    entry: ReconciliationEntry,
    entries: Sequence[ReconciliationEntry],
    *,
    limit: int = DEFAULT_LEAD_LIMIT,
) -> tuple[InvestigationLead, ...]:
    """Scripts that might belong to a candidate who has none.

    Args:
        entry: The *Missing script* entry being investigated.
        entries: Every entry of the same reconciliation, unfiltered.
        limit: How many leads at most.

    Returns:
        Scripts from entries that are themselves suspicious - unread,
        unknown, filed under an absent candidate, or duplicated - whose ID is
        within :data:`MAX_DIGIT_DIFFERENCE` of this candidate's, or whose ID
        could not be compared at all (an unread ID is worth a look whatever it
        says). Closest first. Scripts set aside are not offered.
    """
    target = entry.candidate_id
    found: list[tuple[tuple[int, int, int, str], InvestigationLead]] = []
    for other in entries:
        if other is entry or other.candidate_id == target:
            continue
        source = _SCRIPT_SOURCES.get(other.status)
        if source is None:
            continue
        rank, reason = source
        for view in other.scripts:
            if view.excluded:
                continue
            recognised = view.script.effective_candidate_id
            distance = id_distance(target, recognised)
            unread = other.status is ReconciliationStatus.UNRESOLVED_CANDIDATE_ID
            if not unread and (distance is None or distance > MAX_DIGIT_DIFFERENCE):
                continue
            key = (
                distance if distance is not None else MAX_DIGIT_DIFFERENCE + 1,
                rank,
                view.script.scan_id,
                other.candidate_id,
            )
            found.append(
                (
                    key,
                    InvestigationLead(
                        candidate_id=other.candidate_id,
                        reason=reason,
                        scan_id=view.script.scan_id,
                        source_name=view.script.source_name,
                        recognised_id=recognised,
                        distance=distance,
                    ),
                )
            )
    found.sort(key=lambda item: item[0])
    return tuple(lead for _, lead in found[:limit])


def owner_leads(
    entry: ReconciliationEntry,
    entries: Sequence[ReconciliationEntry],
    *,
    limit: int = DEFAULT_LEAD_LIMIT,
) -> tuple[InvestigationLead, ...]:
    """Candidates who might have filled in this entry's roll number by mistake.

    Args:
        entry: An entry with a script that may not be its own - *Absent but
            script found*, an unknown ID, or a duplicate.
        entries: Every entry of the same reconciliation, unfiltered.
        limit: How many leads at most.

    Returns:
        Registered candidates expected present who have **no script**, whose
        ID is within :data:`MAX_DIGIT_DIFFERENCE` of this entry's. Closest
        first. A suggestion of whose paper to look for, never an assignment.
    """
    target = entry.candidate_id
    found: list[tuple[tuple[int, str], InvestigationLead]] = []
    for other in entries:
        if other is entry or other.status is not ReconciliationStatus.PRESENT_WITHOUT_SCRIPT:
            continue
        distance = id_distance(target, other.candidate_id)
        if distance is None or distance > MAX_DIGIT_DIFFERENCE:
            continue
        found.append(
            (
                (distance, other.candidate_id),
                InvestigationLead(
                    candidate_id=other.candidate_id,
                    reason=(
                        "expected present, no script"
                        + (f" ({other.display_name})" if other.display_name else "")
                    ),
                    distance=distance,
                ),
            )
        )
    found.sort(key=lambda item: item[0])
    return tuple(lead for _, lead in found[:limit])


__all__ = [
    "DEFAULT_LEAD_LIMIT",
    "MAX_DIGIT_DIFFERENCE",
    "InvestigationLead",
    "id_distance",
    "owner_leads",
    "script_leads",
]
