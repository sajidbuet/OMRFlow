r"""Driving one SMB qualification run from the OMRFlow machine (revised phase 10).

The run reuses the Phase 9 supervisor's machinery (:class:`ContinuousRun`):
the coordinator under test is the same separate process (from source, or the
packaged ``OMRFlow.exe``), killed with ``TerminateProcess`` and restarted into
the same project and session; durable state is read read-only from outside.
What differs:

* **the writers are not started here.** They run on the scanner PCs
  (``Invoke-OmrflowScannerWriter.ps1``); their evidence logs are read over
  the network as they grow. A log that cannot be read for a while (the share
  is down) is simply read later.
* **the sources are UNC shares** with the production network stability
  policy, and every listing the intake service makes is timed (the
  coordinator's ``instrument_listing``).
* **the outage is real and made by a person**: at the planned point the
  supervisor prints exactly what to do, then waits until it *observes* the
  share unreachable - from this machine and in the project's own source
  status - and then until it observes it back. Nothing is assumed from the
  wall clock.
* **one restart** - a forced kill of the coordinator while sheets are in a
  worker and the remote writers keep writing - then a restart into the same
  session.

A *rehearsal* (``mode="rehearsal"``) runs the same procedure on this machine
with local folders and local PowerShell writers, the outage simulated by
removing a folder link. It validates the tooling only and can never be
reported as SMB evidence (the evaluator refuses).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from omr_scanner.evaluation.intake_qualification.assertions import _integrity_failures
from omr_scanner.evaluation.intake_qualification.evidence import LogTail
from omr_scanner.evaluation.intake_qualification.supervisor import (
    CampaignError,
    ContinuousRun,
    make_link,
    remove_link,
)
from omr_scanner.evaluation.smb_qualification import topology
from omr_scanner.evaluation.smb_qualification.package import SourceSpec, writer_script

MODE_SMB = "smb"
MODE_REHEARSAL = "rehearsal"


class TolerantLogTail(LogTail):
    """A writer log read over the network: an unreadable moment is retried later."""

    def __init__(self, path: Path, *, campaign_id: str) -> None:
        super().__init__(path, campaign_id=campaign_id)
        self.errors = 0
        self.last_error = ""

    def read_new(self) -> list[dict[str, Any]]:
        """New complete lines, or none while the log cannot be read (retried next time)."""
        try:
            return super().read_new()
        except OSError as exc:
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []


@dataclass
class SmbPlan:
    """When the run's two planned disturbances happen.

    Attributes:
        restart_fraction: The coordinator is killed once this share of the
            planned files has been completely written (and a sheet is in a
            worker), then restarted after ``offline_files`` more were written.
        outage_source: The source whose share is taken away.
        outage_fraction: The person is asked to take it away at this share of
            the planned files completely written.
        outage_min_seconds: The share stays away at least this long.
        offline_files: Files the writers must complete while the coordinator
            is down before it is restarted.
        writer_start_timeout: How long to wait for the remote writers to start.
        observe_timeout: How long to wait for an outage / restoration to be observed.
    """

    restart_fraction: float = 0.35
    outage_source: str = "B"
    outage_fraction: float = 0.60
    outage_min_seconds: float = 120.0
    offline_files: int = 10
    writer_start_timeout: float = 7_200.0
    observe_timeout: float = 3_600.0


@dataclass
class SmbEvidence:
    """What the SMB-specific part of the run observed (beside the Phase 9 evidence)."""

    mode: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    database: dict[str, Any] = field(default_factory=dict)
    machine: dict[str, Any] = field(default_factory=dict)
    smb_connections: dict[str, Any] = field(default_factory=dict)
    clock: dict[str, dict[str, Any]] = field(default_factory=dict)
    writers: dict[str, dict[str, Any]] = field(default_factory=dict)
    outage: dict[str, Any] = field(default_factory=dict)
    restart: dict[str, Any] = field(default_factory=dict)
    reachability_samples: list[dict[str, Any]] = field(default_factory=list)
    log_read_errors: dict[str, int] = field(default_factory=dict)
    coordinator: str = "source"


class SmbRun(ContinuousRun):
    """One SMB qualification run (see the module docstring)."""

    POLL = 1.0

    def __init__(self, campaign: Any, sources: list[SourceSpec], *, mode: str,
                 plan: SmbPlan | None = None, prompt: Any = None) -> None:
        super().__init__(campaign, "smb" if mode == MODE_SMB else "rehearsal", campaign.plan,
                         interrupted=True)
        self.mode = mode
        self.sources = {item.label: item for item in sources}
        self.smb_plan = plan or SmbPlan()
        # ContinuousRun.restart() waits for this many files written while down.
        self.config = replace(self.config, restart_offline_files=self.smb_plan.offline_files)
        self.say = prompt or (lambda text: print(text, flush=True))
        self.writer_tails = {
            label: TolerantLogTail(Path(item.writer_log), campaign_id=campaign.campaign_id)
            for label, item in self.sources.items()
        }
        self.extra_spec = {"instrument_listing": True}
        self.smb = SmbEvidence(mode=mode)
        self.smb.coordinator = "packaged" if campaign.coordinator_command else "source"
        self._last_reach = 0.0

    # ------------------------------------------------------------------
    # Writers: remote, observed through their logs
    # ------------------------------------------------------------------
    def writers_done(self) -> bool:
        """Whether every source's writer logged its end (and any local writer exited)."""
        ended = {item["role"].split(":", 1)[1] for item in self.writer_events
                 if item["event"] in ("writer_finished", "writer_stopped", "writer_error")}
        local = all(proc.poll() is not None for proc in self.writers.values())
        return set(self.sources) <= ended and local

    def writers_started(self) -> set[str]:
        """Labels whose writer has logged ``writer_started``."""
        return {item["role"].split(":", 1)[1] for item in self.writer_events
                if item["event"] == "writer_started"}

    def planned_files(self) -> int:
        """Files the writers are scheduled to write."""
        return len(self.plan.main_arrivals)

    def start_rehearsal_writers(self) -> None:
        """Rehearsal only: run the PowerShell writer here, once per source, on local folders."""
        for label, spec in self.sources.items():
            package = self.campaign.root / "packages" / f"writer_{label}"
            out = (self.logs / f"writer_{label}.out").open("wb")
            self.writers[label] = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(package / writer_script().name), "-Package", str(package),
                 "-Target", spec.writer_target, "-Log", spec.writer_log,
                 "-StopFile", str(self.dir / "stop_writers"), "-StartDelaySeconds", "2"],
                cwd=str(package), stdout=out, stderr=subprocess.STDOUT,
            )

    # ------------------------------------------------------------------
    # Observation of the sources
    # ------------------------------------------------------------------
    def _lists(self, label: str) -> bool:
        """Whether this machine can list the source's root right now."""
        try:
            with os.scandir(self.sources[label].root) as entries:
                next(entries, None)
            return True
        except OSError:
            return False

    def _reachability(self) -> dict[str, str]:
        """The project's own view: ``intake_source.reachability`` per label."""
        assert self.evidence is not None
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        with contextlib.suppress(sqlite3.Error):
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                rows = connection.execute(
                    "SELECT label, reachability FROM intake_source WHERE kind = 'watched'"
                ).fetchall()
            finally:
                connection.close()
            return {str(label).removeprefix("Scanner ").strip(): str(state)
                    for label, state in rows}
        return {}

    def _sample(self) -> None:
        now = time.monotonic()
        if now - self._last_reach < 10.0:
            return
        self._last_reach = now
        self.smb.reachability_samples.append({
            "t": time.time(), "project": self._reachability(),
            "lists": {label: self._lists(label) for label in self.sources},
        })

    def completed(self) -> int:
        """Files the writers have logged completely written."""
        return len(self.completed_writes())

    # ------------------------------------------------------------------
    # The run
    # ------------------------------------------------------------------
    def execute(self) -> Any:
        """Build the project, run to the writers' end with the restart and outage; evidence."""
        from omr_scanner.evaluation.intake_qualification.project import create_campaign_project
        from omr_scanner.evaluation.intake_qualification.supervisor import RunEvidence

        began = time.monotonic()
        identities = topology.local_identities()
        self.smb.machine = topology.machine_description()
        self.smb.sources = [
            {**topology.classify_source(label, spec.root, identities).to_json(),
             "writer_log": spec.writer_log}
            for label, spec in sorted(self.sources.items())
        ]
        if self.mode == MODE_SMB:
            local = [item for item in self.smb.sources if not item["genuine_smb"]]
            if local:
                raise CampaignError(
                    "not genuine SMB: " + "; ".join(f"{i['label']}: {i['reason']}" for i in local)
                    + " - use the rehearsal mode for local folders", stage="topology")
        for label, spec in self.sources.items():
            self.smb.clock[label] = topology.clock_offset(Path(spec.writer_log).parent)
        facts = create_campaign_project(
            self.dir / "workspace", f"SMB qualification {self.name}", self.plan,
            self.campaign.template_path, self.inputs,
            source_roots={label: Path(spec.root) for label, spec in self.sources.items()},
            stability=self.config.stability,
        )
        self.project_template = facts.template_path
        self.smb.database = topology.database_location(facts.root)
        self.evidence = RunEvidence(name=self.name, project=facts.root,
                                    session_id=facts.scan_session_id, interrupted=True,
                                    started_at=time.time())
        self.log.write("project_created", project=str(facts.root), session=facts.scan_session_id,
                       sources=facts.source_ids, database=self.smb.database,
                       topology=self.smb.sources)
        self.launch(force_lock=False, await_go=False)
        self.wait_started()
        self.smb.smb_connections = topology.smb_connections()
        if self.mode == MODE_REHEARSAL:
            self.start_rehearsal_writers()
        else:
            self.say(
                "\n=== START THE WRITERS NOW ===\n"
                "On each scanner PC run the command in its package's RUN-ON-SCANNER-PC.txt\n"
                f"(packages: {self.campaign.root / 'packages'}).\n"
                f"Waiting up to {self.smb_plan.writer_start_timeout:.0f} s for "
                f"{', '.join(sorted(self.sources))} to log writer_started ...\n")
        self.wait_for(lambda: set(self.sources) <= self.writers_started(),
                      what="the scanner writers to start",
                      timeout=self.smb_plan.writer_start_timeout)
        self.evidence.writers_started_at = min(
            float(item["t"]) for item in self.writer_events if item["event"] == "writer_started"
        )
        self.smb.writers = {
            item["role"].split(":", 1)[1]: {k: item.get(k) for k in (
                "host", "user", "os_caption", "os_version", "os_build", "powershell",
                "powershell_edition", "target", "target_is_unc", "files", "writer")}
            for item in self.writer_events if item["event"] == "writer_started"
        }
        self.log.write("writers_started", writers=self.smb.writers)
        self._drive_smb()
        self.evidence.writers_finished_at = max(
            (float(item["t"]) for item in self.writer_events
             if item["event"] == "writer_finished"), default=time.time())
        self._wait_caught_up()
        self.evidence.caught_up_at = time.time()
        self.command("stop", wait=False)
        assert self.process is not None
        try:
            self.evidence.endgame["stop_exit"] = self.process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise CampaignError("the coordinator did not stop", stage="stop") from None
        self.pump()
        self.evidence.finished_at = time.time()
        self.evidence.timings["run_seconds"] = time.monotonic() - began
        self._final()
        self.smb.log_read_errors = {
            label: getattr(tail, "errors", 0) for label, tail in self.writer_tails.items()
        }
        return self.evidence

    def _drive_smb(self) -> None:
        """Until every writer has finished: the restart, then the outage, as observed."""
        planned = self.planned_files()
        state = {"restart": "planned", "outage": "planned"}
        while True:
            self.pump()
            self._sample()
            done = self.completed()
            progress = self.progress()
            if (state["restart"] == "planned" and done >= self.smb_plan.restart_fraction * planned
                    and progress.get("processing", 0) >= 1):
                attempt = len(self.evidence.kills) + 1  # type: ignore[union-attr]
                record = self.kill("forced", "smb-restart" if attempt == 1
                                   else f"smb-restart#{attempt}",
                                   {"completed_writes": done, "planned": planned,
                                    "committed": progress["committed"],
                                    "processing": progress.get("processing", 0)})
                if record.processing_after == 0 and attempt <= self.KILL_RETRIES:
                    # The sheet in the worker committed between the observation
                    # and the termination: a real kill and restart, but not on
                    # work in flight. Kill again (as the Phase 9 supervisor does).
                    self.log.write("kill_without_work_in_flight", label=record.label)
                    continue
                self.smb.restart = {
                    "label": record.label, "attempts": attempt,
                    "completed_writes_at_kill": done, "committed_at_kill": record.committed_after,
                    "in_flight_at_kill": record.processing_after,
                    "offline_files": record.offline_files,
                    "restart_seconds": (record.restart.get("started") or {}).get("seconds"),
                    "recovery_ok": record.restart.get("ok"),
                    "recovery_failures": record.restart.get("failures", [])[:20],
                    "same_session": True, "orphans": list(record.orphans),
                    "integrity_ok": not _integrity_failures(record.integrity, record.label),
                }
                state["restart"] = "done"
            if (state["outage"] == "planned" and state["restart"] == "done"
                    and done >= self.smb_plan.outage_fraction * planned):
                self._outage()
                state["outage"] = "done"
            if self.writers_done():
                if state["restart"] != "done" or state["outage"] != "done":
                    raise CampaignError(
                        f"the writers finished before the planned disturbances ({state})",
                        stage="drive")
                return
            time.sleep(self.POLL)

    def _wait_observed(self, predicate: Any, what: str) -> float:
        deadline = time.monotonic() + self.smb_plan.observe_timeout
        while time.monotonic() < deadline:
            self.pump()
            self._sample()
            if predicate():
                return time.time()
            time.sleep(self.POLL)
        raise CampaignError(f"timed out waiting to observe {what}", stage="outage",
                            diagnostics=self.diagnostics())

    def _outage(self) -> None:
        """Take one source's share away (a person, or a link in a rehearsal) and bring it back."""
        label = self.smb_plan.outage_source
        spec = self.sources[label]
        record: dict[str, Any] = {"source": label, "root": spec.root, "mode": self.mode}
        self.evidence.outages.append(record)  # type: ignore[union-attr]
        record["requested_at"] = time.time()
        record["completed_writes_at_request"] = self.completed()
        if self.mode == MODE_REHEARSAL:
            remove_link(Path(spec.root))
            record["mechanism"] = "rehearsal: the local folder link was removed (NOT SMB)"
        else:
            self.say(
                f"\n=== CHECKPOINT: TAKE SHARE {spec.root} AWAY NOW ===\n"
                f"Make scanner {label}'s share unreachable from this machine while its writer\n"
                "keeps writing - disable the scanner PC's network adapter, unplug its cable,\n"
                "or stop its Server service (Stop-Service LanmanServer -Force) - and leave it\n"
                f"away for at least {self.smb_plan.outage_min_seconds:.0f} s. Do NOT stop the\n"
                "writer. Waiting to observe the share unreachable ...\n")
            (self.dir / "CHECKPOINT.txt").write_text(
                f"TAKE {spec.root} AWAY NOW ({time.ctime()})\n", encoding="utf-8")
        record["unreachable_from_here_at"] = self._wait_observed(
            lambda: not self._lists(label), f"{spec.root} unreachable from this machine")
        record["start"] = record["unreachable_from_here_at"]
        record["seen_unreachable"] = self._wait_observed(
            lambda: self._reachability().get(label, "online") != "online",
            f"the project marking Scanner {label} unreachable")
        record["reachability"] = self._reachability().get(label)
        record["other_sources_listing"] = {
            other: self._lists(other) for other in self.sources if other != label
        }
        settle = record["start"] + self.smb_plan.outage_min_seconds
        while time.time() < settle:
            self.pump()
            self._sample()
            time.sleep(self.POLL)
        record["completed_writes_before_restore"] = self.completed()
        record["restore_requested_at"] = time.time()
        if self.mode == MODE_REHEARSAL:
            target = Path(spec.writer_target)
            make_link(Path(spec.root), target)
        else:
            self.say(f"\n=== CHECKPOINT: BRING SHARE {spec.root} BACK NOW ===\n"
                     "Reconnect it. Waiting to observe it reachable again ...\n")
            (self.dir / "CHECKPOINT.txt").write_text(
                f"BRING {spec.root} BACK NOW ({time.ctime()})\n", encoding="utf-8")
        record["reachable_from_here_at"] = self._wait_observed(
            lambda: self._lists(label), f"{spec.root} reachable again")
        record["end"] = record["reachable_from_here_at"]
        record["project_online_at"] = self._wait_observed(
            lambda: self._reachability().get(label) == "online",
            f"the project marking Scanner {label} online again")
        with contextlib.suppress(OSError):
            (self.dir / "CHECKPOINT.txt").unlink()
        self.log.write("outage", **dict(record))


