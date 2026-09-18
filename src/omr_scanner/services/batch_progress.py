"""Knowing how far a batch has got, and how long it has left.

Purpose:
    Track a batch of any size - ten sheets or ten thousand - and answer the
    four questions a person watching it actually has: how much is done, how
    fast is it going, how long is left, and how much of it went wrong.

Responsibilities:
    * :class:`BatchProgressTracker` - counts terminal jobs, measures
      throughput, estimates time remaining. Thread-safe, because the thread
      driving the batch and the thread drawing the window are not the same one.
    * :class:`ProgressSnapshot` - one immutable reading of that state, which is
      what a user interface renders.
    * :func:`format_duration` / :func:`format_rate` - one consistent way to
      write a duration and a speed, so two parts of the application never
      disagree about what ``00:18:42`` means.

What does NOT belong here:
    * Qt, widgets, timers, or any knowledge that a GUI exists. All of this is
      unit-testable with a fake clock and no display, which is the point: an
      estimator that can only be exercised by watching a progress bar is an
      estimator nobody tests.
    * Recognition. The tracker is told that a job reached a terminal state; it
      has no opinion about what was on the sheet.

Why every terminal state advances progress:
    A failed scan is a *finished* scan. Counting only successes is how a
    progress bar stalls at 97% for the rest of the afternoon while the last
    three hundred corrupted files quietly fail. :class:`JobStatus` enumerates
    the terminal states, and all of them move the bar.

On the word "estimate":
    The remaining time is an estimate and is always presented as one. It is
    derived from recent measured throughput, which is the best available
    evidence and still wrong whenever the next thousand sheets are unlike the
    last thousand.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

DEFAULT_SMOOTHING = 0.3
"""Weight given to the newest throughput sample in the moving average.

Low enough that one slow sheet does not halve the estimate, high enough that a
genuine change in speed - a pool that has finished warming up, a folder of
larger images - shows up within a few seconds rather than a few minutes. At
one sample every half second, a step change is roughly 90% absorbed in about
three seconds."""

SAMPLE_INTERVAL_SECONDS = 0.5
"""How much time one throughput sample covers.

Shorter samples measure scheduling noise rather than throughput; longer ones
make the estimate sluggish. Half a second is long enough to contain several
completions on a fast multicore run and short enough to react."""

WARMUP_JOBS = 10
"""Completions required before an estimate is offered at all.

The first sheets of a multicore run are unrepresentative: worker processes are
starting, NumPy and OpenCV are being imported, the template is being
unpickled, and the operating system has not yet cached anything. An estimate
from that evidence is not a cautious estimate, it is a wrong one - and a user
who is shown "27 hours" and then "34 minutes" ten seconds later has learned
that the number is noise."""

WARMUP_SECONDS = 3.0
"""Alternative warm-up rule, for batches too small to reach :data:`WARMUP_JOBS`.

A slow, small batch should still eventually say something useful, so a few
completions spread over a few seconds also qualifies."""

WARMUP_MINIMUM_JOBS = 3
"""Completions required before :data:`WARMUP_SECONDS` can qualify on its own.

Two samples can be two accidents; a rate from a single completion is not a
rate at all."""


class JobStatus(StrEnum):
    """The terminal states one scan can reach.

    Every one of these advances the progress bar. A job that has reached any
    of them will not be worked on again.
    """

    SUCCESS = "success"
    """Read cleanly; nothing needs a human."""

    WARNING = "warning"
    """Read, but something needs review - a blank, a double mark, a faint one,
    an alignment reservation."""

    FAILED = "failed"
    """Could not be read: unreadable file, unregistrable page, or an
    unexpected error."""

    SKIPPED = "skipped"
    """Deliberately not processed - an unsupported file, or one already done in
    an earlier run. Reserved for the resumable batches of a later phase."""

    CANCELLED = "cancelled"
    """Abandoned because the user stopped the run before it was reached."""


class BatchState(StrEnum):
    """Where a batch is in its life."""

    IDLE = "idle"
    """Nothing is running."""

    PREPARING = "preparing"
    """Counting and validating files. The total is not yet known, so there is
    nothing honest to put in a progress bar."""

    PROCESSING = "processing"
    """Reading sheets."""

    CANCELLING = "cancelling"
    """The user has asked to stop. Sheets already inside a worker are
    finishing; nothing new will start."""

    CANCELLED = "cancelled"
    """Stopped before the end, at the user's request."""

    COMPLETED = "completed"
    """Every job reached a terminal state."""

    FAILED = "failed"
    """The run itself could not proceed - not a failed sheet, a failed batch."""


