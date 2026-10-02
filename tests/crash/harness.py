"""Supervisor side of the real-process crash tests (0.1.1 phase 3).

The parent half of :mod:`tests.crash.scan_resolve_child`. It launches the
child as a real operating-system process, watches the child's **external**
evidence log, kills it with ``Process.kill()`` (``TerminateProcess`` on
Windows - no shutdown handler, no flush, no lock release) at a *state*, never
after a sleep, and then inspects the project database from outside, read-only.

Kill points are state-based: "the child has logged N committed sheets", or
"the child has reached boundary X and logged ``paused``". Evidence of what was
committed comes from the database after the kill (the authoritative record),
evidence of what was *submitted to recognition* from the log, which the child
writes before a run hands a single sheet to a worker.

Reused from Phase 10 (:mod:`omr_scanner.evaluation.qualification`):
``kill_run_abruptly`` (coordinator-only kill, orphan check and clean-up) and
``integrity_report`` (``quick_check``, ``integrity_check``,
``foreign_key_check`` and the full Project Health check).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
from sqlalchemy import func, select, text

from omr_scanner.database.engine import open_project_database
from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    BatchSupersession,
    ProcessingManifest,
    ReviewConflict,
    ScanBatch,
    ScanSession,
)
from omr_scanner.evaluation.qualification import integrity_report, kill_run_abruptly

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parent / "scan_resolve_child.py"
TERMINAL = ("completed", "warning", "failed")
TEMPLATE_NAME = "synthetic_answer_sheet.omrt"
TEMPLATE_ID = "0b1a5e00-c4a5-4f3e-9a51-000000000003"


def template() -> Any:
    """The synthetic answer sheet the suite's recognition tests use.

    Not ``examples/templates/100_question_4_choice_example.omrt``: the stress
    dataset rendered against that template fails registration on every sheet
    (measured while building this harness), which would make every crash test
    a test of failures only. Against this one the same generator yields a
    realistic mix of complete, needs-review and unreadable sheets.
    """
    from tests.conftest import build_answer_sheet_template

    # One fixed id: the builder mints a new one per call, and results from
    # two projects are compared field by field.
    return build_answer_sheet_template().model_copy(update={"template_id": TEMPLATE_ID})


# ----------------------------------------------------------------------
# Datasets and projects
# ----------------------------------------------------------------------
def render_sheets(directory: Path, count: int, *, seed: int = 20261001) -> list[Path]:
    """Deterministic synthetic sheets with the stress dataset's mix of cases.

    Clean sheets, blanks, double marks, faint marks, blank/ambiguous/duplicate
    Student IDs, byte-identical duplicates, several sets and malformed files -
    so a crash test exercises real conflicts, duplicate-ID passes and failures.
    Files already present are reused.
    """
    from omr_scanner.evaluation import stress_dataset

    sheet_template = template()
    spec = stress_dataset.StressDatasetSpec(seed=seed, sheet_count=count)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = directory / f"sheet_{index:05d}.png"
        if not path.exists():
            sheet = stress_dataset.render_sheet_for_index(spec, sheet_template, index)
            path.write_bytes(sheet.malformed_bytes or sheet.png_bytes or b"")
        paths.append(path)
    return paths


def create_project_with_template(workspace: Path, name: str) -> Path:
    """A new project whose active template is :func:`template`."""
    from omr_scanner.services import create_project, save_template, set_active_template

    session = create_project(workspace, name)
    try:
        target = session.project.layout.templates_dir / TEMPLATE_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        save_template(template(), target)
        set_active_template(session, target)
        return session.root
    finally:
        session.close()


# ----------------------------------------------------------------------
# Evidence log
# ----------------------------------------------------------------------
def read_log(path: Path) -> list[dict[str, Any]]:
    """Every complete line of the evidence log (a torn last line is skipped)."""
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            events.append(json.loads(line))
    return events


def submissions(events: list[dict[str, Any]]) -> list[list[str]]:
    """The sheet names each run submitted to recognition, run by run."""
    return [list(item["paths"]) for item in events if item["event"] == "submitted"]


def committed_names(events: list[dict[str, Any]]) -> list[str]:
    """Every sheet name the child logged as committed, in order (may repeat)."""
    names: list[str] = []
    for item in events:
        if item["event"] == "committed":
            names.extend(item["paths"])
    return names


# ----------------------------------------------------------------------
# Child processes
# ----------------------------------------------------------------------
@dataclass
class Child:
    """One running child and its evidence log."""

    process: subprocess.Popen[bytes]
    log: Path
    project: Path

    def events(self) -> list[dict[str, Any]]:
        return read_log(self.log)


def launch(
    project: Path,
    log: Path,
    action: str,
    *,
    scans: Path | None = None,
    workers: int = 2,
    pause: str = "",
    pause_count: int = 0,
    clean_close_after: int = 0,
    decisions: int = 0,
    force_lock: bool = False,
) -> Child:
    """Start the child; it opens the project through the real main window."""
    args = [
        sys.executable,
        str(CHILD),
        "--project",
        str(project),
        "--log",
        str(log),
        "--action",
        action,
        "--workers",
        str(workers),
    ]
    if scans is not None:
        args += ["--scans", str(scans)]
    if pause:
        args += ["--pause", pause, "--pause-count", str(pause_count)]
    if clean_close_after:
        args += ["--clean-close-after", str(clean_close_after)]
    if decisions:
        args += ["--decisions", str(decisions)]
    if force_lock:
        args.append("--force-lock")
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT / "src"), environment.get("PYTHONPATH", "")]
    )
    process = subprocess.Popen(
        args,
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return Child(process=process, log=log, project=project)


def wait_for(child: Child, predicate: Any, *, timeout: float, what: str) -> list[dict[str, Any]]:
    """Poll the evidence log until ``predicate(events)`` holds, or fail.

    Polling, not sleeping-then-killing: the kill happens at the first poll
    that sees the state, whatever the machine's speed.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = child.events()
        if predicate(events):
            return events
        if child.process.poll() is not None:
            events = child.events()
            if predicate(events):
                return events
            raise AssertionError(
                f"child exited ({child.process.returncode}) before {what}; "
                f"last events: {events[-5:]}"
            )
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


