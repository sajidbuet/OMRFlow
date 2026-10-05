"""Reject & Rescan on the Resolve stage: the dialog, the case panel, the purge.

Purpose:
    The three pieces of interface Reject & Rescan needs beside the ordinary
    conflict workspace:

    * :class:`RejectScanDialog` - a compact, focused question: *which* scan,
      why it is unusable, and - optionally - the identity an operator can read
      off the paper.
    * :class:`RescanPanel` - what stands in the decision area while a case
      from the *Rejected / Rescan* queue is selected: its state in words, its
      history in brief, possible rescans, and the actions that apply.
    * :class:`PurgeRejectsDialog` - the project-level maintenance operation.

What does NOT belong here:
    * Any lifecycle rule. Everything these widgets offer is decided by
      :mod:`omr_scanner.services.scan_lifecycle`, which refuses what is not
      allowed whatever a button says; a widget here only asks and displays.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.scan_lifecycle import (
    RESCAN_REASONS,
    FileState,
    LifecycleState,
    PurgeMode,
    RejectionReason,
    format_bytes,
)
from omr_scanner.gui.review.display_names import middle_ellipsis
from omr_scanner.gui.theme import (
    VARIANT_DESTRUCTIVE,
    VARIANT_PRIMARY,
    VARIANT_PROPERTY,
    Color,
    Spacing,
)
from omr_scanner.gui.ui_scale import resize_scaled, scale_layout, scale_widget
from omr_scanner.services.set_identity import same_set

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from omr_scanner.domain.scan_lifecycle import (
        PurgePlan,
        ReplacementCandidate,
        RescanCase,
    )
    from omr_scanner.services.scan_lifecycle import LifecycleEvent, ProcessedSheet

UNKNOWN_SET = "(not known)"
"""The set-code choice meaning "the operator does not know"."""


# ----------------------------------------------------------------------
# Reject dialog
# ----------------------------------------------------------------------
class RejectScanDialog(QDialog):
    """Ask, for one named scan, why it is unusable.

    Identity is **optional**. A sheet folded through its roll-number grid may
    have no readable Student ID at all, and refusing to reject it until one is
    typed would push the operator into inventing one. What they can read off
    the paper they may give; it identifies this case only.

    Args:
        scan_name: The scan's file name - the unambiguous identity shown.
        recognised_id: Its current effective Student ID, or ``""``.
        recognised_set: Its current effective set code, or ``""``.
        set_codes: The project's defined set codes, offered for the optional
            set; empty when the project defines none.
        parent: Optional Qt parent.
        set_labels: How each code is shown - ``"Set 10 (A on sheet)"`` for a
            set printed as another mark. The value chosen is always the
            logical code.
    """

    def __init__(
        self,
        scan_name: str,
        recognised_id: str,
        recognised_set: str,
        set_codes: Sequence[str] = (),
        parent: QWidget | None = None,
        set_labels: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("rejectScanDialog")
        self.setWindowTitle("Reject scan - rescan required")
        scale_widget(self, minimum_width=460)
        layout = QVBoxLayout(self)
        scale_layout(layout, spacing=Spacing.SM)

        intro = QLabel(
            f"<b>{html.escape(scan_name or '(unnamed scan)')}</b><br>"
            f"Recognised Student ID: <b>{html.escape(recognised_id or '(not read)')}</b>"
            f" &middot; Set code: <b>{html.escape(recognised_set or '(not read)')}</b>"
        )
        intro.setObjectName("rejectScanIdentity")
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        explain = QLabel(
            "The scan stops counting towards any result at once. Nothing is "
            "deleted: the image, what was read and every decision on it are "
            "kept, and the rejection can be undone until a rescan is confirmed."
        )
        explain.setWordWrap(True)
        explain.setStyleSheet(f"color: {Color.TEXT_TERTIARY};")
        layout.addWidget(explain)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.reason_combo = QComboBox()
        self.reason_combo.setObjectName("rejectReasonCombo")
        for reason in RESCAN_REASONS:
            self.reason_combo.addItem(reason.label, userData=reason.value)
        form.addRow("Reason", self.reason_combo)

        self.note_edit = QLineEdit()
        self.note_edit.setObjectName("rejectNoteEdit")
        self.note_edit.setPlaceholderText("Optional; required for 'Other'")
        form.addRow("Note", self.note_edit)

        self.identity_edit = QLineEdit()
        self.identity_edit.setObjectName("rejectDeclaredIdEdit")
        self.identity_edit.setPlaceholderText(
            "Optional - the Student ID written on the paper, if readable"
        )
        self.identity_edit.setToolTip(
            "Identifies this rejected case and helps find its rescan. It is not "
            "recorded as the scan's Student ID."
        )
        form.addRow("Student ID on paper", self.identity_edit)

        self.set_combo = QComboBox()
        self.set_combo.setObjectName("rejectDeclaredSetCombo")
        self.set_combo.addItem(UNKNOWN_SET, userData="")
        labels = set_labels or {}
        for code in set_codes:
            self.set_combo.addItem(labels.get(code, f"Set {code}"), userData=code)
        # The recognised set is matched through set identity: "a" selects Set A.
        matching = next(
            (code for code in set_codes if recognised_set and same_set(code, recognised_set)),
            None,
        )
        if matching is not None:
            self.set_combo.setCurrentIndex(self.set_combo.findData(matching))
        form.addRow("Set on paper", self.set_combo)
        layout.addLayout(form)

        self.problem_label = QLabel("")
        self.problem_label.setObjectName("rejectProblemLabel")
        self.problem_label.setStyleSheet(f"color: {Color.DESTRUCTIVE};")
        self.problem_label.setWordWrap(True)
        layout.addWidget(self.problem_label)

        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.reject_button = buttons.addButton(
            "Reject - rescan required", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.reject_button.setObjectName("confirmRejectButton")
        self.reject_button.setProperty(VARIANT_PROPERTY, VARIANT_DESTRUCTIVE)
        # Cancel is the default: Enter must never reject a sheet by accident.
        self.cancel_button.setDefault(True)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_reason(self) -> RejectionReason:
        """The reason the combo shows."""
        return RejectionReason(self.reason_combo.currentData())

    def values(self) -> tuple[RejectionReason, str, str, str]:
        """``(reason, note, declared Student ID, declared set code)``."""
        return (
            self.selected_reason(),
            self.note_edit.text().strip(),
            self.identity_edit.text().strip(),
            str(self.set_combo.currentData() or ""),
        )

    def _accept_if_valid(self) -> None:
        reason, note, _identity, _set = self.values()
        if reason.requires_note and not note:
            self.problem_label.setText("Describe why the scan is unusable when choosing 'Other'.")
            return
        self.accept()


# ----------------------------------------------------------------------
# The case panel
# ----------------------------------------------------------------------
class RescanPanel(QWidget):
    """What the Resolve workspace shows for one Reject & Rescan case.

    Signals:
        use_replacement: ``int`` scan id the operator chose as the rescan.
        undo_requested: Undo the rejection.
        unlink_requested: Remove a confirmed replacement link.
        import_requested: Read a rescan file into this batch.
        compare_toggled: ``bool`` - show the replacement (``True``) or the
            original (``False``) in the image view.
        history_requested: Show the case's lifecycle history.
    """

    use_replacement = Signal(int)
    undo_requested = Signal()
    unlink_requested = Signal()
    import_requested = Signal()
    compare_toggled = Signal(bool)
    history_requested = Signal()
    search_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rescanPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Scrolled, like the machine evidence: this panel's height is set by
        # the splitter, and at 1366x768 every control must stay reachable.
        scroll = QScrollArea()
        scroll.setObjectName("rescanPanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        layout = QVBoxLayout(body)
        scale_layout(
            layout,
            margins=(Spacing.SM, Spacing.XS, Spacing.SM, Spacing.XS),
            spacing=Spacing.XS,
        )
        scroll.setWidget(body)
        outer.addWidget(scroll)

        self.state_label = QLabel("")
        self.state_label.setObjectName("rescanStateLabel")
        self.state_label.setTextFormat(Qt.TextFormat.RichText)
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.details_label = QLabel("")
        self.details_label.setObjectName("rescanDetailsLabel")
        self.details_label.setTextFormat(Qt.TextFormat.RichText)
        self.details_label.setWordWrap(True)
        self.details_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        # Two columns, like the conflict decision panel: what is recorded on
        # the left, what can be done on the right - so the candidates and the
        # actions are on screen at 1366x768 without scrolling past the record.
        columns = QHBoxLayout()
        scale_layout(columns, spacing=Spacing.MD)
        columns.addWidget(self.details_label, stretch=37, alignment=Qt.AlignmentFlag.AlignTop)
        actions_box = QWidget()
        actions = QVBoxLayout(actions_box)
        actions.setContentsMargins(0, 0, 0, 0)
        scale_layout(actions, spacing=Spacing.XS)
        columns.addWidget(actions_box, stretch=63)
        layout.addLayout(columns)

        self.candidates_heading = QLabel("POSSIBLE RESCAN OF REJECTED SHEET")
        self.candidates_heading.setObjectName("resolveSectionHeading")
        actions.addWidget(self.candidates_heading)
        self.candidates_list = QListWidget()
        self.candidates_list.setObjectName("rescanCandidatesList")
        self.candidates_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        scale_widget(self.candidates_list, minimum_height=40, maximum_height=64)
        # Wrapped, never scrolled sideways: the batch a rescan came from ends
        # the line, and at 1366 px it would otherwise be out of sight.
        self.candidates_list.setWordWrap(True)
        self.candidates_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.candidates_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.candidates_list.itemSelectionChanged.connect(self._refresh_use_button)
        actions.addWidget(self.candidates_list)

        self.show_all_button = QPushButton("Choose from all scans...")
        self.show_all_button.setObjectName("rescanShowAllButton")
        self.show_all_button.setCheckable(True)
        self.show_all_button.setToolTip(
            "For a sheet whose Student ID could not be read: search every read "
            "scan in the project - any batch - and link the rescan by hand."
        )
        # Filtered in SQL as the operator types a file name or Student ID, and
        # capped: a project of a hundred thousand scans never fills a widget.
        self.association_search = QLineEdit()
        self.association_search.setObjectName("rescanAssociationSearch")
        self.association_search.setPlaceholderText(
            "Search all scans in this project by file name or Student ID..."
        )
        self.association_search.setClearButtonEnabled(True)
        self.association_search.setVisible(False)
        self.association_search.editingFinished.connect(
            lambda: self.search_changed.emit(self.association_search.text())
        )
        self.show_all_button.toggled.connect(self.association_search.setVisible)
        actions.addWidget(self.association_search)

        row = QHBoxLayout()
        scale_layout(row, spacing=Spacing.SM)
        self.use_button = QPushButton("Use as replacement")
        self.use_button.setObjectName("useAsReplacementButton")
        self.use_button.setProperty(VARIANT_PROPERTY, VARIANT_PRIMARY)
        self.use_button.setToolTip(
            "Confirm that the selected scan is this sheet's rescan. The rejected "
            "scan stays rejected and is kept for the record."
        )
        self.use_button.clicked.connect(self._emit_use)
        row.addWidget(self.use_button)
        self.import_button = QPushButton("Import rescan...")
        self.import_button.setObjectName("importRescanButton")
        self.import_button.setToolTip(
            "Read a rescanned image into this batch on the Scan stage. It is "
            "not linked until you confirm it here."
        )
        self.import_button.clicked.connect(self.import_requested)
        row.addWidget(self.import_button)
        row.addWidget(self.show_all_button)
        row.addStretch(1)
        actions.addLayout(row)

        row2 = QHBoxLayout()
        scale_layout(row2, spacing=Spacing.SM)
        self.compare_button = QPushButton("Show replacement")
        self.compare_button.setObjectName("compareRescanButton")
        self.compare_button.setCheckable(True)
        self.compare_button.setToolTip(
            "Switch the image between the rejected original and its rescan"
        )
        self.compare_button.toggled.connect(self._on_compare)
        row2.addWidget(self.compare_button)
        self.undo_button = QPushButton("Undo Reject")
        self.undo_button.setObjectName("undoRejectButton")
        self.undo_button.clicked.connect(self.undo_requested)
        row2.addWidget(self.undo_button)
        self.unlink_button = QPushButton("Remove replacement link...")
        self.unlink_button.setObjectName("removeReplacementButton")
        self.unlink_button.clicked.connect(self.unlink_requested)
        row2.addWidget(self.unlink_button)
        self.history_button = QPushButton("History...")
        self.history_button.setObjectName("rescanHistoryButton")
        self.history_button.clicked.connect(self.history_requested)
        row2.addWidget(self.history_button)
        row2.addStretch(1)
        actions.addLayout(row2)
        # Said in words beside the disabled actions, not left to a tooltip.
        self.unavailable_label = QLabel("")
        self.unavailable_label.setObjectName("rescanImageUnavailableLabel")
        self.unavailable_label.setWordWrap(True)
        self.unavailable_label.setVisible(False)
        actions.addWidget(self.unavailable_label)
        actions.addStretch(1)
        layout.addStretch(1)

        self._case: RescanCase | None = None
        self._can_decide = False

    # ------------------------------------------------------------------
    def show_case(
        self,
        case: RescanCase | None,
        candidates: Sequence[ReplacementCandidate] = (),
        *,
        can_decide: bool = False,
    ) -> None:
        """Display one case, or the empty state."""
        self._case = case
        self.candidates_list.clear()
        self.compare_button.blockSignals(True)
        self.compare_button.setChecked(False)
        self.compare_button.setText("Show replacement")
        self.compare_button.blockSignals(False)
        if case is None:
            self.state_label.setText("Select a rejected sheet from the queue.")
            self.details_label.setText("")
            for button in (
                self.use_button, self.import_button, self.undo_button,
                self.unlink_button, self.compare_button, self.history_button,
            ):
                button.setEnabled(False)
            self.show_all_button.setEnabled(False)
            self.unavailable_label.setVisible(False)
            return

        self.state_label.setText(state_html(case))
        self.details_label.setText(details_html(case))
        outstanding = case.state is LifecycleState.REJECTED_PENDING_RESCAN
        superseded = case.state is LifecycleState.SUPERSEDED_BY_REPLACEMENT
        self.candidates_heading.setVisible(outstanding)
        self.candidates_list.setVisible(outstanding)
        self.show_all_button.setVisible(outstanding)
        self.show_all_button.setEnabled(outstanding)
        self.use_button.setVisible(outstanding)
        self.import_button.setVisible(outstanding)
        if outstanding:
            if not candidates:
                placeholder = QListWidgetItem(
                    "No scan with this Student ID has been read yet."
                    if case.identity
                    else "Student ID unknown - show every scan to link one by hand."
                )
                placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
                self.candidates_list.addItem(placeholder)
            for candidate in candidates:
                item = QListWidgetItem(candidate_text(case, candidate))
                item.setData(Qt.ItemDataRole.UserRole, candidate.scan_id)
                item.setToolTip(candidate_tooltip(candidate))
                self.candidates_list.addItem(item)
        self.import_button.setEnabled(can_decide and outstanding)
        self.undo_button.setVisible(outstanding)
        self.undo_button.setEnabled(
            can_decide and outstanding and case.file_state is FileState.PRESENT
        )
        self.unlink_button.setVisible(superseded)
        self.unlink_button.setEnabled(
            can_decide and superseded and case.file_state is FileState.PRESENT
        )
        note = case.file_state.unavailable_note
        self.unlink_button.setToolTip(
            note
            or "Withdraw a mistaken link. The original goes back to awaiting a "
            "rescan; it is not reactivated."
        )
        self.unavailable_label.setText(note)
        self.unavailable_label.setVisible(bool(note))
        self.compare_button.setVisible(superseded)
        self.compare_button.setEnabled(superseded)
        self.history_button.setEnabled(True)
        self._can_decide = can_decide
        self._refresh_use_button()

    def selected_candidate(self) -> int | None:
        """The scan id of the selected candidate row, if any."""
        items = self.candidates_list.selectedItems()
        if not items:
            return None
        value = items[0].data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def select_candidate(self, scan_id: int) -> bool:
        """Select the candidate row for ``scan_id``."""
        for index in range(self.candidates_list.count()):
            item = self.candidates_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == scan_id:
                self.candidates_list.setCurrentItem(item)
                return True
        return False

    def _refresh_use_button(self) -> None:
        case = self._case
        self.use_button.setEnabled(
            self._can_decide
            and case is not None
            and case.state is LifecycleState.REJECTED_PENDING_RESCAN
            and self.selected_candidate() is not None
        )

    def _emit_use(self) -> None:
        chosen = self.selected_candidate()
        if chosen is not None:
            self.use_replacement.emit(chosen)

    def _on_compare(self, checked: bool) -> None:
        self.compare_button.setText("Show original" if checked else "Show replacement")
        self.compare_toggled.emit(checked)


def state_html(case: RescanCase) -> str:
    """The case's state as a heading: words and a glyph, never colour alone."""
    colour = (
        Color.DESTRUCTIVE
        if case.state is LifecycleState.REJECTED_PENDING_RESCAN
        else Color.TEXT_TERTIARY
    )
    return (
        f"<span style='font-size:12pt;color:{colour};'><b>{case.state.marker} "
        f"{html.escape(case.state.label)}</b></span>"
    )


