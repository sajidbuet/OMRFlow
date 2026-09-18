"""Opening a calibration scan's registration and measurement off the GUI thread.

Purpose:
    Load, register and measure each representative scan
    (:meth:`~omr_scanner.services.recognition_service.RecognitionEngine.open_session`)
    without freezing the window - the one part of calibration that is not
    cheap, because unlike a threshold change it genuinely reads a file and
    runs Phase 1 registration.

Responsibilities:
    * :class:`CalibrationWorker` - opens a list of scans in the background and
      reports each one as it becomes ready.

What does NOT belong here:
    * Recognition or judgement of any kind. This thread only opens sessions;
      :class:`~omr_scanner.gui.calibration.page.CalibrationPage` decides what
      to do with them.
    * Repeating a scan's registration on a threshold change - that is exactly
      what :class:`~omr_scanner.services.recognition_service.CalibrationSession`
      exists to avoid, and it is cheap enough to call directly from the GUI
      thread (``docs/calibration_workflow.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from omr_scanner.services import RecognitionEngine, RecognitionOptions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services import CalibrationSession, ScanResult


@dataclass(frozen=True, slots=True)
class CalibrationSessionResult:
    """One scan's opened session, with its first result already computed.

    Attributes:
        path: The scan that was opened.
        session: The cached session - hand it back to the GUI thread and call
            :meth:`~omr_scanner.services.recognition_service.CalibrationSession.recompute`
            on it as many times as a threshold changes.
        result: What ``session.recompute(template)`` produced against the
            template this worker was given, so the page has something to show
            immediately without a second call.
    """

    path: Path
    session: CalibrationSession
    result: ScanResult


class CalibrationWorker(QThread):
    """Opens a list of representative scans, one at a time, in the background.

    Signals:
        session_ready: :class:`CalibrationSessionResult` as each scan finishes
            opening.
        finished_all: Every scan in the list has been attempted.
        failed: ``str`` when the run itself could not proceed - as opposed to
            one scan failing to register, which is an ordinary
            :class:`CalibrationSessionResult` whose
            :attr:`~omr_scanner.services.recognition_service.CalibrationSession.registered`
            is ``False``.

    Args:
        paths: Scans to open, in order.
        template: The template to register and measure each one against.
        parent: Optional Qt parent.
    """

    session_ready = Signal(object)
    finished_all = Signal()
    failed = Signal(str)

    def __init__(
        self, paths: list[Path], template: OmrTemplate, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._paths = list(paths)
        self._template = template
        self._cancelled = False

    def cancel(self) -> None:
        """Stop opening further scans after the one currently in progress."""
        self._cancelled = True

    def run(self) -> None:
        """Open each scan. Runs on the worker thread; touches no widget."""
        try:
            engine = RecognitionEngine(
                RecognitionOptions(
                    with_preview=True,
                    keep_bubble_measurements=True,
                    keep_quality_metrics=True,
                )
            )
            for path in self._paths:
                if self._cancelled:
                    break
                session = engine.open_session(path, self._template)
                result = session.recompute(self._template)
                self.session_ready.emit(
                    CalibrationSessionResult(path=path, session=session, result=result)
                )
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished_all.emit()


__all__ = ["CalibrationSessionResult", "CalibrationWorker"]
