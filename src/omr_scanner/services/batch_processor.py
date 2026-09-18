"""Processing many scans without letting one bad file stop the rest.

Purpose:
    Run :func:`~omr_scanner.services.recognition_service.recognise_scan` over a
    list of files, decide each one's output name, optionally copy it there, and
    report progress - all without Qt, so the same code runs from a worker
    thread, a test or a future command line tool.

Responsibilities:
    * :func:`process_scan` - one file: recognise, name, copy.
    * :func:`finalise_scan` - name and copy a sheet that has *already* been
      recognised, which is what a multicore run does in the parent process.
    * :func:`process_batch` - many files, with progress, cancellation,
      per-file error isolation and an optional pool of worker processes.
    * :class:`BatchOptions` - what the user chose in the Scan page.

What does NOT belong here:
    * Threads. The caller decides whether this runs on one; the only
      concurrency contract is that ``on_progress``/``on_result`` are called from
      whichever thread is running the batch, and must therefore not touch Qt
      widgets directly.
    * The mechanics of the worker pool, which are
      :mod:`omr_scanner.services.parallel_batch`.
    * Naming rules, which are :mod:`omr_scanner.services.filename_manager`, and
      recognition, which is
      :mod:`omr_scanner.services.recognition_service`.

Multicore processing, and what stays single-threaded:
    ``workers > 1`` reads several *pages* at once, one page per worker process.
    Everything that touches shared state stays in this process and in batch
    order: the file-name allocator, the copy into the output folder, the results
    list and therefore the CSV. Recognition is pure - same image, same template,
    same answer - so a batch read on eight cores produces exactly the results
    and exactly the file names it would have produced on one. See
    ``docs/scan_workflow.md`` for the diagram.

Error isolation:
    Every per-file failure - unreadable image, failed registration, a bug - is
    caught and recorded against that file. A batch of two hundred sheets with
    one corrupted JPEG must produce one hundred and ninety-nine results and one
    error, never an aborted run.

Copying, not moving:
    The default output mode copies the recognised sheet into the output folder
    under its new name and leaves the original untouched. Renaming originals in
    place is destructive, unrecoverable if the recognition was wrong, and is
    deliberately not implemented in Phase 3.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.imaging.metrics import BubbleMetricsConfig
from omr_scanner.services.filename_manager import FilenameAllocator
from omr_scanner.services.parallel_batch import recognise_in_parallel
from omr_scanner.services.recognition_service import (
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    recognise_scan,
)
from omr_scanner.services.recognition_settings import RecognitionOptions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from omr_scanner.domain.template import OmrTemplate

_LOGGER = logging.getLogger(__name__)


class BatchStage(StrEnum):
    """Which part of one file's processing a progress report refers to."""

    STARTED = "started"
    RECOGNISED = "recognised"
    COPIED = "copied"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class BatchOptions:
    """What the user chose for this run.

    Attributes:
        output_dir: Where renamed copies are written. ``None`` means "do not
            write anything", which is also what happens when
            ``rename_with_identifier`` is off.
        rename_with_identifier: Copy each recognised sheet into ``output_dir``
            named after its identifier (the roll number). Off by default:
            recognition is useful on its own, and writing files is a side
            effect a user should opt into.
        with_preview: Keep a display image on every result. Off for a batch -
            only the selected scan is ever shown, and a hundred rectified pages
            is a gigabyte held for nothing.
        metrics_config: Bubble sampling tuning, or ``None`` for the defaults.
        recognition: Full engine options - diagnostics, what evidence to keep,
            everything :class:`~omr_scanner.services.recognition_settings.RecognitionOptions`
            covers. When given it supersedes ``metrics_config`` and
            ``with_preview``, which remain for the callers (and tests) that
            only ever needed those two.
    """

    output_dir: Path | None = None
    rename_with_identifier: bool = False
    with_preview: bool = False
    metrics_config: BubbleMetricsConfig | None = None
    recognition: RecognitionOptions | None = None

    @property
    def writes_files(self) -> bool:
        """Whether this run will copy anything to disk."""
        return self.rename_with_identifier and self.output_dir is not None

    def engine_options(self) -> RecognitionOptions:
        """Return the engine options this run implies.

        One place decides how the two spellings combine, so the sequential path
        and the worker pool cannot disagree about what the user asked for.
        """
        if self.recognition is not None:
            return self.recognition
        return RecognitionOptions(
            metrics=self.metrics_config
            if self.metrics_config is not None
            else BubbleMetricsConfig(),
            with_preview=self.with_preview,
        )


