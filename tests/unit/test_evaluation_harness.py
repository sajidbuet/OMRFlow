"""Tests for the ground-truth schema and the benchmark harness.

Why these are pure:
    The harness must be trustworthy before its numbers mean anything, and its
    logic - is this a false blank or a wrong option, does this metric count as
    a regression - is arithmetic over stored results. Testing it against
    hand-built results rather than real recognition keeps the test about the
    *scoring*, which is what would otherwise go quietly wrong.

A benchmark that is itself untested is a benchmark that will eventually tell
somebody their engine improved when it did not.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from tests.unit.test_recognition_contract import make_answer, make_field, make_result

from omr_scanner.evaluation.benchmark import (
    DEFAULT_TOLERANCE,
    BenchmarkSummary,
    ErrorCategory,
    compare_baseline,
    compare_result,
    evaluate,
    load_summary,
    write_report,
)
from omr_scanner.evaluation.ground_truth import (
    GROUND_TRUTH_SCHEMA_VERSION,
    DatasetManifest,
    SheetGroundTruth,
    load_ground_truth,
    load_ground_truth_directory,
    load_manifest,
    save_ground_truth,
    save_manifest,
)
from omr_scanner.recognition.models import FieldStatus, MarkStatus
from omr_scanner.services.recognition_models import (
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
)


def result_with(answers: dict[int, str], **kwargs: object) -> ScanResult:
    """A registered result whose questions read as ``answers``."""
    views = tuple(
        make_answer(
            number,
            value,
            MarkStatus.MULTIPLE.value
            if "-" in value
            else (MarkStatus.BLANK.value if value == "" else MarkStatus.RESOLVED.value),
        )
        for number, value in sorted(answers.items())
    )
    defaults = {
        "source_path": Path("scan.png"),
        "outcome": RecognitionOutcome.COMPLETE,
        "registration": RegistrationStatus.REGISTERED,
        "answers": views,
        "fields": (make_field("roll_number", "12", FieldStatus.RESOLVED.value),),
        "identifier_zone_id": "roll_number",
    }
    defaults.update(kwargs)
    return make_result(**defaults)


def truth_with(answers: dict[int, str], **kwargs: object) -> SheetGroundTruth:
    """Ground truth for ``scan.png`` with those answers.

    The roll defaults to the one :func:`result_with` reads, so a test that is
    about answers says nothing about identifiers, and a test that is about the
    identifier passes ``roll=`` explicitly.
    """
    kwargs.setdefault("roll", "12")
    return SheetGroundTruth(scan="scan.png", answers=answers, **kwargs)


class TestGroundTruthSchema:
    def test_it_round_trips_through_json(self, tmp_path: Path):
        truth = SheetGroundTruth(
            scan="scan0001.jpg",
            roll="2103123",
            set_code="A",
            answers={1: "B", 2: "", 3: "A-C"},
            ambiguous=(17,),
            human_verified=True,
            reviewer="exam office",
            dataset_version="2026-10",
            metadata={"case_kind": "difficult"},
        )
        path = save_ground_truth(truth, tmp_path / "scan0001.json")
        assert load_ground_truth(path) == truth

    def test_question_numbers_survive_being_json_object_keys(self, tmp_path: Path):
        # JSON keys are strings; a schema that forgot this would come back with
        # {"1": "B"} and every comparison would silently miss.
        truth = SheetGroundTruth(scan="a.png", answers={1: "B", 10: "C"})
        restored = load_ground_truth(save_ground_truth(truth, tmp_path / "a.json"))
        assert restored.answers == {1: "B", 10: "C"}

    def test_a_newer_schema_version_is_refused(self, tmp_path: Path):
        path = tmp_path / "a.json"
        path.write_text(
            json.dumps({"schema_version": GROUND_TRUTH_SCHEMA_VERSION + 1, "scan": "a.png"}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="newer than this build"):
            load_ground_truth(path)

    def test_a_non_numeric_question_key_is_refused_with_the_file_named(self, tmp_path: Path):
        path = tmp_path / "a.json"
        path.write_text(json.dumps({"scan": "a.png", "answers": {"Q1": "B"}}), encoding="utf-8")
        with pytest.raises(ValueError, match=r"a\.png"):
            load_ground_truth(path)

    def test_blank_and_multiple_questions_are_listed(self):
        truth = SheetGroundTruth(scan="a.png", answers={1: "B", 2: "", 3: "A-C", 4: ""})
        assert truth.blank_answers == (2, 4)
        assert truth.multiple_mark_answers == (3,)

    def test_a_directory_is_keyed_by_scan_stem(self, tmp_path: Path):
        save_ground_truth(SheetGroundTruth(scan="SYN_000001.png"), tmp_path / "SYN_000001.json")
        save_ground_truth(SheetGroundTruth(scan="SYN_000002.jpg"), tmp_path / "SYN_000002.json")
        loaded = load_ground_truth_directory(tmp_path)
        # By stem, so a dataset re-encoded as JPEG still matches its labels.
        assert sorted(loaded) == ["SYN_000001", "SYN_000002"]

    def test_a_missing_directory_says_so(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_ground_truth_directory(tmp_path / "nowhere")

    def test_a_manifest_records_how_a_dataset_was_made(self, tmp_path: Path):
        manifest = DatasetManifest(
            name="synthetic",
            version="2",
            created_at="2026-09-18T09:00:00+00:00",
            template="sheet.omrt",
            generator={"seed": 7, "profile": "mixed", "count": 20},
            entries=("SYN_000001.json",),
        )
        assert load_manifest(save_manifest(manifest, tmp_path / "manifest.json")) == manifest


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("expected", "actual", "category"),
        [
            ("", "B", ErrorCategory.FALSE_MARK),
            ("B", "", ErrorCategory.FALSE_BLANK),
            ("B", "C", ErrorCategory.WRONG_OPTION),
            ("A-C", "A", ErrorCategory.MISSED_MULTIPLE_MARK),
            ("A", "A-C", ErrorCategory.FALSE_MULTIPLE_MARK),
        ],
    )
    def test_each_kind_of_wrong_answer_is_named_for_what_to_do_about_it(
        self, expected: str, actual: str, category: ErrorCategory
    ):
        errors = compare_result(result_with({1: actual}), truth_with({1: expected}))
        assert [record.category for record in errors] == [category]
        assert errors[0].question == 1
        assert (errors[0].expected, errors[0].actual) == (expected, actual)

    def test_a_correct_sheet_produces_no_records(self):
        assert compare_result(result_with({1: "B", 2: ""}), truth_with({1: "B", 2: ""})) == []

    def test_a_wrong_roll_number_is_its_own_category(self):
        errors = compare_result(result_with({}), truth_with({}, roll="99"))
        assert [record.category for record in errors] == [ErrorCategory.ROLL_ERROR]

    def test_a_failed_sheet_is_one_processing_failure_not_a_hundred_wrong_answers(self):
        result = result_with(
            {}, outcome=RecognitionOutcome.ERROR, registration=RegistrationStatus.FAILED
        )
        errors = compare_result(result, truth_with(dict.fromkeys(range(1, 101), "B")))
        assert [record.category for record in errors] == [ErrorCategory.PROCESSING_FAILURE]

    def test_an_unregistered_sheet_is_an_alignment_error(self):
        result = result_with({}, registration=RegistrationStatus.FAILED)
        errors = compare_result(result, truth_with({1: "B"}))
        assert [record.category for record in errors] == [ErrorCategory.ALIGNMENT_ERROR]

    def test_a_sheet_expected_to_fail_and_failing_is_not_an_error(self):
        result = result_with({}, registration=RegistrationStatus.FAILED)
        assert compare_result(result, truth_with({}, expect_failure=True)) == []

    def test_a_sheet_expected_to_fail_but_read_anyway_is_reported(self):
        # Reading a page that should have been refused means it was rectified
        # on evidence that was not there; whatever it says came from somewhere.
        errors = compare_result(result_with({1: "B"}), truth_with({1: "B"}, expect_failure=True))
        assert [record.category for record in errors] == [ErrorCategory.ALIGNMENT_ERROR]

    def test_a_question_absent_from_ground_truth_is_not_checked(self):
        # A partially verified real sheet must not be scored on the parts
        # nobody checked.
        assert compare_result(result_with({1: "B", 2: "C"}), truth_with({1: "B"})) == []


class TestAmbiguousQuestions:
    def test_flagging_a_borderline_mark_counts_as_correct(self):
        result = result_with({1: ""})
        result = make_result(
            **{
                **{key: getattr(result, key) for key in ("source_path", "outcome", "registration")},
                "answers": (make_answer(1, "", MarkStatus.UNCERTAIN.value, needs_review=True),),
                "fields": result.fields,
                "identifier_zone_id": "roll_number",
            }
        )
        truth = truth_with({1: "B"}, ambiguous=(1,))
        assert compare_result(result, truth) == []

    def test_confidently_reading_a_different_option_still_counts_against_it(self):
        result = result_with({1: "C"})
        truth = truth_with({1: "B"}, ambiguous=(1,))
        errors = compare_result(result, truth)
        assert [record.category for record in errors] == [ErrorCategory.WRONG_OPTION]

    def test_borderline_questions_are_counted_separately_from_accuracy(self):
        results = [result_with({1: "B", 2: ""})]
        truths = {"scan": truth_with({1: "B", 2: "C"}, ambiguous=(2,))}
        summary = evaluate(results, truths).summary
        # One question checked, not two: the borderline one has its own column.
        assert summary.questions_checked == 1
        assert summary.question_accuracy == 1.0
        assert summary.ambiguous_expected == 1
        assert summary.ambiguous_handled == 1


class TestSummaryMetrics:
    def test_it_counts_what_it_says_it_counts(self):
        results = [result_with({1: "B", 2: "", 3: "A-C", 4: "D"})]
        truths = {"scan": truth_with({1: "B", 2: "", 3: "A-C", 4: "A"})}
        summary = evaluate(results, truths, dataset="unit").summary

        assert summary.dataset == "unit"
        assert summary.scans == 1
        assert summary.processed == 1
        assert summary.questions_checked == 4
        assert summary.questions_correct == 3
        assert summary.question_accuracy == 0.75
        assert summary.blanks_expected == 1
        assert summary.blank_accuracy == 1.0
        assert summary.multiples_expected == 1
        assert summary.multiple_accuracy == 1.0
        assert summary.roll_accuracy == 1.0
        assert summary.error_counts == {ErrorCategory.WRONG_OPTION.value: 1}

    def test_a_rate_with_no_denominator_is_zero_not_a_crash(self):
        summary = evaluate([], {}).summary
        assert summary.question_accuracy == 0.0
        assert summary.roll_accuracy == 0.0
        assert summary.mean_seconds_per_scan == 0.0

    def test_a_result_without_ground_truth_is_skipped_not_scored(self, caplog):
        # Silently scoring an unlabelled sheet as a pass is how a benchmark
        # starts lying.
        summary = evaluate([result_with({1: "B"})], {}).summary
        assert summary.scans == 0
        assert "No ground truth" in caplog.text

    def test_expected_failures_are_counted_apart_from_real_ones(self):
        result = result_with({}, registration=RegistrationStatus.FAILED)
        summary = evaluate([result], {"scan": truth_with({}, expect_failure=True)}).summary
        assert summary.expected_failures == 1
        assert summary.failed == 0

    def test_results_are_summarised_in_a_stable_order(self):
        # Two runs over the same data must produce the same report, or a diff
        # between them is meaningless.
        results = [result_with({1: "B"}, source_path=Path(f"{n}.png")) for n in ("b", "a")]
        truths = {"a": truth_with({1: "B"}), "b": truth_with({1: "C"})}
        first = evaluate(results, truths)
        second = evaluate(list(reversed(results)), truths)
        assert first.summary.to_dict() == second.summary.to_dict()
        assert [record.scan for record in first.errors] == [
            record.scan for record in second.errors
        ]

    def test_accuracy_is_reported_per_confidence_band(self):
        results = [result_with({1: "B"})]
        summary = evaluate(results, {"scan": truth_with({1: "B"})}).summary
        assert summary.accuracy_by_confidence
        band = next(iter(summary.accuracy_by_confidence.values()))
        assert band["questions"] == 1.0
        assert band["accuracy"] == 1.0


class TestReportFiles:
    def test_it_writes_a_summary_and_an_error_list(self, tmp_path: Path):
        report = evaluate([result_with({1: "C"})], {"scan": truth_with({1: "B"})})
        summary_path, errors_path = write_report(report, tmp_path / "out")

        stored = json.loads(summary_path.read_text(encoding="utf-8"))
        assert stored["questions_checked"] == 1
        assert stored["error_counts"]["WRONG_OPTION"] == 1

        rows = list(csv.DictReader(errors_path.open(encoding="utf-8")))
        assert rows[0]["category"] == "WRONG_OPTION"
        assert rows[0]["question"] == "1"
        assert rows[0]["expected"] == "B"
        assert rows[0]["actual"] == "C"

    def test_an_error_free_run_still_writes_a_csv_with_its_header(self, tmp_path: Path):
        report = evaluate([result_with({1: "B"})], {"scan": truth_with({1: "B"})})
        _summary, errors_path = write_report(report, tmp_path / "out")
        text = errors_path.read_text(encoding="utf-8")
        assert text.startswith("scan,category,question")

    def test_a_written_summary_can_be_read_back_as_a_baseline(self, tmp_path: Path):
        report = evaluate([result_with({1: "B"})], {"scan": truth_with({1: "B"})})
        summary_path, _errors = write_report(report, tmp_path / "out")
        assert load_summary(summary_path).question_accuracy == 1.0


class TestBaselineComparison:
    def test_a_better_run_is_reported_as_improved(self):
        before = BenchmarkSummary(questions_checked=100, questions_correct=90)
        after = BenchmarkSummary(questions_checked=100, questions_correct=95)
        verdicts = {item.metric: item.verdict for item in compare_baseline(before, after)}
        assert verdicts["question_accuracy"] == "improved"

    def test_a_worse_run_is_reported_as_regressed(self):
        before = BenchmarkSummary(questions_checked=100, questions_correct=95)
        after = BenchmarkSummary(questions_checked=100, questions_correct=80)
        verdicts = {item.metric: item.verdict for item in compare_baseline(before, after)}
        assert verdicts["question_accuracy"] == "regressed"

    def test_a_change_inside_the_tolerance_is_not_a_change(self):
        # Small datasets move by more than this when one sheet differs; a
        # comparison that shouts every run gets ignored within a week.
        before = BenchmarkSummary(questions_checked=1000, questions_correct=900)
        after = BenchmarkSummary(questions_checked=1000, questions_correct=902)
        verdicts = {item.metric: item.verdict for item in compare_baseline(before, after)}
        assert verdicts["question_accuracy"] == "unchanged"

    def test_the_tolerance_is_reported_with_the_verdict(self):
        comparisons = compare_baseline(BenchmarkSummary(), BenchmarkSummary())
        assert all(item.tolerance == DEFAULT_TOLERANCE for item in comparisons)
        assert all(item.delta == 0.0 for item in comparisons)

    def test_every_headline_metric_is_compared(self):
        metrics = {item.metric for item in compare_baseline(BenchmarkSummary(), BenchmarkSummary())}
        assert metrics == {
            "question_accuracy",
            "roll_accuracy",
            "set_accuracy",
            "blank_accuracy",
            "multiple_accuracy",
        }
