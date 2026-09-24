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
from omr_scanner.evaluation.synthetic_dataset import (
    DEFAULT_DPI,
    DEFAULT_JPEG_QUALITY,
    CaseFamily,
    DatasetProfile,
    ImageFormat,
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
        dpi: Rendering resolution.
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
    ) -> None:
        super().__init__(parent)
        self.setObjectName("generateDatasetDialog")
        self.setWindowTitle("Generate Synthetic Test Dataset")
        self.setModal(True)

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
            "Template and destination",
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
        # Folded by default: the format, quality and resolution are the
        # settings a developer changes least often, and folding the one group
        # nobody usually touches is most of the difference between a form that
        # needs scrolling on a laptop and one that does not.
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

    def _build_source_box(
        self, template_path: Path | None, output_dir: Path | None
    ) -> QGroupBox:
        """Template in, dataset out."""
        box = QGroupBox("Template and destination")
        form = QFormLayout(box)

        self.template_edit = QLineEdit(str(template_path) if template_path else "")
        self.template_edit.setObjectName("datasetTemplateEdit")
        self.template_edit.setPlaceholderText("Select the .omrt template to render from")
        browse_template = QPushButton("Browse...")
        browse_template.setObjectName("browseTemplateButton")
        browse_template.clicked.connect(self._prompt_template)
        form.addRow("Template:", _with_button(self.template_edit, browse_template))

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

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setObjectName("datasetDpiSpin")
        self.dpi_spin.setRange(72, 600)
        self.dpi_spin.setValue(DEFAULT_DPI)
        self.dpi_spin.setToolTip(
            "Pages are rendered from the template's physical size, so 150 dpi on A4 "
            "is 1240 x 1754 pixels."
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
        self.source_section.set_summary(
            Path(self.template_edit.text().strip()).name or "no template chosen"
        )
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
        quality = (
            f" {self.quality_spin.value()}%"
            if self.selected_format() is ImageFormat.JPEG
            else ""
        )
        self.output_section.set_summary(
            f"{self.dpi_spin.value()} dpi - "
            f"{self.selected_format().value.upper()}{quality}"
        )

    def _connect_summaries(self) -> None:
        """Keep every folded section's summary describing the current values.

        Wired in one place, after the whole form exists, rather than beside
        each widget: a summary that silently goes stale is worse than no
        summary, and scattering these connections is how one gets missed.
        """
        self.template_edit.textChanged.connect(self._refresh_summaries)
        self.name_edit.textChanged.connect(self._refresh_summaries)
        self.sets_edit.textChanged.connect(self._refresh_summaries)
        self.dpi_spin.valueChanged.connect(self._refresh_summaries)
        self.quality_spin.valueChanged.connect(self._refresh_summaries)
        self.format_combo.currentIndexChanged.connect(self._refresh_summaries)
        self.absentee_spin.valueChanged.connect(self._refresh_summaries)
        self.attendance_checkbox.toggled.connect(self._refresh_summaries)

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

        with_attendance = self.attendance_checkbox.isChecked()
        set_codes = self.selected_set_codes()
        if with_attendance and not set_codes:
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
            name=self.name_edit.text().strip() or "synthetic",
            write_metadata=self.metadata_checkbox.isChecked(),
            run_benchmark=self.benchmark_checkbox.isChecked(),
            with_attendance=with_attendance,
            set_codes=set_codes,
            conflict_profile=ConflictProfile.CUSTOM,
            conflict_rates=rates,
            include_reconciliation_edge_cases=self.edge_case_checkbox.isChecked(),
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
        if not self.output_edit.text().strip():
            self._complain("Choose a folder for the dataset.", self.output_edit)
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


__all__ = ["PROFILE_DESCRIPTIONS", "GenerateDatasetDialog", "GenerationRequest"]



