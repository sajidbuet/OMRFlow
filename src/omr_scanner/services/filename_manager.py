"""Choosing an output file name for a processed scan, without ever overwriting.

Purpose:
    Decide what a recognised sheet's image should be called - normally the
    candidate's roll number - and guarantee that two sheets which recognise to
    the *same* roll number both survive.

Responsibilities:
    * :class:`FilenameAllocator` - hands out names, remembering everything it
      has already handed out and everything already present in the output
      directory.
    * :func:`duplicate_suffix` - the documented ``_a``, ``_b``, ... ``_z``,
      ``_aa`` sequence.
    * :func:`sanitise_stem` - keep a recognised value usable as a file name.

What does NOT belong here:
    * Copying or moving files. This module only decides names; the batch
      processor performs the side effect, which keeps the naming rule testable
      without touching a disk.
    * Deciding whether a roll number is trustworthy. The caller passes ``None``
      for an identifier it does not trust, and gets a review name back.

Why duplicates get a letter rather than a number:
    ``2103123_2.jpg`` is indistinguishable from a roll number that genuinely
    ends in ``_2`` and from a "copy 2" produced by a file manager. A letter
    suffix cannot be mistaken for part of an identifier, sorts next to the
    original, and makes "these three scans all claimed the same roll" obvious at
    a glance in a directory listing.

Why an existing file counts as a duplicate:
    A second run over the same folder, or a second batch appended to an earlier
    one, must not silently replace the first run's output. The allocator
    therefore consults the output directory itself, not only the names it issued
    during this session.
"""

from __future__ import annotations

import re
from pathlib import Path

DUPLICATE_SEPARATOR = "_"
"""Separator between a stem and its duplicate suffix (``2103123_a``)."""

UNRESOLVED_PREFIX = "UNRESOLVED"
"""Prefix for a scan whose identifier could not be trusted.

Deliberately shouty and unmistakably not a roll number: these files are the ones
a human has to look at, and they must be impossible to confuse with a recognised
sheet when the folder is sorted by name."""

UNRESOLVED_NUMBER_WIDTH = 3
"""Digits in the unresolved counter (``UNRESOLVED_001``), so a folder of them
sorts in processing order rather than lexicographically by digit count."""

_UNSAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]")
"""Everything outside this set is replaced in a file name.

Recognised values are not always digits: an alphanumeric field may legitimately
contain a symbol the template declares, such as ``*`` or ``#``, and those are
either illegal in a file name or meaningful to a shell."""

_SAFE_REPLACEMENT = "-"


def duplicate_suffix(index: int) -> str:
    """Return the suffix for the ``index``-th file sharing one stem.

    Args:
        index: ``0`` for the first file, ``1`` for the second, and so on.

    Returns:
        ``""`` for the first, then ``"_a"``, ``"_b"`` ... ``"_z"``, ``"_aa"``,
        ``"_ab"`` and onwards. The letter part is bijective base-26, so the
        sequence never repeats however many duplicates appear.

    Raises:
        ValueError: ``index`` is negative.
    """
    if index < 0:
        raise ValueError("Duplicate index must be zero or positive")
    if index == 0:
        return ""

    letters: list[str] = []
    remaining = index
    while remaining > 0:
        remaining, position = divmod(remaining - 1, 26)
        letters.append(chr(ord("a") + position))
    return DUPLICATE_SEPARATOR + "".join(reversed(letters))


def sanitise_stem(value: str) -> str:
    """Return ``value`` reduced to characters that are safe in a file name.

    Args:
        value: A recognised field value, such as a roll number.

    Returns:
        The value with every character outside ``A-Z a-z 0-9 . _ -`` replaced by
        ``-``, and surrounding whitespace and dots removed. An empty result -
        a value that was nothing but unsafe characters - is returned as ``""``
        so the caller can fall back to a review name.
    """
    cleaned = _UNSAFE_CHARACTERS.sub(_SAFE_REPLACEMENT, value.strip())
    return cleaned.strip(". ")


