"""Modal dialogs for creating regions, a new template, and reviewing validation.

Purpose:
    Collect the parameters each region generator in
    :mod:`omr_scanner.domain.template_authoring` needs, and the parameters for
    starting a new template, without any of that logic living in the canvas or
    the page.

Responsibilities:
    * One dialog per region kind (Student ID, Question Set, Question Block,
      Custom bubble group), each producing the domain `Zone` object(s) on
      accept by calling straight into `template_authoring`.
    * `NewTemplateDialog` for picking a reference image and the canonical page
      size.
    * `ValidationReportDialog` for showing a
      `template_authoring.DesignerValidationReport`.

What does NOT belong here:
    * Applying the result to the document - the page reads `result_zones()` (or
      equivalent) after `exec()` returns `Accepted`, and calls
      `state.DesignerState` itself, so every mutation still goes through the
      one method that pushes undo history.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.geometry import NormalizedRect, NormalizedSize
from omr_scanner.domain.template import FieldType, SymbolAxis, Zone
from omr_scanner.domain.template_authoring import (
    DesignerValidationReport,
    generate_character_grid_zone,
    generate_ignored_zone,
    generate_question_columns,
)

DEFAULT_BUBBLE_WIDTH = 0.022
DEFAULT_BUBBLE_HEIGHT = 0.016

DIGIT_SYMBOLS = tuple(str(digit) for digit in range(10))
LETTER_SYMBOLS = tuple(chr(ord("A") + i) for i in range(26))


def _existing_ids(existing: list[str], zone_id: str) -> str:
    """Return ``zone_id``, disambiguated with a numeric suffix if it collides."""
    if zone_id not in existing:
        return zone_id
    suffix = 2
    while f"{zone_id}_{suffix}" in existing:
        suffix += 1
    return f"{zone_id}_{suffix}"


class RegionDialogBase(QDialog):
    """Shared plumbing for the region-generation dialogs.

    Args:
        title: Window title.
        bounds: The rectangle the user drew on the canvas (or a default),
            passed through to the generator functions.
        existing_zone_ids: Ids already used in the template, to avoid a
            collision when this dialog invents an id from a label.
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        title: str,
        *,
        bounds: NormalizedRect,
        existing_zone_ids: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.bounds = bounds
        self.existing_zone_ids = existing_zone_ids
        self._zones: tuple[Zone, ...] = ()

        self.layout_ = QVBoxLayout(self)
        self.form = QFormLayout()
        self.layout_.addLayout(self.form)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        self.layout_.addWidget(self.buttons)

    def result_zones(self) -> tuple[Zone, ...]:
        """The generated zone(s), valid only after `exec()` returned `Accepted`."""
        return self._zones

    def _on_accept(self) -> None:
        """Build the zone(s), showing an inline error instead of closing on failure."""
        try:
            self._zones = self._build_zones()
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.accept()

    def _build_zones(self) -> tuple[Zone, ...]:
        """Subclasses build and return their zone(s) here, raising `ValueError` on bad input."""
        raise NotImplementedError

    def _show_error(self, message: str) -> None:
        if not hasattr(self, "_error_label"):
            self._error_label = QLabel()
            self._error_label.setObjectName("dialogError")
            self._error_label.setWordWrap(True)
            self.layout_.insertWidget(self.layout_.count() - 1, self._error_label)
        self._error_label.setText(message)


