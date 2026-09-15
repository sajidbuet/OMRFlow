"""Application workflows.

Purpose:
    Coordinate domain models, the database, imaging, recognition and reporting
    into the operations a user actually performs ("create a project", "process a
    batch of scans", "calculate results").

Responsibilities:
    * Own multi-step operations and their failure handling.
    * Translate low-level failures into :mod:`omr_scanner.errors` exceptions the
      GUI can present.
    * Perform file system and database side effects; the domain layer never does.

What does NOT belong here:
    * Widgets, dialogs, Qt signals or any other presentation concern. A service
      must be callable from a test, a script or a future command line tool.
    * Image-processing algorithms (``imaging``) and value interpretation
      (``recognition``); services *call* those, they do not implement them.

Phase status:
    Phase 0 implements :mod:`omr_scanner.services.project_service` and
    :mod:`omr_scanner.services.template_service`. Scan, recognition, conflict,
    attendance, scoring and reporting services arrive with their own phases
    (see ``development/ROADMAP.md``).
"""

from omr_scanner.services.project_service import (
    ProjectSession,
    create_project,
    is_project_directory,
    open_project,
    read_project_metadata,
)
from omr_scanner.services.template_service import (
    list_templates,
    load_template,
    save_template,
)

__all__ = [
    "ProjectSession",
    "create_project",
    "is_project_directory",
    "list_templates",
    "load_template",
    "open_project",
    "read_project_metadata",
    "save_template",
]
