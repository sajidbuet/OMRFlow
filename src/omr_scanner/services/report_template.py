"""Read and validate an examination office's own result/absentee workbook.

Purpose:
    Turn a spreadsheet an examination office already has - "Absentee-Sample.xlsx"
    and its like - into an ordered roster Phase 9 can populate, without
    recreating the office's own roll, name or row order from scratch. See
    ``docs/reporting.md`` and the phase brief's §3, §7-§9.

Scope:
    Reading and column identification. No writing, no database, no Qt. Writing
    into a *copy* of this workbook is :mod:`omr_scanner.reporting.excel`'s job;
    cross-checking the roster this module reads against Phase 7/8's canonical
    state is :mod:`omr_scanner.services.report_readiness`'s.

Two rules this module exists to enforce, matching
:mod:`omr_scanner.services.candidate_import`'s own two rules exactly - a
result template is a roster, and a roster's rules do not change because the
file is a result sheet instead of an attendance sheet:

1. **A Roll No. is an identifier, never a quantity.** The same
   :func:`~omr_scanner.services.candidate_import.normalise_candidate_id` is
   reused here, so a candidate matches identically whether they were imported
   through the Attendance stage or looked up from a result template.
2. **Ambiguity is reported, never resolved silently.** A template whose
   columns cannot be identified with confidence stops with a message naming
   what could not be identified, never a guess that puts one candidate's mark
   in another's row.

What this module will not do:
    * Assume the sample's exact column names, sheet name or row count. The
      phase brief is explicit that a real examination office's workbook will
      differ, and a template-specific assumption here would work for exactly
      one paper.
    * Modify the workbook it reads. Every read here uses ``read_only=True``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omr_scanner.domain.reconciliation import is_absent_token
from omr_scanner.errors import OMRScannerError
from omr_scanner.services.candidate_import import (
    ID_HEADERS,
    NAME_HEADERS,
    normalise_candidate_id,
)

_LOGGER = logging.getLogger(__name__)

MAX_HEADER_SEARCH_ROWS = 15
"""How many leading rows are searched for the header row.

The phase brief explicitly requires supporting "additional decorative/title
rows" and a header starting on row 3 rather than row 1 (§38) - a real
examination office's workbook often carries an institution name and an
examination title above the table. Fifteen rows is generous for that and
still cheap to scan even on a workbook with thousands of data rows, since the
search stops as soon as a row scores as a header.
"""

PREVIEW_ROW_LIMIT = 50

SERIAL_HEADERS: tuple[str, ...] = (
    "sl.no.", "sl. no.", "sl no.", "sl no", "sl.no", "serial", "serial no",
    "serial number", "s.no", "s. no.", "sno", "si. no.", "si no",
)
MARKS_HEADERS: tuple[str, ...] = (
    "total", "marks", "mark", "score", "obtained", "obtained marks", "result",
)
"""Deliberately does not name the supplied sample's own ``"total (90)"`` -
see :mod:`omr_scanner.services.candidate_import`'s identical note about
hard-coding one examination's column name."""

RANK_HEADERS: tuple[str, ...] = ("merit", "rank", "position", "standing")


class ReportTemplateError(OMRScannerError):
    """A result template could not be read, or its columns are ambiguous.

    Always carries a ``user_message`` naming the problem - the same contract
    :class:`~omr_scanner.services.candidate_import.CandidateImportError` keeps,
    because an operator who has just been told "cannot map this template"
    needs a next action, not a stack trace (phase brief §7, §32).
    """


