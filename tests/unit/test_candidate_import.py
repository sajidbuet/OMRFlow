"""Reading a candidate/attendance list (Phase 7).

Scope:
    Parsing, column detection, identifier normalisation and validation, against
    real CSV fixtures and real workbooks written by the test. No database.

Why the workbooks are generated rather than committed:
    A binary fixture cannot be reviewed in a pull request, and the structures
    that matter here - a numeric roll number, several worksheets, the trailing
    blank rows Excel leaves behind - are worth seeing spelled out in the test
    that needs them. The one workbook that *is* committed is the sample the
    application ships, and it is read here precisely because a generated file
    could not prove the shipped one is importable.

Privacy:
    Every identifier and name in this module is fictional.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from omr_scanner.domain.reconciliation import (
    ABSENT_TOKENS,
    AttendanceState,
    is_absent_token,
)
from omr_scanner.services.candidate_import import (
    CandidateImportError,
    ColumnMapping,
    RosterIssueCode,
    list_worksheets,
    normalise_candidate_id,
    preview_roster,
    read_roster,
    sample_template_bytes,
    save_sample_template,
    suggest_mapping,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "reconciliation"


def make_workbook(
    path: Path, sheets: dict[str, Sequence[Sequence[object]]]
) -> Path:
    """Write a real .xlsx with the given sheets, and return its path."""
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(title=name)
        for row in rows:
            sheet.append(list(row))
    workbook.save(path)
    return path


SAMPLE_HEADER = ("Sl.No.", "Roll No.", "Name", "Total (90)", "Merit")


class TestCandidateIdNormalisation:
    """A candidate ID is an identifier, never a quantity."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (15000001, "15000001"),
            # The one that matters: Excel stores a roll number as a float, and
            # "15000001.0" would match no scanned sheet ever again.
            (15000001.0, "15000001"),
            ("15000001", "15000001"),
            ("  15000001  ", "15000001"),
            ("0015", "0015"),
            ("ABC-123", "ABC-123"),
            ("", ""),
            (None, ""),
            ("   ", ""),
        ],
    )
    def test_values_normalise_to_identifiers(self, value, expected):
        assert normalise_candidate_id(value) == expected

    def test_a_non_integral_float_is_not_rounded(self):
        # Rounding would be a transformation that can make two candidates one.
        assert normalise_candidate_id(12.5) == "12.5"

    def test_leading_zeros_in_text_survive(self):
        assert normalise_candidate_id("007") == "007"

    def test_distinct_ids_never_collapse(self):
        # The failure this function exists to prevent.
        assert normalise_candidate_id("0100") != normalise_candidate_id(100)

    def test_a_boolean_cell_does_not_become_a_digit(self):
        # True -> "1" could collide with a real roll number.
        assert normalise_candidate_id(True) == "True"


class TestAbsenceTokens:
    """ABSENT/ABS in any case and spacing; nothing else."""

    @pytest.mark.parametrize(
        "value",
        ["ABSENT", "absent", "Absent", " ABSENT", "ABSENT ", " ABSENT ",
         "ABS", "abs", "Abs", " ABS ", "\tabs\n"],
    )
    def test_these_mean_absent(self, value):
        assert is_absent_token(value) is True

    @pytest.mark.parametrize(
        "value",
        ["ABSENTEE", "ABSENCE", "ABS123", "absentee", "0", "55", "", None,
         "A", "PRESENT", "ABS ENT"],
    )
    def test_these_do_not(self, value):
        # Substring matching would turn the first three into absences, which is
        # why the comparison is against whole tokens.
        assert is_absent_token(value) is False

    def test_the_token_set_is_exactly_two_words(self):
        assert {"absent", "abs"} == ABSENT_TOKENS


