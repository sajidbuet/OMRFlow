"""Complete-image checks on the bytes of one read (intake's readiness evidence).

A header is not an image: a truncated file must fail even where a lenient
decoder would return a grey-padded page.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from tests.intake_fakes import header_only, jpeg, multipage_tiff, png, random_bytes, tiff

from omr_scanner.domain.intake import IntakeReason
from omr_scanner.services.image_integrity import check_image_bytes, sniff_format


def bmp() -> bytes:
    return cv2.imencode(".bmp", np.full((50, 40), 200, np.uint8))[1].tobytes()


@pytest.mark.parametrize(
    "data,kind", [(jpeg(), "jpeg"), (png(), "png"), (tiff(), "tiff"), (bmp(), "bmp")]
)
def test_complete_images_pass(data, kind):
    check = check_image_bytes(data)
    assert check.ok, check.detail
    assert check.image_format == kind
    assert (check.width, check.height) == ((120, 160) if kind != "bmp" else (40, 50))
    assert check.page_count == 1


@pytest.mark.parametrize("make", [jpeg, png, tiff, bmp])
@pytest.mark.parametrize("keep", [0.1, 0.5, 0.9, 0.99])
def test_a_valid_header_with_a_truncated_body_fails(make, keep):
    data = make()
    check = check_image_bytes(header_only(data, keep))
    assert not check.ok
    assert check.reason is IntakeReason.DECODE_FAILED


@pytest.mark.parametrize("make", [jpeg, png])
def test_missing_only_the_last_bytes_fails(make):
    data = make()
    for cut in (1, 2, 3):
        assert not check_image_bytes(data[:-cut]).ok


def test_zero_bytes_fail():
    check = check_image_bytes(b"")
    assert not check.ok and check.reason is IntakeReason.DECODE_FAILED


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_bytes_fail(seed):
    assert not check_image_bytes(random_bytes(seed=seed)).ok


def test_a_jpeg_shell_without_pixels_fails_the_decode():
    assert not check_image_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9").ok


def test_trailing_zero_padding_after_eoi_is_accepted():
    assert check_image_bytes(jpeg() + b"\x00" * 32).ok


@pytest.mark.parametrize("pages", [2, 5])
def test_multipage_tiff_is_refused_even_though_page_one_decodes(pages):
    check = check_image_bytes(multipage_tiff(pages))
    assert not check.ok
    assert check.reason is IntakeReason.MULTIPAGE_TIFF
    assert check.page_count == pages
    assert "multi-page TIFF is not supported" in check.detail


def test_sniff():
    assert sniff_format(jpeg()) == "jpeg"
    assert sniff_format(png()) == "png"
    assert sniff_format(tiff()) == "tiff"
    assert sniff_format(b"hello") == ""


def test_a_png_named_jpg_is_judged_by_content():
    # Recognition decodes by content too; the suffix only gates admission.
    assert check_image_bytes(png()).ok
