"""Tests for reading a batch of sheets on several CPU cores at once.

These start real worker processes. They are deliberately small - six to eight
synthetic sheets - because what is under test is the *coordination*, not the
throughput: that parallel execution changes nothing about what is recognised,
that the batch order survives out-of-order completion, that two sheets claiming
one roll number both keep their file, that one bad image does not take the batch
with it, and that no worker process outlives the run.

Throughput is measured separately by ``scripts/benchmark_batch.py``, which is a
benchmark rather than an assertion: a test that fails because a CI runner was
busy would tell nobody anything.
"""

from __future__ import annotations

import multiprocessing
from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.services.batch_processor import (
    BatchOptions,
    BatchStage,
    process_batch,
)
from omr_scanner.services.filename_manager import FilenameAllocator
from omr_scanner.services.parallel_batch import (
    WorkerOutcome,
    _outcome_of,
    recognise_in_parallel,
)
from omr_scanner.services.recognition_service import RecognitionOutcome
from omr_scanner.services.scan_export import render_scan_results

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

WORKER_COUNTS = (1, 2, 4)
"""The worker counts every consistency test is run at."""


def sheet_marks(roll: str, *, set_code: str = "A", answer: str = "B") -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: set_code},
        "questions_0": dict.fromkeys(range(10), answer),
        "questions_1": dict.fromkeys(range(10), answer),
    }


@pytest.fixture(scope="module")
def template():
    # Seven identifier digits, so the tests can use the roll number from the
    # Phase 3 brief (2103123) and check the exact documented name sequence.
    return build_answer_sheet_template(roll_digits=7)


@pytest.fixture
def make_scan(tmp_path: Path, template):
    """Render a sheet with a chosen roll number into the scans folder."""
    import cv2

    scans = tmp_path / "scans"
    scans.mkdir(parents=True, exist_ok=True)

    def make(name: str, roll: str = "1203170", **kwargs: str) -> Path:
        image = render_marked_sheet(template, sheet_marks(roll, **kwargs))
        path = scans / name
        cv2.imwrite(str(path), image)
        return path

    return make


@pytest.fixture
def batch_of_six(make_scan):
    """Six distinct sheets, in a deliberate non-alphabetical roll order."""
    rolls = ["1203111", "1203222", "1203333", "1203444", "1203555", "1203666"]
    return [make_scan(f"scan{index:03d}.png", roll=roll) for index, roll in enumerate(rolls)]


def summarise(report) -> list[tuple]:
    """Reduce a report to the values a user would actually compare."""
    return [
        (
            item.result.source_path.name,
            item.result.identifier_value,
            item.result.set_code_value,
            item.result.outcome.value,
            item.result.registration.value,
            tuple(answer.display_value for answer in item.result.answers),
            item.output_name,
        )
        for item in report.processed
    ]


class TestResultConsistency:
    def test_one_two_and_four_workers_read_exactly_the_same_thing(
        self, template, batch_of_six
    ):
        summaries = {
            workers: summarise(process_batch(batch_of_six, template, workers=workers))
            for workers in WORKER_COUNTS
        }
        assert summaries[2] == summaries[1]
        assert summaries[4] == summaries[1]

    def test_the_csv_is_byte_identical_whatever_the_worker_count(
        self, template, batch_of_six
    ):
        exports = {
            workers: render_scan_results(
                process_batch(batch_of_six, template, workers=workers).processed,
                template,
            )
            for workers in WORKER_COUNTS
        }
        assert exports[2] == exports[1]
        assert exports[4] == exports[1]

    def test_every_sheet_is_read_exactly_once(self, template, batch_of_six):
        report = process_batch(batch_of_six, template, workers=4)
        assert report.total == len(batch_of_six)
        assert sorted(item.source_path for item in report.processed) == sorted(
            batch_of_six
        )

    def test_confidence_values_are_unchanged_by_parallel_execution(
        self, template, batch_of_six
    ):
        single = process_batch(batch_of_six[:3], template, workers=1)
        parallel = process_batch(batch_of_six[:3], template, workers=3)
        for one, many in zip(single.processed, parallel.processed, strict=True):
            assert [a.confidence for a in one.result.answers] == [
                a.confidence for a in many.result.answers
            ]


