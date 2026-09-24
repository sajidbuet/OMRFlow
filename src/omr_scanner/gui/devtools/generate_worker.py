"""Generating a dataset without freezing the window.

Purpose:
    Run :func:`~omr_scanner.evaluation.synthetic_dataset.generate_dataset` on a
    worker thread and report its progress as Qt signals.

Responsibilities:
    * :class:`DatasetWorker` - one run, cancellable, reporting counts.

What does NOT belong here:
    * Any decision about *what* to generate; the worker is handed a request.
    * Any widget. The worker emits; the dialog draws.

Why a thread and not a process:
    Rendering is one page at a time and spends its life in OpenCV, which
    releases the GIL, so a thread keeps the window responsive without the
    pickling and start-up cost a pool would add. The batch *recognition* path
    uses processes for the opposite reason - it is CPU-bound Python.

Progress and ETA:
    Reuses :class:`~omr_scanner.services.BatchProgressTracker`, the same
    estimator the Scan page's progress panel uses. One smoothing rule, one
    warm-up rule, one definition of "remaining", so the two screens cannot
    disagree about how long something will take.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from omr_scanner.evaluation.synthetic_dataset import generate_dataset
from omr_scanner.services import BatchProgressTracker, JobStatus, load_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PySide6.QtCore import QObject

    from omr_scanner.evaluation.synthetic_dataset import GenerationProgress
    from omr_scanner.gui.devtools.generate_dialog import GenerationRequest
    from omr_scanner.services import ProgressSnapshot

_LOGGER = logging.getLogger(__name__)


class DatasetWorker(QThread):
    """Generate one dataset in the background.

    Signals:
        sheet_done: ``str`` file name, once per written sheet. Throttled
            drawing is the receiver's job, exactly as it is for a batch.
        finished_dataset: ``DatasetManifest`` when the run ends, including a
            cancelled run - what was written is real and its manifest says how
            much of it there is.
        failed: ``str`` when the run could not proceed at all.

    Args:
        request: What to generate.
        parent: Optional Qt parent.
    """

    sheet_done = Signal(str)
    finished_dataset = Signal(object)
    failed = Signal(str)

    def __init__(self, request: GenerationRequest, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._request = request
        self._tracker = BatchProgressTracker()
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the run to stop after the sheet being drawn.

        Thread-safe by being a single boolean write, polled between sheets.
        There is no mid-sheet cancellation: a half-drawn page is not something
        anybody wants on disk, and one page is a few milliseconds.
        """
        self._cancelled = True
        self._tracker.request_cancel()

    def progress_snapshot(self) -> ProgressSnapshot:
        """The current counts, rate and estimate. Safe to call from the GUI thread."""
        return self._tracker.snapshot()

    def run(self) -> None:
        """Generate the dataset, reporting as it goes.

        Every failure is turned into a ``failed`` signal rather than an
        exception: this runs on a Qt thread, and an exception escaping here
        takes the thread down without telling anybody why.
        """
        request = self._request
        self._tracker.prepare()
        try:
            template = load_template(request.template_path)
            # Planned before the progress bar is sized, because with attendance
            # on it is the roster - not the sheet count - the operator typed:
            # absentees and missing scans mean fewer images than candidates,
            # and a bar sized to the roster would stop short of full.
            population = request.population()
            expected = (
                len(population.sheets_to_render())
                if population is not None
                else request.count
            )
            self._tracker.start(expected, workers=1)
            manifest = generate_dataset(
                request.output_dir,
                template,
                count=request.count,
                seed=request.seed,
                profile=request.profile,
                custom_families=list(request.families) or None,
                dpi=request.dpi,
                image_format=request.image_format,
                jpeg_quality=request.jpeg_quality,
                name=request.name,
                template_path=str(request.template_path),
                write_metadata=request.write_metadata,
                population=population,
                on_progress=self._on_sheet,
                should_cancel=lambda: self._cancelled,
            )
        except Exception as exc:
            self._tracker.fail()
            _LOGGER.exception("Dataset generation failed")
            self.failed.emit(str(exc))
            return

        self._tracker.finish(cancelled=self._cancelled)
        _LOGGER.info(
            "Generated %d sheet(s) into %s%s",
            len(manifest.entries),
            request.output_dir,
            " (cancelled)" if self._cancelled else "",
        )
        self.finished_dataset.emit(manifest)

    def _on_sheet(self, progress: GenerationProgress) -> None:
        """Count one written sheet and announce it."""
        self._tracker.record(JobStatus.SUCCESS)
        self.sheet_done.emit(progress.name)


__all__ = ["DatasetWorker"]
