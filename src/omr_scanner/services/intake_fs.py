"""The filesystem boundary of intake: list a source, read a file once, copy it safely.

Purpose:
    Everything :mod:`omr_scanner.services.intake` needs from a disk or a share,
    behind a small protocol, so the stabilisation state machine can be tested
    with a fake filesystem and a fake clock - no sleeping, no scanner - and the
    real implementation stays one short, auditable module.

Responsibilities:
    * :class:`IntakeFileSystem` - the protocol: :meth:`~IntakeFileSystem.list_source`
      and :meth:`~IntakeFileSystem.read_snapshot`.
    * :class:`OsFileSystem` - ``os.scandir`` listing (UNC and long-path aware),
      one-read snapshots with before/after metadata, Windows sharing-violation
      classification.
    * :class:`IngestStore` - the project's content-addressed copy store:
      temporary name, write, flush + fsync, re-read and verify the hash, atomic
      rename; never overwrites a different file silently.

What does NOT belong here:
    * Any state decision (ready, vanished, unreadable): the service decides.
    * Writing to a source. **Sources are read-only inputs**: nothing here opens a
      source file for writing, renames, truncates or deletes it.
    * Qt (``QFileSystemWatcher`` is excluded on purpose - reconciliation is the
      source of truth, ARCHITECTURE_NOTES §10.6).
"""

from __future__ import annotations

import errno
import os
import secrets
import stat as stat_module
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol

from omr_scanner.domain.intake import (
    Exclusions,
    IntakeReason,
    ObservedFile,
    Reachability,
    SourceListing,
    is_hidden_folder,
)
from omr_scanner.services import scan_provenance

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

_SHARING_VIOLATIONS = frozenset({32, 33})
"""``ERROR_SHARING_VIOLATION`` and ``ERROR_LOCK_VIOLATION``: another process
holds the file open without read sharing. Not yet - not an error."""

_UNREACHABLE_WINERRORS = frozenset({2, 3, 21, 53, 59, 64, 67, 121, 1219, 1231, 1232, 1326})
"""Path / network-name not found, device not ready, network name deleted,
semaphore timeout, network unreachable, credential conflicts."""

_LONG_PATH = 240
"""Above this length a Windows path is used in extended (``\\\\?\\``) form."""


class SourceListingError(Exception):
    """The source's root folder could not be listed.

    Attributes:
        reachability: :attr:`Reachability.UNREACHABLE` or
            :attr:`Reachability.PERMISSION_DENIED`.
        detail: One plain sentence, kept on the source row.
    """

    def __init__(self, reachability: Reachability, detail: str) -> None:
        super().__init__(detail)
        self.reachability = reachability
        self.detail = detail


@dataclass(frozen=True, slots=True)
class FileMeta:
    """Size and modification time at one instant."""

    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class ReadSnapshot:
    """The outcome of reading one file once.

    Attributes:
        data: Every byte read, or ``None`` when the read did not happen.
        before: Metadata of the open handle before reading.
        after: Metadata of the open handle after reading.
        path_after: Metadata of the *path* after the handle closed - catches a
            replacement by rename during the read.
        failure: Why there are no bytes: ``locked``, ``access_denied``,
            ``read_error``, or ``disappeared`` (the file is gone).
        detail: The error text, for the ledger.
    """

    data: bytes | None
    before: FileMeta | None = None
    after: FileMeta | None = None
    path_after: FileMeta | None = None
    failure: IntakeReason = IntakeReason.NONE
    detail: str = ""


class IntakeFileSystem(Protocol):
    """What intake needs from a filesystem.

    Implemented by :class:`OsFileSystem` and by the tests' fake.
    """

    def list_source(
        self, root: str, *, recursive: bool, exclusions: Exclusions
    ) -> SourceListing:
        """List every file under ``root`` (and its sub-folders when ``recursive``).

        Raises:
            SourceListingError: The root itself could not be listed.
        """
        ...

    def read_snapshot(self, path: str) -> ReadSnapshot:
        """Read the whole file once, with metadata before, after and of the path."""
        ...


def extended_path(path: str) -> str:
    r"""The ``\\?\`` form of a long Windows path (``\\?\UNC\`` for a share).

    Only the path handed to the operating system changes; the path recorded as
    provenance is always the one observed.
    """
    if sys.platform != "win32" or len(path) < _LONG_PATH or path.startswith("\\\\?\\"):
        return path
    # Not Path.resolve(): resolving follows links and may touch the share; only
    # the lexical absolute form is wanted here.
    absolute = os.path.abspath(path)  # noqa: PTH100
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _join(root: str, relative: str) -> str:
    """``root`` + a ``/``-relative path, in the platform's separator."""
    if not relative:
        return root
    separator = os.sep
    base = root.rstrip("/\\")
    return base + separator + relative.replace("/", separator)


def classify_listing_error(exc: OSError) -> Reachability:
    """Unreachable or permission-denied, from a listing failure."""
    winerror = getattr(exc, "winerror", None)
    if isinstance(exc, PermissionError) and winerror not in _UNREACHABLE_WINERRORS:
        return Reachability.PERMISSION_DENIED
    return Reachability.UNREACHABLE


