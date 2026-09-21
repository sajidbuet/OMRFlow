"""Reconcile scanned scripts against the registered candidates (Phase 7).

The stage where an examination office satisfies itself that every script
belongs to somebody and everybody who sat the paper handed one in.

Shape of the page:

    ┌───────────────────────────────────────────────────────────────┐
    │ Exam: <exam name>                                             │
    │ one row per defined Set: file · candidates · template · state │
    │ Choose/Replace Attendance File · Sample · Reconcile           │
    ├───────────────────────────────────────────────────────────────┤
    │ summary: registered / present / absent / scripts / exceptions │
    ├──────────────────────────────┬────────────────────────────────┤
    │ reconciliation table         │ detail: this entry's scripts,  │
    │ (filtered and paged in SQL)  │ its history, and what to do    │
    └──────────────────────────────┴────────────────────────────────┘

Three rules the page is arranged around:

* **Attendance belongs to a Set, and the pairing is unmistakable.** Each
  defined Set has its own row, its own attendance file and its own candidate
  list; selecting a Set is what decides whose reconciliation is below. One
  Set's workbook is never offered to another, and a Set without one says so
  plainly rather than quietly reconciling against a neighbour's list (§3,
  §15).
* **Every problem is visible.** An entry shows *all* its issues, not just the
  headline, so an absent candidate with two scripts does not have one fact hide
  the other.
* **No destructive shortcut exists.** There is no button that drops a script,
  picks a duplicate, or edits the imported roster. Everything either records a
  decision beside the original data or does nothing.

A project with no Sets defined yet still works: attendance is then *unscoped*
(one list for the project, ``set_id`` NULL), exactly as it was before Sets
existed, and the page points at *File > Project Configuration...* for an
operator who wants per-Set attendance. That is the same distinction the
database draws, not a special case invented here - see
:func:`omr_scanner.services.reconciliation_store.active_roster`.

Candidate names and IDs are shown here - reconciliation would be impossible
otherwise - and are never written to a log line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.reconciliation import (
    AttendanceSource,
    AttendanceState,
    ReconciliationEntry,
    ReconciliationReason,
    ReconciliationStatus,
    ResolutionState,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.attendance.import_dialog import RosterImportDialog
from omr_scanner.gui.attendance.worker import ReconcileResult, ReconcileWorker
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.services import batch_store, reconciliation_store, set_attendance
from omr_scanner.services.candidate_import import (
    CandidateImportError,
    save_sample_template,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.exam_sets import ExamSet
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import ProjectDatabase, ProjectSession
    from omr_scanner.services.reconciliation_store import RosterSummary
    from omr_scanner.services.set_attendance import SetAttendanceStatus

_LOGGER = logging.getLogger(__name__)

SAMPLE_FILENAME = "candidate_attendance_sample.xlsx"

TABLE_COLUMNS: tuple[str, ...] = (
    "Status",
    "Candidate ID",
    "Name",
    "Attendance",
    "Scripts",
    "Recognised ID",
    "Review",
)

SET_COLUMNS: tuple[str, ...] = (
    "Set",
    "Description",
    "Attendance file",
    "Candidates",
    "Result template",
    "Status",
)
"""The per-Set table's columns.

