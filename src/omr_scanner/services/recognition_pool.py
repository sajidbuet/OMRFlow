"""Recognition workers for the continuous engine: bounded, warm, never writing.

Purpose:
    Give :mod:`omr_scanner.services.continuous_engine` one small interface to
    "read these sheets somewhere and tell me when each is done" - a pool of
    worker processes kept warm across finite units, or the calling thread -
    without the engine knowing anything about multiprocessing (0.1.1 revised
    phase 6, ``docs/decisions/ADR-0009-continuous-engine-single-writer.md``).

Responsibilities:
    * :class:`Recogniser` - the protocol: ``submit`` a ticket, ``poll`` for
      finished tickets, ``cancel_queued``, ``close``.
    * :class:`ProcessRecogniser` - a ``spawn`` process pool running the
      **same** worker entry points as the finite path
      (:func:`~omr_scanner.services.parallel_batch.worker_initialise`,
      :func:`~omr_scanner.services.parallel_batch.worker_recognise`), so a
      sheet read here produces exactly the result it produces there.
    * :class:`InlineRecogniser` - reads in the calling thread, one sheet per
      poll; deterministic, for a single-worker run and for tests (an injected
      ``recognise`` function can count, slow down or fail calls).

What does NOT belong here:
    * The database. Workers return :class:`ScanResult` objects and nothing
      else; the engine's coordinator thread is the only writer (X3).
    * Output names or copies (the engine produces none, ARCHITECTURE_NOTES
      §11), progress semantics, claims.
    * Qt.

Bounds:
    The engine never submits more than its ``max_in_flight`` sheets, so a pool
    holds at most that many futures, each carrying a ticket and a path - never
    an image. Images are decoded inside the worker and released there.

Worker death:
    A worker process that dies outright (a native crash, the operating system
    killing it) breaks the whole ``ProcessPoolExecutor``: every outstanding
    future fails. Each comes back as a :class:`RecognitionDone` with
    ``worker_lost=True`` - an infrastructure fault, not a fact about the
    sheet - and the next submission starts a fresh pool. What to do with such
    a sheet is the engine's decision (it retries it once, then records the
    failure as the finite path always has).
"""

from __future__ import annotations

import logging
import multiprocessing
import time
import traceback
from collections import deque
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from omr_scanner.services.parallel_batch import (
    START_METHOD,
    WorkerOutcome,
    worker_initialise,
    worker_recognise,
)
from omr_scanner.services.process_containment import (
    ensure_worker_processes_die_with_this_one,
)
from omr_scanner.services.recognition_service import (
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    recognise_scan,
)
from omr_scanner.services.recognition_settings import RecognitionOptions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecognitionDone:
    """One finished ticket.

    Attributes:
        ticket: What the engine submitted it as (the scan id).
        result: The recognition result - always present; recognition reports
            its own failures as results.
        error: Traceback text when recognition raised unexpectedly, else ``""``.
        worker_lost: The worker *process* died while this sheet was
            outstanding; ``result`` is a placeholder failure, not a reading.
    """

    ticket: int
    result: ScanResult
    error: str = ""
    worker_lost: bool = False


class Recogniser(Protocol):
    """Where the engine's sheets are read. Used from one thread only."""

    @property
    def outstanding(self) -> int:
        """Tickets submitted and not yet returned by :meth:`poll` or dropped."""
        ...

    @property
    def accepting(self) -> bool:
        """Whether :meth:`submit` may be called now (``False`` while recycling)."""
        ...

    def submit(self, ticket: int, path: Path) -> None:
        """Queue one sheet."""
        ...

    def poll(self, timeout: float) -> list[RecognitionDone]:
        """Return finished tickets, waiting up to ``timeout`` seconds for the first."""
        ...

    def cancel_queued(self) -> list[int]:
        """Withdraw every ticket not yet started; return them."""
        ...

    def close(self) -> list[int]:
        """Stop, returning every ticket whose result will never be delivered.

        Withdraws what has not started and lets running sheets end, discarding
        their results. Idempotent.
        """
        ...


