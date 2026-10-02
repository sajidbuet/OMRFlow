"""Which sheets of a scan session count: the pure rules (0.1.1 phase 4, ADR-0007).

Purpose:
    Decide, for every sheet recorded in a scan session, exactly one
    :class:`SheetDisposition`, and from that the session's **effective sheet
    set** - the one examination population Attendance, scoring, Results,
    Reports and duplicate-ID detection all consume. Pure: no database, no Qt.
    :mod:`omr_scanner.services.session_population` gathers the persisted facts
    and calls :func:`classify`.

The rule, in the order it is applied (first match decides):
    1. A sheet in a batch that a live batch supersession replaces (*Reprocess
       All*) is :attr:`~SheetDisposition.BATCH_SUPERSEDED` - its batch was read
       again as a whole.
    2. A sheet that is a confirmed rescan in a lineage rooted in **another**
       scan session counts there, not here
       (:attr:`~SheetDisposition.COUNTED_IN_OTHER_SESSION`). Conversely a
       replacement read into another session counts here when its lineage
       root is here. Only explicit links decide this - never a Student ID.
    3. A non-active lifecycle state (``scan_rejection``) decides next:
       superseded by a replacement, rejected awaiting a rescan, an exact
       re-import of rejected content, excluded, deferred.
    4. A sheet never read (pending, queued, cancelled) is
       :attr:`~SheetDisposition.NOT_READ`.
    5. Everything else - an active, read sheet - is effective: read cleanly or
       needing review (:attr:`~SheetDisposition.EFFECTIVE`), or read and found
       unreadable (:attr:`~SheetDisposition.EFFECTIVE_UNREADABLE`, a physical
       script whose identity nobody knows yet; it is in the population, as it
       always was, so its candidate is not silently reported missing).

Lineage:
    A rescan is an explicit link ``original -> replacement``
    (``scan_rejection.replacement_scan_id``). A chain ``A -> B -> C`` arises
    when a replacement is itself rejected and replaced. :func:`lineage_roots`
    walks every link back to its root without assuming the chain is sound:
    a cycle is reported, never followed forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping


class SheetDisposition(StrEnum):
    """What one recorded sheet means for its session's examination population."""

    EFFECTIVE = "effective"
    """Read, active, in a live batch of this session: counts."""

    EFFECTIVE_UNREADABLE = "effective_unreadable"
    """Read and found unreadable (failed recognition), active: a physical
    script in the population whose identity is not yet known. Counts as a
    script; supplies no answers."""

    NOT_READ = "not_read"
    """Registered but never read (pending, queued, cancelled): not yet part
    of anything."""

    SUPERSEDED_BY_REPLACEMENT = "superseded_by_replacement"
    """A rejected original whose rescan is confirmed: history only."""

    REJECTED_PENDING_RESCAN = "rejected_pending_rescan"
    """Rejected; its rescan is awaited. Listed against its candidate, never
    counted."""

    REIMPORT_OF_REJECTED = "reimport_of_rejected"
    """The exact bytes of a rejected scan imported again: not a script."""

    EXCLUDED = "excluded"
    """Rejected / excluded on the Attendance stage: never counted."""

    DEFERRED = "deferred"
    """Decision postponed: listed, not counted while deferred."""

    BATCH_SUPERSEDED = "batch_superseded"
    """Its whole batch was replaced by a later reading (*Reprocess All*)."""

    COUNTED_IN_OTHER_SESSION = "counted_in_other_session"
    """A confirmed rescan of a sheet that belongs to another scan session; it
    counts there."""

    @property
    def counts(self) -> bool:
        """Whether a sheet with this disposition is in the effective set."""
        return self in (SheetDisposition.EFFECTIVE, SheetDisposition.EFFECTIVE_UNREADABLE)

    @property
    def is_listed(self) -> bool:
        """Whether Attendance lists the sheet against its candidate without counting it.

        A sheet awaiting a rescan tells its candidate *rescan required*; a
        deferred one is still an open item. Both are visible, neither counts.
        """
        return self in (
            SheetDisposition.REJECTED_PENDING_RESCAN,
            SheetDisposition.DEFERRED,
        )

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return _LABELS[self]


