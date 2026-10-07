"""Judging an SMB qualification run (revised phase 10, ``ACCEPTANCE_CRITERIA.md`` §6).

Every assertion is computed from evidence recorded outside the process that
was killed: the scanner writers' logs (written on the scanner PCs), the
coordinator's log, and the project database read read-only afterwards. The
Phase 9 checks are reused where they apply unchanged (byte copies, same-name
independence, re-recognition, integrity, health); the rest are the network
cases ``ACCEPTANCE_CRITERIA.md`` §6 and the brief's C11 list name.

Clocks: the writers log their own machine's clock. Before any time is
compared, every writer timestamp is moved onto this machine's clock with the
offset measured through the share, and every time comparison allows the
measured uncertainty. The decisive "no partial file" evidence does not depend
on clocks at all: what OMRFlow registered and recognised must have exactly
the SHA-256 of the *completed* file.

Verdicts:
    ``PASS`` - genuine SMB (topology checked from facts), the required scale,
    and every assertion passed. ``FAIL`` - any assertion failed.
    ``NOT SMB QUALIFICATION`` - every assertion that could run passed, but the
    run was a rehearsal on local folders or below the required scale; it is
    tooling evidence only.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime
from typing import Any

from omr_scanner.evaluation.intake_qualification import assertions as phase9
from omr_scanner.evaluation.intake_qualification.assertions import (
    FAIL,
    NOT_EXERCISED,
    CheckResult,
    RunIndex,
)
from omr_scanner.evaluation.intake_qualification.cohort import WritePattern
from omr_scanner.evaluation.smb_qualification.package import MIN_FILES_PER_SOURCE, MIN_SOURCES

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_NOT_SMB = "NOT SMB QUALIFICATION"

SMB_ASSERTIONS = (
    "genuine_smb_topology",
    "database_on_local_disk",
    "scale_and_workload",
    "completed_files_discovered_exactly_once",
    "no_partial_file_processed",
    "no_stable_file_duplicated",
    "provenance_correct",
    "same_filenames_independent",
    "outage_marked_unreachable",
    "reconnection_recovers_files",
    "restart_same_session_and_offline_arrivals",
    "no_completed_scan_rerecognised",
    "no_database_lock_errors",
    "sqlite_integrity",
    "project_health",
)
"""The SMB qualification's assertions, in report order; every one is always emitted."""

REQUIRED_PATTERNS = (
    WritePattern.STEPPED.value, WritePattern.HELD_OPEN.value, WritePattern.HEADER_FIRST.value,
    WritePattern.RENAME.value, WritePattern.LONG_PAUSE.value,
)

_TIME_KEYS = ("t", "write_started_at", "write_completed_at", "created_at")


def _epoch(moment: datetime | None) -> float | None:
    return phase9._epoch(moment)


def to_local_clock(events: list[dict[str, Any]], clocks: dict[str, dict[str, Any]]
                   ) -> list[dict[str, Any]]:
    """Writer events with every timestamp moved onto this machine's clock."""
    moved = []
    for item in events:
        label = str(item.get("role", ":")).split(":", 1)[1]
        offset = float((clocks.get(label) or {}).get("offset_seconds") or 0.0)
        copy = dict(item)
        for key in _TIME_KEYS:
            if key in copy and copy[key] is not None:
                copy[key] = float(copy[key]) - offset
        moved.append(copy)
    return moved


def _uncertainty(clocks: dict[str, dict[str, Any]], label: str) -> float:
    clock = clocks.get(label) or {}
    return (float(clock.get("uncertainty_seconds") or 0.0)
            + float(clock.get("spread_seconds") or 0.0))


def distribution(values: list[float]) -> dict[str, Any]:
    """Count / min / median / p95 / max of ``values`` (seconds)."""
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, max(0, round(0.95 * (len(ordered) - 1))))]
    return {"count": len(ordered), "min": round(ordered[0], 4),
            "median": round(statistics.median(ordered), 4), "p95": round(p95, 4),
            "max": round(ordered[-1], 4)}


