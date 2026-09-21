"""Read a candidate/attendance roster from a CSV or Excel file.

Purpose:
    Turn a spreadsheet an examination office already has into
    :class:`~omr_scanner.domain.reconciliation.CandidateRecord` values, telling
    the operator exactly what is wrong when it cannot - and refusing to guess
    when guessing could file one candidate's paper under another's name.

Scope:
    Reading, normalising, validating. No database, no Qt. The result of
    :func:`read_roster` is data; storing it is
    :mod:`omr_scanner.services.reconciliation_store`'s job.

Two rules this module exists to enforce:

1. **A candidate ID is an identifier, never a quantity.** Excel stores
   ``15000001`` as a number and will happily hand it back as ``15000001.0``;
   writing that into a roster would mean no scanned sheet ever matched it
   again. :func:`normalise_candidate_id` is the single place that is dealt
   with.
2. **Ambiguity is reported, never resolved silently.** Two columns that both
   look like candidate IDs, or a roster with the same ID twice, stop the
   import with a message naming the problem. A roster is the mapping from
   paper to person; a wrong guess here is a candidate's result attached to
   someone else.

Privacy:
    Nothing in this module logs a candidate ID, a name or a marks value. Log
    lines carry counts and *source row numbers*, which identify a line in a
    file the operator is already looking at without identifying a person. See
    ``docs/ARCHITECTURE.md`` ("Logging and privacy").
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omr_scanner.domain.reconciliation import (
    AttendanceState,
    CandidateRecord,
    is_absent_token,
)
from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_LOGGER = logging.getLogger(__name__)

CSV_SUFFIXES: frozenset[str] = frozenset({".csv"})
EXCEL_SUFFIXES: frozenset[str] = frozenset({".xlsx", ".xlsm"})
SUPPORTED_SUFFIXES: frozenset[str] = CSV_SUFFIXES | EXCEL_SUFFIXES
"""What the importer reads.

``.xls`` - the pre-2007 binary format - is deliberately absent: the phase brief
does not require it and the only way to support it is another dependency for a
format Excel itself has discouraged for fifteen years. A user with one is told
to save it as ``.xlsx``, which Excel does in two clicks.
"""

PREVIEW_ROW_LIMIT = 50
"""How many data rows :func:`preview_roster` reads.

Enough to see the shape of a file and to spot a header that is not on row 1,
few enough that previewing a 50,000-row workbook is instant.
"""

_SAMPLE_PACKAGE = "omr_scanner.resources.templates"
_SAMPLE_FILENAME = "candidate_attendance_sample.xlsx"


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------
class CandidateImportError(OMRScannerError):
    """A roster file could not be read, or could not be read unambiguously.

    Always carries a ``user_message`` that says what to do next. "Import
    failed" tells an operator nothing they did not already know.
    """


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------
def normalise_candidate_id(value: object) -> str:
    """Return a candidate ID as a stable identifier string.

    Args:
        value: The cell as the reader produced it - ``str``, ``int``,
            ``float``, ``None``.

    Returns:
        The identifier, stripped of surrounding whitespace. ``""`` for a blank
        cell.

    The whole-number float case is the one that matters. Excel stores an
    8-digit roll number as a number, openpyxl may hand it back as
    ``15000001.0``, and ``str()`` would produce an ID that matches no scanned
    sheet ever again. A float that is *not* whole is left alone rather than
    rounded, because silently turning ``12.5`` into ``12`` or ``13`` is the
    kind of transformation that makes two candidates one.

    Text is never reinterpreted: ``"0015"`` stays ``"0015"``, and an
    alphanumeric ID passes through untouched.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # A boolean cell is not an identifier. Keep the text rather than
        # producing "1"/"0", which could collide with a real roll number.
        return str(value).strip()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
            return ""
        if value.is_integer() and abs(value) < 2**53:
            return str(int(value))
        return repr(value).strip()
    return str(value).strip()


