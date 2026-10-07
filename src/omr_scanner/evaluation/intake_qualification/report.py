"""``report.json`` and ``report.md`` for an intake qualification campaign (revised phase 9).

The JSON is the machine-readable result - the verdict in it is the one
:func:`~.assertions.verdict` computed, never typed by hand. The Markdown is
generated from the same payload, with every required assertion in its table
(``not_evaluated`` rows included), the crash matrix, the endurance cases, the
kill and restart sequence, the workload counters, the environment and what
the campaign does **not** prove.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omr_scanner.evaluation.intake_qualification.assertions import _plain

REPORT_JSON = "report.json"
REPORT_MD = "report.md"


def write_reports(root: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    """Write both reports into ``root``; return their paths."""
    data = _plain(payload)
    json_path = root / REPORT_JSON
    json_path.write_text(json.dumps(data, indent=1, sort_keys=False, default=str),
                         encoding="utf-8")
    md_path = root / REPORT_MD
    md_path.write_text(render_markdown(data), encoding="utf-8")
    return json_path, md_path


def _cell(value: Any) -> str:
    text = str(value).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= 160 else text[:157] + "..."


def _evidence(item: dict[str, Any]) -> str:
    if item.get("failures"):
        return "; ".join(item["failures"][:3])
    evidence = {k: v for k, v in item.get("evidence", {}).items()
                if not isinstance(v, dict | list)}
    parts = [f"checked {item.get('checked', 0):,} (min {item.get('minimum', 0):,})"]
    parts += [f"{k} {v}" for k, v in list(evidence.items())[:5]]
    return ", ".join(parts)


def render_markdown(data: dict[str, Any]) -> str:
    """The human-readable report."""
    lines: list[str] = []
    add = lines.append
    env = data.get("environment", {})
    add(f"# Intake qualification - {data.get('campaign_id', '?')}")
    add("")
    add(f"**Verdict: {data.get('verdict', '?')}**")
    add("")
    add(f"Mode `{data.get('mode')}` · commit `{env.get('commit', '?')}` "
        f"(working tree clean: {env.get('working_tree_clean')}) · schema "
        f"{env.get('schema_version')} · OMRFlow {env.get('application_version')}")
    add("")
    scale = data.get("scale", {})
    if not scale.get("release_scale"):
        add("Not the release qualification because: " + "; ".join(scale.get("reasons", ["?"])))
        add("")
    if data.get("error"):
        error = data["error"]
        add(f"**The campaign stopped**: stage `{error.get('stage')}` - {error.get('message')}")
        add("")
    add("## What this proves")
    add("")
    add(data.get("proves", ""))
    add("")
    add("It does **not** prove: " + "; ".join(data.get("not_proven", [])) + ".")
    add("")
    add("## Required assertions")
    add("")
    add("| Assertion | Status | Evidence |")
    add("|---|---|---|")
    for item in data.get("assertions", []):
        add(f"| `{item['name']}` | {item['status'].upper()} | {_cell(_evidence(item))} |")
    add("")
    add("## Crash matrix")
    add("")
    add("| Case | Scale | Result | Evidence |")
    add("|---|---|---|---|")
    for item in data.get("crash_matrix", []):
        add(f"| {item['name']} - {item['title']} | {_cell(item.get('scale', ''))} | "
            f"{item['status'].upper()} | {_cell(_evidence(item))} |")
    add("")
    add("## Endurance")
    add("")
    add("| Case | Scale / duration | Result | Evidence |")
    add("|---|---|---|---|")
    for item in data.get("endurance", []):
        add(f"| {item['name']} - {item['title']} | {_cell(item.get('scale', ''))} | "
            f"{item['status'].upper()} | {_cell(_evidence(item))} |")
    add("")
    run = data.get("runs", {}).get("interrupted")
    if run:
        add("## Kill / restart sequence (interrupted run)")
        add("")
        add("| # | Kind | Trigger | Committed at kill | In flight | Offline files | "
            "Restart s | Recovery | Integrity |")
        add("|---|---|---|---|---|---|---|---|---|")
        for kill in run.get("kills", []):
            add(f"| {kill['index']} | {kill['kind']} | {kill['label']} | "
                f"{kill['committed_at_kill']:,} | {kill['in_flight_at_kill']} | "
                f"{kill['offline_files']} | {kill.get('restart_seconds')} | "
                f"{'ok' if kill['recovery_ok'] else _cell(kill['recovery_failures'])} | "
                f"{'clean' if kill['integrity_ok'] else 'NOT CLEAN'} |")
        add("")
        add("Checkpoints: " + ", ".join(
            f"{c['label']} ({c['committed']:,} committed, {'ok' if c['ok'] else 'FAILED'})"
            for c in run.get("checkpoints", [])))
        add("")
    add("## Workload")
    add("")
    for key, value in data.get("workload", {}).items():
        add(f"- {key.replace('_', ' ')}: {value}")
    add("")
    add("## Ground truth (planned)")
    add("")
    for key, value in data.get("ground_truth", {}).items():
        if not isinstance(value, dict):
            add(f"- {key.replace('_', ' ')}: {value}")
    add("")
    add("## Runs, timing and resources")
    add("")
    for name, item in data.get("runs", {}).items():
        resource = item.get("resource", {})
        add(f"- **{name}**: {item['files_written']:,} files over {item['arrival_seconds']} s "
            f"({item['arrival_rate_per_second']}/s); recognition "
            f"{item['recognition_rate_per_second']} sheets/s; run {item['run_seconds']} s; "
            f"peak tree RSS {resource.get('peak_tree_rss_bytes', 0) / 1e6:.0f} MB; "
            f"peak processes {resource.get('peak_processes')}; max in flight "
            f"{resource.get('max_in_flight_seen')}; "
            f"DB {resource.get('database_bytes', 0) / 1e6:.0f} MB; "
            f"logged lock errors {item.get('database_lock_errors_logged')}")
    finite = data.get("finite")
    if finite:
        add(f"- **finite control**: {finite['seconds']} s ({finite['recognition_seconds']} s "
            f"recognition), batches {len(finite['batches'])}")
    golden = data.get("golden")
    if golden:
        add(f"- **golden one-batch regression**: {'match' if golden.get('ok') else 'DIFFERS'} "
            f"({golden.get('fixture', '')})")
    add("")
    add("## Environment")
    add("")
    for key in ("platform", "windows", "python", "processor", "cpu_logical", "cpu_physical",
                "ram_bytes", "sqlite"):
        add(f"- {key}: {env.get(key)}")
    config = data.get("config", {})
    add(f"- seeds: cohort {config.get('seed')}, timing {config.get('timing_seed')}")
    add(f"- workers {config.get('workers')}, unit size {config.get('unit_size')}, trickle "
        f"{config.get('trickle_seconds')} s, in flight {config.get('max_in_flight')}, "
        f"stability {config.get('stability')}")
    coordinator = env.get("coordinator")
    if coordinator:
        if coordinator.get("kind") == "packaged":
            add(f"- coordinator: **packaged** `{coordinator.get('executable')}` "
                f"({coordinator.get('size')} bytes, SHA-256 `{coordinator.get('sha256')}`)")
        else:
            add(f"- coordinator: source (`{coordinator.get('executable')}`)")
    for runtime in data.get("coordinator_runtime", []):
        add(f"- coordinator runtime ({runtime.get('incarnations')} incarnations of "
            f"{', '.join(runtime.get('runs', []))}): frozen {runtime.get('frozen')}, "
            f"Python {runtime.get('python')}, package `{runtime.get('package_location')}`, "
            f"SQLite {runtime.get('sqlite_library')}, journal_mode {runtime.get('journal_mode')}, "
            f"synchronous {runtime.get('synchronous')}, "
            f"busy_timeout {runtime.get('busy_timeout')}, "
            f"foreign_keys {runtime.get('foreign_keys')}")
    add("")
    return "\n".join(lines) + "\n"


__all__ = ["REPORT_JSON", "REPORT_MD", "render_markdown", "write_reports"]
