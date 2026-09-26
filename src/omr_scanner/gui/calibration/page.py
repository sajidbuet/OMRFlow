"""The Calibration workflow stage: verify and tune a template (Phase 4).

Purpose:
    Let an operator load a saved template and one or more representative real
    scans, run the existing Phase 3 pipeline on them in diagnostic mode,
    inspect exactly what it measured and decided, adjust recognition
    thresholds and watch the effect immediately, and get an explicit
    validation status before trusting the template with a batch.

Responsibilities:
    * :class:`CalibrationPage` - the workflow stage.

What does NOT belong here:
    * Recognition, registration, measurement or judgement.
      :mod:`omr_scanner.services.recognition_service` and
      :mod:`omr_scanner.services.calibration_service` do all of it; this page
      calls them and draws the result.
    * ``cv2``, ``numpy``, ``omr_scanner.imaging`` or ``omr_scanner.recognition``
      imports - enforced by ``tests/unit/test_architecture.py`` exactly as for
      every other page.
    * A second geometry editor. Geometry problems are fixed in the Template
      Designer; see :meth:`CalibrationPage._emit_edit_template_requested`.

Architecture, in one paragraph:
    Adding a scan opens a
    :class:`~omr_scanner.services.recognition_service.CalibrationSession` on a
    background thread (:mod:`omr_scanner.gui.calibration.worker`) - the one
    genuinely expensive step, because it loads the file and runs Phase 1
    registration. Every later threshold change calls
    :meth:`~omr_scanner.services.recognition_service.CalibrationSession.recompute`
    synchronously, on the GUI thread, because it costs one pass over already-
    measured bubbles and nothing more - see ``docs/calibration_workflow.md``
    for why that split is safe and why it is not premature optimisation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.template import (
    GridFieldDefinition,
    QuestionBlockFieldDefinition,
    RecognitionSettings,
    SymbolAxis,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.calibration.worker import CalibrationWorker
from omr_scanner.gui.error_reporting import report_error
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.scan.preview import ScanPreviewView
from omr_scanner.gui.theme import TEMPLATE_DESIGNER_STYLESHEET, Spacing
from omr_scanner.gui.widgets import CollapsibleSection, StatusChipStrip
from omr_scanner.services import (
    CalibrationStatus,
    ProjectSession,
    RegistrationStatus,
    active_template_is_missing,
    aggregate_calibration,
    apply_calibration,
    collect_scan_files,
    evaluate_calibration,
    load_template,
    resolve_active_template,
    save_template,
    separation_label,
    write_calibration_report,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.calibration.worker import CalibrationSessionResult
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import (
        BubbleView,
        CalibrationReport,
        CalibrationSession,
        ScanResult,
    )

_LOGGER = logging.getLogger(__name__)

CONTROL_PANEL_WIDTH = 300
DIAGNOSTIC_PANEL_WIDTH = 300
PREVIEW_MINIMUM_HEIGHT = 260
"""Floor for the preview, not its size. The splitter's stretch factors decide
the height; this only stops the drawer or a very short window squeezing the
page down to a strip."""

PREVIEW_STRETCH = 8
DRAWER_STRETCH = 2
"""How the centre column divides its height. The preview is the subject of the
page and keeps four fifths of whatever is available; with the drawer shut it
takes essentially all of it."""

FIELD_DIAGNOSTICS_MIN_HEIGHT = 140
"""Enough of the flagged-question list to be worth expanding; it scrolls past
that rather than making the right panel demand more height than the window."""

DRAWER_MINIMUM_HEIGHT = 180
"""Enough of the results table to be worth opening."""

THRESHOLD_SLIDER_STEPS = 1000
"""Slider resolution: the four thresholds are fractions in ``[0, 1]``, and a
0.001 step is finer than the difference a fill-ratio measurement can actually
resolve, so nothing is lost to the slider's own granularity."""

VIEW_MODE_REGISTERED = "Registered page"
VIEW_MODE_ORIGINAL = "Original scan"

FIELD_FILTER_ALL = "All"
FIELD_FILTER_IDENTIFIER = "Student ID"
FIELD_FILTER_SET_CODE = "Set Code"
FIELD_FILTER_QUESTIONS = "Questions"
FIELD_FILTER_OTHER = "Other fields"

BUBBLE_HIT_MARGIN_PX = 6.0
"""Extra tolerance, in canonical pixels, around a bubble's own ellipse when
deciding whether a click landed on it. A click has to be forgiving of the
pixel it actually lands on; the bubble geometry it is matched against is not
approximated at all - see :meth:`CalibrationPage._bubble_at`."""


@dataclass
class TestScanEntry:
    """One representative scan, and what calibration has learned about it.

    Attributes:
        path: The scan file.
        session: The opened registration/measurement session, once the
            background worker has finished with it.
        result: The most recent recognition result for this scan, against the
            page's current working settings.
        report: The most recent calibration verdict for this scan.
    """

    path: Path
    session: CalibrationSession | None = None
    result: ScanResult | None = None
    report: CalibrationReport | None = None

    @property
    def is_open(self) -> bool:
        """Whether this scan has been through the background worker at all."""
        return self.session is not None


@dataclass
class CalibrationPageState:
    """Everything the page knows that is not a widget.

    Attributes:
        template: The loaded template, or ``None``.
        template_path: Where it came from.
        entries: The representative scans, in the order they were added.
        working_settings: The recognition settings currently in effect on
            this page - not yet written to :attr:`template`, and not the same
            object, until an operator explicitly saves them (see
            ``docs/calibration_workflow.md`` "Working value vs. saved value").
    """

    template: OmrTemplate | None = None
    template_path: Path | None = None
    entries: list[TestScanEntry] = field(default_factory=list)
    working_settings: RecognitionSettings = field(default_factory=RecognitionSettings)


