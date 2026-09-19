"""The Results stage (Phase 8).

Configure how a paper is marked, check the rules before applying them, mark a
reconciled batch, and inspect any candidate's mark question by question.

As with every page in this layer, nothing here imports OpenCV, NumPy or
SQLAlchemy: it asks :mod:`omr_scanner.services` and displays what comes back.
"""

from omr_scanner.gui.results.page import ResultsPage
from omr_scanner.gui.results.policy_dialog import ScoringPolicyDialog

__all__ = ["ResultsPage", "ScoringPolicyDialog"]
