"""Tests for the revised phase 10 SMB qualification tooling.

What is tested here is the *tooling*: deciding from facts whether a source is
genuine SMB, the clock correction, the measurements, the verdict rules (a
rehearsal can never pass as SMB), the writer packages, and the PowerShell
scanner writer itself on local folders. None of this is SMB evidence.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omr_scanner.evaluation.intake_qualification.assertions import (
    FAIL,
    NOT_EXERCISED,
    PASS,
    CheckResult,
)
from omr_scanner.evaluation.smb_qualification import evaluate, package, topology
from omr_scanner.tools import smb_qualification as cli

REPOSITORY = Path(__file__).resolve().parents[2]
WRITER = REPOSITORY / "scripts" / "smb" / "Invoke-OmrflowScannerWriter.ps1"


class TestUncPaths:
    @pytest.mark.parametrize(("path", "host"), [
        (r"\\scanner-pc-a\scans\omr", "scanner-pc-a"),
        (r"\\scanner-pc-a\scans", "scanner-pc-a"),
        ("//scanner-pc-a/scans/omr", "scanner-pc-a"),
        (r"\\?\UNC\scanner-pc-b\scans\omr", "scanner-pc-b"),
        (r"\\10.0.0.7\scans", "10.0.0.7"),
    ])
    def test_the_host_of_a_unc_path(self, path, host):
        assert topology.unc_host(path) == host

    @pytest.mark.parametrize("path", [
        r"C:\Scans\omr", r"Z:\omr", r"\\scanner-pc-a", r"\\?\C:\Scans", r"\\.\pipe\x", "relative",
    ])
    def test_not_a_unc_share(self, path):
        assert topology.unc_host(path) is None


class TestGenuineSmb:
    def test_a_local_folder_is_never_smb(self, tmp_path):
        judged = topology.classify_source("A", str(tmp_path))
        assert not judged.genuine_smb and "not a UNC path" in judged.reason

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.2", "::1"])
    def test_a_loopback_share_is_never_smb(self, host):
        assert not topology.classify_source("A", rf"\\{host}\c$\scans").genuine_smb

    def test_a_share_on_this_machine_is_never_smb(self):
        name = socket.gethostname()
        for host in (name, name.upper(), name.lower()):
            judged = topology.classify_source("A", rf"\\{host}\scans")
            assert not judged.genuine_smb, host
            assert "this machine" in judged.reason

    def test_a_share_on_another_host_is_genuine(self):
        judged = topology.classify_source(
            "B", r"\\scanner-pc-b.invalid\scans\omr", frozenset({"omrflow-pc"}),
        )
        assert judged.genuine_smb and judged.host == "scanner-pc-b.invalid"

    def test_the_database_folder_here_is_a_local_fixed_disk(self, tmp_path):
        location = topology.database_location(tmp_path)
        assert location["drive_type"] == "fixed" and location["local"] is True
        assert location["path"].endswith("database.sqlite")

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="UNC paths exist only on Windows; on POSIX Path(r'\\\\host\\share') is a "
        "relative file name, and the SMB qualification runs on Windows",
    )
    def test_a_unc_database_is_not_local(self):
        assert topology.drive_type(Path(r"\\scanner-pc-b.invalid\scans")) == "remote"

    def test_the_clock_probe_on_a_local_folder_finds_no_offset(self, tmp_path):
        clock = topology.clock_offset(tmp_path, samples=3)
        assert clock["ok"] and abs(clock["offset_seconds"]) < 2.0
        assert not list(tmp_path.iterdir()), "probes are removed"


def smb_evidence(mode: str = "smb", **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "mode": mode,
        "machine": {"host": "omrflow-pc"},
        "sources": [
            {"label": "A", "root": r"\\pc-a\scans\omr", "host": "pc-a", "genuine_smb": True,
             "reason": "UNC share on pc-a"},
            {"label": "B", "root": r"\\pc-b\scans\omr", "host": "pc-b", "genuine_smb": True,
             "reason": "UNC share on pc-b"},
        ],
        "writers": {"A": {"host": "PC-A"}, "B": {"host": "PC-B"}},
        "database": {"path": "C:/p/database.sqlite", "drive_type": "fixed", "local": True},
        "clock": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestTopologyAssertion:
    def test_two_remote_shares_and_remote_writers_pass(self):
        assert evaluate.a_topology(smb_evidence()).status == PASS

    def test_a_local_source_fails(self):
        smb = smb_evidence()
        smb.sources[1] = {**smb.sources[1], "genuine_smb": False, "reason": "a junction"}
        assert evaluate.a_topology(smb).status == FAIL

    def test_a_writer_on_the_omrflow_machine_fails(self):
        smb = smb_evidence(writers={"A": {"host": "PC-A"}, "B": {"host": "OMRFLOW-PC"}})
        result = evaluate.a_topology(smb)
        assert result.status == FAIL
        assert any("ran on the OMRFlow machine" in item for item in result.failures)

    def test_a_writer_reporting_this_machines_netbios_name_fails(self):
        """Found by the first rehearsal: the writer logs %COMPUTERNAME% (15 characters)."""
        smb = smb_evidence(
            machine={"host": "Sajid-Asus-Laptop",
                     "identities": ["sajid-asus-laptop", "sajid-asus-lapt", "127.0.0.1"]},
            writers={"A": {"host": "PC-A"}, "B": {"host": "SAJID-ASUS-LAPT"}},
        )
        result = evaluate.a_topology(smb)
        assert result.status == FAIL
        assert any("writer B ran on the OMRFlow machine" in item for item in result.failures)

    def test_this_machines_identities_include_its_netbios_name(self):
        import os

        description = topology.machine_description()
        if os.environ.get("COMPUTERNAME"):
            assert os.environ["COMPUTERNAME"].lower() in description["identities"]

    def test_one_source_is_not_enough(self):
        smb = smb_evidence()
        smb.sources = smb.sources[:1]
        assert evaluate.a_topology(smb).status == FAIL

    def test_a_rehearsal_is_never_exercised(self):
        assert evaluate.a_topology(smb_evidence(mode="rehearsal")).status == NOT_EXERCISED

    def test_a_network_database_fails(self):
        smb = smb_evidence(database={"path": r"\\nas\p\database.sqlite", "drive_type": "remote",
                                     "local": False})
        assert evaluate.a_database(smb).status == FAIL


class TestVerdict:
    def results(self, *statuses: str) -> list[CheckResult]:
        return [CheckResult(f"x{index}", status=status) for index, status in enumerate(statuses)]

    def test_all_passing_genuine_smb_is_pass(self):
        assert evaluate.verdict(self.results(PASS, PASS), smb_evidence()) == evaluate.VERDICT_PASS

    def test_any_failure_fails(self):
        assert evaluate.verdict(self.results(PASS, FAIL), smb_evidence()) == evaluate.VERDICT_FAIL

    def test_a_rehearsal_is_never_pass(self):
        verdict = evaluate.verdict(self.results(PASS, PASS), smb_evidence(mode="rehearsal"))
        assert verdict == evaluate.VERDICT_NOT_SMB

    def test_anything_unexercised_is_not_the_qualification(self):
        verdict = evaluate.verdict(self.results(PASS, NOT_EXERCISED), smb_evidence())
        assert verdict == evaluate.VERDICT_NOT_SMB

    def test_every_assertion_is_named_once(self):
        assert len(set(evaluate.SMB_ASSERTIONS)) == len(evaluate.SMB_ASSERTIONS) == 15


class TestClockAndMeasurements:
    def test_writer_times_move_onto_this_clock(self):
        events = [{"event": "write_completed", "role": "writer:A", "t": 100.0,
                   "write_started_at": 90.0, "write_completed_at": 99.0, "created_at": 90.0},
                  {"event": "write_completed", "role": "writer:B", "t": 100.0,
                   "write_completed_at": 99.0}]
        moved = evaluate.to_local_clock(events, {"A": {"offset_seconds": 2.5}})
        assert moved[0]["write_completed_at"] == 96.5 and moved[0]["t"] == 97.5
        assert moved[1]["write_completed_at"] == 99.0  # no offset measured for B
        assert events[0]["t"] == 100.0, "the evidence itself is not modified"

    def test_distribution(self):
        values = [float(v) for v in range(1, 101)]
        found = evaluate.distribution(values)
        assert found["count"] == 100 and found["min"] == 1.0 and found["max"] == 100.0
        assert found["median"] == 50.5 and found["p95"] == 95.0
        assert evaluate.distribution([]) == {"count": 0}


class TestPackages:
    def test_a_package_holds_only_its_source(self, tmp_path):
        arrival = SimpleNamespace
        pattern = SimpleNamespace(value="stepped")
        plan = SimpleNamespace(
            main_arrivals=(
                arrival(seq=1, source="A", name="s1.png", content="c1", at=2.0, pattern=pattern,
                        pauses=(0.5,), hold_after=0.0),
                arrival(seq=2, source="B", name="s1.png", content="c2", at=1.0, pattern=pattern,
                        pauses=(), hold_after=0.0),
                arrival(seq=3, source="A", name="s0.png", content="c2", at=1.0, pattern=pattern,
                        pauses=(), hold_after=0.0),
            ),
            digest=lambda: "d" * 64,
        )
        pool = {}
        for key in ("c1", "c2"):
            path = tmp_path / "pool" / f"{key}.png"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(key.encode() * 10)
            pool[key] = SimpleNamespace(path=str(path), size=20,
                                        sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        spec = package.SourceSpec("A", r"\\pc-a\scans\omr", r"\\pc-a\scans\logs\writer_A.jsonl",
                                  r"D:\Scans\omr", r"D:\Scans\logs\writer_A.jsonl")
        summary = package.write_package(tmp_path / "writer_A", "camp", spec, plan, pool)
        schedule = json.loads((tmp_path / "writer_A" / "schedule.json").read_text("utf-8"))
        assert [item["seq"] for item in schedule["arrivals"]] == [3, 1]  # by time
        assert {item["content"] for item in schedule["arrivals"]} == {"c1", "c2"}
        assert summary["files"] == 2 and summary["images"] == 2
        assert (tmp_path / "writer_A" / package.WRITER_SCRIPT_NAME).is_file()
        run_text = (tmp_path / "writer_A" / "RUN-ON-SCANNER-PC.txt").read_text("utf-8")
        assert '-Target "D:\\Scans\\omr"' in run_text and r"\\pc-a\scans\omr" in run_text

    def test_the_network_policy_is_the_production_default(self):
        from omr_scanner.domain.intake import NETWORK_POLICY

        settings = package.network_stability()
        assert settings.quiet_seconds == NETWORK_POLICY.quiet_seconds
        assert settings.poll_interval_seconds == NETWORK_POLICY.poll_interval_seconds

    def test_the_plan_gives_every_source_its_minimum(self):
        """The planner splits arrivals unevenly; the sized plan still clears 1,000 each."""
        from omr_scanner.evaluation.intake_qualification.cohort import plan_campaign
        from omr_scanner.services.template_service import load_template

        template = load_template(REPOSITORY / "examples" / "templates"
                                 / "synthetic_answer_sheet.omrt")
        config = package.sized_config(template, sources=2, files_per_source=1_000,
                                      seed=20261007, timing_seed=7102026,
                                      duration_seconds=1_800.0, workers=6)
        counts = package.per_source_counts(plan_campaign(config, template))
        assert set(counts) == {"A", "B"} and min(counts.values()) >= 1_000
        assert config.stability == package.network_stability()


class TestCommandLine:
    def test_a_label_without_a_path_is_refused(self):
        with pytest.raises(ValueError, match="LABEL=PATH"):
            cli._pairs(["A"], "--source")

    def test_every_source_needs_its_log(self, tmp_path):
        code = cli.main(["prepare", "--output", str(tmp_path), "--source", r"A=\\pc-a\s",
                         "--source", r"B=\\pc-b\s", "--log", r"A=\\pc-a\l\w.jsonl"])
        assert code == 2


def _schedule(tmp_path: Path, patterns: list[str]) -> tuple[Path, dict[str, bytes]]:
    package_dir = tmp_path / "package"
    (package_dir / "pool").mkdir(parents=True)
    contents: dict[str, bytes] = {}
    arrivals = []
    for index, pattern in enumerate(patterns):
        data = bytes(range(256)) * (8 + index)
        key = f"c{index}"
        (package_dir / "pool" / f"{key}.png").write_bytes(data)
        contents[f"sheet_{index}.png"] = data
        arrivals.append({
            "seq": index + 1, "name": f"sheet_{index}.png", "content": key, "at": 0.05 * index,
            "pattern": pattern, "pauses": [0.2, 0.2] if pattern != "atomic" else [],
            "hold_after": 0.3 if pattern == "held_open" else 0.0, "pool": f"pool/{key}.png",
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
        })
    (package_dir / "schedule.json").write_text(
        json.dumps({"campaign": "camp-1", "source": "A", "arrivals": arrivals}), "utf-8")
    return package_dir, contents


@pytest.mark.skipif(sys.platform != "win32", reason="the scanner writer is a Windows script")
class TestPowerShellWriter:
    """The writer the scanner PCs run - exercised here on local folders (not SMB)."""

    PATTERNS = ("atomic", "stepped", "long_pause", "header_first", "held_open", "rename")

    def run_writer(self, package_dir: Path, target: Path, log: Path, *extra: str
                   ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WRITER),
             "-Package", str(package_dir), "-Target", str(target), "-Log", str(log),
             "-StartDelaySeconds", "0.2", *extra],
            capture_output=True, text=True, timeout=180, check=False,
        )

    def test_every_pattern_writes_the_exact_bytes_and_logs_them(self, tmp_path):
        package_dir, contents = _schedule(tmp_path, list(self.PATTERNS))
        target, log = tmp_path / "scans", tmp_path / "logs" / "writer_A.jsonl"
        done = self.run_writer(package_dir, target, log)
        assert done.returncode == 0, done.stdout + done.stderr
        for name, data in contents.items():
            assert (target / name).read_bytes() == data, name
        assert not list(target.glob("*.part")), "the rename pattern leaves no temporary"
        events = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
        assert {item["campaign"] for item in events} == {"camp-1"}
        assert {item["role"] for item in events} == {"writer:A"}
        assert events[0]["event"] == "writer_started" and events[0]["host"]
        assert events[-1]["event"] == "writer_finished" and events[-1]["written"] == 6
        completed = {item["name"]: item for item in events if item["event"] == "write_completed"}
        started = {item["name"]: item for item in events if item["event"] == "write_started"}
        for name, data in contents.items():
            item = completed[name]
            assert item["sha256"] == hashlib.sha256(data).hexdigest()
            assert item["size"] == len(data)
            assert item["write_completed_at"] >= item["write_started_at"]
            assert started[name]["t"] <= item["write_started_at"]
        # The steps really took their pauses (two pauses of 0.2 s).
        stepped = completed["sheet_1.png"]
        assert stepped["write_completed_at"] - stepped["write_started_at"] >= 0.35

    def test_the_phase9_reader_reads_its_log(self, tmp_path):
        from omr_scanner.evaluation.intake_qualification.evidence import LogTail

        package_dir, _contents = _schedule(tmp_path, ["atomic", "stepped"])
        log = tmp_path / "logs" / "writer_A.jsonl"
        assert self.run_writer(package_dir, tmp_path / "scans", log).returncode == 0
        events = LogTail(log, campaign_id="camp-1").read_new()
        assert [item["event"] for item in events].count("write_completed") == 2

    def test_a_log_inside_the_scanner_folder_is_refused(self, tmp_path):
        package_dir, _contents = _schedule(tmp_path, ["atomic"])
        target = tmp_path / "scans"
        done = self.run_writer(package_dir, target, target / "writer_A.jsonl")
        assert done.returncode != 0
        assert not (target / "sheet_0.png").exists()

    def test_changed_pool_bytes_stop_the_writer(self, tmp_path):
        package_dir, _contents = _schedule(tmp_path, ["atomic", "atomic"])
        (package_dir / "pool" / "c1.png").write_bytes(b"tampered")
        log = tmp_path / "logs" / "w.jsonl"
        done = self.run_writer(package_dir, tmp_path / "scans", log)
        assert done.returncode == 2
        events = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
        assert events[-1]["event"] == "writer_error"

    def test_a_stop_file_stops_it(self, tmp_path):
        package_dir, _contents = _schedule(tmp_path, ["atomic", "atomic"])
        stop = tmp_path / "stop"
        stop.write_text("stop", "utf-8")
        log = tmp_path / "logs" / "w.jsonl"
        done = self.run_writer(package_dir, tmp_path / "scans", log, "-StopFile", str(stop))
        assert done.returncode == 3
        events = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
        assert events[-1]["event"] == "writer_stopped"
