"""Run a pytest selection in a subprocess and collect its results.

Why a subprocess:
    Qt, OpenCV and the multiprocessing worker pool all live in C. A fault in
    any of them takes down the interpreter it is in. Running pytest in-process
    would mean one segfaulting GUI test destroys a forty-minute qualification
    run and its report; in a subprocess it is a failed stage with a log.

Why JUnit XML rather than the exit code:
    An exit code says "something failed". The report has to say *what*, and a
    reader has to be able to find it again. See ``junit.py``.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
from pathlib import Path

from tools.release_validation import config as cfg
from tools.release_validation.junit import parse_junit
from tools.release_validation.process_utils import run_command
from tools.release_validation.results import StageResult, Status


def run_pytest(
    config: cfg.ValidationConfig,
    *,
    stage_name: str,
    selection: Sequence[str],
    junit_name: str,
    extra_args: Sequence[str] = (),
    environment: dict[str, str] | None = None,
    timeout_seconds: float | None = None,
    blocking: bool = True,
) -> StageResult:
    """Run ``selection`` under pytest and turn the result into a stage.

    Args:
        config: The run this stage belongs to.
        stage_name: What the stage is called in the report.
        selection: Paths or node ids, relative to the repository root.
        junit_name: File name for the JUnit XML, written into the run's
            artifacts directory so it is kept with the rest of the evidence.
        extra_args: Further pytest arguments - markers, ``-k`` expressions.
        environment: The child environment. Defaults to one with the
            application's configuration and log directories redirected into the
            workspace, so a GUI test cannot touch the operator's real settings.
        timeout_seconds: Stuck-process guard; the configured default otherwise.
        blocking: Whether a failure here should stop a release.

    Returns:
        A stage holding one check per test, plus a check for the run itself so
        that "pytest crashed before collecting" is visible rather than silent.
    """
    started = time.monotonic()
    stage = StageResult(name=stage_name, blocking=blocking)

    junit_path = config.artifacts_dir / junit_name
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = config.logs_dir / f"{junit_path.stem}.log"

    command = [
        sys.executable,
        "-m",
        "pytest",
        *selection,
        f"--junit-xml={junit_path}",
        "-q",
        # The framework decides what is blocking; pytest should report
        # everything it finds rather than stopping at the first failure.
        "--maxfail=0",
        *extra_args,
    ]

    exit_code, output = run_command(
        command,
        timeout_seconds=timeout_seconds or config.timeouts.pytest_seconds,
        cwd=cfg.REPOSITORY_ROOT,
        environment=environment if environment is not None else config.child_environment(),
    )
    log_path.write_text(output, encoding="utf-8", errors="replace")

    stage.checks.extend(
        parse_junit(junit_path, artifact=f"artifacts/{junit_path.name}")
    )

    # pytest's exit codes: 0 all passed, 1 tests failed, 2 interrupted,
    # 3 internal error, 4 usage error, 5 no tests collected. Only 0 and 1 are
    # accounted for by the per-test rows above; the rest mean the suite did not
    # get to say anything, and must not be mistaken for "nothing failed".
    if exit_code not in (0, 1):
        stage.record(
            "pytest ran to completion",
            Status.FAIL,
            reason=f"pytest exited with {exit_code} - see logs/{log_path.name}",
            detail=output.strip()[-1500:],
            artifacts=[f"logs/{log_path.name}"],
        )
    else:
        stage.record(
            "pytest ran to completion",
            Status.PASS,
            detail=f"exit {exit_code}",
            artifacts=[f"logs/{log_path.name}", f"artifacts/{junit_path.name}"],
        )

    stage.duration_seconds = time.monotonic() - started
    return stage


def suite_path(*parts: str) -> str:
    """A path inside this framework's own pytest suites, for ``selection``."""
    return str(Path(cfg.SUITES_DIR, *parts).relative_to(cfg.REPOSITORY_ROOT))