TERMINAL_STATES: frozenset[BatchState] = frozenset(
    {BatchState.CANCELLED, BatchState.COMPLETED, BatchState.FAILED}
)
"""States from which nothing more will happen."""


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """One immutable reading of a batch's progress.

    A snapshot is what a user interface renders. It is a value, not a view onto
    live state, so a slow repaint can never show a half-updated batch - and a
    test can assert against one without a clock.

    Attributes:
        state: Where the batch is in its life.
        total: Jobs in the batch, or ``0`` while preparing.
        successful: Jobs that finished cleanly.
        warnings: Jobs that finished but need a human.
        failed: Jobs that could not be read.
        skipped: Jobs deliberately not processed.
        cancelled: Jobs abandoned when the run was stopped.
        elapsed_seconds: Wall-clock time since processing started, measured on
            a monotonic clock so that a system clock change cannot make a batch
            appear to run backwards.
        rate: Smoothed throughput in jobs per second, or ``0.0`` when there is
            not yet enough evidence.
        eta_seconds: Estimated seconds remaining, or ``None`` when no estimate
            is warranted - during warm-up, while cancelling, or when nothing is
            moving. Always an estimate.
        finish_wall_clock: Estimated finish as a Unix timestamp, or ``None``.
            Wall clock rather than monotonic because this one is meant to be
            read as a time of day.
        workers: How many sheets the run reads at once.
    """

    state: BatchState = BatchState.IDLE
    total: int = 0
    successful: int = 0
    warnings: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    elapsed_seconds: float = 0.0
    rate: float = 0.0
    eta_seconds: float | None = None
    finish_wall_clock: float | None = None
    workers: int = 1

    @property
    def completed(self) -> int:
        """Jobs that have reached a terminal state, however they ended."""
        return self.successful + self.warnings + self.failed + self.skipped + self.cancelled

    @property
    def remaining(self) -> int:
        """Jobs not yet finished. Never negative, even if a caller over-counts."""
        return max(self.total - self.completed, 0)

    @property
    def fraction(self) -> float:
        """Completion in ``[0, 1]``; ``0.0`` when the total is unknown."""
        if self.total <= 0:
            return 0.0
        return min(self.completed / self.total, 1.0)

    @property
    def percent(self) -> float:
        """Completion as a percentage, to be formatted by the caller."""
        return self.fraction * 100.0

    @property
    def is_running(self) -> bool:
        """Whether work is still going on."""
        return self.state in (BatchState.PREPARING, BatchState.PROCESSING, BatchState.CANCELLING)

    @property
    def is_finished(self) -> bool:
        """Whether the batch has stopped, for any reason."""
        return self.state in TERMINAL_STATES

    @property
    def has_eta(self) -> bool:
        """Whether an estimate of the remaining time is being offered."""
        return self.eta_seconds is not None

    @property
    def average_rate(self) -> float:
        """Jobs per second over the whole run, for a completion summary.

        The *average*, unlike :attr:`rate`, is not smoothed and not recent: it
        is the honest overall figure to report once a batch has finished.
        """
        if self.elapsed_seconds <= 0.0:
            return 0.0
        return self.completed / self.elapsed_seconds


