"""Mark a reconciled batch, and explain every mark it produced.

    ┌───────────────────────────────────────────────────────────────┐
    │ policy summary · Scoring Configuration… · Check · Calculate   │
    ├───────────────────────────────────────────────────────────────┤
    │ registered / scored / absent / cannot score / stale           │
    ├──────────────────────────────┬────────────────────────────────┤
    │ results table                │ candidate detail:              │
    │ (filter: all / scored /      │  answer string, key revision,  │
    │  absent / blocked / stale)   │  policy revision, question     │
    │                              │  table with every contribution │
    └──────────────────────────────┴────────────────────────────────┘

Three rules the page is arranged around:

* **A stale number is never shown as current.** A result whose key, policy,
  answers or reconciliation has changed since it was computed keeps its mark -
  it is a true record of what the old inputs produced - but says so, loudly,
  and the batch summary counts it.
* **Recalculating is a recomputation.** The button runs the scorer again over
  the stored inputs. Nothing here adjusts an existing mark by a delta.
* **Nothing that cannot be scored is hidden.** A blocked candidate is a row
  with a reason, not an absence from the list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from fractions import Fraction
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.scoring import (
    BLANK,
    MULTIPLE,
    QuestionOutcome,
    ResultStatus,
    format_mark,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.results.policy_dialog import ScoringPolicyDialog
from omr_scanner.gui.results.worker import ScoringResult, ScoringWorker
from omr_scanner.services import (
    batch_store,
    reconciliation_store,
    scoring,
    scoring_store,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.scoring import ResultCounts
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import ProjectDatabase, ProjectSession
    from omr_scanner.services.scoring_store import StoredResult

_LOGGER = logging.getLogger(__name__)

RESULT_COLUMNS: tuple[str, ...] = (
    "Candidate",
    "Name",
    "Set",
    "Status",
    "Score",
    "Correct",
    "Wrong",
    "Blank",
    "Multiple",
    "Key rev.",
    "Needs attention",
)

DETAIL_COLUMNS: tuple[str, ...] = ("Q", "Machine", "Effective", "Key", "Evaluation", "Mark")

_FILTERS: tuple[tuple[str, tuple[ResultStatus, ...], bool], ...] = (
    ("Everything", (), False),
    ("Needs attention", (), True),
    ("Scored", (ResultStatus.SCORED,), False),
    ("Absent", (ResultStatus.ABSENT,), False),
    ("Cannot be scored", (ResultStatus.BLOCKED,), False),
)


@dataclass
class ResultsPageState:
    """Everything the page is currently looking at."""

    session: ProjectSession | None = None
    template: OmrTemplate | None = None
    roster_id: int | None = None
    batch_id: str | None = None
    reviewer: str = ""
    results: list[StoredResult] = field(default_factory=list)
    counts: ResultCounts | None = None
    """The whole batch's summary, from the same read that filled :attr:`results`.

    Kept rather than asked for again: counting a batch means listing it, and
    listing a ten-thousand-candidate batch twice per refresh - once for the
    table, once for the line above it - is the batch's work done twice for one
    label.
    """


class ResultsPage(WorkflowPage):
    """Configure marking, score a batch, and explain the marks."""

    scored = Signal()
    """Emitted whenever the results table has been rebuilt."""

    policy_changed = Signal(int)
    """Emitted with the new policy revision after a configuration change."""

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.state = ResultsPageState()
        self._worker: ScoringWorker | None = None
        self._workers: list[ScoringWorker] = []
        self._closing = False

        self.body.addWidget(self._build_policy_bar())
        self.body.addWidget(self._build_summary())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("resultsSplitter")
        splitter.addWidget(self._build_table_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.body.addWidget(splitter, stretch=1)

        self._update_enabled()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_policy_bar(self) -> QWidget:
        """The rules in force, and the three commands that act on them."""
        box = QGroupBox("Scoring configuration")
        box.setObjectName("scoringPolicyBox")
        layout = QHBoxLayout(box)

        self.policy_label = QLabel("No project open.")
        self.policy_label.setObjectName("scoringPolicyLabel")
        self.policy_label.setWordWrap(True)
        self.policy_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.policy_label, stretch=1)

        self.configure_button = QPushButton(load_icon("pencil"), "Scoring Configuration...")
        self.configure_button.setObjectName("configureScoringButton")
        self.configure_button.setToolTip(
            "Change how the paper is marked. Changing any rule makes existing "
            "results stale until they are recalculated."
        )
        self.configure_button.clicked.connect(self.configure_policy)
        layout.addWidget(self.configure_button)

        self.check_button = QPushButton(load_icon("list-checks"), "Check Before Scoring")
        self.check_button.setObjectName("checkBeforeScoringButton")
        self.check_button.setToolTip(
            "Show the rules and the verified keys, and list everything that "
            "would stop a candidate being marked."
        )
        self.check_button.clicked.connect(self.show_preflight)
        layout.addWidget(self.check_button)

        self.score_button = QPushButton(load_icon("circle-check"), "Calculate Results")
        self.score_button.setObjectName("calculateResultsButton")
        self.score_button.setToolTip(
            "Mark every candidate from the stored answers, the verified key "
            "for their set and the current configuration."
        )
        self.score_button.clicked.connect(self.score_batch)
        layout.addWidget(self.score_button)
        return box

    def _build_summary(self) -> QWidget:
        """The counts an operator reads before trusting the marks."""
        box = QGroupBox("Results summary")
        box.setObjectName("resultsSummaryBox")
        layout = QVBoxLayout(box)

        self.summary_label = QLabel("Nothing scored yet.")
        self.summary_label.setObjectName("resultsSummaryLabel")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)

        self.progress = QProgressBar()
        self.progress.setObjectName("scoringProgressBar")
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        return box

    def _build_table_panel(self) -> QWidget:
        """The filter row and the results table."""
        panel = QWidget()
        panel.setObjectName("resultsTablePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        filters = QHBoxLayout()
        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("resultsFilterCombo")
        for label, _, _ in _FILTERS:
            self.filter_combo.addItem(label)
        self.filter_combo.currentIndexChanged.connect(self.refresh_table)
        filters.addWidget(self.filter_combo, stretch=1)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("resultsSearchBox")
        self.search_box.setPlaceholderText("Search by candidate ID or name...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.refresh_table)
        filters.addWidget(self.search_box, stretch=1)
        layout.addLayout(filters)

        self.table = QTableWidget(0, len(RESULT_COLUMNS))
        self.table.setObjectName("resultsTable")
        self.table.setHorizontalHeaderLabels(list(RESULT_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.table, stretch=1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("resultsCountLabel")
        layout.addWidget(self.count_label)
        return panel

    def _build_detail_panel(self) -> QWidget:
        """One candidate's mark, and how it was arrived at."""
        panel = QWidget()
        panel.setObjectName("resultsDetailPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.detail_label = QLabel("Select a candidate to see their marks.")
        self.detail_label.setObjectName("resultsDetailLabel")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.detail_label)

        self.detail_table = QTableWidget(0, len(DETAIL_COLUMNS))
        self.detail_table.setObjectName("resultDetailTable")
        self.detail_table.setHorizontalHeaderLabels(list(DETAIL_COLUMNS))
        self.detail_table.verticalHeader().setVisible(False)
        self.detail_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.detail_table.setAlternatingRowColors(True)
        self.detail_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.detail_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.detail_table, stretch=1)

        buttons = QHBoxLayout()
        self.review_button = QPushButton(load_icon("list-checks"), "Review Sheet...")
        self.review_button.setObjectName("reviewAnswersButton")
        self.review_button.setToolTip(
            "Open this candidate's sheet on the Resolve stage, where the "
            "student ID and set code are settled. A correction there never "
            "overwrites what the machine read, and makes this result stale "
            "until it is recalculated."
        )
        self.review_button.clicked.connect(self.review_selected)
        buttons.addWidget(self.review_button)

        self.rescore_button = QPushButton(load_icon("rotate-ccw"), "Recalculate This Candidate")
        self.rescore_button.setObjectName("recalculateCandidateButton")
        self.rescore_button.setToolTip(
            "Run the scorer again over this candidate's stored answers, key "
            "revision and the current configuration."
        )
        self.rescore_button.clicked.connect(self.rescore_selected)
        buttons.addWidget(self.rescore_button)
        layout.addLayout(buttons)
        return panel

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt an opened project, or clear everything when one closes."""
        self.state.session = session
        self.state.results = []
        self.state.roster_id = None
        self.state.batch_id = None
        if session is not None:
            roster = reconciliation_store.active_roster(session.database)
            self.state.roster_id = roster.roster_id if roster else None
            batches = batch_store.list_batches(session.database, limit=1)
            self.state.batch_id = batches[0].batch_id if batches else None
        self._refresh_policy_label()
        self.refresh_table()
        self._update_enabled()

    @property
    def database(self) -> ProjectDatabase | None:
        """The open project's database, or ``None``."""
        return self.state.session.database if self.state.session is not None else None

    def set_reviewer(self, name: str) -> None:
        """Adopt the configured reviewer name."""
        self.state.reviewer = name.strip()

    def set_template(self, template: OmrTemplate | None) -> None:
        """Adopt the template the batch was read with."""
        self.state.template = template
        self.refresh_table()
        self._update_enabled()

    def set_batch(self, batch_id: str) -> None:
        """Score a particular batch rather than the most recent one."""
        self.state.batch_id = batch_id
        self.refresh_table()
        self._update_enabled()

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------
    def configure_policy(self) -> bool:
        """Edit the marking rules, and warn if results go stale."""
        database = self.database
        if database is None:
            return False
        current = scoring_store.active_policy(database)
        dialog = ScoringPolicyDialog(current.policy, self)
        if dialog.exec() != ScoringPolicyDialog.DialogCode.Accepted:
            return False
        return self.apply_policy(dialog.policy())

    def apply_policy(self, policy: object) -> bool:
        """Store a policy, creating a revision only if a rule changed."""
        database = self.database
        if database is None:
            return False
        try:
            stored = scoring_store.save_policy(
                database, policy, created_by=self.state.reviewer  # type: ignore[arg-type]
            )
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Configuration not saved", exc.user_message or str(exc)
            )
            return False
        self._refresh_policy_label()
        self.refresh_table()
        self.policy_changed.emit(stored.revision)
        return True

    def _refresh_policy_label(self) -> None:
        """Show the rules in force."""
        database = self.database
        if database is None:
            self.policy_label.setText("No project open.")
            return
        stored = scoring_store.active_policy(database)
        keys = scoring_store.verified_keys(database)
        verified = (
            ", ".join(
                f"{code} rev {item.revision}" for code, item in sorted(keys.items())
            )
            or "none"
        )
        self.policy_label.setText(
            f"<b>Revision {stored.revision}</b> · "
            + " · ".join(stored.policy.describe())
            + f"<br>Verified answer keys: {verified}"
        )

    # ------------------------------------------------------------------
    # Pre-scoring check
    # ------------------------------------------------------------------
    def preflight(self) -> tuple[str, ...]:
        """Everything that would stop a candidate being marked.

        Returned as lines so they can be shown together - an operator who has
        to discover blocking issues one dialog at a time will fix them one at a
        time, and the batch will fail five times.
        """
        database = self.database
        if database is None or self.state.roster_id is None:
            return ("No project or candidate list is open.",)
        if self.state.batch_id is None:
            return ("No batch has been processed in this project yet.",)
        if self.state.template is None:
            return ("No template is loaded. Load it on the Scan stage.",)

        try:
            data = scoring_store.gather_inputs(
                database, self.state.roster_id, self.state.batch_id, self.state.template
            )
        except OMRScannerError as exc:
            return (exc.user_message or str(exc),)

        issues: list[str] = []
        needed = {
            answers.set_code
            for answers in data.answers.values()
            if answers.set_code
        }
        for code in sorted(needed - set(data.keys)):
            issues.append(f"Set {code} has no verified answer key.")
        for entry in data.entries:
            outcome = scoring.score_candidate(
                scoring_store.inputs_for_candidate(data, entry)
            )
            if outcome.status is ResultStatus.BLOCKED:
                issues.append(
                    f"Candidate {entry.candidate_id or '(unknown)'}: "
                    f"{outcome.describe_blocks()}"
                )
        return tuple(issues)

    def show_preflight(self) -> None:
        """Show the rules and everything blocking, in one dialog."""
        database = self.database
        issues = self.preflight()
        rules = (
            "\n".join(scoring_store.active_policy(database).policy.describe())
            if database is not None
            else ""
        )
        if not issues:
            QMessageBox.information(
                self,
                "Ready to score",
                f"{rules}\n\nEvery candidate can be marked.",
            )
            return
        shown = issues[:12]
        more = "" if len(issues) <= 12 else f"\n...and {len(issues) - 12} more."
        QMessageBox.warning(
            self,
            "Some candidates cannot be scored",
            f"{rules}\n\n{len(issues)} issue(s) require attention:\n\n• "
            + "\n• ".join(shown)
            + more
            + "\n\nScoring will still run: these candidates are recorded as "
            "'cannot be scored', with the reason, rather than being skipped.",
        )

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------
    def score_batch(self, candidates: tuple[str, ...] | None = None) -> bool:
        """Mark the batch - or the named candidates - off the GUI thread."""
        database = self.database
        if (
            database is None
            or self.state.roster_id is None
            or self.state.batch_id is None
            or self.state.template is None
        ):
            QMessageBox.information(
                self,
                "Not ready to score",
                "Scoring needs an open project with a candidate list, a "
                "processed batch and the template it was read with.",
            )
            return False

        self.score_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        if self._worker is not None and self._worker.isRunning():
            self._worker.ready.disconnect()
        worker = ScoringWorker(
            database,
            self.state.roster_id,
            self.state.batch_id,
            self.state.template,
            computed_by=self.state.reviewer,
            candidates=candidates,
            parent=self,
        )
        worker.ready.connect(self._on_scored)
        worker.progressed.connect(self._on_progress)
        self._worker = worker
        self._workers = [item for item in self._workers if item.isRunning()]
        self._workers.append(worker)
        worker.start()
        return True

    def _on_progress(self, done: int, total: int) -> None:
        """Show how far a run has got."""
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(done)

    def _on_scored(self, result: ScoringResult) -> None:
        """Adopt a finished run. Runs on the GUI thread.

        A run that finishes *after* the stage has been asked to shut down is
        dropped: its signal is delivered while the page is being torn down, and
        redrawing a table whose widgets are on their way out is how a clean
        close turns into a crash.
        """
        if self._closing:
            return
        self.progress.setVisible(False)
        self.score_button.setEnabled(True)
        if result.error:
            QMessageBox.warning(self, "Scoring failed", result.error)
            return
        self.refresh_table()
        if result.cancelled:
            # A cancelled run deliberately writes nothing, so the marks on
            # screen are the previous ones. Saying so is the difference between
            # a coherent state and a coherent state that looks, to the person
            # who stopped it, like a finished run.
            #
            # Said in the summary rather than in a modal box: this arrives on
            # the GUI thread whenever the run happens to end, including while
            # the stage is being torn down, and a dialog opened then blocks a
            # window that is halfway closed.
            self.summary_label.setText(
                self.summary_label.text()
                + "<br><span style='color:#a4262c'><b>Scoring was cancelled, so "
                "nothing was written.</b> The marks above are from the previous "
                "run.</span>"
            )
            return
        self.scored.emit()

    def rescore_selected(self) -> bool:
        """Recompute one candidate from their stored inputs."""
        selected = self.selected_result()
        if selected is None:
            return False
        return self.score_batch(candidates=(selected.candidate_id,))

    def review_selected(self) -> None:
        """Ask the window to open this candidate's sheet on the Resolve stage."""
        selected = self.selected_result()
        if selected is None or selected.scan_id is None:
            QMessageBox.information(
                self,
                "No sheet to review",
                "This candidate has no script attributed to them, so there is "
                "nothing to review. Deal with it on the Attendance stage.",
            )
            return
        window = self.window()
        opened = False
        if hasattr(window, "review_batch") and self.state.batch_id:
            opened = bool(window.review_batch(self.state.batch_id))
        if not opened:
            QMessageBox.information(
                self,
                "Open the Resolve stage",
                "Open the Resolve stage to review this candidate's student ID "
                "and set code. A correction there is recorded beside the "
                "machine's reading, and makes this result stale until it is "
                "recalculated.",
            )

    # ------------------------------------------------------------------
    # The table
    # ------------------------------------------------------------------
    def refresh_table(self) -> None:
        """Re-read the results and rebuild the table."""
        database = self.database
        selected = self.selected_result()
        keep = selected.candidate_id if selected else None

        if database is None or self.state.roster_id is None or self.state.batch_id is None:
            self.state.results = []
            self.state.counts = None
        else:
            _, statuses, attention = _FILTERS[max(0, self.filter_combo.currentIndex())]
            text = self.search_box.text().strip().casefold()
            everything = scoring_store.list_results(
                database, self.state.roster_id, self.state.batch_id, self.state.template
            )
            # The summary describes the whole batch, not the filtered view, so
            # it is taken before filtering and from this same read.
            self.state.counts = scoring_store.summarise(everything)
            self.state.results = [
                item
                for item in everything
                if (not statuses or item.status in statuses)
                and (not attention or item.is_stale or item.status is ResultStatus.BLOCKED)
                and (
                    not text
                    or text in item.candidate_id.casefold()
                    or text in item.display_name.casefold()
                )
            ]
        self._rebuild_table()
        self._restore_selection(keep)
        self._refresh_summary()
        # The policy bar lists the verified keys, and verifying one happens on
        # another stage - so it is refreshed whenever the table is, not only
        # when the policy itself changes.
        self._refresh_policy_label()

    def _rebuild_table(self) -> None:
        """Fill the table from the current results."""
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(self.state.results))
        for row, item in enumerate(self.state.results):
            attention = (
                item.describe_stale()
                if item.is_stale
                else (item.describe_blocks() if item.status is ResultStatus.BLOCKED else "")
            )
            values = (
                item.candidate_id or "(unknown)",
                item.display_name,
                item.set_code,
                item.status.label,
                _mark_text(item.final_score) if item.has_mark else "-",
                str(item.correct_count) if item.has_mark else "",
                str(item.incorrect_count) if item.has_mark else "",
                str(item.blank_count) if item.has_mark else "",
                str(item.multiple_count) if item.has_mark else "",
                str(item.answer_key_revision) if item.answer_key_revision else "",
                attention,
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column == len(values) - 1 and attention:
                    cell.setToolTip(attention)
                self.table.setItem(row, column, cell)
        self.table.blockSignals(False)
        outstanding = sum(
            1
            for item in self.state.results
            if item.is_stale or item.status is ResultStatus.BLOCKED
        )
        self.count_label.setText(
            f"{len(self.state.results)} row(s) shown"
            + (f" · {outstanding} needing attention" if outstanding else "")
        )

    def _restore_selection(self, candidate_id: str | None) -> None:
        """Re-select by identity, never by row index."""
        if candidate_id is not None:
            for row, item in enumerate(self.state.results):
                if item.candidate_id == candidate_id:
                    self.table.selectRow(row)
                    return
        self._show_result(None)

    def selected_result(self) -> StoredResult | None:
        """The result the table has selected, if any."""
        row = self.table.currentRow()
        if 0 <= row < len(self.state.results):
            return self.state.results[row]
        return None

    def _on_selection_changed(self) -> None:
        """Show whatever the table now has selected."""
        self._show_result(self.selected_result())

    def _refresh_summary(self) -> None:
        """Show the batch counts, from the read that filled the table."""
        counts = self.state.counts
        if counts is None:
            self.summary_label.setText("Nothing scored yet.")
            return
        if not counts.registered:
            self.summary_label.setText(
                "Nothing scored yet. Reconcile the batch on the "
                "<b>Attendance</b> stage, verify an answer key, then "
                "<b>Calculate Results</b>."
            )
            return
        verdict = (
            "<span style='color:#1b7f3b'><b>Every candidate is accounted "
            "for.</b></span>"
            if counts.is_clear
            else (
                f"<span style='color:#a4262c'><b>{counts.blocked} cannot be "
                f"scored, {counts.stale} need recalculating.</b></span>"
            )
        )
        by_set = " · ".join(
            f"Set {code}: {total}" for code, total in sorted(counts.by_set.items())
        )
        self.summary_label.setText(
            f"Candidates <b>{counts.registered}</b> · "
            f"Scored <b>{counts.scored}</b> · "
            f"Absent <b>{counts.absent}</b> · "
            f"Cannot be scored <b>{counts.blocked}</b> · "
            f"Need recalculating <b>{counts.stale}</b><br>"
            + (f"{by_set}<br>" if by_set else "")
            + verdict
        )

    # ------------------------------------------------------------------
    # The detail panel
    # ------------------------------------------------------------------
    def _show_result(self, result: StoredResult | None) -> None:
        """Explain one candidate's mark question by question."""
        self.detail_table.setRowCount(0)
        if result is None:
            self.detail_label.setText("Select a candidate to see their marks.")
            self._update_enabled()
            return

        parts = [
            f"<b>{result.candidate_id}</b>"
            + (f" - {result.display_name}" if result.display_name else "")
            + f" · Set <b>{result.set_code or '(not read)'}</b> · "
            f"<b>{result.status.label}</b>"
        ]
        if result.has_mark:
            parts.append(
                f"<br>Total: <b>{_mark_text(result.final_score)}</b>"
                + (
                    f" (raw {format_mark(result.raw_score)}, raised to the "
                    "minimum)"
                    if result.clamped and result.raw_score is not None
                    else ""
                )
            )
            parts.append(f"<br>{result.describe_provenance()}")
            parts.append(f"<br>Answers: <code>{result.answer_string}</code>")
            if result.corrected_questions:
                parts.append(
                    "<br>Corrected on the Resolve stage: Q"
                    + ", Q".join(str(n) for n in sorted(result.corrected_questions))
                    + " - the machine's reading is kept."
                )
        if result.status is ResultStatus.BLOCKED:
            parts.append(f"<br><b>{result.describe_blocks()}</b>")
        if result.is_stale:
            parts.append(
                f"<br><span style='color:#a4262c'><b>{result.describe_stale()}."
                "</b> The mark below is what the earlier inputs produced.</span>"
            )
        self.detail_label.setText("".join(parts))

        database = self.database
        breakdown = (
            scoring_store.breakdown_for(database, result) if database else None
        )
        if breakdown is None:
            self._update_enabled()
            return

        self.detail_table.setRowCount(len(breakdown.questions))
        for row, item in enumerate(breakdown.questions):
            machine = _readable(
                result.machine_answer_string,
                item.number - result.first_question,
            )
            values = (
                str(item.number),
                machine,
                _readable(result.answer_string, item.number - result.first_question),
                item.key,
                item.outcome.label,
                item.display_mark,
            )
            for column, text in enumerate(values):
                cell = QTableWidgetItem(text)
                if column == 4 and item.outcome is QuestionOutcome.WRONG_QUESTION:
                    cell.setToolTip(
                        "Withdrawn for this set: full credit whatever was "
                        "marked, and never a deduction."
                    )
                if column == 1 and machine != values[2]:
                    cell.setToolTip(
                        "Recognition read this; a reviewer decided otherwise. "
                        "Both are kept."
                    )
                self.detail_table.setItem(row, column, cell)
        self._update_enabled()

    # ------------------------------------------------------------------
    # Enablement and lifetime
    # ------------------------------------------------------------------
    def _update_enabled(self) -> None:
        """Enable only what the current state allows."""
        database = self.database
        ready = (
            database is not None
            and self.state.roster_id is not None
            and self.state.batch_id is not None
            and self.state.template is not None
        )
        selected = self.selected_result()
        self.configure_button.setEnabled(database is not None)
        self.check_button.setEnabled(ready)
        self.score_button.setEnabled(ready)
        self.rescore_button.setEnabled(ready and selected is not None)
        self.review_button.setEnabled(
            selected is not None and selected.scan_id is not None
        )

    def shutdown(self) -> None:
        """Wait for every scoring run this page started."""
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


def _mark_text(value: Fraction | None) -> str:
    """Render a mark that may be absent.

    An absent candidate has no mark, not a mark of zero, so this shows a dash
    rather than inventing one.
    """
    return format_mark(value) if value is not None else "-"


def _readable(answers: str, offset: int) -> str:
    """Render one position of an answer string for a table cell."""
    if not 0 <= offset < len(answers):
        return ""
    character = answers[offset]
    if character == BLANK:
        return "(blank)"
    if character == MULTIPLE:
        return "(multiple)"
    return character
