"""The Calibration workflow stage (Phase 4).

Purpose:
    Let an operator verify a template against representative real scans, and
    tune its recognition thresholds, before trusting it with a batch.

Responsibilities:
    * :mod:`~omr_scanner.gui.calibration.page` - :class:`CalibrationPage`, the
      workflow stage itself.
    * :mod:`~omr_scanner.gui.calibration.worker` - :class:`CalibrationWorker`,
      which opens a scan's registration and measurement off the GUI thread.

What does NOT belong here:
    * Recognition, registration or measurement of any kind - all of it is
      :mod:`omr_scanner.services.recognition_service` and
      :mod:`omr_scanner.services.calibration_service`. This package presents;
      it never decides.
    * A second geometry editor. A template whose regions are physically wrong
      is edited in the Template Designer, not here (``docs/calibration_workflow.md``).
"""

from omr_scanner.gui.calibration.page import CalibrationPage
from omr_scanner.gui.calibration.worker import CalibrationSessionResult, CalibrationWorker

__all__ = ["CalibrationPage", "CalibrationSessionResult", "CalibrationWorker"]