@dataclass(frozen=True, slots=True)
class ProcessedScan:
    """One file's outcome: what was read, what it will be called, what happened.

    Attributes:
        result: The recognition result, including its failure states.
        output_name: The file name chosen for this scan, or ``""`` when naming
            was not requested. Present even when nothing was written, so the
            scan list can show a rename *preview* before the user commits.
        output_path: Where the copy was written, or ``None``.
        copied: Whether a file was actually written.
        message: Plain-language note for the scan list - why nothing was
            written, or why the file failed.
    """

    result: ScanResult
    output_name: str = ""
    output_path: Path | None = None
    copied: bool = False
    message: str = ""

    @property
    def source_path(self) -> Path:
        """The image this result came from."""
        return self.result.source_path

    @property
    def outcome(self) -> RecognitionOutcome:
        """The recognition outcome, for the scan list's status column."""
        return self.result.outcome


@dataclass(frozen=True, slots=True)
class BatchProgress:
    """One progress report during a batch.

    Attributes:
        index: Zero-based position of the file in the batch.
        total: How many files the batch contains.
        path: The file being worked on.
        stage: What just happened to it.
        completed_count: How many files have finished, when that is not simply
            ``index + 1``. On a multicore run the sheets finish out of order, so
            a progress bar must count completions rather than trust the position
            of whichever sheet happened to finish last.
        outcome: The scan's :class:`RecognitionOutcome` value on a terminal
            event, or ``""``. Carried on the progress event - and not only on
            the result - so that a progress display can count successes,
            reviews and failures *as they finish*, which on a multicore run is
            earlier than the results are released in batch order.
    """

    index: int
    total: int
    path: Path
    stage: BatchStage
    completed_count: int | None = None
    outcome: str = ""

    @property
    def completed(self) -> int:
        """How many files have finished, for a progress bar."""
        if self.completed_count is not None:
            return self.completed_count
        return self.index + 1 if self.stage is not BatchStage.STARTED else self.index


@dataclass(frozen=True, slots=True)
class BatchReport:
    """Summary of a finished (or cancelled) batch.

    Attributes:
        processed: Every file's outcome, in batch order - never in the order the
            workers happened to finish.
        cancelled: Whether the run stopped early at the caller's request.
        worker_count: How many worker processes actually read sheets. ``1``
            means the batch ran in the calling thread with no pool at all.
        elapsed_seconds: Wall-clock duration of the whole run, for the
            diagnostic log and the benchmark script.
    """

    processed: tuple[ProcessedScan, ...] = ()
    cancelled: bool = False
    worker_count: int = 1
    elapsed_seconds: float = 0.0

    @property
    def total(self) -> int:
        """How many files were processed."""
        return len(self.processed)

    @property
    def complete_count(self) -> int:
        """How many sheets were read with nothing needing review."""
        return sum(
            item.outcome is RecognitionOutcome.COMPLETE for item in self.processed
        )

    @property
    def review_count(self) -> int:
        """How many sheets were read but carry something for a human."""
        return sum(item.outcome is RecognitionOutcome.REVIEW for item in self.processed)

    @property
    def failed_count(self) -> int:
        """How many sheets could not be read or registered."""
        return sum(
            item.outcome
            in (RecognitionOutcome.REGISTRATION_FAILED, RecognitionOutcome.ERROR)
            for item in self.processed
        )

    @property
    def written_count(self) -> int:
        """How many files were copied to the output folder."""
        return sum(item.copied for item in self.processed)


