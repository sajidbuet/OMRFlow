"""Recognition against the repository's real scanned sample sheet.

Everything else in the Phase 3 test suite reads sheets this project rendered
itself, which proves the pipeline is self-consistent but not that it copes with
a real scanner, real printing or real ink. `examples/ECE-0000.png` is an actual
scan: its bubbles carry printed option glyphs, its paper is not white, its
registration squares have scanner ringing around them, and its answers were
filled in by hand.

The original file is opened read-only. Every transformed variant is rendered
into the test's own temporary directory.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

from omr_scanner.domain.template import OmrTemplate
from omr_scanner.imaging.synthetic import (
    DistortionSpec,
    SyntheticSheet,
    SyntheticSheetSpec,
    apply_distortion,
)
from omr_scanner.services.recognition_service import (
    RecognitionOutcome,
    RegistrationStatus,
    recognise_scan,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_IMAGE = REPO_ROOT / "examples" / "ECE-0000.png"
SAMPLE_TEMPLATE = REPO_ROOT / "examples" / "templates" / "ece_0000_sample.omrt"

EXPECTED_ROLL = "00000000"
EXPECTED_SET_CODE = "10"
EXPECTED_ANSWERS = (
    "aaaabbbbccccddddabcdabcdabcdabcdabcdabcdabcdabcdabcdabcd"
    "abcdabcdabcdabcdabcdabcdabcdabcdabcdaaabbbbb"
)
"""What is actually marked on the sheet, one character per question.

