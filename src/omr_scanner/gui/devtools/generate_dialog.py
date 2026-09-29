"""Asking what synthetic dataset to generate.

Purpose:
    Collect the handful of decisions that define a dataset - template, where to
    put it, how many sheets, which profile, which format, which seed - and hand
    them back as one plain object.

Responsibilities:
    * The form, its defaults and its validation.
    * :class:`GenerationRequest`, which is what the rest of the feature works
      from.

What does NOT belong here:
    * Generating anything, or knowing how. The dialog does not import the
      renderer; it reports a request, and
      :mod:`~omr_scanner.gui.devtools.generate_worker` carries it out.

Why the template is a file rather than "the loaded one":
    A dataset is generated *for* a template and must be benchmarked against the
    same one. Naming the file, and recording it in the manifest, is what makes
    a dataset traceable three months later; borrowing whatever happened to be
    loaded would produce datasets nobody can attribute.

Why the reference scan is not remembered between runs:
    It is an input to *this* job, like the template and the output folder, and
    the dialog treats it as one. Persisting it would mean introducing a
    preference store this application does not have, to remember a path whose
    usefulness expires with the run. The Browse button opens on the current
    project's folder instead, which is where a scanned blank form is kept.

Why the dialog cannot tell you the scan is unusable before you press Generate:
    Checking would mean registering it, which is an imaging operation, and this
    layer must not import ``cv2``, ``numpy`` or ``omr_scanner.imaging`` (see
    ``docs/ARCHITECTURE.md`` and ``tests/unit/test_architecture.py``). So the
    dialog checks that a file was chosen and exists, and the worker reports the
    registration failure - which it does before writing a single sheet, so the
    answer still arrives immediately rather than after a long run.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.evaluation.attendance_dataset import (
    ConflictProfile,
    ConflictRates,
    Population,
    plan_population,
)
from omr_scanner.evaluation.fold_plans import NO_FOLDS
from omr_scanner.evaluation.performance import (
    DEFAULT_PERFORMANCE,
    PerformanceDistribution,
    PerformancePolicy,
)
from omr_scanner.evaluation.synthetic_dataset import (
    DEFAULT_DPI,
    DEFAULT_JPEG_QUALITY,
    CaseFamily,
    ColorMode,
    DatasetProfile,
    FoldCorner,
    FoldPolicy,
    FoldSeverity,
    ImageFormat,
    RenderMode,
)
from omr_scanner.gui.theme import Spacing
from omr_scanner.gui.widgets.collapsible import CollapsibleSection

DEFAULT_COUNT = 50
MAX_COUNT = 100_000
"""Upper bound on one generated dataset.

Not a technical limit - sheets are written one at a time and nothing is held in
memory - but a hundred thousand A4 pages is roughly 60 GB, and a spin box that
allows it invites somebody to fill a disk by holding an arrow key."""

MAX_SEED = 2_147_483_647

DEFAULT_SET_CODES: tuple[str, ...] = ("10", "11", "12")

PREFERRED_WIDTH = 760
PREFERRED_HEIGHT = 720
"""What the dialog opens at when the screen has room for it.

Wide enough that a template path is readable without scrolling it sideways,
and tall enough to show the first two groups whole."""

MAX_WIDTH_FRACTION = 0.9
MAX_HEIGHT_FRACTION = 0.85
"""Share of the *available* screen the dialog may occupy when it cannot have
its preferred size. Under 1 on purpose: a configuration dialog that fills the
display looks like it has gone wrong, and leaves nowhere to see the window it
belongs to."""

MINIMUM_WIDTH = 520
MINIMUM_HEIGHT = 360
"""The floor, expressed as the smallest *viewport* that is still workable -
not the height of the form. See ``_apply_initial_size``."""

FAMILY_COLUMNS = 2
"""Columns in the case-family grid.

Two rather than three: the longest family name is around twenty characters,
and three columns of that need more width than the dialog's preferred size
gives, which would push the form into horizontal scrolling on exactly the
narrow displays this layout exists to serve."""

RENDER_MODE_LABELS: dict[RenderMode, str] = {
    RenderMode.TEMPLATE: "Template-rendered synthetic",
    RenderMode.REFERENCE_SCAN: "Real scanned sheet + synthetic markings",
}
"""How each rendering mode is named to an operator.

Spelled out rather than derived from the enum value: "reference_scan" does not
tell somebody choosing between them what the difference is."""

RENDER_MODE_DESCRIPTIONS: dict[RenderMode, str] = {
    RenderMode.TEMPLATE: (
        "The whole page is drawn from the template: markers, bubbles and marks. "
        "Clean geometry and even paper - good for regressions, weak evidence "
        "about real scans."
    ),
    RenderMode.REFERENCE_SCAN: (
        "Only the marks are drawn, onto a scan of a real blank form. Real paper, "
        "print, lighting and scanner behaviour; the resolution comes from the "
        "scan, so Resolution (dpi) is not used. Marker-damage cases are skipped "
        "- the printed markers belong to the scan."
    ),
}
"""What each mode is *for*, and what it costs. Both halves matter: an operator
choosing the reference mode needs to know the DPI setting stops applying and
that the marker cases will not be generated."""

COLOR_MODE_LABELS: dict[ColorMode, str] = {
    ColorMode.GRAYSCALE: "Grayscale",
    ColorMode.COLOR: "Colour (RGB)",
    ColorMode.BLACK_AND_WHITE: "Black and white (1-bit)",
}

CORNER_LABELS: dict[FoldCorner, str] = {
    FoldCorner.TOP_LEFT: "Top-left",
    FoldCorner.TOP_RIGHT: "Top-right",
    FoldCorner.BOTTOM_LEFT: "Bottom-left",
    FoldCorner.BOTTOM_RIGHT: "Bottom-right",
}

SEVERITY_LABELS: dict[FoldSeverity, str] = {
    FoldSeverity.MICRO: "Micro (a few mm)",
    FoldSeverity.SMALL: "Small (a dog-ear)",
    FoldSeverity.MODERATE: "Moderate",
    FoldSeverity.SEVERE: "Severe (reaches a marker)",
}

RANDOM_SEVERITY = "random"
"""What the severity chooser stores for "a mixture".

