"""Everything the qualification run needs to know about this machine.

Purpose:
    One place that answers "where is the repository, where does the build put
    its executable, where do results go, how long may a launch take". Every
    other module takes a :class:`ValidationConfig` and asks it, rather than
    recomputing paths or hard-coding timeouts.

Why the paths are discovered rather than configured:
    The build scripts under ``scripts/release`` already decide where the bundle
    and the installer go. A second copy of those locations here would be a
    second thing to keep in step, and the failure mode - validating an artifact
    that is not the one that would be published - is exactly what this
    framework exists to prevent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- repository

PACKAGE_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = PACKAGE_DIR.parents[1]
"""Derived from this file's location, never from the working directory.

The launcher may be invoked from anywhere; a qualification run that silently
validated a different checkout because of a stray `cd` would be worse than one
that failed.
"""

SOURCE_DIR = REPOSITORY_ROOT / "src"
TESTS_DIR = REPOSITORY_ROOT / "tests"
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts" / "release"
PACKAGING_DIR = REPOSITORY_ROOT / "packaging"

# ------------------------------------------------------------------ artifacts

BUNDLE_DIR = REPOSITORY_ROOT / "dist" / "OMRFlow"
BUNDLE_EXE = BUNDLE_DIR / "OMRFlow.exe"
INSTALLER_DIR = REPOSITORY_ROOT / "dist" / "installer"
CHECKSUM_FILE = INSTALLER_DIR / "SHA256SUMS.txt"

INTERNAL_DIR_NAME = "_internal"
"""PyInstaller's one-directory layout puts the runtime beside the executable."""

# --------------------------------------------------- where the app keeps data

INSTALLED_APP_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OMRFlow"
INSTALLED_EXE = INSTALLED_APP_DIR / "OMRFlow.exe"
USER_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "OMRFlow"
START_MENU_DIR = (
    Path(os.environ.get("APPDATA", ""))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
)

ENV_CONFIG_DIR = "OMRFLOW_CONFIG_DIR"
ENV_LOG_DIR = "OMRFLOW_LOG_DIR"
"""The application's own configuration and log overrides.

`tests/conftest.py` already redirects both for every test. The packaged and
installed applications are launched with the same two variables pointed into
the validation workspace, which is what keeps a qualification run from writing
into the operator's real OMRFlow configuration - see §12 of the framework's
README.
"""

# ------------------------------------------------------------------- results

RESULTS_ROOT = REPOSITORY_ROOT / "validation-results"
BASELINE_DIR = PACKAGE_DIR / "baselines"
SUITES_DIR = PACKAGE_DIR / "suites"


@dataclass(frozen=True)
class Timeouts:
    """How long each waiting step may take before it is called a failure.

    Every one of these exists because the alternative is a qualification run
    that hangs forever on a machine nobody is watching, which is the single
    worst outcome for a harness meant to run unattended.
    """

    app_launch_seconds: int = 120
    """Cold start of a frozen build on a slow disk, unpacking Qt."""

    window_seconds: int = 120
    """From process start to a window with a title."""

    shutdown_seconds: int = 60
    installer_seconds: int = 900
    uninstaller_seconds: int = 600
    build_seconds: int = 3600
    pytest_seconds: int = 7200
    """The full suite is ~30 minutes on an idle machine and longer on a busy
    one; this is a stuck-process guard, not a performance target."""


@dataclass
class ValidationConfig:
    """One qualification run's settings and output locations."""

    run_id: str
    output_dir: Path
    workspace: Path

    verbose: bool = False
    keep_artifacts: bool = False
    fail_on_warning: bool = False
    update_visual_baselines: bool = False

    installer_path: Path | None = None
    allow_replace_installation: bool = False
    """Whether the installer stage may remove an OMRFlow that is already
    installed. Off by default: an operator's working installation is not
    something a test may destroy because it was convenient."""

    timeouts: Timeouts = field(default_factory=Timeouts)

    # --------------------------------------------------------------- outputs
    @property
    def screenshots_dir(self) -> Path:
        """Checkpoint screenshots taken by the GUI suites."""
        return self.output_dir / "screenshots"

    @property
    def logs_dir(self) -> Path:
        """stdout/stderr of everything this run launched."""
        return self.output_dir / "logs"

    @property
    def artifacts_dir(self) -> Path:
        """JUnit XML and accessibility findings."""
        return self.output_dir / "artifacts"

    @property
    def summary_path(self) -> Path:
        """The Markdown report a person reads."""
        return self.output_dir / "validation-summary.md"

    @property
    def json_path(self) -> Path:
        """The machine-readable report, for CI ingestion."""
        return self.output_dir / "validation-results.json"

    # ------------------------------------------------------------- workspace
    @property
    def app_config_dir(self) -> Path:
        """Where a launched application is told to keep its configuration."""
        return self.workspace / "user-config"

    @property
    def app_log_dir(self) -> Path:
        """Where a launched application is told to write its log."""
        return self.workspace / "user-logs"

    @property
    def projects_dir(self) -> Path:
        """Where throwaway test projects are created."""
        return self.workspace / "projects"

    def child_environment(self) -> dict[str, str]:
        """The environment a launched OMRFlow gets.

        Inherits the current environment and redirects the two directories the
        application writes to, so nothing a qualification run does can reach
        the operator's real configuration, logs or recent-project list.
        """
        environment = dict(os.environ)
        environment[ENV_CONFIG_DIR] = str(self.app_config_dir)
        environment[ENV_LOG_DIR] = str(self.app_log_dir)
        return environment

    def prepare(self) -> None:
        """Create every directory this run will write into."""
        for directory in (
            self.output_dir,
            self.screenshots_dir,
            self.logs_dir,
            self.artifacts_dir,
            self.workspace,
            self.app_config_dir,
            self.app_log_dir,
            self.projects_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def new_run(
    *,
    workspace_root: Path | None = None,
    results_root: Path | None = None,
    **overrides: object,
) -> ValidationConfig:
    """Build the configuration for a fresh run, with a timestamped output dir.

    The run identifier is local time to the second. Two runs started in the
    same second would collide, which is theoretically possible and practically
    not - a run takes minutes.
    """
    run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    root = results_root if results_root is not None else RESULTS_ROOT
    workspace_base = (
        workspace_root if workspace_root is not None else root / run_id / "workspace"
    )
    config = ValidationConfig(
        run_id=run_id,
        output_dir=root / run_id,
        workspace=workspace_base,
    )
    for name, value in overrides.items():
        if not hasattr(config, name):  # pragma: no cover - programming error
            raise AttributeError(f"ValidationConfig has no field {name!r}")
        setattr(config, name, value)
    return config
