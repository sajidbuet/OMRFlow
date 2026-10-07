"""One whole campaign, end to end (revised phase 9).

:func:`run_campaign` prepares the campaign (plan, images, manifest), runs the
interrupted qualification run, the uninterrupted control and the finite
control, runs the golden one-batch regression, evaluates every registered
assertion, crash case and endurance case, and writes ``report.json`` and
``report.md``. A harness failure, a timeout or Ctrl+C still writes a report -
``FAILED``, with the stage and its diagnostics - after stopping every child
process it owns.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from omr_scanner.evaluation.intake_qualification import assertions, cohort, reference
from omr_scanner.evaluation.intake_qualification.config import VERDICT_FAILED, CampaignConfig
from omr_scanner.evaluation.intake_qualification.report import write_reports
from omr_scanner.evaluation.intake_qualification.supervisor import (
    CampaignError,
    ContinuousRun,
    prepare_campaign,
)

DEFAULT_TEMPLATE = Path("examples") / "templates" / "synthetic_answer_sheet.omrt"


def environment() -> dict[str, Any]:
    """Everything needed to rerun the campaign on equivalent terms."""
    import psutil

    from omr_scanner import __version__
    from omr_scanner.database.migrations import SCHEMA_VERSION

    commit = dirty = ""
    root = Path(__file__).resolve().parents[4]
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, check=True, timeout=30).stdout.strip()
        dirty = subprocess.run(["git", "status", "--short", "--untracked-files=no"], cwd=root,
                               capture_output=True, text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    import sqlite3

    return {
        "commit": commit,
        "working_tree_clean": dirty.strip() == "",
        "working_tree_changes": dirty.strip().splitlines()[:50],
        "application_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "windows": platform.win32_ver() if sys.platform == "win32" else None,
        "processor": platform.processor(),
        "cpu_logical": os.cpu_count(),
        "cpu_physical": psutil.cpu_count(logical=False),
        "ram_bytes": psutil.virtual_memory().total,
        "sqlite": sqlite3.sqlite_version,
        "environment": {key: os.environ[key] for key in (
            "PYTHONPATH", "OMP_NUM_THREADS", "QT_QPA_PLATFORM", "PROCESSOR_IDENTIFIER",
        ) if key in os.environ},
    }


def run_campaign(
    config: CampaignConfig,
    output_root: Path,
    *,
    template_path: Path = DEFAULT_TEMPLATE,
    progress: Any = None,
) -> tuple[Path, dict[str, Any]]:
    """Run everything; return ``(campaign folder, report payload)``. Never raises for a failure."""
    from omr_scanner.evaluation.intake_qualification.finite import run_finite, run_golden

    say = progress or (lambda *_a, **_k: None)
    began = time.time()
    env = environment()
    campaign = prepare_campaign(
        config, output_root, template_path,
        progress=lambda done, total: say(f"rendered {done:,} / {total:,} images"),
    )
    say(f"campaign {campaign.campaign_id}: {len(campaign.plan.main_arrivals):,} arrivals, "
        f"{len(campaign.plan.contents):,} images rendered")
    runs: dict[str, Any] = {}
    error: dict[str, Any] = {}
    active: ContinuousRun | None = None
    finite = golden = None
    try:
        say("interrupted run: starting")
        active = ContinuousRun(campaign, "interrupted", campaign.plan, interrupted=True)
        runs["interrupted"] = active.execute()
        runs["interrupted"].plan_mode = config.mode.value
        active = None
        say("interrupted run: finished")
        if config.run_control:
            control_plan = cohort.retime(campaign.plan, config.timing_seed + 1,
                                         config.control_duration_seconds)
            say("control run: starting")
            active = ContinuousRun(campaign, "control", control_plan, interrupted=False)
            runs["control"] = active.execute()
            runs["control"].plan_mode = config.mode.value
            active = None
            say("control run: finished")
        if config.run_finite:
            say("finite control: starting")
            finite = run_finite(campaign)
            say("finite control: finished")
            golden = run_golden(campaign.root / "golden.json")
    except KeyboardInterrupt:
        error = {"stage": "interrupted by the operator", "message": "Ctrl+C",
                 "trace": traceback.format_exc()[-4000:]}
        if active is not None:
            active.abort()
    except CampaignError as exc:
        error = {"stage": exc.stage, "message": str(exc), "diagnostics": exc.diagnostics,
                 "trace": traceback.format_exc()[-4000:]}
        if active is not None:
            active.abort()
    except Exception as exc:
        error = {"stage": "harness", "message": f"{type(exc).__name__}: {exc}",
                 "trace": traceback.format_exc()[-6000:]}
        if active is not None:
            active.abort()
    say("evaluating")
    try:
        payload = evaluate(campaign, runs, finite, golden, env=env, error=error,
                           seconds=time.time() - began)
    except Exception as exc:  # an evaluator defect still leaves a (failed) report
        error = error or {"stage": "evaluation", "message": f"{type(exc).__name__}: {exc}",
                          "trace": traceback.format_exc()[-6000:]}
        payload = evaluate(campaign, {}, None, None, env=env, error=error,
                           seconds=time.time() - began)
    write_reports(campaign.root, payload)
    from omr_scanner.evaluation.intake_qualification.supervisor import cleanup_links

    cleanup_links(campaign.root)
    return campaign.root, payload


def evaluate(campaign: Any, runs: dict[str, Any], finite: Any, golden: Any, *,
             env: dict[str, Any], error: dict[str, Any], seconds: float) -> dict[str, Any]:
    """The report payload, from the evidence only."""
    plan = campaign.plan
    config = campaign.config
    run = runs.get("interrupted")
    control = runs.get("control")
    sha_to_content = campaign.sha_to_content()
    payload: dict[str, Any] = {
        "schema_version": assertions.REPORT_SCHEMA_VERSION,
        "campaign_id": campaign.campaign_id,
        "mode": config.mode.value,
        "config": config.to_json(),
        "plan_digest": plan.digest(),
        "template_fingerprint": plan.template_fingerprint,
        "environment": env,
        "error": error,
        "seconds": round(seconds, 1),
        "render_seconds": round(campaign.timings.get("render_seconds", 0.0), 1),
        "ground_truth": reference.summarize_expected(plan),
        "not_proven": NOT_PROVEN,
        "proves": PROVES,
    }
    completed = run is not None and not error and (control is not None or not config.run_control)
    if run is None:
        names = list(assertions.REQUIRED_ASSERTIONS)
        failed = []
        for name in names:
            item = assertions.CheckResult(name)
            item.fail(f"not evaluated: the campaign stopped at {error.get('stage', '?')}")
            failed.append(item.settle())
        payload["assertions"] = [item.to_json() for item in failed]
        payload["crash_matrix"] = []
        payload["endurance"] = []
        payload["verdict"] = VERDICT_FAILED
        payload["scale"] = {"release_scale": False, "reasons": ["no completed run"]}
        return payload
    from omr_scanner.evaluation.intake_qualification.assertions import evaluate_checkpoint

    final_checkpoint = evaluate_checkpoint(
        plan, run.final_facts, run.final_snapshot, run.final_population,
        sha_to_content=sha_to_content, seal_digests=run.seal_digests,
    )
    final_checkpoint["label"] = "final"
    results = assertions.evaluate_assertions(
        plan, run, control, finite, golden, sha_to_content=sha_to_content,
        template_path=campaign.template_path, share_root=campaign.root / "interrupted" / "share",
        final_checkpoint=final_checkpoint,
    )
    comparison = assertions.compare_runs(plan, run, control, sha_to_content,
                                         label="interrupted vs control")
    index = assertions.RunIndex.build(run, sha_to_content)
    crash = assertions.crash_matrix(plan, run, control, index, comparison)
    endurance = assertions.endurance(plan, run, control, comparison)
    counters = assertions.workload(plan, run)
    offline = next((item for item in results if item.name == "offline_arrivals_discovered"), None)
    if offline is not None:
        counters["offline_arrivals"] = offline.evidence.get("written_while_coordinator_down")
    scale = assertions.scale_check(config.mode.value, counters, results)
    payload.update({
        "verdict": assertions.verdict(results, crash, endurance, scale, completed=completed),
        "scale": scale,
        "workload": counters,
        "assertions": [item.to_json() for item in results],
        "crash_matrix": [item.to_json() for item in crash],
        "endurance": [item.to_json() for item in endurance],
        "runs": {name: summarize_run(item) for name, item in runs.items()},
        "finite": summarize_finite(finite),
        "golden": golden,
        "final_checkpoint": final_checkpoint,
        "control_comparison": comparison,
    })
    return payload


def summarize_run(run: Any) -> dict[str, Any]:
    writes = [item for item in run.writer_events if item["event"] == "write_completed"]
    claims = sum(
        len(item["scans"]) for item in run.coordinator_events if item["event"] == "claimed"
    )
    commits = [item for item in run.coordinator_events if item["event"] == "committed"]
    committed = sum(len(item["scans"]) for item in commits)
    first = min((float(item["write_completed_at"]) for item in writes), default=0.0)
    last = max((float(item["write_completed_at"]) for item in writes), default=0.0)
    commit_times = sorted(float(item["t"]) for item in commits)
    lock_errors = sum(
        1 for item in run.coordinator_events
        if "database is locked" in str(item.get("error", "")) + str(item.get("last_error", ""))
    )
    return {
        "name": run.name,
        "project": str(run.project),
        "session_id": run.session_id,
        "interrupted": run.interrupted,
        "error": run.error,
        "files_written": len(writes),
        "arrival_seconds": round(last - first, 1),
        "arrival_rate_per_second": round(len(writes) / max(last - first, 1e-9), 3),
        "sheets_claimed": claims,
        "sheets_committed_logged": committed,
        "recognition_rate_per_second": round(
            committed / max((commit_times[-1] - commit_times[0]) if commit_times else 1.0, 1e-9), 3
        ),
        "run_seconds": round(run.timings.get("run_seconds", 0.0), 1),
        "caught_up_after_last_write_seconds": round(run.caught_up_at - last, 1) if last else None,
        "kills": [
            {"index": k.index, "kind": k.kind, "label": k.label, "incarnation": k.incarnation,
             "trigger": k.trigger, "committed_at_kill": k.committed_after,
             "in_flight_at_kill": k.processing_after, "exit_code": k.exit_code,
             "orphans": list(k.orphans), "lock_left": k.lock_left, "hot_journal": k.hot_journal,
             "offline_files": k.offline_files,
             "restart_seconds": (k.restart.get("started") or {}).get("seconds"),
             "recovery_ok": k.restart.get("ok"), "recovery_failures": k.restart.get("failures"),
             "returned_to_pending": k.restart.get("returned"),
             "added_conflicts": len(k.restart.get("added_conflicts", [])),
             "integrity_ok": not assertions._integrity_failures(k.integrity, k.label),
             "integrity_seconds": k.integrity.get("seconds")}
            for k in run.kills
        ],
        "checkpoints": [
            {key: c.get(key) for key in ("label", "committed", "ok", "failures", "incarnation",
                                         "snapshot_seconds", "population_seconds")}
            for c in run.checkpoints
        ],
        "outages": run.outages,
        "vanished_seen": [{"intake_file_id": key, **value}
                          for key, value in sorted(run.vanished_seen.items())],
        "reprocess": run.reprocess,
        "endgame": {key: value for key, value in run.endgame.items()
                    if key not in ("first_downstream", "second_downstream")},
        "downstream": {
            phase: {code: {k: v for k, v in outcome.items()
                           if k not in ("statuses_before", "statuses_after")}
                    for code, outcome in (run.endgame.get(key) or {}).items()}
            for phase, key in (("first_close", "first_downstream"),
                               ("reopen", "second_downstream"))
        },
        "resource": run.resource,
        "database_lock_errors_logged": lock_errors,
        "final_integrity": {k: run.final_integrity.get(k) for k in (
            "quick_check", "integrity_check", "foreign_key_check", "journal_mode", "page_count",
            "seconds", "health_ok")},
        "final_snapshot": run.final_snapshot.partition.as_dict() if run.final_snapshot else None,
        "audit_events": len(run.final_facts.audit) if run.final_facts else None,
        "conflicts": len(run.final_facts.conflicts) if run.final_facts else None,
        "batches": len(run.final_facts.batches) if run.final_facts else None,
        "sheets": len(run.final_facts.sheets) if run.final_facts else None,
    }


def summarize_finite(finite: Any) -> dict[str, Any] | None:
    if finite is None:
        return None
    return {
        "project": str(finite.project),
        "batches": finite.batches,
        "seconds": round(finite.seconds, 1),
        "recognition_seconds": round(finite.recognition_seconds, 1),
        "endgame": {k: v for k, v in finite.endgame.items()
                    if k not in ("first_downstream", "second_downstream")},
        "error": finite.error,
    }


PROVES = (
    "Automated source-build behaviour under a large synthetic multi-source intake campaign "
    "with controlled failures and exact ground truth: separate scanner writer processes on "
    "local folders, real forced termination of the OMRFlow coordinator, restarts into the "
    "same project and session, a scripted operator using the production services, closure "
    "through the authoritative finish policy, and Results / report cell values compared with "
    "an independent reference."
)

NOT_PROVEN = (
    "genuine SMB / network-share behaviour (the source outage is a local folder link removed "
    "and restored)",
    "real scanner behaviour (images are synthetic, written by processes)",
    "actual power-loss durability (process termination is not power removal)",
    "real-paper quality-policy calibration (the quality policy is the unvalidated default)",
    "installed-build behaviour (this runs from source)",
    "recognition accuracy on real scans",
    "production readiness",
)


__all__ = ["DEFAULT_TEMPLATE", "NOT_PROVEN", "PROVES", "environment", "evaluate", "run_campaign"]
