"""Leave the machine as the run found it - even when the run failed.

Called from the orchestrator's ``finally``, so a crashed stage cannot leave an
OMRFlow running, a temporary project locking a database, or gigabytes of
rendered sheets on the disk.

What it will not do:
    Kill processes it did not start. The registry knows every process this run
    launched and only those are stopped. A sweep by name exists for the case
    where a previous run crashed hard, and it is opt-in
    (``--kill-stray-processes``) precisely because "terminate everything called
    OMRFlow" would close the operator's own window mid-examination.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from tools.release_validation import config as cfg
from tools.release_validation.process_utils import ProcessRegistry
from tools.release_validation.results import StageResult, Status


def _stray_omrflow_processes(exclude: set[int]) -> list[tuple[int, str]]:
    """OMRFlow processes not started by this run, identified by image path.

    Matched on the executable's path, not merely its name, so an unrelated
    program that happens to be called ``OMRFlow.exe`` somewhere else is not
    swept up.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a runtime dependency
        return []

    found: list[tuple[int, str]] = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = process.info
            if info["pid"] in exclude or os.getpid() == info["pid"]:
                continue
            if (info["name"] or "").lower() != "omrflow.exe":
                continue
            found.append((info["pid"], info["exe"] or info["name"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):  # pragma: no cover
            continue
    return found


def run(
    config: cfg.ValidationConfig,
    registry: ProcessRegistry,
    *,
    kill_stray: bool = False,
) -> StageResult:
    """Stop what this run started and remove what it wrote."""
    started = time.monotonic()
    stage = StageResult(name="Cleanup", blocking=False)

    # ------------------------------------------------------------ processes
    notes = registry.stop_all(timeout_seconds=config.timeouts.shutdown_seconds)
    stage.record(
        "processes started by this run were stopped",
        Status.PASS,
        detail="; ".join(notes) if notes else "none were still running",
    )

    ours = {process.pid for process in registry.processes}
    strays = _stray_omrflow_processes(ours)
    if strays and kill_stray:
        import psutil

        for pid, path in strays:
            try:
                psutil.Process(pid).terminate()
                stage.record(f"stray process {pid} terminated", Status.WARNING, detail=path)
            except (psutil.NoSuchProcess, psutil.AccessDenied) as error:  # pragma: no cover
                stage.record(
                    f"stray process {pid}", Status.WARNING, reason=f"could not stop it: {error}"
                )
    elif strays:
        stage.record(
            "other OMRFlow processes are running",
            Status.WARNING,
            detail="; ".join(f"pid {pid} ({path})" for pid, path in strays),
            reason=(
                "left alone - they were not started by this run. Pass "
                "--kill-stray-processes if they are leftovers from a crashed one."
            ),
        )

    # ----------------------------------------------------------- workspace
    if config.keep_artifacts:
        stage.record(
            "the workspace was kept",
            Status.PASS,
            detail=str(config.workspace),
        )
    elif config.workspace.exists():
        size = _directory_size(config.workspace)
        try:
            shutil.rmtree(config.workspace, ignore_errors=False)
            stage.record(
                "the temporary workspace was removed",
                Status.PASS,
                detail=f"{size / 1_048_576:.1f} MB freed from {config.workspace}",
            )
        except OSError as error:
            # Usually a database handle Windows has not released yet. Worth a
            # warning, never worth failing a release over.
            stage.record(
                "the temporary workspace was removed",
                Status.WARNING,
                detail=str(config.workspace),
                reason=f"could not remove it: {error}. Delete it by hand.",
            )

    stage.duration_seconds = time.monotonic() - started
    return stage


def _directory_size(directory: Path) -> int:
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:  # pragma: no cover - vanished mid-walk
            continue
    return total