TEARDOWN_ABORT_CODES = frozenset({0xC0000005, 0xC0000409})
"""Windows access violation / fail-fast, as process exit codes."""

TEARDOWN_ABORTS: list[str] = []
"""Children that aborted *after* their clean close (evidence, reported)."""


def run_to_exit(child: Child, *, timeout: float) -> list[dict[str, Any]]:
    """Wait for a child that should finish on its own, and require a clean close.

    Success is the child's own ``closed_cleanly`` record with code 0 as its
    **last** event: the scripted work finished and the application's close
    path ran (window closed, project and database released). The process exit
    after that is not under test: ``faulthandler`` records an access violation
    inside ``os._exit`` - DLL unload of the offscreen Qt / OpenCV / NumPy
    stack - in nearly every child, and in a minority of runs it becomes the
    exit code. Such an abort is accepted only after a clean close, and counted
    in :data:`TEARDOWN_ABORTS`; any other non-zero exit fails.
    """
    try:
        code = child.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.process.kill()
        raise AssertionError(f"child did not finish within {timeout}s") from None
    events = child.events()
    closed = bool(events) and events[-1]["event"] == "closed_cleanly"
    assert closed and events[-1].get("code") == 0, (
        f"child exited {code} without a clean close; last events: {events[-5:]}"
    )
    if code != 0:
        assert code in TEARDOWN_ABORT_CODES, f"child exited {code} after its clean close"
        TEARDOWN_ABORTS.append(f"{child.log.name}: {code:#x}")
    return events


