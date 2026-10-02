"""Real process kills of the continuous engine (0.1.1 revised phase 6).

The parent launches :mod:`tests.crash.engine_child` - the real engine on the
real filesystem, real ``spawn`` workers (or in-process reading where a
boundary needs one sheet at a time) - waits for a **state** in its external
evidence log, kills the coordinator with ``TerminateProcess`` (Phase 10's
``kill_run_abruptly``: no shutdown handler runs; its workers must die with
it), then restarts a fresh child on the same project and lets it finish.

Every case asserts, from outside the killed process:

* the final durable outcome equals an **uninterrupted control child's** over
  the same files (results, effective set, conflicts, unread duplicates);
* no sheet committed before the kill was submitted to recognition after it
  (the submission log against the database);
* same scan session, every batch that existed at the kill kept with its
  membership, no supersession, no recovery batch;
* ``quick_check`` / ``integrity_check`` / ``foreign_key_check`` and Project
  Health clean; no orphaned worker process.

The in-process versions of every boundary (and the 1/25/50/75/99 % series)
are ``tests/integration/test_engine_recovery.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from tests.crash.harness import (
    REPOSITORY_ROOT,
    Child,
    _database,
    create_project_with_template,
    integrity,
    kill,
    run_to_exit,
    wait_for,
)
from tests.engine_rig import committed_contents, durable_view, readable_sheets, structure

from omr_scanner.database.models import BatchScan, BatchSupersession, ScanJobStatus
from omr_scanner.domain.intake import StabilityPolicy
from omr_scanner.services import intake, open_project, scan_sessions

CHILD = Path(__file__).resolve().parent / "engine_child.py"
SHEETS = 30
POLICY = StabilityPolicy(
    min_observations=2, quiet_seconds=0.3, max_decode_attempts=3, retry_backoff_seconds=0.5,
    poll_interval_seconds=0.0,
)


def launch(project: Path, log: Path, session_id: str, *, workers: int = 2, pause: str = "",
           pause_count: int = 1, stop_after: int = 0, unit: int = 8,
           force_lock: bool = False) -> Child:
    args = [
        sys.executable, str(CHILD), "--project", str(project), "--log", str(log),
        "--session", session_id, "--workers", str(workers), "--unit", str(unit),
        "--expect", str(SHEETS),
    ]
    if pause:
        args += ["--pause", pause, "--pause-count", str(pause_count)]
    if stop_after:
        args += ["--stop-after", str(stop_after)]
    if force_lock:
        args.append("--force-lock")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT / "src"), str(REPOSITORY_ROOT), environment.get("PYTHONPATH", "")]
    )
    errors = log.with_name(f"{log.stem}-{len(list(log.parent.glob(log.stem + '-*.err')))}.err")
    with errors.open("wb") as stderr:
        process = subprocess.Popen(
            args, cwd=REPOSITORY_ROOT, env=environment,
            stdout=subprocess.DEVNULL, stderr=stderr,
        )
    return Child(process=process, log=log, project=project)


def prepare(workspace: Path, name: str, sources: Path) -> tuple[Path, str]:
    """A project with the synthetic template, one open session, two watched sources."""
    root = create_project_with_template(workspace, name)
    project = open_project(root)
    try:
        session_id = scan_sessions.create_scan_session(
            project.database, name="Exam", created_by="op"
        ).scan_session_id
        for folder in sorted(sources.iterdir()):
            info = intake.create_source(
                project.database, label=folder.name, root_path=str(folder), created_by="op",
                policy=POLICY,
            )
            intake.attach_source(project.database, info.source_id, session_id, actor="op")
    finally:
        project.close()
    return root, session_id


@pytest.fixture(scope="module")
def sources(tmp_path_factory) -> Path:
    """Two scanner folders with the same 30 real synthetic sheets, written once."""
    root = tmp_path_factory.mktemp("engine-kill-sources")
    for index, data in enumerate(readable_sheets(36)[:SHEETS]):
        folder = root / ("scanner-a" if index % 2 == 0 else "scanner-b")
        folder.mkdir(exist_ok=True)
        (folder / f"{index:06d}.png").write_bytes(data)
    return root


def outcome(project: Path, session_id: str) -> dict[str, Any]:
    database = _database(project)
    try:
        return durable_view(database, session_id)
    finally:
        database.close()


@pytest.fixture(scope="module")
def expected(sources, tmp_path_factory) -> dict[str, Any]:
    """The uninterrupted control child's outcome."""
    workspace = tmp_path_factory.mktemp("engine-kill-control")
    project, session_id = prepare(workspace, "Control", sources)
    events = run_to_exit(launch(project, workspace / "control.jsonl", session_id), timeout=300)
    assert any(item["event"] == "caught_up" for item in events)
    return outcome(project, session_id)


def interpreter_pid(events: list[dict[str, Any]]) -> int:
    """The killed child's interpreter pid, as its own evidence log recorded it.

    Not ``Popen.pid``: a virtual environment's ``python.exe`` on Windows is a
    launcher that starts the real interpreter as its own child process.
    """
    return int(events[-1]["pid"])


def submitted_after(events: list[dict[str, Any]], killed: int | set[int]) -> list[str]:
    """Sheets submitted by children that were *not* killed (the resumed runs)."""
    excluded = killed if isinstance(killed, set) else {killed}
    return [
        item["sheet"] for item in events
        if item["event"] == "submitted" and item["pid"] not in excluded
    ]


