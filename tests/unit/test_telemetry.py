"""Unit tests for batch-run telemetry sampling (Phase 10, §24/§25)."""

from __future__ import annotations

import time
from pathlib import Path

from omr_scanner.services import telemetry


def _busy_child(stop_after: float) -> None:
    """Burns real CPU for `stop_after` seconds.

    Module-level: a `spawn`-context child process target must be picklable,
    which a nested test function is not.
    """
    deadline = time.monotonic() + stop_after
    total = 0
    while time.monotonic() < deadline:
        total += 1
    del total


class TestTelemetryRecorder:
    def test_stop_takes_a_final_sample_even_with_a_long_interval(self, tmp_path: Path) -> None:
        output = tmp_path / "telemetry.jsonl"
        progress = {"completed": 0, "pending": 10, "failed": 0}
        recorder = telemetry.TelemetryRecorder(
            output,
            lambda: (progress["completed"], progress["pending"], progress["failed"]),
            interval_seconds=999,
        )
        recorder.start()
        progress["completed"] = 5
        progress["pending"] = 5
        recorder.stop()

        samples = telemetry.read_samples(output)
        assert len(samples) == 1
        assert samples[0].sheets_completed == 5
        assert samples[0].sheets_pending == 5

    def test_samples_accumulate_as_json_lines(self, tmp_path: Path) -> None:
        output = tmp_path / "telemetry.jsonl"
        progress = {"completed": 0, "pending": 10, "failed": 0}
        recorder = telemetry.TelemetryRecorder(
            output,
            lambda: (progress["completed"], progress["pending"], progress["failed"]),
            interval_seconds=0.05,
        )
        recorder.start()
        time.sleep(0.2)
        progress["completed"] = 3
        time.sleep(0.2)
        recorder.stop()

        samples = telemetry.read_samples(output)
        assert len(samples) >= 2
        assert samples[-1].sheets_completed == 3

    def test_a_malformed_trailing_line_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        output = tmp_path / "telemetry.jsonl"
        output.write_text('{"elapsed_seconds": 1.0, "sheets_completed": 1, '
                           '"sheets_pending": 0, "sheets_failed": 0, "sheets_per_second": 1.0, '
                           '"process_cpu_percent": 0.0, "system_cpu_percent": 0.0, '
                           '"process_memory_mb": 0.0, "system_available_memory_mb": 0.0, '
                           '"database_size_mb": 0.0, "disk_free_mb": 0.0, "worker_count": 1}\n'
                           '{"incomplete json line was cut off mid-write')
        samples = telemetry.read_samples(output)
        assert len(samples) == 1

    def test_reading_a_missing_file_returns_no_samples(self, tmp_path: Path) -> None:
        assert telemetry.read_samples(tmp_path / "does_not_exist.jsonl") == []

    def test_a_sample_from_before_worker_pool_telemetry_still_reads_back(
        self, tmp_path: Path
    ) -> None:
        """A telemetry file from before the worker-pool fields existed.

        Regression coverage for a real gap this phase's own validation
        found: `process_cpu_percent`/`process_memory_mb` only ever measured
        this process, never the worker pool where actual recognition CPU
        time is spent - a 10,000-sheet benchmark run showed `system_cpu_percent`
        far exceeding `process_cpu_percent` for exactly this reason. The new
        `worker_pool_*` fields must default rather than make old files
        unreadable (`read_samples` treats a `TypeError` as "malformed line,
        skip it" - a missing-but-defaulted field must not trigger that).
        """
        output = tmp_path / "telemetry.jsonl"
        output.write_text(
            '{"elapsed_seconds": 1.0, "sheets_completed": 1, '
            '"sheets_pending": 0, "sheets_failed": 0, "sheets_per_second": 1.0, '
            '"process_cpu_percent": 0.0, "system_cpu_percent": 0.0, '
            '"process_memory_mb": 0.0, "system_available_memory_mb": 0.0, '
            '"database_size_mb": 0.0, "disk_free_mb": 0.0, "worker_count": 1}\n'
        )
        samples = telemetry.read_samples(output)
        assert len(samples) == 1
        assert samples[0].worker_pool_cpu_percent == 0.0
        assert samples[0].worker_pool_process_count == 0

    def test_worker_pool_cpu_and_memory_are_aggregated_across_real_child_processes(
        self, tmp_path: Path
    ) -> None:
        """Spawns two real child processes and confirms their CPU/RSS is summed in.

        Proof the aggregation walks real OS process state, not a mocked or
        assumed value (this session's established standard: independently
        verify with real process identity, the same way worker recycling
        and orphan containment were verified elsewhere in this phase).
        """
        import multiprocessing

        context = multiprocessing.get_context("spawn")
        children = [context.Process(target=_busy_child, args=(3.0,)) for _ in range(2)]
        for child in children:
            child.start()
        try:
            output = tmp_path / "telemetry.jsonl"
            progress = {"completed": 0, "pending": 10, "failed": 0}
            recorder = telemetry.TelemetryRecorder(
                output,
                lambda: (progress["completed"], progress["pending"], progress["failed"]),
                worker_count=2,
                interval_seconds=0.3,
            )
            recorder.start()
            time.sleep(0.3)  # let the pool be discovered and primed
            time.sleep(1.5)  # a real measurement window
            recorder.stop()
        finally:
            for child in children:
                child.join(timeout=5)

        samples = telemetry.read_samples(output)
        assert any(sample.worker_pool_process_count >= 2 for sample in samples)
        assert any(sample.worker_pool_cpu_percent > 0.0 for sample in samples)
        assert any(sample.worker_pool_memory_mb > 0.0 for sample in samples)


class TestEstimateCompletion:
    def _sample(self, *, pending: int, rate: float) -> telemetry.TelemetrySample:
        return telemetry.TelemetrySample(
            elapsed_seconds=0.0,
            sheets_completed=0,
            sheets_pending=pending,
            sheets_failed=0,
            sheets_per_second=rate,
            process_cpu_percent=0.0,
            system_cpu_percent=0.0,
            process_memory_mb=0.0,
            system_available_memory_mb=0.0,
            database_size_mb=0.0,
            disk_free_mb=0.0,
            worker_count=1,
        )

    def test_returns_none_with_fewer_than_two_samples(self) -> None:
        assert telemetry.estimate_completion([]) is None
        assert telemetry.estimate_completion([self._sample(pending=10, rate=1.0)]) is None

    def test_returns_none_when_throughput_is_zero(self) -> None:
        samples = [self._sample(pending=10, rate=0.0), self._sample(pending=10, rate=0.0)]
        assert telemetry.estimate_completion(samples) is None

    def test_computes_seconds_remaining_from_rolling_rate(self) -> None:
        samples = [self._sample(pending=100, rate=10.0), self._sample(pending=90, rate=10.0)]
        eta = telemetry.estimate_completion(samples)
        assert eta == 9.0

    def test_zero_remaining_reports_zero_not_none(self) -> None:
        samples = [self._sample(pending=0, rate=5.0), self._sample(pending=0, rate=5.0)]
        assert telemetry.estimate_completion(samples) == 0.0

    def test_one_slow_sample_does_not_dominate_the_rolling_average(self) -> None:
        fast = [self._sample(pending=50, rate=10.0) for _ in range(11)]
        slow_then_fast = [*fast, self._sample(pending=50, rate=0.1)]
        eta = telemetry.estimate_completion(slow_then_fast)
        # With a 12-sample window the one slow sample is averaged in with
        # eleven fast ones, not allowed to dominate the estimate alone.
        assert eta is not None
        assert eta < 50 / 0.1
