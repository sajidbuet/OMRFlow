"""Resolve's operational queues: suggested rescans and files awaiting a decision (revised phase 8).

Purpose:
    Two queues a continuous scan session produces that are not identifier
    conflicts, shown in the Resolve stage's workspace beside the existing
    *Rejected / Rescan* view:

    * **Suggested rescans** - sheets the scan-quality decision layer
      (:mod:`omr_scanner.services.quality_decisions`) marked *rescan
      required*. A suggestion is **never** a rejection: the operator confirms
      it (through the existing Reject & Rescan flow, by name) or dismisses it
      (audited; the evidence stays).
    * **Files awaiting a decision** - held (arrived for a closed session),
      unreadable or unsupported intake files
      (:mod:`omr_scanner.services.intake_decisions`). The actions offered are
      exactly that service's ``OPTIONS`` for the file's state.

What does NOT belong here:
    Any decision. These panels show the service's values and emit what the
    operator chose; the Resolve page calls the service. Correcting a Student
    ID or set code stays in the conflict workspace - this module never edits
    a recognised value.

The quality policy is unvalidated:
    Every suggestion panel says so, once, in a non-modal note - the default
    mapping from evidence to *rescan required* has not been calibrated on real
    scanners (``PHASE_G_HANDOFF.md`` §3).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.intake import IntakeState
from omr_scanner.domain.quality_decision import QualityReason
from omr_scanner.domain.scan_lifecycle import RESCAN_REASONS, RejectionReason
from omr_scanner.domain.scan_quality import ScanQualityIssueCode, issue_label
from omr_scanner.gui.theme import VARIANT_PRIMARY, VARIANT_PROPERTY, Color, Spacing
from omr_scanner.gui.ui_scale import resize_scaled, scale_layout
from omr_scanner.services.intake_decisions import FileDecision

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.quality_decision import QualityPolicy
    from omr_scanner.services.intake_decisions import PendingFile
    from omr_scanner.services.quality_decisions import QualitySuggestion
    from omr_scanner.services.session_sheets import SheetProvenance

POLICY_NOTE = (
    "Rescan suggestions use an unvalidated default quality policy. Operator "
    "confirmation is always required - nothing is rejected automatically."
)

_REASON_TEXT: dict[QualityReason, str] = {
    QualityReason.IMAGE_NOT_DECODED: "The image could not be decoded",
    QualityReason.REGISTRATION_FAILED: "The page could not be aligned (registration failed)",
    QualityReason.QUALITY_UNUSABLE: "The page geometry check found it unusable",
    QualityReason.QUALITY_REVIEW: "The page geometry check asked for review",
    QualityReason.GEOMETRY_NOT_VERIFIED: "The page geometry could not be verified",
    QualityReason.UNKNOWN_EVIDENCE: "Quality evidence this version does not recognise",
    QualityReason.PROCESSING_ERROR: "The software failed while reading it",
    QualityReason.TEMPLATE_ERROR: "The template could not be applied",
    QualityReason.ALIGNMENT_WARNING: "Aligned with a warning",
    QualityReason.QUALITY_NOT_EVALUATED: "Page quality was not evaluated",
}

_DECISION_TEXT: dict[FileDecision, str] = {
    FileDecision.RELEASE: "Release into the session",
    FileDecision.RETRY: "Read Again",
    FileDecision.DISMISS: "Dismiss (Do Not Process)...",
}

_STATE_TEXT: dict[IntakeState, str] = {
    IntakeState.HELD: "Held",
    IntakeState.UNREADABLE: "Unreadable",
    IntakeState.UNSUPPORTED: "Unsupported",
}


def reason_text(reason: QualityReason) -> str:
    """One quality reason in an operator's words."""
    return _REASON_TEXT.get(reason, reason.value.replace("_", " "))


def _issue_text(code: str) -> str:
    """A stored geometry issue code in words (the code itself if unknown here)."""
    try:
        return issue_label(ScanQualityIssueCode(code))
    except ValueError:
        return code.replace("_", " ")


