"""Finish scan session: the blocker vocabulary (0.1.1 revised phase 7).

Purpose:
    Name every reason a scan session may not be closed yet
    (``ACCEPTANCE_CRITERIA.md`` §3, ``ARCHITECTURE_NOTES.md`` §14.3) as a
    typed code with a count, so the backend returns the **complete** set of
    blockers and phase 8 renders them - no preformatted sentences in the core
    API.

The rule:
    A session closes only after a final reconciliation of every enabled
    source attached to it, and only when nothing is stabilising, ready, queued
    or being read, no unit is still running, no required conflict is
    unresolved, no rescan is outstanding or unmatched, no suggested rescan is
    unanswered, no file awaits a decision and every enabled source was
    reachable for that reconciliation. Disabled sources are reported by name,
    never treated as checked. **A quiet period never closes a session.**

Incomplete results:
    Outstanding rescans (with or without a likely replacement waiting) and
    deferred sheets may be closed past **only** by a named operator's explicit,
    audited :class:`IncompleteAcceptance` - the same decision the existing
    *Export incomplete results* records. There is no other bypass and no
    ``force``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BlockerCode(StrEnum):
    """Why a session cannot close yet."""

    SESSION_NOT_OPEN = "session_not_open"
    PROCESSING_ACTIVE = "processing_active"
    """Another coordinator is processing the project: stop it first."""
    FILES_STABILIZING = "files_stabilizing"
    FILES_READY = "files_ready"
    SHEETS_QUEUED = "sheets_queued"
    SHEETS_PROCESSING = "sheets_processing"
    UNITS_RUNNING = "units_running"
    """A unit is still ``running`` (being read, or left by a coordinator that
    stopped before finishing its cross-sheet pass - recovery completes it)."""
    UNRESOLVED_CONFLICTS = "unresolved_conflicts"
    RESCAN_OUTSTANDING = "rescan_outstanding"
    """Rejected sheets whose rescan has not arrived (no likely replacement)."""
    REPLACEMENT_UNMATCHED = "replacement_unmatched"
    """Rejected sheets for which a likely rescan is in the session but no
    operator has confirmed (or declined) it."""
    RESCAN_SUGGESTED = "rescan_suggested"
    """Suggested rescans (quality policy) no operator has answered."""
    FILES_AWAITING_DECISION = "files_awaiting_decision"
    """Held, unreadable or unsupported files awaiting an operator decision."""
    SHEETS_DEFERRED = "sheets_deferred"
    SOURCE_UNREACHABLE = "source_unreachable"
    """An enabled source could not be listed for the final reconciliation."""
    SOURCE_NOT_RECONCILED = "source_not_reconciled"
    """An enabled source could not be reconciled at all (no intake service)."""

    @property
    def acknowledgeable(self) -> bool:
        """Whether a named operator may close past it by accepting incomplete results."""
        return self in _ACKNOWLEDGEABLE


_ACKNOWLEDGEABLE = frozenset(
    {
        BlockerCode.RESCAN_OUTSTANDING,
        BlockerCode.REPLACEMENT_UNMATCHED,
        BlockerCode.SHEETS_DEFERRED,
    }
)


@dataclass(frozen=True, slots=True)
class FinishBlocker:
    """One reason the session cannot close, with how many items it concerns.

    Attributes:
        code: The typed reason.
        count: How many files, sheets, conflicts or units (``1`` for a source).
        source_id / source_label: For a source blocker, which source.
        detail: Machine detail (the listing error for an unreachable source);
            not operator wording.
    """

    code: BlockerCode
    count: int = 1
    source_id: str | None = None
    source_label: str = ""
    detail: str = ""

    @property
    def acknowledgeable(self) -> bool:
        """See :attr:`BlockerCode.acknowledgeable`."""
        return self.code.acknowledgeable


@dataclass(frozen=True, slots=True)
class IncompleteAcceptance:
    """A named operator's explicit acceptance of incomplete results. Audited with the close."""

    reviewer: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SourceCheck:
    """What the final reconciliation found for one source."""

    source_id: str
    label: str
    enabled: bool
    reachability: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FinishOutcome:
    """The result of *Finish scan session*.

    Attributes:
        scan_session_id: The session.
        closed: Whether it is now closed.
        blockers: **Every** blocker found (empty when closed without
            acceptance); never just the first.
        accepted: Blockers closed past by an :class:`IncompleteAcceptance`.
        sources: The final reconciliation, per enabled source.
        disabled_sources: Disabled sources attached to the session, by label
            - reported, never treated as checked.
    """

    scan_session_id: str
    closed: bool
    blockers: tuple[FinishBlocker, ...] = ()
    accepted: tuple[FinishBlocker, ...] = ()
    sources: tuple[SourceCheck, ...] = ()
    disabled_sources: tuple[str, ...] = ()

    @property
    def codes(self) -> tuple[BlockerCode, ...]:
        """The blockers' codes, in the order found."""
        return tuple(item.code for item in self.blockers)


__all__ = [
    "BlockerCode",
    "FinishBlocker",
    "FinishOutcome",
    "IncompleteAcceptance",
    "SourceCheck",
]