def error_result(path: Path, message: str, elapsed: float = 0.0) -> ScanResult:
    """The result that stands for "this file could not be processed" (as the pool's)."""
    return ScanResult(
        source_path=path,
        outcome=RecognitionOutcome.ERROR,
        registration=RegistrationStatus.FAILED,
        registration_message=message,
        elapsed_seconds=elapsed,
    )


def _default_recognise(
    path: Path, template: OmrTemplate, options: RecognitionOptions
) -> ScanResult:
    """What a worker process does with one sheet, in this thread."""
    return recognise_scan(path, template, options=options.with_preview_disabled())


class InlineRecogniser:
    """Reads sheets in the calling thread, first in, first out, ``per_poll`` per poll.

    Args:
        template: The template every sheet is read with.
        options: Engine options (previews are always dropped, as in a worker).
        recognise: ``(path, template, options) -> ScanResult``; the real
            recogniser by default. Tests inject one that counts invocations,
            replays stored results, sleeps or raises.
        per_poll: Sheets read per :meth:`poll` call.
    """

    def __init__(
        self,
        template: OmrTemplate,
        *,
        options: RecognitionOptions | None = None,
        recognise: Callable[[Path, OmrTemplate, RecognitionOptions], ScanResult] | None = None,
        per_poll: int = 1,
    ) -> None:
        self._template = template
        self._options = options if options is not None else RecognitionOptions()
        self._recognise = recognise if recognise is not None else _default_recognise
        self._per_poll = max(1, per_poll)
        self._queue: deque[tuple[int, Path]] = deque()
        self._closed = False

    @property
    def outstanding(self) -> int:
        """Tickets queued and not yet read."""
        return len(self._queue)

    @property
    def accepting(self) -> bool:
        """Whether :meth:`submit` may be called (until :meth:`close`)."""
        return not self._closed

    def submit(self, ticket: int, path: Path) -> None:
        """Queue one sheet."""
        if self._closed:
            raise RuntimeError("recogniser is closed")
        self._queue.append((ticket, path))

    def poll(self, timeout: float) -> list[RecognitionDone]:  # noqa: ARG002 - never blocks
        """Read up to ``per_poll`` queued sheets now, in submission order."""
        done: list[RecognitionDone] = []
        while self._queue and len(done) < self._per_poll:
            ticket, path = self._queue.popleft()
            started = time.perf_counter()
            try:
                result = self._recognise(path, self._template, self._options)
            except Exception as exc:
                done.append(
                    RecognitionDone(
                        ticket=ticket,
                        result=error_result(
                            path,
                            f"An unexpected error occurred: {exc}",
                            time.perf_counter() - started,
                        ),
                        error=traceback.format_exc(),
                    )
                )
                continue
            done.append(RecognitionDone(ticket=ticket, result=result))
        return done

    def cancel_queued(self) -> list[int]:
        """Withdraw every queued sheet (none has started); return their tickets."""
        dropped = [ticket for ticket, _path in self._queue]
        self._queue.clear()
        return dropped

    def close(self) -> list[int]:
        """Refuse further work; return the tickets withdrawn."""
        self._closed = True
        return self.cancel_queued()


