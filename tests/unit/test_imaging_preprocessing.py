"""Image validation, grayscale conversion, downscaling and binarisation.

The validation tests matter more than they look: this function is the boundary
where an untrusted file becomes an array the rest of the engine trusts, and the
requirement is that nothing gets past it far enough to produce a raw OpenCV
assertion in front of a user.
"""

from __future__ import annotations

import numpy as np
import pytest

from omr_scanner.errors import ImageValidationError, ImagingError
from omr_scanner.imaging.config import PreprocessingConfig, ThresholdStrategy
from omr_scanner.imaging.preprocessing import (
    binarize,
    prepare_for_detection,
    to_grayscale,
    validate_image,
    working_scale,
)

CONFIG = PreprocessingConfig()

MARKER_SIDE_PX = 12
"""Side of the test square, sized like a real marker: about 3 per cent of the
page width, which matters for the adaptive strategies (see
:class:`TestBinarize`)."""

INK_SAMPLE = (45, 45)
"""A pixel inside the black square."""

PAPER_SAMPLE = (300, 300)
"""A pixel well away from it."""


def page(width: int = 400, height: int = 560, channels: int | None = None):
    """Return a white page with one marker-sized black square."""
    shape = (height, width) if channels is None else (height, width, channels)
    image = np.full(shape, 255, dtype=np.uint8)
    image[40 : 40 + MARKER_SIDE_PX, 40 : 40 + MARKER_SIDE_PX] = 0
    return image


class TestValidateImage:
    def test_a_grayscale_page_is_accepted_unchanged(self):
        image = page()
        assert validate_image(image, config=CONFIG) is image

    @pytest.mark.parametrize("channels", [1, 3, 4])
    def test_supported_channel_counts_are_accepted(self, channels):
        image = page(channels=channels)
        assert validate_image(image, config=CONFIG) is image

    def test_a_non_array_is_rejected(self):
        with pytest.raises(ImageValidationError, match="Expected a NumPy array"):
            validate_image("not an image", config=CONFIG)

    def test_none_is_rejected(self):
        with pytest.raises(ImageValidationError, match="Expected a NumPy array"):
            validate_image(None, config=CONFIG)

    def test_an_empty_array_is_rejected(self):
        with pytest.raises(ImageValidationError):
            validate_image(np.zeros((0, 0), dtype=np.uint8), config=CONFIG)

    def test_a_one_dimensional_array_is_rejected(self):
        with pytest.raises(ImageValidationError, match="2- or 3-dimensional"):
            validate_image(np.zeros(500, dtype=np.uint8), config=CONFIG)

    def test_a_four_dimensional_array_is_rejected(self):
        with pytest.raises(ImageValidationError, match="2- or 3-dimensional"):
            validate_image(np.zeros((2, 100, 100, 3), dtype=np.uint8), config=CONFIG)

    def test_an_unsupported_channel_count_is_rejected(self):
        with pytest.raises(ImageValidationError, match="channels"):
            validate_image(np.zeros((100, 100, 2), dtype=np.uint8), config=CONFIG)

    def test_a_float_image_is_rejected(self):
        with pytest.raises(ImageValidationError, match="8-bit"):
            validate_image(np.zeros((100, 100), dtype=np.float32), config=CONFIG)

    def test_a_sixteen_bit_image_is_rejected(self):
        with pytest.raises(ImageValidationError, match="8-bit"):
            validate_image(np.zeros((100, 100), dtype=np.uint16), config=CONFIG)

    def test_an_image_too_small_for_a_marker_is_rejected(self):
        with pytest.raises(ImageValidationError, match=r"too small|at least"):
            validate_image(np.zeros((20, 20), dtype=np.uint8), config=CONFIG)

    def test_every_validation_failure_is_an_imaging_error(self):
        # The GUI catches the base class; a subclass outside the tree would
        # surface as a traceback.
        with pytest.raises(ImagingError):
            validate_image(np.zeros((1, 1), dtype=np.uint8), config=CONFIG)

    def test_the_failure_carries_a_stable_code(self):
        with pytest.raises(ImageValidationError) as error:
            validate_image(np.zeros((5, 5), dtype=np.uint8), config=CONFIG)
        assert error.value.code == "INVALID_IMAGE"

    def test_the_failure_carries_a_plain_language_message(self):
        with pytest.raises(ImageValidationError) as error:
            validate_image(np.zeros((5, 5), dtype=np.uint8), config=CONFIG)
        assert error.value.user_message
        assert "dtype" not in error.value.user_message


