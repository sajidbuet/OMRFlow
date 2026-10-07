"""The supervisor: runs a campaign from outside every process it tests (revised phase 9).

Purpose:
    Plan the campaign, render its images, build its projects, start the
    scanner writers and the OMRFlow coordinator as separate processes, and
    drive the run: consistent checkpoints, a source's folder disappearing and
    returning, forced kills of the coordinator chosen from observed state
    (committed and in-flight work, a pause point reached), clean closes,
    restarts into the same project and session, the operator's endgame,
    closure, reports, a late file held, a reopen, a re-close. Everything it
    observes it writes to its own append-only log - the one process nobody
    kills - and every durable fact it uses is read from the database
    read-only, after the kill, never taken from the killed process's word.

Runs of one campaign (sharing the plan and the rendered images):
    * ``interrupted`` - the qualification run: kills, outage, everything;
    * ``control`` - the same logical cohort, uninterrupted (arrival timing
      redrawn), for the "interrupted equals uninterrupted" comparisons;
    * ``finite`` - the same cohort as one finite batch through the finite
      Scan-stage path, for the finite-mode control (``finite.py``).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omr_scanner.evaluation.intake_qualification import cohort, reference
from omr_scanner.evaluation.intake_qualification.config import CampaignConfig
from omr_scanner.evaluation.intake_qualification.evidence import EvidenceLog, LogTail

SRC_ROOT = Path(__file__).resolve().parents[3]
TEARDOWN_ABORT_CODES = frozenset({0xC0000005, 0xC0000409})


class CampaignError(RuntimeError):
    """The campaign could not continue (a stage timed out, a process failed)."""

    def __init__(
        self, message: str, *, stage: str, diagnostics: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.diagnostics = diagnostics or {}


class CampaignInterruptedError(CampaignError):
    """The operator interrupted the harness (Ctrl+C)."""


# ----------------------------------------------------------------------
# Filesystem helpers
# ----------------------------------------------------------------------
def make_link(link: Path, target: Path) -> None:
    """A directory link ``link`` -> ``target`` (a junction on Windows)."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:  # pragma: no cover - the release campaign runs on Windows
        link.symlink_to(target, target_is_directory=True)


def remove_link(link: Path, *, attempts: int = 200) -> None:
    """Remove a directory link (never its target); retries while it is in use."""
    last: OSError | None = None
    for _ in range(attempts):
        try:
            if sys.platform == "win32":
                link.rmdir()
            else:  # pragma: no cover
                link.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    raise OSError(f"could not remove the link {link}: {last}")


def python_env() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(SRC_ROOT), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    environment["PYTHONUNBUFFERED"] = "1"
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    return environment


PACKAGED_COORDINATOR_ARGUMENT = "--intake-qualification-coordinator"
"""The hidden first argument the packaged executable runs the coordinator with
(``omr_scanner.main.QUALIFICATION_COORDINATOR_ARGUMENT``; repeated here so the
harness does not import the application's entry point)."""

_DEVELOPMENT_VARIABLES = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV",
    "CONDA_PREFIX", "CONDA_DEFAULT_ENV",
)


def packaged_env() -> dict[str, str]:
    """The environment for a packaged coordinator: nothing pointing at a Python or the source."""
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() not in _DEVELOPMENT_VARIABLES
    }
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    return environment


def coordinator_command_for(executable: Path) -> tuple[str, ...]:
    """The command prefix that runs the coordinator inside a packaged ``OMRFlow.exe``."""
    return (str(executable), PACKAGED_COORDINATOR_ARGUMENT)


def descendants(pid: int) -> tuple[int, ...]:
    import psutil

    try:
        return tuple(item.pid for item in psutil.Process(pid).children(recursive=True))
    except psutil.Error:
        return ()


def tree_memory(pid: int, *, launcher: bool = True) -> tuple[int, int, int]:
    """``(tree RSS, coordinator RSS, processes)`` of ``pid``'s whole process tree.

    ``launcher``: ``pid`` is the venv's launcher and the interpreter its first
    child (a source run); a packaged executable is the coordinator itself.
    """
    import psutil

    try:
        root = psutil.Process(pid)
        members = [root, *root.children(recursive=True)]
    except psutil.Error:
        return 0, 0, 0
    total = own = 0
    count = 0
    for member in members:
        try:
            rss = member.memory_info().rss
        except psutil.Error:
            continue
        total += rss
        count += 1
        if member.pid == pid:
            own = rss
    # The venv launcher is the root; the interpreter is its child.
    if launcher and len(members) > 1:
        with contextlib.suppress(psutil.Error):
            own = max(own, members[1].memory_info().rss)
    return total, own, count


# ----------------------------------------------------------------------
# The campaign
# ----------------------------------------------------------------------
@dataclass
class Campaign:
    """One campaign: its folder, plan, rendered pool and supervisor log."""

    config: CampaignConfig
    campaign_id: str
    root: Path
    template_path: Path
    plan: cohort.CampaignPlan
    pool: dict[str, Any]
    log: EvidenceLog
    started: float = field(default_factory=time.time)
    timings: dict[str, float] = field(default_factory=dict)
    coordinator_command: tuple[str, ...] = ()
    """Empty: the coordinator runs from source (``python -m``). Otherwise the
    command prefix of a packaged executable (:func:`coordinator_command_for`)."""

    @property
    def pool_index(self) -> Path:
        """The rendered pool's index file."""
        from omr_scanner.evaluation.intake_qualification.render import POOL_INDEX

        return self.root / "pool" / POOL_INDEX

    def sha_to_content(self) -> dict[str, str]:
        """Rendered image SHA-256 -> content key."""
        return {item.sha256: key for key, item in self.pool.items()}


def new_campaign_id(config: CampaignConfig) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"p9-{config.mode.value}-{stamp}-{config.seed}"


def prepare_campaign(
    config: CampaignConfig,
    output_root: Path,
    template_path: Path,
    *,
    render_workers: int | None = None,
    progress: Any = None,
    coordinator_command: tuple[str, ...] = (),
) -> Campaign:
    """Plan, render and lay out a new campaign folder (never reusing an old one)."""
    from omr_scanner.evaluation.intake_qualification.render import render_pool
    from omr_scanner.services.template_service import load_template

    campaign_id = new_campaign_id(config)
    output_root = output_root.resolve()
    template_path = template_path.resolve()
    root = output_root / campaign_id
    if root.exists():
        raise CampaignError(f"{root} already exists", stage="prepare")
    root.mkdir(parents=True)
    log = EvidenceLog(root / "supervisor.jsonl", campaign_id=campaign_id, role="supervisor")
    log.write("campaign_started", config=config.to_json(), template=str(template_path))
    template = load_template(template_path)
    began = time.monotonic()
    plan = cohort.plan_campaign(config, template)
    (root / "manifest.json").write_text(json.dumps(plan.to_json(), indent=0), encoding="utf-8")
    log.write("planned", digest=plan.digest(), contents=len(plan.contents),
              arrivals=len(plan.main_arrivals), seconds=round(time.monotonic() - began, 2))
    began = time.monotonic()
    workers = render_workers or max(1, (os.cpu_count() or 2) - 2)
    pool = render_pool(plan, template_path, root / "pool", workers=workers, progress=progress)
    rendered = time.monotonic() - began
    log.write("rendered", images=len(pool), seconds=round(rendered, 2), workers=workers,
              bytes=sum(item.size for item in pool.values()))
    campaign = Campaign(config=config, campaign_id=campaign_id, root=root,
                        template_path=template_path, plan=plan, pool=pool, log=log,
                        coordinator_command=tuple(coordinator_command))
    campaign.timings["render_seconds"] = rendered
    return campaign


