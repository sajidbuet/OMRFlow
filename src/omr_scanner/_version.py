"""The single source of truth for OMRFlow's version and release maturity.

Purpose:
    Hold the application's version once, and derive everything else from it:
    the release channel shown in the About dialog, the Git tag, the installer
    and artifact names, and the build identifier recorded in diagnostics.

Responsibilities:
    * :data:`__version__` - the one string that is edited to make a release.
    * :class:`ReleaseChannel` and :data:`RELEASE_CHANNEL` - what that version
      *means*, derived from the string rather than maintained beside it.
    * :func:`release_tag`, :func:`artifact_version` - the spellings the
      release process needs.
    * :func:`build_identifier`, :func:`version_info` - what a bug report
      needs in order to be actionable.

What does NOT belong here:
    * Any import of GUI, database, imaging or service modules, and any
      third-party import at all. :mod:`omr_scanner` must stay cheap and
      side-effect free to import - command line tools, tests and worker
      processes all pay for anything added here. That is also why the channel
      is recognised with a regular expression instead of `packaging.version`,
      which is a build-time dependency and not a runtime one.

Why one string serves both Semantic Versioning and Python packaging:
    ``0.1.0-alpha.1`` is simultaneously a valid SemVer version and a valid
    (non-normalised) PEP 440 version, which Python packaging normalises to
    ``0.1.0a1`` for wheel and sdist filenames. So the Git tag, the installer
    name and the About dialog can all show the SemVer spelling an operator
    expects while `pip` and `setuptools` see something they accept - from the
    same constant, with no second version to keep in step. ``pyproject.toml``
    reads this attribute rather than declaring its own.

Making a release:
    Edit :data:`__version__` here and nowhere else, update ``CHANGELOG.md``,
    then follow ``docs/release/RELEASE_CHECKLIST.md``.
"""

from __future__ import annotations

import os
import re
import subprocess
from enum import Enum
from pathlib import Path
from typing import Final

__version__: Final = "0.1.0-alpha.2"
"""The application version. **The only place it is written down.**

Semantic Versioning, and simultaneously a PEP 440 prerelease that normalises
to ``0.1.0a1``. See the module docstring for why that matters.
"""

_PRERELEASE_PATTERN: Final = re.compile(
    r"^(?P<release>\d+\.\d+\.\d+)"
    r"(?:[-_.]?(?P<kind>alpha|a|beta|b|rc|c|pre|preview)[-_.]?(?P<number>\d+)?)?"
    r"(?:\+(?P<local>.+))?$",
    re.IGNORECASE,
)


class ReleaseChannel(Enum):
    """How finished a build claims to be.

    The four rungs OMRFlow's release process defines, documented in
    ``docs/wiki/Release-Process.md``. Attached to the version string rather
    than configured separately, so a build cannot claim to be stable while
    carrying a prerelease version.

    Attributes:
        ALPHA: Core features implemented and substantially covered by
            automated and synthetic tests; **real examination-data
            qualification incomplete**. Results must be independently
            verified before operational use.
        BETA: Representative real attendance workbooks and scanned cohorts
            processed end to end; remaining work is mostly defect
            correction.
        RELEASE_CANDIDATE: Feature frozen; the packaged application itself
            passes qualification, including upgrade and migration.
        STABLE: Production release, with the full qualification met.
    """

    ALPHA = "Alpha"
    BETA = "Beta"
    RELEASE_CANDIDATE = "Release Candidate"
    STABLE = "Stable"

    @property
    def is_prerelease(self) -> bool:
        """Whether a build on this channel is a prerelease.

        A GitHub release for anything but :attr:`STABLE` must be marked as a
        pre-release, and must not be presented as the latest production
        version.
        """
        return self is not ReleaseChannel.STABLE


_CHANNEL_BY_KIND: Final = {
    "a": ReleaseChannel.ALPHA,
    "alpha": ReleaseChannel.ALPHA,
    "b": ReleaseChannel.BETA,
    "beta": ReleaseChannel.BETA,
    "c": ReleaseChannel.RELEASE_CANDIDATE,
    "rc": ReleaseChannel.RELEASE_CANDIDATE,
    "pre": ReleaseChannel.RELEASE_CANDIDATE,
    "preview": ReleaseChannel.RELEASE_CANDIDATE,
}


def _parse(version: str) -> tuple[str, ReleaseChannel]:
    """Split ``version`` into its release number and its channel.

    Raises:
        ValueError: ``version`` is not a version this project can release.
            Deliberately fatal rather than defaulting to "stable": a typo
            that silently downgraded a prerelease into a stable-looking build
            is exactly the mistake the release process exists to prevent.
    """
    match = _PRERELEASE_PATTERN.match(version.strip())
    if match is None:
        raise ValueError(
            f"{version!r} is not a recognised OMRFlow version. Expected "
            "MAJOR.MINOR.PATCH optionally followed by a prerelease such as "
            "'-alpha.1', '-beta.2' or '-rc.1'."
        )
    kind = (match.group("kind") or "").lower()
    return match.group("release"), _CHANNEL_BY_KIND.get(kind, ReleaseChannel.STABLE)


