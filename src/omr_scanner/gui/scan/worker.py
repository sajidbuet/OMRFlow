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
    so "cancel" means "stop after the current sheet", which is the honest
    behaviour and takes at most a second or two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from omr_scanner.services import (
    BatchOptions,
    BatchProgress,
    FilenameAllocator,
    ProcessedScan,
    ScanResult,
    process_batch,
    recognise_scan,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate


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
        parent: Optional Qt parent.
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
    ) -> None:
        super().__init__(parent)
        self._paths = list(paths)
        self._template = template
        self._options = options
        self._allocator = allocator
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the run to stop after the file it is working on."""
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        return self._cancelled

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
            )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_report.emit(report)

    def _emit_progress(self, update: BatchProgress) -> None:
        self.progress.emit(update)

    def _emit_result(self, processed: ProcessedScan) -> None:
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
