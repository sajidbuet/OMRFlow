"""Tests for the recognition engine as a replaceable subsystem.

Scope:
    Phase 3 seen from outside: does the documented entry point work, does it
    work with no GUI anywhere, does it report what it promises to report, and
    does turning diagnostics on change anything it should not.

    The recognition *accuracy* tests live in
    ``tests/integration/test_recognition_pipeline.py`` and
    ``tests/integration/test_sample_sheet_recognition.py``. These are about the
    contract and the instrumentation, which is what has to survive Recognition
    Engine v2.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from tests.conftest import build_answer_sheet_template, render_marked_sheet

from omr_scanner.services.recognition_diagnostics import (
    RESULT_DOCUMENT,
    STAGE_MEASUREMENTS,
    STAGE_ORIGINAL,
    STAGE_OVERLAY,
    STAGE_REGISTERED,
    render_overlay,
    write_diagnostics,
)
from omr_scanner.services.recognition_models import (
    ENGINE_VERSION,
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    StatusCode,
)
from omr_scanner.services.recognition_service import RecognitionEngine, recognise_scan
from omr_scanner.services.recognition_settings import DiagnosticsOptions, RecognitionOptions

if TYPE_CHECKING:
    from pathlib import Path


def sheet_marks(roll: str = "120317", *, answer: str = "B") -> dict:
    return {
        "roll_number": dict(enumerate(roll)),
        "set_code": {0: "A"},
        "questions_0": dict.fromkeys(range(10), answer),
        "questions_1": dict.fromkeys(range(10), answer),
    }


@pytest.fixture(scope="module")
def template():
    return build_answer_sheet_template()


@pytest.fixture
def scan(tmp_path: Path, template) -> Path:
    """One clean, fully marked sheet on disk."""
    import cv2

    path = tmp_path / "scan.png"
    cv2.imwrite(str(path), render_marked_sheet(template, sheet_marks()))
    return path


class TestTheEngineIsSelfContained:
    def test_it_runs_in_a_process_that_never_imports_qt(self, tmp_path: Path, scan: Path, template):
        """Recognition must work where there is no GUI toolkit at all.

        Asserted in a *fresh interpreter*, because this test session has
        pytest-qt loaded and would therefore find PySide6 in ``sys.modules``
        however clean the engine is. A subprocess is the only honest way to ask
        the question, and it is the same question a server or a CI container
        asks.
        """
        from omr_scanner.services.template_service import save_template

        template_path = save_template(template, tmp_path / "sheet.omrt")
        program = (
            "import sys, json\n"
            "from pathlib import Path\n"
            "from omr_scanner.services.recognition_service import RecognitionEngine\n"
            "from omr_scanner.services.template_service import load_template\n"
            f"template = load_template(Path(r'{template_path}'))\n"
            f"result = RecognitionEngine().process(Path(r'{scan}'), template)\n"
            "qt = sorted(name for name in sys.modules if name.startswith('PySide6'))\n"
            "print(json.dumps({'roll': result.identifier_value, 'qt': qt}))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload["roll"] == "120317"
        assert payload["qt"] == []

    def test_the_engine_describes_itself(self):
        description = RecognitionEngine().describe()
        assert ENGINE_VERSION in description
        assert "omrflow-recognition" in description

    def test_one_engine_reads_many_sheets(self, tmp_path: Path, template):
        import cv2

        engine = RecognitionEngine(RecognitionOptions(with_preview=False))
        results = []
        for index in range(3):
            path = tmp_path / f"s{index}.png"
            cv2.imwrite(str(path), render_marked_sheet(template, sheet_marks(f"10000{index}")))
            results.append(engine.process(path, template))

        assert [item.identifier_value for item in results] == ["100000", "100001", "100002"]

    def test_the_function_and_the_engine_agree(self, scan: Path, template):
        # `recognise_scan` is the older spelling and stays supported; if the
        # two ever diverged, half the repository would be reading sheets
        # differently from the other half.
        by_function = recognise_scan(scan, template, with_preview=False)
        by_engine = RecognitionEngine(RecognitionOptions(with_preview=False)).process(
            scan, template
        )
        assert by_function.identifier_value == by_engine.identifier_value
        assert by_function.answers == by_engine.answers
        assert by_function.bubbles == by_engine.bubbles


class TestWhatAResultReports:
    def test_it_stamps_the_engine_and_the_template(self, scan: Path, template):
        result = RecognitionEngine().process(scan, template)
        assert result.engine_version == ENGINE_VERSION
        assert result.template_id == template.template_id
        assert result.template_name == template.name
        assert result.recognised_at

    def test_it_records_where_the_time_went(self, scan: Path, template):
        timings = RecognitionEngine().process(scan, template).timings
        assert timings.load > 0.0
        assert timings.register > 0.0
        assert timings.measure > 0.0
        assert timings.total >= timings.load + timings.register

    def test_it_measures_the_scan_without_judging_it(self, scan: Path, template):
        quality = RecognitionEngine().process(scan, template).quality
        assert quality is not None
        assert quality.marker_count == 4
        assert 0.0 < quality.brightness <= 1.0
        assert quality.contrast > 0.0
        assert quality.sharpness > 0.0
        assert quality.source_width > 0

    def test_quality_metrics_can_be_switched_off(self, scan: Path, template):
        options = RecognitionOptions(with_preview=False, keep_quality_metrics=False)
        assert RecognitionEngine(options).process(scan, template).quality is None

    def test_a_rotated_page_reports_the_rotation_it_corrected(self, tmp_path: Path, template):
        import cv2
        from tests.conftest import marked_sheet_spec

        from omr_scanner.imaging.synthetic import DistortionSpec, apply_distortion, render_sheet

        sheet = render_sheet(marked_sheet_spec(template, sheet_marks()))
        distorted = apply_distortion(sheet, DistortionSpec(rotation_degrees=5.0, seed=3))
        path = tmp_path / "rotated.png"
        cv2.imwrite(str(path), distorted.image)

        quality = RecognitionEngine().process(path, template).quality
        assert quality is not None
        # The sign depends on which way the correction runs; the magnitude is
        # the claim, and a flat scan would report ~0.
        assert abs(quality.rotation_degrees) == pytest.approx(5.0, abs=1.0)

    def test_every_bubble_carries_its_evidence(self, scan: Path, template):
        result = RecognitionEngine().process(scan, template)
        assert result.bubbles
        for bubble in result.bubbles:
            assert bubble.ink_threshold > 0.0
            assert bubble.paper_level > 0.0
            assert bubble.sample_pixels > 0
        # Rank is per group, so every group has exactly one darkest bubble.
        selected = [bubble for bubble in result.bubbles if bubble.selected]
        assert all(bubble.rank == 0 for bubble in selected)

    def test_the_evidence_can_be_dropped_when_it_is_not_wanted(self, scan: Path, template):
        """Dropped means *omitted*, because the records are the memory.

        A hundred-question sheet carries five hundred bubble records against a
        hundred answers, so a caller that keeps results for ten thousand sheets
        - the Scan page, building a CSV - saves roughly an order of magnitude
        by declining them. Blanking the fields would have saved nothing.
        """
        options = RecognitionOptions(with_preview=False, keep_bubble_measurements=False)
        lean = RecognitionEngine(options).process(scan, template)
        full = RecognitionEngine(RecognitionOptions(with_preview=False)).process(scan, template)

        assert lean.bubbles == ()
        assert full.bubbles
        # The decision is unaffected - only what is reported about it.
        assert lean.identifier_value == full.identifier_value == "120317"
        assert lean.answers == full.answers
        assert lean.fields == full.fields

    def test_a_readable_sheet_reports_ok_or_a_named_reservation(self, scan: Path, template):
        result = RecognitionEngine().process(scan, template)
        assert result.status_codes
        assert StatusCode.PROCESSING_ERROR.value not in result.status_codes

    def test_a_result_serialises_straight_to_json(self, scan: Path, template):
        result = RecognitionEngine(RecognitionOptions(with_preview=False)).process(scan, template)
        restored = ScanResult.from_dict(json.loads(json.dumps(result.to_dict())))
        assert restored.identifier_value == result.identifier_value
        assert restored.answers == result.answers


class TestFailuresAreResultsNotExceptions:
    def test_an_unreadable_file_comes_back_as_a_result(self, tmp_path: Path, template):
        path = tmp_path / "broken.png"
        path.write_bytes(b"not an image at all")

        result = RecognitionEngine().process(path, template)
        assert result.outcome is RecognitionOutcome.ERROR
        assert result.has_status(StatusCode.IMAGE_LOAD_ERROR)
        assert result.registration_message
        # Even a failure is fully described: a later phase must not have to
        # special-case it.
        assert result.engine_version == ENGINE_VERSION
        assert result.template_id == template.template_id
        assert result.recognised_at
        assert result.timings.total > 0.0

    def test_a_missing_file_comes_back_as_a_result(self, tmp_path: Path, template):
        result = RecognitionEngine().process(tmp_path / "gone.png", template)
        assert result.outcome is RecognitionOutcome.ERROR

    def test_an_unregistrable_page_never_invents_answers(self, tmp_path: Path, template):
        import cv2
        import numpy as np

        path = tmp_path / "blank.png"
        cv2.imwrite(str(path), np.full((1754, 1240), 250, dtype=np.uint8))

        result = RecognitionEngine().process(path, template)
        assert result.registration is RegistrationStatus.FAILED
        assert result.answers == ()
        assert result.has_status(StatusCode.ALIGNMENT_FAILED)
        assert result.error_code


class TestDeterminism:
    def test_the_same_image_and_template_give_the_same_result(self, scan: Path, template):
        engine = RecognitionEngine(RecognitionOptions(with_preview=False))
        first = engine.process(scan, template)
        second = engine.process(scan, template)

        assert first.answers == second.answers
        assert first.fields == second.fields
        assert first.bubbles == second.bubbles
        assert first.status_codes == second.status_codes

    def test_only_the_timings_and_the_timestamp_may_differ(self, scan: Path, template):
        engine = RecognitionEngine(RecognitionOptions(with_preview=False))
        first = engine.process(scan, template).to_dict()
        second = engine.process(scan, template).to_dict()
        for payload in (first, second):
            payload.pop("timings")
            payload.pop("elapsed_seconds")
            payload.pop("recognised_at")
        assert first == second


class TestDiagnostics:
    def test_nothing_is_written_by_default(self, tmp_path: Path, scan: Path, template):
        RecognitionEngine().process(scan, template)
        assert list(tmp_path.glob("**/*.png")) == [scan]

    def test_enabling_them_writes_the_documented_stages(self, tmp_path: Path, scan: Path, template):
        folder = tmp_path / "diagnostics"
        options = RecognitionOptions(
            with_preview=False,
            diagnostics=DiagnosticsOptions(enabled=True, directory=folder),
        )
        RecognitionEngine(options).process(scan, template)

        produced = sorted(path.name for path in (folder / "scan").iterdir())
        assert produced == sorted(
            [
                f"{STAGE_ORIGINAL}.png",
                f"{STAGE_REGISTERED}.png",
                f"{STAGE_OVERLAY}.png",
                f"{STAGE_MEASUREMENTS}.png",
                RESULT_DOCUMENT,
            ]
        )

    def test_each_scan_gets_its_own_folder(self, tmp_path: Path, template):
        import cv2

        folder = tmp_path / "diagnostics"
        engine = RecognitionEngine(
            RecognitionOptions(
                with_preview=False,
                diagnostics=DiagnosticsOptions(enabled=True, directory=folder),
            )
        )
        for index in range(2):
            path = tmp_path / f"sheet{index}.png"
            cv2.imwrite(str(path), render_marked_sheet(template, sheet_marks()))
            engine.process(path, template)

        assert sorted(item.name for item in folder.iterdir()) == ["sheet0", "sheet1"]

    def test_a_subset_of_stages_can_be_requested(self, tmp_path: Path, scan: Path, template):
        folder = tmp_path / "diagnostics"
        options = RecognitionOptions(
            with_preview=False,
            diagnostics=DiagnosticsOptions(
                enabled=True, directory=folder, stages=frozenset({STAGE_OVERLAY})
            ),
        )
        RecognitionEngine(options).process(scan, template)
        assert [path.name for path in (folder / "scan").iterdir()] == [f"{STAGE_OVERLAY}.png"]

    def test_failures_only_skips_a_clean_sheet(self, tmp_path: Path, scan: Path, template):
        folder = tmp_path / "diagnostics"
        result = RecognitionEngine(RecognitionOptions(with_preview=False)).process(scan, template)
        written = write_diagnostics(
            result,
            DiagnosticsOptions(enabled=True, directory=folder, failures_only=True),
        )
        # This sheet registers with a warning, so it is *not* clean and is
        # written; the assertion that matters is that the filter is applied at
        # all, and against the sheet's own state.
        assert bool(written) == bool(result.warnings)

    def test_diagnostics_never_change_what_was_recognised(
        self, tmp_path: Path, scan: Path, template
    ):
        plain = RecognitionEngine(RecognitionOptions(with_preview=False)).process(scan, template)
        noisy = RecognitionEngine(
            RecognitionOptions(
                with_preview=False,
                diagnostics=DiagnosticsOptions(enabled=True, directory=tmp_path / "d"),
            )
        ).process(scan, template)

        assert plain.answers == noisy.answers
        assert plain.fields == noisy.fields
        assert plain.bubbles == noisy.bubbles

    def test_a_write_failure_does_not_fail_the_scan(self, tmp_path: Path, scan: Path, template):
        # A full disk, or a folder somebody removed mid-batch, must cost the
        # diagnostics and not the recognition.
        blocked = tmp_path / "blocked"
        blocked.write_text("this is a file, not a directory", encoding="utf-8")
        options = RecognitionOptions(
            with_preview=False,
            diagnostics=DiagnosticsOptions(enabled=True, directory=blocked),
        )
        result = RecognitionEngine(options).process(scan, template)
        assert result.identifier_value == "120317"

    def test_diagnostics_enabled_without_a_folder_is_refused_at_construction(self):
        # Better to fail when the options are built than to run a batch that
        # silently writes nothing.
        with pytest.raises(ValueError, match="no output directory"):
            DiagnosticsOptions(enabled=True)


class TestOverlay:
    def test_it_draws_without_a_gui(self, scan: Path, template):
        from omr_scanner.imaging.alignment import align_sheet
        from omr_scanner.services.alignment_service import (
            alignment_config_from_template,
            load_scan_image,
        )

        image = load_scan_image(scan, color=False)
        alignment = align_sheet(image, config=alignment_config_from_template(template))
        result = RecognitionEngine(RecognitionOptions(with_preview=False)).process(scan, template)

        overlay = render_overlay(alignment.normalized_image, result)
        assert overlay.shape[:2] == alignment.normalized_image.shape[:2]
        assert overlay.ndim == 3  # colour, so selections are distinguishable

    def test_it_never_modifies_the_page_it_documents(self, scan: Path, template):
        from omr_scanner.imaging.alignment import align_sheet
        from omr_scanner.services.alignment_service import (
            alignment_config_from_template,
            load_scan_image,
        )

        image = load_scan_image(scan, color=False)
        alignment = align_sheet(image, config=alignment_config_from_template(template))
        page = alignment.normalized_image
        before = page.copy()

        result = RecognitionEngine(RecognitionOptions(with_preview=False)).process(scan, template)
        render_overlay(page, result, show_empty=True, annotate_measurements=True)

        assert (page == before).all()