_LABELS: dict[SheetDisposition, str] = {
    SheetDisposition.EFFECTIVE: "Counts",
    SheetDisposition.EFFECTIVE_UNREADABLE: "Counts - unreadable, identity unknown",
    SheetDisposition.NOT_READ: "Not read yet",
    SheetDisposition.SUPERSEDED_BY_REPLACEMENT: "Replaced by a rescan",
    SheetDisposition.REJECTED_PENDING_RESCAN: "Rejected - rescan awaited",
    SheetDisposition.REIMPORT_OF_REJECTED: "Re-import of a rejected scan",
    SheetDisposition.EXCLUDED: "Excluded",
    SheetDisposition.DEFERRED: "Deferred",
    SheetDisposition.BATCH_SUPERSEDED: "Batch read again (superseded)",
    SheetDisposition.COUNTED_IN_OTHER_SESSION: "Counts in another scan session",
}

_LIFECYCLE: dict[str, SheetDisposition] = {
    "superseded_by_replacement": SheetDisposition.SUPERSEDED_BY_REPLACEMENT,
    "rejected_pending_rescan": SheetDisposition.REJECTED_PENDING_RESCAN,
    "reimport_of_rejected": SheetDisposition.REIMPORT_OF_REJECTED,
    "excluded": SheetDisposition.EXCLUDED,
    "deferred": SheetDisposition.DEFERRED,
}

READ_STATUSES = frozenset({"completed", "warning", "failed"})
"""``batch_scan.status`` values meaning recognition ran and committed."""


@dataclass(frozen=True, slots=True)
class SheetFacts:
    """The persisted facts one sheet's disposition is decided from.

    Attributes:
        scan_id: The sheet.
        batch_id: The batch it was read into.
        status: ``batch_scan.status``.
        lifecycle: ``scan_rejection.state``, or ``"active"`` when it has no row.
        batch_superseded: Its batch is replaced by a live batch supersession.
        counting_session: The session its lineage counts in (its root's), or
            ``None`` when it belongs to no lineage.
    """

    scan_id: int
    batch_id: str
    status: str
    lifecycle: str = "active"
    batch_superseded: bool = False
    counting_session: str | None = None


def classify(facts: SheetFacts, session_id: str) -> SheetDisposition:
    """The one disposition of one sheet with respect to ``session_id``.

    See the module docstring for the order. Deterministic: the same facts
    always give the same answer.
    """
    if facts.batch_superseded:
        return SheetDisposition.BATCH_SUPERSEDED
    if facts.counting_session is not None and facts.counting_session != session_id:
        return SheetDisposition.COUNTED_IN_OTHER_SESSION
    if facts.lifecycle != "active":
        # An unknown state from a newer build is "not known to be eligible".
        return _LIFECYCLE.get(facts.lifecycle, SheetDisposition.REJECTED_PENDING_RESCAN)
    if facts.status not in READ_STATUSES:
        return SheetDisposition.NOT_READ
    if facts.status == "failed":
        return SheetDisposition.EFFECTIVE_UNREADABLE
    return SheetDisposition.EFFECTIVE


@dataclass(frozen=True, slots=True)
class Lineage:
    """Every explicit rescan link, walked to its roots.

    Attributes:
        root_of: ``scan id -> root scan id`` for every scan that appears in a
            link (a root maps to itself).
        cycles: Scans on a cycle of links, or on a chain leading into one.
            Their roots are undefined and they are reported, not repaired.
    """

    root_of: dict[int, int] = field(default_factory=dict)
    cycles: frozenset[int] = frozenset()


def lineage_roots(replacement_of: Mapping[int, int]) -> Lineage:
    """Walk ``replacement -> original`` links back to each lineage's root.

    Args:
        replacement_of: For every confirmed link, the replacement's scan id
            mapped to the original it replaces. A scan replaces at most one
            original (the database's unique constraint), so this is a
            function.

    Returns:
        The roots, and any scans caught in a cycle. Linear in the number of
        links: each scan's walk stops at a root already known.
    """
    root_of: dict[int, int] = {}
    cycles: set[int] = set()
    for start in replacement_of:
        if start in root_of or start in cycles:
            continue
        path: list[int] = []
        seen: set[int] = set()
        node = start
        root: int | None
        while True:
            if node in root_of:
                root = root_of[node]
                break
            if node in cycles:
                root = None
                break
            if node in seen:
                cycles.update(path[path.index(node) :])
                root = None
                break
            seen.add(node)
            path.append(node)
            parent = replacement_of.get(node)
            if parent is None:
                root = node
                break
            node = parent
        for item in path:
            if item in cycles:
                continue
            if root is None:
                cycles.add(item)
            else:
                root_of[item] = root
    return Lineage(root_of=root_of, cycles=frozenset(cycles))


__all__ = [
    "READ_STATUSES",
    "Lineage",
    "SheetDisposition",
    "SheetFacts",
    "classify",
    "lineage_roots",
]
