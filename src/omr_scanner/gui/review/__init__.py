"""The Resolve workflow stage: human review of disputed values (Phase 6).

Purpose:
    Present every value recognition could not decide, with the evidence behind
    it, and record what a named person decided - so that any final value can be
    traced back to either the machine or a human correction with a reason.

What does NOT belong here:
    * Deciding what a conflict *is*, or storing a decision. Those are
      :mod:`omr_scanner.services.conflict_policy` and
      :mod:`omr_scanner.services.review_store`; this package draws what they
      produce and calls them, and contains no resolution logic of its own.
"""

from omr_scanner.gui.review.history_dialog import HistoryDialog, render_history
from omr_scanner.gui.review.page import ResolvePage, ResolvePageState
from omr_scanner.gui.review.worker import SheetBundle, SheetWorker

__all__ = [
    "HistoryDialog",
    "ResolvePage",
    "ResolvePageState",
    "SheetBundle",
    "SheetWorker",
    "render_history",
]
