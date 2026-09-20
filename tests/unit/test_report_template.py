"""Reading and mapping a result/absentee template (Phase 9).

Scope:
    :mod:`omr_scanner.services.report_template` against generated workbooks
    reproducing the phase brief's supplied sample and its documented
    variations (§37, §38).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.report_fixtures import (
    SAMPLE_CANDIDATE_COUNT,
    SAMPLE_HEADERS,
    build_result_template,
)

from omr_scanner.services.report_template import (
    ReportColumnMapping,
    ReportTemplateError,
    list_worksheets,
    preview_template,
    read_template,
    suggest_mapping,
)

if TYPE_CHECKING:
    from pathlib import Path


# ----------------------------------------------------------------------
class TestColumnMapping:
    def test_the_samples_own_headers_map_unambiguously(self):
        suggestion = suggest_mapping(list(SAMPLE_HEADERS))
        assert suggestion.is_complete
        mapping = suggestion.to_mapping()
        assert mapping == ReportColumnMapping(roll=1, marks=3, serial=0, name=2, rank=4)

    def test_roll_instead_of_roll_no(self):
        suggestion = suggest_mapping(["Sl.No.", "Roll", "Name", "Total (90)", "Merit"])
        assert suggestion.roll == 1

    def test_student_id_is_recognised(self):
        suggestion = suggest_mapping(["No.", "Student ID", "Name", "Marks", "Rank"])
        assert suggestion.roll == 1

    def test_marks_column_not_in_position_d(self):
        suggestion = suggest_mapping(["Name", "Roll No.", "Sl.No.", "Merit", "Total (90)"])
        assert suggestion.marks == 4
        assert suggestion.roll == 1

    def test_rank_column_not_in_position_e(self):
        suggestion = suggest_mapping(["Merit", "Sl.No.", "Roll No.", "Name", "Total (90)"])
        assert suggestion.rank == 0

    def test_two_equally_strong_roll_headers_are_ambiguous(self):
        suggestion = suggest_mapping(["Sl.No.", "Roll No.", "Candidate ID", "Total", "Merit"])
        assert suggestion.is_ambiguous
        assert set(suggestion.ambiguous_roll) == {1, 2}
        assert suggestion.to_mapping() is None

    def test_two_equally_strong_marks_headers_are_ambiguous(self):
        suggestion = suggest_mapping(["Sl.No.", "Roll No.", "Name", "Total", "Score"])
        assert suggestion.is_ambiguous
        assert set(suggestion.ambiguous_marks) == {3, 4}

    def test_extra_unrelated_columns_do_not_confuse_the_mapping(self):
        suggestion = suggest_mapping(
            ["Sl.No.", "Roll No.", "Name", "Total (90)", "Merit", "Remarks", "Signature"]
        )
        assert suggestion.to_mapping() == ReportColumnMapping(
            roll=1, marks=3, serial=0, name=2, rank=4
        )

    def test_no_rank_column_is_reported_as_missing_not_guessed(self):
        suggestion = suggest_mapping(["Sl.No.", "Roll No.", "Name", "Total (90)"])
        assert suggestion.rank is None
        # Still usable for a preview/draft - only final export needs a rank column.
        assert suggestion.to_mapping() is not None

    def test_a_mapping_reusing_one_column_twice_is_refused(self):
        with pytest.raises(ReportTemplateError, match="mapped to two fields"):
            ReportColumnMapping(roll=1, marks=1).validate(5)

    def test_a_mapping_column_outside_the_sheet_is_refused(self):
        with pytest.raises(ReportTemplateError, match="outside the sheet"):
            ReportColumnMapping(roll=1, marks=99).validate(5)


# ----------------------------------------------------------------------
class TestReadingTheSample:
    """Structural fidelity against the brief's supplied sample (§37)."""

    @pytest.fixture
    def sample(self, tmp_path: Path) -> Path:
        return build_result_template(tmp_path / "sample.xlsx")

    def test_the_worksheet_is_detected(self, sample: Path):
        names = list_worksheets(sample)
        assert names == ("5.AD-E-1.Rollwise(All)",)

    def test_the_header_row_is_detected(self, sample: Path):
        preview = preview_template(sample)
        assert preview.header_row_index == 0
        assert preview.headers == SAMPLE_HEADERS

    def test_no_candidate_row_is_lost(self, sample: Path):
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(sample, mapping)
        assert len(roster.rows) == SAMPLE_CANDIDATE_COUNT

    def test_roll_numbers_keep_their_leading_zeros(self, sample: Path):
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(sample, mapping)
        for roll in roster.roll_numbers:
            assert len(roll) == 8, roll

    def test_names_are_unchanged(self, sample: Path):
        from tests.report_fixtures import DEFAULT_NAMES

        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(sample, mapping)
        assert roster.rows[0].name == DEFAULT_NAMES[0]

    def test_row_order_matches_the_template(self, sample: Path):
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(sample, mapping)
        rolls = roster.roll_numbers
        assert rolls == tuple(sorted(rolls, key=rolls.index)), "already in file order"
        assert list(rolls) == sorted(rolls), "the fixture's own rolls are sequential"

    def test_absent_candidates_remain_in_the_roster(self, sample: Path):
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(sample, mapping)
        absentees = [row for row in roster.rows if row.marks_says_absent]
        assert len(absentees) == SAMPLE_CANDIDATE_COUNT // 10

    def test_the_template_file_is_never_modified(self, sample: Path):
        from tests.report_fixtures import sha256_of

        before = sha256_of(sample)
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        read_template(sample, mapping)
        preview_template(sample)
        after = sha256_of(sample)
        assert before == after


