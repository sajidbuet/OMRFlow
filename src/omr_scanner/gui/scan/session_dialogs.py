"""Dialogs of the operational scan session (0.1.1 revised phase 8).

Purpose:
    The few questions the session controls must ask a person: a source's
    name and folder, whether to cancel queued work, whether to reopen, and -
    the important one - why a session cannot be finished yet.

Rules every dialog here keeps:
    * **It decides nothing.** A dialog collects an answer; the session-mode
      controller calls the service; the service decides. The finish dialog
      renders the typed blockers the service returned and groups them under
      headings for reading - the list itself is the service's, complete.
    * **Safe defaults.** The default button never confirms anything
      irreversible: Cancel is the default and Escape cancels. Enter in a text
      field never closes the session or cancels work.
    * **Bounded size.** Each dialog starts at a size capped to the screen
      (:func:`~omr_scanner.gui.ui_scale.resize_scaled`) and scrolls its
      content, so a long blocker list at 175 % scaling never pushes the
      buttons off the screen.
    * Nothing here is opened from a worker callback: only the controller's
      ``_prompt_*`` methods construct and ``exec()`` them.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
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

from omr_scanner.gui import session_close
from omr_scanner.gui.theme import Color, Spacing
from omr_scanner.gui.ui_scale import add_scaled_spacing, resize_scaled, scale_layout

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.session_finish import FinishBlocker
    from omr_scanner.services.intake import SourceInfo


# ----------------------------------------------------------------------
# Source configuration
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SourceDraft:
    """What the operator typed for a source. Validated by the intake service, not here."""

    label: str
    root_path: str
    recursive: bool = False


class SourceDialog(QDialog):
    """Add a watched scanner folder, or edit one's name and options.

    The folder may be a local path or a network (UNC) path, typed or pasted:
    a share that is unreachable right now is still a valid source - its
    status will say *Unreachable* until it can be listed. The folder of an
    existing source cannot change (every file's provenance is relative to
    it); a different folder is a new source.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        source: SourceInfo | None = None,
        suggested_label: str = "",
        error: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sourceDialog")
        editing = source is not None
        self.setWindowTitle("Edit Source" if editing else "Add Source")
        layout = QVBoxLayout(self)
        scale_layout(layout, spacing=Spacing.SM)

        intro = QLabel(
            "A scanner's output folder. New files in it join this scan session once they "
            "are completely written. Original files are never moved, changed or deleted."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.label_edit = QLineEdit(source.label if source is not None else suggested_label)
        self.label_edit.setObjectName("sourceLabelEdit")
        self.label_edit.setPlaceholderText("Scanner A")
        self.label_edit.setAccessibleName("Source name")
        form.addRow("Name", self.label_edit)

        path_row = QHBoxLayout()
        self.path_edit = QLineEdit(source.root_path if source is not None else "")
        self.path_edit.setObjectName("sourcePathEdit")
        self.path_edit.setPlaceholderText(r"C:\Scans\ScannerA  or  \\scanner-pc\scans")
        self.path_edit.setAccessibleName("Source folder")
        self.path_edit.setToolTip(
            "Type or paste a local folder or a network (UNC) path. An unreachable share is "
            "kept and shown as unreachable until it can be listed."
        )
        self.path_edit.setReadOnly(editing)
        path_row.addWidget(self.path_edit, stretch=1)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.setObjectName("sourceBrowseButton")
        self.browse_button.setEnabled(not editing)
        self.browse_button.clicked.connect(self._prompt_browse)
        path_row.addWidget(self.browse_button)
        form.addRow("Folder", path_row)

        self.recursive_box = QCheckBox("Include files in sub-folders")
        self.recursive_box.setObjectName("sourceRecursiveBox")
        self.recursive_box.setChecked(source.recursive if source is not None else False)
        form.addRow("", self.recursive_box)
        layout.addLayout(form)

        if editing:
            fixed = QLabel(
                "The folder of an existing source cannot be changed - every file's record is "
                "relative to it. Add a new source for a different folder."
            )
            fixed.setWordWrap(True)
            fixed.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
            layout.addWidget(fixed)

        self.error_label = QLabel(error)
        self.error_label.setObjectName("sourceErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {Color.STATUS_ERROR};")
        self.error_label.setVisible(bool(error))
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Save" if editing else "Add Source")
        ok.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        resize_scaled(self, 560, 260)

    def draft(self) -> SourceDraft:
        """What was entered (``root_path`` exactly as typed: UNC paths stay UNC)."""
        return SourceDraft(
            label=self.label_edit.text().strip(),
            root_path=self.path_edit.text().strip(),
            recursive=self.recursive_box.isChecked(),
        )

    def _prompt_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Scanner folder", self.path_edit.text())
        if folder:
            self.path_edit.setText(folder)


# ----------------------------------------------------------------------
# Finish scan session
# ----------------------------------------------------------------------
class FinishChoice(StrEnum):
    """What the operator chose in the finish dialog."""

    CANCEL = "cancel"
    ATTEMPT = "attempt"
    """Run *Finish scan session* (final reconciliation, then every check)."""
    ACCEPT_INCOMPLETE = "accept_incomplete"
    """Run it again, accepting incomplete results by name (only offered when
    every remaining blocker is one that may be accepted)."""
    NAVIGATE = "navigate"
    """Go to where a blocker is cleared (:attr:`FinishSessionDialog.destination`)."""


class FinishSessionDialog(QDialog):
    """Before or after *Finish scan session*: every blocker, grouped, with a way to each.

    Args:
        session_name: Which session.
        blockers: The typed blockers - from the read-only preview, or from a
            refused attempt. Every one is listed.
        attempted: ``True`` when ``blockers`` came from a refused attempt
            (after the final reconciliation), ``False`` for the preview.
        operator: The configured operator name; required to accept
            incomplete results.
        disabled_sources: Disabled sources, named (never treated as checked).

    Signals:
        navigate: a destination key (see
            :data:`~omr_scanner.gui.session_close.DESTINATION_LABELS`).
    """

    navigate = Signal(str)

    def __init__(
        self,
        session_name: str,
        blockers: Sequence[FinishBlocker],
        *,
        attempted: bool,
        operator: str,
        disabled_sources: Sequence[str] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("finishSessionDialog")
        self.setWindowTitle("Finish Scan Session")
        self.choice = FinishChoice.CANCEL
        self.destination = ""
        self.blockers = tuple(blockers)
        acknowledgeable = bool(self.blockers) and all(
            item.acknowledgeable for item in self.blockers
        )
        named = bool(operator.strip())

        layout = QVBoxLayout(self)
        scale_layout(layout, spacing=Spacing.SM)
        if not self.blockers:
            heading = (
                f"Finish '{session_name}'? Every enabled source gets a final check first. If "
                "nothing blocks it, the session closes: its batches are sealed, it accepts no "
                "new scans, and its results become final."
            )
        elif attempted:
            heading = (
                f"'{session_name}' was not closed. After a final check of its sources, these "
                "remain:"
            )
        else:
            heading = (
                f"'{session_name}' cannot be closed yet. As stored now (the sources are "
                "checked again when you try):"
            )
        self.heading_label = QLabel(heading)
        self.heading_label.setObjectName("finishHeadingLabel")
        self.heading_label.setWordWrap(True)
        layout.addWidget(self.heading_label)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        scale_layout(body_layout, margins=(0, 0, 0, 0), spacing=Spacing.XS)
        self.group_labels: dict[str, QLabel] = {}
        self.navigation_buttons: dict[str, QPushButton] = {}
        for group, items in session_close.grouped_blockers(self.blockers):
            title = QLabel(f"<b>{html.escape(group)}</b>")
            title.setObjectName(f"finishGroup_{group.replace(' ', '_')}")
            body_layout.addWidget(title)
            lines = "".join(
                f"<li>{html.escape(session_close.blocker_message(item))}"
                + (" <i>(may be accepted as incomplete)</i>" if item.acknowledgeable else "")
                + "</li>"
                for item in items
            )
            text = QLabel(f"<ul style='margin-left:12px;'>{lines}</ul>")
            text.setWordWrap(True)
            text.setTextFormat(Qt.TextFormat.RichText)
            body_layout.addWidget(text)
            self.group_labels[group] = text
            for destination in dict.fromkeys(
                session_close.destination_of(item) for item in items
            ):
                if destination is None or destination in self.navigation_buttons:
                    continue
                button = QPushButton(session_close.DESTINATION_LABELS[destination])
                button.setObjectName(f"finishNavigate_{destination.replace(':', '_')}")
                button.setAutoDefault(False)
                button.clicked.connect(
                    lambda _checked=False, target=destination: self.choose_navigation(target)
                )
                row = QHBoxLayout()
                add_scaled_spacing(row, Spacing.MD)
                row.addWidget(button)
                row.addStretch(1)
                body_layout.addLayout(row)
                self.navigation_buttons[destination] = button
        if disabled_sources:
            note = QLabel(
                "Disabled sources are not checked: " + ", ".join(disabled_sources) + "."
            )
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
            body_layout.addWidget(note)
        body_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("finishBlockersScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        scroll.setVisible(bool(self.blockers) or bool(disabled_sources))
        layout.addWidget(scroll, stretch=1)

        self.accept_box = QCheckBox(
            "Close with incomplete results - the rejected sheets above will have no result"
        )
        self.accept_box.setObjectName("acceptIncompleteBox")
        self.accept_box.setVisible(attempted and acknowledgeable)
        self.accept_box.setEnabled(named)
        layout.addWidget(self.accept_box)
        self.operator_label = QLabel(
            f"Recorded in the audit history against: <b>{html.escape(operator.strip())}</b>"
            if named
            else (
                f"<span style='color:{Color.STATUS_ERROR};'>Set your operator name in "
                "File &gt; Settings - closing with incomplete results is recorded against a "
                "named operator.</span>"
            )
        )
        self.operator_label.setObjectName("finishOperatorLabel")
        self.operator_label.setWordWrap(True)
        self.operator_label.setVisible(attempted and acknowledgeable)
        layout.addWidget(self.operator_label)

        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setObjectName("finishCancelButton")
        self.attempt_button = buttons.addButton(
            "Check Again" if attempted and self.blockers else "Finish Scan Session",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.attempt_button.setObjectName("finishAttemptButton")
        self.attempt_button.setToolTip(
            "Run the final check of every source and every blocker again, fresh."
        )
        self.attempt_button.setEnabled(named)
        self.attempt_button.clicked.connect(self.choose_attempt)
        self.incomplete_button = buttons.addButton(
            "Close With Incomplete Results", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.incomplete_button.setObjectName("finishIncompleteButton")
        self.incomplete_button.setVisible(attempted and acknowledgeable)
        self.incomplete_button.setEnabled(False)
        self.incomplete_button.clicked.connect(self.choose_accept_incomplete)
        self.accept_box.toggled.connect(self.incomplete_button.setEnabled)
        for button in (self.attempt_button, self.incomplete_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)
        if not named:
            warn = QLabel(
                f"<span style='color:{Color.STATUS_ERROR};'>Finishing a scan session is "
                "recorded against a named operator. Set your name in File &gt; Settings."
                "</span>"
            )
            warn.setObjectName("finishNoOperatorLabel")
            warn.setWordWrap(True)
            layout.addWidget(warn)
        resize_scaled(self, 600, 420 if self.blockers else 220)

    def choose_attempt(self) -> None:
        """Attempt the finish (fresh final reconciliation and checks)."""
        self.choice = FinishChoice.ATTEMPT
        self.accept()

    def choose_accept_incomplete(self) -> None:
        """Attempt it again, accepting incomplete results - only when ticked."""
        if not self.accept_box.isChecked():
            return
        self.choice = FinishChoice.ACCEPT_INCOMPLETE
        self.accept()

    def choose_navigation(self, destination: str) -> None:
        """Close the dialog and go to where ``destination`` is cleared."""
        self.choice = FinishChoice.NAVIGATE
        self.destination = destination
        self.navigate.emit(destination)
        self.accept()


__all__ = ["FinishChoice", "FinishSessionDialog", "SourceDialog", "SourceDraft"]
