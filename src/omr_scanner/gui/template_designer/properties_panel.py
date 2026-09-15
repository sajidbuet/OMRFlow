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
from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout, QGroupBox, QLabel, QVBoxLayout, QWidget

PIXEL_DECIMALS = 1
NORMALIZED_DECIMALS = 4
MAX_PIXEL_VALUE = 100_000.0


class PropertiesPanel(QWidget):
    """Numeric X/Y/Width/Height editor for the current selection.

    Signals:
        geometry_edited: ``(x, y, width, height)`` in image pixels, emitted
            once when the user finishes editing any field.
    """

    geometry_edited = Signal(float, float, float, float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.title_label = QLabel("Nothing selected")
        title_font = self.title_label.font()
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        layout.addWidget(self.title_label)

        group = QGroupBox("Geometry")
        form = QFormLayout(group)

        self.x_box = self._make_pixel_box()
        self.y_box = self._make_pixel_box()
        self.width_box = self._make_pixel_box()
        self.height_box = self._make_pixel_box()
        form.addRow("X (px):", self.x_box)
        form.addRow("Y (px):", self.y_box)
        form.addRow("Width (px):", self.width_box)
        form.addRow("Height (px):", self.height_box)

        self.normalized_label = QLabel("")
        self.normalized_label.setWordWrap(True)
        self.normalized_label.setObjectName("normalizedCoordinates")
        form.addRow("Normalised:", self.normalized_label)

        layout.addWidget(group)
        layout.addStretch(1)

        self._image_size: tuple[float, float] | None = None
        self._boxes = (self.x_box, self.y_box, self.width_box, self.height_box)
        for box in self._boxes:
            box.editingFinished.connect(self._emit_change)
        self.setEnabled(False)

    def _make_pixel_box(self) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setDecimals(PIXEL_DECIMALS)
        box.setRange(-MAX_PIXEL_VALUE, MAX_PIXEL_VALUE)
        box.setKeyboardTracking(False)
        return box

    def set_image_size(self, width: float, height: float) -> None:
        """Record the reference image size, used to compute normalised values."""
        self._image_size = (width, height) if width > 0 and height > 0 else None

    def clear_selection(self) -> None:
        """Show the "nothing selected" state."""
        self.title_label.setText("Nothing selected")
        self.normalized_label.setText("")
        self.setEnabled(False)

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


__all__ = ["PropertiesPanel"]
