"""Real process kills at the durable transitions revised phase 7 added.

Each case launches :mod:`tests.crash.phase7_child` as a separate process,
waits for an exact state in its external evidence log, kills it with
``TerminateProcess`` (no shutdown handler runs), and checks the project from
outside: the transition is all-or-nothing, nothing is duplicated, recovery
converges, and the project is not left owned by the dead coordinator.

Cases (``PHASE_G_HANDOFF.md`` §11):

* inside the work-unit transaction of a sheet whose decision is a suggested
  rescan - no half decision, one suggestion afterwards, nothing rejected;
* after a commit, before its cross-sheet duplicate pass - recovery raises the
  duplicate without reading the sheet again;
* inside the confirmed rejection, the replacement confirmation, the close and
  the reopen transactions - each all or nothing, audited once;
* persisted *pause* then a kill - still paused after the restart;
* a kill while processing runs - the next start resumes it (stored intent
  ``running``), and no coordinator ownership outlives the process.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
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
from tests.engine_rig import digest, readable_sheets
from tests.quality_rig import blank_page, displaced_id, rescan_of, roll_of

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    ReviewConflict,
    ScanBatch,
    ScanQualityDecision,
    ScanRejection,
    ScanSession,
)
from omr_scanner.domain.intake import StabilityPolicy
from omr_scanner.domain.processing import EngineLimits, UnitPolicy
from omr_scanner.domain.quality_decision import QualityDecision
from omr_scanner.domain.review import ConflictType, ReasonCode
from omr_scanner.services import (
    intake,
    load_template,
    open_project,
    quality_decisions,
    resolve_active_template,
    review_store,
    scan_sessions,
    session_controls,
)
from omr_scanner.services.continuous_engine import ContinuousEngine
from omr_scanner.services.intake import IntakeService
from omr_scanner.services.recognition_pool import InlineRecogniser

CHILD = Path(__file__).resolve().parent / "phase7_child.py"
POLICY = StabilityPolicy(
    min_observations=2, quiet_seconds=0.3, max_decode_attempts=3, retry_backoff_seconds=0.5,
    poll_interval_seconds=0.0,
)
SHEETS = readable_sheets(48)


def launch(project: Path, log: Path, op: str, *, force_lock: bool = True, **options: Any) -> Child:
    args = [sys.executable, str(CHILD), "--project", str(project), "--log", str(log), "--op", op]
    for key, value in options.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    if force_lock:
        args.append("--force-lock")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPOSITORY_ROOT / "src"), str(REPOSITORY_ROOT), environment.get("PYTHONPATH", "")]
    )
    errors = log.with_name(f"{log.stem}-{len(list(log.parent.glob(log.stem + '-*.err')))}.err")
    with errors.open("wb") as stderr:
        process = subprocess.Popen(
            args, cwd=REPOSITORY_ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=stderr,
        )
    return Child(process=process, log=log, project=project)


def paused(events: list[dict[str, Any]]) -> bool:
    return any(item["event"] == "paused" for item in events)


def prepare(
    workspace: Path, name: str, files: dict[str, list[tuple[str, bytes]]]
) -> tuple[Path, str]:
    """A project with the harness template, one open session and watched folders."""
    root = create_project_with_template(workspace, name)
    folders = workspace / f"{name}-sources"
    project = open_project(root)
    try:
        session_id = scan_sessions.create_scan_session(
            project.database, name="Exam", created_by="op"
        ).scan_session_id
        for folder, contents in files.items():
            path = folders / folder
            path.mkdir(parents=True, exist_ok=True)
            for file_name, data in contents:
                (path / file_name).write_bytes(data)
            info = intake.create_source(
                project.database, label=folder, root_path=str(path), created_by="op",
                policy=POLICY,
            )
            intake.attach_source(project.database, info.source_id, session_id, actor="op")
    finally:
        project.close()
    return root, session_id


def process_in_process(root: Path, session_id: str, *, seconds: float = 120.0) -> None:
    """Run the engine here, on the real filesystem, until caught up (test set-up)."""
    project = open_project(root)
    try:
        template = load_template(resolve_active_template(project.project))  # type: ignore[arg-type]
        engine = ContinuousEngine(
            project.database, scan_session_id=session_id, template=template,
            recogniser=InlineRecogniser(template),
            intake_factory=lambda: IntakeService(project.database, project.root),
            limits=EngineLimits(max_in_flight=2, claim_window=2),
            unit_policy=UnitPolicy(max_unit_size=50, trickle_seconds=0),
        )
        engine.start()
        deadline = time.monotonic() + seconds
        quiet = 0
        while time.monotonic() < deadline:
            engine.poll_intake()
            engine.form_units()
            step = engine.step()
            if step.idle and engine.status().caught_up:
                quiet += 1
                if quiet > 20:
                    break
                time.sleep(0.05)
            else:
                quiet = 0
        engine.shutdown()
    finally:
        project.close()


def read(root: Path, statement: Any) -> list[Any]:
    database = _database(root)
    try:
        with database.session() as session:
            return list(session.execute(statement).all())
    finally:
        database.close()


def scan_with(root: Path, data: bytes) -> int:
    rows = read(root, select(BatchScan.scan_id).where(BatchScan.content_sha256 == digest(data)))
    assert len(rows) == 1, rows
    return int(rows[0][0])


def events_of(root: Path, entity_id: str) -> list[str]:
    return [
        str(row[0])
        for row in read(
            root,
            select(AuditEvent.action)
            .where(AuditEvent.entity_id == entity_id)
            .order_by(AuditEvent.event_id),
        )
    ]


def clean(root: Path) -> None:
    checks = integrity(root)
    assert checks.sqlite_ok, checks
    assert checks.health_errors == [], checks.health_errors


def submitted_by(events: list[dict[str, Any]], pid: int) -> list[str]:
    return [item["sheet"] for item in events if item["event"] == "submitted" and item["pid"] == pid]


def test_kill_inside_the_work_unit_of_a_suggested_rescan(tmp_path):
    blank = digest(blank_page())
    root, session_id = prepare(tmp_path, "unit", {
        "a": [("0.png", SHEETS[0]), ("1.png", blank_page()), ("2.png", SHEETS[2])],
    })
    log = tmp_path / "unit.jsonl"
    child = launch(root, log, "engine", session=session_id, pause="in_commit", sheet=blank,
                   expect=3, force_lock=False)
    seen = wait_for(child, paused, timeout=300, what="inside the blank page's work unit")
    killed = kill(child)
    assert killed.orphans == ()
    assert not read(root, select(ScanQualityDecision.scan_id).join(
        BatchScan, BatchScan.scan_id == ScanQualityDecision.scan_id
    ).where(BatchScan.content_sha256 == blank)), "no half-written decision"
    events = run_to_exit(launch(root, log, "engine", session=session_id, expect=3), timeout=300)
    killed_pid = int(seen[-1]["pid"])
    resumed = [item for item in events if item["pid"] != killed_pid]
    assert any(item["event"] == "caught_up" for item in resumed)
    scan = scan_with(root, blank_page())
    decisions = read(root, select(ScanQualityDecision.decision).where(
        ScanQualityDecision.scan_id == scan
    ))
    assert decisions == [(QualityDecision.RESCAN_REQUIRED.value,)]
    assert read(root, select(func.count()).select_from(ScanRejection)) == [(0,)]
    database = _database(root)
    try:
        assert quality_decisions.count_outstanding(database, session_id) == 1
    finally:
        database.close()
    clean(root)


def test_kill_between_a_commit_and_its_duplicate_pass(tmp_path):
    second = digest(SHEETS[18])
    root, session_id = prepare(tmp_path, "dup", {
        "a": [("a.png", SHEETS[17])],
        "b": [("a-second.png", SHEETS[18]), ("b-later0.png", SHEETS[30]),
              ("b-later1.png", SHEETS[31])],
    })
    log = tmp_path / "dup.jsonl"
    child = launch(root, log, "engine", session=session_id, pause="syncing_duplicates",
                   sheet=second, expect=4, force_lock=False)
    seen = wait_for(child, paused, timeout=300, what="after the commit, before the pass")
    assert kill(child).orphans == ()
    target = scan_with(root, SHEETS[18])
    assert read(root, select(BatchScan.status).where(BatchScan.scan_id == target))[0][0] in (
        "completed", "warning"
    )
    duplicates = select(ReviewConflict.scan_id).where(
        ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value
    )
    assert (target,) not in read(root, duplicates)
    status = read(root, select(ScanBatch.status).join(
        BatchScan, BatchScan.batch_id == ScanBatch.batch_id).where(BatchScan.scan_id == target))
    assert status == [("running",)], "the unit records that its cross-sheet pass is owed"
    events = run_to_exit(launch(root, log, "engine", session=session_id, expect=4), timeout=300)
    # The restarted process is the one that logged the *last* "started" event.
    # (Not the highest pid: Windows does not hand out increasing pids, and the
    # killed child - which legitimately read this sheet - sometimes has the
    # larger one; revised phase 8 baseline run 2026-10-04_221344.)
    restarted = [item for item in events if item["event"] == "started"][-1]["pid"]
    assert second not in submitted_by(events, restarted), (
        "a committed sheet must not be read again to regenerate its conflicts"
    )
    first = scan_with(root, SHEETS[17])
    assert sorted(row[0] for row in read(root, duplicates)) == sorted([first, target])
    clean(root)
    assert seen


def _with_suggestions(tmp_path: Path, name: str) -> tuple[Path, str, int, int]:
    """A processed session: a displaced-identifier sheet (suggested rescan) and its rescan."""
    root, session_id = prepare(tmp_path, name, {
        "a": [("0.png", SHEETS[0]), ("moved.png", displaced_id(7))],
    })
    process_in_process(root, session_id)
    original = scan_with(root, displaced_id(7))
    return root, session_id, original, 0


@pytest.mark.parametrize("op", ["reject", "replace"])
def test_kill_inside_a_lifecycle_confirmation(tmp_path, op):
    root, session_id, original, _ = _with_suggestions(tmp_path, op)
    replacement = 0
    if op == "replace":
        project = open_project(root)
        try:
            quality_decisions.confirm_suggestion(
                project.database, original, reviewer="Operator",
                declared_candidate_id=roll_of(SHEETS[7]),
            )
        finally:
            project.close()
        folder = tmp_path / f"{op}-sources" / "c"
        folder.mkdir(parents=True)
        (folder / "again.png").write_bytes(rescan_of(7))
        project = open_project(root)
        try:
            info = intake.create_source(project.database, label="c", root_path=str(folder),
                                        created_by="op", policy=POLICY)
            intake.attach_source(project.database, info.source_id, session_id, actor="op")
        finally:
            project.close()
        process_in_process(root, session_id)
        replacement = scan_with(root, rescan_of(7))
    before = events_of(root, str(original))
    log = tmp_path / f"{op}.jsonl"
    options: dict[str, Any] = {"scan": original}
    if replacement:
        options["replacement"] = replacement
    child = launch(root, log, op, pause="in_txn", force_lock=False, **options)
    wait_for(child, paused, timeout=120, what=f"inside the {op} transaction")
    assert kill(child).orphans == ()
    # All or nothing: the change and its audit events rolled back together.
    assert events_of(root, str(original)) == before
    state = read(root, select(ScanRejection.state).where(ScanRejection.scan_id == original))
    assert state == ([] if op == "reject" else [("rejected_pending_rescan",)])
    run_to_exit(launch(root, log, op, **options), timeout=120)
    after = events_of(root, str(original))
    expected = "rejected" if op == "reject" else "replaced"
    assert after == [*before, expected], "audited exactly once"
    state = read(root, select(ScanRejection.state).where(ScanRejection.scan_id == original))
    assert state == [
        ("rejected_pending_rescan",) if op == "reject" else ("superseded_by_replacement",)
    ]
    if replacement:
        assert events_of(root, str(replacement)) == ["linked_replacement"]
    clean(root)


@pytest.mark.parametrize("op", ["close", "reopen"])
def test_kill_inside_close_or_reopen_is_never_half_done(tmp_path, op):
    root, session_id = prepare(tmp_path, op, {"a": [("0.png", SHEETS[0])]})
    process_in_process(root, session_id)
    project = open_project(root)
    try:
        with project.database.session() as session:
            conflicts = session.scalars(select(ReviewConflict.conflict_id)).all()
        for conflict in conflicts:
            review_store.accept_machine_value(
                project.database, conflict, reviewer="op", reason=ReasonCode.MACHINE_CONFIRMED
            )
        if op == "reopen":
            scan_sessions.close_scan_session(project.database, session_id, closed_by="op")
    finally:
        project.close()

    def facts() -> tuple[Any, ...]:
        row = read(root, select(ScanSession.state, ScanSession.final_outputs_stale_since)
                   .where(ScanSession.scan_session_id == session_id))[0]
        sealed = read(root, select(ScanBatch.sealed_at).where(
            ScanBatch.scan_session_id == session_id))
        return str(row[0]), row[1] is not None, tuple(item[0] is not None for item in sealed)

    before = facts()
    before_events = events_of(root, session_id)
    log = tmp_path / f"{op}.jsonl"
    child = launch(root, log, op, session=session_id, pause="in_txn", force_lock=False)
    wait_for(child, paused, timeout=120, what=f"inside the {op} transaction")
    assert kill(child).orphans == ()
    assert facts() == before, "a killed transition leaves the session exactly as it was"
    assert events_of(root, session_id) == before_events
    run_to_exit(launch(root, log, op, session=session_id), timeout=120)
    state, stale, sealed = facts()
    if op == "close":
        assert state == "closed" and all(sealed)
        assert events_of(root, session_id)[len(before_events):] == ["session_closed"]
    else:
        assert state == "open" and stale
        assert events_of(root, session_id)[len(before_events):] == ["session_reopened"]
    clean(root)


def test_a_persisted_pause_survives_a_kill_and_running_intent_resumes(tmp_path):
    root, session_id = prepare(tmp_path, "pause", {
        "a": [(f"{i}.png", SHEETS[i]) for i in range(6)],
    })
    log = tmp_path / "pause.jsonl"
    child = launch(root, log, "pause", session=session_id, force_lock=False)
    wait_for(child, paused, timeout=120, what="paused with the engine started")
    assert kill(child).orphans == ()
    events = run_to_exit(launch(root, log, "engine", session=session_id, expect=6), timeout=300)
    restarted = [item for item in events if item["event"] == "started"][-1]
    assert restarted["intent"] == "paused"
    assert not submitted_by(events, restarted["pid"]), "a paused session must not resume itself"
    assert any(
        item["event"] == "held_paused" and item["pid"] == restarted["pid"] for item in events
    )
    project = open_project(root, force_lock=True)
    try:
        session_controls.resume_processing(project.database, session_id, actor="op")
    finally:
        project.close()
    # Kill while it runs; the stored intent is running, so the next start resumes.
    child = launch(root, log, "engine", session=session_id, expect=6)
    wait_for(child, lambda items: any(i["event"] == "committed" for i in items
                                      if i["pid"] != restarted["pid"]),
             timeout=300, what="a commit after resuming")
    assert kill(child).orphans == ()
    # No coordinator ownership outlives the killed process: this one starts.
    events = run_to_exit(launch(root, log, "engine", session=session_id, expect=6), timeout=300)
    last = [item for item in events if item["event"] == "started"][-1]
    assert last["intent"] == "running"
    assert events[-2]["event"] == "caught_up"
    statuses = read(root, select(BatchScan.status))
    assert {row[0] for row in statuses} <= {"completed", "warning", "failed"}
    clean(root)
    shutil.rmtree(tmp_path / "pause-sources", ignore_errors=True)