RELEASE_VERSION: Final = _parse(__version__)[0]
"""``"0.1.0"`` - the version without its prerelease suffix."""

RELEASE_CHANNEL: Final = _parse(__version__)[1]
"""Derived from :data:`__version__`, never configured separately."""

IS_PRERELEASE: Final = RELEASE_CHANNEL.is_prerelease
"""Whether this build must be published as a GitHub pre-release."""

ALPHA_NOTICE: Final = (
    "Alpha release for evaluation and testing. Real examination-data "
    "qualification is still in progress. Independently verify generated "
    "results before operational use."
)
"""The warning shown wherever the Alpha build identifies itself.

One sentence of fact and one instruction. Shown in the About dialog and
repeated in the README and the release notes; deliberately *not* shown as a
modal on every launch, which teaches an operator to dismiss warnings without
reading them.
"""


def release_tag(version: str = __version__) -> str:
    """The Git tag for ``version``: ``"v0.1.0-alpha.1"``."""
    return f"v{version}"


def numeric_version(version: str = __version__) -> tuple[int, int, int, int]:
    """The version as the four integers Windows insists on.

    Args:
        version: An OMRFlow version string.

    Returns:
        ``(major, minor, patch, prerelease)``, where the fourth field is the
        prerelease number and ``0`` for a stable release.

    Windows has no notion of a prerelease and its VERSIONINFO resource and
    installer metadata both require exactly four integers. Mapping the
    prerelease number into the fourth field keeps ``-alpha.1`` and
    ``-alpha.2`` distinguishable to the operating system and to an
    installer's upgrade comparison, and leaves a stable ``0.1.0`` sorting
    above every prerelease that preceded it.

    Lives here rather than in the packaging scripts because two of them need
    it - the executable's version resource and the installer's
    ``VersionInfoVersion`` - and a second copy is a second thing to get
    wrong.
    """
    release, _ = _parse(version)
    major, minor, patch = (int(part) for part in release.split("."))
    match = re.search(r"(?:alpha|beta|rc|a|b|c)[-_.]?(\d+)", version, re.IGNORECASE)
    return major, minor, patch, int(match.group(1)) if match else 0


def numeric_version_string(version: str = __version__) -> str:
    """:func:`numeric_version` as ``"0.1.0.1"``, for Windows tooling."""
    return ".".join(str(part) for part in numeric_version(version))


def artifact_version(version: str = __version__) -> str:
    """The version as it appears in a release artifact's filename.

    Identical to the version itself. A separate function because the
    installer, the checksum file and the release workflow all need the same
    spelling, and "identical for now" is not the same as "may be assumed
    identical forever".
    """
    return version


def _git_commit(repository_root: Path | None = None) -> str | None:
    """The short commit this source tree is at, or ``None``.

    Returns ``None`` rather than raising for every reason it can fail - not a
    Git checkout, Git not installed, or a packaged build with no repository
    at all - because a missing commit is normal in a distributed build and
    must never stop the application starting.
    """
    root = repository_root or Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return None
    try:
        # A fixed argument list with no shell, so the path cannot be
        # interpreted as anything but a path.
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - platform dependent
        return None
    if completed.returncode != 0:
        return None
    commit = completed.stdout.strip()
    return commit or None


BUILD_COMMIT_ENVIRONMENT_VARIABLE: Final = "OMRFLOW_BUILD_COMMIT"
"""Set by the build scripts so a packaged build knows its own commit.

A frozen application has no ``.git`` directory to ask, so the commit is
baked in at build time instead. This is what makes a downloaded installer
traceable back to a source revision.
"""


def build_identifier() -> str:
    """The version plus the source commit, when one can be determined.

    Returns:
        ``"0.1.0-alpha.1+a1b2c3d"`` in a Git checkout or a build that
        recorded its commit, and plain ``"0.1.0-alpha.1"`` otherwise.

    For diagnostics, logs and bug reports - never for the public version
    string, which stays clean. The ``+local`` part is valid in both SemVer
    and PEP 440 and is ignored when versions are compared.
    """
    commit = os.environ.get(BUILD_COMMIT_ENVIRONMENT_VARIABLE) or _git_commit()
    return f"{__version__}+{commit}" if commit else __version__


def version_info() -> dict[str, str]:
    """Everything a bug report should state about which build is running.

    Returns:
        Version, release number, channel, whether it is a prerelease, and the
        build identifier. Deliberately strings throughout, so this can be
        dropped straight into a diagnostic bundle, a log header or an issue
        template without formatting decisions.
    """
    return {
        "version": __version__,
        "release_version": RELEASE_VERSION,
        "release_channel": RELEASE_CHANNEL.value,
        "is_prerelease": str(IS_PRERELEASE).lower(),
        "build": build_identifier(),
    }


__all__ = [
    "ALPHA_NOTICE",
    "BUILD_COMMIT_ENVIRONMENT_VARIABLE",
    "IS_PRERELEASE",
    "RELEASE_CHANNEL",
    "RELEASE_VERSION",
    "ReleaseChannel",
    "__version__",
    "artifact_version",
    "build_identifier",
    "numeric_version",
    "numeric_version_string",
    "release_tag",
    "version_info",
]
