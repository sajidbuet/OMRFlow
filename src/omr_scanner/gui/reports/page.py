"""Result Management: per-set templates, readiness, and report generation.

    ┌───────────────────────────────────────────────────────────────┐
    │ Select/Change Template · Validate · Layout... · Preview        │
    │ Generate XLSX · Generate PDF (Rollwise/Meritwise) · Generate All│
    ├───────────────────────────────┬────────────────────────────────┤
    │ set table                     │ readiness / last-generated      │
    │ (Set, candidates, key,        │ detail for the selected set     │
    │  template, status)            │                                  │
    └───────────────────────────────┴────────────────────────────────┘

Three rules the page is arranged around, matching Phase 8's Results page:

* **Nothing is generated on the GUI thread.** Every generation runs in
  :class:`~omr_scanner.gui.reports.worker.ReportGenerationWorker`.
* **A blocked set is a row with a reason**, not an absence from the list -
  the same "nothing that cannot be scored is hidden" rule Phase 8 follows.
* **Generating one set is independent of every other.** "Generate All Sets"
  reports each set's own outcome; one failure never hides another's success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.reports.layout_dialog import ReportLayoutDialog
from omr_scanner.gui.reports.template_dialog import TemplateMappingDialog
from omr_scanner.gui.reports.worker import ReportGenerationWorker, ReportJob, ReportRunResult
from omr_scanner.services import batch_store, reconciliation_store, report_store

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.domain.reporting import ReadinessReport
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.reporting.excel import LayoutSettings
    from omr_scanner.services import ProjectDatabase, ProjectSession
    from omr_scanner.services.report_store import SetOverview
    from omr_scanner.services.report_template import ReportColumnMapping

_LOGGER = logging.getLogger(__name__)

SET_COLUMNS: tuple[str, ...] = (
    "Set", "Scripts", "Scored", "Blocked", "Answer key", "Template", "Status",
)


@dataclass
class ReportsPageState:
    """Everything the page is currently looking at."""

    session: ProjectSession | None = None
    template: OmrTemplate | None = None
    roster_id: int | None = None
    batch_id: str | None = None
    reviewer: str = ""
    project_name: str = ""
    sets: list[SetOverview] = field(default_factory=list)


class ReportsPage(WorkflowPage):
    """Associate templates, check readiness, and generate result reports."""

    reports_generated = Signal()
    """Emitted whenever a generation run finishes."""

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setObjectName("reportsPage")
        self.state = ReportsPageState()
        self._worker: ReportGenerationWorker | None = None
        self._workers: list[ReportGenerationWorker] = []
        self._closing = False

        self.body.addWidget(self._build_command_bar())
        self.body.addWidget(self._build_progress_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_set_table())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.body.addWidget(splitter, stretch=1)

        self._update_enabled()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_command_bar(self) -> QWidget:
        box = QGroupBox("Result Management")
        box.setObjectName("reportsCommandBar")
        layout = QHBoxLayout(box)

        self.select_template_button = QPushButton(
            load_icon("folder-open"), "Select Template..."
        )
        self.select_template_button.setObjectName("selectTemplateButton")
        self.select_template_button.setToolTip(
            "Choose the result/absentee workbook for the selected set."
        )
        self.select_template_button.clicked.connect(self.prompt_select_template)
        layout.addWidget(self.select_template_button)

        self.validate_button = QPushButton(load_icon("list-checks"), "Validate")
        self.validate_button.setObjectName("validateTemplateButton")
        self.validate_button.clicked.connect(self.validate_selected)
        layout.addWidget(self.validate_button)

        self.layout_button = QPushButton(load_icon("pencil"), "Report Layout...")
        self.layout_button.setObjectName("reportLayoutButton")
        self.layout_button.clicked.connect(self.configure_layout)
        layout.addWidget(self.layout_button)

        self.preview_button = QPushButton(load_icon("scan"), "Preview")
        self.preview_button.setObjectName("previewResultButton")
        self.preview_button.setToolTip(
            "Generate a draft. Every readiness issue is shown as a warning "
            "rather than blocking - a preview may look incomplete."
        )
        self.preview_button.clicked.connect(self.preview_selected)
        layout.addWidget(self.preview_button)

        self.generate_xlsx_button = QPushButton(load_icon("save"), "Generate XLSX")
        self.generate_xlsx_button.setObjectName("generateXlsxButton")
        self.generate_xlsx_button.clicked.connect(self.generate_selected_xlsx)
        layout.addWidget(self.generate_xlsx_button)

        self.generate_pdf_button = QPushButton(load_icon("save"), "Generate PDF")
        self.generate_pdf_button.setObjectName("generatePdfButton")
        self.generate_pdf_button.clicked.connect(self.generate_selected_pdf)
        layout.addWidget(self.generate_pdf_button)

        self.generate_all_button = QPushButton(load_icon("circle-check"), "Generate All Sets")
        self.generate_all_button.setObjectName("generateAllSetsButton")
        self.generate_all_button.clicked.connect(self.generate_all_sets)
        layout.addWidget(self.generate_all_button)
        return box

    def _build_progress_bar(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("reportsProgressPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.progress = QProgressBar()
        self.progress.setObjectName("reportsProgressBar")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.generation_status_label = QLabel("")
        self.generation_status_label.setObjectName("generationStatusLabel")
        self.generation_status_label.setWordWrap(True)
        self.generation_status_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.generation_status_label)
        return panel

    def _build_set_table(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("reportsSetTablePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.set_table = QTableWidget(0, len(SET_COLUMNS))
        self.set_table.setObjectName("reportsSetTable")
        self.set_table.setHorizontalHeaderLabels(list(SET_COLUMNS))
        self.set_table.verticalHeader().setVisible(False)
        self.set_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.set_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.set_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.set_table.setAlternatingRowColors(True)
        self.set_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.set_table.horizontalHeader().setStretchLastSection(True)
        self.set_table.itemSelectionChanged.connect(self._update_enabled)
        layout.addWidget(self.set_table, stretch=1)
        return panel

    def _build_detail_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("reportsDetailPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.detail_label = QLabel("Select a set to see its readiness.")
        self.detail_label.setObjectName("reportsDetailLabel")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.detail_label)

        self.issues_table = QTableWidget(0, 1)
        self.issues_table.setObjectName("reportsIssuesTable")
        self.issues_table.setHorizontalHeaderLabels(["Issue"])
        self.issues_table.verticalHeader().setVisible(False)
        self.issues_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.issues_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.issues_table, stretch=1)
        return panel

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt an opened project, or clear everything when one closes."""
        self.state.session = session
        self.state.sets = []
        self.state.roster_id = None
        self.state.batch_id = None
        self.state.project_name = session.name if session is not None else ""
        if session is not None:
            roster = reconciliation_store.active_roster(session.database)
            self.state.roster_id = roster.roster_id if roster else None
            batches = batch_store.list_batches(session.database, limit=1)
            self.state.batch_id = batches[0].batch_id if batches else None
        self.refresh_table()
        self._update_enabled()

    @property
    def database(self) -> ProjectDatabase | None:
        """The open project's database, or ``None``."""
        return self.state.session.database if self.state.session is not None else None

    def set_reviewer(self, name: str) -> None:
        """Adopt the configured reviewer name - who generation is attributed to."""
        self.state.reviewer = name.strip()

    def set_template(self, template: OmrTemplate | None) -> None:
        """Adopt the template the batch was read with."""
        self.state.template = template
        self.refresh_table()
        self._update_enabled()

    def set_batch(self, batch_id: str) -> None:
        """Report on a particular batch rather than the most recent one."""
        self.state.batch_id = batch_id
        self.refresh_table()
        self._update_enabled()

    # ------------------------------------------------------------------
    # The set table
    # ------------------------------------------------------------------
    def refresh_table(self) -> None:
        """Re-read the set overview and rebuild the table."""
        database = self.database
        selected = self.selected_set_code()

        if database is None or self.state.roster_id is None or self.state.batch_id is None:
            self.state.sets = []
        else:
            self.state.sets = list(
                report_store.set_overview(database, self.state.roster_id, self.state.batch_id)
            )
        self._rebuild_table()
        self._restore_selection(selected)

    def _rebuild_table(self) -> None:
        self.set_table.blockSignals(True)
        self.set_table.setRowCount(len(self.state.sets))
        for row, overview in enumerate(self.state.sets):
            key_status = (
                f"Verified (rev {overview.key_revision})"
                if overview.has_verified_key
                else "Not verified"
            )
            template_label = (
                overview.template.sheet_name if overview.template else "Not selected"
            )
            values = (
                overview.set_code, str(overview.script_count), str(overview.scored_count),
                str(overview.blocked_count), key_status, template_label,
                overview.report_readiness_label,
            )
            for column, text in enumerate(values):
                self.set_table.setItem(row, column, QTableWidgetItem(text))
        self.set_table.blockSignals(False)

    def _restore_selection(self, set_code: str | None) -> None:
        if set_code is not None:
            for row, overview in enumerate(self.state.sets):
                if overview.set_code == set_code:
                    self.set_table.selectRow(row)
                    self._show_detail(overview)
                    return
        self._show_detail(None)

    def selected_overview(self) -> SetOverview | None:
        """The set the table has selected, if any."""
        row = self.set_table.currentRow()
        if 0 <= row < len(self.state.sets):
            return self.state.sets[row]
        return None

    def selected_set_code(self) -> str | None:
        """The set code currently selected, if any."""
        overview = self.selected_overview()
        return overview.set_code if overview else None

    # ------------------------------------------------------------------
    # Template association
    # ------------------------------------------------------------------
    def prompt_select_template(self) -> bool:
        """Ask for a workbook, then let the operator confirm its mapping.

        Owns two modal dialogs (the native file picker and
        :class:`~omr_scanner.gui.reports.template_dialog.TemplateMappingDialog`)
        and contains no logic of its own - the same split every other
        command on this window follows, so that :meth:`associate_template`
        is what a test drives instead of a dialog neither Qt's offscreen
        platform nor a headless test runner can click through.
        """
        set_code = self.selected_set_code()
        database = self.database
        if set_code is None or database is None:
            return False
        chosen, _ = QFileDialog.getOpenFileName(
            self, f"Select Result Template for Set {set_code}", "", "Excel Workbooks (*.xlsx)"
        )
        if not chosen:
            return False
        from pathlib import Path

        dialog = TemplateMappingDialog(Path(chosen), set_code, self)
        if dialog.exec() != TemplateMappingDialog.DialogCode.Accepted:
            return False
        mapping = dialog.current_mapping()
        if mapping is None:
            return False
        return self.associate_template(set_code, Path(chosen), mapping, dialog.sheet_name)

    def associate_template(
        self, set_code: str, path: Path, mapping: ReportColumnMapping, sheet_name: str
    ) -> bool:
        """Store ``path``/``mapping`` as ``set_code``'s result template.

        Contains no dialog and no modal loop, which is precisely why it is
        split from :meth:`prompt_select_template`: a test supplies an
        already-decided mapping (from
        :func:`~omr_scanner.services.report_template.preview_template` or a
        driven, never-``exec``'d
        :class:`~omr_scanner.gui.reports.template_dialog.TemplateMappingDialog`)
        instead of one a person chose by hand.
        """
        database = self.database
        if database is None:
            return False
        try:
            report_store.associate_template(
                database, set_code, path, mapping,
                sheet_name=sheet_name, updated_by=self.state.reviewer,
            )
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Template not associated", exc.user_message or str(exc)
            )
            return False
        self.refresh_table()
        return True

    def validate_selected(self) -> None:
        """Show every readiness issue for the selected set."""
        set_code = self.selected_set_code()
        report = self._readiness_for(set_code, for_final_export=True)
        if report is None:
            return
        if report.is_ready:
            QMessageBox.information(
                self, "Ready to export", f"Set {set_code} has no outstanding issues."
            )
            return
        shown = report.describe()[:20]
        more = "" if len(report.issues) <= 20 else f"\n...and {len(report.issues) - 20} more."
        QMessageBox.warning(
            self, f"Cannot export Set {set_code}",
            f"{len(report.issues)} issue(s) require attention:\n\n• "
            + "\n• ".join(shown) + more,
        )

    def configure_layout(self) -> bool:
        """Edit the project (or per-set) layout configuration.

        Owns the modal :class:`~omr_scanner.gui.reports.layout_dialog.
        ReportLayoutDialog`; :meth:`apply_layout_settings` is what a test
        drives instead.
        """
        database = self.database
        if database is None:
            return False
        set_code = self.selected_set_code() or ""
        stored = report_store.get_layout_config(database, set_code)
        dialog = ReportLayoutDialog(stored.settings, self)
        if dialog.exec() != ReportLayoutDialog.DialogCode.Accepted:
            return False
        return self.apply_layout_settings(set_code, dialog.settings())

    def apply_layout_settings(self, set_code: str, settings: LayoutSettings) -> bool:
        """Persist ``settings`` for ``set_code`` (``""`` for the project default)."""
        database = self.database
        if database is None:
            return False
        report_store.save_layout_config(
            database, set_code, settings, updated_by=self.state.reviewer
        )
        return True

    # ------------------------------------------------------------------
    # Readiness
    # ------------------------------------------------------------------
    def _readiness_for(
        self, set_code: str | None, *, for_final_export: bool
    ) -> ReadinessReport | None:
        database = self.database
        if (
            set_code is None or database is None or self.state.roster_id is None
            or self.state.batch_id is None or self.state.template is None
        ):
            return None
        return report_store.check_readiness(
            database, self.state.roster_id, self.state.batch_id, self.state.template,
            set_code, for_final_export=for_final_export,
        )

    def _show_detail(self, overview: SetOverview | None) -> None:
        self.issues_table.setRowCount(0)
        if overview is None:
            self.detail_label.setText("Select a set to see its readiness.")
            self._update_enabled()
            return
        self.detail_label.setText(
            f"<b>Set {overview.set_code}</b> · {overview.report_readiness_label}"
        )
        report = self._readiness_for(overview.set_code, for_final_export=True)
        if report is not None:
            self.issues_table.setRowCount(len(report.issues))
            for row, issue in enumerate(report.issues):
                item = QTableWidgetItem(issue.message)
                if not issue.blocking:
                    item.setToolTip("Warning only - does not block export.")
                self.issues_table.setItem(row, 0, item)
        self._update_enabled()

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------
    def _output_dir(self) -> Path:
        assert self.state.session is not None
        return self.state.session.project.layout.exports_dir

    def _run_jobs(self, jobs: list[ReportJob], *, final: bool) -> bool:
        database = self.database
        if (
            database is None or self.state.roster_id is None or self.state.batch_id is None
            or self.state.template is None or not jobs
        ):
            return False
        from omr_scanner.reporting.pdf import detect_exporter

        self.generate_xlsx_button.setEnabled(False)
        self.generate_pdf_button.setEnabled(False)
        self.generate_all_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, max(len(jobs), 1))
        self.progress.setValue(0)

        if self._worker is not None and self._worker.isRunning():
            self._worker.ready.disconnect()
        worker = ReportGenerationWorker(
            database, self.state.roster_id, self.state.batch_id, self.state.template, jobs,
            project_name=self.state.project_name, output_dir=self._output_dir(),
            pdf_exporter=detect_exporter(), computed_by=self.state.reviewer, final=final,
            parent=self,
        )
        worker.ready.connect(self._on_generated)
        worker.progressed.connect(self._on_progress)
        self._worker = worker
        self._workers = [item for item in self._workers if item.isRunning()]
        self._workers.append(worker)
        worker.start()
        return True

    def _on_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)

    def _on_generated(self, result: ReportRunResult) -> None:
        """Adopt a finished run. Runs on the GUI thread.

        Deliberately never opens a modal dialog here. This slot fires
        whenever the worker happens to finish - which, for "Generate All
        Sets" run against a large project, is routine, and a `QMessageBox`
        raised from an automatic callback blocks the whole interface (and
        any automated driver, GUI test or smoke script) until a human clicks
        it, whether or not anyone is watching at that moment. The same
        problem, and the same fix, as Phase 8's ``ResultsPage._on_scored``.
        A genuine failure (an unexpected exception inside the worker, as
        opposed to an ordinary blocked/failed job) is still worth an
        operator's attention, so it is shown too - as status text, not a box
        that has to be dismissed before the page is usable again.
        """
        if self._closing:
            return
        self.progress.setVisible(False)
        self.generate_xlsx_button.setEnabled(True)
        self.generate_pdf_button.setEnabled(True)
        self.generate_all_button.setEnabled(True)
        self.refresh_table()
        self.generation_status_label.setText(self._generation_summary_text(result))
        if result.ok:
            self.reports_generated.emit()

    def _generation_summary_text(self, result: ReportRunResult) -> str:
        """One line per job, coloured to flag anything that needs attention."""
        if not result.ok:
            return (
                f"<span style='color:#a4262c'><b>Report generation failed:</b> "
                f"{result.error}</span>"
            )
        lines = []
        for outcome in result.outcomes:
            if outcome.ok:
                lines.append(f"Set {outcome.set_code} ({outcome.report_type}): done")
            elif outcome.status == "blocked":
                lines.append(
                    f"<span style='color:#a4262c'>Set {outcome.set_code}: "
                    "blocked - not ready</span>"
                )
            elif outcome.status == "cancelled":
                lines.append(f"Set {outcome.set_code}: cancelled")
            else:
                lines.append(
                    f"<span style='color:#a4262c'>Set {outcome.set_code}: failed</span>"
                )
        return "<br>".join(lines)

    def preview_selected(self) -> bool:
        """Generate a non-blocking draft of the selected set."""
        set_code = self.selected_set_code()
        if set_code is None:
            return False
        return self._run_jobs([ReportJob(set_code, "xlsx")], final=False)

    def generate_selected_xlsx(self) -> bool:
        """Generate the selected set's final XLSX report."""
        set_code = self.selected_set_code()
        if set_code is None:
            return False
        return self._run_jobs([ReportJob(set_code, "xlsx")], final=True)

    def generate_selected_pdf(self) -> bool:
        """Generate both Rollwise and Meritwise PDFs for the selected set."""
        set_code = self.selected_set_code()
        if set_code is None:
            return False
        return self._run_jobs(
            [ReportJob(set_code, "pdf_rollwise"), ReportJob(set_code, "pdf_meritwise")],
            final=True,
        )

    def generate_all_sets(self) -> bool:
        """Generate the final XLSX report for every set in the project."""
        jobs = [ReportJob(overview.set_code, "xlsx") for overview in self.state.sets]
        return self._run_jobs(jobs, final=True)

    # ------------------------------------------------------------------
    # Enablement and lifetime
    # ------------------------------------------------------------------
    def _update_enabled(self) -> None:
        database = self.database
        ready = (
            database is not None and self.state.roster_id is not None
            and self.state.batch_id is not None and self.state.template is not None
        )
        selected = self.selected_overview() is not None
        self.select_template_button.setEnabled(ready and selected)
        self.validate_button.setEnabled(ready and selected)
        self.layout_button.setEnabled(database is not None)
        self.preview_button.setEnabled(ready and selected)
        self.generate_xlsx_button.setEnabled(ready and selected)
        self.generate_pdf_button.setEnabled(ready and selected)
        self.generate_all_button.setEnabled(ready and bool(self.state.sets))

    def shutdown(self) -> None:
        """Wait for every generation run this page started."""
        self._closing = True
        workers = self._workers
        self._worker = None
        self._workers = []
        for worker in workers:
            if worker.isRunning():
                worker.cancel()
                worker.wait(10_000)

    def closeEvent(self, event: object) -> None:
        """Join the workers before the page goes away."""
        self.shutdown()
        super().closeEvent(event)  # type: ignore[arg-type]
