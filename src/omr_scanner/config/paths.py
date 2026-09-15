"""Resolution of per-user configuration and log directories.

Purpose:
    Decide *where* OMRFlow keeps its application configuration file and its
    application log, per operating system.

Responsibilities:
    * Provide the only platform-conditional code in the application core. Every
      other module asks this module instead of inspecting ``sys.platform``.
    * Honour the ``OMRFLOW_CONFIG_DIR`` / ``OMRFLOW_LOG_DIR`` environment
      overrides, which exist so that tests (and portable installations) never
      touch the real user profile.

What does NOT belong here:
    * Project directory layout; a project's internal paths are relative to the
      project root and are defined in ``omr_scanner.domain.project``.
    * Reading or writing files. This module only computes paths.

Invariants:
    * Returned paths are absolute. Directories are *not* created as a side
      effect of asking for them; callers create what they are about to write.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from omr_scanner import ORGANIZATION_NAME

ENV_CONFIG_DIR = "OMRFLOW_CONFIG_DIR"
"""Environment variable overriding the configuration directory."""

ENV_LOG_DIR = "OMRFLOW_LOG_DIR"
"""Environment variable overriding the application log directory."""

APP_CONFIG_FILENAME = "omrflow.config.json"
APP_LOG_FILENAME = "omrflow.log"

_XDG_CONFIG_HOME = "XDG_CONFIG_HOME"
_XDG_STATE_HOME = "XDG_STATE_HOME"


def user_config_dir() -> Path:
    """Return the directory holding this user's OMRFlow configuration file.

    Resolution order:
        1. ``OMRFLOW_CONFIG_DIR`` when set and non-empty.
        2. Windows: ``%APPDATA%/OMRFlow``.
        3. macOS: ``~/Library/Application Support/OMRFlow``.
        4. Otherwise: ``$XDG_CONFIG_HOME/omrflow`` or ``~/.config/omrflow``.

    Returns:
        An absolute path. The directory may not exist yet.
    """
    override = _path_from_env(ENV_CONFIG_DIR)
    if override is not None:
        return override

    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / ORGANIZATION_NAME
        return Path.home() / "AppData" / "Roaming" / ORGANIZATION_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / ORGANIZATION_NAME

    return _xdg_dir(_XDG_CONFIG_HOME, Path.home() / ".config")


def user_log_dir() -> Path:
    """Return the directory holding the application-wide log file.

    Project-specific logs live inside the project (``<project>/logs``); this is
    the log that covers startup, shutdown and anything happening while no
    project is open.

    Returns:
        An absolute path. The directory may not exist yet.
    """
    override = _path_from_env(ENV_LOG_DIR)
    if override is not None:
        return override

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / ORGANIZATION_NAME / "logs"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / ORGANIZATION_NAME

    return _xdg_dir(_XDG_STATE_HOME, Path.home() / ".local" / "state") / "logs"


def app_config_file() -> Path:
    """Return the absolute path of this user's application configuration file."""
    return user_config_dir() / APP_CONFIG_FILENAME


def app_log_file() -> Path:
    """Return the absolute path of the application-wide log file."""
    return user_log_dir() / APP_LOG_FILENAME


def _path_from_env(variable: str) -> Path | None:
    """Return an absolute path from ``variable``, or ``None`` when unset/empty."""
    raw = os.environ.get(variable, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _xdg_dir(variable: str, fallback: Path) -> Path:
    """Return ``$variable/omrflow`` following the XDG base directory spec."""
    raw = os.environ.get(variable, "").strip()
    base = Path(raw) if raw else fallback
    return base / ORGANIZATION_NAME.lower()
