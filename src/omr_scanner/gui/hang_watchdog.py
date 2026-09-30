"""Leave evidence when the interface stops responding.

Purpose:
    If the GUI thread stops processing events for longer than a threshold,
    write every thread's Python stack to a file in the log folder, and log
    where it is. When the interface recovers, log how long it was blocked.

Why this exists:
    A freeze leaves nothing behind. On 2026-09-30 the Resolve stage stopped
    responding; Windows recorded only "python.exe stopped interacting with
    Windows and was closed" (Application Hang, event 1002), and the OMRFlow
    logs simply stopped - no exception, because nothing had raised. Without a
    stack there is no way to know which line was running. This module turns
    the next freeze into a report.

How it works:
    A ``QTimer`` on the GUI thread records a heartbeat every half second. A
    daemon thread - which keeps running while the GUI thread is stuck - checks
    that heartbeat; once it is older than the threshold it calls
    :func:`faulthandler.dump_traceback` for all threads, once per stall.

What it does not do:
    Interrupt, kill or "recover" anything. It observes and records.
    It reads no project data and writes no candidate information: a Python
    stack names files, functions and line numbers only.
"""

from __future__ import annotations

import faulthandler
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer

_LOGGER = logging.getLogger(__name__)

DEFAULT_THRESHOLD_SECONDS = 10.0
"""Longer than any legitimate synchronous step on the GUI thread, shorter than
the point at which an operator gives up and closes the window."""

HEARTBEAT_MS = 500
CHECK_SECONDS = 1.0


class HangWatchdog(QObject):
    """Dump all thread stacks to ``directory`` when the GUI thread stalls.

    Args:
        directory: Where reports are written (the application log folder).
        threshold_seconds: How long the event loop may be silent before a
            report is written.
        parent: Optional Qt parent; the heartbeat timer lives on the thread
            that owns this object, which must be the GUI thread.
    """

    def __init__(
        self,
        directory: Path,
        *,
        threshold_seconds: float = DEFAULT_THRESHOLD_SECONDS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._directory = directory
        self._threshold = threshold_seconds
        self._last_beat = time.monotonic()
        self._reported_stall_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(HEARTBEAT_MS)
        self._timer.timeout.connect(self._beat)
        self.reports: list[Path] = []
        """Reports written in this session, newest last. For tests and diagnostics."""

    def start(self) -> None:
        """Begin watching. Safe to call once; later calls do nothing."""
        if self._thread is not None:
            return
        self._last_beat = time.monotonic()
        self._timer.start()
        self._thread = threading.Thread(
            target=self._watch, name="omrflow-hang-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and let the checking thread end."""
        self._timer.stop()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * CHECK_SECONDS)
            self._thread = None

    # ------------------------------------------------------------------
    def _beat(self) -> None:
        """Runs on the GUI thread whenever its event loop is alive."""
        now = time.monotonic()
        if self._reported_stall_at is not None:
            _LOGGER.warning(
                "The interface is responding again after being blocked for "
                "about %.0f s",
                now - self._reported_stall_at + self._threshold,
            )
            self._reported_stall_at = None
        self._last_beat = now

    def _watch(self) -> None:
        """Runs on the watchdog thread."""
        while not self._stop.wait(CHECK_SECONDS):
            silent = time.monotonic() - self._last_beat
            if silent < self._threshold or self._reported_stall_at is not None:
                continue
            self._reported_stall_at = time.monotonic()
            self._write_report(silent)

    def _write_report(self, silent: float) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path = self._directory / (
                f"omrflow-hang-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
            )
            with path.open("w", encoding="utf-8") as handle:
                handle.write(
                    f"OMRFlow interface not responding for {silent:.1f} s "
                    f"at {datetime.now().isoformat(timespec='seconds')}.\n"
                    "Stacks of every thread follow; the one named MainThread "
                    "is the interface.\n\n"
                )
                handle.flush()
                faulthandler.dump_traceback(file=handle, all_threads=True)
        except OSError:
            _LOGGER.exception("Could not write the hang report")
            return
        self.reports.append(path)
        _LOGGER.error(
            "The interface has not responded for %.0f s; thread stacks written "
            "to %s",
            silent,
            path,
        )


__all__ = ["DEFAULT_THRESHOLD_SECONDS", "HangWatchdog"]