def suggestion_issue(item: QualitySuggestion) -> str:
    """The queue's *Issue* cell for a suggestion."""
    first = reason_text(item.reasons[0]) if item.reasons else "Quality evidence"
    suggested = item.suggested_reason.label if item.suggested_reason is not None else ""
    return f"{first} - suggested: {suggested}" if suggested else first


def file_state_text(item: PendingFile) -> str:
    """``Held`` / ``Unreadable`` / ``Unsupported``."""
    return _STATE_TEXT.get(item.state, item.state.value.capitalize())


def _rows_html(rows: Sequence[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><td style='color:{Color.TEXT_TERTIARY};padding-right:10px;'>{html.escape(name)}"
        f"</td><td>{html.escape(value)}</td></tr>"
        for name, value in rows
        if value
    )
    return f"<table>{body}</table>"


def suggestion_details_html(
    item: QualitySuggestion,
    provenance: SheetProvenance | None,
    policy: QualityPolicy | None,
    *,
    student_id: str = "",
    set_code: str = "",
) -> str:
    """Everything recorded about one suggestion - evidence, provenance and the policy."""
    rows: list[tuple[str, str]] = [
        ("Sheet", (provenance.original_name if provenance else "") or item.source_name),
        ("Source", provenance.source_label if provenance else ""),
        ("Batch", provenance.batch_label if provenance else ""),
        ("Student ID", student_id or "(not read)"),
        ("Set", set_code or "(not read)"),
        ("Evidence", "; ".join(reason_text(reason) for reason in item.reasons) or "-"),
        ("Issues", "; ".join(_issue_text(code) for code in item.issue_codes)),
        (
            "Suggested reason",
            item.suggested_reason.label if item.suggested_reason is not None else "",
        ),
        (
            "Policy",
            (
                f"{policy.name} v{item.policy_version}"
                + ("" if policy.validated else " - UNVALIDATED DEFAULT")
            )
            if policy is not None
            else f"version {item.policy_version}",
        ),
        (
            "Decided",
            f"{item.evaluated_at.astimezone():%Y-%m-%d %H:%M}"
            if item.evaluated_at is not None
            else "",
        ),
    ]
    return _rows_html(rows)


def file_details_html(item: PendingFile) -> str:
    """Everything recorded about one file waiting for a decision."""
    rows: list[tuple[str, str]] = [
        ("File", item.file_name),
        ("Source", item.source_label),
        ("Where", item.relative_path),
        ("State", file_state_text(item)),
        ("Why", item.why),
        ("Detail", item.detail),
        (
            "First seen",
            f"{item.arrived_at.astimezone():%Y-%m-%d %H:%M}" if item.arrived_at else "",
        ),
        (
            "Waiting since",
            f"{item.waiting_since.astimezone():%Y-%m-%d %H:%M}" if item.waiting_since else "",
        ),
    ]
    if item.quality is not None:
        rows.append(("Quality policy", item.quality.decision.label))
    return _rows_html(rows)


class SuggestionPanel(QWidget):
    """The workspace for one suggested rescan: its evidence and the two answers.

    Signals:
        confirm_requested: confirm the suggestion (opens the named confirmation).
        dismiss_requested: decline it (opens the named confirmation).
    """

    confirm_requested = Signal()
    dismiss_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("suggestionPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("suggestionPanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        scale_layout(
            layout, margins=(Spacing.SM, Spacing.XS, Spacing.SM, Spacing.XS), spacing=Spacing.XS
        )
        scroll.setWidget(body)
        outer.addWidget(scroll)

        self.policy_note = QLabel(POLICY_NOTE)
        self.policy_note.setObjectName("qualityPolicyNote")
        self.policy_note.setWordWrap(True)
        self.policy_note.setStyleSheet(f"color: {Color.STATUS_BUSY};")
        layout.addWidget(self.policy_note)

        self.heading = QLabel("Select a suggested rescan from the queue.")
        self.heading.setObjectName("suggestionHeading")
        self.heading.setTextFormat(Qt.TextFormat.RichText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        columns = QHBoxLayout()
        scale_layout(columns, spacing=Spacing.MD)
        self.details_label = QLabel("")
        self.details_label.setObjectName("suggestionDetailsLabel")
        self.details_label.setTextFormat(Qt.TextFormat.RichText)
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        columns.addWidget(self.details_label, stretch=55, alignment=Qt.AlignmentFlag.AlignTop)
        actions_box = QWidget()
        actions = QVBoxLayout(actions_box)
        actions.setContentsMargins(0, 0, 0, 0)
        scale_layout(actions, spacing=Spacing.XS)
        self.confirm_button = QPushButton("Confirm Rescan Required...")
        self.confirm_button.setObjectName("confirmSuggestionButton")
        self.confirm_button.setProperty(VARIANT_PROPERTY, VARIANT_PRIMARY)
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.setToolTip(
            "Reject this sheet pending a rescan, through Reject & Rescan, recorded against "
            "your name. Nothing is deleted; the sheet stops counting until it is replaced."
        )
        self.confirm_button.clicked.connect(self.confirm_requested)
        actions.addWidget(self.confirm_button)
        self.dismiss_button = QPushButton("Dismiss Suggestion...")
        self.dismiss_button.setObjectName("dismissSuggestionButton")
        self.dismiss_button.setAutoDefault(False)
        self.dismiss_button.setToolTip(
            "Keep the sheet exactly as it was read. Recorded against your name; the "
            "evidence stays on record."
        )
        self.dismiss_button.clicked.connect(self.dismiss_requested)
        actions.addWidget(self.dismiss_button)
        self.reviewer_note = QLabel("")
        self.reviewer_note.setObjectName("suggestionReviewerNote")
        self.reviewer_note.setWordWrap(True)
        actions.addWidget(self.reviewer_note)
        actions.addStretch(1)
        columns.addWidget(actions_box, stretch=45)
        layout.addLayout(columns)
        layout.addStretch(1)
        self.show_suggestion(None)

    def show_suggestion(
        self,
        item: QualitySuggestion | None,
        *,
        details: str = "",
        can_decide: bool = False,
    ) -> None:
        """Display one suggestion, or the empty state."""
        has = item is not None
        if item is None:
            self.heading.setText("Select a suggested rescan from the queue.")
        else:
            self.heading.setText(
                f"<span style='font-size:12pt;color:{Color.STATUS_BUSY};'><b>Suggested rescan"
                f"</b></span> &mdash; {html.escape(suggestion_issue(item))}"
            )
        self.details_label.setText(details)
        self.confirm_button.setEnabled(has and can_decide)
        self.dismiss_button.setEnabled(has and can_decide)
        self.reviewer_note.setText(
            ""
            if can_decide or not has
            else "Set your reviewer name in File > Settings - confirming or dismissing is "
            "recorded against a named operator."
        )


class FilePanel(QWidget):
    """The workspace for one held / unreadable / unsupported file.

    Signals:
        decision_requested: the :class:`FileDecision` the operator chose.
    """

    decision_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("filePanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("filePanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        scale_layout(
            layout, margins=(Spacing.SM, Spacing.XS, Spacing.SM, Spacing.XS), spacing=Spacing.XS
        )
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.heading = QLabel("Select a file from the queue.")
        self.heading.setObjectName("fileHeading")
        self.heading.setTextFormat(Qt.TextFormat.RichText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        columns = QHBoxLayout()
        scale_layout(columns, spacing=Spacing.MD)
        self.details_label = QLabel("")
        self.details_label.setObjectName("fileDetailsLabel")
        self.details_label.setTextFormat(Qt.TextFormat.RichText)
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        columns.addWidget(self.details_label, stretch=55, alignment=Qt.AlignmentFlag.AlignTop)
        actions_box = QWidget()
        self._actions = QVBoxLayout(actions_box)
        self._actions.setContentsMargins(0, 0, 0, 0)
        scale_layout(self._actions, spacing=Spacing.XS)
        self.buttons: dict[FileDecision, QPushButton] = {}
        for decision in FileDecision:
            button = QPushButton(_DECISION_TEXT[decision])
            button.setObjectName(f"fileDecision_{decision.value}")
            button.setAutoDefault(False)
            button.clicked.connect(
                lambda _checked=False, chosen=decision: self.decision_requested.emit(chosen)
            )
            self._actions.addWidget(button)
            self.buttons[decision] = button
        self.note_label = QLabel("")
        self.note_label.setObjectName("fileDecisionNote")
        self.note_label.setWordWrap(True)
        self._actions.addWidget(self.note_label)
        self._actions.addStretch(1)
        columns.addWidget(actions_box, stretch=45)
        layout.addLayout(columns)
        layout.addStretch(1)
        self.show_file(None)

    def show_file(
        self, item: PendingFile | None, *, can_decide: bool = False, session_open: bool = True
    ) -> None:
        """Display one waiting file and exactly the service's options for it."""
        options = item.options if item is not None else ()
        if item is None:
            self.heading.setText("Select a file from the queue.")
            self.details_label.setText("")
        else:
            self.heading.setText(
                f"<span style='font-size:12pt;'><b>{html.escape(file_state_text(item))}</b>"
                f"</span> &mdash; {html.escape(item.file_name)}"
            )
            self.details_label.setText(file_details_html(item))
        for decision, button in self.buttons.items():
            offered = decision in options
            button.setVisible(offered)
            enabled = offered and can_decide
            if decision is FileDecision.RELEASE and not session_open:
                enabled = False
            button.setEnabled(enabled)
        notes = []
        if item is not None and not can_decide:
            notes.append(
                "Set your reviewer name in File > Settings - a file decision is recorded "
                "against a named operator."
            )
        if item is not None and FileDecision.RELEASE in options and not session_open:
            notes.append(
                "This file arrived for a closed scan session. Reopen the session first to "
                "release it into its results - files never join a closed session by "
                "themselves."
            )
        self.note_label.setText(" ".join(notes))
        self.note_label.setVisible(bool(notes))


class ConfirmSuggestionDialog(QDialog):
    """Confirm a suggested rescan: reason (pre-filled, changeable), note, the named operator.

    Cancel is the default; Enter in the note field does not confirm.
    """

    def __init__(
        self,
        item: QualitySuggestion,
        *,
        reviewer: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("confirmSuggestionDialog")
        self.setWindowTitle("Confirm rescan required")
        layout = QVBoxLayout(self)
        scale_layout(layout, spacing=Spacing.SM)
        intro = QLabel(
            f"Reject <b>{html.escape(item.source_name)}</b> pending a rescan? It stops "
            "counting towards any result until a rescan replaces it. Nothing is deleted, and "
            "it can be undone."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(intro)
        form = QFormLayout()
        self.reason_combo = QComboBox()
        self.reason_combo.setObjectName("confirmSuggestionReason")
        for reason in RESCAN_REASONS:
            self.reason_combo.addItem(reason.label, userData=reason)
        if item.suggested_reason is not None:
            index = self.reason_combo.findData(item.suggested_reason)
            if index >= 0:
                self.reason_combo.setCurrentIndex(index)
        form.addRow("Reason", self.reason_combo)
        self.note_edit = QLineEdit()
        self.note_edit.setObjectName("confirmSuggestionNote")
        self.note_edit.setPlaceholderText("Optional note")
        form.addRow("Note", self.note_edit)
        layout.addLayout(form)
        who = QLabel(f"Recorded against: <b>{html.escape(reviewer)}</b>")
        who.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(who)
        buttons = QDialogButtonBox()
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        confirm = buttons.addButton(
            "Confirm Rescan Required", QDialogButtonBox.ButtonRole.AcceptRole
        )
        confirm.setObjectName("confirmSuggestionAccept")
        confirm.setAutoDefault(False)
        cancel.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        resize_scaled(self, 480, 220)

    def reason(self) -> RejectionReason | None:
        """The chosen rejection reason."""
        value = self.reason_combo.currentData()
        return value if isinstance(value, RejectionReason) else None

    def note(self) -> str:
        """The operator's note."""
        return self.note_edit.text().strip()


__all__ = [
    "POLICY_NOTE",
    "ConfirmSuggestionDialog",
    "FilePanel",
    "SuggestionPanel",
    "file_details_html",
    "file_state_text",
    "reason_text",
    "suggestion_details_html",
    "suggestion_issue",
]
