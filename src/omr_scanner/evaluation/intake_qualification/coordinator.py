"""The OMRFlow application under test: the coordinator process (revised phase 9).

Run by the supervisor as a separate operating-system process, killed by it
with ``TerminateProcess`` (``Process.kill``) and restarted into the same
project and the same scan session. Headless and Qt-free, it is what the
operational GUI runs underneath: the project opened as the application opens
it (migrations, project lock - forced after a kill), the production
:class:`~omr_scanner.services.continuous_engine.ContinuousEngine` with a warm
:class:`~omr_scanner.services.recognition_pool.ProcessRecogniser` pool of real
worker processes and an :class:`~omr_scanner.services.intake.IntakeService` on
the real disk, and - on a second thread, as the GUI thread would - the
scripted operator making Resolve and Rescan decisions while scanning goes on.

Evidence:
    Its own append-only log records what it *intends* before it does it:
    ``claimed`` (scan ids, written after the claim commits and before any is
    handed to a worker - the submission log), ``committed`` (after the work
    unit commits), operator decisions (after they commit), a heartbeat with the
    engine's counts and the process tree's memory, and the answers to the
    supervisor's commands. The supervisor never trusts it for what is durable:
    that is read from the database, from outside, after the kill.

Commands:
    The supervisor drops JSON files into the commands folder, named for this
    incarnation; each is executed once, in order, and answered with
    ``command_done``. ``hold`` / ``release`` bracket a consistent checkpoint
    (the engine and the operator stop at a step boundary); ``arm`` sets a pause
    point a kill then lands on; ``close`` is a clean application close; the
    endgame commands finish, reopen, release held files, reconcile, score and
    report through the operator.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

HEARTBEAT_SECONDS = 5.0


def _block(log: Any, where: str, **fields: Any) -> None:
    log.write("paused", at=where, **fields)
    while True:  # killed from outside; never returns
        time.sleep(3600)


def resolve_view(database: Any, session_id: str) -> dict[str, Any]:
    """What the Resolve stage shows on opening, before Scan is visited (case 15).

    The same service calls the Resolve page makes: the session-wide conflict
    counts and the open queue.
    """
    from omr_scanner.domain.review import ConflictState
    from omr_scanner.services import review_store, session_population

    batches = session_population.session_batch_ids(database, session_id)
    if not batches:
        return {"batches": 0, "open": 0, "resolved": 0, "open_ids": []}
    records = review_store.list_conflicts(
        database, batches[0], session_wide=True,
        filters=review_store.ConflictFilter(
            states=(ConflictState.OPEN, ConflictState.RESOLVED, ConflictState.DEFERRED)
        ),
    )
    return {
        "batches": len(batches),
        "open": sum(1 for item in records if item.state is ConflictState.OPEN),
        "resolved": sum(1 for item in records if item.state is ConflictState.RESOLVED),
        "open_ids": sorted(item.conflict_id for item in records
                           if item.state is ConflictState.OPEN),
    }


def runtime_facts(database: Any) -> dict[str, Any]:
    """Which runtime this incarnation really is, and its project connection's SQLite settings.

    Recorded so an installed-build run proves it ran the packaged executable
    (``frozen``, the module's location inside the bundle) and reports the
    SQLite library and connection settings the packaged application actually
    uses (``ARCHITECTURE_NOTES.md`` §13.3), not the build machine's.
    """
    import sqlite3

    from sqlalchemy import text

    import omr_scanner

    facts: dict[str, Any] = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "package_location": str(Path(omr_scanner.__file__).resolve().parent),
        "python": sys.version.split()[0],
        "sqlite_library": sqlite3.sqlite_version,
    }
    with database.session() as session:
        facts["sqlite_connection"] = str(session.execute(text("SELECT sqlite_version()")).scalar())
        for pragma in ("journal_mode", "synchronous", "busy_timeout", "foreign_keys"):
            facts[pragma] = session.execute(text(f"PRAGMA {pragma}")).scalar()
    return facts


class TimedFileSystem:
    """The production intake filesystem, with every source listing timed into the log.

    Used only when the run asks for it (``instrument_listing``) - the SMB
    qualification measures the listing cost of a real share per
    reconciliation (``ACCEPTANCE_CRITERIA.md`` §6). Reads pass straight
    through; nothing about the intake service's behaviour changes.
    """

    def __init__(self, inner: Any, log: Any) -> None:
        self._inner = inner
        self._log = log

    def list_source(self, root: str, *, recursive: bool, exclusions: Any) -> Any:
        """List ``root`` through the production filesystem, logging how long it took."""
        began = time.perf_counter()
        try:
            listing = self._inner.list_source(root, recursive=recursive, exclusions=exclusions)
        except Exception as exc:
            self._log.write("listing", root=root, seconds=round(time.perf_counter() - began, 6),
                            files=None, error=f"{type(exc).__name__}: {exc}"[:300])
            raise
        self._log.write("listing", root=root, seconds=round(time.perf_counter() - began, 6),
                        files=len(listing.files), error="")
        return listing

    def read_snapshot(self, path: str) -> Any:
        """Read ``path`` through the production filesystem (not timed)."""
        return self._inner.read_snapshot(path)


class Coordinator:
    """One incarnation of the application under test."""

    def __init__(self, run: dict[str, Any], incarnation: int, *, force_lock: bool) -> None:
        from omr_scanner.evaluation.intake_qualification.evidence import EvidenceLog

        self.run_spec = run
        self.incarnation = incarnation
        self.force_lock = force_lock
        self.log = EvidenceLog(Path(run["coordinator_log"]), campaign_id=run["campaign"],
                               role=f"coordinator:{incarnation}")
        self.commands = Path(run["commands"])
        self.done: set[str] = set()
        self.holding = threading.Event()
        self.stop_operator = threading.Event()
        self.operator_owned = threading.Event()  # the endgame drives the operator itself
        self.armed: dict[str, Any] = {}
        self.committed_since_start = 0
        self.expected_total = int(run["expected_total"])

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Open the project, rebuild and verify the plan, and build the engine."""
        from omr_scanner.domain.processing import EngineLimits, UnitPolicy
        from omr_scanner.evaluation.intake_qualification.cohort import plan_campaign, retime
        from omr_scanner.evaluation.intake_qualification.config import OPERATOR, CampaignConfig
        from omr_scanner.evaluation.intake_qualification.operator import OperatorActor
        from omr_scanner.services import open_project
        from omr_scanner.services.continuous_engine import ContinuousEngine
        from omr_scanner.services.intake import IntakeService
        from omr_scanner.services.recognition_pool import ProcessRecogniser
        from omr_scanner.services.recognition_settings import RecognitionOptions
        from omr_scanner.services.template_service import load_template

        run = self.run_spec
        self.log.write("starting", incarnation=self.incarnation, force_lock=self.force_lock,
                       python=sys.version.split()[0])
        self.project = open_project(Path(run["project"]), force_lock=self.force_lock)
        self.database = self.project.database
        self.log.write("runtime", **runtime_facts(self.database))
        self.session_id = run["session_id"]
        self.template_path = Path(run["template_path"])
        self.template = load_template(self.template_path)
        config = CampaignConfig.from_json(run["config"])
        plan = plan_campaign(config, self.template)
        if run.get("retime"):
            plan = retime(plan, int(run["retime"]["timing_seed"]), float(run["retime"]["duration"]))
        if plan.digest() != run["plan_digest"]:
            raise SystemExit("the plan this coordinator rebuilt differs from the supervisor's")
        self.plan = plan
        pool_index = json.loads(Path(run["pool_index"]).read_text(encoding="utf-8"))
        sha_to_content = {item["sha256"]: key for key, item in pool_index["items"].items()}
        self.log.write("resolve_view", **resolve_view(self.database, self.session_id))

        coordinator = self

        from omr_scanner.services.continuous_engine import EngineHooks

        class Hooks(EngineHooks):
            def claimed(self, claims: Any) -> None:
                coordinator.log.write("claimed", scans=[item.scan_id for item in claims])

            def committed(self, batch_id: str, claims: Any) -> None:
                ids = [item.scan_id for item in claims]
                coordinator.committed_since_start += len(ids)
                coordinator.log.write("committed", batch=batch_id, scans=ids)
                armed = coordinator.armed.get("after_commit")
                if armed is not None:
                    armed["seen"] += 1
                    if armed["seen"] >= armed["count"]:
                        _block(coordinator.log, "after_commit", batch=batch_id, scans=ids)
                armed = coordinator.armed.get("in_flight_commit")
                if armed is not None:
                    # The tail of a run: hold right after a commit that brings
                    # the session to the target while other sheets are still in
                    # a worker, so the kill lands on both (the engine pops this
                    # group from its in-flight set only after this hook).
                    others = coordinator.engine.in_flight - len(ids)
                    if others >= 1 and coordinator._committed() >= armed["min_committed"]:
                        _block(coordinator.log, "in_flight_commit", batch=batch_id, scans=ids,
                               in_flight_others=others)

            def submitted(self, claim: Any) -> None:
                armed = coordinator.armed.get("in_flight_commit")
                # Or a sheet handed to a worker once the session is at the
                # target (its claim is durable): the last sheets of a run may
                # all commit in one group, leaving no commit with others in
                # flight; a later claim then still lands the kill on work.
                if armed is not None and coordinator._committed() >= armed["min_committed"]:
                    _block(coordinator.log, "in_flight_commit", batch=claim.batch_id,
                           scans=[claim.scan_id], in_flight_others=coordinator.engine.in_flight,
                           when="submitted")

            def syncing_duplicates(self, batch_id: str, claims: Any) -> None:
                armed = coordinator.armed.get("syncing_duplicates")
                if armed is None:
                    return
                ids = [item.scan_id for item in claims]
                shas = coordinator._shas(ids)
                hit = sorted(set(shas.values()) & set(armed["shas"]))
                if hit:
                    _block(coordinator.log, "syncing_duplicates", batch=batch_id, scans=ids,
                           shas=hit)

            def registered(self, batch_id: str, scan_ids: Any) -> None:
                coordinator.log.write("registered", batch=batch_id, scans=len(scan_ids))

            def finalised(self, batch_id: str, status: str) -> None:
                coordinator.log.write("finalised", batch=batch_id, status=status)

        recogniser = ProcessRecogniser(
            self.template, workers=int(run["workers"]),
            options=RecognitionOptions(with_preview=False, keep_bubble_measurements=False),
        )
        filesystem = None
        if run.get("instrument_listing"):
            from omr_scanner.services.intake_fs import OsFileSystem

            filesystem = TimedFileSystem(OsFileSystem(), self.log)
        self.engine = ContinuousEngine(
            self.database,
            scan_session_id=self.session_id,
            template=self.template,
            recogniser=recogniser,
            intake_factory=lambda: IntakeService(self.database, self.project.root, fs=filesystem),
            limits=EngineLimits(
                max_in_flight=int(run["max_in_flight"]), claim_window=int(run["claim_window"]),
                max_commit_group=int(run["max_commit_group"]),
            ),
            unit_policy=UnitPolicy(
                max_unit_size=int(run["unit_size"]), trickle_seconds=float(run["trickle_seconds"])
            ),
            hooks=Hooks(),
            started_by=OPERATOR,
            template_path=self.template_path,
        )
        began = time.monotonic()
        report = self.engine.start()
        self.log.write(
            "started",
            seconds=round(time.monotonic() - began, 3),
            scans_returned=report.scan_recovery.scans_returned,
            interrupted_batches=report.scan_recovery.interrupted_batches,
            intake=str(report.intake_recovery) if report.intake_recovery else "",
            intent=report.controls.processing.value if report.controls else "",
        )
        self.operator = OperatorActor(
            self.database, self.session_id, plan, sha_to_content,
            log=self.log.write, late_progress=float(run.get("late_progress", 0.6)),
        )
        self.operator_thread = threading.Thread(
            target=self._operator_loop, name="scripted-operator", daemon=True
        )

    def _shas(self, scan_ids: list[int]) -> dict[int, str]:
        from sqlalchemy import select

        from omr_scanner.database.models import BatchScan

        with self.database.session() as session:
            return {
                int(a): str(b or "") for a, b in session.execute(
                    select(BatchScan.scan_id, BatchScan.content_sha256)
                    .where(BatchScan.scan_id.in_(scan_ids))
                ).all()
            }

    def _committed(self) -> int:
        """Sheets of the session whose recognition is committed (the durable count)."""
        from sqlalchemy import func, select

        from omr_scanner.database.models import BatchScan, ScanBatch

        with self.database.session() as session:
            return int(session.scalar(
                select(func.count()).select_from(BatchScan)
                .join(ScanBatch, ScanBatch.batch_id == BatchScan.batch_id)
                .where(ScanBatch.scan_session_id == self.session_id)
                .where(BatchScan.status.in_(("completed", "warning", "failed")))
            ) or 0)

    def _progress(self) -> float:
        return float(self._committed()) / max(1, self.expected_total)

    # ------------------------------------------------------------------
    def _operator_loop(self) -> None:
        interval = float(self.run_spec.get("operator_interval", 1.0))
        while not self.stop_operator.is_set():
            if self.holding.is_set() or self.operator_owned.is_set():
                time.sleep(0.05)
                continue
            try:
                self.operator.step(progress=self._progress(), budget=25)
            except Exception as exc:  # an operator error is evidence, never fatal
                self.log.write("operator_error", error=f"{type(exc).__name__}: {exc}",
                               trace=traceback.format_exc()[-2000:])
            self.stop_operator.wait(interval)

    def _heartbeat(self) -> None:
        try:
            import psutil

            me = psutil.Process()
            tree = [me, *me.children(recursive=True)]
            rss = sum(item.memory_info().rss for item in tree if item.is_running())
            workers = len(me.children(recursive=True))
            own = me.memory_info().rss
        except Exception:  # pragma: no cover - best effort
            rss = own = 0
            workers = -1
        status = self.engine.status()
        self.log.write(
            "heartbeat", registered=status.registered, completed=status.completed,
            failed=status.failed, pending=status.pending, processing=status.processing,
            in_flight=status.in_flight, writer_backlog=status.writer_backlog,
            ready_intake=status.ready_intake, last_error=status.last_error,
            tree_rss=rss, coordinator_rss=own, worker_processes=workers,
        )

    # ------------------------------------------------------------------
    def _pending_commands(self) -> list[Path]:
        prefix = f"{self.incarnation:03d}-"
        try:
            names = sorted(
                item.name for item in self.commands.iterdir()
                if item.name.startswith(prefix) and item.name.endswith(".json")
            )
        except FileNotFoundError:
            return []
        return [self.commands / name for name in names if name not in self.done]

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        """A command file, read once it is whole (the supervisor renames it into place)."""
        last: Exception | None = None
        for _ in range(100):
            try:
                data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                return data
            except (OSError, json.JSONDecodeError) as exc:  # a rename in progress
                last = exc
                time.sleep(0.02)
        raise RuntimeError(f"command file {path.name} unreadable: {last}")

    def _wait_command(self, name: str, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path in self._pending_commands():
                command = self._read(path)
                if command["name"] == name:
                    self.done.add(path.name)
                    return command
            time.sleep(0.05)
        return None

    def _answer(self, command: dict[str, Any], **result: Any) -> None:
        self.log.write("command_done", seq=command["seq"], name=command["name"], **result)

    def _execute(self, command: dict[str, Any]) -> bool:
        """Run one command; ``False`` ends the process."""
        from omr_scanner.evaluation.intake_qualification.config import OPERATOR

        name = command["name"]
        args = command.get("args", {})
        if name == "go":
            self._answer(command)
        elif name == "hold":
            self.holding.set()
            with self.operator.lock:
                status = self.engine.status()
                self.log.write("held", seq=command["seq"], registered=status.registered,
                               completed=status.completed, in_flight=status.in_flight,
                               processing=status.processing)
                release = self._wait_command("release", float(args.get("timeout", 900)))
                self.holding.clear()
                if release is None:
                    self._answer(command, released=False)
                    return True
                self._answer(release)
            self._answer(command, released=True)
        elif name == "arm":
            boundary = args["boundary"]
            if boundary == "operator":
                done = self.operator.stats.decisions
                self.operator.pause_after = done + int(args.get("count", 2))
                self.operator.on_pause = lambda n: _block(self.log, "operator", decisions=n)
            else:
                self.armed[boundary] = {"count": int(args.get("count", 1)), "seen": 0,
                                        "shas": list(args.get("shas", ())),
                                        "min_committed": int(args.get("min_committed", 0))}
            self._answer(command, boundary=boundary)
        elif name == "disarm":
            boundary = args["boundary"]
            if boundary == "operator":
                self.operator.pause_after = 0
            self.armed.pop(boundary, None)
            self._answer(command, boundary=boundary)
        elif name == "close":
            self.stop_operator.set()
            self.operator_thread.join(timeout=60)
            status = self.engine.shutdown(drain=True, timeout=60.0)
            self._answer(command, state=status.state.value, committed=status.sheets_committed)
            self.project.close()
            self.log.write("closed_cleanly", code=0)
            return False
        elif name == "stop":
            self.stop_operator.set()
            self.operator_thread.join(timeout=60)
            status = self.engine.shutdown(drain=True, timeout=120.0)
            self._answer(command, state=status.state.value)
            self.project.close()
            self.log.write("closed_cleanly", code=0)
            return False
        elif name == "operator_own":
            self.operator_owned.set()
            self._answer(command)
        elif name == "operator_final":
            self.operator_owned.set()
            made = self.operator.resolve_all()
            self._answer(command, decisions=made, unexpected=sorted(self.operator.stats.unexpected))
        elif name == "finish":
            outcome = self.engine.finish_session(closed_by=OPERATOR,
                                                 reason=str(args.get("reason", "")))
            self._answer(command, closed=outcome.closed,
                         blockers=[[item.code.value, item.count, item.source_label]
                                   for item in outcome.blockers],
                         sources=[[item.label, item.reachability] for item in outcome.sources])
        elif name == "reopen":
            from omr_scanner.services import session_finish

            info = session_finish.reopen_session(
                self.database, self.session_id, reopened_by=OPERATOR,
                reason=str(args.get("reason", "")),
            )
            self._answer(command, state=info.state.value, reopen_count=info.reopen_count)
        elif name == "release_held":
            names = {(item[0], item[1]) for item in args.get("files", ())}
            self._answer(command, released=self.operator.release_held(names))
        elif name == "downstream":
            began = time.monotonic()
            outcomes = self.operator.downstream(
                phase=str(args["phase"]), template=self.template,
                output_dir=Path(args["output_dir"]), inputs=Path(args["inputs"]),
                project_name=str(args.get("project_name", "Qualification")),
                final=bool(args.get("final", True)),
            )
            self._answer(command, seconds=round(time.monotonic() - began, 3), outcomes=outcomes)
        elif name == "reprocess":
            from omr_scanner.services import batch_store, scan_sessions

            batch = scan_sessions.start_reprocess_batch(
                self.database, str(args["batch_id"]),
                identity=batch_store.BatchIdentity.of(self.template, self.template_path),
                started_by=OPERATOR, reason="Reprocess All (qualification)",
            )
            self.log.write("operator_decision", action="reprocess_all",
                           batch=str(args["batch_id"]), reprocess_batch=batch)
            self._answer(command, reprocess_batch=batch)
        else:
            self._answer(command, error=f"unknown command {name!r}")
        return True

    # ------------------------------------------------------------------
    def loop(self) -> int:
        """Run commands and drive the engine until a command ends the incarnation."""
        if self.run_spec.get("await_go"):
            command = self._wait_command("go", float(self.run_spec.get("go_timeout", 600)))
            if command is None:
                self.log.write("no_go", reason="timed out waiting for the supervisor")
                return 4
            self._answer(command)
        self.operator_thread.start()
        last_beat = 0.0
        while True:
            for path in self._pending_commands():
                self.done.add(path.name)
                command = self._read(path)
                try:
                    if not self._execute(command):
                        return 0
                except Exception as exc:
                    self._answer(command, error=f"{type(exc).__name__}: {exc}",
                                 trace=traceback.format_exc()[-3000:])
            self.engine.poll_intake()
            self.engine.form_units()
            step = self.engine.step(wait=0.05)
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_SECONDS:
                self._heartbeat()
                last_beat = now
            if step.idle and not self.engine.in_flight:
                time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    """Run one coordinator incarnation from the command line."""
    parser = argparse.ArgumentParser(description="OMRFlow coordinator under qualification")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--incarnation", type=int, required=True)
    parser.add_argument("--force-lock", action="store_true")
    arguments = parser.parse_args(argv)
    run = json.loads(arguments.run.read_text(encoding="utf-8"))
    coordinator = Coordinator(run, arguments.incarnation, force_lock=arguments.force_lock)
    try:
        coordinator.start()
        return coordinator.loop()
    except BaseException as exc:
        coordinator.log.write("crashed", error=f"{type(exc).__name__}: {exc}",
                              trace=traceback.format_exc()[-4000:])
        raise


if __name__ == "__main__":  # pragma: no cover - a separate process
    sys.exit(main())
