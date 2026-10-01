"""Review a marked solution sheet before any of it becomes an answer key.

    ┌───────────────────────────────┬───────────────────────────────┐
    │ the sheet, as registered      │ recognition / set-code status │
    │ (the selected question is     │ ─────────────────────────────  │
    │  outlined on the page)        │ Q | Read | Status | Answer    │
    │                               │ ...                           │
    └───────────────────────────────┴───────────────────────────────┘
                                   [ Cancel ]  [ Use These Answers ]

Nothing here writes anything. Accepting hands the confirmed answers back to
the Answer Key stage's editor, where they become an *unsaved* draft for the
chosen set; only **Save as New Revision** stores them, and only as a new,
unverified revision. A verified key is never touched by reading a sheet.

The rules the dialog enforces:

* A sheet that did not register offers nothing to import - there are no
  answers on it, only guesses.
* A blank or multiple mark is shown as exactly that and left unanswered until a
  person chooses; it is never resolved by the dialog.
* A sheet whose set field reads a *different* set than the one selected cannot
  be accepted until the operator chooses which set it is for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.scoring import BLANK
from omr_scanner.gui.scan.preview import (
    LANE_PADDING_RATIO,
    FieldLane,
    LaneMark,
    LaneState,
    ScanPreviewView,
)
from omr_scanner.gui.theme import ANSWER_KEY_STAGE_STYLESHEET, VARIANT_PRIMARY, VARIANT_PROPERTY
from omr_scanner.gui.widgets.status_chips import StatusChipStrip
from omr_scanner.services import group_cells
from omr_scanner.services.answer_key import ReadingKind, SetCodeCheck
from omr_scanner.services.set_identity import same_set

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.recognition_models import BubbleView
    from omr_scanner.services.solution_sheet import SolutionSheetReading

SOLUTION_TABLE_COLUMNS: tuple[str, ...] = ("Q", "Read", "Status", "Answer")
NO_ANSWER = "— none —"
"""The combo entry for "no answer chosen yet". Never saved as an answer."""


class SolutionSheetDialog(QDialog):
    """Show one read solution sheet and let the operator confirm or correct it.

    Args:
        reading: The recognised sheet.
        template: The template it was read with - for the question outlines.
        parent: Optional Qt parent.
    """

    def __init__(
        self,
        reading: SolutionSheetReading,
        template: OmrTemplate,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("solutionSheetDialog")
        self.setWindowTitle(f"Review Solution Sheet - {reading.path.name}")
        self.setStyleSheet(ANSWER_KEY_STAGE_STYLESHEET)
        self.reading = reading
        self.template = template
        plan = reading.scanned.plan
        self._labels = plan.labels
        self._numbers = plan.numbers
        self._answers: dict[int, str] = {
            number: character
            for number, character in zip(plan.numbers, reading.scanned.answers, strict=False)
            if character in plan.labels
        }
        self._rows: dict[int, int] = {}

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("solutionSheetSplitter")
        splitter.addWidget(self._build_preview())
        splitter.addWidget(self._build_review())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, stretch=1)

        self.outcome_label = QLabel("")
        self.outcome_label.setObjectName("solutionSheetOutcomeLabel")
        self.outcome_label.setWordWrap(True)
        layout.addWidget(self.outcome_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.accept_button = self.buttons.addButton(
            "Use These Answers", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.accept_button.setObjectName("useSolutionAnswersButton")
        self.accept_button.setProperty(VARIANT_PROPERTY, VARIANT_PRIMARY)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._fill_table()
        self._refresh()
        self._framed = False
        self.resize(1180, 720)

    def showEvent(self, event: QShowEvent) -> None:
        """Open on something worth looking at, once.

        Found on a real scan: without this the preview opened at 1:1 on the
        page's top-left corner - the registration marker and the name box -
        so the operator's first view of the sheet showed no answers at all.
        """
        super().showEvent(event)
        if not self._framed:
            self._framed = True
            self.frame_initial_view()

    def frame_initial_view(self) -> None:
        """Select the first question needing review, or fit the whole page."""
        first = next(
            (
                row
                for row in range(self.table.rowCount())
                if not self.table.isRowHidden(row) and self.only_problems.isChecked()
            ),
            None,
        )
        if first is not None:
            self.table.setCurrentCell(first, 0)
        else:
            self.preview.fit_to_window()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_preview(self) -> QWidget:
        """The sheet, as recognition registered it."""
        self.preview = ScanPreviewView()
        self.preview.setObjectName("solutionSheetPreview")
        self.preview.setMinimumWidth(360)
        result = self.reading.result
        self.preview.set_page(
            result.preview,
            canonical_width=result.canonical_width,
            canonical_height=result.canonical_height,
            preview_scale=result.preview_scale,
        )
        self.preview.set_overlay(result.zones, result.bubbles, result.markers)
        self.preview.set_overlay_visible(zones=False, bubbles=True, empty=False)
        if not self.reading.registered:
            self.preview.set_placeholder(
                "This sheet could not be registered, so no page is shown. "
                + (self.reading.scanned.registration_message or "")
            )
        return self.preview

    def _build_review(self) -> QWidget:
        """Status, the set decision, and the question table."""
        panel = QWidget()
        panel.setMinimumWidth(380)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.chips = StatusChipStrip()
        self.chips.setObjectName("solutionSheetChips")
        layout.addWidget(self.chips)

        verdict = self.reading.verdict
        self.set_label = QLabel(verdict.message)
        self.set_label.setObjectName("solutionSheetSetLabel")
        self.set_label.setWordWrap(True)
        layout.addWidget(self.set_label)

        self.keep_set_radio = QRadioButton(f"Import into Set {verdict.selected} (your selection)")
        self.keep_set_radio.setObjectName("keepSelectedSetRadio")
        self.sheet_set_radio = QRadioButton(
            f"Import into Set {verdict.read_label} (as marked on the sheet)"
        )
        self.sheet_set_radio.setObjectName("useSheetSetRadio")
        needs_choice = verdict.check is SetCodeCheck.MISMATCH
        for radio in (self.keep_set_radio, self.sheet_set_radio):
            radio.setVisible(needs_choice)
            radio.toggled.connect(self._refresh)
            layout.addWidget(radio)
        # Only offered when the sheet's set is one this project defines: a key
        # filed under a set no candidate can belong to would mark nobody.
        self.sheet_set_radio.setEnabled(verdict.read_is_defined)
        if needs_choice and not verdict.read_is_defined:
            self.sheet_set_radio.setToolTip(
                f"Set {verdict.read} is not defined in Project Configuration."
            )

        header = QHBoxLayout()
        heading = QLabel("Answers as read")
        heading.setObjectName("answerKeySectionHeading")
        header.addWidget(heading)
        header.addStretch(1)
        self.only_problems = QCheckBox("Only questions needing review")
        self.only_problems.setObjectName("solutionOnlyProblemsCheck")
        self.only_problems.toggled.connect(self._apply_filter)
        header.addWidget(self.only_problems)
        layout.addLayout(header)

        self.table = QTableWidget(0, len(SOLUTION_TABLE_COLUMNS))
        self.table.setObjectName("solutionSheetTable")
        self.table.setHorizontalHeaderLabels(list(SOLUTION_TABLE_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.currentCellChanged.connect(self._on_row_changed)
        layout.addWidget(self.table, stretch=1)
        return panel

    def _fill_table(self) -> None:
        """One row per question, with an answer chooser on each."""
        readings = {item.number: item for item in self.reading.scanned.readings}
        self.table.setRowCount(len(self._numbers))
        widest = 0
        for row, number in enumerate(self._numbers):
            self._rows[number] = row
            reading = readings.get(number)
            kind = reading.kind if reading is not None else ReadingKind.UNREADABLE
            raw = reading.raw if reading is not None else ""
            status = reading.describe if reading is not None else ReadingKind.UNREADABLE.label
            if kind is ReadingKind.CLEAR:
                status = "Clear mark" + (
                    " - low confidence, please check"
                    if reading is not None and reading.low_confidence
                    else ""
                )
            for column, text in enumerate((str(number), raw or "—", status)):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, number)
                self.table.setItem(row, column, item)
            chooser = QComboBox()
            chooser.setObjectName(f"solutionAnswerCombo_{number}")
            chooser.addItem(NO_ANSWER, "")
            for label in self._labels:
                chooser.addItem(label, label)
            chooser.setCurrentIndex(max(chooser.findData(self._answers.get(number, "")), 0))
            chooser.setToolTip(f"The correct answer for Question {number}.")
            chooser.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            # Rows as tall as the chooser, so one row's combo never spills over
            # the next row's text.
            height = chooser.sizeHint().height() + 4
            self.table.setRowHeight(row, max(self.table.rowHeight(row), height))
            chooser.currentIndexChanged.connect(
                lambda _index, n=number, box=chooser: self._on_chosen(n, str(box.currentData()))
            )
            self.table.setCellWidget(row, 3, chooser)
            widest = max(widest, chooser.sizeHint().width() + 8)
        self.table.setColumnWidth(3, max(widest, 110))
        needs_review = any(
            item.kind is not ReadingKind.CLEAR or item.low_confidence
            for item in self.reading.scanned.readings
        )
        self.only_problems.setChecked(needs_review and self.reading.registered)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------
    def set_answer(self, number: int, label: str) -> None:
        """Choose the correct answer for one question (``""`` clears it)."""
        row = self._rows.get(number)
        if row is None:
            return
        chooser = self.table.cellWidget(row, 3)
        if isinstance(chooser, QComboBox):
            chooser.setCurrentIndex(max(chooser.findData(label.upper() if label else ""), 0))

    def choose_set(self, code: str) -> None:
        """Decide which set a mismatched sheet is for."""
        verdict = self.reading.verdict
        if same_set(code, verdict.sheet_set) and self.sheet_set_radio.isEnabled():
            self.sheet_set_radio.setChecked(True)
        elif same_set(code, verdict.selected):
            self.keep_set_radio.setChecked(True)

    def _on_chosen(self, number: int, label: str) -> None:
        if label:
            self._answers[number] = label
        else:
            self._answers.pop(number, None)
        self._refresh()

    def _on_row_changed(self, row: int, *_rest: int) -> None:
        """Outline the selected question on the sheet and bring it into view."""
        item = self.table.item(row, 0) if row >= 0 else None
        number = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if number is None:
            return
        self.highlight_question(int(number))

    def highlight_question(self, number: int) -> bool:
        """Outline one question's bubbles; ``False`` when it cannot be placed."""
        bubbles = self._question_bubbles(number)
        if not bubbles:
            self.preview.set_lanes(())
            return False
        left = min(b.x - b.width / 2 for b in bubbles)
        top = min(b.y - b.height / 2 for b in bubbles)
        right = max(b.x + b.width / 2 for b in bubbles)
        bottom = max(b.y + b.height / 2 for b in bubbles)
        pad = (sum(b.width for b in bubbles) / len(bubbles)) * LANE_PADDING_RATIO
        marks = tuple(
            LaneMark(x=b.x, y=b.y, width=b.width, height=b.height, label=b.label)
            for b in bubbles
            if b.selected
        )
        self.preview.set_lanes(
            (
                FieldLane(
                    x=left - pad,
                    y=top - pad,
                    width=(right - left) + 2 * pad,
                    height=(bottom - top) + 2 * pad,
                    state=LaneState.UNRESOLVED,
                    active=True,
                    machine_marks=marks,
                ),
            )
        )
        # Frame the question with a few rows either side and a margin, so the
        # operator reads the mark against its neighbours rather than alone.
        row = bottom - top
        width = right - left
        context = QRectF(left - width, top - 6 * row, 3 * width, 13 * row)
        self.preview.focus_on(context)
        return True

    def _question_bubbles(self, number: int) -> tuple[BubbleView, ...]:
        """The measured bubbles of one question, from the recognition result."""
        view = next(
            (item for item in self.reading.result.answers if item.number == number), None
        )
        if view is None:
            return ()
        zone = next((z for z in self.template.zones if z.id == view.zone_id), None)
        first = getattr(zone.field, "first_question", None) if zone is not None else None
        if first is None:
            return ()
        cells = set(group_cells(self.template, view.zone_id, number - int(first)))
        return tuple(
            bubble
            for bubble in self.reading.result.bubbles
            if bubble.zone_id == view.zone_id and (bubble.row, bubble.column) in cells
        )

    def _apply_filter(self) -> None:
        """Hide clean rows when only the problems are wanted."""
        only = self.only_problems.isChecked()
        flagged = {
            item.number
            for item in self.reading.scanned.readings
            if item.kind is not ReadingKind.CLEAR or item.low_confidence
        }
        for number, row in self._rows.items():
            self.table.setRowHidden(row, only and number not in flagged)

    # ------------------------------------------------------------------
    # Outcome
    # ------------------------------------------------------------------
    def unanswered(self) -> tuple[int, ...]:
        """Questions still without a chosen answer."""
        return tuple(number for number in self._numbers if number not in self._answers)

    def answers(self) -> str:
        """The confirmed answers, one character per question.

        A question nobody has answered stays :data:`BLANK`, so the Answer Key
        stage reports it as missing rather than it arriving as a guess.
        """
        return "".join(self._answers.get(number, BLANK) for number in self._numbers)

    def target_set(self) -> str:
        """The *logical* set the answers are for.

        "As marked on the sheet" means the set the sheet's mark names - Set 10
        for a sheet marked ``A`` when Set 10 is printed as ``A``.
        """
        verdict = self.reading.verdict
        if verdict.check is SetCodeCheck.MISMATCH and self.sheet_set_radio.isChecked():
            return verdict.sheet_set
        return verdict.selected

    def set_decision(self) -> str:
        """How a set-code mismatch was resolved, for the revision's provenance."""
        verdict = self.reading.verdict
        if verdict.check is not SetCodeCheck.MISMATCH:
            return ""
        if self.sheet_set_radio.isChecked():
            return f"operator chose the sheet's set ({verdict.read_label})"
        if self.keep_set_radio.isChecked():
            return (
                f"operator kept the selected set ({verdict.selected}) over the "
                f"sheet's ({verdict.read_label})"
            )
        return ""

    def blockers(self) -> tuple[str, ...]:
        """Why the answers cannot be used yet, if they cannot."""
        found: list[str] = []
        if not self.reading.registered:
            found.append(
                "The sheet could not be registered, so it has no answers to "
                "import. Rescan it, or enter the key by hand."
            )
        if self.reading.verdict.check is SetCodeCheck.MISMATCH and not (
            self.keep_set_radio.isChecked() or self.sheet_set_radio.isChecked()
        ):
            found.append("Choose which set this sheet's answers are for.")
        return tuple(found)

    def _refresh(self) -> None:
        """Redraw the chips and say whether the answers can be used."""
        scanned = self.reading.scanned
        verdict = self.reading.verdict
        chips: list[tuple[str, str]] = [
            ("ok", "Registered") if scanned.registered else ("fail", "Registration failed"),
        ]
        if scanned.registered:
            clear = scanned.plan.question_count - len(scanned.unreadable)
            chips.append(("ok", f"{clear} clear"))
            if scanned.blanks:
                chips.append(("fail", f"{len(scanned.blanks)} blank"))
            if scanned.multiples:
                chips.append(("fail", f"{len(scanned.multiples)} multiple"))
            if scanned.low_confidence:
                chips.append(("warn", f"{len(scanned.low_confidence)} low confidence"))
        tone = {
            SetCodeCheck.MATCH: "ok",
            SetCodeCheck.BLANK: "warn",
            SetCodeCheck.MISMATCH: "fail",
            SetCodeCheck.UNREPRESENTABLE: "warn",
        }[verdict.check]
        chips.append(
            (
                tone,
                {
                    SetCodeCheck.MATCH: f"Sheet reads Set {verdict.read_label}",
                    SetCodeCheck.BLANK: "Set not marked",
                    SetCodeCheck.MISMATCH: f"Sheet reads Set {verdict.read_label}",
                    SetCodeCheck.UNREPRESENTABLE: "Set not confirmable",
                }[verdict.check],
            )
        )
        self.chips.set_chips(chips)

        blockers = self.blockers()
        open_questions = self.unanswered() if scanned.registered else ()
        self.accept_button.setEnabled(not blockers)
        if blockers:
            self.outcome_label.setText(" ".join(blockers))
            self.accept_button.setToolTip(" ".join(blockers))
        elif open_questions:
            shown = ", ".join(str(n) for n in open_questions[:10])
            more = "" if len(open_questions) <= 10 else f" (+{len(open_questions) - 10} more)"
            self.outcome_label.setText(
                f"{len(open_questions)} question(s) still have no answer: "
                f"{shown}{more}. They will show as missing in the editor, and the "
                "key cannot be saved until each has an answer or is marked "
                "full credit."
            )
            self.accept_button.setToolTip("")
        else:
            self.outcome_label.setText(
                f"Every question has one answer. The answers become an unsaved "
                f"draft for Set {self.target_set()}; nothing is stored until you "
                "save it as a new revision."
            )
            self.accept_button.setToolTip("")


__all__ = ["NO_ANSWER", "SOLUTION_TABLE_COLUMNS", "SolutionSheetDialog"]
