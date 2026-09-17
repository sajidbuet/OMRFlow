"""The Scan workflow stage: batch recognition of scanned answer sheets.

Purpose:
    Phase 3's user-facing half. The recognition itself lives in
    :mod:`omr_scanner.services` and :mod:`omr_scanner.recognition`; this package
    is the window onto it.

Modules:
    * ``page.py``    - :class:`~omr_scanner.gui.scan.page.ScanPage`, the stage
      itself: controls, scan list, preview and results.
    * ``preview.py`` - the zoomable page view and its recognition overlay.
    * ``worker.py``  - the `QThread` wrappers that keep batch processing off the
      GUI thread.

Layering:
    Nothing here imports ``cv2``, ``numpy``, ``omr_scanner.imaging`` or
    ``omr_scanner.recognition``; the services layer projects every result into
    plain strings, floats and bytes first. See ``docs/ARCHITECTURE.md`` and
    ``tests/unit/test_architecture.py``.
"""

from omr_scanner.gui.scan.page import ScanEntry, ScanPage, ScanPageState
from omr_scanner.gui.scan.preview import OverlayItem, ScanPreviewView
from omr_scanner.gui.scan.worker import BatchWorker, PreviewWorker

__all__ = [
    "BatchWorker",
    "OverlayItem",
    "PreviewWorker",
    "ScanEntry",
    "ScanPage",
    "ScanPageState",
    "ScanPreviewView",
]
