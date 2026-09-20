"""Synthetic result/absentee workbooks for Phase 9 tests.

Purpose:
    Build workbooks matching the phase brief's supplied sample ("Absentee-
    Sample(1).xlsx": ``5.AD-E-1.Rollwise(All)``, ``A1:E230``, columns
    Sl.No./Roll No./Name/Total (90)/Merit) and its documented variations,
    without checking in a workbook that might have carried real student data.

Why generated rather than a checked-in fixture file:
    No sanitised copy of the supplied sample was actually available to build
    from in this environment, and §3 of the brief is explicit that no
    workbook containing real student information may be committed. Building
    the structure programmatically, parametrised over every variation §38
    asks for (a different header row, a different sheet name, extra
    decorative rows, a different absence token, ...), covers strictly more
    ground than one static file could - and never risks the thing §3 forbids.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SAMPLE_HEADERS: tuple[str, ...] = ("Sl.No.", "Roll No.", "Name", "Total (90)", "Merit")
SAMPLE_CANDIDATE_COUNT = 229
"""The supplied sample's own row count (§3, §37) - tests assert nothing fewer
survives a round trip."""

DEFAULT_NAMES: tuple[str, ...] = (
    "MD. RAHIM UDDIN", "FATEMA BEGUM", "KAMAL HOSSAIN", "NASRIN AKTER",
    "ABDUL KARIM", "SHAHIDA AKTER", "JAHANGIR ALAM", "RUMANA YASMIN",
)


def _roll(index: int, *, digits: int = 8, prefix: str = "1500") -> str:
    """A plausible, distinctly non-real roll number with leading zeros kept."""
    return f"{prefix}{index:0{max(digits - len(prefix), 1)}d}"


def build_result_template(
    path: Path,
    *,
    candidate_count: int = SAMPLE_CANDIDATE_COUNT,
    sheet_name: str = "5.AD-E-1.Rollwise(All)",
    headers: Sequence[str] = SAMPLE_HEADERS,
    header_row: int = 1,
    title_rows: Sequence[str] = (),
    absent_every: int = 10,
    absent_token: str = "ABSENT",
    absent_merit_display: str = "---",
    roll_digits: int = 8,
    roll_prefix: str = "1500",
    names: Sequence[str] | None = None,
    unicode_names: bool = False,
    duplicate_roll_at: int | None = None,
    blank_name_at: int | None = None,
    extra_trailing_columns: int = 0,
    merge_header_first_row: bool = False,
    pre_filled_marks: Mapping[int, object] | None = None,
) -> Path:
    """Write a synthetic result template and return its path.

    Args:
        path: Where to write the ``.xlsx`` file.
        candidate_count: Number of candidate rows (default: the sample's own
            229).
        sheet_name: Worksheet name. The sample's own by default; tests for
            "a different worksheet name" (§38) pass something else.
        headers: Column headers, in order. Defaults to the sample's own
            wording (Sl.No./Roll No./Name/Total (90)/Merit); tests for
            "Roll instead of Roll No." etc. (§38) override the Roll header.
        header_row: 1-based row the header is written on. ``> 1`` exercises
            "header starting on row 3 rather than row 1" (§38).
        title_rows: Decorative text written above the header, one row per
            entry - the institution/examination title a real workbook
            carries. Ignored unless ``header_row`` leaves room for it.
        absent_every: Every Nth candidate (1-based position) is absent. ``0``
            means nobody is absent.
        absent_token: The text written in an absent candidate's marks cell -
            override for "ABS" / lowercase "absent" (§38).
        absent_merit_display: What the Merit column shows for an absent
            candidate. The sample's own ``"---"``.
        roll_digits: Total width of the generated Roll No., zero-padded.
        roll_prefix: Fixed leading digits of the generated Roll No. - a
            leading-zero-preserving numeric string by default.
        names: Candidate names to cycle through. Defaults to
            :data:`DEFAULT_NAMES`, all fictional.
        unicode_names: Use Bangla names instead, to exercise Unicode handling
            (§30, §38).
        duplicate_roll_at: 1-based candidate position whose Roll No. is
            repeated at the *next* position - "duplicate Roll No." (§38).
        blank_name_at: 1-based candidate position given an empty name cell -
            "empty candidate name if permitted" (§38).
        extra_trailing_columns: Extra, unrelated columns appended after the
            mapped ones - "extra columns" (§38).
        merge_header_first_row: Merge the header row's cells into one, the
            way a decorative title sometimes is - exercises a merged-cell
            header without breaking column detection.
        pre_filled_marks: ``{1-based position: value}`` to pre-fill a mark
            cell before generation - used by the "template already has
            leftover marks" style of test.

    Present candidates are left with a **blank** marks cell (§10: "blank
    marks cells for candidates whose marks are to be populated") unless
    ``pre_filled_marks`` says otherwise; the generator under test is what is
    expected to fill them in.
    """
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_name

    row_cursor = 1
    for title in title_rows:
        if row_cursor >= header_row:
            break
        sheet.cell(row=row_cursor, column=1, value=title)
        row_cursor += 1

    for column, text in enumerate(headers, start=1):
        sheet.cell(row=header_row, column=column, value=text)
    if merge_header_first_row and len(headers) > 1:
        sheet.merge_cells(
            start_row=header_row, start_column=1, end_row=header_row, end_column=1
        )

    pool = names or (
        _bangla_names() if unicode_names else DEFAULT_NAMES
    )

    rolls: list[str] = [
        _roll(index, digits=roll_digits, prefix=roll_prefix)
        for index in range(1, candidate_count + 1)
    ]
    if duplicate_roll_at is not None and 1 <= duplicate_roll_at < candidate_count:
        rolls[duplicate_roll_at] = rolls[duplicate_roll_at - 1]

    for position in range(1, candidate_count + 1):
        row = header_row + position
        roll = rolls[position - 1]
        name = "" if position == blank_name_at else pool[(position - 1) % len(pool)]
        is_absent = absent_every > 0 and position % absent_every == 0

        sheet.cell(row=row, column=1, value=position)
        sheet.cell(row=row, column=2, value=roll)
        sheet.cell(row=row, column=3, value=name)
        if is_absent:
            sheet.cell(row=row, column=4, value=absent_token)
            sheet.cell(row=row, column=5, value=absent_merit_display)
        elif pre_filled_marks and position in pre_filled_marks:
            sheet.cell(row=row, column=4, value=pre_filled_marks[position])
        # else: blank marks cell, exactly as the sample leaves it.
        for extra in range(extra_trailing_columns):
            sheet.cell(row=row, column=6 + extra, value=f"extra{extra}")

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    workbook.close()
    return path


def _bangla_names() -> tuple[str, ...]:
    """A handful of fictional Bangla names, for Unicode-handling tests."""
    return (
        "মোহাম্মদ রহিম উদ্দিন",
        "ফাতেমা বেগম",
        "কামাল হোসেন",
        "নাসরিন আক্তার",
    )


def read_all_cells(path: Path, sheet: str | None = None) -> list[list[object]]:
    """Read every cell of one worksheet, for assertions in tests."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=False)
    try:
        worksheet = workbook[sheet] if sheet else workbook.active
        return [list(row) for row in worksheet.iter_rows(values_only=True)]
    finally:
        workbook.close()


def sha256_of(path: Path) -> str:
    """SHA-256 hash of a file's bytes, for the never-modify-the-template proof."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
