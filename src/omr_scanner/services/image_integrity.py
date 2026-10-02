"""Is this the complete image? A check on the bytes of one read (0.1.1 revised phase 5).

Purpose:
    Decide whether bytes read from an intake file are a **complete** image
    OMRFlow may hand to recognition - not just a valid header. A scanner that
    writes the header first and the pixels afterwards produces, for a while, a
    file that "looks like" a JPEG; it must never become ``ready``.

Responsibilities:
    * :func:`check_image_bytes` - structural completeness for the formats
      :data:`~omr_scanner.services.scan_import.SUPPORTED_SCAN_SUFFIXES` names,
      the TIFF page count, and a full decode through
      :func:`~omr_scanner.services.alignment_service.decode_scan_bytes`, the
      same call recognition makes.

What does NOT belong here:
    * Reading the file, hashing it or re-checking its metadata: the caller does
      that around **one** read, and passes the bytes in, so the hash and the
      decode describe the same bytes (:mod:`omr_scanner.services.intake`).
    * Scan-quality judgements (blur, skew, contrast). Phase 7.

Why a structural check as well as decoding:
    OpenCV 5's decoders reject every truncation probed (2 bytes to 90 % cut),
    but ``pyproject`` allows OpenCV >= 4.9, whose libjpeg returns a grey-padded
    image for a truncated JPEG with only a warning. The structural checks make
    completeness independent of that: a JPEG must end with its EOI marker, a
    PNG must walk chunk by chunk to ``IEND``, a TIFF's first page's strips or
    tiles must lie inside the file, a BMP's pixel array must fit.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import cv2
import numpy as np

from omr_scanner.domain.intake import IntakeReason
from omr_scanner.services.alignment_service import decode_scan_bytes

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TIFF_LE = b"II*\x00"
_TIFF_BE = b"MM\x00*"
_MAX_TIFF_PAGES = 4096
"""Loop guard for a malformed IFD chain."""


@dataclass(frozen=True, slots=True)
class ImageCheck:
    """The verdict on one read's bytes.

    Attributes:
        ok: Complete, single-page and decodable.
        reason: When not ``ok``: :attr:`IntakeReason.DECODE_FAILED` (truncated,
            corrupt, not an image - may be retried while a writer could still
            be finishing it) or :attr:`IntakeReason.MULTIPAGE_TIFF` (complete,
            refused in this release).
        detail: One plain sentence for the ledger.
        image_format: ``jpeg``, ``png``, ``tiff``, ``bmp`` or ``""``.
        width: Decoded width in pixels, 0 when not decoded.
        height: Decoded height in pixels.
        page_count: Pages found (TIFF), 1 otherwise, 0 when unknown.
    """

    ok: bool
    reason: IntakeReason = IntakeReason.NONE
    detail: str = ""
    image_format: str = ""
    width: int = 0
    height: int = 0
    page_count: int = 0


def sniff_format(data: bytes) -> str:
    """The container format from its magic bytes, or ``""``."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    if data.startswith((_TIFF_LE, _TIFF_BE)):
        return "tiff"
    if data.startswith(b"BM"):
        return "bmp"
    return ""


def _failed(detail: str, image_format: str = "", pages: int = 0) -> ImageCheck:
    return ImageCheck(
        ok=False,
        reason=IntakeReason.DECODE_FAILED,
        detail=detail,
        image_format=image_format,
        page_count=pages,
    )


def _jpeg_complete(data: bytes) -> str:
    """``""`` when the JPEG ends with EOI (trailing zero padding allowed)."""
    end = data.rstrip(b"\x00")
    if not end.endswith(b"\xff\xd9"):
        return "the JPEG has no end-of-image marker (truncated or still being written)"
    return ""


def _png_complete(data: bytes) -> str:
    """``""`` when every chunk fits and the stream reaches ``IEND``."""
    offset = len(_PNG_SIGNATURE)
    total = len(data)
    while offset + 8 <= total:
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > total:
            return f"the PNG's {kind.decode('latin-1')} chunk is cut short (truncated)"
        if kind == b"IEND":
            return ""
        offset = end
    return "the PNG never reaches its IEND chunk (truncated or still being written)"


