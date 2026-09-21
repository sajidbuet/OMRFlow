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
from omr_scanner.services import (
    batch_store,
    reconciliation_store,
    report_store,
    set_attendance,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from omr_scanner.domain.reporting import ReadinessReport
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.reporting.excel import LayoutSettings
    from omr_scanner.services import ProjectDatabase, ProjectSession
    from omr_scanner.services.report_store import SetOverview, StoredTemplateAssociation
    from omr_scanner.services.report_template import ReportColumnMapping

_LOGGER = logging.getLogger(__name__)

SET_COLUMNS: tuple[str, ...] = (
    "Set", "Description", "Attendance / template", "Candidates",
    "Scripts", "Scored", "Answer key", "Status",
)
"""The §14 columns: which set, what it is for, where its layout comes from,
how many candidates it has, and whether it can be generated."""

NO_SETS_TEXT = (
    "This project defines no examination sets. Add them in "
    "<b>File &gt; Project Configuration...</b>, then assign each one an "
    "attendance workbook on the <b>Attendance</b> stage."
)

NO_ATTENDANCE_STATUS = "No attendance file"
NO_TEMPLATE_STATUS = "No result template"


@dataclass(frozen=True, slots=True)
class SetRow:
    """One row of the set table: a defined set, or a legacy evidence-only one.

    Carries the attributes the Phase 9 page already exposed
    (:attr:`set_code`, :attr:`template`, :attr:`report_readiness_label`,
    :attr:`key_revision`) so that everything reading a row keeps working,
    plus what Part 2 needs to generate each set independently.

    :attr:`set_id` is the persistent identity (§3): empty **only** for a set
    this project knows about from evidence alone - a code found on scored
    scripts or an answer key in a project that has never had sets defined.
    Such a row is generated the pre-Part-2 way; a defined set never is.
    """

    set_id: str
    set_code: str
    description: str = ""
    attendance_file: str = ""
    template_file: str = ""
    candidate_count: int = 0
    blocker: str = ""
    overview: SetOverview | None = None

    @property
    def is_defined(self) -> bool:
        """Whether this row is a set the project actually defines."""
        return bool(self.set_id)

    @property
    def template(self) -> StoredTemplateAssociation | None:
        """The associated result template, if any."""
        return self.overview.template if self.overview is not None else None

    @property
    def key_revision(self) -> int:
        """The verified answer key's revision, or ``0``."""
        return self.overview.key_revision if self.overview is not None else 0

    @property
    def has_verified_key(self) -> bool:
        """Whether this set has a verified answer key."""
        return self.overview.has_verified_key if self.overview is not None else False

    @property
    def script_count(self) -> int:
        """How many scripts were read as this set."""
        return self.overview.script_count if self.overview is not None else 0

    @property
    def scored_count(self) -> int:
        """How many of them were scored."""
        return self.overview.scored_count if self.overview is not None else 0

    @property
    def can_generate(self) -> bool:
        """Whether this set has everything a result needs."""
        if not self.is_defined:
            return self.template is not None
        return not self.blocker and self.template is not None

    @property
    def report_readiness_label(self) -> str:
        """A one-word status for the "Status" column.

        A defined set answers "what is missing" from its *own* attendance and
        template first: a set nobody has given a workbook to is not
        "Template required" in the abstract, it has no attendance file yet,
        and saying so is what §15 asks for.
        """
        if self.is_defined and self.blocker:
            return (
                NO_ATTENDANCE_STATUS
                if not self.attendance_file
                else NO_TEMPLATE_STATUS
            )
        if self.overview is not None:
            return self.overview.report_readiness_label
        return NO_TEMPLATE_STATUS


@dataclass
class ReportsPageState:
    """Everything the page is currently looking at."""

    session: ProjectSession | None = None
    template: OmrTemplate | None = None
    roster_id: int | None = None
    """The project's single *unscoped* roster, for legacy evidence-only sets.

    A defined set never reads this: its roster is resolved from its own
    ``set_id`` (§19). ``None`` in a project whose attendance is entirely
    per-set, which is the ordinary Part 2 state."""
    batch_id: str | None = None
    reviewer: str = ""
    project_name: str = ""
    exam_name: str = ""
    sets: list[SetRow] = field(default_factory=list)


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

        self.body.addWidget(self._build_exam_heading())
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
    def _build_exam_heading(self) -> QWidget:
        """The examination this project is reporting on, named once (§14).

        Distinct from the project's folder name, which is what the output
        files are named after - see
        :attr:`ReportsPageState.project_name`.
        """
        panel = QWidget()
        panel.setObjectName("reportsExamHeading")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 4)

        self.exam_name_label = QLabel("")
        self.exam_name_label.setObjectName("reportsExamNameLabel")
        self.exam_name_label.setWordWrap(True)
        self.exam_name_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.exam_name_label)
        return panel

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
        self.state.exam_name = session.exam_name if session is not None else ""
        if session is not None:
            # The project's *unscoped* roster, used only by a legacy
            # evidence-only set. A defined set resolves its own from its
            # set_id, and passing `None` here asks for exactly the unscoped
            # one rather than "whichever roster is active".
            roster = reconciliation_store.active_roster(session.database, None)
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
        """Re-read the project's sets and rebuild the table.

        Rows are **every defined set** (§14), each carrying its own
        attendance file, its own result template and its own candidate count,
        followed by any set code this project knows about from evidence alone
        that no defined set covers.

        That second group is what keeps a project made before sets existed
        working (§29): its results, answer keys and templates are still
        reachable and still generate exactly as they did. A project that has
        defined its sets sees the first group and, normally, nothing else.
        """
        database = self.database
        selected = self.selected_set_code()

        if database is None:
            self.state.sets = []
        else:
            self.state.sets = self._build_rows(database)
        self._rebuild_table()
        self._restore_selection(selected)
        self._refresh_exam_heading()

    def _build_rows(self, database: ProjectDatabase) -> list[SetRow]:
        """Assemble one row per defined set, then per uncovered legacy code."""
        overviews: dict[str, SetOverview] = {}
        if self.state.roster_id is not None and self.state.batch_id is not None:
            overviews = {
                item.set_code: item
                for item in report_store.set_overview(
                    database, self.state.roster_id, self.state.batch_id
                )
            }

        rows: list[SetRow] = []
        covered: set[str] = set()
        for status in set_attendance.attendance_overview(database):
            exam_set = status.exam_set
            covered.add(exam_set.code)
            overview = overviews.get(exam_set.code)
            if overview is None and self.state.batch_id is not None:
                overview = self._overview_for_defined_set(database, status)
            rows.append(
                SetRow(
                    set_id=exam_set.set_id,
                    set_code=exam_set.code,
                    description=exam_set.description,
                    attendance_file=status.attendance_file,
                    template_file=status.template_file,
                    candidate_count=status.candidate_count,
                    blocker=self._blocker_for(status),
                    overview=overview,
                )
            )

        for code, overview in sorted(overviews.items()):
            if code in covered:
                continue
            rows.append(SetRow(set_id="", set_code=code, overview=overview))
        return rows

    def _overview_for_defined_set(
        self, database: ProjectDatabase, status: set_attendance.SetAttendanceStatus
    ) -> SetOverview | None:
        """Counts for a defined set, read against **its own** roster.

        The project-wide overview is computed from the unscoped roster and so
        says nothing about a set whose candidates live in its own. Reading it
        again per set is the only way those counts can be about the right
        candidates (§19).
        """
        if status.roster is None or self.state.batch_id is None:
            return None
        for item in report_store.set_overview(
            database, status.roster.roster_id, self.state.batch_id
        ):
            if item.set_code == status.exam_set.code:
                return item
        return None

    def _blocker_for(self, status: set_attendance.SetAttendanceStatus) -> str:
        """Why this defined set cannot be generated yet, or ``""``.

        Deliberately the service's own wording - the same sentence the
        generation refusal carries - so the table and the failure an operator
        eventually sees cannot disagree (§15).
        """
        if not status.has_attendance:
            return (
                "No attendance/template workbook has been assigned to "
                f"Set {status.exam_set.code}."
            )
        if status.association is None:
            return (
                f"Set {status.exam_set.code} has no result template. "
                f"{status.template_blocker}"
            ).strip()
        return ""

    def _refresh_exam_heading(self) -> None:
        if not self.state.exam_name:
            self.exam_name_label.setText("")
            return
        defined = sum(1 for row in self.state.sets if row.is_defined)
        suffix = (
            f" · {defined} set(s) defined"
            if defined
            else " · no sets defined - see File &gt; Project Configuration..."
        )
        self.exam_name_label.setText(
            f"<b>Exam:</b> {self.state.exam_name}{suffix}"
        )

    def _rebuild_table(self) -> None:
        self.set_table.blockSignals(True)
        self.set_table.setRowCount(len(self.state.sets))
        for row, item in enumerate(self.state.sets):
            key_status = (
                f"Verified (rev {item.key_revision})"
                if item.has_verified_key
                else "Not verified"
            )
            source = item.template_file or (
                item.template.sheet_name if item.template else "Not selected"
            )
            values = (
                item.set_code,
                item.description,
                source,
                str(item.candidate_count) if item.is_defined else "-",
                str(item.script_count),
                str(item.scored_count),
                key_status,
                item.report_readiness_label,
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column == 0:
                    # The row's identity travels with it: every action reads
                    # `set_id` from here rather than inferring the set from a
                    # row position or the displayed code (§3).
                    cell.setData(Qt.ItemDataRole.UserRole, item.set_id)
                if item.blocker:
                    cell.setToolTip(item.blocker)
                self.set_table.setItem(row, column, cell)
        self.set_table.blockSignals(False)

    def _restore_selection(self, set_code: str | None) -> None:
        """Reselect a set by code, which is unique across every row.

        A defined set's code is unique by construction (Part 1), and a legacy
        row is only ever added for a code no defined set already covers - so
        no two rows can share one.
        """
        if set_code is not None:
            for row, item in enumerate(self.state.sets):
                if item.set_code == set_code:
                    self.set_table.selectRow(row)
                    self._show_detail(item)
                    return
        self._show_detail(None)

    def selected_overview(self) -> SetRow | None:
        """The set the table has selected, if any."""
        row = self.set_table.currentRow()
        if 0 <= row < len(self.state.sets):
            return self.state.sets[row]
        return None

    def selected_set_code(self) -> str | None:
        """The set code currently selected, if any."""
        overview = self.selected_overview()
        return overview.set_code if overview else None

    def selected_set_id(self) -> str | None:
        """The selected row's persistent set id, or ``None``.

        ``""`` for a legacy evidence-only row, which is a meaningful value -
        it says "this row names a set the project does not define" - so the
        absence of a selection is ``None`` rather than an empty string.
        """
        overview = self.selected_overview()
        return overview.set_id if overview is not None else None

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
        # Bind the template to the defined set, not only to its code, so a
        # later correction to the code cannot orphan it (§3). A legacy
        # evidence-only row has no set to bind to and passes ``None``.
        row = next(
            (item for item in self.state.sets if item.set_code == set_code), None
        )
        try:
            report_store.associate_template(
                database, set_code, path, mapping,
                sheet_name=sheet_name, updated_by=self.state.reviewer,
                set_id=row.set_id if row is not None and row.is_defined else None,
                source_kind="manual",
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
        row = self.selected_overview()
        set_code = row.set_code if row is not None else None
        if row is not None and row.blocker:
            QMessageBox.warning(self, f"Cannot export Set {set_code}", row.blocker)
            return
        report = self._readiness_for(row, for_final_export=True)
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
        self, row: SetRow | None, *, for_final_export: bool
    ) -> ReadinessReport | None:
        """Readiness for one row, read against **that row's** roster.

        A defined set is checked against its own attendance list; only a
        legacy evidence-only row falls back to the project's unscoped roster,
        because that is the only roster it has.
        """
        database = self.database
        if row is None or database is None or self.state.batch_id is None:
            return None
        if self.state.template is None:
            return None

        roster_id = self.state.roster_id
        if row.is_defined:
            try:
                roster_id = report_store.resolve_set_sources(database, row.set_id).roster_id
            except report_store.SetGenerationRefusedError:
                return None
        if roster_id is None:
            return None
        return report_store.check_readiness(
            database, roster_id, self.state.batch_id, self.state.template,
            row.set_code, for_final_export=for_final_export,
        )

    def _show_detail(self, overview: SetRow | None) -> None:
        self.issues_table.setRowCount(0)
        if overview is None:
            self.detail_label.setText(
                NO_SETS_TEXT
                if self.database is not None and not self.state.sets
                else "Select a set to see its readiness."
            )
            self._update_enabled()
            return

        heading = f"<b>Set {overview.set_code}</b> · {overview.report_readiness_label}"
        if overview.description:
            heading += f"<br>{overview.description}"
        if overview.attendance_file:
            heading += f"<br><i>Attendance/template:</i> {overview.attendance_file}"
        self.detail_label.setText(heading)

        if overview.blocker:
            # §15: the reason is the whole detail for a set that cannot run.
            self.issues_table.setRowCount(1)
            self.issues_table.setItem(0, 0, QTableWidgetItem(overview.blocker))
            self._update_enabled()
            return

        report = self._readiness_for(overview, for_final_export=True)
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
        if database is None or self.state.batch_id is None or self.state.template is None:
            return False
        if not jobs:
            return False
        # A run needs the project's unscoped roster only if it contains a
        # legacy job; a run made entirely of defined sets resolves each set's
        # roster for itself.
        if self.state.roster_id is None and any(not job.set_id for job in jobs):
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
                # §15: the refusal already names the set and what is missing,
                # so show it rather than a generic "not ready".
                reason = outcome.warnings[0] if outcome.warnings else "not ready"
                lines.append(
                    f"<span style='color:#a4262c'>Set {outcome.set_code}: "
                    f"blocked - {reason}</span>"
                )
            elif outcome.status == "cancelled":
                lines.append(f"Set {outcome.set_code}: cancelled")
            else:
                lines.append(
                    f"<span style='color:#a4262c'>Set {outcome.set_code}: failed</span>"
                )
        return "<br>".join(lines)

    def _jobs_for(self, row: SetRow, kinds: tuple[str, ...]) -> list[ReportJob]:
        """One job per kind, carrying the row's own set identity (§13)."""
        return [ReportJob(row.set_code, kind, set_id=row.set_id) for kind in kinds]

    def preview_selected(self) -> bool:
        """Generate a non-blocking draft of the selected set."""
        row = self.selected_overview()
        if row is None:
            return False
        return self._run_jobs(self._jobs_for(row, ("xlsx",)), final=False)

    def generate_selected_xlsx(self) -> bool:
        """Generate the selected set's final XLSX report."""
        row = self.selected_overview()
        if row is None:
            return False
        return self._run_jobs(self._jobs_for(row, ("xlsx",)), final=True)

    def generate_selected_pdf(self) -> bool:
        """Generate both Rollwise and Meritwise PDFs for the selected set."""
        row = self.selected_overview()
        if row is None:
            return False
        return self._run_jobs(
            self._jobs_for(row, ("pdf_rollwise", "pdf_meritwise")), final=True
        )

    def generate_all_sets(self) -> bool:
        """Generate the final XLSX report for every set, each independently.

        One workbook per set (§13). A set that cannot be generated still gets
        a job, so that the run's summary names it and says why, rather than
        quietly producing fewer files than there are sets.
        """
        jobs = [
            job for row in self.state.sets for job in self._jobs_for(row, ("xlsx",))
        ]
        return self._run_jobs(jobs, final=True)

    # ------------------------------------------------------------------
    # Enablement and lifetime
    # ------------------------------------------------------------------
    def _update_enabled(self) -> None:
        database = self.database
        row = self.selected_overview()
        base = (
            database is not None
            and self.state.batch_id is not None
            and self.state.template is not None
        )
        # A defined set needs no project-wide roster; a legacy row does,
        # because the unscoped roster is the only one it has.
        has_roster = self.state.roster_id is not None
        ready = base and (
            has_roster if row is None or not row.is_defined else True
        )
        selected = row is not None
        self.select_template_button.setEnabled(base and selected)
        self.validate_button.setEnabled(ready and selected)
        self.layout_button.setEnabled(database is not None)
        self.preview_button.setEnabled(ready and selected)
        self.generate_xlsx_button.setEnabled(ready and selected)
        self.generate_pdf_button.setEnabled(ready and selected)
        self.generate_all_button.setEnabled(
            base and bool(self.state.sets)
            and (has_roster or all(item.is_defined for item in self.state.sets))
        )

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
