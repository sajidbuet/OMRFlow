"""The Answer Key stage (Phase 8).

Write, read or scan the correct answers for each question-paper set, flag any
question the examiners have withdrawn, and verify the key before anything is
marked against it.

As with every page in this layer, nothing here imports OpenCV, NumPy or
SQLAlchemy: it asks :mod:`omr_scanner.services` and displays what comes back.
"""

from omr_scanner.gui.answer_key.page import AnswerKeyPage

__all__ = ["AnswerKeyPage"]