def facts(project: Path, session_id: str) -> dict[str, Any]:
    database = _database(project)
    try:
        with database.session() as session:
            processing = session.scalar(
                select(func.count()).select_from(BatchScan).where(
                    BatchScan.status == ScanJobStatus.PROCESSING.value
                )
            )
            supersessions = session.scalar(select(func.count()).select_from(BatchSupersession))
        return {
            "committed": committed_contents(database),
            "structure": structure(database, session_id),
            "processing": int(processing or 0),
            "supersessions": int(supersessions or 0),
        }
    finally:
        database.close()


def assert_resumed(project: Path, session_id: str, expected: dict, at_kill: dict,
                   events: list[dict[str, Any]], killed_pid: int | set[int]) -> None:
    assert outcome(project, session_id) == expected
    after = facts(project, session_id)
    reread = set(submitted_after(events, killed_pid)) & at_kill["committed"]
    assert not reread, f"committed before the kill, submitted again: {sorted(reread)[:3]}"
    assert after["processing"] == 0
    assert after["supersessions"] == 0
    assert after["structure"]["sessions"] == [session_id]
    old = {row[0]: row for row in at_kill["structure"]["batches"]}
    new = {row[0]: row for row in after["structure"]["batches"]}
    assert all(new[batch] == row for batch, row in old.items()), "a unit changed at restart"
    assert set(at_kill["structure"]["members"]) <= set(after["structure"]["members"])
    checks = integrity(project)
    assert checks.sqlite_ok, checks
    assert checks.health_errors == [], checks.health_errors


def kill_and_resume(workspace: Path, sources: Path, expected: dict, name: str, *,
                    predicate: Any, what: str, **child: Any) -> tuple[dict, list]:
    project, session_id = prepare(workspace, name, sources)
    log = workspace / f"{name}.jsonl"
    first = launch(project, log, session_id, **child)
    seen = wait_for(first, predicate, timeout=300, what=what)
    killed = kill(first)
    assert killed.orphans == (), "worker processes outlived the coordinator"
    at_kill = facts(project, session_id)
    events = run_to_exit(launch(project, log, session_id, force_lock=True), timeout=300)
    assert_resumed(project, session_id, expected, at_kill, events, interpreter_pid(seen))
    return at_kill, events


def paused(events: list[dict[str, Any]]) -> bool:
    return any(item["event"] == "paused" for item in events)


def committed_at_least(count: int):  # type: ignore[no-untyped-def]
    def check(events: list[dict[str, Any]]) -> bool:
        return sum(len(item["sheets"]) for item in events if item["event"] == "committed") >= count
    return check


CASES = [
    ("claimed", {"pause": "claimed", "pause_count": 3}),
    ("recognised", {"pause": "recognised", "pause_count": 5, "workers": 1}),
    ("in_commit", {"pause": "in_commit", "pause_count": 6, "workers": 1}),
    ("committed", {"pause": "committed", "pause_count": 7, "workers": 1}),
    ("finalising", {"pause": "finalising", "pause_count": 2}),
]


@pytest.mark.parametrize(("name", "child"), CASES, ids=[case[0] for case in CASES])
def test_kill_at_a_boundary(tmp_path, sources, expected, name, child):
    at_kill, _events = kill_and_resume(
        tmp_path, sources, expected, name, predicate=paused, what=f"pause at {name}", **child
    )
    if name in ("recognised", "in_commit", "claimed"):
        assert at_kill["processing"] >= 1, "the kill must leave a claim for recovery"


def test_kill_while_sheets_are_inside_the_workers(tmp_path, sources, expected):
    at_kill, _events = kill_and_resume(
        tmp_path, sources, expected, "free", predicate=committed_at_least(10),
        what="10 committed sheets", workers=2,
    )
    assert len(at_kill["committed"]) >= 10


def test_clean_stop_leaves_nothing_claimed_and_resumes(tmp_path, sources, expected):
    project, session_id = prepare(tmp_path, "clean", sources)
    log = tmp_path / "clean.jsonl"
    events = run_to_exit(launch(project, log, session_id, stop_after=8), timeout=300)
    assert any(item["event"] == "stopped" for item in events)
    at_stop = facts(project, session_id)
    assert at_stop["processing"] == 0
    assert len(at_stop["committed"]) >= 8
    first_pid = interpreter_pid(events)
    events = run_to_exit(launch(project, log, session_id), timeout=300)
    assert_resumed(project, session_id, expected, at_stop, events, first_pid)


def test_repeated_kills_converge(tmp_path, sources, expected):
    """Kill, resume, kill, resume, kill, finish - while units are still being registered."""
    project, session_id = prepare(tmp_path, "repeat", sources)
    log = tmp_path / "repeat.jsonl"
    snapshots: list[tuple[dict, int]] = []
    for target in (4, 12, 20):
        child = launch(project, log, session_id, workers=2, unit=5, force_lock=bool(snapshots))
        seen = wait_for(child, committed_at_least(target), timeout=300, what=f"{target} committed")
        killed = kill(child)
        assert killed.orphans == ()
        snapshots.append((facts(project, session_id), interpreter_pid(seen)))
    events = run_to_exit(launch(project, log, session_id, unit=5, force_lock=True), timeout=300)
    for at_kill, pid in snapshots:
        later = [
            item["sheet"] for item in events
            if item["event"] == "submitted" and item["t"] > max(
                (row["t"] for row in events if row["pid"] == pid), default=0
            )
        ]
        assert not (set(later) & at_kill["committed"])
    assert_resumed(
        project, session_id, expected, snapshots[-1][0], events, {pid for _f, pid in snapshots}
    )
