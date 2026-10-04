"""The scan session's sheet list: a paged table over SQL (0.1.1 revised phase 8).

Purpose:
    A continuous session can hold tens of thousands of sheets. The finite
    Scan stage's list (:mod:`omr_scanner.gui.scan.table_model`) holds the
    current batch's entries in memory, which is right for a batch and wrong
    for a session. This list holds **one page** - at most
    :data:`~omr_scanner.services.session_sheets.PAGE_SIZE` rows - and asks
    :mod:`omr_scanner.services.session_sheets` for each page, already filtered,
    searched and sorted in SQL. Nothing here walks every sheet.

Responsibilities:
    * :class:`SessionSheetModel` - the page as a read-only
      ``QAbstractTableModel``; header clicks ask for a re-sorted query.
    * :class:`SessionSheetList` - the filters (status, source, batch, quality,
      conflict, rescan, search), paging and the table.

What does NOT belong here:
    SQL, counting, classification. The rows are the service's
    :class:`~omr_scanner.services.session_sheets.SheetRow` values, shown as
    they are.

Selection survives a refresh:
    By the sheet's id, never by row number - a new arrival shifts every row
    of a newest-first list down by one.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.scan_lifecycle import LifecycleState
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.scan.table_model import STATUS_COLORS
from omr_scanner.gui.theme import Spacing
from omr_scanner.gui.ui_scale import scale_layout
from omr_scanner.services import session_sheets
from omr_scanner.services.session_sheets import (
    ConflictStateFilter,
    QualityFilter,
    RescanFilter,
    SheetQuery,
    SheetRow,
    SheetSort,
    StatusFilter,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.services import ProjectDatabase

COLUMNS = (
    ("Status", SheetSort.STATUS),
    ("Student ID", SheetSort.STUDENT_ID),
    ("Set", SheetSort.SET_CODE),
    ("Source", SheetSort.SOURCE),
    ("Batch", SheetSort.BATCH),
    ("Original file", SheetSort.FILE_NAME),
    ("Quality", SheetSort.QUALITY),
    ("Conflict", SheetSort.STATUS),
    ("Rescan", SheetSort.STATUS),
)
"""Column title and the SQL order a header click asks for. *Conflict* and
*Rescan* are not sortable on their own (they would need a per-row subquery in
the ORDER BY over the whole session); clicking them keeps the status order."""

SORTABLE = frozenset({0, 1, 2, 3, 4, 5, 6})

_STATUS_TEXT = {
    "pending": "Not read",
    "cancelled": "Not read",
    "queued": "Queued",
    "processing": "Being read",
    "completed": "Read",
    "warning": "Needs review",
    "failed": "Failed",
    "duplicate": "Duplicate image",
}
_STATUS_OUTCOME = {
    "completed": "complete",
    "warning": "review",
    "failed": "error",
}

_QUALITY_TEXT = {
    "": "",
    "accept": "Accepted",
    "accept_with_warning": "Warning",
    "rescan_required": "Rescan required",
    "retry_processing": "Read again",
}

_RESCAN_TEXT = {
    LifecycleState.ACTIVE.value: "",
    LifecycleState.REJECTED_PENDING_RESCAN.value: "Rejected - awaiting rescan",
    LifecycleState.SUPERSEDED_BY_REPLACEMENT.value: "Replaced",
    LifecycleState.REIMPORT_OF_REJECTED.value: "Re-import of rejected",
}


def status_text(row: SheetRow) -> str:
    """The sheet's processing status, in words."""
    return _STATUS_TEXT.get(row.status, row.status.replace("_", " ").capitalize())


def quality_text(row: SheetRow) -> str:
    """The quality decision; an unanswered suggestion says so."""
    if row.suggestion_outstanding:
        return "Suggested rescan"
    return _QUALITY_TEXT.get(row.quality, row.quality)


def rescan_text(row: SheetRow) -> str:
    """Where the sheet stands in Reject & Rescan."""
    if row.is_replacement:
        return "Confirmed rescan"
    return _RESCAN_TEXT.get(row.lifecycle, row.lifecycle.replace("_", " ").capitalize())


