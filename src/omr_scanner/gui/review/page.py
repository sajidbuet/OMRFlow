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

import html
import logging
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QKeySequence,
    QResizeEvent,
    QShortcut,
    QShowEvent,
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
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.review import (
    RESOLUTION_TYPES,
    ConflictScope,
    ConflictState,
    ConflictType,
    FieldKind,
    MachineObservation,
    ReasonCode,
    ReviewAction,
    ValueSource,
)
from omr_scanner.domain.scan_lifecycle import (
    FileState,
    RejectionReason,
    RescanCase,
)
from omr_scanner.domain.scan_quality import issue_label
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.review.history_dialog import HistoryDialog
from omr_scanner.gui.review.lanes import (
    bounds_of,
    build_lanes,
    context_bubbles,
    group_bubbles,
)
from omr_scanner.gui.review.rescan import (
    LifecycleHistoryDialog,
    RejectScanDialog,
    RescanPanel,
    SheetPanel,
)
from omr_scanner.gui.review.worker import OriginalImageWorker, SheetBundle, SheetWorker
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.gui.theme import (
    CANDIDATE_CHOSEN,
    CANDIDATE_MACHINE,
    CANDIDATE_STATE_PROPERTY,
    VARIANT_PRIMARY,
    VARIANT_PROPERTY,
    Color,
    Spacing,
)
from omr_scanner.gui.ui_scale import scale_layout, scale_widget, set_floor, set_scaled_stylesheet
from omr_scanner.services import (
    ConflictFilter,
    ConflictRecord,
    FieldEdit,
    FieldShape,
    ReviewError,
    UndoTarget,
    accept_machine_value,
    correct_value,
    count_conflicts,
    count_conflicts_for_scan,
    defer,
    field_shape,
    group_labels,
    history_for,
    join_field_value,
    last_decision,
    last_resolved_sheet,
    list_conflicts,
    load_summary,
    map_canonical_to_source,
    project_sets,
    provenance_for,
    provenance_for_scan,
    reopen,
    scan_lifecycle,
    scan_source_path,
    session_population,
    set_identity,
    split_field_value,
    split_marks,
    undo_decision,
    undo_field_edit,
    undo_resolved_sheet,
)
from omr_scanner.services.field_edit import (
    UNKNOWN_POSITION,
    FieldEditPlan,
    commit_field_edit,
    machine_characters,
    machine_observation_at,
    override_warning_text,
    plan_field_edit,
    validate_field_value,
)
from omr_scanner.services.field_edit import (
    current_field_values as field_values_now,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Collection, Sequence

    from omr_scanner.domain.review import Provenance
    from omr_scanner.domain.scan_lifecycle import ReplacementCandidate
    from omr_scanner.domain.scan_quality import ScanQualityAssessment
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.gui.scan.preview import FieldLane
    from omr_scanner.services import (
        BubbleView,
        ProjectDatabase,
        ProjectSession,
    )
    from omr_scanner.services.scan_lifecycle import ProcessedSheet

_LOGGER = logging.getLogger(__name__)

QUEUE_STRETCH = 29
WORKSPACE_STRETCH = 71
"""How the window's width is divided between the queue and the workspace.

Stretch factors, not pixels: the operator resizes the splitter, and the ratio
has to hold at 1366 and at 2560 alike. Just under a third for the queue is
what makes its five columns readable without eliding the conflict type, and
the workspace is where the evidence and the decision live."""

PREVIEW_STRETCH = 70
RESOLUTION_STRETCH = 30
"""How the workspace's height is divided between the sheet and the decision.

The image is what a reviewer decides *from*, so it gets the larger share and
keeps it: the resolution panel is a fixed set of controls, and letting a long
diagnostic paragraph push the preview upward is how the evidence ends up
smaller than the prose about it."""

MACHINE_PANEL_STRETCH = 37
DECISION_PANEL_STRETCH = 63
"""How the resolution row is divided. See :meth:`ResolvePage._build_decision_panel`."""

QUEUE_MIN_WIDTH = 320
WORKSPACE_MIN_WIDTH = 520
PREVIEW_MIN_HEIGHT = 220
RESOLUTION_MIN_HEIGHT = 190
"""Floors, so that dragging a splitter cannot collapse a pane into a sliver.

``RESOLUTION_MIN_HEIGHT`` is measured rather than guessed: the heading, the
value buttons, the reason row, the comparison strip and the action row, at the
spacing this page uses."""

ZOOM_TAB_INDEX = 0
NORMALISED_TAB_INDEX = 1
ORIGINAL_TAB_INDEX = 2
"""The three preview tabs, in the order :meth:`ResolvePage._build_views` adds
them. Named so that "switch to the original scan" does not read as ``2``."""

QUEUE_MIN_SECTION_PX = 56
"""How narrow a queue column may get before it stops being readable at all."""

NOTE_HEIGHT_PX = 30
ACTION_BUTTON_HEIGHT_PX = 32
CHOICE_BUTTON_MIN_WIDTH = 38
CHOICE_BUTTON_HEIGHT = 34
"""Value-button metrics. Large enough to hit repeatedly at speed, small enough
that eleven of them fit a 1366-pixel display beside everything else."""

QUEUE_PANEL_WIDTH = 460
"""The queue's resting width, used only as the ceiling below. The split itself
is a ratio - see :data:`QUEUE_STRETCH`."""

QUEUE_PAGE_SIZE = 500
"""How many conflicts the queue holds at once.

A page rather than the whole batch: thousands of conflicts is a realistic
number, and building thousands of table rows on every filter change is the
difference between a queue that responds and one that stutters. The filters do
the narrowing in SQL; this bounds what reaches Qt."""

WORKER_SHUTDOWN_TIMEOUT_MS = 30_000
"""How long the page waits for the sheet loader when it closes."""

FILTER_ALL = "All"
FILTER_OPEN = "Unresolved"
FILTER_RESOLVED = "Resolved"
FILTER_DEFERRED = "Deferred"
FILTER_WITHDRAWN = "Withdrawn"
FILTER_RESCAN = "Rejected / Rescan"
"""The queue view listing Reject & Rescan cases instead of conflicts.

Part of the ordinary status filter rather than a panel of its own: the same
queue, the same search box, the same Ctrl+Up / Ctrl+Down - which here walk the
cases still awaiting a rescan, passing over the completed ones."""

FILTER_SHEETS = "All processed sheets"
"""The inspection view: every read sheet of the batch, conflict or not.

The way to a sheet recognition had no doubt about, so it can be rejected when
the paper says otherwise. It lists sheets, not problems: nothing in it counts
as unresolved, and unresolved navigation does not walk it."""

REJECT_KEY = Qt.Key.Key_R
"""The key that opens *Reject / Rescan* for the sheet being reviewed. Free on
this page: digits choose values, ``B`` blank, ``D`` defer, ``E`` edit."""

_STATE_FILTERS: dict[str, tuple[ConflictState, ...]] = {
    FILTER_OPEN: (ConflictState.OPEN,),
    FILTER_RESOLVED: (ConflictState.RESOLVED,),
    FILTER_DEFERRED: (ConflictState.DEFERRED,),
    FILTER_WITHDRAWN: (ConflictState.WITHDRAWN,),
}
"""``FILTER_ALL`` is absent on purpose: "no filter" is the absence of an entry,
so a state added later cannot be quietly left out of the unfiltered view."""

ALL_TYPES = "All types"
ALL_BATCHES = "All batches of the session"

_STATE_COLORS: dict[ConflictState, QColor] = {
    ConflictState.OPEN: QColor(255, 244, 214),
    ConflictState.RESOLVED: QColor(226, 245, 229),
    ConflictState.DEFERRED: QColor(226, 236, 250),
    ConflictState.WITHDRAWN: QColor(238, 238, 238),
}

_RESCAN_PENDING_COLOR = QColor(252, 226, 224)
_RESCAN_DONE_COLOR = QColor(238, 238, 238)
"""Row tints in the Rejected / Rescan view - the third way of saying what the
State column already says in words and a glyph."""

SHEET_CONTINUATION = "⤷"
"""Drawn instead of the file name on a sheet's second and later queue rows.

Eight identical file names in a column read as eight unrelated problems. One
name, then seven continuations, reads as one script with eight things wrong -
which is what it is, and what decides how the operator works it. The full name
stays in every cell's tooltip."""

BLANK_BUTTON_TEXT = "Blank"
"""What the "no mark here" button says.

The word, not ``(blank)``: the parenthesised form reads as an absence of a
choice in a row of digits, and it is a choice. The stored value is still the
empty string and :data:`BLANK_CHOICE` is still the internal name, so nothing
downstream is affected by the wording."""

BLANK_CHOICE = "(blank)"
"""The label for "this position carries no mark".

A real choice, not the absence of one: a reviewer deciding that a candidate
left a question empty is making a decision, and it has to be recordable."""

BLANK_KEY = Qt.Key.Key_B
"""The key that chooses "no mark here" when a numeric position is active.

``B`` for blank. Deliberately not the space bar, which every Qt widget on the
page already uses for something, and not ``0`` - a roll-number column has a
zero, and a reviewer who meant the digit must never get the blank."""


FIELD_EDIT_APPLY_TEXT = "Apply"
"""The whole-field editor's commit button, when nothing it writes overrides a
confident reading. It reads "Apply override..." when something does."""


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
        pending: ``conflict_id -> value`` for the choices the reviewer has made
            and not yet committed. ``""`` is a real choice - *this position is
            blank* - so membership, not truthiness, is what "picked" means.

            Nothing is written while this holds anything. It exists so that
            choosing a value and committing it are two separate acts: the
            reviewer picks, sees the ring land on the bubble they meant, and
            then commits. A **map** rather than one value because a whole-field
            edit stages every position it will change at once, and all of them
            have to be visible on the sheet before any of them is written.
        editing_field: The field the whole-field editor is open on, or
            ``None``. Held so that the editor survives the refreshes a
            selection change causes.
        editing_scan_id: The sheet it was opened on. Moving to another sheet
            closes it, so a value typed for one script is never applied to the
            next.
        editing_kind: Which identity field that is - the Student ID or the
            set code - so an override is recorded against the field being
            edited, not against whichever record happens to be selected.
        last_field_edit: The most recent whole-field correction, so undo can
            take back the operator's *action* rather than the last of the
            several positions it happened to write.
        rescan_cases: The *Rejected / Rescan* view's rows, when it is shown.
        rescan_candidates: Possible rescans per outstanding case, read once
            with the view.
        showing_replacement: Whether the image view shows a completed case's
            replacement rather than its rejected original.
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
    pending: dict[int, str] = field(default_factory=dict)
    editing_field: FieldShape | None = None
    editing_scan_id: int | None = None
    editing_kind: FieldKind | None = None
    last_field_edit: FieldEdit | None = None
    rescan_cases: list[RescanCase] = field(default_factory=list)
    sheet_rows: list[ProcessedSheet] = field(default_factory=list)
    rescan_candidates: dict[int, tuple[ReplacementCandidate, ...]] = field(
        default_factory=dict
    )
    showing_replacement: bool = False


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
        lifecycle_changed: ``int`` scan id after a Reject & Rescan action -
            reject, undo, replacement confirmed or removed - so the window can
            tell the Attendance, Results and Reports stages to re-read.
        rescan_import_requested: ``(batch id, [paths])`` when the operator
            asks to read rescan files into this batch. The window routes it to
            the Scan stage, which owns processing.

    Args:
        spec: The "resolve" workflow stage description.
        parent: Optional Qt parent.
    """

    conflict_selected = Signal(int)
    sheet_ready = Signal()
    resolution_recorded = Signal(int)
    auto_advance_changed = Signal(bool)
    lifecycle_changed = Signal(int)
    rescan_import_requested = Signal(str, list)

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setObjectName("resolvePage")
        set_scaled_stylesheet(self, "template_designer", "resolve_stage")

        self.state = ResolvePageState()
        self._worker: SheetWorker | OriginalImageWorker | None = None
        self._workers: list[SheetWorker | OriginalImageWorker] = []
        self._loaded_scan_id: int | None = None
        self._suppress_selection = False
        self._batch_unresolved = 0
        self._preferred_tab = ZOOM_TAB_INDEX
        self._suppress_tab_memory = False

        # Ratios rather than pixel sizes, and floors rather than nothing: the
        # proportion has to survive a 1366-pixel laptop and a 2560-pixel
        # desktop, and neither pane may be dragged into a sliver the operator
        # then has to fish back out.
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("resolveMainSplitter")
        self.main_splitter.setChildrenCollapsible(False)
        queue_panel = self._build_queue_panel()
        set_floor(queue_panel, minimum_width=QUEUE_MIN_WIDTH)
        workspace = self._build_workspace()
        set_floor(workspace, minimum_width=WORKSPACE_MIN_WIDTH)
        self.main_splitter.addWidget(queue_panel)
        self.main_splitter.addWidget(workspace)
        self.main_splitter.setStretchFactor(0, QUEUE_STRETCH)
        self.main_splitter.setStretchFactor(1, WORKSPACE_STRETCH)
        self.main_splitter.splitterMoved.connect(self._on_splitter_moved)
        self.body.addWidget(self.main_splitter, stretch=1)
        self._splitters_adjusted = False

        self._install_shortcuts()
        self._refresh_controls()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_queue_panel(self) -> QWidget:
        """Build the left-hand queue, its filters and the batch summary."""
        panel = QWidget()
        panel.setObjectName("conflictQueuePanel")
        scale_widget(panel, maximum_width=QUEUE_PANEL_WIDTH + 200)
        layout = QVBoxLayout(panel)
        scale_layout(layout, margins=(0, 0, 6, 0), spacing=6)

        self.batch_label = QLabel("No batch selected")
        self.batch_label.setObjectName("reviewBatchLabel")
        self.batch_label.setWordWrap(True)
        layout.addWidget(self.batch_label)

        filters = QWidget()
        filter_layout = QHBoxLayout(filters)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(filter_layout, spacing=Spacing.XS)

        # Labelled, on one row. Two bare combo boxes reading "Unresolved" and
        # "All types" look like values of something unnamed; an operator should
        # not have to open one to find out what it filters.
        filter_layout.addWidget(QLabel("Status"))
        self.state_filter = QComboBox()
        self.state_filter.setObjectName("conflictStateFilter")
        self.state_filter.addItems(
            [
                FILTER_OPEN,
                FILTER_ALL,
                FILTER_RESOLVED,
                FILTER_DEFERRED,
                FILTER_WITHDRAWN,
                FILTER_RESCAN,
                FILTER_SHEETS,
            ]
        )
        self.state_filter.setToolTip(
            "Show conflicts in this review state - or, under Rejected / Rescan, "
            "the sheets rejected as unusable and their rescans"
        )
        self.state_filter.currentIndexChanged.connect(self.refresh_queue)
        filter_layout.addWidget(self.state_filter, stretch=1)

        filter_layout.addWidget(QLabel("Type"))
        self.type_filter = QComboBox()
        self.type_filter.setObjectName("conflictTypeFilter")
        self.type_filter.addItem(ALL_TYPES, userData="")
        # Only the types this stage can actually hold. Offering "Multiple
        # answers marked" in a queue that never contains one would advertise a
        # filter that always returns nothing.
        for conflict_type in RESOLUTION_TYPES:
            self.type_filter.addItem(conflict_type.label, userData=conflict_type.value)
        self.type_filter.setToolTip("Show only one kind of conflict")
        self.type_filter.currentIndexChanged.connect(self.refresh_queue)
        filter_layout.addWidget(self.type_filter, stretch=1)
        layout.addWidget(filters)

        # A diagnostic view (0.1.1 phase 4): which batch a sheet was read in.
        # It narrows what is listed only - the queue is the session's, and a
        # duplicate-ID group spans every batch it spans whatever is selected.
        batch_row = QHBoxLayout()
        batch_row.setContentsMargins(0, 0, 0, 0)
        batch_row.addWidget(QLabel("Batch"))
        self.batch_filter = QComboBox()
        self.batch_filter.setObjectName("conflictBatchFilter")
        self.batch_filter.addItem(ALL_BATCHES, userData="")
        self.batch_filter.setToolTip(
            "Diagnostic view: show only sheets read in one batch of the scan session"
        )
        self.batch_filter.currentIndexChanged.connect(self.refresh_queue)
        batch_row.addWidget(self.batch_filter, stretch=1)
        layout.addLayout(batch_row)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("conflictSearchBox")
        self.search_box.setPlaceholderText("Search by student ID or file name...")
        self.search_box.setClearButtonEnabled(True)
        # `editingFinished` rather than `textChanged`: a query per keystroke
        # would re-run a filtered query over thousands of rows on every letter.
        self.search_box.editingFinished.connect(self.refresh_queue)
        self.search_box.returnPressed.connect(self.refresh_queue)
        layout.addWidget(self.search_box)

        # Four columns, not five. "Field" and "Conflict" were two narrow
        # columns whose text was elided in both - "Student ID - ..." beside
        # "Student ID ..." - which is two truncations of one fact. Joined, the
        # row reads as a sentence: *Student ID - position 5 - More than one
        # mark*, and there is room for it.
        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setObjectName("conflictQueueTable")
        self.queue_table.setHorizontalHeaderLabels(
            ["Sheet", "Student ID", "Issue", "State"]
        )
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.queue_table.setWordWrap(False)
        self.queue_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        # Every column is given a mode, because Qt's default - an equal fixed
        # width for each - put the five of them past the panel's edge and left
        # the **State** column off-screen behind a horizontal scrollbar. A
        # reviewer cannot use a state cue they have to scroll to find.
        #
        # Only the two short columns take their own width: the identifier,
        # which is what a reviewer searches by, and the state, which is the
        # cue this column exists for. The file name, the field and the
        # conflict type share what is left and elide - all three are long, all
        # three repeat down the column, and all three are shown in full in the
        # workspace the moment a row is selected. Sizing any of them to content
        # is what pushed the state off the edge.
        header = self.queue_table.horizontalHeader()
        for column, mode in (
            (0, QHeaderView.ResizeMode.Stretch),
            (1, QHeaderView.ResizeMode.ResizeToContents),
            (2, QHeaderView.ResizeMode.Stretch),
            (3, QHeaderView.ResizeMode.ResizeToContents),
        ):
            header.setSectionResizeMode(column, mode)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(QUEUE_MIN_SECTION_PX)
        self.queue_table.itemSelectionChanged.connect(self._on_queue_selection_changed)
        layout.addWidget(self.queue_table, stretch=1)

        # Two lines, not six. The counters an operator watches go on the first;
        # the per-type breakdown, which is a report rather than a control, goes
        # on the second in smaller type. This is a workstation, not a
        # dashboard - the queue above is what the space belongs to.
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("reviewSummaryLabel")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)

        self.summary_breakdown = QLabel("")
        self.summary_breakdown.setObjectName("reviewSummaryBreakdown")
        self.summary_breakdown.setWordWrap(True)
        self.summary_breakdown.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_breakdown)

        self.navigation_note = QLabel("")
        self.navigation_note.setObjectName("queueNavigationNote")
        self.navigation_note.setStyleSheet(f"color: {Color.STATUS_BUSY};")
        self.navigation_note.setVisible(False)
        layout.addWidget(self.navigation_note)
        return panel

    def _build_workspace(self) -> QWidget:
        """Build the right-hand review workspace."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(layout, spacing=4)

        layout.addWidget(self._build_toolbar())

        # Says in words, above the image, when the sheet on screen is a
        # rejected one - never left to a colour or to the queue beside it.
        self.lifecycle_banner = QLabel("")
        self.lifecycle_banner.setObjectName("lifecycleBanner")
        self.lifecycle_banner.setTextFormat(Qt.TextFormat.RichText)
        self.lifecycle_banner.setWordWrap(True)
        self.lifecycle_banner.setVisible(False)
        layout.addWidget(self.lifecycle_banner)

        self.workspace_splitter = QSplitter(Qt.Orientation.Vertical)
        self.workspace_splitter.setObjectName("resolveWorkspaceSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)
        views = self._build_views()
        set_floor(views, minimum_height=PREVIEW_MIN_HEIGHT)
        # The conflict decision panel, or - for a Rejected / Rescan case - the
        # case panel, in the same place and at the same size.
        self.decision_stack = QStackedWidget()
        self.decision_stack.setObjectName("resolveDecisionStack")
        self.decision_stack.addWidget(self._build_decision_panel())
        self.rescan_panel = RescanPanel()
        self.rescan_panel.use_replacement.connect(self.confirm_replacement_for_case)
        self.rescan_panel.undo_requested.connect(self.undo_reject_for_case)
        self.rescan_panel.unlink_requested.connect(self.remove_replacement_for_case)
        self.rescan_panel.import_requested.connect(self.prompt_import_rescan)
        self.rescan_panel.compare_toggled.connect(self.show_replacement_image)
        self.rescan_panel.history_requested.connect(self.show_case_history)
        self.rescan_panel.show_all_button.toggled.connect(self._on_show_all_scans)
        self.rescan_panel.search_changed.connect(self._on_association_search)
        self.decision_stack.addWidget(self.rescan_panel)
        self.sheet_panel = SheetPanel()
        self.sheet_panel.reject_requested.connect(self.prompt_reject_current_sheet)
        self.decision_stack.addWidget(self.sheet_panel)
        decisions = self.decision_stack
        set_floor(decisions, minimum_height=RESOLUTION_MIN_HEIGHT)
        self.workspace_splitter.addWidget(views)
        self.workspace_splitter.addWidget(decisions)
        self.workspace_splitter.setStretchFactor(0, PREVIEW_STRETCH)
        self.workspace_splitter.setStretchFactor(1, RESOLUTION_STRETCH)
        self.workspace_splitter.splitterMoved.connect(self._on_splitter_moved)
        layout.addWidget(self.workspace_splitter, stretch=1)
        return container

    def _on_splitter_moved(self, *_args: int) -> None:
        """Stop imposing the default proportions once the operator sets their own."""
        self._splitters_adjusted = True

    def _apply_split_ratios(self) -> None:
        """Divide the window in the proportions this stage is designed around.

        Applied on every resize **until the operator drags a splitter**, after
        which their sizes are theirs and a window resize must not take them
        back.

        Done here rather than left to ``setStretchFactor`` alone because a
        stretch factor governs how *extra* space is shared, not what the panes
        start at - and the starting point came from size hints, which handed
        the queue a third more width than the design calls for. Setting the
        sizes against a viewport that exists is the difference between a ratio
        that is documented and one that is true.
        """
        if self._splitters_adjusted:
            return
        for splitter, ratios in (
            (self.main_splitter, (QUEUE_STRETCH, WORKSPACE_STRETCH)),
            (self.workspace_splitter, (PREVIEW_STRETCH, RESOLUTION_STRETCH)),
        ):
            extent = (
                splitter.width()
                if splitter.orientation() is Qt.Orientation.Horizontal
                else splitter.height()
            )
            if extent <= 0:
                continue
            total = sum(ratios)
            splitter.setSizes([round(extent * share / total) for share in ratios])

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Keep the designed proportions as the window changes size."""
        super().resizeEvent(event)
        self._apply_split_ratios()

    def showEvent(self, event: QShowEvent) -> None:
        """Apply the proportions once the page has a real size."""
        super().showEvent(event)
        self._apply_split_ratios()

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
        scale_widget(toolbar, icon_size=18)
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
            "Back to the previous conflict nobody has decided in this view "
            "(Ctrl+Up or Shift+Enter)"
        )
        self.previous_open_action.triggered.connect(self.select_previous_unresolved)
        toolbar.addAction(self.previous_open_action)

        self.next_open_action = QAction(load_icon("list-checks"), "Next unresolved", self)
        self.next_open_action.setObjectName("nextUnresolvedConflictButton")
        self.next_open_action.setToolTip(
            "Skip to the next conflict nobody has decided in this view "
            "(Ctrl+Down or Ctrl+Enter)"
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
        self.zoom_out_action.setToolTip("Zoom the visible view out")
        self.zoom_out_action.triggered.connect(lambda: self._current_view().zoom_out())
        toolbar.addAction(self.zoom_out_action)

        self.zoom_in_action = QAction(load_icon("zoom-in"), "Zoom In", self)
        self.zoom_in_action.setObjectName("reviewZoomInButton")
        self.zoom_in_action.setToolTip("Zoom the visible view in")
        self.zoom_in_action.triggered.connect(lambda: self._current_view().zoom_in())
        toolbar.addAction(self.zoom_in_action)

        self.fit_action = QAction(load_icon("maximize"), "Fit", self)
        self.fit_action.setObjectName("reviewFitButton")
        self.fit_action.setToolTip("Fit the whole sheet in the visible view")
        self.fit_action.triggered.connect(lambda: self._current_view().fit_to_window())
        toolbar.addAction(self.fit_action)

        self.recentre_action = QAction(load_icon("scan"), "Re-centre", self)
        self.recentre_action.setObjectName("reviewRecentreButton")
        self.recentre_action.setToolTip("Return the zoomed view to the disputed field")
        self.recentre_action.setToolTip(
            "Frame the disputed position again, with its neighbouring columns"
        )
        self.recentre_action.triggered.connect(self._focus_zoom_on_conflict)
        toolbar.addAction(self.recentre_action)
        toolbar.addSeparator()

        self.history_button = QPushButton(load_icon("file-text"), "History...")
        self.history_button.setObjectName("conflictHistoryButton")
        self.history_button.setToolTip("Every decision recorded for this conflict")
        self.history_button.clicked.connect(self.show_history)
        toolbar.addWidget(self.history_button)

        # A sheet action, like the field editors: the scan on screen is
        # unusable, whichever of its conflicts is selected.
        self.reject_button = QPushButton(load_icon("triangle-alert"), "Reject / Rescan...")
        self.reject_button.setObjectName("rejectRescanButton")
        self.reject_button.setToolTip(
            "This scan is unusable - folded, clipped, skewed, the wrong page. "
            "Reject it pending a rescan (R). Nothing is deleted."
        )
        self.reject_button.clicked.connect(self.prompt_reject_current_sheet)
        toolbar.addWidget(self.reject_button)

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
        scale_layout(original_layout, spacing=2)
        self.original_view = ScanPreviewView()
        self.original_view.setObjectName("originalSheetView")
        original_layout.addWidget(self.original_view, stretch=1)
        self.original_note = QLabel("")
        self.original_note.setObjectName("originalSheetNote")
        self.original_note.setWordWrap(True)
        original_layout.addWidget(self.original_note)
        self.view_tabs.addTab(original_tab, "Original scan")

        self.view_tabs.currentChanged.connect(self._on_view_tab_changed)
        return self.view_tabs

    def _build_decision_panel(self) -> QWidget:
        """Build the evidence display and the action controls.

        Two logical sections on one surface rather than two boxed panels, and
        split **37/63** rather than evenly. What the machine saw is four short
        facts; what the reviewer does is eleven buttons, a reason, a note, a
        comparison strip and the commit. Giving them half the width each left
        the decision cramped beside a mostly empty box.
        """
        panel = QWidget()
        panel.setObjectName("conflictDecisionPanel")
        layout = QHBoxLayout(panel)
        scale_layout(
            layout,
            margins=(Spacing.SM, Spacing.XS, Spacing.SM, Spacing.XS),
            spacing=Spacing.MD,
        )

        layout.addWidget(self._build_machine_panel(), stretch=MACHINE_PANEL_STRETCH)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        layout.addWidget(divider)

        layout.addWidget(self._build_action_box(), stretch=DECISION_PANEL_STRETCH)
        return panel

    def _build_machine_panel(self) -> QWidget:
        """Build the compact statement of what recognition read.

        Four lines a reviewer reads at a glance, and everything else behind
        **Details**. The long paragraph explaining that per-bubble fill scores
        were not kept for this batch is true, is worth having, and is not what
        somebody deciding a digit needs in front of them on every sheet.
        """
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(layout, spacing=Spacing.XXS)

        heading = QLabel("MACHINE OBSERVATION")
        heading.setObjectName("resolveSectionHeading")
        layout.addWidget(heading)

        self.machine_summary_label = QLabel("Select a conflict from the queue.")
        self.machine_summary_label.setObjectName("machineSummaryLabel")
        self.machine_summary_label.setWordWrap(True)
        self.machine_summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.machine_summary_label.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.machine_summary_label)

        self.details_button = QToolButton()
        self.details_button.setObjectName("machineDetailsToggle")
        self.details_button.setText("Details")
        self.details_button.setCheckable(True)
        self.details_button.setArrowType(Qt.ArrowType.DownArrow)
        self.details_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.details_button.setToolTip(
            "Per-bubble fill scores, decision scores and scan-quality findings"
        )
        self.details_button.toggled.connect(self._on_details_toggled)
        layout.addWidget(self.details_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.evidence_label = QLabel("")
        self.evidence_label.setObjectName("machineEvidenceLabel")
        self.evidence_label.setWordWrap(True)
        self.evidence_label.setTextFormat(Qt.TextFormat.RichText)
        self.evidence_label.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Scrolled, because this panel's height is fixed by the splitter and
        # its content is not: a scan-quality finding lists every displaced
        # region, and a group with per-bubble evidence lists a fill score per
        # option. Without this the panel simply stopped mid-sentence - the
        # evidence a reviewer is being asked to decide from, silently cut off.
        self.evidence_scroll = QScrollArea()
        self.evidence_scroll.setObjectName("machineEvidenceScroll")
        self.evidence_scroll.setWidgetResizable(True)
        self.evidence_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.evidence_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.evidence_scroll.setWidget(self.evidence_label)
        self.evidence_scroll.setVisible(False)
        layout.addWidget(self.evidence_scroll, stretch=1)
        layout.addStretch(0)
        return panel

    def _on_details_toggled(self, shown: bool) -> None:
        """Show or hide the full machine evidence."""
        self.evidence_scroll.setVisible(shown)
        self.details_button.setArrowType(
            Qt.ArrowType.UpArrow if shown else Qt.ArrowType.DownArrow
        )

    def _build_action_box(self) -> QWidget:
        """Build the resolution controls - the thing the stage exists for."""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(layout, spacing=Spacing.XS)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self.choice_heading = QLabel("YOUR DECISION")
        self.choice_heading.setObjectName("resolveSectionHeading")
        self.choice_heading.setWordWrap(False)
        header_layout.addWidget(self.choice_heading, stretch=1)
        # The operator's name is audit metadata, not a decision control. It has
        # to be visible - a reviewer must never record a decision believing
        # somebody else's name is on it - but it does not need a full row of
        # the panel, which is what it had.
        self.reviewer_label = QLabel("")
        self.reviewer_label.setObjectName("resolveOperatorBadge")
        self.reviewer_label.setTextFormat(Qt.TextFormat.RichText)
        header_layout.addWidget(self.reviewer_label, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(header)

        self.choice_row = QWidget()
        self.choice_layout = QHBoxLayout(self.choice_row)
        self.choice_layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(self.choice_layout, spacing=Spacing.XS)
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

        layout.addWidget(self._build_field_editor())
        layout.addWidget(self._build_reason_row())
        # Slack goes here, between the controls and the commit, so a taller
        # panel gives the reviewer room rather than stretching the buttons.
        layout.addStretch(1)
        layout.addWidget(self._build_provenance_strip())
        layout.addWidget(self._build_button_row())
        return box

    def _build_field_editor(self) -> QWidget:
        """Build the "correct the whole field in one go" control.

        A student who leaves four digits of their roll number blank produces
        four separate position conflicts, and the operator knows the whole
        number - it is written on the script, or the candidate is standing
        there. Making them visit four positions to type four digits, each with
        its own reason, is the workflow this removes.

        An **inline expander**, not a dialog: the sheet stays visible while the
        value is being typed, which is the whole point - the operator is
        reading the number off the paper in the pane above. It occupies one
        line when shut.

        **Sheet actions, not conflict actions.** One small button per identity
        field the sheet carries - Student ID, Set Code - offered whatever
        record is selected, because a field belongs to the sheet: a roll
        number whose conflicts are all settled, or one read confidently with no
        conflict at all, is still one an operator may need to correct.
        """
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(layout, spacing=Spacing.XXS)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        scale_layout(actions, spacing=Spacing.SM)
        self.field_edit_buttons: dict[FieldKind, QToolButton] = {}
        for kind, name in (
            (FieldKind.IDENTIFIER, "editFullFieldButton"),
            (FieldKind.SET_CODE, "editFullSetCodeButton"),
        ):
            button = QToolButton()
            button.setObjectName(name)
            button.setCheckable(True)
            button.setArrowType(Qt.ArrowType.RightArrow)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setText("Edit field...")
            button.toggled.connect(partial(self._on_field_editor_toggled, kind))
            button.setVisible(False)
            actions.addWidget(button)
            self.field_edit_buttons[kind] = button
        actions.addStretch(1)
        layout.addLayout(actions)

        self.field_edit_row = QWidget()
        row = QHBoxLayout(self.field_edit_row)
        row.setContentsMargins(0, 0, 0, 0)
        scale_layout(row, spacing=Spacing.SM)

        self.field_edit_label = QLabel("Correct value")
        row.addWidget(self.field_edit_label)

        self.field_edit_input = QLineEdit()
        self.field_edit_input.setObjectName("fullFieldValueEdit")
        self.field_edit_input.textChanged.connect(self._on_field_edit_typed)
        self.field_edit_input.returnPressed.connect(self.apply_field_edit)
        row.addWidget(self.field_edit_input, stretch=1)

        self.field_edit_status = QLabel("")
        self.field_edit_status.setObjectName("fullFieldStatusLabel")
        self.field_edit_status.setTextFormat(Qt.TextFormat.RichText)
        # Takes its stretch share and no more, so a long preview never squeezes
        # the box being typed into; the full text is also its tooltip.
        self.field_edit_status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        row.addWidget(self.field_edit_status, stretch=4)

        self.field_edit_apply = QPushButton(FIELD_EDIT_APPLY_TEXT)
        self.field_edit_apply.setObjectName("applyFullFieldButton")
        self.field_edit_apply.setProperty(VARIANT_PROPERTY, VARIANT_PRIMARY)
        self.field_edit_apply.clicked.connect(self.apply_field_edit)
        row.addWidget(self.field_edit_apply)

        self.field_edit_cancel = QPushButton("Cancel")
        self.field_edit_cancel.setObjectName("cancelFullFieldButton")
        self.field_edit_cancel.clicked.connect(self.close_field_editor)
        row.addWidget(self.field_edit_cancel)

        layout.addWidget(self.field_edit_row)
        self.field_edit_row.setVisible(False)
        return holder

    def _build_reason_row(self) -> QWidget:
        """Build the reason and note controls, on one row.

        Both are secondary - they qualify a decision rather than make one - so
        they share a row instead of taking one each. The validation is
        untouched: 'Other' still refuses to commit without an explanation.
        """
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(layout, spacing=Spacing.SM)

        layout.addWidget(QLabel("Reason"))
        self.reason_combo = QComboBox()
        self.reason_combo.setObjectName("correctionReasonCombo")
        for code in ReasonCode:
            if code is ReasonCode.MACHINE_CONFIRMED:
                continue  # Recorded automatically when accepting; never chosen.
            self.reason_combo.addItem(code.label, userData=code.value)
        layout.addWidget(self.reason_combo, stretch=3)

        layout.addWidget(QLabel("Note"))
        self.reason_text = QTextEdit()
        self.reason_text.setObjectName("correctionReasonText")
        self.reason_text.setPlaceholderText("Optional; required for 'Other'")
        # One line high. A note is a sentence, not a paragraph, and the three
        # rows this control used to take were three rows the preview did not
        # get. It is still a QTextEdit, so nothing that reads or writes it
        # changes.
        scale_widget(self.reason_text, fixed_height=NOTE_HEIGHT_PX)
        self.reason_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.reason_text.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        layout.addWidget(self.reason_text, stretch=4)
        return row

    def _build_provenance_strip(self) -> QWidget:
        """Build the machine / manual / effective comparison strip.

        Three columns, always all three, because the question a reviewer is
        answering - *is the value this script will carry the machine's or
        mine?* - has to be answerable by looking at one place. The effective
        column says **Pending** while a choice has been made and not committed,
        so an uncommitted selection is never displayed as a stored result.
        """
        strip = QFrame()
        strip.setObjectName("conflictProvenanceStrip")
        layout = QHBoxLayout(strip)
        scale_layout(
            layout,
            margins=(Spacing.SM, Spacing.XXS, Spacing.SM, Spacing.XXS),
            spacing=Spacing.MD,
        )

        self.summary_machine = QLabel("")
        self.summary_machine.setObjectName("provenanceMachineValue")
        self.summary_manual = QLabel("")
        self.summary_manual.setObjectName("provenanceManualValue")
        self.summary_effective = QLabel("")
        self.summary_effective.setObjectName("provenanceEffectiveValue")
        for label in (self.summary_machine, self.summary_manual, self.summary_effective):
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setWordWrap(False)
            layout.addWidget(label, stretch=1)
        return strip

    def _build_button_row(self) -> QWidget:
        """Build the primary and secondary actions.

        State-aware, and only ever showing what can be done now. An unresolved
        conflict offers a commit and a deferral; a decided one offers to reopen
        it. A permanently disabled third button taught an operator to ignore a
        third of the action area.
        """
        buttons = QWidget()
        layout = QHBoxLayout(buttons)
        layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(layout, spacing=Spacing.SM)

        self.confirm_button = QPushButton(load_icon("circle-check"), "Confirm resolution")
        self.confirm_button.setObjectName("confirmResolutionButton")
        self.confirm_button.setProperty(VARIANT_PROPERTY, VARIANT_PRIMARY)
        scale_widget(self.confirm_button, minimum_height=ACTION_BUTTON_HEIGHT_PX)
        self.confirm_button.setDefault(True)
        self.confirm_button.clicked.connect(self.confirm_resolution)
        layout.addWidget(self.confirm_button, stretch=3)

        self.defer_button = QPushButton("Defer")
        self.defer_button.setObjectName("deferConflictButton")
        self.defer_button.setToolTip("Postpone this decision (D)")
        scale_widget(self.defer_button, minimum_height=ACTION_BUTTON_HEIGHT_PX)
        self.defer_button.clicked.connect(self.defer_conflict)
        layout.addWidget(self.defer_button, stretch=1)

        self.reopen_button = QPushButton(load_icon("rotate-ccw"), "Reopen")
        self.reopen_button.setObjectName("reopenConflictButton")
        scale_widget(self.reopen_button, minimum_height=ACTION_BUTTON_HEIGHT_PX)
        self.reopen_button.setToolTip(
            "Discard every decision on this conflict and put it back in the queue. "
            "Nothing is erased - the earlier corrections stay in the history. "
            "To step back one decision instead, use Undo (Ctrl+Z)."
        )
        self.reopen_button.clicked.connect(self.reopen_conflict)
        layout.addWidget(self.reopen_button, stretch=1)
        return buttons

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
            # Enter confirms whatever the comparison strip says the effective
            # value would be - a picked value, or the machine's reading when
            # that is one this field could carry. One key for one idea.
            (QKeySequence(Qt.Key.Key_Return), self.confirm_resolution),
            (QKeySequence(Qt.Key.Key_Enter), self.confirm_resolution),
            (QKeySequence(Qt.Key.Key_D), self.defer_conflict),
            (QKeySequence(Qt.Key.Key_E), self.open_field_editor),
            (QKeySequence(REJECT_KEY), self.prompt_reject_current_sheet),
            (QKeySequence(Qt.Key.Key_Escape), self.close_field_editor),
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
            # Ctrl+Up / Ctrl+Down: the same two commands, on the keys a queue
            # of thousands is walked with. Nothing else in the application
            # binds them; the table's own Ctrl+arrow (move without selecting)
            # is meaningless in a single-selection queue.
            (
                QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Down),
                self.select_next_unresolved,
            ),
            (
                QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_Up),
                self.select_previous_unresolved,
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
        self.state.rescan_cases = []
        self.state.rescan_candidates = {}
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
        self._fill_batch_filter()
        self.state.template = template
        self.state.bundle = None
        self.state.sheet_conflicts = []
        self.state.sheet_provenance = {}
        self.state.redo = []
        self._loaded_scan_id = None
        population = session_population.population(database, batch_id)
        if len(population.batch_ids) > 1:
            # A session of several batches is reviewed as one population.
            self.batch_label.setText(
                f"<b>Scan session</b><br>{session_population.describe(population)}"
            )
        else:
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
        batch_value = self.batch_filter.currentData()
        return ConflictFilter(
            states=states,
            conflict_types=types,
            search=self.search_box.text().strip(),
            include_withdrawn=self.state_filter.currentText() == FILTER_WITHDRAWN,
            batch_ids=(str(batch_value),) if batch_value else (),
        )

    def _fill_batch_filter(self) -> None:
        """List the session's batches in the diagnostic batch filter."""
        database = self.database
        self.batch_filter.blockSignals(True)
        self.batch_filter.clear()
        self.batch_filter.addItem(ALL_BATCHES, userData="")
        if database is not None and self.state.batch_id is not None:
            population = session_population.population(database, self.state.batch_id)
            for position, batch_id in enumerate(population.batch_ids, start=1):
                state = "" if batch_id in population.live_batch_ids else " · superseded"
                self.batch_filter.addItem(
                    f"Batch {position} · {batch_id[:8]}{state}", userData=batch_id
                )
        self.batch_filter.setEnabled(self.batch_filter.count() > 2)
        self.batch_filter.blockSignals(False)

    @property
    def rescan_mode(self) -> bool:
        """Whether the queue is showing *Rejected / Rescan* cases."""
        return self.state_filter.currentText() == FILTER_RESCAN

    @property
    def sheets_mode(self) -> bool:
        """Whether the queue is showing *All processed sheets*."""
        return self.state_filter.currentText() == FILTER_SHEETS

    def refresh_queue(self) -> None:
        """Re-read the queue from the database and rebuild the table."""
        database = self.database
        if database is None or self.state.batch_id is None:
            self.queue_table.setRowCount(0)
            self.state.conflicts = []
            self.state.rescan_cases = []
            self.state.sheet_rows = []
            self._refresh_summary()
            self._refresh_controls()
            return
        if self.rescan_mode:
            self._refresh_rescan_queue()
            return
        if self.sheets_mode:
            self._refresh_sheet_queue()
            return
        if self.decision_stack.currentIndex() != 0:
            # Back from the Rejected / Rescan or sheet view: the conflict
            # workspace returns, and the banner about a rejected sheet goes
            # with it.
            self.decision_stack.setCurrentIndex(0)
            self.lifecycle_banner.setVisible(False)
            self.state.rescan_cases = []
            self.state.sheet_rows = []
            self._loaded_scan_id = None
            self.state.bundle = None

        selected = self.current_conflict()
        scrollbar = self.queue_table.verticalScrollBar()
        scrolled_to = scrollbar.value()
        self.state.conflicts = list(
            list_conflicts(
                database,
                self.state.batch_id,
                filters=self._current_filter(),
                limit=QUEUE_PAGE_SIZE,
                session_wide=True,
            )
        )
        self._rebuild_queue_table()
        self._refresh_summary()
        self._restore_selection(selected, scrolled_to)
        self._refresh_controls()

    def _restore_selection(
        self, selected: ConflictRecord | None, scrolled_to: int
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

        **Nothing is restored by row number.** Rows disappear as they are
        decided, so an index means a different conflict after every rebuild -
        which is how the queue came to jump. What is restored is, in order: the
        conflict that was selected, the sheet it was on, and finally the
        scrollbar, so a reviewer working halfway down a long queue stays there
        instead of being returned to the top.
        """
        if not self.state.conflicts:
            self.queue_table.clearSelection()
            self._clear_views()
            self.conflict_selected.emit(-1)
            return

        if selected is not None and self.select_conflict_by_id(selected.conflict_id):
            return

        # The conflict that was being reviewed has left this view - almost
        # always because it was just decided. Stay on its sheet if it still has
        # work; that is where the reviewer is looking and what they are holding.
        if selected is not None and self.select_next_on_sheet(selected.scan_id):
            return

        row = self._row_near(selected, scrolled_to)
        self.queue_table.selectRow(row)
        self.queue_table.verticalScrollBar().setValue(
            min(scrolled_to, self.queue_table.verticalScrollBar().maximum())
        )
        # `selectRow` emits nothing when the index is unchanged, and the index
        # very often *is* unchanged here even though the conflict at it is not.
        if self.queue_table.currentRow() == row:
            self._on_queue_selection_changed()

    def _row_near(self, selected: ConflictRecord | None, scrolled_to: int) -> int:
        """The row to fall back to when the selected conflict has gone.

        The first row of the sheet that was being worked on, when that sheet is
        still in the queue - the reviewer's place in a ten-thousand-row batch
        is which *sheet* they had reached, not which row number. Otherwise the
        first row currently on screen, which at least leaves the scrollbar
        where they put it.
        """
        if selected is not None:
            same_sheet = next(
                (
                    index
                    for index, item in enumerate(self.state.conflicts)
                    if item.scan_id == selected.scan_id
                ),
                None,
            )
            if same_sheet is not None:
                return same_sheet
        visible = self.queue_table.rowAt(0)
        if 0 <= visible < len(self.state.conflicts):
            return visible
        if scrolled_to and self.state.conflicts:
            return min(scrolled_to, len(self.state.conflicts) - 1)
        return 0

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
                # One sheet's conflicts are contiguous (see `list_conflicts`),
                # so the file name is written once per sheet and the rows
                # beneath it are indented instead. Eight identical file names
                # in a column read as eight unrelated problems; one name with
                # seven continuations reads as one script with eight.
                previous = self.state.conflicts[row - 1] if row else None
                starts_sheet = previous is None or previous.scan_id != conflict.scan_id
                sheet_text = (
                    conflict.scan_name
                    if starts_sheet
                    else f"{SHEET_CONTINUATION} {conflict.identifier_value}".rstrip()
                )
                values = (
                    sheet_text,
                    conflict.identifier_value,
                    _issue_text(conflict),
                    f"{conflict.state_marker} {conflict.state_label}",
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setBackground(_STATE_COLORS[conflict.state])
                    # Every cell can elide, so every cell carries its full
                    # value where the operator can reach it.
                    item.setToolTip(
                        _state_tooltip(conflict)
                        if column == 3
                        else _sheet_tooltip(conflict, self.state.conflicts)
                        if column == 0
                        else text
                    )
                    if starts_sheet and row:
                        # A hairline above the first row of each sheet, so the
                        # groups are visible without a heavier treatment.
                        font = item.font()
                        font.setOverline(True)
                        item.setFont(font)
                    self.queue_table.setItem(row, column, item)
        finally:
            self.queue_table.setUpdatesEnabled(True)
            self._suppress_selection = False

    def _refresh_summary(self) -> None:
        """Update the batch-level counts under the queue."""
        database = self.database
        if database is None or self.state.batch_id is None:
            self.summary_label.setText("")
            self.summary_breakdown.setText("")
            self._batch_unresolved = 0
            return
        # The whole scan session (0.1.1 phase 4): the queue Resolve shows is
        # the session's, whichever batch each sheet was read in.
        counts = count_conflicts(database, self.state.batch_id, session_wide=True)
        rescans = scan_lifecycle.count_cases(database, self.state.batch_id, session_wide=True)
        self._batch_unresolved = counts.unresolved
        # Outstanding rescans are physical work, not decisions, so they are
        # counted beside the conflicts rather than in them - and said in
        # words, so the count is there whichever view the queue is showing.
        rescan_text = (
            f" &nbsp; <b>{rescans.outstanding}</b> rescan required"
            if rescans.outstanding or rescans.superseded
            else ""
        )
        self.summary_label.setText(
            f"<b>{counts.total}</b> total &nbsp; "
            f"<b>{counts.unresolved}</b> unresolved &nbsp; "
            f"<b>{counts.resolved}</b> resolved{rescan_text}"
        )
        self.summary_label.setToolTip(
            f"{counts.open_count} never looked at, {counts.deferred} deferred, "
            f"{counts.resolved} decided, {counts.withdrawn} withdrawn by a re-read."
            + (
                f" {rescans.outstanding} rejected sheet(s) awaiting a rescan, "
                f"{rescans.superseded} replaced by a confirmed rescan - see "
                "Rejected / Rescan."
                if rescans.total
                else ""
            )
        )
        biggest = sorted(counts.by_type.items(), key=lambda item: -item[1])[:3]
        breakdown = " &middot; ".join(
            f"{ConflictType(kind).label}: {count}" for kind, count in biggest
        )
        self.summary_breakdown.setText(
            f"<span style='color:{Color.TEXT_TERTIARY};font-size:9pt;'>"
            f"{breakdown}</span>"
            if breakdown
            else ""
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
        if row + 1 < self._row_count():
            self.queue_table.selectRow(row + 1)

    def _row_count(self) -> int:
        """How many rows the queue holds, in whichever view it is showing."""
        if self.rescan_mode:
            return len(self.state.rescan_cases)
        if self.sheets_mode:
            return len(self.state.sheet_rows)
        return len(self.state.conflicts)

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

        **Within the current view.** The queue already holds only what the
        filters show, so a reviewer working one conflict type is never taken
        into another.

        Deferred conflicts are passed over as well as decided ones: deferring
        is a reviewer saying "not this one, not now", and walking them back
        round on the next pass would make the button useless on a queue where
        somebody has postponed a few.

        At the end of the view it **wraps** - this stage's convention, so a
        pass ends by returning to whatever was skipped - and says so under
        the queue rather than doing it silently. With nothing undecided in
        view the selection stays put and that is said too.
        """
        if self._editing_text():
            return False
        if self.sheets_mode:
            # Sheets are not problems: this view has nothing "unresolved" to
            # walk. The arrow keys move through it as through any list.
            self._note_navigation(
                "This view lists every read sheet; nothing in it is unresolved. "
                "Use the arrow keys, or a conflict view to walk what is left."
            )
            return False
        rescan = self.rescan_mode
        total = self._row_count()
        # In the Rejected / Rescan view "unresolved" means a sheet still
        # awaiting its rescan; a completed case is passed over like a decided
        # conflict. Same keys, same wrap, same notes - one navigation system.
        nothing = (
            "No rejected sheet is still awaiting a rescan in this view."
            if rescan
            else "Nothing is left undecided in this view."
        )
        if not total:
            self._note_navigation(nothing)
            return False
        start = self.queue_table.currentRow()
        for offset in range(1, total + 1):
            row = (start + step * offset) % total
            if (
                self.state.rescan_cases[row].is_outstanding
                if rescan
                else self.state.conflicts[row].state is ConflictState.OPEN
            ):
                wrapped = (row <= start) if step > 0 else (row >= start)
                self.queue_table.selectRow(row)
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
        self._note_navigation(nothing)
        return False

    def _note_navigation(self, text: str) -> None:
        """Say, under the queue, what the last unresolved-navigation did."""
        self.navigation_note.setText(text)
        self.navigation_note.setVisible(bool(text))

    def _on_queue_selection_changed(self) -> None:
        """React to the queue selection moving."""
        if self._suppress_selection:
            return
        if self.rescan_mode:
            self._show_case(self.current_case())
            return
        if self.sheets_mode:
            self._show_sheet(self.current_sheet())
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
        # A choice belongs to the position it was made on. Carrying one across
        # a selection change would leave a ring on a bubble of a sheet the
        # reviewer has left, and arm the confirm button with it.
        self.clear_pending()
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
            # Everything that needs the sheet's own geometry is re-read here,
            # because it was computed before the sheet existed. The whole-field
            # editor is the case that made this visible: it may only be offered
            # once the page has rectified, so a panel built while the loader
            # was still running hid it and never brought it back.
            self._refresh_evidence(conflict)
            self._refresh_controls()
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
                    # See NEUTRAL_OUTLINE: on this stage amber means "the
                    # machine read this mark" and red means "a person decided
                    # this", and a zone outlined red because its status is
                    # `multiple` would be a third meaning for the same colour
                    # inside the same rectangle.
                    status_colors=False,
                )
            self.normalised_view.fit_to_window()
            self._focus_zoom_on_conflict()
        else:
            # There is no rectified page, which for a registration failure is
            # not a fault but the finding itself. Say so in the view: an
            # unexplained grey rectangle is the one thing this stage must never
            # hand a reviewer, and the original scan - which does exist - is a
            # tab away.
            message = _no_rectified_page_message(bundle)
            for view in (self.normalised_view, self.zoom_view):
                view.clear()
                view.set_placeholder(message)
        self._offer_the_useful_tab(registered=result is not None and result.preview is not None)

        if bundle.original is not None:
            self.original_view.set_placeholder("")
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
            reason = bundle.error or "The original scan could not be decoded."
            self.original_view.set_placeholder(reason)
            self.original_note.setText(reason)

        if bundle.error:
            self.evidence_label.setText(
                f"{self.evidence_label.text()}<br><br><b>This sheet could not be "
                f"read for review:</b> {bundle.error}"
            )

    def _offer_the_useful_tab(self, *, registered: bool) -> None:
        """Show the tab that has something on it, and disable the ones that do not.

        A sheet that would not align has no rectified page and no field to zoom
        into - those two tabs are empty *by definition of the conflict* - while
        the original scan is exactly what the reviewer has to look at to decide
        whether to re-scan it. Leaving all three enabled invites them to click
        through two blank panes to reach the only one that helps.

        **And it switches back.** Forcing the tab without restoring it means
        one registration failure in a queue silently moves every later sheet to
        the original scan, so the zoomed field - the view the stage is built
        around - is never seen again. What is restored is the reviewer's own
        last deliberate choice, tracked in :attr:`_preferred_tab`, so somebody
        who prefers the whole normalised sheet keeps it.
        """
        # The whole method is guarded, not just the explicit switch: disabling
        # the tab that is currently shown makes Qt move to another one and emit
        # `currentChanged` itself, and that move is this page's doing rather
        # than the reviewer's. Guarding only `setCurrentIndex` let Qt's own
        # switch be recorded as a preference, which is how one registration
        # failure came to pin every later sheet to the original scan.
        self._suppress_tab_memory = True
        try:
            for index in (ZOOM_TAB_INDEX, NORMALISED_TAB_INDEX):
                self.view_tabs.setTabEnabled(index, registered)
                self.view_tabs.setTabToolTip(
                    index,
                    ""
                    if registered
                    else "This sheet could not be aligned, so there is no "
                    "rectified page.",
                )
            wanted = self._preferred_tab if registered else ORIGINAL_TAB_INDEX
            if self.view_tabs.currentIndex() != wanted:
                self.view_tabs.setCurrentIndex(wanted)
        finally:
            self._suppress_tab_memory = False

    def _on_view_tab_changed(self, index: int) -> None:
        """Remember a tab the *reviewer* chose, ignoring one this page forced."""
        if not self._suppress_tab_memory and self.view_tabs.isTabEnabled(index):
            self._preferred_tab = index

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

    def _sheet_batch(self, scan_id: int) -> str:
        """The batch one sheet was read into; the stage's batch if unknown."""
        database = self.database
        found = scan_lifecycle.batch_of(database, scan_id) if database is not None else None
        return found or self.state.batch_id or ""

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
        # A sheet's records live in the batch it was read into, which in a
        # session of several batches need not be the one the stage holds.
        sheet_batch = self._sheet_batch(scan_id)
        self.state.sheet_conflicts = list(
            list_conflicts(
                database,
                sheet_batch,
                filters=ConflictFilter(scan_id=scan_id, include_withdrawn=True),
            )
        )
        self.state.sheet_provenance = provenance_for_scan(database, sheet_batch, scan_id)

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
            pending=self.state.pending,
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
        """Frame the disputed group, with its neighbours, in the zoomed view.

        Two changes from the arithmetic this replaces, both of which the
        operator sees. The region now includes the neighbouring printed
        positions (:func:`~omr_scanner.gui.review.lanes.context_bubbles`),
        because a faint mark is judged against the columns beside it, filled by
        the same candidate in the same pencil - framing the disputed column
        alone removes the only comparison available. And the framing is handed
        to the view as a *rectangle to keep in frame* rather than as a
        magnification computed once: the old version ran before the tab had
        been shown, so it sized the image against a viewport that did not exist
        yet and left a small page pinned to the corner of a large empty canvas.
        """
        conflict = self.current_conflict()
        bundle = self.state.bundle
        if (
            conflict is None
            or bundle is None
            or bundle.result is None
            or self.state.template is None
        ):
            self.zoom_view.fit_to_window()
            return
        bubbles = context_bubbles(bundle.result, self.state.template, conflict)
        if not bubbles:
            self.zoom_view.fit_to_window()
            return
        left, top, right, bottom = bounds_of(bubbles)
        self.zoom_view.focus_on(QRectF(left, top, right - left, bottom - top))

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
        self.machine_summary_label.setText("Select a conflict from the queue.")
        self.evidence_label.setText("")
        self.choice_heading.setText("YOUR DECISION")
        self._set_provenance_strip("", "", "")
        self.sheet_progress_label.setText("")
        self.clear_pending()
        self._clear_choices()

    def _current_view(self) -> ScanPreviewView:
        """The image view the visible tab is showing."""
        index = self.view_tabs.currentIndex()
        if index == NORMALISED_TAB_INDEX:
            return self.normalised_view
        if index == ORIGINAL_TAB_INDEX:
            return self.original_view
        return self.zoom_view

    # ------------------------------------------------------------------
    # Evidence, choices, provenance
    # ------------------------------------------------------------------
    def _refresh_evidence(self, conflict: ConflictRecord) -> None:
        """Render what the machine saw, in summary and in full."""
        bundle = self.state.bundle
        result = bundle.result if bundle is not None else None
        assessment = result.scan_quality if result is not None else None
        shape = self._field_shape_for(conflict)
        current = join_field_value(self.current_field_values(shape)) if shape is not None else ""
        self.machine_summary_label.setText(
            _machine_summary_html(conflict, self._machine_marks(conflict), current)
        )
        evidence = _evidence_html(conflict, assessment)
        if conflict.field.kind is FieldKind.SET_CODE:
            evidence += self._set_reading_html(current or conflict.observation.value)
        self.evidence_label.setText(evidence)

    def describe_set_reading(self, paper_value: str) -> str:
        """``"Set 10 (A on sheet)"`` for a set code read or corrected on the paper.

        The logical set the paper's mark names, through
        :mod:`omr_scanner.services.set_identity`; ``""`` when no project is
        open, the value is empty, or it names no defined set.
        """
        database = self.database
        if database is None or not paper_value:
            return ""
        identity = set_identity.load(database)
        found = identity.for_reading(paper_value)
        if found is None:
            return ""
        return identity.describe(found.code, as_read=paper_value)

    def _set_reading_html(self, paper_value: str) -> str:
        """One evidence line naming the logical set a set-code reading means."""
        label = self.describe_set_reading(paper_value)
        if not label:
            return ""
        return f"Reads as: <b>{html.escape(label)}</b><br>"

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
        counts = count_conflicts_for_scan(
            database, self._sheet_batch(conflict.scan_id), conflict.scan_id
        )
        # Counted over **the sheet**, not over the filtered queue. A
        # denominator taken from the queue shrinks as the reviewer works -
        # "1 of 1" on the last conflict of eight - which reads as though the
        # sheet had one problem rather than as progress through eight.
        on_sheet = [
            item
            for item in self.state.sheet_conflicts
            if item.scan_id == conflict.scan_id
            and item.state is not ConflictState.WITHDRAWN
        ]
        position = next(
            (
                index + 1
                for index, item in enumerate(on_sheet)
                if item.conflict_id == conflict.conflict_id
            ),
            0,
        )
        # Every number says what it counts. The header used to read
        # "1 of 1 - 1 unresolved" beside a summary reading "157 unresolved",
        # which are both true of different things and read as a contradiction.
        self.sheet_progress_label.setText(
            f"Conflict <b>{position}</b> of {len(on_sheet)} on this sheet "
            f"&middot; <b>{counts.unresolved}</b> left here "
            f"&middot; <b>{self._batch_unresolved}</b> left in batch"
        )
        self.sheet_progress_label.setToolTip(
            f"Conflict {position} of {len(on_sheet)} on this sheet. "
            f"{counts.open_count} open, {counts.deferred} deferred, "
            f"{counts.resolved} resolved, {counts.withdrawn} withdrawn here. "
            f"The batch has {self._batch_unresolved} unresolved altogether."
        )

    def _clear_choices(self) -> None:
        """Empty the choice row completely, whatever is in it.

        **Every item, not just the buttons.** The row also holds the "this is
        not a value that can be corrected" note and a trailing stretch, and an
        earlier version of this method removed neither - so each visit to a
        sheet-scope conflict appended another copy of the note, and each visit
        to any conflict appended another spacer. After a reviewer had walked
        thirty registration failures the decision panel was thirty slivers of
        wrapped text reading "This is not a", and the value buttons of the next
        conflict were squeezed to nothing by the accumulated stretches.

        Draining the layout is what makes that impossible to reintroduce: there
        is no list of widget kinds to remember to extend.
        """
        while (item := self.choice_layout.takeAt(0)) is not None:
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
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
        # Names the position being edited, so a reviewer choosing a digit does
        # not have to look back at the machine panel to remember which of six
        # roll-number columns they are deciding.
        self.choice_heading.setText(
            f"CHOOSE VALUE FOR {conflict.field.describe().upper()}"
            if conflict.allows_value_correction
            else "YOUR DECISION"
        )

        if not conflict.allows_value_correction:
            note = QLabel(
                "This is not a value that can be corrected. Acknowledge it or "
                "defer it."
            )
            note.setObjectName("choiceUnavailableNote")
            note.setWordWrap(True)
            self.choice_layout.addWidget(note, stretch=1)
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
            button = QPushButton(BLANK_BUTTON_TEXT if label == BLANK_CHOICE else label)
            button.setObjectName(f"choiceButton_{label}")
            # The key is named on the button that does the same thing, because
            # the two are one command - `choose_label` - and a reviewer who
            # never reads the documentation still learns the shortcut.
            key = "B" if label == BLANK_CHOICE else label
            button.setToolTip(
                f"Choose '{label}' for this position"
                + (f" (press {key})" if len(key) == 1 else "")
            )
            button.setAccessibleName(f"Choose {label}")
            scale_widget(
                button, fixed_height=CHOICE_BUTTON_HEIGHT, minimum_width=CHOICE_BUTTON_MIN_WIDTH
            )
            button.setSizePolicy(
                QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
            )
            button.clicked.connect(
                lambda _checked=False, item=label: self.choose_label(item)
            )
            # "Blank" is a word among symbols, so it is allowed the width one
            # needs; the rest stay a uniform numeric row.
            self.choice_layout.addWidget(button, stretch=2 if label == BLANK_CHOICE else 1)
            self._choice_buttons.append(button)
        self._refresh_choice_states()

    def _refresh_choice_states(self) -> None:
        """Repaint the value buttons in the three states they can be in.

        ============================ =====================================
        state                        what it means
        ============================ =====================================
        plain                        a symbol nobody has said anything about
        amber, outlined              the engine read *this* mark on the paper
        accent, filled               the reviewer has picked it
        ============================ =====================================

        Without the middle one, a position the machine read as ``0-5`` showed
        eleven identical buttons and made the reviewer carry "it was 0 and 5"
        in their head from the panel beside it. The state is set as a dynamic
        property and the appearance comes from the stage's stylesheet, so
        eleven buttons rebuilt on every selection do not each carry a palette.
        """
        conflict = self.current_conflict()
        machine = self._machine_marks(conflict) if conflict is not None else frozenset()
        pending = self.pending
        for button in self._choice_buttons:
            label = button.objectName().removeprefix("choiceButton_")
            value = "" if label == BLANK_CHOICE else label
            if pending is not None and value == pending:
                state = CANDIDATE_CHOSEN
            elif label != BLANK_CHOICE and label in machine:
                state = CANDIDATE_MACHINE
            else:
                state = ""
            if button.property(CANDIDATE_STATE_PROPERTY) == state:
                continue
            button.setProperty(CANDIDATE_STATE_PROPERTY, state)
            # Qt does not re-evaluate a property selector on its own.
            button.style().unpolish(button)
            button.style().polish(button)

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
            self._set_provenance_strip("", "", "")
            return
        found = provenance_for(database, conflict.conflict_id)
        machine = found.machine_value or "(blank)"
        picked = self.pending
        if picked is not None:
            manual = picked or "(blank)"
            # "Pending", not the value: nothing has been written, and a strip
            # that showed the chosen value as the effective result would be
            # claiming a decision the ledger does not carry.
            effective = "Pending"
            self.summary_effective.setToolTip(
                "Chosen but not recorded. Confirm to make this the effective value."
            )
        elif found.source is ValueSource.HUMAN:
            manual = found.value or "(blank)"
            effective = found.value or "(blank)"
            self.summary_effective.setToolTip(
                f"Recorded by {found.reviewer} at {found.decided_at}."
            )
        else:
            manual = "—"
            effective = found.value or "(blank)"
            self.summary_effective.setToolTip("The machine's reading; not yet reviewed.")
        self._set_provenance_strip(machine, manual, effective)

    def _set_provenance_strip(self, machine: str, manual: str, effective: str) -> None:
        """Write the three columns of the comparison strip."""
        for label, caption, value in (
            (self.summary_machine, "MACHINE", machine),
            (self.summary_manual, "MANUAL", manual),
            (self.summary_effective, "EFFECTIVE", effective),
        ):
            label.setText(
                f"<span style='color:{Color.TEXT_TERTIARY};font-size:9pt;'>"
                f"{caption}</span><br><b>{value or '&mdash;'}</b>"
            )

    def provenance_summary(self) -> tuple[str, str, str]:
        """The strip's three values as plain text, for a test or a reader.

        The strip is rich text in three widgets; this is the one place that
        says what it currently claims, so an assertion does not have to parse
        markup to find out.
        """
        return tuple(  # type: ignore[return-value]
            label.text().split("<br>")[-1].removeprefix("<b>").removesuffix("</b>")
            for label in (
                self.summary_machine,
                self.summary_manual,
                self.summary_effective,
            )
        )

    def _refresh_reviewer_label(self) -> None:
        """Show who decisions will be recorded against.

        A badge rather than a sentence with a row to itself. It still has to be
        visible - nobody may record a decision believing somebody else's name
        is on it - but it is audit metadata, not a control, and it was taking
        prime space in the panel the reviewer works in.
        """
        if self.state.reviewer:
            self.reviewer_label.setText(f"Operator: <b>{self.state.reviewer}</b>")
            self.reviewer_label.setToolTip(
                f"Every decision here is recorded against {self.state.reviewer}. "
                "Change it in File > Settings."
            )
        else:
            self.reviewer_label.setText(
                f"<span style='color:{Color.DESTRUCTIVE};'>No operator set</span>"
            )
            self.reviewer_label.setToolTip(
                "Set your reviewer name in File > Settings. A correction cannot "
                "be saved without one."
            )

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
        """Pick the value one of the offered symbols stands for.

        Args:
            label: A symbol the template prints for this group, or
                :data:`BLANK_CHOICE`.

        Returns:
            Whether the choice was accepted.

        **Picks; does not commit.** The ring lands on the bubble, the
        comparison strip shows what the effective value *would* become, and
        nothing is written until :meth:`confirm_resolution`. A correction
        changes what a script is worth, and a single mis-click deciding it -
        with no moment in between to see that the ring landed on the bubble the
        reviewer meant - is the accidental edit this stage exists to prevent.

        **The one path a value takes, whether it arrived from a button or a
        key.** The buttons call this, the digit keys call this, and there is no
        second implementation for either to drift from.

        Refuses silently when the active conflict does not offer ``label``. A
        reviewer pressing ``7`` on a set-code position printed ``A``-``D`` has
        typed something this sheet has no answer to.
        """
        if self._editing_text():
            return False
        conflict = self.current_conflict()
        if conflict is None or not conflict.allows_value_correction:
            return False
        if label != BLANK_CHOICE and label not in self._offered_labels(conflict):
            return False
        self.state.pending = {
            conflict.conflict_id: "" if label == BLANK_CHOICE else label
        }
        self._refresh_pending_display()
        return True

    @property
    def pending(self) -> str | None:
        """The value picked for the conflict on screen, or ``None``."""
        conflict = self.current_conflict()
        if conflict is None:
            return None
        return self.state.pending.get(conflict.conflict_id)

    def clear_pending(self) -> None:
        """Forget every uncommitted choice."""
        self.state.pending = {}

    def _refresh_pending_display(self) -> None:
        """Show an uncommitted choice everywhere it has to be visible."""
        conflict = self.current_conflict()
        self._refresh_choice_states()
        self._refresh_lanes()
        if conflict is not None:
            self._refresh_provenance(conflict)
        self._refresh_controls()

    def confirm_resolution(self) -> bool:
        """Commit whatever the comparison strip says the effective value is.

        One button for the two things a reviewer can be confirming, because
        from their side it is one act - *this is the value, record it*:

        * a value they picked, which is recorded as a correction;
        * the machine's own reading, when it is a value this field could
          carry and they have inspected it and agree.

        Returns ``False`` without writing anything when neither applies - which
        is exactly the multiply-marked case §10 is about, where there is no
        machine reading to confirm and no choice has been made yet.
        """
        if self._editing_text():
            return False
        conflict = self.current_conflict()
        if conflict is None:
            return False
        picked = self.pending
        if picked is not None:
            return self.correct(picked)
        if self._machine_value_is_confirmable(conflict):
            return self.accept_machine()
        return False

    def _machine_value_is_confirmable(self, conflict: ConflictRecord) -> bool:
        """Whether the machine's reading is a value this field could carry.

        **The distinction the old wording hid.** "Accept machine value" was
        offered for every conflict, including a roll-number position the engine
        read as ``0-5``. Accepting that recorded ``0-5`` as the human-decided
        value of one printed digit, which
        :func:`~omr_scanner.services.review_store._substitute_position` then
        substitutes into the identifier - producing a candidate ID no roster
        will ever match, from a button that said the machine was right.

        The backend is unchanged and still able to store it; what changes is
        that the interface no longer offers it as a resolution. A reading is
        confirmable when it is one of the symbols the template prints for this
        group, or a blank - a reviewer confirming "this position really is
        empty" is making a legitimate decision. Where the group has no fixed
        alphabet at all (a whole identifier, a duplicate roll number) the
        machine's value is a whole field value and is confirmable as it stands.
        """
        if not conflict.allows_value_correction:
            # A sheet-scope conflict has no value; acknowledging it is what
            # "confirm" means there, and that is what the backend records.
            return True
        value = conflict.observation.value
        labels = self._offered_labels(conflict)
        if not labels:
            return True
        return value == "" or value in labels

    def _offered_labels(self, conflict: ConflictRecord) -> tuple[str, ...]:
        """The symbols the template prints for this conflict's group."""
        if self.state.template is None:
            return ()
        return group_labels(
            self.state.template, conflict.field.zone_id, conflict.field.group_key
        )

    # ------------------------------------------------------------------
    # Correcting a whole field in one action
    # ------------------------------------------------------------------
    @property
    def field_edit_toggle(self) -> QToolButton:
        """The field-editor button for the selected record's own field.

        The Student ID button unless a set-code record is selected. Both are
        offered at once (:attr:`field_edit_buttons`); this names the one ``E``
        opens.
        """
        return self.field_edit_buttons[self._contextual_field_kind()]

    def _contextual_field_kind(self) -> FieldKind:
        """Which identity field ``E`` and the machine panel refer to."""
        conflict = self.current_conflict()
        if conflict is not None and conflict.field.kind is FieldKind.SET_CODE:
            return FieldKind.SET_CODE
        return FieldKind.IDENTIFIER

    def sheet_field_shape(self, kind: FieldKind) -> FieldShape | None:
        """The shape of one identity field on the sheet being reviewed, or ``None``.

        **A property of the sheet, not of the selected record.** It does not
        ask whether that field has a conflict: a Student ID read confidently in
        every position, or one whose conflicts are all settled, is still one an
        operator may need to correct. The zone is the one recognition itself
        read the field from, and the shape comes from the template.

        ``None`` when the loaded sheet never rectified - with no trustworthy
        association between the template's positions and the paper, a typed
        field value could not be mapped onto bubbles honestly - or when the
        sheet is still loading, or the template has no such field.
        """
        conflict = self.current_conflict()
        if self.state.template is None or conflict is None:
            return None
        bundle = self.state.bundle
        if bundle is None or bundle.result is None or bundle.result.preview is None:
            return None
        if self._loaded_scan_id != conflict.scan_id:
            return None
        result = bundle.result
        zone_id = (
            result.identifier_zone_id
            if kind is FieldKind.IDENTIFIER
            else result.set_code_zone_id
        )
        if not zone_id:
            return None
        return field_shape(self.state.template, zone_id)

    def _field_shape_for(self, conflict: ConflictRecord | None) -> FieldShape | None:
        """The identity field one record is about, on its sheet, or ``None``.

        ``None`` for a record that is not about the Student ID or the set code.
        A duplicate-ID record and a whole-field record *are* about the Student
        ID, so they get its shape like any position of it.
        """
        if conflict is None or not conflict.field.kind.is_record_identity:
            return None
        return self.sheet_field_shape(conflict.field.kind)

    def _sheet_records(self) -> list[ConflictRecord]:
        """The review records of the sheet being worked, in every state."""
        conflict = self.current_conflict()
        scan_id = conflict.scan_id if conflict is not None else -1
        return [item for item in self.state.sheet_conflicts if item.scan_id == scan_id]

    def current_field_values(self, shape: FieldShape) -> list[str]:
        """The field as it currently reads, one entry per printed position.

        See :func:`~omr_scanner.services.field_edit.current_field_values`,
        which the Attendance stage's script inspector uses too.
        """
        return field_values_now(
            shape,
            self._sheet_records(),
            self.state.sheet_provenance,
            self._machine_characters(shape),
        )

    def _machine_characters(self, shape: FieldShape) -> dict[int, str]:
        """What the engine read at each position of one field, freshly re-read."""
        bundle = self.state.bundle
        return machine_characters(bundle.result if bundle is not None else None, shape)

    def _split_field_value(self, shape: FieldShape, text: str) -> list[str] | None:
        """Split typed text into one symbol per printed position, or ``None``.

        Delegates to :func:`~omr_scanner.services.conflict_policy.split_field_value`,
        the one parser every field consumer shares, so the editor, the preview
        and the stored effective value agree on what a position is.
        """
        return split_field_value(shape, text)

    def _field_edit_text(self) -> str:
        """What the field editor holds, as the value to write on the *paper*.

        For the set code, a typed **logical** code of a set printed as another
        mark is translated to that mark - typing ``10`` for a Set 10 printed as
        ``A`` records ``A``, because the review ledger keeps what the paper
        says and the template's set field offers ``A``, not ``10``. The
        translation is set identity's
        (:meth:`~omr_scanner.domain.set_identity.SetIdentity.physical_for_logical`);
        anything else is passed through unchanged.
        """
        text = self.field_edit_input.text()
        database = self.database
        if self.state.editing_kind is not FieldKind.SET_CODE or database is None:
            return text
        identity = set_identity.load(database)
        found = identity.logical(text)
        if found is None or not found.physical_mark:
            return text
        return identity.physical_for_logical(found.code)

    def _validate_field_value(
        self, shape: FieldShape, text: str
    ) -> tuple[list[str] | None, str]:
        """Check typed text against the template; see ``validate_field_value``."""
        return validate_field_value(shape, text)

    def _field_edit_plan(self, shape: FieldShape, values: Sequence[str]) -> FieldEditPlan:
        """Decide which positions a typed value would change, and how.

        See :func:`~omr_scanner.services.field_edit.plan_field_edit`: disputed
        positions, earlier decisions, and confident readings - the last kept
        apart as overrides the operator is warned about.
        """
        return plan_field_edit(
            shape,
            values,
            self._sheet_records(),
            self.state.sheet_provenance,
            self._machine_characters(shape),
        )

    def _machine_observation_at(self, shape: FieldShape, position: int) -> MachineObservation:
        """What the engine read at one position, as an override record keeps it."""
        bundle = self.state.bundle
        return machine_observation_at(
            bundle.result if bundle is not None else None, shape, position
        )

    def _confirm_override(
        self, shape: FieldShape, overrides: dict[int, tuple[str, str]]
    ) -> bool:
        """Ask before a typed value overrules a confident machine reading.

        Only ever shown when :attr:`FieldEditPlan.overrides` is non-empty:
        correcting positions that were already in dispute is what Apply means,
        and warning about it would teach the operator to click through this.
        Cancel is the default, so a stray Enter changes nothing.
        """
        box = QMessageBox(self)
        box.setObjectName("fieldOverrideConfirmation")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Manual field override")
        box.setText(override_warning_text(shape.label, overrides))
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        apply = box.addButton("Apply override", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is apply

    def open_field_editor(self, kind: FieldKind | None = None) -> bool:
        """Open the whole-field editor on one of the sheet's identity fields.

        Args:
            kind: Which field; the selected record's own field when omitted
                (the Student ID unless a set-code record is selected).

        Refuses while the keyboard belongs to a text box, so that typing ``e``
        into the search field does not open an editor behind it.
        """
        if self._editing_text():
            return False
        chosen = kind or self._contextual_field_kind()
        shape = self.sheet_field_shape(chosen)
        conflict = self.current_conflict()
        if shape is None or conflict is None:
            return False
        self.state.editing_field = shape
        self.state.editing_scan_id = conflict.scan_id
        self.state.editing_kind = chosen
        self.field_edit_label.setText(f"Correct {shape.label}")
        self.field_edit_input.setText(join_field_value(self.current_field_values(shape)))
        self.field_edit_row.setVisible(True)
        for item, button in self.field_edit_buttons.items():
            opened = item is chosen
            button.blockSignals(True)
            button.setChecked(opened)
            button.blockSignals(False)
            button.setArrowType(
                Qt.ArrowType.DownArrow if opened else Qt.ArrowType.RightArrow
            )
        self.field_edit_input.setFocus()
        # Select-all, because the reviewer is almost always replacing the whole
        # value rather than editing one character of a reading they already
        # know is wrong.
        self.field_edit_input.selectAll()
        self._refresh_field_edit_status()
        return True

    def close_field_editor(self) -> None:
        """Shut the whole-field editor, discarding anything staged in it.

        Hands the keyboard back to the queue. Qt moves focus to the next widget
        in the chain when the focused one is hidden, which landed it in the
        note box - where every shortcut on this page refuses to fire, so the
        reviewer's next keystroke, including ``Ctrl+Z``, did nothing at all.
        """
        self.state.editing_field = None
        self.state.editing_scan_id = None
        self.state.editing_kind = None
        self.field_edit_row.setVisible(False)
        for button in self.field_edit_buttons.values():
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
            button.setArrowType(Qt.ArrowType.RightArrow)
        self.clear_pending()
        self._refresh_pending_display()
        self.queue_table.setFocus()

    def _on_field_editor_toggled(self, kind: FieldKind, shown: bool) -> None:
        """Open or shut the editor from one field's own button."""
        if shown:
            if not self.open_field_editor(kind):
                button = self.field_edit_buttons[kind]
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
            return
        if self.state.editing_field is not None:
            self.close_field_editor()

    def _on_field_edit_typed(self, _text: str) -> None:
        """Re-validate and re-stage as the reviewer types."""
        self._refresh_field_edit_status()

    def _refresh_field_edit_status(self) -> None:
        """Validate what is typed, stage it on the sheet, and say what it does.

        Staging as they type is what keeps a field-level correction from being
        an opaque text operation: the positions the value would change light up
        on the paper in the pane above, so the operator can see that the number
        they typed lands on the bubbles they meant before anything is written.
        """
        shape = self.state.editing_field
        if shape is None:
            return
        values, problem = self._validate_field_value(shape, self._field_edit_text())
        self.field_edit_status.setToolTip("")
        self.field_edit_apply.setText(FIELD_EDIT_APPLY_TEXT)
        if values is None:
            self.clear_pending()
            self._refresh_pending_display()
            self.field_edit_status.setText(
                f"<span style='color:{Color.DESTRUCTIVE};'>{problem}</span>"
            )
            self.field_edit_apply.setEnabled(False)
            return

        plan = self._field_edit_plan(shape, values)
        self.state.pending = dict(plan.changes)
        self._refresh_pending_display()

        if plan.is_empty:
            self.field_edit_status.setText(
                f"<span style='color:{Color.TEXT_TERTIARY};'>Already recorded; "
                "nothing to change.</span>"
            )
            self.field_edit_apply.setEnabled(False)
            return

        # Current -> proposed, then which positions move, then - in the
        # warning colour - any confident reading this would overrule.
        before = html.escape(join_field_value(plan.current))
        after = html.escape(join_field_value(plan.proposed))
        positions = ", ".join(str(item + 1) for item in plan.positions)
        # One line: the editor row is one line high, and a second line was
        # clipped. The override warning comes straight after the values, so
        # it is the last thing a narrow row cuts off, not the first.
        parts = [f"<b>{before}</b> &rarr; <b>{after}</b>"]
        parts.extend(
            f"<span style='color:{Color.STATUS_BUSY};'>Position {position + 1} "
            f"&mdash; confident machine read ({html.escape(machine or '(blank)')} "
            f"&rarr; {html.escape(typed or '(blank)')})</span>"
            for position, (machine, typed) in sorted(plan.overrides.items())
        )
        parts.append(f"Changed positions: <b>{positions}</b>")
        if self.state.editing_kind is FieldKind.SET_CODE:
            # Straight after the values, so a narrow row clips it last.
            meaning = self.describe_set_reading(join_field_value(plan.proposed))
            if meaning:
                parts[0] += f" = <b>{html.escape(meaning)}</b>"
        text = (
            f"<span style='color:{Color.TEXT_TERTIARY};'>"
            + " &middot; ".join(parts)
            + "</span>"
        )
        self.field_edit_status.setText(text)
        self.field_edit_status.setToolTip(text)
        # The button says what it will do: an override asks first.
        self.field_edit_apply.setText(
            "Apply override..." if plan.overrides else FIELD_EDIT_APPLY_TEXT
        )
        self.field_edit_apply.setEnabled(bool(self.state.reviewer))

    def _position_of(self, conflict_id: int) -> int:
        """The printed position one conflict is about."""
        record = next(
            (
                item
                for item in self.state.sheet_conflicts
                if item.conflict_id == conflict_id
            ),
            None,
        )
        return record.field.group_key if record is not None else -1

    def apply_field_edit(self) -> bool:
        """Record the typed field value across the positions it settles.

        One transaction, one reason, one note, one operator action - and, under
        it, one ordinary correction per position with the audit event a
        single-digit correction would have had. See
        :func:`~omr_scanner.services.review_store.correct_field`.
        """
        shape = self.state.editing_field
        database = self.database
        conflict = self.current_conflict()
        if shape is None or database is None or conflict is None:
            return False
        if self.state.batch_id is None or not self.field_edit_apply.isEnabled():
            return False
        values, problem = self._validate_field_value(shape, self._field_edit_text())
        if values is None:
            _LOGGER.info("Field edit refused: %s", problem)
            return False
        plan = self._field_edit_plan(shape, values)
        if plan.is_empty:
            return False
        # Asked before anything is written, so Cancel leaves the database, the
        # editor and what is staged on the sheet exactly as they were.
        if plan.overrides and not self._confirm_override(shape, plan.overrides):
            _LOGGER.info(
                "Field edit cancelled at the override warning (%d position(s))",
                len(plan.overrides),
            )
            return False

        scan_id = conflict.scan_id
        bundle = self.state.bundle
        try:
            edit = commit_field_edit(
                database,
                batch_id=self._sheet_batch(scan_id),
                scan_id=scan_id,
                shape=shape,
                # The field being edited - not the selected record, which may
                # belong to the other field on this sheet.
                kind=self.state.editing_kind or conflict.field.kind,
                plan=plan,
                records=self._sheet_records(),
                result=bundle.result if bundle is not None else None,
                reviewer=self.state.reviewer,
                reason=self._selected_reason(),
                reason_text=self.reason_text.toPlainText(),
            )
        except (ReviewError, OMRScannerError) as exc:
            report_error(self, exc, context="Conflict review (field edit)")
            return False

        self.state.last_field_edit = edit
        # A field edit is one action, so it supersedes anything that could have
        # been redone - exactly as a single decision does.
        self.state.redo.clear()
        self.clear_pending()
        self.close_field_editor()
        self.reason_text.clear()
        self._settle(scan_id)
        self.resolution_recorded.emit(conflict.conflict_id)
        self._advance_after(scan_id)
        _LOGGER.info(
            "Scan %d: %s set to %r across %d position(s) by %s",
            scan_id,
            shape.label,
            edit.value,
            edit.conflict_count,
            self.state.reviewer,
        )
        return True

    def _machine_marks(self, conflict: ConflictRecord) -> frozenset[str]:
        """The symbols the engine read as marked in this group.

        Read from the stored observation through
        :func:`~omr_scanner.services.conflict_policy.split_marks`, so the
        interface does not have its own idea of how ``"0-5"`` is written down.
        """
        return frozenset(split_marks(conflict.observation.value))

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
            self._advance_after(conflict.scan_id)
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

    def _advance_after(self, scan_id: int) -> None:
        """Move to the next thing needing a decision, if that is wanted.

        **The sheet is finished before the next one is started.** An operator
        holds one script; making them settle a digit on it and then sending
        them to a different sheet - which is what advancing purely by queue
        order did - means picking the same paper up again later for every
        other problem on it.

        The order is:

        1. the next unresolved conflict on **this sheet**, after the one just
           decided, wrapping to its first;
        2. failing that, the next unresolved conflict in queue order, which is
           the next sheet that needs work;
        3. failing that, nothing - the queue is clear, and
           :meth:`_restore_selection` has already emptied the workspace.

        Called only by the commands that **settle** a conflict. Deferring and
        reopening deliberately do not advance: both are a reviewer saying "come
        back to this", and skipping past it would make the thing they asked to
        see again the thing they cannot find.
        """
        if not self.state.auto_advance:
            return
        if self.select_next_on_sheet(scan_id):
            return
        self.select_next_unresolved()

    def select_next_on_sheet(self, scan_id: int) -> bool:
        """Select the next conflict still needing a decision on one sheet.

        Args:
            scan_id: The sheet to stay on.

        Returns:
            Whether one was found. ``False`` means the sheet is finished, and
            is what sends the reviewer to the next one.

        Deferred conflicts are passed over for the same reason
        :meth:`_select_unresolved` passes over them: deferring is "not this
        one, not now", and looping back to it would make finishing a sheet
        impossible.
        """
        rows = [
            index
            for index, item in enumerate(self.state.conflicts)
            if item.scan_id == scan_id and item.state is ConflictState.OPEN
        ]
        if not rows:
            return False
        current = self.queue_table.currentRow()
        row = next((item for item in rows if item > current), rows[0])
        self.queue_table.selectRow(row)
        return True

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
        target = last_decision(database, self.state.batch_id, session_wide=True)
        if target is None:
            return False
        try:
            # A whole-field edit was one action from where the operator was
            # sitting - they typed one roll number - so one press takes it
            # back, rather than one press per position it happened to write.
            if target.group:
                reversed_commands = undo_field_edit(
                    database,
                    batch_id=self._sheet_batch(target.scan_id),
                    group=target.group,
                    reviewer=self.state.reviewer,
                    reason_text=self.reason_text.toPlainText(),
                )
                if not reversed_commands:
                    return False
                self.state.redo.extend(reversed_commands)
                undone = reversed_commands[-1]
            else:
                undone = undo_decision(
                    database,
                    target.conflict_id,
                    reviewer=self.state.reviewer,
                    reason_text=self.reason_text.toPlainText(),
                )
                self.state.redo.append(undone)
        except (ReviewError, OMRScannerError) as exc:
            report_error(self, exc, context="Conflict review (undo)")
            return False

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
                session_wide=True,
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

        self._refresh_primary_action(conflict, can_decide=can_decide)
        # An operator override is not an open question, so there is nothing to
        # postpone; undo or reopen it instead.
        self.defer_button.setEnabled(
            can_decide
            and conflict is not None
            and not conflict.conflict_type.is_operator_override
        )
        decided = conflict is not None and conflict.state.is_human_touched
        # Shown only when there is something to reopen. A permanently disabled
        # button teaches an operator to stop seeing a third of the action area.
        self.reopen_button.setVisible(decided)
        self.reopen_button.setEnabled(can_decide and decided)
        for button in self._choice_buttons:
            button.setEnabled(can_decide)
        self.free_value_edit.setEnabled(can_decide)
        self.free_value_button.setEnabled(can_decide)
        self._refresh_field_edit_controls(conflict, can_decide=can_decide)
        self.history_button.setEnabled(has_conflict)
        for action in (
            self.previous_action,
            self.next_action,
            self.previous_open_action,
            self.next_open_action,
        ):
            action.setEnabled(bool(self._row_count()))
        rescan = self.rescan_mode
        # The type filter names conflict types, which the Rejected / Rescan
        # view does not list.
        self.type_filter.setEnabled(not rescan)
        self.reject_button.setEnabled(named and not rescan and self._reject_target() is not None)
        self.reject_button.setToolTip(
            "This scan is unusable - folded, clipped, skewed, the wrong page. "
            "Reject it pending a rescan (R). Nothing is deleted."
            if named
            else "Set your reviewer name in File > Settings before rejecting a scan."
        )
        self._refresh_undo_controls(named=named)
        self._refresh_reviewer_label()

    def _refresh_field_edit_controls(
        self, conflict: ConflictRecord | None, *, can_decide: bool
    ) -> None:
        """Offer each of the sheet's identity-field editors where it can work.

        Per **sheet**, whatever record is selected - see
        :meth:`sheet_field_shape`. Hidden only when the sheet has no such field
        or never rectified: then there is no trustworthy association between
        the template's positions and the paper, and typing an identifier would
        be asserting where bubbles are rather than reading them.
        """
        for kind, button in self.field_edit_buttons.items():
            shape = self.sheet_field_shape(kind)
            button.setVisible(shape is not None)
            button.setEnabled(shape is not None and can_decide)
            if shape is None:
                continue
            button.setText(f"Edit full {shape.label}...")
            button.setToolTip(
                f"Type the whole {shape.label} once ({shape.length} positions). "
                "Settles every disputed position; changing a position the "
                "machine read confidently is an audited override and asks first"
            )
        editing = self.state.editing_field
        if editing is None:
            return
        still_offered = any(
            (shape := self.sheet_field_shape(kind)) is not None
            and shape.zone_id == editing.zone_id
            for kind in self.field_edit_buttons
        )
        moved = conflict is None or conflict.scan_id != self.state.editing_scan_id
        if moved or not still_offered:
            # A value typed for one script is never carried to another.
            self.close_field_editor()

    def _refresh_primary_action(
        self, conflict: ConflictRecord | None, *, can_decide: bool
    ) -> None:
        """Say what the primary button would do, and whether it can do it.

        Three different sentences, because they are three different acts and
        an operator working at speed reads the button rather than reasoning
        about the state behind it:

        * **Confirm 5** - a value has been picked and will be recorded;
        * **Confirm machine reading** - the engine's value is one this field
          could carry, and confirming it says a person checked;
        * **Choose a value first** - disabled, for the multiply-marked case
          where there is nothing to confirm yet. See
          :meth:`_machine_value_is_confirmable`.
        """
        if conflict is None:
            self.confirm_button.setVisible(True)
            self.confirm_button.setEnabled(False)
            self.confirm_button.setText("Confirm resolution")
            self.confirm_button.setToolTip("")
            return

        picked = self.pending
        if picked is not None:
            shown = picked or BLANK_BUTTON_TEXT.lower()
            self.confirm_button.setVisible(True)
            self.confirm_button.setText(f"Confirm '{shown}'")
            self.confirm_button.setToolTip(
                f"Record '{shown}' as this position's value, against your name "
                "and the reason below (Enter)"
            )
            self.confirm_button.setEnabled(can_decide)
            return

        # Already decided, and nothing new picked. There is nothing to confirm,
        # and Reopen - which is now visible - is the action that applies. A
        # button reading "Choose a value first" here would be telling the
        # operator to fix something that is not broken.
        if conflict.state.is_human_touched:
            self.confirm_button.setVisible(False)
            self.confirm_button.setEnabled(False)
            return
        self.confirm_button.setVisible(True)

        if not conflict.allows_value_correction:
            # A page that would not rectify or a file that would not decode is
            # not a value, so "confirm the machine reading" would be confirming
            # nothing. Acknowledging is what the backend records here, and it
            # is what the button should say.
            self.confirm_button.setText("Acknowledge")
            self.confirm_button.setToolTip(
                "Record that you have seen this. It settles the conflict "
                "without changing any value (Enter)"
            )
            self.confirm_button.setEnabled(can_decide)
            return

        if self._machine_value_is_confirmable(conflict):
            self.confirm_button.setText("Confirm machine reading")
            self.confirm_button.setToolTip(
                "Record that you inspected this and the machine was right (Enter)"
            )
            self.confirm_button.setEnabled(can_decide)
            return

        # The multiply-marked case. There is no single value to confirm, so the
        # button says what is missing rather than offering to store a reading
        # the field cannot carry.
        self.confirm_button.setText("Choose a value first")
        self.confirm_button.setToolTip(
            f"The machine read '{conflict.observation.value}', which is not one "
            "value this position can hold. Choose one of the values above, "
            "choose Blank, or defer."
        )
        self.confirm_button.setEnabled(False)

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
            last_decision(database, batch, session_wide=True)
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
            last_resolved_sheet(database, batch, session_wide=True)
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
    # Reject & Rescan
    # ------------------------------------------------------------------
    def prompt_reject_current_sheet(self) -> bool:
        """Ask why the sheet on screen is unusable, then reject it.

        Owns the modal :class:`~omr_scanner.gui.review.rescan.RejectScanDialog`
        and nothing else; :meth:`reject_current_sheet` does the work and is
        what a test drives.
        """
        if self._editing_text() or self.rescan_mode:
            return False
        target = self._reject_target()
        database = self.database
        if target is None or database is None or self.state.batch_id is None:
            return False
        scan_id, scan_name = target
        if not self.state.reviewer:
            QMessageBox.information(
                self,
                "Reviewer needed",
                "Set your reviewer name in File > Settings. A rejection is "
                "recorded against the person who made it.",
            )
            return False
        identity = scan_lifecycle.current_identity(database, scan_id)
        sets = project_sets.list_sets(database)
        dialog = RejectScanDialog(
            scan_name,
            identity[0],
            identity[1],
            [item.code for item in sets],
            self,
            set_labels={item.code: item.label_with_mark for item in sets},
        )
        if dialog.exec() != RejectScanDialog.DialogCode.Accepted:
            return False
        reason, note, declared_id, declared_set = dialog.values()
        return self.reject_current_sheet(
            reason, note=note, declared_candidate_id=declared_id, declared_set_code=declared_set
        )

    def reject_current_sheet(
        self,
        reason: RejectionReason,
        *,
        note: str = "",
        declared_candidate_id: str = "",
        declared_set_code: str = "",
    ) -> bool:
        """Reject the sheet on screen, pending a rescan.

        The sheet the selected conflict is on - or, in *All processed sheets*,
        the selected sheet, which need have no conflict at all. Either way it
        is one rule and one service call.

        The sheet leaves the working queue at once - its conflicts are kept,
        not withdrawn, and come back exactly as they were if the rejection is
        undone - and, with auto-advance on, the next unresolved conflict is
        selected as after any decision that settles a sheet. In the sheet view
        the selection stays on the sheet just rejected, which now says so.
        """
        target = self._reject_target()
        database = self.database
        if target is None or database is None or self.state.batch_id is None:
            return False
        scan_id = target[0]
        try:
            scan_lifecycle.reject_scan(
                database,
                scan_id,
                reviewer=self.state.reviewer,
                reason=reason,
                note=note,
                declared_candidate_id=declared_candidate_id,
                declared_set_code=declared_set_code,
            )
        except OMRScannerError as exc:
            report_error(self, exc, context="Reject scan")
            return False
        _LOGGER.info("Scan %d rejected on the Resolve stage by %s", scan_id, self.state.reviewer)
        self.close_field_editor()
        self.clear_pending()
        self.refresh_queue()
        if self.sheets_mode:
            self.select_sheet(scan_id)
        elif self.state.auto_advance:
            # The rejected sheet's rows are gone, and the selection has moved
            # to what now occupies their place. Stay there when that is work
            # to do; otherwise go on to the next unresolved conflict.
            current = self.current_conflict()
            if current is None or current.state is not ConflictState.OPEN:
                self.select_next_unresolved()
        self.lifecycle_changed.emit(scan_id)
        return True

    def _reject_target(self) -> tuple[int, str] | None:
        """``(scan id, file name)`` of the sheet *Reject / Rescan* would act on."""
        if self.sheets_mode:
            sheet = self.current_sheet()
            if sheet is None or not sheet.state.is_result_eligible:
                return None
            return sheet.scan_id, sheet.filename
        conflict = self.current_conflict()
        if conflict is None:
            return None
        return conflict.scan_id, conflict.scan_name

    def show_processed_sheets(self) -> None:
        """Switch the queue to the *All processed sheets* inspection view."""
        self.state_filter.setCurrentText(FILTER_SHEETS)

    def current_sheet(self) -> ProcessedSheet | None:
        """The sheet selected in *All processed sheets*, or ``None``."""
        if not self.sheets_mode:
            return None
        row = self.queue_table.currentRow()
        if 0 <= row < len(self.state.sheet_rows):
            return self.state.sheet_rows[row]
        return None

    def select_sheet(self, scan_id: int) -> bool:
        """Select one sheet in the sheet view, if it is listed."""
        for row, sheet in enumerate(self.state.sheet_rows):
            if sheet.scan_id == scan_id:
                self.queue_table.selectRow(row)
                return True
        return False

    def _refresh_sheet_queue(self) -> None:
        """Re-read *All processed sheets*: one SQL page, filtered by the search box.

        The same page size as the conflict queue, and the same "filter in SQL,
        hold detached values" rule: a batch of a hundred thousand sheets costs
        one indexed query per refresh, and no image is loaded until a row is
        selected.
        """
        database = self.database
        batch_id = self.state.batch_id
        if database is None or batch_id is None:  # pragma: no cover - checked by caller
            return
        selected = self.current_sheet()
        self.state.conflicts = []
        self.state.sheet_rows = list(
            scan_lifecycle.processed_sheets(
                database,
                batch_id,
                search=self.search_box.text(),
                limit=QUEUE_PAGE_SIZE,
                session_wide=True,
            )
        )
        self._suppress_selection = True
        self.queue_table.setUpdatesEnabled(False)
        try:
            self.queue_table.clearSelection()
            self.queue_table.setCurrentCell(-1, -1)
            self.queue_table.setRowCount(len(self.state.sheet_rows))
            for row, sheet in enumerate(self.state.sheet_rows):
                issue = (
                    f"{sheet.open_conflicts} awaiting a decision"
                    if sheet.open_conflicts
                    else "No open conflict"
                )
                values = (
                    sheet.filename or f"scan {sheet.scan_id}",
                    sheet.identifier,
                    issue,
                    f"{sheet.state.marker} {sheet.state.label}",
                )
                colour = (
                    _RESCAN_PENDING_COLOR
                    if sheet.state.is_outstanding
                    else _RESCAN_DONE_COLOR
                    if not sheet.state.is_result_eligible
                    else QColor(255, 255, 255)
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setBackground(colour)
                    item.setToolTip(text)
                    self.queue_table.setItem(row, column, item)
        finally:
            self.queue_table.setUpdatesEnabled(True)
            self._suppress_selection = False
        self._refresh_summary()
        if self.decision_stack.currentWidget() is not self.sheet_panel:
            self.decision_stack.setCurrentWidget(self.sheet_panel)
            self._loaded_scan_id = None
            self._note_navigation("")
        if not self.state.sheet_rows:
            self._show_sheet(None)
        else:
            # Select quietly, then show once: one image load per refresh.
            self._suppress_selection = True
            try:
                if selected is None or not self.select_sheet(selected.scan_id):
                    self.queue_table.selectRow(0)
            finally:
                self._suppress_selection = False
            self._on_queue_selection_changed()
        self._refresh_controls()

    def _show_sheet(self, sheet: ProcessedSheet | None) -> None:
        """Show one processed sheet: as it arrived, what it reads as, where it stands."""
        self.clear_pending()
        database = self.database
        if sheet is None or database is None:
            self.sheet_panel.show_sheet(None)
            self.lifecycle_banner.setVisible(False)
            self._clear_views()
            self._refresh_controls()
            return
        effective_id, effective_set = scan_lifecycle.current_identity(database, sheet.scan_id)
        self.sheet_panel.show_sheet(
            sheet,
            effective_id=effective_id,
            effective_set=effective_set,
            can_decide=bool(self.state.reviewer),
        )
        if sheet.state.is_result_eligible:
            self.lifecycle_banner.setVisible(False)
        else:
            colour = Color.DESTRUCTIVE if sheet.state.is_outstanding else Color.TEXT_TERTIARY
            self.lifecycle_banner.setText(
                f"<span style='color:{colour};'><b>{sheet.state.marker} "
                f"{html.escape(sheet.state.label)}</b> &mdash; "
                f"{html.escape(sheet.filename)} does not count towards any result</span>"
            )
            self.lifecycle_banner.setVisible(True)
        self._load_image(Path(scan_source_path(database, sheet.scan_id)))
        self._refresh_controls()

    def show_rejected_rescans(self) -> None:
        """Switch the queue to the *Rejected / Rescan* view."""
        self.state_filter.setCurrentText(FILTER_RESCAN)

    def current_case(self) -> RescanCase | None:
        """The Reject & Rescan case selected in that view, or ``None``."""
        if not self.rescan_mode:
            return None
        row = self.queue_table.currentRow()
        if 0 <= row < len(self.state.rescan_cases):
            return self.state.rescan_cases[row]
        return None

    def select_case(self, scan_id: int) -> bool:
        """Select the case of one rejected scan, if it is in the view."""
        for row, case in enumerate(self.state.rescan_cases):
            if case.scan_id == scan_id:
                self.queue_table.selectRow(row)
                return True
        return False

    def _refresh_rescan_queue(self) -> None:
        """Re-read the *Rejected / Rescan* view: outstanding cases, then completed."""
        database = self.database
        batch_id = self.state.batch_id
        if database is None or batch_id is None:  # pragma: no cover - checked by caller
            return
        selected = self.current_case()
        scrolled_to = self.queue_table.verticalScrollBar().value()
        self.state.conflicts = []
        cases = list(
            scan_lifecycle.list_cases(
                database, batch_id, include_completed=True, session_wide=True
            )
        )
        search = self.search_box.text().strip().casefold()
        if search:
            cases = [
                case
                for case in cases
                if search in (case.source_name or "").casefold()
                or search in case.identity.casefold()
                or search in (case.replacement_name or "").casefold()
            ]
        self.state.rescan_cases = cases
        self.state.rescan_candidates = scan_lifecycle.possible_rescans(database, batch_id)
        self._rebuild_rescan_table()
        self._refresh_summary()
        if self.decision_stack.currentWidget() is not self.rescan_panel:
            self.decision_stack.setCurrentWidget(self.rescan_panel)
            self._loaded_scan_id = None
            # A note about the conflict queue says nothing about this view.
            self._note_navigation("")
        if not cases:
            self.queue_table.clearSelection()
            self._show_case(None)
        elif selected is None or not self.select_case(selected.scan_id):
            first = next((i for i, case in enumerate(cases) if case.is_outstanding), 0)
            self.queue_table.selectRow(first)
            self.queue_table.verticalScrollBar().setValue(
                min(scrolled_to, self.queue_table.verticalScrollBar().maximum())
            )
            if self.queue_table.currentRow() == first:
                self._on_queue_selection_changed()
        self._refresh_controls()

    def _rebuild_rescan_table(self) -> None:
        """Rebuild the table from :attr:`ResolvePageState.rescan_cases`."""
        self._suppress_selection = True
        self.queue_table.setUpdatesEnabled(False)
        try:
            self.queue_table.clearSelection()
            self.queue_table.setCurrentCell(-1, -1)
            self.queue_table.setRowCount(len(self.state.rescan_cases))
            for row, case in enumerate(self.state.rescan_cases):
                issue = case.reason_label or "Rejected"
                if case.set_code:
                    issue += f" · Set {case.set_code}"
                values = (
                    case.source_name or f"scan {case.scan_id}",
                    case.identity or "(unknown)",
                    issue,
                    f"{case.state.marker} {case.status_text}",
                )
                tip = (
                    f"{case.source_name}\nRejected "
                    + (f"{case.rejected_at:%Y-%m-%d %H:%M}" if case.rejected_at else "")
                    + f" by {case.rejected_by or '-'}"
                    + (
                        f"\nReplaced by {case.replacement_name}"
                        if case.replacement_name
                        else ""
                    )
                    + f"\n{case.file_state.label}"
                )
                colour = (
                    _RESCAN_PENDING_COLOR if case.is_outstanding else _RESCAN_DONE_COLOR
                )
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setBackground(colour)
                    item.setToolTip(tip if column in (0, 3) else text)
                    self.queue_table.setItem(row, column, item)
        finally:
            self.queue_table.setUpdatesEnabled(True)
            self._suppress_selection = False

    def _candidates_for_case(self, case: RescanCase) -> tuple[ReplacementCandidate, ...]:
        """The scans the case panel offers: suggestions, or every scan on request."""
        database = self.database
        if database is None or not case.is_outstanding:
            return ()
        if self.rescan_panel.show_all_button.isChecked():
            # Project-wide, filtered in SQL by the search box and capped.
            return scan_lifecycle.association_choices(
                database,
                case.scan_id,
                search=self.rescan_panel.association_search.text(),
            )
        return self.state.rescan_candidates.get(case.scan_id, ())

    def _on_association_search(self, _text: str) -> None:
        """Re-query the manual association list for what was typed."""
        self._on_show_all_scans(True)

    def _on_show_all_scans(self, _shown: bool) -> None:
        case = self.current_case()
        if case is not None:
            self.rescan_panel.show_case(
                case, self._candidates_for_case(case), can_decide=bool(self.state.reviewer)
            )

    def _show_case(self, case: RescanCase | None) -> None:
        """Display one case in the workspace: its image, its state, its actions."""
        self.state.showing_replacement = False
        self.clear_pending()
        if case is None:
            self.rescan_panel.show_case(None)
            self.lifecycle_banner.setVisible(False)
            self._clear_views()
            self._refresh_controls()
            return
        self.rescan_panel.show_case(
            case, self._candidates_for_case(case), can_decide=bool(self.state.reviewer)
        )
        self._set_banner(case, replacement=False)
        self._load_case_image(case, replacement=False)
        self._refresh_controls()

    def _set_banner(self, case: RescanCase, *, replacement: bool) -> None:
        """Say above the image which scan is on screen and what it is."""
        if replacement:
            text = (
                f"<b>Showing the replacement</b> {html.escape(case.replacement_name)} "
                f"&mdash; the confirmed rescan of {html.escape(case.source_name)}"
            )
            colour = Color.TEXT_TERTIARY
        else:
            text = (
                f"<b>{case.state.marker} {html.escape(case.state.label)}</b> &mdash; "
                f"{html.escape(case.source_name)} does not count towards any result"
            )
            colour = (
                Color.DESTRUCTIVE if case.is_outstanding else Color.TEXT_TERTIARY
            )
        self.lifecycle_banner.setText(f"<span style='color:{colour};'>{text}</span>")
        self.lifecycle_banner.setVisible(True)

    def _load_case_image(self, case: RescanCase, *, replacement: bool) -> None:
        """Decode the rejected original, or its replacement, for inspection."""
        database = self.database
        if database is None:
            return
        for view in (self.zoom_view, self.normalised_view, self.original_view):
            view.clear()
        if not replacement and case.file_state is not FileState.PRESENT:
            when = f" on {case.file_action_at:%Y-%m-%d}" if case.file_action_at else ""
            message = (
                f"{case.file_state.label}{when}. The scan's record, its rejection, "
                "its replacement link and its history are kept."
            )
            self.original_view.set_placeholder(message)
            self.original_note.setText(message)
            self._offer_the_useful_tab(registered=False)
            return
        source = (
            scan_source_path(database, case.replacement_scan_id)
            if replacement and case.replacement_scan_id is not None
            else case.source_path or scan_source_path(database, case.scan_id)
        )
        if not source:
            return
        self._load_image(Path(source))

    def _load_image(self, path: Path) -> None:
        """Decode one scan for inspection, off the GUI thread.

        No template and no re-read: the Rejected / Rescan and sheet views show
        a scan as it arrived. Supersedes any loader still running.
        """
        if not str(path) or str(path) == ".":
            return
        for view in (self.zoom_view, self.normalised_view, self.original_view):
            view.clear()
        if self._worker is not None and self._worker.isRunning():
            self._worker.ready.disconnect()
        worker = OriginalImageWorker(path, self)
        worker.ready.connect(self._on_case_image_ready)
        self._worker = worker
        self._workers = [item for item in self._workers if item.isRunning()]
        self._workers.append(worker)
        worker.start()

    def _on_case_image_ready(self, bundle: SheetBundle) -> None:
        """Draw a decoded case image. Runs on the GUI thread."""
        shown = self.current_case() if self.rescan_mode else self.current_sheet()
        if shown is None:
            self.sheet_ready.emit()
            return
        message = (
            "A rejected sheet is inspected as it arrived - see the Original scan tab."
            if self.rescan_mode
            else "Shown as it arrived - see the Original scan tab. Its conflicts, if "
            "any, are in the conflict views."
        )
        for view in (self.normalised_view, self.zoom_view):
            view.clear()
            view.set_placeholder(message)
        self._offer_the_useful_tab(registered=False)
        if bundle.original is not None:
            self.original_view.set_placeholder("")
            self.original_view.set_page(
                bundle.original,
                canonical_width=bundle.original.width,
                canonical_height=bundle.original.height,
            )
            self.original_view.set_overlay((), (), ())
            self.original_view.set_scan_quality(None)
            self.original_view.set_overlay_visible(zones=False, bubbles=False, empty=False)
            self.original_view.fit_to_window()
            self.original_note.setText(
                f"{bundle.path.name}, exactly as it arrived (never modified)."
            )
        else:
            reason = bundle.error or "The image could not be decoded."
            self.original_view.set_placeholder(reason)
            self.original_note.setText(reason)
        self.sheet_ready.emit()

    def show_replacement_image(self, shown: bool) -> None:
        """Compare: show a completed case's replacement, or its original."""
        case = self.current_case()
        if case is None or case.replacement_scan_id is None:
            return
        self.state.showing_replacement = shown
        self._set_banner(case, replacement=shown)
        self._load_case_image(case, replacement=shown)

    def confirm_replacement_for_case(self, replacement_scan_id: int) -> bool:
        """Confirm, explicitly, which scan is the selected case's rescan."""
        case = self.current_case()
        database = self.database
        if case is None or database is None:
            return False
        try:
            scan_lifecycle.confirm_replacement(
                database, case.scan_id, replacement_scan_id, reviewer=self.state.reviewer
            )
        except OMRScannerError as exc:
            report_error(self, exc, context="Confirm replacement")
            return False
        self._after_lifecycle_action(case.scan_id)
        return True

    def undo_reject_for_case(self) -> bool:
        """Undo the selected case's rejection.

        Refused by the service once a replacement is linked - see
        :func:`~omr_scanner.services.scan_lifecycle.undo_reject`.
        """
        case = self.current_case()
        database = self.database
        if case is None or database is None:
            return False
        try:
            scan_lifecycle.undo_reject(database, case.scan_id, reviewer=self.state.reviewer)
        except OMRScannerError as exc:
            report_error(self, exc, context="Undo reject")
            return False
        self._after_lifecycle_action(case.scan_id)
        return True

    def remove_replacement_for_case(self) -> bool:
        """Withdraw a confirmed replacement link, after asking."""
        case = self.current_case()
        database = self.database
        if case is None or database is None:
            return False
        if not self.confirm_remove_replacement(case):
            return False
        try:
            scan_lifecycle.remove_replacement(database, case.scan_id, reviewer=self.state.reviewer)
        except OMRScannerError as exc:
            report_error(self, exc, context="Remove replacement link")
            return False
        self._after_lifecycle_action(case.scan_id)
        return True

    def confirm_remove_replacement(self, case: RescanCase) -> bool:
        """Ask before withdrawing a replacement link. Cancel is the default.

        A method of its own so that a test can answer it instead of a person.
        """
        box = QMessageBox(self)
        box.setObjectName("removeReplacementConfirmation")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Remove replacement link")
        box.setText(
            f"{case.replacement_name or 'The replacement'} will no longer be recorded "
            f"as the rescan of {case.source_name}. The rejected scan goes back to "
            "awaiting a rescan - it is not reactivated. Continue?"
        )
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        remove = box.addButton("Remove link", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is remove

    def prompt_import_rescan(self) -> bool:
        """Ask for rescan image files, then hand them to the Scan stage."""
        if self.state.batch_id is None:
            return False
        patterns = "Scanned sheets (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)"
        paths, _filter = QFileDialog.getOpenFileNames(self, "Import rescan", "", patterns)
        if not paths:
            return False
        return self.request_rescan_import([Path(item) for item in paths])

    def request_rescan_import(self, paths: Sequence[Path]) -> bool:
        """Ask the window to read rescan files into this batch.

        The files are processed like any other scan; nothing links them to a
        rejected sheet until the operator confirms it in this view.
        """
        if self.state.batch_id is None or not paths:
            return False
        self.rescan_import_requested.emit(self.state.batch_id, list(paths))
        return True

    def show_case_history(self) -> bool:
        """Show the selected case's lifecycle history."""
        case = self.current_case()
        database = self.database
        if case is None or database is None:
            return False
        LifecycleHistoryDialog(
            case, scan_lifecycle.lifecycle_history(database, case.scan_id), self
        ).exec()
        return True

    def _after_lifecycle_action(self, scan_id: int) -> None:
        """Re-read the view after a lifecycle write, and tell the window."""
        self.refresh_queue()
        self.lifecycle_changed.emit(scan_id)

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


def _describe_marks(marks: Sequence[str]) -> str:
    """Render the symbols a group carries the way a person would say them.

    ``0-5`` is how recognition writes a doubly-marked position down, and it is
    what the ledger and the export keep. It is not what an operator reads: at
    a glance it is as easily a range, a hyphenated code or a minus sign. This
    says *0 and 5*, and changes nothing that is stored.
    """
    if not marks:
        return "nothing"
    if len(marks) == 1:
        return marks[0]
    return f"{', '.join(marks[:-1])} and {marks[-1]}"


_ISSUE_WORDING: dict[ConflictType, str] = {
    ConflictType.IDENTIFIER_BLANK: "No mark anywhere in this field",
    ConflictType.IDENTIFIER_INCOMPLETE: "This position is blank",
    ConflictType.IDENTIFIER_MULTIPLE: "More than one mark",
    ConflictType.IDENTIFIER_UNCERTAIN: "Too faint, or too close to call",
    ConflictType.IDENTIFIER_UNREADABLE: "Could not be measured",
    ConflictType.IDENTIFIER_LOW_CONFIDENCE: "Below the template's confidence floor",
    ConflictType.IDENTIFIER_DUPLICATE: "Another sheet has this student ID",
    ConflictType.SET_CODE_BLANK: "This position is blank",
    ConflictType.SET_CODE_MULTIPLE: "More than one mark",
    ConflictType.SET_CODE_UNCERTAIN: "Too faint, or too close to call",
    ConflictType.SET_CODE_UNREADABLE: "Could not be measured",
    ConflictType.SET_CODE_LOW_CONFIDENCE: "Below the template's confidence floor",
    ConflictType.SET_CODE_UNDEFINED: "Not one of this project's sets",
    ConflictType.MANUAL_OVERRIDE: "Confident reading overridden by an operator",
}
"""What each conflict means, in a reviewer's words rather than the taxonomy's.

The type's own :attr:`~omr_scanner.domain.review.ConflictType.label` names the
*category* - "Student ID multiple marks" - which is right for a queue column
and repeats the field name in a panel that has just said it. Anything not
listed falls back to the label, so a type added later is described rather than
blank."""


def _machine_summary_html(
    conflict: ConflictRecord, marks: Collection[str], field_value: str = ""
) -> str:
    """Render the four facts a reviewer needs before choosing.

    What position, what is wrong with it, what was on the paper, and which
    sheet. Everything else recognition recorded - the decision score, the
    per-bubble fill scores, the scan-quality measurements - is behind
    **Details**, because it answers a question nobody asks on most sheets and
    it was taking more of the panel than all four of these together.
    """
    rows: list[tuple[str, str]] = []
    if field_value:
        # The whole field, not only the position in dispute: a reviewer
        # deciding the third digit of a roll number is deciding it *as part of*
        # a number, and the other five are what tells them whether they have
        # the right script in front of them.
        rows.append(("Current field", field_value))
    rows.append(
        ("Issue", _ISSUE_WORDING.get(conflict.conflict_type, conflict.conflict_type.label))
    )
    if conflict.conflict_type.allows_value_correction:
        rows.append(("Detected", _describe_marks(sorted(marks))))
    rows.append(("Sheet", conflict.scan_name or "(unknown)"))
    if conflict.related_scan_ids:
        rows.append(("Also on", f"{len(conflict.related_scan_ids)} other sheet(s)"))

    body = "".join(
        f"<tr><td style='color:{Color.TEXT_TERTIARY};padding-right:10px;'>{name}</td>"
        f"<td><b>{value}</b></td></tr>"
        for name, value in rows
    )
    return (
        f"<div style='font-size:11pt;'><b>{conflict.field.describe()}</b></div>"
        f"<table style='margin-top:2px;'>{body}</table>"
    )


def _no_rectified_page_message(bundle: SheetBundle) -> str:
    """Say why a sheet has no normalised page to show.

    Three genuinely different situations, and a reviewer decides differently in
    each: a file that would not decode is a scanning problem, a page that would
    not register is a geometry problem, and a page that registered but produced
    no preview is a limitation of how it was read. Collapsing them into "no
    image" would hide the one thing this stage exists to surface.
    """
    if bundle.result is None:
        return (
            "This sheet could not be read.\n\n"
            + (bundle.error or "The file could not be decoded.")
        )
    if not bundle.registered:
        return (
            "This sheet has no normalised page, because it could not be "
            "aligned to the template.\n\n"
            "That is the conflict, not a display problem. Use the "
            "Original scan tab to see the page as it arrived."
        )
    return "No preview was produced for this sheet."


def _reason_or_other(stored: str) -> ReasonCode:
    """Return a stored reason code, falling back to ``OTHER``.

    A code written by a later build is not a reason to refuse to repeat a
    decision; it is a reason not to claim to know what it meant.
    """
    try:
        return ReasonCode(stored)
    except ValueError:
        return ReasonCode.OTHER


def _sheet_tooltip(
    conflict: ConflictRecord, queue: Sequence[ConflictRecord]
) -> str:
    """Name the sheet in full, and say how much of the queue belongs to it."""
    same = sum(1 for item in queue if item.scan_id == conflict.scan_id)
    return (
        f"{conflict.scan_name or '(unknown)'}\n"
        f"{same} conflict(s) from this sheet in the queue"
    )


def _issue_text(conflict: ConflictRecord) -> str:
    """Render one queue row's problem as a phrase rather than two fragments.

    ``Student ID - position 5 . More than one mark``. The field and the
    conflict type used to be two narrow columns, each eliding its own half of
    that sentence; joined, they fit and they read.

    A sheet-scope conflict is about the page rather than a position on it, so
    it is named by its type alone - "Registration failed" - without a "Sheet"
    prefix that would repeat the column beside it.
    """
    wording = _ISSUE_WORDING.get(conflict.conflict_type, conflict.conflict_type.label)
    if conflict.conflict_type.scope is ConflictScope.SHEET:
        return conflict.conflict_type.label
    return f"{conflict.field.describe()} · {wording}"


def _state_tooltip(conflict: ConflictRecord) -> str:
    """Explain a queue row's state in a sentence."""
    if conflict.state is ConflictState.OPEN and conflict.reversed_before:
        return (
            "Open again: somebody decided this and then reopened or undid the "
            "decision. The earlier one is still in the history."
        )
    if conflict.conflict_type.is_operator_override:
        if conflict.state is ConflictState.WITHDRAWN:
            return (
                "The operator override was undone or reopened; the machine's "
                "confident reading stands again. Kept in the history."
            )
        return (
            "An operator overrode a value the machine read confidently. "
            "Recognition raised no conflict here."
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


__all__ = [
    "UNKNOWN_POSITION",
    "FieldEditPlan",
    "ResolvePage",
    "ResolvePageState",
    "override_warning_text",
]
