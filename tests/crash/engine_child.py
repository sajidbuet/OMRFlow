"""The continuous-engine process a crash test kills (0.1.1 revised phase 6).

Run by :mod:`tests.crash.test_engine_kills` as a **separate operating-system
process** - never imported by a test - so a forced termination is a real
``TerminateProcess`` of a real coordinator with real ``spawn`` worker
processes (or reading in-process with ``--workers 1``).

Headless: no Qt. It opens the project as the application does
(:func:`~omr_scanner.services.open_project`: migration, lock), builds the real
intake service on the real filesystem and the real engine, and runs the
engine's own :meth:`~omr_scanner.services.continuous_engine.ContinuousEngine.start`
restart sequence. The only addition is an
:class:`~omr_scanner.services.continuous_engine.EngineHooks` that

* appends every boundary to an **external** JSON-lines evidence log, flushed
  and fsynced per line before the boundary is passed: ``submitted`` (the
  sheet handed to a worker - the submission log), ``committed`` (after the
  work-unit transaction returned), ``finalised``;
* at the boundary named by ``--pause`` (after ``--pause-count`` occurrences),
  writes ``paused`` and blocks forever, so the parent kills it at an exact
  committed state.

Pause boundaries: ``claimed`` (claims committed, nothing submitted),
``recognised`` (a worker's result in memory, nothing written), ``in_commit``
(inside the work-unit transaction: the result rows are flushed to the
database file, conflicts and commit not yet), ``committed`` (the transaction
committed, the engine has not acknowledged it), ``finalising`` (a unit's
batch-scope pass about to run), ``run`` (free-running; the parent kills it
once enough sheets are committed, with sheets inside the workers).

``--stop-after N`` shuts the engine down cleanly (draining) once N sheets have
committed, then exits - the clean-close case.

A sheet is named in the log by its content hash: a watched unit reads phase
5's content-addressed copy, whose file name is that hash.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT / "src") not in sys.path:  # pragma: no cover - child bootstrap
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


class EvidenceLog:
    """Append-only JSON lines, flushed and fsynced per event."""

    def __init__(self, path: Path) -> None:
        self._handle = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, event: str, **fields: object) -> None:
        record = {"event": event, "pid": os.getpid(), "t": time.time(), **fields}
        with self._lock:
            self._handle.write(json.dumps(record, default=str) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--unit", type=int, default=8)
    parser.add_argument("--expect", type=int, required=True)
    parser.add_argument("--pause", default="")
    parser.add_argument("--pause-count", type=int, default=1)
    parser.add_argument("--stop-after", type=int, default=0)
    parser.add_argument("--force-lock", action="store_true")
    arguments = parser.parse_args()

    from sqlalchemy import event, func, select
    from sqlalchemy.orm import Session

    from omr_scanner.database.models import IntakeFile
    from omr_scanner.domain.intake import IntakeState
    from omr_scanner.domain.processing import EngineLimits, UnitPolicy
    from omr_scanner.services import load_template, open_project, resolve_active_template
    from omr_scanner.services.continuous_engine import ContinuousEngine, EngineHooks
    from omr_scanner.services.intake import IntakeService
    from omr_scanner.services.recognition_pool import InlineRecogniser, ProcessRecogniser
    from omr_scanner.services.recognition_settings import RecognitionOptions

    log = EvidenceLog(arguments.log)
    log.write("starting", pause=arguments.pause, pause_count=arguments.pause_count)
    # After a kill the dead coordinator's lock remains; reclaiming it is the
    # operator's explicit decision in the application (the phase 3 harness
    # passes the same flag).
    project = open_project(arguments.project, force_lock=arguments.force_lock)
    database = project.database
    template_path = resolve_active_template(project.project)
    assert template_path is not None
    template = load_template(template_path)
    options = RecognitionOptions(with_preview=False, keep_bubble_measurements=False)

    def block(where: str, **fields: object) -> None:
        log.write("paused", at=where, **fields)
        while True:  # killed from outside; never returns
            time.sleep(3600)

    armed = {"in_commit": False}

    class Hooks(EngineHooks):
        def __init__(self) -> None:
            self.seen: dict[str, int] = {}
            self.committed_total = 0

        def _hit(self, name: str, **fields: object) -> None:
            self.seen[name] = self.seen.get(name, 0) + 1
            if arguments.pause == name and self.seen[name] >= arguments.pause_count:
                block(name, **fields)

        def claimed(self, claims):  # type: ignore[no-untyped-def]
            self._hit("claimed", sheets=[Path(item.path).stem for item in claims])

        def submitted(self, claim):  # type: ignore[no-untyped-def]
            log.write("submitted", sheet=Path(claim.path).stem, scan_id=claim.scan_id)

        def recognised(self, claim):  # type: ignore[no-untyped-def]
            self._hit("recognised", sheet=Path(claim.path).stem)

        def before_commit(self, batch_id, claims):  # type: ignore[no-untyped-def]
            self.seen["before_commit"] = self.seen.get("before_commit", 0) + 1
            if arguments.pause == "in_commit" and self.seen["before_commit"] >= arguments.pause_count:
                armed["in_commit"] = True

        def committed(self, batch_id, claims):  # type: ignore[no-untyped-def]
            sheets = [Path(item.path).stem for item in claims]
            self.committed_total += len(sheets)
            log.write("committed", sheets=sheets, batch_id=batch_id)
            self._hit("committed", sheets=sheets)

        def finalising(self, batch_id):  # type: ignore[no-untyped-def]
            self._hit("finalising", batch_id=batch_id)

        def finalised(self, batch_id, status):  # type: ignore[no-untyped-def]
            log.write("finalised", batch_id=batch_id, status=status)

    @event.listens_for(Session, "after_flush")
    def _pause_inside_the_work_unit(session, _context):  # type: ignore[no-untyped-def]
        # The first flush after `before_commit` is record_results' own: the
        # result rows are written to the database file; conflicts and the
        # commit have not happened yet.
        if armed["in_commit"]:
            armed["in_commit"] = False
            block("in_commit")

    hooks = Hooks()
    recogniser = (
        ProcessRecogniser(template, workers=arguments.workers, options=options)
        if arguments.workers > 1
        else InlineRecogniser(template, options=options)
    )
    engine = ContinuousEngine(
        database,
        scan_session_id=arguments.session,
        template=template,
        recogniser=recogniser,
        intake_factory=lambda: IntakeService(database, project.root),
        limits=EngineLimits.for_workers(max(1, arguments.workers)),
        unit_policy=UnitPolicy(max_unit_size=arguments.unit, trickle_seconds=0),
        hooks=hooks,
        started_by="crash child",
        template_path=template_path,
    )
    report = engine.start()
    log.write(
        "started",
        returned=report.scan_recovery.scans_returned,
        interrupted=report.scan_recovery.interrupted_batches,
    )

    settled = (
        IntakeState.REGISTERED.value, IntakeState.DUPLICATE_CONTENT.value,
        IntakeState.IGNORED.value, IntakeState.UNREADABLE.value,
        IntakeState.UNSUPPORTED.value, IntakeState.HELD.value,
    )

    def finished() -> bool:
        with database.session() as session:
            rows = session.scalar(select(func.count()).select_from(IntakeFile)) or 0
            open_rows = session.scalar(
                select(func.count()).select_from(IntakeFile).where(IntakeFile.state.notin_(settled))
            ) or 0
        return rows >= arguments.expect and not open_rows and engine.status().caught_up

    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        engine.poll_intake()
        engine.form_units()
        step = engine.step(wait=0.1)
        if arguments.stop_after and hooks.committed_total >= arguments.stop_after:
            status = engine.shutdown(drain=True, timeout=60)
            log.write("stopped", processing=status.processing, pending=status.pending)
            break
        if step.idle and finished():
            engine.shutdown()
            log.write("caught_up")
            break
        if step.idle:
            time.sleep(0.05)
    else:
        log.write("timeout")
        return 2
    project.close()
    log.write("closed_cleanly", code=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
