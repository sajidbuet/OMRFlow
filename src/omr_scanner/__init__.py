"""OMRFlow - template-driven OMR examination processing and result management.

Purpose:
    Package root. Exposes the application name and version and nothing else.

Responsibilities:
    * Declare the distribution version used by the GUI "About" box, the project
      metadata written into ``project.json`` and the database schema ledger.

What does NOT belong here:
    * Any import of GUI, database, imaging or service modules. Importing
      :mod:`omr_scanner` must stay cheap and side-effect free so that command
      line tools, tests and future headless workers can import it without
      pulling in Qt.

Invariants:
    * ``__version__`` mirrors ``project.version`` in ``pyproject.toml``.
"""

from __future__ import annotations

APPLICATION_NAME = "OMRFlow"
"""Human readable application name (window titles, logs, exported metadata)."""

ORGANIZATION_NAME = "OMRFlow"
"""Used for per-user configuration and log directory resolution."""

__version__ = "0.1.0.dev0"

__all__ = ["APPLICATION_NAME", "ORGANIZATION_NAME", "__version__"]
