"""Ranking, safe text and readiness vocabulary (Phase 9).

Scope:
    :mod:`omr_scanner.domain.reporting` in isolation - pure functions over
    value objects. No database, no GUI, no openpyxl.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from omr_scanner.domain.reporting import (
    ABSENT_RANK_DISPLAY,
    ReadinessIssue,
    ReadinessIssueKind,
    ReadinessReport,
    column_letter,
    compute_ranks,
    header_matches_maximum,
    rank_formula,
    safe_cell_text,
    safe_filename_component,
    total_header_for,
)


# ----------------------------------------------------------------------
class TestCompetitionRanking:
    """The brief's own worked example, and the edge cases around it."""

    def test_the_briefs_worked_example(self):
        scores = [
            ("a", Fraction(90)),
            ("b", Fraction(88)),
            ("c", Fraction(88)),
            ("d", Fraction(85)),
        ]
        assert compute_ranks(scores) == {"a": 1, "b": 2, "c": 2, "d": 4}

    def test_unique_marks(self):
        scores = [("a", Fraction(50)), ("b", Fraction(70)), ("c", Fraction(60))]
        assert compute_ranks(scores) == {"a": 3, "b": 1, "c": 2}

    def test_all_equal_marks_share_rank_one(self):
        scores = [("a", Fraction(10)), ("b", Fraction(10)), ("c", Fraction(10))]
        assert compute_ranks(scores) == {"a": 1, "b": 1, "c": 1}

    def test_a_rank_gap_after_three_way_tie(self):
        scores = [
            ("a", Fraction(90)),
            ("b", Fraction(90)),
            ("c", Fraction(90)),
            ("d", Fraction(80)),
        ]
        assert compute_ranks(scores)["d"] == 4

    def test_zero_mark_is_ranked_normally(self):
        scores = [("a", Fraction(0)), ("b", Fraction(5))]
        assert compute_ranks(scores) == {"a": 2, "b": 1}

    def test_decimal_marks(self):
        scores = [("a", Fraction(725, 10)), ("b", Fraction(70)), ("c", Fraction(725, 10))]
        ranks = compute_ranks(scores)
        assert ranks["a"] == ranks["c"] == 1
        assert ranks["b"] == 3

    def test_negative_marks_still_rank_correctly(self):
        scores = [("a", Fraction(-5)), ("b", Fraction(2)), ("c", Fraction(-5))]
        ranks = compute_ranks(scores)
        assert ranks["b"] == 1
        assert ranks["a"] == ranks["c"] == 2

    def test_absentees_excluded_receive_no_rank(self):
        scores = [("a", Fraction(90)), ("b", None), ("c", Fraction(80))]
        ranks = compute_ranks(scores)
        assert ranks["b"] is None
        assert ranks["a"] == 1 and ranks["c"] == 2

    def test_an_absentee_does_not_shift_the_ranks_around_them(self):
        # Removing a non-participant must not change anyone else's rank -
        # equivalent to RANK.EQ simply skipping non-numeric cells.
        with_absentee = compute_ranks(
            [("a", Fraction(90)), ("b", None), ("c", Fraction(80))]
        )
        without = compute_ranks([("a", Fraction(90)), ("c", Fraction(80))])
        assert with_absentee["a"] == without["a"]
        assert with_absentee["c"] == without["c"]

    def test_every_candidate_is_present_in_the_result(self):
        scores = [("a", Fraction(1)), ("b", None)]
        assert set(compute_ranks(scores)) == {"a", "b"}

    def test_is_deterministic_across_repeated_calls(self):
        scores = [("a", Fraction(88)), ("b", Fraction(88)), ("c", Fraction(90))]
        first = compute_ranks(scores)
        for _ in range(5):
            assert compute_ranks(scores) == first

    def test_a_large_field_ranks_in_reasonable_time(self):
        import time

        scores = [(str(i), Fraction(i % 101)) for i in range(20_000)]
        started = time.monotonic()
        ranks = compute_ranks(scores)
        elapsed = time.monotonic() - started
        assert len(ranks) == 20_000
        assert elapsed < 5.0, f"ranking 20,000 candidates took {elapsed:.2f}s"


# ----------------------------------------------------------------------
class TestColumnLetter:
    @pytest.mark.parametrize(
        ("index", "expected"),
        [(1, "A"), (4, "D"), (5, "E"), (26, "Z"), (27, "AA"), (28, "AB"), (52, "AZ"), (53, "BA")],
    )
    def test_known_columns(self, index, expected):
        assert column_letter(index) == expected

    def test_zero_is_refused(self):
        with pytest.raises(ValueError, match="1 or greater"):
            column_letter(0)

    def test_negative_is_refused(self):
        with pytest.raises(ValueError, match="1 or greater"):
            column_letter(-1)


# ----------------------------------------------------------------------
class TestRankFormula:
    """The dynamically generated formula, against the brief's literal example."""

    def test_matches_the_briefs_literal_example_when_fed_its_own_numbers(self):
        formula = rank_formula(
            marks_column="D", first_data_row=2, last_data_row=230, row=2
        )
        assert formula == (
            '=IF(OR(UPPER(TRIM(D2))="ABSENT",UPPER(TRIM(D2))="ABS"),"---",'
            'IF(ISNUMBER(D2),RANK.EQ(D2,$D$2:$D$230,0),""))'
        )

    def test_the_column_is_never_hard_coded_to_d(self):
        formula = rank_formula(marks_column="F", first_data_row=5, last_data_row=50, row=10)
        assert "F10" in formula
        assert "$F$5:$F$50" in formula
        assert "D" not in formula.replace("RANK", "").replace("RANd", "")

    def test_the_row_range_is_never_hard_coded_to_230(self):
        formula = rank_formula(marks_column="D", first_data_row=2, last_data_row=97, row=50)
        assert "$D$2:$D$97" in formula
        assert "230" not in formula

    def test_the_range_excludes_the_header_row(self):
        formula = rank_formula(marks_column="D", first_data_row=2, last_data_row=10, row=2)
        assert "$D$1:" not in formula
        assert "$D$2:$D$10" in formula

    def test_absence_tokens_are_configurable(self):
        formula = rank_formula(
            marks_column="D",
            first_data_row=2,
            last_data_row=10,
            row=2,
            absence_tokens=("N/A",),
        )
        assert 'UPPER(TRIM(D2))="N/A"' in formula
        assert "ABSENT" not in formula

    def test_result_string_for_absence_is_three_dashes(self):
        formula = rank_formula(marks_column="D", first_data_row=2, last_data_row=10, row=2)
        assert f'"{ABSENT_RANK_DISPLAY}"' in formula


