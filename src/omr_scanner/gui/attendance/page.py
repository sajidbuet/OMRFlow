"""Reconcile scanned scripts against the registered candidates (Phase 7).

The stage where an examination office satisfies itself that every script
belongs to somebody and everybody who sat the paper handed one in - and, when
the two disagree, looks at the paper to find out why.

Shape of the page:

    ┌───────────────────────────────────────────────────────────────┐
    │ ATTENDANCE BY SET   Exam: ...                                 │
    │ set · description · file · candidates · present · absent · …  │
    │ Choose/Replace Attendance File · Sample             Reconcile │
    │ Set 10 · file.xlsx · 100 candidates · 95 present · 5 absent   │
    ├───────────────────────────────────────────────────────────────┤
    │ [Matched 93] [Missing script 2] [Absent + script 1] [...]     │
    ├──────────────────────────────┬────────────────────────────────┤
    │ filters · search             │ (scrolls)                      │
    │ exception / candidate table  │ the problem, in sentences      │
    │ (the main work area)         │ related scripts · Inspect      │
    │                              │ where to look                  │
    │                              │ scan + Student ID / set editor │
    │                              │ other decisions · history      │
    └──────────────────────────────┴────────────────────────────────┘

The rules the page is arranged around:

* **Attendance belongs to a Set, and the pairing is unmistakable.** Each
  defined Set has its own row, its own attendance file and its own candidate
  list; selecting a Set is what decides whose reconciliation is below. One
  Set's workbook is never offered to another (§3, §15).
* **Every problem is visible, and so is the evidence.** An entry shows all its
  issues, and any script behind one can be opened - the original scan, the
  bubbles as read - without leaving the stage.
* **A disagreement is shown, never settled by the machine.** Attendance saying
  a candidate was absent does not change what their script says, and a similar
  roll number does not reassign a script. The page *suggests* where to look;
  a person decides.
* **A correction is the real correction.** Correcting a Student ID here goes
  through the same review ledger the Resolve stage writes
  (:mod:`omr_scanner.services.field_edit`): the machine's value is kept, the
  operator's becomes effective for every later stage, and the change is
  audited and undoable. Reconciliation is then recomputed at once.
* **No destructive shortcut exists.** Nothing drops a script, picks a
  duplicate or edits the imported roster.

A project with no Sets defined yet still works: attendance is then *unscoped*
(one list for the project, ``set_id`` NULL), exactly as before Sets existed.

Candidate names and IDs are shown here - reconciliation would be impossible
otherwise - and are never written to a log line.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QKeyEvent,
    QKeySequence,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.config.app_config import MAX_SPLIT_RATIO, MIN_SPLIT_RATIO
from omr_scanner.domain.reconciliation import (
    AttendanceSource,
    AttendanceState,
    ReconciliationEntry,
    ReconciliationReason,
    ReconciliationStatus,
    ResolutionState,
)
from omr_scanner.domain.review import ReasonCode
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.attendance.import_dialog import RosterImportDialog
from omr_scanner.gui.attendance.worker import ReconcileResult, ReconcileWorker
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.review.inspector import ScriptInspector
from omr_scanner.gui.theme import (
    ATTENDANCE_STAGE_STYLESHEET,
    VARIANT_PRIMARY,
    VARIANT_PROPERTY,
    Color,
    Spacing,
)
from omr_scanner.services import (
    batch_store,
    load_template,
    reconciliation_store,
    resolve_active_template,
    set_attendance,
)
from omr_scanner.services.candidate_import import (
    CandidateImportError,
    save_sample_template,
)
from omr_scanner.services.reconciliation_leads import (
    InvestigationLead,
    OutOfSetScript,
    owner_leads,
    script_leads,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.exam_sets import ExamSet
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import ProjectDatabase, ProjectSession
    from omr_scanner.services.reconciliation_store import RosterSummary
    from omr_scanner.services.set_attendance import SetAttendanceStatus

_LOGGER = logging.getLogger(__name__)

SAMPLE_FILENAME = "candidate_attendance_sample.xlsx"

DEFAULT_SPLIT_RATIO = 0.6
"""The table's share of the work area until the operator moves the divider."""

SPLIT_SETTLE_MS = 400
"""How long the divider must be still before its position is remembered."""

TABLE_COLUMNS: tuple[str, ...] = (
    "Status",
    "Candidate ID",
    "Name",
    "Attendance",
    "Scripts",
    "Recognised ID",
    "Issue",
    "Review",
)

STATUS_COLUMN = 0
ISSUE_COLUMN = 6

SET_COLUMNS: tuple[str, ...] = (
    "Set",
    "Description",
    "Attendance file",
    "Candidates",
    "Present",
    "Absent",
    "Status",
)
"""The per-Set table's columns.

The result template is **not** a column. It looked like a second attendance
file - the same kind of workbook name in the same kind of cell - and it is
not something an operator reconciling attendance acts on. It is shown in the
selected set's detail line and in the Status cell's tooltip instead, and the
Reports stage is where it is chosen."""

SET_STATUS_COLUMN = 6

SET_TABLE_VISIBLE_ROWS = 4
"""How many set rows are shown before the set table scrolls. Most projects
have one to three sets; a fifty-set project scrolls rather than pushing the
work area off the screen."""

NO_SETS_TEXT = (
    "No sets are defined for this project yet. Attendance below applies to the "
    "whole project. To give each post or paper its own attendance file, define "
    "the sets in File > Project Configuration..."
)

NO_ATTENDANCE_CELL = "None assigned"

UNASSIGNED_ROSTER_TEXT = (
    "This project has a candidate list from before attendance was per-set. It "
    "has deliberately not been attached to any set - choose which set it "
    "belongs to, or import a fresh list for each set."
)

UNRECOGNISED: tuple[ReconciliationStatus, ...] = (
    ReconciliationStatus.UNKNOWN_ID,
    ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
)

ALL_CANDIDATES = "All candidates"
EXCEPTIONS_ONLY = "Exceptions only"

_STATUS_FILTERS: tuple[tuple[str, tuple[ReconciliationStatus, ...]], ...] = (
    (ALL_CANDIDATES, ()),
    (EXCEPTIONS_ONLY, ()),
    ("Missing script", (ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,)),
    (
        "Script found — set unresolved",
        (ReconciliationStatus.SCRIPT_SET_UNRESOLVED,),
    ),
    (
        "Rejected — rescan required",
        (ReconciliationStatus.RESCAN_REQUIRED,),
    ),
    ("Absent but script found", (ReconciliationStatus.ABSENT_WITH_SCRIPT,)),
    ("Unrecognised ID", UNRECOGNISED),
    ("Duplicate scripts", (ReconciliationStatus.DUPLICATE_SCRIPT,)),
    ("Matched", (ReconciliationStatus.MATCHED,)),
    ("Absent, confirmed", (ReconciliationStatus.ABSENT_CONFIRMED,)),
)

_RESOLUTION_FILTERS: tuple[tuple[str, tuple[ResolutionState, ...]], ...] = (
    ("Any review state", ()),
    ("Needs review", (ResolutionState.OPEN,)),
    ("Resolved", (ResolutionState.RESOLVED,)),
    ("Accepted as-is", (ResolutionState.DISMISSED,)),
)

_SERIOUS: frozenset[ReconciliationStatus] = frozenset(
    {ReconciliationStatus.ABSENT_WITH_SCRIPT, ReconciliationStatus.DUPLICATE_SCRIPT}
)
"""Contradictions - two records that cannot both be true. Drawn in the error
colour; everything else that needs review is amber."""

_MARKERS: dict[ReconciliationStatus, str] = {
    ReconciliationStatus.MATCHED: "✓",
    ReconciliationStatus.ABSENT_CONFIRMED: "○",
    ReconciliationStatus.PRESENT_WITHOUT_SCRIPT: "⚠",
    ReconciliationStatus.UNKNOWN_ID: "⚠",
    ReconciliationStatus.UNRESOLVED_CANDIDATE_ID: "⚠",
    ReconciliationStatus.ABSENT_WITH_SCRIPT: "!",
    ReconciliationStatus.DUPLICATE_SCRIPT: "!",
    ReconciliationStatus.RESCAN_REQUIRED: "✖",
    ReconciliationStatus.SCRIPT_SET_UNRESOLVED: "◐",
}
"""A glyph beside every status word, so the state is never colour alone."""