class TestToGrayscale:
    def test_a_grayscale_image_is_copied_not_aliased(self):
        original = page()
        converted = to_grayscale(original)
        converted[0, 0] = 7
        assert original[0, 0] == 255

    def test_a_single_channel_three_dimensional_image_is_flattened(self):
        converted = to_grayscale(page(channels=1))
        assert converted.ndim == 2

    @pytest.mark.parametrize("channels", [3, 4])
    def test_colour_images_become_single_channel(self, channels):
        converted = to_grayscale(page(channels=channels))
        assert converted.ndim == 2
        assert converted.dtype == np.uint8

    def test_the_black_square_survives_conversion(self):
        for channels in (None, 1, 3, 4):
            converted = to_grayscale(page(channels=channels))
            assert converted[INK_SAMPLE] == 0
            assert converted[PAPER_SAMPLE] == 255


class TestWorkingScale:
    def test_a_small_image_is_not_enlarged(self):
        assert working_scale(800, 600, max_dimension_px=2000) == 1.0

    def test_a_large_image_is_reduced_to_the_limit(self):
        assert working_scale(4000, 3000, max_dimension_px=2000) == pytest.approx(0.5)

    def test_the_longer_side_decides(self):
        assert working_scale(1000, 4000, max_dimension_px=2000) == pytest.approx(0.5)


class TestBinarize:
    @pytest.mark.parametrize(
        "strategy",
        [
            ThresholdStrategy.OTSU,
            ThresholdStrategy.ADAPTIVE_MEAN,
            ThresholdStrategy.ADAPTIVE_GAUSSIAN,
        ],
    )
    def test_ink_becomes_white_and_paper_black(self, strategy):
        binary = binarize(page(), config=PreprocessingConfig(threshold_strategy=strategy))
        assert binary[INK_SAMPLE] == 255
        assert binary[PAPER_SAMPLE] == 0

    def test_the_result_is_a_new_array(self):
        image = page()
        binary = binarize(image, config=CONFIG)
        assert binary is not image
        assert binary.shape == image.shape

    def test_otsu_fills_a_blob_larger_than_an_adaptive_neighbourhood(self):
        # Documents the constraint on the adaptive strategies: a solid shape
        # wider than the neighbourhood makes its own local mean, so its interior
        # reads as background and the marker comes out hollow. A global
        # threshold has no such limit. This is why adaptive_block_ratio must
        # stay above the marker's normalised size - see
        # PreprocessingConfig.adaptive_block_ratio.
        image = np.full((400, 400), 255, dtype=np.uint8)
        image[100:200, 100:200] = 0  # 100 px, far wider than the 16 px block
        block_ratio = 0.04

        otsu = binarize(image, config=PreprocessingConfig(adaptive_block_ratio=block_ratio))
        adaptive = binarize(
            image,
            config=PreprocessingConfig(
                threshold_strategy=ThresholdStrategy.ADAPTIVE_MEAN,
                adaptive_block_ratio=block_ratio,
            ),
        )
        assert otsu[150, 150] == 255
        assert adaptive[150, 150] == 0


class TestPrepareForDetection:
    def test_the_source_image_is_never_modified(self):
        image = page()
        before = image.copy()
        prepare_for_detection(image, config=CONFIG)
        assert np.array_equal(image, before)

    def test_the_source_size_is_reported(self):
        prepared = prepare_for_detection(page(400, 560), config=CONFIG)
        assert (prepared.source_width, prepared.source_height) == (400, 560)

    def test_a_small_image_runs_at_full_resolution(self):
        prepared = prepare_for_detection(page(400, 560), config=CONFIG)
        assert prepared.scale == 1.0
        assert prepared.to_source_factor == 1.0
        assert (prepared.working_width, prepared.working_height) == (400, 560)

    def test_a_large_image_is_downscaled_for_detection(self):
        config = PreprocessingConfig(working_max_dimension_px=200)
        prepared = prepare_for_detection(page(400, 560), config=config)
        assert prepared.working_height == 200
        assert prepared.scale < 1.0

    def test_the_reported_scale_matches_the_realised_size(self):
        # Rounding to whole pixels means the achieved scale differs from the
        # requested one; mapping a marker centre back with the requested factor
        # would put it systematically off.
        config = PreprocessingConfig(working_max_dimension_px=333)
        prepared = prepare_for_detection(page(501, 701), config=config)
        assert prepared.scale == pytest.approx(prepared.working_width / 501)

    @pytest.mark.parametrize("channels", [None, 1, 3, 4])
    def test_every_supported_layout_produces_a_binary_image(self, channels):
        prepared = prepare_for_detection(page(channels=channels), config=CONFIG)
        assert prepared.binary.ndim == 2
        assert prepared.grayscale.ndim == 2
        assert prepared.binary[INK_SAMPLE] == 255

    def test_blurring_can_be_disabled(self):
        prepared = prepare_for_detection(page(), config=PreprocessingConfig(blur_kernel_px=0))
        assert prepared.binary[INK_SAMPLE] == 255
