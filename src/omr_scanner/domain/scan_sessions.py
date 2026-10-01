"""Scan sessions and finite batches: the lifecycle vocabulary (0.1.1 phase 2).

Purpose:
    Name the states and rules of ``Project -> ScanSession -> finite ScanBatch
    -> sheets`` (``docs/decisions/ADR-0005-scan-sessions-and-finite-batches.md``)
    as plain values, so the service, the migration backfill, Project Health and
    the tests all apply one definition.

Responsibilities:
    * :class:`ScanSessionState` - ``open`` / ``closed``; *reopen* is a
      transition, not a third state.
    * :class:`BatchMembership` - ``open`` / ``sealed``. Orthogonal to a
      batch's processing :class:`~omr_scanner.database.models.BatchStatus`:
      sealing freezes membership, it does not finish processing.
    * :class:`BatchRole` - ``scan`` / ``rescan`` / ``reprocess`` / ``legacy``.
    * :class:`SessionAction` - the audit vocabulary.
    * :func:`supersession_problem` - whether a batch-level supersession may be
      recorded, given the live ones.
    * :func:`plan_backfill` - how pre-session batches are grouped into
      sessions on upgrade.

What does NOT belong here:
    Database, Qt or file access. :mod:`omr_scanner.services.scan_sessions`
    applies these rules to the project database.

Naming:
    A ``ScanSession`` is never held in a variable called ``session`` - in this
    code base that name means the SQLAlchemy ORM session. Use ``scan_session``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

SESSION_ENTITY = "scan_session"
"""``audit_event.entity_type`` for a session's lifecycle events."""

BATCH_ENTITY = "scan_batch"
"""``audit_event.entity_type`` for a batch's lifecycle events (seal, supersession)."""


class ScanSessionState(StrEnum):
    """Whether a session may receive new batches."""

    OPEN = "open"
    """May receive new finite batches. Its results are provisional."""

    CLOSED = "closed"
    """Receives no new batch, rescan, reprocess or intake. Every batch in it is
    sealed. Returned to :attr:`OPEN` only by an explicit, audited reopen."""

    @property
    def accepts_batches(self) -> bool:
        """Whether a new batch may be attached."""
        return self is ScanSessionState.OPEN

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {ScanSessionState.OPEN: "Open", ScanSessionState.CLOSED: "Closed"}[self]


class BatchMembership(StrEnum):
    """Whether a batch may still gain members. Derived from ``scan_batch.sealed_at``."""

    OPEN = "open"
    """May receive members - the batch the Scan stage is building."""

    SEALED = "sealed"
    """Membership is final: ``total_scans`` never changes again. Processing,
    resume, retry, review, rejection and replacement of its members continue."""

    @classmethod
    def of(cls, sealed_at: datetime | None) -> BatchMembership:
        """The membership a ``sealed_at`` value means."""
        return cls.OPEN if sealed_at is None else cls.SEALED

    @property
    def accepts_members(self) -> bool:
        """Whether a new scan may be registered in the batch."""
        return self is BatchMembership.OPEN


class BatchRole(StrEnum):
    """Why a batch exists, which decides how it contributes to its session."""

    SCAN = "scan"
    """An ordinary import (*Add Folder -> Process All*)."""

    RESCAN = "rescan"
    """Replacements for rejected sheets of a sealed batch. Its sheets count
    through a confirmed Reject & Rescan link, in the original's batch."""

    REPROCESS = "reprocess"
    """A re-reading of the same files (*Reprocess All*). Supersedes the batch it
    re-reads through a batch-supersession record."""

    LEGACY = "legacy"
    """A batch that existed before scan sessions, backfilled on upgrade.
    Contributes exactly as :attr:`SCAN`."""

    @property
    def is_primary(self) -> bool:
        """Whether the batch carries a cohort's scripts of its own.

        A ``rescan`` batch does not: its replacements are counted in the batch of
        the sheet they replace. This is what a downstream stage that still reads
        one batch (until session aggregation) selects among.
        """
        return self is not BatchRole.RESCAN

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            BatchRole.SCAN: "Scan",
            BatchRole.RESCAN: "Rescan",
            BatchRole.REPROCESS: "Reprocess",
            BatchRole.LEGACY: "Legacy",
        }[self]


