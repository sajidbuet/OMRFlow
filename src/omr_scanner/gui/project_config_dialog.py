"""The Project Configuration dialog: the examination name and its sets.

Purpose:
    One place where an operator says what this project *is* - the title of
    the examination, and the list of sets it is divided into - both when the
    project has just been created and at any point afterwards.

Responsibilities:
    * Present and save the examination name
      (:func:`omr_scanner.services.project_service.update_exam_name`).
    * Add, edit, delete and reorder sets
      (:mod:`omr_scanner.services.project_sets`).

What does NOT belong here:
    * Validation rules. Every rule lives in
      :mod:`omr_scanner.domain.exam_sets` and is applied by the service
      layer; this dialog only shows what the service refused and why.
    * Anything about attendance, scoring or reports. A set defined here is a
      definition; nothing downstream reads it yet.

Testability:
    The same split the rest of this application uses: a ``_prompt_*`` method
    owns a modal and contains no logic, and the method beside it
    (:meth:`save_exam_name`, :meth:`add_set`, :meth:`edit_set`,
    :meth:`delete_set`, :meth:`move_set`) does the work and returns whether
    it succeeded. GUI tests drive the second group, so no test has to
    dismiss a dialog - the defect class that split exists to prevent.

Why every action writes immediately instead of on an "OK" button:
    A set list is not a form to fill in and submit; it is a small registry an
    operator edits over time. Committing each change as it is made keeps this
    dialog's model of the project identical to the database's at every
    moment, means a crash while it is open cannot lose work, and removes the
    question of what "Cancel" would have to undo after five edits. The Health
    dialog already works this way for the same reasons.

Why a ``QTableWidget`` here, when the Scan page deliberately uses a lazy
model:
    That page shows one row per scanned sheet and has to survive 100,000 of
    them. This one shows one row per set an operator typed by hand: a large
    examination has tens, and the brief's own upper example is "50+". An
    item-based table is the simpler tool at that size, and the cost that
    forced the Scan page's conversion does not arise here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from omr_scanner.domain.exam_sets import ExamSet
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import project_sets, update_exam_name

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.services import ProjectSession

COLUMN_HEADERS = ("Set", "Description")

EXAM_NAME_PLACEHOLDER = "e.g. Recruitment Exam, Bangladesh Submarine Cable Regulatory Authority"

EXAM_NAME_SAVED_TEXT = "Examination name saved."

NO_SETS_TEXT = (
    "No sets defined yet. Add one for each question paper / post in this "
    "examination - for example Set 10, 'Name of Post: Assistant Engineer "
    "(Electrical)'."
)

READ_ONLY_TEXT = "This project is open read-only, so its configuration cannot be changed."

_OK_COLOR = "#1b7f3a"
_ERROR_COLOR = "#b3261e"


class SetEditorDialog(QDialog):
    """Ask for one set's code and description.

    Args:
        parent: Owning widget.
        code: Value to start from, when editing an existing set.
        description: Value to start from, when editing an existing set.
        title: Window title, which is what tells an operator whether they are
            adding or editing.

    Holds no project state and touches no service: it collects two strings.
    Whoever opened it validates them, because the rule that a code must be
    unique needs the whole project, which this dialog deliberately cannot
    see.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        code: str = "",
        description: str = "",
        title: str = "Add Set",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("setEditorDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(520, 140)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.code_edit = QLineEdit(code)
        self.code_edit.setObjectName("setCodeEdit")
        self.code_edit.setPlaceholderText("e.g. 10, A, EEE-01")
        self.code_edit.setToolTip(
            "The code printed on this set's papers. Must be unique within the project."
        )
        form.addRow("Set code:", self.code_edit)

        # A single line rather than a text box: a description is shown as one
        # row of a table and, later, on a report header, where an embedded
        # line break would be silently mangled rather than helpful. The field
        # itself is not length-limited.
        self.description_edit = QLineEdit(description)
        self.description_edit.setObjectName("setDescriptionEdit")
        self.description_edit.setPlaceholderText(
            "e.g. Name of Post: Assistant Engineer (Electrical)"
        )
        form.addRow("Description:", self.description_edit)
        layout.addLayout(form)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.setObjectName("setEditorButtonBox")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def values(self) -> tuple[str, str]:
        """Return the ``(code, description)`` currently entered, untrimmed.

        Trimming is the service layer's job, so that exactly one place
        decides what "blank" means.
        """
        return self.code_edit.text(), self.description_edit.text()


class ProjectConfigDialog(QDialog):
    """Edit the open project's examination name and its sets.

    Args:
        session: The open project session. Both halves of this dialog need
            it: the examination name lives in ``project.json`` and the sets
            live in the project database.
        parent: Optional Qt parent.
    """

    def __init__(self, session: ProjectSession, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("projectConfigDialog")
        self.setWindowTitle("Project Configuration")
        self.setModal(True)
        self.resize(720, 560)

        self._session = session
        self._sets: tuple[ExamSet, ...] = ()

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_exam_group())
        layout.addWidget(self._build_sets_group(), stretch=1)

        if session.read_only:
            banner = QLabel(READ_ONLY_TEXT)
            banner.setObjectName("projectConfigReadOnlyLabel")
            banner.setWordWrap(True)
            banner.setStyleSheet(f"color: {_ERROR_COLOR};")
            layout.addWidget(banner)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.setObjectName("projectConfigButtonBox")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.refresh_sets()
        self._apply_read_only()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _build_exam_group(self) -> QGroupBox:
        """Build the examination-details section."""
        box = QGroupBox("Examination")
        box.setObjectName("examDetailsGroup")
        layout = QVBoxLayout(box)

        form = QFormLayout()
        # The *stored* name, not `session.exam_name`, which falls back to the
        # project name for a project that predates this field. A field
        # pre-filled with that fallback would claim an examination name had
        # been set when none has; blank with a placeholder says the truth and
        # invites one.
        self.exam_name_edit = QLineEdit(self._session.project.metadata.exam_name)
        self.exam_name_edit.setObjectName("examNameEdit")
        self.exam_name_edit.setPlaceholderText(EXAM_NAME_PLACEHOLDER)
        self.exam_name_edit.setToolTip(
            "The title of this examination, as it should read on a report. "
            "This is not the project folder's name."
        )
        form.addRow("Exam name:", self.exam_name_edit)

        # Shown so it is obvious that the folder and the examination are two
        # different things, and that editing the field above does not rename
        # anything on disk.
        project_name_label = QLabel(self._session.name)
        project_name_label.setObjectName("projectFolderNameLabel")
        project_name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("Project folder:", project_name_label)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.save_exam_name_button = QPushButton("Save Exam Name")
        self.save_exam_name_button.setObjectName("saveExamNameButton")
        self.save_exam_name_button.clicked.connect(self.save_exam_name)
        row.addWidget(self.save_exam_name_button)

        self.exam_name_status_label = QLabel("")
        self.exam_name_status_label.setObjectName("examNameStatusLabel")
        self.exam_name_status_label.setWordWrap(True)
        row.addWidget(self.exam_name_status_label, stretch=1)
        layout.addLayout(row)
        return box

    def _build_sets_group(self) -> QGroupBox:
        """Build the sets editor."""
        box = QGroupBox("Sets")
        box.setObjectName("projectSetsGroup")
        layout = QVBoxLayout(box)

        self.sets_table = QTableWidget(0, len(COLUMN_HEADERS))
        self.sets_table.setObjectName("projectSetsTable")
        self.sets_table.setHorizontalHeaderLabels(list(COLUMN_HEADERS))
        self.sets_table.verticalHeader().setVisible(False)
        self.sets_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sets_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sets_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.sets_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.sets_table.itemSelectionChanged.connect(self._refresh_buttons)
        self.sets_table.doubleClicked.connect(self._prompt_edit_set)
        layout.addWidget(self.sets_table, stretch=1)

        self.sets_status_label = QLabel(NO_SETS_TEXT)
        self.sets_status_label.setObjectName("projectSetsStatusLabel")
        self.sets_status_label.setWordWrap(True)
        layout.addWidget(self.sets_status_label)

        row = QHBoxLayout()
        self.add_set_button = QPushButton("+ Add Set...")
        self.add_set_button.setObjectName("addSetButton")
        self.add_set_button.clicked.connect(self._prompt_add_set)
        row.addWidget(self.add_set_button)

        self.edit_set_button = QPushButton("Edit Set...")
        self.edit_set_button.setObjectName("editSetButton")
        self.edit_set_button.clicked.connect(self._prompt_edit_set)
        row.addWidget(self.edit_set_button)

        self.delete_set_button = QPushButton("Delete Set")
        self.delete_set_button.setObjectName("deleteSetButton")
        self.delete_set_button.clicked.connect(self._prompt_delete_set)
        row.addWidget(self.delete_set_button)

        row.addStretch(1)

        self.move_up_button = QPushButton("Move Up")
        self.move_up_button.setObjectName("moveSetUpButton")
        self.move_up_button.clicked.connect(lambda: self.move_selected_set(-1))
        row.addWidget(self.move_up_button)

        self.move_down_button = QPushButton("Move Down")
        self.move_down_button.setObjectName("moveSetDownButton")
        self.move_down_button.clicked.connect(lambda: self.move_selected_set(1))
        row.addWidget(self.move_down_button)
        layout.addLayout(row)

        # Only meaningful for a project that predates the set registry, so it
        # hides itself entirely rather than sitting there greyed out in every
        # new project for the rest of time.
        self.suggest_button = QPushButton("Add Sets Found in Existing Data...")
        self.suggest_button.setObjectName("suggestSetsButton")
        self.suggest_button.setToolTip(
            "This project already mentions set codes in its answer keys or "
            "scanned sheets. Add those codes as sets, for you to describe."
        )
        self.suggest_button.clicked.connect(self._prompt_adopt_suggestions)
        self.suggest_button.setVisible(False)
        layout.addWidget(self.suggest_button)
        return box

    # ------------------------------------------------------------------
    # Examination name
    # ------------------------------------------------------------------
    def save_exam_name(self) -> bool:
        """Save what the exam-name field currently holds. No dialog.

        Returns:
            Whether it was saved. A rejection is shown beside the field
            rather than in a message box: the operator is still editing, and
            a modal would take the keyboard away from the thing they have to
            fix.
        """
        try:
            metadata = update_exam_name(self._session, self.exam_name_edit.text())
        except OMRScannerError as exc:
            self._set_status(self.exam_name_status_label, exc.user_message, ok=False)
            return False
        self.exam_name_edit.setText(metadata.exam_name)
        self._set_status(self.exam_name_status_label, EXAM_NAME_SAVED_TEXT, ok=True)
        return True

    # ------------------------------------------------------------------
    # Sets
    # ------------------------------------------------------------------
    def refresh_sets(self) -> tuple[ExamSet, ...]:
        """Reload the set list from the database and redraw the table.

        Returns:
            The sets as stored, which is what a test should assert against -
            the table is only a rendering of this.
        """
        self._sets = project_sets.list_sets(self._session.database)
        self.sets_table.setRowCount(len(self._sets))
        for row, item in enumerate(self._sets):
            code_cell = QTableWidgetItem(item.code)
            # The stable identity travels with the row so that an action
            # never has to infer which set it means from a row number - the
            # thing the brief explicitly rules out as an identity.
            code_cell.setData(Qt.ItemDataRole.UserRole, item.set_id)
            code_cell.setToolTip(f"Internal id: {item.set_id}")
            self.sets_table.setItem(row, 0, code_cell)
            description_cell = QTableWidgetItem(item.description)
            description_cell.setToolTip(item.description)
            self.sets_table.setItem(row, 1, description_cell)
        self._refresh_buttons()
        self.suggest_button.setVisible(bool(self.suggested_codes()))
        if not self._sets:
            self._set_status(self.sets_status_label, NO_SETS_TEXT, ok=None)
        return self._sets

    @property
    def sets(self) -> tuple[ExamSet, ...]:
        """The sets this dialog last read from the database."""
        return self._sets

    def selected_set(self) -> ExamSet | None:
        """The set currently selected, or ``None``.

        Driven from the row's stored identifier rather than its position, and
        from ``currentRow()`` rather than ``currentItem()`` for the same
        typing reason the Health dialog documents.
        """
        row = self.sets_table.currentRow()
        if row < 0:
            return None
        cell = self.sets_table.item(row, 0)
        if cell is None:  # pragma: no cover - a populated row always has one
            return None
        set_id = cell.data(Qt.ItemDataRole.UserRole)
        return next((item for item in self._sets if item.set_id == set_id), None)

    def select_set(self, set_id: str) -> bool:
        """Select the row showing ``set_id``. Returns whether it was found."""
        for row, item in enumerate(self._sets):
            if item.set_id == set_id:
                self.sets_table.selectRow(row)
                return True
        return False

    def add_set(self, code: str, description: str = "") -> bool:
        """Define a new set. No dialog.

        Returns:
            Whether it was stored. A refusal - a blank or duplicate code - is
            reported beside the table, with the message the service produced.
        """
        try:
            created = project_sets.add_set(self._session.database, code, description)
        except OMRScannerError as exc:
            self._set_status(self.sets_status_label, exc.user_message, ok=False)
            return False
        self.refresh_sets()
        self.select_set(created.set_id)
        self._set_status(
            self.sets_status_label, f"Added {created.display_label}.", ok=True
        )
        return True

    def edit_set(
        self, set_id: str, code: str | None = None, description: str | None = None
    ) -> bool:
        """Change an existing set's code and/or description. No dialog."""
        try:
            updated = project_sets.update_set(
                self._session.database, set_id, code=code, description=description
            )
        except OMRScannerError as exc:
            self._set_status(self.sets_status_label, exc.user_message, ok=False)
            return False
        self.refresh_sets()
        self.select_set(updated.set_id)
        self._set_status(
            self.sets_status_label, f"Updated {updated.display_label}.", ok=True
        )
        return True

    def delete_set(self, set_id: str) -> bool:
        """Remove a set. No dialog, and no confirmation - see :meth:`_prompt_delete_set`."""
        try:
            project_sets.delete_set(self._session.database, set_id)
        except OMRScannerError as exc:
            self._set_status(self.sets_status_label, exc.user_message, ok=False)
            return False
        self.refresh_sets()
        self._set_status(self.sets_status_label, "Set deleted.", ok=True)
        return True

    def move_set(self, set_id: str, offset: int) -> bool:
        """Move one set up (``-1``) or down (``+1``) the list. No dialog."""
        try:
            project_sets.move_set(self._session.database, set_id, offset)
        except OMRScannerError as exc:
            self._set_status(self.sets_status_label, exc.user_message, ok=False)
            return False
        self.refresh_sets()
        self.select_set(set_id)
        return True

    def move_selected_set(self, offset: int) -> bool:
        """Move whichever set is selected. Returns ``False`` when none is."""
        current = self.selected_set()
        if current is None:
            return False
        return self.move_set(current.set_id, offset)

    def suggested_codes(self) -> tuple[str, ...]:
        """Set codes this project already mentions but has not defined.

        Empty for any project created since the set registry existed. For one
        that predates it, these come from what actually happened - an answer
        key that was entered, a set code recognition read off a sheet.
        """
        if self._session.read_only:
            return ()
        return project_sets.suggest_sets_from_existing_data(self._session.database)

    def adopt_suggested_codes(self) -> int:
        """Define a set for each suggested code. No dialog.

        Returns:
            How many were added.

        Each arrives with an **empty description**, deliberately: the data
        these codes came from records that a set was processed, never what
        the set was for. Inventing a description would be the application
        making something up; leaving it blank asks the operator for the one
        thing only they know.
        """
        added = 0
        for code in self.suggested_codes():
            try:
                project_sets.add_set(self._session.database, code)
            except OMRScannerError:
                # A code that cannot be adopted - malformed in the old data,
                # or defined a moment ago in another window - is skipped
                # rather than abandoning the rest of the list.
                continue
            added += 1
        self.refresh_sets()
        if added:
            self._set_status(
                self.sets_status_label,
                f"Added {added} set(s) from this project's existing data. "
                "Give each one a description.",
                ok=True,
            )
        return added

    # ------------------------------------------------------------------
    # Dialog-owning commands
    # ------------------------------------------------------------------
    def _prompt_add_set(self) -> None:
        """Ask for a code and description, then add the set."""
        editor = SetEditorDialog(self, title="Add Set")
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        code, description = editor.values()
        self.add_set(code, description)

    def _prompt_edit_set(self) -> None:
        """Ask for the selected set's new code and description, then save it."""
        current = self.selected_set()
        if current is None:
            return
        editor = SetEditorDialog(
            self,
            code=current.code,
            description=current.description,
            title=f"Edit {current.display_label}",
        )
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        code, description = editor.values()
        self.edit_set(current.set_id, code=code, description=description)

    def _prompt_adopt_suggestions(self) -> None:
        """Show which codes were found, then add them if the operator agrees."""
        codes = self.suggested_codes()
        if not codes:
            return
        listed = ", ".join(codes)
        answer = QMessageBox.question(
            self,
            "Add sets found in existing data",
            f"This project already mentions {len(codes)} set code(s) that are "
            f"not defined here:\n\n{listed}\n\n"
            "Add them as sets? They arrive without descriptions - only you "
            "know what each one was for. This list comes from work that was "
            "already done, so a set whose papers were never scanned will not "
            "appear in it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.adopt_suggested_codes()

    def _prompt_delete_set(self) -> None:
        """Confirm, then delete the selected set."""
        current = self.selected_set()
        if current is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete set",
            f"Delete {current.display_label}"
            f"{f' ({current.description})' if current.description else ''}?\n\n"
            "This removes the project's definition of the set. Answer keys, "
            "reports and scans that name the same code are not changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_set(current.set_id)

    # ------------------------------------------------------------------
    # Presentation
    # ------------------------------------------------------------------
    def _set_status(self, label: QLabel, text: str, *, ok: bool | None) -> None:
        """Write one status line, coloured by whether it reports a failure."""
        label.setText(text)
        if ok is None:
            label.setStyleSheet("")
        else:
            label.setStyleSheet(f"color: {_OK_COLOR if ok else _ERROR_COLOR};")

    def _refresh_buttons(self) -> None:
        """Enable only the actions that mean something right now."""
        if self._session.read_only:
            return
        has_selection = self.selected_set() is not None
        self.edit_set_button.setEnabled(has_selection)
        self.delete_set_button.setEnabled(has_selection)
        row = self.sets_table.currentRow()
        self.move_up_button.setEnabled(has_selection and row > 0)
        self.move_down_button.setEnabled(has_selection and row < len(self._sets) - 1)

    def _apply_read_only(self) -> None:
        """Disable every writing action when the project cannot be written to.

        Visibly disabled rather than failing on use: Phase 10 made this the
        rule for read-only sessions, so that nothing ever looks like it
        succeeded when the database would have refused it.
        """
        if not self._session.read_only:
            return
        self.exam_name_edit.setReadOnly(True)
        for button in (
            self.save_exam_name_button,
            self.add_set_button,
            self.edit_set_button,
            self.delete_set_button,
            self.move_up_button,
            self.move_down_button,
            self.suggest_button,
        ):
            button.setEnabled(False)


__all__ = [
    "COLUMN_HEADERS",
    "EXAM_NAME_SAVED_TEXT",
    "NO_SETS_TEXT",
    "ProjectConfigDialog",
    "SetEditorDialog",
]
