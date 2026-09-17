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

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.gui.scan.worker import BatchWorker, PreviewWorker
from omr_scanner.gui.theme import TEMPLATE_DESIGNER_STYLESHEET
from omr_scanner.services import (
    BatchOptions,
    BatchProgress,
    BatchReport,
    FilenameAllocator,
    ProcessedScan,
    RecognitionOutcome,
    ScanResult,
    collect_scan_files,
    export_scan_results,
    load_template,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import ProjectSession

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

STATUS_LABELS: dict[str, str] = {
    RecognitionOutcome.PENDING.value: "Pending",
    RecognitionOutcome.COMPLETE.value: "Complete",
    RecognitionOutcome.REVIEW.value: "Review",
    RecognitionOutcome.REGISTRATION_FAILED.value: "Registration failed",
    RecognitionOutcome.ERROR.value: "Error",
}
"""Plain-language names for the outcomes, shown in the scan list."""

STATUS_COLORS: dict[str, QColor] = {
    RecognitionOutcome.COMPLETE.value: QColor(226, 245, 229),
    RecognitionOutcome.REVIEW.value: QColor(255, 244, 214),
    RecognitionOutcome.REGISTRATION_FAILED.value: QColor(253, 226, 226),
    RecognitionOutcome.ERROR.value: QColor(253, 226, 226),
}
"""Row tints. Backed up by the text in the Status column, never used alone."""

MARK_STATUS_LABELS: dict[str, str] = {
    "resolved": "",
    "blank": "blank",
    "multiple": "multiple",
    "uncertain": "uncertain",
    "unreadable": "unreadable",
}


@dataclass
class ScanEntry:
    """One row of the scan list.

    Attributes:
        path: The source image.
        processed: Its outcome once it has been through the pipeline.
    """

    path: Path
    processed: ProcessedScan | None = None

    @property
    def outcome(self) -> str:
        """The outcome's string value, or ``"pending"`` before processing."""
        if self.processed is None:
            return RecognitionOutcome.PENDING.value
        return self.processed.outcome.value

    @property
    def identifier(self) -> str:
        """The recognised identifier, or ``""``."""
        return "" if self.processed is None else self.processed.result.identifier_value

    @property
    def set_code(self) -> str:
        """The recognised set code, or ``""``."""
        return "" if self.processed is None else self.processed.result.set_code_value

    @property
    def output_name(self) -> str:
        """The planned or written output file name, or ``""``."""
        return "" if self.processed is None else self.processed.output_name


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
    """

    template: OmrTemplate | None = None
    template_path: Path | None = None
    entries: list[ScanEntry] = field(default_factory=list)
    output_dir: Path | None = None
    rename_enabled: bool = False


class ScanPage(WorkflowPage):
    """Import scanned sheets, recognise them against a template, and export.

    Signals:
        batch_finished: ``BatchReport`` when a run ends. GUI tests and the
            qtguitesting scripts wait on this instead of sleeping.
        scan_selected: ``int`` row index whenever the shown scan changes.

    Args:
        spec: The "scan" workflow stage description.
        parent: Optional Qt parent.
    """

    batch_finished = Signal(object)
    scan_selected = Signal(int)

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setObjectName("scanPage")
        self.setStyleSheet(TEMPLATE_DESIGNER_STYLESHEET)

        self.state = ScanPageState()
        self._session: ProjectSession | None = None
        self._worker: BatchWorker | None = None
        self._preview_worker: PreviewWorker | None = None
        self._preview_cache: OrderedDict[Path, ScanResult] = OrderedDict()
        self._allocator = FilenameAllocator(None)
        self._suppress_selection = False

        splitter = QSplitter(Qt.Orientation.Horizontal)
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
        """Build the left-hand control column."""
        panel = QWidget()
        panel.setObjectName("scanControlPanel")
        panel.setMaximumWidth(CONTROL_PANEL_WIDTH + 60)
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
        self.process_all_button = QPushButton(load_icon("scan-line"), "Process All")
        self.process_all_button.setObjectName("processAllButton")
        self.process_all_button.clicked.connect(self.process_all)
        process_layout.addWidget(self.process_all_button)

        self.process_selected_button = QPushButton(load_icon("scan"), "Process Selected")
        self.process_selected_button.setObjectName("processSelectedButton")
        self.process_selected_button.clicked.connect(self.process_selected)
        process_layout.addWidget(self.process_selected_button)

        self.reprocess_button = QPushButton(load_icon("rotate-ccw"), "Reprocess")
        self.reprocess_button.setObjectName("reprocessButton")
        self.reprocess_button.setToolTip("Process every scan again, discarding previous results")
        self.reprocess_button.clicked.connect(self.reprocess_all)
        process_layout.addWidget(self.reprocess_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self.cancel_processing)
        process_layout.addWidget(self.cancel_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        process_layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("")
        self.progress_label.setObjectName("progressLabel")
        self.progress_label.setWordWrap(True)
        process_layout.addWidget(self.progress_label)
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

        self.scan_table = QTableWidget(0, 5)
        self.scan_table.setObjectName("scanTable")
        self.scan_table.setHorizontalHeaderLabels(
            ["Original file", "Roll", "Set", "Status", "Output file"]
        )
        self.scan_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.scan_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.scan_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.scan_table.verticalHeader().setVisible(False)
        header = self.scan_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.scan_table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.scan_table.setMinimumHeight(140)

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(self.preview)
        vertical.addWidget(self.scan_table)
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
        """Track the open project, used to default file dialogs sensibly."""
        self._session = session

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
        _LOGGER.info("Scan page loaded template '%s' from %s", template.name, path)
        self._refresh_controls()
        return True

    def _default_template_dir(self) -> Path:
        """Where the template file dialog should start."""
        if self.state.template_path is not None:
            return self.state.template_path.parent
        if self._session is not None:
            return self._session.project.layout.templates_dir
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
        known = {entry.path.resolve() for entry in self.state.entries}
        added = 0
        for path in collect_scan_files(selection):
            resolved = path.resolve()
            if resolved in known:
                continue
            known.add(resolved)
            self.state.entries.append(ScanEntry(path=path))
            added += 1

        if added:
            self._rebuild_scan_table()
            if self.scan_table.currentRow() < 0:
                self.select_scan(0)
        _LOGGER.info("Added %d scan(s); list now holds %d", added, len(self.state.entries))
        self._refresh_controls()
        return added

    def clear_scans(self) -> None:
        """Empty the scan list and the preview."""
        if self._worker is not None and self._worker.isRunning():
            return
        self.state.entries.clear()
        self._preview_cache.clear()
        self._rebuild_scan_table()
        self.preview.clear()
        self._show_result(None)
        self._refresh_controls()

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
        """Discard every result and process the whole list again."""
        for entry in self.state.entries:
            entry.processed = None
        self._preview_cache.clear()
        self._allocator = FilenameAllocator(self.state.output_dir)
        self._rebuild_scan_table()
        return self.process_all()

    def selected_rows(self) -> list[int]:
        """Rows currently selected in the scan list, in order."""
        rows = sorted({index.row() for index in self.scan_table.selectedIndexes()})
        if rows:
            return rows
        current = self.scan_table.currentRow()
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
        )
        self.progress_bar.setRange(0, len(paths))
        self.progress_bar.setValue(0)
        self.progress_label.setText(f"Processing 0 of {len(paths)}...")

        worker = BatchWorker(list(paths), self.state.template, options, self._allocator, self)
        worker.progress.connect(self._on_progress)
        worker.scan_done.connect(self._on_scan_done)
        worker.finished_report.connect(self._on_batch_finished)
        worker.failed.connect(self._on_batch_failed)
        self._worker = worker
        worker.start()
        self._refresh_controls()
        return True

    def cancel_processing(self) -> None:
        """Ask a running batch to stop after the sheet it is working on."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.progress_label.setText("Cancelling after the current sheet...")

    def _on_progress(self, update: BatchProgress) -> None:
        """Update the progress bar. Runs on the GUI thread via a queued signal."""
        self.progress_bar.setValue(update.completed)
        self.progress_label.setText(
            f"Processing {min(update.completed + 1, update.total)} of {update.total}: "
            f"{update.path.name}"
        )

    def _on_scan_done(self, processed: ProcessedScan) -> None:
        """Record one finished scan and refresh its row."""
        for row, entry in enumerate(self.state.entries):
            if entry.path == processed.source_path:
                entry.processed = processed
                self._update_scan_row(row, entry)
                if row == self.scan_table.currentRow():
                    self._show_result(processed)
                break

    def _on_batch_finished(self, report: BatchReport) -> None:
        """Finish a run: summarise it and re-enable the controls."""
        self.progress_bar.setValue(self.progress_bar.maximum())
        summary = (
            f"{report.total} processed - {report.complete_count} complete, "
            f"{report.review_count} for review, {report.failed_count} failed"
        )
        if report.written_count:
            summary += f", {report.written_count} file(s) written"
        if report.cancelled:
            summary += " (cancelled)"
        self.progress_label.setText(summary)
        self._worker = None
        self._refresh_controls()
        if self.scan_table.currentRow() >= 0:
            self.select_scan(self.scan_table.currentRow())
        self.batch_finished.emit(report)

    def _on_batch_failed(self, message: str) -> None:
        """Report a run that could not proceed at all."""
        self._worker = None
        self.progress_label.setText(f"Processing failed: {message}")
        self._refresh_controls()
        QMessageBox.warning(self, "Processing failed", message)

    # ------------------------------------------------------------------
    # Selection and preview
    # ------------------------------------------------------------------
    def _on_table_selection_changed(self) -> None:
        """React to the user clicking a different row."""
        if self._suppress_selection:
            return
        row = self.scan_table.currentRow()
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
        row = self.scan_table.currentRow()
        if row + 1 < len(self.state.entries):
            self.select_scan(row + 1)

    def select_previous(self) -> None:
        """Select the previous scan in the list."""
        row = self.scan_table.currentRow()
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
        row = self.scan_table.currentRow()
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
        """Rebuild the scan list from scratch."""
        self._suppress_selection = True
        try:
            self.scan_table.setRowCount(len(self.state.entries))
            for row, entry in enumerate(self.state.entries):
                self._update_scan_row(row, entry)
        finally:
            self._suppress_selection = False

    def _update_scan_row(self, row: int, entry: ScanEntry) -> None:
        """Refresh one row of the scan list."""
        values = (
            entry.path.name,
            entry.identifier,
            entry.set_code,
            STATUS_LABELS.get(entry.outcome, entry.outcome),
            entry.output_name,
        )
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            if column == 0:
                item.setToolTip(str(entry.path))
            self.scan_table.setItem(row, column, item)
        tint = STATUS_COLORS.get(entry.outcome)
        if tint is not None:
            self._tint_row(self.scan_table, row, tint)

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
        try:
            written = export_scan_results(processed, self.state.template, path)
        except OMRScannerError as exc:
            report_error(self, exc, context="Export results")
            return None
        _LOGGER.info("Exported %d result(s) to %s", len(processed), written)
        self.progress_label.setText(f"Exported {len(processed)} result(s) to {written.name}")
        return written

    # ------------------------------------------------------------------
    # Enablement
    # ------------------------------------------------------------------
    def _refresh_controls(self) -> None:
        """Enable exactly the controls that can do something right now."""
        has_template = self.state.template is not None
        has_scans = bool(self.state.entries)
        running = self._worker is not None and self._worker.isRunning()
        has_results = any(entry.processed is not None for entry in self.state.entries)

        self.add_scans_button.setEnabled(has_template and not running)
        self.add_folder_button.setEnabled(has_template and not running)
        self.clear_scans_button.setEnabled(has_scans and not running)
        self.process_all_button.setEnabled(has_template and has_scans and not running)
        self.process_selected_button.setEnabled(has_template and has_scans and not running)
        self.reprocess_button.setEnabled(has_template and has_results and not running)
        self.cancel_button.setEnabled(running)
        self.load_template_button.setEnabled(not running)
        self.rename_checkbox.setEnabled(not running)
        self.output_folder_button.setEnabled(not running)
        self.export_csv_button.setEnabled(has_results and not running)
        self.previous_action.setEnabled(has_scans)
        self.next_action.setEnabled(has_scans)

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------
    def closeEvent(self, event: object) -> None:
        """Stop any running worker before the page disappears."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        if self._preview_worker is not None and self._preview_worker.isRunning():
            self._preview_worker.wait(5000)
        super().closeEvent(event)  # type: ignore[arg-type]


__all__ = ["ScanEntry", "ScanPage", "ScanPageState"]