def details_html(case: RescanCase) -> str:
    """Everything recorded about the case, in labelled lines."""
    rows = [
        ("Original scan", middle_ellipsis(case.source_name or f"scan {case.scan_id}")),
        ("Student ID", case.identity or "(not known)"),
        ("Set code", case.set_code or "(not known)"),
        ("Reason", case.reason_label or "-"),
        (
            "Rejected",
            f"{case.rejected_at:%Y-%m-%d %H:%M} by {case.rejected_by or '-'}"
            if case.rejected_at
            else "-",
        ),
    ]
    if case.declared_candidate_id or case.declared_set_code:
        rows.append(
            (
                "Case identity",
                "given by the operator for this case only - not the scan's "
                "Student ID",
            )
        )
    if case.state is LifecycleState.SUPERSEDED_BY_REPLACEMENT:
        elsewhere = (
            f" (read in batch {case.replacement_batch_id[:8]})"
            if case.replacement_batch_id and case.replacement_batch_id != case.batch_id
            else ""
        )
        rows.append(
            (
                "Replacement",
                f"{middle_ellipsis(case.replacement_name or str(case.replacement_scan_id))}"
                f"{elsewhere} - confirmed "
                f"{case.replaced_at:%Y-%m-%d %H:%M} by {case.replaced_by or '-'}"
                if case.replaced_at
                else middle_ellipsis(str(case.replacement_name or case.replacement_scan_id)),
            )
        )
    rows.append(
        (
            "Image",
            case.file_state.label
            + (f" on {case.file_action_at:%Y-%m-%d}" if case.file_action_at else ""),
        )
    )
    body = "".join(
        f"<tr><td style='color:{Color.TEXT_TERTIARY};padding-right:10px;'>{name}</td>"
        f"<td>{html.escape(str(value))}</td></tr>"
        for name, value in rows
    )
    return f"<table>{body}</table>"


