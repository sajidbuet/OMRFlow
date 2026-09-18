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
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
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