_EXPLANATIONS: dict[ReconciliationStatus, str] = {
    ReconciliationStatus.PRESENT_WITHOUT_SCRIPT: (
        "The sheet may never have been scanned, its Student ID may be unread "
        "or incomplete, or the candidate may have filled in another roll "
        "number. The scripts under <b>Where to look</b> are the likeliest "
        "places it is filed; nothing is reassigned automatically."
    ),
    ReconciliationStatus.ABSENT_WITH_SCRIPT: (
        "Another candidate may have filled in this roll number by mistake. "
        "Inspect the script: if the roll number on it is not this candidate's, "
        "correct the Student ID; if this candidate really did attend, override "
        "the attendance instead. Attendance is never changed to match the scan, "
        "nor the scan to match attendance."
    ),
    ReconciliationStatus.UNKNOWN_ID: (
        "The roll number read is not on this set's candidate list. It may have "
        "been misread, mistyped by the candidate, or belong to another set. "
        "Inspect the script and correct the Student ID if it is wrong."
    ),
    ReconciliationStatus.UNRESOLVED_CANDIDATE_ID: (
        "Part of this script's Student ID could not be read. Inspect the "
        "script and type the complete ID - the same correction the Resolve "
        "stage records."
    ),
    ReconciliationStatus.DUPLICATE_SCRIPT: (
        "More than one script reads as this ID. One may be an accidental "
        "re-scan (set it aside) or another candidate's sheet (correct its "
        "Student ID). Nothing is chosen for you."
    ),
    ReconciliationStatus.RESCAN_REQUIRED: (
        "This candidate's script <b>was received</b>, but it was rejected as "
        "unusable on the Resolve stage and does not count. Rescan the sheet, "
        "then confirm the rescan under <b>Resolve &gt; Rejected / Rescan</b>. "
        "This is not the same as no script having arrived."
    ),
    ReconciliationStatus.SCRIPT_SET_UNRESOLVED: (
        "A script with exactly this Student ID exists, but its set code is not "
        "settled (or names no defined set), so no set reconciles it yet. "
        "Settle its set code on the Resolve stage; it will then appear here."
    ),
}


_LOOK_FOR_SCRIPT: tuple[ReconciliationStatus, ...] = (
    ReconciliationStatus.PRESENT_WITHOUT_SCRIPT,
    ReconciliationStatus.SCRIPT_SET_UNRESOLVED,
)
"""A candidate with no valid script here whose sheet may exist elsewhere - the
entries *Where to look* searches scripts for. A rejected sheet's candidate is
not among them: their script is known, and its rescan is found on Resolve."""


_CHIP_FILTERS: dict[str, str] = {
    "matched": "Matched",
    "missing": "Missing script",
    "absent": "Absent but script found",
    "unrecognised": "Unrecognised ID",
    "duplicate": "Duplicate scripts",
}
"""Which status filter each summary chip selects."""


@dataclass
class AttendancePageState:
    """Everything the page is currently looking at.

    Attributes:
        session: The open project, or ``None``.
        sets: Every defined set's attendance state, in the operator's order.
        selected_set_id: Which set the reconciliation below belongs to.
            ``None`` means the project's unscoped list, never "whichever set
            happened to be imported last".
        roster: The selected set's candidate list, or ``None``. **Never
            another set's.**
        template: The project's template, which the script inspector needs to
            re-read a sheet. ``None`` until one is known.
        entries: The rows the table shows, in order.
        all_entries: Every entry of the current reconciliation, unfiltered -
            what the investigation leads are drawn from. Re-read whenever the
            reconciliation changes, not on every filter change.
        scope: How the batch's scripts divide around the selected set - in
            it, in other sets, or with a set code still to settle. Cached on
            the same terms as :attr:`all_entries`.
    """

    session: ProjectSession | None = None
    sets: tuple[SetAttendanceStatus, ...] = ()
    selected_set_id: str | None = None
    roster: RosterSummary | None = None
    batch_id: str | None = None
    operator: str = ""
    template: OmrTemplate | None = None
    entries: list[ReconciliationEntry] = field(default_factory=list)
    all_entries: list[ReconciliationEntry] | None = None
    scope: reconciliation_store.SetScriptScope | None = None
    outside: tuple[OutOfSetScript, ...] | None = None

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


def _section_heading(text: str) -> QLabel:
    """A small uppercase heading - a group box's title without the box."""
    label = QLabel(text.upper())
    label.setObjectName("attendanceSectionHeading")
    return label


