"""The developer command line tools.

These are thin front ends, so the tests are correspondingly thin: that the
arguments are wired to the right places, that a refusal becomes a non-zero exit
code with a readable reason rather than a traceback, and that nothing is written
outside the directory the caller named.
"""

from __future__ import annotations

import pytest

from omr_scanner.errors import ImageValidationError
from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheetSpec,
    apply_distortion,
    render_sheet,
)
from omr_scanner.services.alignment_service import load_scan_image, save_image
from omr_scanner.tools import align_image, make_test_sheet


@pytest.fixture
def scan_file(tmp_path, canonical_sheet):
    """A distorted synthetic scan written to a temporary file."""
    distorted = apply_distortion(
        canonical_sheet, DistortionSpec(rotation_degrees=5.0, perspective_strength=0.02, seed=3)
    )
    path = tmp_path / "scan.png"
    save_image(distorted.image, path)
    return path


class TestAlignImage:
    def test_a_good_scan_exits_successfully(self, scan_file, capsys):
        assert align_image.main([str(scan_file)]) == align_image.EXIT_OK
        assert "quarter turns" in capsys.readouterr().out

    def test_the_rectified_page_is_written_at_the_canonical_size(self, scan_file, tmp_path):
        output = tmp_path / "aligned.png"
        assert align_image.main([str(scan_file), "--output", str(output)]) == 0
        assert load_scan_image(output).shape == (1754, 1240)

    def test_an_existing_output_is_not_replaced_silently(self, scan_file, tmp_path):
        output = tmp_path / "aligned.png"
        output.write_bytes(b"existing")
        with pytest.raises(ImageValidationError, match="overwrite"):
            align_image.main([str(scan_file), "--output", str(output)])

    def test_overwrite_is_available(self, scan_file, tmp_path):
        output = tmp_path / "aligned.png"
        align_image.main([str(scan_file), "--output", str(output)])
        assert align_image.main(
            [str(scan_file), "--output", str(output), "--overwrite"]
        ) == 0

    def test_diagnostics_go_where_they_are_asked_to(self, scan_file, tmp_path):
        debug = tmp_path / "debug"
        assert align_image.main([str(scan_file), "--debug", str(debug)]) == 0
        assert (debug / "detection.png").is_file()
        assert (debug / "normalized.png").is_file()
        assert (debug / "summary.txt").read_text(encoding="utf-8")

    def test_a_template_supplies_the_canonical_geometry(
        self, scan_file, tmp_path, example_template_path
    ):
        output = tmp_path / "aligned.png"
        assert align_image.main(
            [
                str(scan_file),
                "--template",
                str(example_template_path),
                "--output",
                str(output),
            ]
        ) == 0
        assert load_scan_image(output).shape == (1754, 1240)

    def test_colour_output_is_available(self, scan_file, tmp_path):
        output = tmp_path / "aligned.png"
        assert align_image.main(
            [str(scan_file), "--color", "--output", str(output)]
        ) == 0
        assert load_scan_image(output, color=True).ndim == 3

    def test_an_unalignable_sheet_exits_non_zero_with_its_code(self, tmp_path, capsys):
        sheet = render_sheet(SyntheticSheetSpec(omit_orientation_marker=True))
        path = tmp_path / "bad.png"
        save_image(apply_distortion(sheet, DistortionSpec()).image, path)

        assert align_image.main([str(path)]) == align_image.EXIT_ALIGNMENT_FAILED
        assert "ORIENTATION_NOT_FOUND" in capsys.readouterr().err

    def test_an_unreadable_file_exits_non_zero(self, tmp_path, capsys):
        path = tmp_path / "notes.png"
        path.write_text("not a scan", encoding="utf-8")
        assert align_image.main([str(path)]) == align_image.EXIT_ALIGNMENT_FAILED
        assert "Alignment failed" in capsys.readouterr().err

    def test_a_damaged_template_exits_non_zero(self, scan_file, tmp_path, capsys):
        template = tmp_path / "broken.omrt"
        template.write_text("{ not json", encoding="utf-8")
        assert align_image.main(
            [str(scan_file), "--template", str(template)]
        ) == align_image.EXIT_ALIGNMENT_FAILED
        assert "Template error" in capsys.readouterr().err

    def test_the_parser_documents_every_option(self):
        options = {
            action.dest for action in align_image.build_parser()._actions
        }
        assert {"image", "template", "output", "debug", "overwrite", "color"} <= options


class TestMakeTestSheet:
    def test_a_sheet_is_written_with_its_ground_truth_printed(self, tmp_path, capsys):
        output = tmp_path / "sheet.png"
        assert make_test_sheet.main([str(output)]) == make_test_sheet.EXIT_OK
        assert output.is_file()
        printed = capsys.readouterr().out
        assert "Marker centres" in printed
        assert "Control points" in printed

    def test_the_default_sheet_is_the_canonical_page_plus_margins(self, tmp_path):
        output = tmp_path / "sheet.png"
        make_test_sheet.main([str(output), "--margin", "0"])
        assert load_scan_image(output).shape == (1754, 1240)

    def test_a_requested_size_is_honoured(self, tmp_path):
        output = tmp_path / "sheet.png"
        make_test_sheet.main(
            [str(output), "--width", "620", "--height", "877", "--margin", "0"]
        )
        assert load_scan_image(output).shape == (877, 620)

    def test_a_distorted_sheet_can_be_produced_and_then_aligned(self, tmp_path):
        # The manual smoke test, run automatically: generate, align, succeed.
        scan = tmp_path / "scan.png"
        aligned = tmp_path / "aligned.png"
        make_test_sheet.main(
            [
                str(scan),
                "--rotate",
                "6",
                "--scale",
                "0.9",
                "--perspective",
                "0.02",
                "--blur",
                "3",
                "--noise",
                "4",
                "--seed",
                "7",
            ]
        )
        assert align_image.main([str(scan), "--output", str(aligned)]) == 0
        assert load_scan_image(aligned).shape == (1754, 1240)

    def test_an_existing_file_is_not_replaced_silently(self, tmp_path):
        output = tmp_path / "sheet.png"
        make_test_sheet.main([str(output)])
        with pytest.raises(ImageValidationError, match="overwrite"):
            make_test_sheet.main([str(output)])

    def test_generation_is_reproducible(self, tmp_path):
        first, second = tmp_path / "a.png", tmp_path / "b.png"
        arguments = ["--rotate", "3", "--noise", "6", "--seed", "11"]
        make_test_sheet.main([str(first), *arguments])
        make_test_sheet.main([str(second), *arguments])
        assert first.read_bytes() == second.read_bytes()