class ProcessRecogniser:
    """A warm ``spawn`` pool of worker processes, kept across finite units.

    Args:
        template: Sent to each worker once, when a pool starts.
        workers: Worker processes.
        options: Engine options, sent once per pool.
        opencv_threads: OpenCV threads inside each worker (as the finite path).
        recycle_after: Sheets per worker before the pool is replaced (``0``:
            never). Recycling drains: no new sheet is accepted until every
            outstanding one has returned, then the next submission starts a
            fresh pool - the same safe pattern the finite path uses instead of
            ``max_tasks_per_child`` (see :mod:`~omr_scanner.services.parallel_batch`).
    """

    def __init__(
        self,
        template: OmrTemplate,
        *,
        workers: int,
        options: RecognitionOptions | None = None,
        opencv_threads: int = 1,
        recycle_after: int = 0,
    ) -> None:
        self._template = template
        self._workers = max(1, workers)
        self._options = options if options is not None else RecognitionOptions()
        self._opencv_threads = opencv_threads
        self._recycle_after = max(0, recycle_after)
        self._executor: ProcessPoolExecutor | None = None
        self._pending: dict[Future[WorkerOutcome], tuple[int, Path]] = {}
        self._submitted_to_pool = 0
        self._pools_started = 0
        self._closed = False

    @property
    def workers(self) -> int:
        """Worker processes per pool."""
        return self._workers

    @property
    def pools_started(self) -> int:
        """How many pools have been started (1 for a warm run without recycling)."""
        return self._pools_started

    @property
    def outstanding(self) -> int:
        """Futures not yet returned by :meth:`poll` or withdrawn."""
        return len(self._pending)

    @property
    def _recycle_due(self) -> bool:
        return bool(self._recycle_after) and (
            self._submitted_to_pool >= self._recycle_after * self._workers
        )

    @property
    def accepting(self) -> bool:
        """``False`` once closed, and while a recycle waits for the old pool to empty."""
        if self._closed:
            return False
        # Draining for a recycle: the old pool must empty first.
        return not (self._recycle_due and self._pending)

    def _pool(self) -> ProcessPoolExecutor:
        if self._executor is not None and self._recycle_due and not self._pending:
            self._executor.shutdown(wait=True)
            self._executor = None
        if self._executor is None:
            ensure_worker_processes_die_with_this_one()
            self._executor = ProcessPoolExecutor(
                max_workers=self._workers,
                mp_context=multiprocessing.get_context(START_METHOD),
                initializer=worker_initialise,
                initargs=(self._template, self._options, self._opencv_threads),
            )
            self._submitted_to_pool = 0
            self._pools_started += 1
            _LOGGER.info("Recognition pool started: %d worker(s)", self._workers)
        return self._executor

    def submit(self, ticket: int, path: Path) -> None:
        """Hand one sheet to the pool (starting one if needed)."""
        if not self.accepting:
            raise RuntimeError("recogniser is not accepting work")
        future = self._pool().submit(worker_recognise, ticket, path)
        self._pending[future] = (ticket, path)
        self._submitted_to_pool += 1

    def poll(self, timeout: float) -> list[RecognitionDone]:
        """Return the sheets finished so far, waiting up to ``timeout`` for the first."""
        if not self._pending:
            return []
        finished, _ = wait(
            list(self._pending), timeout=max(0.0, timeout), return_when=FIRST_COMPLETED
        )
        done: list[RecognitionDone] = []
        broken = False
        for future in finished:
            ticket, path = self._pending.pop(future)
            try:
                outcome = future.result()
            except (CancelledError, Exception) as exc:
                # Our own cancellations leave `_pending` in `cancel_queued`, so
                # a cancelled future here was dropped by a pool that broke.
                broken = True
                _LOGGER.error("Worker process lost while reading %s: %s", path.name, exc)
                done.append(
                    RecognitionDone(
                        ticket=ticket,
                        result=error_result(
                            path, f"The worker process failed while reading this scan: {exc}"
                        ),
                        error=traceback.format_exc(),
                        worker_lost=True,
                    )
                )
                continue
            if outcome.error:
                _LOGGER.error("Recognition of %s failed in a worker:\n%s", path.name, outcome.error)
            done.append(RecognitionDone(ticket=ticket, result=outcome.result, error=outcome.error))
        if broken and self._executor is not None:
            # A broken pool fails every future it holds; they are returned by
            # the following polls. Nothing more can be submitted to it.
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        done.sort(key=lambda item: item.ticket)
        return done

    def cancel_queued(self) -> list[int]:
        """Withdraw every future the pool has not started; return their tickets."""
        dropped: list[int] = []
        for future, (ticket, _path) in list(self._pending.items()):
            if future.cancel():
                dropped.append(ticket)
                del self._pending[future]
        return sorted(dropped)

    def close(self) -> list[int]:
        """Shut the pool down; return every ticket whose result is discarded."""
        if self._closed and self._executor is None:
            return []
        self._closed = True
        dropped = self.cancel_queued()
        if self._executor is not None:
            # Running sheets finish (OpenCV cannot be interrupted mid-warp);
            # their results are discarded, the engine releases their claims.
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None
        dropped += [ticket for ticket, _path in self._pending.values()]
        self._pending.clear()
        return sorted(dropped)


__all__ = [
    "InlineRecogniser",
    "ProcessRecogniser",
    "Recogniser",
    "RecognitionDone",
    "error_result",
]
