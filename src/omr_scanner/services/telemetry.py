"""Lightweight performance telemetry for long batch runs (Phase 10, §24).

Purpose:
    Answer, for a run that might take hours, "how is it going right now and
    how much longer" without adding meaningful overhead or a second
    monitoring stack.

Responsibilities:
    * :class:`TelemetrySample` - one snapshot: elapsed time, throughput, CPU,
      memory, disk, database size, queue depth.
    * :class:`TelemetryRecorder` - samples on a plain background thread at a
      fixed interval and appends each sample as one JSON line to a file, so
      a run that is killed mid-way still leaves every sample it took before
      that moment.
    * :func:`estimate_completion` - a stabilised ETA from rolling throughput
      (§25): never from one sheet, and explicit that pauses and resumes
      reset the *rate* window without discarding cumulative progress.

What does NOT belong here:
    * Deciding *when* to sample from inside the batch pipeline. A recorder is
      handed to whichever caller already has progress counts
      (:mod:`omr_scanner.evaluation.stress_runner`, or a future GUI progress
      view) and is fed snapshots; it never reaches into
      :mod:`omr_scanner.services.batch_processor` itself.
    * A persistent metrics database or a dashboard. One JSON-lines file is
      the entire storage model, on purpose - see §24's explicit instruction
      not to introduce a heavyweight monitoring stack.

Why JSON lines, appended, not one JSON document written at the end:
    A run this module exists to watch might be killed. A single document
    rewritten in full each sample would risk exactly the "torn write on
    interruption" problem the rest of this phase spends effort preventing
    elsewhere; one immutable line appended per sample can never be
    half-written in a way that corrupts an earlier one.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL_SECONDS = 5.0
"""How often a sample is taken. Frequent enough to see a stalled run within
a few seconds; infrequent enough that sampling itself is not measurable
overhead against a run that reads a sheet every few milliseconds."""

ETA_ROLLING_WINDOW_SAMPLES = 12
"""Samples averaged for the ETA's throughput figure - one minute at the
default interval. Short enough to react to a real slowdown, long enough
that one unusually slow sheet cannot swing the estimate (§25)."""


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One point-in-time snapshot of a running batch.

    Attributes:
        elapsed_seconds: Wall-clock time since this recorder started.
        sheets_completed: Sheets that have reached a terminal state.
        sheets_pending: Sheets not yet attempted.
        sheets_failed: Sheets that failed.
        sheets_per_second: Instantaneous rate since the previous sample.
        process_cpu_percent: This process's CPU usage, ``0``-``100`` per
            core (so a fully-used 4-core machine reads up to ``400``).
        system_cpu_percent: Whole-machine CPU usage, ``0``-``100``.
        process_memory_mb: This process's resident memory.
        system_available_memory_mb: System memory still available.
        database_size_mb: Project database file size, or ``0`` when unknown.
        disk_free_mb: Free space on the database's drive, or ``0`` when
            unknown.
        worker_count: Configured worker processes, for the report.
    """

    elapsed_seconds: float
    sheets_completed: int
    sheets_pending: int
    sheets_failed: int
    sheets_per_second: float
    process_cpu_percent: float
    system_cpu_percent: float
    process_memory_mb: float
    system_available_memory_mb: float
    database_size_mb: float
    disk_free_mb: float
    worker_count: int