def plan_output_name(result: ScanResult, allocator: FilenameAllocator) -> tuple[str, str]:
    """Choose the output file name for one recognised scan.

    Args:
        result: The recognition result.
        allocator: The batch's allocator, which guarantees uniqueness against
            both this run and the output folder's existing contents.

    Returns:
        ``(name, message)``. ``message`` explains a fallback name and is empty
        when the identifier was used as-is.

    Naming rules:
        * Identifier resolved  -> ``<identifier><ext>``, then ``_a``, ``_b`` ...
          for each later sheet claiming the same identifier, and for a name that
          already exists in the output folder.
        * Identifier not resolved, or the template has no identifier field ->
          ``UNRESOLVED_001<ext>``. A file is never named after a value the
          engine itself does not trust.
    """
    suffix = result.source_path.suffix
    if result.identifier_is_reliable:
        name = allocator.allocate(result.identifier_value, suffix)
        return name, ""

    name = allocator.allocate(None, suffix)
    if result.registration is RegistrationStatus.FAILED:
        reason = "The sheet could not be registered, so it was not renamed."
    elif result.identifier is None:
        reason = "The template defines no identifier field, so the scan was not renamed."
    else:
        reason = (
            "The identifier could not be determined reliably "
            f"(read as '{result.identifier_value}'), so the scan was not renamed."
        )
    return name, reason