def candidate_text(case: RescanCase, candidate: ReplacementCandidate) -> str:
    """One list row: the brief's *Possible rescan of rejected sheet*.

    A candidate read into another batch says so, and which one, so the
    operator knows where it came from. The batch is never evidence either
    way - only the Student ID is.
    """
    where = f" · {candidate.batch_label}" if candidate.other_batch else ""
    # Which scanner, and whether it arrived after the rejection (revised
    # phase 8): provenance to rank by eye - never certainty, never a link.
    if candidate.provenance:
        where += f" · {candidate.provenance}"
    if candidate.candidate_id == case.identity and case.identity:
        return (
            f"Possible rescan: {candidate.source_name} · Student ID "
            f"{candidate.candidate_id} · original {case.source_name}"
            + (
                " · set code matches"
                if candidate.set_code_agrees
                else " · set code differs"
                if candidate.set_code_agrees is False
                else ""
            )
            + where
        )
    return (
        f"{candidate.source_name} · read as {candidate.candidate_id or '(not read)'}"
        f" · set {candidate.set_code or '?'}{where}"
    )


def candidate_tooltip(candidate: ReplacementCandidate) -> str:
    """Everything known about a candidate, for its tooltip."""
    lines = [candidate.evidence] if candidate.candidate_id else []
    lines.append(
        f"Read in {candidate.batch_label or 'this batch'}"
        + (" - a different batch from the rejected sheet" if candidate.other_batch else "")
    )
    if candidate.read_at is not None:
        lines.append(f"Read at {candidate.read_at:%Y-%m-%d %H:%M}")
    if candidate.arrived_at is not None:
        lines.append(f"Arrived at {candidate.arrived_at.astimezone():%Y-%m-%d %H:%M}")
    if candidate.provenance:
        lines.append(candidate.provenance.capitalize())
    lines.append("A suggestion ranked by Student ID, set and arrival - not a certainty.")
    return "\n".join(lines)