class TestColumnSuggestion:
    def test_the_supplied_sample_structure_is_recognised(self):
        found = suggest_mapping(list(SAMPLE_HEADER))
        assert found.candidate_id == 1
        assert found.name == 2
        assert found.attendance == 3
        assert found.is_complete is True

    def test_total_90_is_matched_without_being_hard_coded(self):
        # A different paper's total must work with no code change.
        found = suggest_mapping(["Roll No.", "Name", "Total (75)"])
        assert found.attendance == 2

    def test_two_exact_candidate_id_columns_are_ambiguous(self):
        found = suggest_mapping(["Roll No.", "Candidate ID", "Name"])
        assert found.is_ambiguous is True
        assert found.ambiguous_candidate_id == (0, 1)
        assert found.candidate_id is None
        assert found.to_mapping() is None

    def test_an_exact_match_beats_a_loose_one(self):
        found = suggest_mapping(["Student Roll Number Field", "Roll No."])
        assert found.candidate_id == 1
        assert found.is_ambiguous is False

    def test_candidate_name_does_not_compete_with_candidate_id(self):
        found = suggest_mapping(["Candidate ID", "Candidate Name"])
        assert found.candidate_id == 0
        assert found.name == 1

    def test_nothing_recognisable_suggests_nothing(self):
        found = suggest_mapping(["Serial", "Comment"])
        assert found.candidate_id is None
        assert found.is_ambiguous is False


class TestReadingCsv:
    def test_a_plain_roster_reads(self):
        found = read_roster(FIXTURES / "roster_basic.csv")
        assert found.can_import is True
        assert len(found.candidates) == 5
        assert [item.candidate_id for item in found.candidates] == [
            "10001", "10002", "10003", "10004", "10005",
        ]
        assert found.expected_absent == 2
        assert found.expected_present == 3

    def test_a_blank_marks_cell_is_not_an_absence(self):
        found = read_roster(FIXTURES / "roster_basic.csv")
        blank = next(i for i in found.candidates if i.candidate_id == "10004")
        assert blank.imported_attendance is AttendanceState.PRESENT
        assert blank.imported_value == ""

    def test_every_absence_spelling_is_read(self):
        found = read_roster(FIXTURES / "roster_tokens.csv")
        absent = {
            item.candidate_id
            for item in found.candidates
            if item.imported_attendance is AttendanceState.ABSENT
        }
        assert absent == {
            "20001", "20002", "20003", "20004", "20005", "20006", "20007", "20008",
        }

    def test_the_near_misses_are_not_absences(self):
        found = read_roster(FIXTURES / "roster_tokens.csv")
        by_id = {item.candidate_id: item for item in found.candidates}
        for candidate_id in ("20012", "20013", "20014"):
            assert by_id[candidate_id].imported_attendance is AttendanceState.PRESENT

    def test_the_raw_cell_is_kept_verbatim(self):
        # So an operator can see what was actually written, spacing and all.
        found = read_roster(FIXTURES / "roster_tokens.csv")
        by_id = {item.candidate_id: item for item in found.candidates}
        assert by_id["20004"].imported_value == "ABSENT"
        assert by_id["20011"].imported_value == "0"

    def test_reordered_and_irrelevant_columns_are_handled(self):
        found = read_roster(FIXTURES / "roster_reordered.csv")
        assert found.mapping.candidate_id == 4
        assert [item.candidate_id for item in found.candidates] == [
            "50001", "50002", "50003",
        ]
        assert found.candidates[0].display_name == "CANDIDATE A"

    def test_a_utf8_bom_does_not_corrupt_the_first_header(self):
        # Excel writes a BOM by default; without utf-8-sig the ID column would
        # be named "﻿Roll No." and match nothing.
        found = read_roster(FIXTURES / "roster_bom.csv")
        assert found.mapping.candidate_id == 0
        assert found.candidates[0].candidate_id == "60001"

    def test_an_explicit_mapping_overrides_detection(self):
        found = read_roster(
            FIXTURES / "roster_basic.csv", ColumnMapping(candidate_id=0)
        )
        # Column 0 is Sl.No., so the "IDs" are 1..5. Nonsense, but the
        # operator's choice, and honoured exactly.
        assert [item.candidate_id for item in found.candidates] == [
            "1", "2", "3", "4", "5",
        ]
        assert found.expected_present == 0
        assert found.attendance_unknown == 5


