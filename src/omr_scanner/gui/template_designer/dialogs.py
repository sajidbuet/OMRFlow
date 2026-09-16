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

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
from omr_scanner.domain.template import (
    BubbleGrid,
    FieldType,
    QuestionBlockFieldDefinition,
    SymbolAxis,
    Zone,
)
from omr_scanner.domain.template_authoring import (
    DEFAULT_BUBBLE_RADIUS,
    DesignerValidationReport,
    fit_grid_to_bounds,
    generate_character_grid_zone,
    generate_column_array,
    generate_ignored_zone,
    generate_question_columns,
)

DEFAULT_COLUMN_GAP = 0.01

# A dialog constructed without a radius falls back to
# `template_authoring.DEFAULT_BUBBLE_RADIUS` scaled by the image it was given -
# see `RegionDialogBase.__init__`. Expressing the fallback as a fraction rather
# than a pixel count is what makes it correct for any image size, including the
# `MIN_IMAGE_DIMENSION` placeholder.

DIGIT_SYMBOLS = tuple(str(digit) for digit in range(10))
LETTER_SYMBOLS = tuple(chr(ord("A") + i) for i in range(26))

MIN_IMAGE_DIMENSION = 1.0
"""Smallest image dimension a dialog will divide by, so a pixel<->normalised
conversion can never divide by zero."""

FALLBACK_IMAGE_WIDTH = 1240.0
FALLBACK_IMAGE_HEIGHT = 1754.0
"""Reference image size assumed when a dialog is constructed without one - A4 at
150 dpi, the same canonical page the project's own example template uses.

Every size field in every region dialog is in *reference-image pixels*, so a
dialog with no image has no scale at which "22 px" means anything. A realistic
page keeps those fields meaningful (and the bubble radius spin box's range
sensible) instead of collapsing them against a 1x1 placeholder. Every caller in
the application passes the real image size; this only affects a dialog
constructed directly, as the dialog tests do."""


def _existing_ids(existing: list[str], zone_id: str) -> str:
    """Return ``zone_id``, disambiguated with a numeric suffix if it collides."""
    if zone_id not in existing:
        return zone_id
    suffix = 2
    while f"{zone_id}_{suffix}" in existing:
        suffix += 1
    return f"{zone_id}_{suffix}"


def _parse_symbol_tokens(text: str) -> tuple[str, ...]:
    """Split a comma-separated field into symbol tokens, kept exactly as typed.

    Each token is used verbatim as a string - never coerced to a number - so
    ``"01"`` stays ``"01"`` and ``"10,11,12"`` yields the three complete
    tokens ``"10"``, ``"11"``, ``"12"``, never split into individual digits.

    Raises:
        ValueError: A token is empty (a stray comma), or a token repeats.
    """
    tokens = tuple(token.strip() for token in text.split(","))
    if any(token == "" for token in tokens):
        raise ValueError("Values must not be empty; remove any stray commas.")
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            raise ValueError(f"Duplicate value: '{token}'")
        seen.add(token)
    return tokens


