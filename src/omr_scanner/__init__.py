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
    * ``REPOSITORY_URL`` mirrors ``project.urls.Homepage`` in ``pyproject.toml``.
    * ``LICENSE_NAME`` mirrors ``project.license`` in ``pyproject.toml``.
    * ``__version__`` is re-exported from :mod:`omr_scanner._version`, which
      ``pyproject.toml`` also reads - so the version is written down once and
      the package metadata cannot drift from the running application.
"""

from __future__ import annotations

from omr_scanner._version import (
    ALPHA_NOTICE,
    IS_PRERELEASE,
    RELEASE_CHANNEL,
    RELEASE_VERSION,
    ReleaseChannel,
    __version__,
    build_identifier,
    release_tag,
    version_info,
)

APPLICATION_NAME = "OMRFlow"
"""Human readable application name (window titles, logs, exported metadata)."""

ORGANIZATION_NAME = "OMRFlow"
"""Used for per-user configuration and log directory resolution."""

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
    "ALPHA_NOTICE",
    "APPLICATION_NAME",
    "COPYRIGHT_YEAR",
    "IS_PRERELEASE",
    "LICENSE_NAME",
    "ORGANIZATION_NAME",
    "RELEASE_CHANNEL",
    "RELEASE_VERSION",
    "REPOSITORY_URL",
    "ReleaseChannel",
    "__version__",
    "build_identifier",
    "release_tag",
    "version_info",
]
