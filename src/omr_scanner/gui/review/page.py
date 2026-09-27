"""The Resolve workflow stage: conflict queue and review workspace (Phase 6).

Purpose:
    Put a human in front of every value that decides **which record a sheet
    is** - the student ID, the set code, and sheets that could not be read at
    all - with enough of what the machine saw to decide it properly, and record
    that decision so the final value can always be traced back to a named
    person and a reason.

    An ambiguous or multiply-marked *answer* is not listed here. It is a
    recognition result, it appears in the results and the export as the sheet
    was marked, and nobody has to decide it before the batch can go on. See
    :attr:`~omr_scanner.domain.review.ConflictType.requires_resolution`.

Responsibilities:
    * :class:`ResolvePage` - the queue, the workspace and the actions.

What does NOT belong here:
    * Deciding *what* is a conflict (:mod:`omr_scanner.services.conflict_policy`)
      or *storing* a decision (:mod:`omr_scanner.services.review_store`). This
      page calls those and draws the result; it contains no resolution logic of
      its own, and in particular there is no code path here that writes a
      recognised value anywhere.
    * ``cv2``, ``numpy``, ``omr_scanner.imaging`` or ``omr_scanner.recognition``
      imports - enforced by ``tests/unit/test_architecture.py`` exactly as for
      every other page.

Why the queue holds value objects and pages its reads:
    A ten-thousand-sheet batch can carry thousands of conflicts. The queue
    therefore asks the database for one page at a time, already filtered and
    ordered in SQL, and holds detached
    :class:`~omr_scanner.services.review_store.ConflictRecord` values rather
    than attached ORM rows. Nothing here loads an image for a row that is not
    being looked at: the sheet is fetched by
    :class:`~omr_scanner.gui.review.worker.SheetWorker` only when a conflict is
    selected, and only when that conflict is on a different sheet from the last
    one - so walking a sheet's ten conflicts decodes the image once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.review import (
    RESOLUTION_TYPES,
    ConflictState,
    ConflictType,
    FieldKind,
    ReasonCode,
    ReviewAction,
    ValueSource,
)
from omr_scanner.domain.scan_quality import issue_label
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.review.history_dialog import HistoryDialog
from omr_scanner.gui.review.lanes import build_lanes, group_bubbles
from omr_scanner.gui.review.worker import SheetBundle, SheetWorker
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.gui.theme import TEMPLATE_DESIGNER_STYLESHEET
from omr_scanner.services import (
    ConflictFilter,
    ConflictRecord,
    ReviewError,
    UndoTarget,
    accept_machine_value,
    correct_value,
    count_conflicts,
    count_conflicts_for_scan,
    defer,
    group_labels,
    history_for,
    last_decision,
    last_resolved_sheet,
    list_conflicts,
    load_summary,
    map_canonical_to_source,
    provenance_for,
    provenance_for_scan,
    reopen,
    scan_source_path,
    undo_decision,
    undo_resolved_sheet,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from omr_scanner.domain.review import Provenance
    from omr_scanner.domain.scan_quality import ScanQualityAssessment
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.gui.scan.preview import FieldLane
    from omr_scanner.services import (
        BubbleView,
        ProjectDatabase,
        ProjectSession,
    )

_LOGGER = logging.getLogger(__name__)

QUEUE_PANEL_WIDTH = 460
EVIDENCE_PANEL_HEIGHT = 250
QUEUE_PAGE_SIZE = 500
"""How many conflicts the queue holds at once.

A page rather than the whole batch: thousands of conflicts is a realistic
number, and building thousands of table rows on every filter change is the
difference between a queue that responds and one that stutters. The filters do
the narrowing in SQL; this bounds what reaches Qt."""

WORKER_SHUTDOWN_TIMEOUT_MS = 30_000
"""How long the page waits for the sheet loader when it closes."""

ZOOM_PADDING_PX = 55.0
"""Canonical pixels of context around a disputed group in the zoomed view.

Enough that the neighbouring bubbles are visible - a reviewer judging "is this
the darker mark" needs to see what it is being compared against - without
zooming out so far that the marks stop being legible."""

FILTER_ALL = "All"
FILTER_OPEN = "Unresolved"
FILTER_RESOLVED = "Resolved"
FILTER_DEFERRED = "Deferred"
FILTER_WITHDRAWN = "Withdrawn"

_STATE_FILTERS: dict[str, tuple[ConflictState, ...]] = {
    FILTER_OPEN: (ConflictState.OPEN,),
    FILTER_RESOLVED: (ConflictState.RESOLVED,),
    FILTER_DEFERRED: (ConflictState.DEFERRED,),
    FILTER_WITHDRAWN: (ConflictState.WITHDRAWN,),
}
"""``FILTER_ALL`` is absent on purpose: "no filter" is the absence of an entry,
so a state added later cannot be quietly left out of the unfiltered view."""

ALL_TYPES = "All types"

_STATE_COLORS: dict[ConflictState, QColor] = {
    ConflictState.OPEN: QColor(255, 244, 214),
    ConflictState.RESOLVED: QColor(226, 245, 229),
    ConflictState.DEFERRED: QColor(226, 236, 250),
    ConflictState.WITHDRAWN: QColor(238, 238, 238),
}

BLANK_CHOICE = "(blank)"
"""The label for "this position carries no mark".

A real choice, not the absence of one: a reviewer deciding that a candidate
left a question empty is making a decision, and it has to be recordable."""

BLANK_KEY = Qt.Key.Key_B
"""The key that chooses "no mark here" when a numeric position is active.