# ----------------------------------------------------------------------
# Assertions
# ----------------------------------------------------------------------
def a_topology(smb: Any) -> CheckResult:
    result = CheckResult("genuine_smb_topology")
    sources = smb.sources
    genuine = [item for item in sources if item["genuine_smb"]]
    for item in sources:
        if not item["genuine_smb"]:
            result.fail(f"source {item['label']} {item['root']}: {item['reason']}")
    # Every name the OMRFlow machine answers to - the writer reports its
    # NetBIOS name (%COMPUTERNAME%, at most 15 characters), which need not be
    # the DNS host name (found by the first rehearsal: "SAJID-ASUS-LAPT" vs
    # "Sajid-Asus-Laptop").
    names = {str(item).lower() for item in smb.machine.get("identities", ())}
    names.add(str(smb.machine.get("host", "")).lower())
    names |= {name.split(".")[0] for name in names}
    for label, writer in sorted(smb.writers.items()):
        host = str(writer.get("host") or "").lower()
        if not host:
            result.fail(f"writer {label} did not report its host")
        elif host in names or host.split(".")[0] in names:
            result.fail(f"writer {label} ran on the OMRFlow machine ({host})")
    if len(sources) < MIN_SOURCES:
        result.fail(f"{len(sources)} source(s); at least {MIN_SOURCES} are required")
    result.checked = len(genuine)
    result.minimum = MIN_SOURCES
    result.evidence = {"sources": sources, "writers": smb.writers,
                       "omrflow_host": smb.machine.get("host"),
                       "omrflow_names": sorted(names)}
    if smb.mode != "smb":
        # A rehearsal never counts, whatever it observed.
        result.failures = []
        result.failure_count = 0
        result.checked = 0
        result.evidence["note"] = "rehearsal on local folders: not SMB evidence"
    return result.settle()


def a_database(smb: Any) -> CheckResult:
    result = CheckResult("database_on_local_disk")
    database = smb.database
    if not database.get("local"):
        result.fail(f"the project database is on a {database.get('drive_type')} drive: "
                    f"{database.get('path')}")
    result.checked = 1
    result.evidence = dict(database)
    return result.settle()


def a_scale(plan: Any, run: Any, smb: Any) -> CheckResult:
    result = CheckResult("scale_and_workload")
    writes = [item for item in run.writer_events if item["event"] == "write_completed"]
    per_source = Counter(item["source"] for item in writes)
    patterns = Counter(item["pattern"] for item in writes)
    names: dict[str, set[str]] = defaultdict(set)
    for item in writes:
        names[item["name"]].add(item["source"])
    same_name = sum(1 for sources in names.values() if len(sources) > 1)
    short = {label: per_source.get(label, 0) for label in plan.config.source_labels
             if per_source.get(label, 0) < MIN_FILES_PER_SOURCE}
    missing = [pattern for pattern in REQUIRED_PATTERNS if not patterns.get(pattern)]
    reasons = []
    if short:
        reasons.append(f"fewer than {MIN_FILES_PER_SOURCE} files written at {short}")
    if len(per_source) < MIN_SOURCES:
        reasons.append(f"{len(per_source)} source(s) wrote files")
    if missing:
        reasons.append(f"write patterns never used: {missing}")
    if not same_name:
        reasons.append("no file name written at more than one source")
    if not run.kills:
        reasons.append("no OMRFlow restart during intake")
    if not run.outages:
        reasons.append("no share outage")
    result.evidence = {"files_per_source": dict(per_source), "patterns": dict(patterns),
                       "partial_writes": sum(n for p, n in patterns.items() if p != "atomic"),
                       "same_name_across_sources": same_name, "restarts": len(run.kills),
                       "outages": len(run.outages), "reasons": reasons}
    result.minimum = 1
    if reasons:
        if smb.mode == "smb":
            for reason in reasons:
                result.fail(reason)
        result.checked = 0  # below the qualification's scale: never PASS
    else:
        result.checked = 1
    return result.settle()


def a_completed(plan: Any, run: Any, index: RunIndex) -> CheckResult:
    result = CheckResult("completed_files_discovered_exactly_once")
    planned = {(item.source, item.name) for item in plan.main_arrivals}
    used: Counter[int] = Counter()
    for key in sorted(planned):
        write = index.writes.get(key)
        if write is None:
            result.fail(f"{key} was never completely written (writer stopped?)")
            continue
        rows = index.ledger_by_path.get(key, [])
        if len(rows) != 1:
            result.fail(f"{key}: {len(rows)} ledger rows")
            continue
        row = rows[0]
        if row.sha != write["sha256"]:
            result.fail(f"{key}: ledger hash differs from the completed file's")
        if row.state not in ("registered", "duplicate_content"):
            result.fail(f"{key}: ledger state {row.state}")
        if row.state == "registered":
            if row.batch_scan_id is None:
                result.fail(f"{key}: registered without a sheet")
            else:
                used[row.batch_scan_id] += 1
        result.checked += 1
    for key, rows in index.ledger_by_path.items():
        if key in planned:
            continue
        for row in rows:
            if not (row.state == "ignored" and key[1].endswith(".part")):
                result.fail(f"unplanned ledger row {key} ({row.state})")
    for scan, count in used.items():
        if count > 1:
            result.fail(f"sheet {scan} registered from {count} ledger rows")
    for failure in phase9.vanished_failures(run):
        result.fail(failure)
    result.minimum = max(1, len(planned))
    result.evidence = {"planned_files": len(planned), "checked": result.checked,
                       "vanished_seen": len(getattr(run, "vanished_seen", {}) or {})}
    return result.settle()


