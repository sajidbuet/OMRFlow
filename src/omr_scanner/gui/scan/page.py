"""The Scan workflow page: import sheets, recognise them, review and export.

Purpose:
    Assemble Phase 3's services into the stage a user actually works in - load a
    template, add scans, process them, look at what was read, and export it.

Responsibilities:
    * Own the scan list and its state, the selected scan's preview and the
      results panel.
    * Turn user actions into service calls, off the GUI thread when they are
      slow (:mod:`omr_scanner.gui.scan.worker`).
    * Keep every long operation cancellable and every failure visible.

What does NOT belong here:
    * Recognition, naming, copying or CSV writing. All four are services, and
      this page must stay a thin shell over them - the rule
      ``docs/ARCHITECTURE.md`` states as "domain logic must not live inside GUI
      event handlers".
    * ``cv2``, ``numpy``, ``omr_scanner.imaging`` or ``omr_scanner.recognition``
      imports; the services layer hands this page plain strings, floats and
      bytes precisely so that it never needs them.

Testability:
    Every command is split the same way the main window splits its own: a
    ``_prompt_*`` method owns the modal dialog and contains no logic, and the
    method beside it (:meth:`load_template_from`, :meth:`add_scan_paths`,
    :meth:`export_csv_to`) does the work. GUI tests drive the second group, so
    no test has to interact with a native file dialog.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.config.processing import ProcessingSettings, detected_cpu_count
from omr_scanner.domain.review import ReviewCounts
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.gui.scan.table_model import (
    STATUS_COLORS,
    STATUS_LABELS,
    ScanEntry,
    ScanTableModel,
)
from omr_scanner.gui.scan.worker import BatchWorker, PreviewWorker
from omr_scanner.gui.theme import TEMPLATE_DESIGNER_STYLESHEET
from omr_scanner.services import (
    BatchIdentity,
    BatchOptions,
    BatchProgress,
    BatchRecorder,
    BatchReport,
    BatchState,
    BatchStatus,
    BatchSummary,
    DiagnosticsOptions,
    FilenameAllocator,
    ProcessedScan,
    ProgressSnapshot,
    RecognitionOptions,
    RecognitionOutcome,
    ScanResult,
    active_template_is_missing,
    check_compatibility,
    collect_scan_files,
    completed_results,
    count_conflicts,
    create_batch,
    export_scan_results,
    failed_scans,
    finalise_batch,
    format_count,
    format_duration,
    format_rate,
    load_summary,
    load_template,
    mark_cancelled,
    mark_queued,
    resolve_active_template,
    resumable_scans,
    scan_ids_by_path,
    scan_paths,
    set_batch_status,
    sheet_resolutions,
    sync_conflicts,
    sync_duplicate_identifiers,
)
from omr_scanner.services.recognition_models import utc_timestamp

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.evaluation.benchmark import BenchmarkReport
    from omr_scanner.evaluation.session import BenchmarkComparison, BenchmarkSession
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import ProjectDatabase, ProjectSession, SheetResolution

_LOGGER = logging.getLogger(__name__)

CONTROL_PANEL_WIDTH = 290
RESULTS_PANEL_WIDTH = 290
PREVIEW_INITIAL_HEIGHT = 720
SCAN_TABLE_INITIAL_HEIGHT = 220
"""Initial split between the preview and the scan list, in pixels.

The preview is the thing being reviewed, so it gets most of the height; the
list needs about six rows to be useful. Both remain freely draggable."""

PREVIEW_CACHE_SIZE = 6
"""How many rectified previews are kept in memory at once.

Enough that stepping back and forth through a few sheets never waits, small
enough that a batch of hundreds cannot fill memory - the scan list holds
results, never images."""

TOOLBAR_ICON_SIZE_PX = 18

PROGRESS_REFRESH_MS = 200
"""How often the progress panel and the scan list are repainted, in milliseconds.

Five refreshes a second: fast enough that the bar looks live and the Cancel
button feels immediate, slow enough that a machine finishing fifty sheets a
second asks Qt to repaint five times rather than fifty. The counters behind it
update on *every* completed sheet - only the drawing is throttled."""

WORKER_SHUTDOWN_TIMEOUT_MS = 30_000
"""How long the page waits for a cancelled batch to finish when it closes.

Long enough for every busy worker to finish the sheet it is holding - a cancel
does not interrupt a sheet mid-warp - so the worker pool is always torn down
before the application exits and no stray process outlives the window."""

FILTER_ALL = "All"
FILTER_COMPLETED = "Completed"
FILTER_REVIEW = "Needs review"
FILTER_FAILED = "Failed"
FILTER_PENDING = "Not processed"

FILTER_OUTCOMES: dict[str, frozenset[str]] = {
    FILTER_COMPLETED: frozenset({RecognitionOutcome.COMPLETE.value}),
    FILTER_REVIEW: frozenset({RecognitionOutcome.REVIEW.value}),
    FILTER_FAILED: frozenset(
        {
            RecognitionOutcome.REGISTRATION_FAILED.value,
            RecognitionOutcome.ERROR.value,
        }
    ),
    FILTER_PENDING: frozenset({RecognitionOutcome.PENDING.value}),
}
"""Which recognition outcomes each filter shows.

