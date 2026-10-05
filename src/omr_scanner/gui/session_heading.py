"""One wording for "which scan session is this stage working through" (0.1.1 revised phase 8).

Resolve and the Answer Key stage show the same compact line:
``Scan session <name> · open · results provisional`` (``open (reopened) ·
results provisional`` after a reopen, ``closed`` once finished). The session
itself is never chosen here - each caller passes the session the main window
selected for the downstream stages (``scan_sessions.downstream_session_id``)
or one it already holds. A virtual (pre-session) record shows nothing.

Pure text from :class:`~omr_scanner.services.scan_sessions.ScanSessionInfo`,
plus one convenience read.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.services import scan_sessions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase


def session_status(info: scan_sessions.ScanSessionInfo) -> str:
    """``closed``, ``open (reopened) · results provisional`` or ``open · results provisional``."""
    if info.state is ScanSessionState.CLOSED:
        return "closed"
    if info.final_outputs_stale_since is not None:
        return "open (reopened) · results provisional"
    return "open · results provisional"


def session_heading_html(info: scan_sessions.ScanSessionInfo | None) -> str:
    """``Scan session <b>name</b> · status`` as rich text, or ``""`` for no / a virtual session."""
    if info is None or info.virtual:
        return ""
    return f"Scan session <b>{html.escape(info.name)}</b> · {session_status(info)}"


def read_session_heading(database: ProjectDatabase | None, scan_session_id: str | None) -> str:
    """:func:`session_heading_html` for a session id (one read); ``""`` when there is none."""
    if database is None or not scan_session_id:
        return ""
    return session_heading_html(scan_sessions.get_scan_session(database, scan_session_id))


__all__ = ["read_session_heading", "session_heading_html", "session_status"]
