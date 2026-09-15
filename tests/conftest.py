"""Shared pytest fixtures.

Purpose:
    Keep every test isolated from the machine it runs on. No test may read or
    write the real user configuration directory, the real log directory or any
    real project.

Key fixture:
    ``isolated_user_environment`` is autouse: it redirects the per-user
    configuration and log directories into a temporary folder for *every* test,
    including ones that never mention configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from omr_scanner.config.paths import ENV_CONFIG_DIR, ENV_LOG_DIR
from omr_scanner.services import ProjectSession, create_project

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
"""Repository root, derived from this file's location rather than the CWD."""

EXAMPLE_TEMPLATE = REPOSITORY_ROOT / "resources" / "templates" / "example_answer_sheet.omrt"


@pytest.fixture(autouse=True)
def isolated_user_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect per-user configuration and logs into the test's temp directory."""
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path / "user_config"))
    monkeypatch.setenv(ENV_LOG_DIR, str(tmp_path / "user_logs"))


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Return an empty directory that may contain project folders."""
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    return workspace_dir


@pytest.fixture
def project_session(workspace: Path) -> Iterator[ProjectSession]:
    """Create a project and yield its open session, closing it afterwards.

    Closing matters on Windows: an open SQLite handle prevents pytest from
    removing the temporary directory.
    """
    session = create_project(workspace, "Sample Examination")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def logging_sandbox() -> Iterator[None]:
    """Restore the root logger after a test that configures logging.

    Logging is process-global; without this, a test that installs file handlers
    would leak them into every test that runs afterwards.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)


@pytest.fixture
def example_template_path() -> Path:
    """Path of the illustrative template shipped in ``resources/templates``."""
    return EXAMPLE_TEMPLATE
