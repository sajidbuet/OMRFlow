"""Select a result template for one set, map its columns, and confirm.

Mirrors ``gui/attendance/import_dialog.py``'s shape - preview the real file,
map columns explicitly when guessing is ambiguous, never import (here:
associate) until the mapping is valid - simplified to a synchronous read
rather than a background worker: a result template is one small worksheet,
not a batch of scans, and phase brief §6/§7 asks for a column-mapping
dialog, not a progress bar.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.services.report_template import (
    ReportColumnMapping,
    ReportTemplateError,
    TemplatePreview,
    preview_template,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

NO_COLUMN = -1


class TemplateMappingDialog(QDialog):
    """Preview a result template and confirm (or fix) its column mapping."""

    def __init__(self, path: Path, set_code: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("templateMappingDialog")
        self.setWindowTitle(f"Result Template for Set {set_code}")
        self.setModal(True)
        self.resize(760, 560)

        self._path = path
        self.preview: TemplatePreview | None = None
        self.error_message = ""

        layout = QVBoxLayout(self)
        self.status_label = QLabel("")
        self.status_label.setObjectName("templateMappingStatusLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addWidget(self._build_mapping_box())
        layout.addWidget(self._build_preview_table(), stretch=1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.setObjectName("templateMappingButtons")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._load()

    def _build_mapping_box(self) -> QWidget:
        box = QGroupBox("Column mapping")
        form = QFormLayout(box)
        self.roll_combo = QComboBox()
        self.roll_combo.setObjectName("templateRollColumnCombo")
        self.roll_combo.currentIndexChanged.connect(self._revalidate)
        form.addRow("Roll No.:", self.roll_combo)

        self.marks_combo = QComboBox()
        self.marks_combo.setObjectName("templateMarksColumnCombo")
        self.marks_combo.currentIndexChanged.connect(self._revalidate)
        form.addRow("Marks / Total:", self.marks_combo)

        self.name_combo = QComboBox()
        self.name_combo.setObjectName("templateNameColumnCombo")
        self.name_combo.currentIndexChanged.connect(self._revalidate)
        form.addRow("Name (optional):", self.name_combo)

        self.serial_combo = QComboBox()
        self.serial_combo.setObjectName("templateSerialColumnCombo")
        self.serial_combo.currentIndexChanged.connect(self._revalidate)
        form.addRow("Sl.No. (optional):", self.serial_combo)

        self.rank_combo = QComboBox()
        self.rank_combo.setObjectName("templateRankColumnCombo")
        self.rank_combo.currentIndexChanged.connect(self._revalidate)
        form.addRow("Merit / Rank (optional):", self.rank_combo)
        return box

    def _build_preview_table(self) -> QWidget:
        self.preview_table = QTableWidget(0, 0)
        self.preview_table.setObjectName("templatePreviewTable")
        self.preview_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        return self.preview_table

    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            self.preview = preview_template(self._path)
        except ReportTemplateError as exc:
            self.error_message = exc.user_message or str(exc)
            self.status_label.setText(f"<b style='color:#a4262c'>{self.error_message}</b>")
            self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
            return

        headers = self.preview.headers
        for combo in (
            self.roll_combo, self.marks_combo, self.name_combo,
            self.serial_combo, self.rank_combo,
        ):
            combo.blockSignals(True)
            combo.clear()

        for index, header in enumerate(headers):
            label = f"Column {index + 1}: {header or '(blank)'}"
            self.roll_combo.addItem(label, index)
            self.marks_combo.addItem(label, index)
        for combo in (self.name_combo, self.serial_combo, self.rank_combo):
            combo.addItem("(not in this file)", NO_COLUMN)
            for index, header in enumerate(headers):
                combo.addItem(f"Column {index + 1}: {header or '(blank)'}", index)

        suggestion = self.preview.suggestion
        self._select(self.roll_combo, suggestion.roll)
        self._select(self.marks_combo, suggestion.marks)
        self._select(self.name_combo, suggestion.name, none_value=NO_COLUMN)
        self._select(self.serial_combo, suggestion.serial, none_value=NO_COLUMN)
        self._select(self.rank_combo, suggestion.rank, none_value=NO_COLUMN)

        for combo in (
            self.roll_combo, self.marks_combo, self.name_combo,
            self.serial_combo, self.rank_combo,
        ):
            combo.blockSignals(False)

        self._fill_preview_table()
        self._revalidate()

    def _select(self, combo: QComboBox, value: int | None, *, none_value: int = 0) -> None:
        target = value if value is not None else none_value
        index = combo.findData(target)
        combo.setCurrentIndex(index if index >= 0 else 0)

    def _fill_preview_table(self) -> None:
        assert self.preview is not None
        self.preview_table.setColumnCount(len(self.preview.headers))
        self.preview_table.setHorizontalHeaderLabels(list(self.preview.headers))
        shown = self.preview.rows[:20]
        self.preview_table.setRowCount(len(shown))
        for row_index, row in enumerate(shown):
            for column_index, value in enumerate(row):
                self.preview_table.setItem(
                    row_index, column_index, QTableWidgetItem(value)
                )

    def current_mapping(self) -> ReportColumnMapping | None:
        """The mapping this dialog currently describes, or ``None`` if invalid."""
        if self.preview is None:
            return None
        roll = self.roll_combo.currentData()
        marks = self.marks_combo.currentData()
        if roll is None or marks is None or roll == marks:
            return None
        name = self.name_combo.currentData()
        serial = self.serial_combo.currentData()
        rank = self.rank_combo.currentData()
        return ReportColumnMapping(
            roll=roll, marks=marks,
            name=None if name == NO_COLUMN else name,
            serial=None if serial == NO_COLUMN else serial,
            rank=None if rank == NO_COLUMN else rank,
        )

    def _revalidate(self) -> None:
        mapping = self.current_mapping()
        valid = mapping is not None
        if valid and mapping is not None:
            same = {mapping.roll, mapping.marks} & {
                v for v in (mapping.name, mapping.serial, mapping.rank) if v is not None
            }
            valid = not same
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(valid)
        if valid:
            self.status_label.setText(
                f"<b style='color:#1b7f3b'>Valid.</b> {self.preview.data_rows} "
                "candidate row(s) found."
                if self.preview else ""
            )
        else:
            self.status_label.setText(
                "<b style='color:#a4262c'>Choose distinct columns for Roll No. "
                "and Marks before saving.</b>"
            )

    @property
    def sheet_name(self) -> str:
        """The worksheet this mapping was read from."""
        return self.preview.sheet if self.preview else ""
