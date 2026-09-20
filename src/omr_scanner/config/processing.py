"""How many CPU workers a batch may use, and where that choice is stored.

Purpose:
    Turn "Automatic", "Single core" or "8 workers" - a *user preference* - into
    the one number the batch processor needs: how many worker processes to start
    for this particular run.

Responsibilities:
    * :class:`ProcessingMode` - the three choices offered in Settings.
    * :class:`ProcessingSettings` - the stored preference, nested inside
      :class:`~omr_scanner.config.app_config.AppConfig`.
    * :func:`detected_cpu_count` - how many logical CPUs this machine reports.
    * :meth:`ProcessingSettings.resolve_worker_count` - the whole decision, in
      one pure function, so the GUI's "Active workers: 7" label and the batch
      that actually runs can never disagree.

What does NOT belong here:
    * Starting processes. This module decides a number; the pool is the batch
      processor's business (:mod:`omr_scanner.services.batch_processor`).
    * Recognition tuning. Thresholds belong to the template, because they are
      properties of a sheet design; worker counts belong to the machine, which
      is why they live in the per-user application configuration.

Why one CPU is left free by default:
    The Qt GUI, the operating system, the file system and the user's other work
    all need a share. Saturating every logical CPU makes the window stutter and
    buys little: the pipeline is memory-bandwidth bound well before it is
    core-count bound (see ``docs/scan_workflow.md``, "Measured throughput").

Why automatic mode is also capped:
    Every worker is a separate Python process that imports NumPy and OpenCV and
    holds a full-resolution page plus its rectified copy. Measured on the
    development machine (16 logical CPUs, 48 copies of ``examples/ECE-0000.png``
    at 2480x3508): 127 MB peak for one worker, 855 MB for eight, 1,607 MB for
    sixteen - roughly 95 MB per additional worker, and it keeps growing
    linearly. Throughput does not: 3.44 scans/s at one worker, 11.63 at eight,
    then *down* to 10.80 at twelve and 10.02 at sixteen, because the machine's
    eight physical cores are already saturated.

    So automatic mode stops at :data:`AUTOMATIC_WORKER_LIMIT`, past which
    measurement says a batch costs more memory and finishes no sooner. A user
    who knows their own machine can still ask for more in Custom mode. The
    numbers are reproducible with ``scripts/benchmark_batch.py``.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

AUTOMATIC_WORKER_LIMIT = 8
"""Upper bound on the worker count :attr:`ProcessingMode.AUTOMATIC` will choose.

Chosen from the benchmark recorded in ``docs/scan_workflow.md`` and summarised
in this module's docstring: on the development machine throughput peaked at
eight workers and fell away beyond it, while memory kept climbing by about
95 MB per worker. Automatic mode is the setting a user never thinks about, so it
stays inside the region where more workers are known to be worth their memory."""

RESERVED_CPU_COUNT = 1
"""Logical CPUs automatic mode leaves for the GUI, the OS and file I/O."""

MAX_CONFIGURABLE_WORKERS = 256
"""Absolute ceiling accepted in a stored configuration file.

The *offered* range is ``1 .. detected_cpu_count()``, which is decided per
machine and enforced by the Settings dialog. This much higher bound is only what
the document validator will tolerate, so that a configuration written on a
64-core machine and copied to a laptop still loads - it is clamped when it is
used, not rejected when it is read."""

DEFAULT_CUSTOM_WORKERS = 4
"""Where the custom worker selector starts before the user has chosen."""

DEFAULT_OPENCV_THREADS = 1
"""OpenCV's own internal thread count inside each worker *process*.

Kept at one by default for the reason :mod:`omr_scanner.services.parallel_batch`'s
module docstring already gives: the parallelism in this application comes
from *processes*, not from OpenCV's own thread pool, and N worker processes
each spawning several OpenCV threads oversubscribes the machine's cores for
no benefit. An advanced setting, not a default a user is expected to touch -
see :data:`MAX_CONFIGURABLE_OPENCV_THREADS`."""

MAX_CONFIGURABLE_OPENCV_THREADS = 16
"""Ceiling accepted in a stored configuration, for the same reason
:data:`MAX_CONFIGURABLE_WORKERS` has one: a value from a different machine's
settings file must load, not fail validation, even if it would be unwise here."""

DEFAULT_WORKER_RECYCLE_AFTER = 500
"""Sheets a worker process reads before it is replaced (Phase 10, §16).

Chosen as a conservative default that recycles a worker often enough to
bound native-library memory growth (OpenCV decoders, NumPy allocations) over
a very long run, without recycling so often that the fixed cost of starting
a fresh process - re-importing OpenCV, re-sending the template - starts to
matter. See ``development/PHASE_10_HANDOFF.md`` for the benchmark this was
chosen from. ``0`` disables recycling entirely."""

MAX_CONFIGURABLE_WORKER_RECYCLE_AFTER = 1_000_000
"""Effectively "never" as a stored upper bound, for the same reason
:data:`MAX_CONFIGURABLE_WORKERS` has one."""


class ProcessingMode(StrEnum):
    """How the worker count for a batch is decided."""

    AUTOMATIC = "automatic"
    """Leave one logical CPU free, and use no more than
    :data:`AUTOMATIC_WORKER_LIMIT`."""

    SINGLE_CORE = "single_core"
    """Exactly one worker, in the GUI's own process. Deterministic ordering, no
    process pool at all, and the mode to reach for when debugging, benchmarking
    a baseline, or working on a machine short of memory."""

    CUSTOM = "custom"
    """Exactly :attr:`ProcessingSettings.worker_count` workers, clamped to the
    machine's logical CPU count."""