class RegionDialogBase(QDialog):
    """Shared plumbing for the region-generation dialogs.

    Owns the two things every region dialog needs and none of them should invent
    for itself: the container rectangle the user drew (never modified - see
    :class:`~omr_scanner.domain.template_authoring.ColumnLayoutMode`), and the
    bubble radius, which defaults to the template's and can be overridden here per
    region. Holding the radius here rather than per dialog is what stopped three
    of the four region kinds from having no bubble sizing control at all.

    Args:
        title: Window title.
        bounds: The rectangle the user drew on the canvas (or a default),
            passed through to the generator functions.
        existing_zone_ids: Ids already used in the template, to avoid a
            collision when this dialog invents an id from a label.
        image_width: Reference image width in pixels - every size field in every
            region dialog is in reference-image pixels, so each needs this to
            convert to the normalised units the document stores.
        image_height: Reference image height in pixels.
        bubble_radius_px: Starting bubble radius, in reference-image pixels; the
            open template's default when the page opens this dialog. ``None``
            falls back to
            :data:`~omr_scanner.domain.template_authoring.DEFAULT_BUBBLE_RADIUS`
            scaled by ``image_width`` - a fraction rather than a pixel count, so
            the fallback is correct for any image size.
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        title: str,
        *,
        bounds: NormalizedRect,
        existing_zone_ids: list[str],
        image_width: float = FALLBACK_IMAGE_WIDTH,
        image_height: float = FALLBACK_IMAGE_HEIGHT,
        bubble_radius_px: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.bounds = bounds
        self.existing_zone_ids = existing_zone_ids
        self._image_width = max(image_width, MIN_IMAGE_DIMENSION)
        self._image_height = max(image_height, MIN_IMAGE_DIMENSION)
        self._zones: tuple[Zone, ...] = ()

        self.layout_ = QVBoxLayout(self)
        self.form = QFormLayout()
        self.layout_.addLayout(self.form)

        self.bubble_radius_box = QDoubleSpinBox()
        self.bubble_radius_box.setObjectName("bubble_radius")
        self.bubble_radius_box.setDecimals(1)
        self.bubble_radius_box.setRange(0.5, 500.0)
        self.bubble_radius_box.setSingleStep(1.0)
        self.bubble_radius_box.setValue(
            bubble_radius_px
            if bubble_radius_px is not None
            else DEFAULT_BUBBLE_RADIUS * self._image_width
        )
        self.bubble_radius_box.setToolTip(
            "Half the bubble's width, in reference-image pixels. Defaults to the "
            "template's bubble radius (the toolbar's Bubble radius)."
        )

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        self.layout_.addWidget(self.buttons)

    def add_bubble_radius_row(self) -> None:
        """Append the bubble radius field to this dialog's form.

        Called by the subclass at the point in its own field order where the
        radius belongs, rather than by the base constructor, so each dialog keeps
        control of its layout while sharing one widget and one conversion.
        """
        self.form.addRow("Bubble radius (px):", self.bubble_radius_box)

    def bubble_size(self) -> NormalizedSize:
        """The dialog's bubble radius, as the normalised bounding size a grid stores.

        ``2 * radius`` pixels on each axis, normalised per axis - so a bubble that
        is circular on paper stays circular, and is an ellipse in normalised
        coordinates exactly as the anisotropic normalised page requires.
        """
        radius = self.bubble_radius_box.value()
        return NormalizedSize(
            width=min(1.0, 2.0 * radius / self._image_width),
            height=min(1.0, 2.0 * radius / self._image_height),
        )

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
        self,
        *,
        bounds: NormalizedRect,
        existing_zone_ids: list[str],
        image_width: float = FALLBACK_IMAGE_WIDTH,
        image_height: float = FALLBACK_IMAGE_HEIGHT,
        bubble_radius_px: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "New Student ID region",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            image_width=image_width,
            image_height=image_height,
            bubble_radius_px=bubble_radius_px,
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

        self.add_bubble_radius_row()

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
            bubble_size=self.bubble_size(),
            symbol_axis=self.axis_box.currentData(),
        )
        return (zone,)


class QuestionSetDialog(RegionDialogBase):
    """Collects the parameters for a Question Set (set-code) region.

    Supports two ways of describing a set/booklet code, matching real answer
    sheets that use either scheme:

    * **Enumerated values** - each printed choice is one complete, arbitrary
      string token (``"A"``, ``"10"``, ``"101"``, ``"*"``, ...), one bubble per
      token. This is the only mode Phase 2 originally offered, and is exactly
      what every existing ``A,B,C,D``-style template already uses (a single
      character position holding several multi-character symbols) - reopening
      one of those templates for editing in a future phase would read back as
      this mode.
    * **Positional code** - each of several printed positions is its own
      column of bubbles (e.g. two columns of digits 0-9 encode any two-digit
      code from "00" to "99").

    Both modes produce the same underlying field type
    (:class:`~omr_scanner.domain.template.GridFieldDefinition` with
    ``type=SET_CODE``); nothing distinguishes them once saved beyond
    ``character_count`` (1 for enumerated, >1 for positional), so no schema
    change or template migration was needed to add this.
    """

    def __init__(
        self,
        *,
        bounds: NormalizedRect,
        existing_zone_ids: list[str],
        image_width: float = FALLBACK_IMAGE_WIDTH,
        image_height: float = FALLBACK_IMAGE_HEIGHT,
        bubble_radius_px: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "New Question Set region",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            image_width=image_width,
            image_height=image_height,
            bubble_radius_px=bubble_radius_px,
            parent=parent,
        )

        self.mode_box = QComboBox()
        self.mode_box.addItem("Enumerated values", "enumerated")
        self.mode_box.addItem("Positional code", "positional")
        self.form.addRow("Set code mode:", self.mode_box)

        self.symbols_edit = QLineEdit("A,B,C,D")
        self.symbols_edit.setToolTip(
            "Comma-separated set values, in printed order (e.g. A,B,C,D or "
            "10,11,12). Each value is kept exactly as typed and is never "
            "treated as a number, so '01' stays '01'."
        )
        self.form.addRow("Set values:", self.symbols_edit)

        self.code_length_box = QSpinBox()
        self.code_length_box.setRange(1, 12)
        self.code_length_box.setValue(2)
        self.form.addRow("Code length:", self.code_length_box)

        self.position_symbols_edit = QLineEdit(",".join(DIGIT_SYMBOLS))
        self.position_symbols_edit.setToolTip(
            "Comma-separated symbols printed at every code position, e.g. 0,1,2,...,9."
        )
        self.form.addRow("Symbols per position:", self.position_symbols_edit)

        self.axis_box = QComboBox()
        self.axis_box.addItem("Values run down the page (vertical)", SymbolAxis.VERTICAL)
        self.axis_box.addItem("Values run across the page (horizontal)", SymbolAxis.HORIZONTAL)
        self.form.addRow("Layout:", self.axis_box)

        self.add_bubble_radius_row()

        self.label_edit = QLineEdit("Question set")
        self.form.addRow("Region name:", self.label_edit)

        self.mode_box.currentIndexChanged.connect(self._update_mode_visibility)
        self._update_mode_visibility()

    def _update_mode_visibility(self) -> None:
        positional = self.mode_box.currentData() == "positional"
        self.form.setRowVisible(self.symbols_edit, not positional)
        self.form.setRowVisible(self.code_length_box, positional)
        self.form.setRowVisible(self.position_symbols_edit, positional)

    def _build_zones(self) -> tuple[Zone, ...]:
        if self.mode_box.currentData() == "positional":
            symbols = _parse_symbol_tokens(self.position_symbols_edit.text())
            character_count = self.code_length_box.value()
        else:
            symbols = _parse_symbol_tokens(self.symbols_edit.text())
            character_count = 1
        if len(symbols) < 2:
            raise ValueError("At least two set values are required.")
        label = self.label_edit.text().strip() or "Question set"
        zone_id = _existing_ids(self.existing_zone_ids, "set_code")
        zone = generate_character_grid_zone(
            zone_id=zone_id,
            label=label,
            field_type=FieldType.SET_CODE,
            symbols=symbols,
            character_count=character_count,
            bounds=self.bounds,
            bubble_size=self.bubble_size(),
            symbol_axis=self.axis_box.currentData(),
        )
        return (zone,)


class QuestionBlockDialog(RegionDialogBase):
    """Collects the parameters for one or more Question Answer columns.

    Every spacing field (bubble width/height, choice spacing, question row
    spacing, column gap) is shown and edited in **reference-image pixels**,
    the same convention the properties panel already uses elsewhere in the
    designer - never display/zoom pixels, so the numbers mean the same thing
    regardless of how far the canvas happens to be zoomed in. They start out
    reproducing exactly what the original auto-fit-into-the-drawn-rectangle
    behaviour would have produced (see :meth:`_seed_spacing_defaults`), so a
    user who never touches them gets pixel-identical geometry to before these
    fields existed; editing them switches
    :func:`~omr_scanner.domain.template_authoring.generate_question_columns`
    into its explicit-pitch mode.

    Emits :attr:`preview_requested` with the zones the current field values
    would produce, recomputed from the exact same code path `_build_zones`
    itself calls, every time any field changes - the page connects this to
    the canvas's dashed preview overlay so Column Gap (and every other field)
    updates the canvas immediately.
    """

    preview_requested = Signal(object)
    """Emitted with a ``tuple[Zone, ...]`` (possibly empty, while input is
    momentarily invalid) whenever a field changes."""

    def __init__(
        self,
        *,
        bounds: NormalizedRect,
        existing_zone_ids: list[str],
        image_width: float = FALLBACK_IMAGE_WIDTH,
        image_height: float = FALLBACK_IMAGE_HEIGHT,
        bubble_radius_px: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "New Question block",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            image_width=image_width,
            image_height=image_height,
            bubble_radius_px=bubble_radius_px,
            parent=parent,
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

        self.add_bubble_radius_row()

        self.auto_pitch_box = QCheckBox("Fit bubble spacing to the region")
        self.auto_pitch_box.setObjectName("auto_pitch")
        self.auto_pitch_box.setChecked(True)
        self.auto_pitch_box.setToolTip(
            "Spread the bubbles evenly across the region you drew, recomputing "
            "the spacing whenever the counts, the column gap or the bubble "
            "radius change. Clear it to type the spacing yourself; either way "
            "the region's own rectangle never moves or resizes."
        )
        self.form.addRow(self.auto_pitch_box)

        self.choice_spacing_box = self._pixel_box()
        self.choice_spacing_box.setToolTip(
            "Centre-to-centre distance between adjacent answer-choice bubbles."
        )
        self.form.addRow("Choice spacing (px):", self.choice_spacing_box)
        self.row_spacing_box = self._pixel_box()
        self.row_spacing_box.setToolTip("Centre-to-centre distance between adjacent question rows.")
        self.form.addRow("Question row spacing (px):", self.row_spacing_box)
        self.column_gap_box = self._pixel_box()
        self.column_gap_box.setToolTip(
            "Empty horizontal distance between adjacent question columns' bounding boxes."
        )
        self.form.addRow("Column gap (px):", self.column_gap_box)

        self.axis_box = QComboBox()
        self.axis_box.addItem("Choices run across (one row per question)", SymbolAxis.HORIZONTAL)
        self.axis_box.addItem("Choices run down (one column per question)", SymbolAxis.VERTICAL)
        self.form.addRow("Layout:", self.axis_box)

        self.id_prefix_edit = QLineEdit("questions")
        self.form.addRow("Region id prefix:", self.id_prefix_edit)

        self._seed_spacing_defaults()
        self._update_pitch_field_state()

        self.question_count_box.valueChanged.connect(self._suggest_per_column)
        self.columns_box.valueChanged.connect(self._suggest_per_column)
        # Anything that changes how much room the bubbles have re-derives the
        # spacing while "fit to the region" is on, so changing the column count
        # reflows the layout across the *same* rectangle rather than leaving the
        # bubbles bunched at the pitch a different column count needed.
        for box in (
            self.columns_box, self.per_column_box, self.column_gap_box,
            self.bubble_radius_box,
        ):
            box.valueChanged.connect(self._reseed_pitch_if_automatic)
        self.labels_edit.textChanged.connect(self._reseed_pitch_if_automatic)
        self.axis_box.currentIndexChanged.connect(self._reseed_pitch_if_automatic)
        self.auto_pitch_box.toggled.connect(self._on_auto_pitch_toggled)

        for box in (
            self.first_question_box, self.question_count_box, self.columns_box,
            self.per_column_box, self.bubble_radius_box,
            self.choice_spacing_box, self.row_spacing_box, self.column_gap_box,
        ):
            box.valueChanged.connect(self._emit_preview)
        self.labels_edit.textChanged.connect(self._emit_preview)
        self.axis_box.currentIndexChanged.connect(self._emit_preview)
        self._emit_preview()

    def _pixel_box(self) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setDecimals(2)
        box.setRange(0.01, 100_000.0)
        return box

    def _current_labels(self) -> tuple[str, ...]:
        labels = tuple(s.strip() for s in self.labels_edit.text().split(",") if s.strip())
        return labels if len(labels) >= 2 else ("A", "B")

    def _seed_spacing_defaults(self) -> None:
        """Pre-fill the spacing fields with the auto-fit pitch, in pixels.

        Reproduces exactly what
        :func:`~omr_scanner.domain.template_authoring.fit_grid_to_bounds` computes
        for the container's strips, so the numbers shown are always the ones that
        would be generated - the fields *report* the layout rather than
        independently defining it, which is what lets "fit to the region" simply
        keep them in step.
        """
        self.column_gap_box.blockSignals(True)
        self.column_gap_box.setValue(DEFAULT_COLUMN_GAP * self._image_width)
        self.column_gap_box.blockSignals(False)
        self._reseed_pitch()

    def _reseed_pitch(self) -> None:
        """Recompute the two pitch fields from the container and the current counts.

        Signals are blocked while the fields are written: this is the
        container -> field direction, and it must not read back as a user edit
        (which would re-enter through ``_reseed_pitch_if_automatic``).
        """
        bubble = self.bubble_size()
        strip = self._strip_bounds()
        if strip is None:
            return
        try:
            grid = fit_grid_to_bounds(
                bounds=strip,
                rows=self._grid_rows(),
                columns=self._grid_columns(),
                bubble_size=bubble,
            )
        except ValueError:
            # The bubbles do not fit the region at these counts. `_build_zones`
            # reports that properly on accept; leaving the last good pitch in the
            # fields is better than clearing them mid-typing.
            return
        for box, value in (
            (self.choice_spacing_box, self._choice_pitch_px(grid)),
            (self.row_spacing_box, self._row_pitch_px(grid)),
        ):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)

    def _strip_bounds(self) -> NormalizedRect | None:
        """One column strip of the container, or ``None`` if the gaps leave no room.

        The same arithmetic
        :func:`~omr_scanner.domain.template_authoring.generate_question_columns`
        uses under :attr:`ColumnLayoutMode.FIT_CONTAINER`, so what the fields show
        and what is generated cannot disagree.
        """
        columns_count = max(self.columns_box.value(), 1)
        gap = self.column_gap_box.value() / self._image_width
        strip_width = (self.bounds.width - gap * (columns_count - 1)) / columns_count
        if strip_width <= 0.0:
            return None
        return NormalizedRect(
            x=self.bounds.x, y=self.bounds.y, width=strip_width, height=self.bounds.height
        )

    def _grid_rows(self) -> int:
        """Bubble rows in one column strip, honouring the layout axis."""
        if self.axis_box.currentData() == SymbolAxis.HORIZONTAL:
            return max(1, self.per_column_box.value())
        return len(self._current_labels())

    def _grid_columns(self) -> int:
        """Bubble columns in one column strip, honouring the layout axis."""
        if self.axis_box.currentData() == SymbolAxis.HORIZONTAL:
            return len(self._current_labels())
        return max(1, self.per_column_box.value())

    def _choice_pitch_px(self, grid: BubbleGrid) -> float:
        """The grid pitch between adjacent answer choices, in pixels."""
        if self.axis_box.currentData() == SymbolAxis.HORIZONTAL:
            return grid.column_pitch * self._image_width
        return grid.row_pitch * self._image_height

    def _row_pitch_px(self, grid: BubbleGrid) -> float:
        """The grid pitch between adjacent question rows, in pixels."""
        if self.axis_box.currentData() == SymbolAxis.HORIZONTAL:
            return grid.row_pitch * self._image_height
        return grid.column_pitch * self._image_width

    def _reseed_pitch_if_automatic(self) -> None:
        """Keep the pitch fields in step with the container while "fit" is on."""
        if self.auto_pitch_box.isChecked():
            self._reseed_pitch()

    def _on_auto_pitch_toggled(self, checked: bool) -> None:
        self._update_pitch_field_state()
        if checked:
            self._reseed_pitch()
        self._emit_preview()

    def _update_pitch_field_state(self) -> None:
        """Grey out the pitch fields while they are derived rather than entered."""
        editable = not self.auto_pitch_box.isChecked()
        self.choice_spacing_box.setEnabled(editable)
        self.row_spacing_box.setEnabled(editable)

    def _pitch(self) -> tuple[float, float]:
        """Return ``(row_pitch, column_pitch)``, normalised, honouring the current axis.

        "Choice spacing" and "question row spacing" are axis-generic labels;
        which of :class:`~omr_scanner.domain.template.BubbleGrid`'s
        axis-specific ``row_pitch``/``column_pitch`` each one maps to flips
        with the layout, exactly as
        :attr:`~omr_scanner.domain.template.QuestionBlockFieldDefinition.rows`/``.columns``
        already do.
        """
        choice_spacing = self.choice_spacing_box.value()
        row_spacing = self.row_spacing_box.value()
        if self.axis_box.currentData() == SymbolAxis.HORIZONTAL:
            return row_spacing / self._image_height, choice_spacing / self._image_width
        return choice_spacing / self._image_height, row_spacing / self._image_width

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
        column_gap = self.column_gap_box.value() / self._image_width

        # While "fit bubble spacing to the region" is on, the pitch is *derived*
        # from the container, so it is not passed at all - the generator's own
        # auto-fit computes it, and there is no second copy of the arithmetic to
        # drift. Off, the typed pitch positions the lattice inside the container
        # without resizing it.
        if self.auto_pitch_box.isChecked():
            row_pitch: float | None = None
            column_pitch: float | None = None
        else:
            row_pitch, column_pitch = self._pitch()

        zones = generate_question_columns(
            id_prefix=base_id,
            label_prefix="Questions",
            first_question=self.first_question_box.value(),
            question_count=self.question_count_box.value(),
            answer_labels=labels,
            columns=self.columns_box.value(),
            questions_per_column=self.per_column_box.value(),
            # The rectangle the user drew, passed through untouched. Everything
            # above changes the layout *inside* it; nothing here may move or
            # resize it. See `ColumnLayoutMode.FIT_CONTAINER`, which is the
            # generator's default and is what enforces that.
            bounds=self.bounds,
            bubble_size=self.bubble_size(),
            symbol_axis=self.axis_box.currentData(),
            column_gap=column_gap,
            row_pitch=row_pitch,
            column_pitch=column_pitch,
        )
        if not zones:
            raise ValueError("This configuration produces no questions.")
        return zones

    def _emit_preview(self) -> None:
        try:
            zones = self._build_zones()
        except ValueError:
            zones = ()
        self.preview_requested.emit(zones)


class CreateColumnArrayDialog(QDialog):
    """Generate a full set of question columns from one calibrated reference zone.

    Unlike the other region dialogs, this one does not draw a new rectangle -
    it starts from an existing, already-positioned question-block zone (see
    :func:`~omr_scanner.domain.template_authoring.generate_column_array`) and
    needs the reference image width to convert its one pixel-based field
    ("Horizontal gap") to normalised units.

    Args:
        reference: The calibrated column to array from.
        existing_zone_ids: Ids already used in the template, excluding
            ``reference``'s own id (which this operation always replaces),
            so the generated siblings never collide with anything else.
        image_width: Reference image width in pixels.
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        *,
        reference: Zone,
        existing_zone_ids: list[str],
        image_width: float = FALLBACK_IMAGE_WIDTH,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Question Column Array")
        self._reference = reference
        self._existing_zone_ids = existing_zone_ids
        self._image_width = max(image_width, MIN_IMAGE_DIMENSION)
        self._zones: tuple[Zone, ...] = ()

        self._layout = QVBoxLayout(self)
        form = QFormLayout()
        self._layout.addLayout(form)

        field = reference.field
        reference_count = (
            field.question_count if isinstance(field, QuestionBlockFieldDefinition) else 1
        )
        reference_first = (
            field.first_question if isinstance(field, QuestionBlockFieldDefinition) else 1
        )

        self.columns_box = QSpinBox()
        self.columns_box.setRange(1, 50)
        self.columns_box.setValue(5)
        form.addRow("Number of columns:", self.columns_box)

        self.per_column_box = QSpinBox()
        self.per_column_box.setRange(1, 100_000)
        self.per_column_box.setValue(reference_count)
        form.addRow("Questions per column:", self.per_column_box)

        self.gap_box = QDoubleSpinBox()
        self.gap_box.setDecimals(2)
        self.gap_box.setRange(0.0, 100_000.0)
        self.gap_box.setValue(DEFAULT_COLUMN_GAP * self._image_width)
        form.addRow("Horizontal gap (px):", self.gap_box)

        self.direction_box = QComboBox()
        self.direction_box.addItem("Left to Right", "left_to_right")
        self.direction_box.addItem("Right to Left", "right_to_left")
        form.addRow("Direction:", self.direction_box)

        self.first_question_box = QSpinBox()
        self.first_question_box.setRange(1, 100_000)
        self.first_question_box.setValue(reference_first)
        form.addRow("Starting question:", self.first_question_box)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        self._layout.addWidget(self.buttons)

    def result_zones(self) -> tuple[Zone, ...]:
        """The generated zones, valid only after `exec()` returned `Accepted`."""
        return self._zones

    def _on_accept(self) -> None:
        try:
            self._zones = generate_column_array(
                self._reference,
                columns=self.columns_box.value(),
                questions_per_column=self.per_column_box.value(),
                first_question=self.first_question_box.value(),
                gap=self.gap_box.value() / self._image_width,
                direction=self.direction_box.currentData(),
                id_prefix=_existing_ids(self._existing_zone_ids, self._reference.id),
            )
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.accept()

    def _show_error(self, message: str) -> None:
        if not hasattr(self, "_error_label"):
            self._error_label = QLabel()
            self._error_label.setObjectName("dialogError")
            self._error_label.setWordWrap(True)
            self._layout.insertWidget(self._layout.count() - 1, self._error_label)
        self._error_label.setText(message)


