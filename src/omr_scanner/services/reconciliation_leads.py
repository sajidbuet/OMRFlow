"""Where to look next: evidence-based leads for an attendance exception.

Purpose:
    Help an operator investigate the exceptions that most often mean a roll
    number was written or read wrongly:

    * **Missing script** - a candidate expected to attend has no script. Their
      sheet may exist under another ID: unread, incomplete, filed under an
      absent candidate, duplicated, or outside this set's reconciliation
      because its set code is unsettled or was read as another set.
    * **Absent but script found**, **unknown candidate ID**, **duplicate** -
      a script whose ID may not be its writer's. The likeliest writers are
      candidates on this set's list, with a similar ID, who have no script.

What this module will never do:
    Reassign anything. A lead is a *suggestion of where to look*, and nothing
    in the application acts on one. The operator inspects the scan and, if
    the roll number is wrong, corrects the Student ID through the review
    ledger like any other correction.

The matching rule (documented in ``docs/reconciliation.md``):

    * IDs are compared as the **exact strings** recognised and imported -
      never as integers - so ``0123`` and ``123`` differ and leading zeros
      count.
    * The distance is the **Levenshtein edit distance** (insertions,
      deletions, substitutions, each cost 1), with one refinement: a position
      recognition could not read (``?`` or ``_``) matches any single
      character at no cost, so an incomplete ``18050?2`` may be ``1805012``.
    * Two IDs are **similar** when their lengths are equal and the distance is
      at most :data:`MAX_SUBSTITUTIONS`, or their lengths differ by exactly one
      and the distance is exactly one (one missing or one extra digit). Nothing
      else is offered - a lead list that grows with the roster is a search, not
      a lead.
    * Leads are ranked by distance, then by the kind of evidence (for example
      "has no script" before "marked absent"), then by the length of the common
      prefix plus suffix, then by closeness to the roster's usual ID length.
    * When the best leads have the same distance and the same kind of
      evidence, every one of them is marked **equally close** rather than one
      being preferred - the prefix/suffix and length terms order the list but
      do not break that tie. No probability or confidence is invented.

A pure function over reconciliation entries - no database, no Qt - so it can
be tested exhaustively and run on every selection without cost.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from omr_scanner.domain.reconciliation import ReconciliationStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from omr_scanner.domain.reconciliation import ReconciliationEntry

UNREAD_POSITION_MARKERS = frozenset({"?", "_"})
"""Characters recognition writes where it could not state a digit."""

MAX_SUBSTITUTIONS = 2
"""How many digits two IDs of the same length may differ by and still be
similar. Two, because the commonest writing errors - a transposed pair, or one
wrong digit plus one misread - change two positions."""

MAX_INDELS = 1
"""How many digits may be missing or extra. One: two missing digits is no
longer a roll number with a slip in it."""

DEFAULT_LEAD_LIMIT = 5
"""How many leads to offer. Beyond a handful the list is a search, not a lead."""

MAX_DIGIT_DIFFERENCE = MAX_SUBSTITUTIONS
"""Kept for callers that ask how different two same-length IDs may be."""

_SCRIPT_SOURCES: dict[ReconciliationStatus, tuple[int, str]] = {
    ReconciliationStatus.UNRESOLVED_CANDIDATE_ID: (0, "Student ID not yet resolved"),
    ReconciliationStatus.UNKNOWN_ID: (1, "Recognised ID is not on the candidate list"),
    ReconciliationStatus.ABSENT_WITH_SCRIPT: (2, "Filed under a candidate marked absent"),
    ReconciliationStatus.DUPLICATE_SCRIPT: (3, "One of several scripts under one ID"),
}
"""Which entries hold scripts worth suggesting, their rank, and the reason."""

_OWNER_SOURCES: dict[ReconciliationStatus, tuple[int, str]] = {
    ReconciliationStatus.PRESENT_WITHOUT_SCRIPT: (0, "expected present, no script"),
    ReconciliationStatus.ABSENT_CONFIRMED: (1, "marked absent, no script"),
}
"""Which candidates on the list might have written a given ID.