def attendance_from_cell(value: object) -> AttendanceState:
    """Read a marks/attendance cell as an attendance state.

    ``ABSENT``/``ABS`` in any case, with any surrounding whitespace, means
    absent. **Everything else means not-marked-absent**, including a blank
    cell - which is the semantics of a marks sheet, where a candidate who sat
    the paper has a mark and one who did not has the word instead. A blank is
    therefore "no absence was recorded", not "no information".
    """
    return AttendanceState.ABSENT if is_absent_token(value) else AttendanceState.PRESENT


def _clean_text(value: object) -> str:
    """Return a trimmed display string for a cell."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


# ----------------------------------------------------------------------
# Column mapping
# ----------------------------------------------------------------------
ID_HEADERS: tuple[str, ...] = (
    "roll no.", "roll no", "roll number", "rollno", "roll",
    "candidate id", "candidate no", "candidate number", "candidate",
    "student id", "student no", "student number",
    "registration no", "registration number", "reg no", "reg. no.",
    "exam roll", "id",
)
"""Column headers recognised as a candidate identifier.

Public (not ``_ID_HEADERS``) because :mod:`omr_scanner.services.report_template`
(Phase 9) recognises the same identifier column in a result template and must
agree with the roster importer about what "looks like a Roll No." means - two
different answers to the same question would let a candidate match one file
and not the other.
"""
_ID_HEADERS = ID_HEADERS
"""Backward-compatible alias for the name this module used before Phase 9."""

NAME_HEADERS: tuple[str, ...] = (
    "candidate name", "student name", "name of candidate", "name",
)
"""Column headers recognised as a candidate's name. Public for the same
reason as :data:`ID_HEADERS`."""
_NAME_HEADERS = NAME_HEADERS
_ATTENDANCE_HEADERS: tuple[str, ...] = (
    "attendance", "status", "result", "marks", "mark", "score",
    "total", "obtained", "total marks",
)
"""Headers the importer offers to map automatically.

Ordered most specific first, because ``"id"`` matches ``"Sl.No."``-adjacent
columns in too many real files to be trusted ahead of ``"Roll No."``.

The marks/attendance list deliberately does **not** contain ``"total (90)"``.
Hard-coding the supplied sample's column name would work for exactly one
examination; ``"total"`` matches it by prefix and every other paper's total as
well.
"""


@dataclass(frozen=True, slots=True)
class ColumnMapping:
    """Which source column holds what.

    Attributes:
        candidate_id: Required. The 0-based column index holding roll numbers.
        name: Optional column holding candidate names.
        attendance: Optional marks/attendance column. When ``None``, the roster
            says nothing about who was expected, and every candidate's
            attendance is :attr:`~omr_scanner.domain.reconciliation.AttendanceState.UNKNOWN`.
    """

    candidate_id: int
    name: int | None = None
    attendance: int | None = None

    def validate(self, column_count: int) -> None:
        """Raise if any mapped index is outside the file's columns."""
        for label, index in (
            ("Candidate ID", self.candidate_id),
            ("Candidate Name", self.name),
            ("Marks / Attendance", self.attendance),
        ):
            if index is None:
                continue
            if not 0 <= index < column_count:
                raise CandidateImportError(
                    f"{label} column index {index} is outside the file",
                    user_message=(
                        f"The {label} column is no longer part of this file. "
                        "Choose the column again."
                    ),
                )
        chosen = [i for i in (self.name, self.attendance) if i is not None]
        if self.candidate_id in chosen:
            raise CandidateImportError(
                "Candidate ID column reused for another field",
                user_message=(
                    "The Candidate ID column cannot also be the name or the "
                    "marks column. Choose a different column for each."
                ),
            )