class TestValidationRefusals:
    def test_duplicate_ids_block_the_import(self):
        found = read_roster(FIXTURES / "roster_duplicates.csv")
        assert found.can_import is False
        assert found.duplicate_ids == 1
        issue = next(
            i for i in found.issues if i.code is RosterIssueCode.DUPLICATE_ID
        )
        # The operator needs the ID and both rows to find the problem.
        assert "15000001" in issue.message
        assert "rows 2 and 4" in issue.message
        assert "unique" in issue.message

    def test_the_first_of_a_duplicate_pair_is_not_quietly_kept(self):
        # "Take the first" is the silent resolution the brief forbids; the
        # roster is refused outright instead.
        found = read_roster(FIXTURES / "roster_duplicates.csv")
        assert found.blocking_issues
        assert found.can_import is False

    def test_blank_candidate_ids_are_reported_per_row(self):
        found = read_roster(FIXTURES / "roster_blank_ids.csv")
        assert found.blank_ids == 2
        rows = [
            i.source_row for i in found.issues if i.code is RosterIssueCode.BLANK_ID
        ]
        assert rows == [3, 5]
        assert found.can_import is False

    def test_a_whitespace_only_id_counts_as_blank(self):
        found = read_roster(FIXTURES / "roster_blank_ids.csv")
        assert "   " not in [i.candidate_id for i in found.candidates]

    def test_no_candidate_id_column_says_what_to_do(self):
        with pytest.raises(CandidateImportError) as caught:
            read_roster(FIXTURES / "roster_no_id_column.csv")
        message = caught.value.user_message
        assert "could not identify a Candidate ID column" in message
        assert "Select the column" in message
        # The columns it did find, so the operator can pick one.
        assert "'Serial'" in message

    def test_ambiguous_id_columns_ask_rather_than_guess(self):
        with pytest.raises(CandidateImportError) as caught:
            read_roster(FIXTURES / "roster_ambiguous.csv")
        message = caught.value.user_message
        assert "More than one column" in message
        assert "'Roll No.'" in message
        assert "'Candidate ID'" in message

    def test_a_header_only_file_is_refused(self):
        with pytest.raises(CandidateImportError) as caught:
            read_roster(FIXTURES / "roster_header_only.csv")
        assert "no candidate rows" in caught.value.user_message

    def test_a_missing_file_is_refused_readably(self, tmp_path):
        with pytest.raises(CandidateImportError) as caught:
            read_roster(tmp_path / "nope.csv")
        assert "could not be found" in caught.value.user_message

    def test_an_unsupported_extension_names_what_is_supported(self, tmp_path):
        path = tmp_path / "roster.txt"
        path.write_text("Roll No.\n1\n", encoding="utf-8")
        with pytest.raises(CandidateImportError) as caught:
            read_roster(path)
        assert ".csv" in caught.value.user_message

    def test_a_legacy_xls_says_how_to_convert_it(self, tmp_path):
        path = tmp_path / "roster.xls"
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        with pytest.raises(CandidateImportError) as caught:
            read_roster(path)
        assert "Save As" in caught.value.user_message
        assert ".xlsx" in caught.value.user_message

    def test_a_directory_is_refused(self, tmp_path):
        folder = tmp_path / "roster.csv"
        folder.mkdir()
        with pytest.raises(CandidateImportError) as caught:
            read_roster(folder)
        assert "folder" in caught.value.user_message

    def test_a_mapping_naming_the_same_column_twice_is_refused(self):
        with pytest.raises(CandidateImportError) as caught:
            read_roster(
                FIXTURES / "roster_basic.csv",
                ColumnMapping(candidate_id=1, name=1),
            )
        assert "cannot also be" in caught.value.user_message

    def test_a_mapping_past_the_end_of_the_file_is_refused(self):
        with pytest.raises(CandidateImportError) as caught:
            read_roster(FIXTURES / "roster_basic.csv", ColumnMapping(candidate_id=99))
        assert "Choose the column again" in caught.value.user_message