def _copy_to_output(source: Path, destination: Path) -> None:
    """Copy ``source`` to ``destination``, refusing to replace anything.

    The allocator has already guaranteed the name is free; this check closes the
    remaining gap - another process writing the same name in between - rather
    than trusting it.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing file '{destination}'")
    shutil.copy2(source, destination)


def process_scan(
    path: Path,
    template: OmrTemplate,
    *,
    options: BatchOptions,
    allocator: FilenameAllocator,
) -> ProcessedScan:
    """Recognise one scan and, if asked, copy it under its new name.

    Args:
        path: The image to process.
        template: The template to read it with.
        options: What the user chose.
        allocator: Supplies collision-free output names.

    Returns:
        The outcome. Never raises for a bad file: recognition reports its own
        failures, and a copy failure is recorded as a message on the result.
    """
    result = recognise_scan(path, template, options=options.engine_options())
    return finalise_scan(result, options=options, allocator=allocator)


def finalise_scan(
    result: ScanResult,
    *,
    options: BatchOptions,
    allocator: FilenameAllocator,
) -> ProcessedScan:
    """Name and, if asked, copy one already-recognised scan.

    Args:
        result: What recognition read from the sheet. It may have been produced
            in this process or returned by a worker process; the decision made
            here is identical either way.
        options: What the user chose.
        allocator: Supplies collision-free output names.

    Returns:
        The outcome. Never raises: a copy failure is recorded as a message on
        the result rather than ending the batch.

    Why this is separate from :func:`process_scan`:
        On a multicore run, recognition happens in a worker process and this
        step does not. Naming and copying are the only parts of the pipeline
        that touch state shared by the whole batch - the allocator and the
        output directory - so they stay in the parent process, called one scan
        at a time in batch order. Two workers can never both decide to write
        ``2103123.jpg``, because no worker decides a name at all.
    """
    if not options.rename_with_identifier:
        return ProcessedScan(result=result)

    path = result.source_path
    name, message = plan_output_name(result, allocator)
    if options.output_dir is None:
        return ProcessedScan(result=result, output_name=name, message=message)

    destination = options.output_dir / name
    try:
        _copy_to_output(path, destination)
    except (OSError, FileExistsError) as exc:
        _LOGGER.warning("Could not copy %s to %s: %s", path.name, destination, exc)
        note = f"The scan could not be copied to the output folder: {exc}"
        return ProcessedScan(
            result=result,
            output_name=name,
            message=f"{message} {note}".strip(),
        )

    _LOGGER.info("Copied %s to %s", path.name, destination.name)
    return ProcessedScan(
        result=result,
        output_name=name,
        output_path=destination,
        copied=True,
        message=message,
    )


def process_batch(
    paths: Sequence[Path],
    template: OmrTemplate,
    *,
    options: BatchOptions | None = None,
    allocator: FilenameAllocator | None = None,
    on_progress: Callable[[BatchProgress], None] | None = None,
    on_result: Callable[[ProcessedScan], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    workers: int = 1,
) -> BatchReport:
    """Process every scan in ``paths``.

    Args:
        paths: Images to process, in the order they should be processed. That
            order decides which of two sheets claiming the same identifier keeps
            the plain file name, and it is the order the report - and therefore
            the CSV - comes back in, whatever order the work finishes in.
        template: The template to read them with.
        options: What the user chose; defaults to "recognise only".
        allocator: Name allocator to use. One is created for
            ``options.output_dir`` when omitted; pass your own to continue an
            earlier run's numbering.
        on_progress: Called as each file starts and finishes. Runs on the
            calling thread, so a GUI caller must marshal to the main thread
            rather than touching widgets here.
        on_result: Called with each file's outcome as soon as it is known, so a
            list can fill in progressively instead of after the whole run.
        should_cancel: Polled while the run proceeds; returning ``True`` stops
            it and reports ``cancelled``.
        workers: How many sheets to read concurrently. ``1`` (the default) runs
            everything in the calling thread, exactly as it always has. More
            than one starts that many worker *processes* - never more than there
            are scans to read.

    Returns:
        The report, containing the results of every file actually processed, in
        batch order.
    """
    settings = options if options is not None else BatchOptions()
    names = (
        allocator
        if allocator is not None
        else FilenameAllocator(settings.output_dir if settings.writes_files else None)
    )

    total = len(paths)
    # More workers than sheets is pure overhead: each unused process still costs
    # an interpreter start-up and its share of memory.
    worker_count = max(1, min(workers, total)) if total else 1
    started = time.perf_counter()

    runner = _run_parallel if worker_count > 1 else _run_sequential
    processed, cancelled, used_workers = runner(
        paths,
        template,
        options=settings,
        allocator=names,
        on_progress=on_progress,
        on_result=on_result,
        should_cancel=should_cancel,
        workers=worker_count,
    )

    report = BatchReport(
        processed=tuple(processed),
        cancelled=cancelled,
        worker_count=used_workers,
        elapsed_seconds=time.perf_counter() - started,
    )
    _LOGGER.info(
        "Batch finished: %d processed, %d complete, %d for review, %d failed, "
        "%d written, %d worker(s), %.2fs%s",
        report.total,
        report.complete_count,
        report.review_count,
        report.failed_count,
        report.written_count,
        report.worker_count,
        report.elapsed_seconds,
        " (cancelled)" if cancelled else "",
    )
    return report


def _failure(path: Path, exc: BaseException) -> ProcessedScan:
    """Return the outcome that stands for "this file blew up unexpectedly"."""
    message = f"An unexpected error occurred: {exc}"
    return ProcessedScan(
        result=ScanResult(
            source_path=path,
            outcome=RecognitionOutcome.ERROR,
            registration=RegistrationStatus.FAILED,
            registration_message=message,
        ),
        message=message,
    )


def _stage_for(outcome: ProcessedScan) -> BatchStage:
    """Which progress stage one finished file should report."""
    if outcome.outcome in (
        RecognitionOutcome.ERROR,
        RecognitionOutcome.REGISTRATION_FAILED,
    ):
        return BatchStage.FAILED
    return BatchStage.COPIED if outcome.copied else BatchStage.RECOGNISED


def _run_sequential(
    paths: Sequence[Path],
    template: OmrTemplate,
    *,
    options: BatchOptions,
    allocator: FilenameAllocator,
    on_progress: Callable[[BatchProgress], None] | None,
    on_result: Callable[[ProcessedScan], None] | None,
    should_cancel: Callable[[], bool] | None,
    workers: int = 1,  # noqa: ARG001 - one runner signature for both strategies
) -> tuple[list[ProcessedScan], bool, int]:
    """Process every scan in the calling thread, one after another."""
    processed: list[ProcessedScan] = []
    total = len(paths)

    for index, path in enumerate(paths):
        if should_cancel is not None and should_cancel():
            if on_progress is not None:
                on_progress(BatchProgress(index, total, path, BatchStage.CANCELLED))
            return processed, True, 1

        if on_progress is not None:
            on_progress(BatchProgress(index, total, path, BatchStage.STARTED))

        try:
            outcome = process_scan(path, template, options=options, allocator=allocator)
        except Exception as exc:
            _LOGGER.exception("Unexpected failure while processing %s", path)
            outcome = _failure(path, exc)

        processed.append(outcome)
        if on_result is not None:
            on_result(outcome)
        if on_progress is not None:
            on_progress(
                BatchProgress(
                    index,
                    total,
                    path,
                    _stage_for(outcome),
                    outcome=outcome.outcome.value,
                )
            )

    return processed, False, 1


def _run_parallel(
    paths: Sequence[Path],
    template: OmrTemplate,
    *,
    options: BatchOptions,
    allocator: FilenameAllocator,
    on_progress: Callable[[BatchProgress], None] | None,
    on_result: Callable[[ProcessedScan], None] | None,
    should_cancel: Callable[[], bool] | None,
    workers: int,
) -> tuple[list[ProcessedScan], bool, int]:
    """Read the scans across a pool of processes, then finish them in order.

    Two orders are in play and the difference is the whole point of this
    function:

    * Sheets are *recognised* in whatever order the workers finish, and progress
      is reported as each one lands - so the bar advances steadily rather than
      waiting on the slowest page.
    * Sheets are *named, copied and recorded* strictly in batch order, here in
      the parent process. A result whose predecessor has not arrived yet waits
      in ``buffered`` until it has. That is what makes ``2103123.jpg``,
      ``2103123_a.jpg``, ``2103123_b.jpg`` come out in the same order as the
      scan list every single time, on any number of cores.

    If the pool cannot be started at all - a locked-down machine, a sandbox that
    forbids new processes, no memory for another interpreter - the whole batch
    falls back to running in this thread. A user who asked for eight workers and
    got one deserves a slower run and a line in the log, not a failed batch and
    an error dialog.
    """
    processed: list[ProcessedScan] = []
    buffered: dict[int, ScanResult] = {}
    total = len(paths)
    next_index = 0
    completed = 0

    def release(result: ScanResult) -> None:
        """Name, copy and record one scan, in the parent process."""
        try:
            outcome = finalise_scan(result, options=options, allocator=allocator)
        except Exception as exc:  # pragma: no cover - finalising is defensive already
            _LOGGER.exception("Unexpected failure while filing %s", result.source_path)
            outcome = _failure(result.source_path, exc)
        processed.append(outcome)
        if on_result is not None:
            on_result(outcome)

    results = recognise_in_parallel(
        paths,
        template,
        workers=workers,
        options=options.engine_options(),
        should_cancel=should_cancel,
    )
    try:
        for index, result in results:
            buffered[index] = result
            completed += 1
            if on_progress is not None:
                stage = (
                    BatchStage.FAILED
                    if result.outcome
                    in (RecognitionOutcome.ERROR, RecognitionOutcome.REGISTRATION_FAILED)
                    else BatchStage.RECOGNISED
                )
                on_progress(
                    BatchProgress(
                        index,
                        total,
                        result.source_path,
                        stage,
                        completed_count=completed,
                        outcome=result.outcome.value,
                    )
                )

            while next_index in buffered:
                release(buffered.pop(next_index))
                next_index += 1
    except Exception:
        if completed:
            raise
        _LOGGER.exception(
            "Could not start %d worker process(es); processing this batch on one core",
            workers,
        )
        results.close()
        return _run_sequential(
            paths,
            template,
            options=options,
            allocator=allocator,
            on_progress=on_progress,
            on_result=on_result,
            should_cancel=should_cancel,
        )

    cancelled = should_cancel is not None and should_cancel()
    # A cancelled run can leave a hole: sheet 4 finished while sheet 3 was still
    # queued and then never ran. What was read is still good work, so it is kept
    # - still in batch order, just with the gap closed.
    for index in sorted(buffered):
        release(buffered.pop(index))

    if cancelled and on_progress is not None:
        last = paths[min(next_index, total - 1)] if total else Path()
        on_progress(
            BatchProgress(
                min(next_index, total),
                total,
                last,
                BatchStage.CANCELLED,
                completed_count=completed,
            )
        )
    return processed, cancelled, workers


__all__ = [
    "BatchOptions",
    "BatchProgress",
    "BatchReport",
    "BatchStage",
    "ProcessedScan",
    "finalise_scan",
    "plan_output_name",
    "process_batch",
    "process_scan",
]
