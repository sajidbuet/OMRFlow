"""Numeric geometry editing for whatever is currently selected.

Purpose:
    Show and let the user type exact pixel/normalised geometry for the
    selected marker or region, per the Phase 2 brief's §16 - and keep the two
    directions (canvas drag -> panel updates; panel edit -> canvas updates)
    from ever looping.

How the loop is avoided:
    `set_geometry()` is the *only* way the panel's fields change, and it always
    blocks the spin boxes' signals first. A canvas drag calls `set_geometry()`
    (never touches the spin boxes directly); a panel edit calls
    `_emit_change()`, which the page connects to the canvas/state - never back
    into `set_geometry()` for the same edit. So a value can flow canvas -> panel
    or panel -> canvas in one user action, never both.

Responsibilities:
    * Display X/Y/Width/Height in image pixels *and* normalised fractions for
      the selection.
    * Emit a single ``geometry_edited`` signal with pixel values when the user
      finishes editing a field (on Enter or focus-out, not per keystroke).

What does NOT belong here:
    * Knowing what kind of thing is selected beyond its geometry - a Zone and
      a RegistrationMarker look identical to this panel.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

PIXEL_DECIMALS = 1
NORMALIZED_DECIMALS = 4
MAX_PIXEL_VALUE = 100_000.0

MIN_BUBBLE_RADIUS_PX = 1.0
MAX_BUBBLE_RADIUS_PX = 200.0


class PropertiesPanel(QWidget):
    """Numeric X/Y/Width/Height editor for the current selection.

    Signals:
        geometry_edited: ``(x, y, width, height)`` in image pixels, emitted
            once when the user finishes editing any field.
    """

    geometry_edited = Signal(float, float, float, float)

    geometry_preview = Signal(float, float, float, float)
    """``(x, y, width, height)`` while the user is still adjusting a field.

    The live counterpart to :attr:`geometry_edited`, and deliberately a
    separate signal rather than the same one emitted more often. A preview
    only moves what is drawn; the commit goes through the designer state and
    pushes an undo entry. Emitting one signal for both would put an undo entry
    on every keystroke and every click of a spin arrow.
    """

    bubble_radius_edited = Signal(float)
    """``radius`` in image pixels - the user gave this region its own bubble size."""

    bubble_radius_preview = Signal(float)
    """``radius`` in image pixels, while the control is still being adjusted."""

    bubble_inherit_toggled = Signal(bool)
    """``True`` when the user asked this region to go back to the template default."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("template_properties_panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.title_label = QLabel("Nothing selected")
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        layout.addWidget(self.title_label)

        group = QGroupBox("Geometry")
        form = QFormLayout(group)

        self.x_box = self._make_pixel_box("geometry_x")
        self.y_box = self._make_pixel_box("geometry_y")
        self.width_box = self._make_pixel_box("geometry_width")
        self.height_box = self._make_pixel_box("geometry_height")
        form.addRow("X (px):", self.x_box)
        form.addRow("Y (px):", self.y_box)
        form.addRow("Width (px):", self.width_box)
        form.addRow("Height (px):", self.height_box)

        self.normalized_label = QLabel("")
        self.normalized_label.setWordWrap(True)
        self.normalized_label.setObjectName("normalizedCoordinates")
        form.addRow("Normalised:", self.normalized_label)

        layout.addWidget(group)

        # -- Bubble geometry -------------------------------------------------
        # Its own group rather than another row in "Geometry", because it is a
        # different kind of number: the rows above describe the *region*, this one
        # describes what is printed inside it, and changing it deliberately leaves
        # the region's rectangle and every bubble centre exactly where they are.
        self.bubble_group = QGroupBox("Bubble geometry")
        self.bubble_group.setObjectName("bubble_geometry_group")
        bubble_form = QFormLayout(self.bubble_group)

        self.bubble_inherit_box = QCheckBox("Use template bubble size")
        self.bubble_inherit_box.setObjectName("bubble_inherit")
        self.bubble_inherit_box.setToolTip(
            "Follow the template's default bubble radius (the toolbar's Bubble "
            "radius). Clear it to give this region its own."
        )
        bubble_form.addRow(self.bubble_inherit_box)

        self.bubble_radius_box = QDoubleSpinBox()
        self.bubble_radius_box.setObjectName("bubble_radius")
        self.bubble_radius_box.setDecimals(PIXEL_DECIMALS)
        self.bubble_radius_box.setRange(MIN_BUBBLE_RADIUS_PX, MAX_BUBBLE_RADIUS_PX)
        self.bubble_radius_box.setSingleStep(1.0)
        self.bubble_radius_box.setKeyboardTracking(False)
        self.bubble_radius_box.setToolTip(
            "Half the bubble's width, in reference-image pixels. Changing it "
            "resizes the bubbles around their existing centres; neither the "
            "centres nor this region's rectangle move."
        )
        bubble_form.addRow("Bubble radius (px):", self.bubble_radius_box)
        self.bubble_group.hide()
        layout.addWidget(self.bubble_group)

        self.bubble_radius_box.editingFinished.connect(self._emit_bubble_radius)
        self.bubble_radius_box.valueChanged.connect(self.bubble_radius_preview.emit)
        self.bubble_inherit_box.toggled.connect(self._on_inherit_toggled)

        self.question_column_label = QLabel("")
        self.question_column_label.setWordWrap(True)
        self.question_column_label.setObjectName("questionColumnInfo")
        self.question_column_label.hide()
        layout.addWidget(self.question_column_label)

        layout.addStretch(1)

        self._image_size: tuple[float, float] | None = None
        self._boxes = (self.x_box, self.y_box, self.width_box, self.height_box)
        for box in self._boxes:
            box.editingFinished.connect(self._emit_change)
            box.valueChanged.connect(self._emit_preview)
        self.setEnabled(False)

    def _make_pixel_box(self, object_name: str) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setObjectName(object_name)
        box.setDecimals(PIXEL_DECIMALS)
        box.setRange(-MAX_PIXEL_VALUE, MAX_PIXEL_VALUE)
        # Keyboard tracking on, so a typed digit previews as it is typed
        # rather than only when the field loses focus. `QDoubleSpinBox` emits
        # `valueChanged` only for input it could actually interpret, and
        # clamps to the range above, so a half-typed "-" or an empty field
        # emits nothing: the preview cannot be handed a partial value.
        box.setKeyboardTracking(True)
        return box

    def set_bubble_geometry(self, radius_px: float | None, *, inherits: bool) -> None:
        """Show, or hide, the bubble radius controls for the current selection.

        Args:
            radius_px: Half the selected region's bubble width, in image pixels,
                or ``None`` for a selection with no bubbles (a marker, an ignored
                region) - which hides the whole group rather than showing a
                meaningless zero.
            inherits: Whether the region currently matches the template default.

        Signals are blocked while the fields are written, for the same reason
        :meth:`set_geometry` blocks them: this method is the model -> panel
        direction and must never look like a user edit.
        """
        if radius_px is None:
            self.bubble_group.hide()
            return
        self.bubble_group.show()
        for widget in (self.bubble_radius_box, self.bubble_inherit_box):
            widget.blockSignals(True)
        self.bubble_radius_box.setValue(radius_px)
        self.bubble_inherit_box.setChecked(inherits)
        for widget in (self.bubble_radius_box, self.bubble_inherit_box):
            widget.blockSignals(False)

    def _emit_bubble_radius(self) -> None:
        self.bubble_radius_edited.emit(self.bubble_radius_box.value())

    def _on_inherit_toggled(self, checked: bool) -> None:
        self.bubble_inherit_toggled.emit(checked)

    def set_image_size(self, width: float, height: float) -> None:
        """Record the reference image size, used to compute normalised values."""
        self._image_size = (width, height) if width > 0 and height > 0 else None

    def clear_selection(self) -> None:
        """Show the "nothing selected" state."""
        self.title_label.setText("Nothing selected")
        self.normalized_label.setText("")
        self.set_question_column_info(None)
        self.set_bubble_geometry(None, inherits=True)
        self.setEnabled(False)

    def set_question_column_info(self, text: str | None) -> None:
        """Show or hide a one-line info summary for a selected question column.

        Args:
            text: Summary such as ``"Column 2 of 5 - gap 118px (uniform)"``,
                or ``None`` to hide the line entirely (every non-question-block
                selection, and nothing selected).
        """
        self.question_column_label.setText(text or "")
        self.question_column_label.setVisible(text is not None)

    def set_geometry(
        self, title: str, x: float, y: float, width: float, height: float
    ) -> None:
        """Display geometry for a newly selected or externally-moved item.

        Args:
            title: Short description shown above the fields (a zone's label,
                or "Top-left marker").
            x: Left edge, in image pixels.
            y: Top edge, in image pixels.
            width: Width, in image pixels.
            height: Height, in image pixels.
        """
        self.title_label.setText(title)
        self.setEnabled(True)

        for box, value in zip(self._boxes, (x, y, width, height), strict=True):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)

        self._update_normalized_label(x, y, width, height)

    def _update_normalized_label(self, x: float, y: float, width: float, height: float) -> None:
        if self._image_size is None:
            self.normalized_label.setText("(no reference image)")
            return
        image_width, image_height = self._image_size
        nx, ny = x / image_width, y / image_height
        nw, nh = width / image_width, height / image_height
        self.normalized_label.setText(
            f"x={nx:.{NORMALIZED_DECIMALS}f}  y={ny:.{NORMALIZED_DECIMALS}f}  "
            f"w={nw:.{NORMALIZED_DECIMALS}f}  h={nh:.{NORMALIZED_DECIMALS}f}"
        )

    def _emit_change(self) -> None:
        x, y, width, height = (box.value() for box in self._boxes)
        self._update_normalized_label(x, y, width, height)
        self.geometry_edited.emit(x, y, width, height)

    def _emit_preview(self) -> None:
        """Report the in-progress value, and keep the normalised line with it.

        Never reached by :meth:`set_geometry`, which blocks the boxes' signals
        while it writes them - so the model -> panel direction cannot loop back
        as a panel -> model preview.
        """
        x, y, width, height = (box.value() for box in self._boxes)
        self._update_normalized_label(x, y, width, height)
        self.geometry_preview.emit(x, y, width, height)


__all__ = ["PropertiesPanel"]