A candidate who already has their own script is not offered: the question is
whose sheet this is, and they are accounted for. Someone marked absent is
offered after those expected present - the attendance list can be wrong, which
is exactly what an absent-but-script-found exception is about."""

LeadKey = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class OutOfSetScript:
    """A script this set's reconciliation does not include, as a lead source.

    Attributes:
        scan_id: The script.
        source_name: Its file name.
        recognised_id: The ID it reads as now.
        reason: Why it is outside this set - its set code unsettled, not a
            defined set, or another set's.
        exact_only: Offer it only for an exact ID match. True for another
            set's script: the same roll number in two sets is normally two
            people, so only an identical ID is worth a look.
    """

    scan_id: int
    source_name: str
    recognised_id: str
    reason: str
    exact_only: bool = False


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
        distance: The edit distance between the two IDs, or ``None`` when the
            script is offered because its ID is unread.
        tied: Whether this lead is indistinguishable from another top lead by
            every ranking rule - an ambiguity shown, not resolved.
    """

    candidate_id: str
    reason: str
    scan_id: int | None = None
    source_name: str = ""
    recognised_id: str = ""
    distance: int | None = None
    tied: bool = False

    @property
    def describe(self) -> str:
        """One line for a list."""
        closeness = ""
        if self.distance == 0:
            closeness = " · same ID"
        elif self.distance is not None:
            plural = "edit" if self.distance == 1 else "edits"
            closeness = f" · {self.distance} {plural} away"
        if self.tied:
            closeness += " · equally close"
        if self.scan_id is None:
            return f"{self.candidate_id} - {self.reason}{closeness}"
        shown = self.recognised_id or "(not read)"
        name = self.source_name or f"scan {self.scan_id}"
        return f"{name} · read as {shown} - {self.reason}{closeness}"


def _same(left: str, right: str) -> bool:
    """Whether two characters match, an unread position matching anything."""
    return left == right or left in UNREAD_POSITION_MARKERS or right in UNREAD_POSITION_MARKERS


def edit_distance(first: str, second: str) -> int:
    """Levenshtein distance, with an unread position matching any character.

    Exact strings throughout: nothing is converted to a number, so a leading
    zero is a character like any other.
    """
    previous = list(range(len(second) + 1))
    for row, left in enumerate(first, start=1):
        current = [row]
        for column, right in enumerate(second, start=1):
            current.append(
                min(
                    previous[column] + 1,  # deletion
                    current[column - 1] + 1,  # insertion
                    previous[column - 1] + (0 if _same(left, right) else 1),
                )
            )
        previous = current
    return previous[-1]


def similarity(first: str, second: str) -> int | None:
    """The edit distance between two IDs, or ``None`` when they are not similar.

    See the module docstring for the rule: equal lengths within
    :data:`MAX_SUBSTITUTIONS`, or lengths one apart at distance one.
    """
    if not first or not second:
        return None
    gap = abs(len(first) - len(second))
    if gap > MAX_INDELS:
        return None
    distance = edit_distance(first, second)
    if gap == 0:
        return distance if distance <= MAX_SUBSTITUTIONS else None
    return distance if distance == MAX_INDELS else None


def id_distance(first: str, second: str) -> int | None:
    """The edit distance when two IDs are similar, ``None`` otherwise."""
    return similarity(first, second)


def _affix(first: str, second: str) -> int:
    """Length of the common prefix plus the common suffix."""
    prefix = 0
    for left, right in zip(first, second, strict=False):
        if left != right:
            break
        prefix += 1
    suffix = 0
    for left, right in zip(reversed(first), reversed(second), strict=False):
        if left != right:
            break
        suffix += 1
    return min(prefix + suffix, max(len(first), len(second)))


def _usual_length(entries: Iterable[ReconciliationEntry]) -> int:
    """The commonest length of a registered candidate ID, or 0."""
    lengths = Counter(
        len(entry.candidate_id) for entry in entries if entry.candidate is not None
    )
    return lengths.most_common(1)[0][0] if lengths else 0


def _rank(
    found: list[tuple[LeadKey, str, InvestigationLead]], limit: int
) -> tuple[InvestigationLead, ...]:
    """Order leads, keep the best few, and mark ties among the best."""
    found.sort(key=lambda item: (item[0], item[1]))
    kept = found[:limit]
    if not kept:
        return ()
    # A tie is decided on the evidence - edit distance and what kind of
    # record it is - not on the prefix/suffix and length terms, which only
    # give a stable order. Two IDs each one deletion away are equally
    # plausible whichever digit went missing.
    best = kept[0][0][:2]
    if sum(1 for key, _, _ in kept if key[:2] == best) > 1:
        return tuple(
            replace(lead, tied=True) if key[:2] == best else lead for key, _, lead in kept
        )
    return tuple(lead for _, _, lead in kept)


