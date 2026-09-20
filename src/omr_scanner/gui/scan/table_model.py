"""The scan list's data, as a Qt model rather than a table full of widgets.

Purpose:
    Back the Scan page's scan list with a :class:`QAbstractTableModel` that
    reads :class:`ScanEntry` objects directly, instead of duplicating every
    cell into a ``QTableWidgetItem``. A batch of up to 100,000 sheets is
    already 100,000 :class:`ScanEntry` objects the page must hold anyway
    (Phase 10, §"lazy GUI models") - a ``QTableWidget`` held five more
    heavyweight Qt objects per row on top of that, purely to display what
    this model instead reads on demand.

Responsibilities:
    * :class:`ScanEntry` - one row's data: a source path and its outcome once
      processed. Owned here because the model is what turns it into cells;
      the page owns the *list* of them, not their shape.
    * :class:`ScanTableModel` - the read model itself, plus three narrow,
      explicit mutation hooks (`entries_reset`, `entries_appended`,
      `mark_dirty`) rather than a general-purpose mutable-sequence model,
      because the page only ever changes the list in exactly those three
      ways: replaced wholesale, grown at the end, or individual rows
      finishing in place.

What does NOT belong here:
    * Filtering. The page hides rows on the view itself
      (``QTableView.setRowHidden``), exactly as it did with ``QTableWidget``,
      so that a table row index stays the same as an index into
      ``ScanPageState.entries`` everywhere else on the page - a filter proxy
      model would break that correspondence.
    * Selection, preview or results-panel logic; this module only answers
      "what does row R, column C show".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QPersistentModelIndex, Qt
from PySide6.QtGui import QColor

from omr_scanner.services import ProcessedScan, RecognitionOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence
    from pathlib import Path

COLUMN_HEADERS = ("Original file", "Roll", "Set", "Status", "Output file")
COLUMN_COUNT = len(COLUMN_HEADERS)

STATUS_LABELS: dict[str, str] = {
    RecognitionOutcome.PENDING.value: "Pending",
    RecognitionOutcome.COMPLETE.value: "Complete",
    RecognitionOutcome.REVIEW.value: "Review",
    RecognitionOutcome.REGISTRATION_FAILED.value: "Registration failed",
    RecognitionOutcome.ERROR.value: "Error",
}
"""Plain-language names for the outcomes, shown in the scan list."""

STATUS_COLORS: dict[str, QColor] = {
    RecognitionOutcome.COMPLETE.value: QColor(226, 245, 229),
    RecognitionOutcome.REVIEW.value: QColor(255, 244, 214),
    RecognitionOutcome.REGISTRATION_FAILED.value: QColor(253, 226, 226),
    RecognitionOutcome.ERROR.value: QColor(253, 226, 226),
}
"""Row tints. Backed up by the text in the Status column, never used alone."""


@dataclass
class ScanEntry:
    """One row of the scan list.

    Attributes:
        path: The source image.
        processed: Its outcome once it has been through the pipeline.
    """

    path: Path
    processed: ProcessedScan | None = None

    @property
    def outcome(self) -> str:
        """The outcome's string value, or ``"pending"`` before processing."""
        if self.processed is None:
            return RecognitionOutcome.PENDING.value
        return self.processed.outcome.value

    @property
    def identifier(self) -> str:
        """The recognised identifier, or ``""``."""
        return "" if self.processed is None else self.processed.result.identifier_value

    @property
    def set_code(self) -> str:
        """The recognised set code, or ``""``."""
        return "" if self.processed is None else self.processed.result.set_code_value

    @property
    def output_name(self) -> str:
        """The planned or written output file name, or ``""``."""
        return "" if self.processed is None else self.processed.output_name


class ScanTableModel(QAbstractTableModel):
    """A read-only view of the scan list, for a ``QTableView``.

    Args:
        entries: Returns the current list of entries. A callable rather than
            a stored list, because the page replaces the list object itself
            wholesale on some operations (:meth:`entries_reset` covers that
            case) - reading it fresh each time means the model never risks
            holding a stale reference.
        parent: Optional Qt parent.
    """

    def __init__(
        self, entries: Callable[[], Sequence[ScanEntry]], parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._entries = entries

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:
        """How many entries there are - the whole point: read, never stored."""
        if parent.isValid():
            return 0
        return len(self._entries())

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()
    ) -> int:
        """Always :data:`COLUMN_COUNT` - a flat table, never a tree."""
        if parent.isValid():
            return 0
        return COLUMN_COUNT

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        """The column title at ``section``, for the horizontal header only."""
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return COLUMN_HEADERS[section]

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        """One cell's value, tooltip or background tint, read live from its entry."""
        if not index.isValid():
            return None
        entries = self._entries()
        row = index.row()
        if not 0 <= row < len(entries):
            return None
        entry = entries[row]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._column_text(entry, index.column())
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 0:
            return str(entry.path)
        if role == Qt.ItemDataRole.BackgroundRole:
            return STATUS_COLORS.get(entry.outcome)
        return None

    @staticmethod
    def _column_text(entry: ScanEntry, column: int) -> str:
        if column == 0:
            return entry.path.name
        if column == 1:
            return entry.identifier
        if column == 2:
            return entry.set_code
        if column == 3:
            return STATUS_LABELS.get(entry.outcome, entry.outcome)
        return entry.output_name

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        """Selectable, never editable - the scan list is read-only."""
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------------
    # Mutation hooks - called by the page, never inferred from the list
    # ------------------------------------------------------------------
    def entries_reset(self) -> None:
        """The list was replaced, emptied, or every row's content changed.

        Used for a wholesale change (loading a batch, clearing the list,
        reprocessing everything). A row count of any size resets in
        constant time - nothing here is proportional to the sheet count -
        only the *previous* per-row ``QTableWidgetItem`` population was.
        """
        self.beginResetModel()
        self.endResetModel()

    def entries_appended(self, count: int) -> None:
        """``count`` new rows were added to the end of the list.

        A proper row-insertion notification, rather than a full reset,
        because a reset clears the view's current selection - and adding
        more scans to a list the user is already looking at must not.
        """
        if count <= 0:
            return
        total = len(self._entries())
        first_row = total - count
        self.beginInsertRows(QModelIndex(), first_row, total - 1)
        self.endInsertRows()

    def mark_dirty(self, rows: Sequence[int]) -> None:
        """Repaint exactly these already-existing rows.

        Each is a completed sheet's row: the entry itself was already
        updated by the caller, so this only has to tell the view those
        cells must be re-read, not what changed.
        """
        for row in rows:
            top_left = self.index(row, 0)
            bottom_right = self.index(row, COLUMN_COUNT - 1)
            if top_left.isValid() and bottom_right.isValid():
                self.dataChanged.emit(top_left, bottom_right)


__all__ = [
    "COLUMN_COUNT",
    "COLUMN_HEADERS",
    "STATUS_COLORS",
    "STATUS_LABELS",
    "ScanEntry",
    "ScanTableModel",
]
