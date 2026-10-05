"""Where the sheet on screen came from, in the corner beside the evidence tabs.

Purpose:
    Revised phase 8 shows a sheet's provenance - scanner source, batch, the
    file name it arrived with, arrival time - in the top-right corner of the
    Resolve evidence tabs, so the decision panel below loses no height.

    A plain label there sized itself to its whole text: a long scanner name or
    an operator's long file name clipped, and - because a tab widget sizes its
    corner from the corner's own size hint - pushed the tabs aside and the
    page wider. This label instead:

    * asks for no more than the room the tabs leave (never wider than that,
      and no minimum width at all), so it cannot widen the page or squeeze a
      tab;
    * shortens what it shows in a fixed order - the scanner name first, then
      the file name from the middle (its start and extension stay), then the
      scanner name altogether - while the batch and arrival time stay whole;
    * keeps every value complete, one per line, in its tooltip.

    The name shown is the one the file *arrived* with; the content-addressed
    copy's name appears only in the tooltip, labelled as the stored copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QTabWidget, QWidget

from omr_scanner.gui.theme import Color

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PySide6.QtGui import QResizeEvent

    from omr_scanner.services.session_sheets import SheetProvenance

SEPARATOR = " · "

TAB_GAP = 12
"""Pixels kept clear between the last tab and the corner text."""

_SOURCE_FLOOR = "Scanner…"
"""The scanner name is not shortened below this much (by width) while the
file name still has to give way."""

_NAME_FLOOR = "2026…_02.png"
"""Below this much room the file name stops being useful; the scanner goes."""


def provenance_parts(found: SheetProvenance) -> tuple[str, str, str, str]:
    """``(source, batch, file name, arrival)`` - the pieces of ``describe()``."""
    # A session batch's label already names its scanner; say it once.
    batch = found.batch_label.replace(f"{SEPARATOR}{found.source_label}", "", 1)
    arrival = (
        f"arrived {found.arrived_at.astimezone():%H:%M}" if found.arrived_at is not None else ""
    )
    return (found.source_label, batch, found.original_name or found.stored_name, arrival)


def provenance_tooltip(found: SheetProvenance) -> str:
    """Every value complete, one per line."""
    lines = []
    if found.source_label:
        lines.append(f"Source: {found.source_label}")
    lines.append(f"Batch: {found.batch_label} ({found.batch_id[:8]})")
    lines.append(f"Original file: {found.original_name or found.stored_name}")
    if found.stored_name and found.stored_name != found.original_name:
        lines.append(f"Stored as: {found.stored_name}")
    if found.arrived_at is not None:
        lines.append(f"Arrived: {found.arrived_at.astimezone():%Y-%m-%d %H:%M:%S}")
    return "\n".join(lines)


class ProvenanceLabel(QLabel):
    """One line of provenance that fits the room it is given (see the module)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Set first: styling below already delivers change events.
        self._parts: tuple[str, str, str, str] = ("", "", "", "")
        self.full_text = ""
        self.setObjectName("sheetProvenanceLabel")
        self.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
        self.setContentsMargins(0, 0, 6, 0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setTextFormat(Qt.TextFormat.PlainText)

    # --- content ---------------------------------------------------------
    def set_provenance(self, found: SheetProvenance | None) -> None:
        """Show ``found``, or hide when there is none."""
        if found is None:
            self._parts = ("", "", "", "")
            self.full_text = ""
            self.setToolTip("")
            super().setText("")
            self.setVisible(False)
            return
        self._parts = provenance_parts(found)
        self.full_text = SEPARATOR.join(part for part in self._parts if part)
        self.setToolTip(provenance_tooltip(found))
        self._refit()
        self.setVisible(True)
        self.updateGeometry()

    def fitted_text(self, width: int) -> str:
        """The line shown in ``width`` pixels (see the module for the order)."""
        metrics = self.fontMetrics()

        def advance(text: str) -> int:
            return metrics.horizontalAdvance(text)

        source, batch, name, arrival = self._parts

        def line(src: str, nm: str) -> str:
            return SEPARATOR.join(part for part in (src, batch, nm, arrival) if part)

        if advance(self.full_text) <= width:
            return self.full_text
        # 1. The scanner name gives way first, down to a recognisable stem.
        source_floor = min(advance(source), advance(_SOURCE_FLOOR))
        if source:
            spare = width - advance(line("", name)) - advance(SEPARATOR)
            if spare >= source_floor:
                return line(metrics.elidedText(source, Qt.TextElideMode.ElideRight, spare), name)
        # 2. Then the file name, from the middle: its start and extension stay.
        if name:
            stem = metrics.elidedText(source, Qt.TextElideMode.ElideRight, source_floor)
            for src in ((stem, "") if source else ("",)):
                spare = width - advance(line(src, "")) - advance(SEPARATOR)
                if spare >= advance(_NAME_FLOOR):
                    return line(src, metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, spare))
            # 3. Too narrow for both: the scanner went above; the file stays.
        # 4. Whatever fits.
        return metrics.elidedText(self.full_text, Qt.TextElideMode.ElideRight, max(0, width))

    def _refit(self) -> None:
        super().setText(self.fitted_text(self.contentsRect().width()))

    # --- size ------------------------------------------------------------
    def room(self) -> int:
        """The width the evidence tabs leave beside their last tab."""
        tabs = self.parentWidget()
        if not isinstance(tabs, QTabWidget):
            return self.width()
        return max(0, tabs.width() - tabs.tabBar().sizeHint().width() - TAB_GAP)

    def sizeHint(self) -> QSize:
        """The whole line, but never more than the room beside the tabs."""
        margins = self.contentsMargins()
        wanted = self.fontMetrics().horizontalAdvance(self.full_text) + margins.left()
        wanted += margins.right() + 2
        height = super().sizeHint().height()
        return QSize(min(wanted, self.room()), height)

    def minimumSizeHint(self) -> QSize:
        """No minimum width: the line shortens instead of widening anything."""
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-fit the line to its new width."""
        super().resizeEvent(event)
        self._refit()

    def changeEvent(self, event: QEvent) -> None:
        """An interface-zoom font change re-measures the line."""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._refit()
            self.updateGeometry()