# ----------------------------------------------------------------------
class TestTemplateVariations:
    """Every shape §38 asks for."""

    def test_a_different_worksheet_name(self, tmp_path: Path):
        path = build_result_template(tmp_path / "t.xlsx", sheet_name="Set A Result")
        assert list_worksheets(path) == ("Set A Result",)
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(path, mapping)
        assert len(roster.rows) == SAMPLE_CANDIDATE_COUNT

    def test_header_starting_on_row_three(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx",
            header_row=3,
            title_rows=("EXAMINATION BOARD", "Result - Set A"),
            candidate_count=10,
        )
        preview = preview_template(path)
        assert preview.header_row_index == 2
        mapping = suggest_mapping(list(preview.headers)).to_mapping()
        roster = read_template(path, mapping)
        assert len(roster.rows) == 10
        assert roster.header_row_number == 3
        assert roster.first_data_row_number == 4

    def test_additional_decorative_rows_do_not_break_detection(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx",
            header_row=5,
            title_rows=("Board", "College", "Examination", "Session 2026"),
            candidate_count=5,
        )
        preview = preview_template(path)
        assert preview.headers == SAMPLE_HEADERS

    def test_extra_columns_are_ignored(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", extra_trailing_columns=3, candidate_count=5
        )
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(path, mapping)
        assert len(roster.rows) == 5

    def test_merged_header_cells_do_not_break_detection(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", merge_header_first_row=True, candidate_count=5
        )
        preview = preview_template(path)
        assert preview.headers[0] == "Sl.No."

    def test_absent_value_abs(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", absent_token="ABS", candidate_count=20
        )
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(path, mapping)
        assert any(row.marks_says_absent for row in roster.rows)

    def test_lowercase_absent(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", absent_token="absent", candidate_count=20
        )
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(path, mapping)
        assert any(row.marks_says_absent for row in roster.rows)

    def test_leading_zero_roll_numbers(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", roll_digits=8, roll_prefix="00", candidate_count=5
        )
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(path, mapping)
        assert all(row.roll.startswith("00") for row in roster.rows)

    def test_unicode_candidate_names(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", unicode_names=True, candidate_count=4
        )
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(path, mapping)
        assert "মোহাম্মদ" in roster.rows[0].name

    def test_empty_candidate_name_if_permitted(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", blank_name_at=2, candidate_count=5
        )
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(path, mapping)
        assert roster.rows[1].name == ""
        assert len(roster.rows) == 5

    def test_duplicate_roll_no_is_kept_and_reported(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", duplicate_roll_at=2, candidate_count=5
        )
        mapping = suggest_mapping(list(SAMPLE_HEADERS)).to_mapping()
        roster = read_template(path, mapping)
        assert len(roster.rows) == 5, "nothing is silently dropped"
        assert len(roster.duplicate_rolls) == 1

    def test_a_malformed_workbook_is_reported_usefully(self, tmp_path: Path):
        path = tmp_path / "broken.xlsx"
        path.write_bytes(b"not really an xlsx file")
        with pytest.raises(ReportTemplateError, match="could not be opened"):
            preview_template(path)

    def test_a_missing_marks_column_cannot_be_mapped(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx", headers=("Sl.No.", "Roll No.", "Name"), candidate_count=3
        )
        preview = preview_template(path)
        assert preview.suggestion.marks is None
        assert preview.suggestion.to_mapping() is None

    def test_a_missing_rank_column_is_still_previewable(self, tmp_path: Path):
        path = build_result_template(
            tmp_path / "t.xlsx",
            headers=("Sl.No.", "Roll No.", "Name", "Total (90)"),
            candidate_count=3,
        )
        preview = preview_template(path)
        assert preview.suggestion.rank is None
        assert preview.suggestion.is_complete

    def test_a_nonexistent_file_is_reported_usefully(self, tmp_path: Path):
        with pytest.raises(ReportTemplateError, match="not found"):
            preview_template(tmp_path / "does_not_exist.xlsx")

    def test_a_non_xlsx_suffix_is_refused(self, tmp_path: Path):
        path = tmp_path / "template.csv"
        path.write_text("a,b,c")
        with pytest.raises(ReportTemplateError, match="Unsupported template suffix"):
            preview_template(path)
