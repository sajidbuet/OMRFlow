"""Deterministic stand-ins for intake tests: a clock, a filesystem, image bytes.

The intake service takes its clock and filesystem by injection
(:mod:`omr_scanner.services.intake`), so its stabilisation state machine is
tested here without sleeping, without a scanner and without real file locks -
and the same scenarios are re-run on real temporary directories by the
integration tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import cv2
import numpy as np

from omr_scanner.domain.intake import (
    Exclusions,
    IntakeReason,
    ObservedFile,
    Reachability,
    SourceListing,
    is_hidden_folder,
)
from omr_scanner.services.intake_fs import FileMeta, ReadSnapshot, SourceListingError

EPOCH = datetime(2026, 10, 2, 9, 0, 0, tzinfo=UTC)


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime = EPOCH) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@dataclass
class FakeFile:
    data: bytes
    mtime_ns: int
    locked: bool = False
    denied: bool = False
    on_read: Callable[[], None] | None = None
    """Called after the bytes are read and before the post-read metadata is
    taken - a writer changing the file during the read."""


@dataclass
class FakeFileSystem:
    r"""An in-memory tree of roots -> relative paths -> files.

    Paths are ``root + "\" + relative`` with ``/`` turned into ``\``, as on
    Windows; UNC roots (``\\scanner-a\scans``) work the same way.
    """

    clock: FakeClock
    roots: dict[str, dict[str, FakeFile]] = field(default_factory=dict)
    unreachable: set[str] = field(default_factory=set)
    denied: set[str] = field(default_factory=set)
    unlistable_folders: dict[str, set[str]] = field(default_factory=dict)
    reads: list[str] = field(default_factory=list)
    writes_to_sources: int = 0
    """Never incremented by intake - the fake has no write API at all; kept to
    make the read-only-source assertion explicit in tests."""

    def add_root(self, root: str) -> str:
        self.roots.setdefault(root, {})
        return root

    def path(self, root: str, relative: str) -> str:
        return root.rstrip("\\/") + "\\" + relative.replace("/", "\\")

    def _tick(self) -> int:
        return int(self.clock.now.timestamp() * 1_000_000_000)

    def write(self, root: str, relative: str, data: bytes, *, mtime_ns: int | None = None) -> None:
        existing = self.roots[root].get(relative)
        stamp = mtime_ns if mtime_ns is not None else self._tick()
        if existing is None:
            self.roots[root][relative] = FakeFile(data=data, mtime_ns=stamp)
        else:
            existing.data = data
            existing.mtime_ns = stamp

    def append(self, root: str, relative: str, data: bytes) -> None:
        file = self.roots[root][relative]
        file.data += data
        file.mtime_ns = self._tick()

    def touch(self, root: str, relative: str) -> None:
        self.roots[root][relative].mtime_ns = self._tick() + 1

    def rename(self, root: str, old: str, new: str) -> None:
        self.roots[root][new] = self.roots[root].pop(old)

    def delete(self, root: str, relative: str) -> None:
        del self.roots[root][relative]

    def file(self, root: str, relative: str) -> FakeFile:
        return self.roots[root][relative]

    # --- IntakeFileSystem -------------------------------------------------
    def list_source(self, root: str, *, recursive: bool, exclusions: Exclusions) -> SourceListing:
        if root in self.unreachable or root not in self.roots:
            raise SourceListingError(Reachability.UNREACHABLE, f"'{root}' is not reachable")
        if root in self.denied:
            raise SourceListingError(Reachability.PERMISSION_DENIED, f"access to '{root}' denied")
        unlisted = self.unlistable_folders.get(root, set())
        files: list[ObservedFile] = []
        skipped: set[str] = set()
        failed: set[str] = set()
        for relative, item in sorted(self.roots[root].items()):
            folder = relative.rpartition("/")[0]
            if folder and not recursive:
                continue
            parts = folder.split("/") if folder else []
            prefixes = ["/".join(parts[: index + 1]) for index in range(len(parts))]
            blocked = False
            for prefix in prefixes:
                if prefix in unlisted:
                    failed.add(prefix)
                    blocked = True
                    break
                name = prefix.rpartition("/")[2]
                if is_hidden_folder(name) or exclusions.excludes_folder(prefix):
                    skipped.add(prefix)
                    blocked = True
                    break
            if blocked:
                continue
            files.append(
                ObservedFile(
                    relative_path=relative,
                    absolute_path=self.path(root, relative),
                    size=len(item.data),
                    mtime_ns=item.mtime_ns,
                )
            )
        return SourceListing(
            files=tuple(files),
            unlisted_folders=tuple(sorted(failed)),
            skipped_folders=tuple(sorted(skipped)),
        )

    def _find(self, path: str) -> tuple[str, str] | None:
        for root, entries in self.roots.items():
            prefix = root.rstrip("\\/") + "\\"
            if path.startswith(prefix):
                relative = path[len(prefix) :].replace("\\", "/")
                if relative in entries:
                    return root, relative
        return None

    def read_snapshot(self, path: str) -> ReadSnapshot:
        self.reads.append(path)
        found = self._find(path)
        if found is None:
            return ReadSnapshot(data=None, failure=IntakeReason.DISAPPEARED, detail="not found")
        root, relative = found
        if root in self.unreachable:
            return ReadSnapshot(data=None, failure=IntakeReason.READ_ERROR, detail="network")
        item = self.roots[root][relative]
        if item.locked:
            return ReadSnapshot(
                data=None, failure=IntakeReason.LOCKED, detail="sharing violation (WinError 32)"
            )
        if item.denied:
            return ReadSnapshot(data=None, failure=IntakeReason.ACCESS_DENIED, detail="denied")
        before = FileMeta(len(item.data), item.mtime_ns)
        data = bytes(item.data)
        if item.on_read is not None:
            hook, item.on_read = item.on_read, None
            hook()
        current = self._find(path)
        after_item = self.roots[root].get(relative) if current is not None else None
        after = FileMeta(len(after_item.data), after_item.mtime_ns) if after_item else before
        return ReadSnapshot(
            data=data, before=before, after=after, path_after=after if after_item else None
        )


# ----------------------------------------------------------------------
# Copy-store writers that fail (IngestStore's ``opener``)
# ----------------------------------------------------------------------
class _Writer:
    def __init__(self, path: Path) -> None:
        self._handle = path.open("xb")

    def __enter__(self) -> _Writer:
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()

    def write(self, data: bytes) -> int:
        return self._handle.write(data)

    def flush(self) -> None:
        self._handle.flush()

    def fileno(self) -> int:
        return self._handle.fileno()


class InterruptedWriter(_Writer):
    """Writes half the bytes, then fails like a dropped drive."""

    def write(self, data: bytes) -> int:
        self._handle.write(data[: len(data) // 2])
        raise OSError(5, "I/O error")


class CorruptingWriter(_Writer):
    """Writes every byte but flips the last one - a copy that must not verify."""

    def write(self, data: bytes) -> int:
        return self._handle.write(data[:-1] + bytes([data[-1] ^ 0xFF]))


class FullDiskWriter:
    """Cannot even create the temporary file."""

    def __init__(self, path: Path) -> None:
        raise OSError(28, "No space left on device")


# ----------------------------------------------------------------------
# Image bytes
# ----------------------------------------------------------------------
def _page(seed: int, size: tuple[int, int] = (160, 120)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full(size, 230, np.uint8)
    for _ in range(12):
        y, x = int(rng.integers(5, size[0] - 20)), int(rng.integers(5, size[1] - 20))
        image[y : y + 10, x : x + 10] = int(rng.integers(0, 120))
    return image


@cache
def jpeg(seed: int = 0) -> bytes:
    return cv2.imencode(".jpg", _page(seed))[1].tobytes()


@cache
def png(seed: int = 0) -> bytes:
    return cv2.imencode(".png", _page(seed))[1].tobytes()


@cache
def tiff(seed: int = 0) -> bytes:
    return cv2.imencode(".tif", _page(seed))[1].tobytes()


@cache
def multipage_tiff(pages: int = 2, seed: int = 0) -> bytes:
    ok, buffer = cv2.imencodemulti(".tif", [_page(seed + index) for index in range(pages)])
    assert ok
    return buffer.tobytes()


def header_only(data: bytes, keep: float = 0.5) -> bytes:
    """A valid header with the rest cut off - what a slow writer leaves mid-file."""
    return data[: max(16, int(len(data) * keep))]


def random_bytes(length: int = 4000, seed: int = 7) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, length, dtype=np.uint8).tobytes()
