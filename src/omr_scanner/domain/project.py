"""The project: an examination processing workspace on disk.

Purpose:
    Define what an OMRFlow project *is* - its on-disk layout, its metadata
    document (``project.json``) and the rules that make a directory a valid
    project.

Responsibilities:
    * :class:`ProjectDirectory` - the fixed set of sub-directories a project owns.
    * :class:`ProjectLayout` - resolves project-relative paths from a root.
    * :class:`ProjectMetadata` - the validated content of ``project.json``.
    * :class:`Project` - metadata plus layout; what services hand to the GUI.

What does NOT belong here:
    * Creating directories, reading or writing ``project.json``, touching the
      database. That is :mod:`omr_scanner.services.project_service`.
    * Anything about *scans* or *results*; a project only knows where such data
      will live, not what it contains.

Invariants:
    * A project root contains ``project.json`` and ``database.sqlite``.
    * Paths recorded *inside* a project (templates, scans, exports) are stored
      relative to the project root, so the whole folder can be moved, archived or
      synchronised between machines without rewriting references.
    * ``project.json`` is the discovery document; the SQLite database is the
      authoritative working store (see ``docs/decisions/ADR-0002``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omr_scanner import __version__
from omr_scanner.domain.exam_sets import MAX_EXAM_NAME_LENGTH

PROJECT_FILE_NAME = "project.json"
"""Discovery document at the root of every project."""

DATABASE_FILE_NAME = "database.sqlite"
"""Authoritative working data store, next to ``project.json``."""

PROJECT_FORMAT_VERSION = 2
"""Version of the ``project.json`` document format.

Bumped only for breaking changes. A build refuses to open a project whose
format version is greater than this value, because it cannot know what the
newer build meant.

History:
    1. Phase 0. ``project_format_version``, ``project_id``, ``name``,
       ``description``, timestamps, ``created_with``.
    2. Adds :attr:`ProjectMetadata.exam_name`. Reading is backward
       compatible - a version 1 document simply has no ``exam_name`` and
       loads with an empty one - but writing is not, because
       :class:`ProjectMetadata` forbids unknown fields, so a version 1 build
       handed a version 2 document would reject it as invalid. The bump is
       what turns that into the accurate "created with a newer version of
       OMRFlow" message instead.
"""

MAX_PROJECT_NAME_LENGTH = 120

_INVALID_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
"""Characters rejected in project names.

The set is the union of what Windows and POSIX forbid in path components:
project names are used as directory names, and a project created on Linux must
still open on Windows.
"""


class ProjectDirectory(StrEnum):
    """Sub-directories created inside every project.

    The enum is the single source of truth: the service creates exactly these,
    the validator checks exactly these, and later phases write into them by
    name rather than by hard-coded string.
    """

    TEMPLATES = "templates"
    """``.omrt`` template documents belonging to this examination."""

    SCANS_ORIGINAL = "scans_original"
    """Untouched source scans. Never modified, never overwritten (Phase 5)."""

    SCANS_ALIGNED = "scans_aligned"
    """Derived, geometrically normalised sheets. Safe to delete and regenerate
    (Phase 1/5)."""

    ANSWER_KEYS = "answer_keys"
    """Answer key documents and scanned solution sheets (Phase 8)."""

    CANDIDATE_LISTS = "candidate_lists"
    """Imported candidate/attendance CSV and XLSX files (Phase 7)."""

    EXPORTS = "exports"
    """Generated result reports (Phase 9)."""

    LOGS = "logs"
    """Per-project log files; written while this project is open."""


class ProjectLayout:
    """Resolves the paths a project owns, from its root directory.

    All properties return absolute paths. Use :meth:`relative_to_root` when a
    path has to be *stored*, so that projects stay relocatable.

    Args:
        root: Project root directory. It need not exist yet.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    @property
    def project_file(self) -> Path:
        """Absolute path of ``project.json``."""
        return self.root / PROJECT_FILE_NAME

    @property
    def database_file(self) -> Path:
        """Absolute path of the project SQLite database."""
        return self.root / DATABASE_FILE_NAME

    @property
    def templates_dir(self) -> Path:
        """Absolute path of the templates directory."""
        return self.directory(ProjectDirectory.TEMPLATES)

    @property
    def scans_original_dir(self) -> Path:
        """Absolute path of the original (never modified) scans directory."""
        return self.directory(ProjectDirectory.SCANS_ORIGINAL)

    @property
    def scans_aligned_dir(self) -> Path:
        """Absolute path of the normalised scans directory."""
        return self.directory(ProjectDirectory.SCANS_ALIGNED)

    @property
    def answer_keys_dir(self) -> Path:
        """Absolute path of the answer keys directory."""
        return self.directory(ProjectDirectory.ANSWER_KEYS)

    @property
    def candidate_lists_dir(self) -> Path:
        """Absolute path of the candidate list directory."""
        return self.directory(ProjectDirectory.CANDIDATE_LISTS)

    @property
    def exports_dir(self) -> Path:
        """Absolute path of the report export directory."""
        return self.directory(ProjectDirectory.EXPORTS)

    @property
    def logs_dir(self) -> Path:
        """Absolute path of the per-project log directory."""
        return self.directory(ProjectDirectory.LOGS)

    @property
    def log_file(self) -> Path:
        """Absolute path of the per-project log file."""
        return self.logs_dir / "project.log"

    def directory(self, which: ProjectDirectory) -> Path:
        """Return the absolute path of one managed sub-directory."""
        return self.root / which.value

    def all_directories(self) -> tuple[Path, ...]:
        """Return every managed sub-directory, in declaration order."""
        return tuple(self.directory(item) for item in ProjectDirectory)

    def relative_to_root(self, path: Path) -> Path:
        """Convert an absolute path inside the project into a storable one.

        Args:
            path: Absolute or relative path. A relative path is returned
                unchanged, on the assumption it is already project-relative.

        Returns:
            The path relative to the project root.

        Raises:
            ValueError: ``path`` is absolute but lies outside the project.
        """
        if not path.is_absolute():
            return path
        return path.resolve().relative_to(self.root)

    def resolve(self, relative_path: Path) -> Path:
        """Turn a stored project-relative path back into an absolute path."""
        return self.root / relative_path

    def __repr__(self) -> str:
        """Return a debugging representation naming the project root."""
        return f"ProjectLayout(root={self.root!s})"


