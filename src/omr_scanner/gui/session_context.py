"""The one-line scan-session context a workflow stage shows above its work.

Purpose:
    Resolve and Answer Key name the scan session the operator is working
    through in the same words: ``Scan session <name> · open · results
    provisional`` - or ``closed``, or ``open (reopened)``. The wording lives
    here so the stages cannot drift apart (0.1.1 revised phase 8).

    It renders a session it is given. Which session a stage shows is the
    stage's existing selection - the active/downstream scan session the main
    window hands every stage - never chosen here.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.gui.theme import Color
from omr_scanner.services import scan_sessions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.services import ProjectDatabase


def session_status(info: scan_sessions.ScanSessionInfo) -> str:
    """``open · results provisional``, ``open (reopened) · results provisional`` or ``closed``."""
    if info.state is ScanSessionState.CLOSED:
        return "closed"
    if info.final_outputs_stale_since is not None:
        return "open (reopened) · results provisional"
    return "open · results provisional"


def session_context_html(info: scan_sessions.ScanSessionInfo | None) -> str:
    """The context line for ``info``, or ``""`` for no session or a virtual one."""
    if info is None or info.virtual:
        return ""
    return (
        f"<span style='color:{Color.TEXT_SECONDARY};'>Scan session "
        f"<b>{html.escape(info.name)}</b> · {session_status(info)}</span>"
    )


def read_session(
    database: ProjectDatabase | None, scan_session_id: str | None
) -> scan_sessions.ScanSessionInfo | None:
    """The session ``scan_session_id`` names, or ``None`` (no project, none, or gone)."""
    if database is None or not scan_session_id:
        return None
    return scan_sessions.get_scan_session(database, scan_session_id)