@dataclass(frozen=True, slots=True)
class MappingSuggestion:
    """What the importer thinks the columns are, and how sure it is.

    Attributes:
        candidate_id / name / attendance: The best guess, or ``None``.
        ambiguous_candidate_id: Every column that matched the candidate-ID
            headers when more than one did. Non-empty means **the operator must
            choose** - picking one would be a coin toss over whose paper is
            whose.
    """

    candidate_id: int | None = None
    name: int | None = None
    attendance: int | None = None
    ambiguous_candidate_id: tuple[int, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        """Whether the candidate-ID column could not be chosen automatically."""
        return bool(self.ambiguous_candidate_id)

    @property
    def is_complete(self) -> bool:
        """Whether this suggestion is usable without the operator changing it."""
        return self.candidate_id is not None and not self.is_ambiguous

    def to_mapping(self) -> ColumnMapping | None:
        """Return the mapping this suggests, or ``None`` if it is not usable."""
        if not self.is_complete or self.candidate_id is None:
            return None
        return ColumnMapping(
            candidate_id=self.candidate_id, name=self.name, attendance=self.attendance
        )


EXACT_MATCH = 2
LOOSE_MATCH = 1
NO_MATCH = 0


def _match_header(header: str, candidates: Sequence[str]) -> tuple[int, int]:
    """Score one header against a list of known names.

    Returns:
        ``(tier, score)``. ``tier`` is :data:`EXACT_MATCH` for a header that
        *is* one of the known names, :data:`LOOSE_MATCH` for one that merely
        begins with or contains one, and :data:`NO_MATCH` for the rest.
        ``score`` ranks within a tier by how specific the matched name was.

    The tier matters more than the score, and only for the candidate-ID column:
    two headers that both *exactly* name a candidate ID is a file OMRFlow must
    not choose between, whereas an exact match beside a loose one is not really
    a contest. Collapsing both into one number would make those
    indistinguishable.
    """
    text = header.strip().casefold().rstrip(":")
    if not text:
        return (NO_MATCH, 0)
    for rank, known in enumerate(candidates):
        if text == known:
            return (EXACT_MATCH, len(candidates) - rank)
    for rank, known in enumerate(candidates):
        if text.startswith(known) or known in text.split():
            return (LOOSE_MATCH, len(candidates) - rank)
    return (NO_MATCH, 0)


def suggest_mapping(headers: Sequence[str]) -> MappingSuggestion:
    """Guess which columns hold the candidate ID, name and marks.

    Args:
        headers: The header row, in order.

    Returns:
        The best guess. When two columns match the candidate-ID names *equally
        strongly*, :attr:`MappingSuggestion.ambiguous_candidate_id` lists them
        and no ID column is proposed - the interface must ask.

    A file with both ``Roll No.`` and ``Candidate ID`` is genuinely ambiguous:
    both are exact names for the thing, and preferring whichever this module
    happens to list first would be a coin toss over whose paper is whose. An
    exact match beside a merely plausible one is not ambiguous, and is resolved
    in favour of the exact one.

    Name and marks columns are never reported as ambiguous: guessing wrongly
    there costs a mislabelled column the operator can see and fix, not a
    misattributed script.
    """
    scored = [(index, _match_header(h, _ID_HEADERS)) for index, h in enumerate(headers)]
    best_tier = max((tier for _, (tier, _) in scored), default=NO_MATCH)
    # Every column at the strongest tier, *without* a tie-break. Preferring
    # "Roll No." over "Candidate ID" because this module lists it first would
    # be a coin toss dressed as a decision, and the cost of losing it is a
    # candidate's paper filed under somebody else's number.
    winners = tuple(
        index for index, (tier, _) in scored if tier == best_tier and tier != NO_MATCH
    )

    name = _best_column(headers, _NAME_HEADERS, exclude=set(winners))
    attendance = _best_column(
        headers,
        _ATTENDANCE_HEADERS,
        exclude={*winners, *((name,) if name is not None else ())},
    )

    if len(winners) > 1:
        return MappingSuggestion(
            name=name, attendance=attendance, ambiguous_candidate_id=winners
        )
    if not winners:
        return MappingSuggestion(name=name, attendance=attendance)
    return MappingSuggestion(candidate_id=winners[0], name=name, attendance=attendance)


def _best_column(
    headers: Sequence[str], known: Sequence[str], *, exclude: set[int]
) -> int | None:
    """Return the best-matching column for ``known``, ignoring ``exclude``."""
    ranked = [
        (index, _match_header(header, known))
        for index, header in enumerate(headers)
        if index not in exclude
    ]
    matched = [(index, score) for index, score in ranked if score[0] != NO_MATCH]
    if not matched:
        return None
    return max(matched, key=lambda item: item[1])[0]


# ----------------------------------------------------------------------
# Reading the file
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RosterPreview:
    """What a roster file looks like, before anything is imported.

    Attributes:
        path: The file.
        sheet: The worksheet read, or ``""`` for CSV.
        sheet_names: Every worksheet in the workbook, for the operator to
            choose from. Empty for CSV.
        headers: The header row.
        rows: Up to :data:`PREVIEW_ROW_LIMIT` data rows, as display text.
        data_rows: How many non-empty data rows the file holds in total.
        suggestion: The importer's guess at the column mapping.
    """

    path: Path
    sheet: str
    sheet_names: tuple[str, ...]
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    data_rows: int
    suggestion: MappingSuggestion


def _check_readable(path: Path) -> str:
    """Validate the path and return its lowercase suffix."""
    suffix = path.suffix.casefold()
    if suffix == ".xls":
        raise CandidateImportError(
            "Legacy .xls workbook",
            user_message=(
                "OMRFlow cannot read the older .xls format. Open the file in "
                "Excel and use File > Save As to save it as .xlsx, then import "
                "it again."
            ),
        )
    if suffix not in SUPPORTED_SUFFIXES:
        listed = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise CandidateImportError(
            f"Unsupported roster suffix {suffix!r}",
            user_message=(
                f"OMRFlow can import {listed} candidate lists. "
                f"'{path.name}' is not one of those."
            ),
        )
    if not path.exists():
        raise CandidateImportError(
            "Roster file not found",
            user_message=(
                f"'{path.name}' could not be found. It may have been moved or "
                "renamed since you chose it."
            ),
        )
    if not path.is_file():
        raise CandidateImportError(
            "Roster path is not a file",
            user_message=f"'{path.name}' is a folder, not a candidate list file.",
        )
    return suffix


def list_worksheets(path: Path) -> tuple[str, ...]:
    """Return the worksheet names in a workbook, or ``()`` for a CSV file."""
    suffix = _check_readable(path)
    if suffix in CSV_SUFFIXES:
        return ()
    workbook = _open_workbook(path)
    try:
        return tuple(workbook.sheetnames)
    finally:
        workbook.close()


def _open_workbook(path: Path) -> Any:
    """Open an XLSX workbook read-only, turning every failure into advice."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - openpyxl is a hard dependency
        raise CandidateImportError(
            "openpyxl is not installed",
            user_message=(
                "Excel support is unavailable in this installation. Save the "
                "candidate list as CSV and import that instead."
            ),
        ) from exc

    try:
        return openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        # openpyxl raises a wide spread of exceptions for a damaged file -
        # zipfile.BadZipFile, KeyError, ValueError - and none of them mean
        # anything to an examination operator.
        raise CandidateImportError(
            f"Workbook could not be opened: {exc}",
            user_message=(
                f"'{path.name}' could not be opened as an Excel workbook. It "
                "may be damaged, password-protected, or not really an .xlsx "
                "file. Try opening it in Excel and saving a fresh copy."
            ),
        ) from exc


def _csv_rows(path: Path) -> Iterator[list[str]]:
    """Yield every row of a CSV file, tolerating a UTF-8 byte-order mark."""
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise CandidateImportError(
            f"Roster file could not be opened: {exc}",
            user_message=(
                f"'{path.name}' could not be opened. Check that it is not open "
                "in another program and that you have permission to read it."
            ),
        ) from exc
    try:
        try:
            yield from csv.reader(handle)
        except (csv.Error, UnicodeDecodeError) as exc:
            raise CandidateImportError(
                f"Malformed CSV: {exc}",
                user_message=(
                    f"'{path.name}' is not readable as CSV - it may use an "
                    "unexpected text encoding, or contain an unclosed quote. "
                    "Opening it in Excel and saving it again as CSV usually "
                    "repairs it."
                ),
            ) from exc
    finally:
        handle.close()


def _sheet_rows(path: Path, sheet: str) -> tuple[list[list[object]], str, tuple[str, ...]]:
    """Read one worksheet fully, returning its rows, its name and every name."""
    workbook = _open_workbook(path)
    try:
        names = tuple(workbook.sheetnames)
        if not names:
            raise CandidateImportError(
                "Workbook has no worksheets",
                user_message=f"'{path.name}' contains no worksheets to import.",
            )
        chosen = sheet or names[0]
        if chosen not in names:
            raise CandidateImportError(
                f"Worksheet {chosen!r} not present",
                user_message=(
                    f"'{path.name}' has no worksheet named '{chosen}'. "
                    f"Available worksheets: {', '.join(names)}."
                ),
            )
        worksheet = workbook[chosen]
        rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
        return rows, chosen, names
    finally:
        workbook.close()


def _is_blank_row(row: Sequence[object]) -> bool:
    """Whether every cell in a row is empty.

    Worth its own function because a real workbook is full of these: the
    supplied sample reports 230 rows and holds 14, the rest being formatting
    Excel remembers and a reader must ignore.
    """
    return all(_clean_text(cell) == "" for cell in row)


MAX_HEADER_SEARCH_ROWS = 15
"""How many leading rows are searched for the header row.

A real examination office's attendance sheet carries its institution name,
the examination title and the post above the table - the very features that
make it worth using as the result template later - so the header is routinely
*not* the first non-blank row. Fifteen is generous for that decoration and
still cheap to scan.

Matches :data:`omr_scanner.services.report_template.MAX_HEADER_SEARCH_ROWS`,
which solved the same problem for result templates first; attendance files
and result templates are the same workbooks, so they must agree about where
a table starts."""


def _header_score(headers: Sequence[str]) -> int:
    """How strongly a row reads as a header row.

    Scored, not pattern-matched: a decorative title row happens to contain
    words, and the question is which row contains the *column names*. An ID
    column is what makes a roster a roster, so it is weighted highest.
    """
    cleaned = [_clean_text(cell) for cell in headers]
    if not any(cleaned):
        return 0
    score = 0
    if _best_column(cleaned, _ID_HEADERS, exclude=set()) is not None:
        score += 3
    if _best_column(cleaned, _NAME_HEADERS, exclude=set()) is not None:
        score += 2
    if _best_column(cleaned, _ATTENDANCE_HEADERS, exclude=set()) is not None:
        score += 1
    return score


def _split_header(rows: Sequence[Sequence[object]], path: Path) -> tuple[
    tuple[str, ...], list[Sequence[object]]
]:
    """Separate the header row from the data rows.

    Searches the first :data:`MAX_HEADER_SEARCH_ROWS` non-blank rows and takes
    the one that reads most strongly as a header, rather than assuming the
    first non-blank row is it. A file whose header *is* its first row - every
    CSV this importer has ever accepted - scores highest there and behaves
    exactly as before.
    """
    candidates: list[tuple[int, Sequence[object]]] = [
        (index, row) for index, row in enumerate(rows) if not _is_blank_row(row)
    ]
    if not candidates:
        raise CandidateImportError(
            "Roster file is empty",
            user_message=(
                f"'{path.name}' contains no rows. Check that you selected the "
                "right file, and the right worksheet if it is a workbook."
            ),
        )

    best_index, _best_row = candidates[0]
    best_score = 0
    for index, row in candidates[:MAX_HEADER_SEARCH_ROWS]:
        score = _header_score([_clean_text(cell) for cell in row])
        if score > best_score:
            best_index, best_score = index, score

    headers = tuple(_clean_text(cell) for cell in rows[best_index])
    data = [item for item in rows[best_index + 1 :] if not _is_blank_row(item)]
    return headers, data


def preview_roster(
    path: Path, *, sheet: str = "", limit: int = PREVIEW_ROW_LIMIT
) -> RosterPreview:
    """Read the top of a roster file so the operator can confirm the mapping.

    Args:
        path: The CSV or XLSX file.
        sheet: Which worksheet, for a workbook. Empty selects the first.
        limit: How many data rows to return for display.

    Returns:
        The headers, a sample of rows, the total row count and a suggested
        column mapping.

    Raises:
        CandidateImportError: The file cannot be read, is empty, or holds only
            a header.
    """
    suffix = _check_readable(path)
    chosen = ""
    names: tuple[str, ...] = ()
    if suffix in CSV_SUFFIXES:
        raw: list[Sequence[object]] = [list(row) for row in _csv_rows(path)]
    else:
        sheet_rows, chosen, names = _sheet_rows(path, sheet)
        raw = list(sheet_rows)

    headers, data = _split_header(raw, path)
    if not data:
        raise CandidateImportError(
            "Roster file holds only a header",
            user_message=(
                f"'{path.name}' has column headings but no candidate rows."
                + (f" Worksheet: '{chosen}'." if chosen else "")
            ),
        )

    width = max(len(headers), max(len(row) for row in data))
    headers = tuple(_pad(list(headers), width, ""))
    preview = tuple(
        tuple(_pad([_clean_text(cell) for cell in row], width, ""))
        for row in data[:limit]
    )
    _LOGGER.info(
        "Roster previewed: rows=%d columns=%d format=%s",
        len(data),
        width,
        suffix.lstrip("."),
    )
    return RosterPreview(
        path=path,
        sheet=chosen,
        sheet_names=names,
        headers=headers,
        rows=preview,
        data_rows=len(data),
        suggestion=suggest_mapping(headers),
    )


def _pad(values: list[str], width: int, filler: str) -> list[str]:
    """Return ``values`` extended to ``width``."""
    return values + [filler] * (width - len(values))


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
class RosterIssueCode(StrEnum):
    """Why one row, or the file as a whole, could not be accepted."""

    BLANK_ID = "blank_id"
    DUPLICATE_ID = "duplicate_id"


@dataclass(frozen=True, slots=True)
class RosterIssue:
    """One problem with the imported roster.

    Attributes:
        code: What kind of problem.
        source_row: The 1-based row in the source file, header included. Safe
            to log: it names a line, not a person.
        message: Operator-facing text. **May contain a candidate ID** - the
            operator needs it to find the row - and must therefore never be
            logged.
        blocking: Whether this stops the import.
    """

    code: RosterIssueCode
    source_row: int
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class RosterValidation:
    """The outcome of reading a roster, ready for the confirmation screen.

    Attributes:
        candidates: Every accepted candidate, in file order.
        issues: Everything wrong.
        rows_read: Non-blank data rows in the file.
        blank_ids: Rows whose candidate ID cell was empty.
        duplicate_ids: How many rows repeated an ID already seen.
        expected_present / expected_absent / attendance_unknown: Counts over
            :attr:`candidates`.
        mapping: The mapping used.
        source_name: The file's name, for display and for recording with the
            roster. Not the full path - a project database is shared, and a
            path can name a person's home directory.
        sheet: The worksheet read, or ``""``.
    """

    candidates: tuple[CandidateRecord, ...]
    issues: tuple[RosterIssue, ...]
    rows_read: int
    blank_ids: int
    duplicate_ids: int
    expected_present: int
    expected_absent: int
    attendance_unknown: int
    mapping: ColumnMapping
    source_name: str
    sheet: str = ""

    @property
    def blocking_issues(self) -> tuple[RosterIssue, ...]:
        """Issues that must be fixed in the source file before importing."""
        return tuple(item for item in self.issues if item.blocking)

    @property
    def can_import(self) -> bool:
        """Whether this roster may be committed.

        Requires at least one candidate and no blocking issue. A roster with a
        repeated candidate ID is *fundamentally* ambiguous - the file states
        two different facts about one person - so it is never importable "with
        warnings".
        """
        return bool(self.candidates) and not self.blocking_issues

    @property
    def summary(self) -> str:
        """A one-line summary for the confirmation screen."""
        parts = [
            f"{self.rows_read} row(s) read",
            f"{len(self.candidates)} candidate(s)",
        ]
        if self.mapping.attendance is not None:
            parts.append(f"{self.expected_present} expected present")
            parts.append(f"{self.expected_absent} marked absent")
        if self.blank_ids:
            parts.append(f"{self.blank_ids} with no candidate ID")
        if self.duplicate_ids:
            parts.append(f"{self.duplicate_ids} duplicate ID(s)")
        return " · ".join(parts)


def read_roster(
    path: Path, mapping: ColumnMapping | None = None, *, sheet: str = ""
) -> RosterValidation:
    """Read and validate a whole roster file.

    Args:
        path: The CSV or XLSX file.
        mapping: Which columns to use. ``None`` asks the importer to work it
            out, which succeeds only when the candidate-ID column is
            unambiguous.
        sheet: Which worksheet, for a workbook.

    Returns:
        Every accepted candidate plus everything wrong with the file. **Reading
        never commits anything**, so a caller may show the result and let the
        operator decide.

    Raises:
        CandidateImportError: The file cannot be read at all, or no candidate
            ID column could be identified.
    """
    suffix = _check_readable(path)
    if suffix in CSV_SUFFIXES:
        raw: list[Sequence[object]] = [list(row) for row in _csv_rows(path)]
        chosen = ""
    else:
        sheet_rows, chosen, _ = _sheet_rows(path, sheet)
        raw = list(sheet_rows)

    headers, data = _split_header(raw, path)
    if not data:
        raise CandidateImportError(
            "Roster file holds only a header",
            user_message=(
                f"'{path.name}' has column headings but no candidate rows."
                + (f" Worksheet: '{chosen}'." if chosen else "")
            ),
        )
    header_offset = _header_offset(raw)
    resolved = mapping or _require_mapping(headers, path)
    resolved.validate(max(len(headers), 1))

    candidates: list[CandidateRecord] = []
    issues: list[RosterIssue] = []
    seen: dict[str, int] = {}
    blank = 0
    duplicates = 0

    for offset, row in enumerate(data):
        source_row = header_offset + offset + 1
        candidate_id = normalise_candidate_id(_cell(row, resolved.candidate_id))
        if not candidate_id:
            blank += 1
            issues.append(
                RosterIssue(
                    code=RosterIssueCode.BLANK_ID,
                    source_row=source_row,
                    message=(
                        f"Row {source_row} has no candidate ID. Add the roll "
                        "number, or delete the row if it is not a candidate."
                    ),
                )
            )
            continue
        if candidate_id in seen:
            duplicates += 1
            issues.append(
                RosterIssue(
                    code=RosterIssueCode.DUPLICATE_ID,
                    source_row=source_row,
                    message=(
                        f"Candidate ID {candidate_id} occurs more than once in "
                        f"the imported candidate list (rows {seen[candidate_id]} "
                        f"and {source_row}). Candidate IDs must be unique "
                        "before reconciliation can proceed."
                    ),
                )
            )
            continue
        seen[candidate_id] = source_row

        raw_attendance = (
            _cell(row, resolved.attendance) if resolved.attendance is not None else None
        )
        attendance = (
            attendance_from_cell(raw_attendance)
            if resolved.attendance is not None
            else AttendanceState.UNKNOWN
        )
        candidates.append(
            CandidateRecord(
                candidate_id=candidate_id,
                display_name=_clean_text(
                    _cell(row, resolved.name) if resolved.name is not None else None
                ),
                source_row=source_row,
                imported_attendance=attendance,
                imported_value=_clean_text(raw_attendance),
            )
        )

    present = sum(
        1 for item in candidates if item.imported_attendance is AttendanceState.PRESENT
    )
    absent = sum(
        1 for item in candidates if item.imported_attendance is AttendanceState.ABSENT
    )
    unknown = len(candidates) - present - absent

    # Counts and a format name only: never an ID, a name or a marks value.
    _LOGGER.info(
        "Roster read: rows=%d accepted=%d blank_id=%d duplicate_id=%d "
        "present=%d absent=%d unknown=%d",
        len(data), len(candidates), blank, duplicates, present, absent, unknown,
    )
    return RosterValidation(
        candidates=tuple(candidates),
        issues=tuple(issues),
        rows_read=len(data),
        blank_ids=blank,
        duplicate_ids=duplicates,
        expected_present=present,
        expected_absent=absent,
        attendance_unknown=unknown,
        mapping=resolved,
        source_name=path.name,
        sheet=chosen,
    )


def _header_offset(rows: Sequence[Sequence[object]]) -> int:
    """Return the 1-based source row number of the header row."""
    for index, row in enumerate(rows):
        if not _is_blank_row(row):
            return index + 1
    return 1


def _cell(row: Sequence[object], index: int | None) -> object:
    """Return one cell, or ``None`` when the row is short or unmapped."""
    if index is None or index >= len(row):
        return None
    return row[index]


def _require_mapping(headers: Sequence[str], path: Path) -> ColumnMapping:
    """Return the automatic mapping, or explain why the operator must choose."""
    suggestion = suggest_mapping(headers)
    if suggestion.is_ambiguous:
        named = ", ".join(
            f"'{headers[index]}'" for index in suggestion.ambiguous_candidate_id
        )
        raise CandidateImportError(
            "Ambiguous candidate ID column",
            user_message=(
                f"More than one column in '{path.name}' could be the candidate "
                f"ID: {named}. Select the column containing roll/candidate "
                "numbers before continuing."
            ),
        )
    resolved = suggestion.to_mapping()
    if resolved is None:
        available = ", ".join(f"'{h}'" for h in headers if h) or "(no column headings)"
        raise CandidateImportError(
            "No candidate ID column",
            user_message=(
                f"OMRFlow could not identify a Candidate ID column in "
                f"'{path.name}'. Select the column containing roll/candidate "
                f"numbers before continuing. Columns found: {available}."
            ),
        )
    return resolved


def with_mapping(
    validation: RosterValidation, mapping: ColumnMapping, *, path: Path
) -> RosterValidation:
    """Re-read ``path`` under a different column mapping.

    The operator changing a dropdown re-reads the file rather than
    reinterpreting the previous result: a different candidate-ID column means
    different duplicates, different blanks and different counts, and patching
    the old answer is how those quietly go stale.
    """
    return read_roster(path, mapping, sheet=validation.sheet)


# ----------------------------------------------------------------------
# The packaged sample
# ----------------------------------------------------------------------
def sample_template_bytes() -> bytes:
    """Return the packaged candidate/attendance sample workbook.

    Read through :mod:`importlib.resources` rather than a path relative to the
    repository, so it resolves identically for a source checkout, an editable
    install, a built wheel and a frozen executable. The top-level ``resources/``
    directory is dev fixtures and is never packaged - see the
    ``[tool.setuptools.package-data]`` comment in ``pyproject.toml``.
    """
    try:
        return (
            resources.files(_SAMPLE_PACKAGE).joinpath(_SAMPLE_FILENAME).read_bytes()
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise CandidateImportError(
            f"Packaged sample template missing: {exc}",
            user_message=(
                "The sample candidate list is missing from this installation. "
                "Reinstalling OMRFlow will restore it."
            ),
        ) from exc


def save_sample_template(destination: Path, *, overwrite: bool = False) -> Path:
    """Write the packaged sample candidate list to ``destination``.

    Args:
        destination: Where to write it.
        overwrite: Whether an existing file may be replaced. The caller is
            expected to have asked the operator first.

    Returns:
        The path written.

    Raises:
        CandidateImportError: The destination exists and ``overwrite`` is
            ``False``, or it could not be written.

    The packaged file is **copied, never opened for writing**, so the sample
    shipped with the application cannot be altered by handing it out.
    """
    payload = sample_template_bytes()
    if destination.exists() and not overwrite:
        raise CandidateImportError(
            "Sample destination already exists",
            user_message=(
                f"'{destination.name}' already exists. Choose another name, or "
                "confirm that you want to replace it."
            ),
        )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    except OSError as exc:
        raise CandidateImportError(
            f"Sample template could not be written: {exc}",
            user_message=(
                f"The sample candidate list could not be saved to "
                f"'{destination.parent}'. Check that the folder exists and "
                "that you have permission to write to it."
            ),
        ) from exc
    _LOGGER.info("Sample candidate list saved: bytes=%d", len(payload))
    return destination


__all__ = [
    "CSV_SUFFIXES",
    "EXCEL_SUFFIXES",
    "PREVIEW_ROW_LIMIT",
    "SUPPORTED_SUFFIXES",
    "CandidateImportError",
    "ColumnMapping",
    "MappingSuggestion",
    "RosterIssue",
    "RosterIssueCode",
    "RosterPreview",
    "RosterValidation",
    "attendance_from_cell",
    "list_worksheets",
    "normalise_candidate_id",
    "preview_roster",
    "read_roster",
    "sample_template_bytes",
    "save_sample_template",
    "suggest_mapping",
    "with_mapping",
]
