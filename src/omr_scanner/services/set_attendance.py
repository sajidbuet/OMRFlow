"""One examination set's attendance workbook, and what it is used for.

Purpose:
    The single place that binds a :class:`~omr_scanner.domain.exam_sets.ExamSet`
    to the attendance file an operator chose for it - importing that file's
    candidates scoped to the set, and, when the file is a workbook, adopting
    it as the set's result template so the final report is built on the
    office's own layout rather than a generic one.

Responsibilities:
    * :func:`assign_attendance_workbook` - the one entry point the interface
      calls; everything else here supports it.
    * :func:`set_attendance_status` / :func:`attendance_overview` - what the
      Attendance and Reports screens read to show each set's state.

What does NOT belong here:
    * Reading a roster file (:mod:`omr_scanner.services.candidate_import`) or
      reading a template's columns
      (:mod:`omr_scanner.services.report_template`). This module orchestrates
      those; it parses nothing itself.
    * Reconciliation, scoring or generation. Assigning attendance says which
      candidates a set has, not what happened to them.
    * Qt.

Why the attendance workbook *is* the result template (§5):
    An examination office's attendance sheet already carries the institution
    name, the post, the column widths, the borders and the logo that the
    result must carry too. Generating a generic workbook and copying roll
    numbers into it would throw all of that away and ask somebody to rebuild
    it by hand. So an ``.xlsx`` attendance file is adopted as that set's
    template automatically, and generation starts by copying it.

    **A ``.csv`` attendance file cannot be**, and this module says so rather
    than pretending: a CSV has no fonts, no merged cells and no page setup to
    preserve. CSV remains fully usable for reconciliation (§16); a set whose
    attendance came from a CSV needs a workbook template chosen separately
    before a result can be generated, and
    :attr:`SetAttendanceStatus.template_blocker` is the message that says so.

    An ``.xlsx`` with no marks column cannot be adopted either - there would
    be nowhere to put a mark. That is reported the same explicit way instead
    of a column being invented in somebody's official stationery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.errors import OMRScannerError
from omr_scanner.services import project_sets, reconciliation_store, report_store
from omr_scanner.services.report_template import (
    ReportTemplateError,
    preview_template,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.exam_sets import ExamSet
    from omr_scanner.services.candidate_import import RosterValidation
    from omr_scanner.services.reconciliation_store import RosterSummary
    from omr_scanner.services.report_store import StoredTemplateAssociation

_LOGGER = logging.getLogger(__name__)

WORKBOOK_SUFFIXES = frozenset({".xlsx", ".xlsm"})
"""Attendance files that can also serve as a result template.

``.xls`` is absent deliberately: openpyxl cannot read the old binary format,
so :mod:`omr_scanner.services.candidate_import` cannot import one either."""

SOURCE_KIND_ATTENDANCE = "attendance"
SOURCE_KIND_MANUAL = "manual"

CSV_TEMPLATE_BLOCKER = (
    "This set's attendance came from a CSV, which carries no layout to build a "
    "result on. Choose an .xlsx result template for this set in the Reports "
    "stage before generating its result."
)

NO_MARKS_COLUMN_BLOCKER = (
    "This set's attendance workbook has no marks/total column, so there is "
    "nowhere to write a result. Add one to the workbook and assign it again, "
    "or choose a different result template for this set in the Reports stage."
)

AMBIGUOUS_TEMPLATE_BLOCKER = (
    "This set's attendance workbook has more than one column that could be the "
    "Roll No. or the marks column, so it was not adopted as the result "
    "template automatically. Choose it explicitly in the Reports stage."
)


class SetAttendanceError(OMRScannerError):
    """An attendance workbook could not be assigned to a set."""


@dataclass(frozen=True, slots=True)
class AttendanceAssignment:
    """What assigning an attendance file to a set actually did."""

    set_id: str
    set_code: str
    roster_id: int
    candidate_count: int
    source_name: str
    template_adopted: bool
    """Whether this file also became the set's result template."""
    template_blocker: str = ""
    """Why it did not, when it did not. Empty when it did, and never a
    silent nothing: a set whose result cannot yet be generated says why."""


@dataclass(frozen=True, slots=True)
class SetAttendanceStatus:
    """One set's attendance and template state, for a screen to render."""

    exam_set: ExamSet
    roster: RosterSummary | None
    association: StoredTemplateAssociation | None
    template_blocker: str = ""

    @property
    def has_attendance(self) -> bool:
        """Whether this set has an imported candidate list."""
        return self.roster is not None

    @property
    def candidate_count(self) -> int:
        """How many candidates this set's attendance declared."""
        return self.roster.candidate_count if self.roster is not None else 0

    @property
    def attendance_file(self) -> str:
        """The attendance file's name, or ``""``."""
        return self.roster.source_name if self.roster is not None else ""

    @property
    def template_file(self) -> str:
        """The result template's file name, or ``""``."""
        if self.association is None:
            return ""
        return Path(self.association.template_path).name

    @property
    def can_generate(self) -> bool:
        """Whether this set has both of the things a result needs."""
        return self.has_attendance and self.association is not None

    def describe(self) -> str:
        """A one-line status for a list or a label."""
        if not self.has_attendance:
            return "No attendance file assigned"
        listed = f"{self.candidate_count} candidate(s) imported"
        if self.association is None:
            return f"{listed} - no result template"
        return listed


