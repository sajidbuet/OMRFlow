"""Running the continuous engine off the GUI thread (0.1.1 revised phase 8).

Purpose:
    The continuous engine
    (:class:`~omr_scanner.services.continuous_engine.ContinuousEngine`) is
    Qt-free and has no thread of its own: *whichever thread calls its methods,
    one at a time, is its coordinator*. This module gives it that thread - a
    :class:`EngineThread` - and a small owner, :class:`SessionRunner`, that the
    Scan stage drives. It adds no behaviour: every decision is the engine's.

How the GUI talks to it:
    * **Commands go in through a queue.** The GUI thread never calls an engine
      method: *Finish current and stop*, *Cancel queued work*, the shutdown on
      exit and *Finish scan session* (which the engine runs as the coordinator,
      so its final reconciliation is done by the ledger's one writer) are
      posted to the engine thread and executed between two steps.
    * **Results come out as signals**, delivered on the GUI thread: the
      startup report, a status about once a second, a refusal by the
      coordinator lease (:class:`~omr_scanner.services.coordinator.CoordinatorBusyError`),
      a failure, the finish outcome, and the final status when it stops.
    * Pause / resume of processing and intake are **not** commands: they are
      persisted operator intent (:mod:`omr_scanner.services.session_controls`),
      written by the GUI thread, which the engine re-reads on every step.

What does NOT belong here:
    * Widgets and dialogs. Nothing in this module touches a widget or opens a
      modal: a worker callback that prompts is the hang this repository has
      fixed twice. The page renders signals; its ``_prompt_*`` methods ask.
    * Business rules - when a session may close, what is caught up, what is
      blocked. Those are the services'.

Lifetime:
    The engine is built *inside* the thread by the factory the caller supplies
    (the production factory starts a warm worker-process pool lazily, on the
    first sheet). On stop the engine settles - draining, committing, releasing
    claims, finishing units and closing its pool - and releases the project's
    coordinator lease before :attr:`EngineThread.stopped` is emitted. The owner
    waits for the thread before the project database may close.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from omr_scanner.domain.processing import EngineState
from omr_scanner.errors import OMRScannerError
from omr_scanner.services.coordinator import CoordinatorBusyError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.session_finish import IncompleteAcceptance
    from omr_scanner.services.continuous_engine import ContinuousEngine

_LOGGER = logging.getLogger(__name__)

STEP_WAIT_SECONDS = 0.2
"""How long one engine step may wait for the first finished sheet."""

IDLE_SLEEP_SECONDS = 0.25
"""How long the loop rests when a step did nothing (woken early by a command)."""

STATUS_INTERVAL_SECONDS = 1.0
"""How often :attr:`EngineThread.status_changed` is emitted while running."""

SHUTDOWN_DRAIN_SECONDS = 30.0
"""How long a shutdown lets sheets inside workers finish and commit."""

SHUTDOWN_WAIT_MS = 90_000
"""How long the owner waits for the thread on exit - longer than the drain, so
the pool is torn down before the database goes away."""

EngineFactory = Callable[[], "ContinuousEngine"]
"""Builds the engine, inside the engine thread."""


class StopMode(StrEnum):
    """How the engine is to stop."""

    SHUTDOWN = "shutdown"
    """Leaving the project or the application: drain and settle, and leave the
    persisted processing intent exactly as it is - the session stays open and
    its intent is not an exit decision."""
    FINISH_CURRENT = "finish_current"
    """*Finish current and stop* (persists ``stopped`` first)."""
    CANCEL_QUEUED = "cancel_queued"
    """*Cancel queued work* (named, audited; persists ``stopped`` first)."""


@dataclass(frozen=True, slots=True)
class _Stop:
    mode: StopMode
    actor: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _Finish:
    closed_by: str
    acknowledge: IncompleteAcceptance | None
    reason: str = ""


class EngineThread(QThread):
    """The continuous engine's coordinator thread.

    Signals:
        engine_started: the engine's ``StartupReport`` - recovery done, the
            persisted intent read back.
        status_changed: an ``EngineStatus`` about once a second.
        busy: the user message of a ``CoordinatorBusyError`` - another
            coordinator (the finite Scan stage) holds the project; nothing
            was started.
        failed: a message when the engine could not start or stopped on an
            error (it then gave up its lease; recovery repairs the rest).
        finish_outcome: the ``FinishOutcome`` of a *Finish scan session*.
        finish_failed: the message of a refused finish (no operator named).
        stopped: the final ``EngineStatus`` (or ``None``) once it settled.
    """

    engine_started = Signal(object)
    status_changed = Signal(object)
    busy = Signal(str)
    failed = Signal(str)
    finish_outcome = Signal(object)
    finish_failed = Signal(str)
    stopped = Signal(object)

    def __init__(self, factory: EngineFactory, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("continuousEngineThread")
        self._factory = factory
        self._commands: queue.SimpleQueue[_Stop | _Finish] = queue.SimpleQueue()
        self._wake = threading.Event()
        self._stop: _Stop | None = None
        self.engine: ContinuousEngine | None = None
        """The engine, once built - read by tests after the thread finished."""

    # ------------------------------------------------------------------
    # Commands (any thread)
    # ------------------------------------------------------------------
    def request_stop(self, mode: StopMode, *, actor: str = "", reason: str = "") -> None:
        """Ask the engine to stop the way ``mode`` says. Returns at once."""
        self._commands.put(_Stop(mode, actor, reason))
        self._wake.set()

    def request_finish(
        self,
        *,
        closed_by: str,
        acknowledge: IncompleteAcceptance | None = None,
        reason: str = "",
    ) -> None:
        """Ask the engine to run *Finish scan session* as the coordinator."""
        self._commands.put(_Finish(closed_by, acknowledge, reason))
        self._wake.set()

    # ------------------------------------------------------------------
    # The thread
    # ------------------------------------------------------------------
    def run(self) -> None:  # noqa: D102 - QThread entry point, documented on the class
        try:
            engine = self._factory()
        except Exception as exc:  # deliberately broad: nothing has started
            _LOGGER.exception("The continuous engine could not be built")
            self.failed.emit(_message(exc))
            self.stopped.emit(None)
            return
        self.engine = engine
        try:
            report = engine.start()
        except CoordinatorBusyError as busy:
            _LOGGER.info("Continuous engine refused: %s", busy)
            engine.shutdown()  # never started: closes the recogniser only
            self.busy.emit(busy.user_message)
            self.stopped.emit(None)
            return
        except Exception as exc:  # start() released its lease and changed nothing
            _LOGGER.exception("The continuous engine could not start")
            engine.shutdown()
            self.failed.emit(_message(exc))
            self.stopped.emit(None)
            return
        self.engine_started.emit(report)
        try:
            self._loop(engine)
        except Exception as exc:  # the engine abandoned itself and gave up its lease
            _LOGGER.exception("The continuous engine stopped on an error")
            self.failed.emit(_message(exc))
            self.stopped.emit(_status(engine))
            return
        self.stopped.emit(self._settle(engine))

    def _loop(self, engine: ContinuousEngine) -> None:
        last_status = 0.0
        while engine.state is EngineState.RUNNING:
            self._run_commands(engine)
            if self._stop is not None:
                return
            engine.poll_intake()
            engine.form_units()
            report = engine.step(wait=STEP_WAIT_SECONDS)
            now = time.monotonic()
            if now - last_status >= STATUS_INTERVAL_SECONDS:
                last_status = now
                self.status_changed.emit(engine.status())
            if report.idle and not engine.in_flight:
                self._wake.wait(IDLE_SLEEP_SECONDS)
                self._wake.clear()

    def _run_commands(self, engine: ContinuousEngine) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if isinstance(command, _Stop):
                if self._stop is None:
                    self._stop = command
                continue
            try:
                outcome = engine.finish_session(
                    closed_by=command.closed_by,
                    acknowledge=command.acknowledge,
                    reason=command.reason,
                )
            except OMRScannerError as exc:
                self.finish_failed.emit(exc.user_message or str(exc))
                continue
            self.finish_outcome.emit(outcome)

    def _settle(self, engine: ContinuousEngine) -> object:
        """Stop the engine the way it was asked; return its final status."""
        stop = self._stop or _Stop(StopMode.SHUTDOWN)
        try:
            if stop.mode is StopMode.FINISH_CURRENT:
                return engine.finish_current_and_stop(actor=stop.actor, reason=stop.reason)
            if stop.mode is StopMode.CANCEL_QUEUED:
                return engine.cancel_queued_and_stop(actor=stop.actor, reason=stop.reason)
            return engine.shutdown(drain=True, timeout=SHUTDOWN_DRAIN_SECONDS)
        except OMRScannerError as exc:
            # A refused operator stop (no operator named): still stop safely.
            _LOGGER.warning("Stop refused (%s); shutting down without it", exc)
            self.failed.emit(exc.user_message or str(exc))
            return engine.shutdown(drain=True, timeout=SHUTDOWN_DRAIN_SECONDS)
        except Exception as exc:
            _LOGGER.exception("The continuous engine failed while stopping")
            self.failed.emit(_message(exc))
            return _status(engine)


def _message(exc: BaseException) -> str:
    if isinstance(exc, OMRScannerError) and exc.user_message:
        return exc.user_message
    return str(exc) or type(exc).__name__


def _status(engine: ContinuousEngine) -> object:
    try:
        return engine.status()
    except Exception:  # the database may be the problem
        return None


class SessionRunner(QObject):
    """Owns at most one :class:`EngineThread` and re-emits its signals.

    One per Scan page. The page's commands call :meth:`start`, :meth:`stop`
    and :meth:`finish_session`; leaving a project or the application calls
    :meth:`shutdown`, which **waits**: the engine drains, commits, releases
    the coordinator lease and closes its worker pool before the database is
    released.

    Signals: as :class:`EngineThread`, plus ``running_changed(bool)``.
    """

    engine_started = Signal(object)
    status_changed = Signal(object)
    busy = Signal(str)
    failed = Signal(str)
    finish_outcome = Signal(object)
    finish_failed = Signal(str)
    stopped = Signal(object)
    running_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: EngineThread | None = None
        self._stopping: StopMode | None = None
        self.last_status: object | None = None
        """The most recent ``EngineStatus`` (``None`` before the first)."""

    @property
    def is_running(self) -> bool:
        """Whether an engine thread exists and has not finished."""
        return self._thread is not None and self._thread.isRunning()

    @property
    def stopping(self) -> StopMode | None:
        """The stop requested of the running engine, or ``None``."""
        return self._stopping if self.is_running else None

    @property
    def engine_thread(self) -> EngineThread | None:
        """The current engine thread (tests wait on it)."""
        return self._thread

    def start(self, factory: EngineFactory) -> bool:
        """Start an engine thread. ``False`` when one is already running."""
        if self.is_running:
            return False
        self._reap()
        thread = EngineThread(factory, self)
        thread.engine_started.connect(self.engine_started)
        thread.status_changed.connect(self._on_status)
        thread.busy.connect(self.busy)
        thread.failed.connect(self.failed)
        thread.finish_outcome.connect(self.finish_outcome)
        thread.finish_failed.connect(self.finish_failed)
        thread.stopped.connect(self._on_stopped)
        self._thread = thread
        self._stopping = None
        self.last_status = None
        thread.start()
        self.running_changed.emit(True)
        return True

    def stop(self, mode: StopMode, *, actor: str = "", reason: str = "") -> bool:
        """Ask the running engine to stop. ``False`` when none is running."""
        thread = self._thread
        if thread is None or not thread.isRunning():
            return False
        self._stopping = mode
        thread.request_stop(mode, actor=actor, reason=reason)
        return True

    def finish_session(
        self,
        *,
        closed_by: str,
        acknowledge: IncompleteAcceptance | None = None,
        reason: str = "",
    ) -> bool:
        """Run *Finish scan session* in the engine thread. ``False`` when none runs."""
        thread = self._thread
        if thread is None or not thread.isRunning():
            return False
        thread.request_finish(closed_by=closed_by, acknowledge=acknowledge, reason=reason)
        return True

    def wait(self, timeout_ms: int = SHUTDOWN_WAIT_MS) -> bool:
        """Wait for the engine thread to finish. ``True`` when it has (or none ran)."""
        thread = self._thread
        if thread is None:
            return True
        return bool(thread.wait(timeout_ms))

    def shutdown(self, timeout_ms: int = SHUTDOWN_WAIT_MS) -> bool:
        """Stop safely and wait - for leaving a project or the application.

        The persisted processing intent is left as it is: leaving is not an
        operator decision about the session, which stays open.
        """
        thread = self._thread
        if thread is None:
            return True
        if thread.isRunning():
            thread.request_stop(StopMode.SHUTDOWN)
            if not thread.wait(timeout_ms):
                _LOGGER.error(
                    "The continuous engine did not stop within %d ms; leaving it to settle",
                    timeout_ms,
                )
                return False
        self._disconnect(thread)
        self._thread = None
        self._stopping = None
        self.running_changed.emit(False)
        return True

    def _on_status(self, status: object) -> None:
        self.last_status = status
        self.status_changed.emit(status)

    def _on_stopped(self, status: object) -> None:
        if status is not None:
            self.last_status = status
        thread = self._thread
        if thread is not None:
            # `stopped` is the thread's last act; wait the instant it takes to
            # return, so `is_running` is already false for every listener.
            thread.wait(5_000)
        self._stopping = None
        self.stopped.emit(status)
        self.running_changed.emit(False)

    def _reap(self) -> None:
        """Forget a finished thread before starting another."""
        thread = self._thread
        if thread is not None and not thread.isRunning():
            thread.wait()
            self._disconnect(thread)
            self._thread = None

    @staticmethod
    def _disconnect(thread: EngineThread) -> None:
        for signal in (
            thread.engine_started,
            thread.status_changed,
            thread.busy,
            thread.failed,
            thread.finish_outcome,
            thread.finish_failed,
            thread.stopped,
        ):
            with contextlib.suppress(RuntimeError, TypeError):  # nothing connected
                signal.disconnect()
        thread.deleteLater()


__all__ = [
    "IDLE_SLEEP_SECONDS",
    "SHUTDOWN_WAIT_MS",
    "STATUS_INTERVAL_SECONDS",
    "EngineFactory",
    "EngineThread",
    "SessionRunner",
    "StopMode",
]
