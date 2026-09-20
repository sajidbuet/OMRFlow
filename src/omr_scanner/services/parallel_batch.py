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

QUEUE_DEPTH_PER_WORKER = 4
"""How many tasks may be outstanding per worker before submission pauses.

Backpressure, and the reason a ten-thousand-sheet batch behaves like a
hundred-sheet one. Submitting every task up front would build ten thousand
`Future` objects and ten thousand queued messages before the first page was
read, which costs memory, delays the first result, and makes cancellation
slower because every one of those futures has to be cancelled individually.

Four per worker is deep enough that no worker ever waits for the parent to
hand it the next page - by the time it finishes one, three more are already
queued for it - and shallow enough that "stop" means stopping within a page or
two. The jobs themselves stay tiny either way: an index and a path, never an
image (see the module docstring)."""

_WORKER_TEMPLATE: OmrTemplate | None = None
_WORKER_OPTIONS: RecognitionOptions | None = None
"""Per-process state, set once by :func:`worker_initialise`.

The template and the engine options are sent to each worker exactly once when
the pool starts, not once per sheet: they are the same for every page of a
batch, and re-sending (and re-validating) them a thousand times would cost more
than some of the pages."""

DEFAULT_MAX_TASKS_PER_CHILD: int | None = None
"""Sheets processed, across the whole pool, before every worker is replaced
(Phase 10, §16). ``None`` means a worker lives for the whole run, which is
what every caller before this phase already did.

**Deliberately not implemented via** :class:`~concurrent.futures.ProcessPoolExecutor`'s
own ``max_tasks_per_child`` constructor argument, despite that being the
obvious first choice. During this phase's own testing, a minimal
reproduction with no OMRFlow code involved at all - just
``ProcessPoolExecutor(max_workers=2, mp_context=spawn, initializer=...,
max_tasks_per_child=2)`` submitting six trivial tasks - hung permanently
after exactly four of the six completed, on this project's supported
platform (Python 3.12.7, Windows, the ``spawn`` start method this module
already requires). The pool's automatic worker-replacement logic did not
resubmit the two tasks still queued once both original workers had reached
their limit. Given that finding, shipping the stdlib parameter as "worker
recycling" would mean a real examination batch could hang indefinitely the
first time enough sheets crossed a recycle boundary - exactly the failure
mode this whole phase exists to prevent, and worse than having no recycling
at all. :func:`recognise_in_parallel` instead recycles by running a fresh,
short-lived pool per bounded *slice* of the batch (see
:func:`_recognise_batch_in_parallel`): provably safe, at the cost of a full
drain (every in-flight sheet in a slice finishes) at each recycle boundary
rather than a seamless mid-stream worker swap. See
``development/PHASE_10_HANDOFF.md`` for the reproduction."""


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
    template: OmrTemplate,
    options: RecognitionOptions | None = None,
    opencv_threads: int = 1,
) -> None:
    """Prepare one worker process. Runs once per process, in that process.

    Args:
        template: The template every task in this batch is read with.
        options: Engine options, or ``None`` for the defaults. Immutable, so
            every worker reads with exactly the settings the parent chose and
            no worker can change another's.
        opencv_threads: OpenCV's own internal thread count for this process
            (Phase 10, §18). Defaults to one, for the reason the module
            docstring gives: the processes are the parallelism.
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

    _limit_worker_threads(opencv_threads)


def _limit_worker_threads(opencv_threads: int = 1) -> None:
    """Cap this worker's OpenCV thread pool; the processes are the parallelism."""
    try:
        import cv2

        cv2.setNumThreads(max(1, opencv_threads))
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
    opencv_threads: int = 1,
    max_tasks_per_child: int | None = DEFAULT_MAX_TASKS_PER_CHILD,
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
        opencv_threads: OpenCV's own internal thread count inside each worker
            process (Phase 10, §18). See
            :attr:`omr_scanner.config.processing.ProcessingSettings.opencv_threads`.
        max_tasks_per_child: Sheets a worker process reads, in total across
            the whole pool, before every worker is replaced with a fresh one
            (Phase 10, §16), or ``None`` to let workers live for the whole
            run. See
            :attr:`omr_scanner.config.processing.ProcessingSettings.worker_recycle_after`
            **and the important caveat in the module docstring** about why
            this is implemented as a pool restart between bounded batches
            rather than via :class:`~concurrent.futures.ProcessPoolExecutor`'s
            own ``max_tasks_per_child`` parameter.

    Yields:
        ``(index, result)`` in **completion** order within each recycle batch
        (not overall submission order - restoring that is the caller's job,
        as ever), and batches strictly in submission order relative to each
        other.
    """
    if not paths:
        return

    total = len(paths)
    batch_size = total if not max_tasks_per_child else max(max_tasks_per_child * workers, workers)
    started = time.perf_counter()
    delivered = 0
    cancelled = False

    for batch_start in range(0, total, batch_size):
        batch_paths = paths[batch_start : batch_start + batch_size]
        for local_index, result in _recognise_batch_in_parallel(
            batch_paths,
            template,
            workers=workers,
            options=options,
            should_cancel=(None if cancelled else should_cancel),
            opencv_threads=opencv_threads,
        ):
            yield batch_start + local_index, result
            delivered += 1
        if should_cancel is not None and should_cancel():
            cancelled = True
        if cancelled:
            break

    _LOGGER.info(
        "Parallel recognition finished: %d of %d scan(s) on %d worker(s) in %.2fs",
        delivered,
        total,
        workers,
        time.perf_counter() - started,
    )


def _recognise_batch_in_parallel(
    paths: Sequence[Path],
    template: OmrTemplate,
    *,
    workers: int,
    options: RecognitionOptions | None,
    should_cancel: Callable[[], bool] | None,
    opencv_threads: int,
) -> Generator[tuple[int, ScanResult], None, None]:
    """Run one pool, start to finish, over one bounded slice of the batch.

    Everything :func:`recognise_in_parallel` used to do directly, unchanged -
    this is that same bounded-submission loop, extracted so a fresh pool can
    be started for each recycle-sized slice of a larger run. Indices yielded
    are local to ``paths``; the caller offsets them back to the whole batch.
    """
    if not paths:
        return

    context = multiprocessing.get_context(START_METHOD)
    pending: dict[Future[WorkerOutcome], int] = {}

    executor = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=context,
        initializer=worker_initialise,
        initargs=(template, options, opencv_threads),
    )
    queue_depth = max(workers * QUEUE_DEPTH_PER_WORKER, workers)
    submitted = 0
    cancelled = False
    try:
        while True:
            # Top the queue back up before waiting. Submission is bounded
            # (`QUEUE_DEPTH_PER_WORKER`) so a ten-thousand-sheet batch never
            # builds ten thousand futures; the pool stays fed because the
            # refill happens before every wait, not after every completion.
            if not cancelled:
                while submitted < len(paths) and len(pending) < queue_depth:
                    future = executor.submit(worker_recognise, submitted, paths[submitted])
                    pending[future] = submitted
                    submitted += 1

            if not pending:
                break

            if not cancelled and should_cancel is not None and should_cancel():
                cancelled = True
                _LOGGER.info(
                    "Cancelling: %d scan(s) in flight, %d never submitted",
                    len(pending),
                    len(paths) - submitted,
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
    finally:
        # `cancel_futures` drops anything still queued; the wait then lets the
        # sheets already inside a worker finish, so no process is killed
        # mid-write and none is left behind when the pool closes.
        executor.shutdown(wait=True, cancel_futures=True)


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
    "QUEUE_DEPTH_PER_WORKER",
    "START_METHOD",
    "WorkerOutcome",
    "describe_environment",
    "recognise_in_parallel",
    "worker_initialise",
    "worker_recognise",
]
