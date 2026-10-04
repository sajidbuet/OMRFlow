"""One processing coordinator per project: the ownership lease (0.1.1 revised phase 7).

Purpose:
    Make ``ARCHITECTURE_NOTES.md`` §13.1 - **one OMRFlow coordinator per
    project** - an enforced invariant instead of a convention. The finite Scan
    stage's batch run and the continuous engine are both *coordinators*: each
    claims sheets (``queued`` / ``processing``), commits results and settles
    batch status, and the engine's restart recovery returns every stale claim
    of the project to ``pending``. Two of them on one project at once would
    reset each other's claims. Before revised phase 7 nothing prevented it
    (``PHASE_F_HANDOFF.md`` §26).

How:
    A **process-wide, in-memory lease** keyed by the project database file.
    A coordinator acquires it before it touches processing state and releases
    it when it has settled; a second coordinator - of either kind - gets a
    typed :class:`CoordinatorBusyError` naming the holder, which phase 8 turns
    into a message.

Why in-memory is the whole answer:
    Separate *processes* cannot both have a project open: the project lock
    (:mod:`omr_scanner.services.project_lock`) already refuses the second one,
    and reclaiming a dead process's lock is an explicit operator decision. So
    the only remaining way to get two coordinators is two of them in **one**
    process, which is exactly what a process-local lease rules out - with no
    file or database row that could outlive a crash. A process that dies takes
    its lease with it: nothing can be left permanently "owned".

Liveness without a stuck lease:
    A lease belongs to an *owner object* (the engine, the Scan page). The
    registry holds it weakly: an owner that no longer exists cannot be
    coordinating anything, so its lease is reclaimable. Owners also release
    explicitly - on normal completion, on shutdown, and when an exception
    escapes them (:meth:`CoordinatorLease.release` is idempotent).

What does NOT belong here:
    Qt, the database, threads of its own.
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types import TracebackType

    from omr_scanner.database.engine import ProjectDatabase

_LOGGER = logging.getLogger(__name__)


class CoordinatorKind(StrEnum):
    """Which kind of coordinator holds a project's processing state."""

    FINITE_SCAN = "finite_scan"
    """The Scan stage's *Process All* / *Resume* run."""

    CONTINUOUS_ENGINE = "continuous_engine"
    """The continuous-processing engine (revised phase 6)."""

    SESSION_FINISH = "session_finish"
    """*Finish scan session* run outside the engine: its final reconciliation
    writes the intake ledger, which is coordinator work (revised phase 7)."""

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            CoordinatorKind.FINITE_SCAN: "the Scan stage's batch run",
            CoordinatorKind.CONTINUOUS_ENGINE: "continuous processing",
            CoordinatorKind.SESSION_FINISH: "finishing the scan session",
        }[self]


@dataclass(frozen=True, slots=True)
class LeaseHolder:
    """Who holds a project's lease, for a message and for tests."""

    kind: CoordinatorKind
    owner: str
    acquired_at: datetime
    thread_name: str


class CoordinatorBusyError(OMRScannerError):
    """Another coordinator already processes this project.

    Attributes:
        holder: Who holds it.
        requested: The kind of coordinator that was refused.
    """

    def __init__(self, holder: LeaseHolder, requested: CoordinatorKind) -> None:
        super().__init__(
            f"Project is being processed by {holder.kind.value} ({holder.owner}); "
            f"{requested.value} refused",
            user_message=(
                f"This project is already being processed by {holder.kind.label}. "
                f"Stop it before starting {requested.label}."
            ),
        )
        self.holder = holder
        self.requested = requested


@dataclass
class _Entry:
    holder: LeaseHolder
    owner_ref: weakref.ReferenceType[object]
    token: object


_LOCK = threading.Lock()
_LEASES: dict[str, _Entry] = {}


def _key(database: ProjectDatabase) -> str:
    return os.path.normcase(str(database.path))


class CoordinatorLease:
    """A held lease. Release it exactly once when the coordinator has settled.

    Usable as a context manager: the lease is released when the block exits,
    normally or by an exception.
    """

    def __init__(self, key: str, holder: LeaseHolder, token: object) -> None:
        self._key = key
        self._holder = holder
        self._token = token
        self._released = False

    @property
    def holder(self) -> LeaseHolder:
        """Who holds it."""
        return self._holder

    @property
    def released(self) -> bool:
        """Whether :meth:`release` has run."""
        return self._released

    def release(self) -> None:
        """Give the project back. Idempotent; never releases somebody else's lease."""
        with _LOCK:
            if self._released:
                return
            self._released = True
            entry = _LEASES.get(self._key)
            if entry is not None and entry.token is self._token:
                del _LEASES[self._key]
        _LOGGER.debug("Coordinator lease released (%s)", self._holder.kind.value)

    def __enter__(self) -> CoordinatorLease:
        """Enter: the lease is already held."""
        return self

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Release, whatever happened in the block."""
        self.release()


def _live(entry: _Entry) -> bool:
    return entry.owner_ref() is not None


def acquire(
    database: ProjectDatabase,
    kind: CoordinatorKind,
    *,
    owner: object,
    label: str = "",
) -> CoordinatorLease:
    """Take the project's coordinator lease for ``owner``.

    Args:
        database: The project (its database file is the key).
        kind: What is asking.
        owner: The object that coordinates (held weakly - when it is gone,
            so is its claim on the project).
        label: Words for the holder in a busy message.

    Raises:
        CoordinatorBusyError: Another live owner holds the lease - of either
            kind, including another instance of the same kind.
    """
    key = _key(database)
    with _LOCK:
        entry = _LEASES.get(key)
        if entry is not None and not _live(entry):
            del _LEASES[key]
            entry = None
    if entry is not None:
        # An owner reachable only through a reference cycle cannot be running:
        # collect once before refusing (this is the contended path only).
        gc.collect()
        with _LOCK:
            entry = _LEASES.get(key)
            if entry is not None and not _live(entry):
                del _LEASES[key]
                entry = None
    with _LOCK:
        entry = _LEASES.get(key)
        if entry is not None:
            raise CoordinatorBusyError(entry.holder, kind)
        holder = LeaseHolder(
            kind=kind,
            owner=label or type(owner).__name__,
            acquired_at=datetime.now(UTC),
            thread_name=threading.current_thread().name,
        )
        token = object()
        _LEASES[key] = _Entry(holder=holder, owner_ref=weakref.ref(owner), token=token)
    _LOGGER.debug("Coordinator lease acquired (%s) for %s", kind.value, key)
    return CoordinatorLease(key, holder, token)


def holder_of(database: ProjectDatabase) -> LeaseHolder | None:
    """Who holds the project's lease now, or ``None`` (a live owner only)."""
    with _LOCK:
        entry = _LEASES.get(_key(database))
        if entry is None or not _live(entry):
            return None
        return entry.holder


__all__ = [
    "CoordinatorBusyError",
    "CoordinatorKind",
    "CoordinatorLease",
    "LeaseHolder",
    "acquire",
    "holder_of",
]
