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

Two contracts, and only one of them is cross-platform. The kill/resume and
durability properties above hold everywhere and are asserted everywhere. That
an abrupt kill of the coordinator *also* kills its workers is a Windows-only
guarantee, provided by
:mod:`omr_scanner.services.process_containment`'s job object and deliberately
not implemented on POSIX (see that module's docstring); it is therefore
asserted only on Windows. Either way the test reaps any surviving worker
before it finishes, so no run leaves processes behind.

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


WORKER_TEARDOWN_GRACE_SECONDS = 1.0
"""How long the OS is given to finish tearing down the workers after the
coordinator dies, before survivors are counted."""


def _survivors(pids: list[int]) -> list[int]:
    """Which of ``pids`` are still running."""
    return [pid for pid in pids if psutil.pid_exists(pid)]


def _reap(pids: list[int]) -> None:
    """Kill any of ``pids`` still running, and wait for them to actually go.

    Without automatic containment (every platform except Windows - see
    :mod:`omr_scanner.services.process_containment`), killing the coordinator
    leaves its workers running. They are this *test's* own child processes, so
    the test is what has to clean them up: left behind they would keep a CPU
    core busy on the CI runner for the rest of the job, and - more to the point
    here - they would still be writing to the project database while the resume
    run below opens it.
    """
    doomed = []
    for pid in pids:
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            process = psutil.Process(pid)
            process.kill()
            doomed.append(process)
    if doomed:
        psutil.wait_procs(doomed, timeout=15)


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

    # Give the OS a moment to finish tearing the workers down before counting
    # survivors, then check the *platform's* containment contract. These are
    # two different contracts and only one of them is cross-platform:
    #
    #   Windows - `services.process_containment` puts the coordinator in a job
    #     object with KILL_ON_JOB_CLOSE, so its abrupt death must take every
    #     worker with it. That is the Phase 10 audit finding the module exists
    #     to close, so it is asserted here rather than merely observed.
    #
    #   POSIX - OMRFlow implements no equivalent, deliberately: a `SIGKILL` of
    #     the parent alone does not reach its children, and process groups do
    #     not change that (they need the *killer* to signal the group). The
    #     module's own docstring records this as a design decision, so
    #     asserting the Windows guarantee here would be asserting a promise the
    #     product does not make. Nothing else in this test is weakened by that:
    #     the durability, no-loss, no-duplication and resume properties below
    #     are checked identically on both platforms, which is what the
    #     single-writer architecture makes safe even when a worker outlives its
    #     coordinator.
    time.sleep(WORKER_TEARDOWN_GRACE_SECONDS)
    assert worker_pids, "Never captured any worker child PIDs to check - test setup issue"
    try:
        if sys.platform == "win32":
            still_alive = _survivors(worker_pids)
            assert not still_alive, (
                f"Worker process(es) {still_alive} survived the coordinator's forced "
                "kill - Windows job-object containment did not hold, and orphaned "
                "workers were left running"
            )
    finally:
        # Unconditional, and after the assertion rather than instead of it: on
        # Windows there is normally nothing left to reap, but if containment
        # ever regresses this stops a failing test from also leaking processes.
        _reap(worker_pids)

    # Cross-platform, and the reason the POSIX branch above is not simply
    # silent: however the workers died, none may still be running once this
    # test has dealt with them. A leak here would pollute the machine and would
    # mean the resume below is racing live writers.
    assert not _survivors(worker_pids), (
        f"Worker process(es) {_survivors(worker_pids)} are still running after "
        "cleanup - the test has leaked processes onto this machine"
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