def rehearsal_sources(root: Path, labels: list[str]) -> list[SourceSpec]:
    """Local stand-ins for a rehearsal: per source a disk folder and a link OMRFlow watches."""
    specs = []
    for label in labels:
        disk = root / "rehearsal" / "disk" / f"Scanner_{label}"
        share = root / "rehearsal" / "share" / f"Scanner_{label}"
        logs = root / "rehearsal" / "logs"
        disk.mkdir(parents=True, exist_ok=True)
        share.parent.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        if not share.exists():
            make_link(share, disk)
        specs.append(SourceSpec(label=label, root=str(share),
                                writer_log=str(logs / f"writer_{label}.jsonl"),
                                writer_target=str(disk),
                                writer_log_local=str(logs / f"writer_{label}.jsonl")))
    return specs


def save_evidence(path: Path, smb: SmbEvidence) -> None:
    """Write the SMB-specific evidence beside the run's logs."""
    from dataclasses import asdict

    path.write_text(json.dumps(asdict(smb), indent=1, default=str), encoding="utf-8")


__all__ = [
    "MODE_REHEARSAL",
    "MODE_SMB",
    "SmbEvidence",
    "SmbPlan",
    "SmbRun",
    "TolerantLogTail",
    "rehearsal_sources",
    "save_evidence",
]
