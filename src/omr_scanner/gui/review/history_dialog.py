"""The provenance of one disputed value, as a reviewer reads it (Phase 6).

Purpose:
    Show the complete, ordered history of a conflict - what the machine saw,
    and every human decision since - so that any final value can be traced back
    to its origin without leaving the application.

What does NOT belong here:
    * A second, textual history kept alongside the real one. Every line this
      dialog renders comes from a persisted
      :class:`~omr_scanner.database.models.AuditEvent`, read through
      :func:`~omr_scanner.services.review_store.history_for`. If the ledger is
      empty this dialog is empty, which is the honest outcome.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.review import ReviewAction

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.services import AuditRecord, ConflictRecord

DIALOG_WIDTH = 620
DIALOG_HEIGHT = 480

_ACTION_HEADINGS: dict[ReviewAction, str] = {
    ReviewAction.DETECTED: "Machine recognition",
    ReviewAction.RE_RECOGNISED: "Sheet read again",
    ReviewAction.ACCEPTED: "Machine value confirmed",
    ReviewAction.CORRECTED: "Corrected",
    ReviewAction.DEFERRED: "Deferred",
    ReviewAction.REOPENED: "Reopened",
    ReviewAction.WITHDRAWN: "Withdrawn by the machine",
}


def render_history(conflict: ConflictRecord, events: Sequence[AuditRecord]) -> str:
    """Render a conflict's ledger as rich text.

    Args:
        conflict: The conflict being described.
        events: Its events, oldest first.

    Returns:
        HTML for a label. Pure, so a test can assert on what a reviewer would
        read without opening a dialog.

    The machine's own reading is repeated in the header rather than only
    appearing in the first event, because it is the one value that stays true
    however many corrections follow it.
    """
    lines: list[str] = [
        f"<b>{conflict.field.describe()}</b> - {conflict.conflict_type.label}<br>",
        f"Sheet: {conflict.scan_name or '(unknown)'}<br>",
        "Machine read: <b>"
        f"{conflict.observation.value or '(blank)'}</b>"
        f" &middot; status {conflict.observation.status or 'n/a'}<br>",
        "<hr>",
    ]
    if not events:
        lines.append("<i>No history has been recorded for this conflict.</i>")
        return "".join(lines)

    for event in events:
        heading = _ACTION_HEADINGS.get(event.action, event.action.value)
        lines.append(f"<p><b>{event.occurred_at}</b> &mdash; {heading}")
        if event.reviewer:
            lines.append(f"<br>Reviewer: <b>{event.reviewer}</b>")
        if event.action.sets_effective_value or event.action is ReviewAction.RE_RECOGNISED:
            lines.append(
                f"<br>Value: {event.previous_value or '(blank)'} "
                f"&rarr; <b>{event.new_value or '(blank)'}</b>"
            )
        elif event.action is ReviewAction.DETECTED:
            lines.append(f"<br>Value: <b>{event.new_value or '(blank)'}</b>")
        if event.reason_label:
            lines.append(f"<br>Reason: {event.reason_label}")
        if event.detail:
            lines.append(f"<br><i>{event.detail}</i>")
        lines.append("</p>")
    return "".join(lines)


class HistoryDialog(QDialog):
    """A read-only window showing one conflict's complete provenance.

    Args:
        conflict: The conflict being described.
        events: Its events, oldest first.
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        conflict: ConflictRecord,
        events: Sequence[AuditRecord],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("conflictHistoryDialog")
        self.setWindowTitle("Conflict history")
        self.resize(DIALOG_WIDTH, DIALOG_HEIGHT)

        layout = QVBoxLayout(self)

        body = QLabel(render_history(conflict, events))
        body.setObjectName("conflictHistoryLabel")
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignTop)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

        # Scrolled: a conflict reopened several times has a long history, and a
        # dialog that grew with it would eventually be taller than the screen.
        scroll = QScrollArea()
        scroll.setObjectName("conflictHistoryScroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.addWidget(scroll, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.setObjectName("conflictHistoryButtons")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.body_label = body


__all__ = ["HistoryDialog", "render_history"]