def script_leads(
    entry: ReconciliationEntry,
    entries: Sequence[ReconciliationEntry],
    *,
    outside: Sequence[OutOfSetScript] = (),
    limit: int = DEFAULT_LEAD_LIMIT,
) -> tuple[InvestigationLead, ...]:
    """Scripts that might belong to a candidate who has none.

    Args:
        entry: The *Missing script* entry being investigated.
        entries: Every entry of the same reconciliation, unfiltered.
        outside: Scripts this set's reconciliation left out (see
            :class:`OutOfSetScript`).
        limit: How many leads at most.

    Returns:
        Scripts from suspicious entries - unread, unknown, filed under an
        absent candidate, duplicated - and from outside the set, whose ID is
        similar to this candidate's. An unread ID is offered whatever it says.
        Scripts set aside are not offered.
    """
    target = entry.candidate_id
    usual = _usual_length(entries) or len(target)
    found: list[tuple[LeadKey, str, InvestigationLead]] = []

    def consider(
        rank: int,
        reason: str,
        filed: str,
        scan_id: int,
        name: str,
        recognised: str,
        *,
        unread: bool = False,
        exact_only: bool = False,
    ) -> None:
        distance = similarity(target, recognised)
        if exact_only and distance != 0:
            return
        if distance is None and not unread:
            return
        key = (
            distance if distance is not None else MAX_SUBSTITUTIONS + 1,
            rank,
            -_affix(target, recognised),
            abs(len(recognised) - usual),
        )
        found.append(
            (
                key,
                f"{filed}:{scan_id}",
                InvestigationLead(
                    candidate_id=filed,
                    reason=reason,
                    scan_id=scan_id,
                    source_name=name,
                    recognised_id=recognised,
                    distance=distance,
                ),
            )
        )

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
            consider(
                rank,
                reason,
                other.candidate_id,
                view.script.scan_id,
                view.script.source_name,
                view.script.effective_candidate_id,
                unread=other.status is ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
            )
    for script in outside:
        consider(
            len(_SCRIPT_SOURCES),
            script.reason,
            script.recognised_id,
            script.scan_id,
            script.source_name,
            script.recognised_id,
            exact_only=script.exact_only,
        )
    return _rank(found, limit)


def owner_leads(
    entry: ReconciliationEntry,
    entries: Sequence[ReconciliationEntry],
    *,
    limit: int = DEFAULT_LEAD_LIMIT,
) -> tuple[InvestigationLead, ...]:
    """Candidates on this set's list who might have written this entry's ID.

    Args:
        entry: An entry with a script that may not be its writer's - *Absent
            but script found*, an unknown ID, an unread one, or a duplicate.
        entries: Every entry of the same reconciliation, unfiltered - the
            roster is the candidate space.
        limit: How many leads at most.

    Returns:
        Registered candidates with **no script** - expected present first,
        then marked absent - whose ID is similar to this entry's recognised ID.
        Closest first; ties marked. A suggestion of whose paper to look for,
        never an assignment.
    """
    targets = [
        view.script.effective_candidate_id
        for view in entry.scripts
        if not view.excluded and view.script.effective_candidate_id
    ] or [entry.candidate_id]
    usual = _usual_length(entries)
    found: list[tuple[LeadKey, str, InvestigationLead]] = []
    for other in entries:
        if other is entry or other.candidate is None:
            continue
        source = _OWNER_SOURCES.get(other.status)
        if source is None:
            continue
        rank, reason = source
        scored = [
            (distance, target)
            for target in targets
            if (distance := similarity(target, other.candidate_id)) is not None
        ]
        if not scored:
            continue
        distance, target = min(scored)
        found.append(
            (
                (
                    distance,
                    rank,
                    -_affix(target, other.candidate_id),
                    abs(len(other.candidate_id) - usual) if usual else 0,
                ),
                other.candidate_id,
                InvestigationLead(
                    candidate_id=other.candidate_id,
                    reason=reason + (f" ({other.display_name})" if other.display_name else ""),
                    distance=distance,
                ),
            )
        )
    return _rank(found, limit)


__all__ = [
    "DEFAULT_LEAD_LIMIT",
    "MAX_DIGIT_DIFFERENCE",
    "MAX_INDELS",
    "MAX_SUBSTITUTIONS",
    "InvestigationLead",
    "OutOfSetScript",
    "edit_distance",
    "id_distance",
    "owner_leads",
    "script_leads",
    "similarity",
]
