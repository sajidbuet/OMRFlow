"""Tests for batch progress counting and the time-remaining estimate.

Why these run headless, with a fake clock:
    An estimator that can only be exercised by watching a progress bar is an
    estimator nobody tests. Injecting the clock turns "does the estimate
    converge over twenty minutes of processing" into a test that runs in
    microseconds and gives the same answer every time.

The scenarios are the ones that actually go wrong in progress bars: the first
few samples, a stall, a batch of nothing, a batch of one, and ten thousand
completions arriving from several threads at once.
"""

from __future__ import annotations

import threading

import pytest

from omr_scanner.services.batch_progress import (
    DEFAULT_SMOOTHING,
    WARMUP_JOBS,
    BatchProgressTracker,
    BatchState,
    JobStatus,
    ProgressSnapshot,
    format_count,
    format_duration,
    format_rate,
)


class FakeClock:
    """A clock a test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tracker(clock: FakeClock) -> BatchProgressTracker:
    """A tracker on a fake clock, with the production warm-up rules."""
    return BatchProgressTracker(clock=clock, wall_clock=lambda: 1_700_000_000.0)


def run_at(
    tracker: BatchProgressTracker,
    clock: FakeClock,
    *,
    jobs: int,
    seconds_per_job: float,
    status: JobStatus = JobStatus.SUCCESS,
) -> None:
    """Complete ``jobs`` at a steady pace."""
    for _ in range(jobs):
        clock.advance(seconds_per_job)
        tracker.record(status)


class TestCounting:
    def test_every_terminal_status_advances_progress(self, tracker, clock):
        tracker.start(5)
        for status in (
            JobStatus.SUCCESS,
            JobStatus.WARNING,
            JobStatus.FAILED,
            JobStatus.SKIPPED,
            JobStatus.CANCELLED,
        ):
            clock.advance(1.0)
            tracker.record(status)

        snapshot = tracker.snapshot()
        # The rule that keeps a bar from stalling on a batch of bad files: a
        # failed scan is a finished scan.
        assert snapshot.completed == 5
        assert snapshot.fraction == 1.0

    def test_the_counters_reconcile_with_the_completed_total(self, tracker, clock):
        tracker.start(10)
        for status, count in (
            (JobStatus.SUCCESS, 6),
            (JobStatus.WARNING, 2),
            (JobStatus.FAILED, 1),
            (JobStatus.SKIPPED, 1),
        ):
            clock.advance(0.1)
            tracker.record(status, count)

        snapshot = tracker.snapshot()
        assert (
            snapshot.successful + snapshot.warnings + snapshot.failed + snapshot.skipped
            == snapshot.completed
        )
        assert snapshot.completed == 10
        assert snapshot.remaining == 0

    def test_percentage_follows_the_exact_counts(self, tracker, clock):
        tracker.start(8)
        run_at(tracker, clock, jobs=3, seconds_per_job=0.1)
        assert tracker.snapshot().percent == pytest.approx(37.5)

    def test_one_job_short_is_not_one_hundred_percent(self, tracker):
        tracker.start(10_000)
        tracker.record(JobStatus.SUCCESS, 9_999)
        snapshot = tracker.snapshot()
        # 99.99%, and a display rounding that to "100%" would be lying about
        # the last sheet.
        assert snapshot.fraction < 1.0
        assert f"{snapshot.percent:.1f}" == "100.0"  # ...which is why the bar
        assert snapshot.completed != snapshot.total  # ...must use the counts

    def test_over_counting_cannot_drive_remaining_negative(self, tracker):
        tracker.start(2)
        tracker.record(JobStatus.SUCCESS, 5)
        assert tracker.snapshot().remaining == 0
        assert tracker.snapshot().fraction == 1.0

    def test_a_snapshot_is_a_value_not_a_live_view(self, tracker):
        tracker.start(4)
        tracker.record(JobStatus.SUCCESS)
        before = tracker.snapshot()
        tracker.record(JobStatus.SUCCESS)
        assert before.completed == 1
        assert tracker.snapshot().completed == 2


class TestWarmUp:
    def test_no_estimate_is_offered_from_the_first_scan(self, tracker, clock):
        tracker.start(10_000)
        clock.advance(3.0)
        tracker.record(JobStatus.SUCCESS)
        # The failure this prevents: "1 / 10,000 - remaining 27 hours",
        # replaced ten seconds later by "34 minutes".
        assert tracker.snapshot().eta_seconds is None
        assert tracker.snapshot().has_eta is False

    def test_an_estimate_appears_once_enough_scans_have_finished(self, tracker, clock):
        tracker.start(1_000)
        run_at(tracker, clock, jobs=WARMUP_JOBS, seconds_per_job=0.2)
        assert tracker.snapshot().has_eta is True

    def test_a_slow_small_batch_eventually_gets_an_estimate(self, tracker, clock):
        # Fewer completions than the warm-up count, but spread over enough
        # time to be a real measurement.
        tracker.start(20)
        run_at(tracker, clock, jobs=4, seconds_per_job=2.0)
        assert tracker.snapshot().has_eta is True

    def test_a_batch_shorter_than_one_sample_still_reports_a_speed(self, tracker, clock):
        """A twelve-sheet run can finish inside half a second.

        With no completed sample window there is no moving average, and an
        estimator that gave up there would report "Speed --" for the whole of
        every small batch. The average since the start is the evidence that
        exists, and it is used.
        """
        tracker.start(12)
        run_at(tracker, clock, jobs=12, seconds_per_job=0.02)

        snapshot = tracker.snapshot()
        assert snapshot.rate == pytest.approx(50.0, rel=0.1)
        assert snapshot.has_eta is True  # twelve completions is past the warm-up

    def test_two_completions_are_never_enough(self, tracker, clock):
        tracker.start(500)
        run_at(tracker, clock, jobs=2, seconds_per_job=10.0)
        assert tracker.snapshot().has_eta is False

    def test_a_slow_first_scan_does_not_dominate_the_estimate(self, clock):
        """The multicore start-up case: worker pools import NumPy and OpenCV.

        The first sheet can take ten times as long as the rest. An estimator
        that believed it would promise a batch four times longer than the real
        one, and would keep promising it for several minutes.
        """
        tracker = BatchProgressTracker(clock=clock, wall_clock=lambda: 0.0)
        tracker.start(1_000)

        clock.advance(8.0)  # pool start-up, first sheet
        tracker.record(JobStatus.SUCCESS)
        run_at(tracker, clock, jobs=29, seconds_per_job=0.2)

        eta = tracker.snapshot().eta_seconds
        assert eta is not None
        # 970 sheets at 5/sec is ~194s. A naive average including the 8-second
        # first sheet would say ~450s.
        assert 150.0 < eta < 260.0


class TestEstimateAccuracy:
    def test_a_steady_rate_converges_on_the_right_answer(self, tracker, clock):
        tracker.start(1_000)
        run_at(tracker, clock, jobs=200, seconds_per_job=0.25)  # 4 per second

        snapshot = tracker.snapshot()
        assert snapshot.rate == pytest.approx(4.0, rel=0.05)
        # 800 remaining at 4/sec = 200 seconds.
        assert snapshot.eta_seconds == pytest.approx(200.0, rel=0.1)

    def test_it_smooths_a_short_fluctuation(self, tracker, clock):
        tracker.start(1_000)
        run_at(tracker, clock, jobs=100, seconds_per_job=0.2)  # 5/sec
        steady = tracker.snapshot().rate

        # One awkward sheet that takes five times as long as its neighbours.
        run_at(tracker, clock, jobs=1, seconds_per_job=1.0)
        after = tracker.snapshot().rate

        assert after < steady  # it noticed...
        assert after > steady * 0.6  # ...but one sheet did not rewrite the estimate

    def test_a_sustained_slowdown_is_believed(self, tracker, clock):
        """The other half of the trade-off: smoothing is not stubbornness.

        Several seconds at a fifth of the pace is not a blip, and an estimator
        that still reported the old speed would be promising a finish time it
        had already stopped believing.
        """
        tracker.start(1_000)
        run_at(tracker, clock, jobs=100, seconds_per_job=0.2)  # 5/sec
        run_at(tracker, clock, jobs=5, seconds_per_job=1.0)  # 1/sec, 5 seconds

        assert tracker.snapshot().rate == pytest.approx(1.0, abs=1.0)

    def test_it_follows_a_sustained_change_in_speed(self, tracker, clock):
        tracker.start(2_000)
        run_at(tracker, clock, jobs=100, seconds_per_job=0.5)  # 2/sec
        run_at(tracker, clock, jobs=300, seconds_per_job=0.1)  # 10/sec

        # Within a few seconds of sustained change the estimate has moved most
        # of the way; it is a moving average, not a memory.
        assert tracker.snapshot().rate == pytest.approx(10.0, rel=0.25)

    def test_the_estimate_shrinks_as_the_batch_proceeds(self, tracker, clock):
        tracker.start(400)
        run_at(tracker, clock, jobs=100, seconds_per_job=0.1)
        early = tracker.snapshot().eta_seconds
        run_at(tracker, clock, jobs=200, seconds_per_job=0.1)
        late = tracker.snapshot().eta_seconds

        assert early is not None and late is not None
        assert late < early


class TestStallsAndEdges:
    def test_a_long_pause_does_not_divide_by_zero(self, tracker, clock):
        tracker.start(100)
        run_at(tracker, clock, jobs=20, seconds_per_job=0.1)
        clock.advance(600.0)  # nothing finishes for ten minutes

        snapshot = tracker.snapshot()
        assert snapshot.rate >= 0.0
        # Either a much longer estimate or none at all; never a crash, never a
        # number that pretends the stall did not happen.
        assert snapshot.eta_seconds is None or snapshot.eta_seconds > 0.0

    def test_a_stall_makes_the_estimate_grow_rather_than_freeze(self, tracker, clock):
        tracker.start(1_000)
        run_at(tracker, clock, jobs=100, seconds_per_job=0.1)
        before = tracker.snapshot().eta_seconds
        clock.advance(30.0)
        after = tracker.snapshot().eta_seconds

        assert before is not None
        assert after is None or after > before

    def test_an_empty_batch_has_no_estimate_and_no_division(self, tracker):
        tracker.start(0)
        snapshot = tracker.snapshot()
        assert snapshot.total == 0
        assert snapshot.fraction == 0.0
        assert snapshot.eta_seconds is None
        assert snapshot.percent == 0.0

    def test_a_single_scan_batch_completes_cleanly(self, tracker, clock):
        tracker.start(1)
        clock.advance(0.4)
        tracker.record(JobStatus.SUCCESS)
        tracker.finish()

        snapshot = tracker.snapshot()
        assert snapshot.fraction == 1.0
        assert snapshot.eta_seconds == 0.0

    def test_recording_before_start_still_counts(self, tracker):
        # A caller bug, but losing the count would be a worse answer than
        # adopting a start time.
        tracker.record(JobStatus.SUCCESS)
        assert tracker.snapshot().completed == 1

    def test_a_backwards_clock_cannot_produce_negative_time(self, clock):
        tracker = BatchProgressTracker(clock=clock, wall_clock=lambda: 0.0)
        tracker.start(10)
        clock.now -= 50.0  # a system clock correction, mid-batch
        assert tracker.snapshot().elapsed_seconds >= 0.0


class TestLifecycle:
    def test_preparing_is_its_own_state(self, tracker):
        tracker.prepare()
        snapshot = tracker.snapshot()
        assert snapshot.state is BatchState.PREPARING
        assert snapshot.is_running is True
        assert snapshot.eta_seconds is None

    def test_completion_reports_zero_remaining(self, tracker, clock):
        tracker.start(50)
        run_at(tracker, clock, jobs=50, seconds_per_job=0.1)
        tracker.finish()

        snapshot = tracker.snapshot()
        assert snapshot.state is BatchState.COMPLETED
        assert snapshot.eta_seconds == 0.0
        assert snapshot.fraction == 1.0
        assert snapshot.is_finished is True

    def test_cancelling_withdraws_the_estimate(self, tracker, clock):
        tracker.start(1_000)
        run_at(tracker, clock, jobs=100, seconds_per_job=0.1)
        assert tracker.snapshot().has_eta is True

        tracker.request_cancel()
        snapshot = tracker.snapshot()
        assert snapshot.state is BatchState.CANCELLING
        # "Remaining: 3 minutes" is untrue once the answer is "as long as the
        # sheets in flight take".
        assert snapshot.eta_seconds is None

    def test_a_cancelled_batch_keeps_what_it_finished(self, tracker, clock):
        tracker.start(1_000)
        run_at(tracker, clock, jobs=120, seconds_per_job=0.1)
        tracker.request_cancel()
        tracker.finish(cancelled=True)

        snapshot = tracker.snapshot()
        assert snapshot.state is BatchState.CANCELLED
        assert snapshot.completed == 120
        assert snapshot.remaining == 880

    def test_cancelling_a_finished_batch_changes_nothing(self, tracker, clock):
        tracker.start(2)
        run_at(tracker, clock, jobs=2, seconds_per_job=0.1)
        tracker.finish()
        tracker.request_cancel()
        assert tracker.snapshot().state is BatchState.COMPLETED

    def test_a_failed_batch_is_distinguishable_from_a_cancelled_one(self, tracker):
        tracker.start(10)
        tracker.fail()
        snapshot = tracker.snapshot()
        assert snapshot.state is BatchState.FAILED
        assert snapshot.eta_seconds is None

    def test_elapsed_time_freezes_when_the_batch_ends(self, tracker, clock):
        tracker.start(4)
        run_at(tracker, clock, jobs=4, seconds_per_job=0.5)
        tracker.finish()
        at_finish = tracker.snapshot().elapsed_seconds
        clock.advance(300.0)
        assert tracker.snapshot().elapsed_seconds == pytest.approx(at_finish)

    def test_a_finished_batch_reports_its_average_speed(self, tracker, clock):
        tracker.start(100)
        run_at(tracker, clock, jobs=100, seconds_per_job=0.2)
        tracker.finish()
        snapshot = tracker.snapshot()
        assert snapshot.average_rate == pytest.approx(5.0, rel=0.01)
        assert snapshot.rate == pytest.approx(5.0, rel=0.01)

    def test_starting_again_forgets_the_previous_run(self, tracker, clock):
        tracker.start(10)
        run_at(tracker, clock, jobs=10, seconds_per_job=0.1)
        tracker.finish()

        tracker.start(5)
        snapshot = tracker.snapshot()
        assert snapshot.completed == 0
        assert snapshot.total == 5
        assert snapshot.state is BatchState.PROCESSING

    def test_the_finish_time_is_offered_once_the_estimate_is(self, clock):
        tracker = BatchProgressTracker(clock=clock, wall_clock=lambda: 1_700_000_000.0)
        tracker.start(1_000)
        run_at(tracker, clock, jobs=100, seconds_per_job=0.1)

        snapshot = tracker.snapshot()
        assert snapshot.finish_wall_clock is not None
        assert snapshot.finish_wall_clock == pytest.approx(
            1_700_000_000.0 + snapshot.eta_seconds
        )


class TestConcurrency:
    def test_counts_stay_exact_when_many_threads_finish_at_once(self, tracker):
        """Eight workers finishing simultaneously must not lose a count.

        The reason completions are reported to one tracker under one lock
        rather than incremented by each worker: ``n += 1`` from several
        threads loses increments, and a progress bar that loses increments
        never reaches the end.
        """
        tracker.start(8_000)
        threads = [
            threading.Thread(
                target=lambda: [tracker.record(JobStatus.SUCCESS) for _ in range(1_000)]
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = tracker.snapshot()
        assert snapshot.successful == 8_000
        assert snapshot.completed == 8_000
        assert snapshot.fraction == 1.0

    def test_snapshots_taken_during_the_storm_are_internally_consistent(self, tracker):
        tracker.start(4_000)
        snapshots: list[ProgressSnapshot] = []
        stop = threading.Event()

        def observe() -> None:
            while not stop.is_set():
                snapshots.append(tracker.snapshot())

        watcher = threading.Thread(target=observe)
        watcher.start()
        try:
            for _ in range(4_000):
                tracker.record(JobStatus.SUCCESS)
        finally:
            stop.set()
            watcher.join()

        assert snapshots
        for snapshot in snapshots:
            # Never more completed than the batch holds, and the parts always
            # add up to the whole.
            assert snapshot.completed <= snapshot.total
            assert (
                snapshot.successful + snapshot.warnings + snapshot.failed
                + snapshot.skipped + snapshot.cancelled
                == snapshot.completed
            )


class TestTenThousandJobs:
    """The scale this whole exercise exists for, without ten thousand images."""

    def test_a_ten_thousand_job_batch_reaches_exactly_one_hundred_percent(
        self, tracker, clock
    ):
        total = 10_000
        tracker.start(total, workers=12)

        successful = warnings = failed = 0
        for index in range(total):
            clock.advance(0.05)
            # A realistic mixture: most read cleanly, a few need review, a
            # handful are unreadable - and every one of them advances the bar.
            if index % 250 == 0:
                tracker.record(JobStatus.FAILED)
                failed += 1
            elif index % 37 == 0:
                tracker.record(JobStatus.WARNING)
                warnings += 1
            else:
                tracker.record(JobStatus.SUCCESS)
                successful += 1
        tracker.finish()

        snapshot = tracker.snapshot()
        assert snapshot.completed == total
        assert snapshot.total == total
        assert snapshot.fraction == 1.0
        assert snapshot.percent == 100.0
        assert snapshot.remaining == 0
        assert snapshot.eta_seconds == 0.0
        assert (snapshot.successful, snapshot.warnings, snapshot.failed) == (
            successful,
            warnings,
            failed,
        )
        assert snapshot.successful + snapshot.warnings + snapshot.failed == total
        assert snapshot.workers == 12
        assert snapshot.average_rate == pytest.approx(20.0, rel=0.01)

    def test_the_estimate_stays_sane_throughout_a_ten_thousand_job_batch(
        self, tracker, clock
    ):
        total = 10_000
        tracker.start(total)
        estimates: list[float] = []

        for index in range(total):
            clock.advance(0.05)
            tracker.record(JobStatus.SUCCESS)
            if index % 500 == 0:
                eta = tracker.snapshot().eta_seconds
                if eta is not None:
                    estimates.append(eta)

        assert estimates
        # A steady 20/sec run: the estimate should track remaining/20 all the
        # way down, and never wander into nonsense.
        assert estimates == sorted(estimates, reverse=True)
        assert max(estimates) < 600.0
        assert min(estimates) >= 0.0

    def test_tracking_ten_thousand_jobs_costs_no_memory_per_job(self, tracker, clock):
        # The tracker holds counters, not a record per job: whatever it costs
        # for ten jobs, it costs for ten thousand.
        import sys

        tracker.start(10_000)
        for _ in range(10_000):
            clock.advance(0.01)
            tracker.record(JobStatus.SUCCESS)

        # Five integer counters and a handful of floats, regardless of scale.
        assert sys.getsizeof(tracker.__dict__) < 2_000
        assert tracker.snapshot().completed == 10_000


class TestFormatting:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "00:00:00"),
            (8, "00:00:08"),
            (84, "00:01:24"),
            (768, "00:12:48"),
            (4363, "01:12:43"),
            (98_240, "1d 03:17:20"),
            (-5, "00:00:00"),
        ],
    )
    def test_durations_use_one_consistent_style(self, seconds, expected):
        assert format_duration(seconds) == expected

    def test_an_absent_duration_reads_as_absent(self):
        assert format_duration(None) == "--:--"

    @pytest.mark.parametrize(
        ("rate", "expected"),
        [
            (5.65, "5.7 scans/sec"),
            (12.3, "12 scans/sec"),
            (1.0, "1.0 scans/sec"),
            (0.42, "2.4 sec/scan"),
            (0.0, "--"),
        ],
    )
    def test_rates_avoid_spurious_precision(self, rate, expected):
        # Never "ETA: 4862.772837 sec" and never "0.42 scans/sec", which makes
        # the reader do the division themselves.
        assert format_rate(rate) == expected

    def test_large_counts_are_readable(self):
        assert format_count(10_000) == "10,000"
        assert format_count(6_342) == "6,342"
        assert format_count(0) == "0"


class TestConstruction:
    @pytest.mark.parametrize("smoothing", [0.0, -0.1, 1.5])
    def test_an_impossible_smoothing_factor_is_refused(self, smoothing):
        with pytest.raises(ValueError, match="smoothing"):
            BatchProgressTracker(smoothing=smoothing)

    def test_a_non_positive_sample_interval_is_refused(self):
        with pytest.raises(ValueError, match="sample_interval"):
            BatchProgressTracker(sample_interval=0.0)

    def test_the_default_smoothing_is_documented_and_sane(self):
        assert 0.0 < DEFAULT_SMOOTHING < 1.0

    def test_an_idle_tracker_reports_nothing_alarming(self):
        snapshot = BatchProgressTracker().snapshot()
        assert snapshot.state is BatchState.IDLE
        assert snapshot.completed == 0
        assert snapshot.eta_seconds is None
        assert snapshot.elapsed_seconds == 0.0
        assert snapshot.is_running is False