class TestReadingXlsx:
    @pytest.fixture
    def sample_like(self, tmp_path):
        """A workbook shaped like the supplied sample, plus its traps."""
        return make_workbook(
            tmp_path / "roster.xlsx",
            {
                "Rollwise(All)": [
                    SAMPLE_HEADER,
                    (1, 15000001, "CANDIDATE A", "ABS", "---"),
                    (2, 15000002, "CANDIDATE B", "ABSENT", "---"),
                    (3, 15000003, "CANDIDATE C", "absent", "---"),
                    (4, 15000004, "CANDIDATE D", " Abs ", "---"),
                    (5, 15000005, "CANDIDATE E", None, None),
                    (6, 15000006, "CANDIDATE F", 72, "7"),
                    (7, "A0007", "CANDIDATE G", "ABSENTEE", "---"),
                ],
                "Notes": [("ignore", "me")],
            },
        )

    def test_the_supplied_column_structure_maps_automatically(self, sample_like):
        found = read_roster(sample_like, sheet="Rollwise(All)")
        assert found.mapping == ColumnMapping(candidate_id=1, name=2, attendance=3)

    def test_numeric_roll_numbers_do_not_gain_a_decimal_point(self, sample_like):
        found = read_roster(sample_like, sheet="Rollwise(All)")
        assert found.candidates[0].candidate_id == "15000001"
        assert not any("." in i.candidate_id for i in found.candidates)

    def test_a_text_candidate_id_survives(self, sample_like):
        found = read_roster(sample_like, sheet="Rollwise(All)")
        assert found.candidates[-1].candidate_id == "A0007"

    def test_every_absence_spelling_reads_from_a_workbook(self, sample_like):
        found = read_roster(sample_like, sheet="Rollwise(All)")
        absent = {
            i.candidate_id
            for i in found.candidates
            if i.imported_attendance is AttendanceState.ABSENT
        }
        assert absent == {"15000001", "15000002", "15000003", "15000004"}

    def test_blank_and_numeric_marks_are_not_absences(self, sample_like):
        found = read_roster(sample_like, sheet="Rollwise(All)")
        by_id = {i.candidate_id: i for i in found.candidates}
        assert by_id["15000005"].imported_attendance is AttendanceState.PRESENT
        assert by_id["15000006"].imported_attendance is AttendanceState.PRESENT
        assert by_id["A0007"].imported_attendance is AttendanceState.PRESENT

    def test_worksheets_are_listed_and_selectable(self, sample_like):
        assert list_worksheets(sample_like) == ("Rollwise(All)", "Notes")
        chosen = read_roster(sample_like, sheet="Rollwise(All)")
        assert chosen.sheet == "Rollwise(All)"

    def test_the_first_worksheet_is_used_when_none_is_named(self, sample_like):
        assert read_roster(sample_like).sheet == "Rollwise(All)"

    def test_a_worksheet_that_is_not_there_lists_the_ones_that_are(
        self, sample_like
    ):
        with pytest.raises(CandidateImportError) as caught:
            read_roster(sample_like, sheet="Nope")
        assert "Rollwise(All)" in caught.value.user_message

    def test_trailing_blank_rows_are_not_candidates(self, tmp_path):
        # Real workbooks report far more rows than they hold; the supplied
        # sample reports 230 and has 14.
        path = make_workbook(
            tmp_path / "padded.xlsx",
            {
                "Sheet1": [
                    ("Roll No.", "Name", "Total (90)"),
                    (70001, "CANDIDATE A", 55),
                    (None, None, None),
                    (None, None, None),
                ]
            },
        )
        found = read_roster(path)
        assert len(found.candidates) == 1
        assert found.rows_read == 1

    def test_an_empty_worksheet_is_refused(self, tmp_path):
        path = make_workbook(tmp_path / "empty.xlsx", {"Sheet1": []})
        with pytest.raises(CandidateImportError) as caught:
            read_roster(path)
        assert "no rows" in caught.value.user_message

    def test_a_corrupt_workbook_suggests_a_repair(self, tmp_path):
        path = tmp_path / "broken.xlsx"
        path.write_bytes(b"this is definitely not a zip archive")
        with pytest.raises(CandidateImportError) as caught:
            read_roster(path)
        assert "could not be opened as an Excel workbook" in caught.value.user_message

    def test_a_zip_that_is_not_a_workbook_is_refused(self, tmp_path):
        path = tmp_path / "notxlsx.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("hello.txt", "not a workbook")
        with pytest.raises(CandidateImportError) as caught:
            read_roster(path)
        assert caught.value.user_message


