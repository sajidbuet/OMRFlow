"""Tests for application configuration loading, saving and the recent list."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from omr_scanner.config import AppConfig, load_app_config, save_app_config
from omr_scanner.config.app_config import CONFIG_VERSION
from omr_scanner.config.paths import app_config_file, user_config_dir, user_log_dir
from omr_scanner.errors import ConfigurationError


def test_defaults_are_usable_without_a_file(tmp_path: Path):
    config = load_app_config(tmp_path / "missing.json")

    assert config.config_version == CONFIG_VERSION
    assert config.log_level == "INFO"
    assert config.recent_projects == ()
    assert config.log_level_value() == logging.INFO


def test_save_then_load_round_trip(tmp_path: Path):
    config_path = tmp_path / "config" / "omrflow.config.json"
    original = AppConfig(log_level="DEBUG", default_projects_root=tmp_path / "projects")

    save_app_config(original, config_path)
    reloaded = load_app_config(config_path)

    assert reloaded == original
    assert reloaded.log_level_value() == logging.DEBUG


def test_damaged_file_falls_back_to_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    config_path = tmp_path / "omrflow.config.json"
    config_path.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        config = load_app_config(config_path)

    assert config == AppConfig()
    assert "damaged configuration file" in caplog.text


def test_damaged_file_raises_in_strict_mode(tmp_path: Path):
    config_path = tmp_path / "omrflow.config.json"
    config_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_app_config(config_path, strict=True)


def test_unknown_key_is_rejected(tmp_path: Path):
    config_path = tmp_path / "omrflow.config.json"
    config_path.write_text(json.dumps({"unexpected_setting": 1}), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_app_config(config_path, strict=True)


def test_recent_projects_are_unique_and_newest_first(tmp_path: Path):
    first = tmp_path / "exam_one"
    second = tmp_path / "exam_two"
    first.mkdir()
    second.mkdir()

    config = AppConfig().with_recent_project(first).with_recent_project(second)
    config = config.with_recent_project(first)

    assert config.recent_projects == (first.resolve(), second.resolve())
    assert config.default_projects_root == first.resolve().parent


def test_recent_projects_are_capped(tmp_path: Path):
    config = AppConfig(max_recent_projects=3)

    for index in range(5):
        config = config.with_recent_project(tmp_path / f"exam_{index}")

    assert len(config.recent_projects) == 3
    assert config.recent_projects[0] == (tmp_path / "exam_4").resolve()


def test_forgetting_a_project_removes_only_that_entry(tmp_path: Path):
    config = AppConfig().with_recent_project(tmp_path / "a").with_recent_project(tmp_path / "b")

    config = config.without_recent_project(tmp_path / "a")

    assert config.recent_projects == ((tmp_path / "b").resolve(),)


def test_config_is_immutable():
    config = AppConfig()

    with pytest.raises(ValidationError):
        config.log_level = "DEBUG"  # type: ignore[misc]


def test_environment_override_directs_paths_into_the_test_sandbox(tmp_path: Path):
    """The autouse fixture in conftest must keep real user directories untouched."""
    assert user_config_dir() == (tmp_path / "user_config").resolve()
    assert user_log_dir() == (tmp_path / "user_logs").resolve()
    assert app_config_file().parent == user_config_dir()