def a_no_partial(plan: Any, run: Any, index: RunIndex, clocks: dict[str, dict[str, Any]]
                 ) -> CheckResult:
    result = CheckResult("no_partial_file_processed")
    sheets = run.final_facts.sheets
    margins: list[float] = []
    for key, rows in index.ledger_by_path.items():
        for row in rows:
            if row.state != "registered" or row.batch_scan_id is None:
                continue
            write = index.writes.get(key)
            if write is None:
                result.fail(f"{key} registered but never completely written")
                continue
            sheet = sheets.get(row.batch_scan_id)
            if row.sha != write["sha256"] or (sheet is not None and sheet.sha != write["sha256"]):
                result.fail(f"{key}: registered / recognised bytes are not the completed file's")
            allowance = _uncertainty(clocks, key[0])
            done = float(write["write_completed_at"])
            registered = _epoch(row.registered_at)
            if registered is not None and registered < done - allowance:
                result.fail(f"{key} registered {done - registered:.3f}s before its writer "
                            f"finished (clock allowance {allowance:.3f}s)")
            claim = index.first_claim.get(row.batch_scan_id)
            if claim is not None:
                if claim < done - allowance:
                    result.fail(f"{key} submitted {done - claim:.3f}s before its writer finished")
                margins.append(claim - done)
            result.checked += 1
    patterns = Counter(item.pattern.value for item in plan.main_arrivals)
    result.minimum = 1
    result.evidence = {
        "registered_files_checked": result.checked,
        "partial_writes_planned": sum(n for p, n in patterns.items() if p != "atomic"),
        "completion_to_submission_seconds": distribution(margins),
        "clock_allowance_seconds": {label: _uncertainty(clocks, label) for label in clocks},
    }
    return result.settle()


def a_provenance(run: Any, index: RunIndex, roots: dict[str, str]) -> CheckResult:
    result = CheckResult("provenance_correct")
    sheets = run.final_facts.sheets
    batches = run.final_facts.batches
    for (source, name), rows in index.ledger_by_path.items():
        for row in rows:
            if row.state != "registered" or row.batch_scan_id is None:
                continue
            result.checked += 1
            if row.file_name != name or row.relative_path != name:
                result.fail(f"{source}/{name}: ledger names "
                            f"{row.relative_path!r}/{row.file_name!r}")
            expected = roots[source].rstrip("\\/") + "\\" + name
            if row.absolute_path.replace("/", "\\").lower() != expected.replace("/", "\\").lower():
                result.fail(f"{source}/{name}: observed path {row.absolute_path!r}, "
                            f"expected {expected!r}")
            sheet = sheets.get(row.batch_scan_id)
            if sheet is None:
                result.fail(f"{source}/{name}: sheet {row.batch_scan_id} missing")
                continue
            if sheet.intake_file_id != row.intake_file_id:
                result.fail(f"{source}/{name}: sheet does not point back to its ledger row")
            batch = batches.get(sheet.batch_id)
            if batch is None or batch.source_id != row.source_id:
                result.fail(f"{source}/{name}: unit not attributed to its source")
    names = getattr(run, "original_names", None) or {}
    for scan, shown in names.items():
        sheet = sheets.get(int(scan))
        if sheet is None or sheet.intake_file_id is None:
            continue
        row = next((r for rs in index.ledger_by_path.values() for r in rs
                    if r.intake_file_id == sheet.intake_file_id), None)
        if row is not None and shown != row.file_name:
            result.fail(f"sheet {scan} shown as {shown!r}, arrived as {row.file_name!r}")
    result.minimum = 1
    result.evidence = {"registered_files_checked": result.checked,
                       "displayed_names_checked": len(names)}
    return result.settle()


