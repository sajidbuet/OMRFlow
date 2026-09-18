"""Running scan recognition off the GUI thread.

Purpose:
    Keep the window responsive while a batch runs, and deliver each result to
    the main thread the only way Qt permits: through a signal.

Responsibilities:
    * :class:`BatchWorker` - runs
      :func:`~omr_scanner.services.batch_processor.process_batch` in a
      `QThread` and re-emits its callbacks as signals.
    * :class:`PreviewWorker` - recognises one scan to obtain its preview image,
      for when the user selects a row whose preview was not kept.

What does NOT belong here:
    * Widget access. Nothing in this module touches a widget: a worker thread
      that paints is the classic Qt crash, and the whole point of the signal
      boundary is that the page decides what to do on the main thread.
    * Recognition logic, which belongs to the services this wraps.

Cancellation:
    Cooperative, by a flag the worker polls between files. A batch cannot be
    interrupted mid-file - OpenCV will not be stopped part-way through a warp -
    so "cancel" means "stop after the sheets currently being read", which is the
    honest behaviour and takes at most a second or two.

Threads and processes:
    This thread is the GUI's bridge to the batch, not the thing that does the
    reading. When the user's settings ask for more than one worker, the batch
    itself starts a pool of worker *processes* and this thread simply waits on
    it - so the pool belongs to a background thread, the signals still arrive on
    the main thread, and closing the page still shuts everything down through
    the same cancel-and-wait path.

Progress, and why it is *pulled* rather than pushed:
    A ten-thousand-sheet batch finishing eleven sheets a second would emit
    eleven progress signals a second per worker if each completion were pushed
    to the window. Instead this thread keeps the counts itself, in a
    :class:`~omr_scanner.services.batch_progress.BatchProgressTracker`, and the
    page reads a snapshot from it on its own timer, a few times a second. The
    counting stays exact - every completion is recorded - while the number of
    cross-thread events stops depending on how fast the machine is.

    The tracker is thread-safe, which is what makes that safe: this thread
    writes to it, the GUI thread reads from it, and neither waits for the other
    for longer than an integer increment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from omr_scanner.services import (
    BatchOptions,
    BatchProgress,
    BatchProgressTracker,
    FilenameAllocator,
    JobStatus,
    ProcessedScan,
    RecognitionOutcome,
    ScanResult,
    process_batch,
    recognise_scan,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services import BatchRecorder, ProgressSnapshot

TERMINAL_OUTCOMES: dict[str, JobStatus] = {
    RecognitionOutcome.COMPLETE.value: JobStatus.SUCCESS,
    RecognitionOutcome.REVIEW.value: JobStatus.WARNING,
    RecognitionOutcome.REGISTRATION_FAILED.value: JobStatus.FAILED,
    RecognitionOutcome.ERROR.value: JobStatus.FAILED,
}
"""Which recognition outcomes count as which kind of finished job.

