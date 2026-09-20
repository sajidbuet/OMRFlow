"""Real, forced-process-kill resume tests (Phase 10, §14, §30, §31, §44).

Unlike every other test in this suite, this one does not simulate the shape
a crash leaves behind - it starts the real CLI
(:mod:`omr_scanner.tools.benchmark_stress`) as a genuine child process and
terminates it abruptly (``Process.kill()`` - ``TerminateProcess`` on
Windows, ``SIGKILL`` on POSIX; no graceful shutdown, no chance to release
the project lock or flush anything not already committed), then verifies
the mandatory acceptance properties: no completed sheet is lost, none is
duplicated, none is silently reprocessed, and a second run reaches the
correct final total.

Runs at 100 and 1,000 sheets - real, deliberate, forced terminations, not a
simulation - as the practical, fast-running validation of the *same*
architecture the mandatory 100,000-sheet kill/resume acceptance matrix
(§30/§31) exercises at full scale. The full-scale matrix is a separate,
deliberately unautomated run - see ``development/PHASE_10_HANDOFF.md`` and
``tools/benchmark_stress.py``'s own module docstring for the exact commands
to run it.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from omr_scanner.database.engine import open_project_database
from omr_scanner.database.models import BatchScan
from omr_scanner.services.project_lock import LOCK_FILE_NAME

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "examples" / "templates" / "100_question_4_choice_example.omrt"


def _read_committed(db_path: Path) -> dict[int, tuple[str, str]]:
    """Return {scan_id: (status, result_json)} for every durably recorded scan.

    Opened read-only and independently of the running child process - real
    concurrent access to the actual SQLite file, not a mock.
    """
    for _attempt in range(20):
        try:
            database = open_project_database(db_path, read_only=True)
        except Exception:
            time.sleep(0.1)
            continue
        try:
            from sqlalchemy import select

            with database.session() as session:
                rows = session.execute(
                    select(BatchScan.scan_id, BatchScan.status, BatchScan.result_json)
                ).all()
            return {int(scan_id): (str(status), str(payload)) for scan_id, status, payload in rows}
        except Exception:
            time.sleep(0.1)
            continue
        finally:
            database.close()
    return {}


def _terminal_count(committed: dict[int, tuple[str, str]]) -> int:
    return sum(1 for status, _ in committed.values() if status != "pending")


def _run_cli(project: Path, *, sheets: int, seed: int, create: bool) -> subprocess.Popen:
    args = [
        sys.executable,
        "-m",
        "omr_scanner.tools.benchmark_stress",
        str(project),
        "--template",
        str(TEMPLATE_PATH),
        "--sheets",
        str(sheets),
        "--seed",
        str(seed),
        "--workers",
        "2",
        "--quiet",
    ]
    if create:
        args.insert(4, "--create")
    return subprocess.Popen(args, cwd=REPO_ROOT)


@pytest.mark.parametrize(
    "sheet_count",
    [
        100,
        1000,
        pytest.param(10_000, marks=pytest.mark.stress),
    ],
)
def test_a_forced_kill_mid_run_resumes_without_loss_or_duplication(
    tmp_path: Path, sheet_count: int
) -> None:
    project = tmp_path / f"stress_kill_{sheet_count}"
    db_path = project / "database.sqlite"
    target_committed = max(sheet_count // 4, 5)
    # Scaled generously to the sheet count: real subprocess start-up and
    # worker-pool spawn overhead dominate at small scale, real recognition
    # time dominates at large scale.
    wait_for_commits_seconds = max(90, sheet_count * 0.05)
    resume_timeout_seconds = max(180, sheet_count * 0.1)

    process = _run_cli(project, sheets=sheet_count, seed=12345, create=True)
    worker_pids: list[int] = []
    try:
        deadline = time.monotonic() + wait_for_commits_seconds
        committed_before_kill: dict[int, tuple[str, str]] = {}
        while time.monotonic() < deadline:
            time.sleep(0.2)
            committed_before_kill = _read_committed(db_path)
            # Capture the coordinator's worker children while it is still
            # alive and actually running a pool, for the orphan check below.
            with contextlib.suppress(psutil.NoSuchProcess):
                children = psutil.Process(process.pid).children(recursive=True)
                worker_pids = [child.pid for child in children]
            if worker_pids and _terminal_count(committed_before_kill) >= target_committed:
                break
        assert _terminal_count(committed_before_kill) >= 1, (
            "The process never durably committed a single sheet before the "
            "deadline - nothing to test a kill against."
        )
    finally:
        # Abrupt, ungraceful termination - not process.terminate() followed
        # by a wait for clean shutdown, and never process.communicate()
        # first, which would let it finish on its own.
        process.kill()
        process.wait(timeout=15)

    # The lock file must not have been released - a real kill gives the
    # process no chance to run its cleanup, which is exactly the scenario
    # `project_lock`'s stale-lock handling exists for.
    assert (project / LOCK_FILE_NAME).is_file()

    # No orphaned worker process must survive the coordinator's abrupt
    # death (Phase 10 audit finding: `services.process_containment` exists
    # specifically to make this true on Windows). Give the OS a moment to
    # finish tearing down the job object before checking.
    time.sleep(1.0)
    assert worker_pids, "Never captured any worker child PIDs to check - test setup issue"
    still_alive = [pid for pid in worker_pids if psutil.pid_exists(pid)]
    assert not still_alive, (
        f"Worker process(es) {still_alive} survived the coordinator's forced kill - "
        "orphaned workers were left running"
    )

    # Resume: the same command, without --create, plus --force-lock - the
    # explicit "I know this is a genuine crash, not a still-running process"
    # decision (Phase 10, §3) a real operator would make after seeing the
    # same "likely stale" lock message this test just forced into existence.
    resumed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omr_scanner.tools.benchmark_stress",
            str(project),
            "--template",
            str(TEMPLATE_PATH),
            "--sheets",
            str(sheet_count),
            "--seed",
            "12345",
            "--workers",
            "2",
            "--quiet",
            "--force-lock",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=resume_timeout_seconds,
    )
    assert resumed.returncode == 0, (
        f"Resume did not finish cleanly: stdout={resumed.stdout!r} stderr={resumed.stderr!r}"
    )

    final = _read_committed(db_path)
    assert len(final) == sheet_count, "Final row count must equal the batch's sheet count exactly"
    assert _terminal_count(final) == sheet_count, "Every sheet must have reached a terminal state"

    # No previously-completed sheet's recorded result changed - resuming
    # must never re-decide a sheet the first run had already committed.
    for scan_id, (status_before, result_before) in committed_before_kill.items():
        if status_before == "pending":
            continue
        status_after, result_after = final[scan_id]
        assert status_after == status_before, f"scan {scan_id} status changed after resume"
        assert result_after == result_before, f"scan {scan_id} result changed after resume"
