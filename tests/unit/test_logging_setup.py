"""Tests for logging configuration and the per-project log handler."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from omr_scanner.utils.logging_setup import attach_log_file, configure_logging, detach_log_file


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """Leave the root logger exactly as it was; other tests rely on caplog."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in saved_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(saved_level)


def _flush_root_handlers() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_configure_logging_writes_to_the_requested_file(tmp_path: Path):
    log_file = tmp_path / "logs" / "omrflow.log"

    configure_logging(level=logging.INFO, log_file=log_file)
    logging.getLogger("omr_scanner.test").info("startup complete")
    _flush_root_handlers()

    assert log_file.is_file()
    assert "startup complete" in log_file.read_text(encoding="utf-8")


def test_repeated_configuration_does_not_duplicate_handlers(tmp_path: Path):
    log_file = tmp_path / "omrflow.log"

    configure_logging(level=logging.INFO, log_file=log_file)
    first_count = len(logging.getLogger().handlers)
    configure_logging(level=logging.INFO, log_file=log_file)

    assert len(logging.getLogger().handlers) == first_count


def test_project_log_handler_can_be_attached_and_detached(tmp_path: Path):
    project_log = tmp_path / "project" / "logs" / "project.log"
    configure_logging(level=logging.INFO)

    handler = attach_log_file(project_log)
    logging.getLogger("omr_scanner.test").info("inside the project")
    detach_log_file(handler)
    logging.getLogger("omr_scanner.test").info("after closing the project")
    _flush_root_handlers()

    contents = project_log.read_text(encoding="utf-8")
    assert "inside the project" in contents
    assert "after closing the project" not in contents


def test_detached_handler_is_removed_from_the_root_logger(tmp_path: Path):
    configure_logging(level=logging.INFO)
    handler = attach_log_file(tmp_path / "project.log")

    assert handler in logging.getLogger().handlers
    detach_log_file(handler)
    assert handler not in logging.getLogger().handlers
