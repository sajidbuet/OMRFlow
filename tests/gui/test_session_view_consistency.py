"""One session view is one moment of the session's lifecycle (revised phase 8).

:func:`~omr_scanner.gui.scan.session_poller.read_session_view` reads the
snapshot and the session record separately, off the GUI thread. A close or
reopen committed between the two reads produced a torn view - the snapshot
still ``closed``, the record already reopened - and the session panel, which
reads its lifecycle from the snapshot, said *CLOSED* for a reopened session
until the next poll. Found as an intermittent failure of
``test_session_finish_gui::TestReopen`` under ``tests/gui`` load; reproduced
here deterministically by committing the reopen exactly between the reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from tests.integration.test_reject_and_rescan import OPERATOR, build_world
from tests.integration.test_session_population import SessionWorld

from omr_scanner.domain.scan_lifecycle import RejectionReason
from omr_scanner.domain.scan_sessions import ScanSessionState
from omr_scanner.gui.scan import session_poller
from omr_scanner.gui.scan.session_panel import SessionPanel
from omr_scanner.services import create_project, scan_lifecycle, scan_sessions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.gui


@pytest.fixture
def sw(workspace: Path, tmp_path: Path) -> Iterator[SessionWorld]:
    session = create_project(workspace, "View Consistency")
    try:
        world = SessionWorld(build_world(session, tmp_path))
        for name in ("s_x.png", "s_blur.png"):
            scan_lifecycle.exclude_scan(
                world.database, world.ids[name], reviewer=OPERATOR,
                reason=RejectionReason.FOLDED,
            )
        yield world
    finally:
        if not session.is_closed:
            session.close()


def commit_between_reads(monkeypatch, change: Callable[[], object]) -> list[int]:
    """Run ``change`` right after the first snapshot is taken; count snapshots."""
    real = session_poller.session_snapshot.take_snapshot
    calls: list[int] = []

    def take(*args: Any, **kwargs: Any) -> Any:
        found = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            change()
        return found

    monkeypatch.setattr(session_poller.session_snapshot, "take_snapshot", take)
    return calls


def test_a_reopen_between_the_reads_gives_a_consistent_view(qtbot, sw, monkeypatch):
    scan_sessions.close_scan_session(sw.database, sw.scan_session_id, closed_by=OPERATOR)
    commit_between_reads(
        monkeypatch,
        lambda: scan_sessions.reopen_scan_session(
            sw.database, sw.scan_session_id, reopened_by=OPERATOR
        ),
    )
    view = session_poller.read_session_view(sw.database, sw.scan_session_id)
    assert view.session is not None
    assert view.session.state is ScanSessionState.OPEN and view.session.reopen_count == 1
    assert view.snapshot.session_state == "open"

    panel = SessionPanel()
    qtbot.addWidget(panel)
    panel.show_view(view)
    assert panel.lifecycle_label.text() == "OPEN · REOPENED"


def test_a_close_between_the_reads_gives_a_consistent_view(sw, monkeypatch):
    commit_between_reads(
        monkeypatch,
        lambda: scan_sessions.close_scan_session(
            sw.database, sw.scan_session_id, closed_by=OPERATOR
        ),
    )
    view = session_poller.read_session_view(sw.database, sw.scan_session_id)
    assert view.session is not None and view.session.state is ScanSessionState.CLOSED
    assert view.snapshot.session_state == "closed"


def test_a_quiet_session_is_read_once(sw, monkeypatch):
    calls = commit_between_reads(monkeypatch, lambda: None)
    view = session_poller.read_session_view(sw.database, sw.scan_session_id)
    assert view.snapshot.session_state == "open"
    assert len(calls) == 1
