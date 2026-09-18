"""Reading a benchmark result.

Purpose:
    Show what the engine got right and, far more usefully, *which kinds of
    sheet* it got wrong - then let a developer jump to a failing scan and look
    at it.

Responsibilities:
    * :class:`BenchmarkResultsDialog` - headline metrics, the per-test-case
      table, every individual disagreement, and the failing scans.

What does NOT belong here:
    * Scoring anything. :mod:`omr_scanner.evaluation.benchmark` produces the
      report; this displays it.

Layout, and why:
    Three tabs rather than one long scroll. The headline answers "is anything
    wrong", the categories answer "what kind", and the errors answer "which
    sheet" - three different questions asked at three different moments, and
    putting them on one page means the important one is always half off the
    screen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.services import format_count, format_duration

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.evaluation.benchmark import BenchmarkReport

ERROR_COLUMNS = ("Scan", "Category", "Q", "Expected", "Read", "Status", "Score")
CATEGORY_COLUMNS = ("Test case", "Sheets", "Sheet accuracy", "Answer accuracy", "Errors")

MAX_ERROR_ROWS = 5000
"""How many individual disagreements the table will show.

A benchmark can produce tens of thousands; a table that tries to show them all
takes seconds to build and nobody scrolls past the first hundred. The full list
is always in ``errors.csv``, and the dialog says so."""


class BenchmarkResultsDialog(QDialog):
    """Show one benchmark run.

    Signals:
        scan_requested: ``str`` file name, when the user asks to look at a
            failing scan. The Scan page selects it - which is the whole reason
            benchmarking runs *in* that page rather than in a window of its
            own.

    Args:
        report: What to show.
        report_dir: Where the files were written, when they were.
        parent: Optional Qt parent.
    """

    scan_requested = Signal(str)

    def __init__(
        self,
        report: BenchmarkReport,
        report_dir: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("benchmarkResultsDialog")
        self.setWindowTitle("Recognition Benchmark")
        self.setMinimumSize(760, 560)

        self._report = report
        self._report_dir = report_dir

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_headline())

        tabs = QTabWidget()
        tabs.setObjectName("benchmarkTabs")
        tabs.addTab(self._build_summary_tab(), "Summary")
        tabs.addTab(self._build_categories_tab(), "By test case")
        tabs.addTab(self._build_errors_tab(), "Errors")
        tabs.addTab(self._build_failures_tab(), "Failing scans")
        layout.addWidget(tabs, stretch=1)

        layout.addWidget(self._build_buttons())

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_headline(self) -> QLabel:
        """The one line somebody reads before deciding whether to care."""
        summary = self._report.summary
        label = QLabel(
            f"<b>{summary.dataset or 'Benchmark'}</b> · "
            f"{format_count(summary.scans)} scan(s) · "
            f"sheet accuracy {summary.sheet_accuracy:.4f} · "
            f"answer accuracy {summary.question_accuracy:.4f} · "
            f"{format_count(len(self._report.errors))} disagreement(s)"
        )
        label.setObjectName("benchmarkHeadlineLabel")
        label.setWordWrap(True)
        return label

    def _build_summary_tab(self) -> QWidget:
        """Every headline metric, with its counts beside it."""
        summary = self._report.summary
        page = QWidget()
        layout = QVBoxLayout(page)

        table = QTableWidget(0, 2)
        table.setObjectName("benchmarkSummaryTable")
        table.setHorizontalHeaderLabels(["Metric", "Value"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        rows: list[tuple[str, str]] = [
            ("Scans", format_count(summary.scans)),
            ("Processed", format_count(summary.processed)),
            ("Failed to register", format_count(summary.failed)),
            ("Expected failures (not scored)", format_count(summary.expected_failures)),
            (
                "Sheets read with nothing wrong",
                _rate_text(
                    summary.sheets_correct,
                    summary.scans - summary.expected_failures,
                    summary.sheet_accuracy,
                ),
            ),
            (
                "Registration",
                _rate_text(
                    summary.registration_succeeded,
                    summary.registration_expected,
                    summary.registration_rate,
                ),
            ),
            (
                "Student ID",
                _rate_text(summary.roll_correct, summary.roll_checked, summary.roll_accuracy),
            ),
            (
                "Set code",
                _rate_text(summary.set_correct, summary.set_checked, summary.set_accuracy),
            ),
            (
                "Answers",
                _rate_text(
                    summary.questions_correct,
                    summary.questions_checked,
                    summary.question_accuracy,
                ),
            ),
            (
                "Blank questions",
                _rate_text(
                    summary.blanks_correct, summary.blanks_expected, summary.blank_accuracy
                ),
            ),
            (
                "Multiple marks",
                _rate_text(
                    summary.multiples_correct,
                    summary.multiples_expected,
                    summary.multiple_accuracy,
                ),
            ),
            (
                "Borderline marks handled acceptably",
                _rate_text(
                    summary.ambiguous_handled,
                    summary.ambiguous_expected,
                    summary.ambiguous_handled_rate,
                ),
            ),
            ("Questions flagged for review", format_count(summary.flagged_questions)),
            (
                "Duplicate identifier groups found",
                f"{summary.duplicate_groups_detected}/{summary.duplicate_groups_expected}"
                f"  ({summary.duplicate_groups_false} unplanted)",
            ),
            ("Time", f"{format_duration(summary.total_seconds)} total, "
                     f"{summary.mean_seconds_per_scan:.3f}s per scan"),
            ("Engine", summary.engine_version or "-"),
        ]
        for name, count in sorted(summary.error_counts.items()):
            rows.append((f"Errors: {name}", format_count(count)))

        table.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            table.setItem(row, 0, QTableWidgetItem(name))
            table.setItem(row, 1, QTableWidgetItem(value))
        layout.addWidget(table)

        note = QLabel(
            "Duplicate identifiers are a batch-level concern and are reported apart "
            "from recognition accuracy: reading the same number twice is correct."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _build_categories_tab(self) -> QWidget:
        """The per-test-case table: the most actionable thing in the dialog."""
        page = QWidget()
        layout = QVBoxLayout(page)

        note = QLabel(
            "Least accurate first. A dash means the category has nothing to score - "
            "it is made of sheets that were supposed to fail."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        table = QTableWidget(len(self._report.categories), len(CATEGORY_COLUMNS))
        table.setObjectName("benchmarkCategoryTable")
        table.setHorizontalHeaderLabels(list(CATEGORY_COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        for row, item in enumerate(self._report.categories):
            values = (
                item.tag,
                str(item.sheets),
                f"{item.sheet_accuracy:.4f}" if item.scored else "-",
                f"{item.question_accuracy:.4f}" if item.questions_checked else "-",
                str(item.errors),
            )
            for column, text in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(text))
        layout.addWidget(table)
        return page

    def _build_errors_tab(self) -> QWidget:
        """Every disagreement, with the engine's own status beside it."""
        page = QWidget()
        layout = QVBoxLayout(page)

        errors = self._report.errors[:MAX_ERROR_ROWS]
        if len(self._report.errors) > MAX_ERROR_ROWS:
            note = QLabel(
                f"Showing the first {format_count(MAX_ERROR_ROWS)} of "
                f"{format_count(len(self._report.errors))}. The full list is in errors.csv."
            )
            note.setWordWrap(True)
            layout.addWidget(note)

        table = QTableWidget(len(errors), len(ERROR_COLUMNS))
        table.setObjectName("benchmarkErrorTable")
        table.setHorizontalHeaderLabels(list(ERROR_COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.setUpdatesEnabled(False)
        try:
            for row, record in enumerate(errors):
                values = (
                    record.scan,
                    record.category.value,
                    "" if record.question is None else str(record.question),
                    record.expected,
                    record.actual,
                    record.status,
                    f"{record.confidence:.2f}",
                )
                for column, text in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(text))
        finally:
            table.setUpdatesEnabled(True)
        table.itemDoubleClicked.connect(
            lambda item: self._request_row_scan(table, item.row())
        )
        layout.addWidget(table)

        hint = QLabel("Double-click a row to open that scan in the scan list.")
        layout.addWidget(hint)
        return page

    def _build_failures_tab(self) -> QWidget:
        """The scans worth looking at, one per line."""
        page = QWidget()
        layout = QVBoxLayout(page)

        self.failures_list = QListWidget()
        self.failures_list.setObjectName("benchmarkFailingScansList")
        for scan in self._report.failing_scans:
            count = len(self._report.errors_for(scan))
            self.failures_list.addItem(f"{scan}  ({count} disagreement(s))")
        self.failures_list.itemDoubleClicked.connect(
            lambda item: self.scan_requested.emit(item.text().split("  ")[0])
        )
        layout.addWidget(self.failures_list)

        hint = QLabel("Double-click a scan to select it in the scan list and see its overlay.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return page

    def _build_buttons(self) -> QWidget:
        """Open the report folder, review the first failure, or close."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)

        self.open_report_button = QPushButton("Open Report Folder")
        self.open_report_button.setObjectName("openReportFolderButton")
        self.open_report_button.setEnabled(self._report_dir is not None)
        self.open_report_button.clicked.connect(self._open_report_folder)
        layout.addWidget(self.open_report_button)

        self.review_button = QPushButton("Review First Failure")
        self.review_button.setObjectName("reviewFirstFailureButton")
        self.review_button.setEnabled(bool(self._report.failing_scans))
        self.review_button.clicked.connect(self._review_first)
        layout.addWidget(self.review_button)

        layout.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("closeBenchmarkButton")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
        return row

    # ------------------------------------------------------------------
    # Behaviour
    # ------------------------------------------------------------------
    def _request_row_scan(self, table: QTableWidget, row: int) -> None:
        """Ask for the scan named in one row of the error table."""
        item = table.item(row, 0)
        if item is not None:
            self.scan_requested.emit(item.text())

    def _open_report_folder(self) -> None:
        """Show the written report in the system file manager."""
        if self._report_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._report_dir)))

    def _review_first(self) -> None:
        """Jump straight to the first scan that disagreed."""
        scans = self._report.failing_scans
        if scans:
            self.scan_requested.emit(scans[0])


def _rate_text(correct: int, total: int, rate: float) -> str:
    """Render one metric as ``correct/total (rate)``, or a dash.

    A dash when nothing was checked. ``0/0 (0.0000)`` reads as a total failure
    when it means "this dataset contained no double marks", and a reader
    scanning a column of rates should not have to check each denominator to
    know which zeros are real.
    """
    if total <= 0:
        return f"{format_count(correct)}/0  (not measured)"
    return f"{format_count(correct)}/{format_count(total)}  ({rate:.4f})"


__all__ = ["CATEGORY_COLUMNS", "ERROR_COLUMNS", "MAX_ERROR_ROWS", "BenchmarkResultsDialog"]