def classify_open_error(exc: OSError, path: str = "") -> IntakeReason:
    """Why a file could not be opened for reading.

    On Windows the C runtime behind :func:`open` reports a sharing violation
    as a plain ``PermissionError`` (``EACCES``) with no ``winerror`` - found by
    a real exclusive-handle test, not by the fake filesystem - so a permission
    error there is re-probed with ``CreateFileW`` to tell *locked* (another
    program is still writing) from *access denied*.
    """
    winerror = getattr(exc, "winerror", None)
    if winerror in _SHARING_VIOLATIONS:
        return IntakeReason.LOCKED
    if isinstance(exc, FileNotFoundError):
        return IntakeReason.DISAPPEARED
    if isinstance(exc, PermissionError):
        if winerror is None and path and _windows_open_error(path) in _SHARING_VIOLATIONS:
            return IntakeReason.LOCKED
        return IntakeReason.ACCESS_DENIED
    return IntakeReason.READ_ERROR


def _windows_open_error(path: str) -> int | None:
    """The Win32 error opening ``path`` for shared reading gives, or ``None``.

    Read access with every share flag - the most permissive open there is - so
    a failure is the other program's sharing mode, or the file's ACL. The
    handle, if one is obtained, is closed at once; nothing is read or written.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    generic_read, share_all, open_existing = 0x80000000, 0x00000007, 3
    handle = kernel32.CreateFileW(path, generic_read, share_all, None, open_existing, 0, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        return int(ctypes.get_last_error())
    kernel32.CloseHandle(handle)
    return None


def _meta(result: os.stat_result) -> FileMeta:
    return FileMeta(size=int(result.st_size), mtime_ns=int(result.st_mtime_ns))


class OsFileSystem:
    """The real filesystem: ``os.scandir`` listing, one-read snapshots."""

    def list_source(
        self, root: str, *, recursive: bool, exclusions: Exclusions
    ) -> SourceListing:
        """List a source folder.

        The root failing to list raises :class:`SourceListingError`; a
        sub-folder failing is recorded in ``unlisted_folders`` and the rest of
        the listing is kept. Excluded and dot-folders are not descended into.
        Symbolic links and junctions to folders are not followed (a loop, or a
        folder outside the source, must not be imported by accident).
        """
        files: list[ObservedFile] = []
        unlisted: list[str] = []
        skipped: list[str] = []
        pending: list[str] = [""]
        first = True
        while pending:
            relative_folder = pending.pop()
            folder = _join(root, relative_folder)
            try:
                with os.scandir(extended_path(folder)) as entries:
                    found = list(entries)
            except OSError as exc:
                if first:
                    raise SourceListingError(
                        classify_listing_error(exc),
                        f"The folder '{root}' could not be listed: {exc.strerror or exc}",
                    ) from exc
                unlisted.append(relative_folder)
                continue
            first = False
            for entry in found:
                relative = f"{relative_folder}/{entry.name}" if relative_folder else entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not recursive:
                            continue
                        if is_hidden_folder(entry.name) or exclusions.excludes_folder(relative):
                            skipped.append(relative)
                        else:
                            pending.append(relative)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    # Gone between the listing and the stat: not observed.
                    continue
                if stat_module.S_ISREG(info.st_mode):
                    files.append(
                        ObservedFile(
                            relative_path=relative,
                            absolute_path=_join(root, relative),
                            size=int(info.st_size),
                            mtime_ns=int(info.st_mtime_ns),
                        )
                    )
        return SourceListing(
            files=tuple(files), unlisted_folders=tuple(unlisted), skipped_folders=tuple(skipped)
        )

    def read_snapshot(self, path: str) -> ReadSnapshot:
        """Read ``path`` once, read-only, and report what it looked like around the read."""
        target = extended_path(path)
        try:
            handle = open(target, "rb")  # noqa: PTH123, SIM115 - fstat on the handle
        except OSError as exc:
            return ReadSnapshot(
                data=None,
                failure=classify_open_error(exc, target),
                detail=str(exc.strerror or exc),
            )
        try:
            before = _meta(os.fstat(handle.fileno()))
            data = handle.read()
            after = _meta(os.fstat(handle.fileno()))
        except OSError as exc:
            return ReadSnapshot(
                data=None, failure=classify_open_error(exc), detail=str(exc.strerror or exc)
            )
        finally:
            handle.close()
        try:
            path_after: FileMeta | None = _meta(Path(target).stat())
        except FileNotFoundError:
            path_after = None
        except OSError as exc:
            return ReadSnapshot(
                data=None, failure=IntakeReason.READ_ERROR, detail=str(exc.strerror or exc)
            )
        return ReadSnapshot(data=data, before=before, after=after, path_after=path_after)


# ----------------------------------------------------------------------
# The project's copy store (ADR-0008)
# ----------------------------------------------------------------------
INGEST_FOLDER = "intake"
"""Under ``scans_original/``: ``scans_original/intake/<aa>/<sha256><ext>``."""

TEMP_MARKER = ".ingest-"
"""Temporary copies are ``<final>.ingest-<random>.part``; only these are ever
deleted by recovery."""


class IngestError(Exception):
    """A copy could not be made and verified.

    Attributes:
        reason: :attr:`IntakeReason.SOURCE_CHANGED` when the bytes no longer
            match the verified hash; :attr:`IntakeReason.READ_ERROR` for a
            write or read failure (disk full, permission).
    """

    def __init__(self, reason: IntakeReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class IngestResult:
    """A verified project copy.

    Attributes:
        path: Absolute path of the copy.
        relative_path: The same, relative to the project root (``/``-separated).
        reused: An identical copy already existed (another file with the same
            bytes, or a crash after the rename and before the ledger commit).
    """

    path: Path
    relative_path: str
    reused: bool


def _sha256_of_file(path: Path) -> str:
    """The one hashing routine (:func:`~omr_scanner.services.scan_provenance.hash_file`)."""
    return scan_provenance.hash_file(path)


class IngestStore:
    """Content-addressed, verified copies inside the project.

    Args:
        project_root: The project folder.
        opener: Opens a temporary file for binary writing; injectable so a test
            can simulate a full disk or a corrupting writer.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        opener: Callable[[Path], BinaryIO] | None = None,
    ) -> None:
        self._project_root = project_root
        self._root = project_root / "scans_original" / INGEST_FOLDER
        self._opener = opener or (lambda path: path.open("xb"))

    @property
    def root(self) -> Path:
        """``<project>/scans_original/intake``."""
        return self._root

    def destination(self, sha256: str, suffix: str) -> Path:
        """Where the copy of these bytes lives."""
        return self._root / sha256[:2] / f"{sha256}{suffix.lower()}"

    def put(self, data: bytes, sha256: str, suffix: str) -> IngestResult:
        """Store ``data`` as a verified copy, or reuse an identical one.

        Steps, each safe to be interrupted:

        1. ``data`` must hash to ``sha256`` (the source's verified hash);
           otherwise :class:`IngestError` (``source_changed``) and nothing is
           written.
        2. An existing destination whose bytes hash to ``sha256`` is reused.
           One that does not is moved aside to ``<name>.mismatch-<n>`` (never
           deleted) before a verified copy takes its place.
        3. Write ``<dest>.ingest-<random>.part``, flush, ``fsync``, close.
        4. Re-read the temporary file and verify its hash; a mismatch deletes
           the temporary file and raises.
        5. ``os.replace`` it onto the destination - atomic within one volume.

        A crash before step 5 leaves only a ``.part`` file, which is never
        referenced and is removed by :meth:`clean_temporaries`; a crash after
        it leaves a correct copy that step 2 reuses.
        """
        if scan_provenance.hash_bytes(data) != sha256:
            raise IngestError(
                IntakeReason.SOURCE_CHANGED,
                "The file's bytes no longer match the hash it was verified with.",
            )
        final = self.destination(sha256, suffix)
        relative = final.relative_to(self._project_root).as_posix()
        if final.is_file():
            try:
                if _sha256_of_file(final) == sha256:
                    return IngestResult(path=final, relative_path=relative, reused=True)
            except OSError as exc:
                raise IngestError(
                    IntakeReason.READ_ERROR, f"The existing copy could not be read: {exc}"
                ) from exc
            self._move_aside(final)
        temporary = final.with_name(f"{final.name}{TEMP_MARKER}{secrets.token_hex(4)}.part")
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            with self._opener(temporary) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if _sha256_of_file(temporary) != sha256:
                raise IngestError(
                    IntakeReason.READ_ERROR,
                    "The project copy did not verify (its bytes differ from the source's).",
                )
            temporary.replace(final)
        except IngestError:
            self._discard(temporary)
            raise
        except OSError as exc:
            self._discard(temporary)
            full = exc.errno == errno.ENOSPC
            detail = "no space left on the project's disk" if full else str(exc)
            raise IngestError(
                IntakeReason.READ_ERROR, f"The project copy could not be written: {detail}"
            ) from exc
        return IngestResult(path=final, relative_path=relative, reused=False)

    def clean_temporaries(self) -> int:
        """Delete this store's own interrupted ``.part`` copies; return how many.

        Only files whose names carry :data:`TEMP_MARKER` inside the store's own
        folder - never a source file, never a finished copy.
        """
        if not self._root.is_dir():
            return 0
        removed = 0
        for candidate in self._root.rglob(f"*{TEMP_MARKER}*.part"):
            if self._discard(candidate):
                removed += 1
        return removed

    def _move_aside(self, path: Path) -> Path:
        counter = 1
        while True:
            target = path.with_name(f"{path.name}.mismatch-{counter}")
            if not target.exists():
                path.replace(target)
                return target
            counter += 1

    @staticmethod
    def _discard(path: Path) -> bool:
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True


__all__ = [
    "INGEST_FOLDER",
    "TEMP_MARKER",
    "FileMeta",
    "IngestError",
    "IngestResult",
    "IngestStore",
    "IntakeFileSystem",
    "OsFileSystem",
    "ReadSnapshot",
    "SourceListingError",
    "classify_listing_error",
    "classify_open_error",
    "extended_path",
]
