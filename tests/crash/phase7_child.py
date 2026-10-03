"""The process the phase 7 crash tests kill (0.1.1 revised phase 7).

Run by :mod:`tests.crash.test_phase7_kills` as a **separate operating-system
process** - never imported by a test - so a forced termination is a real
``TerminateProcess``. Headless, no Qt. It opens the project as the
application does (migration, lock) and performs one ``--op``:

* ``engine`` - the continuous engine on the real filesystem until caught up
  (the restart sequence first). ``--pause`` stops it at a boundary:
  ``in_commit`` (inside the work-unit transaction of the commit holding
  ``--sheet``: result rows flushed, decision and commit not yet) or
  ``syncing_duplicates`` (the commit holding ``--sheet`` is durable, its
  cross-sheet duplicate pass not yet run).
* ``reject`` - confirm the suggested rescan of ``--scan`` (``reject_scan``).
* ``replace`` - confirm ``--replacement`` as ``--scan``'s rescan.
* ``close`` - close ``--session`` (the one-transaction close).
* ``reopen`` - reopen ``--session`` (named, audited).
* ``pause`` - persist *processing paused*, then start the engine and wait.

For the lifecycle ops ``--pause in_txn`` blocks at the first ORM flush of the
operation's own write transaction - after its rows reached the database file,
before its commit - so the parent kills it inside the transaction.

Every boundary is appended to an external JSON-lines evidence log (flushed and
fsynced) before it is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

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
    parser.add_argument("--op", required=True)
    parser.add_argument("--session", default="")
    parser.add_argument("--scan", type=int, default=0)
    parser.add_argument("--replacement", type=int, default=0)
    parser.add_argument("--sheet", default="")
    parser.add_argument("--pause", default="")
    parser.add_argument("--expect", type=int, default=0)
    parser.add_argument("--force-lock", action="store_true")
    arguments = parser.parse_args()

    from sqlalchemy import event, func, select
    from sqlalchemy.orm import Session

    from omr_scanner.database.models import IntakeFile
    from omr_scanner.domain.intake import IntakeState
    from omr_scanner.domain.processing import EngineLimits, UnitPolicy
    from omr_scanner.services import (
        load_template,
        open_project,
        quality_decisions,
        resolve_active_template,
        scan_lifecycle,
        scan_sessions,
        session_controls,
        session_finish,
    )
    from omr_scanner.services.continuous_engine import ContinuousEngine, EngineHooks
    from omr_scanner.services.intake import IntakeService
    from omr_scanner.services.recognition_pool import InlineRecogniser
    from omr_scanner.services.recognition_settings import RecognitionOptions

    log = EvidenceLog(arguments.log)
    log.write("starting", op=arguments.op, pause=arguments.pause)
    project = open_project(arguments.project, force_lock=arguments.force_lock)
    database = project.database

    def block(where: str, **fields: object) -> None:
        log.write("paused", at=where, **fields)
        while True:  # killed from outside; never returns
            time.sleep(3600)

    armed = {"flush": False}

    @event.listens_for(Session, "after_flush")
    def _pause_inside_the_transaction(session: Any, _context: Any) -> None:
        if armed["flush"]:
            armed["flush"] = False
            block("in_txn" if arguments.op != "engine" else "in_commit")

    def engine() -> ContinuousEngine:
        template_path = resolve_active_template(project.project)
        assert template_path is not None
        template = load_template(template_path)
        options = RecognitionOptions(with_preview=False, keep_bubble_measurements=False)

        class Hooks(EngineHooks):
            def submitted(self, claim: Any) -> None:
                log.write("submitted", sheet=Path(claim.path).stem, scan_id=claim.scan_id)

            def before_commit(self, batch_id: str, claims: Any) -> None:
                sheets = {Path(item.path).stem for item in claims}
                if arguments.pause == "in_commit" and arguments.sheet in sheets:
                    armed["flush"] = True

            def committed(self, batch_id: str, claims: Any) -> None:
                log.write("committed", sheets=[Path(item.path).stem for item in claims])

            def syncing_duplicates(self, batch_id: str, claims: Any) -> None:
                sheets = [Path(item.path).stem for item in claims]
                if arguments.pause == "syncing_duplicates" and arguments.sheet in sheets:
                    block("syncing_duplicates", sheets=sheets)

        return ContinuousEngine(
            database,
            scan_session_id=arguments.session,
            template=template,
            recogniser=InlineRecogniser(template, options=options),
            intake_factory=lambda: IntakeService(database, project.root),
            limits=EngineLimits(max_in_flight=2, claim_window=2, max_commit_group=1),
            unit_policy=UnitPolicy(max_unit_size=50, trickle_seconds=0),
            hooks=Hooks(),
            started_by="phase 7 crash child",
            template_path=template_path,
        )

    settled = (
        IntakeState.REGISTERED.value, IntakeState.DUPLICATE_CONTENT.value,
        IntakeState.IGNORED.value, IntakeState.UNREADABLE.value,
        IntakeState.UNSUPPORTED.value, IntakeState.HELD.value,
    )

    def run_engine(live: ContinuousEngine, *, seconds: float = 300.0) -> str:
        report = live.start()
        log.write(
            "started",
            intent=report.controls.processing.value if report.controls else "",
            returned=report.scan_recovery.scans_returned,
        )
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            live.poll_intake()
            live.form_units()
            step = live.step(wait=0.05)
            if step.idle:
                with database.session() as session:
                    rows = session.scalar(select(func.count()).select_from(IntakeFile)) or 0
                    unsettled = session.scalar(
                        select(func.count()).select_from(IntakeFile)
                        .where(IntakeFile.state.notin_(settled))
                    ) or 0
                if rows >= arguments.expect and not unsettled and live.status().caught_up:
                    return "caught_up"
                if not live.controls().processing_allowed and rows >= arguments.expect:
                    return "held_paused"
                time.sleep(0.05)
        return "timeout"

    op = arguments.op
    if op == "engine":
        outcome = run_engine(engine())
        log.write(outcome)
    elif op == "pause":
        session_controls.pause_processing(database, arguments.session, actor="crash child")
        live = engine()
        live.start()
        block("paused_and_running")
    else:
        armed["flush"] = arguments.pause == "in_txn"
        if op == "reject":
            quality_decisions.confirm_suggestion(database, arguments.scan, reviewer="Operator")
        elif op == "replace":
            scan_lifecycle.confirm_replacement(
                database, arguments.scan, arguments.replacement, reviewer="Operator"
            )
        elif op == "close":
            scan_sessions.close_scan_session(database, arguments.session, closed_by="Operator")
        elif op == "reopen":
            session_finish.reopen_session(database, arguments.session, reopened_by="Supervisor")
        else:
            raise SystemExit(f"unknown op {op}")
        armed["flush"] = False
        log.write("done", op=op)
    project.close()
    log.write("closed_cleanly", code=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