def render_lifecycle_history(case: RescanCase, events: Sequence[LifecycleEvent]) -> str:
    """A case's lifecycle ledger as rich text. Pure, for a test to read."""
    lines = [
        f"<b>{html.escape(case.source_name or f'scan {case.scan_id}')}</b> - "
        f"{html.escape(case.state.label)}<br>",
    ]
    if not events:
        lines.append("<i>No lifecycle events recorded.</i>")
    for event in events:
        lines.append(
            f"<br><b>{html.escape(event.action.label)}</b> &middot; "
            f"{event.occurred_at:%Y-%m-%d %H:%M:%S} &middot; "
            f"{html.escape(event.reviewer or 'OMRFlow')}<br>"
        )
        if event.reason_text:
            lines.append(f"Note: {html.escape(event.reason_text)}<br>")
        if event.detail:
            lines.append(f"<i>{html.escape(event.detail)}</i><br>")
    return "".join(lines)


class LifecycleHistoryDialog(QDialog):
    """Every lifecycle event of one case, oldest first."""

    def __init__(
        self, case: RescanCase, events: Sequence[LifecycleEvent], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("lifecycleHistoryDialog")
        self.setWindowTitle("Rejection history")
        resize_scaled(self, 560, 420)
        layout = QVBoxLayout(self)
        label = QLabel(render_lifecycle_history(case, events))
        label.setObjectName("lifecycleHistoryText")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(label)
        layout.addWidget(scroll)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


# ----------------------------------------------------------------------
# Inspecting any processed sheet
# ----------------------------------------------------------------------
class SheetPanel(QWidget):
    """What the workspace shows for one sheet of *All processed sheets*.

    The way to a sheet recognition had no doubt about. It states what the
    sheet reads as and where it stands, and offers the ordinary *Reject /
    Rescan…* - the same dialog, the same service, the same rules as rejecting
    from a conflict.

    Signals:
        reject_requested: The operator asked to reject this sheet.
    """

    reject_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sheetInspectPanel")
        layout = QVBoxLayout(self)
        scale_layout(
            layout,
            margins=(Spacing.SM, Spacing.XS, Spacing.SM, Spacing.XS),
            spacing=Spacing.XS,
        )
        self.state_label = QLabel("Select a sheet from the list.")
        self.state_label.setObjectName("sheetStateLabel")
        self.state_label.setTextFormat(Qt.TextFormat.RichText)
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)
        self.details_label = QLabel("")
        self.details_label.setObjectName("sheetDetailsLabel")
        self.details_label.setTextFormat(Qt.TextFormat.RichText)
        self.details_label.setWordWrap(True)
        layout.addWidget(self.details_label)
        row = QHBoxLayout()
        self.reject_button = QPushButton("Reject / Rescan...")
        self.reject_button.setObjectName("sheetRejectButton")
        self.reject_button.setProperty(VARIANT_PROPERTY, VARIANT_DESTRUCTIVE)
        self.reject_button.setToolTip(
            "This sheet is unusable although it was read - reject it pending a "
            "rescan (R). Nothing is deleted."
        )
        self.reject_button.clicked.connect(self.reject_requested)
        row.addWidget(self.reject_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.note_label = QLabel(
            "Every read sheet of the batch is listed here, whether or not "
            "recognition raised anything. Nothing here counts as unresolved."
        )
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet(f"color: {Color.TEXT_TERTIARY};")
        layout.addWidget(self.note_label)
        layout.addStretch(1)

    def show_sheet(
        self,
        sheet: ProcessedSheet | None,
        *,
        effective_id: str = "",
        effective_set: str = "",
        can_decide: bool = False,
    ) -> None:
        """Display one sheet, or the empty state."""
        if sheet is None:
            self.state_label.setText("Select a sheet from the list.")
            self.details_label.setText("")
            self.reject_button.setEnabled(False)
            return
        state = sheet.state
        colour = Color.DESTRUCTIVE if state.is_outstanding else Color.TEXT_PRIMARY
        self.state_label.setText(
            f"<span style='font-size:12pt;color:{colour};'><b>{state.marker} "
            f"{html.escape(state.label)}</b></span>"
        )
        conflicts = (
            f"{sheet.open_conflicts} awaiting a decision"
            if sheet.open_conflicts
            else "none awaiting a decision"
        )
        rows = (
            ("Sheet", middle_ellipsis(sheet.filename or f"scan {sheet.scan_id}")),
            ("Student ID", f"{effective_id or '(not read)'} (read as {sheet.identifier or '-'})"),
            ("Set code", f"{effective_set or '(not read)'} (read as {sheet.set_code or '-'})"),
            ("Processing", sheet.status),
            ("Resolve records", conflicts),
        )
        self.details_label.setText(
            "<table>"
            + "".join(
                f"<tr><td style='color:{Color.TEXT_TERTIARY};padding-right:10px;'>"
                f"{name}</td><td>{html.escape(value)}</td></tr>"
                for name, value in rows
            )
            + "</table>"
        )
        self.reject_button.setEnabled(can_decide and state.is_result_eligible)
        self.reject_button.setToolTip(
            "This sheet is unusable although it was read - reject it pending a "
            "rescan (R). Nothing is deleted."
            if state.is_result_eligible
            else "Already rejected - see Rejected / Rescan."
        )


# ----------------------------------------------------------------------
# Purge Rejects
# ----------------------------------------------------------------------
PURGE_COLUMNS = ("Original", "Student ID", "Replaced by", "Files", "Size", "Left in place")


def purge_summary(plan: PurgePlan) -> str:
    """The summary line the dialog opens with."""
    lines = [
        f"<b>{len(plan.eligible)}</b> superseded rejected original(s) eligible",
        f"<b>{format_bytes(plan.size_bytes)}</b> potentially freed "
        f"({plan.removable_files} project-owned file(s))",
        f"<b>{plan.pending}</b> rejected sheet(s) still awaiting rescan - "
        "<b>will not be removed</b>",
    ]
    if plan.blocked:
        lines.append(f"<b>{len(plan.blocked)}</b> superseded original(s) not eligible")
    return "<br>".join(lines)


class PurgeRejectsDialog(QDialog):
    """Show what *Purge Rejects* would do, then do it on request.

    Nothing happens until :meth:`run` - the button, or a test - and the
    service re-plans at that moment rather than trusting what is displayed.
    """

    purge_requested = Signal(str)

    def __init__(self, plan: PurgePlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("purgeRejectsDialog")
        self.setWindowTitle("Purge Rejects")
        resize_scaled(self, 760, 460)
        layout = QVBoxLayout(self)
        scale_layout(layout, spacing=Spacing.SM)

        self.summary_label = QLabel(purge_summary(plan))
        self.summary_label.setObjectName("purgeSummaryLabel")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        note = QLabel(
            "Only an original whose rescan has been confirmed is eligible, and "
            "only files inside this project's own scan folders are touched. "
            "Scans read in place from elsewhere are never removed. The database "
            "record, the rejection and its history are always kept."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {Color.TEXT_TERTIARY};")
        layout.addWidget(note)

        self.table = QTableWidget(len(plan.eligible), len(PURGE_COLUMNS))
        self.table.setObjectName("purgeItemsTable")
        self.table.setHorizontalHeaderLabels(list(PURGE_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        for row, item in enumerate(plan.eligible):
            values = (
                item.case.source_name,
                item.case.identity or "(not known)",
                item.case.replacement_name or str(item.case.replacement_scan_id or ""),
                str(len(item.files)),
                format_bytes(item.size_bytes),
                "; ".join(item.skipped),
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                cell.setToolTip(text)
                self.table.setItem(row, column, cell)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.table, stretch=1)

        modes = QHBoxLayout()
        self.quarantine_radio = QRadioButton("Move to project quarantine (recommended)")
        self.quarantine_radio.setObjectName("purgeQuarantineRadio")
        self.quarantine_radio.setChecked(True)
        self.delete_radio = QRadioButton("Delete permanently")
        self.delete_radio.setObjectName("purgeDeleteRadio")
        group = QButtonGroup(self)
        group.addButton(self.quarantine_radio)
        group.addButton(self.delete_radio)
        modes.addWidget(self.quarantine_radio)
        modes.addWidget(self.delete_radio)
        modes.addStretch(1)
        layout.addLayout(modes)

        self.result_label = QLabel("")
        self.result_label.setObjectName("purgeResultLabel")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        buttons = QDialogButtonBox()
        self.close_button = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.purge_button = buttons.addButton("Purge", QDialogButtonBox.ButtonRole.ActionRole)
        self.purge_button.setObjectName("runPurgeButton")
        self.purge_button.setProperty(VARIANT_PROPERTY, VARIANT_DESTRUCTIVE)
        self.purge_button.setEnabled(plan.removable_files > 0)
        self.close_button.setDefault(True)
        self.purge_button.clicked.connect(self._on_purge)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_mode(self) -> PurgeMode:
        """The mode the radio buttons choose."""
        return PurgeMode.DELETE if self.delete_radio.isChecked() else PurgeMode.QUARANTINE

    def _on_purge(self) -> None:
        self.purge_requested.emit(self.selected_mode().value)


__all__ = [
    "LifecycleHistoryDialog",
    "PurgeRejectsDialog",
    "RejectScanDialog",
    "RescanPanel",
    "SheetPanel",
    "candidate_text",
    "candidate_tooltip",
    "details_html",
    "purge_summary",
    "render_lifecycle_history",
    "state_html",
]
