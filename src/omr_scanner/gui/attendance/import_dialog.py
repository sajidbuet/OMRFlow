"""Choose a candidate list, map its columns, and see what validation found.

The interface for brief steps A-F: select a file, select a worksheet, preview
it, map the columns, validate, import.

Design notes worth keeping:

* **The preview is of the real file**, not a description of it. An operator
  who can see ``ABSENT`` in the column they just mapped does not have to trust
  that the importer agrees with them.
* **Changing a mapping re-reads the file** rather than reinterpreting the last
  answer. A different candidate-ID column means different duplicates and
  different counts, and patching the previous result is how those go stale.
* **Importing is disabled until validation passes.** A roster with a repeated
  candidate ID is not importable "with warnings": the file states two different
  facts about one person, and no downstream phase can be right about which.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.attendance.worker import RosterReadResult, RosterReadWorker
from omr_scanner.services.candidate_import import (
    CandidateImportError,
    ColumnMapping,
    RosterValidation,
    list_worksheets,
    preview_roster,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

NO_COLUMN = -1
"""The "(not in this file)" entry in an optional column's dropdown."""

MAX_PREVIEW_ROWS = 50


class RosterImportDialog(QDialog):
    """Map a candidate list's columns and confirm what will be imported.

    The dialog owns no import: it produces a validated
    :class:`~omr_scanner.services.candidate_import.RosterValidation`, and the
    page commits it. That split is what lets the whole mapping-and-validation
    behaviour be tested without a modal dialog, through
    :meth:`apply_mapping` and :attr:`validation`.
    """

    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rosterImportDialog")
        self.setWindowTitle("Import Candidate List")
        self.setModal(True)
        self.resize(900, 640)

        self._path = path
        self._worker: RosterReadWorker | None = None
        # Every reader ever started, so none can be collected mid-run. A
        # QThread garbage-collected while still running aborts the process, and
        # an operator flicking through the column dropdowns supersedes readers
        # faster than they finish. See `_track`.
        self._workers: list[RosterReadWorker] = []
        self.validation: RosterValidation | None = None
        self._headers: tuple[str, ...] = ()
        self._sheet = ""

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_source_box())
        layout.addWidget(self._build_mapping_box())
        layout.addWidget(self._build_preview(), stretch=1)
        layout.addWidget(self._build_validation_box())

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
        )
        self.import_button = QPushButton("Import Candidates")
        self.import_button.setObjectName("confirmRosterImportButton")
        self.import_button.setDefault(True)
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self.accept)
        self.buttons.addButton(
            self.import_button, QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.load()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_source_box(self) -> QWidget:
        """The file, and the worksheet chooser a workbook needs."""
        box = QGroupBox("Source file")
        box.setObjectName("rosterSourceBox")
        layout = QHBoxLayout(box)

        self.file_label = QLabel(self._path.name)
        self.file_label.setObjectName("rosterFileLabel")
        self.file_label.setToolTip(str(self._path))
        layout.addWidget(self.file_label, stretch=1)

        self.sheet_label = QLabel("Worksheet:")
        layout.addWidget(self.sheet_label)
        self.sheet_combo = QComboBox()
        self.sheet_combo.setObjectName("rosterSheetCombo")
        self.sheet_combo.setMinimumWidth(220)
        self.sheet_combo.currentIndexChanged.connect(self._on_sheet_changed)
        layout.addWidget(self.sheet_combo)
        return box

    def _build_mapping_box(self) -> QWidget:
        """The three column dropdowns."""
        box = QGroupBox("Which column holds what")
        box.setObjectName("rosterMappingBox")
        layout = QFormLayout(box)

        self.id_combo = QComboBox()
        self.id_combo.setObjectName("candidateIdColumnCombo")
        self.id_combo.setToolTip(
            "The roll or candidate number. Required - it is what a scanned "
            "sheet is matched against."
        )
        self.id_combo.currentIndexChanged.connect(self._on_mapping_changed)
        layout.addRow("Candidate ID (required):", self.id_combo)

        self.name_combo = QComboBox()
        self.name_combo.setObjectName("candidateNameColumnCombo")
        self.name_combo.currentIndexChanged.connect(self._on_mapping_changed)
        layout.addRow("Candidate Name (optional):", self.name_combo)

        self.attendance_combo = QComboBox()
        self.attendance_combo.setObjectName("attendanceColumnCombo")
        self.attendance_combo.setToolTip(
            "A marks or attendance column. 'ABSENT' or 'ABS' - in any case, "
            "with any spacing - means the candidate did not sit the paper. "
            "Anything else, including a blank cell, means they were not marked "
            "absent."
        )
        self.attendance_combo.currentIndexChanged.connect(self._on_mapping_changed)
        layout.addRow("Marks / Attendance (optional):", self.attendance_combo)
        return box

    def _build_preview(self) -> QWidget:
        """The first rows of the file, exactly as it reads."""
        box = QGroupBox(f"Preview (first {MAX_PREVIEW_ROWS} rows)")
        box.setObjectName("rosterPreviewBox")
        layout = QVBoxLayout(box)
        self.preview_table = QTableWidget(0, 0)
        self.preview_table.setObjectName("rosterPreviewTable")
        self.preview_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.preview_table.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.preview_table.setAlternatingRowColors(True)
        layout.addWidget(self.preview_table)
        return box

    def _build_validation_box(self) -> QWidget:
        """What validation found, and whether the roster may be imported."""
        box = QGroupBox("Validation")
        box.setObjectName("rosterValidationBox")
        layout = QVBoxLayout(box)
        self.validation_label = QLabel("Reading the file...")
        self.validation_label.setObjectName("rosterValidationLabel")
        self.validation_label.setWordWrap(True)
        self.validation_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.validation_label)
        return box

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def load(self) -> None:
        """Read the file's shape and offer a mapping."""
        try:
            sheets = list_worksheets(self._path)
        except CandidateImportError as exc:
            self._fail(exc.user_message or str(exc))
            return

        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        if sheets:
            self.sheet_combo.addItems(list(sheets))
        self.sheet_combo.blockSignals(False)
        has_sheets = bool(sheets)
        self.sheet_combo.setVisible(has_sheets)
        self.sheet_label.setVisible(has_sheets)
        self._sheet = sheets[0] if sheets else ""
        self._load_sheet()

    def _on_sheet_changed(self) -> None:
        """Adopt a different worksheet and re-read it."""
        self._sheet = self.sheet_combo.currentText()
        self._load_sheet()

    def _load_sheet(self) -> None:
        """Preview the chosen sheet and set the mapping dropdowns from it."""
        try:
            preview = preview_roster(
                self._path, sheet=self._sheet, limit=MAX_PREVIEW_ROWS
            )
        except CandidateImportError as exc:
            self._fail(exc.user_message or str(exc))
            return

        self._headers = preview.headers
        self._fill_preview(preview.headers, preview.rows)
        self._fill_columns(preview.headers)

        suggestion = preview.suggestion
        self._set_combo(self.id_combo, suggestion.candidate_id)
        self._set_combo(self.name_combo, suggestion.name)
        self._set_combo(self.attendance_combo, suggestion.attendance)

        if suggestion.is_ambiguous:
            named = ", ".join(
                f"<b>{preview.headers[i]}</b>"
                for i in suggestion.ambiguous_candidate_id
            )
            self._say(
                f"More than one column could be the candidate ID: {named}. "
                "Select the column containing roll/candidate numbers before "
                "continuing.",
                ok=False,
            )
            self.import_button.setEnabled(False)
            return
        self._revalidate()

    def _fill_preview(
        self, headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...]
    ) -> None:
        """Populate the preview table."""
        self.preview_table.clear()
        self.preview_table.setColumnCount(len(headers))
        self.preview_table.setHorizontalHeaderLabels(
            [header or f"(column {i + 1})" for i, header in enumerate(headers)]
        )
        self.preview_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                self.preview_table.setItem(
                    row_index, column_index, QTableWidgetItem(value)
                )
        header = self.preview_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)

    def _fill_columns(self, headers: tuple[str, ...]) -> None:
        """Populate the three dropdowns with this file's columns."""
        for combo, optional in (
            (self.id_combo, False),
            (self.name_combo, True),
            (self.attendance_combo, True),
        ):
            combo.blockSignals(True)
            combo.clear()
            if optional:
                combo.addItem("(not in this file)", NO_COLUMN)
            for index, header in enumerate(headers):
                label = header or f"(column {index + 1})"
                combo.addItem(f"{label}  [column {index + 1}]", index)
            combo.blockSignals(False)

    def _set_combo(self, combo: QComboBox, column: int | None) -> None:
        """Select a column in a dropdown without triggering a re-read."""
        combo.blockSignals(True)
        target = NO_COLUMN if column is None else column
        found = combo.findData(target)
        combo.setCurrentIndex(found if found >= 0 else 0)
        combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Mapping and validation
    # ------------------------------------------------------------------
    def current_mapping(self) -> ColumnMapping | None:
        """The mapping the dropdowns currently describe."""
        candidate = self.id_combo.currentData()
        if candidate is None or candidate == NO_COLUMN:
            return None

        def optional(combo: QComboBox) -> int | None:
            value = combo.currentData()
            return None if value is None or value == NO_COLUMN else int(value)

        return ColumnMapping(
            candidate_id=int(candidate),
            name=optional(self.name_combo),
            attendance=optional(self.attendance_combo),
        )

    def _on_mapping_changed(self) -> None:
        """Re-read the file whenever the operator changes a column."""
        self._revalidate()

    def _revalidate(self) -> None:
        """Read the whole file under the current mapping, off the GUI thread."""
        mapping = self.current_mapping()
        if mapping is None:
            self._say(
                "Select the column containing roll/candidate numbers before "
                "continuing.",
                ok=False,
            )
            self.import_button.setEnabled(False)
            return
        self.validation = None
        self.import_button.setEnabled(False)
        self._say("Checking the candidate list...", ok=None)
        self.apply_mapping(mapping)

    def apply_mapping(self, mapping: ColumnMapping) -> None:
        """Read the file under ``mapping``. Public so a test can drive it."""
        if self._worker is not None and self._worker.isRunning():
            # Superseded, not cancelled: a read of a file already open is a
            # fraction of a second, and interrupting one buys nothing. Its
            # result is simply ignored - but it is still tracked, because a
            # running thread that nothing holds a reference to is a crash.
            self._worker.ready.disconnect()
        worker = RosterReadWorker(self._path, mapping, self._sheet, self)
        worker.ready.connect(self._on_read)
        self._worker = worker
        self._track(worker)
        worker.start()

    def _track(self, worker: RosterReadWorker) -> None:
        """Keep a reference to a live worker, and forget the finished ones."""
        self._workers = [item for item in self._workers if item.isRunning()]
        self._workers.append(worker)

    def _on_read(self, result: RosterReadResult) -> None:
        """Adopt a completed read. Runs on the GUI thread."""
        if not result.ok or result.validation is None:
            self._fail(result.error)
            return
        self.validation = result.validation
        self._describe(result.validation)

    def _describe(self, validation: RosterValidation) -> None:
        """Say what will be imported, and what stands in the way."""
        lines = [f"<b>{validation.summary}</b>"]
        blocking = validation.blocking_issues
        if blocking:
            shown = blocking[:5]
            lines.append(
                "<br><b>This candidate list cannot be imported yet:</b><ul>"
                + "".join(f"<li>{item.message}</li>" for item in shown)
                + ("</ul>" if len(blocking) <= 5 else
                   f"</ul>and {len(blocking) - 5} more.")
            )
        elif validation.mapping.attendance is None:
            lines.append(
                "<br>No marks/attendance column was mapped, so nobody will be "
                "treated as expected present or absent. Candidates without a "
                "script will not be reported as missing."
            )
        self._say("<br>".join(lines), ok=validation.can_import)
        self.import_button.setEnabled(validation.can_import)

    def _say(self, message: str, *, ok: bool | None) -> None:
        """Show a validation message, coloured by outcome."""
        colour = {True: "#1b7f3b", False: "#a4262c", None: ""}[ok]
        style = f"color: {colour};" if colour else ""
        self.validation_label.setStyleSheet(style)
        self.validation_label.setText(message)

    def _fail(self, message: str) -> None:
        """Report that the file cannot be used, and refuse the import."""
        self.validation = None
        self.import_button.setEnabled(False)
        self._say(message or "The candidate list could not be read.", ok=False)

    # ------------------------------------------------------------------
    # Lifetime
    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        """Wait for every reader this dialog started.

        A ``QThread`` still running when the interpreter tears down aborts the
        process - on Windows with a bare ``0xC0000409`` and no traceback - so
        every worker is joined, not just the most recent. A superseded reader
        is still a running thread.
        """
        workers = self._workers
        self._worker = None
        self._workers = []
        for worker in workers:
            if worker.isRunning():
                worker.wait(5000)

    def done(self, result: int) -> None:
        """Close the dialog, joining the readers first."""
        self.shutdown()
        super().done(result)

    def closeEvent(self, event: object) -> None:
        """Join the readers when the dialog is closed rather than answered.

        ``done()`` covers Import and Cancel; this covers the window's close
        button, and a dialog that a test closes without ever showing.
        """
        self.shutdown()
        super().closeEvent(event)  # type: ignore[arg-type]
