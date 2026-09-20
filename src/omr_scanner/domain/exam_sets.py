"""The examination Sets one project is divided into.

Purpose:
    Define what a project-level *Set* is - a named, described division of one
    examination, such as ``Set 10 - Name of Post: Assistant Engineer
    (Electrical)`` - and the rules that decide whether an operator's input
    describes a usable one.

Responsibilities:
    * :class:`ExamSet` - one set's stable identity, its operator-visible code
      and its description.
    * The validation rules for a set code, a set description and the
      examination name, as pure functions over strings.

What does NOT belong here:
    * Any database, file or Qt access. Persistence is
      :mod:`omr_scanner.services.project_sets`; presentation is
      :mod:`omr_scanner.gui.project_config_dialog`.
    * Translating a failure into a message for a dialog. These functions raise
      :class:`ValueError`; the service layer turns that into an
      :class:`~omr_scanner.errors.OMRScannerError` with a ``user_message``,
      exactly as :mod:`omr_scanner.services.project_service` already does for
      :class:`~omr_scanner.domain.project.ProjectMetadata`.

Why this is a separate concept from the ``set_code`` string the rest of the
application already passes around:
    Phases 8 and 9 identify a question paper by a bare ``set_code`` string -
    :class:`~omr_scanner.database.models.AnswerKeyRevision`,
    :class:`~omr_scanner.database.models.ReportTemplateAssociation` and
    :class:`~omr_scanner.database.models.GeneratedReport` all carry one, and
    recognition reads one off each sheet into
    :attr:`~omr_scanner.database.models.BatchScan.set_code_value`. Those are
    *references* to a set by the code printed on the paper. An
    :class:`ExamSet` is the project's own *definition* of that set: the
    registry entry that says which codes this examination actually uses and
    what each one means. The code string is deliberately the same value in
    both - ``"10"`` here is ``"10"`` there - so that a later phase can join
    them without a translation table, which is why
    :data:`MAX_SET_CODE_LENGTH` matches the ``String(32)`` those columns
    already use.

    Nothing in this phase changes those existing columns or adds a foreign
    key to them. This module only introduces the definition side; wiring the
    references to it belongs to the phase that needs it.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_SET_CODE_LENGTH = 32
"""Longest accepted set code.

Matches the ``String(32)`` that :class:`~omr_scanner.database.models.AnswerKeyRevision`
and the Phase 9 report tables already use for their own ``set_code`` columns, so
a code defined here can always be stored there unchanged."""

MAX_EXAM_NAME_LENGTH = 300
"""Longest accepted examination name.

Generous on purpose: a real title such as *"Recruitment Exam, Bangladesh
Submarine Cable Regulatory Authority"* is long, and an examination name is
free text shown on reports - not a file name, and therefore not subject to
:class:`~omr_scanner.domain.project.ProjectMetadata`'s folder-name rules."""


@dataclass(frozen=True, slots=True)
class ExamSet:
    """One division of an examination.

    Attributes:
        set_id: Stable internal identifier, generated once when the set is
            created and never reused or derived from the set's position in
            any list. This is what later phases link attendance, candidates,
            scans and results to; :attr:`code` is what a *person* sees, and a
            person may change it.
        code: Operator-visible code, as printed on the paper - ``"10"``,
            ``"A"``, ``"EEE-01"``. Unique within a project.
        description: Free text explaining what the set is, such as
            ``"Name of Post: Assistant Engineer (Electrical)"``. May be empty
            and may be long.
        display_order: Position in the operator's own ordering, ascending.
            Ties are broken by :attr:`code` so that a listing is always
            deterministic.
    """

    set_id: str
    code: str
    description: str = ""
    display_order: int = 0

    @property
    def display_label(self) -> str:
        """How this set is named in a sentence: ``"Set 10"``."""
        return f"Set {self.code}"


def normalise_exam_name(raw: str) -> str:
    """Return ``raw`` with accidental surrounding whitespace removed."""
    return raw.strip()