@dataclass(frozen=True)
class Killed:
    """What a forced kill left: the evidence the parent gathered outside the child."""

    events: list[dict[str, Any]]
    orphans: tuple[int, ...]
    lock_left: bool
    exit_code: int | None
    hot_journal: bool = False
    """The kill landed inside a commit: SQLite rolled that transaction back."""


def kill(child: Child) -> Killed:
    """Kill the coordinator only, as Phase 10 does, and check its workers died with it."""
    try:
        workers = tuple(c.pid for c in psutil.Process(child.process.pid).children(recursive=True))
    except psutil.Error:
        workers = ()
    evidence = kill_run_abruptly(child.process, workers, child.project, committed_before_kill=0)  # type: ignore[arg-type]
    return Killed(
        events=child.events(),
        orphans=evidence.orphan_pids,
        lock_left=evidence.lock_file_left_behind,
        exit_code=evidence.exit_code,
        hot_journal=settle_journal(child.project),
    )


# ----------------------------------------------------------------------
# Database inspection (read-only, outside the child)
# ----------------------------------------------------------------------
HOT_JOURNALS_ROLLED_BACK: list[str] = []
"""Projects whose first read after a kill found a hot journal (evidence)."""


def settle_journal(project: Path) -> bool:
    """Let SQLite roll back a transaction a kill interrupted; return whether it had to.

    A kill that lands *inside* a commit leaves a hot rollback journal
    (``database.sqlite-journal``). SQLite rolls it back on the next open that
    can write - exactly what the application's own reopen does - but a
    read-only connection cannot, and fails with "attempt to write a readonly
    database". The committed state *is* the state after that rollback, so the
    supervisor performs it (one plain read on a writable connection) before
    inspecting read-only, and records that it happened.
    """
    import sqlite3

    journal = project / "database.sqlite-journal"
    if not journal.is_file() or journal.stat().st_size == 0:
        return False
    connection = sqlite3.connect(project / "database.sqlite")
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()
    HOT_JOURNALS_ROLLED_BACK.append(str(project))
    return True


def _database(project: Path) -> Any:
    settle_journal(project)
    return open_project_database(project / "database.sqlite", read_only=True)


def committed_rows(project: Path) -> dict[str, dict[str, Any]]:
    """``{file name: row facts}`` for every scan row of every batch."""
    database = _database(project)
    try:
        with database.session() as session:
            rows = session.execute(
                select(
                    BatchScan.filename,
                    BatchScan.batch_id,
                    BatchScan.status,
                    BatchScan.attempt_count,
                    BatchScan.result_json,
                    BatchScan.scan_id,
                )
            ).all()
    finally:
        database.close()
    return {
        str(name): {
            "batch_id": str(batch),
            "status": str(status),
            "attempts": int(attempts),
            "result": str(payload),
            "scan_id": int(scan_id),
        }
        for name, batch, status, attempts, payload, scan_id in rows
    }


def terminal_names(rows: dict[str, dict[str, Any]]) -> set[str]:
    return {name for name, row in rows.items() if row["status"] in TERMINAL}


