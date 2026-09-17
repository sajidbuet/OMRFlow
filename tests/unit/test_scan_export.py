"""Tests for the batch results CSV."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from tests.conftest import build_answer_sheet_template

from omr_scanner.errors import ReportingError
from omr_scanner.services.batch_processor import ProcessedScan
from omr_scanner.services.recognition_service import (
    AnswerView,
    FieldView,
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
)
from omr_scanner.services.scan_export import (
    BASE_COLUMNS,
    BOM_ENCODING,
    build_rows,
    export_scan_results,
    question_numbers,
    render_scan_results,
)


def answer(number: int, value: str, status: str = "resolved") -> AnswerView:
    return AnswerView(
        number=number,
        zone_id="questions_0",
        value=value,
        status=status,
        needs_review=status not in ("resolved", "blank"),
        top_fill=0.9,
        margin=0.8,
        confidence=1.0,
    )


def field_view(zone_id: str, value: str, *, status: str = "resolved") -> FieldView:
    return FieldView(
        zone_id=zone_id,
        label=zone_id.replace("_", " ").title(),
        field_type="numeric" if zone_id == "roll_number" else "set_code",
        value=value,
        status=status,
        needs_review=status != "resolved",
        characters=(),
    )


def scan(
    name: str,
    *,
    roll: str = "120317",
    set_code: str = "A",
    answers: tuple[AnswerView, ...] = (),
    outcome: RecognitionOutcome = RecognitionOutcome.COMPLETE,
    registration: RegistrationStatus = RegistrationStatus.REGISTERED,
    warnings: tuple[str, ...] = (),
) -> ScanResult:
    return ScanResult(
        source_path=Path("scans") / name,
        outcome=outcome,
        registration=registration,
        warnings=warnings,
        fields=(field_view("roll_number", roll), field_view("set_code", set_code)),
        answers=answers,
        identifier_zone_id="roll_number",
        set_code_zone_id="set_code",
    )


def processed(result: ScanResult, output_name: str = "") -> ProcessedScan:
    return ProcessedScan(result=result, output_name=output_name)


def parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text, newline="")))


@pytest.fixture
def template():
    return build_answer_sheet_template()


class TestQuestionColumns:
    def test_every_question_the_template_defines_gets_a_column(self, template):
        assert question_numbers(template) == tuple(range(1, 21))

    def test_columns_come_from_the_numbers_not_from_zone_order(self):
        template = build_answer_sheet_template(question_blocks=3, questions_per_block=4)
        reversed_zones = template.model_copy(update={"zones": tuple(reversed(template.zones))})
        assert question_numbers(reversed_zones) == tuple(range(1, 13))

    def test_a_template_with_no_questions_still_exports_its_base_columns(self):
        template = build_answer_sheet_template(question_blocks=0)
        assert question_numbers(template) == ()
        rows = build_rows([processed(scan("a.png"))], template)
        assert rows[0] == BASE_COLUMNS


class TestColumnLayout:
    def test_the_header_is_the_documented_order(self, template):
        header = build_rows([], template)[0]
        assert header[:7] == BASE_COLUMNS
        assert header[7:] == tuple(f"Q{number}" for number in range(1, 21))

    def test_a_row_lines_up_with_the_header(self, template):
        answers = tuple(answer(number, "B") for number in range(1, 21))
        rows = build_rows([processed(scan("a.png"), "120317.png")], template)
        assert len(rows[1]) == len(rows[0])

        rows = build_rows(
            [processed(scan("a.png", answers=answers), "120317.png")], template
        )
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["original_filename"] == "a.png"
        assert record["output_filename"] == "120317.png"
        assert record["roll"] == "120317"
        assert record["set_code"] == "A"
        assert record["registration_status"] == "registered"
        assert record["recognition_status"] == "complete"
        assert record["Q1"] == "B"
        assert record["Q20"] == "B"

    def test_a_question_the_scan_has_no_answer_for_is_an_empty_cell(self, template):
        rows = build_rows([processed(scan("a.png", answers=(answer(1, "B"),)))], template)
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["Q1"] == "B"
        assert record["Q2"] == ""

    def test_rows_follow_the_order_they_were_given_in(self, template):
        batch = [processed(scan(f"{index}.png")) for index in range(5)]
        rows = build_rows(batch, template)
        assert [row[0] for row in rows[1:]] == [f"{index}.png" for index in range(5)]


class TestQuestionCellConventions:
    @pytest.mark.parametrize(
        ("value", "status", "expected"),
        [
            ("B", "resolved", "B"),
            ("", "blank", ""),
            ("B-D", "multiple", "B-D"),
            ("B", "uncertain", "B?"),
            ("", "uncertain", "?"),
            ("", "unreadable", "?"),
        ],
    )
    def test_each_outcome_reads_distinctly_in_its_own_cell(
        self, template, value, status, expected
    ):
        rows = build_rows(
            [processed(scan("a.png", answers=(answer(1, value, status),)))], template
        )
        assert dict(zip(rows[0], rows[1], strict=True))["Q1"] == expected

    def test_a_double_mark_never_loses_one_of_its_marks(self, template):
        rows = build_rows(
            [processed(scan("a.png", answers=(answer(1, "A-C", "multiple"),)))], template
        )
        assert dict(zip(rows[0], rows[1], strict=True))["Q1"] == "A-C"


class TestIdentifierAndStatusColumns:
    def test_an_unreliable_roll_number_is_exported_as_read_placeholders_and_all(
        self, template
    ):
        # The CSV records what the engine saw; it does not blank the value out,
        # because a reviewer needs to know it read "12?317" and not nothing.
        result = ScanResult(
            source_path=Path("a.png"),
            outcome=RecognitionOutcome.REVIEW,
            registration=RegistrationStatus.REGISTERED,
            fields=(field_view("roll_number", "12?317", status="uncertain"),),
            identifier_zone_id="roll_number",
        )
        rows = build_rows([processed(result, "UNRESOLVED_001.png")], template)
        record = dict(zip(rows[0], rows[1], strict=True))
        assert record["roll"] == "12?317"
        assert record["output_filename"] == "UNRESOLVED_001.png"
        assert record["recognition_status"] == "review"

    def test_a_failed_registration_is_visible_in_its_own_column(self, template):
        result = scan(
            "a.png",
            outcome=RecognitionOutcome.REGISTRATION_FAILED,
            registration=RegistrationStatus.FAILED,
        )
        record = dict(zip(*build_rows([processed(result)], template)[:2], strict=True))
        assert record["registration_status"] == "registration_failed"
        assert record["recognition_status"] == "registration_failed"

    def test_the_warning_count_is_the_number_of_alignment_warnings(self, template):
        result = scan(
            "a.png",
            registration=RegistrationStatus.REGISTERED_WITH_WARNING,
            warnings=("marker near border", "unusual aspect ratio"),
        )
        record = dict(zip(*build_rows([processed(result)], template)[:2], strict=True))
        assert record["warning_count"] == "2"


class TestDuplicateFilenames:
    def test_three_scans_of_one_roll_each_record_their_own_output_name(self, template):
        batch = [
            processed(scan("first.png"), "120317.png"),
            processed(scan("second.png"), "120317_a.png"),
            processed(scan("third.png"), "120317_b.png"),
        ]
        rows = build_rows(batch, template)
        pairs = [(row[0], row[1], row[2]) for row in rows[1:]]
        assert pairs == [
            ("first.png", "120317.png", "120317"),
            ("second.png", "120317_a.png", "120317"),
            ("third.png", "120317_b.png", "120317"),
        ]
        # The roll column repeats - that is the point: it is how a marker sees
        # that three different papers claimed the same candidate.
        assert len({row[1] for row in rows[1:]}) == 3


class TestEscapingAndEncoding:
    def test_a_comma_in_a_value_is_quoted_not_split(self, template):
        result = scan("Smith, J.png")
        text = render_scan_results([processed(result, "out,put.png")], template)
        assert '"Smith, J.png"' in text
        parsed = parse(text)
        assert parsed[1][0] == "Smith, J.png"
        assert parsed[1][1] == "out,put.png"

    def test_a_quote_in_a_value_is_doubled(self, template):
        text = render_scan_results([processed(scan('say "hi".png'))], template)
        assert parse(text)[1][0] == 'say "hi".png'

    def test_a_newline_in_a_value_stays_inside_one_field(self, template):
        text = render_scan_results([processed(scan("two\nlines.png"))], template)
        assert parse(text)[1][0] == "two\nlines.png"

    def test_non_ascii_values_round_trip(self, tmp_path: Path, template):
        # The repository's own sample sheet is labelled in Bengali.
        result = scan("পরীক্ষা.png", roll="১২৩৪৫৬", set_code="খ")
        path = export_scan_results([processed(result, "১২৩৪৫৬.png")], template, tmp_path / "r.csv")
        with path.open(encoding=BOM_ENCODING, newline="") as stream:
            record = next(csv.DictReader(stream))
        assert record["original_filename"] == "পরীক্ষা.png"
        assert record["roll"] == "১২৩৪৫৬"
        assert record["set_code"] == "খ"

    def test_the_file_is_written_with_a_bom_by_default(self, tmp_path: Path, template):
        path = export_scan_results([processed(scan("a.png"))], template, tmp_path / "r.csv")
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_the_bom_can_be_turned_off(self, tmp_path: Path, template):
        path = export_scan_results(
            [processed(scan("a.png"))], template, tmp_path / "r.csv", include_bom=False
        )
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert path.read_text(encoding="utf-8").startswith("original_filename")

    def test_records_are_not_separated_by_blank_lines(self, tmp_path: Path, template):
        # `csv` writes its own CRLF; letting Python translate them as well is the
        # classic Windows "every other row is empty" bug.
        path = export_scan_results(
            [processed(scan(f"{i}.png")) for i in range(3)], template, tmp_path / "r.csv"
        )
        text = path.read_text(encoding=BOM_ENCODING)
        assert "\r\n\r\n" not in text
        assert len([line for line in text.splitlines() if line]) == 4


class TestWritingTheFile:
    def test_a_missing_suffix_becomes_csv(self, tmp_path: Path, template):
        path = export_scan_results([], template, tmp_path / "results")
        assert path.name == "results.csv"

    def test_an_explicit_suffix_is_respected(self, tmp_path: Path, template):
        path = export_scan_results([], template, tmp_path / "results.txt")
        assert path.name == "results.txt"

    def test_a_missing_parent_directory_is_created(self, tmp_path: Path, template):
        path = export_scan_results([], template, tmp_path / "new" / "deep" / "r.csv")
        assert path.exists()

    def test_an_unwritable_destination_reports_a_user_facing_error(
        self, tmp_path: Path, template
    ):
        # A directory where the file should go: the open() must fail.
        (tmp_path / "results.csv").mkdir()
        with pytest.raises(ReportingError) as excinfo:
            export_scan_results([], template, tmp_path / "results.csv")
        assert "results.csv" in excinfo.value.user_message

    def test_exporting_the_same_batch_twice_produces_identical_bytes(
        self, tmp_path: Path, template
    ):
        batch = [
            processed(scan("a.png", answers=(answer(1, "B"), answer(2, "A-C", "multiple")))),
            processed(scan("b.png"), "120317_a.png"),
        ]
        first = export_scan_results(batch, template, tmp_path / "one.csv")
        second = export_scan_results(batch, template, tmp_path / "two.csv")
        assert first.read_bytes() == second.read_bytes()

    def test_an_empty_batch_still_writes_the_header(self, tmp_path: Path, template):
        path = export_scan_results([], template, tmp_path / "r.csv")
        with path.open(encoding=BOM_ENCODING, newline="") as stream:
            rows = list(csv.reader(stream))
        assert len(rows) == 1
        assert tuple(rows[0][:7]) == BASE_COLUMNS