class StudentIdDialog(RegionDialogBase):
    """Collects the parameters for a Student ID (numeric) region."""

    def __init__(
        self, *, bounds: NormalizedRect, existing_zone_ids: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "New Student ID region",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            parent=parent,
        )

        self.digit_count_box = QSpinBox()
        self.digit_count_box.setRange(1, 20)
        self.digit_count_box.setValue(7)
        self.form.addRow("Number of digits:", self.digit_count_box)

        self.axis_box = QComboBox()
        self.axis_box.addItem("Digits run down the page (vertical)", SymbolAxis.VERTICAL)
        self.axis_box.addItem("Digits run across the page (horizontal)", SymbolAxis.HORIZONTAL)
        self.form.addRow("Layout:", self.axis_box)

        self.label_edit = QLineEdit("Student ID")
        self.form.addRow("Region name:", self.label_edit)

    def _build_zones(self) -> tuple[Zone, ...]:
        label = self.label_edit.text().strip() or "Student ID"
        zone_id = _existing_ids(self.existing_zone_ids, "student_id")
        zone = generate_character_grid_zone(
            zone_id=zone_id,
            label=label,
            field_type=FieldType.NUMERIC,
            symbols=DIGIT_SYMBOLS,
            character_count=self.digit_count_box.value(),
            bounds=self.bounds,
            bubble_size=NormalizedSize(width=DEFAULT_BUBBLE_WIDTH, height=DEFAULT_BUBBLE_HEIGHT),
            symbol_axis=self.axis_box.currentData(),
        )
        return (zone,)


class QuestionSetDialog(RegionDialogBase):
    """Collects the parameters for a Question Set (set-code) region."""

    def __init__(
        self, *, bounds: NormalizedRect, existing_zone_ids: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "New Question Set region",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            parent=parent,
        )

        self.symbols_edit = QLineEdit("A,B,C,D")
        self.symbols_edit.setToolTip("Comma-separated set values, in printed order.")
        self.form.addRow("Set values:", self.symbols_edit)

        self.axis_box = QComboBox()
        self.axis_box.addItem("Values run down the page (vertical)", SymbolAxis.VERTICAL)
        self.axis_box.addItem("Values run across the page (horizontal)", SymbolAxis.HORIZONTAL)
        self.form.addRow("Layout:", self.axis_box)

        self.label_edit = QLineEdit("Question set")
        self.form.addRow("Region name:", self.label_edit)

    def _build_zones(self) -> tuple[Zone, ...]:
        symbols = tuple(s.strip() for s in self.symbols_edit.text().split(",") if s.strip())
        if len(symbols) < 2:
            raise ValueError("At least two set values are required.")
        label = self.label_edit.text().strip() or "Question set"
        zone_id = _existing_ids(self.existing_zone_ids, "set_code")
        zone = generate_character_grid_zone(
            zone_id=zone_id,
            label=label,
            field_type=FieldType.SET_CODE,
            symbols=symbols,
            character_count=1,
            bounds=self.bounds,
            bubble_size=NormalizedSize(width=DEFAULT_BUBBLE_WIDTH, height=DEFAULT_BUBBLE_HEIGHT),
            symbol_axis=self.axis_box.currentData(),
        )
        return (zone,)


