"""Processing many scans without letting one bad file stop the rest.

Purpose:
    Run :func:`~omr_scanner.services.recognition_service.recognise_scan` over a
    list of files, decide each one's output name, optionally copy it there, and
    report progress - all without Qt, so the same code runs from a worker
    thread, a test or a future command line tool.

Responsibilities:
    * :func:`process_scan` - one file: recognise, name, copy.
    * :func:`process_batch` - many files, with progress, cancellation and
      per-file error isolation.
    * :class:`BatchOptions` - what the user chose in the Scan page.

What does NOT belong here:
    * Threads. The caller decides whether this runs on one; the only
      concurrency contract is that ``on_progress``/``on_result`` are called from
      whichever thread is running the batch, and must therefore not touch Qt
      widgets directly.
    * Naming rules, which are :mod:`omr_scanner.services.filename_manager`, and
      recognition, which is
      :mod:`omr_scanner.services.recognition_service`.

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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.services.filename_manager import FilenameAllocator
from omr_scanner.services.recognition_service import (
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    recognise_scan,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.imaging.metrics import BubbleMetricsConfig

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
    """

    output_dir: Path | None = None
    rename_with_identifier: bool = False
    with_preview: bool = False
    metrics_config: BubbleMetricsConfig | None = None

    @property
    def writes_files(self) -> bool:
        """Whether this run will copy anything to disk."""
        return self.rename_with_identifier and self.output_dir is not None


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
    """

    index: int
    total: int
    path: Path
    stage: BatchStage

    @property
    def completed(self) -> int:
        """How many files have finished, for a progress bar."""
        return self.index + 1 if self.stage is not BatchStage.STARTED else self.index


@dataclass(frozen=True, slots=True)
class BatchReport:
    """Summary of a finished (or cancelled) batch.

    Attributes:
        processed: Every file's outcome, in batch order.
        cancelled: Whether the run stopped early at the caller's request.
    """

    processed: tuple[ProcessedScan, ...] = ()
    cancelled: bool = False

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
    result = recognise_scan(
        path,
        template,
        metrics_config=options.metrics_config,
        with_preview=options.with_preview,
    )

    if not options.rename_with_identifier:
        return ProcessedScan(result=result)

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
) -> BatchReport:
    """Process every scan in ``paths``.

    Args:
        paths: Images to process, in the order they should be processed. That
            order decides which of two sheets claiming the same identifier keeps
            the plain file name.
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
        should_cancel: Polled before each file; returning ``True`` stops the
            run and reports ``cancelled``.

    Returns:
        The report, containing the results of every file actually processed.
    """
    settings = options if options is not None else BatchOptions()
    names = (
        allocator
        if allocator is not None
        else FilenameAllocator(settings.output_dir if settings.writes_files else None)
    )

    processed: list[ProcessedScan] = []
    total = len(paths)
    cancelled = False

    for index, path in enumerate(paths):
        if should_cancel is not None and should_cancel():
            cancelled = True
            if on_progress is not None:
                on_progress(BatchProgress(index, total, path, BatchStage.CANCELLED))
            break

        if on_progress is not None:
            on_progress(BatchProgress(index, total, path, BatchStage.STARTED))

        try:
            outcome = process_scan(
                path, template, options=settings, allocator=names
            )
        except Exception as exc:
            _LOGGER.exception("Unexpected failure while processing %s", path)
            outcome = ProcessedScan(
                result=ScanResult(
                    source_path=path,
                    outcome=RecognitionOutcome.ERROR,
                    registration=RegistrationStatus.FAILED,
                    registration_message=f"An unexpected error occurred: {exc}",
                ),
                message=f"An unexpected error occurred: {exc}",
            )

        processed.append(outcome)
        if on_result is not None:
            on_result(outcome)
        if on_progress is not None:
            stage = (
                BatchStage.FAILED
                if outcome.outcome
                in (RecognitionOutcome.ERROR, RecognitionOutcome.REGISTRATION_FAILED)
                else (BatchStage.COPIED if outcome.copied else BatchStage.RECOGNISED)
            )
            on_progress(BatchProgress(index, total, path, stage))

    report = BatchReport(processed=tuple(processed), cancelled=cancelled)
    _LOGGER.info(
        "Batch finished: %d processed, %d complete, %d for review, %d failed, %d written%s",
        report.total,
        report.complete_count,
        report.review_count,
        report.failed_count,
        report.written_count,
        " (cancelled)" if cancelled else "",
    )
    return report


__all__ = [
    "BatchOptions",
    "BatchProgress",
    "BatchReport",
    "BatchStage",
    "ProcessedScan",
    "plan_output_name",
    "process_batch",
    "process_scan",
]