# ----------------------------------------------------------------------
# One continuous run
# ----------------------------------------------------------------------
@dataclass
class KillRecord:
    """One forced termination (or clean close), with what was observed around it."""

    index: int
    kind: str
    """``forced`` (percent), ``after_commit``, ``syncing_duplicates``, ``operator``,
    ``clean_close``."""
    label: str
    incarnation: int
    trigger: dict[str, Any]
    requested_at: float = 0.0
    confirmed_at: float = 0.0
    exit_code: int | None = None
    orphans: tuple[int, ...] = ()
    lock_left: bool = False
    hot_journal: bool = False
    committed_after: int = 0
    processing_after: int = 0
    pre: dict[str, Any] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)
    restart: dict[str, Any] = field(default_factory=dict)
    offline_files: int = 0
    worker_pids: tuple[int, ...] = ()


@dataclass
class RunEvidence:
    """Everything one run produced, for the evaluator."""

    name: str
    project: Path
    session_id: str
    interrupted: bool
    started_at: float = 0.0
    writers_started_at: float = 0.0
    writers_finished_at: float = 0.0
    caught_up_at: float = 0.0
    finished_at: float = 0.0
    kills: list[KillRecord] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    outages: list[dict[str, Any]] = field(default_factory=list)
    reprocess: dict[str, Any] = field(default_factory=dict)
    endgame: dict[str, Any] = field(default_factory=dict)
    writer_events: list[dict[str, Any]] = field(default_factory=list)
    coordinator_events: list[dict[str, Any]] = field(default_factory=list)
    resource: dict[str, Any] = field(default_factory=dict)
    seal_digests: dict[str, str] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    final_facts: Any = None
    final_integrity: dict[str, Any] = field(default_factory=dict)
    final_snapshot: Any = None
    final_population: Any = None
    original_names: dict[int, str] = field(default_factory=dict)
    vanished_seen: dict[int, dict[str, Any]] = field(default_factory=dict)
    """Ledger rows seen ``vanished`` at any sample - the plan never removes a file."""
    error: str = ""
    plan_mode: str = ""


