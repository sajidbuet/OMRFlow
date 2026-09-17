"""Tests for running a batch of real scans through recognition and renaming.

These process genuinely rendered sheets rather than stub results, so the naming
guarantees are exercised against what recognition actually produces - including
the case where it produces an identifier nobody should trust.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.services.batch_processor import (
    BatchOptions,
    BatchStage,
    plan_output_name,
    process_batch,
    process_scan,
)
from omr_scanner.services.filename_manager import FilenameAllocator
from omr_scanner.services.recognition_service import (
    RecognitionOutcome,
    RegistrationStatus,
)

if TYPE_CHECKING:
    from pathlib import Path


def roll_marks(digits: str) -> dict[int, str]:
    return dict(enumerate(digits))


def sheet_marks(roll: str, *, set_code: str = "A", answer: str = "B") -> dict:
    return {
        "roll_number": roll_marks(roll),
        "set_code": {0: set_code},
        "questions_0": dict.fromkeys(range(10), answer),
        "questions_1": dict.fromkeys(range(10), answer),
    }


@pytest.fixture
def template():
    return build_answer_sheet_template()


@pytest.fixture
def make_scan(tmp_path: Path, template):
    """Render a sheet with the given roll number into the scans folder."""
    import cv2

    scans = tmp_path / "scans"
    scans.mkdir(parents=True, exist_ok=True)

    def make(name: str, roll: str = "120317", **kwargs: str) -> Path:
        image = render_marked_sheet(template, sheet_marks(roll, **kwargs))
        path = scans / name
        cv2.imwrite(str(path), image)
        return path

    return make


class TestRecogniseOnly:
    def test_a_batch_reads_every_sheet(self, template, make_scan):
        paths = [make_scan(f"scan{i}.png", roll=f"10000{i}") for i in range(3)]
        report = process_batch(paths, template)

        assert report.total == 3
        assert report.complete_count == 3
        assert report.written_count == 0
        assert [item.result.identifier_value for item in report.processed] == [
            "100000",
            "100001",
            "100002",
        ]

    def test_nothing_is_written_when_renaming_is_off(self, tmp_path, template, make_scan):
        output = tmp_path / "output"
        report = process_batch(
            [make_scan("a.png")],
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=False),
        )
        assert report.written_count == 0
        assert not output.exists()

    def test_the_source_images_are_never_modified(self, template, make_scan, tmp_path):
        path = make_scan("a.png")
        before = path.read_bytes()
        process_batch(
            [path],
            template,
            options=BatchOptions(
                output_dir=tmp_path / "out", rename_with_identifier=True
            ),
        )
        assert path.read_bytes() == before
        assert path.exists()


class TestRenamingByRollNumber:
    def test_a_recognised_sheet_is_copied_under_its_roll_number(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        report = process_batch(
            [make_scan("messy_scanner_name_0001.png", roll="120317")],
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )
        item = report.processed[0]
        assert item.output_name == "120317.png"
        assert item.copied is True
        assert (output / "120317.png").exists()
        assert report.written_count == 1

    def test_the_source_extension_is_preserved(self, tmp_path, template, make_scan):
        output = tmp_path / "output"
        report = process_batch(
            [make_scan("a.bmp", roll="120317")],
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )
        assert report.processed[0].output_name == "120317.bmp"

    def test_the_copy_is_byte_identical_to_the_original(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        source = make_scan("a.png", roll="120317")
        process_batch(
            [source],
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )
        assert (output / "120317.png").read_bytes() == source.read_bytes()

    def test_a_name_is_planned_even_when_no_output_folder_is_chosen(
        self, template, make_scan
    ):
        # The scan list shows a rename *preview* before the user commits.
        report = process_batch(
            [make_scan("a.png", roll="120317")],
            template,
            options=BatchOptions(output_dir=None, rename_with_identifier=True),
        )
        item = report.processed[0]
        assert item.output_name == "120317.png"
        assert item.copied is False
        assert item.output_path is None


class TestDuplicateRollNumbers:
    def test_three_sheets_claiming_one_roll_all_survive(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        paths = [make_scan(f"scan{i}.png", roll="120317") for i in range(3)]
        report = process_batch(
            paths,
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )

        assert [item.output_name for item in report.processed] == [
            "120317.png",
            "120317_a.png",
            "120317_b.png",
        ]
        assert report.written_count == 3
        written = sorted(p.name for p in output.iterdir())
        assert written == ["120317.png", "120317_a.png", "120317_b.png"]
        # Three distinct files, each still the sheet it came from.
        for item, source in zip(report.processed, paths, strict=True):
            assert item.output_path.read_bytes() == source.read_bytes()

    def test_a_second_run_into_the_same_folder_does_not_replace_the_first(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        options = BatchOptions(output_dir=output, rename_with_identifier=True)

        first = make_scan("morning.png", roll="120317")
        process_batch([first], template, options=options)
        original_bytes = (output / "120317.png").read_bytes()

        second = make_scan("afternoon.png", roll="120317", answer="C")
        report = process_batch([second], template, options=options)

        assert report.processed[0].output_name == "120317_a.png"
        assert (output / "120317.png").read_bytes() == original_bytes
        assert (output / "120317_a.png").read_bytes() == second.read_bytes()

    def test_an_unrelated_existing_file_of_the_same_name_is_not_replaced(
        self, tmp_path, template, make_scan
    ):
        output = tmp_path / "output"
        output.mkdir()
        (output / "120317.png").write_bytes(b"something a human put here")

        report = process_batch(
            [make_scan("a.png", roll="120317")],
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )
        assert report.processed[0].output_name == "120317_a.png"
        assert (output / "120317.png").read_bytes() == b"something a human put here"

    def test_no_output_file_is_ever_written_twice(self, tmp_path, template, make_scan):
        output = tmp_path / "output"
        paths = [make_scan(f"scan{i}.png", roll="120317") for i in range(8)]
        report = process_batch(
            paths,
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )
        names = [item.output_name for item in report.processed]
        assert len(set(names)) == len(names)
        assert len(list(output.iterdir())) == len(paths)


class TestUnreliableIdentifiers:
    def test_a_sheet_with_a_blank_roll_column_gets_a_review_name(self, tmp_path, template):
        output = tmp_path / "output"
        marks = sheet_marks("120317")
        del marks["roll_number"][2]  # third digit column left empty
        import cv2

        path = tmp_path / "scans" / "incomplete.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), render_marked_sheet(template, marks))

        report = process_batch(
            [path],
            template,
            options=BatchOptions(output_dir=output, rename_with_identifier=True),
        )
        item = report.processed[0]
        assert item.output_name == "UNRESOLVED_001.png"
        assert "could not be determined reliably" in item.message
        assert (output / "UNRESOLVED_001.png").exists()

    def test_a_double_marked_roll_digit_gets_a_review_name(self, tmp_path, template):
        import cv2

        marks = sheet_marks("120317")
        marks["roll_number"][1] = ["2", "7"]
        path = tmp_path / "scans" / "double.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), render_marked_sheet(template, marks))

        report = process_batch(
            [path],
            template,
            options=BatchOptions(
                output_dir=tmp_path / "out", rename_with_identifier=True
            ),
        )
        assert report.processed[0].output_name.startswith("UNRESOLVED_")
        assert report.processed[0].result.identifier_value == "1?0317"

    def test_review_names_are_numbered_sequentially(self, tmp_path, template):
        import cv2

        marks = sheet_marks("120317")
        del marks["roll_number"][0]
        image = render_marked_sheet(template, marks)
        scans = tmp_path / "scans"
        scans.mkdir()
        paths = []
        for index in range(3):
            path = scans / f"s{index}.png"
            cv2.imwrite(str(path), image)
            paths.append(path)

        report = process_batch(
            paths,
            template,
            options=BatchOptions(
                output_dir=tmp_path / "out", rename_with_identifier=True
            ),
        )
        assert [item.output_name for item in report.processed] == [
            "UNRESOLVED_001.png",
            "UNRESOLVED_002.png",
            "UNRESOLVED_003.png",
        ]


class TestErrorIsolation:
    def test_one_corrupted_file_does_not_stop_the_batch(
        self, tmp_path, template, make_scan
    ):
        good_before = make_scan("a.png", roll="100001")
        corrupt = tmp_path / "scans" / "broken.png"
        corrupt.write_bytes(b"this is not a PNG at all")
        good_after = make_scan("c.png", roll="100003")

        report = process_batch([good_before, corrupt, good_after], template)

        assert report.total == 3
        assert report.complete_count == 2
        assert report.failed_count == 1
        assert report.processed[1].outcome is RecognitionOutcome.ERROR
        assert report.processed[1].result.registration_message
        assert report.processed[2].result.identifier_value == "100003"

    def test_a_missing_file_is_reported_not_raised(self, tmp_path, template, make_scan):
        report = process_batch(
            [tmp_path / "scans" / "gone.png", make_scan("a.png")], template
        )
        assert report.total == 2
        assert report.processed[0].outcome is RecognitionOutcome.ERROR
        assert report.processed[1].outcome is RecognitionOutcome.COMPLETE

    def test_an_unregistrable_sheet_is_reported_and_not_renamed(
        self, tmp_path, template
    ):
        import cv2
        import numpy as np

        # A blank page: no registration markers to find anywhere on it.
        path = tmp_path / "blank.png"
        cv2.imwrite(str(path), np.full((1754, 1240), 250, dtype=np.uint8))

        report = process_batch(
            [path],
            template,
            options=BatchOptions(
                output_dir=tmp_path / "out", rename_with_identifier=True
            ),
        )
        item = report.processed[0]
        assert item.result.registration is RegistrationStatus.FAILED
        assert item.result.answers == ()  # never invents answers from an unaligned page
        assert item.output_name.startswith("UNRESOLVED_")
        assert item.result.registration_message

    def test_a_failure_inside_process_scan_is_caught_per_file(
        self, monkeypatch, template, make_scan
    ):
        from omr_scanner.services import batch_processor

        paths = [make_scan("a.png"), make_scan("b.png"), make_scan("c.png")]
        calls = {"n": 0}
        original = batch_processor.process_scan

        def explode(path: object, *args: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("a bug nobody predicted")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(batch_processor, "process_scan", explode)
        report = process_batch(paths, template)

        assert report.total == 3
        assert report.processed[1].outcome is RecognitionOutcome.ERROR
        assert "a bug nobody predicted" in report.processed[1].message
        assert report.processed[2].outcome is RecognitionOutcome.COMPLETE


class TestProgressAndCancellation:
    def test_progress_is_reported_for_the_start_and_end_of_every_file(
        self, template, make_scan
    ):
        paths = [make_scan(f"s{i}.png") for i in range(3)]
        seen = []
        process_batch(paths, template, on_progress=seen.append)

        assert [p.stage for p in seen] == [
            BatchStage.STARTED,
            BatchStage.RECOGNISED,
        ] * 3
        assert [p.index for p in seen] == [0, 0, 1, 1, 2, 2]
        assert all(p.total == 3 for p in seen)
        assert [p.completed for p in seen] == [0, 1, 1, 2, 2, 3]

    def test_each_result_is_delivered_as_soon_as_it_is_known(self, template, make_scan):
        paths = [make_scan(f"s{i}.png", roll=f"10000{i}") for i in range(3)]
        seen = []
        report = process_batch(paths, template, on_result=seen.append)
        assert [item.source_path for item in seen] == paths
        assert tuple(seen) == report.processed

    def test_a_copied_file_reports_the_copied_stage(self, tmp_path, template, make_scan):
        seen = []
        process_batch(
            [make_scan("a.png")],
            template,
            options=BatchOptions(
                output_dir=tmp_path / "out", rename_with_identifier=True
            ),
            on_progress=seen.append,
        )
        assert seen[-1].stage is BatchStage.COPIED

    def test_cancelling_stops_the_run_and_keeps_what_was_done(self, template, make_scan):
        paths = [make_scan(f"s{i}.png", roll=f"10000{i}") for i in range(4)]
        done = {"n": 0}

        def should_cancel() -> bool:
            return done["n"] >= 2

        def count(_item) -> None:
            done["n"] += 1

        report = process_batch(
            paths, template, on_result=count, should_cancel=should_cancel
        )
        assert report.cancelled is True
        assert report.total == 2
        assert [item.result.identifier_value for item in report.processed] == [
            "100000",
            "100001",
        ]

    def test_cancelling_before_the_first_file_processes_nothing(
        self, template, make_scan
    ):
        seen = []
        report = process_batch(
            [make_scan("a.png")],
            template,
            on_progress=seen.append,
            should_cancel=lambda: True,
        )
        assert report.cancelled is True
        assert report.total == 0
        assert seen[0].stage is BatchStage.CANCELLED

    def test_an_empty_batch_is_not_an_error(self, template):
        report = process_batch([], template)
        assert report.total == 0
        assert report.cancelled is False


class TestPlanOutputName:
    def test_a_shared_allocator_continues_an_earlier_run_s_numbering(self, template, make_scan):
        allocator = FilenameAllocator()
        options = BatchOptions(rename_with_identifier=True)

        first = process_scan(
            make_scan("a.png", roll="120317"),
            template,
            options=options,
            allocator=allocator,
        )
        second = process_scan(
            make_scan("b.png", roll="120317"),
            template,
            options=options,
            allocator=allocator,
        )
        assert (first.output_name, second.output_name) == ("120317.png", "120317_a.png")

    def test_a_template_without_an_identifier_field_explains_why_nothing_was_renamed(
        self, template, make_scan
    ):
        from omr_scanner.services.recognition_service import recognise_scan

        without = template.model_copy(
            update={"zones": tuple(z for z in template.zones if z.id != "roll_number")}
        )
        result = recognise_scan(make_scan("a.png"), without, with_preview=False)
        name, message = plan_output_name(result, FilenameAllocator())

        assert name == "UNRESOLVED_001.png"
        assert "no identifier field" in message