An item-based :class:`QTableWidget` on purpose: this table holds one row per
Set an operator typed into *Project Configuration* - tens at the very most,
where the brief's own upper example is "50+". The lazy model-view work the
Scan page needed is for one row per *sheet*, up to a hundred thousand of them,
and does not apply at this size."""

NO_SETS_TEXT = (
    "No sets are defined for this project yet. Attendance below applies to the "
    "whole project.\n"
    "To give each post or paper its own attendance file, define the sets in "
    "File > Project Configuration..."
)

NO_ATTENDANCE_CELL = "None assigned"

UNASSIGNED_ROSTER_TEXT = (
    "This project has a candidate list from before attendance was per-set. It "
    "has deliberately not been attached to any set - choose which set it "
    "belongs to, or import a fresh list for each set."
)

_STATUS_FILTERS: tuple[tuple[str, tuple[ReconciliationStatus, ...]], ...] = (
    ("Everything", ()),
    ("Exceptions only", ()),
    ("Matched", (ReconciliationStatus.MATCHED,)),
    ("Absent, confirmed", (ReconciliationStatus.ABSENT_CONFIRMED,)),
    ("Unknown candidate ID", (ReconciliationStatus.UNKNOWN_ID,)),
    ("Duplicate script", (ReconciliationStatus.DUPLICATE_SCRIPT,)),
    ("Present but no script", (ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,)),
    ("Marked absent but script found", (ReconciliationStatus.ABSENT_WITH_SCRIPT,)),
    ("Candidate ID not yet resolved", (ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,)),
)

_RESOLUTION_FILTERS: tuple[tuple[str, tuple[ResolutionState, ...]], ...] = (
    ("Any review state", ()),
    ("Needs review", (ResolutionState.OPEN,)),
    ("Resolved", (ResolutionState.RESOLVED,)),
    ("Accepted as-is", (ResolutionState.DISMISSED,)),
)


@dataclass
class AttendancePageState:
    """Everything the page is currently looking at.

    Attributes:
        session: The open project, or ``None``.
        sets: Every defined set's attendance state, in the operator's order.
            Empty for a project that has not defined any, which is a
            supported way to work - see the module docstring.
        selected_set_id: Which set the reconciliation below belongs to.
            ``None`` means the project's unscoped list, never "whichever set
            happened to be imported last": the two are different rosters in
            the database and are never interchanged.
        roster: The selected set's candidate list, or ``None`` when it has
            none. **Never another set's**, which is the whole point of
            resolving it through :attr:`selected_set_id`.
    """

    session: ProjectSession | None = None
    sets: tuple[SetAttendanceStatus, ...] = ()
    selected_set_id: str | None = None
    roster: RosterSummary | None = None
    batch_id: str | None = None
    operator: str = ""
    entries: list[ReconciliationEntry] = field(default_factory=list)

    @property
    def has_sets(self) -> bool:
        """Whether this project defines any sets at all."""
        return bool(self.sets)

    def status_for(self, set_id: str | None) -> SetAttendanceStatus | None:
        """The status row for one set id, if it is still in the list."""
        if set_id is None:
            return None
        return next(
            (item for item in self.sets if item.exam_set.set_id == set_id), None
        )


class AttendancePage(WorkflowPage):
    """Import a candidate list and reconcile it against a batch of scripts."""

    roster_imported = Signal(int)
    """Emitted with the new roster's id after an import."""

    reconciled = Signal()
    """Emitted whenever the reconciliation table has been rebuilt."""

    resolution_recorded = Signal()
    """Emitted after an operator decision has been stored."""

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.state = AttendancePageState()
        self.last_template_blocker = ""
        """Why the last assigned file did not become the set's result template.

        Empty when it did, or when nothing has been assigned. Set by
        :meth:`commit_roster` and shown by :meth:`import_from`, which is the
        method that owns dialogs."""
        self._worker: ReconcileWorker | None = None
        # Every worker ever started. A QThread garbage-collected - or whose
        # parent is destroyed - while still running aborts the process, so a
        # superseded run is tracked until it finishes rather than dropped.
        self._workers: list[ReconcileWorker] = []

        self.body.addWidget(self._build_roster_bar())
        self.body.addWidget(self._build_summary())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("reconciliationSplitter")
        splitter.addWidget(self._build_table_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.body.addWidget(splitter, stretch=1)

        self._update_enabled()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_roster_bar(self) -> QWidget:
        """The exam, one row per Set, and the commands that change them."""
        box = QGroupBox("Attendance by set")
        box.setObjectName("candidateRosterBox")
        layout = QVBoxLayout(box)

        self.exam_label = QLabel("")
        self.exam_label.setObjectName("attendanceExamNameLabel")
        self.exam_label.setWordWrap(True)
        self.exam_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.exam_label)

        self.no_sets_label = QLabel(NO_SETS_TEXT)
        self.no_sets_label.setObjectName("attendanceNoSetsLabel")
        self.no_sets_label.setWordWrap(True)
        layout.addWidget(self.no_sets_label)

        self.set_table = QTableWidget(0, len(SET_COLUMNS))
        self.set_table.setObjectName("setAttendanceTable")
        self.set_table.setHorizontalHeaderLabels(list(SET_COLUMNS))
        self.set_table.verticalHeader().setVisible(False)
        self.set_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.set_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.set_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.set_table.setAlternatingRowColors(True)
        self.set_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.set_table.horizontalHeader().setStretchLastSection(True)
        self.set_table.setMaximumHeight(180)
        self.set_table.itemSelectionChanged.connect(self._on_set_selection_changed)
        layout.addWidget(self.set_table)

        self.roster_label = QLabel("No candidate list imported")
        self.roster_label.setObjectName("activeRosterLabel")
        self.roster_label.setWordWrap(True)
        layout.addWidget(self.roster_label)

        self.unassigned_label = QLabel(UNASSIGNED_ROSTER_TEXT)
        self.unassigned_label.setObjectName("unassignedRosterLabel")
        self.unassigned_label.setWordWrap(True)
        self.unassigned_label.setStyleSheet("color: #a4262c;")
        self.unassigned_label.setVisible(False)
        layout.addWidget(self.unassigned_label)

        buttons = QHBoxLayout()
        self.import_button = QPushButton(
            load_icon("file-plus"), "Choose / Replace Attendance File..."
        )
        self.import_button.setObjectName("importRosterButton")
        self.import_button.setToolTip(
            "Import candidates and attendance for the selected set, from a CSV "
            "file or an Excel workbook. An Excel workbook also becomes that "
            "set's result template."
        )
        self.import_button.clicked.connect(self.prompt_import)
        buttons.addWidget(self.import_button)

        self.assign_existing_button = QPushButton("Assign Existing List To Set...")
        self.assign_existing_button.setObjectName("assignExistingRosterButton")
        self.assign_existing_button.setToolTip(
            "Attach the project's pre-existing candidate list to the selected "
            "set. Nothing is attached automatically, because nothing in the "
            "old data says which set it was for."
        )
        self.assign_existing_button.clicked.connect(self.assign_existing_roster)
        self.assign_existing_button.setVisible(False)
        buttons.addWidget(self.assign_existing_button)

        self.sample_button = QPushButton(load_icon("save"), "Download Sample Template...")
        self.sample_button.setObjectName("downloadSampleTemplateButton")
        self.sample_button.setToolTip(
            "Save an example candidate list showing the columns OMRFlow "
            "understands. Your own file does not have to match it exactly - "
            "you map the columns when you import."
        )
        self.sample_button.clicked.connect(self.prompt_save_sample)
        buttons.addWidget(self.sample_button)

        buttons.addStretch(1)

        self.reconcile_button = QPushButton(load_icon("rotate-ccw"), "Reconcile")
        self.reconcile_button.setObjectName("reconcileButton")
        self.reconcile_button.setToolTip(
            "Match the scripts in the current batch against the selected set's "
            "candidate list."
        )
        self.reconcile_button.clicked.connect(self.reconcile)
        buttons.addWidget(self.reconcile_button)
        layout.addLayout(buttons)
        return box

    def _build_summary(self) -> QWidget:
        """The counts an operator reads before calling the batch finished."""
        box = QGroupBox("Reconciliation summary")
        box.setObjectName("reconciliationSummaryBox")
        layout = QVBoxLayout(box)
        self.summary_label = QLabel("Nothing reconciled yet.")
        self.summary_label.setObjectName("reconciliationSummaryLabel")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)
        return box

    def _build_table_panel(self) -> QWidget:
        """The filter row and the reconciliation table."""
        panel = QWidget()
        panel.setObjectName("reconciliationTablePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        filters = QHBoxLayout()
        self.status_filter = QComboBox()
        self.status_filter.setObjectName("reconciliationStatusFilter")
        for label, _ in _STATUS_FILTERS:
            self.status_filter.addItem(label)
        self.status_filter.setCurrentIndex(1)  # Exceptions first: that is the work.
        self.status_filter.currentIndexChanged.connect(self.refresh_table)
        filters.addWidget(self.status_filter, stretch=1)

        self.resolution_filter = QComboBox()
        self.resolution_filter.setObjectName("reconciliationResolutionFilter")
        for label, _ in _RESOLUTION_FILTERS:
            self.resolution_filter.addItem(label)
        self.resolution_filter.currentIndexChanged.connect(self.refresh_table)
        filters.addWidget(self.resolution_filter, stretch=1)
        layout.addLayout(filters)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("reconciliationSearchBox")
        self.search_box.setPlaceholderText("Search by candidate ID or name...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.refresh_table)
        layout.addWidget(self.search_box)

        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setObjectName("reconciliationTable")
        self.table.setHorizontalHeaderLabels(list(TABLE_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, stretch=1)

        self.table_count_label = QLabel("")
        self.table_count_label.setObjectName("reconciliationCountLabel")
        layout.addWidget(self.table_count_label)
        return panel

    def _build_detail_panel(self) -> QWidget:
        """The selected entry: what is wrong, its scripts, and what to do."""
        panel = QWidget()
        panel.setObjectName("reconciliationDetailPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.detail_label = QLabel("Select a row to see what needs attention.")
        self.detail_label.setObjectName("reconciliationDetailLabel")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.detail_label)

        scripts_box = QGroupBox("Scripts")
        scripts_box.setObjectName("entryScriptsBox")
        scripts_layout = QVBoxLayout(scripts_box)
        self.scripts_list = QListWidget()
        self.scripts_list.setObjectName("entryScriptsList")
        self.scripts_list.setToolTip(
            "Every script attributed to this entry, including any set aside. "
            "Nothing here is ever deleted."
        )
        scripts_layout.addWidget(self.scripts_list)
        layout.addWidget(scripts_box, stretch=1)

        layout.addWidget(self._build_actions())

        self.history_label = QLabel("")
        self.history_label.setObjectName("reconciliationHistoryLabel")
        self.history_label.setWordWrap(True)
        self.history_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.history_label)
        return panel

    def _build_actions(self) -> QWidget:
        """The decisions an operator can record."""
        box = QGroupBox("Your decision")
        box.setObjectName("reconciliationActionBox")
        layout = QVBoxLayout(box)

        self.operator_label = QLabel("")
        self.operator_label.setObjectName("reconciliationOperatorLabel")
        self.operator_label.setWordWrap(True)
        layout.addWidget(self.operator_label)

        assign_row = QHBoxLayout()
        self.assign_edit = QLineEdit()
        self.assign_edit.setObjectName("assignCandidateEdit")
        self.assign_edit.setPlaceholderText("Candidate ID to assign this script to...")
        assign_row.addWidget(self.assign_edit, stretch=1)
        self.assign_button = QPushButton("Assign Script")
        self.assign_button.setObjectName("assignScriptButton")
        self.assign_button.setToolTip(
            "Attribute the selected script to this candidate. The recognised "
            "ID is kept exactly as it was read."
        )
        self.assign_button.clicked.connect(self.assign_selected_script)
        assign_row.addWidget(self.assign_button)
        layout.addLayout(assign_row)

        buttons = QHBoxLayout()
        self.exclude_button = QPushButton("Set Script Aside")
        self.exclude_button.setObjectName("excludeScriptButton")
        self.exclude_button.setToolTip(
            "Record the selected script as an accidental re-scan. It stops "
            "counting towards this candidate but is never deleted."
        )
        self.exclude_button.clicked.connect(self.toggle_selected_exclusion)
        buttons.addWidget(self.exclude_button)

        self.attendance_button = QPushButton("Override Attendance")
        self.attendance_button.setObjectName("overrideAttendanceButton")
        self.attendance_button.setToolTip(
            "Record that this candidate's real attendance differs from the "
            "imported list. The imported value is kept."
        )
        self.attendance_button.clicked.connect(self.toggle_attendance)
        buttons.addWidget(self.attendance_button)

        self.dismiss_button = QPushButton("Accept As-Is")
        self.dismiss_button.setObjectName("dismissEntryButton")
        self.dismiss_button.setToolTip(
            "Record that nothing can be done about this exception. It stays "
            "visible but stops counting as outstanding work."
        )
        self.dismiss_button.clicked.connect(self.toggle_dismissed)
        buttons.addWidget(self.dismiss_button)
        layout.addLayout(buttons)

        reason_row = QHBoxLayout()
        reason_row.addWidget(QLabel("Reason:"))
        self.reason_combo = QComboBox()
        self.reason_combo.setObjectName("reconciliationReasonCombo")
        for reason in ReconciliationReason:
            self.reason_combo.addItem(reason.label, reason.value)
        reason_row.addWidget(self.reason_combo, stretch=1)
        layout.addLayout(reason_row)

        self.reason_text = QLineEdit()
        self.reason_text.setObjectName("reconciliationReasonText")
        self.reason_text.setPlaceholderText("Optional note; required for 'Other'.")
        layout.addWidget(self.reason_text)
        return box

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt an opened project, or clear everything when one closes."""
        self.state.session = session
        self.state.sets = ()
        self.state.selected_set_id = None
        self.state.roster = None
        self.state.batch_id = None
        self.state.entries = []
        if session is not None:
            self.state.batch_id = self._latest_batch()
        self.refresh_sets()
        self.refresh_table()
        self._update_enabled()

    # ------------------------------------------------------------------
    # Sets
    # ------------------------------------------------------------------
    def refresh_sets(self) -> tuple[SetAttendanceStatus, ...]:
        """Re-read every set's attendance state and rebuild the set table.

        Returns:
            The statuses, so a test can assert against the data rather than
            re-reading it off the widgets.

        The previously selected set is kept **by id**: a set may have been
        renamed, reordered or removed since, and a remembered row index would
        silently move the reconciliation below onto somebody else's
        candidates.
        """
        database = self.database
        wanted = self.state.selected_set_id
        self.state.sets = (
            set_attendance.attendance_overview(database) if database is not None else ()
        )
        self._rebuild_set_table()

        if self.state.has_sets:
            chosen = wanted if self.state.status_for(wanted) is not None else None
            if chosen is None:
                chosen = self.state.sets[0].exam_set.set_id
            self.select_set(chosen)
        else:
            # No sets defined: attendance is the project's unscoped list.
            self.state.selected_set_id = None
            self._adopt_selected_roster()

        self._refresh_unassigned_banner()
        return self.state.sets

    def _rebuild_set_table(self) -> None:
        """Fill the set table from :attr:`AttendancePageState.sets`."""
        session = self.state.session
        self.exam_label.setText(
            f"<b>Exam:</b> {session.exam_name}" if session is not None else ""
        )
        self.no_sets_label.setVisible(session is not None and not self.state.has_sets)
        self.set_table.setVisible(self.state.has_sets)

        self.set_table.blockSignals(True)
        self.set_table.setRowCount(len(self.state.sets))
        for row, status in enumerate(self.state.sets):
            values = (
                status.exam_set.display_label,
                status.exam_set.description,
                status.attendance_file or NO_ATTENDANCE_CELL,
                str(status.candidate_count) if status.has_attendance else "",
                status.template_file or "",
                status.describe(),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    # The stable identity travels with the row, so no action
                    # ever has to infer which set it means from a position.
                    item.setData(Qt.ItemDataRole.UserRole, status.exam_set.set_id)
                    item.setToolTip(f"Internal id: {status.exam_set.set_id}")
                if column == 2 and not status.has_attendance:
                    item.setToolTip(
                        "This set has no attendance file. Choose one for it - "
                        "another set's file is never used in its place."
                    )
                if column == 5 and status.template_blocker:
                    item.setToolTip(status.template_blocker)
                self.set_table.setItem(row, column, item)
        self.set_table.blockSignals(False)

    def selected_set(self) -> ExamSet | None:
        """The set whose attendance the page is showing, or ``None``.

        ``None`` for a project with no sets defined, where attendance is the
        project's own unscoped list.
        """
        status = self.state.status_for(self.state.selected_set_id)
        return status.exam_set if status is not None else None

    def selected_set_status(self) -> SetAttendanceStatus | None:
        """The selected set's attendance state, or ``None``."""
        return self.state.status_for(self.state.selected_set_id)

    def select_set(self, set_id: str) -> bool:
        """Show one set's attendance and reconciliation. No dialog.

        Returns:
            Whether that set is in the current list.
        """
        for row, status in enumerate(self.state.sets):
            if status.exam_set.set_id == set_id:
                self.state.selected_set_id = set_id
                # Re-adopted unconditionally, not only when the *id* changes:
                # importing a file for the set already selected changes that
                # set's roster without changing which set is selected, and a
                # change-guard here left `state.roster` holding the state
                # from before the import - so reconciliation found no roster
                # and the replace-confirmation had nothing to warn about.
                self._adopt_selected_roster()
                self.set_table.blockSignals(True)
                self.set_table.selectRow(row)
                self.set_table.blockSignals(False)
                return True
        return False

    def _on_set_selection_changed(self) -> None:
        """Adopt whichever set the operator clicked."""
        row = self.set_table.currentRow()
        if not 0 <= row < len(self.state.sets):
            return
        set_id = self.state.sets[row].exam_set.set_id
        if set_id == self.state.selected_set_id:
            return
        self.state.selected_set_id = set_id
        self._adopt_selected_roster()
        self.refresh_table()
        self._update_enabled()

    def _adopt_selected_roster(self) -> None:
        """Point the page at the selected set's candidate list.

        Resolved through :func:`~omr_scanner.services.reconciliation_store.active_roster`
        with the set's own id, which returns ``None`` rather than falling back
        to another set's list - the refusal §15 requires, expressed where it
        cannot be forgotten.
        """
        status = self.state.status_for(self.state.selected_set_id)
        if status is not None:
            self.state.roster = status.roster
        else:
            database = self.database
            self.state.roster = (
                reconciliation_store.active_roster(database, None)
                if database is not None
                else None
            )
        self._refresh_roster_label()

    def _refresh_unassigned_banner(self) -> None:
        """Offer a pre-Part-2 list for explicit assignment, never silently."""
        database = self.database
        pending = (
            reconciliation_store.unassigned_rosters(database)
            if database is not None and self.state.has_sets
            else ()
        )
        show = bool(pending)
        self.unassigned_label.setVisible(show)
        self.assign_existing_button.setVisible(show)

    def assign_existing_roster(self) -> bool:
        """Attach the project's unscoped candidate list to the selected set.

        The explicit resolution §29 asks for. Nothing is attached
        automatically: an old project's single list says nothing about which
        of several sets it was meant for, and guessing would hand one set's
        candidates to another.
        """
        database = self.database
        exam_set = self.selected_set()
        if database is None or exam_set is None:
            return False
        pending = reconciliation_store.unassigned_rosters(database)
        if not pending:
            return False
        try:
            reconciliation_store.assign_roster_to_set(
                database, pending[0].roster_id, exam_set.set_id
            )
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Candidate list not assigned", exc.user_message or str(exc)
            )
            return False
        self.refresh_sets()
        self.refresh_table()
        self._update_enabled()
        self.roster_imported.emit(pending[0].roster_id)
        return True

    @property
    def database(self) -> ProjectDatabase | None:
        """The open project's database, or ``None``."""
        return self.state.session.database if self.state.session is not None else None

    def set_operator(self, name: str) -> None:
        """Adopt the configured reviewer name; the same person reconciles."""
        self.state.operator = name.strip()
        self._refresh_operator_label()

    def _latest_batch(self) -> str | None:
        """The most recent batch in this project, which is what to reconcile."""
        database = self.database
        if database is None:
            return None
        batches = batch_store.list_batches(database, limit=1)
        return batches[0].batch_id if batches else None

    def set_batch(self, batch_id: str) -> None:
        """Reconcile a particular batch rather than the most recent one."""
        self.state.batch_id = batch_id
        self.refresh_table()
        self._update_enabled()

    # ------------------------------------------------------------------
    # Sample template
    # ------------------------------------------------------------------
    def prompt_save_sample(self) -> None:
        """Ask where to put the sample candidate list, then write it."""
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "Save Sample Candidate List",
            str(Path.home() / SAMPLE_FILENAME),
            "Excel Workbook (*.xlsx)",
        )
        if not chosen:
            return
        self.save_sample_to(Path(chosen))

    def save_sample_to(self, destination: Path) -> bool:
        """Write the packaged sample to ``destination``.

        Split from the dialog so the behaviour is testable: the file chooser is
        untestable offscreen, and everything that matters happens here.
        """
        if destination.exists():
            answer = QMessageBox.question(
                self,
                "Replace file?",
                f"'{destination.name}' already exists. Replace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return False
        try:
            save_sample_template(destination, overwrite=True)
        except CandidateImportError as exc:
            QMessageBox.warning(
                self, "Sample not saved", exc.user_message or str(exc)
            )
            return False
        QMessageBox.information(
            self,
            "Sample saved",
            f"The sample candidate list was saved as '{destination.name}'.\n\n"
            "It shows the columns OMRFlow understands. Your own list does not "
            "have to match it - you map the columns when you import.",
        )
        return True

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------
    def prompt_import(self) -> None:
        """Ask for a candidate list for the selected set, then map its columns."""
        exam_set = self.selected_set()
        title = (
            f"Choose Attendance File for {exam_set.display_label}"
            if exam_set is not None
            else "Import Candidate List"
        )
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            title,
            "",
            "Candidate lists (*.csv *.xlsx);;CSV (*.csv);;Excel Workbook (*.xlsx)",
        )
        if not chosen:
            return
        self.import_from(Path(chosen))

    def import_from(self, path: Path) -> bool:
        """Open the mapping dialog for ``path`` and import what it confirms.

        The file is imported **for the selected set**, or for the project as a
        whole when no sets are defined. It is never imported "generally" and
        then attached afterwards, because a roster with no set is a different
        record from a roster with one.
        """
        if not self._confirm_replacement():
            return False
        dialog = RosterImportDialog(path, self)
        if dialog.exec() != RosterImportDialog.DialogCode.Accepted:
            return False
        validation = dialog.validation
        if validation is None:
            return False
        stored = self.commit_roster(validation, source_path=path)
        # The modal lives here, in the dialog-owning method, rather than in
        # `commit_roster` - which a test calls directly, and which would
        # otherwise raise a message box on every ordinary CSV import.
        if stored and self.last_template_blocker:
            QMessageBox.information(
                self, "Result template not set", self.last_template_blocker
            )
        return stored

    def _confirm_replacement(self) -> bool:
        """Warn before a second roster supersedes the selected set's."""
        if self.state.roster is None:
            return True
        exam_set = self.selected_set()
        whose = (
            f"{exam_set.display_label} already uses"
            if exam_set is not None
            else "This project already uses"
        )
        scope = (
            "this set's active one" if exam_set is not None else "the active one"
        )
        answer = QMessageBox.question(
            self,
            "Replace the candidate list?",
            f"{whose} '{self.state.roster.source_name}' "
            f"({self.state.roster.candidate_count} candidates).\n\n"
            f"Importing another list makes it {scope} and reconciliation "
            "is recomputed against it. The existing list and every decision "
            "already recorded are kept, not merged or deleted."
            + (
                "\n\nNo other set is affected."
                if exam_set is not None
                else ""
            )
            + "\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer is QMessageBox.StandardButton.Yes

    def commit_roster(self, validation: object, source_path: Path | None = None) -> bool:
        """Store a validated roster for the selected set and reconcile.

        Args:
            validation: The confirmed
                :class:`~omr_scanner.services.candidate_import.RosterValidation`.
            source_path: The file it was read from. Supplying it is what lets
                an ``.xlsx`` also become the set's result template - the whole
                point of §5 - so the import path always does; a caller that
                only has a validation still imports the candidates.

        Returns:
            Whether it was stored.
        """
        database = self.database
        if database is None:
            return False
        exam_set = self.selected_set()
        try:
            if exam_set is not None and source_path is not None:
                assignment = set_attendance.assign_attendance_workbook(
                    database,
                    exam_set.set_id,
                    source_path,
                    validation,  # type: ignore[arg-type]
                    imported_by=self.state.operator,
                )
                roster_id = assignment.roster_id
                blocker = assignment.template_blocker
            else:
                roster_id = reconciliation_store.import_roster(
                    database,
                    validation,  # type: ignore[arg-type]
                    imported_by=self.state.operator,
                    set_id=exam_set.set_id if exam_set is not None else None,
                )
                blocker = ""
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Candidate list not imported", exc.user_message or str(exc)
            )
            return False

        self.refresh_sets()
        if exam_set is not None:
            self.select_set(exam_set.set_id)
        self._update_enabled()
        # Recorded, not shown in a modal. A CSV *always* produces a blocker
        # (it has no layout to build a result on), so a message box here
        # would fire on the ordinary happy path - and, being modal inside a
        # method tests call directly, would hang a headless run. The
        # dialog-owning `import_from` shows it; everything else reads it
        # from here or from the set table's own Result template column.
        self.last_template_blocker = blocker
        if blocker:
            self.roster_label.setText(blocker)
        self.roster_imported.emit(roster_id)
        self.reconcile()
        return True

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def reconcile(self) -> bool:
        """Match the current batch against the active roster, off the GUI thread."""
        database = self.database
        roster = self.state.roster
        if database is None or roster is None:
            return False
        if self.state.batch_id is None:
            self.state.batch_id = self._latest_batch()
        if self.state.batch_id is None:
            self.summary_label.setText(
                "No batch has been processed in this project yet. Run a batch "
                "on the <b>Scan</b> stage, then reconcile."
            )
            return False

        self.reconcile_button.setEnabled(False)
        if self._worker is not None and self._worker.isRunning():
            self._worker.ready.disconnect()
        worker = ReconcileWorker(database, roster.roster_id, self.state.batch_id, self)
        worker.ready.connect(self._on_reconciled)
        self._worker = worker
        self._workers = [item for item in self._workers if item.isRunning()]
        self._workers.append(worker)
        worker.start()
        return True

    def _on_reconciled(self, result: ReconcileResult) -> None:
        """Adopt a finished reconciliation. Runs on the GUI thread."""
        self.reconcile_button.setEnabled(True)
        if not result.ok:
            QMessageBox.warning(self, "Reconciliation failed", result.error)
            return
        self._refresh_summary()
        self.refresh_table()
        self.reconciled.emit()

    def _refresh_summary(self) -> None:
        """Show the counts from the last reconciliation."""
        database = self.database
        roster = self.state.roster
        if database is None or roster is None or self.state.batch_id is None:
            self.summary_label.setText("Nothing reconciled yet.")
            return
        counts = reconciliation_store.stored_counts(
            database, roster.roster_id, self.state.batch_id
        )
        if counts is None:
            self.summary_label.setText("Nothing reconciled yet.")
            return

        verdict = (
            "<span style='color:#1b7f3b'><b>Every exception has been "
            "dealt with.</b></span>"
            if counts.is_clear
            else f"<span style='color:#a4262c'><b>{counts.outstanding} "
            "exception(s) still need review.</b></span>"
        )
        self.summary_label.setText(
            f"Registered <b>{counts.registered}</b> · "
            f"Expected present <b>{counts.expected_present}</b> · "
            f"Marked absent <b>{counts.expected_absent}</b> · "
            f"Scripts <b>{counts.scripts}</b>"
            + (
                f" (<b>{counts.scripts_excluded}</b> set aside)"
                if counts.scripts_excluded
                else ""
            )
            + "<br>"
            f"Matched <b>{counts.matched}</b> · "
            f"Absent confirmed <b>{counts.absent_confirmed}</b> · "
            f"Unknown ID <b>{counts.unknown_id}</b> · "
            f"Duplicate script <b>{counts.duplicate_script}</b> · "
            f"Present without script <b>{counts.present_without_script}</b> · "
            f"Absent with script <b>{counts.absent_with_script}</b> · "
            f"ID not yet resolved <b>{counts.unresolved_candidate_id}</b>"
            "<br>"
            f"Resolved <b>{counts.resolved}</b> · "
            f"Accepted as-is <b>{counts.dismissed}</b> · {verdict}"
        )

    # ------------------------------------------------------------------
    # The table
    # ------------------------------------------------------------------
    def _current_filter(self) -> reconciliation_store.EntryFilter:
        """Build a filter from the three controls."""
        label, statuses = _STATUS_FILTERS[max(0, self.status_filter.currentIndex())]
        _, resolutions = _RESOLUTION_FILTERS[
            max(0, self.resolution_filter.currentIndex())
        ]
        return reconciliation_store.EntryFilter(
            statuses=statuses,
            resolutions=resolutions,
            exceptions_only=label == "Exceptions only",
            search=self.search_box.text(),
        )

    def refresh_table(self) -> None:
        """Re-read the reconciliation from the database and rebuild the table."""
        database = self.database
        roster = self.state.roster
        selected = self._selected_entry()
        keep = selected.candidate_id if selected else None

        if database is None or roster is None or self.state.batch_id is None:
            self.state.entries = []
        else:
            self.state.entries = list(
                reconciliation_store.list_entries(
                    database,
                    roster.roster_id,
                    self.state.batch_id,
                    filters=self._current_filter(),
                )
            )
        self._rebuild_table()
        self._restore_selection(keep)
        self._refresh_summary()

    def _rebuild_table(self) -> None:
        """Fill the table from :attr:`AttendancePageState.entries`."""
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(self.state.entries))
        for row, entry in enumerate(self.state.entries):
            values = (
                entry.status.label,
                entry.candidate_id or "(not read)",
                entry.display_name,
                self._attendance_text(entry),
                self._script_text(entry),
                self._recognised_text(entry),
                entry.resolution.label if entry.status.is_exception else "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setToolTip(entry.status.description)
                    if len(entry.issues) > 1:
                        others = ", ".join(
                            sorted(
                                issue.label
                                for issue in entry.issues
                                if issue.status is not entry.status
                            )
                        )
                        item.setText(f"{entry.status.label}  (+ {others})")
                        item.setToolTip(
                            f"{entry.status.description}\n\nAlso: {others}"
                        )
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        self.table_count_label.setText(
            f"{len(self.state.entries)} row(s) shown"
            + (
                ""
                if not self.state.entries
                else f" · {sum(1 for e in self.state.entries if e.needs_attention)}"
                " needing review"
            )
        )

    def _attendance_text(self, entry: ReconciliationEntry) -> str:
        """Describe attendance, showing an override beside what was imported."""
        if entry.candidate is None:
            return ""
        if entry.attendance_source is AttendanceSource.HUMAN:
            return (
                f"{entry.effective_attendance.label} "
                f"(list said {entry.candidate.imported_attendance.label.lower()})"
            )
        return entry.effective_attendance.label

    def _script_text(self, entry: ReconciliationEntry) -> str:
        """How many scripts count, and how many are set aside."""
        aside = sum(1 for item in entry.scripts if item.excluded)
        if aside:
            return f"{entry.script_count} (+{aside} set aside)"
        return str(entry.script_count)

    def _recognised_text(self, entry: ReconciliationEntry) -> str:
        """What recognition read for this entry's scripts."""
        values = []
        for view in entry.scripts:
            machine = view.script.machine_candidate_id or "(not read)"
            if view.script.effective_candidate_id != machine:
                values.append(f"{machine} -> {view.script.effective_candidate_id}")
            else:
                values.append(machine)
        return ", ".join(dict.fromkeys(values))

    def _restore_selection(self, candidate_id: str | None) -> None:
        """Re-select the entry that was selected before a rebuild.

        By identity, never by row index: after a decision the entry may have
        moved or left the filter entirely, and a stale index would show one
        entry while the detail panel described another.
        """
        if candidate_id is not None:
            for row, entry in enumerate(self.state.entries):
                if entry.candidate_id == candidate_id:
                    self.table.selectRow(row)
                    return
        self._show_entry(None)

    def _selected_entry(self) -> ReconciliationEntry | None:
        """The entry the table has selected, if any."""
        row = self.table.currentRow()
        if 0 <= row < len(self.state.entries):
            return self.state.entries[row]
        return None

    def _on_selection_changed(self) -> None:
        """Show whatever the table now has selected."""
        self._show_entry(self._selected_entry())

    # ------------------------------------------------------------------
    # The detail panel
    # ------------------------------------------------------------------
    def _show_entry(self, entry: ReconciliationEntry | None) -> None:
        """Describe one entry, its scripts and its history."""
        self.scripts_list.clear()
        if entry is None:
            self.detail_label.setText("Select a row to see what needs attention.")
            self.history_label.setText("")
            self._update_enabled()
            return

        parts = [f"<b>{entry.status.label}</b><br>{entry.status.description}"]
        if len(entry.issues) > 1:
            others = ", ".join(
                sorted(
                    issue.label
                    for issue in entry.issues
                    if issue.status is not entry.status
                )
            )
            parts.append(f"<br><b>This entry also has:</b> {others}")
        if entry.candidate is not None:
            parts.append(
                f"<br><b>{entry.candidate.candidate_id}</b>"
                + (f" - {entry.candidate.display_name}" if entry.display_name else "")
                + f"<br>Candidate list said: <b>"
                f"{entry.candidate.imported_attendance.label}</b>"
                + (
                    f" (cell read '{entry.candidate.imported_value}')"
                    if entry.candidate.imported_value
                    else ""
                )
            )
            if entry.attendance_was_overridden:
                parts.append(
                    f"<br>A reviewer recorded: <b>"
                    f"{entry.effective_attendance.label}</b>"
                    + (f" - {entry.reason_text}" if entry.reason_text else "")
                    + (f" ({entry.reviewer})" if entry.reviewer else "")
                    + "<br><i>The imported value is kept.</i>"
                )
        self.detail_label.setText("".join(parts))

        for view in entry.scripts:
            script = view.script
            bits = [script.source_name or f"scan {script.scan_id}"]
            bits.append(f"read as {script.machine_candidate_id or '(not read)'}")
            if script.corrected_by_human:
                bits.append(f"corrected to {script.effective_candidate_id}")
            if view.assignment.value == "human":
                bits.append("assigned by reviewer")
            if view.primary:
                bits.append("working script")
            if view.excluded:
                bits.append("SET ASIDE")
            item = QListWidgetItem(" · ".join(bits))
            item.setData(Qt.ItemDataRole.UserRole, script.scan_id)
            if view.excluded:
                item.setToolTip(
                    "Set aside as an accidental re-scan. The scan, its "
                    "recognition result and this decision are all kept."
                )
            self.scripts_list.addItem(item)
        if self.scripts_list.count():
            self.scripts_list.setCurrentRow(0)

        self._refresh_history(entry)
        self._update_enabled()

    def _refresh_history(self, entry: ReconciliationEntry) -> None:
        """Show every decision recorded about this entry."""
        database = self.database
        if database is None:
            self.history_label.setText("")
            return
        records = reconciliation_store.history_for_entry(database, entry)
        if not records:
            self.history_label.setText(
                "<i>No decisions have been recorded for this entry.</i>"
            )
            return
        lines = ["<b>History</b>"]
        for record in records:
            when = record.occurred_at.strftime("%Y-%m-%d %H:%M")
            change = (
                f" {record.previous_value or '(none)'} &rarr; {record.new_value}"
                if record.new_value and record.new_value != record.previous_value
                else ""
            )
            reason = f" - {record.reason}" if record.reason else ""
            lines.append(
                f"{when} · <b>{record.action.label}</b> by "
                f"{record.reviewer or '(unnamed)'}{change}{reason}"
            )
        self.history_label.setText("<br>".join(lines))

    def _selected_scan_id(self) -> int | None:
        """The scan id of the script selected in the detail list.

        Driven from the row index rather than ``currentItem()``: PySide6's
        generated stubs declare that method non-optional although it returns
        ``None`` for an empty selection, and a row index of ``-1`` says the
        same thing without arguing with the type checker.
        """
        row = self.scripts_list.currentRow()
        if row < 0:
            return None
        value = self.scripts_list.item(row).data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------
    def _reason(self) -> ReconciliationReason:
        """The reason code currently chosen."""
        return ReconciliationReason(self.reason_combo.currentData())

    def _act(self, operation: str, *args: object, **kwargs: object) -> bool:
        """Run one store operation, reporting a refusal rather than raising."""
        database = self.database
        roster = self.state.roster
        if database is None or roster is None or self.state.batch_id is None:
            return False
        function = getattr(reconciliation_store, operation)
        try:
            function(database, roster.roster_id, self.state.batch_id, *args, **kwargs)
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Decision not recorded", exc.user_message or str(exc)
            )
            return False
        self.refresh_table()
        self.resolution_recorded.emit()
        return True

    def assign_selected_script(self) -> bool:
        """Attribute the selected script to the candidate ID that was typed."""
        scan_id = self._selected_scan_id()
        if scan_id is None:
            QMessageBox.information(
                self, "No script selected", "Select a script to assign."
            )
            return False
        return self._act(
            "assign_script",
            scan_id,
            candidate_id=self.assign_edit.text(),
            operator=self.state.operator,
            reason=self._reason(),
            reason_text=self.reason_text.text(),
        )

    def toggle_selected_exclusion(self) -> bool:
        """Set the selected script aside, or bring it back."""
        scan_id = self._selected_scan_id()
        entry = self._selected_entry()
        if scan_id is None or entry is None:
            return False
        view = next(
            (item for item in entry.scripts if item.script.scan_id == scan_id), None
        )
        if view is None:
            return False
        return self._act(
            "set_script_excluded",
            scan_id,
            excluded=not view.excluded,
            operator=self.state.operator,
            reason=self._reason(),
            reason_text=self.reason_text.text(),
        )

    def toggle_attendance(self) -> bool:
        """Override this candidate's attendance, or withdraw the override."""
        entry = self._selected_entry()
        if entry is None or entry.candidate is None:
            QMessageBox.information(
                self,
                "Not a registered candidate",
                "Attendance can only be overridden for a candidate on the "
                "imported list.",
            )
            return False
        if entry.attendance_was_overridden:
            target = AttendanceState.UNKNOWN
        elif entry.effective_attendance is AttendanceState.ABSENT:
            target = AttendanceState.PRESENT
        else:
            target = AttendanceState.ABSENT
        return self._act(
            "override_attendance",
            entry.candidate_id,
            attendance=target,
            operator=self.state.operator,
            reason=self._reason(),
            reason_text=self.reason_text.text(),
        )

    def toggle_dismissed(self) -> bool:
        """Accept this exception as-is, or put it back on the list."""
        entry = self._selected_entry()
        if entry is None:
            return False
        if entry.resolution is ResolutionState.DISMISSED:
            return self._act(
                "reopen_entry", entry.candidate_id, operator=self.state.operator
            )
        return self._act(
            "dismiss_entry",
            entry.candidate_id,
            operator=self.state.operator,
            reason=self._reason(),
            reason_text=self.reason_text.text(),
        )

    # ------------------------------------------------------------------
    # Enablement
    # ------------------------------------------------------------------
    def _refresh_roster_label(self) -> None:
        """Say which candidate list is in force, and whose it is."""
        roster = self.state.roster
        exam_set = self.selected_set()
        whose = f"{exam_set.display_label}: " if exam_set is not None else ""
        if roster is None:
            # §15, in the one sentence an operator reads: this set has none,
            # and no other set's file stands in for it.
            self.roster_label.setText(
                f"{whose}no candidate list imported. Choose an attendance file "
                "for this set to reconcile its scripts against."
                if exam_set is not None
                else "No candidate list imported. Import one to reconcile the "
                "scripts against it."
            )
            return
        attendance = (
            f" · {roster.expected_present} expected present, "
            f"{roster.expected_absent} marked absent"
            if roster.has_attendance_column
            else " · no attendance column mapped"
        )
        self.roster_label.setText(
            f"{whose}<b>{roster.source_name}</b>"
            + (f" ({roster.source_sheet})" if roster.source_sheet else "")
            + f" · {roster.candidate_count} candidate(s){attendance}"
        )

    def _refresh_operator_label(self) -> None:
        """Say who decisions will be recorded as."""
        if self.state.operator:
            self.operator_label.setText(
                f"Decisions are recorded as: <b>{self.state.operator}</b>"
            )
            self.operator_label.setStyleSheet("")
            return
        self.operator_label.setText(
            "No reviewer name is set. Add one in File > Settings > Reviewer - "
            "a decision cannot be recorded without a name."
        )
        self.operator_label.setStyleSheet("color: #a4262c;")

    def _update_enabled(self) -> None:
        """Enable only what the current state allows."""
        session = self.state.session
        has_project = self.database is not None
        writable = has_project and not (session is not None and session.read_only)
        has_roster = self.state.roster is not None
        entry = self._selected_entry()
        has_scripts = bool(entry and entry.scripts)
        registered = bool(entry and entry.is_registered)

        # A project with sets needs one selected before a file can be chosen
        # for it - there is deliberately no "import for whichever set" path.
        can_choose = writable and (not self.state.has_sets or self.selected_set() is not None)
        self.import_button.setEnabled(can_choose)
        self.assign_existing_button.setEnabled(can_choose)
        self.reconcile_button.setEnabled(has_project and has_roster)
        self.assign_button.setEnabled(has_scripts)
        self.assign_edit.setEnabled(has_scripts)
        self.exclude_button.setEnabled(has_scripts)
        self.attendance_button.setEnabled(registered)
        self.dismiss_button.setEnabled(bool(entry and entry.status.is_exception))

        if entry is not None and entry.resolution is ResolutionState.DISMISSED:
            self.dismiss_button.setText("Put Back On The List")
        else:
            self.dismiss_button.setText("Accept As-Is")

        scan_id = self._selected_scan_id()
        scripts = entry.scripts if entry else ()
        view = (
            next(
                (item for item in scripts if item.script.scan_id == scan_id),
                None,
            )
            if scan_id is not None
            else None
        )
        self.exclude_button.setText(
            "Bring Script Back" if view is not None and view.excluded else "Set Script Aside"
        )
        self._refresh_operator_label()

    # ------------------------------------------------------------------
    # Lifetime
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Wait for every reconciliation this page started.

        All of them, not just the most recent: a superseded run is still a
        running thread, and one alive at interpreter teardown aborts the
        process.
        """
        workers = self._workers
        self._worker = None
        self._workers = []
        for worker in workers:
            if worker.isRunning():
                worker.wait(5000)

    def closeEvent(self, event: object) -> None:
        """Join the worker before the page goes away."""
        self.shutdown()
        super().closeEvent(event)  # type: ignore[arg-type]