def a_outage(run: Any, index: RunIndex, smb: Any) -> CheckResult:
    result = CheckResult("outage_marked_unreachable")
    if not run.outages:
        result.fail("no share outage was performed")
        return result.settle()
    for outage in run.outages:
        label = outage["source"]
        result.checked += 1
        if not outage.get("seen_unreachable"):
            result.fail(f"Scanner {label}: never marked unreachable by the project")
        if outage.get("reachability") in (None, "online"):
            result.fail(f"Scanner {label}: reachability during the outage "
                        f"{outage.get('reachability')}")
        start, end = float(outage["start"]), float(outage["end"])
        others = [
            key for key, write in index.writes.items()
            if key[0] != label and start <= float(write["write_completed_at"]) <= end
        ]
        registered_others = [
            key for key in others
            if any(row.state in ("registered", "duplicate_content") and row.registered_at
                   and start <= (_epoch(row.registered_at) or 0) <= end + 3600
                   for row in index.ledger_by_path.get(key, []))
        ]
        outage["other_sources_files_during_outage"] = len(others)
        if not others:
            result.fail("no other source wrote a file during the outage (continuation unexercised)")
        if others and not registered_others:
            result.fail("no other source's file was registered after the outage began")
        for sample in smb.reachability_samples:
            if start <= sample["t"] <= end:
                for other, lists in sample["lists"].items():
                    if other != label and not lists:
                        result.fail(f"Scanner {other} could not be listed during "
                                    f"Scanner {label}'s outage")
                        break
    for failure in phase9.vanished_failures(run):
        result.fail(failure)
    result.minimum = 1
    result.evidence = {"outages": phase9._plain(run.outages)}
    return result.settle()


def a_reconnect(run: Any, index: RunIndex) -> CheckResult:
    result = CheckResult("reconnection_recovers_files")
    for outage in run.outages:
        label = outage["source"]
        start, end = float(outage["start"]), float(outage["end"])
        for (source, name), write in index.writes.items():
            if source != label or not start <= float(write["write_completed_at"]) <= end:
                continue
            rows = index.ledger_by_path.get((source, name), [])
            if len(rows) != 1 or rows[0].state not in ("registered", "duplicate_content"):
                result.fail(f"{source}/{name} written during the outage was not recovered "
                            f"({[row.state for row in rows]})")
                continue
            first_seen = _epoch(rows[0].first_seen_at)
            if first_seen is not None and first_seen < start:
                continue  # seen before the share went away; still recovered
            result.checked += 1
    result.minimum = 1
    result.evidence = {"files_written_during_outages": result.checked}
    return result.settle()


def a_restart(run: Any, index: RunIndex) -> CheckResult:
    result = CheckResult("restart_same_session_and_offline_arrivals")
    sessions = {sid for sid, _state, _n in run.final_facts.sessions}
    if len(sessions) != 1 or run.session_id not in sessions:
        result.fail(f"sessions in the project: {sorted(sessions)}")
    if not run.kills:
        result.fail("OMRFlow was never restarted during intake")
    for kill in run.kills:
        result.checked += 1
        for failure in kill.restart.get("failures", []):
            result.fail(f"{kill.label}: {failure}")
    if run.kills and not any(kill.processing_after >= 1 for kill in run.kills):
        result.fail("no kill landed with a sheet in a worker")
    intervals = run.resource.get("down_intervals", [])
    offline = 0
    for key, write in index.writes.items():
        done = float(write["write_completed_at"])
        if any(start <= done <= end for start, end in intervals):
            rows = index.ledger_by_path.get(key, [])
            if len(rows) != 1 or rows[0].state not in ("registered", "duplicate_content"):
                result.fail(f"{key} written while OMRFlow was down was not discovered")
            offline += 1
    if run.kills and offline == 0:
        result.fail("no file was completed while OMRFlow was down")
    result.minimum = 1
    result.evidence = {"restarts": len(run.kills), "files_written_while_down": offline,
                       "sessions": sorted(sessions)}
    return result.settle()


def a_locks(run: Any) -> CheckResult:
    result = CheckResult("no_database_lock_errors")
    for item in run.coordinator_events:
        text = str(item.get("error", "")) + str(item.get("last_error", ""))
        if "database is locked" in text:
            result.fail(f"{item.get('role')} {item.get('event')}: {text[:160]}")
    result.checked = len(run.coordinator_events)
    result.minimum = 1
    result.evidence = {"coordinator_events_checked": result.checked}
    return result.settle()


def _renamed(item: CheckResult, name: str) -> CheckResult:
    return replace(item, name=name)