A table rather than branching, and one that covers *every* outcome a finished
sheet can have: a sheet that failed is still a sheet that finished, and if it
were missing from here the progress bar would stall on a batch full of
unreadable files - which is exactly the batch a user most wants to watch."""


class BatchWorker(QThread):
    """Processes a list of scans in the background.

    Signals:
        progress: ``BatchProgress`` as each file starts and finishes.
        scan_done: ``ProcessedScan`` as soon as one file's outcome is known, so
            the list fills in progressively.
        finished_report: ``BatchReport`` once the run ends, cancelled or not.
        failed: ``str`` when the run itself could not start or crashed outright,
            as opposed to one file failing, which is a normal result.

    Args:
        paths: Scans to process, in order.
        template: The template to read them with.
        options: What the user chose in the Scan page.
        allocator: Name allocator; pass the page's own so that names stay unique
            across several runs in one session.
        workers: How many sheets to read at once. ``1`` keeps everything in this
            thread; more starts that many worker processes.
        parent: Optional Qt parent.
        tracker: Progress tracker to count into. One is created when omitted;
            a test passes its own with a fake clock and a short warm-up.
        recorder: Durable store for finished sheets (Phase 5), or ``None`` to
            run without persistence - which is what happens with no project
            open, and what the benchmark and most tests do. The recorder is
            driven from *this* thread, one result at a time, which is what
            keeps SQLite's single-writer assumption true without a lock.
    """

    progress = Signal(object)
    scan_done = Signal(object)
    finished_report = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        paths: list[Path],
        template: OmrTemplate,
        options: BatchOptions,
        allocator: FilenameAllocator | None = None,
        parent: QObject | None = None,
        *,
        workers: int = 1,
        tracker: BatchProgressTracker | None = None,
        recorder: BatchRecorder | None = None,
    ) -> None:
        super().__init__(parent)
        self._paths = list(paths)
        self._template = template
        self._options = options
        self._allocator = allocator
        self._workers = max(1, workers)
        self._cancelled = False
        self._tracker = tracker if tracker is not None else BatchProgressTracker()
        self._tracker.start(len(self._paths), workers=self._workers)
        self._recorder = recorder

    def cancel(self) -> None:
        """Ask the run to stop after the sheets currently being read."""
        self._cancelled = True
        self._tracker.request_cancel()

    @property
    def tracker(self) -> BatchProgressTracker:
        """The run's progress tracker, safe to read from the GUI thread."""
        return self._tracker

    def progress_snapshot(self) -> ProgressSnapshot:
        """Return the current progress, for the page's refresh timer."""
        return self._tracker.snapshot()

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancelled

    @property
    def workers(self) -> int:
        """How many sheets this run reads at once."""
        return self._workers

    @property
    def recorder(self) -> BatchRecorder | None:
        """The durable store this run is writing to, if any."""
        return self._recorder

    @property
    def persistence_failure(self) -> str:
        """Why results could not be stored, or ``""``.

        Read by the page when the run ends: a batch whose results were computed
        but not written is **not** a successful batch, and saying so is the
        whole point of tracking it separately from recognition failures.
        """
        return self._recorder.failure if self._recorder is not None else ""

    def run(self) -> None:
        """Process the batch. Runs on the worker thread; touches no widget."""
        try:
            report = process_batch(
                self._paths,
                self._template,
                options=self._options,
                allocator=self._allocator,
                on_progress=self._emit_progress,
                on_result=self._emit_result,
                should_cancel=lambda: self._cancelled,
                workers=self._workers,
            )
        except Exception as exc:
            self._tracker.fail()
            self._flush_recorder()
            self.failed.emit(str(exc))
            return
        # Flush before announcing the run is over. Anything still buffered is
        # finished work, and the page reports completion off the back of this
        # signal - so the last few sheets must be durable before it does.
        self._flush_recorder()
        self._tracker.finish(cancelled=report.cancelled)
        self.finished_report.emit(report)

    def _flush_recorder(self) -> None:
        """Commit whatever the recorder still holds, if there is one."""
        if self._recorder is not None:
            self._recorder.flush()

    def _emit_progress(self, update: BatchProgress) -> None:
        """Count the completion, then pass the event on.

        Counting happens here, in the parent process, on one thread, under the
        tracker's lock - never in a worker. That is what keeps the totals right
        when eight sheets finish at the same instant.
        """
        status = TERMINAL_OUTCOMES.get(update.outcome)
        if status is not None:
            self._tracker.record(status)
        self.progress.emit(update)

    def _emit_result(self, processed: ProcessedScan) -> None:
        """Record one finished sheet durably, then hand it to the page.

        Recording happens *before* the signal so that a result the page shows
        as done has already been offered to the store. A storage failure does
        not stop the batch - the remaining sheets are still worth reading, and
        the results stay in memory where the page can still export them - but
        it is remembered on the recorder and reported when the run ends.
        """
        if self._recorder is not None:
            self._recorder.record(processed)
        self.scan_done.emit(processed)


class PreviewWorker(QThread):
    """Recognises one scan to obtain its rectified preview image.

    Batch runs deliberately discard previews (a hundred rectified pages is most
    of a gigabyte); when the user selects a row, this recreates the one they are
    looking at. Recognition is deterministic, so the values shown alongside the
    preview are the same ones the batch recorded.

    Signals:
        ready: ``ScanResult`` including its preview.
        failed: ``str`` when the scan could not be re-read at all.

    Args:
        path: The scan to preview.
        template: The template to read it with.
        parent: Optional Qt parent.
    """

    ready = Signal(object)
    failed = Signal(str)

    def __init__(
        self, path: Path, template: OmrTemplate, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._template = template

    @property
    def path(self) -> Path:
        """The scan this worker is rendering."""
        return self._path

    def run(self) -> None:
        """Recognise the scan with a preview. Runs on the worker thread."""
        try:
            result: ScanResult = recognise_scan(
                self._path, self._template, with_preview=True
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.ready.emit(result)


__all__ = ["BatchWorker", "PreviewWorker"]