class ContinuousRun:
    """Drives one continuous run of a campaign."""

    POLL = 0.25
    DEFER_SECONDS = 180.0
    """Longest a planned event waits for the state it needs before it is skipped."""
    KILL_MIN_IN_FLIGHT = 2
    """Sheets the database must show ``processing`` before a percentage kill is sent."""
    KILL_RETRIES = 2
    """Further kills at the same point when one lands after its work committed."""

    def __init__(self, campaign: Campaign, name: str, plan: cohort.CampaignPlan, *,
                 interrupted: bool) -> None:
        self.campaign = campaign
        self.config = campaign.config
        self.plan = plan
        self.name = name
        self.interrupted = interrupted
        self.dir = campaign.root / name
        self.disk = self.dir / "disk"
        self.share = self.dir / "share"
        self.logs = self.dir / "logs"
        self.commands = self.dir / "commands"
        self.inputs = self.dir / "inputs"
        self.exports = self.dir / "exports"
        for folder in (self.disk, self.share, self.logs, self.commands, self.inputs, self.exports):
            folder.mkdir(parents=True, exist_ok=True)
        self.log = EvidenceLog(self.logs / "supervisor.jsonl", campaign_id=campaign.campaign_id,
                               role=f"supervisor:{name}")
        self.coordinator_log = self.logs / "coordinator.jsonl"
        self.tail = LogTail(self.coordinator_log, campaign_id=campaign.campaign_id)
        self.writer_tails = {
            label: LogTail(self.logs / f"writer_{label}.jsonl", campaign_id=campaign.campaign_id)
            for label in self.config.source_labels
        }
        self.incarnation = 0
        self.process: subprocess.Popen[bytes] | None = None
        self.writers: dict[str, subprocess.Popen[bytes]] = {}
        self.seq = 0
        self.events: list[dict[str, Any]] = []
        self.writer_events: list[dict[str, Any]] = []
        self.answers: dict[int, dict[str, Any]] = {}
        self.expected_total = len({item.content for item in plan.main_arrivals})
        self.evidence: RunEvidence | None = None
        self.peak_tree = 0
        self.peak_coordinator = 0
        self.peak_processes = 0
        self.max_in_flight_seen = 0
        self.max_backlog = 0
        self._last_sample = 0.0
        self.down_since: float | None = None
        self._targets_at = -1e9
        self._kill_attempts: dict[str, int] = {}
        self.down_intervals: list[tuple[float, float]] = []
        self.extra_spec: dict[str, Any] = {}
        """Extra coordinator run-spec keys (the SMB qualification's listing timing)."""

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        """The run project's database file."""
        assert self.evidence is not None
        return self.evidence.project / "database.sqlite"

    def pump(self) -> None:
        """Read new log lines; sample resources."""
        for event in self.tail.read_new():
            self.events.append(event)
            if event["event"] == "command_done":
                self.answers[int(event["seq"])] = event
            elif event["event"] == "heartbeat":
                self.max_in_flight_seen = max(self.max_in_flight_seen, int(event["in_flight"]))
                self.max_backlog = max(self.max_backlog, int(event["writer_backlog"]))
        for tail in self.writer_tails.values():
            self.writer_events.extend(tail.read_new())
        for label, proc in self.writers.items():
            code = proc.poll()
            if code not in (None, 0):
                raise CampaignError(f"scanner writer {label} exited with code {code}",
                                    stage="writers", diagnostics={"writer": label, "code": code})
        now = time.monotonic()
        if self.process is not None and now - self._last_sample >= 2.0:
            self._last_sample = now
            total, own, count = tree_memory(
                self.process.pid, launcher=not self.campaign.coordinator_command
            )
            self.peak_tree = max(self.peak_tree, total)
            self.peak_coordinator = max(self.peak_coordinator, own)
            self.peak_processes = max(self.peak_processes, count)

    def progress(self) -> dict[str, int]:
        """Session sheet counts by status, and ledger rows by state - read-only, cheap."""
        assert self.evidence is not None
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        for _ in range(40):
            try:
                connection = sqlite3.connect(uri, uri=True, timeout=10)
                try:
                    rows = connection.execute(
                        "SELECT s.status, COUNT(*) FROM batch_scan s JOIN scan_batch b "
                        "ON b.batch_id = s.batch_id WHERE b.scan_session_id = ? "
                        "GROUP BY s.status", (self.evidence.session_id,),
                    ).fetchall()
                    ledger = connection.execute(
                        "SELECT state, COUNT(*) FROM intake_file WHERE scan_session_id = ? "
                        "GROUP BY state", (self.evidence.session_id,),
                    ).fetchall()
                    running = connection.execute(
                        "SELECT COUNT(*) FROM scan_batch WHERE scan_session_id = ? "
                        "AND status = 'running'", (self.evidence.session_id,),
                    ).fetchone()[0]
                    vanished = connection.execute(
                        "SELECT intake_file_id, source_id, relative_path, state_reason "
                        "FROM intake_file WHERE scan_session_id = ? AND state = 'vanished'",
                        (self.evidence.session_id,),
                    ).fetchall() if any(state == "vanished" for state, _n in ledger) else []
                finally:
                    connection.close()
                for row_id, source_id, relative, reason in vanished:
                    self.evidence.vanished_seen.setdefault(int(row_id), {
                        "source_id": source_id, "relative_path": relative, "reason": reason,
                        "t": time.time(), "incarnation": self.incarnation,
                    })
                counts = {str(k): int(v) for k, v in rows}
                counts.update({f"ledger:{k}": int(v) for k, v in ledger})
                counts["committed"] = sum(
                    counts.get(k, 0) for k in ("completed", "warning", "failed")
                )
                counts["running_units"] = int(running)
                return counts
            except sqlite3.OperationalError:
                time.sleep(0.1)
        raise CampaignError("the database could not be read", stage="progress")

    def fraction(self) -> float:
        """The committed share of the expected main-timeline sheets."""
        return self.progress()["committed"] / max(1, self.expected_total)

    def wait_for(self, predicate: Any, *, what: str, timeout: float | None = None) -> Any:
        """Poll until ``predicate()`` is truthy; fail with diagnostics on timeout."""
        limit = timeout if timeout is not None else self.config.stage_timeout_seconds
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            self.pump()
            result = predicate()
            if result:
                return result
            if self.process is not None and self.process.poll() is not None:
                self.pump()
                result = predicate()
                if result:
                    return result
                raise CampaignError(
                    f"the coordinator exited ({self.process.returncode}) while waiting for {what}",
                    stage=what, diagnostics=self.diagnostics(),
                )
            time.sleep(self.POLL)
        raise CampaignError(f"timed out after {limit:.0f}s waiting for {what}", stage=what,
                            diagnostics=self.diagnostics())

    def diagnostics(self) -> dict[str, Any]:
        """What a failure report shows: recent events, process exits, progress."""
        info: dict[str, Any] = {
            "incarnation": self.incarnation,
            "last_coordinator_events": self.events[-8:],
            "writers": {label: proc.poll() for label, proc in self.writers.items()},
            "coordinator_exit": self.process.poll() if self.process else None,
            "checkpoints": len(self.evidence.checkpoints) if self.evidence else 0,
        }
        with contextlib.suppress(Exception):
            info["progress"] = self.progress()
        return info

    def events_of(self, name: str, incarnation: int | None = None) -> list[dict[str, Any]]:
        """The coordinator events named ``name`` (of one incarnation, if given)."""
        return [
            item for item in self.events
            if item["event"] == name
            and (incarnation is None or item["role"] == f"coordinator:{incarnation}")
        ]

    # ------------------------------------------------------------------
    # The coordinator process
    # ------------------------------------------------------------------
    def launch(self, *, force_lock: bool, await_go: bool) -> None:
        """Start the next coordinator incarnation as a separate process."""
        assert self.evidence is not None
        self.incarnation += 1
        spec = {
            "campaign": self.campaign.campaign_id,
            "project": str(self.evidence.project),
            "session_id": self.evidence.session_id,
            "template_path": str(self.project_template),
            "config": self.campaign.config.to_json(),
            "retime": (
                None if self.plan is self.campaign.plan
                else {"timing_seed": self.plan.config.timing_seed,
                      "duration": self.plan.config.duration_seconds}
            ),
            "plan_digest": self.plan.digest(),
            "pool_index": str(self.campaign.pool_index),
            "coordinator_log": str(self.coordinator_log),
            "commands": str(self.commands),
            "workers": self.config.workers,
            "max_in_flight": self.config.max_in_flight,
            "claim_window": self.config.claim_window,
            "max_commit_group": self.config.max_commit_group,
            "unit_size": self.config.unit_size,
            "trickle_seconds": self.config.trickle_seconds,
            "expected_total": self.expected_total,
            "late_progress": 0.6,
            "operator_interval": 0.5 if self.config.duration_seconds < 600 else 1.5,
            "await_go": await_go,
            **self.extra_spec,
        }
        path = self.dir / f"run_{self.incarnation:03d}.json"
        path.write_text(json.dumps(spec, indent=1), encoding="utf-8")
        packaged = bool(self.campaign.coordinator_command)
        prefix = (
            list(self.campaign.coordinator_command) if packaged
            else [sys.executable, "-m", "omr_scanner.evaluation.intake_qualification.coordinator"]
        )
        args = [*prefix, "--run", str(path), "--incarnation", str(self.incarnation)]
        if force_lock:
            args.append("--force-lock")
        out = (self.logs / f"coordinator_{self.incarnation:03d}.out").open("wb")
        self.process = subprocess.Popen(args, cwd=str(self.dir),
                                        env=packaged_env() if packaged else python_env(),
                                        stdout=out, stderr=subprocess.STDOUT)
        self.log.write("coordinator_launched", incarnation=self.incarnation,
                       launcher_pid=self.process.pid, force_lock=force_lock, await_go=await_go)

    def command(self, name: str, *, timeout: float | None = None, wait: bool = True,
                **args: Any) -> dict[str, Any]:
        """Send one command to the live incarnation and (by default) wait for its answer."""
        self.seq += 1
        seq = self.seq
        body = {"seq": seq, "name": name, "args": args, "incarnation": self.incarnation}
        target = self.commands / f"{self.incarnation:03d}-{seq:06d}-{name}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(body), encoding="utf-8")
        temporary.replace(target)
        self.log.write("command_sent", seq=seq, name=name, incarnation=self.incarnation, args=args)
        if not wait:
            return {"seq": seq}
        answer = self.wait_for(lambda: self.answers.get(seq), what=f"command {name}",
                               timeout=timeout)
        if answer.get("error"):
            raise CampaignError(f"command {name} failed: {answer['error']}", stage=name,
                                diagnostics={"answer": answer})
        return dict(answer)

    def wait_started(self) -> dict[str, Any]:
        """Wait for the live incarnation's ``started`` event and return it."""
        incarnation = self.incarnation
        return dict(self.wait_for(
            lambda: next(iter(self.events_of("started", incarnation)), None),
            what=f"coordinator {incarnation} start",
        ))

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------
    def start_writers(self, arrivals: list[cohort.Arrival], *, tag: str = "") -> dict[str, Any]:
        """Launch one writer process per source with its schedule; start them together."""
        start_file = self.dir / f"start{tag}.txt"
        by_source: dict[str, list[cohort.Arrival]] = defaultdict(list)
        for item in arrivals:
            by_source[item.source].append(item)
        processes = {}
        for label, items in sorted(by_source.items()):
            schedule = {
                "campaign": self.campaign.campaign_id,
                "source": label,
                "target": str(self.disk / f"Scanner_{label}"),
                "log": str(self.logs / f"writer_{label}.jsonl"),
                "start_file": str(start_file),
                "stop_file": str(self.dir / "stop_writers"),
                "arrivals": [
                    {**asdict(item), "pattern": item.pattern.value, "pauses": list(item.pauses),
                     "pool": self.campaign.pool[item.content].path,
                     "sha256": self.campaign.pool[item.content].sha256}
                    for item in sorted(items, key=lambda value: (value.at, value.seq))
                ],
            }
            path = self.dir / f"schedule_{label}{tag}.json"
            path.write_text(json.dumps(schedule), encoding="utf-8")
            out = (self.logs / f"writer_{label}{tag}.out").open("wb")
            processes[label] = subprocess.Popen(
                [sys.executable, "-m", "omr_scanner.evaluation.intake_qualification.writer",
                 str(path)],
                cwd=str(self.dir), env=python_env(), stdout=out, stderr=subprocess.STDOUT,
            )
        origin = time.time() + 1.0
        temporary = start_file.with_suffix(".tmp")
        temporary.write_text(f"{origin:.6f}", encoding="utf-8")
        temporary.replace(start_file)
        self.log.write("writers_started", tag=tag, origin=origin,
                       files={label: len(items) for label, items in by_source.items()},
                       pids={label: proc.pid for label, proc in processes.items()})
        self.writers.update({f"{label}{tag}": proc for label, proc in processes.items()})
        return {"origin": origin, "processes": processes}

    def writers_done(self) -> bool:
        """Whether every writer process has exited."""
        return all(proc.poll() is not None for proc in self.writers.values())

    def completed_writes(self) -> list[dict[str, Any]]:
        """The writers' ``write_completed`` events read so far."""
        return [item for item in self.writer_events if item["event"] == "write_completed"]

    # ------------------------------------------------------------------
    # Durable state, read from outside
    # ------------------------------------------------------------------
    def facts(self) -> Any:
        """The durable facts of the run's session, read read-only from outside."""
        from omr_scanner.evaluation.intake_qualification.inspect import open_read_only, read_facts

        assert self.evidence is not None
        database = open_read_only(self.evidence.project)
        try:
            return read_facts(database, self.evidence.session_id)
        finally:
            database.close()

    def integrity(self) -> dict[str, Any]:
        """The database integrity report (after settling a hot journal), timed."""
        from omr_scanner.evaluation.intake_qualification.inspect import settle_journal
        from omr_scanner.evaluation.qualification import integrity_report

        assert self.evidence is not None
        settle_journal(self.evidence.project)
        began = time.monotonic()
        report = integrity_report(self.db_path)
        report["seconds"] = round(time.monotonic() - began, 3)
        return report

    def services_view(self) -> tuple[Any, Any]:
        """The production read services' view: (session snapshot, effective population)."""
        from omr_scanner.evaluation.intake_qualification.inspect import open_read_only
        from omr_scanner.services import session_population, session_snapshot

        assert self.evidence is not None
        database = open_read_only(self.evidence.project)
        try:
            began = time.monotonic()
            snapshot = session_snapshot.take_snapshot(database, self.evidence.session_id)
            snap_seconds = time.monotonic() - began
            began = time.monotonic()
            population = session_population.session_population(database, self.evidence.session_id)
            pop_seconds = time.monotonic() - began
        finally:
            database.close()
        self._service_seconds = (snap_seconds, pop_seconds)
        return snapshot, population

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------
    def checkpoint(self, label: str) -> dict[str, Any]:
        """Hold the application at a step boundary and evaluate the durable state."""
        from omr_scanner.evaluation.intake_qualification.assertions import evaluate_checkpoint

        assert self.evidence is not None
        answer = self.command("hold", wait=False, timeout=600)
        held = self.wait_for(
            lambda: next((item for item in self.events_of("held", self.incarnation)
                          if int(item.get("seq", -1)) == answer["seq"]), None),
            what=f"hold for checkpoint {label}",
        )
        began = time.monotonic()
        facts = self.facts()
        snapshot, population = self.services_view()
        record = evaluate_checkpoint(
            self.plan, facts, snapshot, population, sha_to_content=self.campaign.sha_to_content(),
            seal_digests=self.evidence.seal_digests,
        )
        record.update({
            "label": label,
            "incarnation": self.incarnation,
            "held_at": held["t"],
            "engine": {
                k: held.get(k) for k in ("registered", "completed", "in_flight", "processing")
            },
            "snapshot_seconds": round(self._service_seconds[0], 3),
            "population_seconds": round(self._service_seconds[1], 3),
            "evaluation_seconds": round(time.monotonic() - began, 3),
        })
        for batch in facts.session_batches():
            if batch.sealed:
                self.evidence.seal_digests.setdefault(batch.batch_id, batch.digest)
        self.command("release", timeout=120)
        self.evidence.checkpoints.append(record)
        self.log.write("checkpoint", label=label, ok=record["ok"],
                       committed=record["committed"], failures=record["failures"][:10])
        return record

    # ------------------------------------------------------------------
    # Kills, closes and restarts
    # ------------------------------------------------------------------
    def _pre_kill(self) -> dict[str, Any]:
        progress = self.progress()
        unread = sum(progress.get(state, 0)
                     for state in ("pending", "cancelled", "queued", "processing"))
        unsettled = sum(progress.get(f"ledger:{state}", 0)
                        for state in ("discovered", "stabilizing", "ready"))
        return {"committed": progress["committed"], "processing": progress.get("processing", 0),
                "remaining": unread + unsettled, "t": time.time()}

    def kill(self, kind: str, label: str, trigger: dict[str, Any]) -> KillRecord:
        """A forced kill of the coordinator (TerminateProcess), then evidence, then restart."""
        from omr_scanner.evaluation.intake_qualification.inspect import settle_journal
        from omr_scanner.evaluation.qualification import kill_run_abruptly

        assert self.evidence is not None and self.process is not None
        record = KillRecord(index=len(self.evidence.kills) + 1, kind=kind, label=label,
                            incarnation=self.incarnation, trigger=trigger)
        record.pre = self._pre_kill()
        record.worker_pids = descendants(self.process.pid)
        record.requested_at = time.time()
        self.log.write("kill_requested", kind=kind, label=label, incarnation=self.incarnation,
                       trigger=trigger, pre=record.pre, worker_pids=list(record.worker_pids))
        evidence = kill_run_abruptly(self.process, record.worker_pids, self.evidence.project,
                                     committed_before_kill=int(record.pre["committed"]))
        record.confirmed_at = time.time()
        self.down_since = record.confirmed_at
        record.exit_code = evidence.exit_code
        record.orphans = evidence.orphan_pids
        record.lock_left = evidence.lock_file_left_behind
        record.hot_journal = settle_journal(self.evidence.project)
        self.log.write("kill_confirmed", kind=kind, label=label, exit_code=record.exit_code,
                       orphans=list(record.orphans), lock_left=record.lock_left,
                       hot_journal=record.hot_journal)
        self._after_down(record)
        self.restart(record, force_lock=True)
        return record

    def clean_close(self, label: str, trigger: dict[str, Any]) -> KillRecord:
        """A clean application close (the window closed), then evidence, then restart."""
        assert self.evidence is not None and self.process is not None
        record = KillRecord(index=len(self.evidence.kills) + 1, kind="clean_close", label=label,
                            incarnation=self.incarnation, trigger=trigger)
        record.pre = self._pre_kill()
        record.worker_pids = descendants(self.process.pid)
        record.requested_at = time.time()
        self.command("close", wait=False)
        try:
            code = self.process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            raise CampaignError("the coordinator did not close cleanly", stage="clean_close",
                                diagnostics=self.diagnostics()) from None
        self.pump()
        closed = self.events_of("closed_cleanly", self.incarnation)
        if not closed or (code != 0 and code not in TEARDOWN_ABORT_CODES):
            raise CampaignError(f"clean close failed (exit {code})", stage="clean_close",
                                diagnostics=self.diagnostics())
        record.confirmed_at = time.time()
        self.down_since = record.confirmed_at
        record.exit_code = code
        time.sleep(1.0)
        import psutil

        record.orphans = tuple(pid for pid in record.worker_pids if psutil.pid_exists(pid))
        from omr_scanner.services.project_lock import LOCK_FILE_NAME

        record.lock_left = (self.evidence.project / LOCK_FILE_NAME).is_file()
        self.log.write("closed", label=label, exit_code=code, orphans=list(record.orphans),
                       lock_left=record.lock_left)
        self._after_down(record)
        self.restart(record, force_lock=False)
        return record

    def _after_down(self, record: KillRecord) -> None:
        """Durable evidence while nothing runs: the facts, the integrity checks."""
        facts = self.facts()
        record.committed_after = len(facts.committed())
        record.processing_after = sum(1 for item in facts.session_sheets()
                                      if item.status == "processing")
        record.integrity = self.integrity()
        from omr_scanner.evaluation.intake_qualification.assertions import summarize_down_state

        record.restart["down"] = {
            key: value for key, value in summarize_down_state(facts).items()
            if not key.startswith("_") and key != "committed"
        }
        self._down_facts = facts

    def restart(self, record: KillRecord, *, force_lock: bool) -> None:
        """Wait for files written while down, restart into the same session, compare."""
        from omr_scanner.evaluation.intake_qualification.assertions import compare_recovery

        before = len(self.completed_writes())
        need = self.config.restart_offline_files
        waited = time.monotonic()
        while (len(self.completed_writes()) - before < need and not self.writers_done()
               and time.monotonic() - waited < 120):
            self.pump()
            time.sleep(self.POLL)
        record.offline_files = len(self.completed_writes()) - before
        self.launch(force_lock=force_lock, await_go=True)
        started = self.wait_started()
        self.down_intervals.append(
            (float(self.down_since or record.confirmed_at), float(started["t"]))
        )
        self.down_since = None
        recovered = self.facts()
        record.restart.update(compare_recovery(self._down_facts, recovered))
        record.restart["started"] = {k: started.get(k) for k in (
            "seconds", "scans_returned", "interrupted_batches", "intent", "intake")}
        record.restart["resolve_view"] = next(
            iter(self.events_of("resolve_view", self.incarnation)), {}
        )
        record.restart["incarnation"] = self.incarnation
        self.command("go", timeout=120)
        self.evidence.kills.append(record)  # type: ignore[union-attr]
        self.log.write("restarted", label=record.label, incarnation=self.incarnation,
                       ok=record.restart.get("ok"),
                       failures=record.restart.get("failures", [])[:10],
                       offline_files=record.offline_files)

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------
    def execute(self) -> RunEvidence:
        """Build the run's project, drive the arrival period and endgame, return the evidence."""
        from omr_scanner.evaluation.intake_qualification.project import create_campaign_project

        began = time.monotonic()
        for label in self.config.source_labels:
            (self.disk / f"Scanner_{label}").mkdir(parents=True, exist_ok=True)
            make_link(self.share / f"Scanner_{label}", self.disk / f"Scanner_{label}")
        facts = create_campaign_project(
            self.dir / "workspace", f"Qualification {self.name}", self.plan,
            self.campaign.template_path, self.inputs,
            source_roots={label: self.share / f"Scanner_{label}"
                          for label in self.config.source_labels},
            stability=self.config.stability,
        )
        self.project_template = facts.template_path
        self.evidence = RunEvidence(name=self.name, project=facts.root,
                                    session_id=facts.scan_session_id,
                                    interrupted=self.interrupted, started_at=time.time())
        self.log.write("project_created", project=str(facts.root), session=facts.scan_session_id,
                       sources=facts.source_ids, sets=facts.set_ids)
        self.launch(force_lock=False, await_go=False)
        self.wait_started()
        writers = self.start_writers(list(self.plan.main_arrivals))
        origin = float(writers["origin"])
        self.evidence.writers_started_at = origin
        self._drive(origin)
        self.evidence.writers_finished_at = max(
            (item["t"] for item in self.writer_events if item["event"] == "writer_finished"),
            default=time.time(),
        )
        self._wait_caught_up()
        self.evidence.caught_up_at = time.time()
        self._endgame()
        self.evidence.finished_at = time.time()
        self.evidence.timings["run_seconds"] = time.monotonic() - began
        self._final()
        return self.evidence

    def _drive(self, origin: float) -> None:
        """The arrival period: planned events triggered by observed state or by the timeline."""
        assert self.evidence is not None
        config = self.config
        duration = self.plan.config.duration_seconds
        pending: list[tuple[str, Any]] = []
        if self.interrupted:
            for percent in config.kill_percents:
                pending.append(("kill", percent))
            if config.after_commit_kill_percent:
                pending.append(("after_commit", config.after_commit_kill_percent))
            if config.duplicate_sync_kill_percent:
                pending.append(("syncing_duplicates", config.duplicate_sync_kill_percent))
            if config.operator_kill_percent:
                pending.append(("operator", config.operator_kill_percent))
            if config.clean_close_percent:
                pending.append(("clean_close", config.clean_close_percent))
            if config.reprocess:
                pending.append(("reprocess", 70))
        for percent in config.checkpoint_percents:
            if percent < 100:
                pending.append(("checkpoint", percent))
        pending.sort(key=lambda item: (item[1], item[0] != "checkpoint"))
        outage = self.interrupted and config.outage_source in config.source_labels
        outage_state = "planned" if outage else "none"
        link = self.share / f"Scanner_{config.outage_source}"
        deferred_since = 0.0
        armed: dict[str, Any] | None = None
        last_committed, last_change = -1, time.monotonic()
        while True:
            self.pump()
            now = time.time()
            if outage_state == "planned" and now >= origin + config.outage_start * duration:
                remove_link(link)
                self.evidence.outages.append({"source": config.outage_source, "start": time.time()})
                self.log.write("outage_started", source=config.outage_source)
                outage_state = "active"
            elif outage_state == "active":
                self._observe_outage()
                if now >= origin + config.outage_end * duration:
                    make_link(link, self.disk / f"Scanner_{config.outage_source}")
                    self.evidence.outages[-1]["end"] = time.time()
                    self.log.write("outage_ended", source=config.outage_source)
                    outage_state = "done"
            progress = self.progress()
            fraction = progress["committed"] / max(1, self.expected_total)
            # Nothing left: no sheet to read *and* no file still settling in the
            # ledger (an instant with an empty queue is not the end - found by
            # the endurance test, whose 99 % kill was dropped 6 s before the
            # last files registered).
            idle = (progress.get("pending", 0) + progress.get("processing", 0) == 0
                    and not any(progress.get(f"ledger:{state}", 0)
                                for state in ("discovered", "stabilizing", "ready")))
            if progress["committed"] != last_committed:
                last_committed, last_change = progress["committed"], time.monotonic()
            if self.writers_done() and time.monotonic() - last_change > self.DEFER_SECONDS:
                idle = True  # a ledger row that never settles must not hold the run forever
            if armed is not None:
                # A pause point is armed in the coordinator: wait for it without
                # blocking the timeline (the outage keeps its schedule), and
                # send nothing else meanwhile - a paused coordinator answers
                # nothing.
                paused = self._paused(armed)
                expired = self.deferral_expired(armed, progress["committed"], time.monotonic())
                if paused is None and (expired or (self.writers_done() and idle)):
                    # Not waited for: a coordinator that paused meanwhile cannot answer.
                    sent = self.command("disarm", wait=False, boundary=armed["boundary"])
                    current = dict(armed)
                    self.wait_for(
                        lambda sent=sent, current=current: (
                            self.answers.get(sent["seq"]) or self._paused(current)
                        ),
                        what="disarm", timeout=120,
                    )
                    paused = self._paused(armed)
                    if paused is None:
                        self.log.write("pause_not_reached", boundary=armed["boundary"],
                                       label=armed["label"])
                        pending.pop(0)
                        armed = None
                if paused is not None and armed is not None:
                    self.kill(armed.get("kill_kind", armed["boundary"]), armed["label"], {
                        **armed["trigger"],
                        "paused": {k: paused.get(k) for k in
                                   ("at", "batch", "scans", "shas", "decisions",
                                    "in_flight_others")},
                    })
                    pending.pop(0)
                    armed = None
            elif pending and self._is_tail_kill(pending[0]) and (
                fraction * 100 >= pending[0][1] - self.TAIL_ARM_EARLY
            ):
                kind, percent = pending[0]
                target = -(-percent * self.expected_total // 100)
                self.command("arm", boundary="in_flight_commit", min_committed=target)
                armed = {"boundary": "in_flight_commit", "kill_kind": "forced",
                         "label": f"kill@{percent}%", "incarnation": self.incarnation,
                         "since": time.monotonic(),
                         "trigger": {"percent": percent, "committed": progress["committed"],
                                     "min_committed": target,
                                     "expected_total": self.expected_total,
                                     "held_by": "in_flight_commit pause point"}}
            elif pending and fraction * 100 >= pending[0][1] and pending[0][0] in self.PAUSES:
                kind, percent = pending[0]
                args: dict[str, Any] = {"count": 2 if kind == "operator" else 1}
                if kind == "syncing_duplicates":
                    args["shas"] = self._duplicate_targets()
                if kind == "syncing_duplicates" and not args["shas"]:
                    deferred_since = deferred_since or time.monotonic()
                    if time.monotonic() - deferred_since > self.DEFER_SECONDS:
                        self.log.write("planned_event_not_reached", kind=kind, percent=percent)
                        pending.pop(0)
                        deferred_since = 0.0
                else:
                    self.command("arm", boundary=kind, **args)
                    armed = {"boundary": kind, "label": f"{kind}@{percent}%",
                             "incarnation": self.incarnation, "since": time.monotonic(),
                             "trigger": {"percent": percent, "committed": progress["committed"],
                                         "expected_total": self.expected_total}}
                    deferred_since = 0.0
            elif pending and fraction * 100 >= pending[0][1]:
                kind, percent = pending[0]
                handled = self._planned(kind, percent, progress)
                if handled == "defer":
                    # Waiting for its state (work in flight, an owed duplicate
                    # pass, a plain unit) - for a bounded time, then recorded
                    # as not reached; the case it serves is then unexercised.
                    deferred_since = deferred_since or time.monotonic()
                    if time.monotonic() - deferred_since > self.DEFER_SECONDS:
                        self.log.write("planned_event_not_reached", kind=kind, percent=percent,
                                       waited=self.DEFER_SECONDS)
                        pending.pop(0)
                        deferred_since = 0.0
                else:
                    pending.pop(0)
                    deferred_since = 0.0
            # Remaining state-triggered events need committed work still to come.
            if (self.writers_done() and outage_state in ("none", "done") and armed is None
                    and (not pending or idle)):
                for kind, percent in pending:
                    self.log.write("planned_event_not_reached", kind=kind, percent=percent)
                break
            time.sleep(self.POLL)

    PAUSES = ("after_commit", "syncing_duplicates", "operator")
    TAIL_PERCENT = 95
    """From here on a percentage kill is held by the coordinator's ``in_flight_commit``
    pause point: near the end of a run the last sheets finish within one poll,
    so an unpaused kill would rarely land on work in flight."""
    TAIL_ARM_EARLY = 3

    def deferral_expired(self, armed: dict[str, Any], committed: int, now: float) -> bool:
        """Whether an armed pause point has waited for its state long enough.

        A tail kill is armed a few percent early; its wait starts only once the
        session reaches its target - before that it is still on its way (found
        by the first full release run: armed at 96 %, the 180 s ran out at 97 %).
        """
        target = int(armed.get("trigger", {}).get("min_committed", 0))
        if armed["boundary"] == "in_flight_commit" and committed < target:
            armed["since"] = now
        return bool(now - armed["since"] > self.DEFER_SECONDS)

    def _is_tail_kill(self, item: tuple[str, Any]) -> bool:
        return item[0] == "kill" and int(item[1]) >= self.TAIL_PERCENT

    def _paused(self, armed: dict[str, Any]) -> dict[str, Any] | None:
        return next((item for item in self.events_of("paused", armed["incarnation"])
                     if item.get("at") == armed["boundary"]), None)

    def _observe_outage(self) -> None:
        assert self.evidence is not None
        outage = self.evidence.outages[-1]
        if outage.get("seen_unreachable"):
            return
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        with contextlib.suppress(sqlite3.Error):
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                row = connection.execute(
                    "SELECT reachability, reachability_changed_at FROM intake_source "
                    "WHERE label = ?", (f"Scanner {outage['source']}",),
                ).fetchone()
            finally:
                connection.close()
            if row and row[0] != "online":
                outage["seen_unreachable"] = time.time()
                outage["reachability"] = row[0]

    def _planned(self, kind: str, percent: int, progress: dict[str, int]) -> str:
        label = f"{kind}@{percent}%"
        trigger = {"percent": percent, "committed": progress["committed"],
                   "processing": progress.get("processing", 0),
                   "expected_total": self.expected_total}
        if kind == "checkpoint":
            self.checkpoint(label)
        elif kind == "kill":
            if progress.get("processing", 0) < self.KILL_MIN_IN_FLIGHT:
                return "defer"  # a kill must land with work in flight
            attempt = self._kill_attempts.get(label, 0) + 1
            self._kill_attempts[label] = attempt
            record = self.kill("forced", label if attempt == 1 else f"{label}#{attempt}", trigger)
            if record.processing_after == 0 and attempt <= self.KILL_RETRIES:
                # The work in flight committed between the observation and the
                # termination: a real kill and restart, but not one that landed
                # on in-flight work. Kill again at this point.
                self.log.write("kill_without_work_in_flight", label=label, attempt=attempt)
                return "defer"
        elif kind == "clean_close":
            if progress.get("processing", 0) < 1 or self._pre_kill()["remaining"] < 2:
                return "defer"  # halfway through Scan: work in flight and more to come
            self.clean_close(label, trigger)
        elif kind == "reprocess":
            return self._reprocess(label)
        return "done"

    def _duplicate_targets(self) -> list[str]:
        """Sheets not yet committed whose Student ID duplicates a committed sheet's.

        Reads every durable fact, so at most every few seconds while it waits.
        """
        now = time.monotonic()
        if now - self._targets_at < 5.0:
            return []
        self._targets_at = now
        facts = self.facts()
        sha_to = self.campaign.sha_to_content()
        committed = {sha_to.get(item.sha) for item in facts.committed()}
        corrected = reference.corrected_contents(self.plan)
        by_identity: dict[str, set[str]] = defaultdict(set)
        for content in self.plan.contents:
            identity = content.bubbled_roll
            if reference.reliable(identity):
                by_identity[identity].add(content.key)
        targets: list[str] = []
        for _identity, keys in by_identity.items():
            if len(keys) < 2 or not (keys & committed):
                continue
            for key in keys - committed:
                if key in corrected and key in committed:
                    continue
                targets.append(self.campaign.pool[key].sha256)
        return targets

    def _reprocess(self, label: str) -> str:
        """*Reprocess All* on one finished unit whose sheets nobody decides anything about."""
        assert self.evidence is not None
        facts = self.facts()
        sha_to = self.campaign.sha_to_content()
        involved = {task.content for task in self.plan.tasks}
        involved |= {task.value for task in self.plan.tasks if task.value in
                     {item.key for item in self.plan.contents}}
        copies = set(reference.duplicate_groups(self.plan))
        identities: dict[str, int] = defaultdict(int)
        for content in self.plan.contents:
            identities[content.bubbled_roll] += 1
        plain = []
        for batch in facts.session_batches():
            if batch.status not in ("completed", "completed_with_errors") or not batch.members:
                continue
            if batch.batch_id in facts.superseded_batches or batch.role == "reprocess":
                continue
            keys = [sha_to.get(facts.sheets[scan].sha) for scan in batch.members]
            if any(key is None for key in keys):
                continue
            if any(key in involved or key in copies or
                   self.plan.content(key).kind is not cohort.ContentKind.SCRIPT or
                   identities[self.plan.content(key).bubbled_roll] > 1
                   for key in keys if key is not None):
                continue
            plain.append(batch)
        if not plain:
            return "defer"
        chosen = min(plain, key=lambda item: (len(item.members), item.batch_id))
        answer = self.command("reprocess", batch_id=chosen.batch_id)
        self.evidence.reprocess = {
            "label": label, "superseded_batch": chosen.batch_id, "sheets": len(chosen.members),
            "reprocess_batch": answer.get("reprocess_batch"), "requested_at": time.time(),
        }
        return "done"

    def _wait_caught_up(self) -> None:
        """Until every written file is registered (or a byte copy) and nothing remains to read."""
        expected_files = len(self.plan.main_arrivals)

        def settled() -> bool:
            progress = self.progress()
            ledger = sum(v for k, v in progress.items() if k.startswith("ledger:")
                         and k not in ("ledger:ignored",))
            unsettled = sum(progress.get(f"ledger:{state}", 0)
                            for state in ("discovered", "stabilizing", "ready"))
            unread = sum(progress.get(state, 0)
                         for state in ("pending", "queued", "processing", "cancelled"))
            return (self.writers_done() and ledger >= expected_files and not unsettled
                    and not unread and progress["running_units"] == 0)

        self.wait_for(settled, what="caught up after the last arrival")
        time.sleep(max(3.0, 4 * (0.5 if self.config.duration_seconds < 600 else 1.5)) + 6.0)
        self.pump()

    # ------------------------------------------------------------------
    # The endgame
    # ------------------------------------------------------------------
    def _endgame(self) -> None:
        from omr_scanner.evaluation.intake_qualification.inspect import open_read_only
        from omr_scanner.services import session_finish

        assert self.evidence is not None
        end = self.evidence.endgame
        self.checkpoint("caught_up@100%")
        self.command("operator_own")
        # 1. Finish while planned items remain: refused, listing every blocker.
        database = open_read_only(self.evidence.project)
        try:
            end["preview_blockers"] = [
                [item.code.value, item.count]
                for item in session_finish.finish_blockers(database, self.evidence.session_id)
            ]
        finally:
            database.close()
        refused = self.command("finish", reason="first attempt")
        end["refused_attempt"] = {k: refused.get(k) for k in ("closed", "blockers")}
        # 2. The operator answers everything left.
        began = time.monotonic()
        final = self.command("operator_final")
        end["operator_final"] = {"decisions": final.get("decisions"),
                                 "unexpected": final.get("unexpected"),
                                 "seconds": round(time.monotonic() - began, 3)}
        # 3. Finish through the one closure policy.
        began = time.monotonic()
        closed = self.command("finish", reason="all decisions made")
        end["first_close"] = {k: closed.get(k) for k in ("closed", "blockers", "sources")}
        end["first_close"]["seconds"] = round(time.monotonic() - began, 3)
        if not closed.get("closed"):
            raise CampaignError("the session did not close", stage="finish",
                                diagnostics={"answer": closed})
        end["first_close_facts"] = self._session_state()
        # 4. Attendance, scoring and reports from the closed session.
        began = time.monotonic()
        first = self.command("downstream", phase="first_close",
                             output_dir=str(self.exports / "first_close"),
                             inputs=str(self.inputs), project_name=f"Qualification {self.name}",
                             timeout=self.config.stage_timeout_seconds)
        end["first_downstream"] = first.get("outcomes")
        end["first_downstream_seconds"] = round(time.monotonic() - began, 3)
        # 5. A late file for the closed session: held, not counted.
        late = list(self.plan.late_arrivals)
        if late:
            self.start_writers(late, tag="_late")
            self.wait_for(lambda: self._ledger_state(late[0]) == "held",
                          what="the late file held for the closed session")
            end["held"] = self._held_view(late[0])
        # 6. Reopen: final outputs become stale.
        reopened = self.command("reopen", reason="a missing script was found")
        end["reopen"] = {k: reopened.get(k) for k in ("state", "reopen_count")}
        end["stale_after_reopen"] = self._export_states()
        # 7. Release the held file; it is read into the reopened session.
        if late:
            released = self.command("release_held",
                                    files=[[item.source, item.name] for item in late])
            end["released"] = released.get("released")
            self.wait_for(lambda: self._ledger_state(late[0]) == "registered"
                          and self._unread() == 0,
                          what="the released file read")
        final2 = self.command("operator_final")
        end["operator_final_reopen"] = {"decisions": final2.get("decisions"),
                                        "unexpected": final2.get("unexpected")}
        reclosed = self.command("finish", reason="re-close after the late script")
        end["second_close"] = {k: reclosed.get(k) for k in ("closed", "blockers")}
        if not reclosed.get("closed"):
            raise CampaignError("the reopened session did not close again", stage="re-close",
                                diagnostics={"answer": reclosed})
        began = time.monotonic()
        second = self.command("downstream", phase="reopen", output_dir=str(self.exports / "reopen"),
                              inputs=str(self.inputs), project_name=f"Qualification {self.name}",
                              timeout=self.config.stage_timeout_seconds)
        end["second_downstream"] = second.get("outcomes")
        end["second_downstream_seconds"] = round(time.monotonic() - began, 3)
        end["final_export_states"] = self._export_states()
        self.command("stop", wait=False)
        assert self.process is not None
        try:
            code = self.process.wait(timeout=300)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise CampaignError("the coordinator did not stop", stage="stop") from None
        self.pump()
        end["stop_exit"] = code
        import psutil

        end["leftover_workers"] = [pid for pid in descendants(self.process.pid)
                                   if psutil.pid_exists(pid)]

    def _unread(self) -> int:
        progress = self.progress()
        return sum(progress.get(state, 0) for state in ("pending", "queued", "processing"))

    def _ledger_state(self, arrival: cohort.Arrival) -> str:
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error:
            return ""
        try:
            row = connection.execute(
                "SELECT f.state FROM intake_file f JOIN intake_source s ON s.source_id = "
                "f.source_id WHERE s.label = ? AND f.file_name = ? AND f.is_current = 1",
                (f"Scanner {arrival.source}", arrival.name),
            ).fetchone()
        except sqlite3.Error:
            return ""
        finally:
            connection.close()
        return str(row[0]) if row else ""

    def _held_view(self, arrival: cohort.Arrival) -> dict[str, Any]:
        snapshot, population = self.services_view()
        facts = self.facts()
        sha = self.campaign.pool[arrival.content].sha256
        in_population = any(facts.sheets[scan].sha == sha for scan in population.effective
                            if scan in facts.sheets)
        return {"state": self._ledger_state(arrival), "in_population": in_population,
                "snapshot_held": snapshot.partition.held,
                "session_state": snapshot.session_state}

    def _session_state(self) -> dict[str, Any]:
        facts = self.facts()
        state = next((s for sid, s, _n in facts.sessions if sid == facts.session_id), "")
        batches = facts.session_batches()
        return {"state": state, "batches": len(batches),
                "unsealed": [b.batch_id for b in batches if not b.sealed]}

    def _export_states(self) -> dict[str, Any]:
        from omr_scanner.evaluation.intake_qualification.inspect import open_read_only
        from omr_scanner.services import session_scope

        assert self.evidence is not None
        database = open_read_only(self.evidence.project)
        try:
            return {
                code: session_scope.final_export_status(
                    database, self.evidence.session_id, code
                ).state
                for code in self.plan.config.sets
            }
        finally:
            database.close()

    def _final(self) -> None:
        assert self.evidence is not None
        self.evidence.final_facts = self.facts()
        self.evidence.final_integrity = self.integrity()
        snapshot, population = self.services_view()
        self.evidence.final_snapshot = snapshot
        self.evidence.final_population = population
        from omr_scanner.evaluation.intake_qualification.inspect import open_read_only
        from omr_scanner.services import session_sheets

        database = open_read_only(self.evidence.project)
        try:
            # The names the Scan list and Resolve show for watched sheets.
            self.evidence.original_names = session_sheets.original_names(
                database, [item.scan_id for item in self.evidence.final_facts.session_sheets()],
                intake_only=True,
            )
        finally:
            database.close()
        self.evidence.writer_events = list(self.writer_events)
        self.evidence.coordinator_events = list(self.events)
        self.evidence.resource = {
            "peak_tree_rss_bytes": self.peak_tree,
            "peak_coordinator_rss_bytes": self.peak_coordinator,
            "peak_processes": self.peak_processes,
            "max_in_flight_seen": self.max_in_flight_seen,
            "max_writer_backlog": self.max_backlog,
            "database_bytes": self.db_path.stat().st_size,
            "down_intervals": self.down_intervals,
        }

    def abort(self) -> None:
        """Stop every owned child process, keep the evidence."""
        (self.dir / "stop_writers").write_text("stop", encoding="utf-8")
        for proc in self.writers.values():
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.kill()
        if self.process is not None and self.process.poll() is None:
            pids = descendants(self.process.pid)
            with contextlib.suppress(Exception):
                self.process.kill()
                self.process.wait(timeout=30)
            import psutil

            for pid in pids:
                with contextlib.suppress(psutil.Error):
                    psutil.Process(pid).kill()
        with contextlib.suppress(Exception):
            link = self.share / f"Scanner_{self.config.outage_source}"
            if not link.exists() and (self.disk / f"Scanner_{self.config.outage_source}").exists():
                make_link(link, self.disk / f"Scanner_{self.config.outage_source}")
        self.log.write("aborted")


def cleanup_links(root: Path) -> None:
    """Remove every junction under ``root`` (so deleting a campaign never follows one)."""
    for share in root.glob("*/share"):
        for link in share.iterdir():
            with contextlib.suppress(OSError):
                remove_link(link, attempts=5)


__all__ = [
    "Campaign",
    "CampaignError",
    "CampaignInterruptedError",
    "ContinuousRun",
    "KillRecord",
    "RunEvidence",
    "cleanup_links",
    "make_link",
    "prepare_campaign",
    "remove_link",
]