def cell_text(row: SheetRow, column: int) -> str:
    """One cell's text."""
    if column == 0:
        return status_text(row)
    if column == 1:
        return row.student_id
    if column == 2:
        return row.set_code
    if column == 3:
        return row.source_label or "Added by hand"
    if column == 4:
        return row.batch_label
    if column == 5:
        return row.original_name or row.stored_name
    if column == 6:
        return quality_text(row)
    if column == 7:
        return f"{row.unresolved_conflicts} unresolved" if row.unresolved_conflicts else ""
    return rescan_text(row)


def row_tooltip(row: SheetRow) -> str:
    """Everything known about the row - including the internal details, here only."""
    lines = [row.original_name or row.stored_name]
    if row.stored_name and row.stored_name != row.original_name:
        lines.append(f"Stored copy: {row.stored_name}")
    lines.append(f"{row.batch_label} (batch {row.batch_id[:8]})")
    if row.arrived_at is not None:
        lines.append(f"Arrived {row.arrived_at.astimezone():%Y-%m-%d %H:%M:%S}")
    if row.read_at is not None:
        lines.append(f"Read {row.read_at.astimezone():%Y-%m-%d %H:%M:%S}")
    return "\n".join(lines)


class SessionSheetModel(QAbstractTableModel):
    """One page of a session's sheets. Read-only.

    Signals:
        sort_requested: ``(SheetSort, descending)`` when a header is clicked.
    """

    sort_requested = Signal(object, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: tuple[SheetRow, ...] = ()

    def set_rows(self, rows: tuple[SheetRow, ...]) -> None:
        """Replace the page."""
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        """Rows in this page (never the session's total)."""
        if parent is not None and parent.isValid():
            return 0
        return len(self.rows)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        """The fixed columns."""
        if parent is not None and parent.isValid():
            return 0
        return len(COLUMNS)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        """Cell text, tooltip, status tint, and the sheet id under ``UserRole``."""
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return cell_text(row, index.column())
        if role == Qt.ItemDataRole.ToolTipRole:
            return row_tooltip(row)
        if role == Qt.ItemDataRole.UserRole:
            return row.scan_id
        if role == Qt.ItemDataRole.BackgroundRole and index.column() == 0:
            outcome = _STATUS_OUTCOME.get(row.status)
            return STATUS_COLORS.get(outcome) if outcome else None
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        """Column titles."""
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(COLUMNS)
        ):
            return COLUMNS[section][0]
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Ask for the page re-queried in a new SQL order (never sorted in Python)."""
        if column not in SORTABLE:
            return
        self.sort_requested.emit(
            COLUMNS[column][1], order == Qt.SortOrder.DescendingOrder
        )

    def row_of(self, scan_id: int) -> int:
        """The row of ``scan_id`` in this page, or ``-1``."""
        return next(
            (index for index, row in enumerate(self.rows) if row.scan_id == scan_id), -1
        )


class SessionSheetList(QWidget):
    """Filters, paging and the session sheet table.

    Signals:
        sheet_selected: the selected :class:`SheetRow` (or ``None``).
        failed: a message when the list could not be read (shown, never raised).
    """

    sheet_selected = Signal(object)
    failed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sessionSheetList")
        self._database: ProjectDatabase | None = None
        self._session_id = ""
        self._offset = 0
        self._total = 0
        self._sort = SheetSort.ARRIVAL
        self._descending = True
        self._batch_count = -1
        self.page_size = session_sheets.PAGE_SIZE
        self.last_query_ms = 0.0
        """How long the last page (count + rows) took - responsiveness evidence."""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scale_layout(layout, spacing=Spacing.XXS)

        first = QHBoxLayout()
        first.setContentsMargins(0, 0, 0, 0)
        scale_layout(first, spacing=Spacing.XS)
        self.status_combo = self._combo("sessionStatusFilter", StatusFilter, "Status")
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("sessionSourceFilter")
        self.source_combo.setAccessibleName("Source filter")
        self.batch_combo = QComboBox()
        self.batch_combo.setObjectName("sessionBatchFilter")
        self.batch_combo.setAccessibleName("Batch filter")
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("sessionSheetSearch")
        self.search_edit.setPlaceholderText("Search Student ID or file name...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName("Search sheets")
        self.search_edit.editingFinished.connect(self._on_filter_changed)
        for combo in (self.source_combo, self.batch_combo):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(10)
            combo.currentIndexChanged.connect(self._on_filter_changed)
        first.addWidget(self.status_combo)
        first.addWidget(self.source_combo)
        first.addWidget(self.batch_combo)
        first.addWidget(self.search_edit, stretch=1)
        layout.addLayout(first)

        second = QHBoxLayout()
        second.setContentsMargins(0, 0, 0, 0)
        scale_layout(second, spacing=Spacing.XS)
        self.quality_combo = self._combo("sessionQualityFilter", QualityFilter, "Quality")
        self.conflict_combo = self._combo(
            "sessionConflictFilter", ConflictStateFilter, "Conflict"
        )
        self.rescan_combo = self._combo("sessionRescanFilter", RescanFilter, "Rescan")
        second.addWidget(self.quality_combo)
        second.addWidget(self.conflict_combo)
        second.addWidget(self.rescan_combo)
        second.addStretch(1)
        self.previous_button = QPushButton("Previous")
        self.previous_button.setObjectName("sessionPreviousPage")
        self.previous_button.clicked.connect(self.previous_page)
        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("sessionNextPage")
        self.next_button.clicked.connect(self.next_page)
        self.page_label = QLabel("")
        self.page_label.setObjectName("sessionPageLabel")
        second.addWidget(self.page_label)
        second.addWidget(self.previous_button)
        second.addWidget(self.next_button)
        layout.addLayout(second)

        self.model = SessionSheetModel(self)
        self.model.sort_requested.connect(self._on_sort)
        self.table = QTableView()
        self.table.setObjectName("sessionSheetTable")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSortIndicatorShown(True)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        # Secondary columns hidden by default at narrow widths would surprise;
        # they are kept but narrow, and every value is in the row's tooltip.
        self.table.selectionModel().selectionChanged.connect(
            lambda *_args: self._on_selection()
        )
        layout.addWidget(self.table, stretch=1)
        for combo in (
            self.status_combo, self.quality_combo, self.conflict_combo, self.rescan_combo
        ):
            combo.currentIndexChanged.connect(self._on_filter_changed)
        self._refresh_paging()

    @staticmethod
    def _combo(name: str, enum: type, accessible: str) -> QComboBox:
        combo = QComboBox()
        combo.setObjectName(name)
        combo.setAccessibleName(f"{accessible} filter")
        for item in enum:  # type: ignore[attr-defined]
            combo.addItem(item.label, userData=item)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(10)
        return combo

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------
    @property
    def session_id(self) -> str:
        """The session listed, or ``""``."""
        return self._session_id

    @property
    def total(self) -> int:
        """Sheets matching the filters (the count query's answer)."""
        return self._total

    def set_session(self, database: ProjectDatabase | None, scan_session_id: str) -> None:
        """List ``scan_session_id`` (``""``: nothing). Filters reset to *all*."""
        self._database = database
        self._session_id = scan_session_id if database is not None else ""
        self._offset = 0
        self._batch_count = -1
        for combo in (
            self.status_combo, self.quality_combo, self.conflict_combo, self.rescan_combo
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.search_edit.clear()
        self._fill_options()
        self.refresh()

    def _fill_options(self) -> None:
        database, session_id = self._database, self._session_id
        sources = (
            session_sheets.session_sources(database, session_id)
            if database is not None and session_id
            else ()
        )
        batches = (
            session_sheets.session_batches(database, session_id)
            if database is not None and session_id
            else ()
        )
        self._batch_count = len(batches)
        for combo, first_label, options in (
            (self.source_combo, "All sources", [(item.label, item.source_id) for item in sources]),
            (self.batch_combo, "All batches", [(item.label, item.batch_id) for item in batches]),
        ):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(first_label, userData="")
            for label, value in options:
                combo.addItem(label, userData=value)
            index = combo.findData(current) if current else 0
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)

    def query(self) -> SheetQuery:
        """The query the controls describe."""
        batch = str(self.batch_combo.currentData() or "")
        return SheetQuery(
            status=self.status_combo.currentData() or StatusFilter.ALL,
            quality=self.quality_combo.currentData() or QualityFilter.ALL,
            conflict=self.conflict_combo.currentData() or ConflictStateFilter.ALL,
            rescan=self.rescan_combo.currentData() or RescanFilter.ALL,
            source_id=str(self.source_combo.currentData() or ""),
            batch_ids=(batch,) if batch else (),
            search=self.search_edit.text().strip(),
            sort=self._sort,
            descending=self._descending,
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def refresh(self, *, keep_page: bool = True) -> bool:
        """Re-read the current page (and the count). Keeps the selected sheet by id."""
        database, session_id = self._database, self._session_id
        selected = self.selected_scan_id()
        if database is None or not session_id:
            self._total = 0
            self.model.set_rows(())
            self._refresh_paging()
            return False
        if not keep_page:
            self._offset = 0
        started = time.perf_counter()
        try:
            batches = len(session_sheets.session_batches(database, session_id))
            if batches != self._batch_count:
                self._fill_options()
            query = self.query()
            self._total = session_sheets.count_sheets(database, session_id, query)
            if self._offset >= self._total and self._total:
                self._offset = max(0, (self._total - 1) // self.page_size * self.page_size)
            rows = session_sheets.list_sheets(
                database, session_id, query, offset=self._offset, limit=self.page_size
            )
        except OMRScannerError as exc:
            self.failed.emit(exc.user_message or str(exc))
            return False
        except Exception as exc:  # a list that cannot be read must not crash the page
            self.failed.emit(str(exc))
            return False
        self.last_query_ms = (time.perf_counter() - started) * 1000.0
        self.table.blockSignals(True)
        self.model.set_rows(rows)
        self.table.blockSignals(False)
        if selected is not None:
            self.select_scan(selected, emit=False)
        self._refresh_paging()
        return True

    def next_page(self) -> bool:
        """Show the next page."""
        if self._offset + self.page_size >= self._total:
            return False
        self._offset += self.page_size
        return self.refresh()

    def previous_page(self) -> bool:
        """Show the previous page."""
        if self._offset <= 0:
            return False
        self._offset = max(0, self._offset - self.page_size)
        return self.refresh()

    def _on_filter_changed(self, *_args: object) -> None:
        self.refresh(keep_page=False)

    def _on_sort(self, sort: SheetSort, descending: bool) -> None:
        self._sort = sort
        self._descending = descending
        self.refresh(keep_page=False)

    def _refresh_paging(self) -> None:
        if not self._total:
            self.page_label.setText("No sheets" if self._session_id else "")
        else:
            last = min(self._offset + self.page_size, self._total)
            self.page_label.setText(f"{self._offset + 1:,}-{last:,} of {self._total:,}")
        self.previous_button.setEnabled(self._offset > 0)
        self.next_button.setEnabled(self._offset + self.page_size < self._total)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------
    def selected_row(self) -> SheetRow | None:
        """The selected sheet, or ``None``."""
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        row = indexes[0].row()
        return self.model.rows[row] if 0 <= row < len(self.model.rows) else None

    def selected_scan_id(self) -> int | None:
        """The selected sheet's id, or ``None``."""
        row = self.selected_row()
        return row.scan_id if row is not None else None

    def select_scan(self, scan_id: int, *, emit: bool = True) -> bool:
        """Select a sheet of the current page by id."""
        row = self.model.row_of(scan_id)
        if row < 0:
            return False
        if not emit:
            self.table.selectionModel().blockSignals(True)
        try:
            self.table.selectRow(row)
        finally:
            if not emit:
                self.table.selectionModel().blockSignals(False)
        return True

    def _on_selection(self) -> None:
        self.sheet_selected.emit(self.selected_row())


__all__ = [
    "COLUMNS",
    "SessionSheetList",
    "SessionSheetModel",
    "cell_text",
    "quality_text",
    "rescan_text",
    "status_text",
]
