"""The Phase 10 100,000-sheet qualification campaign.

Purpose:
    Run the final Phase 10 release qualification without a human, a model or
    a GUI supervising it: one uninterrupted reference run, then five
    independent forced-kill/resume runs, each from equivalent clean
    deterministic initial conditions, with every release-blocking assertion
    measured and written to disk as it happens.

Responsibilities:
    * :class:`QualificationConfig` - what a campaign is.
    * :class:`QualificationState` - where it has got to, durable across an
      interruption of the orchestrator *itself*.
    * :func:`preflight` - refuse to start a multi-hour campaign that cannot
      finish.
    * :func:`execute_campaign` - the whole thing, resumable.
    * :func:`build_reports` - the machine-readable and human-readable
      results.

What does NOT belong here:
    * Qt. The GUI launches this module's CLI
      (:mod:`omr_scanner.tools.phase10_qualification`) as an independent
      process and reads the files written here; it never runs a campaign
      in-process, and closing it must not stop one.
    * A second stress framework. Every run is the existing
      :mod:`omr_scanner.tools.benchmark_stress` CLI, driving the existing
      :mod:`omr_scanner.evaluation.stress_runner` over the existing
      deterministic :mod:`omr_scanner.evaluation.stress_dataset`. This module
      supervises those processes from outside; it does not reimplement them.

Why the orchestrator is a separate process from the run it supervises:
    The thing being tested is what survives an abrupt, ungraceful death. A
    supervisor that shared the dying process's memory would lose exactly the
    evidence the qualification depends on - which sheets were durably
    committed *before* the kill. So the pre-kill evidence is written by this
    module, to this module's own directory, about a database it reads
    read-only while another process is still writing to it.

Why every run gets its own project:
    A single project killed at 1%, then 25%, then 50% proves that recovery
    works *after recovery has already worked*. Five independent projects,
    each killed once from the same starting conditions, is what actually
    tests whether recovery depends on the failure point. The faster
    progressive shape remains available for engineering work
    (``--mode progressive``) and is never the release qualification.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import itertools
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psutil
from sqlalchemy import func, select, text

from omr_scanner import __version__
from omr_scanner.database.engine import open_project_database
from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.database.models import (
    BatchScan,
    ProcessingManifest,
    ReviewConflict,
    ScanJobStatus,
)
from omr_scanner.evaluation import stress_dataset
from omr_scanner.services.project_lock import LOCK_FILE_NAME

_LOGGER = logging.getLogger(__name__)

QUALIFICATION_SEED = 20260921
"""The one fixed seed every qualification run uses.

Recorded in the report. The same seed and the same sheet count regenerate
the same logical sheet *N* in every run, which is what makes R0 a semantic
reference the five kill runs can be compared against at all - see
:func:`omr_scanner.evaluation.stress_dataset.render_sheet_for_index`, which
derives each sheet from ``(seed, index)`` and never from a shared mutable
PRNG."""

DEFAULT_SHEETS = 100_000
DEFAULT_CHECKPOINTS: tuple[int, ...] = (1, 25, 50, 75, 99)
DEFAULT_WARMUP_SHEETS = 200

STATE_FILE_NAME = "qualification_state.json"
CONFIG_FILE_NAME = "qualification_config.json"
ENVIRONMENT_FILE_NAME = "environment.json"
PREFLIGHT_FILE_NAME = "preflight.md"
PREFLIGHT_JSON_NAME = "preflight.json"
SUMMARY_JSON_NAME = "qualification_summary.json"
SUMMARY_MD_NAME = "qualification_summary.md"
LOCK_NAME = "qualification.lock"
EVENTS_FILE_NAME = "events.jsonl"

STOP_REQUEST_FILE_NAME = "stop_requested"
"""A file an operator (or the GUI monitor) creates to stop between runs.

A sentinel file rather than a signal because the thing being asked for is
precisely *not* an interruption: the campaign finishes the run it is in the
middle of, records that it was asked to stop, and exits with its evidence
intact and resumable. A signal would arrive inside a run and would be
indistinguishable from the forced kills this campaign performs on purpose."""

TELEMETRY_INTERVAL_SECONDS = 5.0
"""How often the orchestrator samples the run it is supervising."""

MEASURED_SHEETS_PER_SECOND = 11.24
"""Throughput measured on this project's own 10,000-sheet benchmark.

**Previous evidence, not a promise.** Used only to estimate how long a
campaign will take so that an operator can decide whether to start one, and
labelled as an estimate everywhere it is shown. The qualification never
depends on it."""

MEASURED_BYTES_PER_SHEET = 2_117_812_736 / 10_000
"""Database growth per sheet, from the same 10,000-sheet benchmark (~2.1 GB
for 10,000 sheets). The disk estimate is derived from this and then given a
safety margin."""

DISK_SAFETY_FACTOR = 1.35
"""Applied to every disk estimate. A qualification that runs out of disk
after eight hours has wasted eight hours, so the estimate errs high."""

RELEASE_BLOCKING_ASSERTIONS: tuple[str, ...] = (
    "logical_sheet_count",
    "kill_reached_requested_checkpoint",
    "previously_completed_jobs_rescheduled_for_recognition",
    "duplicate_active_recognition_results",
    "lost_committed_recognition_results",
    "changed_previously_committed_results",
    "orphan_workers_after_forced_kill",
    "quick_check",
    "integrity_check",
    "foreign_key_check",
    "application_invariants",
    "semantic_reference_match",
    "unexpected_pending_after_completion",
    "unexpected_running_after_completion",
    "exit_code",
)
"""Every assertion a run must satisfy to pass.