class BatchProgressTracker:
    """Counts a batch's progress and estimates what is left.

    Thread safety:
        Every method takes one lock. Completions arrive on whichever thread is
        driving the batch; snapshots are read from the thread drawing the
        window. Counting in one place under one lock is what keeps the totals
        right when eight workers finish at once - and is the reason workers
        report completions rather than incrementing shared counters themselves.

    Args:
        smoothing: Weight of the newest sample in the moving average.
        warmup_jobs: Completions before an estimate is offered.
        warmup_seconds: Alternative warm-up, for slow small batches.
        sample_interval: Seconds of work one throughput sample covers.
        clock: Monotonic clock, injectable so that the estimator can be tested
            in milliseconds instead of minutes.
        wall_clock: Wall clock, used only for the estimated finishing time.
    """

    def __init__(
        self,
        *,
        smoothing: float = DEFAULT_SMOOTHING,
        warmup_jobs: int = WARMUP_JOBS,
        warmup_seconds: float = WARMUP_SECONDS,
        sample_interval: float = SAMPLE_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not 0.0 < smoothing <= 1.0:
            raise ValueError(f"smoothing must be in (0, 1], got {smoothing}")
        if sample_interval <= 0.0:
            raise ValueError(f"sample_interval must be positive, got {sample_interval}")

        self._smoothing = smoothing
        self._warmup_jobs = max(warmup_jobs, 1)
        self._warmup_seconds = max(warmup_seconds, 0.0)
        self._sample_interval = sample_interval
        self._clock = clock
        self._wall_clock = wall_clock

        self._lock = threading.Lock()
        self._state = BatchState.IDLE
        self._total = 0
        self._workers = 1
        self._counts: dict[JobStatus, int] = dict.fromkeys(JobStatus, 0)

        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._window_started_at = 0.0
        self._window_jobs = 0
        self._smoothed_rate = 0.0
        self._samples = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def prepare(self) -> None:
        """Enter the preparing state: files are being counted.

        A separate state because "0 of 0, 0% done" during a two-second
        directory walk is not progress, it is a bar that looks broken.
        """
        with self._lock:
            self._reset()
            self._state = BatchState.PREPARING

    def start(self, total: int, *, workers: int = 1) -> None:
        """Begin processing ``total`` jobs on ``workers`` workers."""
        with self._lock:
            self._reset()
            self._total = max(total, 0)
            self._workers = max(workers, 1)
            self._state = BatchState.PROCESSING
            now = self._clock()
            self._started_at = now
            self._window_started_at = now

    def record(self, status: JobStatus, count: int = 1) -> None:
        """Record ``count`` jobs reaching ``status``.

        Args:
            status: The terminal state reached.
            count: How many jobs reached it, for a caller that aggregates.

        Cheap on purpose: this runs once per finished sheet, on the hot path of
        a ten-thousand-sheet batch. It increments two integers and, at most
        twice a second, updates one float.
        """
        if count <= 0:
            return
        with self._lock:
            if self._started_at is None:
                # A completion before start() is a caller bug, but losing the
                # count would be worse than adopting a start time here.
                self._state = BatchState.PROCESSING
                self._started_at = self._clock()
                self._window_started_at = self._started_at
            self._counts[status] += count
            self._window_jobs += count
            self._maybe_sample(self._clock())

    def request_cancel(self) -> None:
        """Note that the user has asked to stop.

        Only meaningful while processing: cancelling a finished batch is a
        double click on the button, not a state change.
        """
        with self._lock:
            if self._state is BatchState.PROCESSING:
                self._state = BatchState.CANCELLING

    def finish(self, *, cancelled: bool = False) -> None:
        """Mark the batch finished, by completion or cancellation."""
        with self._lock:
            self._finished_at = self._clock()
            self._state = BatchState.CANCELLED if cancelled else BatchState.COMPLETED

    def fail(self) -> None:
        """Mark the batch itself as having failed to run."""
        with self._lock:
            self._finished_at = self._clock()
            self._state = BatchState.FAILED

    def set_total(self, total: int) -> None:
        """Set the job count once preparation has established it."""
        with self._lock:
            self._total = max(total, 0)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------
    def snapshot(self) -> ProgressSnapshot:
        """Return an immutable reading of the current state.

        Side-effect free: a snapshot never advances the estimator, so taking
        one twice, or not at all, cannot change what the next one says. The
        partial throughput sample in flight *is* folded into the estimate, so a
        stalled batch's remaining time grows rather than freezing at whatever
        the last completion implied.
        """
        with self._lock:
            now = self._clock()
            elapsed = self._elapsed(now)
            rate = self._effective_rate(now)
            eta = self._eta(rate, elapsed)
            return ProgressSnapshot(
                state=self._state,
                total=self._total,
                successful=self._counts[JobStatus.SUCCESS],
                warnings=self._counts[JobStatus.WARNING],
                failed=self._counts[JobStatus.FAILED],
                skipped=self._counts[JobStatus.SKIPPED],
                cancelled=self._counts[JobStatus.CANCELLED],
                elapsed_seconds=elapsed,
                rate=rate,
                eta_seconds=eta,
                finish_wall_clock=(self._wall_clock() + eta) if eta is not None else None,
                workers=self._workers,
            )

    # ------------------------------------------------------------------
    # Internals - all called with the lock held
    # ------------------------------------------------------------------
    def _reset(self) -> None:
        """Forget the previous run."""
        self._total = 0
        self._workers = 1
        self._counts = dict.fromkeys(JobStatus, 0)
        self._started_at = None
        self._finished_at = None
        self._window_started_at = 0.0
        self._window_jobs = 0
        self._smoothed_rate = 0.0
        self._samples = 0

    def _completed(self) -> int:
        return sum(self._counts.values())

    def _elapsed(self, now: float) -> float:
        """Seconds of processing, frozen once the batch has finished."""
        if self._started_at is None:
            return 0.0
        end = self._finished_at if self._finished_at is not None else now
        return max(end - self._started_at, 0.0)

    def _maybe_sample(self, now: float) -> None:
        """Fold the current window into the moving average, if it is long enough."""
        span = now - self._window_started_at
        if span < self._sample_interval:
            return
        instantaneous = self._window_jobs / span
        if self._samples == 0:
            # Seed with the first real measurement rather than with zero;
            # starting from zero would make the first estimate far too
            # pessimistic and then drag towards the truth for several seconds.
            self._smoothed_rate = instantaneous
        else:
            self._smoothed_rate = (
                self._smoothing * instantaneous + (1.0 - self._smoothing) * self._smoothed_rate
            )
        self._samples += 1
        self._window_started_at = now
        self._window_jobs = 0

    def _effective_rate(self, now: float) -> float:
        """The rate an estimate should use, including the sample in flight."""
        if self._finished_at is not None:
            # A finished batch reports its overall average: "how fast was it",
            # not "how fast was it in its last half second".
            elapsed = self._elapsed(now)
            return self._completed() / elapsed if elapsed > 0.0 else 0.0
        if self._samples == 0:
            # No full sample yet - a short, fast batch can finish inside one
            # sample interval. The average since the start is then the only
            # evidence there is, and it is better evidence than nothing: the
            # alternative is a run that reports "Speed --" from beginning to
            # end. The warm-up rule still decides whether an *estimate* is
            # offered on top of it.
            elapsed = self._elapsed(now)
            completed = self._completed()
            if completed > 0 and elapsed > 0.0:
                return completed / elapsed
            return 0.0

        span = now - self._window_started_at
        if span < self._sample_interval:
            return self._smoothed_rate
        # The window has run long without being folded in, which means either a
        # slow patch or a stall. Blending it in - possibly at a rate of zero -
        # is what makes a stalled batch's estimate grow instead of freezing.
        instantaneous = self._window_jobs / span
        return max(
            self._smoothing * instantaneous + (1.0 - self._smoothing) * self._smoothed_rate,
            0.0,
        )

    def _eta(self, rate: float, elapsed: float) -> float | None:
        """Estimate the seconds remaining, or ``None`` when no estimate is honest."""
        completed = self._completed()

        if self._state is BatchState.COMPLETED:
            return 0.0
        if self._state in (BatchState.CANCELLING, BatchState.CANCELLED, BatchState.FAILED):
            # A cancelled run has no meaningful remaining time: the answer is
            # "as long as the sheets in flight take", which is not a number
            # worth pretending to know.
            return None
        if self._state is not BatchState.PROCESSING or self._total <= 0:
            return None

        remaining = max(self._total - completed, 0)
        if remaining == 0:
            return 0.0
        if not self._warmed_up(completed, elapsed):
            return None
        if rate <= 0.0:
            return None
        return remaining / rate

    def _warmed_up(self, completed: int, elapsed: float) -> bool:
        """Whether enough evidence exists to offer an estimate at all."""
        if completed >= self._warmup_jobs:
            return True
        return completed >= WARMUP_MINIMUM_JOBS and elapsed >= self._warmup_seconds


# ----------------------------------------------------------------------
# Formatting - one style, used everywhere
# ----------------------------------------------------------------------
def format_duration(seconds: float | None) -> str:
    """Render a duration the same way everywhere in the application.

    Args:
        seconds: A duration, or ``None`` when there is not one.

    Returns:
        ``"--:--"`` for ``None``, ``"HH:MM:SS"`` below a day, and
        ``"1d 03:17:20"`` beyond it. One style rather than several, so a reader
        never has to work out whether ``12:48`` is minutes or hours.

    A negative duration is clamped to zero: it means a clock went backwards,
    not that a batch will finish in the past.
    """
    if seconds is None:
        return "--:--"
    total = int(max(seconds, 0.0))
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_rate(rate: float) -> str:
    """Render a throughput without spurious precision.

    Fast runs read naturally as a speed (``"5.7 scans/sec"``); slow ones read
    better inverted (``"2.4 sec/scan"``), because "0.42 scans/sec" makes a
    reader do the division themselves.
    """
    if rate <= 0.0:
        return "--"
    if rate >= 10.0:
        return f"{rate:.0f} scans/sec"
    if rate >= 1.0:
        return f"{rate:.1f} scans/sec"
    return f"{1.0 / rate:.1f} sec/scan"


def format_count(value: int) -> str:
    """Render a count with thousands separators: ``6,342``.

    Batches are large enough that ``10000`` and ``100000`` are genuinely hard
    to tell apart at a glance.
    """
    return f"{value:,}"


__all__ = [
    "DEFAULT_SMOOTHING",
    "SAMPLE_INTERVAL_SECONDS",
    "TERMINAL_STATES",
    "WARMUP_JOBS",
    "WARMUP_SECONDS",
    "BatchProgressTracker",
    "BatchState",
    "JobStatus",
    "ProgressSnapshot",
    "format_count",
    "format_duration",
    "format_rate",
]