def validate_exam_name(raw: str) -> str:
    """Return the examination name to store, or raise.

    Args:
        raw: Whatever the operator typed.

    Returns:
        The trimmed name.

    Raises:
        ValueError: The name is blank or only whitespace, or longer than
            :data:`MAX_EXAM_NAME_LENGTH`.

    Deliberately *not* subject to the folder-name character rules
    :class:`~omr_scanner.domain.project.ProjectMetadata` applies to
    :attr:`~omr_scanner.domain.project.ProjectMetadata.name`: an examination
    name is a title that appears on a report, and titles legitimately contain
    characters such as ``:`` and ``/`` that a directory name cannot.
    """
    name = normalise_exam_name(raw)
    if not name:
        raise ValueError("The examination name must not be blank.")
    if len(name) > MAX_EXAM_NAME_LENGTH:
        raise ValueError(
            f"The examination name must be {MAX_EXAM_NAME_LENGTH} characters or fewer."
        )
    return name


def normalise_set_code(raw: str) -> str:
    """Return ``raw`` with accidental surrounding whitespace removed."""
    return raw.strip()


def validate_set_code(raw: str) -> str:
    """Return the set code to store, or raise.

    Args:
        raw: Whatever the operator typed.

    Returns:
        The trimmed code.

    Raises:
        ValueError: The code is blank, longer than
            :data:`MAX_SET_CODE_LENGTH`, or contains a control character.

    Uniqueness is *not* checked here - that needs the rest of the project,
    which this module deliberately knows nothing about. See
    :func:`find_conflicting_set`.

    Control characters (a newline pasted in from a spreadsheet, most
    realistically) are rejected rather than stripped: a code is matched
    exactly against what recognition reads off a sheet and against
    ``answer_key_revision.set_code``, and a value that *looks* like ``10``
    in a table but really ends in a line break would fail those comparisons
    for reasons invisible on screen.
    """
    code = normalise_set_code(raw)
    if not code:
        raise ValueError("A set code must not be blank.")
    if len(code) > MAX_SET_CODE_LENGTH:
        raise ValueError(f"A set code must be {MAX_SET_CODE_LENGTH} characters or fewer.")
    if any(character.isspace() and character != " " for character in code):
        raise ValueError("A set code must not contain tabs or line breaks.")
    if any(ord(character) < 32 or ord(character) == 127 for character in code):
        raise ValueError("A set code must not contain control characters.")
    return code


def normalise_description(raw: str) -> str:
    """Return a set description with surrounding whitespace removed.

    There is no length limit and no character restriction: a description is
    free text an operator writes for their own colleagues, and the column it
    is stored in is unbounded.
    """
    return raw.strip()


def find_conflicting_set(
    code: str,
    existing: tuple[ExamSet, ...],
    *,
    ignoring: str | None = None,
) -> ExamSet | None:
    """Return the set already using ``code``, if any.

    Args:
        code: The candidate code, already trimmed by :func:`validate_set_code`.
        existing: The project's current sets.
        ignoring: A :attr:`ExamSet.set_id` to exclude - the set being edited,
            which must not be reported as a conflict with itself.

    Returns:
        The conflicting set, or ``None`` when ``code`` is free.

    Comparison is exact, not case-folded: ``set_code`` is matched byte for
    byte everywhere else in the application (SQLite compares ``VARCHAR``
    case-sensitively by default, and recognition reports exactly what it
    read), so treating ``"a"`` and ``"A"`` as the same set here would
    disagree with every other layer.
    """
    for candidate in existing:
        if ignoring is not None and candidate.set_id == ignoring:
            continue
        if candidate.code == code:
            return candidate
    return None


__all__ = [
    "MAX_EXAM_NAME_LENGTH",
    "MAX_SET_CODE_LENGTH",
    "ExamSet",
    "find_conflicting_set",
    "normalise_description",
    "normalise_exam_name",
    "normalise_set_code",
    "validate_exam_name",
    "validate_set_code",
]