Read off the paper, and recognisable as a deliberate demonstration pattern -
four of each option, then ABCD repeating - which is what makes it usable as
ground truth: a decode that drifted by one column would break the pattern
visibly rather than producing another plausible-looking string."""

pytestmark = pytest.mark.skipif(
    not (SAMPLE_IMAGE.exists() and SAMPLE_TEMPLATE.exists()),
    reason="the packaged sample sheet or its template is not present",
)


@pytest.fixture(scope="module")
def template() -> OmrTemplate:
    return OmrTemplate.model_validate_json(SAMPLE_TEMPLATE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sample_image():
    image = cv2.imread(str(SAMPLE_IMAGE), cv2.IMREAD_GRAYSCALE)
    assert image is not None, f"could not read {SAMPLE_IMAGE}"
    return image


def answer_string(result) -> str:
    return "".join(answer.value or "_" for answer in result.answers)


def assert_reads_the_sample(result) -> None:
    assert result.registration is not RegistrationStatus.FAILED, (
        result.registration_message
    )
    assert result.identifier_value == EXPECTED_ROLL
    assert result.set_code_value == EXPECTED_SET_CODE
    assert answer_string(result) == EXPECTED_ANSWERS


class TestTheSampleItself:
    def test_the_original_is_never_modified_by_recognition(self, template):
        before = SAMPLE_IMAGE.read_bytes()
        recognise_scan(SAMPLE_IMAGE, template, with_preview=True)
        assert SAMPLE_IMAGE.read_bytes() == before

    def test_the_real_scan_is_read_completely(self, template):
        result = recognise_scan(SAMPLE_IMAGE, template, with_preview=False)
        assert_reads_the_sample(result)
        assert result.outcome is RecognitionOutcome.COMPLETE
        assert result.review_count == 0

    def test_all_hundred_questions_are_answered(self, template):
        result = recognise_scan(SAMPLE_IMAGE, template, with_preview=False)
        assert len(result.answers) == 100
        assert [a.number for a in result.answers] == list(range(1, 101))
        assert all(a.value for a in result.answers), "no question should read blank"

    def test_the_eight_digit_roll_number_keeps_its_leading_zeros(self, template):
        result = recognise_scan(SAMPLE_IMAGE, template, with_preview=False)
        assert result.identifier_value == "00000000"
        assert len(result.identifier_value) == 8

    def test_the_two_character_set_code_is_not_reduced_to_a_number(self, template):
        # "10" is a set code. It is two printed positions, and it must not come
        # back as the integer ten or as "1".
        result = recognise_scan(SAMPLE_IMAGE, template, with_preview=False)
        assert result.set_code_value == "10"
        assert isinstance(result.set_code_value, str)

    def test_marked_bubbles_separate_cleanly_from_printed_glyphs(self, template):
        # The measurement that the whole ink-level rule exists to get right: an
        # untouched bubble still contains its printed option symbol.
        result = recognise_scan(SAMPLE_IMAGE, template, with_preview=False)
        selected = [b.fill_ratio for b in result.bubbles if b.selected]
        unselected = [b.fill_ratio for b in result.bubbles if not b.selected]

        assert min(selected) > max(unselected), (
            "the faintest mark must still be darker than the darkest printed glyph"
        )
        # A wide, unambiguous gap rather than a threshold sitting between two
        # touching populations.
        assert min(selected) - max(unselected) > 0.5

    def test_the_overlay_covers_every_bubble_the_template_defines(self, template):
        result = recognise_scan(SAMPLE_IMAGE, template, with_preview=False)
        # 8 roll digits x 10 + 2 set positions x 10 + 100 questions x 4.
        assert len(result.bubbles) == 8 * 10 + 2 * 10 + 100 * 4
        assert len([b for b in result.bubbles if b.selected]) == 8 + 2 + 100

    def test_a_preview_is_produced_at_a_bounded_size(self, template):
        from omr_scanner.services.recognition_service import DEFAULT_PREVIEW_MAX_DIMENSION

        result = recognise_scan(SAMPLE_IMAGE, template, with_preview=True)
        assert result.preview is not None
        assert max(result.preview.width, result.preview.height) <= (
            DEFAULT_PREVIEW_MAX_DIMENSION
        )
        assert 0.0 < result.preview_scale <= 1.0


class TestTransformedCopiesOfTheRealScan:
    """The sample, geometrically degraded further than a scanner would.

    Each variant is written into ``tmp_path``; the packaged original is only
    ever read.
    """

    @pytest.fixture
    def variant(self, tmp_path: Path, sample_image):
        counter = {"n": 0}
        # The real scan stands in for a rendered page, so the same distortion
        # machinery the synthetic tests use applies to it unchanged. Only the
        # page size matters here; there are no synthetic ground-truth points to
        # carry, because the ground truth in this file is the marked answers.
        height, width = sample_image.shape
        sheet = SyntheticSheet(
            image=sample_image,
            marker_centers=(),
            control_points=(),
            spec=SyntheticSheetSpec(width=width, height=height),
        )

        def make(spec: DistortionSpec, suffix: str = ".png") -> Path:
            counter["n"] += 1
            path = tmp_path / f"variant{counter['n']}{suffix}"
            cv2.imwrite(str(path), apply_distortion(sheet, spec).image)
            return path

        return make

    @pytest.mark.parametrize("degrees", [-3.0, 3.0])
    def test_a_rotated_copy_reads_the_same(self, template, variant, degrees):
        path = variant(DistortionSpec(rotation_degrees=degrees))
        assert_reads_the_sample(recognise_scan(path, template, with_preview=False))

    @pytest.mark.parametrize("degrees", [90.0, 180.0, 270.0])
    def test_a_quarter_turned_copy_is_turned_back(self, template, variant, degrees):
        path = variant(DistortionSpec(rotation_degrees=degrees))
        assert_reads_the_sample(recognise_scan(path, template, with_preview=False))

    @pytest.mark.parametrize("factor", [0.7, 1.3])
    def test_a_rescaled_copy_reads_the_same(self, template, variant, factor):
        path = variant(DistortionSpec(scale_x=factor, scale_y=factor))
        assert_reads_the_sample(recognise_scan(path, template, with_preview=False))

    def test_a_shifted_copy_reads_the_same(self, template, variant):
        path = variant(DistortionSpec(translate_x_px=80, translate_y_px=-55))
        assert_reads_the_sample(recognise_scan(path, template, with_preview=False))

    def test_a_copy_photographed_at_an_angle_reads_the_same(self, template, variant):
        path = variant(DistortionSpec(perspective_strength=0.03, seed=4))
        assert_reads_the_sample(recognise_scan(path, template, with_preview=False))

    def test_a_jpeg_copy_reads_the_same(self, template, variant):
        path = variant(DistortionSpec(rotation_degrees=1.5), suffix=".jpg")
        assert_reads_the_sample(recognise_scan(path, template, with_preview=False))

    def test_a_crooked_rescaled_shifted_copy_reads_the_same(self, template, variant):
        path = variant(
            DistortionSpec(
                rotation_degrees=-2.5,
                scale_x=0.85,
                scale_y=0.86,
                translate_x_px=40,
                translate_y_px=25,
                perspective_strength=0.02,
                seed=6,
            )
        )
        assert_reads_the_sample(recognise_scan(path, template, with_preview=False))