A sentinel rather than a fifth :class:`FoldSeverity` member, because a fold
that has been drawn is always one of the four; "random" is a request, not a
kind of fold, and putting it in the enum would let it reach a manifest."""

DEFAULT_FOLD_PERCENT = 10
DEFAULT_MAX_FOLDS_PER_SHEET = 1
"""Defaults for the fold controls. One corner per affected sheet because more
than one is genuinely uncommon on real paper."""

DISTRIBUTION_LABELS: dict[PerformanceDistribution, str] = {
    PerformanceDistribution.NORMAL: "Normal (Gaussian)",
    PerformanceDistribution.RANDOM: "Fully random (legacy)",
}
"""How each candidate-performance model is named to an operator."""

PROFILE_DESCRIPTIONS: dict[DatasetProfile, str] = {
    DatasetProfile.BASELINE: "Clean, valid sheets only. Anything failing here is a defect.",
    DatasetProfile.RECOGNITION: (
        "Blanks, multiple marks, faint and erased marks, mark styles, intensity sweep."
    ),
    DatasetProfile.DEGRADATION: (
        "Rotation, scale, perspective, cropping, marker damage, blur, noise, exposure."
    ),
    DatasetProfile.BATCH: "Duplicate identifiers and mixed valid/unreadable sheets.",
    DatasetProfile.STRESS: "Several defects at once, in combinations that really co-occur.",
    DatasetProfile.MIXED: "A bit of everything - the closest to a real batch.",
    DatasetProfile.CUSTOM: "Exactly the families ticked below.",
}
"""What each profile is *for*. Shown under the chooser, because "degradation"
does not tell a new developer whether it contains blank answers."""


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """One complete answer to "what should be generated".

    Attributes:
        template_path: The ``.omrt`` the sheets are rendered from.
        output_dir: Folder the dataset is written into.
        count: How many sheets.
        seed: Master seed; the same request twice produces the same dataset.
        profile: Which families of test case to draw on.
        families: Families for a custom profile.
        image_format: PNG or JPEG.
        jpeg_quality: Quality for JPEG output.
        dpi: Rendering resolution. Not applied when :attr:`render_mode` is
            :attr:`~omr_scanner.evaluation.synthetic_dataset.RenderMode.REFERENCE_SCAN`,
            whose pixels come from a real scan at its own resolution.
        render_mode: Whether each page is drawn from the template or is a real
            scanned blank form with marks laid on it.
        reference_scan: The blank scan to lay marks on. Required by
            ``RenderMode.REFERENCE_SCAN`` and ignored otherwise.
        color_mode: Channel layout and tonal range of the written images.
        fold_policy: Physical corner folds. Disabled by default, and a disabled
            policy leaves every sheet exactly as it would otherwise have been -
            which is what lets this field exist without changing what any
            existing caller's request means.
        name: Dataset name, recorded in the manifest.
        write_metadata: Also write the CSV manifest and the dataset summary.
        run_benchmark: Open the benchmark straight after generating.
        with_attendance: Also generate the set-specific attendance workbooks
            and the reconciliation ground truth. When on, :attr:`count` is the
            size of the *candidate roster* rather than the number of images -
            absentees and missing scans mean fewer sheets than candidates,
            which is the point of generating the two together.
        set_codes: The question-paper sets the roster is divided between.
        conflict_profile: How much the paperwork disagrees with reality.
        conflict_rates: The individual rates, used when
            :attr:`conflict_profile` is ``CUSTOM``.
        include_reconciliation_edge_cases: Guarantee one of every conflict,
            whatever the rates work out to at this roster size.
        generate_solutions: Also write ``solution/`` - one clean solution
            sheet and answer-key text file per set. On here and in the dialog
            alike, because it only adds files and changes no candidate's sheet.
        performance: How many questions each candidate answers correctly
            against their set's key. The same default as the generator's own.
    """

    template_path: Path
    output_dir: Path
    count: int = DEFAULT_COUNT
    seed: int = 20260918
    profile: DatasetProfile = DatasetProfile.MIXED
    families: tuple[CaseFamily, ...] = ()
    image_format: ImageFormat = ImageFormat.PNG
    jpeg_quality: int = DEFAULT_JPEG_QUALITY
    dpi: int = DEFAULT_DPI
    render_mode: RenderMode = RenderMode.TEMPLATE
    reference_scan: Path | None = None
    color_mode: ColorMode = ColorMode.GRAYSCALE
    fold_policy: FoldPolicy = NO_FOLDS
    name: str = "synthetic"
    write_metadata: bool = True
    run_benchmark: bool = False

    with_attendance: bool = False
    """Off by default *here*, on by default in the dialog.

    The distinction is deliberate. This dataclass is the contract every
    programmatic caller and test already builds against, and turning attendance
    on by default would silently change what ``GenerationRequest(count=5)``
    means - five candidates, and therefore fewer than five images, where it
    used to mean five sheets. The dialog sets it explicitly, so an operator
    still gets the paired dataset without existing callers changing behaviour
    underneath them.
    """

    set_codes: tuple[str, ...] = DEFAULT_SET_CODES
    conflict_profile: ConflictProfile = ConflictProfile.NORMAL
    conflict_rates: ConflictRates | None = None
    include_reconciliation_edge_cases: bool = True
    generate_solutions: bool = True
    performance: PerformancePolicy = DEFAULT_PERFORMANCE

    def population(self) -> Population | None:
        """The candidate roster this request implies, or ``None``.

        Built here rather than in the worker so a test - and the dialog's own
        pre-generation summary - can see exactly what a request would produce
        without generating anything.
        """
        if not self.with_attendance:
            return None
        rates = (
            self.conflict_rates
            if self.conflict_profile is ConflictProfile.CUSTOM
            and self.conflict_rates is not None
            else self.conflict_profile.rates()
        )
        return plan_population(
            count=self.count,
            set_codes=self.set_codes,
            seed=self.seed,
            rates=rates,
            include_edge_cases=self.include_reconciliation_edge_cases,
        )


class GenerateDatasetDialog(QDialog):
    """Ask for everything :class:`GenerationRequest` needs.

    Args:
        parent: Optional Qt parent.
        template_path: Template to start from - the one the Scan page has
            loaded, when it has one.
        output_dir: Folder to start from.
        project_dir: The open project's folder, which is where the Browse
            button for a reference scan opens. Not remembered between runs -
            see the module docstring.

    Testability:
        The widgets carry stable object names and
        :meth:`request` is pure, so a GUI test sets the fields and reads the
        request back without opening a native file dialog.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        template_path: Path | None = None,
        output_dir: Path | None = None,
        project_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("generateDatasetDialog")
        self.setWindowTitle("Generate Synthetic Test Dataset")
        self.setModal(True)
        self._project_dir = project_dir

        # The form scrolls; the footer does not. Every group below together is
        # well over a thousand logical pixels tall, which is more than the
        # usable height of a 1366x768 laptop even before Windows scaling - so
        # the dialog cannot be sized to its contents and must never try. Same
        # shape as `review/history_dialog.py`, for the same reason.
        form = QWidget()
        form.setObjectName("datasetForm")
        inner = QVBoxLayout(form)
        inner.setContentsMargins(0, 0, Spacing.SM, 0)
        inner.setSpacing(Spacing.SM)

        self.source_section = self._section(
            "Source and destination",
            self._build_source_box(template_path, output_dir),
            expanded=True,
        )
        self.content_section = self._section(
            "Contents", self._build_content_box(), expanded=True
        )
        self.attendance_section = self._section(
            "Attendance and reconciliation",
            self._build_attendance_box(),
            expanded=True,
        )
        # Folded like the two below it: the defaults are what almost every run
        # wants, and the summary beside the title says what they are.
        self.answer_key_section = self._section(
            "Answer keys and candidate performance",
            self._build_answer_key_box(),
            expanded=False,
        )
        # Folded by default, both of them: the format, quality and resolution
        # are the settings a developer changes least often, and physical
        # deformation is off unless somebody has come looking for it. Folding
        # the groups nobody usually touches is most of the difference between
        # a form that needs scrolling on a laptop and one that does not.
        self.deformation_section = self._section(
            "Physical page deformation", self._build_deformation_box(), expanded=False
        )
        self.output_section = self._section(
            "Images and output", self._build_output_box(), expanded=False
        )
        for section in self._sections():
            inner.addWidget(section)
        inner.addStretch(1)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("datasetScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        # Controls resize with the width, so a horizontal bar would only ever
        # mean something is clipped - which is a layout bug, not a thing to
        # scroll past.
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setWidget(form)

        layout = QVBoxLayout(self)
        layout.addWidget(self.scroll_area, stretch=1)

        caveat = QLabel(
            "Synthetic sheets measure regression consistency and controlled edge cases. "
            "They are not evidence of real-world recognition accuracy."
        )
        caveat.setObjectName("syntheticCaveatLabel")
        caveat.setWordWrap(True)
        layout.addWidget(caveat)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Generate")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._connect_summaries()
        self._on_profile_changed()
        self._on_attendance_toggled()
        self._on_render_mode_changed()
        self._on_folds_toggled()
        self._apply_initial_size()

        # Keyboard navigation has to scroll, and Qt will not do it here.
        # `QScrollArea` only calls `ensureWidgetVisible` from its own
        # `focusNextPrevChild`, which is never reached: Tab is handled by the
        # dialog, which walks the whole focus chain itself and moves focus
        # straight to a control the viewport has scrolled past. Watching the
        # application's focus instead catches every route into a control -
        # Tab, Shift+Tab, a mnemonic, or a click.
        # `instance()` is typed as the QCoreApplication base, which has no
        # focus of any kind; only the widget-aware subclass carries the signal.
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application.focusChanged.connect(self._on_focus_changed)

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------
    def _sections(self) -> tuple[CollapsibleSection, ...]:
        """Every collapsible group, in the order they appear."""
        return (
            self.source_section,
            self.content_section,
            self.attendance_section,
            self.answer_key_section,
            self.deformation_section,
            self.output_section,
        )

    def _section(
        self, title: str, content: QWidget, *, expanded: bool
    ) -> CollapsibleSection:
        """Wrap one group in a collapsible section, and keep its summary current.

        The group box's own title is cleared: the section header above it now
        says the same words, and printing them twice cost a text line and a
        frame label per group - four wasted rows on a form whose whole problem
        was height. The builders still name their boxes, because that is what
        makes them readable in isolation.
        """
        if isinstance(content, QGroupBox):
            content.setTitle("")
        section = CollapsibleSection(title, content, expanded=expanded)
        section.toggled.connect(lambda _checked: self._refresh_summaries())
        return section

    def _apply_initial_size(self) -> None:
        """Open at a size that fits the screen the parent window is on.

        Never ``adjustSize()``: that asks the layout how tall it would like to
        be, and the honest answer here is taller than any laptop display. The
        size is taken from the *available* geometry - which excludes the
        taskbar - of the screen showing the parent window, so the dialog also
        opens on the right monitor when the application has been moved to a
        second one.
        """
        parent = self.parentWidget()
        screen = parent.screen() if parent is not None else self.screen()
        available = screen.availableGeometry()
        width = min(PREFERRED_WIDTH, int(available.width() * MAX_WIDTH_FRACTION))
        height = min(PREFERRED_HEIGHT, int(available.height() * MAX_HEIGHT_FRACTION))
        self.resize(width, height)

        # The floor is what the *viewport* needs to stay usable, deliberately
        # not what the form needs to be fully visible. A minimum derived from
        # the content would reintroduce exactly the defect this method exists
        # to fix: a dialog that cannot be made small enough to fit.
        self.setMinimumSize(
            min(MINIMUM_WIDTH, available.width()),
            min(MINIMUM_HEIGHT, available.height()),
        )

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_attendance_box(self) -> QGroupBox:
        """The paperwork half: who was registered, and what the office recorded.

        Separated from "Contents" because it answers a different question.
        The contents box decides what the *images* look like; this decides who
        exists and how badly the attendance workbook disagrees with them - the
        input to the Attendance stage rather than to recognition.
        """
        box = QGroupBox("Attendance and reconciliation")
        form = QFormLayout(box)

        self.attendance_checkbox = QCheckBox(
            "Generate attendance workbooks and reconciliation ground truth"
        )
        self.attendance_checkbox.setObjectName("datasetAttendanceCheckBox")
        self.attendance_checkbox.setChecked(True)
        self.attendance_checkbox.setToolTip(
            "Writes one .xlsx per set, in the layout the Attendance stage "
            "imports, plus candidates.csv and reconciliation.csv. With this on, "
            "'Sheets' above is the size of the candidate roster - absentees and "
            "missing scans produce fewer images than candidates."
        )
        self.attendance_checkbox.toggled.connect(self._on_attendance_toggled)
        form.addRow("", self.attendance_checkbox)

        self.sets_edit = QLineEdit(", ".join(DEFAULT_SET_CODES))
        self.sets_edit.setObjectName("datasetSetsEdit")
        self.sets_edit.setToolTip(
            "Comma-separated question-paper sets. One workbook is written per "
            "set, and set codes are never assumed to be a single digit."
        )
        form.addRow("Sets:", self.sets_edit)

        self.conflict_combo = QComboBox()
        self.conflict_combo.setObjectName("datasetConflictProfileCombo")
        for profile in ConflictProfile:
            self.conflict_combo.addItem(profile.value.title(), profile)
        self.conflict_combo.setCurrentIndex(
            self.conflict_combo.findData(ConflictProfile.NORMAL)
        )
        self.conflict_combo.currentIndexChanged.connect(self._on_attendance_toggled)
        form.addRow("Conflict profile:", self.conflict_combo)

        self.absentee_spin = QDoubleSpinBox()
        self.absentee_spin.setObjectName("datasetAbsenteeRateSpin")
        self.absentee_spin.setRange(0.0, 50.0)
        self.absentee_spin.setDecimals(2)
        self.absentee_spin.setSuffix(" %")
        self.absentee_spin.setValue(ConflictRates().true_absentee * 100.0)
        self.absentee_spin.setToolTip(
            "Genuine non-attendance, before any clerical error. Distinct from "
            "a present candidate wrongly recorded as absent, which the conflict "
            "profile controls."
        )
        form.addRow("True absentee rate:", self.absentee_spin)

        self.edge_case_checkbox = QCheckBox(
            "Guarantee one of every reconciliation conflict"
        )
        self.edge_case_checkbox.setObjectName("datasetReconciliationEdgeCheckBox")
        self.edge_case_checkbox.setChecked(True)
        self.edge_case_checkbox.setToolTip(
            "Stages every conflict at least once even when the rates are too "
            "low to produce one at this roster size. A dataset that omits a "
            "case cannot be used to prove that case is handled."
        )
        form.addRow("", self.edge_case_checkbox)

        self.attendance_summary = QLabel("")
        self.attendance_summary.setObjectName("datasetAttendanceSummary")
        self.attendance_summary.setWordWrap(True)
        form.addRow("", self.attendance_summary)
        return box

    def _build_answer_key_box(self) -> QGroupBox:
        """The key each set is marked against, and how well candidates do on it.

        Four rows. The solution sheets are on by default; the performance model
        decides how many questions each candidate answers correctly against the
        key of the paper they sat, from a truncated normal distribution.
        """
        box = QGroupBox("Answer keys and candidate performance")
        form = QFormLayout(box)

        self.solutions_checkbox = QCheckBox("Generate solution sheets and answer keys")
        self.solutions_checkbox.setObjectName("datasetSolutionsCheckBox")
        self.solutions_checkbox.setChecked(True)
        self.solutions_checkbox.setToolTip(
            "Writes solution/ with one clean solution OMR sheet and one answer-key "
            "text file per set, both from the same key. The text file can be "
            "pasted into the Answer Key stage; the sheet can be read with Read "
            "From Solution Sheet. The keys are drawn either way."
        )
        form.addRow("", self.solutions_checkbox)

        self.distribution_combo = QComboBox()
        self.distribution_combo.setObjectName("datasetPerformanceDistributionCombo")
        for distribution in PerformanceDistribution:
            self.distribution_combo.addItem(DISTRIBUTION_LABELS[distribution], distribution)
        self.distribution_combo.setCurrentIndex(
            self.distribution_combo.findData(DEFAULT_PERFORMANCE.distribution)
        )
        self.distribution_combo.setToolTip(
            "Normal: each candidate's share of correct answers is drawn from a "
            "truncated normal distribution, and exactly that many answers match "
            "their set's key. Fully random: answers are chosen independently of "
            "any key, as before keys existed."
        )
        self.distribution_combo.currentIndexChanged.connect(self._on_distribution_changed)
        form.addRow("Distribution:", self.distribution_combo)

        self.mean_spin = _percent_spin("datasetMeanCorrectSpin", DEFAULT_PERFORMANCE.mean)
        self.mean_spin.setToolTip("Mean share of questions answered correctly.")
        self.sd_spin = _percent_spin("datasetSdCorrectSpin", DEFAULT_PERFORMANCE.stddev)
        self.sd_spin.setToolTip("Standard deviation of that share, before truncation.")
        form.addRow("Mean correct:", _paired(self.mean_spin, "SD:", self.sd_spin))

        self.min_spin = _percent_spin("datasetMinCorrectSpin", DEFAULT_PERFORMANCE.minimum)
        self.min_spin.setToolTip("No candidate scores below this.")
        self.max_spin = _percent_spin("datasetMaxCorrectSpin", DEFAULT_PERFORMANCE.maximum)
        self.max_spin.setToolTip("No candidate scores above this.")
        form.addRow("Minimum:", _paired(self.min_spin, "Maximum:", self.max_spin))
        return box

    def _build_deformation_box(self) -> QGroupBox:
        """What happened to the paper on its way to the scanner.

        Kept to five rows and folded away by default. Everything in here is
        inert until the one check box at the top is ticked, so an operator who
        never opens the section generates exactly what they always did.
        """
        box = QGroupBox("Physical page deformation")
        form = QFormLayout(box)

        self.fold_checkbox = QCheckBox("Simulate corner folds")
        self.fold_checkbox.setObjectName("datasetFoldCheckBox")
        self.fold_checkbox.setToolTip(
            "Folds a corner of the finished sheet - paper, printing and the "
            "candidate's marks together - before the scanner sees it. A fold "
            "deep enough to cover a registration marker is expected to stop "
            "the sheet registering, and the dataset records exactly which "
            "marker it covered and by how much."
        )
        self.fold_checkbox.toggled.connect(self._on_folds_toggled)
        form.addRow("", self.fold_checkbox)

        # Two columns, like the case families above and for the same reason:
        # a four-item list with its own scrollbar costs more height than the
        # grid and shows fewer of them.
        self.fold_corners_widget = QWidget()
        self.fold_corners_widget.setObjectName("datasetFoldCornersGrid")
        grid = QGridLayout(self.fold_corners_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(Spacing.LG)
        grid.setVerticalSpacing(Spacing.XXS)
        self.fold_corner_boxes: dict[FoldCorner, QCheckBox] = {}
        for index, corner in enumerate(FoldCorner):
            corner_box = QCheckBox(CORNER_LABELS[corner])
            corner_box.setObjectName(f"datasetFoldCorner_{corner.value}")
            corner_box.setAccessibleName(f"Eligible fold corner: {corner_box.text()}")
            corner_box.setChecked(True)
            corner_box.toggled.connect(self._refresh_summaries)
            self.fold_corner_boxes[corner] = corner_box
            grid.addWidget(corner_box, index // FAMILY_COLUMNS, index % FAMILY_COLUMNS)
        form.addRow("Eligible corners:", self.fold_corners_widget)

        self.fold_frequency_spin = QSpinBox()
        self.fold_frequency_spin.setObjectName("datasetFoldFrequencySpin")
        self.fold_frequency_spin.setRange(0, 100)
        self.fold_frequency_spin.setValue(DEFAULT_FOLD_PERCENT)
        self.fold_frequency_spin.setSuffix(" %")
        self.fold_frequency_spin.setToolTip(
            "Roughly what share of sheets are folded. The deliberate coverage "
            "cases are filled first, so a small dataset may exceed this to "
            "make sure every kind of fold is present at all."
        )
        self.fold_frequency_spin.valueChanged.connect(self._refresh_summaries)

        self.fold_max_spin = QSpinBox()
        self.fold_max_spin.setObjectName("datasetFoldMaxSpin")
        self.fold_max_spin.setRange(1, len(FoldCorner))
        self.fold_max_spin.setValue(DEFAULT_MAX_FOLDS_PER_SHEET)
        self.fold_max_spin.setToolTip(
            "Folded corners on one affected sheet. One by default: more than "
            "one corner folded on the same page is genuinely uncommon, and "
            "three or more are capped at moderate so they cannot between them "
            "consume the page."
        )
        form.addRow(
            "Fold frequency:",
            _paired(self.fold_frequency_spin, "Max per sheet:", self.fold_max_spin),
        )

        self.fold_severity_combo = QComboBox()
        self.fold_severity_combo.setObjectName("datasetFoldSeverityCombo")
        for severity in FoldSeverity:
            self.fold_severity_combo.addItem(SEVERITY_LABELS[severity], severity)
        self.fold_severity_combo.addItem("Random", RANDOM_SEVERITY)
        self.fold_severity_combo.setCurrentIndex(
            self.fold_severity_combo.findData(RANDOM_SEVERITY)
        )
        self.fold_severity_combo.currentIndexChanged.connect(self._refresh_summaries)
        form.addRow("Severity:", self.fold_severity_combo)

        self.fold_coverage_checkbox = QCheckBox(
            "Guarantee one of every marker interaction"
        )
        self.fold_coverage_checkbox.setObjectName("datasetFoldCoverageCheckBox")
        self.fold_coverage_checkbox.setChecked(True)
        self.fold_coverage_checkbox.setToolTip(
            "Spend part of the quota on folds chosen to reach no marker, to "
            "clip one, to cover one, and to catch two at once - rather than "
            "leaving the spread to chance. An interaction this template's "
            "geometry cannot produce is skipped, never faked."
        )
        self.fold_coverage_checkbox.toggled.connect(self._refresh_summaries)
        form.addRow("", self.fold_coverage_checkbox)
        return box

    def _build_source_box(
        self, template_path: Path | None, output_dir: Path | None
    ) -> QGroupBox:
        """What the sheets are made of, and where the dataset goes."""
        box = QGroupBox("Source and destination")
        form = QFormLayout(box)

        self.template_edit = QLineEdit(str(template_path) if template_path else "")
        self.template_edit.setObjectName("datasetTemplateEdit")
        self.template_edit.setPlaceholderText("Select the .omrt template to render from")
        browse_template = QPushButton("Browse...")
        browse_template.setObjectName("browseTemplateButton")
        browse_template.clicked.connect(self._prompt_template)
        form.addRow("Template:", _with_button(self.template_edit, browse_template))

        self.render_mode_combo = QComboBox()
        self.render_mode_combo.setObjectName("datasetRenderModeCombo")
        for mode in RenderMode:
            self.render_mode_combo.addItem(RENDER_MODE_LABELS[mode], mode)
        self.render_mode_combo.setCurrentIndex(
            self.render_mode_combo.findData(RenderMode.TEMPLATE)
        )
        self.render_mode_combo.currentIndexChanged.connect(self._on_render_mode_changed)
        form.addRow("Render mode:", self.render_mode_combo)

        self.render_mode_description = QLabel("")
        self.render_mode_description.setObjectName("datasetRenderModeDescription")
        self.render_mode_description.setWordWrap(True)
        form.addRow("", self.render_mode_description)

        self.reference_edit = QLineEdit("")
        self.reference_edit.setObjectName("datasetReferenceScanEdit")
        self.reference_edit.setPlaceholderText(
            "Scan of a blank, unmarked sheet of this form"
        )
        self.reference_edit.setToolTip(
            "A clean scan of the printed form with nothing filled in. It is "
            "registered against the template once, and every sheet's marks are "
            "laid onto it. It is used for this run only and is not remembered."
        )
        self.reference_browse = QPushButton("Browse...")
        self.reference_browse.setObjectName("browseReferenceScanButton")
        self.reference_browse.clicked.connect(self._prompt_reference_scan)
        self.reference_row = _with_button(self.reference_edit, self.reference_browse)
        form.addRow("Reference scan:", self.reference_row)

        self.output_edit = QLineEdit(str(output_dir) if output_dir else "")
        self.output_edit.setObjectName("datasetOutputEdit")
        self.output_edit.setPlaceholderText("Folder the dataset will be written into")
        browse_output = QPushButton("Browse...")
        browse_output.setObjectName("browseOutputButton")
        browse_output.clicked.connect(self._prompt_output)
        form.addRow("Output folder:", _with_button(self.output_edit, browse_output))

        self.name_edit = QLineEdit("synthetic")
        self.name_edit.setObjectName("datasetNameEdit")
        form.addRow("Dataset name:", self.name_edit)
        return box

    def _build_content_box(self) -> QGroupBox:
        """What the sheets contain."""
        box = QGroupBox("Contents")
        form = QFormLayout(box)

        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("datasetProfileCombo")
        for profile in DatasetProfile:
            self.profile_combo.addItem(profile.value.replace("_", " ").title(), profile)
        self.profile_combo.setCurrentIndex(
            self.profile_combo.findData(DatasetProfile.MIXED)
        )
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        form.addRow("Profile:", self.profile_combo)

        self.profile_description = QLabel("")
        self.profile_description.setObjectName("datasetProfileDescription")
        self.profile_description.setWordWrap(True)
        form.addRow("", self.profile_description)

        # A grid of check boxes rather than a scrolling list. The list had its
        # own vertical scrollbar inside the form, which meant two nested scroll
        # regions competing for the wheel: pointing at the families and
        # scrolling moved them instead of the dialog, and the list showed three
        # of nine families at a time in a box that could not grow. A grid has
        # no scrollbar, shows every family at once, and costs less height than
        # the list's own minimum did.
        self.families_widget = QWidget()
        self.families_widget.setObjectName("datasetFamiliesGrid")
        grid = QGridLayout(self.families_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(Spacing.LG)
        grid.setVerticalSpacing(Spacing.XXS)
        self.family_boxes: dict[CaseFamily, QCheckBox] = {}
        for index, family in enumerate(CaseFamily):
            family_box = QCheckBox(family.value.replace("_", " ").title())
            family_box.setObjectName(f"datasetFamily_{family.value}")
            family_box.setAccessibleName(f"Case family: {family_box.text()}")
            self.family_boxes[family] = family_box
            grid.addWidget(family_box, index // FAMILY_COLUMNS, index % FAMILY_COLUMNS)
        form.addRow("Case families:", self.families_widget)

        self.count_spin = QSpinBox()
        self.count_spin.setObjectName("datasetCountSpin")
        self.count_spin.setRange(1, MAX_COUNT)
        self.count_spin.setValue(DEFAULT_COUNT)
        # The mandatory edge cases come first, so a small dataset is a *prefix*
        # of the interesting ones rather than a random sample. Saying so here
        # stops somebody generating four sheets and concluding the profile is
        # broken.
        self.count_spin.setToolTip(
            "The profile's edge cases are generated first, so a small dataset is "
            "still a spread of them rather than a random sample."
        )
        # The attendance summary is a function of this and of the seed, so both
        # have to re-run it or the figures shown describe the previous answer.
        self.count_spin.valueChanged.connect(self._refresh_attendance_summary)

        self.seed_spin = QSpinBox()
        self.seed_spin.setObjectName("datasetSeedSpin")
        self.seed_spin.setRange(0, MAX_SEED)
        self.seed_spin.setValue(20260918)
        self.seed_spin.setToolTip(
            "The same seed, count, profile and template produce exactly the same "
            "dataset, which is what makes two benchmark runs comparable."
        )
        randomise = QPushButton("New seed")
        randomise.setObjectName("newSeedButton")
        self.seed_spin.valueChanged.connect(self._refresh_attendance_summary)
        randomise.clicked.connect(
            lambda: self.seed_spin.setValue(random.randint(1, MAX_SEED))
        )
        form.addRow(
            "Sheets:",
            _paired(self.count_spin, "Seed:", _with_button(self.seed_spin, randomise)),
        )
        return box

    def _build_output_box(self) -> QGroupBox:
        """How the images are written, and what happens next."""
        box = QGroupBox("Images and output")
        form = QFormLayout(box)

        self.format_combo = QComboBox()
        self.format_combo.setObjectName("datasetFormatCombo")
        self.format_combo.addItem("PNG (lossless)", ImageFormat.PNG)
        self.format_combo.addItem("JPEG", ImageFormat.JPEG)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)

        self.quality_spin = QSpinBox()
        self.quality_spin.setObjectName("datasetQualitySpin")
        self.quality_spin.setRange(10, 100)
        self.quality_spin.setValue(DEFAULT_JPEG_QUALITY)
        self.quality_spin.setEnabled(False)
        self.quality_spin.setToolTip(
            "High by default. Compression damage is its own test case, with its own "
            "tag - it should not arrive uninvited in every JPEG dataset."
        )
        form.addRow(
            "Image format:", _paired(self.format_combo, "Quality:", self.quality_spin)
        )

        self.color_combo = QComboBox()
        self.color_combo.setObjectName("datasetColorModeCombo")
        for mode in ColorMode:
            self.color_combo.addItem(COLOR_MODE_LABELS[mode], mode)
        self.color_combo.setCurrentIndex(self.color_combo.findData(ColorMode.GRAYSCALE))
        self.color_combo.setToolTip(
            "What a scanner would have been set to. Black and white throws away "
            "the grey levels a faint pencil mark lives in, before recognition "
            "ever sees the page - which is the point of offering it."
        )
        form.addRow("Colour:", self.color_combo)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setObjectName("datasetDpiSpin")
        self.dpi_spin.setRange(72, 600)
        self.dpi_spin.setValue(DEFAULT_DPI)
        self.dpi_spin.setToolTip(
            "Pages are rendered from the template's physical size, so 150 dpi on A4 "
            "is 1240 x 1754 pixels. Not used when rendering onto a real scanned "
            "sheet, whose resolution is already fixed."
        )
        form.addRow("Resolution (dpi):", self.dpi_spin)

        self.metadata_checkbox = QCheckBox("Write manifest.csv and dataset_summary.json")
        self.metadata_checkbox.setObjectName("datasetMetadataCheckBox")
        self.metadata_checkbox.setChecked(True)
        form.addRow("", self.metadata_checkbox)

        self.benchmark_checkbox = QCheckBox("Run the recognition benchmark afterwards")
        self.benchmark_checkbox.setObjectName("datasetBenchmarkCheckBox")
        form.addRow("", self.benchmark_checkbox)
        return box

    # ------------------------------------------------------------------
    # Behaviour
    # ------------------------------------------------------------------
    def selected_profile(self) -> DatasetProfile:
        """The chosen profile.

        Reconstructed from the stored value rather than read back as an object:
        Qt keeps item data as a variant, and a ``StrEnum`` put in comes back out
        as a plain string. Coercing here is what keeps ``is`` comparisons and
        the request's types honest.
        """
        return DatasetProfile(self.profile_combo.currentData())

    def selected_format(self) -> ImageFormat:
        """The chosen image format. See :meth:`selected_profile`."""
        return ImageFormat(self.format_combo.currentData())

    def selected_render_mode(self) -> RenderMode:
        """The chosen rendering mode. See :meth:`selected_profile`."""
        return RenderMode(self.render_mode_combo.currentData())

    def selected_color_mode(self) -> ColorMode:
        """The chosen colour mode. See :meth:`selected_profile`."""
        return ColorMode(self.color_combo.currentData())

    def selected_fold_corners(self) -> tuple[FoldCorner, ...]:
        """The corners ticked as eligible, in declared order."""
        return tuple(
            corner for corner, box in self.fold_corner_boxes.items() if box.isChecked()
        )

    def selected_fold_severity(self) -> FoldSeverity | None:
        """The chosen severity, or ``None`` for a mixture.

        ``None`` rather than a fifth enum member: see :data:`RANDOM_SEVERITY`.
        """
        chosen = self.fold_severity_combo.currentData()
        if chosen == RANDOM_SEVERITY:
            return None
        return FoldSeverity(chosen)

    def fold_policy(self) -> FoldPolicy:
        """The fold settings as the core generator's own type.

        Built here rather than in the worker so that a test - and
        :meth:`request` - can read exactly what will be generated without a
        dialog on screen, the same way :meth:`GenerationRequest.population`
        already works for the roster.

        Falls back to every corner when the operator has unticked all four:
        with folding on and nothing eligible there is nothing to generate, and
        :class:`FoldPolicy` refuses that outright. :meth:`_on_accept` asks them
        to fix it rather than silently choosing for them, so this fallback is
        only ever reached by a caller reading the form mid-edit.
        """
        corners = self.selected_fold_corners()
        enabled = self.fold_checkbox.isChecked()
        return FoldPolicy(
            enabled=enabled and bool(corners),
            corners=corners or tuple(FoldCorner),
            frequency=self.fold_frequency_spin.value() / 100.0,
            severity=self.selected_fold_severity(),
            max_per_sheet=self.fold_max_spin.value(),
            ensure_coverage=self.fold_coverage_checkbox.isChecked(),
        )

    def _on_folds_toggled(self) -> None:
        """Enable the fold controls only while folding is on."""
        enabled = self.fold_checkbox.isChecked()
        for widget in (
            self.fold_corners_widget,
            self.fold_frequency_spin,
            self.fold_max_spin,
            self.fold_severity_combo,
            self.fold_coverage_checkbox,
        ):
            widget.setEnabled(enabled)
        self._refresh_summaries()

    def performance_policy(self) -> PerformancePolicy:
        """The candidate-performance settings, as fractions. Not validated here."""
        return PerformancePolicy(
            distribution=PerformanceDistribution(self.distribution_combo.currentData()),
            mean=self.mean_spin.value() / 100.0,
            stddev=self.sd_spin.value() / 100.0,
            minimum=self.min_spin.value() / 100.0,
            maximum=self.max_spin.value() / 100.0,
        )

    def _on_distribution_changed(self) -> None:
        """The four figures mean nothing to the legacy model; disable them to say so."""
        active = self.performance_policy().active
        for spin in (self.mean_spin, self.sd_spin, self.min_spin, self.max_spin):
            spin.setEnabled(active)
        self._refresh_summaries()

    def _on_render_mode_changed(self) -> None:
        """Offer the reference scan only when it is used, and say what changes.

        The resolution spin box is disabled rather than hidden in the reference
        mode: it still has a value, and hiding it would leave an operator
        wondering where it went. Disabled says "this no longer applies", which
        is exactly the truth - the pixels come from the scan.
        """
        reference = self.selected_render_mode() is RenderMode.REFERENCE_SCAN
        self.render_mode_description.setText(
            RENDER_MODE_DESCRIPTIONS.get(self.selected_render_mode(), "")
        )
        for widget in (self.reference_edit, self.reference_browse):
            widget.setEnabled(reference)
        self.dpi_spin.setEnabled(not reference)
        self._refresh_summaries()

    def _on_profile_changed(self) -> None:
        """Describe the chosen profile, and offer the families only for Custom."""
        profile = self.selected_profile()
        self.profile_description.setText(PROFILE_DESCRIPTIONS.get(profile, ""))
        self.families_widget.setEnabled(profile is DatasetProfile.CUSTOM)
        self._refresh_summaries()

    def _on_format_changed(self) -> None:
        """Quality is a JPEG question; PNG has none."""
        self.quality_spin.setEnabled(self.selected_format() is ImageFormat.JPEG)

    def selected_conflict_profile(self) -> ConflictProfile:
        """The chosen conflict profile. See :meth:`selected_profile`."""
        return ConflictProfile(self.conflict_combo.currentData())

    def selected_set_codes(self) -> tuple[str, ...]:
        """The sets, parsed from the comma-separated field.

        Order is preserved and duplicates dropped, because the roster is dealt
        round-robin across this sequence and a set listed twice would quietly
        get double the candidates.
        """
        seen: list[str] = []
        for chunk in self.sets_edit.text().split(","):
            code = chunk.strip()
            if code and code not in seen:
                seen.append(code)
        return tuple(seen)

    def _on_attendance_toggled(self) -> None:
        """Enable the attendance controls, and re-describe what they will make."""
        enabled = self.attendance_checkbox.isChecked()
        for widget in (
            self.sets_edit,
            self.conflict_combo,
            self.absentee_spin,
            self.edge_case_checkbox,
        ):
            widget.setEnabled(enabled)
        self._refresh_attendance_summary()

    def _refresh_summaries(self) -> None:
        """Describe each section's current settings for when it is folded.

        So that "is anything unusual set in there?" is answerable without
        unfolding every group - which is the failure mode that makes
        collapsible forms annoying rather than helpful.
        """
        if not hasattr(self, "output_section"):
            return
        template_name = (
            Path(self.template_edit.text().strip()).name or "no template chosen"
        )
        if self.selected_render_mode() is RenderMode.REFERENCE_SCAN:
            reference = self.reference_edit.text().strip()
            template_name += (
                f" on {Path(reference).name}" if reference else " - no reference scan"
            )
        self.source_section.set_summary(template_name)
        self.content_section.set_summary(
            f"{self.count_spin.value()} sheet(s) - "
            f"{self.selected_profile().value} - seed {self.seed_spin.value()}"
        )
        self.attendance_section.set_summary(
            "no attendance workbooks"
            if not self.attendance_checkbox.isChecked()
            else (
                f"sets {', '.join(self.selected_set_codes()) or '-'} - "
                f"{self.absentee_spin.value():.1f}% absentee - "
                f"{self.selected_conflict_profile().value} conflicts"
            )
        )
        solutions = (
            "solution sheets" if self.solutions_checkbox.isChecked() else "no solution sheets"
        )
        performance = (
            f"mean {self.mean_spin.value():.0f}% +/- {self.sd_spin.value():.0f}% "
            f"({self.min_spin.value():.0f}-{self.max_spin.value():.0f}%)"
            if self.performance_policy().active
            else "fully random answers"
        )
        self.answer_key_section.set_summary(f"{solutions} - {performance}")
        quality = (
            f" {self.quality_spin.value()}%"
            if self.selected_format() is ImageFormat.JPEG
            else ""
        )
        if not self.fold_checkbox.isChecked():
            self.deformation_section.set_summary("no corner folds")
        else:
            severity = self.selected_fold_severity()
            self.deformation_section.set_summary(
                f"{self.fold_frequency_spin.value()}% folded - "
                f"{len(self.selected_fold_corners())} corner(s) - "
                f"{severity.value if severity is not None else 'random'} - "
                f"up to {self.fold_max_spin.value()} per sheet"
            )
        resolution = (
            "native resolution"
            if self.selected_render_mode() is RenderMode.REFERENCE_SCAN
            else f"{self.dpi_spin.value()} dpi"
        )
        self.output_section.set_summary(
            f"{resolution} - {COLOR_MODE_LABELS[self.selected_color_mode()]} - "
            f"{self.selected_format().value.upper()}{quality}"
        )

    def _connect_summaries(self) -> None:
        """Keep every folded section's summary describing the current values.

        Wired in one place, after the whole form exists, rather than beside
        each widget: a summary that silently goes stale is worse than no
        summary, and scattering these connections is how one gets missed.
        """
        self.template_edit.textChanged.connect(self._refresh_summaries)
        self.reference_edit.textChanged.connect(self._refresh_summaries)
        self.name_edit.textChanged.connect(self._refresh_summaries)
        self.sets_edit.textChanged.connect(self._refresh_summaries)
        self.dpi_spin.valueChanged.connect(self._refresh_summaries)
        self.quality_spin.valueChanged.connect(self._refresh_summaries)
        self.format_combo.currentIndexChanged.connect(self._refresh_summaries)
        self.color_combo.currentIndexChanged.connect(self._refresh_summaries)
        self.absentee_spin.valueChanged.connect(self._refresh_summaries)
        self.attendance_checkbox.toggled.connect(self._refresh_summaries)
        self.solutions_checkbox.toggled.connect(self._refresh_summaries)
        for spin in (self.mean_spin, self.sd_spin, self.min_spin, self.max_spin):
            spin.valueChanged.connect(self._refresh_summaries)

    def _on_focus_changed(self, _old: QWidget | None, new: QWidget | None) -> None:
        """Scroll a newly focused control into view.

        Guarded on ancestry: the application-wide signal also fires for the
        footer buttons and for every other window, and asking the scroll area
        to reveal a widget it does not contain moves the viewport to an
        arbitrary place.
        """
        if new is not None and self.scroll_area.isAncestorOf(new):
            self.scroll_area.ensureWidgetVisible(new)

    def _reveal(self, widget: QWidget) -> None:
        """Make ``widget`` visible and focused, unfolding whatever hides it.

        Validation is allowed to fail on a control the operator cannot see -
        a folded section, or one scrolled past the bottom of the viewport.
        Complaining about a field while leaving it hidden is the specific
        unhelpfulness this exists to prevent, so every ``_complain`` about a
        particular control routes through here first.

        Two of the things validation complains about are containers rather
        than controls - the case-family grid is a plain ``QWidget`` holding
        nine check boxes, and a plain widget cannot take focus. Focusing its
        first focusable child instead puts the caret on something the operator
        can actually act on, rather than nowhere.
        """
        for section in self._sections():
            if section.isAncestorOf(widget):
                section.set_expanded(True)
        self.scroll_area.ensureWidgetVisible(widget)
        target = widget
        if target.focusPolicy() is Qt.FocusPolicy.NoFocus:
            child = target.nextInFocusChain()
            while child is not None and target.isAncestorOf(child):
                if child.focusPolicy() is not Qt.FocusPolicy.NoFocus:
                    target = child
                    break
                child = child.nextInFocusChain()
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def _refresh_attendance_summary(self) -> None:
        """Recompute the summary, tolerating a form that is not yet built.

        The count and seed spin boxes are created before the attendance box is,
        and connecting them means they can fire during construction.
        """
        if not hasattr(self, "attendance_summary"):
            return
        self.attendance_summary.setText(self._summarise_attendance())
        self._refresh_summaries()

    def _summarise_attendance(self) -> str:
        """The pre-generation summary: what this roster will actually contain.

        Computed from the real plan rather than from the rates, so the numbers
        shown are the numbers that will be generated - a 0.25 per cent rate
        over 100 candidates is not a quarter of a sheet, and quoting the
        request instead of the outcome would describe a dataset nobody is
        about to produce.
        """
        if not self.attendance_checkbox.isChecked():
            return "No attendance workbooks; images and their ground truth only."
        request = self.request()
        if request is None:
            population = None
        else:
            try:
                population = request.population()
            except ValueError as exc:
                return f"Cannot plan this roster: {exc}"
        if population is None:
            return ""
        # Read off the typed plan rather than the summary mapping: the mapping
        # exists for the manifest, where values are heterogeneous JSON.
        counts = population.counts()
        conflicts = sum(
            count
            for kind, count in counts.items()
            if kind not in {"none", "true_absentee"}
        )
        absent = counts["true_absentee"] + counts["marked_present_but_absent"]
        sets = len(population.by_set())
        return (
            f"{len(population.candidates)} candidates across {sets} set(s) - "
            f"{len(population.candidates) - absent} present, {absent} absent. "
            f"{len(population.sheets_to_render())} image(s) and "
            f"{sets} workbook(s). "
            f"{conflicts} deliberate reconciliation conflict(s)."
        )

    def _prompt_template(self) -> None:
        """Ask for the template file."""
        start = self.template_edit.text() or str(Path.home())
        path, _filter = QFileDialog.getOpenFileName(
            self, "Select a template", start, "OMRFlow templates (*.omrt)"
        )
        if path:
            self.template_edit.setText(path)

    def _prompt_reference_scan(self) -> None:
        """Ask for the blank scan to lay marks on.

        Opens on the current project's folder when there is one, because that
        is where a scanned blank form lives; on whatever was already typed if
        the operator has been here before in this session; and on the home
        directory only as a last resort.
        """
        typed = self.reference_edit.text().strip()
        if typed:
            start = typed
        elif self._project_dir is not None:
            start = str(self._project_dir)
        else:
            start = str(Path.home())
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Select a scan of a blank sheet",
            start,
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp);;All files (*)",
        )
        if path:
            self.reference_edit.setText(path)

    def _prompt_output(self) -> None:
        """Ask for the destination folder."""
        start = self.output_edit.text() or str(Path.home())
        directory = QFileDialog.getExistingDirectory(
            self, "Select the dataset folder", start
        )
        if directory:
            self.output_edit.setText(directory)

    def selected_families(self) -> tuple[CaseFamily, ...]:
        """The families ticked in the grid, in declared order."""
        return tuple(
            family for family, box in self.family_boxes.items() if box.isChecked()
        )

    def request(self) -> GenerationRequest | None:
        """Return what the user asked for, or ``None`` when the form is incomplete.

        Validation lives here rather than in the accept handler so that a test
        can check the rules without a dialog on screen.
        """
        template = self.template_edit.text().strip()
        output = self.output_edit.text().strip()
        if not template or not output:
            return None

        profile = self.selected_profile()
        families = self.selected_families()
        if profile is DatasetProfile.CUSTOM and not families:
            return None

        render_mode = self.selected_render_mode()
        reference_text = self.reference_edit.text().strip()
        if render_mode is RenderMode.REFERENCE_SCAN and not reference_text:
            return None
        reference_scan = (
            Path(reference_text)
            if render_mode is RenderMode.REFERENCE_SCAN and reference_text
            else None
        )

        with_attendance = self.attendance_checkbox.isChecked()
        set_codes = self.selected_set_codes()
        if with_attendance and not set_codes:
            return None

        if self.fold_checkbox.isChecked() and not self.selected_fold_corners():
            return None

        conflict_profile = self.selected_conflict_profile()
        # The absentee rate is always the operator's; the rest come from the
        # profile. Exposing one rate directly is what the brief asks for, and
        # it is the one an examination office actually knows.
        rates = replace(
            conflict_profile.rates(), true_absentee=self.absentee_spin.value() / 100.0
        )

        return GenerationRequest(
            template_path=Path(template),
            output_dir=Path(output),
            count=self.count_spin.value(),
            seed=self.seed_spin.value(),
            profile=profile,
            families=families,
            image_format=self.selected_format(),
            jpeg_quality=self.quality_spin.value(),
            dpi=self.dpi_spin.value(),
            render_mode=render_mode,
            reference_scan=reference_scan,
            color_mode=self.selected_color_mode(),
            fold_policy=self.fold_policy(),
            name=self.name_edit.text().strip() or "synthetic",
            write_metadata=self.metadata_checkbox.isChecked(),
            run_benchmark=self.benchmark_checkbox.isChecked(),
            with_attendance=with_attendance,
            set_codes=set_codes,
            conflict_profile=ConflictProfile.CUSTOM,
            conflict_rates=rates,
            include_reconciliation_edge_cases=self.edge_case_checkbox.isChecked(),
            generate_solutions=self.solutions_checkbox.isChecked(),
            performance=self.performance_policy(),
        )

    def _on_accept(self) -> None:
        """Validate, then close - explaining exactly what is missing.

        Every complaint names the control it is about and hands it to
        :meth:`_reveal` first, so the field is unfolded, scrolled to and
        focused before the message appears. A dialog that reports a problem
        with something the operator cannot see is worse than one that says
        nothing.
        """
        if not self.template_edit.text().strip():
            self._complain(
                "Select the .omrt template the sheets should be rendered from.",
                self.template_edit,
            )
            return
        if not Path(self.template_edit.text().strip()).is_file():
            self._complain("That template file does not exist.", self.template_edit)
            return
        if self.selected_render_mode() is RenderMode.REFERENCE_SCAN:
            reference = self.reference_edit.text().strip()
            if not reference:
                self._complain(
                    "Rendering onto a real scanned sheet needs a scan of a blank, "
                    "unmarked form. Select one, or switch the render mode back to "
                    "template-rendered.",
                    self.reference_edit,
                )
                return
            if not Path(reference).is_file():
                self._complain(
                    "That reference scan does not exist.", self.reference_edit
                )
                return
        if self.fold_checkbox.isChecked() and not self.selected_fold_corners():
            self._complain(
                "Corner folds are switched on but no corner is eligible. Tick "
                "at least one corner, or switch folding off.",
                self.fold_corners_widget,
            )
            return
        if not self.output_edit.text().strip():
            self._complain("Choose a folder for the dataset.", self.output_edit)
            return
        try:
            self.performance_policy().validate()
        except ValueError as exc:
            self._complain(
                f"{exc}. Adjust the candidate performance settings.", self.mean_spin
            )
            return
        if self.selected_profile() is DatasetProfile.CUSTOM and not self.selected_families():
            self._complain(
                "A custom profile needs at least one case family ticked.",
                self.families_widget,
            )
            return
        if self.attendance_checkbox.isChecked():
            if not self.selected_set_codes():
                self._complain(
                    "Attendance workbooks need at least one question-paper set. "
                    "Enter the set codes, separated by commas.",
                    self.sets_edit,
                )
                return
            request = self.request()
            try:
                if request is not None:
                    request.population()
            except ValueError as exc:
                # The rates cannot be satisfied at this roster size. Said here
                # rather than thrown from the worker thread ten seconds later.
                self._complain(str(exc), self.absentee_spin)
                return
        self.accept()

    def _complain(self, message: str, culprit: QWidget | None = None) -> None:
        """Say what is wrong, without losing what has already been entered.

        Args:
            message: What to tell the operator.
            culprit: The control at fault. Unfolded, scrolled to and focused
                before the message box appears, so dismissing it leaves the
                cursor in the field that needs attention.
        """
        if culprit is not None:
            self._reveal(culprit)
        QMessageBox.information(self, "Generate Synthetic Test Dataset", message)


def _with_button(widget: QWidget, button: QPushButton) -> QWidget:
    """Put a field and its button on one row."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, stretch=1)
    layout.addWidget(button)
    return container


def _percent_spin(name: str, fraction: float) -> QDoubleSpinBox:
    """A 0-100 % field for one candidate-performance figure."""
    spin = QDoubleSpinBox()
    spin.setObjectName(name)
    spin.setRange(0.0, 100.0)
    spin.setDecimals(1)
    spin.setSuffix(" %")
    spin.setValue(fraction * 100.0)
    return spin


def _paired(first: QWidget, label: str, second: QWidget) -> QWidget:
    """Put a second labelled field beside the first, on one form row.

    A spin box two digits wide does not need a row of its own, and the form
    had several. Pairing the ones that are read together - how many sheets and
    which seed, which format and at what quality - removes a row each without
    making either harder to find.

    The trailing stretch matters: without it the two fields would share the
    slack and grow to half the dialog's width apiece on a wide screen, which
    looks like a bug rather than a layout.
    """
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(Spacing.SM)
    layout.addWidget(first)
    caption = QLabel(label)
    caption.setBuddy(second)
    layout.addWidget(caption)
    layout.addWidget(second)
    layout.addStretch(1)
    return container


__all__ = [
    "COLOR_MODE_LABELS",
    "CORNER_LABELS",
    "DISTRIBUTION_LABELS",
    "PROFILE_DESCRIPTIONS",
    "RANDOM_SEVERITY",
    "RENDER_MODE_DESCRIPTIONS",
    "RENDER_MODE_LABELS",
    "SEVERITY_LABELS",
    "GenerateDatasetDialog",
    "GenerationRequest",
]