``B`` for blank. Deliberately not the space bar, which every Qt widget on the
page already uses for something, and not ``0`` - a roll-number column has a
zero, and a reviewer who meant the digit must never get the blank."""


@dataclass
class ResolvePageState:
    """Everything the page knows that is not a widget.

    Attributes:
        session: The open project, or ``None``.
        batch_id: The batch being reviewed, or ``None``.
        template: The template that batch was read with.
        conflicts: The current queue page, in display order.
        reviewer: Who is reviewing - supplied by the main window from the
            application configuration, and required before any decision.
        bundle: The sheet currently loaded into the workspace.
        sheet_conflicts: Every conflict on the loaded sheet, whatever its
            state and whatever the queue filters say. The queue is what a
            reviewer is working through; this is what the *page* is showing,
            and a position filtered out of the queue is still a position on the
            paper that somebody has to be told about.
        sheet_provenance: Where each of those conflicts' values currently comes
            from, keyed by conflict id. Read once per refresh so the overlay
            does not ask the database per lane.
        redo: Decisions taken back in this sitting, newest last, ready to be
            re-issued. Session-only on purpose: re-deciding writes a new
            decision to the ledger, so a redo stack that survived a restart
            would offer to repeat commands whose context nobody remembers.
        auto_advance: Whether resolving the active conflict moves to the next
            unresolved one by itself.
    """

    session: ProjectSession | None = None
    batch_id: str | None = None
    template: OmrTemplate | None = None
    conflicts: list[ConflictRecord] = field(default_factory=list)
    reviewer: str = ""
    bundle: SheetBundle | None = None
    sheet_conflicts: list[ConflictRecord] = field(default_factory=list)
    sheet_provenance: dict[int, Provenance] = field(default_factory=dict)
    redo: list[UndoTarget] = field(default_factory=list)
    auto_advance: bool = True


class ResolvePage(WorkflowPage):
    """Review and resolve what recognition could not decide.

    Signals:
        conflict_selected: ``int`` conflict id whenever the shown conflict
            changes; ``-1`` when the selection is cleared.
        sheet_ready: emitted once a selected conflict's sheet has finished
            loading. GUI tests wait on this instead of sleeping.
        resolution_recorded: ``int`` conflict id after a decision is stored,
            taken back, or made again - anything that changes what a value is.
        auto_advance_changed: ``bool`` when the reviewer turns auto-advance on
            or off, so the window can remember it in their own settings. The
            page never writes a configuration file itself.

    Args:
        spec: The "resolve" workflow stage description.
        parent: Optional Qt parent.
    """

    conflict_selected = Signal(int)
    sheet_ready = Signal()
    resolution_recorded = Signal(int)
    auto_advance_changed = Signal(bool)

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setObjectName("resolvePage")
        self.setStyleSheet(TEMPLATE_DESIGNER_STYLESHEET)

        self.state = ResolvePageState()
        self._worker: SheetWorker | None = None
        self._workers: list[SheetWorker] = []
        self._loaded_scan_id: int | None = None
        self._suppress_selection = False

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_queue_panel())
        splitter.addWidget(self._build_workspace())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([QUEUE_PANEL_WIDTH, 1000])
        self.body.addWidget(splitter, stretch=1)

        self._install_shortcuts()
        self._refresh_controls()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_queue_panel(self) -> QWidget:
        """Build the left-hand queue, its filters and the batch summary."""
        panel = QWidget()
        panel.setObjectName("conflictQueuePanel")
        panel.setMaximumWidth(QUEUE_PANEL_WIDTH + 200)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(6)

        self.batch_label = QLabel("No batch selected")
        self.batch_label.setObjectName("reviewBatchLabel")
        self.batch_label.setWordWrap(True)
        layout.addWidget(self.batch_label)

        filters = QWidget()
        filter_layout = QHBoxLayout(filters)
        filter_layout.setContentsMargins(0, 0, 0, 0)

        self.state_filter = QComboBox()
        self.state_filter.setObjectName("conflictStateFilter")
        self.state_filter.addItems(
            [FILTER_OPEN, FILTER_ALL, FILTER_RESOLVED, FILTER_DEFERRED, FILTER_WITHDRAWN]
        )
        self.state_filter.currentIndexChanged.connect(self.refresh_queue)
        filter_layout.addWidget(self.state_filter, stretch=1)

        self.type_filter = QComboBox()
        self.type_filter.setObjectName("conflictTypeFilter")
        self.type_filter.addItem(ALL_TYPES, userData="")
        # Only the types this stage can actually hold. Offering "Multiple
        # answers marked" in a queue that never contains one would advertise a
        # filter that always returns nothing.
        for conflict_type in RESOLUTION_TYPES:
            self.type_filter.addItem(conflict_type.label, userData=conflict_type.value)
        self.type_filter.currentIndexChanged.connect(self.refresh_queue)
        filter_layout.addWidget(self.type_filter, stretch=1)
        layout.addWidget(filters)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("conflictSearchBox")
        self.search_box.setPlaceholderText("Search by student ID or file name...")
        self.search_box.setClearButtonEnabled(True)
        # `editingFinished` rather than `textChanged`: a query per keystroke
        # would re-run a filtered query over thousands of rows on every letter.
        self.search_box.editingFinished.connect(self.refresh_queue)
        self.search_box.returnPressed.connect(self.refresh_queue)
        layout.addWidget(self.search_box)

        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setObjectName("conflictQueueTable")
        self.queue_table.setHorizontalHeaderLabels(
            ["Sheet", "Student ID", "Field", "Conflict", "State"]
        )
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.queue_table.itemSelectionChanged.connect(self._on_queue_selection_changed)
        layout.addWidget(self.queue_table, stretch=1)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("reviewSummaryLabel")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)
        return panel

    def _build_workspace(self) -> QWidget:
        """Build the right-hand review workspace."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        layout.addWidget(self._build_toolbar())

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(self._build_views())
        vertical.addWidget(self._build_decision_panel())
        vertical.setStretchFactor(0, 1)
        vertical.setStretchFactor(1, 0)
        vertical.setSizes([700, EVIDENCE_PANEL_HEIGHT])
        layout.addWidget(vertical, stretch=1)
        return container

    def _build_toolbar(self) -> QToolBar:
        """Build the undo, navigation and zoom toolbar above the views.

        The two curved arrows in the previous build were **not** undo and redo,
        despite looking exactly like them: they were bound to "previous
        conflict" and "next conflict", so the one control an operator would
        reach for to take a mistaken correction back moved the selection
        instead. They now mean what they look like, and navigation wears
        chevrons.
        """
        toolbar = QToolBar("Review")
        toolbar.setObjectName("reviewToolbar")
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setMovable(False)

        self.undo_action = QAction(load_icon("undo-2"), "Undo decision", self)
        self.undo_action.setObjectName("undoDecisionButton")
        self.undo_action.triggered.connect(self.undo_last_decision)
        toolbar.addAction(self.undo_action)

        self.redo_action = QAction(load_icon("redo-2"), "Redo decision", self)
        self.redo_action.setObjectName("redoDecisionButton")
        self.redo_action.triggered.connect(self.redo_last_decision)
        toolbar.addAction(self.redo_action)

        self.undo_sheet_action = QAction(
            load_icon("rotate-ccw"), "Undo resolved sheet", self
        )
        self.undo_sheet_action.setObjectName("undoResolvedSheetButton")
        self.undo_sheet_action.triggered.connect(self.undo_last_resolved_sheet)
        toolbar.addAction(self.undo_sheet_action)
        toolbar.addSeparator()

        self.previous_action = QAction(load_icon("chevron-left"), "Previous", self)
        self.previous_action.setObjectName("previousConflictButton")
        self.previous_action.setToolTip("Previous conflict (Left arrow)")
        self.previous_action.triggered.connect(self.select_previous)
        toolbar.addAction(self.previous_action)

        self.next_action = QAction(load_icon("chevron-right"), "Next", self)
        self.next_action.setObjectName("nextConflictButton")
        self.next_action.setToolTip("Next conflict (Right arrow)")
        self.next_action.triggered.connect(self.select_next)
        toolbar.addAction(self.next_action)

        self.previous_open_action = QAction(
            load_icon("circle-dot"), "Previous unresolved", self
        )
        self.previous_open_action.setObjectName("previousUnresolvedConflictButton")
        self.previous_open_action.setToolTip(
            "Back to the previous conflict nobody has decided (Shift+Enter)"
        )
        self.previous_open_action.triggered.connect(self.select_previous_unresolved)
        toolbar.addAction(self.previous_open_action)

        self.next_open_action = QAction(load_icon("list-checks"), "Next unresolved", self)
        self.next_open_action.setObjectName("nextUnresolvedConflictButton")
        self.next_open_action.setToolTip(
            "Skip to the next conflict nobody has decided (Ctrl+Enter)"
        )
        self.next_open_action.triggered.connect(self.select_next_unresolved)
        toolbar.addAction(self.next_open_action)

        self.auto_advance_action = QAction(load_icon("check-check"), "Auto-advance", self)
        self.auto_advance_action.setObjectName("autoAdvanceToggle")
        self.auto_advance_action.setCheckable(True)
        self.auto_advance_action.setChecked(self.state.auto_advance)
        self.auto_advance_action.setToolTip(
            "Move to the next unresolved conflict as soon as this one is decided"
        )
        self.auto_advance_action.toggled.connect(self._on_auto_advance_toggled)
        toolbar.addAction(self.auto_advance_action)
        toolbar.addSeparator()

        self.zoom_out_action = QAction(load_icon("zoom-out"), "Zoom Out", self)
        self.zoom_out_action.setObjectName("reviewZoomOutButton")
        self.zoom_out_action.triggered.connect(lambda: self._current_view().zoom_out())
        toolbar.addAction(self.zoom_out_action)

        self.zoom_in_action = QAction(load_icon("zoom-in"), "Zoom In", self)
        self.zoom_in_action.setObjectName("reviewZoomInButton")
        self.zoom_in_action.triggered.connect(lambda: self._current_view().zoom_in())
        toolbar.addAction(self.zoom_in_action)

        self.fit_action = QAction(load_icon("maximize"), "Fit", self)
        self.fit_action.setObjectName("reviewFitButton")
        self.fit_action.triggered.connect(lambda: self._current_view().fit_to_window())
        toolbar.addAction(self.fit_action)

        self.recentre_action = QAction(load_icon("scan"), "Re-centre", self)
        self.recentre_action.setObjectName("reviewRecentreButton")
        self.recentre_action.setToolTip("Return the zoomed view to the disputed field")
        self.recentre_action.triggered.connect(self._focus_zoom_on_conflict)
        toolbar.addAction(self.recentre_action)
        toolbar.addSeparator()

        self.history_button = QPushButton(load_icon("file-text"), "History...")
        self.history_button.setObjectName("conflictHistoryButton")
        self.history_button.setToolTip("Every decision recorded for this conflict")
        self.history_button.clicked.connect(self.show_history)
        toolbar.addWidget(self.history_button)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.sheet_progress_label = QLabel("")
        self.sheet_progress_label.setObjectName("sheetConflictProgressLabel")
        self.sheet_progress_label.setTextFormat(Qt.TextFormat.RichText)
        toolbar.addWidget(self.sheet_progress_label)
        return toolbar

    def _build_views(self) -> QWidget:
        """Build the three image views, as tabs.

        Tabs rather than three panes side by side: on a laptop, three images
        across is three unusable thumbnails, and the zoomed field is what a
        reviewer actually decides from. It is therefore first and selected by
        default; the whole sheet and the original are a click away for the
        cases where the surrounding context is the question.
        """
        self.view_tabs = QTabWidget()
        self.view_tabs.setObjectName("reviewViewTabs")

        self.zoom_view = ScanPreviewView()
        self.zoom_view.setObjectName("conflictZoomView")
        self.view_tabs.addTab(self.zoom_view, "Zoomed field")

        self.normalised_view = ScanPreviewView()
        self.normalised_view.setObjectName("normalisedSheetView")
        self.view_tabs.addTab(self.normalised_view, "Normalised sheet")

        original_tab = QWidget()
        original_layout = QVBoxLayout(original_tab)
        original_layout.setContentsMargins(0, 0, 0, 0)
        original_layout.setSpacing(2)
        self.original_view = ScanPreviewView()
        self.original_view.setObjectName("originalSheetView")
        original_layout.addWidget(self.original_view, stretch=1)
        self.original_note = QLabel("")
        self.original_note.setObjectName("originalSheetNote")
        self.original_note.setWordWrap(True)
        original_layout.addWidget(self.original_note)
        self.view_tabs.addTab(original_tab, "Original scan")

        return self.view_tabs

    def _build_decision_panel(self) -> QWidget:
        """Build the evidence display and the action controls."""
        panel = QWidget()
        panel.setObjectName("conflictDecisionPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        evidence_box = QGroupBox("What the machine saw")
        evidence_layout = QVBoxLayout(evidence_box)
        self.evidence_label = QLabel("Select a conflict from the queue.")
        self.evidence_label.setObjectName("machineEvidenceLabel")
        self.evidence_label.setWordWrap(True)
        self.evidence_label.setTextFormat(Qt.TextFormat.RichText)
        self.evidence_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        evidence_layout.addWidget(self.evidence_label, stretch=1)
        layout.addWidget(evidence_box, stretch=1)

        layout.addWidget(self._build_action_box(), stretch=1)
        return panel

    def _build_action_box(self) -> QGroupBox:
        """Build the resolution controls."""
        box = QGroupBox("Your decision")
        layout = QVBoxLayout(box)
        layout.setSpacing(4)

        self.reviewer_label = QLabel("")
        self.reviewer_label.setObjectName("reviewerNameLabel")
        self.reviewer_label.setWordWrap(True)
        layout.addWidget(self.reviewer_label)

        self.choice_row = QWidget()
        self.choice_layout = QHBoxLayout(self.choice_row)
        self.choice_layout.setContentsMargins(0, 0, 0, 0)
        self.choice_layout.setSpacing(3)
        layout.addWidget(self.choice_row)
        self._choice_buttons: list[QPushButton] = []

        # A free-text row for the conflicts whose value is not one of a fixed
        # set of printed symbols - a whole identifier, or a duplicate roll
        # number where the correct answer is a number nobody printed on this
        # sheet. Buttons cannot express those, and offering only "(blank)" for
        # them, as an earlier build did, gives a reviewer no way to say what
        # they mean.
        self.free_value_row = QWidget()
        free_layout = QHBoxLayout(self.free_value_row)
        free_layout.setContentsMargins(0, 0, 0, 0)
        self.free_value_edit = QLineEdit()
        self.free_value_edit.setObjectName("correctedValueEdit")
        self.free_value_edit.setPlaceholderText("Corrected value...")
        self.free_value_edit.returnPressed.connect(self._correct_from_text)
        free_layout.addWidget(self.free_value_edit, stretch=1)
        self.free_value_button = QPushButton("Save value")
        self.free_value_button.setObjectName("saveCorrectedValueButton")
        self.free_value_button.clicked.connect(self._correct_from_text)
        free_layout.addWidget(self.free_value_button)
        layout.addWidget(self.free_value_row)
        self.free_value_row.setVisible(False)

        reason_row = QWidget()
        reason_layout = QHBoxLayout(reason_row)
        reason_layout.setContentsMargins(0, 0, 0, 0)
        reason_layout.addWidget(QLabel("Reason:"))
        self.reason_combo = QComboBox()
        self.reason_combo.setObjectName("correctionReasonCombo")
        for code in ReasonCode:
            if code is ReasonCode.MACHINE_CONFIRMED:
                continue  # Recorded automatically when accepting; never chosen.
            self.reason_combo.addItem(code.label, userData=code.value)
        reason_layout.addWidget(self.reason_combo, stretch=1)
        layout.addWidget(reason_row)

        self.reason_text = QTextEdit()
        self.reason_text.setObjectName("correctionReasonText")
        self.reason_text.setPlaceholderText("Optional note; required for 'Other'.")
        self.reason_text.setMaximumHeight(52)
        layout.addWidget(self.reason_text)

        buttons = QWidget()
        button_layout = QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)

        self.accept_button = QPushButton(load_icon("circle-check"), "Accept machine value")
        self.accept_button.setObjectName("acceptMachineValueButton")
        self.accept_button.setToolTip(
            "Record that you inspected this and the machine was right (Enter)"
        )
        self.accept_button.clicked.connect(self.accept_machine)
        button_layout.addWidget(self.accept_button)

        self.defer_button = QPushButton("Defer")
        self.defer_button.setObjectName("deferConflictButton")
        self.defer_button.setToolTip("Postpone this decision (D)")
        self.defer_button.clicked.connect(self.defer_conflict)
        button_layout.addWidget(self.defer_button)

        self.reopen_button = QPushButton(load_icon("rotate-ccw"), "Reopen")
        self.reopen_button.setObjectName("reopenConflictButton")
        self.reopen_button.setToolTip(
            "Discard every decision on this conflict and put it back in the queue. "
            "Nothing is erased - the earlier corrections stay in the history. "
            "To step back one decision instead, use Undo (Ctrl+Z)."
        )
        self.reopen_button.clicked.connect(self.reopen_conflict)
        button_layout.addWidget(self.reopen_button)
        layout.addWidget(buttons)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separator)

        self.provenance_label = QLabel("")
        self.provenance_label.setObjectName("conflictProvenanceLabel")
        self.provenance_label.setWordWrap(True)
        self.provenance_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.provenance_label)
        return box

    def _install_shortcuts(self) -> None:
        """Bind the keys a reviewer working through hundreds of conflicts needs.

        A sitting is hundreds of sheets, so the whole loop - look, choose,
        move on - has to be reachable without the mouse. A **digit chooses the
        digit it prints**; there is no separate keyboard code path, because
        every key below calls exactly the public method its button calls, and a
        correction made by typing carries the same reviewer, reason and audit
        event as one made by clicking.

        Two things keep that from being the accidental destructive edit this
        stage exists to prevent. A value key acts only when the active conflict
        genuinely offers that symbol - the labels come from the template, so a
        key nothing is printed for does nothing at all - and every handler
        refuses while the focus is in a text box, so typing a roll number into
        the search field cannot decide the conflict behind it.

        ``Qt.WidgetWithChildrenShortcut`` scopes the lot to this page, so none
        of it can fire while another stage is on screen.
        """
        bindings: list[tuple[QKeySequence, Callable[[], object]]] = [
            (QKeySequence(Qt.Key.Key_Left), self.select_previous),
            (QKeySequence(Qt.Key.Key_Right), self.select_next),
            (QKeySequence(Qt.Key.Key_Return), self.accept_machine),
            (QKeySequence(Qt.Key.Key_Enter), self.accept_machine),
            (QKeySequence(Qt.Key.Key_D), self.defer_conflict),
            (QKeySequence(BLANK_KEY), lambda: self.choose_label(BLANK_CHOICE)),
            (
                QKeySequence(Qt.Modifier.SHIFT | Qt.Key.Key_Return),
                self.select_previous_unresolved,
            ),
            (
                QKeySequence(Qt.Modifier.SHIFT | Qt.Key.Key_Enter),
                self.select_previous_unresolved,
            ),
            (
                QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Return),
                self.select_next_unresolved,
            ),
            (
                QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Enter),
                self.select_next_unresolved,
            ),
            # Spelled out rather than taken from `QKeySequence.StandardKey`:
            # that maps Redo to *both* Ctrl+Y and Ctrl+Shift+Z on Windows, and
            # Ctrl+Shift+Z is the sheet-level undo here. An ambiguous shortcut
            # fires neither.
            (QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Z), self.undo_last_decision),
            (QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Y), self.redo_last_decision),
            (
                QKeySequence(Qt.Modifier.CTRL | Qt.Modifier.SHIFT | Qt.Key.Key_Z),
                self.undo_last_resolved_sheet,
            ),
        ]
        # 0-9 choose the symbol they print. Bound to the *label*, not to a
        # position in the stack: a set code whose options are "10", "11", "12"
        # is not addressed by its digits, and `choose_label` simply finds
        # nothing to do there.
        bindings.extend(
            (QKeySequence(str(digit)), partial(self.choose_label, str(digit)))
            for digit in range(10)
        )

        self._shortcuts: list[QShortcut] = []
        for keys, slot in bindings:
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def _editing_text(self) -> bool:
        """Whether the keyboard currently belongs to a text field.

        Qt's own shortcut override usually keeps a typed character inside the
        widget being typed into, but "usually" is not a guarantee a reviewer's
        roll-number search should depend on - a search box that silently
        recorded ``1`` as somebody's corrected student ID is precisely the
        accidental edit this stage exists to prevent. Every keyboard action
        therefore asks here first, so the rule is explicit and testable rather
        than inherited from event-dispatch order.
        """
        widget = self.focusWidget()
        return isinstance(widget, QLineEdit | QTextEdit)

    # ------------------------------------------------------------------
    # Project and batch
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt the open project, forgetting any batch from the previous one."""
        self.state.session = session
        self.state.batch_id = None
        self.state.conflicts = []
        self.state.bundle = None
        self.state.sheet_conflicts = []
        self.state.sheet_provenance = {}
        # A redo entry names a conflict id, which means nothing in a different
        # project's database. Carrying one across would eventually re-issue a
        # decision onto whatever conflict happened to have that number.
        self.state.redo = []
        self._loaded_scan_id = None
        self.queue_table.setRowCount(0)
        self._clear_views()
        self._refresh_summary()
        self._refresh_controls()

    @property
    def database(self) -> ProjectDatabase | None:
        """The open project's database, or ``None``."""
        return self.state.session.database if self.state.session is not None else None

    def set_reviewer(self, name: str) -> None:
        """Adopt the reviewer identity the application is configured with."""
        self.state.reviewer = name.strip()
        self._refresh_reviewer_label()
        self._refresh_controls()

    def load_batch(self, batch_id: str, template: OmrTemplate | None = None) -> bool:
        """Show one batch's conflicts.

        Args:
            batch_id: The batch to review.
            template: The template it was read with. Without it the page still
                lists conflicts and records decisions - it simply cannot offer
                the template's own answer labels, and says so.

        Returns:
            Whether the batch was found.
        """
        database = self.database
        if database is None:
            return False
        summary = load_summary(database, batch_id)
        if summary is None:
            return False

        self.state.batch_id = batch_id
        self.state.template = template
        self.state.bundle = None
        self.state.sheet_conflicts = []
        self.state.sheet_provenance = {}
        self.state.redo = []
        self._loaded_scan_id = None
        self.batch_label.setText(
            f"<b>Batch {batch_id[:8]}</b><br>{summary.total} scan(s) from "
            f"{summary.source_folder or '(files)'}"
        )
        self.refresh_queue()
        _LOGGER.info("Review page opened batch %s", batch_id)
        return True

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------
    def _current_filter(self) -> ConflictFilter:
        """Build the filter the controls currently describe."""
        states = _STATE_FILTERS.get(self.state_filter.currentText(), ())
        type_value = self.type_filter.currentData()
        types = (ConflictType(type_value),) if type_value else ()
        return ConflictFilter(
            states=states,
            conflict_types=types,
            search=self.search_box.text().strip(),
            include_withdrawn=self.state_filter.currentText() == FILTER_WITHDRAWN,
        )

    def refresh_queue(self) -> None:
        """Re-read the queue from the database and rebuild the table."""
        database = self.database
        if database is None or self.state.batch_id is None:
            self.queue_table.setRowCount(0)
            self.state.conflicts = []
            self._refresh_summary()
            self._refresh_controls()
            return

        selected = self.current_conflict()
        previous_row = self.queue_table.currentRow()
        self.state.conflicts = list(
            list_conflicts(
                database,
                self.state.batch_id,
                filters=self._current_filter(),
                limit=QUEUE_PAGE_SIZE,
            )
        )
        self._rebuild_queue_table()
        self._refresh_summary()
        self._restore_selection(selected, previous_row)
        self._refresh_controls()

    def _restore_selection(
        self, selected: ConflictRecord | None, previous_row: int
    ) -> None:
        """Put the selection back where it belongs after the queue changed.

        The subtle case this exists for: resolving a conflict removes it from
        the default "Unresolved" view. The table keeps its row *index*, so
        without this the page would go on showing the decided conflict's
        evidence while :meth:`current_conflict` returned whichever different
        conflict had slid into that row - and the next click would decide *that*
        one. Selection is therefore cleared before every rebuild and re-applied
        explicitly here, so the workspace and the queue can never disagree
        about which conflict is being reviewed.
        """
        if not self.state.conflicts:
            self.queue_table.clearSelection()
            self._clear_views()
            self.conflict_selected.emit(-1)
            return

        if selected is not None and self.select_conflict_by_id(selected.conflict_id):
            return

        # The conflict that was being reviewed is no longer in this view -
        # almost always because it was just decided. Staying at the same
        # position lands on whatever came next, which is the natural place to
        # continue from.
        row = max(0, min(previous_row, len(self.state.conflicts) - 1))
        self.queue_table.selectRow(row)
        # `selectRow` emits nothing when the index is unchanged, and the index
        # very often *is* unchanged here even though the conflict at it is not.
        if self.queue_table.currentRow() == row:
            self._on_queue_selection_changed()

    def _rebuild_queue_table(self) -> None:
        """Rebuild the table from :attr:`ResolvePageState.conflicts`."""
        self._suppress_selection = True
        self.queue_table.setUpdatesEnabled(False)
        try:
            # Cleared first so that re-selecting afterwards always emits, even
            # when the row index happens to be the same as before. See
            # `_restore_selection`.
            self.queue_table.clearSelection()
            self.queue_table.setCurrentCell(-1, -1)
            self.queue_table.setRowCount(len(self.state.conflicts))
            for row, conflict in enumerate(self.state.conflicts):
                # The state cell carries a glyph *and* the word. The row tint
                # stays, but it is now the third way of saying the same thing
                # rather than the only one - a reviewer with a colour-vision
                # deficiency, or reading a printed screenshot, sees the state
                # either way. "Reopened" is a conflict that is open because
                # somebody put it back, which the tint alone cannot show.
                values = (
                    conflict.scan_name,
                    conflict.identifier_value,
                    conflict.field.describe(),
                    conflict.conflict_type.label,
                    f"{conflict.state_marker} {conflict.state_label}",
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setBackground(_STATE_COLORS[conflict.state])
                    if column == 4:
                        item.setToolTip(_state_tooltip(conflict))
                    self.queue_table.setItem(row, column, item)
        finally:
            self.queue_table.setUpdatesEnabled(True)
            self._suppress_selection = False

    def _refresh_summary(self) -> None:
        """Update the batch-level counts under the queue."""
        database = self.database
        if database is None or self.state.batch_id is None:
            self.summary_label.setText("")
            return
        counts = count_conflicts(database, self.state.batch_id)
        biggest = sorted(counts.by_type.items(), key=lambda item: -item[1])[:3]
        breakdown = ", ".join(
            f"{ConflictType(kind).label}: {count}" for kind, count in biggest
        )
        self.summary_label.setText(
            f"Total conflicts: <b>{counts.total}</b><br>"
            f"Unresolved: <b>{counts.unresolved}</b> "
            f"(open {counts.open_count}, deferred {counts.deferred})<br>"
            f"Resolved: {counts.resolved} &middot; Withdrawn: {counts.withdrawn}"
            + (f"<br><i>{breakdown}</i>" if breakdown else "")
        )

    def current_conflict(self) -> ConflictRecord | None:
        """The conflict currently selected, or ``None``."""
        row = self.queue_table.currentRow()
        if 0 <= row < len(self.state.conflicts):
            return self.state.conflicts[row]
        return None

    def select_conflict_by_id(self, conflict_id: int) -> bool:
        """Select the row carrying ``conflict_id``, if it is in the queue."""
        for row, conflict in enumerate(self.state.conflicts):
            if conflict.conflict_id == conflict_id:
                self.queue_table.selectRow(row)
                return True
        return False

    def select_previous(self) -> None:
        """Move to the previous conflict in the queue."""
        row = self.queue_table.currentRow()
        if row > 0:
            self.queue_table.selectRow(row - 1)

    def select_next(self) -> None:
        """Move to the next conflict in the queue."""
        row = self.queue_table.currentRow()
        if row + 1 < len(self.state.conflicts):
            self.queue_table.selectRow(row + 1)

    def select_next_unresolved(self) -> bool:
        """Skip forward to the next conflict nobody has decided.

        Wraps to the start, so working through a queue ends by returning to
        whatever was skipped rather than stopping silently at the bottom.

        Returns:
            Whether an undecided conflict was found and selected.
        """
        return self._select_unresolved(step=1)

    def select_previous_unresolved(self) -> bool:
        """Step back to the previous conflict nobody has decided."""
        return self._select_unresolved(step=-1)

    def _select_unresolved(self, *, step: int) -> bool:
        """Move to the nearest undecided conflict in one direction.

        Deferred conflicts are passed over as well as decided ones: deferring
        is a reviewer saying "not this one, not now", and walking them back
        round on the next pass would make the button useless on a queue where
        somebody has postponed a few.
        """
        if self._editing_text():
            return False
        total = len(self.state.conflicts)
        if not total:
            return False
        start = self.queue_table.currentRow()
        for offset in range(1, total + 1):
            row = (start + step * offset) % total
            if self.state.conflicts[row].state is ConflictState.OPEN:
                self.queue_table.selectRow(row)
                return True
        return False

    def _on_queue_selection_changed(self) -> None:
        """React to the queue selection moving."""
        if self._suppress_selection:
            return
        conflict = self.current_conflict()
        if conflict is None:
            self._clear_views()
            self.conflict_selected.emit(-1)
            self._refresh_controls()
            return
        self._show_conflict(conflict)
        self.conflict_selected.emit(conflict.conflict_id)

    # ------------------------------------------------------------------
    # Showing one conflict
    # ------------------------------------------------------------------
    def _show_conflict(self, conflict: ConflictRecord) -> None:
        """Display one conflict: its evidence, its actions and its sheet."""
        self._reload_sheet_conflicts(conflict.scan_id)
        self._refresh_evidence(conflict)
        self._refresh_choices(conflict)
        self._refresh_provenance(conflict)
        self._refresh_sheet_progress(conflict)
        self._refresh_controls()

        # The sheet is loaded only when it is a different one. Walking the ten
        # conflicts on one sheet therefore decodes and re-reads it once, which
        # is the difference between a usable queue and one that pauses on every
        # arrow key.
        if conflict.scan_id == self._loaded_scan_id and self.state.bundle is not None:
            self._apply_bundle_to_views(conflict)
            return
        self._load_sheet_for(conflict)

    def _load_sheet_for(self, conflict: ConflictRecord) -> None:
        """Start loading the conflict's sheet in the background."""
        database = self.database
        if database is None or self.state.template is None:
            self._clear_views()
            self.original_note.setText(
                "The template for this batch is not loaded, so the sheet cannot "
                "be re-read. Load it on the Scan page to see the images."
            )
            return
        source = scan_source_path(database, conflict.scan_id)
        if not source:
            self._clear_views()
            return
        if self._worker is not None and self._worker.isRunning():
            # The previous sheet is still loading; it will be superseded when
            # this one arrives, and cancelling a decode mid-flight buys nothing.
            self._worker.ready.disconnect()
        worker = SheetWorker(Path(source), self.state.template, self)
        worker.ready.connect(self._on_sheet_ready)
        self._worker = worker
        # Tracked until it finishes. A superseded loader is still a running
        # thread, and one alive when its parent is destroyed aborts the
        # process - which looks like a crashed test suite rather than a
        # forgotten join. (Found while building the Phase 7 page, which
        # supersedes workers far more often.)
        self._workers = [item for item in self._workers if item.isRunning()]
        self._workers.append(worker)
        worker.start()

    def _on_sheet_ready(self, bundle: SheetBundle) -> None:
        """Adopt a freshly loaded sheet. Runs on the GUI thread."""
        self.state.bundle = bundle
        conflict = self.current_conflict()
        self._loaded_scan_id = conflict.scan_id if conflict is not None else None
        if conflict is not None:
            self._apply_bundle_to_views(conflict)
        self.sheet_ready.emit()

    def _apply_bundle_to_views(self, conflict: ConflictRecord) -> None:
        """Draw the loaded sheet into the three views."""
        bundle = self.state.bundle
        if bundle is None:
            return

        result = bundle.result
        if result is not None and result.preview is not None:
            lanes = self._lanes_for(conflict)
            for view in (self.normalised_view, self.zoom_view):
                view.set_page(
                    result.preview,
                    canonical_width=result.canonical_width,
                    canonical_height=result.canonical_height,
                    preview_scale=result.preview_scale,
                )
                view.set_overlay(result.zones, self._conflict_bubbles(conflict), ())
                view.set_lanes(lanes)
                view.set_scan_quality(result.scan_quality)
                # `status_symbols=False`: the `?` beside the bubble the engine
                # nearly chose is what the lane outline replaced. Leaving both
                # on would say the same thing twice, in two places, and point
                # at one bubble of a group the reviewer has to read as a whole.
                view.set_overlay_visible(
                    zones=True,
                    bubbles=True,
                    empty=True,
                    centers=True,
                    status_symbols=False,
                )
            self.normalised_view.fit_to_window()
            self._focus_zoom_on_conflict()
        else:
            self.normalised_view.clear()
            self.zoom_view.clear()

        if bundle.original is not None:
            self.original_view.set_page(
                bundle.original,
                canonical_width=bundle.original.width,
                canonical_height=bundle.original.height,
            )
            self.original_view.set_overlay((), (), ())
            self.original_view.set_scan_quality(None)
            self.original_view.set_overlay_visible(zones=False, bubbles=False, empty=False)
            self.original_view.fit_to_window()
            self._describe_original_location(conflict)
        else:
            self.original_view.clear()
            self.original_note.setText(
                bundle.error or "The original scan could not be decoded."
            )

        if bundle.error:
            self.evidence_label.setText(
                f"{self.evidence_label.text()}<br><br><b>This sheet could not be "
                f"read for review:</b> {bundle.error}"
            )

    def _conflict_bubbles(self, conflict: ConflictRecord) -> tuple[BubbleView, ...]:
        """Return just the bubbles belonging to the disputed group.

        Everything else on the sheet is drawn as a zone rectangle only, so the
        disputed field is unmistakable rather than one ringed group among five
        hundred. The bubbles come straight from the fresh result - the engine's
        own measured geometry, never a recomputed guess.
        """
        bundle = self.state.bundle
        if bundle is None or bundle.result is None or self.state.template is None:
            return ()
        return group_bubbles(bundle.result, self.state.template, conflict)

    def _reload_sheet_conflicts(self, scan_id: int) -> None:
        """Re-read every conflict on one sheet, and where its value comes from.

        Deliberately unfiltered. The queue shows what the reviewer chose to
        work through; the *sheet* shows what is on the paper, and a position
        hidden by a "Unresolved only" filter is still a position somebody
        corrected. An overlay that honoured the queue's filters would make a
        reviewer's own corrections disappear from the preview the moment they
        made them.
        """
        database = self.database
        if database is None or self.state.batch_id is None:
            self.state.sheet_conflicts = []
            self.state.sheet_provenance = {}
            return
        self.state.sheet_conflicts = list(
            list_conflicts(
                database,
                self.state.batch_id,
                filters=ConflictFilter(scan_id=scan_id, include_withdrawn=True),
            )
        )
        self.state.sheet_provenance = provenance_for_scan(
            database, self.state.batch_id, scan_id
        )

    def _lanes_for(self, conflict: ConflictRecord) -> tuple[FieldLane, ...]:
        """Build the lane rectangles this sheet should show."""
        bundle = self.state.bundle
        if bundle is None or bundle.result is None or self.state.template is None:
            return ()
        return build_lanes(
            bundle.result,
            self.state.template,
            self.state.sheet_conflicts,
            self.state.sheet_provenance,
            active_conflict_id=conflict.conflict_id,
        )

    def _refresh_lanes(self) -> None:
        """Redraw the lane overlay for the conflict currently shown.

        Called after every decision, so the outline turns red - or back to
        amber - the instant it is recorded. Nothing is reloaded: the sheet's
        image and its measured geometry have not changed, only what people have
        said about them.
        """
        conflict = self.current_conflict()
        lanes = self._lanes_for(conflict) if conflict is not None else ()
        for view in (self.normalised_view, self.zoom_view):
            view.set_lanes(lanes)

    def _focus_zoom_on_conflict(self) -> None:
        """Centre and magnify the zoomed view on the disputed group."""
        conflict = self.current_conflict()
        bubbles = self._conflict_bubbles(conflict) if conflict is not None else ()
        if not bubbles:
            self.zoom_view.fit_to_window()
            return
        left = min(item.x - item.width / 2.0 for item in bubbles) - ZOOM_PADDING_PX
        right = max(item.x + item.width / 2.0 for item in bubbles) + ZOOM_PADDING_PX
        top = min(item.y - item.height / 2.0 for item in bubbles) - ZOOM_PADDING_PX
        bottom = max(item.y + item.height / 2.0 for item in bubbles) + ZOOM_PADDING_PX

        viewport = self.zoom_view.viewport().size()
        width = max(right - left, 1.0)
        height = max(bottom - top, 1.0)
        factor = min(viewport.width() / width, viewport.height() / height)
        self.zoom_view.set_zoom(factor)
        self.zoom_view.centerOn((left + right) / 2.0, (top + bottom) / 2.0)

    def _describe_original_location(self, conflict: ConflictRecord) -> None:
        """Say where on the *original* scan the disputed field sits.

        The original is in the scanner's own pixels, not the canonical page, so
        the overlay coordinates every other view uses do not apply to it -
        drawing them here would put the highlight in the wrong place while
        looking perfectly plausible. Instead the disputed centre is mapped back
        through the engine's own inverse transform and reported as a
        coordinate, so a reviewer can find it without being misled.
        """
        bundle = self.state.bundle
        bubbles = self._conflict_bubbles(conflict)
        if bundle is None or bundle.result is None or not bubbles:
            self.original_note.setText(
                "The original scan, exactly as it arrived. Overlays are not drawn "
                "here: they are in the rectified page's coordinates."
            )
            return
        centre = (
            sum(item.x for item in bubbles) / len(bubbles),
            sum(item.y for item in bubbles) / len(bubbles),
        )
        mapped = map_canonical_to_source(bundle.result, [centre])
        if not mapped:
            self.original_note.setText(
                "The original scan, exactly as it arrived. This sheet has no usable "
                "transform, so the disputed field cannot be located on it."
            )
            return
        x, y = mapped[0]
        self.original_note.setText(
            f"The original scan, exactly as it arrived (never modified). "
            f"{conflict.field.describe()} sits near x={x:.0f}, y={y:.0f} in this "
            f"image's own pixels."
        )

    def _clear_views(self) -> None:
        """Empty every view and the evidence panel."""
        for view in (self.zoom_view, self.normalised_view, self.original_view):
            view.clear()
        self.original_note.setText("")
        self.evidence_label.setText("Select a conflict from the queue.")
        self.provenance_label.setText("")
        self.sheet_progress_label.setText("")
        self._clear_choices()

    def _current_view(self) -> ScanPreviewView:
        """The image view the visible tab is showing."""
        index = self.view_tabs.currentIndex()
        if index == 1:
            return self.normalised_view
        if index == 2:
            return self.original_view
        return self.zoom_view

    # ------------------------------------------------------------------
    # Evidence, choices, provenance
    # ------------------------------------------------------------------
    def _refresh_evidence(self, conflict: ConflictRecord) -> None:
        """Render what the machine saw."""
        bundle = self.state.bundle
        result = bundle.result if bundle is not None else None
        assessment = result.scan_quality if result is not None else None
        self.evidence_label.setText(_evidence_html(conflict, assessment))

    def _refresh_sheet_progress(self, conflict: ConflictRecord) -> None:
        """Say where in this sheet the reviewer is, and how much is left.

        ``Conflict 3 of 8 - 5 unresolved`` rather than the previous
        ``This sheet: 8 conflict(s), 8 unresolved``. The old wording answered a
        question nobody was asking: a reviewer part-way through a sheet needs
        to know how far through it they are, and a count that did not move as
        they worked told them nothing about that.

        The ordinal counts the sheet's conflicts **in the queue's own order**,
        because that is the order the reviewer is walking them in, while the
        unresolved figure comes from the database - so a filtered queue cannot
        make a sheet look finished when it is not.
        """
        database = self.database
        if database is None or self.state.batch_id is None:
            self.sheet_progress_label.setText("")
            return
        counts = count_conflicts_for_scan(database, self.state.batch_id, conflict.scan_id)
        on_sheet = [
            item for item in self.state.conflicts if item.scan_id == conflict.scan_id
        ]
        position = next(
            (
                index + 1
                for index, item in enumerate(on_sheet)
                if item.conflict_id == conflict.conflict_id
            ),
            0,
        )
        self.sheet_progress_label.setText(
            f"Conflict {position} of {len(on_sheet)} &middot; "
            f"<b>{counts.unresolved}</b> unresolved"
        )
        self.sheet_progress_label.setToolTip(
            f"This sheet has {counts.total} conflict(s): {counts.open_count} open, "
            f"{counts.deferred} deferred, {counts.resolved} resolved, "
            f"{counts.withdrawn} withdrawn."
        )

    def _clear_choices(self) -> None:
        """Remove the value buttons."""
        for button in self._choice_buttons:
            self.choice_layout.removeWidget(button)
            button.deleteLater()
        self._choice_buttons = []

    def _refresh_choices(self, conflict: ConflictRecord) -> None:
        """Rebuild the value buttons from the *template's own* symbols.

        Never a hard-coded ``A``-``E``. The labels come from
        :func:`~omr_scanner.services.conflict_policy.group_labels`, which reads
        the same ``zone_groups`` recognition itself used - so a six-option
        paper, or a set code whose symbols are ``"10"``, ``"11"``, ``"12"``,
        offers exactly those and nothing else.
        """
        self._clear_choices()
        self.free_value_row.setVisible(False)
        self.free_value_edit.clear()

        if not conflict.allows_value_correction:
            note = QLabel(
                "This is not a value that can be corrected. Acknowledge it or "
                "defer it."
            )
            note.setWordWrap(True)
            self.choice_layout.addWidget(note)
            self._choice_buttons = []
            return
        if self.state.template is None:
            return

        labels = group_labels(
            self.state.template, conflict.field.zone_id, conflict.field.group_key
        )
        if not labels:
            # No fixed alphabet for this conflict: a whole identifier, or a
            # duplicate roll number. Free text is the only honest control.
            self.free_value_row.setVisible(True)
            self.free_value_edit.setText(conflict.observation.value)
            return

        for label in (*labels, BLANK_CHOICE):
            button = QPushButton(label)
            button.setObjectName(f"choiceButton_{label}")
            # The key is named on the button that does the same thing, because
            # the two are one command - `choose_label` - and a reviewer who
            # never reads the documentation still learns the shortcut.
            key = "B" if label == BLANK_CHOICE else label
            button.setToolTip(
                f"Record '{label}' as the corrected value"
                + (f" (press {key})" if len(key) == 1 else "")
            )
            button.setAccessibleName(f"Record {label}")
            button.clicked.connect(
                lambda _checked=False, item=label: self.choose_label(item)
            )
            self.choice_layout.addWidget(button)
            self._choice_buttons.append(button)
        self.choice_layout.addStretch(1)

    def _refresh_provenance(self, conflict: ConflictRecord) -> None:
        """Say where this conflict's current value comes from.

        Three labelled lines, always in the same order and always all three:

        .. code-block:: text

            Machine result:   (blank)
            Manual decision:  1
            Effective result: 1

        A manual decision changes what a script is worth, so which of the two
        readings is in force has to be answerable at a glance rather than
        inferred from a sentence. Writing the machine's value only when it was
        overridden - which the previous wording did - meant the one case where
        the distinction matters was the one case it was easy to misread.
        """
        database = self.database
        if database is None:
            self.provenance_label.setText("")
            return
        self.provenance_label.setText(
            _provenance_html(provenance_for(database, conflict.conflict_id))
        )

    def _refresh_reviewer_label(self) -> None:
        """Show who decisions will be recorded against."""
        if self.state.reviewer:
            self.reviewer_label.setText(
                f"Decisions are recorded as: <b>{self.state.reviewer}</b>"
            )
        else:
            self.reviewer_label.setText(
                "<span style='color:#B3261E;'>Set your reviewer name in "
                "File &gt; Settings before resolving anything.</span>"
            )
        self.reviewer_label.setTextFormat(Qt.TextFormat.RichText)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _selected_reason(self) -> ReasonCode:
        """The reason code the combo currently shows."""
        value = self.reason_combo.currentData()
        try:
            return ReasonCode(value)
        except ValueError:  # pragma: no cover - the combo is built from the enum
            return ReasonCode.OTHER

    def accept_machine(self) -> bool:
        """Record that the reviewer inspected this conflict and agreed."""
        if self._editing_text():
            return False
        return self._act(
            lambda database, conflict: accept_machine_value(
                database, conflict.conflict_id, reviewer=self.state.reviewer
            ),
            "accept",
            advance=True,
        )

    def choose_label(self, label: str) -> bool:
        """Record the value one of the offered symbols stands for.

        Args:
            label: A symbol the template prints for this group, or
                :data:`BLANK_CHOICE`.

        Returns:
            Whether a decision was recorded.

        **The one path a value takes, whether it arrived from a button or a
        key.** The buttons call this, the digit keys call this, and there is no
        second implementation for either to drift from - so a keyboard
        correction cannot end up with a different reviewer, a different reason
        or a different audit event from a clicked one.

        Refuses silently when the active conflict does not offer ``label``. A
        reviewer pressing ``7`` on a set-code position printed ``A``-``D`` has
        typed something this sheet has no answer to, and inventing one would be
        the accidental edit every guard on this page exists to prevent.
        """
        if self._editing_text():
            return False
        conflict = self.current_conflict()
        if conflict is None or not conflict.allows_value_correction:
            return False
        if label != BLANK_CHOICE and label not in self._offered_labels(conflict):
            return False
        return self.correct("" if label == BLANK_CHOICE else label)

    def _offered_labels(self, conflict: ConflictRecord) -> tuple[str, ...]:
        """The symbols the template prints for this conflict's group."""
        if self.state.template is None:
            return ()
        return group_labels(
            self.state.template, conflict.field.zone_id, conflict.field.group_key
        )

    def _correct_from_text(self) -> bool:
        """Record the free-text field's value as the correction."""
        return self.correct(self.free_value_edit.text().strip())

    def correct(self, value: str) -> bool:
        """Record a replacement value for the selected conflict."""
        reason = self._selected_reason()
        text = self.reason_text.toPlainText()
        return self._act(
            lambda database, conflict: correct_value(
                database,
                conflict.conflict_id,
                value=value,
                reviewer=self.state.reviewer,
                reason=reason,
                reason_text=text,
            ),
            "correct",
            advance=True,
        )

    def defer_conflict(self) -> bool:
        """Postpone the selected conflict."""
        if self._editing_text():
            return False
        return self._act(
            lambda database, conflict: defer(
                database,
                conflict.conflict_id,
                reviewer=self.state.reviewer,
                reason_text=self.reason_text.toPlainText(),
            ),
            "defer",
        )

    def reopen_conflict(self) -> bool:
        """Reopen a decided conflict so it can be decided again.

        The earlier decision is not removed - it stays in the history with its
        own reviewer, reason and timestamp.
        """
        return self._act(
            lambda database, conflict: reopen(
                database,
                conflict.conflict_id,
                reviewer=self.state.reviewer,
                reason_text=self.reason_text.toPlainText(),
            ),
            "reopen",
        )

    def _act(
        self,
        operation: Callable[[ProjectDatabase, ConflictRecord], object],
        what: str,
        *,
        advance: bool = False,
    ) -> bool:
        """Run one review action, then refresh what it changed.

        Every action goes through here so that the reviewer check, the error
        handling and the refresh are written once. The action itself is one
        transaction inside the service; this function does no persistence of
        its own, and there is deliberately no other path in this page that
        writes a value anywhere.
        """
        database = self.database
        conflict = self.current_conflict()
        if database is None or conflict is None:
            return False
        try:
            operation(database, conflict)
        except (ReviewError, OMRScannerError) as exc:
            report_error(self, exc, context=f"Conflict review ({what})")
            return False

        _LOGGER.info(
            "Conflict %d: %s recorded by %s", conflict.conflict_id, what, self.state.reviewer
        )
        # A fresh decision makes anything previously undone unrepeatable, which
        # is what every undo stack does and what stops a stale redo putting a
        # value back onto a conflict somebody has since decided differently.
        self.state.redo.clear()
        self.reason_text.clear()
        self._settle(conflict.scan_id)
        self.resolution_recorded.emit(conflict.conflict_id)
        if advance:
            self._advance_after()
        return True

    def _settle(self, scan_id: int) -> None:
        """Re-read everything one write changed, without reloading the sheet.

        The order matters. The sheet's conflicts and their provenance are read
        back *before* the queue is rebuilt, so that the overlay is already
        correct when rebuilding moves the selection - otherwise the decision
        just made would flash away and reappear, or, on the last conflict of a
        sheet, never appear at all.
        """
        self._reload_sheet_conflicts(scan_id)
        self._refresh_lanes()
        self.refresh_queue()

    def _advance_after(self) -> None:
        """Move to the next thing needing a decision, if that is wanted.

        Called only by the two commands that **settle** a conflict. Deferring
        and reopening deliberately do not advance: both are a reviewer saying
        "come back to this", and skipping past it would make the thing they
        asked to see again the thing they cannot find.

        Selecting is all this does. The queue's own ordering decides where
        "next" is - sheet by sheet, since the queue is ordered by severity then
        by scan - so a reviewer who turns auto-advance off walks exactly the
        same sequence by hand, and the last conflict of a sheet leads to the
        next sheet that has one.
        """
        if self.state.auto_advance:
            self.select_next_unresolved()

    # ------------------------------------------------------------------
    # Undo, redo, and taking a whole sheet back
    # ------------------------------------------------------------------
    def undo_last_decision(self) -> bool:
        """Take back the most recent decision recorded anywhere in this batch.

        Batch-wide rather than "whatever is selected", because with
        auto-advance on the conflict just decided is no longer the one on
        screen. The page follows the undo to wherever it landed, so the
        reviewer sees what changed instead of being told that something did.

        Not a visual gesture: the reversal is a persisted, attributed audit
        event, the effective value and the conflict's state are re-derived from
        the ledger, and closing and reopening the project shows the undone
        state, not the decision.
        """
        if self._editing_text():
            return False
        database = self.database
        if database is None or self.state.batch_id is None:
            return False
        target = last_decision(database, self.state.batch_id)
        if target is None:
            return False
        try:
            undone = undo_decision(
                database,
                target.conflict_id,
                reviewer=self.state.reviewer,
                reason_text=self.reason_text.toPlainText(),
            )
        except (ReviewError, OMRScannerError) as exc:
            report_error(self, exc, context="Conflict review (undo)")
            return False

        self.state.redo.append(undone)
        _LOGGER.info(
            "Conflict %d: %s undone by %s",
            undone.conflict_id,
            undone.action.value,
            self.state.reviewer,
        )
        self._settle(undone.scan_id)
        self._show_after_undo(undone.conflict_id)
        self.resolution_recorded.emit(undone.conflict_id)
        return True

    def redo_last_decision(self) -> bool:
        """Make again the decision that was most recently taken back.

        Re-issued through the ordinary decision path, so it is recorded as what
        it is: a decision, by whoever is reviewing now, at the time they made
        it. There is no "redo" event in the ledger, because a history that
        claimed a value had been restored rather than chosen would describe
        something nobody did.
        """
        if self._editing_text() or not self.state.redo:
            return False
        database = self.database
        if database is None:
            return False
        entry = self.state.redo[-1]
        try:
            self._reissue(database, entry)
        except (ReviewError, OMRScannerError) as exc:
            report_error(self, exc, context="Conflict review (redo)")
            return False

        self.state.redo.pop()
        self._settle(entry.scan_id)
        self._show_after_undo(entry.conflict_id)
        self.resolution_recorded.emit(entry.conflict_id)
        return True

    def _reissue(self, database: ProjectDatabase, entry: UndoTarget) -> None:
        """Perform one undone command again, with its original value and reason."""
        if entry.action is ReviewAction.ACCEPTED:
            accept_machine_value(
                database, entry.conflict_id, reviewer=self.state.reviewer
            )
        elif entry.action is ReviewAction.CORRECTED:
            correct_value(
                database,
                entry.conflict_id,
                value=entry.value,
                reviewer=self.state.reviewer,
                reason=_reason_or_other(entry.reason),
                reason_text=entry.reason_text,
            )
        elif entry.action is ReviewAction.DEFERRED:
            defer(
                database,
                entry.conflict_id,
                reviewer=self.state.reviewer,
                reason_text=entry.reason_text,
            )
        else:
            reopen(
                database,
                entry.conflict_id,
                reviewer=self.state.reviewer,
                reason_text=entry.reason_text,
            )

    def undo_last_resolved_sheet(self) -> bool:
        """Put the most recently finished sheet back the way it was.

        Every decision of that sheet's last working session is reversed in one
        transaction and the page navigates back to it, so an operator who
        realises on the next sheet that they were looking at the wrong column
        can return to the whole sheet rather than picking its conflicts out of
        the queue one at a time.

        Decisions made on that sheet in an *earlier* sitting are left alone -
        see :func:`~omr_scanner.services.review_store.undo_resolved_sheet`.
        """
        if self._editing_text():
            return False
        database = self.database
        if database is None or self.state.batch_id is None:
            return False
        try:
            found = undo_resolved_sheet(
                database,
                self.state.batch_id,
                reviewer=self.state.reviewer,
                reason_text=self.reason_text.toPlainText(),
            )
        except (ReviewError, OMRScannerError) as exc:
            report_error(self, exc, context="Conflict review (undo sheet)")
            return False
        if found is None:
            return False

        self.state.redo = list(found.reversed_commands)
        _LOGGER.info(
            "Sheet %d: %d decision(s) undone by %s",
            found.scan_id,
            found.decisions,
            self.state.reviewer,
        )
        self._settle(found.scan_id)
        if found.conflict_ids:
            self._show_after_undo(found.conflict_ids[0])
        return True

    def _show_after_undo(self, conflict_id: int) -> None:
        """Select the conflict an undo acted on, widening the filter if needed.

        A conflict that has just been put back is normally in the default
        "Unresolved" view already. A *reopening* that was undone is not - it
        has returned to resolved - so rather than leave the reviewer looking at
        an unrelated row, the state filter is widened once to show them what
        they changed.
        """
        if self.select_conflict_by_id(conflict_id):
            return
        self.state_filter.setCurrentText(FILTER_ALL)
        self.select_conflict_by_id(conflict_id)

    def _on_auto_advance_toggled(self, enabled: bool) -> None:
        """Adopt the auto-advance toggle and tell the window to remember it."""
        self.state.auto_advance = enabled
        self.auto_advance_changed.emit(enabled)

    def set_auto_advance(self, enabled: bool) -> None:
        """Adopt the stored auto-advance preference."""
        self.state.auto_advance = enabled
        # `setChecked` would emit `toggled` and bounce the preference straight
        # back to the window that just supplied it.
        self.auto_advance_action.blockSignals(True)
        self.auto_advance_action.setChecked(enabled)
        self.auto_advance_action.blockSignals(False)

    def show_history(self) -> bool:
        """Open the provenance dialog for the selected conflict."""
        database = self.database
        conflict = self.current_conflict()
        if database is None or conflict is None:
            return False
        dialog = HistoryDialog(conflict, history_for(database, conflict.conflict_id), self)
        dialog.exec()
        return True

    # ------------------------------------------------------------------
    # Enablement
    # ------------------------------------------------------------------
    def _refresh_controls(self) -> None:
        """Enable exactly the controls that can do something right now."""
        conflict = self.current_conflict()
        has_conflict = conflict is not None
        named = bool(self.state.reviewer)
        can_decide = has_conflict and named

        self.accept_button.setEnabled(can_decide)
        self.defer_button.setEnabled(can_decide)
        self.reopen_button.setEnabled(
            can_decide and conflict is not None and conflict.state.is_human_touched
        )
        for button in self._choice_buttons:
            button.setEnabled(can_decide)
        self.free_value_edit.setEnabled(can_decide)
        self.free_value_button.setEnabled(can_decide)
        self.history_button.setEnabled(has_conflict)
        for action in (
            self.previous_action,
            self.next_action,
            self.previous_open_action,
            self.next_open_action,
        ):
            action.setEnabled(bool(self.state.conflicts))
        self._refresh_undo_controls(named=named)
        self._refresh_reviewer_label()

    def _refresh_undo_controls(self, *, named: bool) -> None:
        """Enable and describe the three reversal controls.

        Each one says what it would actually take back. "Undo decision" with no
        idea what it would undo is how an operator ends up pressing it to find
        out - on a stage where finding out means changing what a script is
        worth.
        """
        database = self.database
        batch = self.state.batch_id
        target = (
            last_decision(database, batch)
            if database is not None and batch is not None
            else None
        )
        self.undo_action.setEnabled(named and target is not None)
        self.undo_action.setToolTip(
            f"Undo {target.describe} (Ctrl+Z)"
            if target is not None
            else "Undo the last decision (Ctrl+Z) - nothing to undo yet"
        )

        entry = self.state.redo[-1] if self.state.redo else None
        self.redo_action.setEnabled(named and entry is not None)
        self.redo_action.setToolTip(
            f"Redo {entry.describe} (Ctrl+Y)"
            if entry is not None
            else "Redo the decision you last undid (Ctrl+Y) - nothing to redo"
        )

        sheet = (
            last_resolved_sheet(database, batch)
            if database is not None and batch is not None
            else None
        )
        self.undo_sheet_action.setEnabled(named and sheet is not None)
        self.undo_sheet_action.setToolTip(
            f"Undo all {sheet.decisions} decision(s) on "
            f"{sheet.scan_name or 'the last sheet you finished'} (Ctrl+Shift+Z)"
            if sheet is not None
            else "Undo the last sheet you finished (Ctrl+Shift+Z) - none yet"
        )

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Wait for the sheet loader, so no thread outlives the window.

        A ``QThread`` still running when the interpreter tears down makes Qt
        abort the process - on Windows with a bare ``0xC0000409`` and no
        traceback. Decoding a sheet cannot usefully be interrupted part-way, so
        this waits rather than trying to cancel it; it is at most the time one
        image takes to read.

        **Every** loader, not just the most recent: a superseded one is still a
        running thread.
        """
        workers = self._workers
        self._worker = None
        self._workers = []
        for worker in workers:
            if worker.isRunning():
                worker.wait(WORKER_SHUTDOWN_TIMEOUT_MS)

    def closeEvent(self, event: object) -> None:
        """Stop the sheet loader before the page disappears."""
        self.shutdown()
        super().closeEvent(event)  # type: ignore[arg-type]


def _reason_or_other(stored: str) -> ReasonCode:
    """Return a stored reason code, falling back to ``OTHER``.

    A code written by a later build is not a reason to refuse to repeat a
    decision; it is a reason not to claim to know what it meant.
    """
    try:
        return ReasonCode(stored)
    except ValueError:
        return ReasonCode.OTHER


def _state_tooltip(conflict: ConflictRecord) -> str:
    """Explain a queue row's state in a sentence."""
    if conflict.state is ConflictState.OPEN and conflict.reversed_before:
        return (
            "Open again: somebody decided this and then reopened or undid the "
            "decision. The earlier one is still in the history."
        )
    return {
        ConflictState.OPEN: "Nobody has decided this yet.",
        ConflictState.RESOLVED: "A named reviewer decided this.",
        ConflictState.DEFERRED: (
            "A named reviewer postponed this. It still counts as unresolved."
        ),
        ConflictState.WITHDRAWN: (
            "Re-reading the sheet no longer produces this conflict. Kept as "
            "evidence; not in any count."
        ),
    }[conflict.state]


def _provenance_html(found: Provenance) -> str:
    """Render the machine's reading, the human decision and what is in force.

    All three lines always, even when two of them say the same thing. The
    question a reviewer is answering - "is this value the machine's or
    somebody's?" - has to be answerable by looking at one place, and a panel
    whose shape changed with the answer made the important case the unusual
    one.
    """
    machine = found.machine_value or "(blank)"
    effective = found.value or "(blank)"
    if found.source is not ValueSource.HUMAN:
        return (
            f"Machine result: <b>{machine}</b><br>"
            "Manual decision: &mdash;<br>"
            f"Effective result: <b>{effective}</b> "
            "<i>(machine result, not yet reviewed)</i>"
        )
    verb = "corrected" if found.was_corrected else "confirmed"
    return (
        f"Machine result: <b>{machine}</b> <i>(kept)</i><br>"
        f"Manual decision: <b>{effective}</b> &mdash; {verb} by "
        f"<b>{found.reviewer}</b> at {found.decided_at}<br>"
        f"Effective result: <b>{effective}</b>"
    )


def _scan_quality_html(assessment: ScanQualityAssessment) -> str:
    """Render the page-geometry diagnostics behind a scan-quality conflict.

    Shown only for that conflict type, and only the numbers a person can act
    on. The full measurement - every probe, its displacement and its
    correlation peak - stays in the stored result for a developer to read; a
    reviewer deciding whether to re-scan a sheet needs to know *where* and *how
    bad*, not the contents of a correlation surface.
    """
    lines = [
        "<br><b>Scan quality</b><br>",
        f"Verdict: <b>{assessment.status.value.upper()}</b><br>",
    ]
    for issue in assessment.issues:
        where = issue.area_label or "across the page"
        lines.append(f"&nbsp;&nbsp;&bull; {issue_label(issue.code)} &mdash; {where}")
        if issue.question_range:
            lines.append(f" (questions {issue.question_range})")
        lines.append("<br>")
        if issue.zone_labels:
            lines.append(
                f"&nbsp;&nbsp;&nbsp;&nbsp;<i>{', '.join(issue.zone_labels)}</i><br>"
            )
    if assessment.evaluated:
        lines.append(
            "<br>Printed areas checked: "
            f"{assessment.matched_count}/{assessment.probe_count} located, "
            f"{assessment.affected_count} displaced<br>"
            "Displacement (95th percentile): "
            f"{assessment.displacement_p95_pitch:.2f} bubble pitches<br>"
            "Unexplained by a flat page: "
            f"{assessment.nonprojective_p95_pitch:.2f}<br>"
        )
    else:
        lines.append(
            "<br><i>The page geometry could not be verified; this is not a "
            "confirmation that it is sound.</i><br>"
        )
    return "".join(lines)


def _evidence_html(
    conflict: ConflictRecord, assessment: ScanQualityAssessment | None = None
) -> str:
    """Render one conflict's machine evidence.

    Fill ratios are labelled **fill score**, never "probability" or
    "confidence": the engine measures what fraction of a sampled bubble is ink,
    and presenting 0.71 as a calibrated likelihood would invent a statistic
    nobody has produced. ``confidence`` *is* shown where the engine genuinely
    computes one, and is described as a decision score.
    """
    observation = conflict.observation
    lines = [
        f"<b>{conflict.field.describe()}</b> &mdash; {conflict.conflict_type.label}<br>",
        f"Sheet: {conflict.scan_name or '(unknown)'}<br>",
        f"Machine value: <b>{observation.value or '(blank)'}</b>"
        f" &middot; status <b>{observation.status or 'n/a'}</b><br>",
    ]
    if conflict.field.kind is not FieldKind.OTHER:
        lines.append(
            f"Decision score: {observation.confidence:.2f} &middot; "
            f"top fill {observation.top_fill:.2f} &middot; "
            f"margin {observation.margin:.2f}<br>"
        )
    if observation.detail:
        lines.append(f"<i>{observation.detail}</i><br>")

    if conflict.conflict_type is ConflictType.SCAN_QUALITY and assessment is not None:
        lines.append(_scan_quality_html(assessment))
        return "".join(lines)

    if observation.candidates:
        lines.append("<br><b>Measured fill scores</b><br>")
        for candidate in observation.candidates:
            mark = " &check;" if candidate.selected else ""
            lines.append(
                f"&nbsp;&nbsp;{candidate.label}: {candidate.fill_ratio:.3f}{mark}<br>"
            )
    else:
        lines.append(
            "<br><i>Per-bubble fill scores were not kept for this batch. The "
            "sheet is re-read when you select it, so the scores above come from "
            "the stored result and the overlay from a fresh reading.</i><br>"
        )

    if conflict.related_scan_ids:
        lines.append(
            f"<br><b>Also on {len(conflict.related_scan_ids)} other sheet(s)</b> in "
            "this batch - search the queue by this student ID to see them.<br>"
        )
    return "".join(lines)


__all__ = ["ResolvePage", "ResolvePageState"]
