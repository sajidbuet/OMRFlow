"""The Resolve workflow stage: conflict queue and review workspace (Phase 6).

Purpose:
    Put a human in front of every value the machine could not decide, with
    enough of what the machine saw to decide it properly - and record that
    decision so the final value can always be traced back to a named person and
    a reason.

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
    ConflictState,
    ConflictType,
    FieldKind,
    ReasonCode,
    ValueSource,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.review.history_dialog import HistoryDialog
from omr_scanner.gui.review.worker import SheetBundle, SheetWorker
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.gui.theme import TEMPLATE_DESIGNER_STYLESHEET
from omr_scanner.services import (
    ConflictFilter,
    ConflictRecord,
    ReviewError,
    accept_machine_value,
    correct_value,
    count_conflicts,
    count_conflicts_for_scan,
    defer,
    group_cells,
    group_labels,
    history_for,
    list_conflicts,
    load_summary,
    map_canonical_to_source,
    provenance_for,
    reopen,
    scan_source_path,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import (
        BubbleView,
        ProjectDatabase,
        ProjectSession,
        ScanResult,
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

_STATE_LABELS: dict[ConflictState, str] = {
    ConflictState.OPEN: "Open",
    ConflictState.RESOLVED: "Resolved",
    ConflictState.DEFERRED: "Deferred",
    ConflictState.WITHDRAWN: "Withdrawn",
}

BLANK_CHOICE = "(blank)"
"""The label for "this position carries no mark".

A real choice, not the absence of one: a reviewer deciding that a candidate
left a question empty is making a decision, and it has to be recordable."""


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
    """

    session: ProjectSession | None = None
    batch_id: str | None = None
    template: OmrTemplate | None = None
    conflicts: list[ConflictRecord] = field(default_factory=list)
    reviewer: str = ""
    bundle: SheetBundle | None = None