class TestPreview:
    def test_a_preview_returns_headers_rows_and_a_suggestion(self):
        found = preview_roster(FIXTURES / "roster_basic.csv")
        assert found.headers == SAMPLE_HEADER
        assert found.data_rows == 5
        assert len(found.rows) == 5
        assert found.suggestion.candidate_id == 1

    def test_a_preview_is_capped(self, tmp_path):
        path = tmp_path / "big.csv"
        lines = ["Roll No.,Name,Total (90)"]
        lines += [f"{80000 + i},CANDIDATE {i},55" for i in range(400)]
        path.write_text("\n".join(lines), encoding="utf-8")
        found = preview_roster(path, limit=20)
        assert len(found.rows) == 20
        # ...but it still knows how many there really are.
        assert found.data_rows == 400

    def test_preview_cells_render_numbers_without_decimals(self, tmp_path):
        path = make_workbook(
            tmp_path / "nums.xlsx",
            {"Sheet1": [("Roll No.", "Name"), (15000001, "CANDIDATE A")]},
        )
        found = preview_roster(path)
        assert found.rows[0][0] == "15000001"


class TestPackagedSample:
    """The workbook the application offers to hand the operator."""

    def test_the_sample_is_packaged_and_readable(self):
        payload = sample_template_bytes()
        assert payload[:2] == b"PK", "an .xlsx is a zip archive"
        assert len(payload) > 1000

    def test_the_sample_imports_cleanly_with_no_mapping_help(self, tmp_path):
        # The point of shipping it: an operator who starts from this file can
        # import it without touching a dropdown.
        destination = tmp_path / "sample.xlsx"
        save_sample_template(destination)
        found = read_roster(destination)
        assert found.can_import is True
        assert found.mapping.candidate_id is not None
        assert found.mapping.attendance is not None

    def test_the_sample_demonstrates_both_absence_spellings(self, tmp_path):
        destination = tmp_path / "sample.xlsx"
        save_sample_template(destination)
        found = read_roster(destination)
        raw = {i.imported_value.strip().casefold() for i in found.candidates}
        assert {"abs", "absent"} <= raw

    def test_the_sample_contains_only_placeholder_names(self, tmp_path):
        # A packaged file is shipped to every user; it must carry nobody's data.
        destination = tmp_path / "sample.xlsx"
        save_sample_template(destination)
        found = read_roster(destination)
        assert {i.display_name for i in found.candidates} == {
            "CANDIDATE NAME GOES HERE"
        }

    def test_saving_does_not_alter_the_packaged_file(self, tmp_path):
        before = sample_template_bytes()
        save_sample_template(tmp_path / "a.xlsx")
        save_sample_template(tmp_path / "b.xlsx")
        assert sample_template_bytes() == before

    def test_saving_writes_the_packaged_bytes_exactly(self, tmp_path):
        destination = tmp_path / "sample.xlsx"
        save_sample_template(destination)
        assert destination.read_bytes() == sample_template_bytes()

    def test_an_existing_destination_is_not_overwritten_silently(self, tmp_path):
        destination = tmp_path / "sample.xlsx"
        destination.write_bytes(b"mine")
        with pytest.raises(CandidateImportError) as caught:
            save_sample_template(destination)
        assert "already exists" in caught.value.user_message
        assert destination.read_bytes() == b"mine"

    def test_overwrite_is_possible_once_confirmed(self, tmp_path):
        destination = tmp_path / "sample.xlsx"
        destination.write_bytes(b"mine")
        save_sample_template(destination, overwrite=True)
        assert destination.read_bytes() == sample_template_bytes()

    def test_missing_parent_directories_are_created(self, tmp_path):
        destination = tmp_path / "deep" / "deeper" / "sample.xlsx"
        save_sample_template(destination)
        assert destination.exists()
