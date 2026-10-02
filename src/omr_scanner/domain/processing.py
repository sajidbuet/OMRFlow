"""Continuous processing: the engine's vocabulary and unit rule (0.1.1 revised phase 6).

Purpose:
    Name the states, limits and scheduling rule of the continuous-processing
    engine (:mod:`omr_scanner.services.continuous_engine`) as plain values, so
    the engine, its status snapshot and the tests apply one definition. See
    ``docs/decisions/ADR-0009-continuous-engine-single-writer.md``.

Responsibilities:
    * :class:`EngineState` - the engine's own lifecycle (never a session's).
    * :class:`EngineLimits` - every bound the engine keeps: sheets in flight,
      claims per transaction, sheets per commit, units per poll, retries.
    * :class:`UnitPolicy` and :func:`plan_units` - which source forms the next
      finite unit (``ScanBatch``) and how many ready files it takes.

What does NOT belong here:
    Database, Qt, file or process access.

The per-sheet processing states (stored in ``batch_scan.status``):

    ``pending`` / ``cancelled``  -> ``processing``  (claimed: one transaction)
    ``processing`` -> ``completed`` | ``warning`` | ``failed``
                                    (the durable work unit commits)
    ``processing`` -> ``pending``   (claim released: cancelled before it
                                    started, a worker process died, a clean
                                    stop that abandons in-flight work, or
                                    recovery after an interruption)

    ``duplicate`` is decided at registration (phase 4/5) and never claimed.
    No new state was needed; ``processing`` existed and is now written.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EngineState(StrEnum):
    """Where one engine instance is in its own lifecycle.

    A property of the *engine*, never of the scan session: an engine that has
    stopped leaves its session exactly as open (or closed) as it found it, and
    "nothing to do right now" is :attr:`EngineStatus.caught_up`, not a state.
    """

    NEW = "new"
    """Constructed; recovery has not run, nothing is claimed."""

    RUNNING = "running"
    """Recovered; registering, claiming, recognising and committing."""

    STOPPING = "stopping"
    """A stop was requested; no new work is claimed, in-flight work drains."""

    STOPPED = "stopped"
    """Shut down cleanly: nothing claimed by this engine remains ``processing``."""

    FAULTED = "faulted"
    """The writer kept failing; the engine stopped claiming. Claimed sheets
    were left ``processing`` on purpose - the next start returns them to
    ``pending`` (recovery), because a writer that cannot commit results cannot
    be trusted to release claims either."""


@dataclass(frozen=True, slots=True)
class EngineLimits:
    """Every bound the engine keeps, so memory follows these and not the session size.

    Attributes:
        max_in_flight: Sheets claimed by this engine and not yet committed -
            queued in the pool, inside a worker, or read and awaiting the
            writer. The engine claims nothing more while this many are out.
            This bounds the futures, the result objects and the writer backlog
            together: a result can only exist for a claimed sheet.
        claim_window: Most sheets claimed in one transaction.
        max_commit_group: Most finished sheets committed in one transaction
            (each is still its own complete work unit inside it).
        max_units_per_poll: Most finite units registered per scheduling pass,
            so a large backlog of ready files never stalls result collection.
        writer_retry_limit: Consecutive failed commits after which the engine
            stops claiming and enters :attr:`EngineState.FAULTED`.
        infrastructure_retries: How many times a sheet whose *worker process*
            died (not a recognition failure) is returned to ``pending`` and
            read again before it is recorded as failed, as today.
    """

    max_in_flight: int = 16
    claim_window: int = 32
    max_commit_group: int = 25
    max_units_per_poll: int = 1
    writer_retry_limit: int = 3
    infrastructure_retries: int = 1

    def __post_init__(self) -> None:
        """Refuse a bound that would stop the engine from ever making progress."""
        for name in (
            "max_in_flight",
            "claim_window",
            "max_commit_group",
            "max_units_per_poll",
            "writer_retry_limit",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"EngineLimits.{name} must be at least 1")
        if self.infrastructure_retries < 0:
            raise ValueError("EngineLimits.infrastructure_retries must not be negative")

    @classmethod
    def for_workers(cls, workers: int, *, per_worker: int = 4) -> EngineLimits:
        """Limits sized like the finite pool: ``per_worker`` sheets out per worker.

        The finite path keeps four tasks queued per worker
        (:data:`~omr_scanner.services.parallel_batch.QUEUE_DEPTH_PER_WORKER`);
        the engine keeps the same depth so a warm pool is never starved.
        """
        depth = max(1, workers) * max(1, per_worker)
        return cls(max_in_flight=depth, claim_window=max(depth, 1))


@dataclass(frozen=True, slots=True)
class UnitPolicy:
    """How ready files become finite units (``ARCHITECTURE_NOTES.md`` §11).

    Attributes:
        max_unit_size: A source's ready files form a unit as soon as this
            many are ready; a unit never holds more.
        trickle_seconds: A source with fewer ready files forms a unit of
            whatever it has once its oldest ready file has waited this long.
            A starting point, not a validated value.
    """

    max_unit_size: int = 200
    trickle_seconds: float = 30.0

    def __post_init__(self) -> None:
        """Refuse an empty unit size or a negative timeout."""
        if self.max_unit_size < 1:
            raise ValueError("UnitPolicy.max_unit_size must be at least 1")
        if self.trickle_seconds < 0:
            raise ValueError("UnitPolicy.trickle_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class UnitCandidate:
    """One source's ready files, summarised (what :func:`plan_units` decides on)."""

    source_id: str
    oldest_ready_at: datetime
    oldest_intake_file_id: int
    count: int


@dataclass(frozen=True, slots=True)
class PlannedUnit:
    """One unit to register now: ``take`` ready files of ``source_id``, oldest first."""

    source_id: str
    take: int
    full: bool
    """``True`` when the unit is full; ``False`` for a trickle unit."""


def plan_units(
    candidates: Sequence[UnitCandidate],
    *,
    now: datetime,
    policy: UnitPolicy,
    limit: int,
) -> tuple[PlannedUnit, ...]:
    """Decide which finite units to register now. Pure and deterministic.

    Sources are taken in the order of their oldest ready file
    ``(ready_at, intake_file_id)`` - the intake ledger's stable order, so the
    unit a file joins depends only on the ledger, never on dictionary order or
    timing inside one pass. A source is due when it has a full unit ready, or
    when its oldest ready file has waited ``trickle_seconds``. A source with
    more than one full unit ready yields several units, still at most
    ``limit`` in all.
    """
    planned: list[PlannedUnit] = []
    ordered = sorted(
        candidates, key=lambda item: (item.oldest_ready_at, item.oldest_intake_file_id)
    )
    for candidate in ordered:
        remaining = candidate.count
        took_full = False
        while remaining >= policy.max_unit_size and len(planned) < limit:
            planned.append(PlannedUnit(candidate.source_id, policy.max_unit_size, True))
            remaining -= policy.max_unit_size
            took_full = True
        # A remainder after a full unit is made of younger files than the
        # oldest one measured here: it is judged at the next pass instead.
        waited = (now - candidate.oldest_ready_at).total_seconds()
        if (
            remaining > 0
            and not took_full
            and waited >= policy.trickle_seconds
            and len(planned) < limit
        ):
            planned.append(PlannedUnit(candidate.source_id, remaining, False))
        if len(planned) >= limit:
            break
    return tuple(planned)


__all__ = [
    "EngineLimits",
    "EngineState",
    "PlannedUnit",
    "UnitCandidate",
    "UnitPolicy",
    "plan_units",
]
