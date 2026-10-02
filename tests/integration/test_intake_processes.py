"""Intake with real processes: killed writers, a killed engine, a restart (revised phase 5).

Nothing is mocked here. Scanner writers are separate processes writing
counter-named JPEGs **non-atomically** to their final names; the intake engine
is a separate process (:mod:`tests.intake_child`) killed with ``kill()``
mid-loop, while the writers carry on, and then started again. The writers'
manifests - appended only after a file's last byte - are the ground truth.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select
from tests.intake_fakes import FakeClock, jpeg

from omr_scanner.database.models import BatchScan, IntakeFile
from omr_scanner.domain.intake import IntakeState, StabilityPolicy
from omr_scanner.services import create_project, open_project, scan_sessions
from omr_scanner.services import intake as intake_service
from omr_scanner.services.intake import IntakeService
from omr_scanner.services.intake_fs import OsFileSystem

REPO = Path(__file__).resolve().parents[2]
CHILD = [sys.executable, "-m", "tests.intake_child"]
FAST = StabilityPolicy(
    min_observations=2, quiet_seconds=0.6, max_decode_attempts=3, retry_backoff_seconds=0.3
)


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), str(REPO)])
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def spawn(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [*CHILD, *args], cwd=REPO, env=child_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def wait_for(predicate, timeout: float = 60.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not reached in time")


def manifest(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(line.split() for line in path.read_text(encoding="utf-8").splitlines() if line)


def test_a_writer_killed_mid_file_never_reaches_ready(project_session, answer_sheet_template):
    from omr_scanner.services import batch_store

    database = project_session.database
    folder = project_session.root.parent / "scanner"
    session_id = scan_sessions.create_scan_session(database, name="Exam").scan_session_id
    source = intake_service.create_source(
        database, label="A", root_path=str(folder), policy=StabilityPolicy(quiet_seconds=5)
    )
    intake_service.attach_source(database, source.source_id, session_id)
    clock = FakeClock()
    service = IntakeService(database, project_session.root, fs=OsFileSystem(), clock=clock)

    writer = spawn("writer", str(folder), "1", "7", "--chunks", "400", "--delay", "0.02")
    target = folder / "000001.jpg"
    wait_for(lambda: target.exists() and target.stat().st_size > 2000)
    writer.kill()
    writer.wait(timeout=30)
    complete = jpeg(7 * 100_000 + 1)
    assert 0 < target.stat().st_size < len(complete)  # really killed mid-file

    seen: list[IntakeState] = []
    for _ in range(12):
        service.reconcile(source.source_id)
        seen.append(intake_service.ledger(database)[0].state)
        clock.advance(6)
    assert IntakeState.READY not in seen
    assert seen[-1] is IntakeState.UNREADABLE
    assert service.ready_items(scan_session_id=session_id) == ()

    # The scanner re-writes the file completely: it stabilises normally.
    target.write_bytes(complete)
    service.reconcile(source.source_id)
    clock.advance(6)
    service.reconcile(source.source_id)
    items = service.ready_items(scan_session_id=session_id)
    assert len(items) == 1
    assert items[0].content_sha256 == hashlib.sha256(complete).hexdigest()
    outcome = service.register(
        scan_session_id=session_id, source_id=source.source_id,
        intake_file_ids=[items[0].intake_file_id],
        identity=batch_store.BatchIdentity.of(answer_sheet_template),
    )
    assert len(outcome.registered) == 1
    states = [row.state for row in intake_service.ledger(database)]
    assert states == [IntakeState.UNREADABLE, IntakeState.REGISTERED]


def test_engine_killed_while_writers_continue_then_restarted(tmp_path):
    """Writers keep writing while the intake engine is dead; every file once."""
    project = create_project(tmp_path, "intake-kill")
    root = project.root
    database = project.database
    session_id = scan_sessions.create_scan_session(database, name="Exam").scan_session_id
    folders = {name: tmp_path / "scanners" / name for name in ("A", "B", "C")}
    for name, folder in folders.items():
        folder.mkdir(parents=True)
        source = intake_service.create_source(
            database, label=name, root_path=str(folder), policy=FAST
        )
        intake_service.attach_source(database, source.source_id, session_id)
    project.close()

    manifests = {name: tmp_path / f"{name}.manifest" for name in folders}
    writers = [
        spawn(
            "writer", str(folder), "12", str(seed), "--chunks", "6", "--delay", "0.08",
            "--manifest", str(manifests[name]),
        )
        for seed, (name, folder) in enumerate(folders.items(), start=1)
    ]
    engine = spawn("intake", str(root), "--max-seconds", "120")
    # Let the engine register some files, then kill it hard mid-loop.
    wait_for(lambda: sum(len(manifest(m)) for m in manifests.values()) >= 9, timeout=90)
    time.sleep(1.5)
    engine.kill()
    first_output = engine.communicate(timeout=30)[0]
    assert "registered" in first_output, first_output
    for writer in writers:
        assert writer.wait(timeout=180) == 0
    truth = {
        (name, file): digest
        for name, path in manifests.items()
        for file, digest in manifest(path).items()
    }
    assert len(truth) == 36

    restarted = spawn("intake", str(root), "--until-idle", "4", "--max-seconds", "180")
    output = restarted.communicate(timeout=240)[0]
    assert restarted.returncode == 0, output
    assert "recovered" in output

    with open_project(root, read_only=True) as session:
        database = session.database
        sources = {
            source.source_id: source.label for source in intake_service.list_sources(database)
        }
        with database.session() as db:
            rows = db.execute(
                select(
                    IntakeFile.source_id, IntakeFile.relative_path, IntakeFile.state,
                    IntakeFile.content_sha256, IntakeFile.batch_scan_id,
                )
            ).all()
            scans = dict(db.execute(select(BatchScan.scan_id, BatchScan.content_sha256)).all())
    registered = [
        (sources[source], path, digest, scan)
        for source, path, state, digest, scan in rows
        if state == IntakeState.REGISTERED.value
    ]
    # Every completely written file registered exactly once, with its final bytes.
    assert sorted((label, path) for label, path, _d, _s in registered) == sorted(truth)
    for label, path, digest, scan in registered:
        assert digest == truth[(label, path)]
        assert scans[scan] == digest
    assert len(scans) == 36  # no double registration, no partial file
    complete = set(truth.values())
    assert set(scans.values()) <= complete
    # Source folders untouched: every file still holds its completed bytes.
    for (name, file), digest in truth.items():
        assert hashlib.sha256((folders[name] / file).read_bytes()).hexdigest() == digest
