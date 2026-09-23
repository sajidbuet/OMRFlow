"""Application-level configuration.

Purpose:
    Owns *global* settings - the ones that belong to the installation and the
    user, not to any single examination.

Configuration layers (see ``docs/DEVELOPMENT_GUIDE.md``, "Where settings live"):
    1. Application defaults      -> this package (``AppConfig``), one file per user.
    2. Project configuration     -> ``project.json`` inside each project directory
       (``omr_scanner.domain.project``).
    3. Template configuration    -> the ``.omrt`` document
       (``omr_scanner.domain.template``).
    4. Recognition settings      -> nested inside the template, because thresholds
       are only meaningful for the sheet design they were tuned on.

What does NOT belong here:
    * Per-project or per-template values. If a setting can differ between two
      examinations processed on the same machine, it is not application config.
    * Constants used by exactly one module; keep those next to their user.
"""

from omr_scanner.config.app_config import (
    DEFAULT_RIBBON_DENSITY,
    MAX_RIBBON_DENSITY,
    MIN_RIBBON_DENSITY,
    AppConfig,
    load_app_config,
    save_app_config,
)
from omr_scanner.config.paths import (
    app_config_file,
    app_log_file,
    user_config_dir,
    user_log_dir,
)
from omr_scanner.config.processing import (
    AUTOMATIC_WORKER_LIMIT,
    MAX_CONFIGURABLE_WORKERS,
    ProcessingMode,
    ProcessingSettings,
    detected_cpu_count,
)

__all__ = [
    "AUTOMATIC_WORKER_LIMIT",
    "DEFAULT_RIBBON_DENSITY",
    "MAX_CONFIGURABLE_WORKERS",
    "MAX_RIBBON_DENSITY",
    "MIN_RIBBON_DENSITY",
    "AppConfig",
    "ProcessingMode",
    "ProcessingSettings",
    "app_config_file",
    "app_log_file",
    "detected_cpu_count",
    "load_app_config",
    "save_app_config",
    "user_config_dir",
    "user_log_dir",
]
