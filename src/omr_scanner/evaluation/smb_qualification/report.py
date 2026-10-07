"""The SMB qualification report: ``report.json`` (machine-readable) and ``report.md``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omr_scanner.evaluation.smb_qualification.evaluate import VERDICT_NOT_SMB, VERDICT_PASS

NOT_PROVEN = (
    "real scanner hardware or real paper (the images are synthetic, written by a script)",
    "power-loss durability (the restart is a process termination)",
    "the installed build's GUI on a share (the coordinator is headless)",
    "other SMB servers, NAS devices, VPNs or Wi-Fi links than the ones described here",
    "real-paper calibration of the quality policy (the unvalidated default)",
    "production readiness",
)


def _distribution(value: dict[str, Any] | None) -> str:
    if not value or not value.get("count"):
        return "no samples"
    return (f"n {value['count']}, min {value['min']}, median {value['median']}, "
            f"p95 {value['p95']}, max {value['max']}")


def render_markdown(data: dict[str, Any]) -> str:
    """The human-readable report, from the machine-readable payload only."""
    lines: list[str] = []
    add = lines.append
    verdict = data["verdict"]
    add(f"# SMB qualification - {data['campaign_id']}")
    add("")
    add(f"**Verdict: `{verdict}`** (mode `{data['mode']}`)")
    add("")
    if verdict == VERDICT_NOT_SMB:
        add("> This run is **not** SMB qualification evidence: it was a rehearsal on local folders "
            "or below the required scale. It shows only that the tooling works.")
        add("")
    elif verdict != VERDICT_PASS:
        add("> At least one assertion failed. See below; nothing here is qualification evidence.")
        add("")
    smb = data["smb"]
    add("## Machines and topology")
    add("")
    machine = smb.get("machine", {})
    add(f"- OMRFlow machine: `{machine.get('host')}` - {machine.get('platform')}, "
        f"{machine.get('cpu_logical')} logical CPUs")
    for label, writer in sorted(smb.get("writers", {}).items()):
        add(f"- Scanner {label} writer: host `{writer.get('host')}` - {writer.get('os_caption')} "
            f"{writer.get('os_version')} (build {writer.get('os_build')}), PowerShell "
            f"{writer.get('powershell')} {writer.get('powershell_edition')}; target "
            f"`{writer.get('target')}`")
    for source in smb.get("sources", []):
        add(f"- Source {source['label']}: `{source['root']}` - "
            f"{'genuine SMB' if source['genuine_smb'] else 'NOT SMB'} ({source['reason']})")
    database = smb.get("database", {})
    add(f"- Project database: `{database.get('path')}` - drive type {database.get('drive_type')}"
        f" ({'local' if database.get('local') else 'NOT LOCAL'})")
    connections = smb.get("smb_connections", {})
    if connections.get("available"):
        for item in connections.get("connections", []):
            add(f"- SMB connection: `\\\\{item.get('ServerName')}\\{item.get('ShareName')}` "
                f"dialect {item.get('Dialect')}, signed {item.get('Signed')}, "
                f"encrypted {item.get('Encrypted')}")
    else:
        add(f"- SMB dialect: not discoverable here ({connections.get('reason', 'unknown')})")
    add(f"- Coordinator: {smb.get('coordinator')}")
    add("")
    add("## Assertions")
    add("")
    add("| Assertion | Status | Checked | Failures |")
    add("|---|---|---:|---|")
    for item in data["assertions"]:
        failures = "; ".join(item["failures"][:3]).replace("|", "/")
        add(f"| `{item['name']}` | {item['status'].upper()} | {item['checked']} | {failures} |")
    add("")
    measured = data["measurements"]
    add("## Measurements")
    add("")
    add("Listing cost per reconciliation (seconds):")
    add("")
    for label, value in measured.get("listing_seconds_per_reconciliation", {}).items():
        add(f"- {label}: {_distribution(value)}; files listed "
            f"{_distribution(value.get('files_listed'))}; listing errors {value.get('errors')}")
    add("")
    add("Stabilisation latency, writer completion to ready (seconds, on this machine's clock):")
    add("")
    for label, value in measured.get("stabilisation_seconds_completion_to_ready", {}).items():
        if isinstance(value, dict):
            add(f"- {label}: {_distribution(value)}")
    add("")
    for outage in measured.get("outages", []):
        add(f"- Outage of Scanner {outage.get('source')}: "
            f"{outage.get('mechanism') or 'by a person'};"
            f" project marked it unreachable {outage.get('detected_after_seconds')} s after it was "
            f"gone; online again {outage.get('online_after_seconds')} s after it returned")
    restart = measured.get("restart") or {}
    if restart:
        add(f"- Restart: killed with {restart.get('in_flight_at_kill')} sheet(s) in flight and "
            f"{restart.get('committed_at_kill')} committed; {restart.get('offline_files')} file(s) "
            f"written while down; restart sequence {restart.get('restart_seconds')} s; recovery "
            f"{'ok' if restart.get('recovery_ok') else 'FAILED'}")
    for label, clock in sorted(measured.get("clock_offsets", {}).items()):
        add(f"- Clock offset, Scanner {label} host minus this machine: "
            f"{clock.get('offset_seconds')} s +/- {clock.get('uncertainty_seconds')} s")
    add("")
    add("## What this does not prove")
    add("")
    for item in data.get("not_proven", NOT_PROVEN):
        add(f"- {item}")
    add("")
    return "\n".join(lines) + "\n"


def write_reports(folder: Path, data: dict[str, Any]) -> tuple[Path, Path]:
    """Write ``report.json`` and ``report.md`` into ``folder``; return both paths."""
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / "report.json"
    md_path = folder / "report.md"
    json_path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(data), encoding="utf-8")
    return json_path, md_path


__all__ = ["NOT_PROVEN", "render_markdown", "write_reports"]