class SessionAction(StrEnum):
    """``audit_event.action`` values for sessions and batch lifecycle."""

    CREATED = "session_created"
    RENAMED = "session_renamed"
    CLOSED = "session_closed"
    REOPENED = "session_reopened"
    ACTIVATED = "session_activated"
    BATCH_ATTACHED = "batch_attached"
    BATCH_SEALED = "batch_sealed"
    SUPERSEDED = "batch_superseded"
    SUPERSESSION_REVERSED = "supersede_reversed"
    COMBINED = "session_combined"
    TEMPLATE_ACKNOWLEDGED = "template_ack"
    BACKFILLED = "session_backfilled"


# ----------------------------------------------------------------------
# Transitions
# ----------------------------------------------------------------------
def close_problem(state: ScanSessionState) -> str:
    """Why a session in ``state`` cannot be closed, or ``""``."""
    return "" if state is ScanSessionState.OPEN else "This scan session is already closed."


def reopen_problem(state: ScanSessionState) -> str:
    """Why a session in ``state`` cannot be reopened, or ``""``."""
    return "" if state is ScanSessionState.CLOSED else "This scan session is already open."


def attach_problem(state: ScanSessionState) -> str:
    """Why a batch cannot be attached to a session in ``state``, or ``""``."""
    if state.accepts_batches:
        return ""
    return (
        "This scan session is closed and receives no new batches. Reopen it, or "
        "start a new scan session."
    )


# ----------------------------------------------------------------------
# Supersession
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BatchFacts:
    """What the supersession rules need to know about one batch."""

    batch_id: str
    scan_session_id: str | None
    sealed: bool


def supersession_problem(
    superseded: BatchFacts,
    superseding: BatchFacts,
    live: Mapping[str, str],
) -> str:
    """Why ``superseding`` may not supersede ``superseded``, or ``""``.

    Args:
        superseded: The batch being replaced.
        superseding: The batch replacing it.
        live: Every live (unreversed) supersession, ``superseded -> superseding``.

    The legal semantics:

    * a batch never supersedes itself;
    * both batches belong to the **same** session;
    * only a **sealed** batch can be superseded (its membership is final, so
      "what it would have contributed" is fixed);
    * a batch is superseded by at most **one** live batch - two would leave
      "which reading is effective" ambiguous;
    * no cycle: following live supersession from ``superseding`` must never
      reach ``superseded`` (``A -> B`` then ``B -> A``, or longer).

    Chains are legal (``A`` superseded by ``B``, ``B`` by ``C``): the effective
    batch is the latest non-superseded one.
    """
    if superseded.batch_id == superseding.batch_id:
        return "A batch cannot supersede itself."
    if superseded.scan_session_id is None or superseded.scan_session_id != (
        superseding.scan_session_id
    ):
        return "A batch can only be superseded by a batch of the same scan session."
    if not superseded.sealed:
        return "Only a sealed batch can be superseded; seal it first."
    if superseded.batch_id in live:
        return (
            f"Batch {superseded.batch_id[:8]} is already superseded by batch "
            f"{live[superseded.batch_id][:8]}; reverse that first."
        )
    seen: set[str] = set()
    current = superseding.batch_id
    while current in live and current not in seen:
        seen.add(current)
        current = live[current]
        if current == superseded.batch_id:
            return "That supersession would create a cycle."
    return ""


def effective_batches(batch_ids: Iterable[str], live: Mapping[str, str]) -> tuple[str, ...]:
    """The batches no live supersession replaces, in the order given."""
    return tuple(batch_id for batch_id in batch_ids if batch_id not in live)


def supersession_cycles(live: Mapping[str, str]) -> tuple[tuple[str, ...], ...]:
    """Every cycle among live supersessions (should be none). For Project Health."""
    cycles: list[tuple[str, ...]] = []
    reported: set[str] = set()
    for start in live:
        path: list[str] = []
        current = start
        while current in live and current not in path:
            path.append(current)
            current = live[current]
        if current in path:
            cycle = tuple(path[path.index(current):])
            if not reported.intersection(cycle):
                reported.update(cycle)
                cycles.append(cycle)
    return tuple(cycles)


# ----------------------------------------------------------------------
# Upgrade backfill
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LegacyBatch:
    """A batch that predates scan sessions, as the backfill sees it."""

    batch_id: str
    created_at: datetime
    template_identity: tuple[str, str, str, str]
    """``(template_id, geometry_fingerprint, recognition_fingerprint, engine_version)``."""


