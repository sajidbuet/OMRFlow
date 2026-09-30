"""The Results stage's *Dashboard* tab: the marked examination, analysed.

    ┌──────────────────────────────────────────────────────────────┐
    │ Analysis: [Overall Exam ▼]                   status · Refresh │
    │ notices (stale results, incomplete batch, excluded scripts)  │
    ├──────────────────────────────────────────────────────────────┤
    │ KPI cards: scripts · mean · median · SD · min · max · mode · │
    ├───────────────────────────────┬──────────────────────────────┤
    │ Marks distribution            │ Score summary (box plot)     │
    ├───────────────────────────────┴──────────────────────────────┤
    │ Set comparison                           (Overall Exam only) │
    ├──────────────────────────────────────────────────────────────┤
    │ Question-wise % correct · difficulty bands                   │
    │ Question discrimination                                      │
    │ Easiest questions            │ Hardest questions             │
    │ Correct / incorrect / multiple / blank by question           │
    │ Questions to review          │ Selected question (options)   │
    │ Reliability                                                  │
    └──────────────────────────────────────────────────────────────┘

Arrangement rules:

* **Read-only, and never in the way.** Nothing here writes, scores or
  changes a key. The Results tab does not depend on this one: a failure
  here is shown here, logged, and goes no further.
* **The same rows as the Results table.** The page hands over the list of
  stored results its own table was built from; every figure comes from
  :mod:`omr_scanner.services.result_analytics`, which consumes those rows.
* **Computed once, off the GUI thread.** Every scope is computed together by
  a worker and cached against a fingerprint of the results; switching scope
  is a lookup, and returning to the tab with nothing changed recomputes
  nothing.
* **It scrolls rather than squeezes.** Charts keep a readable height and the
  page scrolls vertically; two-column rows stack on a narrow window.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QBoxLayout,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.results.charts import (
    BoxPlotChart,
    HistogramBar,
    HistogramChart,
    OptionBar,
    OptionChart,
    QuestionBar,
    QuestionChart,
    format_value,
)
from omr_scanner.gui.theme import ChartColor, Color, Radius, Spacing
from omr_scanner.services import result_analytics as ra

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Sequence

    from PySide6.QtCore import QObject
    from PySide6.QtGui import QResizeEvent, QShowEvent

    from omr_scanner.services import ProjectDatabase
    from omr_scanner.services.scoring_store import StoredResult

_LOGGER = logging.getLogger(__name__)

NO_RESULTS_TEXT = "No scored results are available yet."

TOOLTIPS: dict[str, str] = {
    "count": "Scored scripts in this scope: one per candidate, after duplicates, "
    "rejected and superseded sheets have been dealt with. Absent candidates and "
    "candidates who cannot be scored are not included.",
    "mean": "The arithmetic mean of the final marks.",
    "median": "The middle mark: half the candidates scored at or below it.",
    "sd": "Sample standard deviation (n - 1): how widely the marks spread around "
    "the mean.",
    "min": "The lowest final mark.",
    "max": "The highest final mark.",
    "mode": "The most frequent mark. 'None' when every mark occurs once.",
    "iqr": "The middle half of the marks, from the 25th percentile (Q1) to the 75th "
    "(Q3), by linear interpolation.",
    "discrimination": "Corrected point-biserial correlation between getting this "
    "question right (1/0) and the number of the other questions answered correctly. "
    "Positive: stronger candidates did better on it. Near zero: it does not separate "
    "candidates. Negative: stronger candidates did worse - worth checking the key and "
    "the wording. A guide, not a verdict.",
    "kr20": "Kuder-Richardson 20: internal consistency of the 0/1 question scores, "
    "from 0 (none) towards 1. It depends on the cohort and the number of questions, "
    "and is shown for information, not as a pass/fail standard.",
    "sem": "Standard error of measurement = SD x sqrt(1 - KR-20), in questions "
    "correct: roughly how far an observed score may sit from the candidate's 'true' "
    "score on this paper.",
    "difficulty": "\n".join(ra.describe_bands()),
}


@dataclass(frozen=True, slots=True)
class DashboardInputs:
    """What the Results page hands the dashboard.

    Attributes:
        database: For reading the key and policy revisions results point at.
        results: Every stored result the Results table was built from.
        set_codes: The project's defined sets, in the operator's order.
        labels: The template's option labels.
        maximum_possible: The paper's full mark, or ``None`` if unknown.
        notices: Rich-text warnings to show above the figures.
        set_notes: Per set, why it may have no scored scripts (its key state).
    """

    database: ProjectDatabase
    results: tuple[StoredResult, ...]
    set_codes: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    maximum_possible: float | None = None
    notices: tuple[str, ...] = ()
    set_notes: dict[str, str] = field(default_factory=dict)

    def fingerprint(self) -> tuple[object, ...]:
        """Changes whenever anything the figures depend on changes."""
        return (
            tuple(
                (
                    item.result_id,
                    item.computed_at,
                    item.status,
                    item.is_stale,
                    item.set_code,
                    item.final_score,
                    item.answer_key_id,
                    item.policy_id,
                )
                for item in self.results
            ),
            self.set_codes,
            self.labels,
            self.maximum_possible,
        )


class AnalyticsWorker(QThread):
    """Regenerate every breakdown and compute every scope, off the GUI thread."""

    ready = Signal(int, object)
    """``(generation, ExamAnalytics | str)`` - a string is an error message."""

    def __init__(
        self, generation: int, inputs: DashboardInputs, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._generation = generation
        self._inputs = inputs

    def run(self) -> None:
        """Compute. Runs on the worker thread."""
        inputs = self._inputs
        try:
            keys, policies = ra.load_keys_and_policies(inputs.database, inputs.results)
            records = ra.build_records(inputs.results, keys, policies)
            analytics = ra.analyse(
                records,
                set_codes=inputs.set_codes,
                labels=inputs.labels,
                maximum_possible=inputs.maximum_possible,
            )
        except Exception as exc:
            # The traceback goes to the log; the message names only the type,
            # since an arbitrary exception's text may quote a candidate's data.
            _LOGGER.exception("Results analytics could not be computed")
            self.ready.emit(
                self._generation,
                f"The analysis could not be computed ({type(exc).__name__}). The Results "
                "tab is unaffected; details are in the application log.",
            )
            return
        self.ready.emit(self._generation, analytics)


# ----------------------------------------------------------------------
# Layout helpers
# ----------------------------------------------------------------------
class _ReflowRow(QWidget):
    """Two panels side by side, stacked when the row is narrower than ``breakpoint``."""

    def __init__(
        self, *widgets: QWidget, breakpoint: int = 860, stretch: tuple[int, ...] = ()
    ) -> None:
        super().__init__()
        self._breakpoint = breakpoint
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(Spacing.CARD_GAP)
        for index, widget in enumerate(widgets):
            self._layout.addWidget(widget, stretch[index] if index < len(stretch) else 1)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Stack below the breakpoint."""
        wanted = (
            QBoxLayout.Direction.LeftToRight
            if event.size().width() >= self._breakpoint
            else QBoxLayout.Direction.TopToBottom
        )
        if self._layout.direction() != wanted:
            self._layout.setDirection(wanted)
        super().resizeEvent(event)