class CalibrationPage(WorkflowPage):
    """Verify and tune a template against representative real scans.

    Signals:
        edit_template_requested: ``Path`` when the operator asks to fix this
            template's geometry in the Template Designer - see
            :meth:`_emit_edit_template_requested`.
        run_finished: Every scan a run touched has been attempted - the
            signal GUI tests and any future automation wait on, mirroring
            :attr:`~omr_scanner.gui.scan.page.ScanPage.batch_finished`. Fires
            after a run that opened at least one session, cancelled or not.

    Args:
        spec: The "calibration" workflow stage description.
        parent: Optional Qt parent.
    """

    edit_template_requested = Signal(Path)
    run_finished = Signal()

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        self._session: ProjectSession | None = None
        self._zoom_is_fit = True
        """Whether the preview is following the viewport or a zoom the user chose."""
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setObjectName("calibrationPage")
        self.setStyleSheet(TEMPLATE_DESIGNER_STYLESHEET)

        self.state = CalibrationPageState()
        self._worker: CalibrationWorker | None = None
        self._suppress_selection = False
        self._current_row = -1

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_centre())
        splitter.addWidget(self._build_diagnostics_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([CONTROL_PANEL_WIDTH, 900, DIAGNOSTIC_PANEL_WIDTH])
        self.body.addWidget(splitter, stretch=1)

        self._refresh_controls()
        self._refresh_thresholds_from_working_settings()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_controls(self) -> QWidget:
        """Build the left-hand control column."""
        panel = QWidget()
        panel.setObjectName("calibrationControlPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, Spacing.XS, 0)
        layout.setSpacing(Spacing.SM)

        layout.addWidget(self._build_template_box())
        layout.addWidget(self._build_scans_box())
        # Folded by default: once the working values are right, nobody needs
        # eight sliders in view permanently, and they were what pushed Export
        # Report off the bottom of a short window.
        self.threshold_section = CollapsibleSection(
            "Recognition thresholds", self._build_threshold_box(), expanded=False
        )
        self.threshold_section.setObjectName("calibrationThresholdSection")
        layout.addWidget(self.threshold_section)
        layout.addWidget(self._build_actions_row())
        layout.addStretch(1)

        # The sidebar scrolls as a whole. Expanding the thresholds on a 768
        # pixel screen then scrolls rather than pushing the buttons below the
        # window, which is what made them unreachable before.
        scroller = QScrollArea()
        scroller.setObjectName("calibrationControlScroll")
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QScrollArea.Shape.NoFrame)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroller.setWidget(panel)
        scroller.setMaximumWidth(CONTROL_PANEL_WIDTH + 60)
        scroller.setMinimumWidth(CONTROL_PANEL_WIDTH)
        return scroller

    def _build_template_box(self) -> QGroupBox:
        box = QGroupBox("Template")
        box_layout = QVBoxLayout(box)

        self.load_template_button = QPushButton(load_icon("folder-open"), "Load Template...")
        self.load_template_button.setObjectName("calibrationLoadTemplateButton")
        self.load_template_button.clicked.connect(self._prompt_load_template)
        box_layout.addWidget(self.load_template_button)

        self.template_name_label = QLabel("No template loaded")
        self.template_name_label.setObjectName("calibrationTemplateNameLabel")
        self.template_name_label.setWordWrap(True)
        box_layout.addWidget(self.template_name_label)

        self.calibration_state_label = QLabel("")
        self.calibration_state_label.setObjectName("calibrationStateLabel")
        self.calibration_state_label.setWordWrap(True)
        box_layout.addWidget(self.calibration_state_label)

        self.edit_template_button = QPushButton(load_icon("pencil"), "Edit Template...")
        self.edit_template_button.setObjectName("editTemplateButton")
        self.edit_template_button.setToolTip(
            "Open this template in the Template Designer to fix geometry - "
            "region placement is not edited here."
        )
        self.edit_template_button.clicked.connect(self._emit_edit_template_requested)
        box_layout.addWidget(self.edit_template_button)
        return box

    def _build_scans_box(self) -> QGroupBox:
        box = QGroupBox("Representative scans")
        box_layout = QVBoxLayout(box)

        buttons_row = QWidget()
        buttons_layout = QHBoxLayout(buttons_row)
        buttons_layout.setContentsMargins(0, 0, 0, 0)

        self.add_scan_button = QPushButton(load_icon("file-plus"), "Add Scan(s)...")
        self.add_scan_button.setObjectName("addTestScanButton")
        self.add_scan_button.clicked.connect(self._prompt_add_scans)
        buttons_layout.addWidget(self.add_scan_button)

        self.add_folder_button = QPushButton(load_icon("folder-open"), "Add Folder...")
        self.add_folder_button.setObjectName("addTestScanFolderButton")
        self.add_folder_button.clicked.connect(self._prompt_add_folder)
        buttons_layout.addWidget(self.add_folder_button)
        box_layout.addWidget(buttons_row)

        self.test_scan_list = QListWidget()
        self.test_scan_list.setObjectName("testScanList")
        self.test_scan_list.setMaximumHeight(140)
        self.test_scan_list.currentRowChanged.connect(self._on_list_row_changed)
        box_layout.addWidget(self.test_scan_list)

        remove_row = QWidget()
        remove_layout = QHBoxLayout(remove_row)
        remove_layout.setContentsMargins(0, 0, 0, 0)
        self.remove_scan_button = QPushButton(load_icon("trash"), "Remove")
        self.remove_scan_button.setObjectName("removeTestScanButton")
        self.remove_scan_button.clicked.connect(self.remove_selected_scan)
        remove_layout.addWidget(self.remove_scan_button)
        self.clear_scans_button = QPushButton("Clear All")
        self.clear_scans_button.setObjectName("clearTestScansButton")
        self.clear_scans_button.clicked.connect(self.clear_scans)
        remove_layout.addWidget(self.clear_scans_button)
        box_layout.addWidget(remove_row)

        run_row = QWidget()
        run_layout = QHBoxLayout(run_row)
        run_layout.setContentsMargins(0, 0, 0, 0)
        self.run_button = QPushButton(load_icon("scan-line"), "Run Test")
        self.run_button.setObjectName("runCalibrationButton")
        self.run_button.setToolTip("Register and measure the selected scan.")
        self.run_button.clicked.connect(self.run_selected)
        run_layout.addWidget(self.run_button)
        self.run_all_button = QPushButton(load_icon("list-checks"), "Run All Tests")
        self.run_all_button.setObjectName("runAllCalibrationButton")
        self.run_all_button.clicked.connect(self.run_all)
        run_layout.addWidget(self.run_all_button)
        box_layout.addWidget(run_row)
        return box

    def _build_threshold_box(self) -> QGroupBox:
        box = QGroupBox("Recognition thresholds (working values)")
        box_layout = QVBoxLayout(box)

        self.threshold_state_label = QLabel("")
        self.threshold_state_label.setObjectName("thresholdStateLabel")
        self.threshold_state_label.setWordWrap(True)
        box_layout.addWidget(self.threshold_state_label)

        self.fill_slider, self.fill_spin = self._build_threshold_row(
            box_layout,
            "Bubble fill threshold",
            "bubbleThreshold",
            "Fraction of a bubble's interior that must be dark for it to "
            "count as marked.",
        )
        self.blank_slider, self.blank_spin = self._build_threshold_row(
            box_layout,
            "Blank threshold",
            "blankThreshold",
            "Below this, a bubble is certainly empty. Between this and the "
            "fill threshold is the ambiguous band.",
        )
        self.margin_slider, self.margin_spin = self._build_threshold_row(
            box_layout,
            "Ambiguity margin",
            "ambiguityMargin",
            "Minimum separation between the best and second-best bubble in a "
            "group before a single mark counts as resolved rather than "
            "uncertain.",
        )
        self.confidence_slider, self.confidence_spin = self._build_threshold_row(
            box_layout,
            "Minimum confidence",
            "minConfidence",
            "Below this decision score, a value is queued for human review "
            "even though it resolved to something.",
        )

        buttons_row = QWidget()
        buttons_layout = QHBoxLayout(buttons_row)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.reset_button = QPushButton(load_icon("rotate-ccw"), "Reset to Template")
        self.reset_button.setObjectName("resetCalibrationButton")
        self.reset_button.setToolTip(
            "Discard working changes; restore this template's saved values."
        )
        self.reset_button.clicked.connect(self.reset_to_template)
        buttons_layout.addWidget(self.reset_button)
        self.reset_defaults_button = QPushButton("Defaults")
        self.reset_defaults_button.setObjectName("resetToDefaultsButton")
        self.reset_defaults_button.setToolTip(
            "Restore the application's built-in default thresholds."
        )
        self.reset_defaults_button.clicked.connect(self.reset_to_defaults)
        buttons_layout.addWidget(self.reset_defaults_button)
        box_layout.addWidget(buttons_row)

        return box

    def _build_actions_row(self) -> QWidget:
        """Save and Export, outside the collapsible thresholds.

        They used to sit inside the threshold group, which was harmless while
        that group was always open and a defect the moment it folded: folding
        the sliders away also hid Save and Export Report, neither of which is
        a threshold control.
        """
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)

        self.save_button = QPushButton(load_icon("save"), "Save to Template")
        self.save_button.setObjectName("saveCalibrationButton")
        self.save_button.setToolTip(
            "Write the working thresholds and the calibration verdict back to "
            "the template file."
        )
        self.save_button.clicked.connect(self.save_to_template)
        layout.addWidget(self.save_button)

        self.export_report_button = QPushButton(load_icon("file-text"), "Export Report...")
        self.export_report_button.setObjectName("exportCalibrationReportButton")
        self.export_report_button.clicked.connect(self.export_report)
        layout.addWidget(self.export_report_button)
        return row

    def _build_threshold_row(
        self, parent_layout: QVBoxLayout, title: str, object_prefix: str, tooltip: str
    ) -> tuple[QSlider, QDoubleSpinBox]:
        """Build one "label / slider / exact spin box" threshold control.

        A slider for quick, visual exploration and a spin box for exact entry
        (spec: "the numeric input should allow exact adjustment") - kept in
        sync in both directions by :meth:`_wire_threshold_control`.
        """
        label = QLabel(title)
        label.setToolTip(tooltip)
        parent_layout.addWidget(label)

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setObjectName(f"{object_prefix}Slider")
        slider.setRange(0, THRESHOLD_SLIDER_STEPS)
        slider.setToolTip(tooltip)
        row_layout.addWidget(slider, stretch=1)

        spin = QDoubleSpinBox()
        spin.setObjectName(f"{object_prefix}SpinBox")
        spin.setRange(0.0, 1.0)
        spin.setSingleStep(0.01)
        spin.setDecimals(3)
        spin.setToolTip(tooltip)
        row_layout.addWidget(spin)
        parent_layout.addWidget(row)

        self._wire_threshold_control(slider, spin)
        return slider, spin

    def _wire_threshold_control(self, slider: QSlider, spin: QDoubleSpinBox) -> None:
        """Keep a slider and its spin box synchronised.

        Also re-applies settings on every change - see the module docstring
        on why that is cheap.
        """

        def from_slider(value: int) -> None:
            fraction = value / THRESHOLD_SLIDER_STEPS
            if abs(spin.value() - fraction) > 1e-9:
                spin.blockSignals(True)
                spin.setValue(fraction)
                spin.blockSignals(False)
            self._on_threshold_changed()

        def from_spin(value: float) -> None:
            as_slider = round(value * THRESHOLD_SLIDER_STEPS)
            if slider.value() != as_slider:
                slider.blockSignals(True)
                slider.setValue(as_slider)
                slider.blockSignals(False)
            self._on_threshold_changed()

        slider.valueChanged.connect(from_slider)
        spin.valueChanged.connect(from_spin)

    def _build_centre(self) -> QWidget:
        """Build the preview, its toolbar, the status strip and the drawer.

        The preview is the page's subject, so it gets the height: the status
        below it is one wrapped row of chips rather than a ten-line paragraph,
        and the sample table lives in a drawer that starts closed. The
        splitter's stretch factors - not a fixed height - decide the split, so
        the preview grows with the window and reclaims the drawer's space when
        it is folded away.
        """
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)

        layout.addWidget(self._build_preview_toolbar())

        self.preview = ScanPreviewView()
        self.preview.setObjectName("calibrationImageView")
        self.preview.setMinimumHeight(PREVIEW_MINIMUM_HEIGHT)
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview.clicked_scene_point.connect(self._on_preview_clicked)

        self.calibration_status_label = QLabel("No scan selected")
        self.calibration_status_label.setObjectName("calibrationStatusLabel")
        self.calibration_status_label.setWordWrap(True)
        status_font = self.calibration_status_label.font()
        status_font.setBold(True)
        self.calibration_status_label.setFont(status_font)

        self.status_chip_strip = StatusChipStrip()
        self.status_chip_strip.setObjectName("calibrationStatusChips")

        # Kept, and kept populated, but no longer shown under the preview: it
        # is the body of the details drawer. Nothing that used to be readable
        # has been deleted, only moved to where it is asked for.
        self.calibration_summary_panel = QLabel("")
        self.calibration_summary_panel.setObjectName("calibrationSummaryPanel")
        self.calibration_summary_panel.setWordWrap(True)
        self.calibration_summary_panel.setTextFormat(Qt.TextFormat.RichText)

        preview_column = QWidget()
        preview_layout = QVBoxLayout(preview_column)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(Spacing.XS)
        preview_layout.addWidget(self.preview, stretch=1)

        status_row = QWidget()
        status_row.setObjectName("calibrationStatusStrip")
        status_layout = QVBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(Spacing.XXS)
        status_layout.addWidget(self.calibration_status_label)
        status_layout.addWidget(self.status_chip_strip)
        preview_layout.addWidget(status_row)

        # Built after the summary panel exists: the drawer adopts it as its
        # "Selected scan details" section.
        self.results_drawer = CollapsibleSection(
            "Sample results", self._build_sample_summary_box(), expanded=False
        )
        self.results_drawer.setObjectName("calibrationResultsDrawer")
        self.results_drawer.toggled.connect(self._on_results_drawer_toggled)

        self.centre_splitter = QSplitter(Qt.Orientation.Vertical)
        self.centre_splitter.setObjectName("calibrationCentreSplitter")
        self.centre_splitter.setChildrenCollapsible(False)
        self.centre_splitter.addWidget(preview_column)
        self.centre_splitter.addWidget(self.results_drawer)
        # Stretch, not pixels: the preview keeps its share of whatever height
        # the window has, and takes nearly all of it while the drawer is shut.
        self.centre_splitter.setStretchFactor(0, PREVIEW_STRETCH)
        self.centre_splitter.setStretchFactor(1, DRAWER_STRETCH)
        layout.addWidget(self.centre_splitter, stretch=1)
        return container

    def _on_results_drawer_toggled(self, expanded: bool) -> None:
        """Give the drawer room when it opens, and the preview it back when it shuts."""
        total = max(1, self.centre_splitter.height())
        if expanded:
            drawer = max(
                DRAWER_MINIMUM_HEIGHT,
                total * DRAWER_STRETCH // (PREVIEW_STRETCH + DRAWER_STRETCH),
            )
            self.centre_splitter.setSizes([total - drawer, drawer])
        else:
            self.centre_splitter.setSizes([total, 0])
        # A fitted page has to be re-fitted for the viewport it now has; a
        # manually chosen zoom is the user's and is left alone.
        self._refit_preview_if_fitted()

    def _fit_preview(self) -> None:
        """Fit the page, and remember that fit is what the user asked for."""
        self._zoom_is_fit = True
        self.preview.fit_to_window()

    def _actual_size_preview(self) -> None:
        """Show the page at 100%. Also a zoom the user chose, so fit stops."""
        self._zoom_is_fit = False
        self.preview.zoom_to_actual_size()

    def _manual_zoom(self, direction: str) -> None:
        """Zoom by one step, and stop re-fitting on every viewport change.

        A zoom the user chose is theirs: once they have zoomed, collapsing the
        drawer or resizing the window must not silently throw that away and
        snap back to fit.
        """
        self._zoom_is_fit = False
        if direction == "in":
            self.preview.zoom_in()
        else:
            self.preview.zoom_out()

    def _update_drawer_summary(self) -> None:
        """Put the headline on the closed drawer's own header.

        So "is there anything in there worth opening?" is answerable without
        opening it, which is the difference between a drawer and somewhere
        information goes to be forgotten.
        """
        reports = [
            entry.report for entry in self.state.entries if entry.report is not None
        ]
        if not reports:
            self.results_drawer.set_summary("No scans tested yet")
            return
        tested = reports
        passed = sum(1 for report in reports if report.status.passed)
        review = sum(1 for report in reports if report.answers_needing_review)
        self.results_drawer.set_summary(
            f"{len(tested)} scan(s) · {passed} passed · {len(tested) - passed} failed"
            f" · {review} with review items"
        )

    def _refit_preview_if_fitted(self) -> None:
        """Re-apply fit-to-page after the viewport changes size.

        Only when fit is the current mode. The viewport changes whenever the
        drawer opens or closes or the window is resized, and a page fitted to
        the old viewport is no longer fitted to the new one.
        """
        if self._zoom_is_fit:
            self.preview.fit_to_window()

    def _build_preview_toolbar(self) -> QToolBar:
        toolbar = QToolBar("Calibration view")
        toolbar.setObjectName("calibrationPreviewToolbar")
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setMovable(False)

        self.previous_action = QAction(load_icon("undo-2"), "Previous", self)
        self.previous_action.setObjectName("previousTestScanButton")
        self.previous_action.triggered.connect(self.select_previous)
        toolbar.addAction(self.previous_action)

        self.next_action = QAction(load_icon("redo-2"), "Next", self)
        self.next_action.setObjectName("nextTestScanButton")
        self.next_action.triggered.connect(self.select_next)
        toolbar.addAction(self.next_action)
        toolbar.addSeparator()

        self.zoom_out_action = QAction(load_icon("zoom-out"), "Zoom Out", self)
        self.zoom_out_action.setObjectName("zoomOutButton")
        self.zoom_out_action.triggered.connect(lambda: self._manual_zoom("out"))
        toolbar.addAction(self.zoom_out_action)

        self.zoom_in_action = QAction(load_icon("zoom-in"), "Zoom In", self)
        self.zoom_in_action.setObjectName("zoomInButton")
        self.zoom_in_action.triggered.connect(lambda: self._manual_zoom("in"))
        toolbar.addAction(self.zoom_in_action)

        self.fit_action = QAction(load_icon("maximize"), "Fit", self)
        self.fit_action.setObjectName("fitButton")
        self.fit_action.triggered.connect(self._fit_preview)
        toolbar.addAction(self.fit_action)

        self.actual_size_action = QAction(load_icon("scan"), "100%", self)
        self.actual_size_action.setObjectName("actualSizeButton")
        self.actual_size_action.triggered.connect(self._actual_size_preview)
        toolbar.addAction(self.actual_size_action)
        toolbar.addSeparator()

        self.view_mode_combo = QComboBox()
        self.view_mode_combo.setObjectName("calibrationViewModeCombo")
        self.view_mode_combo.addItems([VIEW_MODE_REGISTERED, VIEW_MODE_ORIGINAL])
        self.view_mode_combo.setToolTip(
            "Registered page: the rectified sheet, in the coordinates recognition "
            "works in - the only view overlays can be drawn over.\n"
            "Original scan: the file exactly as it arrived, before Phase 1 "
            "corrected it."
        )
        self.view_mode_combo.currentIndexChanged.connect(self._on_view_mode_changed)
        toolbar.addWidget(self.view_mode_combo)
        toolbar.addSeparator()

        self.marker_overlay_toggle = QCheckBox("Markers")
        self.marker_overlay_toggle.setObjectName("markerOverlayToggle")
        self.marker_overlay_toggle.setChecked(True)
        self.marker_overlay_toggle.setToolTip(
            "Expected marker position (blue cross) vs detected, reprojected "
            "position (green = close, red = displaced)."
        )
        self.marker_overlay_toggle.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.marker_overlay_toggle)

        self.region_overlay_toggle = QCheckBox("Regions")
        self.region_overlay_toggle.setObjectName("regionOverlayToggle")
        self.region_overlay_toggle.setChecked(True)
        self.region_overlay_toggle.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.region_overlay_toggle)

        self.bubble_overlay_toggle = QCheckBox("Selections")
        self.bubble_overlay_toggle.setObjectName("bubbleOverlayToggle")
        self.bubble_overlay_toggle.setChecked(True)
        self.bubble_overlay_toggle.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.bubble_overlay_toggle)

        self.score_overlay_toggle = QCheckBox("All bubbles")
        self.score_overlay_toggle.setObjectName("scoreOverlayToggle")
        self.score_overlay_toggle.setToolTip("Also outline bubbles measured as empty.")
        self.score_overlay_toggle.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.score_overlay_toggle)

        self.sample_overlay_toggle = QCheckBox("Sampling")
        self.sample_overlay_toggle.setObjectName("sampleWindowOverlayToggle")
        self.sample_overlay_toggle.setToolTip(
            "Outline the exact elliptical interior the engine measured for each "
            "bubble. Smaller than the printed bubble on purpose - the printed "
            "ring is ink and is excluded from the sample."
        )
        self.sample_overlay_toggle.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.sample_overlay_toggle)

        self.center_overlay_toggle = QCheckBox("Centres")
        self.center_overlay_toggle.setObjectName("bubbleCenterOverlayToggle")
        self.center_overlay_toggle.setToolTip(
            "Mark each bubble's sampled centre. The quickest way to see a "
            "template that is displaced consistently across the page."
        )
        self.center_overlay_toggle.toggled.connect(self._refresh_overlay_visibility)
        toolbar.addWidget(self.center_overlay_toggle)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.field_filter_combo = QComboBox()
        self.field_filter_combo.setObjectName("fieldFilterCombo")
        self.field_filter_combo.addItems(
            [
                FIELD_FILTER_ALL,
                FIELD_FILTER_IDENTIFIER,
                FIELD_FILTER_SET_CODE,
                FIELD_FILTER_QUESTIONS,
                FIELD_FILTER_OTHER,
            ]
        )
        self.field_filter_combo.currentIndexChanged.connect(self._refresh_overlay_content)
        toolbar.addWidget(self.field_filter_combo)
        return toolbar

    def _build_sample_summary_box(self) -> QWidget:
        """The drawer's contents: the details, the summary line and the table.

        No longer a `QGroupBox`: the collapsible section already draws a
        titled header, and a border inside a header is the visual noise this
        pass exists to remove.
        """
        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, Spacing.XS, 0, 0)

        # The full per-scan detail that used to sit permanently under the
        # preview. Same text, same information, on request.
        self.details_section = CollapsibleSection(
            "Selected scan details", self.calibration_summary_panel, expanded=False
        )
        self.details_section.setObjectName("calibrationDetailsSection")
        box_layout.addWidget(self.details_section)

        self.sample_summary_label = QLabel("No scans tested yet.")
        self.sample_summary_label.setObjectName("calibrationSampleSummaryLabel")
        self.sample_summary_label.setWordWrap(True)
        box_layout.addWidget(self.sample_summary_label)

        self.sample_table = QTableWidget(0, 6)
        self.sample_table.setObjectName("calibrationSampleTable")
        self.sample_table.setHorizontalHeaderLabels(
            ["File", "Registration", "Student ID", "Set", "Ambiguous", "Status"]
        )
        self.sample_table.verticalHeader().setVisible(False)
        self.sample_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sample_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sample_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.sample_table.itemSelectionChanged.connect(self._on_summary_table_selected)
        box_layout.addWidget(self.sample_table)
        return box

    def _build_diagnostics_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("bubbleDiagnosticPanel")
        panel.setMaximumWidth(DIAGNOSTIC_PANEL_WIDTH + 100)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(6)

        heading = QLabel("Bubble inspector")
        heading_font = heading.font()
        heading_font.setBold(True)
        heading.setFont(heading_font)
        layout.addWidget(heading)

        self.inspector_label = QLabel("Click a bubble in the preview to inspect it.")
        self.inspector_label.setObjectName("bubbleInspectorLabel")
        self.inspector_label.setWordWrap(True)
        self.inspector_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.inspector_label)

        self.field_diagnostics_label = QLabel("Run a scan to see per-position detail.")
        self.field_diagnostics_label.setObjectName("fieldDiagnosticsLabel")
        self.field_diagnostics_label.setWordWrap(True)
        self.field_diagnostics_label.setTextFormat(Qt.TextFormat.RichText)

        # Scrolled, and folded by default. Thirty flagged questions is one
        # problem, not thirty, and a list that long used to make the right
        # panel demand more height than the window had.
        field_scroll = QScrollArea()
        field_scroll.setObjectName("fieldDiagnosticsScroll")
        field_scroll.setWidgetResizable(True)
        field_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        field_scroll.setMinimumHeight(FIELD_DIAGNOSTICS_MIN_HEIGHT)
        field_scroll.setWidget(self.field_diagnostics_label)

        self.field_diagnostics_section = CollapsibleSection(
            "Field diagnostics", field_scroll, expanded=False
        )
        self.field_diagnostics_section.setObjectName("fieldDiagnosticsSection")
        layout.addWidget(self.field_diagnostics_section)

        second_separator = QFrame()
        second_separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(second_separator)

        self.advanced_toggle = QPushButton("Show Advanced Diagnostics")
        self.advanced_toggle.setObjectName("advancedDiagnosticsButton")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.toggled.connect(self._on_advanced_toggled)
        layout.addWidget(self.advanced_toggle)

        self.advanced_panel = QLabel("")
        self.advanced_panel.setObjectName("advancedDiagnosticsPanel")
        self.advanced_panel.setWordWrap(True)
        self.advanced_panel.setVisible(False)
        layout.addWidget(self.advanced_panel)

        layout.addStretch(1)
        return panel

    # ------------------------------------------------------------------
    # Template
    # ------------------------------------------------------------------
    def _prompt_load_template(self) -> None:
        start = self._template_dialog_directory()
        path_str, _filter = QFileDialog.getOpenFileName(
            self, "Load template", str(start), "OMRFlow templates (*.omrt)"
        )
        if path_str:
            self.load_template_from(Path(path_str))

    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt the open project's template, and its folder for dialogs.

        Calibration had no project hook at all: it kept its own template path
        and its Browse dialog opened at the user's home directory, so the same
        template had to be found again here after it had already been chosen
        on the Template screen.

        The template is *consumed* from the session, never re-discovered: the
        main window settles which one the project uses, once, when the project
        opens. Loading is skipped when the same file is already loaded, so
        moving between screens does not reparse it.
        """
        self._session = session
        if session is None:
            self._clear_template()
            return

        active = resolve_active_template(session.project)
        if active is None:
            missing = active_template_is_missing(session.project)
            loaded = self.state.template_path
            # Nothing to adopt. What is already loaded is dropped when it came
            # from the project that was open a moment ago, or when it is the
            # file the project names and that file has gone - otherwise this
            # project would be calibrated against another one's geometry. A
            # template the user browsed to inside this project is left alone.
            stale = loaded is not None and session.project.layout.root not in loaded.parents
            if missing or stale:
                self._clear_template()
            if missing:
                # Said on the page, after the label has been refreshed, and
                # only here. A dialog would appear every time the user walked
                # past this screen.
                self._set_template_status("The project's template is missing. Load a replacement.")
            return

        if self.state.template_path != active or self.state.template is None:
            self.load_template_from(active)

    def _clear_template(self) -> None:
        """Forget the loaded template, because the project no longer names one."""
        self.state.template = None
        self.state.template_path = None
        self._refresh_template_label()
        self._refresh_controls()

    def _set_template_status(self, message: str) -> None:
        """Show a one-line note about the project's template."""
        self.template_name_label.setText(message)

    def _template_dialog_directory(self) -> Path:
        """Where the template browser opens.

        The project root first: when a project is open, every file it owns is
        under there, and starting anywhere else makes the user navigate back
        to the project they already told the application about. Falls back to
        the loaded template's folder, then to the home directory, so the page
        still behaves sensibly with no project open.
        """
        if self._session is not None:
            return self._session.root
        if self.state.template_path is not None:
            return self.state.template_path.parent
        return Path.home()

    def load_template_from(self, path: Path) -> bool:
        """Load the template at ``path`` and reset the working state.

        Returns:
            ``True`` when it loaded.
        """
        try:
            template = load_template(path)
        except OMRScannerError as exc:
            report_error(self, exc, context="Load template")
            return False

        self.state.template = template
        self.state.template_path = path
        self.state.entries.clear()
        self.state.working_settings = template.recognition
        self.test_scan_list.clear()
        self._current_row = -1
        self._refresh_thresholds_from_working_settings()
        self._refresh_template_label()
        self._refresh_preview()
        self._refresh_sample_summary()
        self._refresh_controls()
        _LOGGER.info("Calibration page loaded template '%s' from %s", template.name, path)
        return True

    def _refresh_template_label(self) -> None:
        template = self.state.template
        if template is None:
            self.template_name_label.setText("No template loaded")
            self.calibration_state_label.setText("")
            return
        path_name = self.state.template_path.name if self.state.template_path else ""
        self.template_name_label.setText(f"<b>{template.name}</b><br>{path_name}")
        if not template.calibration.is_recorded:
            self.calibration_state_label.setText("Never calibrated.")
        elif template.is_calibration_current():
            record = template.calibration
            self.calibration_state_label.setText(
                f"Calibration current: {record.status.replace('_', ' ')} "
                f"({record.sample_count} scan(s), {record.validated_at})."
            )
        else:
            self.calibration_state_label.setText(
                "Validation out of date - the template's geometry or recognition "
                "settings have changed since it was last calibrated."
            )

    def _emit_edit_template_requested(self) -> None:
        """Ask the main window to open this template in the Template Designer.

        A shortcut, not a second geometry editor - see the module docstring.
        The main window owns cross-page navigation, so this page only asks.
        """
        if self.state.template_path is not None:
            self.edit_template_requested.emit(self.state.template_path)

    # ------------------------------------------------------------------
    # Representative scans
    # ------------------------------------------------------------------
    def _scan_dialog_directory(self) -> Path:
        """Where the scan browser opens: the project's own scans, if there is one."""
        if self._session is not None:
            return self._session.project.layout.scans_original_dir
        return Path.home()

    def _prompt_add_scans(self) -> None:
        patterns = "Scanned sheets (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)"
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "Add representative scans", str(self._scan_dialog_directory()), patterns
        )
        if paths:
            self.add_scan_paths([Path(item) for item in paths])

    def _prompt_add_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Add a folder of representative scans", str(self._scan_dialog_directory())
        )
        if directory:
            self.add_scan_paths([Path(directory)])

    def add_scan_paths(self, selection: Sequence[Path]) -> int:
        """Add files and/or folders as representative scans.

        Returns:
            How many were added; a scan already in the list is skipped.
        """
        known = {entry.path.resolve() for entry in self.state.entries}
        added = 0
        for path in collect_scan_files(selection):
            resolved = path.resolve()
            if resolved in known:
                continue
            known.add(resolved)
            self.state.entries.append(TestScanEntry(path=path))
            self.test_scan_list.addItem(QListWidgetItem(path.name))
            added += 1
        if added and self.test_scan_list.currentRow() < 0:
            self.test_scan_list.setCurrentRow(0)
        self._refresh_controls()
        return added

    def remove_selected_scan(self) -> None:
        """Remove the currently selected scan from the sample."""
        row = self.test_scan_list.currentRow()
        if not 0 <= row < len(self.state.entries):
            return
        del self.state.entries[row]
        self.test_scan_list.takeItem(row)
        self._refresh_sample_summary()
        self._refresh_controls()

    def clear_scans(self) -> None:
        """Empty the sample, the preview and the summary."""
        if self._worker is not None and self._worker.isRunning():
            return
        self.state.entries.clear()
        self.test_scan_list.clear()
        self._current_row = -1
        self.preview.clear()
        self._show_status(None)
        self._refresh_sample_summary()
        self._refresh_controls()

    def _default_row(self) -> int:
        rows = sorted({index.row() for index in self.test_scan_list.selectedIndexes()})
        if rows:
            return rows[0]
        return self.test_scan_list.currentRow()

    # ------------------------------------------------------------------
    # Running (opening sessions)
    # ------------------------------------------------------------------
    def run_selected(self) -> bool:
        """Register and measure the currently selected scan."""
        row = self._default_row()
        if not 0 <= row < len(self.state.entries):
            return False
        return self._start_worker([self.state.entries[row]])

    def run_all(self) -> bool:
        """Register and measure every representative scan."""
        if not self.state.entries:
            return False
        return self._start_worker(list(self.state.entries))

    def _start_worker(self, entries: list[TestScanEntry]) -> bool:
        if self.state.template is None or not entries:
            return False
        if self._worker is not None and self._worker.isRunning():
            return False

        template = self._working_template()
        worker = CalibrationWorker([entry.path for entry in entries], template, self)
        worker.session_ready.connect(self._on_session_ready)
        worker.finished_all.connect(self._on_worker_finished)
        worker.failed.connect(self._on_worker_failed)
        self._worker = worker
        self.calibration_status_label.setText("Running calibration...")
        worker.start()
        self._refresh_controls()
        return True

    def _on_session_ready(self, item: CalibrationSessionResult) -> None:
        entry = next((e for e in self.state.entries if e.path == item.path), None)
        if entry is None:
            return
        entry.session = item.session
        # Re-decide against the settings in force *now*, not the ones captured
        # when the worker started: an operator may have moved a slider while
        # this scan was still registering, and `_recompute_all` skipped it
        # because it had no session yet. Judging `item.result` (old settings)
        # with the current working template would show a verdict and a result
        # that disagree about which thresholds produced them.
        template = self._working_template()
        entry.result = item.session.recompute(template)
        entry.report = evaluate_calibration(entry.result, template)
        self._refresh_sample_summary()
        if self._current_selected_entry() is entry:
            self._show_entry(entry)

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._refresh_controls()
        current = self._current_selected_entry()
        if current is not None:
            self._show_entry(current)
        self.run_finished.emit()

    def _on_worker_failed(self, message: str) -> None:
        self._worker = None
        self.calibration_status_label.setText("Calibration could not run.")
        self._refresh_controls()
        _LOGGER.error("Calibration worker failed: %s", message)
        QMessageBox.warning(self, "Calibration", message)
        self.run_finished.emit()

    def _working_template(self) -> OmrTemplate:
        """Return the loaded template with the working settings substituted.

        Never mutates or saves :attr:`CalibrationPageState.template` - see
        :meth:`save_to_template` for the one place that does.
        """
        template = self.state.template
        if template is None:  # pragma: no cover - guarded by callers
            raise RuntimeError("No template loaded")
        return template.model_copy(update={"recognition": self.state.working_settings})

    # ------------------------------------------------------------------
    # Thresholds
    # ------------------------------------------------------------------
    def _refresh_thresholds_from_working_settings(self) -> None:
        settings = self.state.working_settings
        for spin, value in (
            (self.fill_spin, settings.fill_ratio_threshold),
            (self.blank_spin, settings.blank_ratio_threshold),
            (self.margin_spin, settings.ambiguity_margin),
            (self.confidence_spin, settings.min_confidence),
        ):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        for slider, value in (
            (self.fill_slider, settings.fill_ratio_threshold),
            (self.blank_slider, settings.blank_ratio_threshold),
            (self.margin_slider, settings.ambiguity_margin),
            (self.confidence_slider, settings.min_confidence),
        ):
            slider.blockSignals(True)
            slider.setValue(round(value * THRESHOLD_SLIDER_STEPS))
            slider.blockSignals(False)

    def _on_threshold_changed(self) -> None:
        """A threshold moved: rebuild working settings and reclassify at once.

        No file read, no marker detection, no perspective warp - every open
        session's cached measurements are simply re-decided
        (``docs/calibration_workflow.md``, "immediate feedback").
        """
        if self.state.template is None:
            return
        try:
            settings = RecognitionSettings(
                fill_ratio_threshold=self.fill_spin.value(),
                blank_ratio_threshold=self.blank_spin.value(),
                ambiguity_margin=self.margin_spin.value(),
                min_confidence=self.confidence_spin.value(),
            )
        except ValueError:
            # The blank threshold briefly exceeded the fill threshold while the
            # two spin boxes were both being typed into; the invalid
            # combination is simply not applied yet.
            return
        self.state.working_settings = settings
        self._recompute_all()

    def _recompute_all(self) -> None:
        template = self._working_template()
        for entry in self.state.entries:
            if entry.session is None:
                continue
            entry.result = entry.session.recompute(template)
            entry.report = evaluate_calibration(entry.result, template)
        self._refresh_sample_summary()
        self._refresh_threshold_state_label()
        current = self._current_selected_entry()
        if current is not None:
            self._show_entry(current)

    def reset_to_template(self) -> None:
        """Discard working changes; restore the template's own saved values."""
        if self.state.template is None:
            return
        self.state.working_settings = self.state.template.recognition
        self._refresh_thresholds_from_working_settings()
        self._recompute_all()

    def reset_to_defaults(self) -> None:
        """Restore the application's built-in default thresholds."""
        self.state.working_settings = RecognitionSettings()
        self._refresh_thresholds_from_working_settings()
        self._recompute_all()

    def save_to_template(self) -> None:
        """Write the working thresholds and calibration verdict to the template.

        Nothing is written until this is explicitly called - see the module
        and state docstrings on "working value vs. saved value".
        """
        template = self.state.template
        if template is None or self.state.template_path is None:
            return
        tested = [entry for entry in self.state.entries if entry.report is not None]
        sample = aggregate_calibration([entry.report for entry in tested if entry.report])

        confirm = QMessageBox.question(
            self,
            "Save calibration",
            (
                f"Save these recognition thresholds to '{template.name}', recording "
                f"a calibration run over {len(tested)} scan(s) with status "
                f"'{sample.status.label}'?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        updated = template.model_copy(update={"recognition": self.state.working_settings})
        updated = apply_calibration(updated, status=sample.status, sample_count=len(tested))
        try:
            save_template(updated, self.state.template_path)
        except OMRScannerError as exc:
            report_error(self, exc, context="Save calibration")
            return

        self.state.template = updated
        self._refresh_template_label()
        self._refresh_threshold_state_label()
        _LOGGER.info(
            "Calibration saved to %s: status=%s sample=%d",
            self.state.template_path,
            sample.status.value,
            len(tested),
        )

    def export_report(self) -> None:
        """Write the current sample's calibration report to a JSON file."""
        template = self.state.template
        if template is None:
            return
        tested = [entry.report for entry in self.state.entries if entry.report is not None]
        sample = aggregate_calibration(tested)
        exports = (
            self._session.project.layout.exports_dir
            if self._session is not None
            else Path.home()
        )
        start = str(exports / "calibration_report.json")
        path_str, _filter = QFileDialog.getSaveFileName(
            self, "Export calibration report", start, "JSON files (*.json)"
        )
        if not path_str:
            return
        try:
            written = write_calibration_report(sample, template, Path(path_str))
        except OSError as exc:
            QMessageBox.warning(self, "Export calibration report", str(exc))
            return
        self.calibration_status_label.setText(f"Report written to {written.name}")

    # ------------------------------------------------------------------
    # Selection and display
    # ------------------------------------------------------------------
    def _on_list_row_changed(self, row: int) -> None:
        if self._suppress_selection:
            return
        self._current_row = row
        entry = self._current_selected_entry()
        if entry is not None:
            self._show_entry(entry)
        else:
            self.preview.clear()
            self._show_status(None)
        self._refresh_controls()

    def _current_selected_entry(self) -> TestScanEntry | None:
        row = self._current_row
        if 0 <= row < len(self.state.entries):
            return self.state.entries[row]
        return None

    def select_previous(self) -> None:
        """Select the previous representative scan."""
        row = self.test_scan_list.currentRow()
        if row > 0:
            self.test_scan_list.setCurrentRow(row - 1)

    def select_next(self) -> None:
        """Select the next representative scan."""
        row = self.test_scan_list.currentRow()
        if row + 1 < len(self.state.entries):
            self.test_scan_list.setCurrentRow(row + 1)

    def _on_summary_table_selected(self) -> None:
        rows = sorted({index.row() for index in self.sample_table.selectedIndexes()})
        if rows and rows[0] != self.test_scan_list.currentRow():
            self.test_scan_list.setCurrentRow(rows[0])

    def _on_view_mode_changed(self) -> None:
        entry = self._current_selected_entry()
        if entry is not None:
            self._refresh_preview_mode(entry)

    def _refresh_preview_mode(self, entry: TestScanEntry) -> None:
        """Show either the rectified page or the original scan.

        The overlay is drawn **only** over the registered page. Every overlay
        coordinate - zone, bubble, marker - is in canonical pixels, which is a
        frame the original scan is not in; painting them over it would put
        every ellipse in the wrong place while looking entirely plausible.
        Rather than mapping them backwards (a second geometry path, which
        Phase 4 must not create), the original view simply shows the image.
        """
        result = entry.result
        if result is None:
            return
        showing_original = self.view_mode_combo.currentText() == VIEW_MODE_ORIGINAL
        session = entry.session
        if showing_original and session is not None and session.registered:
            image, scale, width, height = session.original_preview()
            self.preview.set_page(
                image, canonical_width=width, canonical_height=height, preview_scale=scale
            )
            self.preview.set_overlay((), (), ())
            self.preview.set_scan_quality(None)
            self.preview.set_overlay_visible(zones=False, bubbles=False, empty=False)
        else:
            if showing_original:
                # Nothing was cached to show - say so rather than silently
                # displaying the rectified page under the wrong label.
                self.preview.clear()
                return
            self.preview.set_page(
                result.preview,
                canonical_width=result.canonical_width,
                canonical_height=result.canonical_height,
                preview_scale=result.preview_scale,
            )
            self._refresh_overlay_content()
        self.preview.fit_to_window()

    @property
    def overlay_is_drawable(self) -> bool:
        """Whether the current view is the one overlays belong in."""
        return self.view_mode_combo.currentText() == VIEW_MODE_REGISTERED

    def _show_entry(self, entry: TestScanEntry) -> None:
        result = entry.result
        if result is None:
            self.preview.clear()
            self._show_status(None)
            self.calibration_summary_panel.setText(
                "Not run yet - press Run Test to register and measure this scan."
            )
            self.status_chip_strip.set_chips([])
            self.field_diagnostics_label.setText("Run a scan to see per-position detail.")
            return

        self._refresh_preview_mode(entry)
        self._show_status(entry.report)
        self.calibration_summary_panel.setText(_quality_summary_html(result, entry.report))
        self.status_chip_strip.set_chips(_status_chips(result, entry.report))
        self._update_drawer_summary()
        self.inspector_label.setText("Click a bubble in the preview to inspect it.")
        self.field_diagnostics_label.setText(_field_diagnostics_html(result))
        review = entry.report.answers_needing_review if entry.report else 0
        self.field_diagnostics_section.set_summary(
            f"{review} question(s) require review" if review else "No questions flagged"
        )
        self.advanced_panel.setText(_advanced_diagnostics_html(result))

    def _show_status(self, report: CalibrationReport | None) -> None:
        if report is None:
            self.calibration_status_label.setText("No scan selected")
            self.calibration_status_label.setStyleSheet("")
            return
        self.calibration_status_label.setText(report.status.label)
        self.calibration_status_label.setStyleSheet(f"color: {_STATUS_TEXT_COLOR[report.status]};")

    # ------------------------------------------------------------------
    # Overlay
    # ------------------------------------------------------------------
    def _refresh_overlay_visibility(self) -> None:
        if not self.overlay_is_drawable:
            # Original-scan mode: overlay coordinates do not apply to what is
            # on screen. See `_refresh_preview_mode`.
            return
        self.preview.set_overlay_visible(
            zones=self.region_overlay_toggle.isChecked(),
            bubbles=self.bubble_overlay_toggle.isChecked(),
            empty=self.score_overlay_toggle.isChecked(),
            markers=self.marker_overlay_toggle.isChecked(),
            sample_windows=self.sample_overlay_toggle.isChecked(),
            centers=self.center_overlay_toggle.isChecked(),
        )

    def _refresh_overlay_content(self) -> None:
        entry = self._current_selected_entry()
        if entry is None or entry.result is None or self.state.template is None:
            return
        if not self.overlay_is_drawable:
            return
        result = entry.result
        bubbles = _filter_bubbles(
            result.bubbles, self.field_filter_combo.currentText(), result, self.state.template
        )
        self.preview.set_overlay(result.zones, bubbles, result.markers)
        self.preview.set_scan_quality(result.scan_quality)
        self._refresh_overlay_visibility()

    def _refresh_preview(self) -> None:
        self.preview.clear()

    def _on_preview_clicked(self, x: float, y: float) -> None:
        entry = self._current_selected_entry()
        if entry is None or entry.result is None or self.state.template is None:
            return
        if not self.overlay_is_drawable:
            # The click is in the original scan's own pixels; matching it
            # against canonical bubble geometry would report a bubble the
            # operator did not click on.
            self.inspector_label.setText(
                "Switch to the registered page to inspect individual bubbles."
            )
            return
        bubble = _bubble_at(entry.result.bubbles, x, y)
        if bubble is None:
            return
        self.inspector_label.setText(_bubble_inspector_html(bubble, self.state.template))

    def _on_advanced_toggled(self, checked: bool) -> None:
        self.advanced_panel.setVisible(checked)
        self.advanced_toggle.setText(
            "Hide Advanced Diagnostics" if checked else "Show Advanced Diagnostics"
        )

    # ------------------------------------------------------------------
    # Sample summary
    # ------------------------------------------------------------------
    def _refresh_sample_summary(self) -> None:
        reports = [entry.report for entry in self.state.entries if entry.report is not None]
        sample = aggregate_calibration(reports) if reports else None

        self.sample_table.setRowCount(len(self.state.entries))
        for row, entry in enumerate(self.state.entries):
            result = entry.result
            report = entry.report
            values = (
                entry.path.name,
                _registration_label(result),
                report.identifier_value if report else "",
                report.set_code_value if report else "",
                str(report.ambiguous_count) if report else "",
                report.status.label if report else "Not run",
            )
            for column, text in enumerate(values):
                self.sample_table.setItem(row, column, QTableWidgetItem(text))

        if sample is None:
            self.sample_summary_label.setText(
                f"{len(self.state.entries)} scan(s) loaded; none tested yet."
            )
            return

        all_bubbles = tuple(
            bubble
            for entry in self.state.entries
            if entry.result is not None
            for bubble in entry.result.bubbles
        )
        self.sample_summary_label.setText(
            f"Calibration sample: {sample.scan_count} scan(s)<br>"
            f"Registration successful: {sample.registered_count} / {sample.scan_count}<br>"
            f"Scans needing review: {sample.review_count} · Failed: {sample.failed_count}<br>"
            f"Scans with ambiguity: {sample.scans_with_ambiguity} · "
            f"with multiple marks: {sample.scans_with_multiple_marks}<br>"
            f"Bubble-score separation: {separation_label(all_bubbles)}<br>"
            f"<b>Sample status: {sample.status.label}</b>"
        )

    # ------------------------------------------------------------------
    # Enablement
    # ------------------------------------------------------------------
    def _refresh_threshold_state_label(self) -> None:
        """Say plainly whether the working thresholds differ from the saved ones.

        Spec section 26/62: experimentation must never be silently persisted,
        and the operator must never have to guess which of the two values is
        in force. The page is explicit in both directions.
        """
        template = self.state.template
        if template is None:
            self.threshold_state_label.setText("")
            return
        if self.state.working_settings == template.recognition:
            self.threshold_state_label.setText(
                "<span style='color:#1B7F3A;'>Matching the template's saved values.</span>"
            )
        else:
            self.threshold_state_label.setText(
                "<span style='color:#9A6A00;'>Modified - not saved to the template "
                "yet. Use Save to Template to keep these, or Reset to Template to "
                "discard them.</span>"
            )

    def _refresh_controls(self) -> None:
        self._refresh_threshold_state_label()
        has_template = self.state.template is not None
        has_scans = bool(self.state.entries)
        running = self._worker is not None and self._worker.isRunning()
        has_selection = 0 <= self.test_scan_list.currentRow() < len(self.state.entries)

        self.add_scan_button.setEnabled(has_template and not running)
        self.add_folder_button.setEnabled(has_template and not running)
        self.remove_scan_button.setEnabled(has_selection and not running)
        self.clear_scans_button.setEnabled(has_scans and not running)
        self.run_button.setEnabled(has_template and has_selection and not running)
        self.run_all_button.setEnabled(has_template and has_scans and not running)
        self.previous_action.setEnabled(has_scans)
        self.next_action.setEnabled(has_scans)
        any_report = any(entry.report is not None for entry in self.state.entries)
        self.save_button.setEnabled(has_template and any_report and not running)
        self.export_report_button.setEnabled(any_report and not running)
        for control in (
            self.fill_slider, self.fill_spin, self.blank_slider, self.blank_spin,
            self.margin_slider, self.margin_spin, self.confidence_slider, self.confidence_spin,
            self.reset_button, self.reset_defaults_button,
        ):
            control.setEnabled(has_template)


_STATUS_TEXT_COLOR: dict[CalibrationStatus, str] = {
    CalibrationStatus.PASSED: "#1B7F3A",
    CalibrationStatus.PASSED_WITH_WARNINGS: "#9A6A00",
    CalibrationStatus.NEEDS_REVIEW: "#B15C00",
    CalibrationStatus.FAILED: "#B3261E",
}


def _registration_label(result: ScanResult | None) -> str:
    if result is None:
        return ""
    if result.registration is RegistrationStatus.FAILED:
        return "FAILED"
    if result.registration is RegistrationStatus.REGISTERED_WITH_WARNING:
        return "WARNING"
    return "PASS"


def _bubble_at(bubbles: Sequence[BubbleView], x: float, y: float) -> BubbleView | None:
    """Return the bubble whose printed ellipse contains ``(x, y)``, if any.

    Hit-testing deliberately uses the *printed* bubble extent (plus
    :data:`BUBBLE_HIT_MARGIN_PX`) rather than the smaller sampled interior:
    an operator aims at the bubble they can see on the page, and requiring
    them to land inside the 62 per-cent interior would make inspection
    needlessly fiddly. Nothing is approximated either way - both extents come
    from the engine's own
    :class:`~omr_scanner.services.recognition_models.BubbleView`, and what the
    inspector then *reports* is the sampled geometry, not this one.

    Args:
        bubbles: Every measured bubble on this sheet - already the recognition
            engine's own geometry (:attr:`~omr_scanner.services.recognition_models.BubbleView.x`),
            never recomputed here.
        x: Click position, canonical pixels.
        y: Click position, canonical pixels.

    Returns:
        The closest bubble whose (slightly enlarged) printed ellipse contains
        the point, or ``None``.
    """
    best: BubbleView | None = None
    best_distance = float("inf")
    for bubble in bubbles:
        half_x = bubble.width / 2.0 + BUBBLE_HIT_MARGIN_PX
        half_y = bubble.height / 2.0 + BUBBLE_HIT_MARGIN_PX
        if half_x <= 0.0 or half_y <= 0.0:
            continue
        normalised = ((x - bubble.x) / half_x) ** 2 + ((y - bubble.y) / half_y) ** 2
        if normalised <= 1.0 and normalised < best_distance:
            best_distance = normalised
            best = bubble
    return best


def _filter_bubbles(
    bubbles: Sequence[BubbleView],
    filter_name: str,
    result: ScanResult,
    template: OmrTemplate,
) -> tuple[BubbleView, ...]:
    """Return only the bubbles belonging to the selected field-level filter.

    The category comes from information already on the result and the
    template - which zone recognition chose as the identifier or set code,
    and which zone's field is a question block - never from re-deriving
    anything about a bubble's geometry.
    """
    if filter_name == FIELD_FILTER_ALL:
        return tuple(bubbles)

    question_zones = {
        zone.id for zone in template.zones if isinstance(zone.field, QuestionBlockFieldDefinition)
    }

    def category(zone_id: str) -> str:
        if zone_id == result.identifier_zone_id:
            return FIELD_FILTER_IDENTIFIER
        if zone_id == result.set_code_zone_id:
            return FIELD_FILTER_SET_CODE
        if zone_id in question_zones:
            return FIELD_FILTER_QUESTIONS
        return FIELD_FILTER_OTHER

    return tuple(bubble for bubble in bubbles if category(bubble.zone_id) == filter_name)


def _question_number(zone_field: object, bubble: BubbleView) -> int | None:
    """Return the printed question number a bubble belongs to, if it is one.

    Reads the same axis convention
    :class:`~omr_scanner.domain.template.QuestionBlockFieldDefinition` already
    declares (one row per question when ``HORIZONTAL``, one column per
    question when ``VERTICAL``) - a presentation label, not a recognition
    computation; ``tests/gui/test_calibration_page.py`` cross-checks it
    against the engine's own recognised answer numbers so it can never
    silently drift from that convention.
    """
    if not isinstance(zone_field, QuestionBlockFieldDefinition):
        return None
    offset = bubble.row if zone_field.symbol_axis is SymbolAxis.HORIZONTAL else bubble.column
    return zone_field.first_question + offset


def _bubble_inspector_html(bubble: BubbleView, template: OmrTemplate) -> str:
    """Render the compact per-bubble diagnostic (spec section 17)."""
    zone = next((item for item in template.zones if item.id == bubble.zone_id), None)
    field_label = zone.label if zone is not None else bubble.zone_id
    identity = f"{field_label}"
    if zone is not None and isinstance(zone.field, QuestionBlockFieldDefinition):
        number = _question_number(zone.field, bubble)
        if number is not None:
            identity = f"Question {number}"
    elif zone is not None and isinstance(zone.field, GridFieldDefinition):
        identity = f"{field_label} — position {bubble.column + 1}"

    settings = template.effective_recognition(zone) if zone is not None else template.recognition
    classification = "FILLED" if bubble.selected else bubble.group_status.upper()
    # The sampled ellipse, not the printed bubble - this panel's job is to say
    # what the engine measured, and those are two different regions.
    window = (
        f"{bubble.sample_half_width * 2.0:.1f} x {bubble.sample_half_height * 2.0:.1f} px"
        if bubble.sample_half_width > 0.0
        else "not recorded"
    )
    return (
        f"<b>{identity} — value {bubble.label or '(none)'}</b><br>"
        f"Row {bubble.row}, column {bubble.column}<br>"
        f"Centre: x={bubble.x:.1f}, y={bubble.y:.1f} px<br>"
        f"Sampling window (ellipse): {window}<br>"
        f"Printed bubble: {bubble.width:.1f} x {bubble.height:.1f} px<br>"
        f"Fill score: {bubble.fill_ratio:.3f}<br>"
        f"Fill threshold: {settings.fill_ratio_threshold:.3f} · "
        f"Blank threshold: {settings.blank_ratio_threshold:.3f}<br>"
        f"Mean darkness: {bubble.mean_darkness:.3f} · Contrast: {bubble.contrast:.3f}<br>"
        f"Ink threshold: {bubble.ink_threshold:.1f} · Paper: {bubble.paper_level:.1f}<br>"
        f"Rank in group: {bubble.rank + 1}<br>"
        f"Classification: <b>{classification}</b><br>"
        f"Usable sample: {'yes' if bubble.usable else 'no'} ({bubble.sample_pixels} px)"
    )


def _quality_summary_html(result: ScanResult, report: CalibrationReport | None) -> str:
    """Render the per-scan recognition quality summary (spec section 28)."""
    # A sheet that never registered has no marker count to report. Printing
    # "0 / 4" for it stated something the engine never measured - registration
    # can fail with all four markers found and only the orientation mark
    # unresolved, and "0 / 4" sent a real investigation looking for a marker
    # detection fault that did not exist.
    registered = bool(report and report.registered)
    markers = (
        f"{report.markers_detected} / 4"
        if registered and report is not None
        else "not reached"
    )
    orientation_state = (
        ("resolved" if (report and report.orientation_ok) else "assumed/unresolved")
        if registered
        else "not reached"
    )
    lines = [
        f"Registration: <b>{_registration_label(result)}</b>",
        f"Markers detected: {markers}",
        f"Orientation: {orientation_state}",
    ]
    identifier = result.identifier
    if identifier is not None:
        lines.append(
            f"Student ID: recognised '{identifier.value}' "
            f"({'needs review' if identifier.needs_review else 'no warnings'})"
        )
    set_code = result.set_code
    if set_code is not None:
        lines.append(
            f"Set code: recognised '{set_code.value}' "
            f"({'needs review' if set_code.needs_review else 'no warnings'})"
        )
    if report is not None:
        lines.append(
            f"Questions: {report.answers_total} total — "
            f"single: {report.answers_single}, blank: {report.answers_blank}, "
            f"multiple: {report.answers_multiple}, "
            f"flagged for review: {report.answers_needing_review}"
        )
        lines.append(
            f"Marks detected in {report.groups_with_marks} of "
            f"{report.groups_total} response positions"
        )
        lines.append(f"Near-threshold bubbles: {report.near_threshold_count}")
        lines.append(
            f"Unusable sampling windows: {report.bubbles_unusable} / {report.bubbles_total}"
        )
        for finding in report.findings:
            lines.append(f"⚠ {finding.message}")
    return "<br>".join(lines)


def _status_chips(
    result: ScanResult, report: CalibrationReport | None
) -> list[tuple[str, str]]:
    """The one-line summary shown permanently under the preview.

    Returns ``(tone, text)`` pairs, where tone is ``ok``, ``warn``, ``fail``
    or ``neutral``. Every chip carries a word as well as a colour, because a
    colour alone is not a status anybody can read.

    Deliberately the *headline* only. The same numbers in full - the response
    positions, the near-threshold count, the unusable sampling windows - stay
    in the details drawer, so the page shows the answer and keeps the working
    out one click away rather than spending a tenth of the workspace on it.
    """
    chips: list[tuple[str, str]] = []
    registered = bool(report and report.registered)

    if registered and report is not None:
        chips.append(("ok", f"Registration {report.markers_detected}/4"))
        chips.append(
            ("ok", "Orientation") if report.orientation_ok
            else ("warn", "Orientation assumed")
        )
    else:
        chips.append(("fail", "Registration failed"))

    identifier = result.identifier
    if identifier is not None:
        chips.append(
            ("warn" if identifier.needs_review else "neutral", f"ID {identifier.value}")
        )
    set_code = result.set_code
    if set_code is not None:
        chips.append(
            ("warn" if set_code.needs_review else "neutral", f"Set {set_code.value}")
        )

    if report is not None and registered:
        chips.append(("neutral", f"{report.answers_single} marked"))
        chips.append(("neutral", f"{report.answers_blank} blank"))
        if report.answers_multiple:
            chips.append(("warn", f"{report.answers_multiple} multiple"))
        if report.answers_needing_review:
            chips.append(("warn", f"{report.answers_needing_review} review"))
    return chips


MAX_LISTED_QUESTIONS = 12
"""How many flagged questions the field-diagnostics panel lists before saying
how many more there are. A calibration scan with a hundred flagged questions
has one problem, not a hundred, and a panel that scrolls for a page hides it."""


def _field_diagnostics_html(result: ScanResult) -> str:
    """Render per-position detail for each recognised field, plus flagged questions.

    Every number here comes from
    :class:`~omr_scanner.services.recognition_models.CharacterView` - the
    per-position decisions the engine already made. Positions are iterated
    generically, so a multi-position or multi-character set code ("10", "11",
    "12") is reported exactly as the template defines it rather than being
    assumed to be a single letter.
    """
    if not result.fields and not result.answers:
        return "No fields were recognised on this scan."

    lines: list[str] = []
    for item in result.fields:
        flag = " ⚠" if item.needs_review else ""
        lines.append(f"<b>{item.label}</b> — '{item.value}' ({item.status}){flag}")
        for character in item.characters:
            symbol = character.value or "(blank)"
            mark = " ⚠" if character.status not in ("resolved", "blank") else ""
            lines.append(
                f"&nbsp;&nbsp;{character.position + 1}: {symbol} · {character.status} · "
                f"fill {character.top_fill:.2f} · margin {character.margin:.2f} · "
                f"conf {character.confidence:.2f}{mark}"
            )

    flagged = [answer for answer in result.answers if answer.needs_review]
    if result.answers:
        lines.append(
            f"<b>Questions</b> — {len(flagged)} of {len(result.answers)} flagged"
        )
        for answer in flagged[:MAX_LISTED_QUESTIONS]:
            shown = answer.value or "(none)"
            lines.append(
                f"&nbsp;&nbsp;Q{answer.number}: {shown} · {answer.status} · "
                f"fill {answer.top_fill:.2f} · margin {answer.margin:.2f}"
            )
        if len(flagged) > MAX_LISTED_QUESTIONS:
            lines.append(f"&nbsp;&nbsp;... and {len(flagged) - MAX_LISTED_QUESTIONS} more")
    return "<br>".join(lines)


def _advanced_diagnostics_html(result: ScanResult) -> str:
    """Render the technical registration numbers (spec section 12)."""
    quality = result.quality
    lines = [
        f"Canonical page: {result.canonical_width} x {result.canonical_height} px",
        f"Source scan: {result.source_width} x {result.source_height} px",
        f"Engine: {result.engine_name} {result.engine_version}",
    ]
    if quality is not None:
        lines.extend(
            [
                f"Marker score (min): {quality.min_marker_score:.3f}",
                f"Reprojection error: mean {quality.mean_reprojection_error_px:.3f} px, "
                f"max {quality.max_reprojection_error_px:.3f} px",
                f"Aspect ratio deviation: {quality.aspect_ratio_deviation:.3f}",
                f"Rotation: {quality.rotation_degrees:.2f}° · Skew: {quality.skew_degrees:.2f}°",
                f"Perspective strength: {quality.perspective_strength:.4f}",
                f"Orientation confidence: {quality.orientation_confidence:.3f} "
                f"({'assumed' if quality.orientation_assumed else 'measured'})",
                f"Brightness: {quality.brightness:.3f} · Contrast: {quality.contrast:.3f} · "
                f"Sharpness: {quality.sharpness:.4f}",
            ]
        )
    lines.append(
        f"Timings (s): load {result.timings.load:.3f}, register {result.timings.register:.3f}, "
        f"measure {result.timings.measure:.3f}, decide {result.timings.decide:.3f}, "
        f"present {result.timings.present:.3f}"
    )
    return "<br>".join(lines)


__all__ = ["CalibrationPage", "CalibrationPageState", "TestScanEntry"]
