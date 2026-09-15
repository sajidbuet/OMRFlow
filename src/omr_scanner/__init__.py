"""OMRFlow - template-driven OMR examination processing and result management.

Purpose:
    Package root. Exposes the application identity - name, version, licence
    and repository - and nothing else.

Responsibilities:
    * Declare the distribution version used by the GUI "About" box, the project
      metadata written into ``project.json`` and the database schema ledger.
    * Declare the project's public identity (repository URL, licence name,
      copyright year) once, so the GUI "About" dialog and any future exported
      metadata read it from here instead of each hard-coding their own copy.

What does NOT belong here:
    * Any import of GUI, database, imaging or service modules. Importing
      :mod:`omr_scanner` must stay cheap and side-effect free so that command
      line tools, tests and future headless workers can import it without
      pulling in Qt.

Invariants:
    * ``__version__`` mirrors ``project.version`` in ``pyproject.toml``.
    * ``REPOSITORY_URL`` mirrors ``project.urls.Homepage`` in ``pyproject.toml``.
    * ``LICENSE_NAME`` mirrors ``project.license`` in ``pyproject.toml``.
"""

from __future__ import annotations

APPLICATION_NAME = "OMRFlow"
"""Human readable application name (window titles, logs, exported metadata)."""

ORGANIZATION_NAME = "OMRFlow"
"""Used for per-user configuration and log directory resolution."""

__version__ = "0.1.0.dev0"

REPOSITORY_URL = "https://github.com/sajidbuet/OMRflow"
"""Public source repository, shown as a clickable link in the About dialog."""

LICENSE_NAME = "MIT"
"""Name of the licence this project is released under; the full text is the
repository's ``LICENSE`` file."""

COPYRIGHT_YEAR = 2026
"""Year shown in copyright notices (the About dialog, ``LICENSE``). A fixed
value, not the current calendar year, so the two never disagree with each
other."""

__all__ = [
    "APPLICATION_NAME",
    "COPYRIGHT_YEAR",
    "LICENSE_NAME",
    "ORGANIZATION_NAME",
    "REPOSITORY_URL",
    "__version__",
]
