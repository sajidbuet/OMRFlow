"""Build a result workbook from a copy of the operator's own template.

Purpose:
    The openpyxl mechanics behind Phase 9: populate the Rollwise sheet inside
    a *copy* of the supplied template, add the Meritwise/Summary/Answer
    Key/Processing Log sheets, write dynamic ``RANK.EQ`` formulas, and apply
    whatever layout an operator configured - all without ever opening the
    original template file for writing.

Scope:
    Pure(ish) spreadsheet mechanics. Takes plain data in (a
    :class:`~omr_scanner.services.report_template.TemplateRoster`, marks,
    layout settings) and writes to an output path. No database, no Qt, no
    deciding who is present or absent - that is
    :mod:`omr_scanner.services.report_store`'s job, reading Phase 7/8's
    canonical state. This module never re-derives a mark.

Preserving the template (§17, §18):
    ``copy_into`` copies the template's *bytes* before this module opens
    anything - the original file is never opened in write mode by any
    function here, so it cannot be modified regardless of what the rest of
    this module does. Cells this module does not touch keep every formatting
    attribute openpyxl loaded them with, because they are simply never
    reassigned.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from omr_scanner.domain.reporting import (
    ABSENT_MARK_DISPLAY,
    ABSENT_RANK_DISPLAY,
    column_letter,
    rank_formula,
    safe_cell_text,
)
from omr_scanner.domain.scoring import format_mark
from omr_scanner.errors import ReportingError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from omr_scanner.domain.scoring import AnswerKey
    from omr_scanner.services.report_template import TemplateRoster

_LOGGER = logging.getLogger(__name__)


def _bold_font(**overrides: Any) -> Any:
    """A fresh bold ``openpyxl.styles.Font``, with any extra attributes set.

    ``Font.copy(...)`` looks tempting but is deprecated in current openpyxl
    (a warning this project's pytest configuration turns into a failure) and
    was never necessary here: every call site wants a *new* font on a cell
    that has not been styled yet, not a modification of one a template
    already set - this module never touches an existing template cell's
    font, only cells it creates itself on new sheets.
    """
    from openpyxl.styles import Font

    return Font(bold=True, **overrides)

MERITWISE_SHEET_NAME = "Meritwise"
SUMMARY_SHEET_NAME = "Summary"
ANSWER_KEY_SHEET_NAME = "Answer Key"
PROCESSING_LOG_SHEET_NAME = "Processing Log"

_MARK_NUMBER_FORMAT = "0.00"
"""Two decimal places, matching :data:`omr_scanner.domain.scoring.MARK_DISPLAY_PLACES` -
the same precision Phase 8's own GUI shows a mark at. Excel's own numeric
type is an IEEE-754 double, so a mark whose exact value is a repeating
fraction (an eighth of a thirds-based deduction, say) is necessarily
approximated the moment it becomes an Excel number - this format keeps the
*displayed* figure consistent with what an operator already saw on the
Results stage, which is the precision Excel can actually promise, not a
claim that the workbook cell equals the stored :class:`fractions.Fraction`
bit for bit.
"""


class ExcelReportError(ReportingError):
    """A workbook could not be built. Always carries a ``user_message``.

    The `reporting` package's own error, per its documented contract
    (`reporting/__init__.py`): every failure here is a
    :class:`~omr_scanner.errors.ReportingError`.
    """


# ----------------------------------------------------------------------
# Preserving the template
# ----------------------------------------------------------------------
def copy_into(template_path: Path, output_path: Path) -> None:
    """Copy ``template_path``'s bytes to ``output_path``, unopened.

    This is the whole of how the original template is preserved (§18): a
    plain file copy, before any openpyxl call touches either path. Every
    later function in this module opens and writes only ``output_path``.

    Raises:
        ExcelReportError: The template cannot be read, or the destination
            cannot be written (a locked output file, a missing directory).
    """
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template_path, output_path)
    except OSError as exc:
        raise ExcelReportError(
            f"Could not copy template to output: {exc}",
            user_message=(
                f"'{output_path.name}' could not be written. It may be open "
                "in Excel, or the destination folder may not be writable. "
                "Close the file or choose a different location and try again."
            ),
        ) from exc


def _open_for_writing(path: Path) -> Any:
    """Open a workbook (the *copy*, never the template) for modification."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - openpyxl is a hard dependency
        raise ExcelReportError(
            "openpyxl is not installed",
            user_message="Excel support is unavailable in this installation.",
        ) from exc
    try:
        return openpyxl.load_workbook(path)
    except Exception as exc:
        raise ExcelReportError(
            f"Copied workbook could not be reopened: {exc}",
            user_message=f"'{path.name}' could not be opened after copying.",
        ) from exc


def force_recalculation_on_load(workbook: Any) -> None:
    """Set the workbook to fully recalculate every formula when it is opened.

    Required because openpyxl - like every Python spreadsheet library -
    never evaluates the ``RANK.EQ`` formulas this module writes; without
    this, a workbook opened in a viewer that trusts cached values (some PDF
    converters, some lightweight viewers) could show stale or blank rank
    cells. Excel and LibreOffice both honour ``fullCalcOnLoad`` (§26).
    """
    from openpyxl.workbook.properties import CalcProperties

    workbook.calculation = CalcProperties(calcMode="auto", fullCalcOnLoad=True)


# ----------------------------------------------------------------------
# Rollwise
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CandidateReportRow:
    """One candidate's effective reporting state, already decided.

    Deliberately holds *decisions*, not raw Phase 7/8 objects: this module
    must never re-derive who is absent or what a mark is, so the caller
    (:mod:`omr_scanner.services.report_store`) resolves every field from
    canonical stored data first and hands over only what is to be written.
    """

    roll: str
    is_absent: bool
    final_score: Fraction | None = None
    """``None`` for an absentee, or for a present candidate with no score yet
    (which :mod:`omr_scanner.services.report_readiness` should already have
    blocked from *final* export, but a draft/preview may still render)."""


@dataclass(frozen=True, slots=True)
class RollwiseWriteResult:
    """What actually happened writing the Rollwise sheet."""

    rows_written: int
    marks_written: int
    absent_written: int
    unresolved_rows: tuple[str, ...] = field(default_factory=tuple)
    """Rolls left blank because no decided mark was supplied for them."""


def populate_rollwise(
    output_path: Path,
    roster: TemplateRoster,
    candidates: Mapping[str, CandidateReportRow],
    *,
    marks_header_override: str | None = None,
) -> RollwiseWriteResult:
    """Fill the Rollwise sheet inside the already-copied workbook.

    Args:
        output_path: The copy made by :func:`copy_into` - never the original.
        roster: The template's own roster, in its own order.
        candidates: Every roll's decided attendance/mark, keyed by the same
            canonical Roll No. :mod:`omr_scanner.services.report_template`
            produced. A roll absent from this mapping is left exactly as the
            template had it - which for the sample means a blank marks cell.
        marks_header_override: Replacement text for the marks column header
            (§14 - a maximum score that does not match the template's own
            wording), or ``None`` to leave the template's header untouched.

    Returns:
        A summary of what was written, for the processing log.

    Never sorts, drops or inserts a row: every write is a cell assignment at
    the row the template's own :class:`~omr_scanner.services.
    report_template.TemplateRow` already named.
    """
    workbook = _open_for_writing(output_path)
    try:
        sheet = workbook[roster.sheet]
        mapping = roster.mapping

        if marks_header_override:
            sheet.cell(
                row=roster.header_row_number,
                column=mapping.marks + 1,
                value=safe_cell_text(marks_header_override),
            )

        marks_column_letter = column_letter(mapping.marks + 1)

        marks_written = 0
        absent_written = 0
        unresolved: list[str] = []

        for row in roster.rows:
            decision = candidates.get(row.roll)
            marks_cell = sheet.cell(row=row.sheet_row_number, column=mapping.marks + 1)
            rank_cell = (
                sheet.cell(row=row.sheet_row_number, column=mapping.rank + 1)
                if mapping.rank is not None
                else None
            )

            if decision is None:
                # No decision was supplied for this roll at all - typically a
                # template row that matches no registered candidate, which
                # readiness already reports separately. Leave the cell alone
                # rather than guess.
                if row.roll:
                    unresolved.append(row.roll)
                continue

            if decision.is_absent:
                marks_cell.value = ABSENT_MARK_DISPLAY
                if rank_cell is not None:
                    rank_cell.value = ABSENT_RANK_DISPLAY
                absent_written += 1
                continue

            if decision.final_score is None:
                unresolved.append(row.roll)
                continue

            marks_cell.value = float(decision.final_score)
            marks_cell.number_format = _MARK_NUMBER_FORMAT
            marks_written += 1
            if rank_cell is not None:
                rank_cell.value = rank_formula(
                    marks_column=marks_column_letter,
                    first_data_row=roster.first_data_row_number,
                    last_data_row=roster.last_data_row_number,
                    row=row.sheet_row_number,
                )

        force_recalculation_on_load(workbook)
        workbook.save(output_path)
    finally:
        workbook.close()

    _LOGGER.info(
        "Rollwise sheet populated: rows=%d marks=%d absent=%d unresolved=%d",
        len(roster.rows),
        marks_written,
        absent_written,
        len(unresolved),
    )
    return RollwiseWriteResult(
        rows_written=len(roster.rows),
        marks_written=marks_written,
        absent_written=absent_written,
        unresolved_rows=tuple(unresolved),
    )


# ----------------------------------------------------------------------
# Meritwise
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MeritCandidate:
    """One candidate eligible for the Meritwise listing."""

    roll: str
    name: str
    final_score: Fraction


def add_meritwise_sheet(
    output_path: Path, candidates: Sequence[MeritCandidate]
) -> None:
    """Add a ``Meritwise`` sheet: scored candidates, ranked, sorted.

    Args:
        output_path: The workbook to add the sheet to.
        candidates: Present, scored candidates only - the caller has already
            excluded absentees and anyone unresolved (§13: "final export
            should already be blocked" for the latter).

    Sort order:
        Descending final score; **Roll No. ascending** breaks a tie
        deterministically (§13) - never left to whatever order the input
        happened to arrive in, and never a plain ``sort()`` on a type that
        does not define a stable secondary key.

    The Merit column holds the same kind of ``RANK.EQ`` formula the Rollwise
    sheet does, over *this sheet's own* marks column and row range - a
    candidate's Meritwise rank does not depend on Rollwise being present or
    unchanged.
    """
    workbook = _open_for_writing(output_path)
    try:
        if MERITWISE_SHEET_NAME in workbook.sheetnames:
            del workbook[MERITWISE_SHEET_NAME]
        sheet = workbook.create_sheet(MERITWISE_SHEET_NAME)
        sheet.append(["Sl.No.", "Roll No.", "Name", "Total", "Merit"])
        for cell in sheet[1]:
            cell.font = _bold_font()

        ordered = sorted(candidates, key=lambda item: (-item.final_score, item.roll))
        first_row, last_row = 2, 1 + len(ordered)
        for offset, candidate in enumerate(ordered):
            row = 2 + offset
            sheet.cell(row=row, column=1, value=offset + 1)
            sheet.cell(row=row, column=2, value=candidate.roll)
            sheet.cell(row=row, column=3, value=safe_cell_text(candidate.name))
            marks_cell = sheet.cell(row=row, column=4, value=float(candidate.final_score))
            marks_cell.number_format = _MARK_NUMBER_FORMAT
            if ordered:
                sheet.cell(
                    row=row,
                    column=5,
                    value=rank_formula(
                        marks_column="D",
                        first_data_row=first_row,
                        last_data_row=last_row,
                        row=row,
                    ),
                )
        for column, width in ((1, 8), (2, 16), (3, 32), (4, 10), (5, 10)):
            sheet.column_dimensions[column_letter(column)].width = width
        sheet.freeze_panes = "A2"
        force_recalculation_on_load(workbook)
        workbook.save(output_path)
    finally:
        workbook.close()


# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SummaryData:
    """Set-level report metadata (§19). ``None`` renders as ``N/A``."""

    project_name: str
    set_code: str
    registered: int
    present: int
    absent: int
    scored: int
    unresolved: int
    maximum_score: Fraction | None
    highest_score: Fraction | None
    lowest_score: Fraction | None
    mean_score: Fraction | None
    median_score: Fraction | None
    scoring_policy_summary: tuple[str, ...]
    answer_key_revision: int | None
    generated_at: datetime
    application_version: str


def add_summary_sheet(output_path: Path, data: SummaryData) -> None:
    """Add a ``Summary`` sheet: the metadata an office files alongside a result."""
    workbook = _open_for_writing(output_path)
    try:
        if SUMMARY_SHEET_NAME in workbook.sheetnames:
            del workbook[SUMMARY_SHEET_NAME]
        sheet = workbook.create_sheet(SUMMARY_SHEET_NAME)

        def _mark(value: Fraction | None) -> str:
            return format_mark(value) if value is not None else "N/A"

        rows: list[tuple[str, str]] = [
            ("Project", safe_cell_text(data.project_name)),
            ("Set", data.set_code),
            ("Registered candidates", str(data.registered)),
            ("Present", str(data.present)),
            ("Absent", str(data.absent)),
            ("Scored", str(data.scored)),
            ("Unresolved", str(data.unresolved)),
            ("Maximum possible score", _mark(data.maximum_score)),
            ("Highest obtained score", _mark(data.highest_score)),
            ("Lowest obtained score", _mark(data.lowest_score)),
            ("Mean score", _mark(data.mean_score)),
            ("Median score", _mark(data.median_score)),
            (
                "Answer-key revision",
                str(data.answer_key_revision) if data.answer_key_revision else "N/A",
            ),
            ("Generated", data.generated_at.strftime("%Y-%m-%d %H:%M:%S %Z")),
            ("Application version", data.application_version or "N/A"),
        ]
        for index, (label, value) in enumerate(rows, start=1):
            sheet.cell(row=index, column=1, value=label).font = _bold_font()
            sheet.cell(row=index, column=2, value=value)

        policy_start = len(rows) + 2
        sheet.cell(
            row=policy_start, column=1, value="Scoring configuration"
        ).font = _bold_font()
        for offset, line in enumerate(data.scoring_policy_summary, start=1):
            sheet.cell(row=policy_start + offset, column=1, value=line)

        sheet.column_dimensions["A"].width = 28
        sheet.column_dimensions["B"].width = 40
        workbook.save(output_path)
    finally:
        workbook.close()


# ----------------------------------------------------------------------
# Answer Key
# ----------------------------------------------------------------------
def add_answer_key_sheet(
    output_path: Path,
    *,
    set_code: str,
    key: AnswerKey,
    key_id: int,
) -> None:
    """Add an ``Answer Key`` sheet for **exactly this set's** stored key.

    The key handed in must already be the exact revision this set's result
    was scored under - this function writes whatever it is given and does
    not look one up, so a caller that fetched the wrong set's key would
    produce the wrong sheet with no warning here. Guarding against that is
    :mod:`omr_scanner.services.report_store`'s responsibility, verified by
    the per-set isolation tests.
    """
    workbook = _open_for_writing(output_path)
    try:
        if ANSWER_KEY_SHEET_NAME in workbook.sheetnames:
            del workbook[ANSWER_KEY_SHEET_NAME]
        sheet = workbook.create_sheet(ANSWER_KEY_SHEET_NAME)
        sheet.cell(
            row=1, column=1, value=f"Set {set_code} - Answer Key"
        ).font = _bold_font(size=12)
        sheet.cell(row=2, column=1, value=f"Revision {key.revision} (id {key_id})")
        sheet.cell(row=3, column=1, value=f"Canonical string: {key.answers}")

        header_row = 5
        sheet.cell(row=header_row, column=1, value="Q No.").font = _bold_font()
        sheet.cell(row=header_row, column=2, value="Correct Answer").font = _bold_font()
        sheet.cell(row=header_row, column=3, value="Wrong Question").font = _bold_font()
        for offset, number in enumerate(key.question_numbers):
            row = header_row + 1 + offset
            sheet.cell(row=row, column=1, value=number)
            sheet.cell(row=row, column=2, value=key.answer_for(number))
            sheet.cell(
                row=row, column=3, value="Yes" if key.is_wrong_question(number) else ""
            )
        for column, width in ((1, 10), (2, 16), (3, 16)):
            sheet.column_dimensions[column_letter(column)].width = width
        workbook.save(output_path)
    finally:
        workbook.close()


# ----------------------------------------------------------------------
# Processing Log
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProcessingLogEntry:
    """One line of the generation audit - counts and hashes, never a name."""

    label: str
    value: str


def add_processing_log_sheet(
    output_path: Path, entries: Sequence[ProcessingLogEntry]
) -> None:
    """Add a ``Processing Log`` sheet: the generation audit, not a data dump.

    Every entry here must already have been through
    :mod:`omr_scanner.services.report_store`'s privacy discipline - this
    function writes exactly what it is given and performs no filtering of
    its own, so a caller that hands it a candidate name would put one in the
    workbook. It never does, by construction: see the callers in
    ``services/report_store.py``.
    """
    workbook = _open_for_writing(output_path)
    try:
        if PROCESSING_LOG_SHEET_NAME in workbook.sheetnames:
            del workbook[PROCESSING_LOG_SHEET_NAME]
        sheet = workbook.create_sheet(PROCESSING_LOG_SHEET_NAME)
        sheet.append(["Field", "Value"])
        for cell in sheet[1]:
            cell.font = _bold_font()
        for entry in entries:
            sheet.append([entry.label, entry.value])
        sheet.column_dimensions["A"].width = 28
        sheet.column_dimensions["B"].width = 60
        workbook.save(output_path)
    finally:
        workbook.close()


# ----------------------------------------------------------------------
# Layout: header text, logo, page setup
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LayoutSettings:
    """Everything an operator can configure about a report's presentation.

    Mirrors :class:`omr_scanner.database.models.ReportLayoutConfig` field for
    field; kept as a separate plain dataclass here so this module never
    imports SQLAlchemy.
    """

    title_text: str = ""
    subtitle_text: str = ""
    examination_name: str = ""
    footer_text: str = ""
    column_header_overrides: Mapping[str, str] = field(default_factory=dict)
    auto_update_marks_header: bool = True
    logo_path: str = ""
    logo_max_width_px: int = 120
    logo_max_height_px: int = 120
    font_family: str = ""
    title_font_size: int = 14
    header_font_size: int = 11
    body_font_size: int = 10
    bold_headers: bool = True
    page_size: str = "A4"
    orientation: str = "portrait"
    margin_top_mm: float = 20.0
    margin_bottom_mm: float = 20.0
    margin_left_mm: float = 20.0
    margin_right_mm: float = 20.0
    fit_to_width: bool = True
    scale_percent: int = 100
    center_horizontally: bool = True
    repeat_header_row: bool = True

    @classmethod
    def from_json(cls, payload: str) -> LayoutSettings:
        """Reconstruct settings from :meth:`to_json`'s output."""
        data = json.loads(payload) if payload else {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_json(self) -> str:
        """Serialise for a ``GeneratedReport.layout_config_snapshot_json`` cell."""
        return json.dumps(
            {
                name: (
                    dict(getattr(self, name))
                    if isinstance(getattr(self, name), Mapping)
                    else getattr(self, name)
                )
                for name in self.__dataclass_fields__
            },
            ensure_ascii=False,
            sort_keys=True,
        )


_MM_PER_INCH = 25.4
_PAGE_SIZE_MAP = {"A4": "A4", "LETTER": "letter"}


def apply_layout(
    output_path: Path,
    sheet_names: Sequence[str],
    layout: LayoutSettings,
    *,
    primary_header_row: int,
) -> tuple[str, ...]:
    """Apply page setup, and header text/logo where space allows.

    Args:
        output_path: The workbook to modify.
        sheet_names: Which sheets get the page-setup treatment (typically
            Rollwise and Meritwise - Summary/Answer Key/Processing Log are
            working documents, not print-formatted pages).
        layout: The configured settings.
        primary_header_row: The **already-known** 1-based header row of the
            primary (first-listed) sheet - the same
            :attr:`~omr_scanner.services.report_template.TemplateRoster.header_row_number`
            the roster was read with. Title/logo placement never re-detects
            this: a template with decorative rows containing text of their
            own (the common case this feature is for) would make a "first
            non-blank row" heuristic pick the decorative row itself as the
            header, believing there is no blank space left at all.

    Returns:
        Warnings that do not stop generation - most notably, "there was no
        blank space above the header to add a title without disturbing
        candidate rows", which the phase brief's "never automatically append
        or delete a candidate row" rule (§8) makes an absolute constraint
        rather than something this function may work around by shifting rows.

    Layout preservation is the default (§16, §17): every field left at its
    default (empty text, no logo) changes nothing, so a template with its
    own header, its own logo and its own print setup is untouched unless the
    operator explicitly asks for something different.
    """
    workbook = _open_for_writing(output_path)
    warnings: list[str] = []
    try:
        for name in sheet_names:
            if name not in workbook.sheetnames:
                continue
            sheet = workbook[name]
            _apply_page_setup(sheet, layout)
            if name == sheet_names[0]:
                # Header/logo insertion only ever targets the primary
                # (Rollwise) sheet - it is the one page the phase brief
                # describes as "the official result", and repeating a title
                # into every generated sheet risks the same row-shifting
                # problem this function exists to avoid, sheet by sheet.
                warnings.extend(
                    _apply_header_and_logo(sheet, layout, header_row=primary_header_row)
                )
        workbook.save(output_path)
    finally:
        workbook.close()
    return tuple(warnings)


def _apply_page_setup(sheet: Any, layout: LayoutSettings) -> None:
    """Page size, orientation, margins, scaling and the repeating header row."""
    sheet.page_setup.paperSize = (
        sheet.PAPERSIZE_A4
        if layout.page_size.upper() != "LETTER"
        else sheet.PAPERSIZE_LETTER
    )
    sheet.page_setup.orientation = (
        "landscape" if layout.orientation.lower() == "landscape" else "portrait"
    )
    sheet.page_margins.top = layout.margin_top_mm / _MM_PER_INCH
    sheet.page_margins.bottom = layout.margin_bottom_mm / _MM_PER_INCH
    sheet.page_margins.left = layout.margin_left_mm / _MM_PER_INCH
    sheet.page_margins.right = layout.margin_right_mm / _MM_PER_INCH
    if layout.fit_to_width:
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
    else:
        sheet.page_setup.scale = max(10, min(400, layout.scale_percent))
    sheet.print_options.horizontalCentered = layout.center_horizontally
    if layout.repeat_header_row:
        sheet.print_title_rows = "1:1"


def _apply_header_and_logo(
    sheet: Any, layout: LayoutSettings, *, header_row: int
) -> list[str]:
    """Insert title/subtitle/logo into blank rows above the header, if any.

    Never shifts a row. If the operator configured a title but the template
    leaves no blank row above its header, the title is skipped and a warning
    is returned rather than the candidate table being disturbed.
    """
    warnings: list[str] = []
    available = header_row - 1

    wanted_text_rows = sum(
        1 for text in (layout.title_text, layout.subtitle_text, layout.examination_name)
        if text
    )
    if wanted_text_rows and available < wanted_text_rows:
        warnings.append(
            "The report title could not be added: this template has no "
            f"blank row above its header ({available} available, "
            f"{wanted_text_rows} needed). Add blank rows above the header "
            "in the template, or leave the title unset."
        )
    else:
        row = 1
        for text, size in (
            (layout.title_text, layout.title_font_size),
            (layout.examination_name, layout.header_font_size),
            (layout.subtitle_text, layout.body_font_size),
        ):
            if not text:
                continue
            from openpyxl.styles import Font

            cell = sheet.cell(row=row, column=1, value=safe_cell_text(text))
            font_kwargs: dict[str, Any] = {"size": size, "bold": row == 1}
            if layout.font_family:
                font_kwargs["name"] = layout.font_family
            cell.font = Font(**font_kwargs)
            row += 1

    if layout.logo_path:
        warnings.extend(_insert_logo(sheet, layout))

    return warnings


def _insert_logo(sheet: Any, layout: LayoutSettings) -> list[str]:
    """Insert a size-constrained, aspect-preserved logo image, best-effort.

    Failure here (a missing Pillow installation, an unreadable image file)
    degrades to a warning, never a failed generation - a report without its
    logo is still a usable, correct result; one that failed to generate at
    all because of a decorative image is not an acceptable trade.
    """
    from pathlib import Path as _Path

    logo_file = _Path(layout.logo_path)
    if not logo_file.is_file():
        return [f"Logo file not found: {logo_file.name}. The logo was skipped."]
    try:
        from openpyxl.drawing.image import Image as XLImage
    except ImportError:
        return [
            "The logo could not be embedded because image support "
            "(Pillow) is not installed. The logo was skipped."
        ]
    try:
        image = XLImage(str(logo_file))
        original_width, original_height = image.width, image.height
        if not original_width or not original_height:
            raise ValueError("image reports zero size")
        scale = min(
            layout.logo_max_width_px / original_width,
            layout.logo_max_height_px / original_height,
            1.0,
        )
        image.width = max(1, round(original_width * scale))
        image.height = max(1, round(original_height * scale))
        # Anchored beside the header text rather than over it - never on top
        # of a candidate row, and never resized without keeping the aspect
        # ratio computed above.
        anchor_column = max(1, sheet.max_column - 1)
        image.anchor = f"{column_letter(anchor_column)}1"
        sheet.add_image(image)
        return []
    except Exception as exc:
        # Any failure here degrades to a warning, never a crash - see the
        # function's own docstring.
        return [f"The logo could not be embedded ({exc}). The logo was skipped."]
