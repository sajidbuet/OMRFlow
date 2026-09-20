"""A privacy-safe diagnostic bundle for support requests (Phase 10, §39).

Purpose:
    Give an operator one file to attach to a bug report or support request
    that answers "what version, what platform, what does the health check
    say, what does recent (sanitised) activity look like" - without ever
    including anything about a candidate, a script, an answer or a scan.

Responsibilities:
    * :func:`build_diagnostic_bundle` - the one entry point. Writes a ZIP
      containing a handful of small JSON/text files, never the project
      database itself.

What does NOT belong here:
    * Qt version detection. This module is a ``services`` module and must
      not import PySide6 (``tests/unit/test_architecture.py`` forbids it);
      the caller (the GUI's diagnostics dialog) passes Qt's own version
      string in through ``extra_environment`` instead.
    * Any repair or analysis - this module only collects and writes what
      :mod:`omr_scanner.services.project_health` and the rest of the
      application already know, verbatim.

What is deliberately excluded, always:
    Candidate names, roll/student IDs, recognised answer strings, answer
    keys, scores, attendance records, scan images, and the raw project
    database file. :mod:`omr_scanner.services.project_health`'s own
    messages are already free of candidate data by construction (every
    :class:`~omr_scanner.services.project_health.HealthIssue` message is a
    count and a generic description, never a name or a value) - this module
    adds no new source of that data, and the log excerpt it includes relies
    on the same "no candidate data reaches the log" invariant Phase 7
    established and `tests/unit/test_candidate_privacy.py` already pins.

Why a bounded log tail, not the whole file:
    A long-running application's log can grow to many megabytes; a bundle
    meant to be emailed or attached to an issue tracker should not become
    one. The most recent activity is almost always what a support request
    needs.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

    from omr_scanner.config.processing import ProcessingSettings
    from omr_scanner.database.engine import ProjectDatabase

_LOGGER = logging.getLogger(__name__)

LOG_TAIL_LINES = 200
"""How much of the application log to include - recent activity, not the
whole file, and small enough to attach anywhere."""


@dataclass(frozen=True, slots=True)
class DiagnosticBundleManifest:
    """What the bundle's own ``manifest.json`` records about itself."""

    created_at: str
    application_name: str
    application_version: str
    python_version: str
    opencv_version: str
    os_platform: str
    privacy_statement: str


_PRIVACY_STATEMENT = (
    "This bundle never includes candidate names, roll or student IDs, "
    "recognised answer strings, answer keys, scores, attendance records, "
    "scan images, or the project database itself."
)


def _environment_info(extra_environment: Mapping[str, str] | None) -> dict[str, str]:
    try:
        import cv2

        opencv_version = cv2.__version__
    except Exception:  # pragma: no cover - OpenCV always present in practice
        opencv_version = "unknown"

    info = {
        "application_version": __version__,
        "python_version": sys.version.split()[0],
        "opencv_version": opencv_version,
        "os_platform": platform.platform(),
        "processor": platform.processor() or "unknown",
    }
    if extra_environment:
        info.update(dict(extra_environment))
    return info


def _health_summary(
    database: ProjectDatabase | None, project_root: Path | None
) -> dict[str, object]:
    if database is None:
        return {"status": "no project open"}
    from omr_scanner.services import project_health

    try:
        report = (
            project_health.full_check(database, project_root)
            if project_root is not None
            else project_health.quick_check(database)
        )
    except Exception as exc:  # a failed health check must not block the bundle
        return {"status": "health check itself failed", "error": str(exc)}
    return {
        "level": report.level.value,
        "issue_count": len(report.issues),
        "issues": [
            {"level": issue.level.value, "code": issue.code, "message": issue.message}
            for issue in report.issues
        ],
    }


def _processing_summary(processing: ProcessingSettings | None) -> dict[str, object]:
    if processing is None:
        return {}
    return {
        "mode": processing.mode.value,
        "worker_count": processing.worker_count,
        "opencv_threads": processing.opencv_threads,
        "worker_recycle_after": processing.worker_recycle_after,
        "diagnostics_enabled": processing.diagnostics_enabled,
    }


def _latest_benchmark_summary(project_root: Path | None) -> dict[str, object] | None:
    if project_root is None:
        return None
    benchmarks_dir = project_root / "benchmarks"
    if not benchmarks_dir.is_dir():
        return None
    reports = sorted(benchmarks_dir.glob("stress_*.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        return None
    try:
        payload: dict[str, object] = json.loads(reports[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Everything in a benchmark report is already sheet counts, timings and
    # resource usage - never candidate data - but the telemetry file path
    # names a location on this machine, which is excluded on general
    # principle rather than because it is examination-sensitive.
    payload.pop("telemetry_file", None)
    return payload


def _log_tail(project_root: Path | None) -> str:
    if project_root is None:
        return ""
    log_path = project_root / "logs" / "project.log"
    if not log_path.is_file():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-LOG_TAIL_LINES:])


def build_diagnostic_bundle(
    output_path: Path,
    *,
    database: ProjectDatabase | None = None,
    project_root: Path | None = None,
    processing: ProcessingSettings | None = None,
    extra_environment: Mapping[str, str] | None = None,
) -> Path:
    """Write a privacy-safe diagnostic bundle to ``output_path``.

    Args:
        output_path: Destination ``.zip`` file. Parent directories are
            created if needed; an existing file at this exact path is
            overwritten (the bundle is a disposable support artefact, not
            examination data - none of Phase 10's "never overwrite" rules
            apply to it).
        database: The open project database, for the health check and
            schema version. ``None`` when no project is open.
        project_root: The project's root directory, for the comprehensive
            health check, the log tail and the latest benchmark report.
            ``None`` runs only the cheap checks that need no project.
        processing: Current processing settings, recorded verbatim (worker
            count, OpenCV threads, recycle interval - machine configuration,
            never examination data).
        extra_environment: Additional environment fields the caller already
            knows and this module cannot - Qt's version, most notably,
            since this ``services`` module must not import PySide6.

    Returns:
        ``output_path``, for chaining.
    """
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    environment = _environment_info(extra_environment)
    manifest = DiagnosticBundleManifest(
        created_at=datetime.now(UTC).isoformat(),
        application_name="OMRFlow",
        application_version=__version__,
        python_version=environment["python_version"],
        opencv_version=environment["opencv_version"],
        os_platform=environment["os_platform"],
        privacy_statement=_PRIVACY_STATEMENT,
    )

    schema_version = database.schema_version if database is not None else None
    health = _health_summary(database, project_root)
    processing_info = _processing_summary(processing)
    benchmark = _latest_benchmark_summary(project_root)
    log_tail = _log_tail(project_root)

    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(asdict(manifest), indent=2))
        bundle.writestr("environment.json", json.dumps(environment, indent=2))
        bundle.writestr(
            "database.json",
            json.dumps({"schema_version": schema_version}, indent=2),
        )
        bundle.writestr("health_check.json", json.dumps(health, indent=2))
        bundle.writestr("processing_settings.json", json.dumps(processing_info, indent=2))
        if benchmark is not None:
            bundle.writestr("latest_benchmark_summary.json", json.dumps(benchmark, indent=2))
        if log_tail:
            bundle.writestr("recent_log_tail.txt", log_tail)

    _LOGGER.info("Wrote diagnostic bundle to %s", output_path)
    return output_path


__all__ = [
    "LOG_TAIL_LINES",
    "DiagnosticBundleManifest",
    "build_diagnostic_bundle",
]
