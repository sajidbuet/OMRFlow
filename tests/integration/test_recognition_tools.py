"""Tests for the headless Phase 3 command line tools.

Why drive them through ``main(argv)``:
    These tools are the proof that recognition needs no GUI. Testing the
    functions they call would prove nothing about *them* - the argument
    parsing, the exit codes, the files they write - and those are exactly what
    a developer or a CI job depends on.

No subprocesses: ``main`` returns an exit code by design, so a test can call it
directly, which is both faster and able to say *why* a run failed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.evaluation.benchmark import ERRORS_FILENAME, SUMMARY_FILENAME
from omr_scanner.evaluation.synthetic_dataset import GROUND_TRUTH_DIRNAME, IMAGES_DIRNAME
from omr_scanner.services.template_service import save_template
from omr_scanner.tools import benchmark_recognition, make_dataset, recognise

if TYPE_CHECKING:
    from pathlib import Path


def sheet_marks(roll: str = "120317") -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), "B"),
        "questions_1": dict.fromkeys(range(10), "C"),
    }


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture
def template_path(tmp_path: Path, template) -> Path:
    return save_template(template, tmp_path / "sheet.omrt")


@pytest.fixture
def scans(tmp_path: Path, template) -> Path:
    """A folder of three readable sheets."""
    import cv2

    folder = tmp_path / "scans"
    folder.mkdir()
    for index in range(3):
        cv2.imwrite(
            str(folder / f"scan{index}.png"),
            render_marked_sheet(template, sheet_marks(f"10000{index}")),
        )
    return folder


class TestRecogniseTool:
    def test_it_reads_a_folder_and_reports_success(self, scans: Path, template_path: Path, capsys):
        code = recognise.main([str(scans), "--template", str(template_path)])
        assert code == 0
        output = capsys.readouterr().out
        assert "3 processed" in output
        assert "100000" in output

    def test_it_writes_one_json_document_per_scan(
        self, tmp_path: Path, scans: Path, template_path: Path
    ):
        out = tmp_path / "json"
        code = recognise.main(
            [str(scans), "--template", str(template_path), "--json-dir", str(out), "--quiet"]
        )
        assert code == 0
        written = sorted(path.name for path in out.glob("*.json"))
        assert written == ["scan0.json", "scan1.json", "scan2.json"]

        payload = json.loads((out / "scan0.json").read_text(encoding="utf-8"))
        assert payload["identifier_value"] == "100000"
        assert payload["engine"]["version"]

    def test_it_can_write_every_result_as_one_document(
        self, tmp_path: Path, scans: Path, template_path: Path
    ):
        destination = tmp_path / "all.json"
        recognise.main(
            [str(scans), "--template", str(template_path), "--json", str(destination), "--quiet"]
        )
        payload = json.loads(destination.read_text(encoding="utf-8"))
        assert len(payload) == 3

    def test_it_writes_overlays_when_asked(
        self, tmp_path: Path, scans: Path, template_path: Path
    ):
        out = tmp_path / "overlays"
        recognise.main(
            [str(scans), "--template", str(template_path), "--overlay-dir", str(out), "--quiet"]
        )
        assert sorted(path.name for path in out.glob("*.png")) == [
            "scan0_overlay.png",
            "scan1_overlay.png",
            "scan2_overlay.png",
        ]

    def test_it_writes_the_staged_diagnostics_when_asked(
        self, tmp_path: Path, scans: Path, template_path: Path
    ):
        out = tmp_path / "diagnostics"
        recognise.main(
            [str(scans), "--template", str(template_path), "--diagnostics", str(out), "--quiet"]
        )
        assert sorted(item.name for item in out.iterdir()) == ["scan0", "scan1", "scan2"]
        assert (out / "scan0" / "02_overlay.png").is_file()

    def test_nothing_is_written_when_nothing_is_asked_for(
        self, tmp_path: Path, scans: Path, template_path: Path
    ):
        before = set(tmp_path.rglob("*"))
        recognise.main([str(scans), "--template", str(template_path), "--quiet"])
        assert set(tmp_path.rglob("*")) == before

    def test_a_corrupt_scan_reports_a_failure_exit_code_without_stopping(
        self, scans: Path, template_path: Path, capsys
    ):
        (scans / "broken.png").write_bytes(b"not an image")
        code = recognise.main([str(scans), "--template", str(template_path)])
        assert code == 1  # something failed...
        output = capsys.readouterr().out
        assert "4 processed" in output  # ...but everything was attempted
        assert "3 complete" in output

    def test_an_unreadable_template_is_refused_clearly(self, tmp_path: Path, scans: Path, capsys):
        bad = tmp_path / "bad.omrt"
        bad.write_text("{not a template}", encoding="utf-8")
        assert recognise.main([str(scans), "--template", str(bad)]) == 1
        assert "template" in capsys.readouterr().err.lower()

    def test_an_empty_folder_says_so(self, tmp_path: Path, template_path: Path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert recognise.main([str(empty), "--template", str(template_path)]) == 1
        assert "No supported scans" in capsys.readouterr().err

    def test_it_runs_on_several_workers(self, scans: Path, template_path: Path, capsys):
        code = recognise.main(
            [str(scans), "--template", str(template_path), "--workers", "2", "--quiet"]
        )
        assert code == 0
        assert "2 worker(s)" in capsys.readouterr().out

    @pytest.mark.parametrize("requested", ["auto", "AUTOMATIC", "4"])
    def test_worker_arguments_resolve_the_way_the_application_does(self, requested: str):
        assert recognise.resolve_workers(requested, item_count=3) >= 1
        assert recognise.resolve_workers(requested, item_count=3) <= 3

    @pytest.mark.parametrize("requested", ["0", "-2", "many"])
    def test_a_nonsense_worker_argument_is_refused(self, requested: str):
        with pytest.raises(ValueError):
            recognise.resolve_workers(requested, item_count=3)


class TestMakeDatasetTool:
    def test_it_writes_a_complete_dataset(self, tmp_path: Path, template_path: Path, capsys):
        out = tmp_path / "dataset"
        code = make_dataset.main(
            [
                str(out),
                "--template",
                str(template_path),
                "--count",
                "3",
                "--seed",
                "5",
                "--profile",
                "clean",
            ]
        )
        assert code == 0
        assert len(list((out / IMAGES_DIRNAME).glob("*.png"))) == 3
        assert len(list((out / GROUND_TRUTH_DIRNAME).glob("*.json"))) == 3
        assert (out / "manifest.json").is_file()

        printed = capsys.readouterr().out
        assert "3 sheet(s)" in printed
        # The caveat travels with the tool, not only with the documentation.
        assert "not real-world accuracy" in printed

    def test_a_missing_template_is_refused(self, tmp_path: Path, capsys):
        code = make_dataset.main([str(tmp_path / "out"), "--template", str(tmp_path / "no.omrt")])
        assert code == 1
        assert "template" in capsys.readouterr().err.lower()

    def test_an_impossible_count_is_refused(self, tmp_path: Path, template_path: Path, capsys):
        code = make_dataset.main(
            [str(tmp_path / "out"), "--template", str(template_path), "--count", "0"]
        )
        assert code == 1
        assert "at least one sheet" in capsys.readouterr().err


class TestBenchmarkTool:
    @pytest.fixture
    def dataset(self, tmp_path: Path, template_path: Path) -> Path:
        out = tmp_path / "dataset"
        make_dataset.main(
            [str(out), "--template", str(template_path), "--count", "3",
             "--seed", "21", "--profile", "clean"]
        )
        return out

    def test_it_scores_a_dataset_and_writes_a_report(
        self, tmp_path: Path, dataset: Path, template_path: Path, capsys
    ):
        report = tmp_path / "report"
        code = benchmark_recognition.main(
            [str(dataset), "--template", str(template_path), "--report", str(report)]
        )
        assert code == 0

        printed = capsys.readouterr().out
        assert "Answer accuracy" in printed
        assert "Roll accuracy" in printed

        summary = json.loads((report / SUMMARY_FILENAME).read_text(encoding="utf-8"))
        assert summary["scans"] == 3
        assert summary["question_accuracy"] == 1.0
        assert summary["engine_version"]
        assert (report / ERRORS_FILENAME).is_file()

    def test_a_clean_dataset_produces_an_empty_error_list(
        self, tmp_path: Path, dataset: Path, template_path: Path
    ):
        report = tmp_path / "report"
        benchmark_recognition.main(
            [str(dataset), "--template", str(template_path), "--report", str(report)]
        )
        rows = (report / ERRORS_FILENAME).read_text(encoding="utf-8").strip().splitlines()
        assert len(rows) == 1  # the header only

    def test_it_compares_against_a_stored_baseline(
        self, tmp_path: Path, dataset: Path, template_path: Path, capsys
    ):
        first = tmp_path / "first"
        benchmark_recognition.main(
            [str(dataset), "--template", str(template_path), "--report", str(first)]
        )
        capsys.readouterr()

        code = benchmark_recognition.main(
            [
                str(dataset),
                "--template",
                str(template_path),
                "--baseline",
                str(first / SUMMARY_FILENAME),
            ]
        )
        assert code == 0
        printed = capsys.readouterr().out
        assert "Against the baseline" in printed
        assert "unchanged" in printed

    def test_a_missing_ground_truth_folder_is_refused(
        self, tmp_path: Path, template_path: Path, capsys
    ):
        empty = tmp_path / "nothing"
        empty.mkdir()
        code = benchmark_recognition.main([str(empty), "--template", str(template_path)])
        assert code == 1
        assert "Could not start the benchmark" in capsys.readouterr().err

    def test_the_sweep_reports_without_changing_anything(
        self, dataset: Path, template_path: Path, template, capsys
    ):
        before = template.recognition.fill_ratio_threshold
        code = benchmark_recognition.main(
            [str(dataset), "--template", str(template_path), "--sweep-fill", "0.5", "0.6", "0.1"]
        )
        assert code == 0
        printed = capsys.readouterr().out
        assert "Fill-threshold sweep" in printed
        assert "not a calibration" in printed
        # The template on disk, and the one in memory, are untouched: a sweep
        # experiments, it does not retune the application.
        from omr_scanner.services.template_service import load_template

        assert load_template(template_path).recognition.fill_ratio_threshold == before