def assign_attendance_workbook(
    database: ProjectDatabase,
    set_id: str,
    source_path: Path,
    validation: RosterValidation,
    *,
    imported_by: str = "",
    adopt_as_template: bool = True,
) -> AttendanceAssignment:
    """Make ``source_path`` this set's attendance list, and its template.

    Args:
        database: The open project database.
        set_id: The set this file belongs to - the persistent key, never the
            set's code and never a row position (§3).
        source_path: The file the operator chose. **Never modified**: it is
            read to import candidates, and later *copied* when a result is
            generated (§17).
        validation: The already-read roster
            (:func:`omr_scanner.services.candidate_import.read_roster`), so
            that the operator has confirmed its column mapping before
            anything is stored.
        imported_by: Who assigned it, for the audit trail.
        adopt_as_template: Whether an ``.xlsx`` should also become this set's
            result template. ``False`` leaves an existing template alone -
            for an operator who has deliberately chosen a separate one.

    Returns:
        What happened, including why the file was not adopted as a template
        when it was not.

    Raises:
        SetAttendanceError: No such set, or the roster could not be imported.
    """
    exam_set = project_sets.get_set(database, set_id)
    if exam_set is None:
        raise SetAttendanceError(
            f"No set with id {set_id!r}",
            user_message="That set no longer exists in this project.",
        )

    try:
        roster_id = reconciliation_store.import_roster(
            database, validation, imported_by=imported_by, set_id=set_id
        )
    except OMRScannerError as exc:
        raise SetAttendanceError(
            f"Roster import failed for set {exam_set.code}: {exc}",
            user_message=exc.user_message,
        ) from exc

    adopted = False
    blocker = ""
    if adopt_as_template:
        adopted, blocker = _adopt_as_result_template(
            database, exam_set, source_path, updated_by=imported_by
        )
    _LOGGER.info(
        "Attendance assigned: set=%s roster=%d candidates=%d template=%s",
        exam_set.code,
        roster_id,
        len(validation.candidates),
        "adopted" if adopted else f"not adopted ({blocker or 'not requested'})",
    )
    return AttendanceAssignment(
        set_id=set_id,
        set_code=exam_set.code,
        roster_id=roster_id,
        candidate_count=len(validation.candidates),
        source_name=source_path.name,
        template_adopted=adopted,
        template_blocker=blocker,
    )


def _adopt_as_result_template(
    database: ProjectDatabase,
    exam_set: ExamSet,
    source_path: Path,
    *,
    updated_by: str,
) -> tuple[bool, str]:
    """Try to make the attendance workbook this set's result template.

    Returns:
        ``(adopted, blocker)``. Never raises for a file that simply cannot
        serve as a template: that is an ordinary outcome an operator resolves,
        not a failure of the import that has already succeeded.
    """
    if source_path.suffix.casefold() not in WORKBOOK_SUFFIXES:
        return False, CSV_TEMPLATE_BLOCKER

    try:
        preview = preview_template(source_path)
    except ReportTemplateError as exc:
        return False, exc.user_message

    suggestion = preview.suggestion
    if suggestion.marks is None:
        return False, NO_MARKS_COLUMN_BLOCKER
    mapping = suggestion.to_mapping()
    if mapping is None:
        return False, AMBIGUOUS_TEMPLATE_BLOCKER

    try:
        report_store.associate_template(
            database,
            exam_set.code,
            source_path,
            mapping,
            sheet_name=preview.sheet,
            updated_by=updated_by,
            set_id=exam_set.set_id,
            source_kind=SOURCE_KIND_ATTENDANCE,
        )
    except OMRScannerError as exc:
        return False, exc.user_message
    return True, ""


def set_attendance_status(
    database: ProjectDatabase, exam_set: ExamSet
) -> SetAttendanceStatus:
    """Return one set's attendance and template state."""
    roster = reconciliation_store.active_roster(database, exam_set.set_id)
    association = report_store.get_template_association_for_set(
        database, exam_set.set_id, exam_set.code
    )
    blocker = ""
    if roster is not None and association is None:
        blocker = (
            CSV_TEMPLATE_BLOCKER
            if not roster.source_name.casefold().endswith((".xlsx", ".xlsm"))
            else NO_MARKS_COLUMN_BLOCKER
        )
    return SetAttendanceStatus(
        exam_set=exam_set,
        roster=roster,
        association=association,
        template_blocker=blocker,
    )


def attendance_overview(database: ProjectDatabase) -> tuple[SetAttendanceStatus, ...]:
    """Return every defined set's attendance state, in the operator's order.

    What the Attendance screen lists, and what the Reports screen uses to show
    one row per *defined* set - rather than only the sets that happen to have
    produced data already.
    """
    return tuple(
        set_attendance_status(database, exam_set)
        for exam_set in project_sets.list_sets(database)
    )


__all__ = [
    "AMBIGUOUS_TEMPLATE_BLOCKER",
    "CSV_TEMPLATE_BLOCKER",
    "NO_MARKS_COLUMN_BLOCKER",
    "SOURCE_KIND_ATTENDANCE",
    "SOURCE_KIND_MANUAL",
    "WORKBOOK_SUFFIXES",
    "AttendanceAssignment",
    "SetAttendanceError",
    "SetAttendanceStatus",
    "assign_attendance_workbook",
    "attendance_overview",
    "set_attendance_status",
]
