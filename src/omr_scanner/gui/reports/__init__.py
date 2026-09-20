"""The Result Management workflow stage (Phase 9).

Purpose:
    The GUI seam for :mod:`omr_scanner.services.report_store`: list the
    project's sets, associate and validate each one's result template,
    preview and generate Rollwise/Meritwise reports, and configure the
    report layout - never generating a workbook or touching openpyxl
    directly on the GUI thread.
"""
