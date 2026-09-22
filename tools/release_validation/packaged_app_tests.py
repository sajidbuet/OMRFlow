"""Stage: launch the real packaged executable and drive it.

Why this matters more than the source GUI stage:
    Everything under ``tests/`` and ``suites/test_gui_functional.py`` imports
    ``omr_scanner`` from the source tree. That proves the code works; it proves
    nothing about the thing a user downloads. A missing Qt plugin, a resource
    that was never collected, an import that only resolved because the source
    tree was on ``sys.path`` - all of those pass every source test and fail
    here.

Automation:
    ``pywinauto`` when it is installed, for UI Automation checks against the
    real window tree. When it is not, the stage still launches the executable
    and inspects its window through Win32 - title, responsiveness, clean exit -
    and records the UIA-only checks as SKIPPED with the reason. Degrading is
    deliberate: a missing optional dependency must narrow the stage, never
    silently pass it.

Timeouts:
    Every wait is bounded by :class:`~tools.release_validation.config.Timeouts`.
    A hang is reported as a hang and the process is stopped.
"""

from __future__ import annotations

import time
from pathlib import Path

from tools.release_validation import config as cfg
from tools.release_validation.process_utils import (
    LaunchedProcess,
    ProcessRegistry,
    wait_until,
    window_is_responding,
    window_title_for_pid,
)
from tools.release_validation.results import StageResult, Status


def _pywinauto() -> type | None:
    """The pywinauto Application class, or ``None`` when it is not installed."""
    try:
        from pywinauto.application import Application
    except ImportError:
        return None
    return Application


def _wait_for_window(process: LaunchedProcess, timeout_seconds: int) -> str:
    """Wait for a titled window, returning the title or ``""``."""
    title = ""

    def appeared() -> bool:
        nonlocal title
        if not process.running:
            return True
        title = window_title_for_pid(process.pid)
        return bool(title)

    wait_until(appeared, timeout_seconds, interval_seconds=0.5)
    return title


def _inspect_with_uia(stage: StageResult, process: LaunchedProcess) -> None:
    """The checks that need UI Automation, when pywinauto is available."""
    application_type = _pywinauto()
    if application_type is None:
        stage.skip(
            "UI Automation inspection",
            "pywinauto is not installed (pip install pywinauto); the window was "
            "still launched and checked through Win32",
        )
        return

    try:
        app = application_type(backend="uia").connect(process=process.pid, timeout=30)
        window = app.top_window()
        window.wait("visible ready", timeout=60)
    except Exception as error:
        stage.fail(
            "UI Automation can attach to the window",
            f"could not attach to pid {process.pid}: {error}",
            exception=error,
        )
        return

    stage.ok("UI Automation can attach to the window", True, detail=f"pid {process.pid}")

    try:
        texts = window.descendants()
        stage.ok(
            "the window exposes an accessibility tree",
            len(texts) > 0,
            detail=f"{len(texts)} automation elements",
            reason="the window exposes no automation elements, so no screen "
            "reader could read it",
        )

        # The nine stage names should be somewhere in the tree. Matched
        # case-insensitively on the visible titles rather than on positions.
        from omr_scanner.gui.pages import WORKFLOW_PAGES

        names = " | ".join(
            (element.window_text() or "") for element in texts
        ).lower()
        missing = [
            spec.title for spec in WORKFLOW_PAGES if spec.title.lower() not in names
        ]
        stage.ok(
            "every workflow stage is present in the packaged window",
            not missing,
            detail=f"{len(WORKFLOW_PAGES) - len(missing)} of {len(WORKFLOW_PAGES)} found",
            reason=f"stages not found in the packaged window: {missing}",
        )
    except Exception as error:
        stage.fail("the window tree can be read", str(error), exception=error)


def run(config: cfg.ValidationConfig, registry: ProcessRegistry) -> StageResult:
    """Launch ``dist/OMRFlow/OMRFlow.exe`` and check it behaves."""
    return _run_executable(
        config,
        registry,
        executable=cfg.BUNDLE_EXE,
        stage_name="Packaged application launch",
        missing_hint=(
            f"no packaged executable at {cfg.BUNDLE_EXE}. Run "
            ".\\scripts\\release\\Build-App.ps1 -Clean, or pass --build."
        ),
    )