``FILTER_ALL`` is deliberately absent: "no filter" is the absence of an entry
rather than a set that happens to contain everything, so adding an outcome
later cannot accidentally leave it out of the unfiltered view."""

MARK_STATUS_LABELS: dict[str, str] = {
    "resolved": "",
    "blank": "blank",
    "multiple": "multiple",
    "uncertain": "uncertain",
    "unreadable": "unreadable",
}


@dataclass
class ScanPageState:
    """Everything the page knows that is not a widget.

    Kept as one object so a test can inspect the page's state without walking
    the widget tree, and so the widget code below reads as presentation only.

    Attributes:
        template: The loaded template, or ``None``.
        template_path: Where it came from.
        entries: The scan list, in processing order.
        output_dir: Where renamed copies are written, or ``None``.
        rename_enabled: Whether recognised sheets are copied under new names.
        processing: How many CPU workers a run may use. Set from the application
            settings by the main window; the default is what a page built
            without one uses.
        benchmark: The labelled dataset this page is scoring against, or
            ``None`` in ordinary use. Benchmark mode changes what happens *after*
            a run, never how the run itself works - the point is to measure the
            pipeline users actually get.
    """

    template: OmrTemplate | None = None
    template_path: Path | None = None
    entries: list[ScanEntry] = field(default_factory=list)
    output_dir: Path | None = None
    rename_enabled: bool = False
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)
    benchmark: BenchmarkSession | None = None
    batch_id: str | None = None
    """The stored batch this scan list belongs to (Phase 5).

    ``None`` when there is no project open, which is a supported way to run:
    recognition, renaming and CSV export all work exactly as before, they are
    simply not durable. Resume and retry need a project, and say so."""


class ScanPage(WorkflowPage):
    """Import scanned sheets, recognise them against a template, and export.

    Signals:
        batch_finished: ``BatchReport`` when a run ends. GUI tests and the
            qtguitesting scripts wait on this instead of sleeping.
        review_requested: ``str`` batch id when the operator asks to review
            that batch's conflicts. The main window owns cross-page
            navigation, so this page asks rather than reaching into another
            stage.
        scan_selected: ``int`` row index whenever the shown scan changes.
        benchmark_finished: ``BenchmarkReport`` when a run in benchmark mode has
            been scored. Emitted before the results dialog opens, so a test can
            read the numbers without a dialog on screen.

    Args:
        spec: The "scan" workflow stage description.
        parent: Optional Qt parent.
    """

    batch_finished = Signal(object)
    scan_selected = Signal(int)
    benchmark_finished = Signal(object)
    review_requested = Signal(str)

    processing_changed = Signal(bool)
    """Emitted when a batch starts or stops running.

    Carries :attr:`is_processing`. Emitted only on a *change*, from
    ``_refresh_controls`` - which already runs at exactly the four moments
    that matter (a run starting, finishing, failing, and being cancelled) -
    so the application's status footer can say "Processing" without polling a
    boolean on a timer, and without this page knowing the footer exists.
    """

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setObjectName("scanPage")
        self.setStyleSheet(TEMPLATE_DESIGNER_STYLESHEET)

        self.state = ScanPageState()
        self._session: ProjectSession | None = None
        self._worker: BatchWorker | None = None
        self._announced_processing = False
        """Last value :attr:`processing_changed` reported - see
        ``_announce_processing``."""
        self._preview_worker: PreviewWorker | None = None
        self._preview_cache: OrderedDict[Path, ScanResult] = OrderedDict()
        self._allocator = FilenameAllocator(None)
        self._suppress_selection = False

        # Row lookup by source path. A ten-thousand-sheet batch finishes ten
        # sheets a second; searching the list for each one would be quadratic
        # and would spend more time finding rows than reading pages.
        self._row_by_path: dict[Path, int] = {}
        # Rows whose contents have changed but which have not been redrawn
        # yet. Flushed by the refresh timer, so the table is repainted a few
        # times a second rather than once per completed sheet.
        self._dirty_rows: set[int] = set()
        self._last_snapshot = ProgressSnapshot()
        self._final_report: BatchReport | None = None
        self._batch_started_at = ""

        # The last benchmark this page produced, kept so the banner's "Show
        # Results" can reopen it without re-running anything.
        self.last_benchmark: BenchmarkReport | None = None
        self.last_comparison: BenchmarkComparison | None = None
        self.benchmark_auto_show = True
        """Whether scoring a run opens the results dialog. GUI tests turn this
        off and read :attr:`last_benchmark` instead of dismissing a modal."""

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(PROGRESS_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._refresh_progress)

        # Backs `scan_table`. Reads `state.entries` directly rather than
        # duplicating each cell into a `QTableWidgetItem` - see
        # `omr_scanner.gui.scan.table_model` for why that matters at
        # 100,000-sheet scale.
        self._scan_model = ScanTableModel(lambda: self.state.entries, self)
        # What the status filter combo currently restricts the list to, kept
        # alongside the widget's own selection so a single finished sheet can
        # be re-checked against it without rescanning every other row (see
        # `_apply_status_filter_to_rows`).
        self._active_filter: frozenset[str] | None = None
        self._visible_count = 0

        self.body.addWidget(self._build_benchmark_banner())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        # A collapsible pane lets a user drag the divider until a column's
        # controls are squeezed to nothing - the same unreadable-button defect
        # this pass exists to fix, just reachable by hand instead of by a short
        # window. Every pane keeps at least its laid-out size.
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_centre())
        splitter.addWidget(self._build_results_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([CONTROL_PANEL_WIDTH, 900, RESULTS_PANEL_WIDTH])
        self.body.addWidget(splitter, stretch=1)

        self._refresh_controls()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_controls(self) -> QWidget:
        """Build the left-hand control column, in a scroll area.

        Four group boxes stacked without one need roughly 900 logical pixels
        to lay out at their natural size - the Processing group alone, with
        seven action buttons, a status line and the progress readout, needs
        about 450 of them. A window shorter than that (a laptop at 1366x768,
        a restored rather than maximised window, or higher Windows display
        scaling asking for the same layout in fewer logical pixels) leaves
        Qt nothing to do but compress every widget in the column below its
        own size hint, which is what made button text and icons overlap.

        The scroll area breaks that dependency: the column inside it is
        always laid out at its full, uncompressed size, and a short window
        gets a scrollbar instead of a squeezed button. ``setWidgetResizable``
        keeps the column's *width* tracking the viewport - the buttons still
        fill the same 290-pixel column as before - while its height is free
        to exceed the viewport and scroll.
        """
        panel = QWidget()
        panel.setObjectName("scanControlPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(8)

        # -- Template --------------------------------------------------
        template_box = QGroupBox("Template")
        template_layout = QVBoxLayout(template_box)
        self.load_template_button = QPushButton(load_icon("folder-open"), "Load Template...")
        self.load_template_button.setObjectName("loadTemplateButton")
        self.load_template_button.setToolTip(
            "Open the .omrt template these sheets were printed from"
        )
        self.load_template_button.clicked.connect(self._prompt_load_template)
        template_layout.addWidget(self.load_template_button)

        self.template_name_label = QLabel("No template loaded")
        self.template_name_label.setObjectName("templateNameLabel")
        self.template_name_label.setWordWrap(True)
        template_layout.addWidget(self.template_name_label)

        self.calibration_warning_label = QLabel("")
        self.calibration_warning_label.setObjectName("calibrationWarningLabel")
        self.calibration_warning_label.setWordWrap(True)
        self.calibration_warning_label.setStyleSheet("color: #9A6A00;")
        template_layout.addWidget(self.calibration_warning_label)
        layout.addWidget(template_box)

        # -- Scans -----------------------------------------------------
        scans_box = QGroupBox("Scans")
        scans_layout = QVBoxLayout(scans_box)
        self.add_scans_button = QPushButton(load_icon("file-plus"), "Add Scan(s)...")
        self.add_scans_button.setObjectName("addScansButton")
        self.add_scans_button.clicked.connect(self._prompt_add_scans)
        scans_layout.addWidget(self.add_scans_button)

        self.add_folder_button = QPushButton(load_icon("folder-open"), "Add Folder...")
        self.add_folder_button.setObjectName("addFolderButton")
        self.add_folder_button.clicked.connect(self._prompt_add_folder)
        scans_layout.addWidget(self.add_folder_button)

        self.clear_scans_button = QPushButton(load_icon("trash"), "Clear Scan List")
        self.clear_scans_button.setObjectName("clearScansButton")
        self.clear_scans_button.clicked.connect(self.clear_scans)
        scans_layout.addWidget(self.clear_scans_button)
        layout.addWidget(scans_box)

        # -- Processing ------------------------------------------------
        process_box = QGroupBox("Processing")
        process_layout = QVBoxLayout(process_box)

        # What the current setting means for the list as it stands, beside the
        # button that will act on it - so the answer to "why is this taking so
        # long" is on screen before the run starts, not buried in Settings.
        self.workers_label = QLabel("")
        self.workers_label.setObjectName("workersLabel")
        self.workers_label.setWordWrap(True)
        self.workers_label.setToolTip(
            "How many sheets are read at the same time. Change this in "
            "File > Settings > Processing."
        )
        process_layout.addWidget(self.workers_label)
        process_layout.addSpacing(4)

        self.process_all_button = QPushButton(load_icon("scan-line"), "Process All")
        self.process_all_button.setObjectName("processAllButton")
        self.process_all_button.clicked.connect(self.process_all)
        process_layout.addWidget(self.process_all_button)

        self.process_selected_button = QPushButton(load_icon("scan"), "Process Selected")
        self.process_selected_button.setObjectName("processSelectedButton")
        self.process_selected_button.clicked.connect(self.process_selected)
        process_layout.addWidget(self.process_selected_button)

        self.resume_button = QPushButton(load_icon("redo-2"), "Resume Batch")
        self.resume_button.setObjectName("resumeBatchButton")
        self.resume_button.setToolTip(
            "Continue the stored batch: process only the scans that were never "
            "finished. Scans already read are kept, not read again."
        )
        self.resume_button.clicked.connect(self.resume_batch)
        process_layout.addWidget(self.resume_button)

        self.retry_failed_button = QPushButton(load_icon("rotate-ccw"), "Retry Failed")
        self.retry_failed_button.setObjectName("retryFailedButton")
        self.retry_failed_button.setToolTip(
            "Process the scans that failed, and only those. Successful results "
            "are left exactly as they are."
        )
        self.retry_failed_button.clicked.connect(self.retry_failed)
        process_layout.addWidget(self.retry_failed_button)

        self.reprocess_button = QPushButton(load_icon("rotate-ccw"), "Reprocess")
        self.reprocess_button.setObjectName("reprocessButton")
        self.reprocess_button.setToolTip("Process every scan again, discarding previous results")
        self.reprocess_button.clicked.connect(self.reprocess_all)
        process_layout.addWidget(self.reprocess_button)

        self.cancel_button = QPushButton("Cancel Processing")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self.cancel_processing)
        process_layout.addWidget(self.cancel_button)

        self.review_button = QPushButton(load_icon("list-checks"), "Review Conflicts")
        self.review_button.setObjectName("reviewConflictsButton")
        self.review_button.setToolTip(
            "Open everything this batch could not decide, with the evidence "
            "behind it, for human review."
        )
        self.review_button.clicked.connect(self.request_review)
        process_layout.addWidget(self.review_button)

        # A visible gap between the action stack and the status text below it,
        # wider than the buttons' own spacing - so the two read as separate
        # groups rather than one more item in the button list.
        process_layout.addSpacing(6)

        self.conflict_label = QLabel("")
        self.conflict_label.setObjectName("batchConflictLabel")
        self.conflict_label.setWordWrap(True)
        process_layout.addWidget(self.conflict_label)

        self.batch_state_label = QLabel("")
        self.batch_state_label.setObjectName("batchStateLabel")
        self.batch_state_label.setWordWrap(True)
        self.batch_state_label.setToolTip(
            "The stored state of this batch. Recognition results are written to "
            "the project database as they finish, so an interrupted run can be "
            "resumed instead of restarted."
        )
        process_layout.addWidget(self.batch_state_label)

        process_layout.addWidget(self._build_progress_panel())
        layout.addWidget(process_box)

        # -- Output ----------------------------------------------------
        output_box = QGroupBox("Output")
        output_layout = QVBoxLayout(output_box)
        # Split across two lines rather than shortened: the full sentence is
        # what makes the option unambiguous, and a clipped label is worse than
        # a taller control.
        self.rename_checkbox = QCheckBox("Rename processed scans\nusing detected roll number")
        self.rename_checkbox.setObjectName("renameScansCheckBox")
        self.rename_checkbox.setToolTip(
            "Copy each recognised sheet into the output folder, named after its roll "
            "number. Originals are never moved or overwritten."
        )
        self.rename_checkbox.toggled.connect(self._on_rename_toggled)
        output_layout.addWidget(self.rename_checkbox)

        self.output_folder_button = QPushButton(load_icon("folder-open"), "Output Folder...")
        self.output_folder_button.setObjectName("outputFolderButton")
        self.output_folder_button.clicked.connect(self._prompt_output_folder)
        output_layout.addWidget(self.output_folder_button)

        self.output_folder_label = QLabel("No output folder selected")
        self.output_folder_label.setObjectName("outputFolderLabel")
        self.output_folder_label.setWordWrap(True)
        output_layout.addWidget(self.output_folder_label)

        self.export_csv_button = QPushButton(load_icon("save"), "Export CSV...")
        self.export_csv_button.setObjectName("exportCsvButton")
        self.export_csv_button.clicked.connect(self._prompt_export_csv)
        output_layout.addWidget(self.export_csv_button)
        layout.addWidget(output_box)

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("scanControlScrollArea")
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMaximumWidth(CONTROL_PANEL_WIDTH + 60)
        return scroll

    def _build_benchmark_banner(self) -> QWidget:
        """Build the strip that says this page is scoring a labelled dataset.

        Hidden in ordinary use. Visible and unmissable in benchmark mode,
        because the one thing that must never happen is somebody processing a
        real examination while the page is quietly comparing it against
        somebody else's answer key.
        """
        banner = QWidget()
        banner.setObjectName("benchmarkBanner")
        banner.setVisible(False)
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(8, 4, 8, 4)

        self.benchmark_label = QLabel("")
        self.benchmark_label.setObjectName("benchmarkBannerLabel")
        self.benchmark_label.setWordWrap(True)
        layout.addWidget(self.benchmark_label, stretch=1)

        self.benchmark_results_button = QPushButton("Show Results")
        self.benchmark_results_button.setObjectName("showBenchmarkResultsButton")
        self.benchmark_results_button.setEnabled(False)
        self.benchmark_results_button.clicked.connect(self.show_benchmark_results)
        layout.addWidget(self.benchmark_results_button)

        self.exit_benchmark_button = QPushButton("Exit Benchmark Mode")
        self.exit_benchmark_button.setObjectName("exitBenchmarkButton")
        self.exit_benchmark_button.clicked.connect(self.exit_benchmark_mode)
        layout.addWidget(self.exit_benchmark_button)

        self.benchmark_banner = banner
        return banner

    def _build_progress_panel(self) -> QWidget:
        """Build the batch progress readout.

        Five short lines rather than a table of statistics: this sits in a
        290-pixel column beside the sheet a user is reading, and the questions
        it has to answer at a glance are "how far", "how long", "how fast" and
        "how much went wrong". Every line has a stable ``objectName`` so the
        GUI tests can read it without walking the widget tree.

        Nothing here is per-scan. A batch of ten thousand sheets produces
        exactly these widgets, which is the whole point: the alternative -
        a row, a bar or a log line per sheet - is what makes large-batch
        interfaces collapse.
        """
        panel = QWidget()
        panel.setObjectName("batchProgressPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(2)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progressBar")
        # Driven by completed/total directly - no percentage conversion, so
        # the widget can never round 9,999 of 10,000 up to a finished-looking
        # bar. The format string carries the percentage instead.
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("progressLabel")
        self.progress_label.setWordWrap(True)
        layout.addWidget(self.progress_label)

        self.progress_counts_label = QLabel("")
        self.progress_counts_label.setObjectName("progressCountsLabel")
        self.progress_counts_label.setWordWrap(True)
        layout.addWidget(self.progress_counts_label)

        self.progress_timing_label = QLabel("")
        self.progress_timing_label.setObjectName("progressTimingLabel")
        self.progress_timing_label.setWordWrap(True)
        layout.addWidget(self.progress_timing_label)

        self.progress_rate_label = QLabel("")
        self.progress_rate_label.setObjectName("progressRateLabel")
        self.progress_rate_label.setWordWrap(True)
        layout.addWidget(self.progress_rate_label)

        self.progress_outcome_label = QLabel("")
        self.progress_outcome_label.setObjectName("progressOutcomeLabel")
        self.progress_outcome_label.setWordWrap(True)
        # Words, not colour alone: "Failed 13" has to be readable to someone
        # who cannot distinguish the tint behind it.
        self.progress_outcome_label.setToolTip(
            "How the finished sheets ended: read cleanly, needing review, or unreadable."
        )
        layout.addWidget(self.progress_outcome_label)
        return panel

    def _build_centre(self) -> QWidget:
        """Build the preview and the scan list beneath it."""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        layout.addWidget(self._build_preview_toolbar())

        self.preview = ScanPreviewView()
        self.preview.setMinimumHeight(240)

        self.scan_table = QTableView()
        self.scan_table.setObjectName("scanTable")
        self.scan_table.setModel(self._scan_model)
        self.scan_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.scan_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.scan_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.scan_table.verticalHeader().setVisible(False)
        header = self.scan_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        # `setModel` creates the selection model, so this can only be wired
        # afterwards - there is nothing to connect to before it exists.
        self.scan_table.selectionModel().selectionChanged.connect(
            lambda *_args: self._on_table_selection_changed()
        )
        self.scan_table.setMinimumHeight(140)

        # Filtering hides rows rather than rebuilding the table: the row index
        # *is* the index into `state.entries` everywhere else on this page, and
        # a filtered rebuild would break that correspondence in a way that is
        # invisible until someone selects the wrong scan.
        filter_row = QWidget()
        filter_layout = QHBoxLayout(filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.addWidget(QLabel("Show:"))
        self.status_filter_combo = QComboBox()
        self.status_filter_combo.setObjectName("scanStatusFilterCombo")
        self.status_filter_combo.addItems(
            [
                FILTER_ALL,
                FILTER_COMPLETED,
                FILTER_REVIEW,
                FILTER_FAILED,
                FILTER_PENDING,
            ]
        )
        self.status_filter_combo.setToolTip(
            "Narrow the list to one kind of outcome. Filtering never changes "
            "what was recognised, only what is shown."
        )
        self.status_filter_combo.currentIndexChanged.connect(self._apply_status_filter)
        filter_layout.addWidget(self.status_filter_combo, stretch=1)
        self.filter_count_label = QLabel("")
        self.filter_count_label.setObjectName("scanFilterCountLabel")
        filter_layout.addWidget(self.filter_count_label)

        table_column = QWidget()
        table_layout = QVBoxLayout(table_column)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(2)
        table_layout.addWidget(filter_row)
        table_layout.addWidget(self.scan_table, stretch=1)

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(self.preview)
        vertical.addWidget(table_column)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 1)
        # Stretch factors only govern how extra space is *shared out on resize*;
        # the initial split comes from the size hints, which give the table far
        # more than it needs and leave the preview a letterbox. Setting the
        # sizes explicitly is what actually decides the first layout.
        vertical.setSizes([PREVIEW_INITIAL_HEIGHT, SCAN_TABLE_INITIAL_HEIGHT])
        layout.addWidget(vertical, stretch=1)
        return container

    def _build_preview_toolbar(self) -> QToolBar:
        """Build the zoom/overlay toolbar above the preview."""
        toolbar = QToolBar("Preview")
        toolbar.setObjectName("scanPreviewToolbar")
        toolbar.setIconSize(QSize(TOOLBAR_ICON_SIZE_PX, TOOLBAR_ICON_SIZE_PX))
        toolbar.setMovable(False)

        self.previous_action = QAction(load_icon("undo-2"), "Previous", self)
        self.previous_action.setObjectName("previousScanButton")
        self.previous_action.setToolTip("Show the previous scan")
        self.previous_action.triggered.connect(self.select_previous)
        toolbar.addAction(self.previous_action)

        self.next_action = QAction(load_icon("redo-2"), "Next", self)
        self.next_action.setObjectName("nextScanButton")
        self.next_action.setToolTip("Show the next scan")
        self.next_action.triggered.connect(self.select_next)
        toolbar.addAction(self.next_action)
        toolbar.addSeparator()

        self.zoom_out_action = QAction(load_icon("zoom-out"), "Zoom Out", self)
        self.zoom_out_action.setObjectName("zoomOutButton")
        self.zoom_out_action.setToolTip("Zoom out")
        self.zoom_out_action.triggered.connect(lambda: self.preview.zoom_out())
        toolbar.addAction(self.zoom_out_action)

        self.zoom_in_action = QAction(load_icon("zoom-in"), "Zoom In", self)
        self.zoom_in_action.setObjectName("zoomInButton")
        self.zoom_in_action.setToolTip("Zoom in")
        self.zoom_in_action.triggered.connect(lambda: self.preview.zoom_in())
        toolbar.addAction(self.zoom_in_action)

        self.fit_action = QAction(load_icon("maximize"), "Fit", self)
        self.fit_action.setObjectName("fitButton")
        self.fit_action.setToolTip("Fit the whole sheet in the view")
        self.fit_action.triggered.connect(lambda: self.preview.fit_to_window())
        toolbar.addAction(self.fit_action)

        self.actual_size_action = QAction(load_icon("scan"), "100%", self)
        self.actual_size_action.setObjectName("actualSizeButton")
        self.actual_size_action.setToolTip("Show the sheet at 100%")
        self.actual_size_action.triggered.connect(lambda: self.preview.zoom_to_actual_size())
        toolbar.addAction(self.actual_size_action)
        toolbar.addSeparator()

        self.zones_checkbox = QCheckBox("Regions")
        self.zones_checkbox.setObjectName("overlayZonesCheckBox")
        self.zones_checkbox.setChecked(True)
        self.zones_checkbox.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.zones_checkbox)

        self.bubbles_checkbox = QCheckBox("Marks")
        self.bubbles_checkbox.setObjectName("overlayBubblesCheckBox")
        self.bubbles_checkbox.setChecked(True)
        self.bubbles_checkbox.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.bubbles_checkbox)

        self.empty_checkbox = QCheckBox("All bubbles")
        self.empty_checkbox.setObjectName("overlayEmptyCheckBox")
        self.empty_checkbox.setToolTip("Also outline the bubbles that were measured as empty")
        self.empty_checkbox.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.empty_checkbox)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.preview_status_label = QLabel("")
        self.preview_status_label.setObjectName("previewStatusLabel")
        toolbar.addWidget(self.preview_status_label)
        return toolbar

    def _build_results_panel(self) -> QWidget:
        """Build the right-hand recognised-values panel."""
        panel = QWidget()
        panel.setObjectName("scanResultsPanel")
        panel.setMaximumWidth(RESULTS_PANEL_WIDTH + 120)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(6)

        heading = QLabel("Recognised values")
        heading_font = heading.font()
        heading_font.setBold(True)
        heading.setFont(heading_font)
        layout.addWidget(heading)

        self.result_summary_label = QLabel("No scan selected")
        self.result_summary_label.setObjectName("resultSummaryLabel")
        self.result_summary_label.setWordWrap(True)
        layout.addWidget(self.result_summary_label)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separator)

        self.fields_table = QTableWidget(0, 3)
        self.fields_table.setObjectName("resultFieldsTable")
        self.fields_table.setHorizontalHeaderLabels(["Field", "Value", "Status"])
        self.fields_table.verticalHeader().setVisible(False)
        self.fields_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.fields_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.fields_table.setMaximumHeight(140)
        layout.addWidget(self.fields_table)

        self.answers_table = QTableWidget(0, 3)
        self.answers_table.setObjectName("resultAnswersTable")
        self.answers_table.setHorizontalHeaderLabels(["Q", "Answer", "Status"])
        self.answers_table.verticalHeader().setVisible(False)
        self.answers_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.answers_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.answers_table, stretch=1)
        return panel

    # ------------------------------------------------------------------
    # WorkflowPage hook
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt the open project: file dialog defaults, and durable batches.

        A batch belongs to a project, so switching projects abandons the
        current batch id rather than carrying it into a database that has never
        heard of it. Nothing is deleted - the previous project's rows stay
        exactly where they were, ready to be resumed when it is reopened.
        """
        self._session = session
        self.state.batch_id = None
        self._adopt_project_template(session)
        self._refresh_batch_state_label()
        self._refresh_controls()

    def _adopt_project_template(self, session: ProjectSession | None) -> None:
        """Take the template from the project rather than asking again.

        Scan kept its own template path, so a template already chosen on the
        Template screen and used on Calibrate had to be browsed for a third
        time here. It is consumed from the session - never re-discovered, the
        main window settles that once - and reloaded only when it is genuinely
        a different file, so walking between screens does not reparse it.
        """
        if session is None:
            self._clear_template()
            return
        active = resolve_active_template(session.project)
        if active is None:
            # Nothing to adopt. What is already loaded is dropped when it came
            # from the project that was open a moment ago, or when it is the
            # file the project names and that file has gone - otherwise these
            # sheets would silently be read against another project's geometry.
            loaded = self.state.template_path
            stale = loaded is not None and session.project.layout.root not in loaded.parents
            if stale or active_template_is_missing(session.project):
                self._clear_template()
            return
        if self.state.template_path != active or self.state.template is None:
            self.load_template_from(active)

    def _clear_template(self) -> None:
        """Forget the loaded template, because the open project does not name it."""
        self.state.template = None
        self.state.template_path = None

    # ------------------------------------------------------------------
    # Durable batches (Phase 5)
    # ------------------------------------------------------------------
    @property
    def database(self) -> ProjectDatabase | None:
        """The open project's database, or ``None`` when no project is open."""
        return self._session.database if self._session is not None else None

    def _batch_identity(self) -> BatchIdentity | None:
        """Capture what the next run would be bound to."""
        if self.state.template is None:
            return None
        return BatchIdentity.of(self.state.template, self.state.template_path)

    def _batch_settings(self) -> dict[str, object]:
        """The run configuration worth recording, as plain JSON-safe data."""
        return {
            "rename_with_identifier": self.state.rename_enabled,
            "output_dir": str(self.state.output_dir) if self.state.output_dir else "",
            "processing_mode": self.state.processing.mode.value,
            "diagnostics": self.state.processing.writes_diagnostics,
        }

    def _ensure_batch(self, paths: Sequence[Path]) -> str | None:
        """Return the batch id to record this run under, creating one if needed.

        A batch is registered the first time a run starts against the current
        scan list, not when scans are added: enumerating a folder the operator
        then changes their mind about should not leave a row behind.
        """
        database = self.database
        identity = self._batch_identity()
        if database is None or identity is None:
            return None
        if self.state.batch_id is not None:
            return self.state.batch_id
        try:
            batch_id = create_batch(
                database,
                [entry.path for entry in self.state.entries] or list(paths),
                identity=identity,
                source_folder=paths[0].parent if paths else None,
                settings=self._batch_settings(),
            )
        except OMRScannerError:
            # Losing durability must not lose the run: the batch still
            # processes, exports and renames, it simply cannot be resumed. The
            # label says so rather than a dialog interrupting the operator.
            _LOGGER.exception("Could not register a batch; this run will not be resumable")
            return None
        self.state.batch_id = batch_id
        return batch_id

    def adopt_batch(self, batch_id: str) -> bool:
        """Load a stored batch into the scan list, results and all.

        Args:
            batch_id: The batch to reopen.

        Returns:
            Whether it was found and adopted.

        This is what makes a batch reopenable rather than merely resumable:
        the scans come back in their stored order and the results already
        produced come back with them, so a CSV exported after reopening covers
        the whole batch and not just what this session read.
        """
        database = self.database
        if database is None:
            return False
        summary = load_summary(database, batch_id)
        if summary is None:
            return False

        stored = {result.source_path: result for result in completed_results(database, batch_id)}
        paths = scan_paths(database, batch_id)
        self.state.entries = [
            ScanEntry(
                path=path,
                processed=(
                    ProcessedScan(result=stored[path]) if path in stored else None
                ),
            )
            for path in paths
        ]
        self.state.batch_id = batch_id
        self._preview_cache.clear()
        self._allocator = FilenameAllocator(self.state.output_dir)
        self._rebuild_scan_table()
        self._refresh_batch_state_label()
        self._refresh_controls()
        _LOGGER.info(
            "Reopened batch %s: %s", batch_id, summary.resume_label
        )
        return True

    def batch_summary(self) -> BatchSummary | None:
        """The stored counts for the current batch, or ``None``."""
        database = self.database
        if database is None or self.state.batch_id is None:
            return None
        return load_summary(database, self.state.batch_id)

    def _refresh_batch_state_label(self) -> None:
        """Say what is stored for this batch, in one line."""
        summary = self.batch_summary()
        if summary is None:
            self.batch_state_label.setText(
                "" if self._session is not None else "No project open - this run will not be saved."
            )
            return
        self.batch_state_label.setText(
            f"Batch {summary.batch_id[:8]} - {summary.resume_label} "
            f"({summary.completed} ok, {summary.warning} review, {summary.failed} failed)"
        )

    # ------------------------------------------------------------------
    # Processing settings
    # ------------------------------------------------------------------
    def set_processing_settings(self, processing: ProcessingSettings) -> None:
        """Adopt the application's processing preference.

        Called by the main window at start-up and whenever the user accepts the
        Settings dialog. A run already under way keeps the worker count it
        started with; changing the setting mid-batch and having half the sheets
        read differently would make a run impossible to reason about.
        """
        self.state.processing = processing
        self._refresh_worker_label()

    def _engine_options(self) -> RecognitionOptions:
        """Build the engine options a run should use.

        The page owns no recognition settings of its own: it translates the
        application's stored preferences into the options object the engine
        takes, and nothing else. Previews are off because a batch never shows
        one - the selected sheet is re-rendered on demand.
        """
        processing = self.state.processing
        writes_diagnostics = processing.writes_diagnostics
        return RecognitionOptions(
            with_preview=False,
            # The per-bubble evidence is roughly 90% of a result's memory - on
            # a 100-question sheet, five hundred records against a hundred
            # answers - and the page never reads it: the overlay comes from the
            # preview worker's own fresh result, and the CSV exports values.
            # Over ten thousand sheets that is the difference between about
            # 600 MB and about 60 MB of retained results. Diagnostics *do* need
            # it, so it is kept exactly when something will use it.
            keep_bubble_measurements=writes_diagnostics,
            diagnostics=DiagnosticsOptions(
                enabled=writes_diagnostics,
                directory=processing.diagnostics_dir,
            ),
        )

    def planned_worker_count(self, item_count: int | None = None) -> int:
        """How many workers a run over ``item_count`` scans would use now.

        Args:
            item_count: Scans in the run. Defaults to the whole list.

        Returns:
            The same number the batch processor will arrive at, because it comes
            from the same function.
        """
        count = len(self.state.entries) if item_count is None else item_count
        return self.state.processing.resolve_worker_count(count)

    def _refresh_worker_label(self) -> None:
        """Say what the next run will do, in scans and workers."""
        total = len(self.state.entries)
        if total == 0:
            self.workers_label.setText(
                f"{detected_cpu_count()} CPU threads detected - no scans added yet"
            )
            return
        workers = self.planned_worker_count(total)
        sheets = "scan" if total == 1 else "scans"
        self.workers_label.setText(
            f"{total} {sheets} - "
            f"{workers} parallel worker{'' if workers == 1 else 's'}"
        )

    # ------------------------------------------------------------------
    # Template
    # ------------------------------------------------------------------
    def _prompt_load_template(self) -> None:
        """Ask for a template file, then load it."""
        start = self._default_template_dir()
        path_str, _filter = QFileDialog.getOpenFileName(
            self, "Load template", str(start), "OMRFlow templates (*.omrt)"
        )
        if path_str:
            self.load_template_from(Path(path_str))

    def load_template_from(self, path: Path) -> bool:
        """Load the template at ``path``.

        Args:
            path: The ``.omrt`` document.

        Returns:
            ``True`` when it loaded. A failure is reported to the user and
            logged; it never propagates into Qt's event loop.
        """
        try:
            template = load_template(path)
        except OMRScannerError as exc:
            report_error(self, exc, context="Load template")
            return False

        self.state.template = template
        self.state.template_path = path
        self._preview_cache.clear()
        questions = sum(
            zone.field.question_count
            for zone in template.zones
            if hasattr(zone.field, "question_count")
        )
        self.template_name_label.setText(
            f"<b>{template.name}</b><br>{path.name}<br>"
            f"{len(template.zones)} region(s), {questions} question(s)"
        )
        self.calibration_warning_label.setText(
            ""
            if template.is_calibration_current()
            else (
                "⚠ Not validated against representative scans since its last "
                "change. Consider running Calibration before a large batch."
            )
        )
        _LOGGER.info("Scan page loaded template '%s' from %s", template.name, path)
        self._refresh_controls()
        return True

    def _default_template_dir(self) -> Path:
        """Where the template file dialog should start.

        The open project first. It used to prefer whatever template happened
        to be loaded, which after switching projects was the *previous*
        project's folder - so the dialog opened somewhere the user had
        deliberately navigated away from.
        """
        if self._session is not None:
            return self._session.project.layout.templates_dir
        if self.state.template_path is not None:
            return self.state.template_path.parent
        return Path.home()

    # ------------------------------------------------------------------
    # Scan list
    # ------------------------------------------------------------------
    def _prompt_add_scans(self) -> None:
        """Ask for image files, then add them."""
        patterns = "Scanned sheets (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)"
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Add scans", str(self._default_scan_dir()), patterns
        )
        if paths:
            self.add_scan_paths([Path(item) for item in paths])

    def _prompt_add_folder(self) -> None:
        """Ask for a folder, then add every supported image inside it."""
        directory = QFileDialog.getExistingDirectory(
            self, "Add a folder of scans", str(self._default_scan_dir())
        )
        if directory:
            self.add_scan_paths([Path(directory)])

    def add_scan_paths(self, selection: Sequence[Path]) -> int:
        """Add files and/or folders to the scan list.

        Args:
            selection: Files and directories, in any order. Directories
                contribute the supported images they contain; unsupported files
                are ignored.

        Returns:
            How many scans were added. Files already in the list are skipped, so
            adding the same folder twice does not duplicate it.
        """
        # Walking a folder of ten thousand files and building their rows takes
        # a moment, and a window that says nothing during it looks stuck.
        # `repaint()` rather than `processEvents()`: this paints one label
        # synchronously without re-entering the event loop, so no stray click
        # can arrive in the middle of rebuilding the list.
        self.progress_label.setText("Preparing batch...")
        self.progress_label.repaint()

        known = {entry.path.resolve() for entry in self.state.entries}
        added = 0
        # Only the paths are collected here - never the images. Ten thousand
        # scans is ten thousand short strings, which is the difference between
        # a batch that starts instantly and one that runs the machine out of
        # memory before it reads anything.
        for path in collect_scan_files(selection):
            resolved = path.resolve()
            if resolved in known:
                continue
            known.add(resolved)
            self.state.entries.append(ScanEntry(path=path))
            added += 1

        if added:
            self._append_scan_rows(added)
            if self._current_row() < 0:
                self.select_scan(0)
        _LOGGER.info("Added %d scan(s); list now holds %d", added, len(self.state.entries))
        self.progress_label.setText("")
        self._refresh_controls()
        return added

    def clear_scans(self) -> None:
        """Empty the scan list, the preview and the progress readout."""
        if self._worker is not None and self._worker.isRunning():
            return
        self.state.entries.clear()
        self._preview_cache.clear()
        self._final_report = None
        self._last_snapshot = ProgressSnapshot()
        self._rebuild_scan_table()
        self.preview.clear()
        self._show_result(None)
        self._reset_progress_panel()
        self._refresh_controls()

    def _reset_progress_panel(self) -> None:
        """Return the progress readout to its idle, nothing-to-report state."""
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        for label in (
            self.progress_label,
            self.progress_counts_label,
            self.progress_timing_label,
            self.progress_rate_label,
            self.progress_outcome_label,
        ):
            label.setText("")

    def _default_scan_dir(self) -> Path:
        """Where the scan file dialogs should start."""
        if self.state.entries:
            return self.state.entries[-1].path.parent
        if self._session is not None:
            return self._session.project.layout.scans_original_dir
        return Path.home()

    # ------------------------------------------------------------------
    # Output options
    # ------------------------------------------------------------------
    def _on_rename_toggled(self, checked: bool) -> None:
        """Remember the rename choice and refresh what it enables."""
        self.state.rename_enabled = checked
        self._refresh_controls()

    def _prompt_output_folder(self) -> None:
        """Ask for the folder renamed copies are written to."""
        directory = QFileDialog.getExistingDirectory(
            self, "Select the output folder", str(self._default_output_dir())
        )
        if directory:
            self.set_output_directory(Path(directory))

    def set_output_directory(self, path: Path | None) -> None:
        """Set - or clear - the folder renamed copies are written to.

        A new folder resets the name allocator: names are only unique relative
        to one destination, and carrying the previous folder's reservations over
        would push the first file there to ``_a`` for no reason.
        """
        self.state.output_dir = path
        self._allocator = FilenameAllocator(path)
        self.output_folder_label.setText(
            str(path) if path is not None else "No output folder selected"
        )
        self._refresh_controls()

    def _default_output_dir(self) -> Path:
        """Where the output-folder dialog should start."""
        if self.state.output_dir is not None:
            return self.state.output_dir
        if self._session is not None:
            return self._session.root
        return Path.home()

    # ------------------------------------------------------------------
    # Processing
    # ------------------------------------------------------------------
    def process_all(self) -> bool:
        """Process every scan in the list. Returns whether a run started."""
        return self._start_batch([entry.path for entry in self.state.entries])

    def process_selected(self) -> bool:
        """Process the selected rows, or the current one when nothing is selected."""
        rows = self.selected_rows()
        if not rows:
            return False
        return self._start_batch([self.state.entries[row].path for row in rows])

    def reprocess_all(self) -> bool:
        """Discard every result and process the whole list again.

        The stored batch is abandoned rather than overwritten: this run reads
        everything afresh, so it is a new batch, and keeping the old rows means
        a mistaken click does not destroy the previous run's record.
        """
        for entry in self.state.entries:
            entry.processed = None
        self._preview_cache.clear()
        self._allocator = FilenameAllocator(self.state.output_dir)
        self.state.batch_id = None
        self._rebuild_scan_table()
        return self.process_all()

    def resume_batch(self) -> bool:
        """Process only the scans this batch never finished.

        Returns:
            Whether a run started. ``False`` when there is nothing to resume,
            no project is open, or the operator declined an incompatible
            resume.
        """
        return self._resume(include_failed=False)

    def retry_failed(self) -> bool:
        """Process the scans that failed, and only those.

        Returns:
            Whether a run started.

        Successful and needs-review results are untouched. Each retried scan's
        attempt counter increases, so a sheet that has failed three times is
        visibly different from one that has failed once.
        """
        return self._resume(include_failed=True, failed_only=True)

    def _resume(self, *, include_failed: bool, failed_only: bool = False) -> bool:
        """Shared body of resume and retry."""
        database = self.database
        batch_id = self.state.batch_id
        if database is None or batch_id is None:
            QMessageBox.information(
                self,
                "Nothing to resume",
                "Resuming needs a saved batch. Open a project and process some "
                "scans first - results are stored as they finish.",
            )
            return False

        if not self._confirm_compatible(database, batch_id):
            return False

        paths = (
            failed_scans(database, batch_id)
            if failed_only
            else resumable_scans(database, batch_id, include_failed=include_failed)
        )
        if not paths:
            QMessageBox.information(
                self,
                "Nothing to process",
                "Every scan in this batch has already been processed."
                if not failed_only
                else "No scan in this batch has failed.",
            )
            return False

        _LOGGER.info(
            "Resuming batch %s: %d scan(s)%s",
            batch_id,
            len(paths),
            " (failed only)" if failed_only else "",
        )
        return self._start_batch(paths)

    def _confirm_compatible(self, database: ProjectDatabase, batch_id: str) -> bool:
        """Check the current configuration against the batch's, and ask if not.

        Mixing results produced under two different templates or two different
        thresholds gives a CSV whose rows are not comparable - and nothing
        downstream could tell, because each row looks perfectly ordinary. The
        operator is told exactly what changed and chooses; nothing is silently
        invalidated and nothing is silently mixed.
        """
        identity = self._batch_identity()
        if identity is None:
            return False
        verdict = check_compatibility(database, batch_id, identity)
        if verdict.compatible:
            return True

        _LOGGER.warning(
            "Resume of batch %s requested with incompatible settings: %s",
            batch_id,
            verdict.summary,
        )
        answer = QMessageBox.warning(
            self,
            "Settings have changed",
            f"{verdict.summary}\n\n"
            "Continuing would mix results produced under different rules in one "
            "batch. Process the remaining scans anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _current_row(self) -> int:
        """The scan list's current row, or ``-1``.

        `QTableView` has no `currentRow()` convenience method the way
        `QTableWidget` did.
        """
        return self.scan_table.currentIndex().row()

    def selected_rows(self) -> list[int]:
        """Rows currently selected in the scan list, in order."""
        rows = sorted({index.row() for index in self.scan_table.selectedIndexes()})
        if rows:
            return rows
        current = self._current_row()
        return [current] if current >= 0 else []

    def _start_batch(self, paths: Sequence[Path]) -> bool:
        """Start a background run over ``paths``."""
        if self.state.template is None or not paths:
            return False
        if self._worker is not None and self._worker.isRunning():
            return False

        if self.state.rename_enabled and self.state.output_dir is None:
            QMessageBox.information(
                self,
                "Output folder needed",
                "Choose an output folder before renaming scans, so that the renamed "
                "copies have somewhere to go. The original files are never changed.",
            )
            return False

        options = BatchOptions(
            output_dir=self.state.output_dir,
            rename_with_identifier=self.state.rename_enabled,
            with_preview=False,
            recognition=self._engine_options(),
            opencv_threads=self.state.processing.opencv_threads,
            worker_recycle_after=self.state.processing.worker_recycle_after,
        )
        workers = self.planned_worker_count(len(paths))
        self._final_report = None
        self._batch_started_at = utc_timestamp()
        # Everything before the first sheet is read is "preparing": the worker
        # pool has to start, and showing "0 / 10,000, 0%, remaining 00:00:00"
        # while that happens looks like a stalled run rather than a starting
        # one.
        self._show_preparing(len(paths))

        # Register (or reuse) the durable batch before a single sheet is read,
        # so that a crash one second into the run still leaves a resumable
        # record of what was supposed to happen.
        batch_id = self._ensure_batch(paths)
        recorder: BatchRecorder | None = None
        database = self.database
        if batch_id is not None and database is not None:
            try:
                mark_queued(database, batch_id, paths)
                set_batch_status(database, batch_id, BatchStatus.RUNNING)
                recorder = BatchRecorder(database=database, batch_id=batch_id)
            except OMRScannerError:
                _LOGGER.exception("Could not mark batch %s as running", batch_id)
                recorder = None

        _LOGGER.info(
            "Batch started: %d scan(s), %d worker(s), mode %s, diagnostics %s, "
            "batch %s",
            len(paths),
            workers,
            self.state.processing.mode.value,
            "on" if self.state.processing.writes_diagnostics else "off",
            batch_id or "(not stored)",
        )

        worker = BatchWorker(
            list(paths),
            self.state.template,
            options,
            self._allocator,
            self,
            workers=workers,
            recorder=recorder,
        )
        worker.progress.connect(self._on_progress)
        worker.scan_done.connect(self._on_scan_done)
        worker.finished_report.connect(self._on_batch_finished)
        worker.failed.connect(self._on_batch_failed)
        self._worker = worker
        worker.start()
        # The timer takes over from here. Deliberately not refreshed inline:
        # "Preparing batch..." should survive until the first tick, which is
        # roughly how long a worker pool takes to start.
        self._refresh_timer.start()
        self._refresh_controls()
        return True

    def _show_preparing(self, total: int) -> None:
        """Put the panel into its "counting the work" state."""
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_label.setText("Preparing batch...")
        self.progress_counts_label.setText(f"0 / {format_count(total)} processed")
        self.progress_timing_label.setText("Elapsed --:-- · Remaining Calculating...")
        self.progress_rate_label.setText("Speed --")
        self.progress_outcome_label.setText("")

    def cancel_processing(self) -> None:
        """Ask a running batch to stop after the sheets being read.

        The button is disabled immediately and the panel says so: a cancel that
        leaves the button live invites a second click, and a second click on a
        batch that is already stopping has nothing to do except confuse.
        """
        if self._worker is None or not self._worker.isRunning():
            return
        self._worker.cancel()
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling...")
        self.progress_label.setText("Cancelling batch processing...")
        self.progress_timing_label.setText("Remaining: Cancelling...")
        _LOGGER.info("Batch cancellation requested by the user")
        self._refresh_progress()

    # ------------------------------------------------------------------
    # Progress rendering
    # ------------------------------------------------------------------
    def _on_progress(self, update: BatchProgress) -> None:
        """React to one sheet finishing. Runs on the GUI thread.

        Deliberately almost empty. The counting already happened in the worker
        thread's tracker, and the widgets are repainted by
        :meth:`_refresh_progress` on a timer - so a machine that finishes fifty
        sheets a second does not ask Qt to repaint fifty times a second.
        """

    def _on_scan_done(self, processed: ProcessedScan) -> None:
        """Record one finished scan and mark its row for redrawing.

        The row is *not* redrawn here. It is added to a set that the refresh
        timer flushes, which turns ten thousand individual table updates into
        a few hundred batched ones.
        """
        row = self._row_by_path.get(processed.source_path)
        if row is None:
            return
        self.state.entries[row].processed = processed
        self._dirty_rows.add(row)

    def _refresh_progress(self) -> None:
        """Repaint the progress panel and any rows that have changed.

        The single throttled point at which batch state reaches the screen.
        Called by the refresh timer while a run is going, and directly at the
        start and end of one so that neither is left waiting for a tick.
        """
        self._flush_dirty_rows()
        if self._worker is None:
            return
        snapshot = self._worker.progress_snapshot()
        self._last_snapshot = snapshot
        self._render_progress(snapshot)

    def _flush_dirty_rows(self) -> None:
        """Redraw the scan-list rows that finished since the last refresh."""
        if not self._dirty_rows:
            return
        rows = sorted(self._dirty_rows)
        self._dirty_rows.clear()
        self._scan_model.mark_dirty(rows)

        # A row that just finished may no longer belong to the active filter.
        # Only these rows can have changed, so only they need re-checking -
        # not every row in the batch, which is what made this scale with the
        # whole list instead of with what actually changed.
        self._apply_status_filter_to_rows(rows)

        current = self._current_row()
        if current in rows and 0 <= current < len(self.state.entries):
            self._show_result(self.state.entries[current].processed)

    def _render_progress(self, snapshot: ProgressSnapshot) -> None:
        """Write one progress snapshot into the panel's labels."""
        total = snapshot.total
        completed = snapshot.completed

        if total > 0:
            if self.progress_bar.maximum() != total:
                self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(completed)
            # One decimal, and computed from the exact counts rather than from
            # the widget: 9,999 of 10,000 must not read as 100%.
            self.progress_bar.setFormat(f"{snapshot.percent:.1f}%")
            self.progress_counts_label.setText(
                f"{format_count(completed)} / {format_count(total)} processed"
            )

        if snapshot.state is BatchState.PROCESSING:
            self.progress_label.setText("Processing OMR scans...")

        if snapshot.state in (BatchState.CANCELLING, BatchState.CANCELLED):
            remaining = "Cancelling..."
        elif not snapshot.has_eta:
            # Warm-up, or a stall. Saying so is honest; a number derived from
            # two completions would not be.
            remaining = "Calculating..."
        else:
            remaining = f"~{format_duration(snapshot.eta_seconds)}"
        self.progress_timing_label.setText(
            f"Elapsed {format_duration(snapshot.elapsed_seconds)} · Remaining {remaining}"
        )

        rate = f"Speed {format_rate(snapshot.rate)}"
        workers = f"{snapshot.workers} worker{'' if snapshot.workers == 1 else 's'}"
        finish = ""
        if snapshot.finish_wall_clock is not None and snapshot.state is BatchState.PROCESSING:
            finish = f" · Finish ~{_clock_time(snapshot.finish_wall_clock)}"
        self.progress_rate_label.setText(f"{rate} · {workers}{finish}")

        self.progress_outcome_label.setText(
            f"Successful {format_count(snapshot.successful)} · "
            f"Review {format_count(snapshot.warnings)} · "
            f"Failed {format_count(snapshot.failed)}"
        )

    def _on_batch_finished(self, report: BatchReport) -> None:
        """Finish a run: settle its stored state, summarise it, re-enable."""
        self._refresh_timer.stop()
        snapshot = self._worker.progress_snapshot() if self._worker is not None else None
        persistence_failure = (
            self._worker.persistence_failure if self._worker is not None else ""
        )
        self._flush_dirty_rows()
        self._worker = None
        self._final_report = report

        self._settle_batch_state(report)
        # Conflicts are detected from the finished results, in this process,
        # after the pool has been torn down - see `_generate_conflicts`.
        self._generate_conflicts(report)
        if snapshot is not None:
            self._last_snapshot = snapshot
        self._render_completion(report, self._last_snapshot)
        if persistence_failure:
            self._report_persistence_failure(persistence_failure)

        _LOGGER.info(
            "Batch %s: %d processed (%d complete, %d review, %d failed), "
            "%d written, %d worker(s), %s, %s",
            "cancelled" if report.cancelled else "completed",
            report.total,
            report.complete_count,
            report.review_count,
            report.failed_count,
            report.written_count,
            format_duration(report.elapsed_seconds),
            format_rate(report.total / report.elapsed_seconds if report.elapsed_seconds else 0.0),
        )

        self._refresh_controls()
        if self._current_row() >= 0:
            self.select_scan(self._current_row())
        # Scored before the signal so that anything waiting on `batch_finished`
        # - a qtguitesting script, a test - already sees the benchmark result.
        if self.state.benchmark is not None and not report.cancelled:
            self._score_benchmark(report)
        self.batch_finished.emit(report)

    def request_review(self) -> bool:
        """Ask the main window to open this batch's conflicts."""
        if self.state.batch_id is None:
            QMessageBox.information(
                self,
                "Nothing to review",
                "Conflicts are recorded against a saved batch. Open a project and "
                "process some scans first.",
            )
            return False
        self.review_requested.emit(self.state.batch_id)
        return True

    def _generate_conflicts(self, report: BatchReport) -> None:
        """Record what this run could not decide, for human review (Phase 6).

        Runs in the coordinating process, once, after the batch has finished -
        never inside a worker. Two reasons, and both are architectural rather
        than convenience:

        * A worker process must not open the project database. SQLite is a
          single-writer store and Phase 5 keeps it that way by construction;
          letting eight processes write conflicts would be exactly the shared
          mutable state the worker pool exists to avoid.
        * A duplicate identifier is not a property of one sheet - both sheets
          are perfectly legible - so it cannot be seen while reading one.

        Detection is idempotent: re-running a batch, resuming it or retrying a
        failed sheet updates the existing conflicts rather than creating a
        second set, because a conflict's identity is
        ``(batch, scan, type, zone, group)``.

        Never fatal. A batch that read ten thousand sheets correctly is not
        spoiled by a failure to write its review queue; the failure is logged
        and reported in the label, and the results remain exported and
        resumable.
        """
        database = self.database
        batch_id = self.state.batch_id
        template = self.state.template
        if database is None or batch_id is None or template is None:
            self.conflict_label.setText("")
            return

        rows = scan_ids_by_path(database, batch_id)
        total = 0
        try:
            for item in report.processed:
                scan_id = rows.get(item.source_path)
                if scan_id is None:
                    continue
                total += sync_conflicts(
                    database,
                    batch_id=batch_id,
                    scan_id=scan_id,
                    result=item.result,
                    template=template,
                )
            duplicates = sync_duplicate_identifiers(database, batch_id)
        except OMRScannerError:
            _LOGGER.exception("Conflicts could not be recorded for batch %s", batch_id)
            self.conflict_label.setText(
                "⚠ The review queue could not be written. Results are still saved."
            )
            return

        counts = count_conflicts(database, batch_id)
        _LOGGER.info(
            "Batch %s conflict detection: %d total, %d unresolved, %d duplicate id(s)",
            batch_id,
            counts.total,
            counts.unresolved,
            duplicates,
        )
        self._refresh_conflict_label(counts)

    def _refresh_conflict_label(self, counts: ReviewCounts | None = None) -> None:
        """Say how much of this batch still needs a human."""
        database = self.database
        if database is None or self.state.batch_id is None:
            self.conflict_label.setText("")
            return
        summary = counts if counts is not None else count_conflicts(
            database, self.state.batch_id
        )
        if summary.total == 0:
            self.conflict_label.setText("No conflicts: nothing needs review.")
            return
        self.conflict_label.setText(
            f"{summary.unresolved} of {summary.total} conflict(s) still need review."
        )

    def unresolved_conflict_count(self) -> int:
        """How many of this batch's conflicts still need a decision.

        Read by :meth:`export_csv_to` before writing a file, so that an export
        cannot silently present unreviewed ambiguity as finished data.
        """
        database = self.database
        if database is None or self.state.batch_id is None:
            return 0
        return count_conflicts(database, self.state.batch_id).unresolved

    def _settle_batch_state(self, report: BatchReport) -> None:
        """Write the batch's terminal state after a run ends.

        A cancelled run returns its unfinished scans to a resumable state; a
        completed one records whether anything failed, because "the loop
        ended" and "the work succeeded" are different claims.
        """
        database = self.database
        batch_id = self.state.batch_id
        if database is None or batch_id is None:
            return
        try:
            if report.cancelled:
                mark_cancelled(database, batch_id)
            else:
                finalise_batch(database, batch_id)
        except OMRScannerError:
            _LOGGER.exception("Could not record the final state of batch %s", batch_id)
        self._refresh_batch_state_label()

    def _report_persistence_failure(self, message: str) -> None:
        """Tell the operator that results were produced but not stored.

        Deliberately a dialog and not a status line. Every other failure this
        page reports is about a *sheet*; this one is about the batch's record
        of itself, and an operator who closes the window believing the run was
        saved would lose work they have no way of knowing they lost.
        """
        _LOGGER.error("Batch results could not be persisted: %s", message)
        self.batch_state_label.setText(
            "⚠ Results could not be saved to the project database. "
            "Export the CSV before closing."
        )
        QMessageBox.warning(
            self,
            "Results could not be saved",
            "The scans were processed, but their results could not be written to "
            "the project database, so this batch cannot be resumed and the "
            "results will be lost when the window closes.\n\n"
            f"{message}\n\n"
            "Export the CSV now to keep what was read.",
        )

    def _render_completion(self, report: BatchReport, snapshot: ProgressSnapshot) -> None:
        """Show the final state: exactly 100%, or an honest partial count."""
        total = max(snapshot.total, report.total)
        processed = report.total
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(processed)
            self.progress_bar.setFormat(f"{(processed / total) * 100:.1f}%")

        duration = format_duration(snapshot.elapsed_seconds or report.elapsed_seconds)
        average = report.total / report.elapsed_seconds if report.elapsed_seconds > 0 else 0.0

        if report.cancelled:
            self.progress_label.setText("Batch cancelled.")
            self.progress_counts_label.setText(
                f"{format_count(processed)} / {format_count(total)} processed · "
                f"{format_count(max(total - processed, 0))} not processed"
            )
            self.progress_timing_label.setText(f"Stopped after {duration}")
        else:
            self.progress_label.setText("Batch processing complete.")
            self.progress_counts_label.setText(
                f"{format_count(processed)} / {format_count(total)} processed"
            )
            self.progress_timing_label.setText(
                f"Completed in {duration} · Remaining {format_duration(0)}"
            )

        written = (
            f" · {format_count(report.written_count)} file(s) written"
            if report.written_count
            else ""
        )
        self.progress_rate_label.setText(
            f"Average {format_rate(average)} · {report.worker_count} "
            f"worker{'' if report.worker_count == 1 else 's'}{written}"
        )
        self.progress_outcome_label.setText(
            f"Successful {format_count(report.complete_count)} · "
            f"Review {format_count(report.review_count)} · "
            f"Failed {format_count(report.failed_count)}"
        )

        self.cancel_button.setText("Cancel Processing")

    def _on_batch_failed(self, message: str) -> None:
        """Report a run that could not proceed at all.

        One dialog, and only for a batch that never ran. An individual sheet
        that fails is counted, logged and shown in the list - never raised as a
        dialog, because a thousand-sheet batch with fifty bad files would
        otherwise produce fifty modal interruptions.
        """
        self._refresh_timer.stop()
        self._worker = None
        self.progress_label.setText("Unable to start batch processing.")
        self.progress_timing_label.setText(message)
        self.cancel_button.setText("Cancel Processing")
        self._refresh_controls()
        _LOGGER.error("Batch could not start: %s", message)
        QMessageBox.warning(self, "Processing failed", message)

    # ------------------------------------------------------------------
    # Benchmark mode
    # ------------------------------------------------------------------
    def enter_benchmark_mode(self, dataset_dir: Path) -> bool:
        """Score the next run against the labelled dataset at ``dataset_dir``.

        Args:
            dataset_dir: A dataset folder holding ``images/`` and
                ``ground_truth/``.

        Returns:
            ``True`` when the dataset was opened and its scans were loaded.

        Benchmark mode is deliberately thin: it loads the dataset's scans into
        the ordinary scan list and remembers the ground truth. Processing then
        happens through exactly the same batch architecture, with the same
        settings and the same worker pool, because a benchmark of a *different*
        pipeline would measure nothing worth knowing.
        """
        from omr_scanner.evaluation.session import BenchmarkSession

        if self._worker is not None and self._worker.isRunning():
            return False
        try:
            session = BenchmarkSession.open(dataset_dir)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Benchmark", str(exc))
            return False

        self.clear_scans()
        self.state.benchmark = session
        self.last_benchmark = None
        self.last_comparison = None
        # Renaming copies files about; a benchmark reads a dataset and must not
        # rewrite it.
        self.rename_checkbox.setChecked(False)
        added = self.add_scan_paths([session.images_dir])
        self._refresh_benchmark_banner()
        _LOGGER.info(
            "Benchmark mode: dataset '%s' at %s, %d labelled sheet(s), %d scan(s) loaded",
            session.name,
            session.dataset_dir,
            session.sheet_count,
            added,
        )
        if added == 0:
            QMessageBox.warning(
                self,
                "Benchmark",
                f"No scans found in {session.images_dir}.",
            )
        return added > 0

    def exit_benchmark_mode(self) -> None:
        """Return the page to ordinary scanning, leaving the scan list alone."""
        if self.state.benchmark is None:
            return
        _LOGGER.info("Benchmark mode ended")
        self.state.benchmark = None
        self._refresh_benchmark_banner()

    def _refresh_benchmark_banner(self) -> None:
        """Show, hide and word the benchmark banner."""
        session = self.state.benchmark
        self.benchmark_banner.setVisible(session is not None)
        self.benchmark_results_button.setEnabled(self.last_benchmark is not None)
        if session is None:
            self.benchmark_label.setText("")
            return
        self.benchmark_label.setText(
            f"<b>Benchmark mode</b> - scoring against '{session.name}' "
            f"({format_count(session.sheet_count)} labelled sheet(s)) from "
            f"{session.dataset_dir}. Results are synthetic and are not "
            f"real-world accuracy."
        )

    def _score_benchmark(self, report: BatchReport) -> None:
        """Compare a finished run with the dataset's ground truth.

        Called from the batch-finished handler. Failures here are reported and
        swallowed: a benchmark that cannot be scored must not take the batch
        result down with it - the run itself succeeded.
        """
        session = self.state.benchmark
        if session is None:
            return
        results = [item.result for item in report.processed]
        try:
            scored, comparison = session.score(
                results,
                self.state.template,
                worker_count=report.worker_count,
                started_at=self._batch_started_at,
            )
        except OSError as exc:
            _LOGGER.exception("Could not write the benchmark report")
            QMessageBox.warning(self, "Benchmark", f"Could not write the report: {exc}")
            return

        self.last_benchmark = scored
        self.last_comparison = comparison
        self.benchmark_results_button.setEnabled(True)
        self.progress_label.setText(
            f"Benchmark: sheet accuracy {scored.summary.sheet_accuracy:.4f}, "
            f"{format_count(len(scored.errors))} disagreement(s)"
        )
        _LOGGER.info(
            "Benchmark scored: %d scan(s), sheet accuracy %.4f, %d error(s)%s",
            scored.summary.scans,
            scored.summary.sheet_accuracy,
            len(scored.errors),
            f", {len(comparison.regressions)} regression(s)" if comparison.has_baseline else "",
        )
        self.benchmark_finished.emit(scored)
        if self.benchmark_auto_show:
            self.show_benchmark_results()

    def show_benchmark_results(self) -> None:
        """Open the results dialog for the last scored run."""
        if self.last_benchmark is None:
            return
        from omr_scanner.gui.devtools.benchmark_dialog import BenchmarkResultsDialog

        session = self.state.benchmark
        dialog = BenchmarkResultsDialog(
            self.last_benchmark,
            session.report_dir if session is not None else None,
            self,
        )
        dialog.scan_requested.connect(self.select_scan_named)
        dialog.exec()

    def select_scan_named(self, name: str) -> bool:
        """Select the scan whose file name is ``name``.

        How a failing case gets reviewed: the benchmark names a scan, the page
        selects it, and the existing preview and overlay do the rest. Returns
        whether the scan was in the list.
        """
        for row, entry in enumerate(self.state.entries):
            if entry.path.name == name:
                self.select_scan(row)
                self.scan_table.scrollTo(self._scan_model.index(row, 0))
                return True
        return False

    # ------------------------------------------------------------------
    # Selection and preview
    # ------------------------------------------------------------------
    def _on_table_selection_changed(self) -> None:
        """React to the user clicking a different row."""
        if self._suppress_selection:
            return
        row = self._current_row()
        if row >= 0:
            self.select_scan(row)

    def select_scan(self, row: int) -> None:
        """Show the scan at ``row``: its preview, overlay and recognised values."""
        if not 0 <= row < len(self.state.entries):
            return
        self._suppress_selection = True
        try:
            self.scan_table.selectRow(row)
        finally:
            self._suppress_selection = False

        entry = self.state.entries[row]
        self._show_result(entry.processed)
        self._show_preview_for(entry)
        self.scan_selected.emit(row)

    def select_next(self) -> None:
        """Select the next scan in the list."""
        row = self._current_row()
        if row + 1 < len(self.state.entries):
            self.select_scan(row + 1)

    def select_previous(self) -> None:
        """Select the previous scan in the list."""
        row = self._current_row()
        if row > 0:
            self.select_scan(row - 1)

    def _show_preview_for(self, entry: ScanEntry) -> None:
        """Show ``entry``'s rectified page, recreating it in the background if needed."""
        if self.state.template is None:
            return

        cached = self._preview_cache.get(entry.path)
        if cached is not None:
            self._preview_cache.move_to_end(entry.path)
            self._apply_preview(cached)
            return

        if entry.processed is None:
            self.preview.clear()
            self.preview_status_label.setText("Not processed yet")
            return

        result = entry.processed.result
        if result.outcome is RecognitionOutcome.ERROR:
            self.preview.clear()
            self.preview_status_label.setText(result.registration_message or "Could not be read")
            return

        # Already rendering this very scan: let the running worker finish rather
        # than starting a second one for the same file. Two workers would both
        # call _apply_preview, and the late one would re-fit the view - undoing
        # a zoom the user had just set - as well as rectifying the page twice
        # for nothing. This happens routinely: finishing a batch re-selects the
        # current row, and the user may click that same row again meanwhile.
        running = self._preview_worker
        if running is not None and running.isRunning() and running.path == entry.path:
            return

        # The batch discarded previews; recreate this one off the GUI thread.
        self.preview_status_label.setText("Rendering preview...")
        worker = PreviewWorker(entry.path, self.state.template, self)
        worker.ready.connect(self._on_preview_ready)
        worker.failed.connect(self._on_preview_failed)
        self._preview_worker = worker
        worker.start()

    def _on_preview_ready(self, result: ScanResult) -> None:
        """Cache and show a preview that finished rendering."""
        self._preview_cache[result.source_path] = result
        while len(self._preview_cache) > PREVIEW_CACHE_SIZE:
            self._preview_cache.popitem(last=False)
        row = self._current_row()
        if (
            0 <= row < len(self.state.entries)
            and self.state.entries[row].path == result.source_path
        ):
            self._apply_preview(result)
        self._preview_worker = None

    def _on_preview_failed(self, message: str) -> None:
        """Report a preview that could not be produced."""
        self.preview_status_label.setText(f"Preview failed: {message}")
        self._preview_worker = None

    def _apply_preview(self, result: ScanResult) -> None:
        """Put one result's page and overlay into the preview view."""
        self.preview.set_page(
            result.preview,
            canonical_width=result.canonical_width,
            canonical_height=result.canonical_height,
            preview_scale=result.preview_scale,
        )
        self.preview.set_overlay(result.zones, result.bubbles)
        self._refresh_overlay_visibility()
        self.preview.fit_to_window()
        self.preview_status_label.setText(
            f"{result.canonical_width} x {result.canonical_height} - "
            f"{result.registration.value.replace('_', ' ')}"
        )

    def _refresh_overlay_visibility(self) -> None:
        """Apply the overlay checkboxes to the preview."""
        self.preview.set_overlay_visible(
            zones=self.zones_checkbox.isChecked(),
            bubbles=self.bubbles_checkbox.isChecked(),
            empty=self.empty_checkbox.isChecked(),
        )

    # ------------------------------------------------------------------
    # Results panel
    # ------------------------------------------------------------------
    def _show_result(self, processed: ProcessedScan | None) -> None:
        """Fill the right-hand panel from one scan's outcome."""
        self.fields_table.setRowCount(0)
        self.answers_table.setRowCount(0)

        if processed is None:
            self.result_summary_label.setText("Not processed yet")
            return

        result = processed.result
        lines = [
            f"<b>{result.source_path.name}</b>",
            f"Registration: {result.registration.value.replace('_', ' ')}",
            f"Status: {STATUS_LABELS.get(result.outcome.value, result.outcome.value)}",
        ]
        if processed.output_name:
            lines.append(f"Output file: {processed.output_name}")
        if result.registration_message:
            lines.append(result.registration_message)
        if processed.message:
            lines.append(processed.message)
        self.result_summary_label.setText("<br>".join(lines))

        self.fields_table.setRowCount(len(result.fields))
        for row, view in enumerate(result.fields):
            self.fields_table.setItem(row, 0, QTableWidgetItem(view.label))
            self.fields_table.setItem(row, 1, QTableWidgetItem(view.value))
            self.fields_table.setItem(
                row, 2, QTableWidgetItem(MARK_STATUS_LABELS.get(view.status, view.status))
            )
            if view.needs_review:
                self._tint_row(
                    self.fields_table, row, STATUS_COLORS[RecognitionOutcome.REVIEW.value]
                )

        self.answers_table.setRowCount(len(result.answers))
        for row, answer in enumerate(result.answers):
            self.answers_table.setItem(row, 0, QTableWidgetItem(str(answer.number)))
            self.answers_table.setItem(row, 1, QTableWidgetItem(answer.display_value))
            self.answers_table.setItem(
                row, 2, QTableWidgetItem(MARK_STATUS_LABELS.get(answer.status, answer.status))
            )
            if answer.needs_review:
                self._tint_row(
                    self.answers_table, row, STATUS_COLORS[RecognitionOutcome.REVIEW.value]
                )

    @staticmethod
    def _tint_row(table: QTableWidget, row: int, color: QColor) -> None:
        """Give one row a background tint, for rows that need attention."""
        for column in range(table.columnCount()):
            item = table.item(row, column)
            if item is not None:
                item.setBackground(color)

    # ------------------------------------------------------------------
    # Scan table
    # ------------------------------------------------------------------
    def _rebuild_scan_table(self) -> None:
        """Tell the scan list its entries were replaced, emptied, or all changed.

        Also rebuilds the path-to-row index, which is what makes finishing one
        sheet an O(1) update instead of a scan of the whole list - the
        difference between linear and quadratic work over a ten-thousand-sheet
        batch.

        Used when the entries list itself was rebuilt wholesale (loading a
        stored batch, clearing the list, reprocessing everything). Adding
        scans to an already-populated list uses :meth:`_append_scan_rows`
        instead, which does not disturb the current selection.
        """
        self._suppress_selection = True
        try:
            self._row_by_path = {
                entry.path: row for row, entry in enumerate(self.state.entries)
            }
            self._dirty_rows.clear()
            self._scan_model.entries_reset()
        finally:
            self._suppress_selection = False
        self._apply_status_filter()

    def _append_scan_rows(self, added: int) -> None:
        """Tell the scan list that ``added`` new entries were appended.

        A row-insertion notification rather than a full reset, so that adding
        more scans to a list the user is already looking at does not clear
        their current selection.
        """
        total = len(self.state.entries)
        for row in range(total - added, total):
            self._row_by_path[self.state.entries[row].path] = row
        self._scan_model.entries_appended(added)
        self._apply_status_filter_to_rows(range(total - added, total))

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def _prompt_export_csv(self) -> None:
        """Ask where to save the results CSV, then write it."""
        start = self._default_output_dir() / "omrflow_results.csv"
        path_str, _filter = QFileDialog.getSaveFileName(
            self, "Export results", str(start), "CSV files (*.csv)"
        )
        if path_str:
            self.export_csv_to(Path(path_str))

    def export_csv_to(self, path: Path) -> Path | None:
        """Write the processed results to ``path``.

        Args:
            path: Destination file.

        Returns:
            The path written, or ``None`` when there was nothing to export or
            the write failed (which is reported to the user).
        """
        if self.state.template is None:
            return None
        processed = [entry.processed for entry in self.state.entries if entry.processed]
        if not processed:
            QMessageBox.information(
                self,
                "Nothing to export",
                "Process at least one scan before exporting results.",
            )
            return None
        if not self._confirm_unresolved_export():
            return None

        # Human decisions are applied here, at the one point results leave the
        # application - never by editing a result. What recognition read stays
        # in the project database whatever this file says.
        resolutions = self._export_resolutions()
        try:
            written = export_scan_results(
                processed, self.state.template, path, resolutions=resolutions
            )
        except OMRScannerError as exc:
            report_error(self, exc, context="Export results")
            return None
        _LOGGER.info(
            "Exported %d result(s) to %s (%d sheet(s) carry human decisions)",
            len(processed),
            written,
            sum(1 for item in resolutions.values() if item.reviewed),
        )
        self.progress_label.setText(f"Exported {len(processed)} result(s) to {written.name}")
        return written

    def _export_resolutions(self) -> dict[Path, SheetResolution]:
        """The human decisions that apply to this batch, or none."""
        database = self.database
        if database is None or self.state.batch_id is None or self.state.template is None:
            return {}
        try:
            return sheet_resolutions(database, self.state.batch_id, self.state.template)
        except OMRScannerError:
            # An export that silently dropped corrections would be worse than
            # no export, so this is reported rather than swallowed - but it
            # does not have to stop the machine values being written.
            _LOGGER.exception("Human decisions could not be read for the export")
            QMessageBox.warning(
                self,
                "Corrections could not be read",
                "The project's review decisions could not be read, so this export "
                "contains the machine's own values only. The decisions themselves "
                "are not lost.",
            )
            return {}

    def _confirm_unresolved_export(self) -> bool:
        """Warn before exporting a batch that still has disputes open.

        Informational, not a block (Phase 6 brief §24): an operator may
        legitimately want an interim export. What must not happen is unresolved
        ambiguity leaving the application *silently* looking like finished
        data - so the count is stated, and the exported rows carry it too in
        the ``unresolved_conflicts`` column.
        """
        unresolved = self.unresolved_conflict_count()
        if not unresolved:
            return True
        answer = QMessageBox.question(
            self,
            "Conflicts are still unresolved",
            f"{unresolved} conflict(s) in this batch have not been reviewed.\n\n"
            "Those rows will carry the machine's own values, and the export marks "
            "them as unresolved. Export anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------
    def _apply_status_filter(self) -> None:
        """Re-evaluate every row against the current filter choice.

        Rows are hidden, never removed: every other part of this page treats a
        table row index as an index into :attr:`ScanPageState.entries`, and a
        filter that rebuilt the table would silently break that.

        Called when the filter combo itself changes, or the entries list was
        rebuilt wholesale - anything that can invalidate every row's
        visibility at once. A run in progress instead calls
        :meth:`_apply_status_filter_to_rows` for just the rows that finished,
        since re-scanning every row on every ~200ms refresh tick is exactly
        the unbounded cost this page's large-batch design otherwise avoids.
        """
        choice = self.status_filter_combo.currentText()
        self._active_filter = FILTER_OUTCOMES.get(choice)
        if self._active_filter is None:
            # The common case (no filter applied) needs no per-row work at
            # all: nothing is hidden.
            for row in range(len(self.state.entries)):
                self.scan_table.setRowHidden(row, False)
            self._visible_count = len(self.state.entries)
        else:
            self._visible_count = 0
            for row, entry in enumerate(self.state.entries):
                visible = entry.outcome in self._active_filter
                self.scan_table.setRowHidden(row, not visible)
                self._visible_count += visible
        self._update_filter_count_label()

    def _apply_status_filter_to_rows(self, rows: Iterable[int]) -> None:
        """Re-evaluate visibility for specific rows only.

        Every other row's visibility cannot have changed, so re-checking it
        would only repeat work already done - this is what keeps a filtered
        view's upkeep proportional to how many sheets just finished, not to
        how many sheets exist in total.
        """
        wanted = self._active_filter
        for row in rows:
            if not 0 <= row < len(self.state.entries):
                continue
            visible = wanted is None or self.state.entries[row].outcome in wanted
            was_hidden = self.scan_table.isRowHidden(row)
            if was_hidden == visible:
                self._visible_count += 1 if visible else -1
            self.scan_table.setRowHidden(row, not visible)
        self._update_filter_count_label()

    def _update_filter_count_label(self) -> None:
        """Show how many rows the active filter leaves visible, or nothing."""
        self.filter_count_label.setText(
            ""
            if self._active_filter is None
            else f"{self._visible_count} of {len(self.state.entries)}"
        )

    def visible_rows(self) -> list[int]:
        """Rows the current filter leaves on screen, in order."""
        return [
            row
            for row in range(len(self.state.entries))
            if not self.scan_table.isRowHidden(row)
        ]

    # ------------------------------------------------------------------
    # Enablement
    # ------------------------------------------------------------------
    def _refresh_controls(self) -> None:
        """Enable exactly the controls that can do something right now."""
        has_template = self.state.template is not None
        has_scans = bool(self.state.entries)
        running = self._worker is not None and self._worker.isRunning()
        self._announce_processing(running)
        has_results = any(entry.processed is not None for entry in self.state.entries)

        self.add_scans_button.setEnabled(has_template and not running)
        self.add_folder_button.setEnabled(has_template and not running)
        self.clear_scans_button.setEnabled(has_scans and not running)
        self.process_all_button.setEnabled(has_template and has_scans and not running)
        self.process_selected_button.setEnabled(has_template and has_scans and not running)
        self.reprocess_button.setEnabled(has_template and has_results and not running)
        # Resume and retry act on the *stored* batch, so they need a project
        # and something actually left to do - not merely a non-empty list.
        summary = self.batch_summary()
        self.resume_button.setEnabled(
            has_template and not running and summary is not None and summary.pending > 0
        )
        self.retry_failed_button.setEnabled(
            has_template and not running and summary is not None and summary.failed > 0
        )
        # Enabled only while a run is going *and* has not already been asked to
        # stop: a second cancel has nothing left to do.
        cancelling = self._last_snapshot.state is BatchState.CANCELLING
        self.cancel_button.setEnabled(running and not cancelling)
        self.load_template_button.setEnabled(not running)
        self.rename_checkbox.setEnabled(not running)
        self.output_folder_button.setEnabled(not running)
        self.export_csv_button.setEnabled(has_results and not running)
        self.previous_action.setEnabled(has_scans)
        self.next_action.setEnabled(has_scans)
        self._refresh_worker_label()

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------
    @property
    def is_processing(self) -> bool:
        """Whether a batch run is currently going.

        Read by the main window before it closes, so the operator is warned
        rather than having a run stopped out from under them.
        """
        return self._worker is not None and self._worker.isRunning()

    def _announce_processing(self, running: bool) -> None:
        """Emit :attr:`processing_changed` when, and only when, it changed.

        Guarded because ``_refresh_controls`` runs on every progress tick of a
        running batch - several times a second - and re-emitting ``True`` each
        time would make every connected slot run for no reason.
        """
        if running == self._announced_processing:
            return
        self._announced_processing = running
        self.processing_changed.emit(running)

    def shutdown_batch(self) -> None:
        """Stop a running batch and wait for it, leaving the state consistent.

        Waiting matters more than it did single-threaded: the batch thread owns
        a pool of worker processes, and letting the application exit while it
        still exists is how orphaned Python processes are left behind. The wait
        is generous because cancelling still lets the sheets already inside a
        worker finish, which is a second or two each.

        The worker flushes its recorder before it returns, so by the time this
        call ends every sheet that finished is durable and the rest are
        resumable. That is the whole reason this is a wait and not a kill.
        """
        self._refresh_timer.stop()
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(WORKER_SHUTDOWN_TIMEOUT_MS)
            # `finished_report` may never be delivered - the event loop is on
            # its way out - so the batch's stored state is settled here rather
            # than relying on a signal that might not arrive.
            database = self.database
            batch_id = self.state.batch_id
            if database is not None and batch_id is not None:
                try:
                    mark_cancelled(database, batch_id)
                except OMRScannerError:
                    _LOGGER.exception(
                        "Could not mark batch %s cancelled during shutdown", batch_id
                    )
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_worker.wait(WORKER_SHUTDOWN_TIMEOUT_MS)

    def closeEvent(self, event: object) -> None:
        """Stop any running worker before the page disappears."""
        self.shutdown_batch()
        super().closeEvent(event)  # type: ignore[arg-type]


def _clock_time(epoch_seconds: float) -> str:
    """Render a Unix timestamp as a local time of day, for "finish at ~".

    Time of day only: a batch that will finish tomorrow is already being
    described by its remaining duration, and a date here would be noise in a
    290-pixel column.
    """
    from datetime import datetime

    return datetime.fromtimestamp(epoch_seconds).strftime("%H:%M")


__all__ = ["ScanEntry", "ScanPage", "ScanPageState"]
