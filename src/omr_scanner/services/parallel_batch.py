"""Reading many sheets at once, on several CPU cores.

Purpose:
    Spread :func:`~omr_scanner.services.recognition_service.recognise_scan` over
    a pool of worker *processes*, and hand each finished sheet back to the
    caller as soon as it is done - without the caller having to know anything
    about multiprocessing.

Responsibilities:
    * :func:`recognise_in_parallel` - the whole pool lifecycle, as a generator of
      ``(index, ScanResult)`` in **completion** order.
    * :func:`worker_initialise` / :func:`worker_recognise` - what runs inside a
      worker process. Module-level functions on purpose: anything handed to a
      process pool has to be importable by name in the child.

What does NOT belong here:
    * Output names, copying, CSV rows, progress semantics or cancellation
      *policy*. Those stay in :mod:`omr_scanner.services.batch_processor`, in
      the parent process, which is what makes duplicate roll numbers safe (see
      "Why the workers never name a file" below).
    * Qt. A worker process has no event loop, no widgets and no signals.

The unit of parallelism is one page:
    Load, register, measure, interpret - the whole pipeline for one scan - runs
    inside one worker. Nothing is shared and nothing is mutated across
    processes, so a page read on core 5 of a 16-core machine produces exactly
    the bytes it would have produced single-core.

Why the workers never name a file:
    Two sheets can legitimately recognise to the same roll number. If two
    workers each decided their own output name, both would pick ``2103123.jpg``
    and one would silently overwrite the other - a lost script, which is the
    worst failure this application has. Workers therefore return *recognition
    results only*; the parent assigns every output name from a single allocator,
    in batch order.

Process start method:
    ``spawn``, explicitly, on every platform. Forking a process that already has
    a Qt event loop, OpenCV thread pools and open file handles is unsafe, and
    relying on the Linux default would mean the code path tested on a developer
    machine is not the one that runs on Windows. Spawn re-imports the package in
    the child, which is why every entry point in this repository is guarded by
    ``if __name__ == "__main__":`` - without that, starting a worker would start
    a second GUI.

Threads inside workers:
    OpenCV runs its own thread pool. With N worker processes each spawning
    N threads the machine spends its time in the scheduler, so each worker pins
    OpenCV to one thread and the parallelism comes from the processes.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.services.recognition_service import (
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    recognise_scan,
)
from omr_scanner.services.recognition_settings import RecognitionOptions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Generator, Sequence

    from omr_scanner.domain.template import OmrTemplate

_LOGGER = logging.getLogger(__name__)

START_METHOD = "spawn"
"""Multiprocessing start method used for every worker pool. See the module
docstring for why it is not left to the platform default."""

CANCEL_POLL_SECONDS = 0.2
"""How often a busy pool re-checks for a cancellation request.

Short enough that "Cancel" feels immediate, long enough that waiting costs
nothing measurable next to reading a page."""

_WORKER_TEMPLATE: OmrTemplate | None = None
_WORKER_OPTIONS: RecognitionOptions | None = None
"""Per-process state, set once by :func:`worker_initialise`.

