"""Write, check and verify the answer key for each question-paper set.

    ┌──────────────────────────────────────────────────────────────┐
    │ set chooser · revisions · source (typed / scanned)           │
    ├───────────────────────────────┬──────────────────────────────┤
    │ the key, as text              │ the key, question by question│
    │ wrong questions               │ (one model, two views)       │
    │ validation                    │                              │
    └───────────────────────────────┴──────────────────────────────┘

Two rules the page is arranged around:

* **One authoritative model, two views.** The text box and the table show the
  same key; editing either re-reads it through
  :mod:`omr_scanner.services.answer_key` and redraws both. Keeping two
  independently editable copies is how a key comes to disagree with itself.
* **Verification is a decision, not a formality.** The Verify button is
  disabled until validation passes, says who verified it, and warns that
  results computed under an earlier revision will go stale.

A key is the standard every candidate is measured against, so nothing here
repairs a key silently: every refusal names the question it is about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.scoring import AnswerKeySource, AnswerKeyStatus
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.services import scoring_store
from omr_scanner.services.answer_key import (
    AnswerKeyError,
    KeyDraft,
    QuestionPlan,
    parse_wrong_questions,
    plan_for,
    read_key,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import ProjectDatabase, ProjectSession
    from omr_scanner.services.scoring_store import StoredKey

_LOGGER = logging.getLogger(__name__)

KEY_TABLE_COLUMNS: tuple[str, ...] = ("Q", "Key", "Wrong question")


@dataclass
class AnswerKeyPageState:
    """Everything the page is currently looking at."""

    session: ProjectSession | None = None
    template: OmrTemplate | None = None
    plan: QuestionPlan | None = None
    set_code: str = ""
    reviewer: str = ""
    draft: KeyDraft | None = None
    source: AnswerKeySource = AnswerKeySource.MANUAL
    source_scan: str = ""
    revisions: list[StoredKey] = field(default_factory=list)


class AnswerKeyPage(WorkflowPage):
    """Enter, review and verify one answer key per question-paper set."""

    key_saved = Signal(int)
    """Emitted with the stored revision's id after a save."""

    key_verified = Signal(int)
    """Emitted with the stored revision's id after verification."""

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.state = AnswerKeyPageState()

        self.body.addWidget(self._build_set_bar())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("answerKeySplitter")
        splitter.addWidget(self._build_editor())
        splitter.addWidget(self._build_table_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.body.addWidget(splitter, stretch=1)

        self._update_enabled()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_set_bar(self) -> QWidget:
        """The set chooser, its revisions, and the two import commands."""
        box = QGroupBox("Question-paper set")
        box.setObjectName("answerKeySetBox")
        layout = QHBoxLayout(box)

        layout.addWidget(QLabel("Set:"))
        self.set_combo = QComboBox()
        self.set_combo.setObjectName("answerKeySetCombo")
        self.set_combo.setEditable(True)
        self.set_combo.setMinimumWidth(120)
        self.set_combo.setToolTip(
            "The question-paper set this key answers. Set codes may be more "
            "than one character - '10' and 'X1' are as valid as 'A'."
        )
        self.set_combo.currentTextChanged.connect(self._on_set_changed)
        layout.addWidget(self.set_combo)

        self.revision_combo = QComboBox()
        self.revision_combo.setObjectName("answerKeyRevisionCombo")
        self.revision_combo.setMinimumWidth(300)
        self.revision_combo.setToolTip(
            "Every revision ever stored for this set. A revision is never "
            "edited - correcting a verified key creates the next one."
        )
        self.revision_combo.currentIndexChanged.connect(self._on_revision_chosen)
        layout.addWidget(self.revision_combo, stretch=1)

        self.scan_button = QPushButton(load_icon("scan"), "Read From Solution Sheet...")
        self.scan_button.setObjectName("readKeyFromScanButton")
        self.scan_button.setToolTip(
            "Recognise a filled solution sheet with the loaded template and "
            "use its answers as a draft key."
        )
        self.scan_button.clicked.connect(self.prompt_read_from_scan)
        layout.addWidget(self.scan_button)
        return box

    def _build_editor(self) -> QWidget:
        """The key as text, the wrong-question list, and validation."""
        panel = QWidget()
        panel.setObjectName("answerKeyEditorPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        entry = QGroupBox("Answers")
        entry.setObjectName("answerKeyEntryBox")
        entry_layout = QVBoxLayout(entry)
        self.key_edit = QPlainTextEdit()
        self.key_edit.setObjectName("answerKeyTextEdit")
        self.key_edit.setPlaceholderText(
            "Type or paste one character per question, in question order - for "
            "example ABCBCCADBDAC..."
        )
        self.key_edit.setToolTip(
            "One character per question. Spaces, tabs, line breaks and commas "
            "are ignored so a key can be pasted from a spreadsheet; anything "
            "else is reported rather than dropped."
        )
        self.key_edit.textChanged.connect(self._revalidate)
        entry_layout.addWidget(self.key_edit)
        layout.addWidget(entry, stretch=1)

        wrong = QGroupBox("Wrong questions (full credit for everyone)")
        wrong.setObjectName("wrongQuestionBox")
        wrong_layout = QVBoxLayout(wrong)
        self.wrong_edit = QLineEdit()
        self.wrong_edit.setObjectName("wrongQuestionEdit")
        self.wrong_edit.setPlaceholderText("Question numbers, e.g. 17, 64")
        self.wrong_edit.setToolTip(
            "Questions withdrawn for this set. Every scored candidate receives "
            "the full mark for these whatever they marked - including a blank "
            "or a multiple - and no deduction is ever applied to them. Flagged "
            "per set: Set A and Set B need not agree."
        )
        self.wrong_edit.textChanged.connect(self._revalidate)
        wrong_layout.addWidget(self.wrong_edit)
        layout.addWidget(wrong)

        validation = QGroupBox("Validation")
        validation.setObjectName("answerKeyValidationBox")
        validation_layout = QVBoxLayout(validation)
        self.validation_label = QLabel("Load a template on the Scan stage to begin.")
        self.validation_label.setObjectName("answerKeyValidationLabel")
        self.validation_label.setWordWrap(True)
        self.validation_label.setTextFormat(Qt.TextFormat.RichText)
        validation_layout.addWidget(self.validation_label)
        layout.addWidget(validation)

        buttons = QHBoxLayout()
        self.save_button = QPushButton(load_icon("save"), "Save As New Revision")
        self.save_button.setObjectName("saveAnswerKeyButton")
        self.save_button.setToolTip(
            "Store these answers as the next revision of this set's key, as a "
            "draft. Existing revisions are never edited."
        )
        self.save_button.clicked.connect(self.save_key)
        buttons.addWidget(self.save_button)

        self.verify_button = QPushButton(load_icon("circle-check"), "Verify Answer Key")
        self.verify_button.setObjectName("verifyAnswerKeyButton")
        self.verify_button.setToolTip(
            "Lock this revision so candidates may be marked against it. Only a "
            "verified key can produce marks."
        )
        self.verify_button.clicked.connect(self.verify_key)
        buttons.addWidget(self.verify_button)
        layout.addLayout(buttons)

        self.reviewer_label = QLabel("")
        self.reviewer_label.setObjectName("answerKeyReviewerLabel")
        self.reviewer_label.setWordWrap(True)
        layout.addWidget(self.reviewer_label)
        return panel

    def _build_table_panel(self) -> QWidget:
        """The same key, question by question."""
        panel = QWidget()
        panel.setObjectName("answerKeyTablePanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, len(KEY_TABLE_COLUMNS))
        self.table.setObjectName("answerKeyTable")
        self.table.setHorizontalHeaderLabels(list(KEY_TABLE_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, stretch=1)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("answerKeySummaryLabel")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        return panel

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt an opened project, or clear everything when one closes."""
        self.state.session = session
        self.state.draft = None
        self.state.revisions = []
        self._refresh_sets()
        self._revalidate()
        self._update_enabled()

    @property
    def database(self) -> ProjectDatabase | None:
        """The open project's database, or ``None``."""
        return self.state.session.database if self.state.session is not None else None

    def set_reviewer(self, name: str) -> None:
        """Adopt the configured reviewer name; the same person verifies a key."""
        self.state.reviewer = name.strip()
        self._refresh_reviewer_label()

    def set_template(self, template: OmrTemplate | None) -> None:
        """Adopt the template whose questions the key must cover."""
        self.state.template = template
        self.state.plan = None
        if template is not None:
            try:
                self.state.plan = plan_for(template)
            except AnswerKeyError as exc:
                self._say(exc.user_message or str(exc), ok=False)
        self._revalidate()
        self._update_enabled()

    # ------------------------------------------------------------------
    # Sets and revisions
    # ------------------------------------------------------------------
    def _refresh_sets(self) -> None:
        """Offer every set a key exists for, plus whatever the batch produced."""
        database = self.database
        known: list[str] = []
        if database is not None:
            known = list(scoring_store.known_set_codes(database))
        current = self.state.set_code or (known[0] if known else "")

        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        self.set_combo.addItems(known)
        self.set_combo.setCurrentText(current)
        self.set_combo.blockSignals(False)
        self.state.set_code = current
        self._refresh_revisions()

    def offer_set_codes(self, codes: list[str]) -> None:
        """Add set codes seen in a batch to the chooser.

        Called by the main window once a batch has been reconciled, so an
        operator does not have to remember which papers were sat.
        """
        existing = {self.set_combo.itemText(i) for i in range(self.set_combo.count())}
        for code in sorted({item.strip().upper() for item in codes if item.strip()}):
            if code not in existing:
                self.set_combo.addItem(code)
        if not self.state.set_code and self.set_combo.count():
            self.set_combo.setCurrentIndex(0)

    def _on_set_changed(self, text: str) -> None:
        """Adopt a different set and show its revisions."""
        self.state.set_code = text.strip().upper()
        self._refresh_revisions()
        self._revalidate()

    def _refresh_revisions(self) -> None:
        """List every stored revision for the current set."""
        database = self.database
        self.state.revisions = []
        self.revision_combo.blockSignals(True)
        self.revision_combo.clear()
        self.revision_combo.addItem("New revision (not yet saved)", -1)
        if database is not None and self.state.set_code:
            self.state.revisions = list(
                scoring_store.list_keys(database, set_code=self.state.set_code)
            )
            for stored in self.state.revisions:
                self.revision_combo.addItem(stored.describe(), stored.key_id)
        self.revision_combo.blockSignals(False)
        self._update_enabled()

    def _on_revision_chosen(self) -> None:
        """Load a stored revision into the editor."""
        key_id = self.revision_combo.currentData()
        if key_id is None or key_id < 0:
            return
        stored = next(
            (item for item in self.state.revisions if item.key_id == key_id), None
        )
        if stored is None:
            return
        self.state.source = stored.key.source
        self.state.source_scan = stored.source_scan
        self.key_edit.blockSignals(True)
        self.key_edit.setPlainText(stored.key.answers)
        self.key_edit.blockSignals(False)
        self.wrong_edit.blockSignals(True)
        self.wrong_edit.setText(
            ", ".join(str(number) for number in sorted(stored.key.wrong_questions))
        )
        self.wrong_edit.blockSignals(False)
        self._revalidate()

    def current_revision(self) -> StoredKey | None:
        """The stored revision the chooser has selected, if any."""
        key_id = self.revision_combo.currentData()
        if key_id is None or key_id < 0:
            return None
        return next(
            (item for item in self.state.revisions if item.key_id == key_id), None
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _revalidate(self) -> None:
        """Re-read the key and redraw both views."""
        plan = self.state.plan
        if plan is None:
            self.state.draft = None
            self.table.setRowCount(0)
            self.summary_label.setText("")
            self._say(
                "No template is loaded. Load one on the <b>Scan</b> stage; an "
                "answer key has to know how many questions the paper has.",
                ok=None,
            )
            self._update_enabled()
            return

        wrong, wrong_error = parse_wrong_questions(self.wrong_edit.text(), plan)
        draft = read_key(
            self.key_edit.toPlainText(),
            plan,
            self.state.set_code,
            wrong_questions=sorted(wrong),
            source=self.state.source,
        )
        self.state.draft = draft
        self._rebuild_table(draft)
        self._describe(draft, wrong_error)
        self._update_enabled()

    def _rebuild_table(self, draft: KeyDraft) -> None:
        """Show the key question by question."""
        plan = draft.plan
        self.table.setRowCount(plan.question_count)
        for row, number in enumerate(plan.numbers):
            offset = number - plan.first_question
            answer = draft.answers[offset] if offset < len(draft.answers) else ""
            flagged = number in draft.wrong_questions
            values = (
                str(number),
                answer or "(missing)",
                "Yes - full credit" if flagged else "",
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 1 and not answer:
                    item.setToolTip("No answer has been given for this question.")
                if flagged:
                    item.setToolTip(
                        "Withdrawn for this set: every scored candidate "
                        "receives full credit, whatever they marked."
                    )
                self.table.setItem(row, column, item)
        self.summary_label.setText(
            f"{plan.question_count} question(s), answers "
            f"{'/'.join(plan.labels)}. Canonical key: "
            f"<code>{draft.answers or '(empty)'}</code>"
        )

    def _describe(self, draft: KeyDraft, wrong_error: str) -> None:
        """Say whether this key may be saved, and what is wrong if not."""
        if wrong_error:
            self._say(wrong_error, ok=False)
            return
        if not draft.answers:
            self._say(
                "Type or paste the correct answers, one character per question.",
                ok=None,
            )
            return
        if draft.is_valid:
            flagged = (
                f" {len(draft.wrong_questions)} question(s) flagged as wrong."
                if draft.wrong_questions
                else ""
            )
            ignored = (
                f" ({len(draft.ignored_characters)} separator(s) ignored.)"
                if draft.ignored_characters
                else ""
            )
            self._say(
                f"<b>Valid.</b> {draft.plan.question_count} answer(s) for set "
                f"<b>{draft.set_code}</b>.{flagged}{ignored}",
                ok=True,
            )
            return
        shown = draft.issues[:6]
        more = (
            ""
            if len(draft.issues) <= 6
            else f"<li>...and {len(draft.issues) - 6} more.</li>"
        )
        self._say(
            f"<b>This answer key cannot be saved yet - "
            f"{len(draft.issues)} problem(s):</b><ul>"
            + "".join(f"<li>{item.message}</li>" for item in shown)
            + more
            + "</ul>",
            ok=False,
        )

    def _say(self, message: str, *, ok: bool | None) -> None:
        """Show a validation message, coloured by outcome."""
        colour = {True: "#1b7f3b", False: "#a4262c", None: ""}[ok]
        self.validation_label.setStyleSheet(f"color: {colour};" if colour else "")
        self.validation_label.setText(message)

    # ------------------------------------------------------------------
    # Reading a key off a solution sheet
    # ------------------------------------------------------------------
    def prompt_read_from_scan(self) -> None:
        """Ask for a solution sheet, then recognise it."""
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Read Answer Key From Solution Sheet",
            "",
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)",
        )
        if chosen:
            self.read_from_scan(Path(chosen))

    def read_from_scan(self, path: Path) -> bool:
        """Recognise a solution sheet and load its answers as a draft.

        Runs the existing recognition engine - there is no second OMR pipeline
        here. **A recognised sheet is a draft, never a verified key**: blanks
        and double marks are reported and must be dealt with by a person before
        anything can be marked against it.
        """
        template = self.state.template
        plan = self.state.plan
        if template is None or plan is None:
            QMessageBox.information(
                self,
                "No template",
                "Load the template the solution sheet was printed from before "
                "reading a key off it.",
            )
            return False
        try:
            from omr_scanner.services import RecognitionEngine
            from omr_scanner.services.answer_key import key_from_scan

            result = RecognitionEngine().process(path, template)
            scanned = key_from_scan(result, plan)
        except OMRScannerError as exc:
            QMessageBox.warning(
                self,
                "Solution sheet could not be read",
                exc.user_message or str(exc),
            )
            return False

        self.state.source = AnswerKeySource.SCANNED
        self.state.source_scan = path.name
        self.key_edit.blockSignals(True)
        self.key_edit.setPlainText(scanned.answers)
        self.key_edit.blockSignals(False)
        if scanned.set_code and not self.state.set_code:
            self.set_combo.setCurrentText(scanned.set_code)
        self._revalidate()

        if not scanned.is_clean:
            QMessageBox.warning(
                self,
                "Solution sheet needs review",
                f"{scanned.summary}\n\n"
                "An answer key must give exactly one correct choice for every "
                "question. Correct the highlighted questions before verifying "
                "this key - recognition completing does not make a key right.",
            )
        return True

    # ------------------------------------------------------------------
    # Saving and verifying
    # ------------------------------------------------------------------
    def save_key(self) -> bool:
        """Store the current draft as the next revision of this set's key."""
        database = self.database
        draft = self.state.draft
        if database is None or draft is None or not draft.is_valid:
            return False
        try:
            stored = scoring_store.save_key(
                database, draft.to_key(), source_scan=self.state.source_scan
            )
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Answer key not saved", exc.user_message or str(exc)
            )
            return False
        self._refresh_sets()
        self.set_combo.setCurrentText(stored.set_code)
        index = self.revision_combo.findData(stored.key_id)
        if index >= 0:
            self.revision_combo.setCurrentIndex(index)
        self.key_saved.emit(stored.key_id)
        self._update_enabled()
        return True

    def verify_key(self) -> bool:
        """Lock the selected revision so candidates may be marked against it."""
        database = self.database
        stored = self.current_revision()
        if database is None or stored is None:
            QMessageBox.information(
                self,
                "Save the key first",
                "Save this key as a revision before verifying it.",
            )
            return False

        replaced = [
            item
            for item in self.state.revisions
            if item.key.status is AnswerKeyStatus.VERIFIED
            and item.key_id != stored.key_id
        ]
        warning = (
            "\n\nResults already computed under revision "
            f"{replaced[0].revision} will be marked as needing recomputation."
            if replaced
            else ""
        )
        answer = QMessageBox.question(
            self,
            "Verify this answer key?",
            f"{stored.describe()}\n\n"
            f"Answers: {stored.key.answers}\n"
            + (
                "Wrong questions: "
                + ", ".join(str(n) for n in sorted(stored.key.wrong_questions))
                + "\n"
                if stored.key.wrong_questions
                else ""
            )
            + "\nVerifying locks this revision and allows candidates to be "
            "marked against it." + warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return False

        try:
            scoring_store.verify_key(
                database, stored.key_id, verified_by=self.state.reviewer
            )
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Answer key not verified", exc.user_message or str(exc)
            )
            return False
        self._refresh_revisions()
        index = self.revision_combo.findData(stored.key_id)
        if index >= 0:
            self.revision_combo.setCurrentIndex(index)
        self.key_verified.emit(stored.key_id)
        self._update_enabled()
        return True

    # ------------------------------------------------------------------
    # Enablement
    # ------------------------------------------------------------------
    def _refresh_reviewer_label(self) -> None:
        """Say who a verification will be recorded as."""
        if self.state.reviewer:
            self.reviewer_label.setText(
                f"Verification is recorded as: <b>{self.state.reviewer}</b>"
            )
            self.reviewer_label.setStyleSheet("")
            return
        self.reviewer_label.setText(
            "No reviewer name is set. Add one in File > Settings > Reviewer - "
            "an answer key cannot be verified without a name."
        )
        self.reviewer_label.setStyleSheet("color: #a4262c;")

    def _update_enabled(self) -> None:
        """Enable only what the current state allows."""
        has_project = self.database is not None
        has_plan = self.state.plan is not None
        draft = self.state.draft
        stored = self.current_revision()

        self.scan_button.setEnabled(has_plan)
        self.key_edit.setEnabled(has_plan)
        self.wrong_edit.setEnabled(has_plan)
        self.save_button.setEnabled(
            has_project and draft is not None and draft.is_valid
        )
        self.verify_button.setEnabled(
            has_project
            and stored is not None
            and stored.key.status is not AnswerKeyStatus.VERIFIED
            and stored.key.status is not AnswerKeyStatus.SUPERSEDED
        )
        self._refresh_reviewer_label()
