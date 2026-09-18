"""Tests for how many CPU workers a batch is allowed to use.

The rules are pure arithmetic over "what mode did the user pick", "how many
logical CPUs are there" and "how many scans are in this batch", so every case -
including a one-core machine and a thirty-two-core one - is testable on any
machine by passing ``cpu_count`` explicitly. Nothing here starts a process.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omr_scanner.config.app_config import AppConfig, load_app_config, save_app_config
from omr_scanner.config.processing import (
    AUTOMATIC_WORKER_LIMIT,
    MAX_CONFIGURABLE_WORKERS,
    ProcessingMode,
    ProcessingSettings,
    detected_cpu_count,
)


class TestDefaults:
    def test_automatic_is_the_default_mode(self):
        assert ProcessingSettings().mode is ProcessingMode.AUTOMATIC

    def test_the_detected_cpu_count_is_at_least_one(self):
        assert detected_cpu_count() >= 1


class TestAutomaticMode:
    @pytest.mark.parametrize(
        ("cpus", "expected"),
        [
            (1, 1),  # a single-core machine still processes
            (2, 1),
            (4, 3),
            (8, 7),
            (16, min(15, AUTOMATIC_WORKER_LIMIT)),
            (32, min(31, AUTOMATIC_WORKER_LIMIT)),
        ],
    )
    def test_one_cpu_is_left_free_up_to_the_automatic_cap(self, cpus, expected):
        settings = ProcessingSettings(mode=ProcessingMode.AUTOMATIC)
        assert settings.configured_worker_count(cpus) == expected

    def test_a_large_machine_is_capped_rather_than_saturated(self):
        settings = ProcessingSettings(mode=ProcessingMode.AUTOMATIC)
        assert settings.configured_worker_count(128) == AUTOMATIC_WORKER_LIMIT

    def test_a_one_core_machine_never_asks_for_zero_workers(self):
        settings = ProcessingSettings(mode=ProcessingMode.AUTOMATIC)
        assert settings.configured_worker_count(1) == 1
        assert settings.resolve_worker_count(100, cpu_count=1) == 1


class TestSingleCoreMode:
    @pytest.mark.parametrize("cpus", [1, 2, 8, 64])
    def test_single_core_is_always_exactly_one_worker(self, cpus):
        settings = ProcessingSettings(mode=ProcessingMode.SINGLE_CORE, worker_count=16)
        assert settings.configured_worker_count(cpus) == 1
        assert settings.resolve_worker_count(500, cpu_count=cpus) == 1


class TestCustomMode:
    def test_the_chosen_number_is_used_as_given(self):
        settings = ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=6)
        assert settings.configured_worker_count(16) == 6

    def test_more_workers_than_cpus_is_clamped_to_the_machine(self):
        settings = ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=64)
        assert settings.configured_worker_count(8) == 8

    def test_a_worker_count_below_one_is_refused_by_the_model(self):
        with pytest.raises(ValidationError):
            ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=0)

    def test_an_absurd_worker_count_is_refused_by_the_model(self):
        with pytest.raises(ValidationError):
            ProcessingSettings(worker_count=MAX_CONFIGURABLE_WORKERS + 1)

    def test_with_worker_count_clamps_instead_of_raising(self):
        settings = ProcessingSettings()
        assert settings.with_worker_count(0).worker_count == 1
        assert (
            settings.with_worker_count(10_000).worker_count == MAX_CONFIGURABLE_WORKERS
        )

    def test_the_custom_number_survives_switching_modes(self):
        settings = ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=12)
        switched = settings.with_mode(ProcessingMode.SINGLE_CORE)
        assert switched.worker_count == 12
        assert switched.with_mode(ProcessingMode.CUSTOM).configured_worker_count(16) == 12


class TestBatchSize:
    def test_three_scans_never_start_more_than_three_workers(self):
        settings = ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=31)
        assert settings.resolve_worker_count(3, cpu_count=32) == 3

    def test_a_full_batch_uses_the_configured_count(self):
        settings = ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=4)
        assert settings.resolve_worker_count(100, cpu_count=16) == 4

    @pytest.mark.parametrize("items", [0, -1])
    def test_an_empty_batch_resolves_to_one_worker(self, items):
        assert ProcessingSettings().resolve_worker_count(items, cpu_count=16) == 1

    def test_one_scan_is_always_one_worker(self):
        settings = ProcessingSettings(mode=ProcessingMode.AUTOMATIC)
        assert settings.resolve_worker_count(1, cpu_count=32) == 1


class TestPersistence:
    def test_the_setting_round_trips_through_the_configuration_file(self, tmp_path):
        path = tmp_path / "omrflow.config.json"
        config = AppConfig().with_processing(
            ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=5)
        )
        save_app_config(config, path)

        loaded = load_app_config(path, strict=True)
        assert loaded.processing.mode is ProcessingMode.CUSTOM
        assert loaded.processing.worker_count == 5

    def test_a_configuration_written_before_this_feature_still_loads(self, tmp_path):
        # Every field is optional, so a file from an earlier version must load
        # and simply get the default - never fail validation.
        path = tmp_path / "omrflow.config.json"
        path.write_text('{"config_version": 1, "log_level": "INFO"}', encoding="utf-8")

        loaded = load_app_config(path, strict=True)
        assert loaded.processing == ProcessingSettings()

    def test_a_count_stored_on_a_bigger_machine_is_clamped_when_used(self, tmp_path):
        # The file travels with the user; the hardware does not. A 32-worker
        # setting on a 4-core laptop is clamped at use, not rejected at load.
        path = tmp_path / "omrflow.config.json"
        save_app_config(
            AppConfig().with_processing(
                ProcessingSettings(mode=ProcessingMode.CUSTOM, worker_count=32)
            ),
            path,
        )
        loaded = load_app_config(path, strict=True)
        assert loaded.processing.configured_worker_count(4) == 4

    def test_changing_processing_leaves_the_rest_of_the_configuration_alone(self):
        config = AppConfig(log_level="DEBUG", max_recent_projects=7)
        changed = config.with_processing(
            ProcessingSettings(mode=ProcessingMode.SINGLE_CORE)
        )
        assert changed.log_level == "DEBUG"
        assert changed.max_recent_projects == 7
        assert config.processing.mode is ProcessingMode.AUTOMATIC  # unchanged