The template and the engine options are sent to each worker exactly once when
the pool starts, not once per sheet: they are the same for every page of a
batch, and re-sending (and re-validating) them a thousand times would cost more
than some of the pages."""


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """What one worker process sends back for one scan.

    Attributes:
        index: The scan's position in the batch, so the parent can restore the
            submitted order whatever order the workers finish in.
        result: The recognition result. Always present - recognition reports its
            own failures as a result rather than by raising.
        error: Traceback text when something failed in a way recognition itself
            did not expect, otherwise ``""``. Logged by the parent, never shown
            to the user as-is.
    """

    index: int
    result: ScanResult
    error: str = ""


def worker_initialise(
    template: OmrTemplate, options: RecognitionOptions | None = None
) -> None:
    """Prepare one worker process. Runs once per process, in that process.

    Args:
        template: The template every task in this batch is read with.
        options: Engine options, or ``None`` for the defaults. Immutable, so
            every worker reads with exactly the settings the parent chose and
            no worker can change another's.
    """
    global _WORKER_TEMPLATE, _WORKER_OPTIONS
    _WORKER_TEMPLATE = template
    _WORKER_OPTIONS = options if options is not None else RecognitionOptions()

    # A worker has no log configuration of its own, so anything it logged would
    # reach the console through logging's "last resort" handler - interleaved
    # with other workers and out of order. The parent re-logs what matters
    # (failures, timings) from the results it receives.
    logging.getLogger("omr_scanner").addHandler(logging.NullHandler())
    logging.getLogger("omr_scanner").propagate = False

    _limit_worker_threads()


def _limit_worker_threads() -> None:
    """Keep each worker to one OpenCV thread; the processes are the parallelism."""
    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception:  # pragma: no cover - OpenCV always present in practice
        pass


def worker_recognise(index: int, path: Path) -> WorkerOutcome:
    """Read one scan inside a worker process.

    Args:
        index: The scan's position in the batch.
        path: The image to read.

    Returns:
        The outcome. Never raises: an exception here would poison the pool and
        take the rest of the batch with it, so every failure comes back as an
        ``ERROR`` result with its traceback attached.
    """
    started = time.perf_counter()
    if _WORKER_TEMPLATE is None:  # pragma: no cover - guarded by the initialiser
        return WorkerOutcome(
            index=index,
            result=_error_result(path, "The worker process was not initialised."),
            error="worker not initialised",
        )

    try:
        options = _WORKER_OPTIONS if _WORKER_OPTIONS is not None else RecognitionOptions()
        # A preview is a picture for a screen this process does not have, and
        # pickling one back to the parent would dominate the transfer cost.
        result = recognise_scan(path, _WORKER_TEMPLATE, options=options.with_preview_disabled())
    except Exception as exc:
        return WorkerOutcome(
            index=index,
            result=_error_result(
                path,
                f"An unexpected error occurred: {exc}",
                elapsed=time.perf_counter() - started,
            ),
            error=traceback.format_exc(),
        )
    return WorkerOutcome(index=index, result=result)


def _error_result(path: Path, message: str, elapsed: float = 0.0) -> ScanResult:
    """Return the result that stands for "this file could not be processed"."""
    return ScanResult(
        source_path=path,
        outcome=RecognitionOutcome.ERROR,
        registration=RegistrationStatus.FAILED,
        registration_message=message,
        elapsed_seconds=elapsed,
    )


def recognise_in_parallel(
    paths: Sequence[Path],
    template: OmrTemplate,
    *,
    workers: int,
    options: RecognitionOptions | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Generator[tuple[int, ScanResult], None, None]:
    """Recognise every scan in ``paths`` across ``workers`` processes.

    Args:
        paths: The images, in batch order. The index yielded alongside each
            result is that order's index.
        template: The template to read them with. Sent to each worker once.
        workers: How many worker processes to start. Callers get this number
            from
            :meth:`omr_scanner.config.processing.ProcessingSettings.resolve_worker_count`,
            which has already capped it to the batch size.
        options: Engine options, or ``None`` for the defaults. Sent to each
            worker once, when the pool starts.
        should_cancel: Polled as results arrive. When it returns ``True``, work
            not yet started is cancelled, the pool is shut down, and iteration
            stops - sheets already inside a worker are allowed to finish, since
            OpenCV cannot be interrupted part-way through a warp.

    Yields:
        ``(index, result)`` in **completion** order, which on a multicore run is
        not submission order. Restoring the submitted order is the caller's job
        and is what keeps CSV rows and duplicate-name suffixes deterministic.
    """
    if not paths:
        return

    context = multiprocessing.get_context(START_METHOD)
    started = time.perf_counter()
    pending: dict[Future[WorkerOutcome], int] = {}
    delivered = 0

    executor = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=context,
        initializer=worker_initialise,
        initargs=(template, options),
    )
    try:
        for index, path in enumerate(paths):
            pending[executor.submit(worker_recognise, index, path)] = index

        while pending:
            if should_cancel is not None and should_cancel():
                _LOGGER.info(
                    "Cancelling %d scan(s) that had not started yet", len(pending)
                )
                for future in pending:
                    future.cancel()
                break

            done, _not_done = wait(
                list(pending),
                timeout=CANCEL_POLL_SECONDS,
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                index = pending.pop(future)
                yield index, _outcome_of(future, index, paths[index]).result
                delivered += 1
    finally:
        # `cancel_futures` drops anything still queued; the wait then lets the
        # sheets already inside a worker finish, so no process is killed
        # mid-write and none is left behind when the pool closes.
        executor.shutdown(wait=True, cancel_futures=True)
        _LOGGER.info(
            "Parallel recognition finished: %d of %d scan(s) on %d worker(s) in %.2fs",
            delivered,
            len(paths),
            workers,
            time.perf_counter() - started,
        )


def _outcome_of(
    future: Future[WorkerOutcome], index: int, path: Path
) -> WorkerOutcome:
    """Turn a finished future into an outcome, whatever became of its process.

    A worker that died outright (a segmentation fault in a native library, the
    operating system killing it for memory) fails its future rather than
    returning; that has to become one failed *scan*, not a failed batch.
    """
    try:
        outcome = future.result()
    except Exception as exc:
        _LOGGER.exception("Worker process failed while reading %s", path.name)
        return WorkerOutcome(
            index=index,
            result=_error_result(
                path, f"The worker process failed while reading this scan: {exc}"
            ),
            error=traceback.format_exc(),
        )

    if outcome.error:
        _LOGGER.error("Recognition of %s failed in a worker:\n%s", path.name, outcome.error)
    return outcome


def describe_environment() -> str:
    """Return a one-line description of the machine, for the diagnostic log."""
    return f"{os.cpu_count() or 1} logical CPU(s), start method '{START_METHOD}'"


__all__ = [
    "START_METHOD",
    "WorkerOutcome",
    "describe_environment",
    "recognise_in_parallel",
    "worker_initialise",
    "worker_recognise",
]
