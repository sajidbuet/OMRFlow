"""Prevent two OMRFlow processes from writing to the same project (Phase 10, §3).

Purpose:
    Make "two people opened the same project folder" (a shared drive, a
    second terminal, a crashed instance restarted by mistake) into a clear
    message instead of two SQLite connections quietly racing each other.

Responsibilities:
    * :func:`acquire` - claim the lock, or report who already holds it.
    * :func:`force_acquire` - the explicit-operator-decision path: remove a
      lock this process has determined is stale, and claim it.
    * :class:`ProjectLock` - the held lock; releases itself, once, on close.

What does NOT belong here:
    * Deciding what the GUI shows for a conflict. This module only ever
      returns data (:class:`LockHolder`, :attr:`LockHolder.likely_stale`);
      the choice of "Cancel / Open read-only / Remove stale lock" is
      `gui.project_health`'s to present, per the phase brief's explicit
      instruction never to auto-remove a lock without an operator decision.
    * Qt. A lock file is plain text; nothing here imports PySide6, so the
      service is usable from a script or a test with no display.

Why a plain lock file rather than ``QLockFile``:
    ``QLockFile`` would pull Qt into a service module the architecture rule
    (``docs/ARCHITECTURE.md``) forbids, for no capability this does not
    already have: PID + hostname + a liveness check
    (:func:`psutil.pid_exists`) is exactly what ``QLockFile`` itself does
    under Windows and POSIX. The GUI layer may still reach for ``QLockFile``
    directly if a future need arises; today one honest, tested,
    Qt-independent implementation is better than two.

Staleness, and why this module never removes a lock on its own:
    A lock is *likely* stale when its recorded host matches this machine and
    its recorded process id is not running - conclusive evidence that
    process is gone. It is never *certainly* safe to remove automatically:
    the phase brief is explicit that a crash must not be treated as licence
    to silently reclaim the lock, because the same evidence (an unreachable
    PID) is also what a suspended, swapped-out or debugger-paused process
    would show. :func:`acquire` therefore always raises when a lock file
    exists, carrying whether it looks stale; only :func:`force_acquire` -
    called after a human has been shown that judgement and agreed - removes
    one.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

from omr_scanner import __version__
from omr_scanner.errors import OMRScannerError

_LOGGER = logging.getLogger(__name__)

LOCK_FILE_NAME = ".omrflow.lock"
"""Name of the lock file inside a project's root directory. Leading dot so it
reads, to any file browser, as an application artifact rather than project
data."""


class ProjectLockError(OMRScannerError):
    """Base class for project-locking failures."""


@dataclass(frozen=True, slots=True)
class LockHolder:
    """Who currently holds a project's lock, as recorded in the lock file.

    Attributes:
        pid: Process id of the holder, on its own machine.
        hostname: The machine that created the lock.
        app_version: OMRFlow version that created it.
        acquired_at: When it was created (UTC, ISO 8601).
        likely_stale: ``True`` when this lock was created on *this* machine
            and no process with :attr:`pid` is currently running - meaning
            whatever created it did not shut down cleanly. ``False`` on any
            other machine (this process has no way to check a PID on a
            different computer, so it must not guess) and ``False`` when the
            recorded process is genuinely still running.
    """

    pid: int
    hostname: str
    app_version: str
    acquired_at: str
    likely_stale: bool

    def describe(self) -> str:
        """One sentence for a dialog, naming what is known."""
        if self.hostname == socket.gethostname():
            return (
                f"Locked by OMRFlow (process {self.pid}) on this machine, "
                f"since {self.acquired_at}."
            )
        return (
            f"Locked by OMRFlow (process {self.pid}) on '{self.hostname}', "
            f"since {self.acquired_at}."
        )


class ProjectLockHeldError(ProjectLockError):
    """Another OMRFlow process appears to hold this project's lock.

    Attributes:
        holder: Who holds it, and whether it looks stale.
    """

    def __init__(self, holder: LockHolder, *, project_dir: Path) -> None:
        self.holder = holder
        message = f"Project locked: {holder.describe()}"
        super().__init__(
            message,
            user_message=(
                f"This project is already open elsewhere. {holder.describe()} "
                "Choose to cancel, open the project read-only, or remove the "
                "lock if you are certain no other OMRFlow process is using it."
            ),
        )
        self.project_dir = project_dir


def _lock_path(project_dir: Path) -> Path:
    return project_dir / LOCK_FILE_NAME


def _read_holder(path: Path) -> LockHolder | None:
    """Read and validate a lock file, treating anything unreadable as absent.

    A lock file that cannot be parsed (truncated by a crash mid-write, or
    from a build old enough to have written a different format) is *not*
    proof that no other process is using the project - but it also cannot be
    trusted to name a holder. It is reported as an unreadable, definitely
    stale-looking lock (pid ``-1``) rather than raising, so opening the
    project is never blocked by a corrupt one-line file.
    """
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        hostname = str(payload["hostname"])
        app_version = str(payload["app_version"])
        acquired_at = str(payload["acquired_at"])
    except (OSError, ValueError, KeyError, TypeError):
        _LOGGER.warning("Lock file %s could not be parsed; treating it as stale", path)
        return LockHolder(
            pid=-1,
            hostname="",
            app_version="",
            acquired_at="",
            likely_stale=True,
        )
    likely_stale = hostname == socket.gethostname() and not psutil.pid_exists(pid)
    return LockHolder(
        pid=pid,
        hostname=hostname,
        app_version=app_version,
        acquired_at=acquired_at,
        likely_stale=likely_stale,
    )


def _write_lock_file(path: Path) -> None:
    """Create the lock file, atomically and exclusively.

    ``O_EXCL`` makes creation itself the race-free check: two processes that
    both find no lock file and both try to create one cannot both succeed -
    the loser gets ``FileExistsError``, converted to :class:`ProjectLockHeldError`
    by the caller.
    """
    payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "app_version": __version__,
        "acquired_at": datetime.now(UTC).isoformat(),
    }
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class ProjectLock:
    """A held project lock. Release it, once, when the project closes.

    Args:
        path: The lock file this instance owns.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._released = False

    @property
    def path(self) -> Path:
        """Where the lock file lives."""
        return self._path

    def release(self) -> None:
        """Remove the lock file. Safe to call more than once."""
        if self._released:
            return
        self._released = True
        self._path.unlink(missing_ok=True)

    def __enter__(self) -> ProjectLock:
        """Enter a context manager that releases the lock on exit."""
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Release the lock when leaving the context."""
        self.release()


def acquire(project_dir: Path) -> ProjectLock:
    """Claim the project's lock, or report who already holds it.

    Args:
        project_dir: The project's root directory. Must already exist.

    Returns:
        The held lock.

    Raises:
        ProjectLockHeldError: A lock file already exists. Its
            :attr:`~ProjectLockHeldError.holder` says whether it looks stale;
            the caller decides what to offer - never this function.
    """
    path = _lock_path(project_dir)
    existing = _read_holder(path) if path.is_file() else None
    if existing is not None:
        raise ProjectLockHeldError(existing, project_dir=project_dir)
    try:
        _write_lock_file(path)
    except FileExistsError as exc:
        # Lost a race against another process between the check above and
        # this call - exactly what O_EXCL exists to catch.
        holder = _read_holder(path) or LockHolder(
            pid=-1, hostname="", app_version="", acquired_at="", likely_stale=True
        )
        raise ProjectLockHeldError(holder, project_dir=project_dir) from exc
    return ProjectLock(path)


def force_acquire(project_dir: Path) -> ProjectLock:
    """Remove an existing lock file and claim a fresh one.

    This is the explicit-operator-decision path the phase brief requires:
    call it only after a human has been shown the existing lock's
    :attr:`~LockHolder.describe` and :attr:`~LockHolder.likely_stale`
    judgement and has chosen to proceed anyway. It removes whatever lock file
    is present unconditionally - it does not re-check staleness, because that
    check already happened, in the caller, in front of the person deciding.

    Args:
        project_dir: The project's root directory.

    Returns:
        The newly held lock.
    """
    _lock_path(project_dir).unlink(missing_ok=True)
    return acquire(project_dir)


__all__ = [
    "LOCK_FILE_NAME",
    "LockHolder",
    "ProjectLock",
    "ProjectLockError",
    "ProjectLockHeldError",
    "acquire",
    "force_acquire",
]
