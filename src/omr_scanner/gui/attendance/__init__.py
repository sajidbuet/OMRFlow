"""Candidate & attendance reconciliation (Phase 7).

The stage that answers "did we receive a script from everybody who sat this
paper, and does each one belong to the candidate it says it does?".

Layout:
    * :mod:`~omr_scanner.gui.attendance.page` - the reconciliation summary,
      table and resolution panel.
    * :mod:`~omr_scanner.gui.attendance.import_dialog` - choosing a file, a
      worksheet and the column mapping, and reviewing what validation found.
    * :mod:`~omr_scanner.gui.attendance.worker` - reading a workbook and
      running reconciliation off the GUI thread.

As with every page in this layer, no module here imports OpenCV, NumPy or
SQLAlchemy: it asks :mod:`omr_scanner.services` and displays what comes back.
"""

from omr_scanner.gui.attendance.page import AttendancePage

__all__ = ["AttendancePage"]