class TelemetryRecorder:
    """Samples progress on a background thread and appends JSON lines.

    Args:
        output_path: Where samples are appended, one JSON object per line.
            Parent directories are created if needed.
        progress_fn: Called on each tick; returns
            ``(completed, pending, failed)`` sheet counts. Reading progress
            is the caller's business (a
            :class:`~omr_scanner.services.batch_progress.BatchProgressTracker`,
            or a direct database count) - this class only turns it into a
            sample.
        database_path: Project database file, for its size. ``None`` skips
            that field (still zero-cost, and honest about not knowing).
        worker_count: Recorded verbatim into every sample.
        interval_seconds: How often to sample.

    Usage:
        Call :meth:`start`, run the batch, call :meth:`stop` when it ends
        (successfully, cancelled, or about to raise) - the ``finally`` of
        whatever loop owns the run. A sample taken after the last one before
        an abrupt kill is not lost; only a sample that would have been taken
        after the process died is.
    """

    def __init__(
        self,
        output_path: Path,
        progress_fn: Callable[[], tuple[int, int, int]],
        *,
        database_path: Path | None = None,
        worker_count: int = 1,
        interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self._output_path = output_path
        self._progress_fn = progress_fn
        self._database_path = database_path
        self._worker_count = worker_count
        self._interval = interval_seconds
        self._process = psutil.Process(os.getpid())
        self._started_at: float | None = None
        self._last_completed = 0
        self._last_sample_time = 0.0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.samples: list[TelemetrySample] = []

    def start(self) -> None:
        """Begin sampling on a background thread."""
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        self._started_at = time.monotonic()
        self._last_sample_time = self._started_at
        self._process.cpu_percent(interval=None)  # prime the non-blocking counter
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling and take one final sample."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 2)
        self._sample_once()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._sample_once()
            except Exception:  # pragma: no cover - telemetry must never crash a run
                _LOGGER.exception("Telemetry sample failed; continuing without it")

    def _sample_once(self) -> None:
        if self._started_at is None:  # pragma: no cover - start() always sets this
            return
        now = time.monotonic()
        completed, pending, failed = self._progress_fn()
        elapsed_since_last = max(now - self._last_sample_time, 1e-6)
        rate = (completed - self._last_completed) / elapsed_since_last
        self._last_completed = completed
        self._last_sample_time = now

        memory = self._process.memory_info()
        system_memory = psutil.virtual_memory()
        database_size = 0.0
        disk_free = 0.0
        if self._database_path is not None:
            try:
                database_size = self._database_path.stat().st_size / (1024 * 1024)
                disk_free = shutil.disk_usage(self._database_path.parent).free / (1024 * 1024)
            except OSError:
                pass

        sample = TelemetrySample(
            elapsed_seconds=now - self._started_at,
            sheets_completed=completed,
            sheets_pending=pending,
            sheets_failed=failed,
            sheets_per_second=max(rate, 0.0),
            process_cpu_percent=self._process.cpu_percent(interval=None),
            system_cpu_percent=psutil.cpu_percent(interval=None),
            process_memory_mb=memory.rss / (1024 * 1024),
            system_available_memory_mb=system_memory.available / (1024 * 1024),
            database_size_mb=database_size,
            disk_free_mb=disk_free,
            worker_count=self._worker_count,
        )
        self.samples.append(sample)
        with self._output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(sample)) + "\n")


def estimate_completion(
    samples: list[TelemetrySample], *, window: int = ETA_ROLLING_WINDOW_SAMPLES
) -> float | None:
    """Return estimated seconds remaining, from recent rolling throughput.

    Args:
        samples: Samples so far, oldest first.
        window: How many of the most recent samples to average.

    Returns:
        Seconds remaining, or ``None`` when there is not yet enough evidence
        (fewer than two samples, or zero measured throughput) - the honest
        answer per §25's "do not claim precision the measurements do not
        support", rather than a number from a single early sample.
    """
    if len(samples) < 2:
        return None
    recent = samples[-window:]
    average_rate = sum(sample.sheets_per_second for sample in recent) / len(recent)
    if average_rate <= 0:
        return None
    remaining = recent[-1].sheets_pending
    if remaining <= 0:
        return 0.0
    return remaining / average_rate


def read_samples(path: Path) -> list[TelemetrySample]:
    """Read back a telemetry file written by :class:`TelemetryRecorder`.

    A malformed trailing line (a sample interrupted mid-write) is skipped
    rather than failing the whole read - the same "one bad row costs one
    row" discipline the rest of this project applies to a batch of scans.
    """
    if not path.is_file():
        return []
    samples: list[TelemetrySample] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            samples.append(TelemetrySample(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return samples


__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "ETA_ROLLING_WINDOW_SAMPLES",
    "TelemetryRecorder",
    "TelemetrySample",
    "estimate_completion",
    "read_samples",
]