class ProjectMetadata(BaseModel):
    """Validated content of ``project.json``.

    Attributes:
        project_format_version: Layout/metadata format version.
        project_id: Stable identifier, generated once at creation. Report files
            and database rows reference it, so it must never be regenerated for
            an existing project.
        name: Name of the project *as a workspace*. Doubles as the default
            folder name, and is therefore validated as a portable directory
            name. Use :attr:`exam_name` for the examination's real title.
        exam_name: Title of the examination this project processes, as it
            should read on a report - for example *"Recruitment Exam,
            Bangladesh Submarine Cable Regulatory Authority"*. Free text: it
            is never used as a path, so none of :attr:`name`'s folder-name
            restrictions apply to it. Empty only for a project created before
            this field existed; :attr:`Project.exam_name` falls back to
            :attr:`name` for those.
        description: Optional free text.
        created_at: Creation timestamp (timezone aware, UTC).
        modified_at: Last time the metadata document was written.
        created_with: OMRFlow version that created the project, for diagnostics.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_format_version: int = PROJECT_FORMAT_VERSION
    project_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=MAX_PROJECT_NAME_LENGTH)
    exam_name: str = Field(default="", max_length=MAX_EXAM_NAME_LENGTH)
    description: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    modified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_with: str = __version__

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        """Reject names that cannot be used as a directory name on any platform."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("Project name must not be blank")
        if _INVALID_NAME_CHARACTERS.search(stripped):
            raise ValueError(
                "Project name must not contain any of the characters < > : \" / \\ | ? *"
            )
        if stripped != stripped.rstrip("."):
            # Windows silently strips trailing dots from directory names, which
            # would make the stored name and the actual folder disagree.
            raise ValueError("Project name must not end with a period")
        return stripped

    @field_validator("exam_name")
    @classmethod
    def _trim_exam_name(cls, value: str) -> str:
        """Trim surrounding whitespace; allow empty for a pre-v2 project.

        Blank is a legitimate *stored* state (a project created before this
        field existed, or one whose exam name has not been filled in yet) but
        never a legitimate *submitted* one - see
        :func:`omr_scanner.domain.exam_sets.validate_exam_name`, which the
        service layer applies to operator input before it reaches here.
        """
        return value.strip()

    @field_validator("created_at", "modified_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """Force timezone-aware UTC timestamps.

        Naive timestamps compare incorrectly across machines and daylight saving
        changes, which matters for audit history in later phases.
        """
        if value.tzinfo is None:
            raise ValueError("Timestamps must be timezone aware")
        return value.astimezone(UTC)

    def touched(self) -> Self:
        """Return a copy with ``modified_at`` set to now."""
        return self.model_copy(update={"modified_at": datetime.now(UTC)})

    def with_exam_name(self, exam_name: str) -> Self:
        """Return a copy carrying a new examination name, marked as modified.

        Args:
            exam_name: The already-validated name (see
                :func:`omr_scanner.domain.exam_sets.validate_exam_name`).

        The copy also declares the current
        :data:`PROJECT_FORMAT_VERSION`, because the document about to be
        written *is* a current-format document: it contains ``exam_name``.
        Leaving a project that predates this field claiming to be version 1
        while writing a version 2 field into it would make the stored version
        a lie.
        """
        return self.model_copy(
            update={
                "exam_name": exam_name.strip(),
                "project_format_version": PROJECT_FORMAT_VERSION,
                "modified_at": datetime.now(UTC),
            }
        )


class Project:
    """An opened project: its metadata and its resolved on-disk layout.

    Instances are produced by :mod:`omr_scanner.services.project_service`; they
    are never constructed directly by the GUI.

    Args:
        metadata: Validated ``project.json`` content.
        layout: Resolved paths for this project's root.
    """

    def __init__(self, metadata: ProjectMetadata, layout: ProjectLayout) -> None:
        self.metadata = metadata
        self.layout = layout

    @property
    def name(self) -> str:
        """Display name of the project."""
        return self.metadata.name

    @property
    def exam_name(self) -> str:
        """Title of the examination, for display.

        Falls back to :attr:`name` for a project created before
        ``project.json`` carried an examination name, so that every caller
        has something truthful to show without having to know which format
        version the project came from. The *stored* value is left empty in
        that case rather than being silently backfilled - see
        ``docs/DATA_MODEL.md`` for why an unanswered question is recorded as
        unanswered.
        """
        return self.metadata.exam_name or self.metadata.name

    @property
    def root(self) -> Path:
        """Absolute project root directory."""
        return self.layout.root

    def __repr__(self) -> str:
        """Return a debugging representation naming the project and its root."""
        return f"Project(name={self.metadata.name!r}, root={self.layout.root!s})"
