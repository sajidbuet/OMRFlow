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

import html
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
    QTabWidget,
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
from omr_scanner.gui.results.dashboard import DashboardInputs, ResultsDashboard
from omr_scanner.gui.results.policy_dialog import ScoringPolicyDialog
from omr_scanner.gui.results.worker import ScoringResult, ScoringWorker
from omr_scanner.gui.theme import Spacing
from omr_scanner.gui.ui_scale import scale_layout
from omr_scanner.services import (
    batch_store,
    project_sets,
    report_store,
    result_analytics,
    scan_lifecycle,
    scan_sessions,
    scoring,
    scoring_store,
    session_population,
    session_scope,
)
from omr_scanner.services.answer_key import AnswerKeyError, QuestionPlan, plan_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.scoring import ResultCounts
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import ProjectDatabase, ProjectSession
    from omr_scanner.services.scoring_store import StoredResult, UnreconciledRoster

_LOGGER = logging.getLogger(__name__)


def plan_or_none(template: OmrTemplate | None) -> QuestionPlan | None:
    """The template's question plan, or ``None`` when it has no usable one."""
    if template is None:
        return None
    try:
        return plan_for(template)
    except AnswerKeyError:
        return None


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
    roster_ids: tuple[int, ...] = ()
    """Every active candidate list: one per defined set, then any unscoped one.

    See :func:`~omr_scanner.services.scoring_store.scoring_rosters`. Re-read on
    every refresh rather than held from when the project opened, so a list
    imported on the Attendance stage during this session is scored without
    closing and reopening the project."""
    batch_id: str | None = None
    """The selected session's downstream **store** - derived from
    :attr:`scan_session_id`, never chosen on its own (0.1.1 phase 4)."""
    scan_session_id: str | None = None
    """The scan session being marked: the authoritative selection (default:
    the active session)."""
    reviewer: str = ""
    unreconciled: tuple[UnreconciledRoster, ...] = ()
    """Active lists never reconciled against :attr:`batch_id`, as of the last
    refresh. Scoring them would find no candidates, so they stop the run."""
    results: list[StoredResult] = field(default_factory=list)
    every_result: tuple[StoredResult, ...] = ()
    """The whole batch, unfiltered, from the same read as :attr:`results` -
    what the summary counts and the Dashboard tab analyses."""
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

        # The existing stage, unchanged, is the first tab; the analytics
        # dashboard is a second, read-only one beside it. Nothing on the
        # Results tab depends on the dashboard.
        self.tabs = QTabWidget()
        self.tabs.setObjectName("resultsTabs")
        results_tab = QWidget()
        results_tab.setObjectName("resultsMainTab")
        results_layout = QVBoxLayout(results_tab)
        scale_layout(results_layout, margins=(Spacing.SM, Spacing.SM, Spacing.SM, Spacing.SM))
        results_layout.addWidget(self._build_policy_bar())
        results_layout.addWidget(self._build_summary())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("resultsSplitter")
        splitter.addWidget(self._build_table_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        results_layout.addWidget(splitter, stretch=1)
        self.tabs.addTab(results_tab, load_icon("list-checks"), "Results")

        self.dashboard = ResultsDashboard()
        self.dashboard.set_provider(self.dashboard_inputs)
        self.tabs.addTab(self.dashboard, load_icon("chart-column"), "Dashboard")
        self.tabs.setTabToolTip(
            1,
            "Statistics and charts for the scored results: marks, questions, sets "
            "and reliability. Read-only.",
        )
        self.body.addWidget(self.tabs, stretch=1)

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
        # Its own handler, never `score_batch` directly: `clicked` emits a
        # bool, which a slot with an optional first parameter receives - and
        # `score_batch(False)` once reached the scorer as the candidate list
        # (`set(False)`: "'bool' object is not iterable").
        self.score_button.clicked.connect(self._on_calculate_clicked)
        layout.addWidget(self.score_button)
        return box

    def _build_summary(self) -> QWidget:
        """The counts an operator reads before trusting the marks."""
        box = QGroupBox("Results summary")
        box.setObjectName("resultsSummaryBox")
        layout = QVBoxLayout(box)

        # Which batch is being marked. Without it, a Results stage holding a
        # different batch from the Attendance stage looks exactly like one
        # with nothing to mark.
        self.batch_label = QLabel("")
        self.batch_label.setObjectName("resultsBatchLabel")
        self.batch_label.setWordWrap(True)
        self.batch_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.batch_label)

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
        self.state.roster_ids = ()
        self.state.batch_id = None
        self.state.scan_session_id = None
        if session is not None:
            self.state.roster_ids = scoring_store.scoring_rosters(session.database)
            # The active scan session - never "the most recently updated
            # batch" (0.1.1 phase 4: the session is the selection).
            self._select_session(scan_sessions.downstream_session_id(session.database))
            # The project's own template, so Results can mark a reopened
            # project without the Scan stage having been visited. A template
            # the Scan stage loads later still arrives through set_template.
            from omr_scanner.services.project_template import load_project_template

            found = load_project_template(session.project).template
            if found is not None:
                self.state.template = found
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

    def set_session(self, scan_session_id: str | None) -> None:
        """Mark one scan session (the authoritative selection)."""
        self._select_session(scan_session_id)
        self.refresh_table()
        self._update_enabled()

    def set_batch(self, batch_id: str) -> None:
        """Compatibility: mark the scan session ``batch_id`` belongs to."""
        database = self.database
        self.set_session(
            session_scope.session_of(database, batch_id) if database is not None else None
        )

    def _select_session(self, scan_session_id: str | None) -> None:
        database = self.database
        self.state.scan_session_id = scan_session_id
        self.state.batch_id = None
        if database is not None and scan_session_id is not None:
            try:
                self.state.batch_id = session_scope.store(database, scan_session_id)
            except session_scope.SessionScopeError:
                self.state.scan_session_id = None

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
        if database is not None:
            self.state.roster_ids = scoring_store.scoring_rosters(database)
        if database is None or not self.state.roster_ids:
            return ("No project or candidate list is open.",)
        if self.state.batch_id is None:
            return ("No batch has been processed in this project yet.",)
        if self.state.template is None:
            return (
                "This project does not yet have an active OMR template. Create or "
                "load one on the Template stage.",
            )

        batch_id = self.state.batch_id
        unreconciled = scoring_store.unreconciled_rosters(
            database, self.state.roster_ids, batch_id
        )
        issues: list[str] = [item.describe(batch_id) for item in unreconciled]
        skipped = {item.roster_id for item in unreconciled}
        missing: set[str] = set()
        for roster_id in self.state.roster_ids:
            if roster_id in skipped:
                continue
            try:
                data = scoring_store.gather_inputs(
                    database, roster_id, self.state.batch_id, self.state.template
                )
            except OMRScannerError as exc:
                return (exc.user_message or str(exc),)
            needed = {
                answers.set_code
                for answers in data.answers.values()
                if answers.set_code
            }
            # `data.keys` compares set codes canonically; a set difference on
            # its spellings would not.
            missing |= {code for code in needed if code not in data.keys}
            for entry in data.entries:
                outcome = scoring.score_candidate(
                    scoring_store.inputs_for_candidate(data, entry)
                )
                if outcome.status is ResultStatus.BLOCKED:
                    issues.append(
                        f"Candidate {entry.candidate_id or '(unknown)'}: "
                        f"{outcome.describe_blocks()}"
                    )
        # Say *why* each set has no usable key - missing, draft only, or a key
        # for a different paper - rather than "invalid".
        overview = {
            item.set_code: item
            for item in scoring_store.key_overview(
                database, sorted(missing), plan_or_none(self.state.template)
            )
        }
        reasons = {
            scoring_store.SetKeyState.MISSING: "no key has been entered",
            scoring_store.SetKeyState.DRAFT: "a key is saved but not verified",
            scoring_store.SetKeyState.STALE: "its key does not fit the active template",
            scoring_store.SetKeyState.VERIFIED: "",
        }
        return (
            *(
                f"Set {code} has no verified answer key"
                + (
                    f" ({reasons[overview[code].state]})."
                    if code in overview and reasons[overview[code].state]
                    else "."
                )
                for code in sorted(missing)
            ),
            *issues,
        )

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
    def _on_calculate_clicked(self, _checked: bool = False) -> None:
        """*Calculate Results*: the whole batch, every roster."""
        self.score_batch(candidates=None)

    def score_batch(self, *, candidates: tuple[str, ...] | None = None) -> bool:
        """Mark the batch - or the named candidates - off the GUI thread.

        ``candidates`` is keyword-only so that no signal argument (a button's
        ``checked`` bool) can ever be taken for a candidate list.
        """
        database = self.database
        if database is not None:
            self.state.roster_ids = scoring_store.scoring_rosters(database)
        if (
            database is None
            or not self.state.roster_ids
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
        if candidates is None:
            # A whole-batch run over a list never reconciled against this
            # batch would "finish" having marked nobody. Refused, with the
            # sets named, rather than reported as an empty success.
            unreconciled = scoring_store.unreconciled_rosters(
                database, self.state.roster_ids, self.state.batch_id
            )
            if unreconciled:
                QMessageBox.warning(
                    self,
                    "Not ready to score",
                    "\n\n".join(item.describe(self.state.batch_id) for item in unreconciled),
                )
                self.refresh_table()
                return False

        self.score_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        if self._worker is not None and self._worker.isRunning():
            self._worker.ready.disconnect()
        worker = ScoringWorker(
            database,
            self.state.roster_ids,
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

    def rescore_selected(self, _checked: bool = False) -> bool:
        """Recompute one candidate from their stored inputs.

        Connected to *Recalculate This Candidate*; the button's ``checked``
        argument is accepted and ignored, never passed on.
        """
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

        if database is not None:
            self.state.roster_ids = scoring_store.scoring_rosters(database)
        if database is None or not self.state.roster_ids or self.state.batch_id is None:
            self.state.results = []
            self.state.every_result = ()
            self.state.counts = None
            self.state.unreconciled = ()
        else:
            self.state.unreconciled = scoring_store.unreconciled_rosters(
                database, self.state.roster_ids, self.state.batch_id
            )
            _, statuses, attention = _FILTERS[max(0, self.filter_combo.currentIndex())]
            text = self.search_box.text().strip().casefold()
            scan_session_id = self.state.scan_session_id or session_scope.session_of(
                database, self.state.batch_id
            )
            # The session's rows, once - the Dashboard analyses exactly these
            # (`state.every_result`), never a second read.
            everything = [
                item
                for roster_id in self.state.roster_ids
                for item in session_scope.results(
                    database, roster_id, scan_session_id, self.state.template
                )
            ]
            # The summary describes the whole batch, not the filtered view, so
            # it is taken before filtering and from this same read.
            self.state.counts = scoring_store.summarise(everything)
            self.state.every_result = tuple(everything)
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
        # Only marks the dashboard out of date; it reads its inputs when it is
        # next shown (or now, if it is showing), and recomputes only if the
        # results actually changed.
        self.dashboard.invalidate()

    # ------------------------------------------------------------------
    # The Dashboard tab
    # ------------------------------------------------------------------
    def dashboard_inputs(self) -> DashboardInputs | None:
        """What the Dashboard tab analyses: this page's own unfiltered results.

        The same rows the Results table was built from, so the dashboard's
        counts reconcile with the summary above the table.
        """
        database = self.database
        if database is None or not self.state.every_result:
            return None
        plan = plan_or_none(self.state.template)
        policy = scoring_store.active_policy(database).policy
        set_codes = tuple(item.code for item in project_sets.list_sets(database))
        notes = {
            item.set_code: item.describe()
            for item in scoring_store.key_overview(database, set_codes, plan)
            if not item.is_ready
        }
        notices: list[str] = [
            html.escape(item.describe(self.state.batch_id or ""))
            for item in self.state.unreconciled
        ]
        incomplete = self._incomplete_warning().removeprefix("<br>")
        if incomplete:
            notices.append(incomplete)
        return DashboardInputs(
            database=database,
            results=self.state.every_result,
            set_codes=set_codes,
            labels=plan.labels if plan is not None else (),
            maximum_possible=(
                result_analytics.maximum_mark(plan.question_count, policy)
                if plan is not None
                else None
            ),
            notices=tuple(notices),
            set_notes=notes,
        )

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

    def _refresh_batch_label(self) -> None:
        """Name the batch being marked."""
        database = self.database
        batch_id = self.state.batch_id
        summary = (
            batch_store.load_summary(database, batch_id)
            if database is not None and batch_id is not None
            else None
        )
        if summary is None or batch_id is None or database is None:
            self.batch_label.setText("")
            self.batch_label.setToolTip("")
            return
        scope = scan_sessions.describe_downstream(database, batch_id)
        population = session_population.population(database, batch_id)
        if len(population.batch_ids) > 1:
            # 0.1.1 phase 4: the session is marked as one population.
            text = f"Marking the <b>scan session</b> · {session_population.describe(population)}"
        else:
            text = (
                f"Marking <b>batch {batch_id[:8]}</b> · {summary.total} scan(s) from "
                f"{html.escape(summary.source_folder or '(files)')}"
            )
        session_scope = report_store.session_scope(database, batch_id)
        if session_scope.provisional:
            # While the session is open its results are provisional (§8.2).
            stale = ""
            if session_scope.scan_session_id is not None:
                info = scan_sessions.get_scan_session(database, session_scope.scan_session_id)
                if info is not None and info.final_outputs_stale_since is not None:
                    # Reopened (revised phase 8): what was exported while it
                    # was closed no longer stands - the session's own record.
                    stale = " It was reopened: final exports made while closed are stale."
            text = (
                "<span style='color:#8a5a00'><b>PROVISIONAL</b> - scan session "
                f"'{html.escape(session_scope.name)}' is open; results may change.{stale}"
                "</span><br>" + text
            )
        elif session_scope.scan_session_id is not None:
            text = (
                f"Scan session '{html.escape(session_scope.name)}' · <b>closed</b> - "
                "final scope<br>" + text
            )
        self.batch_label.setText(text)
        self.batch_label.setToolTip(scope)

    def _refresh_summary(self) -> None:
        """Show the batch counts, from the read that filled the table."""
        self._refresh_batch_label()
        counts = self.state.counts
        if counts is None:
            self.summary_label.setText("Nothing scored yet.")
            return
        if self.state.unreconciled and self.state.batch_id is not None:
            batch_id = self.state.batch_id
            lines = "<br>".join(
                html.escape(item.describe(batch_id)) for item in self.state.unreconciled
            )
            self.summary_label.setText(
                "<span style='color:#a4262c'><b>Not ready to score.</b></span><br>"
                + lines
                + ("<br>" + self._counts_text(counts) if counts.registered else "")
                + self._incomplete_warning()
            )
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
        self.summary_label.setText(
            self._counts_text(counts) + verdict + self._incomplete_warning()
        )

    @staticmethod
    def _counts_text(counts: ResultCounts) -> str:
        """The count line, and the per-set line when there is one, each ending in a break."""
        by_set = " · ".join(
            f"Set {code}: {total}" for code, total in sorted(counts.by_set.items())
        )
        return (
            f"Candidates <b>{counts.registered}</b> · "
            f"Scored <b>{counts.scored}</b> · "
            f"Absent <b>{counts.absent}</b> · "
            f"Cannot be scored <b>{counts.blocked}</b> · "
            f"Need recalculating <b>{counts.stale}</b><br>"
            + (f"{by_set}<br>" if by_set else "")
        )

    def _incomplete_warning(self) -> str:
        """Say, when it is true, that these results may be incomplete.

        Results stay viewable while rejected sheets await their rescan - a
        rescan may take days - but they must not look finished. Final export
        of such a set asks for an explicit *Export incomplete results* on the
        Reports stage.
        """
        session = self.state.session
        if session is None or not self.state.batch_id:
            return ""
        cases = scan_lifecycle.count_cases(session.database, self.state.batch_id)
        text = ""
        if cases.outstanding:
            text += (
                f"<br><span style='color:#a4262c'><b>Results may be incomplete: "
                f"{cases.outstanding} rejected sheet(s) still awaiting rescan.</b></span>"
            )
        if cases.deferred:
            # A deferred sheet is an open decision, not a reviewed one: the
            # results must not read as a completely reviewed dataset.
            text += (
                f"<br><span style='color:#a4262c'><b>{cases.deferred} sheet(s) "
                "are deferred and will not be included in scoring or results."
                "</b></span> Restore or reject them on the <b>Attendance</b> stage."
            )
        return text

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
            and bool(self.state.roster_ids)
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
        self.dashboard.shutdown()

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