class QuestionBlockDialog(RegionDialogBase):
    """Collects the parameters for one or more Question Answer columns."""

    def __init__(
        self, *, bounds: NormalizedRect, existing_zone_ids: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "New Question block", bounds=bounds, existing_zone_ids=existing_zone_ids, parent=parent
        )

        self.first_question_box = QSpinBox()
        self.first_question_box.setRange(1, 100_000)
        self.first_question_box.setValue(1)
        self.form.addRow("Starting question number:", self.first_question_box)

        self.question_count_box = QSpinBox()
        self.question_count_box.setRange(1, 100_000)
        self.question_count_box.setValue(100)
        self.form.addRow("Number of questions:", self.question_count_box)

        self.labels_edit = QLineEdit("A,B,C,D")
        self.labels_edit.setToolTip("Comma-separated answer choice labels, in printed order.")
        self.form.addRow("Choice labels:", self.labels_edit)

        self.columns_box = QSpinBox()
        self.columns_box.setRange(1, 20)
        self.columns_box.setValue(4)
        self.form.addRow("Number of columns:", self.columns_box)

        self.per_column_box = QSpinBox()
        self.per_column_box.setRange(1, 100_000)
        self.per_column_box.setValue(25)
        self.form.addRow("Questions per column:", self.per_column_box)

        self.axis_box = QComboBox()
        self.axis_box.addItem("Choices run across (one row per question)", SymbolAxis.HORIZONTAL)
        self.axis_box.addItem("Choices run down (one column per question)", SymbolAxis.VERTICAL)
        self.form.addRow("Layout:", self.axis_box)

        self.id_prefix_edit = QLineEdit("questions")
        self.form.addRow("Region id prefix:", self.id_prefix_edit)

        self.question_count_box.valueChanged.connect(self._suggest_per_column)
        self.columns_box.valueChanged.connect(self._suggest_per_column)

    def _suggest_per_column(self) -> None:
        columns = max(1, self.columns_box.value())
        total = self.question_count_box.value()
        suggested = -(-total // columns)  # ceiling division
        self.per_column_box.blockSignals(True)
        self.per_column_box.setValue(suggested)
        self.per_column_box.blockSignals(False)

    def _build_zones(self) -> tuple[Zone, ...]:
        labels = tuple(s.strip() for s in self.labels_edit.text().split(",") if s.strip())
        if len(labels) < 2:
            raise ValueError("At least two answer choice labels are required.")
        prefix = self.id_prefix_edit.text().strip() or "questions"
        base_id = _existing_ids(self.existing_zone_ids, prefix)

        zones = generate_question_columns(
            id_prefix=base_id,
            label_prefix="Questions",
            first_question=self.first_question_box.value(),
            question_count=self.question_count_box.value(),
            answer_labels=labels,
            columns=self.columns_box.value(),
            questions_per_column=self.per_column_box.value(),
            bounds=self.bounds,
            bubble_size=NormalizedSize(width=DEFAULT_BUBBLE_WIDTH, height=DEFAULT_BUBBLE_HEIGHT),
            symbol_axis=self.axis_box.currentData(),
            column_gap=0.01,
        )
        if not zones:
            raise ValueError("This configuration produces no questions.")
        return zones


class CustomBubbleDialog(RegionDialogBase):
    """Collects the parameters for a custom (alphanumeric) bubble group."""

    def __init__(
        self, *, bounds: NormalizedRect, existing_zone_ids: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "New custom bubble region",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            parent=parent,
        )

        self.label_edit = QLineEdit("Custom region")
        self.form.addRow("Region name:", self.label_edit)

        self.symbols_edit = QLineEdit(",".join(LETTER_SYMBOLS[:5]))
        self.symbols_edit.setToolTip("Comma-separated symbols, in printed order.")
        self.form.addRow("Symbols per position:", self.symbols_edit)

        self.character_count_box = QSpinBox()
        self.character_count_box.setRange(1, 200)
        self.character_count_box.setValue(1)
        self.form.addRow("Number of positions:", self.character_count_box)

        self.axis_box = QComboBox()
        self.axis_box.addItem("Symbols run down the page (vertical)", SymbolAxis.VERTICAL)
        self.axis_box.addItem("Symbols run across the page (horizontal)", SymbolAxis.HORIZONTAL)
        self.form.addRow("Layout:", self.axis_box)

    def _build_zones(self) -> tuple[Zone, ...]:
        symbols = tuple(s.strip() for s in self.symbols_edit.text().split(",") if s.strip())
        if len(symbols) < 2:
            raise ValueError("At least two symbols are required.")
        label = self.label_edit.text().strip() or "Custom region"
        zone_id = _existing_ids(self.existing_zone_ids, "custom")
        zone = generate_character_grid_zone(
            zone_id=zone_id,
            label=label,
            field_type=FieldType.ALPHANUMERIC,
            symbols=symbols,
            character_count=self.character_count_box.value(),
            bounds=self.bounds,
            bubble_size=NormalizedSize(width=DEFAULT_BUBBLE_WIDTH, height=DEFAULT_BUBBLE_HEIGHT),
            symbol_axis=self.axis_box.currentData(),
        )
        return (zone,)


class IgnoredRegionDialog(RegionDialogBase):
    """Collects the parameters for a region deliberately excluded from recognition."""

    def __init__(
        self, *, bounds: NormalizedRect, existing_zone_ids: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(
            "New reference/ignored region",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            parent=parent,
        )
        self.label_edit = QLineEdit("Instructions")
        self.form.addRow("Region name:", self.label_edit)

    def _build_zones(self) -> tuple[Zone, ...]:
        label = self.label_edit.text().strip() or "Ignored region"
        zone_id = _existing_ids(self.existing_zone_ids, "ignored")
        zone = generate_ignored_zone(zone_id=zone_id, label=label, bounds=self.bounds)
        return (zone,)


class NewTemplateDialog(QDialog):
    """Collects a reference image and the canonical page size for a new template.

    Args:
        parent: Optional Qt parent.
        initial_directory: Where the file browser starts.
    """

    def __init__(
        self, parent: QWidget | None = None, *, initial_directory: Path | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New template from image")
        self._initial_directory = initial_directory or Path.home()
        self.image_path: Path | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.name_edit = QLineEdit("Untitled template")
        form.addRow("Template name:", self.name_edit)

        self.path_label = QLabel("(no image selected)")
        self.path_label.setWordWrap(True)

        picker_row = QHBoxLayout()
        picker_row.addWidget(self.path_label, stretch=1)
        pick_button = QPushButton("Browse...")
        pick_button.clicked.connect(self._pick_image)
        picker_row.addWidget(pick_button)
        form.addRow("Reference image:", picker_row)

        self.width_mm_box = QSpinBox()
        self.width_mm_box.setRange(50, 2000)
        self.width_mm_box.setValue(210)
        form.addRow("Page width (mm):", self.width_mm_box)

        self.height_mm_box = QSpinBox()
        self.height_mm_box.setRange(50, 2000)
        self.height_mm_box.setValue(297)
        form.addRow("Page height (mm):", self.height_mm_box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _pick_image(self) -> None:
        path_str, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Select a reference sheet image",
            str(self._initial_directory),
            "Images (*.png *.jpg *.jpeg *.tif *.tiff)",
        )
        if path_str:
            self.image_path = Path(path_str)
            self.path_label.setText(str(self.image_path))

    def _on_accept(self) -> None:
        if self.image_path is None:
            self.path_label.setText("(no image selected) - please choose a reference image")
            return
        self.accept()

    def template_name(self) -> str:
        """The name entered for the new template."""
        return self.name_edit.text().strip() or "Untitled template"

    def page_size_mm(self) -> tuple[float, float]:
        """The physical page size entered, in millimetres."""
        return (float(self.width_mm_box.value()), float(self.height_mm_box.value()))


class ValidationReportDialog(QDialog):
    """Shows the errors and warnings from `template_authoring.validate_template_for_designer`."""

    def __init__(
        self, report: DesignerValidationReport, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Template validation")
        layout = QVBoxLayout(self)

        if report.is_clean:
            layout.addWidget(QLabel("No problems found."))
        else:
            if report.errors:
                layout.addWidget(QLabel(f"Errors ({len(report.errors)}):"))
                error_list = QListWidget()
                error_list.addItems(report.errors)
                error_list.setObjectName("validationErrors")
                layout.addWidget(error_list)
            if report.warnings:
                layout.addWidget(QLabel(f"Warnings ({len(report.warnings)}):"))
                warning_list = QListWidget()
                warning_list.addItems(report.warnings)
                warning_list.setObjectName("validationWarnings")
                layout.addWidget(warning_list)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


__all__ = [
    "CustomBubbleDialog",
    "IgnoredRegionDialog",
    "NewTemplateDialog",
    "QuestionBlockDialog",
    "QuestionSetDialog",
    "StudentIdDialog",
    "ValidationReportDialog",
]