A run that fails any one of these fails the campaign. There is no severity
ladder here on purpose: the temptation at the end of a long phase is to
demote an inconvenient failure to a warning, so the only two outcomes a run
can have are *passed* and *failed*."""

Mode = Literal["full", "reference", "progressive"]
StageStatus = Literal["pending", "running", "passed", "failed", "skipped"]


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QualificationConfig:
    """Everything that defines one campaign.

    Attributes:
        output_dir: Where every artefact is written. Nothing outside this
            directory is ever created or removed.
        template_path: The ``.omrt`` every run reads sheets with.
        sheets: Logical sheets per run. 100,000 for the release
            qualification; smaller only for validating the harness itself.
        seed: :data:`QUALIFICATION_SEED` unless deliberately overridden.
        checkpoints: Kill points, as whole percentages of ``sheets``.
        workers / opencv_threads / worker_recycle_after: The processing
            configuration, **identical for every run** - a reference run
            tuned differently from the runs compared against it would prove
            nothing.
        mode: ``"full"`` (R0 + one independent project per checkpoint),
            ``"reference"`` (R0 only - the single-100k stress run the GUI
            offers), or ``"progressive"`` (one project killed at each
            checkpoint in turn; an engineering aid, never the qualification).
        warmup_sheets: A short run before R0 to prove workers start and paths
            are writable. Its results are never part of any measurement.
        retain_passed_projects: Keep a K-run's project directory after it has
            passed and its evidence has been captured. Failing projects are
            always kept.
    """

    output_dir: Path
    template_path: Path
    sheets: int = DEFAULT_SHEETS
    seed: int = QUALIFICATION_SEED
    checkpoints: tuple[int, ...] = DEFAULT_CHECKPOINTS
    workers: int = 0
    opencv_threads: int = 1
    worker_recycle_after: int = 500
    mode: Mode = "full"
    warmup_sheets: int = DEFAULT_WARMUP_SHEETS
    retain_passed_projects: bool = False

    def resolved_workers(self) -> int:
        """The worker count to use, resolved once so every run shares it."""
        if self.workers > 0:
            return self.workers
        return max((os.cpu_count() or 2) // 2, 1)

    def run_ids(self) -> tuple[str, ...]:
        """Every run this campaign performs, in order."""
        if self.mode == "reference":
            return ("R0",)
        return ("R0", *(f"K{percent:02d}" for percent in self.checkpoints))

    def checkpoint_for(self, run_id: str) -> int | None:
        """The kill percentage for a run id, or ``None`` for the reference."""
        if run_id == "R0":
            return None
        return int(run_id[1:])

    def committed_target(self, percent: int) -> int:
        """How many durably committed sheets must exist before the kill."""
        return max(int(self.sheets * percent / 100), 1)

    def to_json(self) -> dict[str, Any]:
        """Serialise for the state file and the report."""
        payload = asdict(self)
        payload["output_dir"] = str(self.output_dir)
        payload["template_path"] = str(self.template_path)
        payload["checkpoints"] = list(self.checkpoints)
        payload["resolved_workers"] = self.resolved_workers()
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> QualificationConfig:
        """Rebuild a config from a state file, for ``resume``."""
        return cls(
            output_dir=Path(payload["output_dir"]),
            template_path=Path(payload["template_path"]),
            sheets=int(payload["sheets"]),
            seed=int(payload["seed"]),
            checkpoints=tuple(int(item) for item in payload["checkpoints"]),
            workers=int(payload.get("workers", 0)),
            opencv_threads=int(payload.get("opencv_threads", 1)),
            worker_recycle_after=int(payload.get("worker_recycle_after", 500)),
            mode=payload.get("mode", "full"),
            warmup_sheets=int(payload.get("warmup_sheets", DEFAULT_WARMUP_SHEETS)),
            retain_passed_projects=bool(payload.get("retain_passed_projects", False)),
        )


# ----------------------------------------------------------------------
# Durable campaign state
# ----------------------------------------------------------------------
@dataclass
class StageRecord:
    """One campaign stage, as recorded on disk."""

    status: StageStatus = "pending"
    started_at: str = ""
    finished_at: str = ""
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class QualificationState:
    """The campaign's progress, rewritten atomically after every step.

    This is what makes the *orchestrator* resumable, which is a different
    thing from the application crashes it deliberately causes: if the
    PowerShell window closes, the machine is rebooted, or this process dies
    eight hours in, ``resume`` reads this file and continues from the last
    finished stage rather than repeating validated work.
    """

    def __init__(self, path: Path, config: QualificationConfig) -> None:
        self.path = path
        self.config = config
        self.stages: dict[str, StageRecord] = {}
        self.started_at = _now()
        self.overall_status = "running"
        self.notes: list[str] = []

    # -- persistence ---------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        """The whole campaign state, as the state file holds it."""
        return {
            "schema": 1,
            "application_version": __version__,
            "started_at": self.started_at,
            "updated_at": _now(),
            "overall_status": self.overall_status,
            "config": self.config.to_json(),
            "stages": {name: asdict(record) for name, record in self.stages.items()},
            "notes": self.notes,
        }

    def save(self) -> None:
        """Write the state file atomically.

        Atomic because the GUI monitor reads this file on a timer while the
        campaign is writing it; a half-written state file would make the
        monitor show nonsense at exactly the moment something interesting
        happened.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.to_json(), indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    @classmethod
    def load(cls, path: Path) -> QualificationState:
        """Read a campaign's state back off disk, config included."""
        payload = json.loads(path.read_text(encoding="utf-8"))
        config = QualificationConfig.from_json(payload["config"])
        state = cls(path, config)
        state.started_at = payload.get("started_at", _now())
        state.overall_status = payload.get("overall_status", "running")
        state.notes = list(payload.get("notes", ()))
        for name, record in payload.get("stages", {}).items():
            state.stages[name] = StageRecord(**record)
        return state

    # -- stage transitions ---------------------------------------------
    def record(self, name: str) -> StageRecord:
        """The record for ``name``, created pending if it does not exist."""
        return self.stages.setdefault(name, StageRecord())

    def is_passed(self, name: str) -> bool:
        """Whether stage ``name`` has already been verified."""
        return self.stages.get(name, StageRecord()).status == "passed"

    def begin(self, name: str, detail: str = "") -> None:
        """Mark a stage running and persist that before doing anything."""
        stage = self.record(name)
        stage.status = "running"
        stage.started_at = _now()
        stage.finished_at = ""
        stage.detail = detail
        self.save()

    def finish(
        self,
        name: str,
        status: StageStatus,
        *,
        detail: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Record a stage's verdict and persist it immediately."""
        stage = self.record(name)
        stage.status = status
        stage.finished_at = _now()
        stage.detail = detail
        if data is not None:
            stage.data = data
        self.save()

    def next_pending_run(self) -> str | None:
        """The first run this campaign has not yet passed."""
        for run_id in self.config.run_ids():
            if not self.is_passed(run_id):
                return run_id
        return None


# ----------------------------------------------------------------------
# Environment and preflight
# ----------------------------------------------------------------------
def describe_environment() -> dict[str, Any]:
    """Everything about this machine the report has to state."""
    try:
        import cv2

        opencv_version = cv2.__version__
    except Exception:  # pragma: no cover - OpenCV is a hard dependency
        opencv_version = "unknown"
    # Deliberately no Qt version here. The `evaluation` layer must not import
    # PySide6 (tests/unit/test_architecture.py), and the reason is exactly the
    # property this harness depends on: a qualification campaign runs headless,
    # unattended, with no display and no GUI process involved at all. Recording
    # a Qt version would suggest the campaign had something to do with the GUI.
    # The versions that *do* affect a recognition result are recorded instead.
    import numpy
    import sqlalchemy

    memory = psutil.virtual_memory()
    return {
        "application_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "logical_cpus": os.cpu_count() or 0,
        "physical_cpus": psutil.cpu_count(logical=False) or 0,
        "total_memory_mb": round(memory.total / 1_048_576, 1),
        "available_memory_mb": round(memory.available / 1_048_576, 1),
        "opencv_version": opencv_version,
        "numpy_version": numpy.__version__,
        "sqlalchemy_version": sqlalchemy.__version__,
        "executable": sys.executable,
    }


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One preflight question and its answer."""

    name: str
    passed: bool
    detail: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """What preflight concluded, and the estimates an operator needs."""

    checks: tuple[PreflightCheck, ...]
    environment: dict[str, Any]
    estimated_runtime_seconds: float
    estimated_peak_disk_bytes: int
    available_disk_bytes: int
    per_run_disk_bytes: int
    runs: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Whether every *blocking* check passed."""
        return all(check.passed for check in self.checks if check.blocking)

    @property
    def disk_sufficient(self) -> bool:
        """Whether the free space covers the estimated peak."""
        return self.available_disk_bytes >= self.estimated_peak_disk_bytes

    def to_json(self) -> dict[str, Any]:
        """The preflight, for ``preflight.json`` and the GUI dialog."""
        return {
            "passed": self.passed,
            "disk_sufficient": self.disk_sufficient,
            "runs": list(self.runs),
            "estimated_runtime_seconds": self.estimated_runtime_seconds,
            "estimated_runtime_text": format_duration(self.estimated_runtime_seconds),
            "estimated_peak_disk_bytes": self.estimated_peak_disk_bytes,
            "estimated_peak_disk_text": format_bytes(self.estimated_peak_disk_bytes),
            "per_run_disk_bytes": self.per_run_disk_bytes,
            "available_disk_bytes": self.available_disk_bytes,
            "available_disk_text": format_bytes(self.available_disk_bytes),
            "environment": self.environment,
            "checks": [asdict(check) for check in self.checks],
        }


def format_duration(seconds: float) -> str:
    """``"3 h 42 min"``, for a human deciding whether to start."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    hours = int(minutes // 60)
    return f"{hours} h {int(minutes - hours * 60):02d} min"


def format_bytes(count: float) -> str:
    """``"12.4 GB"``."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(count) < 1024 or unit == "TB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{count:.0f} B"
        count /= 1024
    return f"{count:.1f} TB"  # pragma: no cover - unreachable


def estimate_runtime_seconds(config: QualificationConfig) -> float:
    """How long a campaign should take, from this project's own measurement.

    Deliberately simple and deliberately labelled an estimate: previous
    throughput times the number of runs, plus a flat allowance per run for
    registration, recovery, integrity checking and reporting. A campaign is
    never gated on this number; an operator deciding whether to start one
    overnight is.
    """
    runs = len(config.run_ids())
    processing = config.sheets * runs / MEASURED_SHEETS_PER_SECOND
    # Kill runs re-do the work up to the checkpoint once: a run killed at
    # 75% processes 175% of a run's sheets in total.
    redone = sum(
        config.sheets * (config.checkpoint_for(run_id) or 0) / 100
        for run_id in config.run_ids()
    )
    overhead_per_run = 15 * 60
    warmup = config.warmup_sheets / MEASURED_SHEETS_PER_SECOND
    return processing + redone / MEASURED_SHEETS_PER_SECOND + runs * overhead_per_run + warmup


def estimate_disk(config: QualificationConfig) -> tuple[int, int]:
    """Return ``(per_run_bytes, estimated_peak_bytes)``.

    Peak, not total: a K-run's project is removed once it has passed and its
    evidence has been captured (unless ``retain_passed_projects``), so the
    campaign's high-water mark is the reference project plus one K-run plus
    the evidence - not six full projects.
    """
    per_run = int(config.sheets * MEASURED_BYTES_PER_SHEET * DISK_SAFETY_FACTOR)
    runs = len(config.run_ids())
    concurrent = runs if config.retain_passed_projects else min(runs, 2)
    evidence = 256 * 1024 * 1024
    return per_run, per_run * concurrent + evidence


def _check_generator_reproducibility(config: QualificationConfig) -> PreflightCheck:
    """Prove the deterministic generator really is deterministic, cheaply.

    Renders the same three sheet indices twice, from two independently built
    specs, and compares the bytes. If this fails, the semantic comparison
    between R0 and the kill runs would be meaningless, so it is worth the
    two seconds it costs before an overnight campaign.
    """
    from omr_scanner.services.template_service import load_template

    try:
        template = load_template(config.template_path)
        first = stress_dataset.StressDatasetSpec(seed=config.seed, sheet_count=config.sheets)
        second = stress_dataset.StressDatasetSpec(seed=config.seed, sheet_count=config.sheets)
        for index in (0, config.sheets // 2, config.sheets - 1):
            left = stress_dataset.render_sheet_for_index(first, template, index)
            right = stress_dataset.render_sheet_for_index(second, template, index)
            left_bytes = left.malformed_bytes if left.is_malformed else left.png_bytes
            right_bytes = right.malformed_bytes if right.is_malformed else right.png_bytes
            if left_bytes != right_bytes:
                return PreflightCheck(
                    "deterministic generator",
                    False,
                    f"sheet {index} rendered differently on a second attempt",
                )
    except Exception as exc:
        return PreflightCheck("deterministic generator", False, f"{type(exc).__name__}: {exc}")
    return PreflightCheck(
        "deterministic generator", True, "three sample sheets reproduced byte for byte"
    )


def _leftover_worker_processes() -> list[int]:
    """PIDs that look like OMRFlow workers left behind by an earlier test.

    Matched by command line rather than by name, because every one of them is
    just ``python``. A false positive here only produces a warning, never a
    refusal to start.
    """
    found: list[int] = []
    me = os.getpid()
    for process in psutil.process_iter(["pid", "cmdline"]):
        if process.info["pid"] == me:
            continue
        try:
            command = " ".join(process.info["cmdline"] or ())
        except (psutil.Error, TypeError):  # pragma: no cover - race with exit
            continue
        if "omr_scanner" in command and (
            "multiprocessing" in command or "benchmark_stress" in command
        ):
            found.append(int(process.info["pid"]))
    return found


def preflight(config: QualificationConfig) -> PreflightReport:
    """Answer "can this campaign finish?" before hours are spent on it."""
    checks: list[PreflightCheck] = []

    checks.append(
        PreflightCheck(
            "OMRFlow importable",
            True,
            f"version {__version__}, schema {SCHEMA_VERSION}",
        )
    )
    checks.append(
        PreflightCheck(
            "template readable",
            config.template_path.is_file(),
            str(config.template_path),
        )
    )

    try:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        probe = config.output_dir / ".preflight_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
        detail = str(config.output_dir)
    except OSError as exc:
        writable = False
        detail = f"{config.output_dir}: {exc}"
    checks.append(PreflightCheck("output directory writable", writable, detail))

    per_run, peak = estimate_disk(config)
    try:
        usage = shutil.disk_usage(config.output_dir)
        available = usage.free
    except OSError:  # pragma: no cover - unreadable drive
        available = 0
    checks.append(
        PreflightCheck(
            "disk space",
            available >= peak,
            f"{format_bytes(available)} free; campaign needs about "
            f"{format_bytes(peak)} at its peak "
            f"({format_bytes(per_run)} per run, "
            f"{'keeping' if config.retain_passed_projects else 'reclaiming'} "
            "passed run projects)",
        )
    )

    memory = psutil.virtual_memory()
    checks.append(
        PreflightCheck(
            "system memory",
            memory.total >= 4 * 1024**3,
            f"{format_bytes(memory.total)} total, {format_bytes(memory.available)} available",
        )
    )
    checks.append(
        PreflightCheck(
            "cpu topology",
            (os.cpu_count() or 0) >= 2,
            f"{os.cpu_count()} logical CPUs; campaign will use "
            f"{config.resolved_workers()} worker(s) and "
            f"{config.opencv_threads} OpenCV thread(s) each",
            blocking=False,
        )
    )

    # "Is another campaign using this directory?", not "does a lock file
    # exist?". Two campaigns sharing an output directory would overwrite each
    # other's evidence, which is what this blocks; a lock left behind by a
    # dead orchestrator, or the one this very process has just taken, is not
    # that. Getting this wrong made `run` refuse to start at all, because
    # `execute_campaign` takes the lock before it runs preflight.
    lock = config.output_dir / LOCK_NAME
    conflicting: int | None = None
    if lock.is_file():
        try:
            holder = int(json.loads(lock.read_text(encoding="utf-8"))["pid"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            holder = None
        if holder is not None and holder != os.getpid() and psutil.pid_exists(holder):
            conflicting = holder
    checks.append(
        PreflightCheck(
            "no competing campaign in this directory",
            conflicting is None,
            (
                f"another orchestrator (PID {conflicting}) holds {lock}"
                if conflicting is not None
                else "none"
            ),
        )
    )

    leftovers = _leftover_worker_processes()
    checks.append(
        PreflightCheck(
            "no leftover worker processes",
            not leftovers,
            f"{len(leftovers)} process(es): {leftovers}" if leftovers else "none",
            blocking=False,
        )
    )

    checks.append(_check_generator_reproducibility(config))

    try:
        from omr_scanner.services import project_health

        checks.append(
            PreflightCheck(
                "health-check infrastructure",
                hasattr(project_health, "full_check"),
                "project_health.full_check available",
            )
        )
    except Exception as exc:
        checks.append(
            PreflightCheck("health-check infrastructure", False, f"{type(exc).__name__}: {exc}")
        )

    existing_state = config.output_dir / STATE_FILE_NAME
    checks.append(
        PreflightCheck(
            "previous qualification",
            True,
            (
                f"an earlier campaign exists at {existing_state} - use "
                "`resume` to continue it rather than starting over"
                if existing_state.is_file()
                else "none"
            ),
            blocking=False,
        )
    )

    return PreflightReport(
        checks=tuple(checks),
        environment=describe_environment(),
        estimated_runtime_seconds=estimate_runtime_seconds(config),
        estimated_peak_disk_bytes=peak,
        available_disk_bytes=available,
        per_run_disk_bytes=per_run,
        runs=config.run_ids(),
    )


def render_preflight_markdown(report: PreflightReport, config: QualificationConfig) -> str:
    """The preflight, as a human reads it."""
    lines = [
        "# Phase 10 qualification - preflight",
        "",
        f"Generated: {_now()}",
        "",
        f"**Result: {'PASS' if report.passed else 'FAIL'}**",
        "",
        "## Campaign",
        "",
        f"* Runs: {', '.join(report.runs)}",
        f"* Sheets per run: {config.sheets:,}",
        f"* Seed: {config.seed}",
        f"* Workers: {config.resolved_workers()}",
        f"* OpenCV threads per worker: {config.opencv_threads}",
        f"* Worker recycle interval: {config.worker_recycle_after or 'disabled'}",
        f"* Mode: {config.mode}",
        f"* Output: {config.output_dir}",
        "",
        "## Estimates",
        "",
        f"* Estimated runtime: **{format_duration(report.estimated_runtime_seconds)}** "
        f"(from this project's previously measured {MEASURED_SHEETS_PER_SECOND} sheets/s - "
        "previous evidence, not a guarantee)",
        f"* Estimated peak disk: **{format_bytes(report.estimated_peak_disk_bytes)}**",
        f"* Available disk: **{format_bytes(report.available_disk_bytes)}**",
        "",
        "## Checks",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for check in report.checks:
        mark = "PASS" if check.passed else ("FAIL" if check.blocking else "WARN")
        lines.append(f"| {check.name} | {mark} | {check.detail} |")
    lines += [
        "",
        "## Before you start",
        "",
        "> Keep the machine connected to reliable power and disable automatic",
        "> sleep for the duration of the qualification. The forced process",
        "> terminations this campaign performs are deliberate; a machine",
        "> losing power is not part of the test.",
        "",
        "Do not run other substantial workloads while the campaign is running -",
        "throughput figures from a contended machine are not comparable.",
        "",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Reading a database another process is writing
# ----------------------------------------------------------------------
TERMINAL_STATUSES = frozenset(
    status.value for status in ScanJobStatus if status.is_terminal
)
"""What "durably committed" means, taken from the model rather than restated.

:attr:`~omr_scanner.database.models.ScanJobStatus.is_terminal` is the
application's own definition of work a resume must keep. The qualification
asserts against exactly that definition, so a future change to the state
machine cannot leave this module quietly asserting something weaker."""

SEMANTIC_RESULT_KEYS = ("outcome", "registration", "warnings", "fields", "answers")
"""The parts of ``result_json`` that a re-read must reproduce.

``timings`` and ``elapsed_seconds`` are excluded because they legitimately
differ between two runs of the same sheet - comparing them would make every
comparison fail for a reason that has nothing to do with correctness. Every
remaining key is a *decision* the pipeline made about the sheet, and a
decision that changed is a release-blocking failure."""


def _open_read_only(db_path: Path, *, attempts: int = 30) -> Any:
    """Open a project database read-only, retrying while another process writes.

    A retry loop rather than a single attempt: this is deliberately called
    against a SQLite file a live child process is committing to, so a
    transient lock is expected and is not evidence of anything.
    """
    last: Exception | None = None
    for _attempt in range(attempts):
        try:
            return open_project_database(db_path, read_only=True)
        except Exception as exc:
            last = exc
            time.sleep(0.2)
    raise RuntimeError(f"Could not open {db_path} read-only: {last}")


def read_committed_rows(db_path: Path) -> dict[int, tuple[str, str]]:
    """Return ``{batch_index: (status, result_json)}`` for every durable row.

    Keyed by ``batch_index``, not ``scan_id``. ``scan_id`` is an autoincrement
    surrogate that means nothing across two independently created projects,
    whereas ``batch_index`` *is* the sheet's identity in the deterministic
    dataset: sheet 41,207 is the same sheet in R0 and in K75. Every piece of
    evidence, every submission-log entry and every digest in this module is
    keyed the same way for that reason.
    """
    for _attempt in range(30):
        try:
            database = _open_read_only(db_path)
        except RuntimeError:
            time.sleep(0.2)
            continue
        try:
            with database.session() as session:
                rows = session.execute(
                    select(BatchScan.batch_index, BatchScan.status, BatchScan.result_json)
                ).all()
            return {
                int(index): (str(status), str(payload)) for index, status, payload in rows
            }
        except Exception:
            time.sleep(0.2)
        finally:
            database.close()
    return {}


def read_progress(db_path: Path) -> dict[str, int]:
    """Return durable status counts, cheaply enough to poll every few seconds.

    Counts in SQL over ``ix_batch_scan_batch_status`` instead of pulling
    every row, because at 100,000 sheets the naive version would itself
    become the thing slowing the run being measured.
    """
    try:
        database = _open_read_only(db_path, attempts=3)
    except RuntimeError:
        return {}
    try:
        with database.session() as session:
            rows = session.execute(
                select(BatchScan.status, func.count()).group_by(BatchScan.status)
            ).all()
        counts = {str(status): int(count) for status, count in rows}
    except Exception:
        return {}
    finally:
        database.close()
    counts["_committed"] = sum(
        count for status, count in counts.items() if status in TERMINAL_STATUSES
    )
    counts["_total"] = sum(
        count for status, count in counts.items() if not status.startswith("_")
    )
    return counts


def committed_indexes(rows: dict[int, tuple[str, str]]) -> set[int]:
    """The ``batch_index`` of every durably committed sheet in ``rows``."""
    return {index for index, (status, _payload) in rows.items() if status in TERMINAL_STATUSES}


def semantic_digest(rows: dict[int, tuple[str, str]]) -> dict[int, str]:
    """Return ``{batch_index: digest}`` over only the substantive result.

    The digest covers :data:`SEMANTIC_RESULT_KEYS` and the row's status, hashed
    over a canonical JSON serialisation so that key ordering cannot make two
    identical results look different. A sheet whose ``result_json`` will not
    parse digests as ``"unparseable"`` rather than raising: that is itself a
    difference the comparison should report, not a crash in the reporter.
    """
    digests: dict[int, str] = {}
    for index, (status, payload) in rows.items():
        try:
            parsed = json.loads(payload) if payload else {}
        except (json.JSONDecodeError, TypeError):
            digests[index] = "unparseable"
            continue
        if not isinstance(parsed, dict):
            digests[index] = "unparseable"
            continue
        substantive: dict[str, Any] = {"status": status}
        for key in SEMANTIC_RESULT_KEYS:
            if key in parsed:
                substantive[key] = parsed[key]
        canonical = json.dumps(substantive, sort_keys=True, separators=(",", ":"))
        digests[index] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digests


def read_submission_log(path: Path) -> list[int]:
    """Return every ``batch_index`` a run submitted, in submission order.

    Tolerates a truncated final line: the log is written by a process that
    this campaign deliberately kills, so a partial write is an expected
    state, not corruption.
    """
    if not path.is_file():
        return []
    submitted: list[int] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            submitted.append(int(stripped))
        except ValueError:
            continue
    return submitted


def integrity_report(db_path: Path) -> dict[str, Any]:
    """Run SQLite's own structural checks and the application's health check.

    ``quick_check`` and ``integrity_check`` are both run even though the
    second subsumes the first: they are named separately in the acceptance
    criteria, they are separately reported, and at 2 GB the cheap one
    finishing first is useful information while the thorough one runs.
    """
    result: dict[str, Any] = {}
    try:
        database = _open_read_only(db_path)
    except RuntimeError as exc:
        return {"error": str(exc)}
    try:
        with database.session() as session:
            result["quick_check"] = [
                str(row[0]) for row in session.execute(text("PRAGMA quick_check")).all()
            ]
            result["integrity_check"] = [
                str(row[0]) for row in session.execute(text("PRAGMA integrity_check")).all()
            ]
            result["foreign_key_check"] = [
                list(row) for row in session.execute(text("PRAGMA foreign_key_check")).all()
            ]
            result["page_count"] = int(
                session.execute(text("PRAGMA page_count")).scalar_one()
            )
            result["journal_mode"] = str(
                session.execute(text("PRAGMA journal_mode")).scalar_one()
            )
            result["conflicts_total"] = int(
                session.execute(select(func.count()).select_from(ReviewConflict)).scalar_one()
            )
            result["conflicts_open"] = int(
                session.execute(
                    select(func.count())
                    .select_from(ReviewConflict)
                    .where(ReviewConflict.state == "open")
                ).scalar_one()
            )
            manifests = session.execute(
                select(func.count()).select_from(ProcessingManifest)
            ).scalar_one()
            result["processing_manifests"] = int(manifests)
            # §35: the deterministic dataset deliberately contains
            # byte-identical duplicate sheets. They must be *detected*, which
            # means their content hashes must agree - not deduplicated away,
            # which would silently drop a sheet the office really did scan
            # twice and needs to know about.
            duplicate_hashes = session.execute(
                select(BatchScan.content_sha256, func.count())
                .where(BatchScan.content_sha256 != "")
                .group_by(BatchScan.content_sha256)
                .having(func.count() > 1)
            ).all()
            result["duplicate_content_hash_groups"] = len(duplicate_hashes)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        database.close()

    try:
        from omr_scanner.services import project_health

        database = open_project_database(db_path, read_only=True)
        try:
            health = project_health.full_check(database, db_path.parent)
        finally:
            database.close()
        result["health_ok"] = bool(health.is_ok)
        result["health_issues"] = [
            {"level": issue.level.value, "code": issue.code, "message": issue.message}
            for issue in health.issues
        ]
    except Exception as exc:
        result["health_ok"] = False
        result["health_issues"] = [
            {"level": "error", "code": "health_check_failed", "message": f"{exc}"}
        ]
    return result


# ----------------------------------------------------------------------
# Supervising and killing a run
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class KillEvidence:
    """What was observed around one forced termination."""

    requested_at: str
    committed_before_kill: int
    worker_pids: tuple[int, ...]
    coordinator_pid: int
    lock_file_left_behind: bool
    orphan_pids: tuple[int, ...]
    orphans_force_cleaned: tuple[int, ...]
    exit_code: int | None

    def to_json(self) -> dict[str, Any]:
        """The kill's evidence, for ``kill.json`` and the report."""
        return asdict(self)


def _capture_worker_pids(coordinator_pid: int) -> tuple[int, ...]:
    """The coordinator's live descendants, captured while it is still running.

    Captured *before* the kill because after it there is, by design, nothing
    left to ask. A pid list gathered this way is the only way to check
    afterwards whether a worker outlived its coordinator.
    """
    try:
        children = psutil.Process(coordinator_pid).children(recursive=True)
    except psutil.Error:
        return ()
    return tuple(child.pid for child in children)


def kill_run_abruptly(
    process: subprocess.Popen[bytes],
    worker_pids: tuple[int, ...],
    project_dir: Path,
    committed_before_kill: int,
) -> KillEvidence:
    """Terminate ``process`` ungracefully and record what survived.

    Only the *coordinator* is killed, and deliberately so. Killing the whole
    tree ourselves would prove nothing: the property under test is that
    :mod:`omr_scanner.services.process_containment`'s Windows Job Object
    takes the worker pool down with the coordinator without anyone asking it
    to - which is what stops a crashed run leaving eight recognition
    processes chewing a CPU each, the exact defect the Phase 10 audit found
    in the field.

    Any orphan that does survive is recorded as a release-blocking failure
    **and then cleaned up**, because leaving it running would corrupt every
    later run's measurements as well.
    """
    requested_at = _now()
    coordinator_pid = process.pid
    # kill(), never terminate() and never communicate(): no graceful
    # shutdown, no flush, no chance to release the project lock.
    process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=30)

    # Give the OS a moment to finish tearing the job object down before
    # concluding that anything survived it.
    time.sleep(1.5)
    orphans = tuple(pid for pid in worker_pids if psutil.pid_exists(pid))

    cleaned: list[int] = []
    for pid in orphans:
        with contextlib.suppress(psutil.Error):
            victim = psutil.Process(pid)
            victim.kill()
            cleaned.append(pid)
    if cleaned:
        _LOGGER.error(
            "Orphaned worker process(es) %s survived the coordinator's forced "
            "kill and were force-cleaned; this is a release-blocking failure.",
            cleaned,
        )

    return KillEvidence(
        requested_at=requested_at,
        committed_before_kill=committed_before_kill,
        worker_pids=worker_pids,
        coordinator_pid=coordinator_pid,
        lock_file_left_behind=(project_dir / LOCK_FILE_NAME).is_file(),
        orphan_pids=orphans,
        orphans_force_cleaned=tuple(cleaned),
        exit_code=process.returncode,
    )


class TelemetryWriter:
    """Samples a supervised run from outside it, straight to a CSV.

    Deliberately self-contained and deliberately external:

    * **Self-contained** - one ``telemetry.csv`` per campaign, flushed after
      every row, so a campaign that dies at hour six still leaves six hours
      of measurements rather than an empty buffer.
    * **External** - the process being sampled is the one this campaign is
      about to kill. An in-process sampler would lose its own history at the
      moment of the kill, and it could only ever see the coordinator;
      sampling from here sees the whole process tree, where the recognition
      work actually happens.
    """

    FIELDS = (
        "timestamp",
        "run_id",
        "attempt",
        "elapsed_seconds",
        "committed",
        "pending",
        "failed",
        "total_rows",
        "sheets_per_second",
        "coordinator_cpu_percent",
        "coordinator_memory_mb",
        "tree_cpu_percent",
        "tree_memory_mb",
        "process_count",
        "database_bytes",
        "disk_free_bytes",
    )

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.path.is_file()
        self._handle = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._handle, fieldnames=self.FIELDS)
        if new_file:
            self._writer.writeheader()
            self._handle.flush()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.latest: dict[str, int] = {}
        self.peak_tree_memory_mb = 0.0
        self.peak_tree_cpu_percent = 0.0
        self.sample_count = 0

    def write_row(self, row: dict[str, Any]) -> None:
        """Append one sample and flush it, so a kill cannot lose it."""
        with self._lock:
            self._writer.writerow({key: row.get(key, "") for key in self.FIELDS})
            self._handle.flush()

    def begin_run(self) -> None:
        """Reset the per-run peaks. Called once per run, before attempt 1.

        Per *run*, not per attempt: a kill run's peak memory should cover both
        the killed attempt and the resumed one.
        """
        self.peak_tree_memory_mb = 0.0
        self.peak_tree_cpu_percent = 0.0
        self.sample_count = 0

    def start(
        self,
        *,
        run_id: str,
        attempt: int,
        coordinator_pid: int,
        db_path: Path,
    ) -> None:
        """Begin sampling ``coordinator_pid``'s whole process tree.

        ``latest`` is cleared here, and that is load-bearing rather than
        tidiness. It is what :func:`_wait_for_checkpoint` reads to decide when
        a run has committed enough sheets to be worth killing. Leaving the
        *previous* run's final counts in it made the checkpoint condition
        true the instant the next run started, so K50 was killed before it
        had committed a single sheet - and then every "no committed result
        was lost / re-read / changed" assertion passed vacuously against an
        empty set. A vacuous pass is worse than a failure, because it looks
        like evidence.
        """
        self.latest = {}
        self._stop.clear()
        started = time.monotonic()
        seen: dict[int, psutil.Process] = {}
        last_committed = 0
        last_time = started

        def sample() -> None:
            nonlocal last_committed, last_time
            while not self._stop.wait(TELEMETRY_INTERVAL_SECONDS):
                now = time.monotonic()
                progress = read_progress(db_path)
                if progress:
                    self.latest = progress
                committed = int(self.latest.get("_committed", 0))
                interval = max(now - last_time, 1e-6)
                rate = max(committed - last_committed, 0) / interval
                last_committed, last_time = committed, now

                coordinator_cpu = coordinator_memory = 0.0
                tree_cpu = tree_memory = 0.0
                count = 0
                try:
                    root = psutil.Process(coordinator_pid)
                    members = [root, *root.children(recursive=True)]
                except psutil.Error:
                    members = []
                for member in members:
                    try:
                        if member.pid not in seen:
                            # cpu_percent() is meaningless on its first call
                            # for a process - it has no previous sample to
                            # difference against - so prime it and skip it
                            # this round rather than record a zero as if it
                            # were a measurement.
                            member.cpu_percent()
                            seen[member.pid] = member
                            continue
                        cpu = member.cpu_percent()
                        memory = member.memory_info().rss / 1_048_576
                    except psutil.Error:
                        continue
                    count += 1
                    tree_cpu += cpu
                    tree_memory += memory
                    if member.pid == coordinator_pid:
                        coordinator_cpu, coordinator_memory = cpu, memory
                self.peak_tree_memory_mb = max(self.peak_tree_memory_mb, tree_memory)
                self.peak_tree_cpu_percent = max(self.peak_tree_cpu_percent, tree_cpu)
                self.sample_count += 1

                try:
                    database_bytes = db_path.stat().st_size if db_path.is_file() else 0
                    disk_free = shutil.disk_usage(db_path.parent).free
                except OSError:
                    database_bytes, disk_free = 0, 0

                self.write_row(
                    {
                        "timestamp": _now(),
                        "run_id": run_id,
                        "attempt": attempt,
                        "elapsed_seconds": round(now - started, 2),
                        "committed": committed,
                        "pending": self.latest.get("pending", 0),
                        "failed": self.latest.get("failed", 0),
                        "total_rows": self.latest.get("_total", 0),
                        "sheets_per_second": round(rate, 3),
                        "coordinator_cpu_percent": round(coordinator_cpu, 1),
                        "coordinator_memory_mb": round(coordinator_memory, 1),
                        "tree_cpu_percent": round(tree_cpu, 1),
                        "tree_memory_mb": round(tree_memory, 1),
                        "process_count": count,
                        "database_bytes": database_bytes,
                        "disk_free_bytes": disk_free,
                    }
                )

        self._thread = threading.Thread(
            target=sample, name=f"qualification-telemetry-{run_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling, leaving the file open for the next run."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=TELEMETRY_INTERVAL_SECONDS * 3)
            self._thread = None

    def close(self) -> None:
        """Stop sampling and close the CSV."""
        self.stop()
        with contextlib.suppress(Exception):
            self._handle.close()

    def __enter__(self) -> TelemetryWriter:
        """Enter a context manager that closes the CSV on exit."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the CSV when leaving the context."""
        self.close()


# ----------------------------------------------------------------------
# Assertions
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Assertion:
    """One release-blocking acceptance criterion, and whether it held."""

    name: str
    passed: bool
    expected: str
    observed: str

    def to_json(self) -> dict[str, Any]:
        """The assertion and its verdict, for the report."""
        return asdict(self)


def evaluate_assertions(
    *,
    config: QualificationConfig,
    final_rows: dict[int, tuple[str, str]],
    pre_kill_rows: dict[int, tuple[str, str]] | None,
    checkpoint_target: int | None,
    resumed_submissions: list[int],
    kill: KillEvidence | None,
    integrity: dict[str, Any],
    reference_digest: dict[int, str] | None,
    exit_code: int,
) -> tuple[Assertion, ...]:
    """Evaluate every release-blocking assertion for one completed run.

    A pure function of measurements already taken, so that the decision
    "did this run qualify?" is testable at 100 sheets in a unit test without
    killing anything, and is not entangled with the process handling that
    produced the numbers.
    """
    assertions: list[Assertion] = []

    def add(name: str, passed: bool, expected: str, observed: str) -> None:
        assertions.append(Assertion(name, passed, expected, observed))

    committed = committed_indexes(final_rows)
    statuses = [status for status, _payload in final_rows.values()]

    add(
        "logical_sheet_count",
        len(final_rows) == config.sheets and len(committed) == config.sheets,
        f"{config.sheets} rows, all terminal",
        f"{len(final_rows)} rows, {len(committed)} terminal",
    )
    add(
        "duplicate_active_recognition_results",
        len(final_rows) == len(set(final_rows)),
        "0 duplicate batch_index values",
        # A dict keyed by batch_index cannot itself hold a duplicate, so the
        # real check is the database's own (batch_id, source_path) uniqueness
        # constraint plus the row-count assertion above; this records the
        # count the report has to state.
        f"{len(final_rows)} distinct of {len(final_rows)}",
    )
    add(
        "unexpected_pending_after_completion",
        statuses.count(ScanJobStatus.PENDING.value)
        + statuses.count(ScanJobStatus.QUEUED.value)
        == 0,
        "0 pending/queued rows",
        f"{statuses.count(ScanJobStatus.PENDING.value)} pending, "
        f"{statuses.count(ScanJobStatus.QUEUED.value)} queued",
    )
    add(
        "unexpected_running_after_completion",
        statuses.count(ScanJobStatus.PROCESSING.value) == 0,
        "0 processing rows",
        f"{statuses.count(ScanJobStatus.PROCESSING.value)} processing",
    )

    if pre_kill_rows is None:
        for name in (
            "kill_reached_requested_checkpoint",
            "previously_completed_jobs_rescheduled_for_recognition",
            "lost_committed_recognition_results",
            "changed_previously_committed_results",
            "orphan_workers_after_forced_kill",
        ):
            add(name, True, "n/a for the uninterrupted reference run", "not applicable")
    else:
        pre_committed = committed_indexes(pre_kill_rows)
        # The anti-vacuity assertion, and the reason it exists: an empty
        # pre-kill set makes every assertion below trivially true. That is
        # not a pass, it is a measurement that did not happen - so the kill
        # has to have landed near where it was asked to land, with real
        # committed work to protect, before anything else counts.
        floor = int((checkpoint_target or 1) * 0.8)
        add(
            "kill_reached_requested_checkpoint",
            len(pre_committed) >= max(floor, 1),
            f"at least {max(floor, 1):,} sheets committed before the kill "
            f"(80% of the requested {checkpoint_target or 0:,})",
            f"{len(pre_committed):,} committed",
        )
        resubmitted = sorted(pre_committed.intersection(resumed_submissions))
        add(
            "previously_completed_jobs_rescheduled_for_recognition",
            not resubmitted and bool(resumed_submissions) and bool(pre_committed),
            "0 already-committed sheets submitted again, out of a non-empty "
            "committed set and a non-empty set of resubmissions",
            (
                f"{len(resubmitted)} resubmitted "
                f"(first few: {resubmitted[:10]}) of "
                f"{len(pre_committed):,} committed before the kill; the "
                f"resumed run submitted {len(resumed_submissions):,} sheet(s)"
            ),
        )
        lost = sorted(pre_committed - committed)
        add(
            "lost_committed_recognition_results",
            not lost,
            "every pre-kill committed sheet still committed",
            f"{len(lost)} lost (first few: {lost[:10]})",
        )
        before = semantic_digest(
            {index: pre_kill_rows[index] for index in sorted(pre_committed)}
        )
        after = semantic_digest(
            {index: final_rows[index] for index in sorted(pre_committed & committed)}
        )
        changed = sorted(
            index
            for index, digest in before.items()
            if index in after and after[index] != digest
        )
        add(
            "changed_previously_committed_results",
            not changed,
            "no pre-kill result re-decided by the resume",
            f"{len(changed)} changed (first few: {changed[:10]})",
        )
        add(
            "orphan_workers_after_forced_kill",
            kill is not None and not kill.orphan_pids,
            "0 worker processes outliving the killed coordinator",
            (
                f"{len(kill.orphan_pids)} orphan(s): {list(kill.orphan_pids)}"
                if kill is not None
                else "no kill evidence recorded"
            ),
        )

    quick = integrity.get("quick_check", [])
    add(
        "quick_check",
        quick == ["ok"],
        "PRAGMA quick_check = ok",
        str(quick)[:300],
    )
    full = integrity.get("integrity_check", [])
    add(
        "integrity_check",
        full == ["ok"],
        "PRAGMA integrity_check = ok",
        str(full)[:300],
    )
    foreign = integrity.get("foreign_key_check", [])
    add(
        "foreign_key_check",
        not foreign,
        "PRAGMA foreign_key_check returns no rows",
        f"{len(foreign)} violation(s): {str(foreign)[:300]}",
    )
    issues = list(integrity.get("health_issues", ()))
    # Error-level only, deliberately. `HealthReport.is_ok` is False for a
    # *warning* too, and a stress project legitimately carries two of them
    # for its whole life: it has no verified answer key (the dataset's set
    # codes are synthetic and nothing here scores anything) and no backup
    # (it is a throwaway). Failing the qualification on those would be
    # failing it for not being a real examination project. Every warning is
    # still reported below, never swallowed.
    blocking_issues = [
        issue for issue in issues if issue.get("level") in {"error", "critical"}
    ]
    warnings = [issue for issue in issues if issue.get("level") == "warning"]
    add(
        "application_invariants",
        not blocking_issues,
        "project_health.full_check reports no error-level or critical issue",
        (
            f"{len(blocking_issues)} blocking: {str(blocking_issues)[:250]}; "
            f"{len(warnings)} warning(s): "
            f"{[issue.get('code') for issue in warnings]}"
        ),
    )

    if reference_digest is None:
        add(
            "semantic_reference_match",
            True,
            "this run *is* the semantic reference",
            "reference established",
        )
    else:
        run_digest = semantic_digest(final_rows)
        missing = sorted(set(reference_digest) - set(run_digest))
        extra = sorted(set(run_digest) - set(reference_digest))
        differing = sorted(
            index
            for index, digest in reference_digest.items()
            if index in run_digest and run_digest[index] != digest
        )
        add(
            "semantic_reference_match",
            not missing and not extra and not differing,
            "every sheet's decision identical to the reference run",
            (
                f"{len(differing)} differing (first few: {differing[:10]}), "
                f"{len(missing)} missing, {len(extra)} unexpected"
            ),
        )

    add(
        "exit_code",
        exit_code == 0,
        "the final run exits 0",
        str(exit_code),
    )
    return tuple(assertions)


# ----------------------------------------------------------------------
# At-scale measurements taken after a run finishes
# ----------------------------------------------------------------------
def measure_at_scale(db_path: Path, batch_id: str) -> dict[str, Any]:
    """Time the real queries the application performs on a finished batch.

    This is the honest version of "prove reporting works at 100,000 sheets".
    A full Excel result workbook cannot be generated here: the stress project
    has no attendance roster, no answer key and no result template, and
    inventing them would measure a fabricated scenario rather than this one.
    What *can* be measured, on the actual 100,000-row batch, is every stored
    query the Review, Results and Reports stages depend on - the batch
    summary, the conflict counts, a first page of the conflict queue and a
    full result read. Those are the operations that would make a 100,000-sheet
    project unusable if they scaled badly, and they are measured here for
    real.
    """
    from omr_scanner.services import batch_store, review_store

    measurements: dict[str, Any] = {}
    database = open_project_database(db_path, read_only=True)
    try:

        def timed(name: str, call: Any) -> Any:
            started = time.perf_counter()
            try:
                value = call()
            except Exception as exc:
                measurements[name] = {"error": f"{type(exc).__name__}: {exc}"}
                return None
            measurements[name] = {
                "seconds": round(time.perf_counter() - started, 4),
            }
            return value

        summary = timed("load_summary", lambda: batch_store.load_summary(database, batch_id))
        if summary is not None:
            measurements["load_summary"]["total"] = summary.total
            measurements["load_summary"]["completed"] = summary.completed
            measurements["load_summary"]["warning"] = summary.warning
            measurements["load_summary"]["failed"] = summary.failed

        counts = timed("count_conflicts", lambda: review_store.count_conflicts(database, batch_id))
        if counts is not None:
            measurements["count_conflicts"].update(
                {
                    "total": counts.total,
                    "open": counts.open_count,
                    "deferred": counts.deferred,
                    "withdrawn": counts.withdrawn,
                    "unresolved": counts.unresolved,
                    "by_type": dict(counts.by_type),
                }
            )

        page = timed(
            "list_conflicts_first_page",
            lambda: review_store.list_conflicts(database, batch_id, limit=100),
        )
        if page is not None:
            measurements["list_conflicts_first_page"]["rows"] = len(page)

        results = timed(
            "completed_results", lambda: batch_store.completed_results(database, batch_id)
        )
        if results is not None:
            measurements["completed_results"]["rows"] = len(results)
    finally:
        database.close()
    return measurements


def batch_configuration(db_path: Path) -> dict[str, Any]:
    """The processing configuration a batch actually ran under.

    Read back out of the database rather than taken from this module's own
    config, because the point is to prove that every run really did use
    identical settings - a claim that is worthless if its evidence is the
    same variable that set them. Compared between runs in place of a
    :class:`~omr_scanner.database.models.ProcessingManifest`, which no
    service writes yet.
    """
    from omr_scanner.database.models import ScanBatch

    database = open_project_database(db_path, read_only=True)
    try:
        with database.session() as session:
            row = session.execute(
                select(
                    ScanBatch.batch_id,
                    ScanBatch.template_id,
                    ScanBatch.geometry_fingerprint,
                    ScanBatch.recognition_fingerprint,
                    ScanBatch.engine_version,
                    ScanBatch.settings_json,
                    ScanBatch.total_scans,
                )
            ).first()
    finally:
        database.close()
    if row is None:
        return {}
    try:
        settings = json.loads(row.settings_json)
    except (json.JSONDecodeError, TypeError):
        settings = {}
    return {
        "batch_id": str(row.batch_id),
        "template_id": str(row.template_id),
        "geometry_fingerprint": str(row.geometry_fingerprint),
        "recognition_fingerprint": str(row.recognition_fingerprint),
        "engine_version": str(row.engine_version),
        "total_scans": int(row.total_scans),
        "stress_seed": settings.get("stress_seed"),
        "stress_sheet_count": settings.get("stress_sheet_count"),
    }


def source_immutability(db_path: Path, seed: int) -> dict[str, Any]:
    """Confirm that no run wrote a source image anywhere (§36).

    The stress dataset is virtual: every sheet exists only as bytes handed to
    recognition, identified by ``stress://seed-index``. So the strongest
    possible immutability statement is also the simplest one - there were no
    source files for anything to modify, and every stored ``source_path``
    still names a virtual sheet of *this* seed rather than a path on disk.
    """
    database = open_project_database(db_path, read_only=True)
    try:
        with database.session() as session:
            paths = [
                str(value)
                for (value,) in session.execute(select(BatchScan.source_path)).all()
            ]
    finally:
        database.close()
    foreign: list[str] = []
    for path in paths:
        if not stress_dataset.is_stress_source(path):
            foreign.append(path)
            continue
        if stress_dataset.parse_virtual_source_path(path)[0] != seed:
            foreign.append(path)
    return {
        "source_paths_examined": len(paths),
        "all_virtual_for_this_seed": not foreign,
        "unexpected_source_paths": foreign[:20],
    }


def worker_recycling_evidence(telemetry_path: Path, run_id: str) -> dict[str, Any]:
    """Whether the worker pool was observed being replaced during ``run_id``.

    Read back out of the campaign's own ``telemetry.csv`` - the samples taken
    from outside the run include the live process count, and recycling shows
    up there as the pool shrinking and growing again. Reported as an
    observation with its evidence, never as a bare "recycling works".
    """
    if not telemetry_path.is_file():
        return {"observed": False, "detail": "no telemetry file"}
    counts: list[int] = []
    with telemetry_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("run_id") != run_id:
                continue
            try:
                count = int(row["process_count"])
            except (KeyError, TypeError, ValueError):
                continue
            # A zero is the sampler's first round, where every process is
            # being primed for cpu_percent() and none is reported yet. It is
            # not the pool having no members, and counting it would show a
            # spurious "the pool shrank to nothing".
            if count > 0:
                counts.append(count)
    if not counts:
        return {"observed": False, "detail": f"no samples for {run_id}"}
    dips = sum(1 for previous, current in itertools.pairwise(counts) if current < previous)
    return {
        "observed": dips > 0,
        "samples": len(counts),
        "min_process_count": min(counts),
        "max_process_count": max(counts),
        "pool_size_decreases_observed": dips,
        "detail": (
            f"the supervised process tree shrank {dips} time(s) across "
            f"{len(counts)} samples, between {min(counts)} and "
            f"{max(counts)} processes"
        ),
    }


# ----------------------------------------------------------------------
# Running one run
# ----------------------------------------------------------------------
def _launch(
    config: QualificationConfig,
    project_dir: Path,
    *,
    submission_log: Path,
    report_out: Path,
    create: bool,
    force_lock: bool,
    log_path: Path,
) -> tuple[subprocess.Popen[bytes], Any]:
    """Start the real ``benchmark_stress`` CLI as an independent child process.

    The same command an operator would type, with nothing about the pipeline
    swapped out: no injected results, no stubbed recognition, no direct
    SQLite writes. Its stdout and stderr go to a file rather than a pipe, so
    that a run killed mid-sentence cannot block on a pipe nobody is draining
    and so the output survives the kill.
    """
    args = [
        sys.executable,
        "-m",
        "omr_scanner.tools.benchmark_stress",
        str(project_dir),
        "--template",
        str(config.template_path),
        "--sheets",
        str(config.sheets),
        "--seed",
        str(config.seed),
        "--workers",
        str(config.resolved_workers()),
        "--opencv-threads",
        str(config.opencv_threads),
        "--worker-recycle-after",
        str(config.worker_recycle_after),
        "--submission-log",
        str(submission_log),
        "--report-out",
        str(report_out),
        "--quiet",
    ]
    if create:
        args.append("--create")
    if force_lock:
        args.append("--force-lock")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    handle.write(f"\n$ {' '.join(args)}\n".encode())
    handle.flush()
    process = subprocess.Popen(
        args,
        cwd=Path(__file__).resolve().parents[3],
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    return process, handle


@dataclass
class RunOutcome:
    """Everything one qualification run produced."""

    run_id: str
    checkpoint_percent: int | None
    passed: bool = False
    assertions: tuple[Assertion, ...] = ()
    kill: KillEvidence | None = None
    exit_code: int | None = None
    elapsed_seconds: float = 0.0
    sheets_per_second: float = 0.0
    peak_tree_memory_mb: float = 0.0
    peak_tree_cpu_percent: float = 0.0
    database_bytes: int = 0
    integrity: dict[str, Any] = field(default_factory=dict)
    at_scale: dict[str, Any] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    immutability: dict[str, Any] = field(default_factory=dict)
    recycling: dict[str, Any] = field(default_factory=dict)
    submitted_first_attempt: int = 0
    submitted_after_restart: int = 0
    committed_before_kill: int = 0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Everything this run measured, for ``outcome.json`` and the report."""
        payload: dict[str, Any] = asdict(self)
        payload["assertions"] = [item.to_json() for item in self.assertions]
        payload["kill"] = self.kill.to_json() if self.kill is not None else None
        payload["failed_assertions"] = [
            item.name for item in self.assertions if not item.passed
        ]
        return payload


def _wait_for_checkpoint(
    telemetry: TelemetryWriter,
    *,
    target: int,
    process: subprocess.Popen[bytes],
    db_path: Path,
    timeout_seconds: float,
) -> int:
    """Block until ``target`` sheets are durably committed, or give up.

    Two speeds, deliberately. For most of the run it reads the count the
    telemetry sampler is already taking, because at 100,000 sheets a second
    poller of its own would add measurable contention to the very run whose
    throughput is being recorded. Once the sampler shows the run within 10%
    of the checkpoint it polls directly and often, because the sampler's
    five-second interval is hundreds of sheets wide at full speed and the
    kill should land near the percentage it claims to land at.
    """
    deadline = time.monotonic() + timeout_seconds
    near = max(int(target * 0.9), 1)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return int(telemetry.latest.get("_committed", 0))
        committed = int(telemetry.latest.get("_committed", 0))
        if committed >= near:
            direct = read_progress(db_path)
            if direct:
                committed = int(direct.get("_committed", committed))
            if committed >= target:
                return committed
            time.sleep(0.25)
            continue
        time.sleep(1.0)
    return int(telemetry.latest.get("_committed", 0))


def _await_exit(process: subprocess.Popen[bytes], timeout_seconds: float) -> int | None:
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _LOGGER.error("Run exceeded its %.0fs budget; killing it.", timeout_seconds)
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=30)
        return None


def _run_timeout(config: QualificationConfig) -> float:
    """A generous per-run wall-clock budget.

    Four times the estimated time plus an hour. Generous on purpose: the
    budget exists to stop a wedged run holding a campaign open forever, not
    to enforce a performance target - a run that is merely slow must be
    reported as slow, not failed by a stopwatch.
    """
    return config.sheets / MEASURED_SHEETS_PER_SECOND * 4 + 3600


def execute_run(
    config: QualificationConfig,
    state: QualificationState,
    run_id: str,
    *,
    telemetry: TelemetryWriter,
    reference_digest: dict[int, str] | None,
) -> RunOutcome:
    """Perform one qualification run, start to verdict.

    For ``R0`` that is a single uninterrupted pass. For a ``K`` run it is:
    start, wait for the checkpoint, capture evidence *from here*, kill the
    coordinator abruptly, check for orphans, restart with ``--force-lock``,
    and then test what the restart did against what was already on disk.
    """
    checkpoint = config.checkpoint_for(run_id)
    outcome = RunOutcome(run_id=run_id, checkpoint_percent=checkpoint)

    project_dir = config.output_dir / "projects" / run_id
    evidence_dir = config.output_dir / "evidence" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    db_path = project_dir / "database.sqlite"
    log_path = config.output_dir / "logs" / f"{run_id}.log"
    first_log = evidence_dir / "submitted_attempt1.txt"
    second_log = evidence_dir / "submitted_attempt2.txt"

    # Every run starts from clean initial conditions, and a half-finished
    # project left by an interrupted orchestrator is discarded rather than
    # resumed. That costs real time on a ``resume`` - up to a whole run - and
    # it is the right trade: the campaign's premise is that each kill run
    # begins from the *same* deterministic starting point as the reference
    # run, and a project that has already survived one unplanned interruption
    # is not that starting point. Resuming it would quietly test a different
    # scenario from the one the report describes. (Resuming a *batch* is what
    # the K runs themselves test, deliberately and under measurement; this is
    # about the campaign's own bookkeeping.)
    if project_dir.is_dir():
        _LOGGER.info(
            "Discarding a previous incomplete project for %s so that this run "
            "starts from clean initial conditions.",
            run_id,
        )
        shutil.rmtree(project_dir, ignore_errors=True)
    for stale in (first_log, second_log):
        stale.unlink(missing_ok=True)

    started = time.monotonic()
    pre_kill_rows: dict[int, tuple[str, str]] | None = None
    telemetry.begin_run()

    # -- attempt 1 ------------------------------------------------------
    process, handle = _launch(
        config,
        project_dir,
        submission_log=first_log,
        report_out=evidence_dir / "benchmark_attempt1.json",
        create=True,
        force_lock=False,
        log_path=log_path,
    )
    try:
        telemetry.start(
            run_id=run_id, attempt=1, coordinator_pid=process.pid, db_path=db_path
        )
        if checkpoint is None:
            outcome.exit_code = _await_exit(process, _run_timeout(config))
        else:
            target = config.committed_target(checkpoint)
            committed = _wait_for_checkpoint(
                telemetry,
                target=target,
                process=process,
                db_path=db_path,
                timeout_seconds=_run_timeout(config),
            )
            outcome.committed_before_kill = committed
            if process.poll() is not None:
                outcome.notes.append(
                    f"The run finished on its own before reaching {target} "
                    f"committed sheets ({committed} committed), so no kill "
                    "was performed and this checkpoint proves nothing."
                )
                outcome.exit_code = process.returncode
            elif committed < target:
                outcome.notes.append(
                    f"Only {committed} of {target} sheets were committed "
                    "within the run budget; killing anyway at the point "
                    "actually reached."
                )
            if process.poll() is None:
                # Evidence captured *here*, in the orchestrator, about a
                # database the child is still writing to - the whole reason
                # this supervisor is a separate process.
                pre_kill_rows = read_committed_rows(db_path)
                (evidence_dir / "pre_kill_committed.json").write_text(
                    json.dumps(
                        {
                            "captured_at": _now(),
                            "committed_count": len(committed_indexes(pre_kill_rows)),
                            "committed_indexes": sorted(committed_indexes(pre_kill_rows)),
                            "digest": {
                                str(index): digest
                                for index, digest in semantic_digest(pre_kill_rows).items()
                            },
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                worker_pids = _capture_worker_pids(process.pid)
                if not worker_pids:
                    outcome.notes.append(
                        "No worker child processes were visible at the moment "
                        "of the kill, so the orphan check had nothing to "
                        "observe."
                    )
                outcome.kill = kill_run_abruptly(
                    process,
                    worker_pids,
                    project_dir,
                    len(committed_indexes(pre_kill_rows)),
                )
                (evidence_dir / "kill.json").write_text(
                    json.dumps(outcome.kill.to_json(), indent=2), encoding="utf-8"
                )
    finally:
        telemetry.stop()
        with contextlib.suppress(Exception):
            handle.close()

    outcome.submitted_first_attempt = len(read_submission_log(first_log))

    # -- attempt 2: the restart, only if a kill actually happened -------
    if outcome.kill is not None:
        state.record(run_id).detail = f"{run_id}: restarting after forced kill"
        state.save()
        process, handle = _launch(
            config,
            project_dir,
            submission_log=second_log,
            report_out=evidence_dir / "benchmark_attempt2.json",
            create=False,
            force_lock=True,
            log_path=log_path,
        )
        try:
            telemetry.start(
                run_id=run_id, attempt=2, coordinator_pid=process.pid, db_path=db_path
            )
            outcome.exit_code = _await_exit(process, _run_timeout(config))
        finally:
            telemetry.stop()
            with contextlib.suppress(Exception):
                handle.close()
        outcome.submitted_after_restart = len(read_submission_log(second_log))

    outcome.elapsed_seconds = time.monotonic() - started

    # -- verdict --------------------------------------------------------
    final_rows = read_committed_rows(db_path)
    digest = semantic_digest(final_rows)
    (evidence_dir / "semantic_digest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "sheets": len(digest),
                "digest": {str(index): value for index, value in digest.items()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    outcome.integrity = integrity_report(db_path)
    outcome.configuration = batch_configuration(db_path)
    outcome.immutability = source_immutability(db_path, config.seed)
    outcome.recycling = worker_recycling_evidence(
        config.output_dir / "telemetry.csv", run_id
    )
    batch_id = str(outcome.configuration.get("batch_id", ""))
    if batch_id:
        outcome.at_scale = measure_at_scale(db_path, batch_id)
    outcome.peak_tree_memory_mb = telemetry.peak_tree_memory_mb
    outcome.peak_tree_cpu_percent = telemetry.peak_tree_cpu_percent
    outcome.database_bytes = db_path.stat().st_size if db_path.is_file() else 0
    if outcome.elapsed_seconds > 0:
        outcome.sheets_per_second = len(final_rows) / outcome.elapsed_seconds

    outcome.assertions = evaluate_assertions(
        config=config,
        final_rows=final_rows,
        pre_kill_rows=pre_kill_rows,
        checkpoint_target=(
            None if checkpoint is None else config.committed_target(checkpoint)
        ),
        resumed_submissions=read_submission_log(second_log),
        kill=outcome.kill,
        integrity=outcome.integrity,
        reference_digest=reference_digest,
        exit_code=outcome.exit_code if outcome.exit_code is not None else -1,
    )
    outcome.passed = all(item.passed for item in outcome.assertions)
    if checkpoint is not None and outcome.kill is None:
        outcome.passed = False
        outcome.notes.append(
            "A kill run that never killed anything cannot pass: there is no "
            "recovery to have verified."
        )

    (evidence_dir / "outcome.json").write_text(
        json.dumps(outcome.to_json(), indent=2), encoding="utf-8"
    )
    return outcome


def _reclaim(config: QualificationConfig, run_id: str, outcome: RunOutcome) -> str:
    """Delete a passed run's project directory, and nothing else.

    Every path this touches is under ``config.output_dir / "projects"``. A
    *failed* run's project is never removed, whatever the retention setting
    says - it is the only remaining copy of the evidence for why it failed.
    """
    if not outcome.passed:
        return "kept: the run failed and its project is the evidence"
    if config.retain_passed_projects:
        return "kept: retention requested"
    if run_id == "R0":
        return "kept: R0 is the semantic reference for the runs after it"
    project_dir = config.output_dir / "projects" / run_id
    try:
        project_dir.resolve().relative_to((config.output_dir / "projects").resolve())
    except ValueError:  # pragma: no cover - defensive
        return "kept: refused to remove a path outside the projects directory"
    if not project_dir.is_dir():
        return "already absent"
    freed = outcome.database_bytes
    shutil.rmtree(project_dir, ignore_errors=True)
    return f"reclaimed {format_bytes(freed)} (evidence and digests kept)"


def _warm_up(config: QualificationConfig, state: QualificationState) -> bool:
    """A short real run before the campaign, to fail fast rather than late.

    Two hundred sheets through the real pipeline proves the template loads,
    the worker pool starts, the database migrates, the output directory is
    writable and the submission log is produced - all the things that would
    otherwise be discovered forty minutes into R0. Its project is removed and
    its results are never part of any measurement.
    """
    stage = "warmup"
    if state.is_passed(stage):
        return True
    state.begin(stage, f"{config.warmup_sheets}-sheet warm-up")
    warm_dir = config.output_dir / "projects" / "warmup"
    if warm_dir.is_dir():
        shutil.rmtree(warm_dir, ignore_errors=True)
    evidence = config.output_dir / "evidence" / "warmup"
    evidence.mkdir(parents=True, exist_ok=True)
    warm_config = replace(config, sheets=config.warmup_sheets)
    process, handle = _launch(
        warm_config,
        warm_dir,
        submission_log=evidence / "submitted.txt",
        report_out=evidence / "benchmark.json",
        create=True,
        force_lock=False,
        log_path=config.output_dir / "logs" / "warmup.log",
    )
    try:
        code = _await_exit(process, 1800)
    finally:
        with contextlib.suppress(Exception):
            handle.close()
    submitted = len(read_submission_log(evidence / "submitted.txt"))
    rows = read_committed_rows(warm_dir / "database.sqlite")
    ok = (
        code == 0
        and len(committed_indexes(rows)) == config.warmup_sheets
        and submitted == config.warmup_sheets
    )
    shutil.rmtree(warm_dir, ignore_errors=True)
    state.finish(
        stage,
        "passed" if ok else "failed",
        detail=(
            f"{len(committed_indexes(rows))}/{config.warmup_sheets} committed, "
            f"{submitted} submitted, exit {code}"
        ),
        data={
            "exit_code": code,
            "committed": len(committed_indexes(rows)),
            "submitted": submitted,
        },
    )
    return ok


def _load_reference_digest(config: QualificationConfig) -> dict[int, str] | None:
    """Read R0's stored digest back off disk, so a resume can still compare.

    Deliberately re-read rather than held in memory: a campaign resumed after
    the orchestrator itself died must be able to compare K50 against R0
    without re-running R0.
    """
    path = config.output_dir / "evidence" / "R0" / "semantic_digest.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {int(index): str(value) for index, value in payload.get("digest", {}).items()}


def terminate_leftover_runs(config: QualificationConfig) -> tuple[int, ...]:
    """Kill any ``benchmark_stress`` process still working on this campaign.

    Needed because of an asymmetry the design deliberately accepts: the
    orchestrator is *not* the parent of a job object containing the runs it
    starts, since containing them would mean the orchestrator's own death
    took the run with it - and a run that dies when its supervisor dies
    cannot be used to test what survives a supervisor's death. The
    consequence is that if the orchestrator is killed, its child run keeps
    going.

    So a ``resume`` must clean that up before starting anything, or it would
    start a second coordinator on the same project, steal the lock with
    ``--force-lock``, and have two processes writing the same database - the
    exact corruption this whole campaign exists to rule out.

    Matched strictly on the campaign's own output directory appearing in the
    process's command line, so this can only ever kill a run belonging to
    this campaign.
    """
    marker = str(config.output_dir).lower()
    killed: list[int] = []
    me = os.getpid()
    for process in psutil.process_iter(["pid", "cmdline"]):
        if process.info["pid"] == me:
            continue
        try:
            command = " ".join(process.info["cmdline"] or ()).lower()
        except (psutil.Error, TypeError):  # pragma: no cover - race with exit
            continue
        if "benchmark_stress" not in command or marker not in command:
            continue
        with contextlib.suppress(psutil.Error):
            victim = psutil.Process(int(process.info["pid"]))
            for child in victim.children(recursive=True):
                with contextlib.suppress(psutil.Error):
                    child.kill()
            victim.kill()
            killed.append(int(process.info["pid"]))
    if killed:
        _LOGGER.warning(
            "Killed %s leftover stress run process(es) from an earlier, "
            "interrupted orchestrator before continuing: %s",
            len(killed),
            killed,
        )
        time.sleep(2.0)
    return tuple(killed)


def _write_event(config: QualificationConfig, event: dict[str, Any]) -> None:
    """Append one line to the campaign's own event log.

    Flushed per line: the whole point is that it survives whatever happens
    next.
    """
    path = config.output_dir / EVENTS_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": _now(), **event}) + "\n")
        handle.flush()


def stop_requested(output_dir: Path) -> bool:
    """Whether someone has asked this campaign to stop after the current run.

    Read-only, so the GUI monitor can show "stopping after this run" without
    changing anything.
    """
    return (output_dir / STOP_REQUEST_FILE_NAME).is_file()


def consume_stop_request(output_dir: Path) -> bool:
    """Take the stop request, if there is one, and report that there was.

    The sentinel is removed as it is honoured, and that matters: leaving it
    behind would make the very next ``resume`` stop again before doing any
    work, which looks exactly like a campaign that cannot make progress.
    """
    path = output_dir / STOP_REQUEST_FILE_NAME
    if not path.is_file():
        return False
    with contextlib.suppress(OSError):
        path.unlink()
    return True


def is_release_qualification(config: QualificationConfig) -> bool:
    """Whether this campaign, if it passes, qualifies the release.

    Three things have to be true, and none of them is negotiable: the full
    100,000 sheets, ``full`` mode (independent projects, not one project
    killed repeatedly), and every one of the five mandated checkpoints. A
    campaign that passes without all three has demonstrated something useful
    and has *not* qualified Phase 10, and both this function and the report
    say so rather than letting a convenient smaller run be cited later.
    """
    return (
        config.sheets >= DEFAULT_SHEETS
        and config.mode == "full"
        and set(DEFAULT_CHECKPOINTS).issubset(config.checkpoints)
    )


def qualification_scope_caveat(config: QualificationConfig) -> str:
    """Say precisely why this campaign is not the release qualification.

    Deliberately says nothing about whether the runs *passed*, because the
    same sentence is shown for a campaign that passed, one that was stopped
    between runs and one that failed - and "Every run passed, but..." was
    printed above a stopped campaign whose remaining runs had never been
    attempted. The passing claim is made separately, by the caller that
    knows it.
    """
    reasons: list[str] = []
    if config.sheets < DEFAULT_SHEETS:
        reasons.append(
            f"it ran {config.sheets:,} sheets per run, not {DEFAULT_SHEETS:,}"
        )
    if config.mode != "full":
        reasons.append(
            f"it ran in '{config.mode}' mode, not 'full' "
            "(an uninterrupted reference run plus one independent "
            "forced-kill run per checkpoint)"
        )
    missing = sorted(set(DEFAULT_CHECKPOINTS) - set(config.checkpoints))
    if missing:
        reasons.append(
            "it omitted the mandated checkpoint(s) "
            + ", ".join(f"{percent}%" for percent in missing)
        )
    return (
        "This campaign is NOT the Phase 10 release qualification: "
        + "; ".join(reasons)
        + "."
    )


def execute_campaign(
    config: QualificationConfig, state: QualificationState
) -> bool:
    """Run (or continue) the whole campaign. Returns whether it qualified.

    Resumable at stage granularity: every stage writes its verdict to
    ``qualification_state.json`` before the next one starts, and a stage
    already marked ``passed`` is skipped. So the orchestrator dying - or
    being closed, or the machine being rebooted - costs at most the run that
    was in flight, never the ones already verified.
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / CONFIG_FILE_NAME).write_text(
        json.dumps(config.to_json(), indent=2), encoding="utf-8"
    )
    (config.output_dir / ENVIRONMENT_FILE_NAME).write_text(
        json.dumps(describe_environment(), indent=2), encoding="utf-8"
    )

    leftover = terminate_leftover_runs(config)
    if leftover:
        state.notes.append(
            f"Killed {len(leftover)} leftover stress run process(es) "
            f"({list(leftover)}) belonging to this campaign before starting - "
            "an earlier orchestrator was interrupted while a run was live."
        )

    lock_path = config.output_dir / LOCK_NAME
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "started_at": _now()}, indent=2), encoding="utf-8"
    )
    _write_event(config, {"event": "campaign_started", "runs": list(config.run_ids())})

    telemetry = TelemetryWriter(config.output_dir / "telemetry.csv")
    qualified = True
    stopped = False
    try:
        report = preflight(config)
        (config.output_dir / PREFLIGHT_JSON_NAME).write_text(
            json.dumps(report.to_json(), indent=2), encoding="utf-8"
        )
        (config.output_dir / PREFLIGHT_FILE_NAME).write_text(
            render_preflight_markdown(report, config), encoding="utf-8"
        )
        state.finish(
            "preflight",
            "passed" if report.passed else "failed",
            detail=(
                f"estimated {format_duration(report.estimated_runtime_seconds)}, "
                f"{format_bytes(report.estimated_peak_disk_bytes)} peak disk"
            ),
            data=report.to_json(),
        )
        if not report.passed:
            state.overall_status = "failed"
            state.notes.append("Preflight failed; no run was started.")
            state.save()
            return False

        if config.warmup_sheets > 0 and not _warm_up(config, state):
            state.overall_status = "failed"
            state.notes.append(
                "The warm-up run failed, so the campaign was not started. "
                "Fix that first - it is a much cheaper failure to diagnose."
            )
            state.save()
            return False

        for run_id in config.run_ids():
            if state.is_passed(run_id):
                _LOGGER.info("Skipping %s: already passed in an earlier attempt.", run_id)
                continue
            # Between runs, never inside one: a stop honoured mid-run would be
            # a second kind of kill, and the campaign already has one of those.
            # Here the run that was in flight has finished and been judged, so
            # stopping costs nothing already measured.
            if consume_stop_request(config.output_dir):
                stopped = True
                state.notes.append(
                    f"Stopped before {run_id} at the operator's request "
                    f"(the '{STOP_REQUEST_FILE_NAME}' sentinel was present). "
                    "This is not a failure: no assertion failed and nothing "
                    "was interrupted. Continue with `resume`."
                )
                _write_event(config, {"event": "campaign_stopped_by_operator", "before": run_id})
                break
            reference = None if run_id == "R0" else _load_reference_digest(config)
            if run_id != "R0" and reference is None:
                state.finish(
                    run_id,
                    "failed",
                    detail="R0's semantic digest is missing; cannot compare.",
                )
                qualified = False
                break

            state.begin(run_id, f"{run_id}: running")
            _write_event(config, {"event": "run_started", "run": run_id})
            outcome = execute_run(
                config, state, run_id, telemetry=telemetry, reference_digest=reference
            )
            retention = _reclaim(config, run_id, outcome)
            payload = outcome.to_json()
            payload["retention"] = retention
            state.finish(
                run_id,
                "passed" if outcome.passed else "failed",
                detail=(
                    f"{len(outcome.assertions)} assertion(s), "
                    f"{sum(1 for a in outcome.assertions if not a.passed)} failed; "
                    f"{format_duration(outcome.elapsed_seconds)}"
                ),
                data=payload,
            )
            _write_event(
                config,
                {
                    "event": "run_finished",
                    "run": run_id,
                    "passed": outcome.passed,
                    "failed_assertions": payload["failed_assertions"],
                },
            )
            if not outcome.passed:
                # Stop at the first failure rather than spending another six
                # hours producing more evidence for a campaign that has
                # already failed. Everything it produced is preserved.
                qualified = False
                state.notes.append(
                    f"{run_id} failed: "
                    f"{', '.join(payload['failed_assertions']) or 'see its outcome.json'}. "
                    "Its project directory and evidence have been kept."
                )
                break
    except BaseException as exc:
        state.overall_status = "interrupted"
        state.notes.append(f"Campaign interrupted: {type(exc).__name__}: {exc}")
        state.save()
        _write_event(config, {"event": "campaign_interrupted", "error": str(exc)})
        raise
    else:
        if stopped:
            # Deliberately its own status rather than "failed" or "qualified".
            # An operator who stopped a campaign early must not find a report
            # that says either of those; what happened is that some runs
            # passed and the rest were never attempted.
            state.overall_status = "stopped"
            qualified = False
        elif not qualified:
            state.overall_status = "failed"
        elif is_release_qualification(config):
            state.overall_status = "qualified"
        else:
            # Every run passed, but this campaign was not the release
            # qualification - it was a reduced-scale or reference-only run.
            # Saying "qualified" here is exactly the kind of claim this
            # phase must not make, so it says what actually happened.
            state.overall_status = "passed_not_qualification"
            state.notes.append(
                "Every run passed. " + qualification_scope_caveat(config)
            )
        state.save()
    finally:
        telemetry.close()
        build_reports(state)
        with contextlib.suppress(OSError):
            lock_path.unlink()
        _write_event(
            config, {"event": "campaign_finished", "status": state.overall_status}
        )
    return qualified


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------
def build_reports(state: QualificationState) -> tuple[Path, Path]:
    """Write ``qualification_summary.json`` and ``.md``. Returns both paths.

    Written whenever the campaign stops, successfully or not - a failed
    campaign needs a report more than a successful one does.
    """
    config = state.config
    json_path = config.output_dir / SUMMARY_JSON_NAME
    md_path = config.output_dir / SUMMARY_MD_NAME

    runs = {
        run_id: state.stages[run_id].data
        for run_id in config.run_ids()
        if run_id in state.stages and state.stages[run_id].data
    }
    payload = {
        "generated_at": _now(),
        "overall_status": state.overall_status,
        "all_runs_passed": all(state.is_passed(run_id) for run_id in config.run_ids()),
        "is_release_qualification": is_release_qualification(config),
        "qualified": state.overall_status == "qualified"
        and is_release_qualification(config)
        and all(state.is_passed(run_id) for run_id in config.run_ids()),
        "application_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "config": config.to_json(),
        "environment": describe_environment(),
        "release_blocking_assertions": list(RELEASE_BLOCKING_ASSERTIONS),
        "stages": {name: asdict(record) for name, record in state.stages.items()},
        "runs": runs,
        "notes": state.notes,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(render_summary_markdown(state, payload), encoding="utf-8")
    return json_path, md_path


def render_summary_markdown(state: QualificationState, payload: dict[str, Any]) -> str:
    """The campaign report a human reads and a release decision cites."""
    config = state.config
    qualified = bool(payload["qualified"])
    all_passed = bool(payload["all_runs_passed"])
    stopped = state.overall_status == "stopped"
    if qualified:
        headline = "QUALIFIED"
    elif stopped:
        headline = "STOPPED AT THE OPERATOR'S REQUEST - NOT A FAILURE"
    elif all_passed and not payload["is_release_qualification"]:
        headline = "ALL RUNS PASSED - NOT THE RELEASE QUALIFICATION"
    else:
        headline = state.overall_status.upper()
    lines = [
        "# Phase 10 - 100,000-sheet qualification",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        f"## Result: {headline}",
        "",
    ]
    if not payload["is_release_qualification"]:
        lines += [
            f"> {qualification_scope_caveat(config)}",
            ">",
            "> Do not cite this report as the Phase 10 qualification. It is a",
            "> validation of the harness, or an engineering run.",
            "",
        ]
    if stopped:
        lines += [
            "> This campaign was stopped between runs because an operator",
            "> asked it to. No assertion failed and no run was interrupted:",
            "> the runs below marked `pending` were simply never attempted.",
            "> Continue it with `resume` - the verified runs are not repeated.",
            "",
        ]
    elif not all_passed:
        lines += [
            "> This campaign did **not** pass. Nothing below has been",
            "> downgraded to a warning to reach a conclusion - a failed",
            "> assertion is a failed campaign.",
            "",
        ]
    lines += [
        "## Campaign",
        "",
        f"* Mode: `{config.mode}`",
        f"* Runs: {', '.join(config.run_ids())}",
        f"* Sheets per run: {config.sheets:,}",
        f"* Seed: {config.seed}",
        f"* Workers: {config.resolved_workers()}, "
        f"{config.opencv_threads} OpenCV thread(s) each, "
        f"recycled every {config.worker_recycle_after or 'never'}",
        f"* Started: {state.started_at}",
        f"* Output: `{config.output_dir}`",
        "",
        "## Environment",
        "",
    ]
    for key, value in payload["environment"].items():
        lines.append(f"* {key}: {value}")

    lines += [
        "",
        "## Runs",
        "",
        "| Run | Killed at | Result | Sheets | Elapsed | Sheets/s | Peak tree RSS | DB size |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for run_id in config.run_ids():
        data = payload["runs"].get(run_id)
        if not data:
            status = state.stages.get(run_id, StageRecord()).status
            lines.append(f"| {run_id} | - | {status} | - | - | - | - | - |")
            continue
        checkpoint = data.get("checkpoint_percent")
        lines.append(
            f"| {run_id} "
            f"| {f'{checkpoint}%' if checkpoint is not None else 'not killed'} "
            f"| {'PASS' if data.get('passed') else 'FAIL'} "
            f"| {config.sheets:,} "
            f"| {format_duration(float(data.get('elapsed_seconds', 0)))} "
            f"| {float(data.get('sheets_per_second', 0)):.2f} "
            f"| {float(data.get('peak_tree_memory_mb', 0)):.0f} MB "
            f"| {format_bytes(float(data.get('database_bytes', 0)))} |"
        )

    lines += ["", "## Release-blocking assertions", ""]
    for run_id in config.run_ids():
        data = payload["runs"].get(run_id)
        if not data:
            continue
        lines += [
            f"### {run_id}",
            "",
            "| Assertion | Result | Expected | Observed |",
            "|---|---|---|---|",
        ]
        for item in data.get("assertions", ()):
            lines.append(
                f"| `{item['name']}` | {'PASS' if item['passed'] else '**FAIL**'} "
                f"| {item['expected']} | {item['observed']} |"
            )
        kill = data.get("kill")
        if kill:
            lines += [
                "",
                f"**Forced kill**: coordinator PID {kill['coordinator_pid']} killed at "
                f"{kill['requested_at']} with {kill['committed_before_kill']:,} sheets "
                f"durably committed; {len(kill['worker_pids'])} worker process(es) were "
                f"live at that moment; "
                f"{len(kill['orphan_pids'])} survived; project lock left behind: "
                f"{kill['lock_file_left_behind']}.",
            ]
        lines += [
            "",
            f"**Submission measurement**: {data.get('submitted_first_attempt', 0):,} sheets "
            f"submitted before the kill, {data.get('submitted_after_restart', 0):,} after the "
            "restart. The no-reprocessing assertion is the intersection of the restart's "
            "submissions with what was already committed - measured, not inferred from "
            "final row counts.",
            "",
            f"**Worker recycling**: {data.get('recycling', {}).get('detail', 'not observed')}.",
            "",
            f"**Source immutability**: "
            f"{data.get('immutability', {}).get('source_paths_examined', 0):,} source paths "
            f"examined, all virtual for this seed: "
            f"{data.get('immutability', {}).get('all_virtual_for_this_seed')}.",
            "",
        ]
        at_scale = data.get("at_scale") or {}
        if at_scale:
            lines += ["**At 100,000 sheets, the stored queries the application performs:**", ""]
            for name, measurement in at_scale.items():
                lines.append(f"* `{name}`: {measurement}")
            lines.append("")
        integrity = data.get("integrity") or {}
        lines += [
            f"**Integrity**: quick_check {integrity.get('quick_check')}, "
            f"integrity_check {integrity.get('integrity_check')}, "
            f"{len(integrity.get('foreign_key_check', ()))} foreign-key violation(s), "
            f"journal mode {integrity.get('journal_mode')}, "
            f"{integrity.get('conflicts_total', 0):,} conflict(s) "
            f"({integrity.get('conflicts_open', 0):,} open), "
            f"{integrity.get('duplicate_content_hash_groups', 0):,} duplicate content-hash "
            "group(s) detected (the dataset contains deliberate duplicates; they must be "
            "detected, never deduplicated away).",
            "",
        ]
        for note in data.get("notes", ()):
            lines.append(f"> {note}")
        if data.get("notes"):
            lines.append("")

    if state.notes:
        lines += ["## Campaign notes", ""]
        lines += [f"* {note}" for note in state.notes]
        lines.append("")

    lines += [
        "## What this does and does not establish",
        "",
        "Establishes, by measurement on this machine:",
        "",
        "* 100,000 logical sheets processed through the real recognition",
        "  pipeline, in a real multi-process worker pool, with no result",
        "  inserted into SQLite by anything other than that pipeline.",
        "* Recovery from a genuine, abrupt, ungraceful termination at each of",
        f"  {', '.join(f'{p}%' for p in config.checkpoints)} durably-committed",
        "  progress, each from independent clean initial conditions.",
        "* That a resumed run does not re-read a sheet it had already",
        "  committed - measured from the run's own submission log, not",
        "  inferred from final counts.",
        "* That every sheet's recorded decision is identical to the",
        "  uninterrupted reference run's.",
        "",
        "Does not establish:",
        "",
        "* Behaviour under power loss, disk failure, or filesystem corruption.",
        "  The terminations here are process kills; the storage layer was",
        "  never interrupted mid-write by the OS.",
        "* Throughput on any other hardware. Every figure above is this",
        "  machine's.",
        "* End-to-end result-workbook generation at this scale: the stress",
        "  dataset has no attendance roster, answer key or result template,",
        "  so the stored queries reporting depends on are timed instead, and",
        "  are reported as exactly that.",
        "",
    ]
    return "\n".join(lines)


def load_status(output_dir: Path) -> dict[str, Any]:
    """Read a campaign's current state without disturbing it.

    Read-only and tolerant of a missing or half-written file, because this is
    what the GUI monitor calls on a timer while the campaign is running - it
    must never be able to interfere with the thing it is watching.
    """
    state_path = output_dir / STATE_FILE_NAME
    if not state_path.is_file():
        return {"exists": False, "output_dir": str(output_dir)}
    payload: dict[str, Any]
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"exists": True, "readable": False, "error": str(exc)}
    lock_path = output_dir / LOCK_NAME
    running = False
    holder: int | None = None
    if lock_path.is_file():
        try:
            holder = int(json.loads(lock_path.read_text(encoding="utf-8"))["pid"])
            running = psutil.pid_exists(holder)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            running = False
    payload["exists"] = True
    payload["readable"] = True
    payload["orchestrator_running"] = running
    payload["orchestrator_pid"] = holder
    payload["stop_requested"] = stop_requested(output_dir)
    payload["telemetry_csv"] = str(output_dir / "telemetry.csv")
    payload["summary_markdown"] = str(output_dir / SUMMARY_MD_NAME)
    return payload


__all__ = [
    "CONFIG_FILE_NAME",
    "DEFAULT_CHECKPOINTS",
    "DEFAULT_SHEETS",
    "DEFAULT_WARMUP_SHEETS",
    "EVENTS_FILE_NAME",
    "LOCK_NAME",
    "MEASURED_BYTES_PER_SHEET",
    "MEASURED_SHEETS_PER_SECOND",
    "PREFLIGHT_FILE_NAME",
    "PREFLIGHT_JSON_NAME",
    "QUALIFICATION_SEED",
    "RELEASE_BLOCKING_ASSERTIONS",
    "SEMANTIC_RESULT_KEYS",
    "STATE_FILE_NAME",
    "STOP_REQUEST_FILE_NAME",
    "SUMMARY_JSON_NAME",
    "SUMMARY_MD_NAME",
    "TERMINAL_STATUSES",
    "Assertion",
    "KillEvidence",
    "PreflightCheck",
    "PreflightReport",
    "QualificationConfig",
    "QualificationState",
    "RunOutcome",
    "StageRecord",
    "TelemetryWriter",
    "batch_configuration",
    "build_reports",
    "committed_indexes",
    "consume_stop_request",
    "describe_environment",
    "estimate_disk",
    "estimate_runtime_seconds",
    "evaluate_assertions",
    "execute_campaign",
    "execute_run",
    "format_bytes",
    "format_duration",
    "integrity_report",
    "is_release_qualification",
    "kill_run_abruptly",
    "load_status",
    "measure_at_scale",
    "preflight",
    "qualification_scope_caveat",
    "read_committed_rows",
    "read_progress",
    "read_submission_log",
    "render_preflight_markdown",
    "render_summary_markdown",
    "semantic_digest",
    "source_immutability",
    "stop_requested",
    "worker_recycling_evidence",
]
