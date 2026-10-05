"""Polling a scan session's snapshot without blocking the GUI (0.1.1 revised phase 8).

Purpose:
    The operational Scan stage shows a live session from
    :func:`omr_scanner.services.session_snapshot.take_snapshot` - an immutable
    value from a bounded number of grouped queries (tens to low hundreds of
    milliseconds at 10,000-100,000 sheets, ``PHASE_G_HANDOFF.md`` §13). This
    module **pulls** it on a timer, off the GUI thread, and hands each result
    to the GUI thread as one signal. Nothing is pushed by the engine, and no
    count is computed here.

Bounded by construction:
    * One worker thread, kept for the poller's life; one request at a time.
    * A timer tick that finds a request still running does **not** queue
      another: it marks one refresh as owed, and that one runs when the
      current one returns. However slow the database, there is at most one
      pending refresh - never a backlog of snapshots behind each other.
    * Each request carries a generation number; a result for a session the
      page has since left is dropped on arrival.

What does NOT belong here:
    Widgets, dialogs, and any rule: the caught-up state, the partition and
    the progress lines are the snapshot's. ``read_session_view`` only collects
    the service values a view renders.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from omr_scanner.services import intake as intake_service
from omr_scanner.services import scan_sessions, session_snapshot

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from datetime import datetime

    from omr_scanner.domain.session_snapshot import SessionSnapshot
    from omr_scanner.services import ProjectDatabase
    from omr_scanner.services.intake import SourceInfo

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL_MS = 1_000
"""How often a session's snapshot is taken: once a second.

Not the per-batch tracker's 200 ms: a snapshot is a database read (tens to a
few hundred milliseconds at 100k rows), and five a second would keep a reader
on the database most of the time - in rollback-journal mode a writer waits
for readers to finish. Once a second is what the phase 7 contract measured
and is fast enough for counts a person reads."""

CONSISTENT_READ_ATTEMPTS = 3
"""Snapshots taken at most for one view while a close / reopen races the read;
past that the newest pair is shown and the next poll settles it."""


def _lifecycle(info: scan_sessions.ScanSessionInfo | None) -> tuple[object, ...]:
    """The part of a session record a snapshot's ``session_state`` must agree with."""
    if info is None:
        return ()
    return (info.state, info.reopen_count, info.closed_at, info.final_outputs_stale_since)


@dataclass(frozen=True, slots=True)
class SessionView:
    """Everything one refresh of the operational view renders, read together.

    Attributes:
        snapshot: The session snapshot (counts, progress, activity, sources).
        session: The session's identity and lifecycle (name, open / closed,
            reopened, final outputs stale since), or ``None`` if it is gone.
        sources: The intake sources attached to the session, as configured
            (label, path, enabled, reachability and since when, last file).
        elapsed_ms: How long reading this took - recorded, for the
            responsiveness evidence.
    """

    snapshot: SessionSnapshot
    session: scan_sessions.ScanSessionInfo | None
    sources: tuple[SourceInfo, ...]
    elapsed_ms: float = 0.0