def evaluate(plan: Any, run: Any, smb: Any, sha_to_content: dict[str, str]) -> dict[str, Any]:
    """Every SMB assertion, the measurements, and the verdict."""
    clocks = smb.clock
    # The SMB run writes the main timeline only (no closure, so no late script).
    plan = replace(plan, arrivals=tuple(plan.main_arrivals))
    local = replace(run, writer_events=to_local_clock(run.writer_events, clocks))
    index = RunIndex.build(local, sha_to_content)
    roots = {item["label"]: item["root"] for item in smb.sources}
    results = [
        a_topology(smb),
        a_database(smb),
        a_scale(plan, local, smb),
        a_completed(plan, local, index),
        a_no_partial(plan, local, index, clocks),
        _renamed(phase9.a_duplicates(plan, local, index), "no_stable_file_duplicated"),
        a_provenance(local, index, roots),
        _renamed(phase9.a_names(plan, local, index), "same_filenames_independent"),
        a_outage(local, index, smb),
        a_reconnect(local, index),
        a_restart(local, index),
        phase9.a_not_reread(plan, local, index),
        a_locks(local),
        phase9.a_sqlite([local], None),
        _renamed(phase9.a_health([local], None), "project_health"),
    ]
    assert tuple(item.name for item in results) == SMB_ASSERTIONS
    return {
        "assertions": [item.to_json() for item in results],
        "measurements": measurements(local, index, smb),
        "verdict": verdict(results, smb),
    }


def verdict(results: list[CheckResult], smb: Any) -> str:
    """PASS only for genuine SMB with every assertion passed; see the module docstring."""
    if any(item.status == FAIL for item in results):
        return VERDICT_FAIL
    if smb.mode != "smb" or any(item.status == NOT_EXERCISED for item in results):
        return VERDICT_NOT_SMB
    return VERDICT_PASS


# ----------------------------------------------------------------------
# Measurements
# ----------------------------------------------------------------------
def measurements(run: Any, index: RunIndex, smb: Any) -> dict[str, Any]:
    """Listing cost per source, stabilisation latency, outage and restart timings."""
    roots = {item["root"].rstrip("\\/").lower(): item["label"] for item in smb.sources}
    listing: dict[str, list[float]] = defaultdict(list)
    listed: dict[str, list[int]] = defaultdict(list)
    listing_errors: Counter[str] = Counter()
    for item in run.coordinator_events:
        if item["event"] != "listing":
            continue
        root = str(item.get("root", "")).rstrip("\\/").lower()
        label = roots.get(root) or roots.get(root.removeprefix("\\\\?\\unc\\")) or root
        if item.get("error"):
            listing_errors[label] += 1
            continue
        listing[label].append(float(item["seconds"]))
        if item.get("files") is not None:
            listed[label].append(int(item["files"]))
    ready: dict[str, list[float]] = defaultdict(list)
    seen: dict[str, list[float]] = defaultdict(list)
    for key, write in index.writes.items():
        rows = index.ledger_by_path.get(key, [])
        if len(rows) != 1:
            continue
        done = float(write["write_completed_at"])
        ready_at = _epoch(rows[0].ready_at)
        first_seen = _epoch(rows[0].first_seen_at)
        in_outage = any(float(o["start"]) <= done <= float(o.get("end", o["start"]))
                        and o["source"] == key[0] for o in run.outages)
        in_down = any(start <= done <= end for start, end in run.resource.get("down_intervals", []))
        if ready_at is not None and not in_outage and not in_down:
            ready[key[0]].append(ready_at - done)
        if first_seen is not None:
            seen[key[0]].append(first_seen - float(write["write_started_at"]))
    everything = [value for values in ready.values() for value in values]
    return {
        "listing_seconds_per_reconciliation": {
            label: {**distribution(values),
                    "files_listed": distribution([float(v) for v in listed[label]]),
                    "errors": listing_errors.get(label, 0)}
            for label, values in sorted(listing.items())
        },
        "stabilisation_seconds_completion_to_ready": {
            "all": distribution(everything),
            **{label: distribution(values) for label, values in sorted(ready.items())},
            "excludes": "files completed during an outage or while OMRFlow was down",
        },
        "first_seen_after_write_started_seconds": {
            label: distribution(values) for label, values in sorted(seen.items())
        },
        "outages": [
            {key: outage.get(key) for key in (
                "source", "mechanism", "requested_at", "start", "seen_unreachable",
                "restore_requested_at", "end", "project_online_at")}
            | {"detected_after_seconds": round(float(outage["seen_unreachable"])
                                               - float(outage["start"]), 3)
               if outage.get("seen_unreachable") else None,
               "online_after_seconds": round(float(outage["project_online_at"])
                                             - float(outage["end"]), 3)
               if outage.get("project_online_at") else None}
            for outage in run.outages
        ],
        "restart": smb.restart,
        "clock_offsets": smb.clock,
    }


__all__ = [
    "SMB_ASSERTIONS",
    "VERDICT_FAIL",
    "VERDICT_NOT_SMB",
    "VERDICT_PASS",
    "distribution",
    "evaluate",
    "measurements",
    "to_local_clock",
    "verdict",
]