# ----------------------------------------------------------------------
class TestSafeCellText:
    """Spreadsheet-injection mitigation for user-controlled text fields."""

    @pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
    def test_a_dangerous_prefix_is_quoted(self, prefix):
        text = f"{prefix}cmd|'/c calc'!A1"
        assert safe_cell_text(text) == "'" + text

    def test_ordinary_text_is_untouched(self):
        assert safe_cell_text("MD. RAHIM UDDIN") == "MD. RAHIM UDDIN"

    def test_a_hyphenated_name_is_quoted_even_though_it_is_legitimate(self):
        # Safety over convenience: a name that happens to start with a hyphen
        # is rare and the quote is invisible in Excel; a live formula from an
        # unsanitised name is not something to risk for either.
        assert safe_cell_text("-Test Name") == "'-Test Name"

    def test_none_becomes_empty_string(self):
        assert safe_cell_text(None) == ""

    def test_empty_string_is_untouched(self):
        assert safe_cell_text("") == ""

    def test_a_leading_tab_used_for_injection_is_stripped(self):
        assert not safe_cell_text("\t=cmd()").startswith("\t")

    def test_unicode_text_is_preserved(self):
        name = "মোহাম্মদ রহিম"
        assert safe_cell_text(name) == name


# ----------------------------------------------------------------------
class TestSafeFilenameComponent:
    @pytest.mark.parametrize(
        "character", [*'<>:"/\\|?*', chr(0), chr(31)]
    )
    def test_every_invalid_character_is_replaced(self, character):
        cleaned = safe_filename_component(f"Set{character}A")
        assert character not in cleaned

    def test_trailing_dots_and_spaces_are_removed(self):
        assert safe_filename_component("Report.. ") == "Report"

    def test_a_fully_invalid_name_falls_back(self):
        assert safe_filename_component("///") == "report"

    def test_a_custom_fallback_is_honoured(self):
        assert safe_filename_component("***", fallback="set") == "set"

    def test_unicode_is_preserved(self):
        assert safe_filename_component("পরীক্ষা") == "পরীক্ষা"

    def test_a_very_long_name_is_truncated(self):
        cleaned = safe_filename_component("x" * 500)
        assert len(cleaned) <= 180


# ----------------------------------------------------------------------
class TestTotalHeader:
    def test_a_whole_number_maximum(self):
        assert total_header_for(Fraction(90)) == "Total (90)"

    def test_a_fractional_maximum_is_shown_exactly(self):
        assert total_header_for(Fraction(725, 10)) == "Total (72.50)"

    def test_a_matching_header_is_recognised(self):
        assert header_matches_maximum("Total (90)", Fraction(90)) is True

    def test_a_differently_worded_but_correct_header_is_recognised(self):
        assert header_matches_maximum("Marks (out of 90)", Fraction(90)) is True

    def test_a_wrong_header_is_not_recognised(self):
        assert header_matches_maximum("Total (90)", Fraction(100)) is False

    def test_a_fractional_maximum_header_match(self):
        assert header_matches_maximum("Total (72.50)", Fraction(725, 10)) is True


# ----------------------------------------------------------------------
class TestReadinessReport:
    def test_no_issues_is_ready(self):
        assert ReadinessReport(set_code="A").is_ready is True

    def test_a_blocking_issue_is_not_ready(self):
        report = ReadinessReport(
            set_code="A",
            issues=(ReadinessIssue(ReadinessIssueKind.NO_TEMPLATE, "no template"),),
        )
        assert report.is_ready is False

    def test_a_non_blocking_issue_still_allows_export(self):
        report = ReadinessReport(
            set_code="A",
            issues=(
                ReadinessIssue(
                    ReadinessIssueKind.STALE_RESULT, "stale", blocking=False
                ),
            ),
        )
        assert report.is_ready is True
        assert report.has_warnings is True

    def test_describe_lists_every_message(self):
        report = ReadinessReport(
            set_code="A",
            issues=(
                ReadinessIssue(ReadinessIssueKind.NO_TEMPLATE, "first"),
                ReadinessIssue(ReadinessIssueKind.NO_VERIFIED_KEY, "second"),
            ),
        )
        assert report.describe() == ("first", "second")

    def test_by_kind_filters(self):
        report = ReadinessReport(
            set_code="A",
            issues=(
                ReadinessIssue(ReadinessIssueKind.PRESENT_WITHOUT_SCORE, "x", roll="1"),
                ReadinessIssue(ReadinessIssueKind.PRESENT_WITHOUT_SCORE, "y", roll="2"),
                ReadinessIssue(ReadinessIssueKind.NO_TEMPLATE, "z"),
            ),
        )
        found = report.by_kind(ReadinessIssueKind.PRESENT_WITHOUT_SCORE)
        assert len(found) == 2
        assert {item.roll for item in found} == {"1", "2"}