def read_session_view(
    database: ProjectDatabase, scan_session_id: str, *, now: datetime | None = None
) -> SessionView:
    """Collect one :class:`SessionView` from the services. Read-only.

    ``now``: the snapshot's moment (the service's own clock when ``None``).
    """
    started = time.perf_counter()
    # The snapshot and the session record are separate reads. A close or
    # reopen committed between them paired a "closed" snapshot with a
    # reopened record (or the reverse), and the panel showed both until the
    # next poll. So the record is read on both sides of the snapshot, and the
    # pair is taken again if the lifecycle moved in between.
    info = scan_sessions.get_scan_session(database, scan_session_id)
    for _attempt in range(CONSISTENT_READ_ATTEMPTS):
        snapshot = session_snapshot.take_snapshot(database, scan_session_id, now=now)
        after = scan_sessions.get_scan_session(database, scan_session_id)
        moved = _lifecycle(after) != _lifecycle(info)
        info = after
        if not moved:
            break
    sources = tuple(
        item
        for item in intake_service.list_sources(database)
        if item.attached_session_id == scan_session_id
    )
    return SessionView(
        snapshot=snapshot,
        session=info,
        sources=sources,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


class _SnapshotWorker(QObject):
    """Lives in the poller's thread; takes one view per request."""

    done = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self.clock: Callable[[], datetime] | None = None

    @Slot(object, str, int)
    def take(self, database: object, scan_session_id: str, generation: int) -> None:
        try:
            view = read_session_view(
                database,  # type: ignore[arg-type]
                scan_session_id,
                now=self.clock() if self.clock is not None else None,
            )
        except Exception as exc:  # deliberately broad: a poll must never kill the thread
            _LOGGER.warning("Session snapshot failed: %s", exc)
            self.failed.emit(generation, str(exc) or type(exc).__name__)
            return
        self.done.emit(generation, view)


class SessionPoller(QObject):
    """Takes a session's :class:`SessionView` about once a second, off the GUI thread.

    Signals:
        view_ready: a :class:`SessionView`, on the GUI thread.
        failed: a message when a read failed (the previous view stands; the
            next tick tries again).

    Use :meth:`watch` to follow a session, :meth:`refresh` for an immediate
    read after an operator action, and :meth:`stop` before the database
    closes - it waits for the read in progress.
    """

    view_ready = Signal(object)
    failed = Signal(str)
    _request = Signal(object, str, int)

    def __init__(
        self, parent: QObject | None = None, *, interval_ms: int = POLL_INTERVAL_MS
    ) -> None:
        super().__init__(parent)
        self._database: ProjectDatabase | None = None
        self._session_id = ""
        self._generation = 0
        self._busy = False
        self._owed = False
        self._thread: QThread | None = None
        self._worker: _SnapshotWorker | None = None
        self.requests = 0
        """How many reads were issued (tests assert the bound)."""
        self.clock: Callable[[], datetime] | None = None
        """The snapshot's clock; ``None`` is the service's own (real time).
        Tests driving intake on a fake clock set theirs here."""
        self.last_view: SessionView | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.refresh)

    @property
    def watching(self) -> str:
        """The session being followed, or ``""``."""
        return self._session_id

    @property
    def busy(self) -> bool:
        """Whether a read is in progress."""
        return self._busy

    def watch(self, database: ProjectDatabase | None, scan_session_id: str) -> None:
        """Follow ``scan_session_id`` (``""`` or no database: stop following)."""
        if database is self._database and scan_session_id == self._session_id:
            if scan_session_id and not self.timer.isActive():
                self.timer.start()
            return
        self._generation += 1
        self._owed = False
        self.last_view = None
        self._database = database
        self._session_id = scan_session_id if database is not None else ""
        if not self._session_id:
            self.timer.stop()
            return
        self._ensure_thread()
        self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        """Read now - or, if a read is running, owe exactly one more."""
        if self._database is None or not self._session_id:
            return
        if self._busy:
            self._owed = True
            return
        self._ensure_thread()
        if self._worker is not None:
            self._worker.clock = self.clock
        self._busy = True
        self.requests += 1
        self._request.emit(self._database, self._session_id, self._generation)

    def stop(self) -> None:
        """Stop polling and wait for the read in progress. Safe to call twice."""
        self.timer.stop()
        self._generation += 1
        self._database = None
        self._session_id = ""
        self._owed = False
        thread = self._thread
        worker = self._worker
        if worker is not None:
            with contextlib.suppress(RuntimeError, TypeError):  # already disconnected
                self._request.disconnect(worker.take)
        if thread is not None:
            # The worker is deleted by the thread's own `finished` (connected
            # when it was made): a deferred delete posted from here would wait
            # for an event loop that has already stopped.
            thread.quit()
            thread.wait()
            thread.deleteLater()
        self._thread = None
        self._worker = None
        self._busy = False

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        thread = QThread(self)
        thread.setObjectName("sessionSnapshotThread")
        worker = _SnapshotWorker()
        worker.clock = self.clock
        worker.moveToThread(thread)
        self._request.connect(worker.take)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        thread.finished.connect(worker.deleteLater)
        thread.start()
        self._thread = thread
        self._worker = worker

    def _on_done(self, generation: int, view: object) -> None:
        self._busy = False
        if generation == self._generation:
            self.last_view = view  # type: ignore[assignment]
            self.view_ready.emit(view)
        self._settle_owed()

    def _on_failed(self, generation: int, message: str) -> None:
        self._busy = False
        if generation == self._generation:
            self.failed.emit(message)
        self._settle_owed()

    def _settle_owed(self) -> None:
        if self._owed:
            self._owed = False
            self.refresh()


__all__ = ["POLL_INTERVAL_MS", "SessionPoller", "SessionView", "read_session_view"]