class _KpiCard(QFrame):
    """One headline figure: a caption and a value."""

    def __init__(self, key: str, caption: str) -> None:
        super().__init__()
        self.setObjectName("dashboardKpiCard")
        self.setProperty("kpi", key)
        self.setToolTip(TOOLTIPS.get(key, ""))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        layout.setSpacing(Spacing.XXS)
        self.caption = QLabel(caption)
        self.caption.setObjectName("dashboardKpiCaption")
        self.value = QLabel("-")
        self.value.setObjectName(f"dashboardKpi_{key}")
        font = self.value.font()
        size = font.pointSizeF()
        if size > 0:
            font.setPointSizeF(size + 4)
        font.setBold(True)
        self.value.setFont(font)
        self.value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class _KpiGrid(QWidget):
    """The KPI cards, in as many columns as fit (two to eight)."""

    MIN_CARD_WIDTH = 132

    def __init__(self, cards: Sequence[_KpiCard]) -> None:
        super().__init__()
        self._cards = list(cards)
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(Spacing.SM)
        self._grid.setVerticalSpacing(Spacing.SM)
        self._arrange(len(self._cards))

    def _arrange(self, columns: int) -> None:
        if columns == self._columns:
            return
        self._columns = columns
        for card in self._cards:
            self._grid.removeWidget(card)
        for index, card in enumerate(self._cards):
            self._grid.addWidget(card, index // columns, index % columns)
        for column in range(len(self._cards)):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-flow the cards for the new width."""
        width = event.size().width()
        fits = max(2, min(len(self._cards), width // self.MIN_CARD_WIDTH))
        # Prefer an even split: 8 cards as 8, 4x2 or 2x4, never 5+3.
        for columns in (8, 4, 2):
            if columns <= fits:
                self._arrange(min(columns, len(self._cards)))
                break
        super().resizeEvent(event)


def _table(columns: Sequence[str], name: str, *, stretch_last: bool = True) -> QTableWidget:
    """A compact, read-only table."""
    table = QTableWidget(0, len(columns))
    table.setObjectName(name)
    table.setHorizontalHeaderLabels(list(columns))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(stretch_last)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return table


def _fit_rows(table: QTableWidget, rows: int, *, cap: int = 12) -> None:
    """Make a table tall enough for ``rows`` rows (up to ``cap``), no taller."""
    header = table.horizontalHeader().sizeHint().height()
    row = table.verticalHeader().defaultSectionSize()
    shown = max(1, min(rows, cap))
    table.setFixedHeight(header + row * shown + 2 * table.frameWidth() + 2)


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, places: int = 2) -> str:
    return "N/A" if value is None else format_value(round(value, places))


def _signed(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2f}"


def _item(text: str, *, number: int | None = None, align_right: bool = False) -> QTableWidgetItem:
    cell = QTableWidgetItem(text)
    if number is not None:
        cell.setData(Qt.ItemDataRole.UserRole, number)
    if align_right:
        cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return cell


# ----------------------------------------------------------------------
# The dashboard
# ----------------------------------------------------------------------
class ResultsDashboard(QWidget):
    """Statistics and charts for the marked examination, by scope."""

    computed = Signal()
    """Emitted after a computation has been adopted and drawn."""

    KPIS: tuple[tuple[str, str], ...] = (
        ("count", "Scored scripts"),
        ("mean", "Mean"),
        ("median", "Median"),
        ("sd", "Std deviation"),
        ("min", "Minimum"),
        ("max", "Maximum"),
        ("mode", "Mode"),
        ("iqr", f"Q1 {ra.RANGE_DASH} Q3"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("resultsDashboard")
        self._inputs: DashboardInputs | None = None
        self._provider: Callable[[], DashboardInputs | None] | None = None
        self._dirty = True
        self._fingerprint: tuple[object, ...] | None = None
        self._generation = 0
        self._workers: list[AnalyticsWorker] = []
        self._closing = False
        self.analytics: ra.ExamAnalytics | None = None
        self.error = ""
        self._scope_key: str | None = ra.OVERALL
        self._question: int | None = None

        self.setStyleSheet(
            f"""
            QFrame#dashboardKpiCard {{
                background: {Color.SURFACE};
                border: 1px solid {Color.BORDER};
                border-radius: {Radius.MD}px;
            }}
            QLabel#dashboardKpiCaption {{ color: {Color.TEXT_SECONDARY}; }}
            QLabel[role="note"] {{ color: {Color.TEXT_SECONDARY}; }}
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("dashboardScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        content.setObjectName("dashboardContent")
        content.setMinimumWidth(560)
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.LG)
        self.content_layout.setSpacing(Spacing.CARD_GAP)
        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area, 1)

        self.message_label = QLabel(NO_RESULTS_TEXT)
        self.message_label.setObjectName("dashboardMessage")
        self.message_label.setWordWrap(True)
        self.message_label.setTextFormat(Qt.TextFormat.RichText)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setMinimumHeight(80)
        self.content_layout.addWidget(self.message_label)

        self.figures = QWidget()
        self.figures.setObjectName("dashboardFigures")
        figures = QVBoxLayout(self.figures)
        figures.setContentsMargins(0, 0, 0, 0)
        figures.setSpacing(Spacing.CARD_GAP)
        self.kpi_cards = {key: _KpiCard(key, caption) for key, caption in self.KPIS}
        self.kpi_grid = _KpiGrid(list(self.kpi_cards.values()))
        figures.addWidget(self.kpi_grid)
        figures.addWidget(
            _ReflowRow(self._build_histogram(), self._build_summary_box(), stretch=(3, 2))
        )
        figures.addWidget(self._build_set_comparison())
        figures.addWidget(self._build_questions())
        self.content_layout.addWidget(self.figures)
        self.content_layout.addStretch(1)
        self.figures.setVisible(False)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_header(self) -> QWidget:
        """The scope selector, the status and the refresh button."""
        bar = QWidget()
        bar.setObjectName("dashboardHeader")
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.XS)
        row = QHBoxLayout()
        caption = QLabel("Analysis:")
        caption.setObjectName("dashboardScopeCaption")
        row.addWidget(caption)
        self.scope_combo = QComboBox()
        self.scope_combo.setObjectName("dashboardScopeCombo")
        self.scope_combo.setMinimumContentsLength(14)
        self.scope_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.scope_combo.setToolTip(
            "Overall Exam combines every set's marks. A set shows only the scripts "
            "whose effective (resolved) set code is that set."
        )
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)
        row.addWidget(self.scope_combo)
        self.status_label = QLabel("")
        self.status_label.setObjectName("dashboardStatus")
        self.status_label.setProperty("role", "note")
        row.addWidget(self.status_label, 1)
        self.refresh_button = QPushButton(load_icon("rotate-ccw"), "Refresh")
        self.refresh_button.setObjectName("dashboardRefreshButton")
        self.refresh_button.setToolTip("Recompute the analysis from the current results.")
        self.refresh_button.clicked.connect(self._on_refresh_clicked)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("dashboardNotice")
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextFormat(Qt.TextFormat.RichText)
        self.notice_label.setVisible(False)
        layout.addWidget(self.notice_label)
        return bar

    def _build_histogram(self) -> QWidget:
        box = QGroupBox("Marks distribution")
        box.setObjectName("dashboardHistogramBox")
        layout = QVBoxLayout(box)
        self.histogram = HistogramChart()
        self.histogram.setMinimumHeight(240)
        layout.addWidget(self.histogram)
        return box

    def _build_summary_box(self) -> QWidget:
        box = QGroupBox("Score summary")
        box.setObjectName("dashboardSummaryBox")
        layout = QVBoxLayout(box)
        self.box_plot = BoxPlotChart()
        layout.addWidget(self.box_plot)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("dashboardSummaryLabel")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)
        layout.addStretch(1)
        return box

    def _build_set_comparison(self) -> QWidget:
        self.comparison_box = QGroupBox("Set comparison")
        self.comparison_box.setObjectName("dashboardComparisonBox")
        layout = QVBoxLayout(self.comparison_box)
        note = QLabel(
            "Differences between sets are evidence to weigh, not a conclusion: sets "
            "can differ in who sat them as well as in the paper."
        )
        note.setWordWrap(True)
        note.setProperty("role", "note")
        layout.addWidget(note)
        self.comparison_table = _table(
            ("Set", "Scripts", "Mean", "Median", "Std dev", "Min", "Max", "KR-20"),
            "dashboardComparisonTable",
            stretch_last=False,
        )
        self.comparison_plot = BoxPlotChart()
        self.comparison_plot.setObjectName("dashboardComparisonPlot")
        layout.addWidget(_ReflowRow(self.comparison_table, self.comparison_plot, stretch=(1, 1)))
        return self.comparison_box

    def _build_questions(self) -> QWidget:
        self.questions_box = QWidget()
        self.questions_box.setObjectName("dashboardQuestions")
        layout = QVBoxLayout(self.questions_box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.CARD_GAP)

        self.question_note = QLabel("")
        self.question_note.setObjectName("dashboardQuestionNote")
        self.question_note.setWordWrap(True)
        self.question_note.setProperty("role", "note")
        layout.addWidget(self.question_note)

        self.question_panels = QWidget()
        panels = QVBoxLayout(self.question_panels)
        panels.setContentsMargins(0, 0, 0, 0)
        panels.setSpacing(Spacing.CARD_GAP)

        correct_box = QGroupBox("Question-wise % correct")
        correct_box.setObjectName("dashboardCorrectBox")
        correct_layout = QVBoxLayout(correct_box)
        self.difficulty_label = QLabel("")
        self.difficulty_label.setObjectName("dashboardDifficultyLabel")
        self.difficulty_label.setWordWrap(True)
        self.difficulty_label.setToolTip(TOOLTIPS["difficulty"])
        correct_layout.addWidget(self.difficulty_label)
        self.correct_chart = QuestionChart("percent")
        self.correct_chart.setObjectName("dashboardCorrectChart")
        self.correct_chart.setMinimumHeight(230)
        self.correct_chart.activated.connect(self._on_chart_question)
        correct_layout.addWidget(self.correct_chart)
        panels.addWidget(correct_box)

        disc_box = QGroupBox("Question discrimination")
        disc_box.setObjectName("dashboardDiscriminationBox")
        disc_box.setToolTip(TOOLTIPS["discrimination"])
        disc_layout = QVBoxLayout(disc_box)
        self.discrimination_label = QLabel("")
        self.discrimination_label.setObjectName("dashboardDiscriminationLabel")
        self.discrimination_label.setWordWrap(True)
        self.discrimination_label.setProperty("role", "note")
        disc_layout.addWidget(self.discrimination_label)
        self.discrimination_chart = QuestionChart("signed")
        self.discrimination_chart.setObjectName("dashboardDiscriminationChart")
        self.discrimination_chart.activated.connect(self._on_chart_question)
        disc_layout.addWidget(self.discrimination_chart)
        panels.addWidget(disc_box)

        self.easiest_table = _table(
            ("Question", "% correct", "Discrimination"), "dashboardEasiestTable",
            stretch_last=False,
        )
        self.hardest_table = _table(
            ("Question", "% correct", "Discrimination"), "dashboardHardestTable",
            stretch_last=False,
        )
        for table in (self.easiest_table, self.hardest_table):
            table.cellClicked.connect(lambda row, _col, t=table: self._on_table_question(t, row))
        easy_box = QGroupBox("Easiest questions")
        easy_box.setObjectName("dashboardEasiestBox")
        QVBoxLayout(easy_box).addWidget(self.easiest_table)
        hard_box = QGroupBox("Hardest questions")
        hard_box.setObjectName("dashboardHardestBox")
        QVBoxLayout(hard_box).addWidget(self.hardest_table)
        panels.addWidget(_ReflowRow(easy_box, hard_box, breakpoint=600))

        outcome_box = QGroupBox("Correct / incorrect / multiple / blank by question")
        outcome_box.setObjectName("dashboardOutcomeBox")
        outcome_layout = QVBoxLayout(outcome_box)
        legend = QLabel(
            " ".join(
                f"<span style='color:{colour}'>&#9632;</span> {name}&nbsp;&nbsp;"
                for name, colour in (
                    ("Correct", ChartColor.CORRECT),
                    ("Incorrect", ChartColor.INCORRECT),
                    ("Multiple", ChartColor.MULTIPLE),
                    ("Blank", ChartColor.BLANK),
                )
            )
        )
        legend.setObjectName("dashboardOutcomeLegend")
        legend.setTextFormat(Qt.TextFormat.RichText)
        outcome_layout.addWidget(legend)
        self.outcome_chart = QuestionChart("stack")
        self.outcome_chart.setObjectName("dashboardOutcomeChart")
        self.outcome_chart.activated.connect(self._on_chart_question)
        outcome_layout.addWidget(self.outcome_chart)
        panels.addWidget(outcome_box)

        flags_box = QGroupBox("Questions to review")
        flags_box.setObjectName("dashboardFlagsBox")
        flags_layout = QVBoxLayout(flags_box)
        flags_note = QLabel(
            "Prompts to look again, not verdicts. Nothing here changes a key or a mark."
        )
        flags_note.setWordWrap(True)
        flags_note.setProperty("role", "note")
        flags_layout.addWidget(flags_note)
        self.flags_table = _table(("Question", "Reason", "Detail"), "dashboardFlagsTable")
        self.flags_table.cellClicked.connect(
            lambda row, _col: self._on_table_question(self.flags_table, row)
        )
        flags_layout.addWidget(self.flags_table)

        option_box = QGroupBox("Selected question")
        option_box.setObjectName("dashboardOptionBox")
        option_layout = QVBoxLayout(option_box)
        picker = QHBoxLayout()
        picker.addWidget(QLabel("Question:"))
        self.question_combo = QComboBox()
        self.question_combo.setObjectName("dashboardQuestionCombo")
        self.question_combo.setMinimumContentsLength(6)
        self.question_combo.currentIndexChanged.connect(self._on_question_combo)
        picker.addWidget(self.question_combo)
        picker.addStretch(1)
        option_layout.addLayout(picker)
        self.option_label = QLabel("")
        self.option_label.setObjectName("dashboardOptionLabel")
        self.option_label.setWordWrap(True)
        self.option_label.setTextFormat(Qt.TextFormat.RichText)
        option_layout.addWidget(self.option_label)
        self.option_chart = OptionChart()
        option_layout.addWidget(self.option_chart)
        option_layout.addStretch(1)
        panels.addWidget(_ReflowRow(flags_box, option_box, stretch=(1, 1)))

        reliability_box = QGroupBox("Reliability")
        reliability_box.setObjectName("dashboardReliabilityBox")
        reliability_layout = QVBoxLayout(reliability_box)
        self.reliability_label = QLabel("")
        self.reliability_label.setObjectName("dashboardReliabilityLabel")
        self.reliability_label.setWordWrap(True)
        self.reliability_label.setTextFormat(Qt.TextFormat.RichText)
        self.reliability_label.setToolTip(TOOLTIPS["kr20"] + "\n\n" + TOOLTIPS["sem"])
        reliability_layout.addWidget(self.reliability_label)
        panels.addWidget(reliability_box)

        layout.addWidget(self.question_panels)
        return self.questions_box

    # ------------------------------------------------------------------
    # Inputs and computation
    # ------------------------------------------------------------------
    def set_provider(self, provider: Callable[[], DashboardInputs | None]) -> None:
        """Where to fetch inputs from when the tab is shown and they are stale."""
        self._provider = provider

    def invalidate(self) -> None:
        """The results may have changed; refetch before the next display."""
        self._dirty = True
        if self.isVisible():
            self.refresh()

    def showEvent(self, event: QShowEvent) -> None:
        """Compute on first display, and whenever the results changed meanwhile."""
        super().showEvent(event)
        if self._dirty:
            self.refresh()

    def _on_refresh_clicked(self, _checked: bool = False) -> None:
        self._fingerprint = None
        self._dirty = True
        self.refresh()

    def refresh(self) -> None:
        """Fetch the current inputs and recompute if they changed."""
        self._dirty = False
        inputs: DashboardInputs | None = None
        if self._provider is not None:
            try:
                inputs = self._provider()
            except Exception as exc:  # the Results tab must not be affected
                _LOGGER.exception("Results dashboard inputs could not be read")
                self.show_error(
                    f"The results could not be read for analysis ({type(exc).__name__})."
                )
                return
        self.set_inputs(inputs)

    def set_inputs(self, inputs: DashboardInputs | None) -> None:
        """Adopt new inputs, computing in the background when they changed."""
        self._inputs = inputs
        self._render_notices()
        if inputs is None:
            self._fingerprint = None
            self.analytics = None
            self.error = ""
            self._render()
            return
        fingerprint = inputs.fingerprint()
        if fingerprint == self._fingerprint and (self.analytics is not None or self.error):
            return
        self._fingerprint = fingerprint
        self._generation += 1
        self.status_label.setText("Calculating...")
        self.refresh_button.setEnabled(False)
        worker = AnalyticsWorker(self._generation, inputs, self)
        worker.ready.connect(self._on_ready)
        self._workers = [item for item in self._workers if item.isRunning()]
        self._workers.append(worker)
        worker.start()

    def _on_ready(self, generation: int, outcome: object) -> None:
        """Adopt a finished computation, if it is still the latest one."""
        if self._closing or generation != self._generation:
            return
        self.refresh_button.setEnabled(True)
        if isinstance(outcome, str):
            self.show_error(outcome)
            return
        assert isinstance(outcome, ra.ExamAnalytics)
        self.analytics = outcome
        self.error = ""
        try:
            self._render()
        except Exception as exc:
            _LOGGER.exception("Results dashboard could not be drawn")
            self.show_error(
                f"The analysis could not be displayed ({type(exc).__name__}). The Results "
                "tab is unaffected; details are in the application log."
            )
            return
        self.computed.emit()

    def show_error(self, message: str) -> None:
        """Say that the analysis failed, and show nothing that could mislead."""
        self.error = message
        self.analytics = None
        self.refresh_button.setEnabled(True)
        self.status_label.setText("")
        self.figures.setVisible(False)
        self.message_label.setVisible(True)
        self.message_label.setText(
            f"<span style='color:{Color.DESTRUCTIVE}'>{html.escape(message)}</span>"
        )

    def shutdown(self) -> None:
        """Wait for any computation in flight."""
        self._closing = True
        for worker in self._workers:
            if worker.isRunning():
                worker.wait(10_000)
        self._workers = []

    # ------------------------------------------------------------------
    # Scope and question selection
    # ------------------------------------------------------------------
    def current_scope(self) -> ra.ScopeAnalytics | None:
        """The scope the selector shows."""
        if self.analytics is None:
            return None
        return self.analytics.scope(self._scope_key) or self.analytics.overall

    def select_scope(self, key: str | None) -> bool:
        """Select a scope by key (a set code, or ``None`` for Overall)."""
        for index in range(self.scope_combo.count()):
            if self.scope_combo.itemData(index) == key:
                self.scope_combo.setCurrentIndex(index)
                return True
        return False

    def select_question(self, number: int) -> bool:
        """Show one question in the distractor panel."""
        index = self.question_combo.findData(number)
        if index < 0:
            return False
        self.question_combo.setCurrentIndex(index)
        return True

    @property
    def selected_question(self) -> int | None:
        """The question the distractor panel shows."""
        return self._question

    def _on_scope_changed(self, index: int) -> None:
        if index < 0:
            return
        self._scope_key = self.scope_combo.itemData(index)
        self._render_scope()

    def _on_chart_question(self, index: int) -> None:
        scope = self.current_scope()
        if scope is not None and 0 <= index < len(scope.questions):
            self.select_question(scope.questions[index].number)

    def _on_table_question(self, table: QTableWidget, row: int) -> None:
        cell = table.item(row, 0)
        number = cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None
        if isinstance(number, int):
            self.select_question(number)

    def _on_question_combo(self, index: int) -> None:
        number = self.question_combo.itemData(index) if index >= 0 else None
        self._question = number if isinstance(number, int) else None
        self._render_question()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render_notices(self) -> None:
        inputs = self._inputs
        notices = list(inputs.notices) if inputs is not None else []
        analytics = self.analytics
        if analytics is not None and analytics.stale_count:
            notices.insert(
                0,
                f"<b>{analytics.stale_count} scored result(s) need recalculating.</b> "
                "Their marks are included, as on the Results tab, but may not reflect "
                "the current key, configuration or answers. Recalculate on the "
                "Results tab before relying on these figures.",
            )
        if analytics is not None and analytics.excluded_count:
            notices.append(
                f"{analytics.excluded_count} scored script(s) are counted in the marks but "
                "not in the question analysis: the key or scoring revision that produced "
                "their mark can no longer be read."
            )
        self.notice_label.setVisible(bool(notices))
        self.notice_label.setText(
            f"<span style='color:{Color.DESTRUCTIVE}'>" + "<br>".join(notices) + "</span>"
        )

    def _render(self) -> None:
        """Rebuild the scope list and draw the selected scope."""
        analytics = self.analytics
        self.status_label.setText("")
        self._render_notices()
        self.scope_combo.blockSignals(True)
        self.scope_combo.clear()
        if analytics is not None:
            for scope in analytics.scopes:
                suffix = "" if scope.key is None else f"  ({scope.count})"
                self.scope_combo.addItem(scope.label + suffix, scope.key)
            index = self.scope_combo.findData(self._scope_key)
            if self._scope_key is None or index < 0:
                self._scope_key = ra.OVERALL
                index = 0
            self.scope_combo.setCurrentIndex(index)
        self.scope_combo.blockSignals(False)
        self.scope_combo.setEnabled(analytics is not None)
        self._render_scope()

    def _render_scope(self) -> None:
        analytics = self.analytics
        scope = self.current_scope()
        if analytics is None or scope is None or analytics.overall.count == 0:
            self.figures.setVisible(False)
            self.message_label.setVisible(True)
            if not self.error:
                self.message_label.setText(NO_RESULTS_TEXT)
            return
        if scope.count == 0:
            self.figures.setVisible(False)
            self.message_label.setVisible(True)
            note = self._inputs.set_notes.get(scope.key or "", "") if self._inputs else ""
            self.message_label.setText(
                f"No scored scripts for {html.escape(scope.label)}."
                + (f"<br><span style='color:{Color.TEXT_SECONDARY}'>{html.escape(note)}</span>"
                   if note else "")
            )
            return
        self.message_label.setVisible(False)
        self.figures.setVisible(True)
        self._render_kpis(scope)
        self._render_distribution(scope, analytics)
        self._render_comparison(scope, analytics)
        self._render_questions(scope)

    def _render_kpis(self, scope: ra.ScopeAnalytics) -> None:
        summary = scope.summary
        assert summary is not None
        if not summary.modes:
            mode = "None"
        elif len(summary.modes) <= 3:
            mode = ", ".join(format_value(round(value, 2)) for value in summary.modes)
        else:
            mode = f"{len(summary.modes)} values"
        values = {
            "count": str(scope.count),
            "mean": _num(summary.mean),
            "median": _num(summary.median),
            "sd": _num(summary.std_dev),
            "min": _num(summary.minimum),
            "max": _num(summary.maximum),
            "mode": mode,
            "iqr": f"{_num(summary.q1)} {ra.RANGE_DASH} {_num(summary.q3)}",
        }
        for key, card in self.kpi_cards.items():
            card.value.setText(values[key])
        self.kpi_cards["mode"].value.setToolTip(
            f"Occurs {summary.mode_frequency} time(s)" if summary.modes else ""
        )
        if summary.std_dev is None:
            self.kpi_cards["sd"].value.setToolTip("Needs at least two scored scripts.")

    def _render_distribution(self, scope: ra.ScopeAnalytics, analytics: ra.ExamAnalytics) -> None:
        summary, histogram = scope.summary, scope.histogram
        assert summary is not None and histogram is not None
        bars = [
            HistogramBar(
                lower=item.lower,
                upper=item.upper,
                count=item.count,
                tooltip=(
                    f"<b>Score {html.escape(item.label)}</b><br>Students: {item.count}"
                    f"<br>Percentage: {item.proportion * 100:.1f}% of {histogram.total}"
                ),
            )
            for item in histogram.bins
        ]
        self.histogram.set_data(bars, mean=summary.mean, median=summary.median)
        scale = (histogram.bins[0].lower, histogram.bins[-1].upper)
        self.box_plot.set_data([(scope.label, summary.box)], scale)
        lines = [
            f"Mean <b>{_num(summary.mean)}</b> · Median <b>{_num(summary.median)}</b> "
            f"· Full mark {format_value(round(analytics.maximum_possible, 2))}",
            f"Q1 {_num(summary.q1)} · Q3 {_num(summary.q3)} · IQR {_num(summary.iqr)}",
            f"Variance {_num(summary.variance)} · Skewness {_num(summary.skewness)}",
        ]
        if summary.box.outliers:
            lines.append(f"{len(summary.box.outliers)} mark(s) beyond 1.5 x IQR (circles).")
        lines.append(
            f"<span style='color:{Color.TEXT_SECONDARY}'>Histogram bins are "
            f"{format_value(histogram.width)} mark(s) wide. No pass mark is configured, "
            "so no pass rate is shown.</span>"
        )
        self.summary_label.setText("<br>".join(lines))

    def _render_comparison(self, scope: ra.ScopeAnalytics, analytics: ra.ExamAnalytics) -> None:
        rows = list(analytics.set_comparison)
        show = scope.key is ra.OVERALL and len([r for r in rows if r.count]) >= 1 and len(rows) > 1
        self.comparison_box.setVisible(show)
        if not show:
            return
        table = self.comparison_table
        table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            summary = row.summary
            kr20 = row.reliability.kr20 if row.reliability is not None else None
            cells = (
                f"Set {row.set_code}",
                str(row.count),
                _num(summary.mean) if summary else "-",
                _num(summary.median) if summary else "-",
                _num(summary.std_dev) if summary else "-",
                _num(summary.minimum) if summary else "-",
                _num(summary.maximum) if summary else "-",
                _num(kr20) if summary else "-",
            )
            for column, text in enumerate(cells):
                table.setItem(index, column, _item(text, align_right=column > 0))
        _fit_rows(table, len(rows))
        histogram = analytics.overall.histogram
        scale = (
            (histogram.bins[0].lower, histogram.bins[-1].upper)
            if histogram is not None
            else (0.0, analytics.maximum_possible or 1.0)
        )
        self.comparison_plot.set_data(
            [(f"Set {row.set_code}", row.summary.box) for row in rows if row.summary], scale
        )

    def _render_questions(self, scope: ra.ScopeAnalytics) -> None:
        has_questions = bool(scope.questions)
        self.question_panels.setVisible(has_questions)
        self.question_note.setVisible(
            bool(scope.question_note) or scope.excluded_from_questions > 0
        )
        note = scope.question_note
        if has_questions and scope.excluded_from_questions:
            note = (
                f"{scope.excluded_from_questions} script(s) are not in the question analysis "
                "because the key or scoring revision behind their mark can no longer be read."
            )
        self.question_note.setText(note)
        if not has_questions:
            self.question_combo.blockSignals(True)
            self.question_combo.clear()
            self.question_combo.blockSignals(False)
            self._question = None
            return

        settings = ra.DEFAULT_SETTINGS
        counts = scope.difficulty_counts()
        withdrawn = sum(1 for stat in scope.questions if stat.is_withdrawn)
        self.difficulty_label.setText(
            "   ".join(f"{band.label}: {counts[band]}" for band in ra.Difficulty)
            + (f"   Withdrawn: {withdrawn}" if withdrawn else "")
            + "   (dashed lines at "
            + ", ".join(
                f"{value * 100:g}%"
                for value in (settings.difficult_min, settings.moderate_min, settings.easy_min)
            )
            + ")"
        )
        self.correct_chart.set_data(
            [
                QuestionBar(stat.number, stat.p_correct, self._question_tooltip(stat))
                for stat in scope.questions
            ],
            bands=(settings.difficult_min, settings.moderate_min, settings.easy_min),
        )
        self.discrimination_chart.set_data(
            [
                QuestionBar(stat.number, stat.discrimination, self._question_tooltip(stat))
                for stat in scope.questions
            ]
        )
        negative = [
            stat.number
            for stat in scope.questions
            if stat.discrimination is not None
            and round(stat.discrimination, 2) < settings.flag_negative_discrimination
        ]
        unavailable = sum(1 for stat in scope.questions if stat.discrimination is None)
        self.discrimination_label.setText(
            (
                f"Negative discrimination (red): Q{', Q'.join(map(str, negative))}. "
                if negative
                else "No question has negative discrimination. "
            )
            + (f"Not available for {unavailable} question(s) (no variation, or fewer than "
               f"{settings.min_candidates_discrimination} responses). " if unavailable else "")
            + "Hover a bar for its value."
        )
        self.outcome_chart.set_data(
            [
                QuestionBar(
                    stat.number,
                    stat.p_correct,
                    self._question_tooltip(stat),
                    stack=(
                        (stat.p_correct or 0.0, stat.p_incorrect or 0.0,
                         stat.p_multiple or 0.0, stat.p_blank or 0.0)
                        if stat.responses
                        else ()
                    ),
                )
                for stat in scope.questions
            ]
        )
        self._fill_ranked(self.easiest_table, scope.easiest(settings.ranked_count))
        self._fill_ranked(self.hardest_table, scope.hardest(settings.ranked_count))
        self._fill_flags(scope.flags)
        self._fill_reliability(scope.reliability)

        keep = self._question
        self.question_combo.blockSignals(True)
        self.question_combo.clear()
        for stat in scope.questions:
            self.question_combo.addItem(f"Q{stat.number}", stat.number)
        self.question_combo.blockSignals(False)
        target = keep if keep is not None and scope.question(keep) is not None else None
        if target is None:
            flagged = scope.flags[0].number if scope.flags else None
            target = flagged if flagged is not None else scope.questions[0].number
        index = self.question_combo.findData(target)
        self.question_combo.setCurrentIndex(max(index, 0))
        self._on_question_combo(self.question_combo.currentIndex())

    @staticmethod
    def _question_tooltip(stat: ra.QuestionStat) -> str:
        if stat.responses == 0:
            return f"<b>Question {stat.number}</b><br>Withdrawn: no responses counted."
        lines = [
            f"<b>Question {stat.number}</b>",
            f"Correct: {_pct(stat.p_correct)}",
            f"Incorrect: {_pct(stat.p_incorrect)}",
            f"Multiple: {_pct(stat.p_multiple)}",
            f"Blank: {_pct(stat.p_blank)}",
            f"Responses: {stat.responses}",
            f"Discrimination: {_signed(stat.discrimination)}",
        ]
        if stat.difficulty is not None:
            lines.append(f"Difficulty: {stat.difficulty.label}")
        if stat.withdrawn:
            lines.append(f"Withdrawn for {stat.withdrawn} script(s)")
        return "<br>".join(lines)

    @staticmethod
    def _fill_ranked(table: QTableWidget, stats: Sequence[ra.QuestionStat]) -> None:
        table.setRowCount(len(stats))
        for row, stat in enumerate(stats):
            table.setItem(row, 0, _item(f"Q{stat.number}", number=stat.number))
            table.setItem(row, 1, _item(_pct(stat.p_correct), align_right=True))
            table.setItem(row, 2, _item(_signed(stat.discrimination), align_right=True))
        _fit_rows(table, len(stats))

    def _fill_flags(self, flags: Sequence[ra.QuestionFlag]) -> None:
        table = self.flags_table
        if not flags:
            table.setRowCount(1)
            table.setItem(0, 0, _item("-"))
            table.setItem(0, 1, _item("No question met a review threshold."))
            table.setItem(0, 2, _item(""))
            _fit_rows(table, 1)
            return
        table.setRowCount(len(flags))
        for row, flag in enumerate(flags):
            table.setItem(row, 0, _item(f"Q{flag.number}", number=flag.number))
            table.setItem(row, 1, _item(flag.kind.label))
            detail = _item(flag.detail)
            detail.setToolTip(flag.detail)
            table.setItem(row, 2, detail)
        _fit_rows(table, len(flags), cap=10)

    def _fill_reliability(self, found: ra.Reliability | None) -> None:
        if found is None:
            self.reliability_label.setText("Not available.")
            return
        kr20 = _num(found.kr20, 3) if found.kr20 is not None else "Not available"
        sem = f"{_num(found.sem)} questions" if found.sem is not None else "Not available"
        text = (
            f"KR-20: <b>{kr20}</b> · Standard error of measurement: <b>{sem}</b>"
            f"<br>From {found.candidates} script(s) and {found.items} question(s)."
        )
        if found.reason:
            text += (
                f"<br><span style='color:{Color.TEXT_SECONDARY}'>"
                f"{html.escape(found.reason)}</span>"
            )
        self.reliability_label.setText(text)

    def _render_question(self) -> None:
        scope = self.current_scope()
        stat = scope.question(self._question) if scope and self._question is not None else None
        index = (
            next((i for i, item in enumerate(scope.questions) if item.number == stat.number), None)
            if scope is not None and stat is not None
            else None
        )
        for chart in (self.correct_chart, self.discrimination_chart, self.outcome_chart):
            chart.set_selected(index)
        if stat is None:
            self.option_label.setText("")
            self.option_chart.set_data([])
            return
        scripts = stat.scripts or 1
        key = ", ".join(stat.key) if stat.key else "not recorded"
        parts = [
            f"<b>Q{stat.number}</b> - Correct answer: <b>{html.escape(key)}</b>",
            f"{stat.scripts} script(s) · {_pct(stat.p_correct)} correct · "
            f"Discrimination {_signed(stat.discrimination)}"
            + (f" · {stat.difficulty.label}" if stat.difficulty else ""),
        ]
        if len(stat.key) > 1:
            parts.append(
                "Results in this scope were marked with key revisions that disagree on "
                "this question."
            )
        if stat.withdrawn:
            parts.append(
                f"Withdrawn for {stat.withdrawn} script(s): full credit, whatever was marked."
            )
        self.option_label.setText("<br>".join(parts))
        bars = [
            OptionBar(label, count, count / scripts, is_key=label in stat.key)
            for label, count in stat.option_counts
        ]
        bars.append(OptionBar("Blank", stat.option_blank, stat.option_blank / scripts))
        if stat.option_multiple:
            bars.append(OptionBar("Multiple", stat.option_multiple, stat.option_multiple / scripts))
        self.option_chart.set_data(bars)