@dataclass(frozen=True, slots=True)
class ReplacementLink:
    """A confirmed cross-batch rescan: a sheet of one batch replaced from another."""

    original_batch: str
    replacement_batch: str


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    """How the backfill groups pre-session batches.

    Attributes:
        groups: One tuple of batch ids per session to create, each oldest first.
        ambiguous: One sentence per relationship that was **not** used to group
            batches, for the upgrade report.
    """

    groups: tuple[tuple[str, ...], ...]
    ambiguous: tuple[str, ...] = field(default_factory=tuple)


def plan_backfill(
    batches: Sequence[LegacyBatch], links: Iterable[ReplacementLink]
) -> BackfillPlan:
    """Decide which pre-session batches share a migrated session.

    **Default: each batch is its own one-batch session.** Batches are grouped
    only through a confirmed Reject & Rescan replacement whose original and
    replacement sheets are in different batches, and only when the
    relationship is **unambiguous**, defined exactly as:

    1. the batches connected by such links (directly or through other links)
       number **exactly two**;
    2. every link between them runs the **same direction** (original batch ->
       replacement batch);
    3. the replacement batch was **created after** the original batch;
    4. both batches have the **same template identity** (template id, geometry
       and recognition fingerprints, engine version).

    Anything else - three or more batches chained together, links both ways,
    a replacement older than its original, a template difference - is left as
    separate sessions and described in :attr:`BackfillPlan.ambiguous`. Results
    do not change either way: a cross-batch replacement already counts in the
    batch of the sheet it replaces. Batches with no confirmed link are never
    combined, however close in time or alike they look.
    """
    by_id = {item.batch_id: item for item in batches}
    neighbours: dict[str, set[str]] = {item.batch_id: set() for item in batches}
    directed: set[tuple[str, str]] = set()
    for link in links:
        if (
            link.original_batch == link.replacement_batch
            or link.original_batch not in by_id
            or link.replacement_batch not in by_id
        ):
            continue
        neighbours[link.original_batch].add(link.replacement_batch)
        neighbours[link.replacement_batch].add(link.original_batch)
        directed.add((link.original_batch, link.replacement_batch))

    ordered = sorted(batches, key=lambda item: (item.created_at, item.batch_id))
    groups: list[tuple[str, ...]] = []
    ambiguous: list[str] = []
    placed: set[str] = set()
    for item in ordered:
        if item.batch_id in placed:
            continue
        component = _component(item.batch_id, neighbours)
        placed.update(component)
        members = sorted(component, key=lambda batch_id: (by_id[batch_id].created_at, batch_id))
        if len(members) == 1:
            groups.append((item.batch_id,))
            continue
        reason = _ambiguity(members, directed, by_id)
        if reason:
            ambiguous.append(
                "Batches " + ", ".join(batch_id[:8] for batch_id in members)
                + f" are linked by confirmed rescans but were kept in separate "
                f"sessions: {reason}"
            )
            groups.extend((batch_id,) for batch_id in members)
        else:
            groups.append(tuple(members))
    return BackfillPlan(groups=tuple(groups), ambiguous=tuple(ambiguous))


def _component(start: str, neighbours: Mapping[str, set[str]]) -> set[str]:
    found = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for other in neighbours.get(current, ()):
            if other not in found:
                found.add(other)
                frontier.append(other)
    return found


def _ambiguity(
    members: Sequence[str],
    directed: set[tuple[str, str]],
    by_id: Mapping[str, LegacyBatch],
) -> str:
    if len(members) != 2:
        return f"{len(members)} batches are chained together, which is ambiguous."
    first, second = members
    edges = {edge for edge in directed if set(edge) == {first, second}}
    if len(edges) != 1:
        return "rescans run in both directions between them."
    ((original, replacement),) = edges
    if not by_id[replacement].created_at > by_id[original].created_at:
        return "the replacement batch is not newer than the original batch."
    if by_id[original].template_identity != by_id[replacement].template_identity:
        return "they were processed with different template identities."
    return ""


__all__ = [
    "BATCH_ENTITY",
    "SESSION_ENTITY",
    "BackfillPlan",
    "BatchFacts",
    "BatchMembership",
    "BatchRole",
    "LegacyBatch",
    "ReplacementLink",
    "ScanSessionState",
    "SessionAction",
    "attach_problem",
    "close_problem",
    "effective_batches",
    "plan_backfill",
    "reopen_problem",
    "supersession_cycles",
    "supersession_problem",
]