class ResolvePage(WorkflowPage):
    """Review and resolve what recognition could not decide.

    Signals:
        conflict_selected: ``int`` conflict id whenever the shown conflict
            changes; ``-1`` when the selection is cleared.
        sheet_ready: emitted once a selected conflict's sheet has finished
            loading. GUI tests wait on this instead of sleeping.
        resolution_recorded: ``int`` conflict id after a decision is stored.

    Args:
        spec: The "resolve" workflow stage description.
        parent: Optional Qt parent.
    """

    conflict_selected = Signal(int)
    sheet_ready = Signal()
    resolution_recorded = Signal(int)

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setObjectName("resolvePage")
        self.setStyleSheet(TEMPLATE_DESIGNER_STYLESHEET)

        self.state = ResolvePageState()
        self._worker: SheetWorker | None = None
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
        for conflict_type in ConflictType:
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
        """Build the navigation and zoom toolbar above the views."""
        toolbar = QToolBar("Review")
        toolbar.setObjectName("reviewToolbar")
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setMovable(False)

        self.previous_action = QAction(load_icon("undo-2"), "Previous", self)
        self.previous_action.setObjectName("previousConflictButton")
        self.previous_action.setToolTip("Previous conflict (Left arrow)")
        self.previous_action.triggered.connect(self.select_previous)
        toolbar.addAction(self.previous_action)

        self.next_action = QAction(load_icon("redo-2"), "Next", self)
        self.next_action.setObjectName("nextConflictButton")
        self.next_action.setToolTip("Next conflict (Right arrow)")
        self.next_action.triggered.connect(self.select_next)
        toolbar.addAction(self.next_action)

        self.next_open_action = QAction(load_icon("list-checks"), "Next unresolved", self)
        self.next_open_action.setObjectName("nextUnresolvedConflictButton")
        self.next_open_action.setToolTip("Skip to the next conflict nobody has decided")
        self.next_open_action.triggered.connect(self.select_next_unresolved)
        toolbar.addAction(self.next_open_action)
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
            "Undo the decision without erasing it - the earlier correction stays "
            "in the history"
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

        Only navigation and the two non-destructive actions get a key. Choosing
        a *value* deliberately does not: a stray keypress that silently
        corrected an answer would be exactly the kind of accidental destructive
        edit this phase exists to prevent, and every correction must carry a
        reason anyway.

        ``Qt.WidgetWithChildrenShortcut`` scopes them to this page, so they
        cannot fire while another page is on screen; and because the reason box
        is a text editor, typing in it consumes the keys before a shortcut sees
        them.
        """
        for keys, slot in (
            (QKeySequence(Qt.Key.Key_Left), self.select_previous),
            (QKeySequence(Qt.Key.Key_Right), self.select_next),
            (QKeySequence(Qt.Key.Key_Return), self.accept_machine),
            (QKeySequence(Qt.Key.Key_Enter), self.accept_machine),
            (QKeySequence(Qt.Key.Key_D), self.defer_conflict),
        ):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

    # ------------------------------------------------------------------
    # Project and batch
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt the open project, forgetting any batch from the previous one."""
        self.state.session = session
        self.state.batch_id = None
        self.state.conflicts = []
        self.state.bundle = None
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
                values = (
                    conflict.scan_name,
                    conflict.identifier_value,
                    conflict.field.describe(),
                    conflict.conflict_type.label,
                    _STATE_LABELS[conflict.state],
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setBackground(_STATE_COLORS[conflict.state])
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

    def select_next_unresolved(self) -> None:
        """Skip forward to the next conflict nobody has decided.

        Wraps to the start, so working through a queue ends by returning to
        whatever was skipped rather than stopping silently at the bottom.
        """
        total = len(self.state.conflicts)
        if not total:
            return
        start = self.queue_table.currentRow()
        for step in range(1, total + 1):
            row = (start + step) % total
            if self.state.conflicts[row].state is ConflictState.OPEN:
                self.queue_table.selectRow(row)
                return

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
            for view in (self.normalised_view, self.zoom_view):
                view.set_page(
                    result.preview,
                    canonical_width=result.canonical_width,
                    canonical_height=result.canonical_height,
                    preview_scale=result.preview_scale,
                )
                view.set_overlay(result.zones, self._conflict_bubbles(conflict), ())
                view.set_overlay_visible(
                    zones=True, bubbles=True, empty=True, centers=True
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
        zone_id = conflict.field.zone_id or self._zone_for_kind(
            bundle.result, conflict.field.kind
        )
        if not zone_id:
            return ()
        if conflict.field.is_whole_field:
            return tuple(
                item for item in bundle.result.bubbles if item.zone_id == zone_id
            )
        cells = set(group_cells(self.state.template, zone_id, conflict.field.group_key))
        return tuple(
            item
            for item in bundle.result.bubbles
            if item.zone_id == zone_id and (item.row, item.column) in cells
        )

    @staticmethod
    def _zone_for_kind(result: ScanResult, kind: FieldKind) -> str:
        """Return the zone a whole-field conflict refers to, when it names none.

        A batch-level conflict is raised from the identifiers alone - a
        duplicate is not a property of one sheet, so the pass that finds it has
        never seen a zone. The sheet has been re-read by the time it is
        reviewed, though, and the *engine's own* result says which zone it read
        the identifier from. Using that keeps the highlight on the block a
        reviewer needs to read, without the GUI guessing at a zone name.
        """
        if kind is FieldKind.IDENTIFIER:
            return result.identifier_zone_id or ""
        if kind is FieldKind.SET_CODE:
            return result.set_code_zone_id or ""
        return ""

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
        self.evidence_label.setText(_evidence_html(conflict))

    def _refresh_sheet_progress(self, conflict: ConflictRecord) -> None:
        """Say how many conflicts this sheet has, and how many are left."""
        database = self.database
        if database is None or self.state.batch_id is None:
            self.sheet_progress_label.setText("")
            return
        counts = count_conflicts_for_scan(database, self.state.batch_id, conflict.scan_id)
        self.sheet_progress_label.setText(
            f"This sheet: {counts.total} conflict(s), {counts.unresolved} unresolved"
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
            button.setToolTip(f"Record '{label}' as the corrected value")
            value = "" if label == BLANK_CHOICE else label
            button.clicked.connect(lambda _checked=False, v=value: self.correct(v))
            self.choice_layout.addWidget(button)
            self._choice_buttons.append(button)
        self.choice_layout.addStretch(1)

    def _refresh_provenance(self, conflict: ConflictRecord) -> None:
        """Say where this conflict's current value comes from."""
        database = self.database
        if database is None:
            self.provenance_label.setText("")
            return
        found = provenance_for(database, conflict.conflict_id)
        if found.source is ValueSource.MACHINE:
            self.provenance_label.setText(
                f"Effective value: <b>{found.value or '(blank)'}</b> "
                "&mdash; machine result, not yet reviewed."
            )
            return
        self.provenance_label.setText(
            f"Effective value: <b>{found.value or '(blank)'}</b> &mdash; "
            f"{'corrected' if found.was_corrected else 'confirmed'} by "
            f"<b>{found.reviewer}</b> at {found.decided_at}.<br>"
            f"Machine read <b>{found.machine_value or '(blank)'}</b>, which is kept."
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
        return self._act(
            lambda database, conflict: accept_machine_value(
                database, conflict.conflict_id, reviewer=self.state.reviewer
            ),
            "accept",
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
        )

    def defer_conflict(self) -> bool:
        """Postpone the selected conflict."""
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
        self, operation: Callable[[ProjectDatabase, ConflictRecord], object], what: str
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
        self.reason_text.clear()
        self.refresh_queue()
        self.resolution_recorded.emit(conflict.conflict_id)
        return True

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
        for action in (self.previous_action, self.next_action, self.next_open_action):
            action.setEnabled(bool(self.state.conflicts))
        self._refresh_reviewer_label()

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
        """
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.wait(WORKER_SHUTDOWN_TIMEOUT_MS)

    def closeEvent(self, event: object) -> None:
        """Stop the sheet loader before the page disappears."""
        self.shutdown()
        super().closeEvent(event)  # type: ignore[arg-type]


def _evidence_html(conflict: ConflictRecord) -> str:
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
