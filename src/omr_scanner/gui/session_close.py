"""Closing a scan session from the GUI: the one finish policy, in operator wording.

Purpose:
    Every GUI path that closes a scan session - Reports' *Close session and
    generate final export* and the Scan stage's *Close scan session* - closes
    through :func:`omr_scanner.services.session_finish.finish_scan_session`,
    the **one** backend definition of whether a session may become ``closed``
    (final reconciliation of its sources and the complete blocker set). This
    module decides nothing: it supplies the intake service the final
    reconciliation runs with and renders the typed
    :class:`~omr_scanner.domain.session_finish.FinishBlocker` codes as the
    sentences the existing dialogs list.

    Whether a *report* can be generated (a set's template, key, roster) is a
    different question, answered by the Reports page's readiness checks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from omr_scanner.domain.session_finish import (
    BlockerCode,
    FinishBlocker,
    FinishOutcome,
    IncompleteAcceptance,
)
from omr_scanner.services import intake as intake_service
from omr_scanner.services import session_finish

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from omr_scanner.services.project_service import ProjectSession

_LOGGER = logging.getLogger(__name__)

_COUNTED: dict[BlockerCode, str] = {
    BlockerCode.FILES_STABILIZING: "{n} file(s) in a watched source are still being written.",
    BlockerCode.FILES_READY: "{n} file(s) are ready but have not been registered for reading.",
    BlockerCode.SHEETS_QUEUED: (
        "{n} sheet(s) have not been read yet - resume the batch on the Scan stage."
    ),
    BlockerCode.SHEETS_PROCESSING: "{n} sheet(s) are being read.",
    BlockerCode.UNITS_RUNNING: "{n} batch(es) of this scan session are still being read.",
    BlockerCode.UNRESOLVED_CONFLICTS: "{n} conflict(s) are unresolved on the Resolve stage.",
    BlockerCode.RESCAN_OUTSTANDING: "{n} rejected sheet(s) are awaiting a rescan.",
    BlockerCode.REPLACEMENT_UNMATCHED: (
        "{n} rejected sheet(s) have a likely rescan waiting that no operator has "
        "confirmed or declined."
    ),
    BlockerCode.RESCAN_SUGGESTED: "{n} suggested rescan(s) have not been answered.",
    BlockerCode.FILES_AWAITING_DECISION: (
        "{n} held, unreadable or unsupported file(s) await an operator decision."
    ),
    BlockerCode.SHEETS_DEFERRED: "{n} sheet(s) are deferred on the Attendance stage.",
}


def blocker_message(item: FinishBlocker) -> str:
    """One blocker as the sentence the close dialogs list."""
    if item.code is BlockerCode.SESSION_NOT_OPEN:
        return "The scan session is not open."
    if item.code is BlockerCode.PROCESSING_ACTIVE:
        return "Another process is processing this project - stop it first."
    if item.code is BlockerCode.SOURCE_UNREACHABLE:
        detail = f" ({item.detail})" if item.detail else ""
        return f"Source '{item.source_label}' could not be reached for its final check{detail}."
    if item.code is BlockerCode.SOURCE_NOT_RECONCILED:
        return f"Source '{item.source_label}' could not be given its final check."
    return _COUNTED[item.code].format(n=item.count)


def blocker_messages(items: Iterable[FinishBlocker]) -> list[str]:
    """Every blocker as a sentence, in the order the service found them."""
    return [blocker_message(item) for item in items]


def refusal_messages(outcome: FinishOutcome) -> list[str]:
    """Why ``outcome`` did not close - never empty for a refused outcome."""
    found = blocker_messages(outcome.blockers)
    return found or ["The scan session changed while it was being checked - try again."]


def make_intake(project: ProjectSession) -> intake_service.IntakeService | None:
    """The intake service for the final reconciliation, or ``None`` if none can be built.

    Built without restart recovery: the final reconciliation only lists and
    stabilises a source; it never registers. Without one, every enabled
    source attached to the session blocks as ``source_not_reconciled``.
    Looked up at call time, so a test can give the final reconciliation its
    fake disk.
    """
    try:
        return intake_service.IntakeService(project.database, project.root, recover=False)
    except Exception:
        _LOGGER.exception("No intake service for the final reconciliation")
        return None


def finish(
    project: ProjectSession,
    scan_session_id: str,
    *,
    operator: str,
    reason: str = "",
    accept_incomplete: bool = False,
) -> FinishOutcome:
    """Run *Finish scan session* for a GUI close.

    ``accept_incomplete`` is the named ``operator``'s explicit acceptance of
    incomplete results (outstanding rescans, unmatched replacements, deferred
    sheets only), audited with the close.

    Raises:
        omr_scanner.errors.OMRScannerError: No operator named, or no such session.
    """
    return session_finish.finish_scan_session(
        project.database,
        scan_session_id,
        closed_by=operator,
        intake=make_intake(project),
        acknowledge=(
            IncompleteAcceptance(operator, "(incomplete results accepted)")
            if accept_incomplete
            else None
        ),
        reason=reason,
    )


__all__ = [
    "blocker_message",
    "blocker_messages",
    "finish",
    "make_intake",
    "refusal_messages",
]