def normalise_suffix(suffix: str) -> str:
    """Return ``suffix`` with a leading dot, or ``""`` when there is no suffix."""
    if not suffix:
        return ""
    return suffix if suffix.startswith(".") else f".{suffix}"


class FilenameAllocator:
    """Issues output file names that never collide.

    One allocator serves one output directory for one batch. It remembers every
    name it has issued, so two scans processed in the same run cannot be given
    the same name even before either file has been written.

    Args:
        output_dir: Directory the files will be written to. When given, names
            already present there are treated as taken. ``None`` allocates names
            in the abstract, which is what the unit tests and the GUI's
            "what would this be called" preview use.
        case_sensitive: Whether two names differing only in case are distinct.
            Defaults to ``False``, matching Windows and macOS: on those systems
            ``2103123.JPG`` *would* overwrite ``2103123.jpg``, so the allocator
            must treat them as the same name even though Linux would not.

    Attributes:
        output_dir: The directory passed in, or ``None``.
    """

    def __init__(self, output_dir: Path | None = None, *, case_sensitive: bool = False) -> None:
        self.output_dir = output_dir
        self._case_sensitive = case_sensitive
        self._taken: set[str] = set()
        self._unresolved_count = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _key(self, name: str) -> str:
        return name if self._case_sensitive else name.lower()

    def _is_free(self, name: str) -> bool:
        """Whether ``name`` is unused, both in this session and on disk."""
        if self._key(name) in self._taken:
            return False
        if self.output_dir is None:
            return True
        if self._case_sensitive:
            return not (self.output_dir / name).exists()
        # A case-insensitive file system reports `2103123.JPG` as existing when
        # asked about `2103123.jpg`, but a case-sensitive one does not; listing
        # the directory is the only way to be right on both.
        try:
            existing = {entry.name.lower() for entry in self.output_dir.iterdir()}
        except (OSError, FileNotFoundError):
            return True
        return name.lower() not in existing

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def reserve(self, name: str) -> None:
        """Record ``name`` as taken without issuing it.

        Used when a caller writes a file the allocator did not name, so that
        later allocations still avoid it.
        """
        self._taken.add(self._key(name))

    def allocate(self, stem: str | None, suffix: str) -> str:
        """Return an unused file name for one scan.

        Args:
            stem: The recognised identifier to name the file after, or ``None``
                when it could not be trusted. An empty or wholly unsafe stem is
                treated exactly like ``None``: a wrong-looking file name is
                worse than an obviously-for-review one.
            suffix: The file extension to keep, with or without its dot. The
                source image's own extension is preserved so that a JPEG stays a
                JPEG and nothing is silently re-encoded.

        Returns:
            A file name that is not currently taken, and which is immediately
            recorded as taken.
        """
        extension = normalise_suffix(suffix)
        safe = sanitise_stem(stem) if stem is not None else ""
        if not safe:
            return self._allocate_unresolved(extension)

        index = 0
        while True:
            candidate = f"{safe}{duplicate_suffix(index)}{extension}"
            if self._is_free(candidate):
                self.reserve(candidate)
                return candidate
            index += 1

    def _allocate_unresolved(self, extension: str) -> str:
        """Return the next free ``UNRESOLVED_nnn`` name."""
        while True:
            self._unresolved_count += 1
            candidate = (
                f"{UNRESOLVED_PREFIX}{DUPLICATE_SEPARATOR}"
                f"{self._unresolved_count:0{UNRESOLVED_NUMBER_WIDTH}d}{extension}"
            )
            if self._is_free(candidate):
                self.reserve(candidate)
                return candidate


__all__ = [
    "DUPLICATE_SEPARATOR",
    "UNRESOLVED_NUMBER_WIDTH",
    "UNRESOLVED_PREFIX",
    "FilenameAllocator",
    "duplicate_suffix",
    "normalise_suffix",
    "sanitise_stem",
]