def detected_cpu_count() -> int:
    """Return the number of logical CPUs this machine reports.

    Returns:
        ``os.cpu_count()``, or ``1`` when the platform declines to say. Never
        zero: every caller divides work among this many workers.
    """
    return os.cpu_count() or 1


class ProcessingSettings(BaseModel):
    """The user's batch-processing preference.

    Attributes:
        mode: Which rule decides the worker count.
        worker_count: The number used by :attr:`ProcessingMode.CUSTOM`. Kept
            even while another mode is selected, so switching to Custom and back
            does not lose the number the user chose.
        diagnostics_enabled: Write the recognition engine's staged debug images
            for every sheet processed. Off by default and meant to stay that
            way: it is three or four full-page images per scan, which on a real
            batch is gigabytes written for nobody.
        diagnostics_dir: Where those images go. Diagnostics stay off until this
            is set, so switching them on can never scatter debug files through
            a project folder.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: ProcessingMode = ProcessingMode.AUTOMATIC
    worker_count: int = Field(
        default=DEFAULT_CUSTOM_WORKERS, ge=1, le=MAX_CONFIGURABLE_WORKERS
    )
    diagnostics_enabled: bool = False
    diagnostics_dir: Path | None = None
    opencv_threads: int = Field(
        default=DEFAULT_OPENCV_THREADS, ge=1, le=MAX_CONFIGURABLE_OPENCV_THREADS
    )
    """OpenCV's internal thread count *inside each worker process* (Phase 10,
    §18). Not the same axis as :attr:`worker_count` - that is how many
    processes run at once; this is how many native threads each one may use
    for its own image operations."""
    worker_recycle_after: int = Field(
        default=DEFAULT_WORKER_RECYCLE_AFTER,
        ge=0,
        le=MAX_CONFIGURABLE_WORKER_RECYCLE_AFTER,
    )
    """Sheets a worker processes before being replaced (Phase 10, §16).
    ``0`` disables recycling."""

    @property
    def writes_diagnostics(self) -> bool:
        """Whether a run would actually write diagnostic images."""
        return self.diagnostics_enabled and self.diagnostics_dir is not None

    def configured_worker_count(self, cpu_count: int | None = None) -> int:
        """Return the workers this setting asks for, ignoring the batch size.

        Args:
            cpu_count: Logical CPUs to assume. Defaults to this machine's.

        Returns:
            At least ``1``. This is the number the Settings dialog shows as
            "Active workers"; a run may still use fewer when there are fewer
            scans than workers (see :meth:`resolve_worker_count`).
        """
        cpus = max(cpu_count if cpu_count is not None else detected_cpu_count(), 1)

        if self.mode is ProcessingMode.SINGLE_CORE:
            return 1
        if self.mode is ProcessingMode.CUSTOM:
            return max(1, min(self.worker_count, cpus))
        return max(1, min(cpus - RESERVED_CPU_COUNT, AUTOMATIC_WORKER_LIMIT))

    def resolve_worker_count(
        self, item_count: int, cpu_count: int | None = None
    ) -> int:
        """Return how many workers a run over ``item_count`` scans should use.

        Args:
            item_count: How many scans the batch contains.
            cpu_count: Logical CPUs to assume. Defaults to this machine's.

        Returns:
            ``min(configured workers, item_count)``, and never less than ``1``.
            Three scans never start thirty-one processes: each one would cost
            more to start than the page it was given costs to read.
        """
        configured = self.configured_worker_count(cpu_count)
        if item_count <= 0:
            return 1
        return max(1, min(configured, item_count))

    def with_mode(self, mode: ProcessingMode) -> ProcessingSettings:
        """Return a copy using ``mode``; the receiver is unchanged."""
        return self.model_copy(update={"mode": mode})

    def with_diagnostics(
        self, *, enabled: bool, directory: Path | None
    ) -> ProcessingSettings:
        """Return a copy with the diagnostics choice applied."""
        return self.model_copy(
            update={"diagnostics_enabled": enabled, "diagnostics_dir": directory}
        )

    def with_worker_count(self, workers: int) -> ProcessingSettings:
        """Return a copy whose custom worker count is ``workers``.

        Args:
            workers: Requested workers. Clamped into the accepted range rather
                than rejected: a spin box and a hand-edited file are both
                capable of producing nonsense, and neither is worth an error
                dialog when the intent is obvious.
        """
        clamped = max(1, min(int(workers), MAX_CONFIGURABLE_WORKERS))
        return self.model_copy(update={"worker_count": clamped})

    def with_opencv_threads(self, threads: int) -> ProcessingSettings:
        """Return a copy with OpenCV's per-worker thread count set, clamped."""
        clamped = max(1, min(int(threads), MAX_CONFIGURABLE_OPENCV_THREADS))
        return self.model_copy(update={"opencv_threads": clamped})

    def with_worker_recycle_after(self, sheets: int) -> ProcessingSettings:
        """Return a copy with the worker-recycle interval set, clamped.

        ``0`` disables recycling and is a valid, deliberate choice - not
        clamped up to the minimum the way a worker or thread *count* would be.
        """
        clamped = max(0, min(int(sheets), MAX_CONFIGURABLE_WORKER_RECYCLE_AFTER))
        return self.model_copy(update={"worker_recycle_after": clamped})


__all__ = [
    "AUTOMATIC_WORKER_LIMIT",
    "DEFAULT_CUSTOM_WORKERS",
    "DEFAULT_OPENCV_THREADS",
    "DEFAULT_WORKER_RECYCLE_AFTER",
    "MAX_CONFIGURABLE_OPENCV_THREADS",
    "MAX_CONFIGURABLE_WORKERS",
    "MAX_CONFIGURABLE_WORKER_RECYCLE_AFTER",
    "RESERVED_CPU_COUNT",
    "ProcessingMode",
    "ProcessingSettings",
    "detected_cpu_count",
]