# ----------------------------------------------------------------------
# Column mapping
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReportColumnMapping:
    """Which column of a result template holds what.

    Attributes:
        roll: Required. 0-based column index holding the Roll No.
        marks: Required. 0-based column index holding the total/marks.
        serial: Optional column holding the printed serial number. When
            ``None``, this module numbers rows itself for internal use only -
            the template's own Sl.No. column, if it has one, is what actually
            gets preserved in the output.
        name: Optional column holding the candidate's name.
        rank: Optional column holding Merit/Rank. When ``None``, the template
            has no rank column and one must be added or mapped before a
            report can be generated - :mod:`omr_scanner.services.
            report_readiness` is where that becomes a blocking issue, not
            here, since a mapping without a rank column is still valid enough
            to *preview*.
    """

    roll: int
    marks: int
    serial: int | None = None
    name: int | None = None
    rank: int | None = None

    def validate(self, column_count: int) -> None:
        """Raise if any mapped index is outside the sheet's columns."""
        for label, index in (
            ("Roll No.", self.roll),
            ("Marks", self.marks),
            ("Sl.No.", self.serial),
            ("Name", self.name),
            ("Merit/Rank", self.rank),
        ):
            if index is None:
                continue
            if not 0 <= index < column_count:
                raise ReportTemplateError(
                    f"{label} column index {index} is outside the sheet",
                    user_message=(
                        f"The {label} column is no longer part of this "
                        "template. Choose the column again."
                    ),
                )
        chosen: dict[int, str] = {}
        for label, index in (
            ("Roll No.", self.roll),
            ("Marks", self.marks),
            ("Sl.No.", self.serial),
            ("Name", self.name),
            ("Merit/Rank", self.rank),
        ):
            if index is None:
                continue
            if index in chosen:
                raise ReportTemplateError(
                    "A template column is mapped to two fields",
                    user_message=(
                        f"Column {index + 1} is mapped as both {chosen[index]} "
                        f"and {label}. Choose a different column for each."
                    ),
                )
            chosen[index] = label