class TestDeterministicOrder:
    def test_results_come_back_in_batch_order_not_completion_order(
        self, template, batch_of_six
    ):
        report = process_batch(batch_of_six, template, workers=4)
        assert [item.source_path for item in report.processed] == batch_of_six

    def test_results_are_delivered_progressively_in_batch_order(
        self, template, batch_of_six
    ):
        seen = []
        process_batch(batch_of_six, template, workers=4, on_result=seen.append)
        assert [item.source_path for item in seen] == batch_of_six

    def test_a_reversed_scan_list_exports_in_that_reversed_order(
        self, template, batch_of_six
    ):
        reversed_paths = list(reversed(batch_of_six))
        report = process_batch(reversed_paths, template, workers=4)
        assert [item.source_path for item in report.processed] == reversed_paths


class TestDuplicateRollConcurrency:
    def test_eight_sheets_with_one_roll_number_all_survive_four_workers(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        paths = [make_scan(f"scan{index}.png", roll="2103123") for index in range(8)]

        report = process_batch(
            paths,
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
            workers=4,
        )

        expected = [
            "2103123.png",
            "2103123_a.png",
            "2103123_b.png",
            "2103123_c.png",
            "2103123_d.png",
            "2103123_e.png",
            "2103123_f.png",
            "2103123_g.png",
        ]
        assert [item.output_name for item in report.processed] == expected
        assert sorted(entry.name for entry in output.iterdir()) == sorted(expected)
        assert report.written_count == 8

    def test_each_written_file_is_the_scan_it_came_from(
        self, tmp_path, template, make_scan
    ):
        # The race this rules out: two workers both deciding on "2103123.png"
        # and one silently overwriting the other's script.
        output = tmp_path / "output"
        paths = [
            make_scan(f"scan{index}.png", roll="2103123", answer=letter)
            for index, letter in enumerate("ABCD")
        ]
        report = process_batch(
            paths,
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
            workers=4,
        )
        for item, source in zip(report.processed, paths, strict=True):
            assert item.output_path is not None
            assert item.output_path.read_bytes() == source.read_bytes()

    def test_the_names_are_the_same_ones_a_single_core_run_would_choose(
        self, tmp_path, template, make_scan
    ):
        paths = [make_scan(f"scan{index}.png", roll="2103123") for index in range(6)]
        single = process_batch(
            paths,
            template,
            options=BatchOptions(
                output_dir=tmp_path / "one", rename_with_identifier=True
            ),
            workers=1,
        )
        parallel = process_batch(
            paths,
            template,
            options=BatchOptions(
                output_dir=tmp_path / "many", rename_with_identifier=True
            ),
            workers=4,
        )
        assert [item.output_name for item in parallel.processed] == [
            item.output_name for item in single.processed
        ]

    def test_every_csv_row_names_the_file_that_was_actually_written(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        paths = [make_scan(f"scan{index}.png", roll="2103123") for index in range(5)]
        report = process_batch(
            paths,
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
            workers=4,
        )
        rows = render_scan_results(report.processed, template).splitlines()[1:]
        for row, item in zip(rows, report.processed, strict=True):
            original, written, *_ = row.split(",")
            assert original == item.source_path.name
            assert written == item.output_name
            assert (output / written).exists()

    def test_files_already_in_the_output_folder_are_never_overwritten(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        output.mkdir()
        (output / "2103123.png").write_bytes(b"a file a human put here")

        paths = [make_scan(f"scan{index}.png", roll="2103123") for index in range(4)]
        report = process_batch(
            paths,
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
            workers=4,
        )
        assert [item.output_name for item in report.processed] == [
            "2103123_a.png",
            "2103123_b.png",
            "2103123_c.png",
            "2103123_d.png",
        ]
        assert (output / "2103123.png").read_bytes() == b"a file a human put here"

    def test_unresolved_sheets_are_numbered_in_batch_order(
        self, tmp_path, template, write_marked_sheet
    ):
        marks = sheet_marks("1203170")
        del marks["roll_number"][0]  # first digit column blank: not trustworthy
        paths = [
            write_marked_sheet(template, marks, name=f"unreadable{index}.png")
            for index in range(4)
        ]
        report = process_batch(
            paths,
            template,
            options=BatchOptions(
                output_dir=tmp_path / "out", rename_with_identifier=True
            ),
            workers=4,
        )
        assert [item.output_name for item in report.processed] == [
            "UNRESOLVED_001.png",
            "UNRESOLVED_002.png",
            "UNRESOLVED_003.png",
            "UNRESOLVED_004.png",
        ]


class TestFailureIsolation:
    def test_a_corrupted_scan_between_two_valid_ones_fails_alone(
        self, tmp_path, template, make_scan
    ):
        good_before = make_scan("a.png", roll="1000010")
        corrupt = tmp_path / "scans" / "broken.png"
        corrupt.write_bytes(b"this is not a PNG at all")
        good_after = make_scan("c.png", roll="1000030")

        report = process_batch([good_before, corrupt, good_after], template, workers=3)

        assert report.total == 3
        assert report.complete_count == 2
        assert report.failed_count == 1
        assert report.processed[1].outcome is RecognitionOutcome.ERROR
        assert report.processed[1].result.registration_message
        assert report.processed[0].result.identifier_value == "1000010"
        assert report.processed[2].result.identifier_value == "1000030"

    def test_a_missing_file_is_reported_not_raised(self, tmp_path, template, make_scan):
        report = process_batch(
            [tmp_path / "scans" / "gone.png", make_scan("a.png")], template, workers=2
        )
        assert report.total == 2
        assert report.processed[0].outcome is RecognitionOutcome.ERROR
        assert report.processed[1].outcome is RecognitionOutcome.COMPLETE

    def test_several_bad_files_still_leave_the_good_ones_intact(
        self, tmp_path, template, make_scan
    ):
        scans = tmp_path / "scans"
        scans.mkdir(exist_ok=True)
        paths = []
        for index in range(6):
            if index % 2:
                bad = scans / f"bad{index}.png"
                bad.write_bytes(b"not an image")
                paths.append(bad)
            else:
                paths.append(make_scan(f"good{index}.png", roll=f"100000{index}"))

        report = process_batch(paths, template, workers=4)
        assert report.failed_count == 3
        assert report.complete_count == 3
        assert [item.outcome is RecognitionOutcome.ERROR for item in report.processed] == [
            False,
            True,
            False,
            True,
            False,
            True,
        ]

    def test_a_worker_process_that_dies_becomes_one_failed_scan(self, tmp_path):
        # A future that failed outright is what a killed worker looks like from
        # the parent: it must become one failed *scan*, never a failed batch.
        from concurrent.futures import Future

        future: Future = Future()
        future.set_exception(RuntimeError("the worker process died"))
        outcome = _outcome_of(future, 3, tmp_path / "scan003.png")

        assert isinstance(outcome, WorkerOutcome)
        assert outcome.index == 3
        assert outcome.result.outcome is RecognitionOutcome.ERROR
        assert "worker process failed" in outcome.result.registration_message


class TestWorkerCounts:
    def test_more_workers_than_scans_starts_one_worker_per_scan(
        self, template, make_scan
    ):
        paths = [make_scan(f"s{index}.png", roll=f"100000{index}") for index in range(3)]
        report = process_batch(paths, template, workers=32)
        assert report.worker_count == 3
        assert report.total == 3

    def test_a_single_worker_uses_no_pool_at_all(self, template, make_scan):
        report = process_batch([make_scan("a.png")], template, workers=1)
        assert report.worker_count == 1
        assert not multiprocessing.active_children()

    def test_an_empty_batch_with_many_workers_is_not_an_error(self, template):
        report = process_batch([], template, workers=8)
        assert report.total == 0
        assert report.worker_count == 1

    def test_the_report_records_what_the_run_cost(self, template, batch_of_six):
        report = process_batch(batch_of_six, template, workers=2)
        assert report.worker_count == 2
        assert report.elapsed_seconds > 0.0

    def test_a_pool_that_cannot_start_falls_back_to_one_core(
        self, monkeypatch, template, batch_of_six
    ):
        # A locked-down machine, a sandbox that forbids new processes, or simply
        # no memory for another interpreter. The user asked for four workers and
        # should get a slower run and a line in the log - not a failed batch.
        from omr_scanner.services import batch_processor

        def refuse(*_args: object, **_kwargs: object) -> Iterator[object]:
            raise OSError("this machine will not start worker processes")
            yield  # pragma: no cover - makes `refuse` a generator, as the real one is

        monkeypatch.setattr(batch_processor, "recognise_in_parallel", refuse)
        report = process_batch(batch_of_six, template, workers=4)

        assert report.total == len(batch_of_six)
        assert report.complete_count == len(batch_of_six)
        assert report.worker_count == 1  # reports what it did, not what was asked
        assert [item.source_path for item in report.processed] == batch_of_six


class TestProgressReporting:
    def test_progress_counts_completions_and_never_goes_backwards(
        self, template, batch_of_six
    ):
        seen = []
        process_batch(batch_of_six, template, workers=4, on_progress=seen.append)

        completed = [update.completed for update in seen]
        assert completed == sorted(completed)
        assert completed[-1] == len(batch_of_six)
        assert all(update.total == len(batch_of_six) for update in seen)

    def test_every_scan_reports_exactly_one_completion(self, template, batch_of_six):
        seen = []
        process_batch(batch_of_six, template, workers=4, on_progress=seen.append)
        assert len(seen) == len(batch_of_six)
        assert {update.path for update in seen} == set(batch_of_six)

    def test_a_failed_scan_reports_the_failed_stage(self, tmp_path, template, make_scan):
        corrupt = tmp_path / "scans" / "broken.png"
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_bytes(b"not an image")

        seen = []
        process_batch(
            [make_scan("a.png"), corrupt], template, workers=2, on_progress=seen.append
        )
        stages = {update.path: update.stage for update in seen}
        assert stages[corrupt] is BatchStage.FAILED


class TestCancellation:
    def test_cancelling_stops_the_run_and_keeps_what_was_read(
        self, template, batch_of_six
    ):
        done = {"n": 0}

        def count(_item) -> None:
            done["n"] += 1

        def should_cancel() -> bool:
            return done["n"] >= 2

        report = process_batch(
            batch_of_six,
            template,
            workers=2,
            on_result=count,
            should_cancel=should_cancel,
        )
        assert report.cancelled is True
        assert report.total < len(batch_of_six)
        assert all(
            item.outcome is not RecognitionOutcome.PENDING for item in report.processed
        )

    def test_cancelling_before_anything_starts_processes_nothing_further(
        self, template, batch_of_six
    ):
        report = process_batch(
            batch_of_six, template, workers=4, should_cancel=lambda: True
        )
        assert report.cancelled is True
        assert report.total <= len(batch_of_six)

    def test_no_worker_process_outlives_a_cancelled_run(self, template, batch_of_six):
        process_batch(batch_of_six, template, workers=4, should_cancel=lambda: True)
        assert not multiprocessing.active_children()


class TestProgressForADisplay:
    """What a progress display can rely on while several workers run."""

    def test_every_completion_carries_its_outcome(self, template, batch_of_six):
        seen = []
        process_batch(batch_of_six, template, workers=4, on_progress=seen.append)

        # The outcome travels on the *progress* event, not only on the result,
        # because on a multicore run the results are released in batch order
        # and a counter that waited for them would lag behind the bar.
        assert all(update.outcome for update in seen)
        assert {update.outcome for update in seen} == {"complete"}

    def test_the_counts_a_tracker_derives_match_the_final_report(
        self, tmp_path, template, make_scan
    ):
        from omr_scanner.services.batch_progress import BatchProgressTracker, JobStatus

        scans = tmp_path / "scans"
        scans.mkdir(exist_ok=True)
        paths = []
        for index in range(8):
            if index % 4 == 3:
                bad = scans / f"bad{index}.png"
                bad.write_bytes(b"not an image")
                paths.append(bad)
            else:
                paths.append(make_scan(f"good{index}.png", roll=f"100000{index}"))

        tracker = BatchProgressTracker()
        tracker.start(len(paths), workers=4)
        statuses = {
            "complete": JobStatus.SUCCESS,
            "review": JobStatus.WARNING,
            "registration_failed": JobStatus.FAILED,
            "error": JobStatus.FAILED,
        }

        def count(update) -> None:
            status = statuses.get(update.outcome)
            if status is not None:
                tracker.record(status)

        report = process_batch(paths, template, workers=4, on_progress=count)
        tracker.finish()
        snapshot = tracker.snapshot()

        # Eight sheets finishing on four workers, counted once each - and the
        # tally agrees with the report the batch itself produced.
        assert snapshot.completed == report.total == 8
        assert snapshot.successful == report.complete_count
        assert snapshot.warnings == report.review_count
        assert snapshot.failed == report.failed_count == 2
        assert snapshot.fraction == 1.0

    def test_a_cancelled_run_reports_fewer_completions_than_the_total(
        self, template, batch_of_six
    ):
        from omr_scanner.services.batch_progress import BatchProgressTracker, JobStatus

        tracker = BatchProgressTracker()
        tracker.start(len(batch_of_six), workers=2)
        done = {"n": 0}

        def count(update) -> None:
            if update.outcome:
                tracker.record(JobStatus.SUCCESS)
                done["n"] += 1

        report = process_batch(
            batch_of_six,
            template,
            workers=2,
            on_progress=count,
            should_cancel=lambda: done["n"] >= 2,
        )
        tracker.finish(cancelled=True)
        snapshot = tracker.snapshot()

        assert report.cancelled is True
        assert snapshot.completed == done["n"]
        assert snapshot.remaining == len(batch_of_six) - snapshot.completed
        assert snapshot.eta_seconds is None


class TestProcessHygiene:
    def test_no_worker_process_is_left_behind_after_a_normal_run(
        self, template, batch_of_six
    ):
        process_batch(batch_of_six, template, workers=4)
        assert not multiprocessing.active_children()

    def test_no_worker_process_is_left_behind_after_a_batch_full_of_failures(
        self, tmp_path, template
    ):
        scans = tmp_path / "scans"
        scans.mkdir()
        paths = []
        for index in range(4):
            bad = scans / f"bad{index}.png"
            bad.write_bytes(b"not an image")
            paths.append(bad)

        process_batch(paths, template, workers=4)
        assert not multiprocessing.active_children()

    def test_the_generator_shuts_its_pool_down_even_if_abandoned(
        self, template, batch_of_six
    ):
        # A caller that stops iterating (an exception in the loop body, say)
        # must not leave a pool running; closing the generator has to tear it
        # down through the `finally`.
        stream = recognise_in_parallel(batch_of_six, template, workers=2)
        next(stream)
        stream.close()
        assert not multiprocessing.active_children()


class TestSharedAllocator:
    def test_a_second_parallel_run_continues_the_first_run_s_numbering(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        allocator = FilenameAllocator(output)
        options = BatchOptions(output_dir=output, rename_with_identifier=True)

        first = process_batch(
            [make_scan(f"a{index}.png", roll="2103123") for index in range(2)],
            template,
            options=options,
            allocator=allocator,
            workers=2,
        )
        second = process_batch(
            [make_scan(f"b{index}.png", roll="2103123") for index in range(2)],
            template,
            options=options,
            allocator=allocator,
            workers=2,
        )
        assert [item.output_name for item in first.processed] == [
            "2103123.png",
            "2103123_a.png",
        ]
        assert [item.output_name for item in second.processed] == [
            "2103123_b.png",
            "2103123_c.png",
        ]
        assert len(list(output.iterdir())) == 4


def _by_index(results: list[tuple[int, object]]) -> list[tuple[int, str]]:
    return sorted(
        ((index, result.identifier_value) for index, result in results),
        key=lambda pair: pair[0],
    )


class TestWorkerRecyclingAndOpenCvThreads:
    """Phase 10, §16/§18: neither knob may change what is recognised or lose a job."""

    def test_recycling_every_two_sheets_produces_the_same_results_as_no_recycling(
        self, template, batch_of_six
    ):
        without_recycling = list(
            recognise_in_parallel(batch_of_six, template, workers=2, max_tasks_per_child=None)
        )
        with_recycling = list(
            recognise_in_parallel(
                batch_of_six, template, workers=2, max_tasks_per_child=2
            )
        )

        assert _by_index(without_recycling) == _by_index(with_recycling)
        # Every sheet arrived exactly once - recycling a worker between tasks
        # never drops or duplicates a job.
        assert len(with_recycling) == len(batch_of_six)
        assert not multiprocessing.active_children()

    def test_a_small_opencv_thread_count_does_not_change_recognition(
        self, template, batch_of_six
    ):
        default_threads = list(
            recognise_in_parallel(batch_of_six, template, workers=2, opencv_threads=1)
        )
        two_threads = list(
            recognise_in_parallel(batch_of_six, template, workers=2, opencv_threads=2)
        )

        assert _by_index(default_threads) == _by_index(two_threads)
