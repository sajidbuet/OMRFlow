"""Sheets whose *real* recognition produces each quality case (0.1.1 revised phase 7).

Built from the stress dataset's real synthetic sheets (:mod:`tests.engine_rig`)
with deterministic image edits, then read by the real recognition engine - no
result is fabricated. Measured with this repository's engine and the crash
harness template (``tests/crash/harness.py``):

=========================  ==========================================  =================
Sheet                      Real recognition                            Policy decision
=========================  ==========================================  =================
:func:`blank_page`         ``registration_failed`` (no markers)        RESCAN_REQUIRED,
                                                                       *registration*
:func:`displaced_id`       ``review``; geometry ``UNUSABLE``,          RESCAN_REQUIRED,
                           ``PAGE_GEOMETRY_DISTORTION`` on the          *folded*
                           identifier block
:func:`erased_id`          ``review``; geometry ``UNUSABLE``,          RESCAN_REQUIRED,
                           ``CRITICAL_REGION_UNREADABLE``               *id_unreadable*
=========================  ==========================================  =================

These are **simulated** defects (a block of the page moved by 5 % of its size,
or blanked), chosen because they are deterministic and genuinely exercise the
recognition engine's own geometry check. They are not photographs of folded
paper and say nothing about how real damage is classified - that is the
unvalidated part of the quality policy.
"""

from __future__ import annotations

import tempfile
from functools import cache
from pathlib import Path

import cv2
import numpy as np
from tests.crash.harness import template as harness_template
from tests.engine_rig import OPTIONS, readable_sheets

from omr_scanner.recognition.fields import choose_identifier_zone
from omr_scanner.services.recognition_service import recognise_scan


def _decode(data: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    assert image is not None
    return image


def _encode(image: np.ndarray, compression: int = 3) -> bytes:
    ok, buffer = cv2.imencode(".png", image, [cv2.IMWRITE_PNG_COMPRESSION, compression])
    assert ok
    return buffer.tobytes()


def _identifier_rect(image: np.ndarray) -> tuple[int, int, int, int]:
    height, width = image.shape[:2]
    bounds = choose_identifier_zone(harness_template()).bounds  # type: ignore[union-attr]
    return (
        int(bounds.x * width),
        int(bounds.y * height),
        int((bounds.x + bounds.width) * width),
        int((bounds.y + bounds.height) * height),
    )


@cache
def blank_page(variant: int = 0) -> bytes:
    """A white page of a sheet's size: registration fails (no markers)."""
    image = np.full_like(_decode(readable_sheets(12)[0]), 255)
    image[0, variant % image.shape[1]] = 254  # distinct bytes per variant
    return _encode(image)


@cache
def displaced_id(index: int = 0) -> bytes:
    """Sheet ``index`` with its identifier block displaced by 5 %: geometry UNUSABLE."""
    image = _decode(readable_sheets(48)[index])
    x0, y0, x1, y1 = _identifier_rect(image)
    region = image[y0:y1, x0:x1]
    shift = np.float32([[1, 0, int(0.05 * (x1 - x0))], [0, 1, int(0.05 * (y1 - y0))]])
    image[y0:y1, x0:x1] = cv2.warpAffine(region, shift, (x1 - x0, y1 - y0), borderValue=255)
    return _encode(image)


@cache
def erased_id(index: int = 0) -> bytes:
    """Sheet ``index`` with its identifier block blanked: CRITICAL_REGION_UNREADABLE."""
    image = _decode(readable_sheets(48)[index])
    x0, y0, x1, y1 = _identifier_rect(image)
    image[y0:y1, x0:x1] = 255
    return _encode(image)


@cache
def rescan_of(index: int) -> bytes:
    """A *rescan* of sheet ``index``: the same page, different bytes (another scan)."""
    return _encode(_decode(readable_sheets(48)[index]), compression=1)


@cache
def roll_of(data: bytes) -> str:
    """The Student ID real recognition reads from ``data``."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "sheet.png"
        path.write_bytes(data)
        result = recognise_scan(path, harness_template(), options=OPTIONS)
    return result.identifier_value


__all__ = ["blank_page", "displaced_id", "erased_id", "rescan_of", "roll_of"]