@dataclass(frozen=True, slots=True)
class MappingSuggestion:
    """What this module thinks a result template's columns are.

    Attributes:
        roll / marks / serial / name / rank: The best guess for each field,
            or ``None``.
        ambiguous_roll / ambiguous_marks: Every column that matched equally
            strongly, when more than one did. Non-empty means **the operator
            must choose** - a wrong guess on either puts one candidate's mark
            in another's row, which is the one failure this whole module
            exists to prevent.
    """

    roll: int | None = None
    marks: int | None = None
    serial: int | None = None
    name: int | None = None
    rank: int | None = None
    ambiguous_roll: tuple[int, ...] = ()
    ambiguous_marks: tuple[int, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        """Whether either identifying column could not be chosen automatically."""
        return bool(self.ambiguous_roll) or bool(self.ambiguous_marks)

    @property
    def is_complete(self) -> bool:
        """Whether this suggestion is usable without the operator changing it."""
        return self.roll is not None and self.marks is not None and not self.is_ambiguous

    def to_mapping(self) -> ReportColumnMapping | None:
        """Return the mapping this suggests, or ``None`` if it is not usable."""
        if not self.is_complete or self.roll is None or self.marks is None:
            return None
        return ReportColumnMapping(
            roll=self.roll, marks=self.marks, serial=self.serial,
            name=self.name, rank=self.rank,
        )


_EXACT_MATCH = 2
_LOOSE_MATCH = 1
_NO_MATCH = 0


def _match_header(header: str, candidates: Sequence[str]) -> tuple[int, int]:
    """Score one header against a list of known names.

    The same two-tier algorithm
    :mod:`omr_scanner.services.candidate_import` uses (exact name beats a
    loose prefix/word match, which beats nothing), reimplemented here rather
    than imported: it is a dozen lines of generic string matching, not
    identifier-specific logic, and keeping the copy local means this module's
    header vocabulary (Sl.No., Merit) can grow without touching the roster
    importer.
    """
    text = header.strip().casefold().rstrip(":")
    if not text:
        return (_NO_MATCH, 0)
    for rank, known in enumerate(candidates):
        if text == known:
            return (_EXACT_MATCH, len(candidates) - rank)
    for rank, known in enumerate(candidates):
        if text.startswith(known) or known in text.split():
            return (_LOOSE_MATCH, len(candidates) - rank)
    return (_NO_MATCH, 0)


def _best_column(
    headers: Sequence[str], known: Sequence[str], *, exclude: set[int]
) -> int | None:
    """Return the best-matching column for ``known``, ignoring ``exclude``."""
    ranked = [
        (index, _match_header(header, known))
        for index, header in enumerate(headers)
        if index not in exclude
    ]
    matched = [(index, score) for index, score in ranked if score[0] != _NO_MATCH]
    if not matched:
        return None
    return max(matched, key=lambda item: item[1])[0]


def _winners(headers: Sequence[str], known: Sequence[str]) -> tuple[int, ...]:
    """Every column tied for the strongest match against ``known``."""
    scored = [(index, _match_header(h, known)) for index, h in enumerate(headers)]
    best_tier = max((tier for _, (tier, _) in scored), default=_NO_MATCH)
    if best_tier == _NO_MATCH:
        return ()
    return tuple(index for index, (tier, _) in scored if tier == best_tier)


def suggest_mapping(headers: Sequence[str]) -> MappingSuggestion:
    """Guess which columns hold the Roll No., marks, and the rest.

    Args:
        headers: The header row, in order.

    Returns:
        The best guess, mirroring
        :func:`omr_scanner.services.candidate_import.suggest_mapping`'s own
        contract: Roll No. and marks are reported ambiguous rather than
        guessed at when two columns match equally strongly, because a wrong
        guess on either misattributes a mark, not merely a label.
    """
    roll_winners = _winners(headers, ID_HEADERS)
    marks_winners = _winners(headers, MARKS_HEADERS)

    used = {*roll_winners, *marks_winners}
    serial = _best_column(headers, SERIAL_HEADERS, exclude=used)
    if serial is not None:
        used = {*used, serial}
    name = _best_column(headers, NAME_HEADERS, exclude=used)
    if name is not None:
        used = {*used, name}
    rank = _best_column(headers, RANK_HEADERS, exclude=used)

    roll = roll_winners[0] if len(roll_winners) == 1 else None
    marks = marks_winners[0] if len(marks_winners) == 1 else None

    return MappingSuggestion(
        roll=roll,
        marks=marks,
        serial=serial,
        name=name,
        rank=rank,
        ambiguous_roll=roll_winners if len(roll_winners) > 1 else (),
        ambiguous_marks=marks_winners if len(marks_winners) > 1 else (),
    )


# ----------------------------------------------------------------------
# Reading the workbook
# ----------------------------------------------------------------------
def _check_readable(path: Path) -> None:
    """Validate the path, raising a usable message for every way it can fail."""
    if path.suffix.casefold() != ".xlsx":
        raise ReportTemplateError(
            f"Unsupported template suffix {path.suffix!r}",
            user_message=(
                "OMRFlow can use an .xlsx result template. "
                f"'{path.name}' is not one - save it as .xlsx in Excel and "
                "select the new file."
            ),
        )
    if not path.exists():
        raise ReportTemplateError(
            "Template file not found",
            user_message=(
                f"'{path.name}' could not be found. It may have been moved "
                "or renamed since it was selected."
            ),
        )
    if not path.is_file():
        raise ReportTemplateError(
            "Template path is not a file",
            user_message=f"'{path.name}' is a folder, not a template file.",
        )


def _open_workbook(path: Path) -> Any:
    """Open a template workbook read-only, turning every failure into advice.

    ``read_only=True`` is not only a performance choice: it is how this
    module keeps its promise (phase brief §18) never to write into the
    template it was handed - the handle this returns cannot be saved.
    """
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - openpyxl is a hard dependency
        raise ReportTemplateError(
            "openpyxl is not installed",
            user_message="Excel support is unavailable in this installation.",
        ) from exc
    try:
        return openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        raise ReportTemplateError(
            f"Workbook could not be opened: {exc}",
            user_message=(
                f"'{path.name}' could not be opened as an Excel workbook. It "
                "may be damaged, password-protected, or not really an .xlsx "
                "file. Try opening it in Excel and saving a fresh copy."
            ),
        ) from exc


def list_worksheets(path: Path) -> tuple[str, ...]:
    """Return every worksheet name in ``path``."""
    _check_readable(path)
    workbook = _open_workbook(path)
    try:
        return tuple(workbook.sheetnames)
    finally:
        workbook.close()


def _clean_text(value: object) -> str:
    """Return a trimmed display string for a cell."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _is_blank_row(row: Sequence[object]) -> bool:
    """Whether every cell in a row is empty."""
    return all(_clean_text(cell) == "" for cell in row)


def _sheet_rows(path: Path, sheet: str) -> tuple[list[list[object]], str, tuple[str, ...]]:
    """Read one worksheet fully, returning its rows, its name and every name."""
    workbook = _open_workbook(path)
    try:
        names = tuple(workbook.sheetnames)
        if not names:
            raise ReportTemplateError(
                "Workbook has no worksheets",
                user_message=f"'{path.name}' contains no worksheets.",
            )
        chosen = sheet or _guess_rollwise_sheet(workbook, names)
        if chosen not in names:
            raise ReportTemplateError(
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


def _guess_rollwise_sheet(workbook: Any, names: Sequence[str]) -> str:
    """Pick the worksheet most likely to hold the roll-wise roster.

    Scores each worksheet by how many of Roll No./Marks/Name it can find
    among its first :data:`MAX_HEADER_SEARCH_ROWS` rows, and returns the best
    one. A single-worksheet workbook (the common case, and the supplied
    sample) never reaches the scoring at all.
    """
    if len(names) == 1:
        return names[0]
    best_name, best_score = names[0], -1
    for name in names:
        worksheet = workbook[name]
        rows: list[list[object]] = []
        for index, row in enumerate(worksheet.iter_rows(values_only=True)):
            rows.append(list(row))
            if index >= MAX_HEADER_SEARCH_ROWS:
                break
        _, score = _find_header_row(rows)
        if score > best_score:
            best_name, best_score = name, score
    return best_name


def _header_score(headers: Sequence[str]) -> int:
    """How strongly one candidate header row matches the known vocabulary."""
    score = 0
    for known in (ID_HEADERS, MARKS_HEADERS, NAME_HEADERS, SERIAL_HEADERS, RANK_HEADERS):
        if any(_match_header(h, known)[0] != _NO_MATCH for h in headers):
            score += 1
    return score


def _find_header_row(rows: Sequence[Sequence[object]]) -> tuple[int, int]:
    """Return ``(row_index, score)`` of the most header-like leading row.

    Args:
        rows: Every row read so far, in file order.

    Returns:
        The 0-based index of the best candidate and its score. A workbook
        with decorative title rows above the real header (phase brief §38)
        is exactly what this search exists for: row 0 might be an
        institution's name, row 1 an examination title, and only row 2 the
        real ``Sl.No. / Roll No. / Name / Total / Merit`` row - a header row
        must score at least :data:`_MIN_HEADER_SCORE` (it names a Roll No.
        column at minimum) to be accepted at all, or a title row full of
        merged, decorative text could be mistaken for one.
    """
    best_index, best_score = 0, -1
    for index, row in enumerate(rows[:MAX_HEADER_SEARCH_ROWS]):
        if _is_blank_row(row):
            continue
        headers = [_clean_text(cell) for cell in row]
        score = _header_score(headers)
        if score > best_score:
            best_index, best_score = index, score
    return best_index, best_score


_MIN_HEADER_SCORE = 1
"""A header row must at least name something recognisable (typically Roll
No.) to be accepted; a row with none is not a header, however plausible its
position."""


@dataclass(frozen=True, slots=True)
class TemplatePreview:
    """What a result template looks like, before it is committed to.

    Attributes:
        path: The template file.
        sheet: The worksheet read.
        sheet_names: Every worksheet in the workbook.
        header_row_index: 0-based index, within the rows actually read
            (i.e. relative to the top of the sheet), of the detected header.
        headers: The header row's text.
        rows: Up to :data:`PREVIEW_ROW_LIMIT` data rows, as display text.
        data_rows: How many data rows the sheet holds in total.
        suggestion: The best-guess column mapping.
    """

    path: Path
    sheet: str
    sheet_names: tuple[str, ...]
    header_row_index: int
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    data_rows: int
    suggestion: MappingSuggestion


def preview_template(path: Path, sheet: str = "") -> TemplatePreview:
    """Read enough of a result template to show it and suggest a mapping.

    Raises:
        ReportTemplateError: The file cannot be read, or no row in the first
            :data:`MAX_HEADER_SEARCH_ROWS` looks like a header at all - which
            means this is very unlikely to be a roll-wise result template.
    """
    _check_readable(path)
    rows, chosen_sheet, names = _sheet_rows(path, sheet)
    header_index, score = _find_header_row(rows)
    if score < _MIN_HEADER_SCORE:
        raise ReportTemplateError(
            "No header row could be identified",
            user_message=(
                f"OMRFlow could not find a header row (naming a Roll No. or "
                f"similar column) in the first {MAX_HEADER_SEARCH_ROWS} rows "
                f"of '{chosen_sheet}'. Check that this is the right "
                "worksheet, or that it has not been reformatted."
            ),
        )
    headers = tuple(_clean_text(cell) for cell in rows[header_index])
    data = [row for row in rows[header_index + 1 :] if not _is_blank_row(row)]
    display_rows = tuple(
        tuple(_clean_text(cell) for cell in row) for row in data[:PREVIEW_ROW_LIMIT]
    )
    return TemplatePreview(
        path=path,
        sheet=chosen_sheet,
        sheet_names=names,
        header_row_index=header_index,
        headers=headers,
        rows=display_rows,
        data_rows=len(data),
        suggestion=suggest_mapping(headers),
    )


# ----------------------------------------------------------------------
# The roster
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TemplateRow:
    """One candidate row, as the template states it.

    Attributes:
        sheet_row_number: The 1-based Excel row number this came from - what
            :mod:`omr_scanner.reporting.excel` writes back into, so a
            candidate's mark lands in exactly the row their name came from.
        serial_display: The template's own printed serial number, or ``""``.
        roll: The canonical, normalised Roll No. - see
            :func:`~omr_scanner.services.candidate_import.normalise_candidate_id`.
        name: The candidate's name, exactly as the template has it.
        marks_raw: The existing marks cell's value, untouched - ``None`` for
            blank, the absence token's own text for an already-absent row, or
            a leftover number if the template was pre-filled.
        rank_raw: The existing Merit/Rank cell's value, untouched.
    """

    sheet_row_number: int
    roll: str
    name: str = ""
    serial_display: str = ""
    marks_raw: object = None
    rank_raw: object = None

    @property
    def marks_says_absent(self) -> bool:
        """Whether the template's own marks cell already reads as an absence."""
        return is_absent_token(self.marks_raw)


@dataclass(frozen=True, slots=True)
class TemplateRoster:
    """A result template, read and mapped - the authoritative report roster.

    Attributes:
        path / sheet: Where this came from.
        mapping: The column mapping used to read it.
        rows: Every candidate row, in the template's own order - **never**
            reordered, filtered or de-duplicated here (phase brief §8, §10):
            duplicates and gaps are reported as validation issues by
            :mod:`omr_scanner.services.report_readiness`, not silently
            resolved by this module.
        header_row_number: 1-based Excel row number of the header, so the
            generator knows where the data rows begin.
    """

    path: Path
    sheet: str
    mapping: ReportColumnMapping
    rows: tuple[TemplateRow, ...]
    header_row_number: int

    @property
    def first_data_row_number(self) -> int:
        """The 1-based Excel row number of the first candidate row."""
        return self.header_row_number + 1

    @property
    def last_data_row_number(self) -> int:
        """The 1-based Excel row number of the last candidate row."""
        return self.rows[-1].sheet_row_number if self.rows else self.header_row_number

    @property
    def roll_numbers(self) -> tuple[str, ...]:
        """Every Roll No. in template order, including duplicates."""
        return tuple(row.roll for row in self.rows)

    @property
    def duplicate_rolls(self) -> tuple[str, ...]:
        """Roll numbers that appear more than once, in first-seen order."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for roll in self.roll_numbers:
            if not roll:
                continue
            if roll in seen and roll not in duplicates:
                duplicates.append(roll)
            seen.add(roll)
        return tuple(duplicates)

    def row_for(self, roll: str) -> TemplateRow | None:
        """The first row for ``roll``, or ``None``."""
        for row in self.rows:
            if row.roll == roll:
                return row
        return None


def read_template(
    path: Path, mapping: ReportColumnMapping, *, sheet: str = ""
) -> TemplateRoster:
    """Read a result template into an ordered, mapped roster.

    Args:
        path: The template file.
        mapping: A column mapping - typically
            :meth:`MappingSuggestion.to_mapping`'s result, confirmed or
            corrected by the operator when it was ambiguous.
        sheet: Worksheet name; the best guess when omitted.

    Returns:
        Every data row, unmodified and in file order.

    Raises:
        ReportTemplateError: The mapping does not fit this sheet, or the file
            cannot be read.

    A row with a blank Roll No. is **kept**, not dropped - a genuinely blank
    row means the template has a gap this module has no business papering
    over; :mod:`omr_scanner.services.report_readiness` reports it as an issue
    an operator can see and act on. Every other module in Phase 9 receives
    the roster exactly as this function read it.
    """
    _check_readable(path)
    rows, chosen_sheet, _ = _sheet_rows(path, sheet)
    header_index, score = _find_header_row(rows)
    if score < _MIN_HEADER_SCORE:
        raise ReportTemplateError(
            "No header row could be identified",
            user_message=(
                f"OMRFlow could not find a header row in '{chosen_sheet}'. "
                "Check that this is the right worksheet."
            ),
        )
    column_count = len(rows[header_index]) if rows else 0
    mapping.validate(column_count)

    template_rows: list[TemplateRow] = []
    for offset, row in enumerate(rows[header_index + 1 :]):
        if _is_blank_row(row):
            continue
        sheet_row_number = header_index + 1 + offset + 1  # 1-based Excel row
        roll_cell = row[mapping.roll] if mapping.roll < len(row) else None
        name_cell = (
            row[mapping.name]
            if mapping.name is not None and mapping.name < len(row)
            else None
        )
        serial_cell = (
            row[mapping.serial]
            if mapping.serial is not None and mapping.serial < len(row)
            else None
        )
        marks_cell = row[mapping.marks] if mapping.marks < len(row) else None
        rank_cell = (
            row[mapping.rank]
            if mapping.rank is not None and mapping.rank < len(row)
            else None
        )
        template_rows.append(
            TemplateRow(
                sheet_row_number=sheet_row_number,
                roll=normalise_candidate_id(roll_cell),
                name=_clean_text(name_cell),
                serial_display=_clean_text(serial_cell),
                marks_raw=marks_cell,
                rank_raw=rank_cell,
            )
        )

    _LOGGER.info(
        "Result template read: sheet=%r rows=%d roll_column=%d marks_column=%d",
        chosen_sheet,
        len(template_rows),
        mapping.roll,
        mapping.marks,
    )
    return TemplateRoster(
        path=path,
        sheet=chosen_sheet,
        mapping=mapping,
        rows=tuple(template_rows),
        header_row_number=header_index + 1,
    )