def _tiff_pages_and_extent(data: bytes) -> tuple[int, str]:
    """Page count and ``""`` when page 1's image data lies inside the file."""
    endian = "<" if data.startswith(_TIFF_LE) else ">"
    total = len(data)
    if total < 8:
        return 0, "the TIFF header is incomplete"
    (first,) = struct.unpack(endian + "I", data[4:8])
    offsets: list[int] = []
    seen: set[int] = set()
    position = first
    problem = ""
    pages = 0
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 6: 1, 7: 1, 8: 2, 9: 4, 11: 4, 12: 8, 16: 8, 17: 8, 18: 8}
    while position and pages < _MAX_TIFF_PAGES:
        if position in seen or position + 2 > total:
            return pages, problem or "the TIFF directory chain points outside the file"
        seen.add(position)
        (count,) = struct.unpack(endian + "H", data[position : position + 2])
        entries_end = position + 2 + 12 * count
        if entries_end + 4 > total:
            return pages, "a TIFF directory is cut short (truncated)"
        if pages == 0:
            tags: dict[int, list[int]] = {}
            for index in range(count):
                start = position + 2 + 12 * index
                tag, kind, number = struct.unpack(endian + "HHI", data[start : start + 8])
                if tag not in (273, 279, 324, 325) or kind not in (3, 4):
                    continue
                width = sizes[kind]
                code = endian + ("H" if kind == 3 else "I")
                if width * number <= 4:
                    raw = data[start + 8 : start + 8 + width * number]
                else:
                    (pointer,) = struct.unpack(endian + "I", data[start + 8 : start + 12])
                    if pointer + width * number > total:
                        return 1, "a TIFF strip table lies outside the file (truncated)"
                    raw = data[pointer : pointer + width * number]
                tags[tag] = [
                    struct.unpack(code, raw[k * width : (k + 1) * width])[0] for k in range(number)
                ]
            pairs = [(273, 279), (324, 325)]
            for offset_tag, count_tag in pairs:
                if offset_tag in tags and count_tag in tags:
                    offsets = [
                        start + length
                        for start, length in zip(tags[offset_tag], tags[count_tag], strict=False)
                    ]
            if offsets and max(offsets) > total:
                problem = "the TIFF's image data runs past the end of the file (truncated)"
        pages += 1
        (position,) = struct.unpack(endian + "I", data[entries_end : entries_end + 4])
    return pages, problem


def _bmp_complete(data: bytes) -> str:
    """``""`` when an uncompressed BMP's pixel array fits."""
    if len(data) < 30:
        return "the BMP header is incomplete"
    (pixel_offset,) = struct.unpack("<I", data[10:14])
    header = struct.unpack("<I", data[14:18])[0]
    if header < 40 or len(data) < 34:
        return ""
    width, height, _planes, bits, compression = struct.unpack("<iiHHI", data[18:34])
    if compression not in (0, 3):
        return ""
    row = ((bits * abs(width) + 31) // 32) * 4
    if pixel_offset + row * abs(height) > len(data):
        return "the BMP's pixel data is cut short (truncated)"
    return ""


def check_image_bytes(data: bytes) -> ImageCheck:
    """Decide whether ``data`` is one complete, decodable, single-page image.

    Args:
        data: Every byte of the file, from one read.

    Returns:
        The verdict. An empty buffer, an unknown container, a structural
        truncation or a failed decode are ``decode_failed``; a TIFF of more
        than one page is ``multipage_tiff`` even when page 1 decodes.
    """
    if not data:
        return _failed("the file is empty")
    image_format = sniff_format(data)
    if not image_format:
        return _failed("the file is not a JPEG, PNG, TIFF or BMP image")
    pages = 1
    problem = ""
    if image_format == "jpeg":
        problem = _jpeg_complete(data)
    elif image_format == "png":
        problem = _png_complete(data)
    elif image_format == "tiff":
        pages, problem = _tiff_pages_and_extent(data)
    else:
        problem = _bmp_complete(data)
    if problem:
        return _failed(problem, image_format, pages)
    if image_format == "tiff" and pages > 1:
        return ImageCheck(
            ok=False,
            reason=IntakeReason.MULTIPAGE_TIFF,
            detail=(
                f"a {pages}-page TIFF; multi-page TIFF is not supported in this "
                "release - scan one page per file"
            ),
            image_format=image_format,
            page_count=pages,
        )
    try:
        decoded = decode_scan_bytes(np.frombuffer(data, dtype=np.uint8))
    except cv2.error as exc:
        return _failed(f"the image could not be decoded: {exc}", image_format, pages)
    if decoded is None or decoded.size == 0:
        return _failed(
            "the image could not be decoded (corrupt or incomplete)", image_format, pages
        )
    height, width = decoded.shape[:2]
    return ImageCheck(
        ok=True,
        image_format=image_format,
        width=int(width),
        height=int(height),
        page_count=pages,
    )


__all__ = ["ImageCheck", "check_image_bytes", "sniff_format"]
