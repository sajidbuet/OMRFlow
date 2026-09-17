"""Collecting scan files from what a user selected.

Purpose:
    Turn "these files" or "this folder" into an ordered list of images the
    pipeline can actually read, without surprising the user by silently
    including a spreadsheet or by listing ``scan10`` before ``scan2``.

Responsibilities:
    * :data:`SUPPORTED_SCAN_SUFFIXES` - the formats OMRFlow reads.
    * :func:`collect_scan_files` - expand files and folders into a sorted list.
    * :func:`natural_sort_key` - the ordering a human expects.

What does NOT belong here:
    * Reading or decoding the images. That is
      :mod:`omr_scanner.services.alignment_service`.
    * Any Qt file dialog; the GUI collects the selection and passes it here.

Why natural sorting:
    Scanners name their output ``scan1.jpg``, ``scan2.jpg`` ... ``scan10.jpg``,
    and plain lexicographic order puts ``scan10`` immediately after ``scan1``.
    That matters here beyond tidiness: batch order is the order results appear
    in, the order a reviewer walks through them, and - when two sheets carry the
    same roll number - the order that decides which one gets the plain name and
    which gets ``_a``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

SUPPORTED_SCAN_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
)
"""Image formats OMRFlow can decode.

Exactly the set OpenCV reads without an extra dependency. PDF is deliberately
absent: supporting it would mean adding a rasteriser to the runtime dependencies
for a format a scanner can always be asked to avoid, which
``development/ROADMAP.md`` defers rather than smuggles into Phase 3."""

_NUMBER_RUN = re.compile(r"(\d+)")


def natural_sort_key(path: Path) -> tuple[object, ...]:
    """Return a sort key that orders embedded numbers numerically.

    ``scan2.jpg`` sorts before ``scan10.jpg``; case is ignored so that
    ``IMG_2`` and ``img_10`` interleave the way a file manager shows them.

    Args:
        path: The file to derive a key for.

    Returns:
        A tuple of alternating text and integer parts, prefixed by the parent
        directory so that a multi-folder selection stays grouped by folder.
    """
    parts: list[object] = [str(path.parent).lower()]
    for chunk in _NUMBER_RUN.split(path.name.lower()):
        parts.append(int(chunk) if chunk.isdigit() else chunk)
    return tuple(parts)


def is_supported_scan(path: Path) -> bool:
    """Whether ``path`` looks like an image OMRFlow can read."""
    return path.suffix.lower() in SUPPORTED_SCAN_SUFFIXES


def collect_scan_files(
    selection: Iterable[Path], *, recursive: bool = False
) -> tuple[Path, ...]:
    """Expand a user's selection into an ordered list of scan files.

    Args:
        selection: Files and/or directories, in any order. A directory
            contributes the supported images it contains; an unsupported file
            is skipped rather than reported, because the user selected a folder
            and expects OMRFlow to pick out what it can read.
        recursive: Descend into sub-directories as well.

    Returns:
        Existing, supported image files, de-duplicated by resolved path and
        sorted with :func:`natural_sort_key`.
    """
    found: dict[Path, Path] = {}
    for entry in selection:
        if entry.is_dir():
            pattern = "**/*" if recursive else "*"
            for candidate in entry.glob(pattern):
                if candidate.is_file() and is_supported_scan(candidate):
                    found.setdefault(candidate.resolve(), candidate)
        elif entry.is_file() and is_supported_scan(entry):
            found.setdefault(entry.resolve(), entry)
    return tuple(sorted(found.values(), key=natural_sort_key))


__all__ = [
    "SUPPORTED_SCAN_SUFFIXES",
    "collect_scan_files",
    "is_supported_scan",
    "natural_sort_key",
]
