"""Write, check and verify the answer key for each question-paper set.

    ┌──────────────────────────────────────────────────────────────┐
    │ Answer keys: 2 of 4 verified   [A ✓] [B ●] [C ○] [D ⚠]        │  every set
    ├──────────────────────────────────────────────────────────────┤
    │ Set [A▾]  Verified  Revision [Rev 2 ▾]  [Enter/Paste] [Read…] │  the chosen set
    │ Source · created · template · verification                    │
    ├──────────────────────────┬───────────────────────────────────┤
    │ answer sequence          │ Q | Key | Full credit | Note       │
    │ full-credit questions    │ (edit here, or in the sequence)    │
    │ checks                   │                                   │
    └──────────────────────────┴───────────────────────────────────┘
      Verification is recorded as: …     [Save as New Revision] [Verify]

Four rules the page is arranged around:

* **The template comes from the project.** Opening a project is enough: the
  page reads the active template through
  :func:`~omr_scanner.services.project_template.load_project_template`, never
  from whatever the Scan stage happens to have loaded.
* **The set is the primary object.** Every set the project defines is listed
  with its state in words, glyph and colour; choosing one loads that set's
  newest revision.
* **One authoritative model, two views.** The sequence box and the table show
  the same key; editing either rewrites the sequence and re-reads it through
  :mod:`omr_scanner.services.answer_key`.
* **Verification is a decision, not a formality.** It applies to a stored,
  complete, compatible revision - never to text on screen - and every reason it
  is unavailable is listed next to the button.

A key is the standard every candidate is measured against, so nothing here
repairs a key silently: every refusal names the question it is about.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.scoring import BLANK, MULTIPLE, AnswerKeySource, AnswerKeyStatus
from omr_scanner.errors import OMRScannerError
from omr_scanner.gui.answer_key.solution_dialog import SolutionSheetDialog
from omr_scanner.gui.icons import load_icon
from omr_scanner.gui.pages.base_page import WorkflowPage
from omr_scanner.gui.theme import (
    ANSWER_KEY_STAGE_STYLESHEET,
    VARIANT_PRIMARY,
    VARIANT_PROPERTY,
    Color,
    Spacing,
)
from omr_scanner.gui.theme.stylesheet import ANSWER_KEY_STATE_PROPERTY
from omr_scanner.services import project_sets, scoring_store
from omr_scanner.services.answer_key import (
    KeyDraft,
    QuestionPlan,
    ReadingKind,
    compatibility_issues,
    format_question_numbers,
    normalise_key_text,
    parse_wrong_questions,
    read_key,
)
from omr_scanner.services.project_template import (
    NO_TEMPLATE_MESSAGE,
    ProjectTemplate,
    TemplateAvailability,
    from_template,
    load_project_template,
)
from omr_scanner.services.scoring_store import SetKeyOverview, SetKeyState

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PySide6.QtCore import QAbstractItemModel, QModelIndex, QPersistentModelIndex
    from PySide6.QtWidgets import QStyleOptionViewItem

    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.gui.pages.catalog import WorkflowPageSpec
    from omr_scanner.services import ProjectDatabase, ProjectSession
    from omr_scanner.services.scoring_store import StoredKey
    from omr_scanner.services.solution_sheet import SolutionSheetReading

_LOGGER = logging.getLogger(__name__)

KEY_TABLE_COLUMNS: tuple[str, ...] = ("Q", "Key", "Full credit", "Note")
KEY_COLUMN = 1
FULL_CREDIT_COLUMN = 2
NOTE_COLUMN = 3
MISSING_ANSWER = "—"
NO_REVISION_TEXT = "No saved revision"

FULL_CREDIT_HELP = (
    "These questions receive full credit for every candidate, regardless of "
    "the marked response. Flagged per set: Set A and Set B need not agree."
)

STATE_GLYPHS: dict[str, str] = {
    "verified": "✓",
    "draft": "●",
    "missing": "○",
    "stale": "⚠",
    "unsaved": "✎",
    "invalid": "✕",
}
"""Each state is carried by a glyph *and* a word as well as by colour."""


class UnsavedChoice:
    """What the operator decided about unsaved edits."""

    SAVE = "save"
    DISCARD = "discard"
    CANCEL = "cancel"


@dataclass
class AnswerKeyPageState:
    """Everything the page is currently looking at."""

    session: ProjectSession | None = None
    template: OmrTemplate | None = None
    plan: QuestionPlan | None = None
    template_status: ProjectTemplate | None = None
    template_override: bool = False
    set_code: str = ""
    reviewer: str = ""
    draft: KeyDraft | None = None
    source: AnswerKeySource = AnswerKeySource.MANUAL
    source_scan: str = ""
    source_sha256: str = ""
    source_metadata: dict[str, Any] | None = None
    reading: SolutionSheetReading | None = None
    revisions: list[StoredKey] = field(default_factory=list)
    overview: dict[str, SetKeyOverview] = field(default_factory=dict)
    offered_codes: list[str] = field(default_factory=list)
    baseline: tuple[str, frozenset[int]] = ("", frozenset())
    """The answers and full-credit questions of what was last loaded or saved."""


class _AnswerDelegate(QStyledItemDelegate):
    """Edits the Key column with a choice of the template's option labels."""

    def __init__(self, page: AnswerKeyPage) -> None:
        super().__init__(page.table)
        self._page = page

    def createEditor(
        self,
        parent: QWidget,
        option: QStyleOptionViewItem,  # noqa: ARG002 - Qt's signature
        index: QModelIndex | QPersistentModelIndex,  # noqa: ARG002 - Qt's signature
    ) -> QWidget:
        """A combo of the option labels, plus "no answer"."""
        editor = QComboBox(parent)
        editor.setObjectName("answerKeyCellEditor")
        editor.addItem(MISSING_ANSWER, "")
        plan = self._page.state.plan
        for label in plan.labels if plan is not None else ():
            editor.addItem(label, label)
        return editor

    def setEditorData(
        self, editor: QWidget, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        """Start on the current answer, with the list already open."""
        if isinstance(editor, QComboBox):
            current = str(index.data() or "")
            found = editor.findData(current)
            editor.setCurrentIndex(found if found >= 0 else 0)
            editor.showPopup()

    def setModelData(
        self,
        editor: QWidget,
        model: QAbstractItemModel,  # noqa: ARG002 - Qt's signature
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        """Write the choice through the page, so the sequence is rewritten too."""
        if isinstance(editor, QComboBox):
            self._page.set_answer_at_row(index.row(), str(editor.currentData() or ""))


class _TableKeys(QObject):
    """Type an option label on a selected row to answer it; Delete clears it."""

    def __init__(self, page: AnswerKeyPage) -> None:
        super().__init__(page)
        self._page = page

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            return self._page.handle_table_key(event)
        return super().eventFilter(watched, event)


class AnswerKeyPage(WorkflowPage):
    """Enter, review and verify one answer key per question-paper set."""

    key_saved = Signal(int)
    """Emitted with the stored revision's id after a save."""

    key_verified = Signal(int)
    """Emitted with the stored revision's id after verification."""

    def __init__(self, spec: WorkflowPageSpec, parent: QWidget | None = None) -> None:
        super().__init__(spec, parent, expand=True, show_summary=False, compact=True)
        self.setStyleSheet(ANSWER_KEY_STAGE_STYLESHEET)
        self.state = AnswerKeyPageState()
        self._tiles: dict[str, QToolButton] = {}
        self._rebuilding = False
        self._shown_revision_id: int | None = None

        scroll = QScrollArea()
        scroll.setObjectName("answerKeyScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("answerKeyContent")
        # Below this the splitter would squeeze the table to a few rows; the
        # page scrolls instead, and the action bar below stays in view.
        content.setMinimumHeight(430)
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(Spacing.SM)
        column.addWidget(self._build_sets_bar())
        column.addWidget(self._build_set_box())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("answerKeySplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_editor())
        splitter.addWidget(self._build_table_panel())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        column.addWidget(splitter, stretch=1)
        scroll.setWidget(content)

        self.body.addWidget(scroll, stretch=1)
        self.body.addWidget(self._build_action_bar())

        self._update_enabled()
        self._revalidate()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("answerKeySectionHeading")
        return label

    def _build_sets_bar(self) -> QWidget:
        """Every set's key state, at a glance, and the way to choose one."""
        bar = QFrame()
        bar.setObjectName("answerKeySetsBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(Spacing.SM, Spacing.XS, Spacing.SM, Spacing.XS)
        layout.setSpacing(Spacing.SM)

        self.readiness_label = QLabel("")
        self.readiness_label.setObjectName("answerKeyReadinessLabel")
        self.readiness_label.setTextFormat(Qt.TextFormat.RichText)
        self.readiness_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.readiness_label)

        self.tile_scroll = QScrollArea()
        self.tile_scroll.setObjectName("answerKeySetTiles")
        self.tile_scroll.setWidgetResizable(True)
        self.tile_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tile_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tile_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        host = QWidget()
        host.setObjectName("answerKeySetTilesHost")
        # The strip sits on the bar's own surface, not on a grey viewport.
        self.tile_scroll.viewport().setAutoFillBackground(False)
        host.setStyleSheet("QWidget#answerKeySetTilesHost { background: transparent; }")
        self.tile_scroll.setStyleSheet("QScrollArea#answerKeySetTiles { background: transparent; }")
        self.tile_layout = QHBoxLayout(host)
        self.tile_layout.setContentsMargins(0, 0, 0, 0)
        self.tile_layout.setSpacing(Spacing.XS)
        self.tile_layout.addStretch(1)
        self.tile_scroll.setWidget(host)
        self.tile_group = QButtonGroup(self)
        self.tile_group.setExclusive(True)
        layout.addWidget(self.tile_scroll, stretch=1)
        return bar

    def _build_set_box(self) -> QWidget:
        """The chosen set: status, revision, source, and the two ways in."""
        box = QFrame()
        box.setObjectName("answerKeySetBox")
        grid = QGridLayout(box)
        grid.setContentsMargins(Spacing.SM, Spacing.SM, Spacing.SM, Spacing.SM)
        grid.setHorizontalSpacing(Spacing.SM)
        grid.setVerticalSpacing(Spacing.XS)

        set_label = QLabel("Set:")
        grid.addWidget(set_label, 0, 0)
        self.set_combo = QComboBox()
        self.set_combo.setObjectName("answerKeySetCombo")
        self.set_combo.setMinimumWidth(110)
        self.set_combo.setToolTip(
            "The question-paper set this key answers. Sets come from Project "
            "Configuration; codes may be more than one character - '10' and "
            "'X1' are as valid as 'A'."
        )
        self.set_combo.currentTextChanged.connect(self._on_set_changed)
        set_label.setBuddy(self.set_combo)
        grid.addWidget(self.set_combo, 0, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("answerKeyStatusLabel")
        self.status_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        grid.addWidget(self.status_label, 0, 2)

        revision_label = QLabel("Revision:")
        grid.addWidget(revision_label, 0, 3)
        self.revision_combo = QComboBox()
        self.revision_combo.setObjectName("answerKeyRevisionCombo")
        self.revision_combo.setMinimumWidth(220)
        self.revision_combo.setMaximumWidth(460)
        self.revision_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.revision_combo.setToolTip(
            "Every revision stored for this set, newest first. Choosing one "
            "shows it here without changing which revision Results uses. A "
            "revision is never edited - saving creates the next one."
        )
        self.revision_combo.currentIndexChanged.connect(self._on_revision_chosen)
        revision_label.setBuddy(self.revision_combo)
        grid.addWidget(self.revision_combo, 0, 4)

        # The slack goes between the revision and the two entry commands, so
        # the commands stay right-aligned and the combo stays readable.
        grid.setColumnStretch(5, 1)

        self.entry_button = QPushButton(load_icon("pencil"), "Enter / Paste Key")
        self.entry_button.setObjectName("enterKeyButton")
        self.entry_button.setToolTip(
            "Type or paste the answers, one per question - ABCD..., A B C D, "
            "A,B,C,D and line breaks all read the same."
        )
        self.entry_button.clicked.connect(self.start_manual_entry)
        grid.addWidget(self.entry_button, 0, 6)

        self.scan_button = QPushButton(load_icon("scan"), "Read Marked Solution Sheet...")
        self.scan_button.setObjectName("readKeyFromScanButton")
        self.scan_button.clicked.connect(self.prompt_read_from_scan)
        grid.addWidget(self.scan_button, 0, 7)

        self.provenance_label = QLabel("")
        self.provenance_label.setObjectName("answerKeyProvenanceLabel")
        self.provenance_label.setWordWrap(True)
        self.provenance_label.setTextFormat(Qt.TextFormat.RichText)
        self.provenance_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        grid.addWidget(self.provenance_label, 1, 0, 1, 8)
        return box

    def _build_editor(self) -> QWidget:
        """The sequence, the full-credit list, and the checks."""
        panel = QWidget()
        panel.setObjectName("answerKeyEditorPanel")
        panel.setMinimumWidth(300)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)

        heading = self._heading("Answer sequence")
        layout.addWidget(heading)
        self.key_edit = QPlainTextEdit()
        self.key_edit.setObjectName("answerKeyTextEdit")
        self.key_edit.setPlaceholderText(
            "Type or paste one answer per question, in question order - for "
            "example ABCDCBADAC..."
        )
        self.key_edit.setToolTip(
            "One answer per question. Spaces, tabs, line breaks, commas, "
            "semicolons and '|' are ignored so a key can be pasted from a "
            "spreadsheet; anything else is reported rather than dropped. '_' "
            "leaves a question unanswered."
        )
        self.key_edit.setTabChangesFocus(True)
        self.key_edit.setMaximumHeight(96)
        self.key_edit.setMinimumHeight(56)
        heading.setBuddy(self.key_edit)
        self.key_edit.textChanged.connect(self._revalidate)
        layout.addWidget(self.key_edit)

        self.count_label = QLabel("")
        self.count_label.setObjectName("answerKeyCountLabel")
        self.count_label.setWordWrap(True)
        layout.addWidget(self.count_label)

        credit_heading = self._heading("Full-credit questions")
        layout.addWidget(credit_heading)
        self.wrong_edit = QLineEdit()
        self.wrong_edit.setObjectName("wrongQuestionEdit")
        self.wrong_edit.setPlaceholderText("Question numbers, e.g. 17, 64")
        self.wrong_edit.setToolTip(
            FULL_CREDIT_HELP + " Whatever each candidate marked - including a "
            "blank or a multiple - scores full marks, with no deduction. Also "
            "set with the Full credit column of the table."
        )
        credit_heading.setBuddy(self.wrong_edit)
        self.wrong_edit.textChanged.connect(self._revalidate)
        layout.addWidget(self.wrong_edit)
        credit_help = QLabel(FULL_CREDIT_HELP)
        credit_help.setObjectName("answerKeyFullCreditHelp")
        credit_help.setWordWrap(True)
        credit_help.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
        layout.addWidget(credit_help)

        layout.addWidget(self._heading("Checks"))
        self.validation_label = QLabel("")
        self.validation_label.setObjectName("answerKeyValidationLabel")
        self.validation_label.setWordWrap(True)
        self.validation_label.setTextFormat(Qt.TextFormat.RichText)
        self.validation_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.validation_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.validation_label)
        layout.addStretch(1)
        return panel

    def _build_table_panel(self) -> QWidget:
        """The same key, question by question - the place to correct one answer."""
        panel = QWidget()
        panel.setObjectName("answerKeyTablePanel")
        panel.setMinimumWidth(340)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)

        header = QHBoxLayout()
        header.addWidget(self._heading("Questions"))
        hint = QLabel("Double-click a key, or select rows and type a choice. Tick to withdraw.")
        hint.setObjectName("answerKeyTableHint")
        hint.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
        hint.setWordWrap(True)
        header.addWidget(hint, stretch=1)
        layout.addLayout(header)

        self.table = QTableWidget(0, len(KEY_TABLE_COLUMNS))
        self.table.setObjectName("answerKeyTable")
        self.table.setHorizontalHeaderLabels(list(KEY_TABLE_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegateForColumn(KEY_COLUMN, _AnswerDelegate(self))
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(KEY_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(
            FULL_CREDIT_COLUMN, QHeaderView.ResizeMode.ResizeToContents
        )
        header_view.setSectionResizeMode(NOTE_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.table.setToolTip(
            "Double-click a Key cell to choose an answer, or select a row and "
            "type the option (the next row is selected). Delete clears it. "
            "Tick Full credit to withdraw a question for this set."
        )
        self.table.itemChanged.connect(self._on_table_item_changed)
        self._table_keys = _TableKeys(self)
        self.table.installEventFilter(self._table_keys)
        layout.addWidget(self.table, stretch=1)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("answerKeySummaryLabel")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.summary_label)
        return panel

    def _build_action_bar(self) -> QWidget:
        """Save and Verify, always in view, with why either is unavailable."""
        bar = QFrame()
        bar.setObjectName("answerKeyActionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(Spacing.SM, Spacing.XS, Spacing.SM, Spacing.XS)
        layout.setSpacing(Spacing.SM)

        text = QVBoxLayout()
        text.setSpacing(0)
        self.reviewer_label = QLabel("")
        self.reviewer_label.setObjectName("answerKeyReviewerLabel")
        self.reviewer_label.setWordWrap(True)
        text.addWidget(self.reviewer_label)
        self.action_hint_label = QLabel("")
        self.action_hint_label.setObjectName("answerKeyActionHint")
        self.action_hint_label.setWordWrap(True)
        self.action_hint_label.setStyleSheet(f"color: {Color.TEXT_SECONDARY};")
        text.addWidget(self.action_hint_label)
        layout.addLayout(text, stretch=1)

        self.save_button = QPushButton(load_icon("save"), "Save as New Revision")
        self.save_button.setObjectName("saveAnswerKeyButton")
        self.save_button.setProperty(VARIANT_PROPERTY, VARIANT_PRIMARY)
        self.save_button.clicked.connect(self.save_key)
        layout.addWidget(self.save_button)

        self.verify_button = QPushButton(load_icon("circle-check"), "Verify Answer Key")
        self.verify_button.setObjectName("verifyAnswerKeyButton")
        self.verify_button.clicked.connect(self.verify_key)
        layout.addWidget(self.verify_button)
        return bar

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------
    def on_project_changed(self, session: ProjectSession | None) -> None:
        """Adopt an opened project and its template, or clear when one closes."""
        changed = session is not self.state.session
        self.state.session = session
        if changed:
            # The editor belongs to the project it was typed in. Left filled,
            # the previous project's key - and the set it was written for -
            # would sit in the next project's editor as an unsaved draft, one
            # click from being saved as that project's key. The same project
            # re-announced (a renamed examination) keeps what is being typed.
            self.state.set_code = ""
            self.state.template_override = False
            self.state.offered_codes = []
            self._reset_source()
            self.state.baseline = ("", frozenset())
            self._set_editor("", frozenset())
        if not self.state.template_override:
            self._adopt_project_template()
        self._refresh_sets(load=changed)

    def refresh_project_template(self) -> None:
        """Re-read the project's template - after it was re-saved in place, say."""
        if self.state.template_override:
            return
        self._adopt_project_template()
        self._refresh_sets(load=False)

    def _adopt_project_template(self) -> None:
        session = self.state.session
        if session is None:
            self._apply_template_status(None)
            return
        self._apply_template_status(load_project_template(session.project))

    @property
    def database(self) -> ProjectDatabase | None:
        """The open project's database, or ``None``."""
        return self.state.session.database if self.state.session is not None else None

    def set_reviewer(self, name: str) -> None:
        """Adopt the configured reviewer name; the same person verifies a key."""
        self.state.reviewer = name.strip()
        self._update_enabled()

    def set_template(self, template: OmrTemplate | None) -> None:
        """Use ``template`` instead of the project's, until the project changes.

        For tools and tests that hold a template without a project that names
        it. The main window never calls this: the page reads the project.
        """
        self.state.template_override = True
        self._apply_template_status(
            from_template(template)
            if template is not None
            else ProjectTemplate(TemplateAvailability.NONE, message=NO_TEMPLATE_MESSAGE)
        )
        self._refresh_sets(load=False)

    def _apply_template_status(self, status: ProjectTemplate | None) -> None:
        self.state.template_status = status
        self.state.template = status.template if status is not None else None
        self.state.plan = status.plan if status is not None and status.is_ready else None

    # ------------------------------------------------------------------
    # Sets
    # ------------------------------------------------------------------
    def defined_set_codes(self) -> tuple[str, ...]:
        """The sets Project Configuration defines, in the operator's order."""
        database = self.database
        if database is None:
            return ()
        return tuple(item.code for item in project_sets.list_sets(database))

    def set_codes(self) -> tuple[str, ...]:
        """Every set this page offers.

        The project's defined sets first, in their order; then any set a key was
        stored for but is no longer defined, so an old key is never hidden;
        then codes a processed batch contained.
        """
        codes: list[str] = list(self.defined_set_codes())
        database = self.database
        stored = scoring_store.known_set_codes(database) if database is not None else ()
        for code in (*stored, *self.state.offered_codes):
            if code and code not in codes:
                codes.append(code)
        if self.state.set_code and self.state.set_code not in codes:
            codes.append(self.state.set_code)
        return tuple(codes)

    def offer_set_codes(self, codes: list[str]) -> None:
        """Add set codes seen in a batch to the chooser.

        Called by the main window once a batch has been reconciled, so an
        operator of a project without defined sets does not have to remember
        which papers were sat.
        """
        for code in sorted({item.strip().upper() for item in codes if item.strip()}):
            if code not in self.state.offered_codes:
                self.state.offered_codes.append(code)
        self._refresh_sets(load=not self.state.set_code)

    def _normalise_code(self, text: str) -> str:
        """A typed code, matched to a defined set's exact spelling when there is one."""
        code = text.strip()
        for known in self.set_codes():
            if known.upper() == code.upper():
                return known
        return code.upper()

    def _refresh_sets(self, *, load: bool) -> None:
        """Rebuild the chooser, the tiles and the readiness summary."""
        codes = self.set_codes()
        defined = self.defined_set_codes()
        if not self.state.set_code and codes:
            self.state.set_code = codes[0]
            load = True

        self.set_combo.blockSignals(True)
        self.set_combo.clear()
        # A project without defined sets (every project made before sets
        # existed) must still be able to name one; one with defined sets keys
        # exactly those, and a new set is added in Project Configuration.
        self.set_combo.setEditable(not defined)
        self.set_combo.addItems(list(codes))
        self.set_combo.setCurrentText(self.state.set_code)
        self.set_combo.blockSignals(False)

        self._refresh_overview()
        if load:
            self._load_set(self.state.set_code)
        else:
            self._refresh_revisions(select=self.current_revision_id())
            self._revalidate()

    def _refresh_overview(self) -> None:
        database = self.database
        codes = self.set_codes()
        self.state.overview = (
            {
                item.set_code: item
                for item in scoring_store.key_overview(database, codes, self.state.plan)
            }
            if database is not None
            else {}
        )
        self._rebuild_tiles(codes)

    def _rebuild_tiles(self, codes: tuple[str, ...]) -> None:
        for tile in list(self._tiles.values()):
            self.tile_group.removeButton(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._tiles = {}
        tallest = 0
        for index, code in enumerate(codes):
            tile = QToolButton()
            tile.setObjectName("answerKeySetTile")
            tile.setCheckable(True)
            tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            tile.clicked.connect(lambda _checked=False, c=code: self.select_set(c))
            self.tile_group.addButton(tile)
            self.tile_layout.insertWidget(index, tile)
            self._tiles[code] = tile
            tallest = max(tallest, tile.sizeHint().height())
        # One row of tiles plus room for the horizontal scroll bar a long set
        # list needs - no taller, so the bar never eats the table's height.
        bar = self.tile_scroll.horizontalScrollBar().sizeHint().height()
        self.tile_scroll.setFixedHeight(max(tallest, 28) + bar + 2)
        self._refresh_tile_states()

    def set_state(self, code: str) -> str:
        """The state a set is shown in: its stored state, or ``unsaved`` / ``invalid``."""
        if code == self.state.set_code and self.state.plan is not None and self.is_dirty():
            draft = self.state.draft
            has_errors = draft is not None and bool(draft.answers) and not draft.is_valid
            return "invalid" if has_errors else "unsaved"
        found = self.state.overview.get(code)
        return found.state.value if found is not None else SetKeyState.MISSING.value

    def _state_words(self, code: str) -> str:
        state = self.set_state(code)
        found = self.state.overview.get(code)
        words = {
            "verified": "Verified",
            "draft": "Draft",
            "missing": "Missing",
            "stale": "Incompatible",
            "unsaved": "Unsaved changes",
            "invalid": "Unsaved - has errors",
        }[state]
        if state == "verified" and found is not None and found.has_pending_draft:
            words = "Verified, newer draft"
        return f"{STATE_GLYPHS[state]} {words}"

    def _refresh_tile_states(self) -> None:
        verified = 0
        for code, tile in self._tiles.items():
            state = self.set_state(code)
            if state == "verified":
                verified += 1
            tile.setText(f"{code}   {self._state_words(code)}")
            tile.setProperty(ANSWER_KEY_STATE_PROPERTY, state)
            found = self.state.overview.get(code)
            tile.setToolTip(found.describe() if found is not None else f"Set {code}")
            tile.setAccessibleName(f"Set {code}: {self._state_words(code)[2:]}")
            tile.style().unpolish(tile)
            tile.style().polish(tile)
            tile.setChecked(code == self.state.set_code)
        total = len(self._tiles)
        if total == 0:
            self.readiness_label.setText("<b>Answer keys:</b> no sets yet")
            self.readiness_label.setToolTip(
                "Define the examination's sets in Project Configuration, or type "
                "a set code in the Set box."
            )
            return
        colour = Color.STATUS_READY if verified == total else Color.STATUS_BUSY
        self.readiness_label.setText(
            f"<b>Answer keys:</b> <span style='color:{colour}'>"
            f"{verified} of {total} verified</span>"
        )
        self.readiness_label.setToolTip("\n".join(self.readiness_issues()) or "Every set is ready.")

    def readiness_issues(self) -> tuple[str, ...]:
        """One sentence per set that Results could not mark with, in set order."""
        return tuple(
            item.describe()
            for code in self.set_codes()
            if (item := self.state.overview.get(code)) is not None and not item.is_ready
        )

    def select_set(self, code: str) -> bool:
        """Choose a set, as the tiles do. ``False`` when the change was cancelled."""
        self.set_combo.setCurrentText(code)
        return self.state.set_code == code

    def _on_set_changed(self, text: str) -> None:
        """Move to another set without losing unsaved edits."""
        code = self._normalise_code(text)
        previous = self.state.set_code
        if code == previous:
            return
        if self.is_dirty() and not self._retargets_new_draft(previous, code):
            choice = self.ask_unsaved(f"switch to Set {code}" if code else "switch sets")
            if choice == UnsavedChoice.CANCEL or (
                choice == UnsavedChoice.SAVE and not self.save_key()
            ):
                self.set_combo.blockSignals(True)
                self.set_combo.setCurrentText(previous)
                self.set_combo.blockSignals(False)
                self._refresh_tile_states()
                return
        carry = self._retargets_new_draft(previous, code)
        self.state.set_code = code
        if carry:
            # Naming the set of a key that has never been saved anywhere - the
            # code typed into an editable Set box, one character at a time. The
            # text is not tied to any stored revision, so keeping it is not
            # filing one set's key under another; it is finishing the label.
            self._refresh_revisions(select=None)
            self._revalidate()
            self._refresh_tile_states()
            return
        self._load_set(code)

    def _retargets_new_draft(self, previous: str, code: str) -> bool:
        """Whether moving ``previous`` -> ``code`` only relabels a never-saved key."""
        if not self.set_combo.isEditable():
            return False
        return not self._revisions_for(previous) and not self._revisions_for(code)

    def _revisions_for(self, code: str) -> tuple[StoredKey, ...]:
        database = self.database
        if database is None or not code:
            return ()
        return scoring_store.list_keys(database, set_code=code)

    def _load_set(self, code: str) -> None:
        """Show a set's newest revision - or an empty editor for a new key."""
        self.state.set_code = code
        self._refresh_revisions(select=None)
        latest = self.state.revisions[0] if self.state.revisions else None
        if latest is not None:
            self._show_revision(latest)
        else:
            self._reset_source()
            self.state.baseline = ("", frozenset())
            self._set_editor("", frozenset())
            self._revalidate()
        self._refresh_tile_states()

    # ------------------------------------------------------------------
    # Revisions
    # ------------------------------------------------------------------
    def _refresh_revisions(self, *, select: int | None) -> None:
        """List every stored revision for the current set, newest first."""
        self.state.revisions = list(self._revisions_for(self.state.set_code))
        self.revision_combo.blockSignals(True)
        self.revision_combo.clear()
        if not self.state.revisions:
            self.revision_combo.addItem(NO_REVISION_TEXT, -1)
        for stored in self.state.revisions:
            self.revision_combo.addItem(self._revision_text(stored), stored.key_id)
        index = self.revision_combo.findData(select) if select is not None else 0
        self.revision_combo.setCurrentIndex(max(index, 0))
        self.revision_combo.setEnabled(bool(self.state.revisions))
        self.revision_combo.blockSignals(False)

    @staticmethod
    def _revision_text(stored: StoredKey) -> str:
        status = {
            AnswerKeyStatus.VERIFIED: "✓ Verified",
            AnswerKeyStatus.DRAFT: "Draft",
            AnswerKeyStatus.SUPERSEDED: "Superseded",
        }[stored.key.status]
        # "revision N" is kept in the wording: it is what the rest of the
        # application (Results, reports, logs) calls a revision.
        return (
            f"revision {stored.revision} · {status} · "
            f"{_when(stored.created_at)}"
        )

    def current_revision_id(self) -> int | None:
        """The id of the revision the chooser shows, or ``None``."""
        key_id = self.revision_combo.currentData()
        return int(key_id) if key_id is not None and int(key_id) >= 0 else None

    def current_revision(self) -> StoredKey | None:
        """The stored revision the chooser has selected, if any."""
        key_id = self.current_revision_id()
        if key_id is None:
            return None
        return next((item for item in self.state.revisions if item.key_id == key_id), None)

    def _on_revision_chosen(self) -> None:
        """Show a stored revision - after asking about unsaved edits."""
        stored = self.current_revision()
        if stored is None:
            return
        if self.is_dirty():
            choice = self.ask_unsaved(f"show revision {stored.revision}")
            if choice == UnsavedChoice.CANCEL or (
                choice == UnsavedChoice.SAVE and not self.save_key()
            ):
                self._refresh_revisions(select=self._baseline_revision_id())
                self._revalidate()
                return
        self._show_revision(stored)

    def _baseline_revision_id(self) -> int | None:
        return self._shown_revision_id

    def _show_revision(self, stored: StoredKey) -> None:
        self._shown_revision_id = stored.key_id
        self.state.source = stored.key.source
        self.state.source_scan = stored.source_scan
        self.state.source_sha256 = stored.source_sha256
        self.state.source_metadata = dict(stored.source_metadata) or None
        self.state.reading = None
        self.state.baseline = (stored.key.answers, frozenset(stored.key.wrong_questions))
        self.revision_combo.blockSignals(True)
        self.revision_combo.setCurrentIndex(max(self.revision_combo.findData(stored.key_id), 0))
        self.revision_combo.blockSignals(False)
        self._set_editor(stored.key.answers, stored.key.wrong_questions)
        self._revalidate()

    def _reset_source(self) -> None:
        self._shown_revision_id = None
        self.state.source = AnswerKeySource.MANUAL
        self.state.source_scan = ""
        self.state.source_sha256 = ""
        self.state.source_metadata = None
        self.state.reading = None

    def _set_editor(self, answers: str, wrong: frozenset[int]) -> None:
        for editor in (self.key_edit, self.wrong_edit):
            editor.blockSignals(True)
        self.key_edit.setPlainText(answers)
        self.wrong_edit.setText(format_question_numbers(wrong))
        for editor in (self.key_edit, self.wrong_edit):
            editor.blockSignals(False)

    # ------------------------------------------------------------------
    # Dirty state
    # ------------------------------------------------------------------
    def is_dirty(self) -> bool:
        """Whether the editor holds something that is not the loaded revision."""
        cleaned, _ignored = normalise_key_text(self.key_edit.toPlainText())
        plan = self.state.plan
        wrong: frozenset[int] = frozenset()
        error = ""
        if plan is not None:
            wrong, error = parse_wrong_questions(self.wrong_edit.text(), plan)
        answers, baseline_wrong = self.state.baseline
        # An unreadable full-credit list is a change too: it is not what was
        # stored, whatever numbers could be salvaged from it.
        return cleaned != answers or wrong != baseline_wrong or bool(error)

    def is_restoring(self) -> bool:
        """Whether an older revision is on screen, unchanged - saving restores it."""
        stored = self.current_revision()
        return (
            stored is not None
            and bool(self.state.revisions)
            and stored.key_id != self.state.revisions[0].key_id
            and not self.is_dirty()
        )

    def unsaved_changes_summary(self) -> str:
        """One line naming unsaved edits, or ``""`` - for the window's close check."""
        if self.state.plan is None or not self.is_dirty():
            return ""
        return f"The answer key for Set {self.state.set_code or '(no set)'} has unsaved changes."

    def ask_unsaved(self, action: str) -> str:
        """Ask whether to save, discard or keep unsaved edits before ``action``."""
        answer = QMessageBox.question(
            self,
            "Unsaved answer key",
            f"The answer key for Set {self.state.set_code or '(no set)'} has "
            f"changes that have not been saved.\n\nSave them as a new revision "
            f"before you {action}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            return UnsavedChoice.SAVE
        if answer == QMessageBox.StandardButton.Discard:
            return UnsavedChoice.DISCARD
        return UnsavedChoice.CANCEL

    def resolve_unsaved(self, action: str) -> bool:
        """Save, discard or cancel before ``action``; ``False`` means stop."""
        if not self.unsaved_changes_summary():
            return True
        choice = self.ask_unsaved(action)
        if choice == UnsavedChoice.SAVE:
            return self.save_key()
        if choice == UnsavedChoice.DISCARD:
            answers, wrong = self.state.baseline
            self._set_editor(answers, wrong)
            self._revalidate()
            return True
        return False

    # ------------------------------------------------------------------
    # Manual entry and the table
    # ------------------------------------------------------------------
    def start_manual_entry(self) -> None:
        """Put the cursor in the sequence box, ready to type or paste over it."""
        self.key_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.key_edit.selectAll()

    def _current_characters(self) -> list[str]:
        cleaned, _ignored = normalise_key_text(self.key_edit.toPlainText())
        return list(cleaned)

    def set_answer(self, number: int, label: str) -> None:
        """Set one question's answer (``""`` clears it), rewriting the sequence."""
        plan = self.state.plan
        if plan is None or number not in plan.numbers:
            return
        offset = number - plan.first_question
        characters = self._current_characters()
        while len(characters) <= offset:
            characters.append(BLANK)
        characters[offset] = label.upper() if label else BLANK
        # Trailing blanks added only to reach this row say nothing the length
        # check does not already say; the ones before it are real gaps.
        self.key_edit.setPlainText("".join(characters))

    def set_answer_at_row(self, row: int, label: str) -> None:
        """Set the answer of the question shown on table row ``row``."""
        plan = self.state.plan
        if plan is not None and 0 <= row < plan.question_count:
            self.set_answer(plan.numbers[row], label)

    def set_full_credit(self, number: int, flagged: bool) -> None:
        """Withdraw a question for this set, or restore it."""
        plan = self.state.plan
        if plan is None:
            return
        current, _error = parse_wrong_questions(self.wrong_edit.text(), plan)
        updated = set(current) | {number} if flagged else set(current) - {number}
        self.wrong_edit.setText(format_question_numbers(updated))

    def handle_table_key(self, event: QKeyEvent) -> bool:
        """Answer the selected question by typing its option; ``True`` if handled."""
        plan = self.state.plan
        row = self.table.currentRow()
        if plan is None or row < 0 or self.table.state() == QAbstractItemView.State.EditingState:
            return False
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.set_answer_at_row(row, "")
            return True
        typed = event.text().strip().upper()
        if typed and typed in plan.labels:
            self.set_answer_at_row(row, typed)
            if row + 1 < self.table.rowCount():
                self.table.setCurrentCell(row + 1, KEY_COLUMN)
            return True
        return False

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._rebuilding or item.column() != FULL_CREDIT_COLUMN:
            return
        plan = self.state.plan
        if plan is None or not 0 <= item.row() < plan.question_count:
            return
        self.set_full_credit(
            plan.numbers[item.row()], item.checkState() == Qt.CheckState.Checked
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _revalidate(self) -> None:
        """Re-read the key and redraw every view of it."""
        plan = self.state.plan
        if plan is None:
            self.state.draft = None
            self.table.setRowCount(0)
            self.summary_label.setText("")
            self.count_label.setText("")
            status = self.state.template_status
            if self.state.session is None and status is None:
                message = "Open a project to write its answer keys."
            else:
                message = status.message if status is not None else NO_TEMPLATE_MESSAGE
            self._say(message, ok=None if status is None else False)
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
        self._describe_count(draft)
        self._describe(draft, wrong_error)
        self._update_enabled()

    def _rebuild_table(self, draft: KeyDraft) -> None:
        """Show the key question by question, keeping the operator's place."""
        plan = draft.plan
        readings = (
            {item.number: item for item in self.state.reading.scanned.readings}
            if self.state.reading is not None
            else {}
        )
        problems = {issue.question for issue in draft.issues if issue.question}
        self._rebuilding = True
        self.table.blockSignals(True)
        if self.table.rowCount() != plan.question_count:
            self.table.setRowCount(plan.question_count)
        attention = QColor(Color.ATTENTION_SOFT)
        for row, number in enumerate(plan.numbers):
            offset = number - plan.first_question
            answer = draft.answers[offset] if offset < len(draft.answers) else ""
            flagged = number in draft.wrong_questions
            shown = answer if answer and answer not in (BLANK,) else MISSING_ANSWER
            note = ""
            if not answer or answer == BLANK:
                note = "Withdrawn - no answer needed" if flagged else "No answer"
            elif answer == MULTIPLE:
                note = "Marked as multiple - choose one"
            elif answer not in plan.labels:
                note = f"Not an answer choice ({'/'.join(plan.labels)})"
            reading = readings.get(number)
            if reading is not None and reading.kind is not ReadingKind.CLEAR:
                read_note = f"Sheet: {reading.describe}"
                note = f"{read_note}; {note}" if note else f"{read_note} - corrected"
            elif reading is not None and reading.low_confidence:
                note = f"Sheet: low-confidence mark{'; ' + note if note else ''}"

            cells = (
                self._cell(row, 0, str(number)),
                self._cell(row, KEY_COLUMN, shown),
                self._cell(row, FULL_CREDIT_COLUMN, "Full credit" if flagged else ""),
                self._cell(row, NOTE_COLUMN, note),
            )
            number_item, key_item, credit_item, note_item = cells
            number_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            key_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEditable
            )
            key_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            font = key_item.font()
            font.setWeight(QFont.Weight.DemiBold)
            key_item.setFont(font)
            credit_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            credit_item.setCheckState(Qt.CheckState.Checked if flagged else Qt.CheckState.Unchecked)
            credit_item.setToolTip(
                "Withdrawn for this set: every scored candidate receives full "
                "credit, whatever they marked."
                if flagged
                else "Tick to give every candidate full credit for this question."
            )
            note_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            needs_attention = number in problems or (
                reading is not None
                and (reading.kind is not ReadingKind.CLEAR or reading.low_confidence)
            )
            for item in cells:
                item.setBackground(attention if needs_attention else QColor(0, 0, 0, 0))
        self.table.blockSignals(False)
        self._rebuilding = False

        grouped = " ".join(
            draft.answers[index : index + 10] for index in range(0, len(draft.answers), 10)
        )
        self.summary_label.setText(
            f"{plan.question_count} questions (Q{plan.first_question}-"
            f"Q{plan.numbers[-1]}), choices {'/'.join(plan.labels)}. Key, in tens: "
            f"<code>{grouped or '(empty)'}</code>"
        )

    def _cell(self, row: int, column: int, text: str) -> QTableWidgetItem:
        item = self.table.item(row, column)
        if item is None:
            item = QTableWidgetItem(text)
            self.table.setItem(row, column, item)
        elif item.text() != text:
            item.setText(text)
        return item

    def _describe_count(self, draft: KeyDraft) -> None:
        """"98 of 100 answers entered - Questions 99-100 are missing."""
        plan = draft.plan
        entered = sum(
            1 for character in draft.answers[: plan.question_count] if character in plan.labels
        )
        length = len(draft.answers)
        if length > plan.question_count:
            text = (
                f"{length} answers entered; the template defines "
                f"{plan.question_count} questions. Nothing beyond Question "
                f"{plan.numbers[-1]} is dropped - remove the extra ones."
            )
        elif length < plan.question_count:
            first_missing = plan.first_question + length
            last = plan.numbers[-1]
            span = (
                f"Question {first_missing} is"
                if first_missing == last
                else f"Questions {first_missing}-{last} are"
            )
            text = f"{entered} of {plan.question_count} answers entered. {span} missing."
        else:
            text = f"{entered} of {plan.question_count} answers entered."
        self.count_label.setText(text)

    def _describe(self, draft: KeyDraft, wrong_error: str) -> None:
        """The checks, as a list: what passes, and every problem by question."""
        plan = draft.plan
        lines: list[tuple[bool | None, str]] = []
        if wrong_error:
            lines.append((False, wrong_error))
        if not draft.answers:
            lines.append((None, "Type or paste the correct answers, one per question."))
        elif draft.is_valid:
            lines.append((True, f"All {plan.question_count} questions answered."))
            lines.append((True, f"Every answer is one of {'/'.join(plan.labels)}."))
            if draft.wrong_questions:
                lines.append(
                    (True, f"{len(draft.wrong_questions)} full-credit question(s): "
                     f"{format_question_numbers(draft.wrong_questions)}.")
                )
        else:
            for issue in draft.issues[:8]:
                lines.append((False, issue.message))
            if len(draft.issues) > 8:
                lines.append((False, f"...and {len(draft.issues) - 8} more."))
        stored = self.current_revision()
        if stored is not None and not self.is_dirty():
            problems = compatibility_issues(stored.key, plan)
            if problems:
                lines.extend((False, item) for item in problems)
            else:
                lines.append((True, "Compatible with the active template."))
        reading = self.state.reading
        if reading is not None:
            # Informational: a mismatch was already decided in the review
            # dialog, and a blank or unconfirmable set field does not block.
            decision = str((self.state.source_metadata or {}).get("set_code_decision", ""))
            lines.append(
                (None, reading.verdict.message + (f" Decision: {decision}." if decision else ""))
            )
        ok = not any(flag is False for flag, _text in lines) and draft.is_valid
        heading = (
            "<b>This answer key cannot be saved yet - "
            f"{len(draft.issues) + (1 if wrong_error else 0)} problem(s):</b>"
            if draft.answers and (not draft.is_valid or wrong_error)
            else ""
        )
        # Each line carries its own colour and glyph: a pass is green, a
        # problem red, a notice neutral - so a notice never reads as a pass.
        colours = {True: Color.STATUS_READY, False: Color.STATUS_ERROR, None: Color.TEXT_PRIMARY}
        body = "<br>".join(
            f"<span style='color:{colours[flag]}'>{_mark(flag)} {html.escape(text)}</span>"
            for flag, text in lines
        )
        self._say(
            (f"<span style='color:{Color.STATUS_ERROR}'>{heading}</span><br>" if heading else "")
            + body,
            ok=None,
        )
        self.validation_label.setProperty("checksPassed", ok)

    def _say(self, message: str, *, ok: bool | None) -> None:
        """Show the checks, coloured by outcome (the words carry it too)."""
        colour = {True: Color.STATUS_READY, False: Color.STATUS_ERROR, None: ""}[ok]
        self.validation_label.setStyleSheet(f"color: {colour};" if colour else "")
        self.validation_label.setText(message)

    # ------------------------------------------------------------------
    # Reading a key off a solution sheet
    # ------------------------------------------------------------------
    def prompt_read_from_scan(self) -> None:
        """Ask for a solution sheet, then recognise and review it."""
        # The project folder, as the other stages' choosers do - not the
        # process's working directory, which for an installed application is
        # nowhere an operator keeps scans.
        session = self.state.session
        start = str(session.project.layout.root) if session is not None else ""
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            f"Read Marked Solution Sheet for Set {self.state.set_code}",
            start,
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp)",
        )
        if chosen:
            self.read_from_scan(Path(chosen))

    def read_from_scan(self, path: Path) -> bool:
        """Recognise a solution sheet, review it, and load it as an unsaved draft.

        Runs the project's ordinary recognition engine and template - there is
        no second OMR pipeline here, and the sheet never becomes a candidate
        script. **The result is an unsaved draft, never a verified key**, and
        nothing is stored until **Save as New Revision**.
        """
        template = self.state.template
        plan = self.state.plan
        if template is None or plan is None:
            QMessageBox.information(
                self,
                "No usable template",
                (self.state.template_status.message if self.state.template_status else "")
                or NO_TEMPLATE_MESSAGE,
            )
            return False
        if not self.state.set_code:
            QMessageBox.information(
                self, "Choose a set", "Choose the set this solution sheet answers first."
            )
            return False
        try:
            from omr_scanner.services.solution_sheet import read_solution_sheet

            reading = read_solution_sheet(
                path,
                template,
                plan,
                selected_set=self.state.set_code,
                defined_sets=self.set_codes(),
            )
        except OMRScannerError as exc:
            QMessageBox.warning(
                self, "Solution sheet could not be read", exc.user_message or str(exc)
            )
            return False

        dialog = SolutionSheetDialog(reading, template, self)
        if not self._run_solution_dialog(dialog):
            return False
        return self.apply_solution_reading(
            reading,
            answers=dialog.answers(),
            target_set=dialog.target_set(),
            decision=dialog.set_decision(),
        )

    def _run_solution_dialog(self, dialog: SolutionSheetDialog) -> bool:
        """Show the review dialog; whether the operator accepted it."""
        return dialog.exec() == QDialog.DialogCode.Accepted

    def apply_solution_reading(
        self,
        reading: SolutionSheetReading,
        *,
        answers: str,
        target_set: str,
        decision: str = "",
    ) -> bool:
        """Put confirmed solution-sheet answers in the editor for ``target_set``."""
        if target_set != self.state.set_code:
            if not self.select_set(target_set):
                return False
        elif self.is_dirty() and not self.resolve_unsaved(
            "replace the editor with the solution sheet"
        ):
            return False
        self.state.reading = reading
        self.state.source = AnswerKeySource.SCANNED
        self.state.source_scan = reading.path.name
        self.state.source_sha256 = reading.sha256
        self.state.source_metadata = reading.metadata(
            target_set=target_set, final_answers=answers, decision=decision
        )
        self.key_edit.setPlainText(answers)
        self._refresh_tile_states()
        return True

    # ------------------------------------------------------------------
    # Saving and verifying
    # ------------------------------------------------------------------
    def save_key(self) -> bool:
        """Store the current draft as the next revision of this set's key."""
        database = self.database
        draft = self.state.draft
        if database is None or draft is None or self.save_blockers():
            return False
        metadata = self.state.source_metadata
        if self.state.reading is not None:
            metadata = self.state.reading.metadata(
                target_set=self.state.set_code,
                final_answers=draft.answers,
                decision=str((metadata or {}).get("set_code_decision", "")),
            )
        try:
            stored = scoring_store.save_key(
                database,
                draft.to_key(),
                source_scan=self.state.source_scan,
                created_by=self.state.reviewer,
                template=self.state.template,
                source_sha256=self.state.source_sha256,
                source_metadata=metadata,
            )
        except OMRScannerError as exc:
            QMessageBox.warning(self, "Answer key not saved", exc.user_message or str(exc))
            return False
        self._refresh_overview()
        self._refresh_revisions(select=stored.key_id)
        self._show_revision(stored)
        self._refresh_tile_states()
        self.key_saved.emit(stored.key_id)
        return True

    def verify_blockers(self) -> tuple[str, ...]:
        """Every reason the Verify button is unavailable, in words."""
        if self.database is None:
            return ("Open a project first.",)
        if self.state.plan is None:
            return ("The project has no usable template.",)
        stored = self.current_revision()
        found: list[str] = []
        draft = self.state.draft
        if stored is None:
            found.append(
                "Save the key as a revision first - verification applies to a "
                "stored revision."
            )
        elif self.is_dirty():
            found.append(
                "Save these edits as a new revision before verifying: verification "
                "locks a stored revision, not the text on screen."
            )
        else:
            if stored.key.status is AnswerKeyStatus.VERIFIED:
                found.append(f"Revision {stored.revision} is already verified.")
            if stored.key.status is AnswerKeyStatus.SUPERSEDED:
                found.append(f"Revision {stored.revision} was superseded and cannot be verified.")
            if self.state.revisions and stored.key_id != self.state.revisions[0].key_id:
                found.append(
                    f"A newer revision ({self.state.revisions[0].revision}) exists; "
                    "verify that one."
                )
            found.extend(compatibility_issues(stored.key, self.state.plan))
            if draft is not None and not draft.is_valid:
                found.extend(issue.message for issue in draft.issues[:3])
        if not self.state.reviewer:
            found.append("Set a reviewer name in File > Settings > Reviewer.")
        return tuple(found)

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
            if item.key.status is AnswerKeyStatus.VERIFIED and item.key_id != stored.key_id
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
                "Full-credit questions: "
                + format_question_numbers(stored.key.wrong_questions)
                + "\n"
                if stored.key.wrong_questions
                else ""
            )
            + "\nVerifying locks this revision and allows candidates to be "
            "marked against it." + warning,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        # Compared by value, never by identity. PySide6 (6.11) returns the
        # clicked button from the *real* QMessageBox.question as a plain int
        # (16384), which is equal to - but never *is* - StandardButton.Yes.
        # An identity check here rejected every real "Yes": the dialog closed
        # and the key silently stayed a draft. Tests that replaced `question`
        # with a function returning the enum member could not see it.
        if answer != QMessageBox.StandardButton.Yes:
            return False

        try:
            verified = scoring_store.verify_key(
                database, stored.key_id, verified_by=self.state.reviewer, plan=self.state.plan
            )
            # Announce success only for what is actually stored.
            persisted = scoring_store.get_key(database, stored.key_id)
        except OMRScannerError as exc:
            _LOGGER.exception(
                "Answer key verification failed: set=%s revision=%d",
                stored.set_code,
                stored.revision,
            )
            QMessageBox.warning(self, "Answer key not verified", exc.user_message or str(exc))
            self._refresh_overview()
            self._refresh_revisions(select=stored.key_id)
            self._revalidate()
            return False
        if persisted is None or persisted.key.status is not AnswerKeyStatus.VERIFIED:
            _LOGGER.error(
                "Answer key verification did not persist: set=%s revision=%d status=%s",
                stored.set_code,
                stored.revision,
                persisted.key.status.value if persisted is not None else "missing",
            )
            QMessageBox.critical(
                self,
                "Answer key not verified",
                f"Set {stored.set_code} revision {stored.revision} could not be "
                "recorded as verified. It remains a draft; nothing will be "
                "marked against it. Try again, and check File > Project Health "
                "if this repeats.",
            )
            self._refresh_overview()
            self._refresh_revisions(select=stored.key_id)
            self._revalidate()
            return False
        verified = persisted
        self._refresh_overview()
        self._refresh_revisions(select=verified.key_id)
        self._show_revision(verified)
        self._refresh_tile_states()
        self.key_verified.emit(verified.key_id)
        return True

    # ------------------------------------------------------------------
    # Enablement and the set header
    # ------------------------------------------------------------------
    def matches_editor(self, stored: StoredKey) -> bool:
        """Whether the editor still shows exactly what ``stored`` holds.

        Verification applies to a *stored revision*, not to the text on screen.
        An operator who edits the box and then presses Verify would otherwise
        lock the revision they had already saved and lose the edit without
        being told - and the key they believed they had checked would not be
        the key candidates are marked against.
        """
        draft = self.state.draft
        if draft is None:
            return False
        return (
            draft.answers == stored.key.answers
            and frozenset(draft.wrong_questions) == stored.key.wrong_questions
        )

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
        self.reviewer_label.setStyleSheet(f"color: {Color.STATUS_ERROR};")

    def save_blockers(self) -> tuple[str, ...]:
        """Every reason Save is unavailable, in words."""
        if self.database is None:
            return ("Open a project first.",)
        if self.state.plan is None:
            return ("The project has no usable template.",)
        draft = self.state.draft
        if draft is None or not draft.answers:
            return ("Enter the answers first.",)
        if not self.is_dirty() and not self.is_restoring():
            return ("Nothing to save - the editor shows the stored revision.",)
        if not draft.is_valid:
            return tuple(issue.message for issue in draft.issues[:3])
        if parse_wrong_questions(self.wrong_edit.text(), draft.plan)[1]:
            return ("Correct the full-credit question list.",)
        return ()

    def _update_enabled(self) -> None:
        """Enable only what the current state allows, and say why not."""
        has_plan = self.state.plan is not None
        has_set = bool(self.state.set_code)
        for widget in (self.key_edit, self.wrong_edit, self.table, self.entry_button):
            widget.setEnabled(has_plan)
        self.scan_button.setEnabled(has_plan and has_set and self.database is not None)
        if not has_plan:
            self.scan_button.setToolTip(
                "Create or load an active template on the Template stage before "
                "reading a solution sheet."
            )
        elif not has_set:
            self.scan_button.setToolTip("Choose the set this solution sheet answers first.")
        else:
            self.scan_button.setToolTip(
                f"Choose a scan or photo of a sheet marked with Set "
                f"{self.state.set_code}'s correct answers. It is read with the "
                "project's template and calibration, shown for review, and "
                "loaded as an unsaved draft - never as a candidate script, and "
                "never straight over a verified key."
            )

        save_reasons = self.save_blockers()
        verify_reasons = self.verify_blockers()
        self.save_button.setEnabled(not save_reasons)
        self.verify_button.setEnabled(not verify_reasons)
        self.save_button.setToolTip(
            "Store these answers as the next revision of this set's key, as an "
            "unverified draft. Existing revisions are never edited."
            if not save_reasons
            else "Cannot save:\n• " + "\n• ".join(save_reasons)
        )
        self.verify_button.setToolTip(
            "Lock this revision so candidates may be marked against it. Only a "
            "verified key can produce marks."
            if not verify_reasons
            else "Cannot verify:\n• " + "\n• ".join(verify_reasons)
        )
        hint = ""
        if has_plan and self.database is not None:
            if save_reasons and verify_reasons:
                hint = f"Cannot verify: {verify_reasons[0]}"
            elif not save_reasons:
                hint = "Unsaved changes - save them as a new revision."
            elif verify_reasons:
                hint = f"Cannot verify: {verify_reasons[0]}"
            else:
                hint = "Ready to verify."
        self.action_hint_label.setText(hint)
        self._refresh_reviewer_label()
        self._refresh_set_header()
        self._refresh_tile_states()

    def _refresh_set_header(self) -> None:
        """The chosen set's status in words, and where its key came from."""
        code = self.state.set_code
        if not code:
            self.status_label.setText("No set chosen")
            self.status_label.setProperty(ANSWER_KEY_STATE_PROPERTY, "missing")
            self.provenance_label.setText(
                "Define the examination's sets in Project Configuration, or type a set code."
                if self.database is not None
                else ""
            )
        else:
            state = self.set_state(code)
            self.status_label.setText(self._state_words(code))
            self.status_label.setProperty(ANSWER_KEY_STATE_PROPERTY, state)
            self.provenance_label.setText(self._provenance_text())
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _provenance_text(self) -> str:
        stored = self.current_revision()
        parts: list[str] = []
        dirty = self.is_dirty()
        if self.state.source is AnswerKeySource.SCANNED and self.state.source_scan:
            source = f"Marked solution sheet - {self.state.source_scan}"
            if self.state.source_sha256:
                source += f" (SHA-256 {self.state.source_sha256[:12]}...)"
        else:
            source = "Entered by hand" if self.key_edit.toPlainText().strip() else "Not entered yet"
        parts.append(f"<b>Source:</b> {source}")
        if stored is not None and not dirty:
            creator = stored.created_by or "creator not recorded"
            parts.append(
                f"<b>Saved:</b> revision {stored.revision}, "
                f"{_when(stored.created_at)} by {creator}"
            )
            if stored.template_name:
                parts.append(f"<b>Template:</b> {stored.template_name}")
            if stored.key.status is AnswerKeyStatus.VERIFIED:
                parts.append(
                    f"<span style='color:{Color.STATUS_READY}'>✓ Verified by "
                    f"{stored.verified_by or 'unknown'}, {_when(stored.verified_at)}</span>"
                )
            elif stored.key.status is AnswerKeyStatus.SUPERSEDED:
                parts.append("Superseded - kept because results may reference it")
            else:
                parts.append("Not verified")
        elif dirty:
            parts.append(
                f"<span style='color:{Color.STATUS_BUSY}'>✎ Unsaved changes</span>"
                + (f" (based on revision {stored.revision})" if stored is not None else "")
            )
        found = self.state.overview.get(self.state.set_code)
        if found is not None and found.verified is not None and (
            stored is None or stored.key_id != found.verified.key_id or dirty
        ):
            parts.append(f"Results use verified revision {found.verified.revision}")
        elif found is not None and found.verified is None and found.latest is not None:
            parts.append("Results cannot use this set until a revision is verified")
        return " · ".join(parts)


def _mark(flag: bool | None) -> str:
    return {True: "✓", False: "✕", None: "•"}[flag]


def _when(moment: datetime | None) -> str:
    """A stored instant in the operator's local time: ``29 Sep 2026 13:15``."""
    if moment is None:
        return "time not recorded"
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone().strftime("%d %b %Y %H:%M")


__all__ = ["KEY_TABLE_COLUMNS", "AnswerKeyPage", "AnswerKeyPageState", "UnsavedChoice"]
