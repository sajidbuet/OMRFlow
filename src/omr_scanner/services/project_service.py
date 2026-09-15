"""Creating, validating and opening projects.

Purpose:
    The workflow behind the "New project" and "Open project" commands: build the
    directory layout, write ``project.json``, initialise the SQLite database and
    hand back an open :class:`ProjectSession`.

Responsibilities:
    * All file system side effects for a project.
    * Validation of an existing directory before anything is opened.
    * Lifetime of the project database connection and the project log handler.

What does NOT belong here:
    * Knowledge of *which* directories exist - that is
      :class:`omr_scanner.domain.project.ProjectDirectory`.
    * SQL. The service asks :mod:`omr_scanner.database` to open the file.
    * Dialogs or message boxes.

Failure modes (all raised as :class:`omr_scanner.errors.ProjectError` subclasses):
    * target directory already populated -> :class:`ProjectExistsError`;
    * missing/unreadable/invalid ``project.json`` -> :class:`ProjectValidationError`;
    * ``project.json`` newer than this build -> :class:`ProjectValidationError`;
    * missing database file -> :class:`ProjectValidationError`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from omr_scanner.database import ProjectDatabase, open_project_database
from omr_scanner.database.models import ProjectSetting, SettingKey
from omr_scanner.domain.project import (
    PROJECT_FORMAT_VERSION,
    Project,
    ProjectLayout,
    ProjectMetadata,
)
from omr_scanner.errors import ProjectExistsError, ProjectValidationError
from omr_scanner.utils.json_io import read_json, write_json_atomic
from omr_scanner.utils.logging_setup import attach_log_file, detach_log_file

logger = logging.getLogger(__name__)


class ProjectSession:
    """An open project together with the resources it holds.

    A session owns an open database connection pool and (optionally) a log
    handler writing into the project's ``logs`` directory. It must be closed, or
    used as a context manager, before the project folder is moved or deleted.

    Args:
        project: The project's metadata and layout.
        database: Open project database.
        log_handler: Handler added to the root logger for the project log, or
            ``None`` when project logging could not be started.
    """

    def __init__(
        self,
        project: Project,
        database: ProjectDatabase,
        log_handler: logging.Handler | None = None,
    ) -> None:
        self.project = project
        self.database = database
        self._log_handler = log_handler
        self._closed = False

    @property
    def name(self) -> str:
        """Display name of the open project."""
        return self.project.name

    @property
    def root(self) -> Path:
        """Absolute root directory of the open project."""
        return self.project.root

    @property
    def is_closed(self) -> bool:
        """Whether :meth:`close` has already run."""
        return self._closed

    def close(self) -> None:
        """Release the database connection and detach the project log handler.

        Safe to call more than once.
        """
        if self._closed:
            return
        self._closed = True
        logger.info("Closing project '%s'", self.project.name)
        if self._log_handler is not None:
            detach_log_file(self._log_handler)
            self._log_handler = None
        self.database.close()

    def __enter__(self) -> ProjectSession:
        """Enter a context manager that closes the session on exit."""
        return self

    def __exit__(self, *_exc_info: object) -> None:
        """Close the session when leaving the context."""
        self.close()

    def __repr__(self) -> str:
        """Return a debugging representation naming the project and its state."""
        state = "closed" if self._closed else "open"
        return f"ProjectSession({self.project.name!r}, {state})"


def create_project(
    parent_directory: Path,
    name: str,
    *,
    description: str = "",
    directory_name: str | None = None,
) -> ProjectSession:
    """Create a new project directory and open it.

    The project name is validated by :class:`ProjectMetadata` before anything is
    written, so an invalid name never leaves a half-built directory behind.

    Args:
        parent_directory: Folder that will contain the project directory.
        name: Display name of the examination.
        description: Optional free text stored in ``project.json``.
        directory_name: Folder name to use. Defaults to ``name``; supply it when
            the display name is unsuitable as a folder name.

    Returns:
        An open :class:`ProjectSession` for the new project.

    Raises:
        ProjectValidationError: ``name`` is not a usable project name, or the
            directory could not be created.
        ProjectExistsError: The target directory already exists and is not empty.
    """
    try:
        metadata = ProjectMetadata(name=name, description=description)
    except ValidationError as exc:
        raise ProjectValidationError(
            f"Invalid project metadata: {exc}",
            user_message="The project name is not valid. Avoid the characters < > : \" / \\ | ? *",
        ) from exc

    folder = directory_name if directory_name is not None else metadata.name
    layout = ProjectLayout(parent_directory / folder)

    if layout.root.exists() and any(layout.root.iterdir()):
        raise ProjectExistsError(
            f"Directory is not empty: {layout.root}",
            user_message=f"The folder '{layout.root.name}' already exists and is not empty.",
        )

    try:
        layout.root.mkdir(parents=True, exist_ok=True)
        for directory in layout.all_directories():
            directory.mkdir(exist_ok=True)
        write_json_atomic(layout.project_file, metadata.model_dump(mode="json"))
    except OSError as exc:
        raise ProjectValidationError(
            f"Could not create project at {layout.root}: {exc}",
            user_message=(
                "The project folder could not be created. "
                "Check the location and your permissions."
            ),
        ) from exc

    logger.info("Created project '%s' at %s", metadata.name, layout.root)

    database = open_project_database(layout.database_file, create=True)
    _store_identity(database, metadata)

    return _start_session(Project(metadata, layout), database)


def open_project(project_directory: Path) -> ProjectSession:
    """Open an existing project directory.

    Args:
        project_directory: Directory containing ``project.json``.

    Returns:
        An open :class:`ProjectSession`.

    Raises:
        ProjectValidationError: The directory is not a valid project.
        DatabaseError: The database exists but could not be opened or migrated.
        SchemaVersionError: The database was written by a newer build.
    """
    metadata = read_project_metadata(project_directory)
    layout = ProjectLayout(project_directory)

    if not layout.database_file.is_file():
        raise ProjectValidationError(
            f"Project database missing: {layout.database_file}",
            user_message="This project folder has no database file and cannot be opened.",
        )

    # Missing sub-directories are repaired rather than rejected: a synchronisation
    # tool that drops empty folders must not make an otherwise valid project
    # unopenable.
    for directory in layout.all_directories():
        directory.mkdir(exist_ok=True)

    database = open_project_database(layout.database_file)
    logger.info("Opened project '%s' at %s", metadata.name, layout.root)

    return _start_session(Project(metadata, layout), database)


def read_project_metadata(project_directory: Path) -> ProjectMetadata:
    """Read and validate ``project.json`` without opening the database.

    Used by "Open project" and by the recent-projects list, which needs a
    project's name without paying for a database connection.

    Args:
        project_directory: Directory expected to contain ``project.json``.

    Returns:
        The validated metadata.

    Raises:
        ProjectValidationError: The file is missing, unreadable, malformed, or
            declares a format version this build cannot open.
    """
    layout = ProjectLayout(project_directory)
    project_file = layout.project_file

    if not project_file.is_file():
        raise ProjectValidationError(
            f"Not an OMRFlow project (no {project_file.name}): {layout.root}",
            user_message="This folder is not an OMRFlow project.",
        )

    try:
        payload = read_json(project_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectValidationError(
            f"Could not read {project_file}: {exc}",
            user_message="The project file is damaged and could not be read.",
        ) from exc

    if isinstance(payload, dict):
        stored_version = payload.get("project_format_version", PROJECT_FORMAT_VERSION)
        if isinstance(stored_version, int) and stored_version > PROJECT_FORMAT_VERSION:
            raise ProjectValidationError(
                f"Project format version {stored_version} is newer than supported "
                f"version {PROJECT_FORMAT_VERSION}",
                user_message=(
                    "This project was created with a newer version of OMRFlow. "
                    "Please update OMRFlow to open it."
                ),
            )

    try:
        return ProjectMetadata.model_validate(payload)
    except ValidationError as exc:
        raise ProjectValidationError(
            f"Invalid project file {project_file}: {exc}",
            user_message="The project file is not valid and could not be opened.",
        ) from exc


def is_project_directory(path: Path) -> bool:
    """Return whether ``path`` looks like an openable project directory.

    Cheap check used by the GUI to enable or disable commands; it does not
    guarantee a successful open. Prefer :func:`read_project_metadata` when the
    reason for a failure matters.
    """
    layout = ProjectLayout(path)
    return layout.project_file.is_file() and layout.database_file.is_file()


def _start_session(project: Project, database: ProjectDatabase) -> ProjectSession:
    """Attach the per-project log file and build the session."""
    handler: logging.Handler | None = None
    try:
        handler = attach_log_file(project.layout.log_file)
    except OSError as exc:
        # Losing the project log is not a reason to refuse to open the project;
        # the application log still records the failure.
        logger.warning("Project log unavailable for %s (%s)", project.root, exc)

    # Logged after the handler is attached so that the project log always starts
    # with the identity of the project it belongs to.
    logger.info(
        "Project session started: '%s' (schema version %d)",
        project.name,
        database.schema_version,
    )
    return ProjectSession(project, database, handler)


def _store_identity(database: ProjectDatabase, metadata: ProjectMetadata) -> None:
    """Mirror identifying metadata into the database.

    Keeps a stray ``database.sqlite`` identifiable when it is found without its
    ``project.json``. Candidate data is never mirrored here.
    """
    values = {
        SettingKey.PROJECT_ID: metadata.project_id,
        SettingKey.PROJECT_NAME: metadata.name,
        SettingKey.PROJECT_FORMAT_VERSION: str(metadata.project_format_version),
        SettingKey.CREATED_WITH: metadata.created_with,
    }
    with database.session() as session:
        for key, value in values.items():
            session.merge(
                ProjectSetting(key=key, value=value, updated_at=metadata.modified_at)
            )
