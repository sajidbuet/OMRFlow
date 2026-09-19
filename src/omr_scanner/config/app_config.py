"""The per-user application configuration document.

Purpose:
    Define, load and save ``omrflow.config.json`` - the small set of settings
    that follow the *user*, not the examination: log verbosity, the list of
    recently opened projects and the folder new projects default to.

Responsibilities:
    * Validate the document with Pydantic so a hand-edited file fails loudly.
    * Survive a damaged configuration file: a broken preferences file must never
      stop the application from starting.

What does NOT belong here:
    * Project or template settings (see :mod:`omr_scanner.config`).
    * Recognition thresholds. Those are tuned per sheet design and therefore
      live in the template.

Invariants:
    * ``config_version`` is bumped whenever a field is renamed or removed, and
      the migration is documented in ``docs/DEVELOPMENT_GUIDE.md``.
    * :class:`AppConfig` is immutable; "changing" a setting produces a new
      instance, which keeps configuration out of the global mutable state the
      project forbids.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omr_scanner.config.paths import app_config_file
from omr_scanner.config.processing import ProcessingSettings
from omr_scanner.errors import ConfigurationError
from omr_scanner.utils.json_io import read_json, write_json_atomic

logger = logging.getLogger(__name__)

CONFIG_VERSION = 1
"""Current application-configuration format version."""

DEFAULT_MAX_RECENT_PROJECTS = 10

LogLevelName = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class AppConfig(BaseModel):
    """User-level application preferences.

    Attributes:
        config_version: Format version of the stored document.
        log_level: Threshold applied to the root logger at startup.
        recent_projects: Most recently opened project directories, newest first.
        default_projects_root: Folder the "New project" dialog starts in.
        max_recent_projects: Upper bound on ``recent_projects``.
        processing: How many CPU workers batch recognition may use. A machine
            property rather than an examination property, which is why it
            belongs to the per-user document and not to a project or a template.
        reviewer_name: Who is sitting at this machine, recorded against every
            conflict resolution they make (Phase 6). Remembered here rather
            than asked for on every correction, because a reviewer works
            through hundreds of them in a sitting.

            Deliberately a name and not an account: Phase 6 requires
            *attribution*, not authentication, and the field is shaped so that
            a real identity system could later supply it without changing
            anything that reads it. An empty value is normal - it simply means
            nobody has said who they are yet, and
            :func:`~omr_scanner.services.review_store.validate_reviewer`
            refuses corrections until somebody does.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_version: int = CONFIG_VERSION
    log_level: LogLevelName = "INFO"
    recent_projects: tuple[Path, ...] = ()
    default_projects_root: Path | None = None
    max_recent_projects: int = Field(default=DEFAULT_MAX_RECENT_PROJECTS, ge=1, le=50)
    processing: ProcessingSettings = ProcessingSettings()
    reviewer_name: str = ""

    def log_level_value(self) -> int:
        """Return :attr:`log_level` as a :mod:`logging` numeric level."""
        return logging.getLevelNamesMapping()[self.log_level]

    def with_recent_project(self, project_dir: Path) -> AppConfig:
        """Return a copy with ``project_dir`` promoted to the head of the list.

        Existing entries for the same directory are removed first, so the list
        holds no duplicates and the most recent project is always index 0.

        Args:
            project_dir: Directory of a project that was just created or opened.

        Returns:
            A new :class:`AppConfig`; the receiver is unchanged.
        """
        resolved = project_dir.resolve()
        remaining = [path for path in self.recent_projects if path != resolved]
        trimmed = tuple([resolved, *remaining][: self.max_recent_projects])
        return self.model_copy(
            update={"recent_projects": trimmed, "default_projects_root": resolved.parent}
        )

    def without_recent_project(self, project_dir: Path) -> AppConfig:
        """Return a copy with ``project_dir`` removed from the recent list.

        Used when a remembered project has been moved or deleted on disk.
        """
        resolved = project_dir.resolve()
        remaining = tuple(path for path in self.recent_projects if path != resolved)
        return self.model_copy(update={"recent_projects": remaining})

    def with_processing(self, processing: ProcessingSettings) -> AppConfig:
        """Return a copy carrying ``processing``; the receiver is unchanged."""
        return self.model_copy(update={"processing": processing})

    def with_reviewer_name(self, name: str) -> AppConfig:
        """Return a copy remembering who is reviewing; the receiver is unchanged.

        Whitespace is trimmed, because a name of three spaces would satisfy a
        non-empty check while naming nobody.
        """
        return self.model_copy(update={"reviewer_name": name.strip()})


def load_app_config(path: Path | None = None, *, strict: bool = False) -> AppConfig:
    """Load the application configuration.

    Args:
        path: Configuration file. Defaults to
            :func:`omr_scanner.config.paths.app_config_file`.
        strict: When ``True``, a missing, unreadable or invalid file raises
            instead of falling back to defaults. Used by tests and by any future
            command line tooling where silence would hide a real problem.

    Returns:
        The stored configuration, or :class:`AppConfig` defaults when the file is
        absent or unusable and ``strict`` is ``False``.

    Raises:
        ConfigurationError: ``strict`` is ``True`` and the file is missing,
            unreadable or invalid.
    """
    config_path = path if path is not None else app_config_file()

    if not config_path.is_file():
        if strict:
            raise ConfigurationError(
                f"Configuration file not found: {config_path}",
                user_message="The OMRFlow settings file could not be found.",
            )
        logger.debug("No configuration file at %s; using defaults", config_path)
        return AppConfig()

    try:
        payload = read_json(config_path)
        return AppConfig.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        if strict:
            raise ConfigurationError(
                f"Invalid configuration file {config_path}: {exc}",
                user_message="The OMRFlow settings file is damaged and could not be read.",
            ) from exc
        # A damaged preferences file is never a reason to refuse to start; the
        # user loses preferences, not data.
        logger.warning("Ignoring damaged configuration file %s (%s)", config_path, exc)
        return AppConfig()


def save_app_config(config: AppConfig, path: Path | None = None) -> Path:
    """Persist the application configuration.

    Args:
        config: Configuration to store.
        path: Destination file. Defaults to
            :func:`omr_scanner.config.paths.app_config_file`.

    Returns:
        The path written.

    Raises:
        ConfigurationError: The file could not be written.
    """
    config_path = path if path is not None else app_config_file()
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(config_path, config.model_dump(mode="json"))
    except OSError as exc:
        raise ConfigurationError(
            f"Could not write configuration file {config_path}: {exc}",
            user_message="OMRFlow could not save your settings.",
        ) from exc
    logger.debug("Configuration saved to %s", config_path)
    return config_path