class AttendancePage(WorkflowPage):
    """Import a candidate list, reconcile it, and investigate what disagrees."""

    roster_imported = Signal(int)
    """Emitted with the new roster's id after an import."""

    reconciled = Signal()
    """Emitted whenever the reconciliation table has been rebuilt."""

    resolution_recorded = Signal()
    """Emitted after an operator decision has been stored - including a
    Student ID or set code corrected from the inspector."""

    split_ratio_changed = Signal(float)
    """Emitted, settled, after the operator moves the work-area divider: the
    table's share of the width. The window persists it; the page never writes
    a configuration file."""

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setStyleSheet(ATTENDANCE_STAGE_STYLESHEET)
        self.state = AttendancePageState()
        self.last_template_blocker = ""
        """Why the last assigned file did not become the set's result template."""
        self._worker: ReconcileWorker | None = None
        self._workers: list[ReconcileWorker] = []
        self._after_correction: tuple[str, int, int] | None = None
        """``(candidate_id, row, scan_id)`` of the entry an inspector correction
        was made from, while the reconciliation it triggered is running."""
        self._chips: dict[str, QPushButton] = {}

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(Spacing.XS)
        top_layout.addWidget(self._build_roster_bar())
        top_layout.addWidget(self._build_summary())

        work = QSplitter(Qt.Orientation.Horizontal)
        work.setObjectName("reconciliationSplitter")
        work.setChildrenCollapsible(False)
        work.addWidget(self._build_table_panel())
        work.addWidget(self._build_detail_panel())
        work.setStretchFactor(0, 3)
        work.setStretchFactor(1, 2)
        work.setSizes([600, 400])
        self.work_splitter = work
        self._split_ratio: float | None = None
        # The ratio is re-applied whenever the splitter itself is resized, so
        # it holds from the first real layout and across window resizes.
        work.installEventFilter(self)
        # Dragging the divider emits a stream of moves; the preference is
        # written once, when the operator lets go.
        self._split_timer = QTimer(self)
        self._split_timer.setSingleShot(True)
        self._split_timer.setInterval(SPLIT_SETTLE_MS)
        self._split_timer.timeout.connect(self._emit_split_ratio)
        work.splitterMoved.connect(lambda *_args: self._split_timer.start())

        # The set section takes exactly the height it needs and no more; every
        # remaining pixel goes to the work area, which is where the work is.
        top.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.body.addWidget(top)
        self.body.addWidget(work, stretch=1)

        self._install_shortcuts()
        self._update_enabled()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_roster_bar(self) -> QWidget:
        """The exam, one row per Set, and the commands that change them."""
        box = QWidget()
        box.setObjectName("candidateRosterBox")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)

        heading = QHBoxLayout()
        heading.setSpacing(Spacing.MD)
        heading.addWidget(_section_heading("Attendance by set"))
        self.exam_label = QLabel("")
        self.exam_label.setObjectName("attendanceExamNameLabel")
        self.exam_label.setTextFormat(Qt.TextFormat.RichText)
        heading.addWidget(self.exam_label, stretch=1)
        layout.addLayout(heading)

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
        header = self.set_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.set_table.itemSelectionChanged.connect(self._on_set_selection_changed)
        layout.addWidget(self.set_table)

        self.unassigned_label = QLabel(UNASSIGNED_ROSTER_TEXT)
        self.unassigned_label.setObjectName("unassignedRosterLabel")
        self.unassigned_label.setWordWrap(True)
        self.unassigned_label.setStyleSheet(f"color: {Color.DESTRUCTIVE};")
        self.unassigned_label.setVisible(False)
        layout.addWidget(self.unassigned_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(Spacing.SM)
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
        self.reconcile_button.setProperty(VARIANT_PROPERTY, VARIANT_PRIMARY)
        self.reconcile_button.setToolTip(
            "Match the scripts in the current batch against the selected set's "
            "candidate list."
        )
        self.reconcile_button.clicked.connect(self.reconcile)
        buttons.addWidget(self.reconcile_button)
        layout.addLayout(buttons)

        self.roster_label = QLabel("No candidate list imported")
        self.roster_label.setObjectName("activeRosterLabel")
        self.roster_label.setTextFormat(Qt.TextFormat.RichText)
        self.roster_label.setWordWrap(True)
        layout.addWidget(self.roster_label)

        self.template_label = QLabel("")
        self.template_label.setObjectName("setTemplateLabel")
        self.template_label.setTextFormat(Qt.TextFormat.RichText)
        self.template_label.setWordWrap(True)
        self.template_label.setStyleSheet(f"color: {Color.TEXT_TERTIARY};")
        layout.addWidget(self.template_label)
        return box

    def _build_summary(self) -> QWidget:
        """The counts, as chips that filter the table, and one line of verdict."""
        bar = QWidget()
        bar.setObjectName("reconciliationSummaryBox")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)
        for key, label, tooltip in (
            ("matched", "Matched", "Expected present, exactly one script."),
            ("missing", "Missing script", "Expected present, no script found."),
            (
                "absent",
                "Absent + script",
                "Marked absent, yet a script reads as this candidate.",
            ),
            (
                "unrecognised",
                "Unrecognised",
                "A script whose ID is not on the list, or not yet fully read.",
            ),
            ("duplicate", "Duplicates", "More than one script under one ID."),
        ):
            chip = QPushButton(f"{label}  -")
            chip.setObjectName("attendanceCountChip")
            chip.setProperty("chipKey", key)
            chip.setCheckable(True)
            chip.setToolTip(tooltip + " Click to show only these.")
            chip.clicked.connect(lambda _checked=False, k=key: self.filter_by_chip(k))
            layout.addWidget(chip)
            self._chips[key] = chip
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("reconciliationSummaryLabel")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label, stretch=1)
        return bar

    def _build_table_panel(self) -> QWidget:
        """The filter row and the reconciliation table - the main work area."""
        panel = QWidget()
        panel.setObjectName("reconciliationTablePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)

        filters = QHBoxLayout()
        filters.setSpacing(Spacing.SM)
        self.status_filter = QComboBox()
        self.status_filter.setObjectName("reconciliationStatusFilter")
        for label, _ in _STATUS_FILTERS:
            self.status_filter.addItem(label)
        self.status_filter.setCurrentIndex(1)  # Exceptions first: that is the work.
        self.status_filter.currentIndexChanged.connect(self._on_filter_changed)
        filters.addWidget(self.status_filter, stretch=2)

        self.resolution_filter = QComboBox()
        self.resolution_filter.setObjectName("reconciliationResolutionFilter")
        for label, _ in _RESOLUTION_FILTERS:
            self.resolution_filter.addItem(label)
        self.resolution_filter.currentIndexChanged.connect(self._on_filter_changed)
        filters.addWidget(self.resolution_filter, stretch=1)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("reconciliationSearchBox")
        self.search_box.setPlaceholderText("Search ID, name or recognised ID  (Ctrl+F)")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.refresh_table)
        filters.addWidget(self.search_box, stretch=3)
        layout.addLayout(filters)

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
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # The issue sentence takes what is left and elides; its full text is
        # the cell's tooltip. Everything before it is short and fixed.
        header.setSectionResizeMode(ISSUE_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(48)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(lambda _index: self.inspect_primary())
        layout.addWidget(self.table, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(Spacing.SM)
        self.table_count_label = QLabel("")
        self.table_count_label.setObjectName("reconciliationCountLabel")
        self.table_count_label.setTextFormat(Qt.TextFormat.RichText)
        self.table_count_label.setWordWrap(True)
        footer.addWidget(self.table_count_label, stretch=1)
        self.previous_unresolved_button = QPushButton("Previous unresolved")
        self.previous_unresolved_button.setObjectName("previousUnresolvedEntryButton")
        self.previous_unresolved_button.setToolTip(
            "Back to the previous row in this view that still needs review (Ctrl+Up)"
        )
        self.previous_unresolved_button.clicked.connect(self.select_previous_unresolved)
        footer.addWidget(self.previous_unresolved_button)
        self.next_unresolved_button = QPushButton("Next unresolved")
        self.next_unresolved_button.setObjectName("nextUnresolvedEntryButton")
        self.next_unresolved_button.setToolTip(
            "On to the next row in this view that still needs review (Ctrl+Down)"
        )
        self.next_unresolved_button.clicked.connect(self.select_next_unresolved)
        footer.addWidget(self.next_unresolved_button)
        layout.addLayout(footer)
        self.navigation_note = QLabel("")
        self.navigation_note.setObjectName("reconciliationNavigationNote")
        self.navigation_note.setStyleSheet(f"color: {Color.STATUS_BUSY};")
        self.navigation_note.setVisible(False)
        layout.addWidget(self.navigation_note)
        return panel

    def _build_detail_panel(self) -> QWidget:
        """The selected entry: what is wrong, its scripts, the evidence, and what to do.

        Scrolls as a whole, and nothing else on the page does, so however
        short the window every control needed to finish a correction is one
        scroll away - never clipped below the bottom edge.
        """
        scroll = QScrollArea()
        scroll.setObjectName("reconciliationDetailScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        panel = QWidget()
        panel.setObjectName("reconciliationDetailPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(Spacing.SM, 0, Spacing.SM, Spacing.SM)
        layout.setSpacing(Spacing.XS)

        self.detail_label = QLabel("Select a row to see what needs attention.")
        self.detail_label.setObjectName("reconciliationDetailLabel")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.detail_label)

        layout.addWidget(_section_heading("Scripts"))
        self.scripts_list = QListWidget()
        self.scripts_list.setObjectName("entryScriptsList")
        self.scripts_list.setToolTip(
            "Every script attributed to this entry, including any set aside. "
            "Nothing here is ever deleted."
        )
        self.scripts_list.currentRowChanged.connect(lambda _row: self._update_enabled())
        self.scripts_list.itemActivated.connect(lambda _item: self.inspect_selected_script())
        layout.addWidget(self.scripts_list)

        script_actions = QHBoxLayout()
        self.inspect_button = QPushButton(load_icon("scan-line"), "Inspect / Correct Script")
        self.inspect_button.setObjectName("inspectScriptButton")
        self.inspect_button.setProperty(VARIANT_PROPERTY, VARIANT_PRIMARY)
        self.inspect_button.setToolTip(
            "Open the selected script's original scan and its Student ID and "
            "set code, here, to check and correct them. (Enter)"
        )
        self.inspect_button.clicked.connect(self.inspect_selected_script)
        script_actions.addWidget(self.inspect_button)
        script_actions.addStretch(1)
        layout.addLayout(script_actions)

        self.leads_heading = _section_heading("Where to look")
        layout.addWidget(self.leads_heading)
        self.leads_list = QListWidget()
        self.leads_list.setObjectName("investigationLeadsList")
        self.leads_list.setToolTip(
            "Suggestions only, ranked by how many digits differ. Nothing is "
            "reassigned until you inspect a script and correct it."
        )
        self.leads_list.currentRowChanged.connect(lambda _row: self._update_enabled())
        self.leads_list.itemActivated.connect(lambda _item: self.follow_selected_lead())
        layout.addWidget(self.leads_list)
        lead_actions = QHBoxLayout()
        self.lead_button = QPushButton("Inspect Script")
        self.lead_button.setObjectName("followLeadButton")
        self.lead_button.clicked.connect(self.follow_selected_lead)
        lead_actions.addWidget(self.lead_button)
        lead_actions.addStretch(1)
        layout.addLayout(lead_actions)
        self.leads_empty_label = QLabel("")
        self.leads_empty_label.setObjectName("investigationLeadsEmptyLabel")
        self.leads_empty_label.setWordWrap(True)
        self.leads_empty_label.setStyleSheet(f"color: {Color.TEXT_TERTIARY};")
        layout.addWidget(self.leads_empty_label)

        self.inspector_heading = _section_heading("Scan")
        layout.addWidget(self.inspector_heading)
        self.inspector = ScriptInspector()
        self.inspector.corrected.connect(self._on_script_corrected)
        # Once the sheet has been read, bring it into view: the operator asked
        # to see the evidence, and it may be below the fold.
        self.inspector.loaded.connect(self._show_loaded_scan)
        layout.addWidget(self.inspector)

        layout.addWidget(_section_heading("Other decisions"))
        layout.addWidget(self._build_actions())

        self.history_label = QLabel("")
        self.history_label.setObjectName("reconciliationHistoryLabel")
        self.history_label.setWordWrap(True)
        self.history_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.history_label)
        layout.addStretch(1)

        scroll.setWidget(panel)
        scroll.setMinimumWidth(360)
        self.detail_scroll = scroll
        return scroll

    def _build_actions(self) -> QWidget:
        """The reconciliation decisions an operator can record."""
        box = QWidget()
        box.setObjectName("reconciliationActionBox")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)

        self.operator_label = QLabel("")
        self.operator_label.setObjectName("reconciliationOperatorLabel")
        self.operator_label.setWordWrap(True)
        layout.addWidget(self.operator_label)

        assign_row = QHBoxLayout()
        self.assign_edit = QLineEdit()
        self.assign_edit.setObjectName("assignCandidateEdit")
        self.assign_edit.setPlaceholderText("Candidate ID to attribute the script to...")
        assign_row.addWidget(self.assign_edit, stretch=1)
        self.assign_button = QPushButton("Confirm Script Assignment")
        self.assign_button.setObjectName("assignScriptButton")
        self.assign_button.setToolTip(
            "Attribute the selected script to this candidate without changing "
            "its Student ID - for a sheet whose roll number is right but belongs "
            "elsewhere. To fix a wrongly filled or misread roll number, correct "
            "the Student ID under Scan instead."
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
            "Record that this has been investigated and nothing more can be done "
            "- for example, no scan was found. It stays visible but stops "
            "counting as outstanding work."
        )
        self.dismiss_button.clicked.connect(self.toggle_dismissed)
        buttons.addWidget(self.dismiss_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        reason_row = QHBoxLayout()
        reason_row.addWidget(QLabel("Reason"))
        self.reason_combo = QComboBox()
        self.reason_combo.setObjectName("reconciliationReasonCombo")
        for reason in ReconciliationReason:
            self.reason_combo.addItem(reason.label, reason.value)
        reason_row.addWidget(self.reason_combo, stretch=1)
        self.reason_text = QLineEdit()
        self.reason_text.setObjectName("reconciliationReasonText")
        self.reason_text.setPlaceholderText("Optional note; required for 'Other'.")
        reason_row.addWidget(self.reason_text, stretch=1)
        layout.addLayout(reason_row)
        return box

    def _install_shortcuts(self) -> None:
        """Ctrl+F finds; Enter on the table inspects. Nothing the window uses."""
        find = QShortcut(QKeySequence.StandardKey.Find, self)
        find.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find.activated.connect(self.focus_search)
        # The same keys as the Resolve stage's next/previous unresolved.
        for keys, slot in (
            (QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Down), self.select_next_unresolved),
            (QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Up), self.select_previous_unresolved),
        ):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        # Enter is taken from the table's own key events rather than a
        # shortcut: a QTableWidget consumes Return itself, and a shortcut only
        # fires in an active window.
        self.table.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        """Enter on the table inspects; a resized work area keeps its divider."""
        if watched is self.work_splitter and event.type() == QEvent.Type.Resize:
            handled = super().eventFilter(watched, event)
            if isinstance(event, QResizeEvent):
                handle = self.work_splitter.handleWidth()
                self._apply_split_ratio(max(event.size().width() - handle, 0))
            return handled
        if (
            watched is self.table
            and event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
        ):
            self.inspect_primary()
            return True
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------------
    # The work-area divider
    # ------------------------------------------------------------------
    def split_ratio(self) -> float | None:
        """The table's current share of the work area, or ``None`` before layout."""
        sizes = self.work_splitter.sizes()
        total = sum(sizes)
        return sizes[0] / total if total > 0 else None

    def set_split_ratio(self, ratio: float | None) -> None:
        """Adopt a remembered divider position; the default when there is none.

        Clamped to :data:`~omr_scanner.config.app_config.MIN_SPLIT_RATIO` -
        :data:`~omr_scanner.config.app_config.MAX_SPLIT_RATIO`, and applied once
        the page has a width, so a ratio saved on a wide monitor still leaves
        the detail pane its minimum on a narrow one.
        """
        self._split_ratio = (
            None if ratio is None else max(MIN_SPLIT_RATIO, min(MAX_SPLIT_RATIO, ratio))
        )
        self._apply_split_ratio()

    def _apply_split_ratio(self, total: int | None = None) -> None:
        """Divide the work area by the remembered ratio - the default if none."""
        width = total if total is not None else sum(self.work_splitter.sizes())
        if width <= 0:
            return
        ratio = self._split_ratio if self._split_ratio is not None else DEFAULT_SPLIT_RATIO
        right = max(width - int(width * ratio), self.detail_scroll.minimumWidth())
        self.work_splitter.setSizes([max(width - right, 0), right])

    def _emit_split_ratio(self) -> None:
        ratio = self.split_ratio()
        if ratio is not None:
            self._split_ratio = ratio
            self.split_ratio_changed.emit(ratio)

    # ------------------------------------------------------------------
    # Next / previous unresolved
    # ------------------------------------------------------------------
    def select_next_unresolved(self) -> bool:
        """Select the next row in this view that still needs review."""
        return self._select_unresolved(step=1)

    def select_previous_unresolved(self) -> bool:
        """Select the previous row in this view that still needs review."""
        return self._select_unresolved(step=-1)

    def _select_unresolved(self, *, step: int) -> bool:
        """Move to the nearest row needing review, within the current filter.

        The same rules as the Resolve stage: only rows the current filter and
        search show are visited; a row whose exception has been dealt with
        (resolved, or accepted as-is) is passed over; at the end of the view
        the selection wraps and the page says so; with nothing left it stays
        put and says that.
        """
        total = len(self.state.entries)
        start = self.table.currentRow()
        for offset in range(1, total + 1):
            row = (start + step * offset) % total
            if self.state.entries[row].needs_attention:
                wrapped = (row <= start) if step > 0 else (row >= start)
                self.table.selectRow(row)
                self._note_navigation(
                    (
                        "Reached the end of this view - continued from the top."
                        if step > 0
                        else "Reached the top of this view - continued from the end."
                    )
                    if wrapped and start >= 0 and row != start
                    else ""
                )
                return True
        self._note_navigation("Nothing in this view still needs review.")
        return False

    def _note_navigation(self, text: str) -> None:
        self.navigation_note.setText(text)
        self.navigation_note.setVisible(bool(text))

    def focus_search(self) -> None:
        """Put the keyboard in the search box, selecting what is there."""
        self.search_box.setFocus()
        self.search_box.selectAll()

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
        self.state.all_entries = None
        self.state.scope = None
        self.state.outside = None
        self.state.template = None
        if session is not None:
            self.state.batch_id = self._latest_batch()
            self.state.template = self._project_template(session)
        self.inspector.clear()
        self._refresh_inspector_context()
        self.refresh_sets()
        self.refresh_table()
        self._update_enabled()

    @staticmethod
    def _project_template(session: ProjectSession) -> OmrTemplate | None:
        """The project's own template, for re-reading a sheet on inspection."""
        try:
            path = resolve_active_template(session.project)
            return load_template(path) if path is not None else None
        except (OMRScannerError, OSError) as exc:  # pragma: no cover - defensive
            _LOGGER.info("Project template not available for inspection: %s", exc)
            return None

    def set_template(self, template: OmrTemplate | None) -> None:
        """Adopt the template the batch was read with (from the window)."""
        if template is not None:
            self.state.template = template
            self._refresh_inspector_context()

    def _refresh_inspector_context(self) -> None:
        self.inspector.set_context(
            self.database, self.state.batch_id, self.state.template, self.state.operator
        )

    # ------------------------------------------------------------------
    # Sets
    # ------------------------------------------------------------------
    def refresh_sets(self) -> tuple[SetAttendanceStatus, ...]:
        """Re-read every set's attendance state and rebuild the set table.

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
            self.state.selected_set_id = None
            self._adopt_selected_roster()

        self._refresh_unassigned_banner()
        return self.state.sets

    def _rebuild_set_table(self) -> None:
        """Fill the set table from :attr:`AttendancePageState.sets`."""
        session = self.state.session
        self.exam_label.setText(
            f"Exam: <b>{html.escape(session.exam_name)}</b>" if session is not None else ""
        )
        self.no_sets_label.setVisible(session is not None and not self.state.has_sets)
        self.set_table.setVisible(self.state.has_sets)

        self.set_table.blockSignals(True)
        self.set_table.setRowCount(len(self.state.sets))
        for row, status in enumerate(self.state.sets):
            roster = status.roster
            values = (
                status.exam_set.display_label,
                status.exam_set.description,
                status.attendance_file or NO_ATTENDANCE_CELL,
                str(status.candidate_count) if status.has_attendance else "",
                str(roster.expected_present) if roster is not None else "",
                str(roster.expected_absent) if roster is not None else "",
                status.describe(),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, status.exam_set.set_id)
                    item.setToolTip(f"Internal id: {status.exam_set.set_id}")
                if column == 2:
                    if not status.has_attendance:
                        item.setToolTip(
                            "This set has no attendance file. Choose one for it - "
                            "another set's file is never used in its place."
                        )
                    elif roster is not None:
                        # The name is what fits; the path is what tells two
                        # files of the same name apart.
                        item.setToolTip(roster.source_path or roster.source_name)
                if column == SET_STATUS_COLUMN:
                    item.setToolTip(self._template_sentence(status, rich=False))
                self.set_table.setItem(row, column, item)
        self.set_table.blockSignals(False)
        self._fit_set_table()

    def _fit_set_table(self) -> None:
        """Size the set table to its rows, up to a few, then let it scroll."""
        rows = max(1, min(self.set_table.rowCount(), SET_TABLE_VISIBLE_ROWS))
        header = self.set_table.horizontalHeader().height() or 24
        row_height = self.set_table.verticalHeader().defaultSectionSize()
        frame = self.set_table.frameWidth() * 2
        self.set_table.setFixedHeight(header + rows * row_height + frame + 2)

    @staticmethod
    def _template_sentence(status: SetAttendanceStatus, *, rich: bool) -> str:
        """Say which workbook the set's result will be built on, and why."""
        if status.association is not None:
            name = status.template_file
            origin = (
                "the attendance file"
                if status.association.source_kind == set_attendance.SOURCE_KIND_ATTENDANCE
                else "chosen on the Reports stage"
            )
            shown = f"<b>{html.escape(name)}</b>" if rich else name
            return f"Result template: {shown} ({origin})."
        if status.template_blocker:
            return status.template_blocker
        return ""

    def selected_set(self) -> ExamSet | None:
        """The set whose attendance the page is showing, or ``None``."""
        status = self.state.status_for(self.state.selected_set_id)
        return status.exam_set if status is not None else None

    def selected_set_status(self) -> SetAttendanceStatus | None:
        """The selected set's attendance state, or ``None``."""
        return self.state.status_for(self.state.selected_set_id)

    def select_set(self, set_id: str) -> bool:
        """Show one set's attendance and reconciliation. No dialog."""
        for row, status in enumerate(self.state.sets):
            if status.exam_set.set_id == set_id:
                changed = set_id != self.state.selected_set_id
                self.state.selected_set_id = set_id
                # Re-adopted unconditionally: importing a file for the set
                # already selected changes that set's roster without changing
                # which set is selected.
                self._adopt_selected_roster()
                if changed:
                    self.state.all_entries = None
                    self.state.scope = None
                    self.state.outside = None
                    self.inspector.clear()
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
        self.state.all_entries = None
        self.state.scope = None
        self.state.outside = None
        self.inspector.clear()
        self._adopt_selected_roster()
        self.refresh_table()
        self._update_enabled()

    def _adopt_selected_roster(self) -> None:
        """Point the page at the selected set's candidate list, and only that."""
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
        """Attach the project's unscoped candidate list to the selected set."""
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
        self._refresh_inspector_context()

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
        self.state.all_entries = None
        self.state.scope = None
        self.state.outside = None
        self.inspector.clear()
        self._refresh_inspector_context()
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
        """Write the packaged sample to ``destination``."""
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
        roster = self.state.roster
        start = str(Path(roster.source_path).parent) if roster and roster.source_path else ""
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            title,
            start,
            "Candidate lists (*.csv *.xlsx);;CSV (*.csv);;Excel Workbook (*.xlsx)",
        )
        if not chosen:
            return
        self.import_from(Path(chosen))

    def import_from(self, path: Path) -> bool:
        """Open the mapping dialog for ``path`` and import what it confirms.

        The file is imported **for the selected set**, or for the project as a
        whole when no sets are defined.
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
        scope = "this set's active one" if exam_set is not None else "the active one"
        answer = QMessageBox.question(
            self,
            "Replace the candidate list?",
            f"{whose} '{self.state.roster.source_name}' "
            f"({self.state.roster.candidate_count} candidates).\n\n"
            f"Importing another list makes it {scope} and reconciliation "
            "is recomputed against it. The existing list and every decision "
            "already recorded are kept, not merged or deleted."
            + ("\n\nNo other set is affected." if exam_set is not None else "")
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
            source_path: The file it was read from - recorded in full, and what
                lets an ``.xlsx`` also become the set's result template.

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
                    source_path=source_path,
                )
                blocker = ""
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Candidate list not imported", exc.user_message or str(exc)
            )
            return False

        # A new list is a new reconciliation: nothing from the previous one may
        # stand in for it, on screen or in the leads.
        self.state.all_entries = None
        self.state.scope = None
        self.state.outside = None
        self.state.entries = []
        self.inspector.clear()
        self.refresh_sets()
        if exam_set is not None:
            self.select_set(exam_set.set_id)
        self.refresh_table()
        self._update_enabled()
        # Recorded, not shown in a modal here (`import_from` shows it). It is
        # shown on its own line - never in place of the file's name, which is
        # what hid a successful replacement behind a template message.
        self.last_template_blocker = blocker
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
            self._refresh_inspector_context()
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
            self._after_correction = None
            QMessageBox.warning(self, "Reconciliation failed", result.error)
            return
        self.state.all_entries = None
        self.state.scope = None
        self.state.outside = None
        pending = self._after_correction
        self._after_correction = None
        if pending is None:
            self.refresh_table()
        else:
            self._settle_after_correction(*pending)
        self.reconciled.emit()

    def _settle_after_correction(self, candidate_id: str, row: int, scan_id: int) -> None:
        """Rebuild the table after a correction without losing the operator's place.

        The entry stays selected if it is still shown. If the correction
        settled it - the exception has gone, which is the point - the row that
        took its place is selected instead: the next exception, not the top of
        the table. The script being inspected stays on screen either way.
        """
        self.refresh_table(keep=candidate_id, fallback_row=row, keep_inspector=True)
        home = next(
            (
                entry
                for entry in self._entries_for_leads()
                if any(view.script.scan_id == scan_id for view in entry.scripts)
            ),
            None,
        )
        if home is not None:
            # Say where the script went: the row it came from has often just
            # left the filter, and "nothing matches" alone would read as loss.
            self.table_count_label.setText(
                f"{self.table_count_label.text()}<br>"
                f"<span style='color:{Color.STATUS_READY};'>The corrected script is "
                f"now filed under <b>{html.escape(home.candidate_id or '(not read)')}"
                f"</b> ({home.status.label}).</span>"
            )

    def _refresh_summary(self) -> None:
        """Show the counts from the last reconciliation, as chips and one line."""
        database = self.database
        roster = self.state.roster
        counts = (
            reconciliation_store.stored_counts(database, roster.roster_id, self.state.batch_id)
            if database is not None and roster is not None and self.state.batch_id is not None
            else None
        )
        values = {
            "matched": counts.matched if counts else None,
            "missing": counts.present_without_script if counts else None,
            "absent": counts.absent_with_script if counts else None,
            "unrecognised": (
                counts.unknown_id + counts.unresolved_candidate_id if counts else None
            ),
            "duplicate": counts.duplicate_script if counts else None,
        }
        for key, chip in self._chips.items():
            name = chip.text().rsplit("  ", 1)[0]
            value = values[key]
            chip.setText(f"{name}  {value if value is not None else '-'}")
            chip.setEnabled(value is not None)
            _paint_chip(chip, key, value or 0)
        self._sync_chips()

        if counts is None:
            if roster is None:
                self.summary_label.setText("")
            elif self.state.batch_id is None:
                self.summary_label.setText(
                    "No batch has been processed yet. Run a batch on the <b>Scan</b> "
                    "stage, then reconcile."
                )
            else:
                self.summary_label.setText(
                    "Attendance loaded. Click <b>Reconcile</b> to compare attendance "
                    "against scanned scripts."
                )
            return

        if counts.is_clear:
            verdict = (
                f"<span style='color:{Color.STATUS_READY};'><b>Reconciliation "
                "complete. No attendance exceptions need review.</b></span>"
            )
        else:
            verdict = (
                f"<span style='color:{Color.STATUS_ERROR};'><b>{counts.outstanding} "
                "attendance exception(s) still need review.</b></span>"
            )
        self.summary_label.setText(
            f"{verdict}<br><span style='color:{Color.TEXT_TERTIARY};'>"
            f"Registered <b>{counts.registered}</b> · "
            f"Expected present <b>{counts.expected_present}</b> · "
            f"Marked absent <b>{counts.expected_absent}</b> · "
            f"Scripts <b>{counts.scripts}</b>"
            + (f" ({counts.scripts_excluded} set aside)" if counts.scripts_excluded else "")
            + f" · Absent confirmed <b>{counts.absent_confirmed}</b>"
            f" · Resolved <b>{counts.resolved}</b>"
            f" · Accepted as-is <b>{counts.dismissed}</b></span>"
            + self._scope_sentence()
        )
        self._refresh_roster_label(counts.outstanding)

    def _script_scope(self) -> reconciliation_store.SetScriptScope | None:
        """How the batch divides around the selected set, read once per change."""
        database = self.database
        roster = self.state.roster
        if database is None or roster is None or self.state.batch_id is None:
            return None
        if self.state.scope is None:
            self.state.scope = reconciliation_store.script_scope(
                database, roster.roster_id, self.state.batch_id
            )
        return self.state.scope

    def _scope_sentence(self) -> str:
        """Say which of the batch's scripts this set's reconciliation left out, and why.

        Nothing disappears silently: a script of another set is simply not
        this set's, and a script whose set code is not settled is named, with
        where to settle it.
        """
        scope = self._script_scope()
        if scope is None or not scope.set_code:
            return ""
        parts = [f"This set reconciles <b>{scope.in_set}</b> script(s) of the batch"]
        if scope.other_set_total:
            others = ", ".join(
                f"set {html.escape(code)}: {count}" for code, count in scope.other_sets.items()
            )
            parts.append(f"{scope.other_set_total} belong to other sets ({others})")
        if scope.needs_attention:
            waiting = []
            if scope.unresolved:
                waiting.append(f"{scope.unresolved} with a set code not yet resolved")
            if scope.undefined:
                waiting.append(f"{scope.undefined} whose set code is not a defined set")
            parts.append(
                f"<span style='color:{Color.STATUS_BUSY};'><b>"
                + " and ".join(waiting)
                + "</b> - settle them on the <b>Resolve</b> stage</span>"
            )
        if scope.rescan_required:
            parts.append(
                f"<span style='color:{Color.DESTRUCTIVE};'><b>{scope.rescan_required} "
                "rejected - rescan required</b> (not counted; see Resolve &gt; "
                "Rejected / Rescan)</span>"
            )
        if scope.superseded:
            parts.append(f"{scope.superseded} superseded by a confirmed rescan (not counted)")
        return f"<br><span style='color:{Color.TEXT_TERTIARY};'>{' · '.join(parts)}.</span>"

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def filter_by_chip(self, key: str) -> None:
        """Show only one category - or, clicked again, every exception."""
        wanted = _CHIP_FILTERS[key]
        target = wanted if self.status_filter.currentText() != wanted else EXCEPTIONS_ONLY
        self.status_filter.setCurrentIndex(self.status_filter.findText(target))

    def _sync_chips(self) -> None:
        """Check the chip whose category the table is showing, if any."""
        current = self.status_filter.currentText()
        for key, chip in self._chips.items():
            chip.blockSignals(True)
            chip.setChecked(_CHIP_FILTERS[key] == current)
            chip.blockSignals(False)

    def _on_filter_changed(self) -> None:
        self._sync_chips()
        self.refresh_table()

    def _current_filter(self) -> reconciliation_store.EntryFilter:
        """Build a filter from the three controls."""
        label, statuses = _STATUS_FILTERS[max(0, self.status_filter.currentIndex())]
        _, resolutions = _RESOLUTION_FILTERS[
            max(0, self.resolution_filter.currentIndex())
        ]
        return reconciliation_store.EntryFilter(
            statuses=statuses,
            resolutions=resolutions,
            exceptions_only=label == EXCEPTIONS_ONLY,
            search=self.search_box.text(),
        )

    # ------------------------------------------------------------------
    # The table
    # ------------------------------------------------------------------
    def refresh_table(
        self,
        *_args: object,
        keep: str | None = None,
        fallback_row: int | None = None,
        keep_inspector: bool = False,
    ) -> None:
        """Re-read the reconciliation from the database and rebuild the table.

        Args:
            keep: The candidate to re-select; the current selection by default.
            fallback_row: The row to select when ``keep`` is no longer shown -
                so settling an exception moves to the next one, not the top.
            keep_inspector: Leave the script inspector showing what it shows.
        """
        database = self.database
        roster = self.state.roster
        selected = self._selected_entry()
        wanted = keep if keep is not None else (selected.candidate_id if selected else None)
        row = fallback_row if fallback_row is not None else self.table.currentRow()

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
        self._restore_selection(wanted, row if keep_inspector else None, keep_inspector)
        self._refresh_summary()

    def _rebuild_table(self) -> None:
        """Fill the table from :attr:`AttendancePageState.entries`."""
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(self.state.entries))
        for row, entry in enumerate(self.state.entries):
            colour = _status_colour(entry)
            values = (
                self._status_text(entry),
                entry.candidate_id or "(not read)",
                entry.display_name,
                self._attendance_text(entry),
                self._script_text(entry),
                self._recognised_text(entry),
                self._issue_text(entry),
                entry.resolution.label if entry.status.is_exception else "",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == STATUS_COLUMN:
                    item.setToolTip(self._status_tooltip(entry))
                    if colour is not None:
                        item.setForeground(QColor(colour))
                if column == ISSUE_COLUMN:
                    item.setToolTip(value)
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        outstanding = sum(1 for entry in self.state.entries if entry.needs_attention)
        self.table_count_label.setText(self._empty_or_count(outstanding))

    def _empty_or_count(self, outstanding: int) -> str:
        """The line under the table: a count, or why there is nothing in it."""
        exam_set = self.selected_set()
        if self.state.roster is None:
            if exam_set is not None:
                label = html.escape(exam_set.display_label)
                return (
                    f"No attendance file has been assigned to {label}. Choose one above."
                )
            return "No candidate list imported. Choose an attendance file above."
        if not self.state.entries:
            if self.database is not None and self.state.batch_id is not None and (
                self.state.roster is not None
                and reconciliation_store.stored_counts(
                    self.database, self.state.roster.roster_id, self.state.batch_id
                )
                is not None
            ):
                if self._current_filter().exceptions_only and not self.search_box.text():
                    return "Reconciliation complete. No attendance exceptions found."
                return "Nothing matches this filter."
            return (
                "Attendance loaded. Click <b>Reconcile</b> to compare attendance "
                "against scanned scripts."
            )
        text = f"{len(self.state.entries)} row(s) shown"
        if outstanding:
            text += f" · <b>{outstanding}</b> needing review"
        return text

    @staticmethod
    def _status_text(entry: ReconciliationEntry) -> str:
        """The status word, its glyph, and any co-occurring issues."""
        marker = _MARKERS.get(entry.status, "")
        text = f"{marker} {entry.status.label}".strip()
        if len(entry.issues) > 1:
            others = ", ".join(
                sorted(
                    issue.label for issue in entry.issues if issue.status is not entry.status
                )
            )
            text += f"  (+ {others})"
        return text

    @staticmethod
    def _status_tooltip(entry: ReconciliationEntry) -> str:
        tip = entry.status.description
        if len(entry.issues) > 1:
            others = ", ".join(
                sorted(
                    issue.label for issue in entry.issues if issue.status is not entry.status
                )
            )
            tip += f"\n\nAlso: {others}"
        return tip

    def _issue_text(self, entry: ReconciliationEntry) -> str:
        """One short sentence saying what is wrong with this row."""
        status = entry.status
        if status is ReconciliationStatus.PRESENT_WITHOUT_SCRIPT:
            return "Expected present; no script matched"
        if status is ReconciliationStatus.ABSENT_WITH_SCRIPT:
            ids = self._recognised_text(entry) or entry.candidate_id
            return f"Marked absent; a script reads as {ids}"
        if status is ReconciliationStatus.DUPLICATE_SCRIPT:
            return f"{entry.script_count} scripts read as this ID"
        if status is ReconciliationStatus.UNKNOWN_ID:
            return "Read ID is not on this set's list"
        if status is ReconciliationStatus.UNRESOLVED_CANDIDATE_ID:
            return "Student ID not fully read"
        if status is ReconciliationStatus.RESCAN_REQUIRED:
            return "Script received but rejected; rescan required"
        if status is ReconciliationStatus.SCRIPT_SET_UNRESOLVED:
            return "Script exists; its set assignment is unresolved"
        return ""

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
        """How many scripts count, and how many are set aside or rejected."""
        aside = sum(1 for item in entry.scripts if item.excluded)
        rejected = sum(1 for item in entry.scripts if item.script.rejected)
        extras = []
        if aside:
            extras.append(f"+{aside} set aside")
        if rejected:
            extras.append(f"+{rejected} rejected")
        if extras:
            return f"{entry.script_count} ({', '.join(extras)})"
        return str(entry.script_count)

    def _recognised_text(self, entry: ReconciliationEntry) -> str:
        """What recognition read for this entry's scripts, and any correction."""
        values = []
        for view in entry.scripts:
            machine = view.script.machine_candidate_id or "(not read)"
            if view.script.rejected:
                # For a rejected script the second value is the case's
                # identity, which may be the operator's declaration - not a
                # correction of the scan's Student ID, so it is not shown as one.
                case = view.script.effective_candidate_id
                values.append(
                    f"{machine} (rejected; case {case})" if case and case != machine
                    else f"{machine} (rejected)"
                )
            elif view.script.effective_candidate_id != machine:
                values.append(f"{machine} → {view.script.effective_candidate_id}")
            else:
                values.append(machine)
        return ", ".join(dict.fromkeys(values))

    def _restore_selection(
        self, candidate_id: str | None, fallback_row: int | None, keep_inspector: bool
    ) -> None:
        """Re-select by identity, falling back to a row position when asked.

        By identity first: after a decision the entry may have moved or left
        the filter, and a stale index would show one entry while the detail
        panel described another. The positional fallback is used only after a
        correction, where it means "the next one".
        """
        target = -1
        if candidate_id is not None:
            target = next(
                (
                    row
                    for row, entry in enumerate(self.state.entries)
                    if entry.candidate_id == candidate_id
                ),
                -1,
            )
        if target < 0 and fallback_row is not None and self.state.entries:
            target = min(max(fallback_row, 0), len(self.state.entries) - 1)
        if target >= 0:
            self.table.blockSignals(True)
            self.table.selectRow(target)
            self.table.blockSignals(False)
            self._show_entry(self.state.entries[target], keep_inspector=keep_inspector)
            return
        self._show_entry(None, keep_inspector=keep_inspector)

    def _selected_entry(self) -> ReconciliationEntry | None:
        """The entry the table has selected, if any."""
        row = self.table.currentRow()
        if 0 <= row < len(self.state.entries):
            return self.state.entries[row]
        return None

    def select_candidate(self, candidate_id: str) -> bool:
        """Select one entry by its ID, widening the filter if it is hidden."""
        for row, entry in enumerate(self.state.entries):
            if entry.candidate_id == candidate_id:
                self.table.selectRow(row)
                return True
        self.search_box.blockSignals(True)
        self.search_box.clear()
        self.search_box.blockSignals(False)
        self.status_filter.setCurrentIndex(0)
        for row, entry in enumerate(self.state.entries):
            if entry.candidate_id == candidate_id:
                self.table.selectRow(row)
                return True
        return False

    def _on_selection_changed(self) -> None:
        """Show whatever the table now has selected."""
        self._show_entry(self._selected_entry())

    # ------------------------------------------------------------------
    # The detail panel
    # ------------------------------------------------------------------
    def _outside_scripts(self) -> tuple[OutOfSetScript, ...]:
        """Scripts this set's reconciliation left out, read once per change."""
        if self.state.outside is None:
            database = self.database
            roster = self.state.roster
            self.state.outside = (
                reconciliation_store.out_of_set_scripts(
                    database, roster.roster_id, self.state.batch_id
                )
                if database is not None and roster is not None and self.state.batch_id
                else ()
            )
        return self.state.outside

    def _entries_for_leads(self) -> list[ReconciliationEntry]:
        """Every entry of the current reconciliation, read once per change."""
        if self.state.all_entries is None:
            database = self.database
            roster = self.state.roster
            self.state.all_entries = (
                list(
                    reconciliation_store.list_entries(
                        database, roster.roster_id, self.state.batch_id
                    )
                )
                if database is not None and roster is not None and self.state.batch_id
                else []
            )
        return self.state.all_entries

    def _show_entry(
        self, entry: ReconciliationEntry | None, *, keep_inspector: bool = False
    ) -> None:
        """Describe one entry: the problem, its scripts, where to look, its history."""
        self.scripts_list.clear()
        self.leads_list.clear()
        if not keep_inspector:
            self.inspector.clear()
        if entry is None:
            self.detail_label.setText(
                "Select a row to see what needs attention."
                if self.state.entries
                else ""
            )
            self.history_label.setText("")
            self._show_leads(None, ())
            self._update_enabled()
            return

        self.detail_label.setText(self._describe(entry))
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
            if script.rejected:
                bits.append("REJECTED — RESCAN REQUIRED")
            item = QListWidgetItem(" · ".join(bits))
            item.setData(Qt.ItemDataRole.UserRole, script.scan_id)
            if view.excluded:
                item.setToolTip(
                    "Set aside as an accidental re-scan. The scan, its "
                    "recognition result and this decision are all kept."
                )
            if script.rejected:
                item.setToolTip(
                    "Rejected as unusable on the Resolve stage. It is kept, but "
                    "it does not count until its rescan is confirmed there."
                )
            self.scripts_list.addItem(item)
        if self.scripts_list.count():
            self.scripts_list.setCurrentRow(0)
        _fit_list(self.scripts_list)

        entries = self._entries_for_leads()
        if entry.status in _LOOK_FOR_SCRIPT:
            leads = script_leads(entry, entries, outside=self._outside_scripts())
        elif entry.status in (
            ReconciliationStatus.ABSENT_WITH_SCRIPT,
            ReconciliationStatus.UNKNOWN_ID,
            ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
            ReconciliationStatus.DUPLICATE_SCRIPT,
        ):
            leads = owner_leads(entry, entries)
        else:
            leads = ()
        self._show_leads(entry, leads)
        self.inspector.set_reason_context(
            f"Made from the Attendance stage while investigating "
            f"{entry.candidate_id or 'an unread ID'} ({entry.status.label})."
        )
        if entry.status is ReconciliationStatus.ABSENT_WITH_SCRIPT:
            self.inspector.select_reason(ReasonCode.WRONG_ID_ENTERED)
        elif entry.status in UNRECOGNISED:
            self.inspector.select_reason(ReasonCode.MISCLASSIFICATION)
        self._refresh_history(entry)
        self._update_enabled()

    def _describe(self, entry: ReconciliationEntry) -> str:
        """The problem, in labelled sentences - Candidate, Attendance, Script, Problem."""
        lines: list[str] = []
        heading = (
            f"<b>{html.escape(entry.candidate_id)}</b>"
            + (f" — {html.escape(entry.display_name)}" if entry.display_name else "")
            if entry.candidate_id
            else "<b>A script whose ID was not read</b>"
        )
        colour = _status_colour(entry) or Color.TEXT_PRIMARY
        lines.append(
            f"{heading}<br><span style='color:{colour};'><b>"
            f"{html.escape(_MARKERS.get(entry.status, ''))} {entry.status.label}</b></span>"
            + (
                f" · <i>{entry.resolution.label}</i>"
                if entry.status.is_exception and entry.resolution is not ResolutionState.OPEN
                else ""
            )
        )
        if len(entry.issues) > 1:
            others = ", ".join(
                sorted(
                    issue.label for issue in entry.issues if issue.status is not entry.status
                )
            )
            lines.append(f"<b>This entry also has:</b> {others}")
        if entry.candidate is not None:
            lines.append(
                f"<b>Attendance:</b> <b>{entry.effective_attendance.label}</b> "
                "on the candidate list"
                + (
                    f" <span style='color:{Color.TEXT_TERTIARY};'>(cell read "
                    f"'{html.escape(entry.candidate.imported_value)}')</span>"
                    if entry.candidate.imported_value
                    else ""
                )
            )
            if entry.attendance_was_overridden:
                lines.append(
                    f"A reviewer recorded <b>{entry.effective_attendance.label}</b>"
                    + (f" - {html.escape(entry.reason_text)}" if entry.reason_text else "")
                    + (f" ({html.escape(entry.reviewer)})" if entry.reviewer else "")
                    + f"; the list said {entry.candidate.imported_attendance.label.lower()}, "
                    "and that is kept."
                )
        else:
            lines.append("<b>Attendance:</b> not on this set's candidate list")
        count = entry.script_count
        rejected = sum(1 for view in entry.scripts if view.script.rejected)
        if count == 0 and rejected:
            lines.append(
                f"<b>Script:</b> received, but <b>rejected — rescan required</b> "
                f"({rejected} rejected scan(s); none counts)"
            )
        elif count == 0:
            lines.append("<b>Script:</b> none matched")
        else:
            plural = "script" if count == 1 else "scripts"
            lines.append(
                f"<b>Script:</b> {count} scanned {plural} recognised as "
                f"{html.escape(self._recognised_text(entry) or entry.candidate_id)}"
            )
        lines.append(f"<b>Problem:</b> {entry.status.description}")
        explanation = _EXPLANATIONS.get(entry.status)
        if explanation:
            lines.append(f"<b>Possible explanation:</b> {explanation}")
        return "<br>".join(lines)

    def _show_leads(
        self, entry: ReconciliationEntry | None, leads: tuple[InvestigationLead, ...]
    ) -> None:
        """Fill *Where to look*, or say why it is empty."""
        relevant = entry is not None and entry.status in (
            *_LOOK_FOR_SCRIPT,
            ReconciliationStatus.ABSENT_WITH_SCRIPT,
            ReconciliationStatus.UNKNOWN_ID,
            ReconciliationStatus.UNRESOLVED_CANDIDATE_ID,
            ReconciliationStatus.DUPLICATE_SCRIPT,
        )
        self.leads_heading.setVisible(relevant)
        self.leads_list.setVisible(relevant and bool(leads))
        self.lead_button.setVisible(relevant and bool(leads))
        self.leads_empty_label.setVisible(relevant and not leads)
        for lead in leads:
            item = QListWidgetItem(lead.describe)
            item.setData(Qt.ItemDataRole.UserRole, lead)
            self.leads_list.addItem(item)
        if leads:
            self.leads_list.setCurrentRow(0)
        _fit_list(self.leads_list)
        if entry is not None and relevant and not leads:
            self.leads_empty_label.setText(
                "No likely script found: nothing unread, unknown or duplicated "
                "is within two digits of this ID. Search the table, or review "
                "unread sheets on the Resolve stage."
                if entry.status in _LOOK_FOR_SCRIPT
                else "No candidate expected present without a script has a "
                "similar ID."
            )
        self.leads_heading.setText(
            "WHERE TO LOOK"
            if entry is None or entry.status in _LOOK_FOR_SCRIPT
            else "WHO ELSE MIGHT HAVE WRITTEN THIS ID"
        )

    def _refresh_history(self, entry: ReconciliationEntry) -> None:
        """Show every decision recorded about this entry."""
        database = self.database
        if database is None:
            self.history_label.setText("")
            return
        records = reconciliation_store.history_for_entry(database, entry)
        if not records:
            self.history_label.setText(
                f"<span style='color:{Color.TEXT_TERTIARY};'><i>No reconciliation "
                "decisions have been recorded for this entry.</i></span>"
            )
            return
        lines = ["<b>History</b>"]
        for record in records:
            when = record.occurred_at.strftime("%Y-%m-%d %H:%M")
            change = (
                f" {html.escape(record.previous_value or '(none)')} &rarr; "
                f"{html.escape(record.new_value)}"
                if record.new_value and record.new_value != record.previous_value
                else ""
            )
            reason = f" - {html.escape(record.reason)}" if record.reason else ""
            lines.append(
                f"{when} · <b>{record.action.label}</b> by "
                f"{html.escape(record.reviewer or '(unnamed)')}{change}{reason}"
            )
        self.history_label.setText("<br>".join(lines))

    def _selected_scan_id(self) -> int | None:
        """The scan id of the script selected in the detail list."""
        row = self.scripts_list.currentRow()
        if row < 0:
            return None
        value = self.scripts_list.item(row).data(Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def _selected_lead(self) -> InvestigationLead | None:
        row = self.leads_list.currentRow()
        if row < 0:
            return None
        value = self.leads_list.item(row).data(Qt.ItemDataRole.UserRole)
        return value if isinstance(value, InvestigationLead) else None

    # ------------------------------------------------------------------
    # Inspecting
    # ------------------------------------------------------------------
    def inspect_primary(self) -> bool:
        """Enter: inspect this entry's script, or the first lead's."""
        if self._selected_scan_id() is not None:
            return self.inspect_selected_script()
        lead = self._selected_lead()
        if lead is not None and lead.scan_id is not None:
            return self.follow_selected_lead()
        return False

    def inspect_selected_script(self) -> bool:
        """Open the selected script in the inspector below."""
        scan_id = self._selected_scan_id()
        entry = self._selected_entry()
        if scan_id is None or entry is None:
            return False
        view = next((item for item in entry.scripts if item.script.scan_id == scan_id), None)
        name = view.script.source_name if view is not None else ""
        return self.inspect_scan(scan_id, title=name or f"Scan {scan_id}")

    def follow_selected_lead(self) -> bool:
        """Inspect a suggested script, or go to a suggested candidate."""
        lead = self._selected_lead()
        if lead is None:
            return False
        if lead.scan_id is not None:
            return self.inspect_scan(
                lead.scan_id,
                title=f"{lead.source_name or f'Scan {lead.scan_id}'} "
                f"(filed under {lead.candidate_id or 'an unread ID'})",
            )
        return self.select_candidate(lead.candidate_id)

    def inspect_scan(self, scan_id: int, *, title: str = "") -> bool:
        """Load any script of this batch into the inspector."""
        self._refresh_inspector_context()
        started = self.inspector.show_script(scan_id, title=title)
        # Bring the evidence into view in the detail pane.
        self.detail_scroll.ensureWidgetVisible(self.inspector_heading, 0, 0)
        return started

    def _show_loaded_scan(self) -> None:
        """Scroll the freshly loaded scan into view in the detail pane."""
        self.detail_scroll.ensureWidgetVisible(self.inspector.view_tabs, 0, 0)

    def _on_script_corrected(self, scan_id: int) -> None:
        """A Student ID or set code was corrected: reconcile again, keep our place."""
        entry = self._selected_entry()
        self._after_correction = (
            entry.candidate_id if entry is not None else "",
            max(self.table.currentRow(), 0),
            scan_id,
        )
        _LOGGER.info("Scan %d corrected from Attendance; reconciling again", scan_id)
        self.resolution_recorded.emit()
        if not self.reconcile():
            self._after_correction = None

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
        self.state.all_entries = None
        self.state.scope = None
        self.state.outside = None
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
    # Labels and enablement
    # ------------------------------------------------------------------
    def _refresh_roster_label(self, outstanding: int | None = None) -> None:
        """One line: which list is in force, for whom, and how it stands."""
        roster = self.state.roster
        exam_set = self.selected_set()
        status = self.selected_set_status()
        self.template_label.setText(
            self._template_sentence(status, rich=True) if status is not None else ""
        )
        self.template_label.setVisible(bool(self.template_label.text()))
        whose = f"<b>{html.escape(exam_set.display_label)}</b> · " if exam_set else ""
        if roster is None:
            self.roster_label.setText(
                f"{whose}no candidate list imported. Choose an attendance file for "
                "this set to reconcile its scripts against."
                if exam_set is not None
                else "No candidate list imported. Import one to reconcile the "
                "scripts against it."
            )
            self.roster_label.setToolTip("")
            return
        attendance = (
            f" · {roster.expected_present} expected present · "
            f"{roster.expected_absent} absent"
            if roster.has_attendance_column
            else " · no attendance column mapped"
        )
        tail = ""
        if outstanding is not None:
            tail = (
                f" · Reconciled: <b>{outstanding}</b> exception(s)"
                if outstanding
                else " · Reconciled: no exceptions"
            )
        self.roster_label.setText(
            f"{whose}<b>{html.escape(roster.source_name)}</b>"
            + (f" ({html.escape(roster.source_sheet)})" if roster.source_sheet else "")
            + f" · {roster.candidate_count} candidate(s){attendance}{tail}"
        )
        self.roster_label.setToolTip(roster.source_path or roster.source_name)

    def _refresh_operator_label(self) -> None:
        """Say who decisions will be recorded as."""
        if self.state.operator:
            self.operator_label.setText(
                f"Decisions are recorded as: <b>{html.escape(self.state.operator)}</b>"
            )
            self.operator_label.setTextFormat(Qt.TextFormat.RichText)
            self.operator_label.setStyleSheet("")
            return
        self.operator_label.setText(
            "No reviewer name is set. Add one in File > Settings > Reviewer - "
            "a decision cannot be recorded without a name."
        )
        self.operator_label.setStyleSheet(f"color: {Color.DESTRUCTIVE};")

    def _update_enabled(self) -> None:
        """Enable only what the current state allows."""
        session = self.state.session
        has_project = self.database is not None
        writable = has_project and not (session is not None and session.read_only)
        has_roster = self.state.roster is not None
        entry = self._selected_entry()
        has_scripts = bool(entry and entry.scripts)
        registered = bool(entry and entry.is_registered)

        can_choose = writable and (not self.state.has_sets or self.selected_set() is not None)
        self.import_button.setEnabled(can_choose)
        self.assign_existing_button.setEnabled(can_choose)
        self.reconcile_button.setEnabled(has_project and has_roster)
        self.assign_button.setEnabled(has_scripts)
        self.assign_edit.setEnabled(has_scripts)
        self.exclude_button.setEnabled(has_scripts)
        self.attendance_button.setEnabled(registered)
        self.dismiss_button.setEnabled(bool(entry and entry.status.is_exception))
        self.inspect_button.setEnabled(has_scripts and self._selected_scan_id() is not None)
        lead = self._selected_lead()
        self.lead_button.setEnabled(lead is not None)
        self.lead_button.setText(
            "Go To Candidate" if lead is not None and lead.scan_id is None else "Inspect Script"
        )

        if entry is not None and entry.resolution is ResolutionState.DISMISSED:
            self.dismiss_button.setText("Put Back On The List")
        else:
            self.dismiss_button.setText("Accept As-Is")

        scan_id = self._selected_scan_id()
        scripts = entry.scripts if entry else ()
        view = (
            next((item for item in scripts if item.script.scan_id == scan_id), None)
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
        """Wait for every reconciliation and sheet loader this page started."""
        workers = self._workers
        self._worker = None
        self._workers = []
        for worker in workers:
            if worker.isRunning():
                worker.wait(5000)
        self.inspector.shutdown()

    def closeEvent(self, event: object) -> None:
        """Join the workers before the page goes away."""
        self.shutdown()
        super().closeEvent(event)  # type: ignore[arg-type]


def _status_colour(entry: ReconciliationEntry) -> str | None:
    """The status cell's colour, always beside a word - never instead of one.

    Red for a contradiction, amber for something unread or missing, green for
    matched; a decision already recorded is drawn neutral, because it no
    longer needs anybody.
    """
    if entry.status.is_exception and entry.resolution is not ResolutionState.OPEN:
        return Color.TEXT_TERTIARY
    if entry.status in _SERIOUS:
        return Color.STATUS_ERROR
    if entry.status.is_exception:
        return Color.STATUS_BUSY
    if entry.status is ReconciliationStatus.MATCHED:
        return Color.STATUS_READY
    return None


LIST_VISIBLE_ROWS = 4
"""How many rows a detail list shows before it scrolls."""


def _fit_list(widget: QListWidget) -> None:
    """Size a short list to its rows - one to four - rather than a fixed box."""
    rows = max(1, min(widget.count(), LIST_VISIBLE_ROWS))
    row_height = widget.sizeHintForRow(0) if widget.count() else 24
    widget.setFixedHeight(rows * max(row_height, 20) + widget.frameWidth() * 2 + 6)


def _paint_chip(chip: QPushButton, key: str, value: int) -> None:
    """Tint a chip's text by what it counts, and only when it counts something."""
    colour = {
        "matched": Color.STATUS_READY,
        "missing": Color.STATUS_BUSY,
        "unrecognised": Color.STATUS_BUSY,
        "absent": Color.STATUS_ERROR,
        "duplicate": Color.STATUS_ERROR,
    }[key]
    chip.setStyleSheet(f"color: {colour};" if value else f"color: {Color.TEXT_TERTIARY};")
    chip.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


__all__ = ["AttendancePage", "AttendancePageState"]