def run_installed(config: cfg.ValidationConfig, registry: ProcessRegistry) -> StageResult:
    """The same checks against the *installed* application."""
    return _run_executable(
        config,
        registry,
        executable=cfg.INSTALLED_EXE,
        stage_name="Installed application launch",
        missing_hint=(
            f"OMRFlow is not installed at {cfg.INSTALLED_APP_DIR}. Run the "
            "installer stage first, or install it by hand."
        ),
    )


def _run_executable(
    config: cfg.ValidationConfig,
    registry: ProcessRegistry,
    *,
    executable: Path,
    stage_name: str,
    missing_hint: str,
) -> StageResult:
    started = time.monotonic()
    stage = StageResult(name=stage_name)

    if not executable.is_file():
        stage.skipped_reason = missing_hint
        return stage

    from omr_scanner import _version

    # --version first: it exercises the frozen entry point and its argument
    # parsing without opening a window, and a failure here localises the
    # problem before the GUI muddies it.
    from tools.release_validation.process_utils import run_command

    code, output = run_command(
        [executable, "--version"],
        timeout_seconds=config.timeouts.app_launch_seconds,
        environment=config.child_environment(),
    )
    stage.ok(
        "the executable reports its version",
        code == 0 and _version.__version__ in output,
        detail=output.strip()[:200] or f"exit {code}",
        reason=f"`--version` exited {code} and printed {output.strip()[:200]!r}",
    )

    # ---------------------------------------------------------- the window
    process = registry.launch(
        [executable],
        label=stage_name.replace(" ", "-").lower(),
        log_dir=config.logs_dir,
        environment=config.child_environment(),
        cwd=config.workspace,
    )

    title = _wait_for_window(process, config.timeouts.window_seconds)
    log_refs = [
        f"logs/{process.stdout_path.name}",
        f"logs/{process.stderr_path.name}",
    ]

    if not process.running:
        stage.record(
            "the application stays running after start-up",
            Status.FAIL,
            detail=f"exited with {process.exit_code}",
            reason=(
                f"the process exited immediately with {process.exit_code}. "
                f"Output: {process.read_output(1500) or '(none)'}"
            ),
            artifacts=log_refs,
        )
        stage.duration_seconds = time.monotonic() - started
        return stage

    stage.ok("the application stays running after start-up", True, detail="running")
    stage.ok(
        "a main window appeared",
        bool(title),
        detail=title,
        reason=(
            f"no window within {config.timeouts.window_seconds}s - the "
            "application started but never showed anything"
        ),
        artifacts=log_refs,
    )

    if title:
        stage.ok(
            "the window title names the application and its version",
            title.startswith("OMRFlow") and _version.__version__ in title,
            detail=title,
            reason=f"expected a title like 'OMRFlow {_version.__version__}', got {title!r}",
        )

    responding = wait_until(lambda: window_is_responding(process.pid), 30)
    stage.ok(
        "the window is responding",
        responding,
        detail="answers messages" if responding else "not responding",
        reason="the window exists but its message loop is not running - the "
        "application has hung",
    )

    _inspect_with_uia(stage, process)

    # --------------------------------------------------------- clean close
    outcome = registry.stop(process, timeout_seconds=config.timeouts.shutdown_seconds)
    stage.ok(
        "the application closes cleanly when asked",
        outcome in {"closed its window", "already exited"},
        detail=outcome,
        reason=f"the application had to be {outcome} rather than closing on request",
    )
    if process.exit_code is not None:
        stage.ok(
            "the exit code is zero",
            process.exit_code == 0,
            detail=str(process.exit_code),
            reason=f"exited with {process.exit_code}",
        )

    # ------------------------------------------- did it write where it must not?
    installation = executable.parent
    intruders = [
        str(path.relative_to(installation))
        for pattern in ("*.log", "*.db", "*.sqlite", "*.ini", "*.json")
        for path in installation.glob(pattern)
    ]
    stage.ok(
        "nothing was written into the installation directory",
        not intruders,
        detail="; ".join(intruders[:5]) if intruders else "unchanged",
        reason=f"the application wrote into its own directory: {intruders[:5]}",
    )

    logs_written = list(config.app_log_dir.rglob("*.log"))
    stage.ok(
        "a log was written under the redirected user directory",
        bool(logs_written),
        detail=str(config.app_log_dir),
        reason="the application wrote no log, so a bug report from it would "
        "carry no diagnostics",
    )

    stage.duration_seconds = time.monotonic() - started
    return stage
