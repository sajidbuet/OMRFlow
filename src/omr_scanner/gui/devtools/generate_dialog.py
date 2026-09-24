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
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
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

DEFAULT_COUNT = 50
MAX_COUNT = 100_000
"""Upper bound on one generated dataset.

Not a technical limit - sheets are written one at a time and nothing is held in
memory - but a hundred thousand A4 pages is roughly 60 GB, and a spin box that
allows it invites somebody to fill a disk by holding an arrow key."""

MAX_SEED = 2_147_483_647

DEFAULT_SET_CODES: tuple[str, ...] = ("10", "11", "12")

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

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_source_box(template_path, output_dir))
        layout.addWidget(self._build_content_box())
        layout.addWidget(self._build_attendance_box())
        layout.addWidget(self._build_output_box())

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

        self._on_profile_changed()
        self._on_attendance_toggled()

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

        self.families_list = QListWidget()
        self.families_list.setObjectName("datasetFamiliesList")
        # A floor as well as a ceiling: without the minimum the dialog's own
        # `adjustSize` collapses the list to a row and a half, and a chooser you
        # have to scroll to see one item at a time is not a chooser.
        self.families_list.setMinimumHeight(120)
        self.families_list.setMaximumHeight(160)
        for family in CaseFamily:
            item = QListWidgetItem(family.value.replace("_", " ").title())
            item.setData(Qt.ItemDataRole.UserRole, family)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.families_list.addItem(item)
        form.addRow("Case families:", self.families_list)

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
        form.addRow("Sheets:", self.count_spin)

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
        form.addRow("Random seed:", _with_button(self.seed_spin, randomise))
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
        form.addRow("Image format:", self.format_combo)

        self.quality_spin = QSpinBox()
        self.quality_spin.setObjectName("datasetQualitySpin")
        self.quality_spin.setRange(10, 100)
        self.quality_spin.setValue(DEFAULT_JPEG_QUALITY)
        self.quality_spin.setEnabled(False)
        self.quality_spin.setToolTip(
            "High by default. Compression damage is its own test case, with its own "
            "tag - it should not arrive uninvited in every JPEG dataset."
        )
        form.addRow("JPEG quality:", self.quality_spin)

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
        self.families_list.setEnabled(profile is DatasetProfile.CUSTOM)

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

    def _refresh_attendance_summary(self) -> None:
        """Recompute the summary, tolerating a form that is not yet built.

        The count and seed spin boxes are created before the attendance box is,
        and connecting them means they can fire during construction.
        """
        if not hasattr(self, "attendance_summary"):
            return
        self.attendance_summary.setText(self._summarise_attendance())

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
        """The families ticked in the list, in declared order."""
        return tuple(
            CaseFamily(self.families_list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.families_list.count())
            if self.families_list.item(row).checkState() is Qt.CheckState.Checked
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
        """Validate, then close - explaining exactly what is missing."""
        if not self.template_edit.text().strip():
            self._complain("Select the .omrt template the sheets should be rendered from.")
            return
        if not Path(self.template_edit.text().strip()).is_file():
            self._complain("That template file does not exist.")
            return
        if not self.output_edit.text().strip():
            self._complain("Choose a folder for the dataset.")
            return
        if self.selected_profile() is DatasetProfile.CUSTOM and not self.selected_families():
            self._complain("A custom profile needs at least one case family ticked.")
            return
        if self.attendance_checkbox.isChecked():
            if not self.selected_set_codes():
                self._complain(
                    "Attendance workbooks need at least one question-paper set. "
                    "Enter the set codes, separated by commas."
                )
                return
            request = self.request()
            try:
                if request is not None:
                    request.population()
            except ValueError as exc:
                # The rates cannot be satisfied at this roster size. Said here
                # rather than thrown from the worker thread ten seconds later.
                self._complain(str(exc))
                return
        self.accept()

    def _complain(self, message: str) -> None:
        """Say what is wrong, without losing what has already been entered."""
        QMessageBox.information(self, "Generate Synthetic Test Dataset", message)


def _with_button(widget: QWidget, button: QPushButton) -> QWidget:
    """Put a field and its button on one row."""
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(widget, stretch=1)
    layout.addWidget(button)
    return container


__all__ = ["PROFILE_DESCRIPTIONS", "GenerateDatasetDialog", "GenerationRequest"]