class CustomBubbleDialog(RegionDialogBase):
    """Collects the parameters for a custom (alphanumeric) bubble group."""

    def __init__(
        self,
        *,
        bounds: NormalizedRect,
        existing_zone_ids: list[str],
        image_width: float = FALLBACK_IMAGE_WIDTH,
        image_height: float = FALLBACK_IMAGE_HEIGHT,
        bubble_radius_px: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "New custom bubble region",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            image_width=image_width,
            image_height=image_height,
            bubble_radius_px=bubble_radius_px,
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

        self.add_bubble_radius_row()

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
            bubble_size=self.bubble_size(),
            symbol_axis=self.axis_box.currentData(),
        )
        return (zone,)


class IgnoredRegionDialog(RegionDialogBase):
    """Collects the parameters for a region deliberately excluded from recognition."""

    def __init__(
        self,
        *,
        bounds: NormalizedRect,
        existing_zone_ids: list[str],
        image_width: float = FALLBACK_IMAGE_WIDTH,
        image_height: float = FALLBACK_IMAGE_HEIGHT,
        bubble_radius_px: float | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "New reference/ignored region",
            bounds=bounds,
            existing_zone_ids=existing_zone_ids,
            image_width=image_width,
            image_height=image_height,
            bubble_radius_px=bubble_radius_px,
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
    "FALLBACK_IMAGE_HEIGHT",
    "FALLBACK_IMAGE_WIDTH",
    "MIN_IMAGE_DIMENSION",
    "CreateColumnArrayDialog",
    "CustomBubbleDialog",
    "IgnoredRegionDialog",
    "NewTemplateDialog",
    "QuestionBlockDialog",
    "QuestionSetDialog",
    "RegionDialogBase",
    "StudentIdDialog",
    "ValidationReportDialog",
]