def snapshot(project: Path) -> dict[str, Any]:
    """Counts and identities that a recovery must never change by itself."""
    database = _database(project)
    try:
        with database.session() as session:
            def count(model: Any) -> int:
                return int(session.scalar(select(func.count()).select_from(model)) or 0)

            conflicts = session.execute(
                select(
                    ReviewConflict.conflict_id,
                    ReviewConflict.scan_id,
                    ReviewConflict.conflict_type,
                    ReviewConflict.zone_id,
                    ReviewConflict.group_key,
                    ReviewConflict.state,
                    ReviewConflict.machine_value,
                ).order_by(ReviewConflict.conflict_id)
            ).all()
            audit = session.execute(
                select(AuditEvent.event_id, AuditEvent.action, AuditEvent.conflict_id)
                .order_by(AuditEvent.event_id)
            ).all()
            batches = session.execute(
                select(
                    ScanBatch.batch_id,
                    ScanBatch.scan_session_id,
                    ScanBatch.role,
                    ScanBatch.sealed_at,
                    ScanBatch.total_scans,
                ).order_by(ScanBatch.batch_id)
            ).all()
            sessions = session.execute(
                select(ScanSession.scan_session_id, ScanSession.state).order_by(
                    ScanSession.scan_session_id
                )
            ).all()
            results = session.execute(
                select(BatchScan.filename, BatchScan.status, BatchScan.result_json).order_by(
                    BatchScan.filename
                )
            ).all()
            other = {}
            for table in (
                "reconciliation_run",
                "reconciliation_entry",
                "reconciliation_script",
                "reconciliation_decision",
                "candidate_result",
                "scan_rejection",
                "generated_report",
                "batch_scan_history",
            ):
                other[table] = int(
                    session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                )
            return {
                "scan_sessions": [tuple(str(v) for v in row) for row in sessions],
                "scan_batches": [tuple(str(v) for v in row) for row in batches],
                "supersessions": count(BatchSupersession),
                "manifests": count(ProcessingManifest),
                "batch_scans": count(BatchScan),
                "conflicts": [tuple(row) for row in conflicts],
                "audit": [tuple(row) for row in audit],
                "results": [tuple(row) for row in results],
                "other_tables": other,
            }
    finally:
        database.close()


def duplicate_conflict_identities(project: Path) -> int:
    """Conflict identities appearing more than once (the unique constraint forbids it)."""
    database = _database(project)
    try:
        with database.session() as session:
            grouped = (
                select(
                    ReviewConflict.batch_id,
                    ReviewConflict.scan_id,
                    ReviewConflict.conflict_type,
                    ReviewConflict.zone_id,
                    ReviewConflict.group_key,
                )
                .group_by(
                    ReviewConflict.batch_id,
                    ReviewConflict.scan_id,
                    ReviewConflict.conflict_type,
                    ReviewConflict.zone_id,
                    ReviewConflict.group_key,
                )
                .having(func.count() > 1)
                .subquery()
            )
            return int(session.scalar(select(func.count()).select_from(grouped)) or 0)
    finally:
        database.close()


def detected_twice(project: Path) -> int:
    """Conflicts whose history records ``detected`` more than once."""
    database = _database(project)
    try:
        with database.session() as session:
            grouped = (
                select(AuditEvent.conflict_id)
                .where(AuditEvent.action == "detected")
                .group_by(AuditEvent.conflict_id)
                .having(func.count() > 1)
                .subquery()
            )
            return int(session.scalar(select(func.count()).select_from(grouped)) or 0)
    finally:
        database.close()


@dataclass(frozen=True)
class Integrity:
    """SQLite structural checks plus Project Health, gathered outside the child."""

    quick_check: list[str]
    integrity_check: list[str]
    foreign_key_check: list[Any]
    health_errors: list[str] = field(default_factory=list)
    health_codes: list[str] = field(default_factory=list)

    @property
    def sqlite_ok(self) -> bool:
        return (
            self.quick_check == ["ok"]
            and self.integrity_check == ["ok"]
            and self.foreign_key_check == []
        )


def integrity(project: Path) -> Integrity:
    settle_journal(project)
    report = integrity_report(project / "database.sqlite")
    issues = report.get("health_issues", [])
    return Integrity(
        quick_check=report.get("quick_check", []),
        integrity_check=report.get("integrity_check", []),
        foreign_key_check=report.get("foreign_key_check", []),
        health_errors=[item["code"] for item in issues if item["level"] == "error"],
        health_codes=[item["code"] for item in issues],
    )


__all__ = [
    "TEMPLATE_NAME",
    "Child",
    "Integrity",
    "Killed",
    "committed_names",
    "committed_rows",
    "create_project_with_template",
    "detected_twice",
    "duplicate_conflict_identities",
    "integrity",
    "kill",
    "launch",
    "read_log",
    "render_sheets",
    "run_to_exit",
    "snapshot",
    "submissions",
    "terminal_names",
    "wait_for",
]
