r"""Command line for the revised phase 10 SMB qualification (``ACCEPTANCE_CRITERIA.md`` §6).

Three steps on the OMRFlow machine (PowerShell, from the repository root)::

    # 1. Plan, render and package (one package per scanner PC):
    .venv\Scripts\python.exe -m omr_scanner.tools.smb_qualification prepare `
        --output Scratch\Qualification\smb `
        --source "A=\\SCANNER-PC-A\scans\omr" `
        --log "A=\\SCANNER-PC-A\scans\omrflow-smb-logs\writer_A.jsonl" `
        --source "B=\\SCANNER-PC-B\scans\omr" `
        --log "B=\\SCANNER-PC-B\scans\omrflow-smb-logs\writer_B.jsonl"

    # 2. Copy packages\writer_A / writer_B to the scanner PCs, then run:
    .venv\Scripts\python.exe -m omr_scanner.tools.smb_qualification run --campaign <folder>

    # A local rehearsal of the whole procedure (tooling check only; never SMB evidence):
    .venv\Scripts\python.exe -m omr_scanner.tools.smb_qualification rehearse `
        --output Scratch\Qualification\smb

Exit codes: ``0`` PASS (``run``) or a completed rehearsal whose every
assertion that could run passed (``rehearse``); ``1`` FAIL; ``2`` bad
arguments; ``3`` ``run`` completed but is NOT SMB QUALIFICATION (not genuine
SMB, or below scale); ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

DEFAULT_TEMPLATE = Path("examples") / "templates" / "synthetic_answer_sheet.omrt"


def _pairs(values: list[str] | None, what: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for value in values or []:
        label, sep, rest = value.partition("=")
        if not sep or not label.strip() or not rest.strip():
            raise ValueError(f"{what} must be LABEL=PATH, not {value!r}")
        found[label.strip().upper()] = rest.strip()
    return found


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser (``prepare`` / ``run`` / ``rehearse``)."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.smb_qualification",
        description="Revised phase 10 SMB qualification (genuine network shares only).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="plan, render and package a campaign")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--source", action="append", required=True,
                         help=r"LABEL=\\host\share\folder (repeat; labels A, B, ...)")
    prepare.add_argument("--log", action="append", required=True,
                         help=r"LABEL=\\host\share\logs\writer_LABEL.jsonl as read from here")
    prepare.add_argument("--writer-target", action="append",
                         help=r"LABEL=D:\Scans\omr - the -Target on the scanner PC")
    prepare.add_argument("--writer-log-local", action="append",
                         help=r"LABEL=D:\Scans\omrflow-smb-logs\writer_LABEL.jsonl "
                              "on the scanner PC")
    prepare.add_argument("--files-per-source", type=int, default=1_000)
    prepare.add_argument("--duration", type=float, default=1_800.0,
                         help="arrival timeline in seconds (writes are serial; the real "
                              "period is longer)")
    prepare.add_argument("--seed", type=int, default=20261007)
    prepare.add_argument("--workers", type=int, default=6)
    prepare.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)

    run = commands.add_parser("run", help="drive a prepared campaign against genuine SMB")
    run.add_argument("--campaign", type=Path, required=True)
    _run_options(run)

    rehearse = commands.add_parser("rehearse", help="the whole procedure on local folders "
                                                    "(tooling check; never SMB evidence)")
    rehearse.add_argument("--output", type=Path, required=True)
    rehearse.add_argument("--files-per-source", type=int, default=40)
    rehearse.add_argument("--duration", type=float, default=120.0)
    rehearse.add_argument("--seed", type=int, default=20261007)
    rehearse.add_argument("--workers", type=int, default=2)
    rehearse.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    rehearse.add_argument("--fast-stability", action="store_true",
                          help="a short quiet period instead of the network default")
    _run_options(rehearse)
    return parser


def _run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--coordinator-exe", type=Path,
                        help="run OMRFlow from this packaged OMRFlow.exe instead of source")
    parser.add_argument("--restart-fraction", type=float, default=0.35)
    parser.add_argument("--outage-source", default="B")
    parser.add_argument("--outage-fraction", type=float, default=0.60)
    parser.add_argument("--outage-min-seconds", type=float, default=120.0)
    parser.add_argument("--offline-files", type=int, default=10)
    parser.add_argument("--writer-start-timeout", type=float, default=7_200.0)


def _plan(arguments: argparse.Namespace) -> Any:
    from omr_scanner.evaluation.smb_qualification.run import SmbPlan

    return SmbPlan(restart_fraction=arguments.restart_fraction,
                   outage_source=arguments.outage_source.upper(),
                   outage_fraction=arguments.outage_fraction,
                   outage_min_seconds=arguments.outage_min_seconds,
                   offline_files=arguments.offline_files,
                   writer_start_timeout=arguments.writer_start_timeout)


def execute(root: Path, *, mode: str, arguments: argparse.Namespace) -> tuple[str, Path]:
    """Run a prepared campaign; write the report; return ``(verdict, report folder)``."""
    from omr_scanner.evaluation.intake_qualification.campaign import coordinator_identity
    from omr_scanner.evaluation.intake_qualification.supervisor import coordinator_command_for
    from omr_scanner.evaluation.smb_qualification import evaluate as judge
    from omr_scanner.evaluation.smb_qualification import package
    from omr_scanner.evaluation.smb_qualification.report import NOT_PROVEN, write_reports
    from omr_scanner.evaluation.smb_qualification.run import SmbRun, save_evidence

    campaign, record = package.load(root)
    executable = arguments.coordinator_exe.resolve() if arguments.coordinator_exe else None
    if executable is not None:
        campaign.coordinator_command = coordinator_command_for(executable)
    sources = [package.SourceSpec(**item) for item in record["sources"]]
    began = time.time()
    run = SmbRun(campaign, sources, mode=mode, plan=_plan(arguments))
    error: dict[str, Any] = {}
    evidence = None
    try:
        evidence = run.execute()
    except KeyboardInterrupt:
        error = {"stage": "interrupted by the operator", "trace": traceback.format_exc()[-3000:]}
        run.abort()
    except Exception as exc:
        error = {"stage": getattr(exc, "stage", "harness"),
                 "message": f"{type(exc).__name__}: {exc}",
                 "diagnostics": getattr(exc, "diagnostics", {}),
                 "trace": traceback.format_exc()[-6000:]}
        run.abort()
    save_evidence(run.dir / "smb_evidence.json", run.smb)
    payload: dict[str, Any] = {
        "campaign_id": campaign.campaign_id, "mode": mode, "plan_digest": record["plan_digest"],
        "config": record["config"], "prepared": {k: record[k] for k in (
            "files_per_source_required", "files_per_source_planned", "packages",
            "writer_script_sha256")},
        "coordinator": coordinator_identity(executable), "seconds": round(time.time() - began, 1),
        "error": error, "smb": asdict(run.smb), "not_proven": list(NOT_PROVEN),
    }
    if evidence is None:
        payload.update({"verdict": judge.VERDICT_FAIL, "measurements": {},
                        "assertions": [{"name": name, "status": "fail", "checked": 0,
                                        "failures": [f"not evaluated: stopped at "
                                                     f"{error.get('stage')}"]}
                                       for name in judge.SMB_ASSERTIONS]})
    else:
        payload.update(judge.evaluate(campaign.plan, evidence, run.smb, campaign.sha_to_content()))
    if error and payload["verdict"] != judge.VERDICT_FAIL:
        payload["verdict"] = judge.VERDICT_FAIL
    folder = run.dir
    write_reports(folder, payload)
    from omr_scanner.evaluation.intake_qualification.supervisor import cleanup_links

    cleanup_links(root)
    return str(payload["verdict"]), folder


def main(argv: list[str] | None = None) -> int:
    """Run one command; return the exit code (see the module docstring)."""
    from omr_scanner.evaluation.smb_qualification import evaluate as judge
    from omr_scanner.evaluation.smb_qualification import package
    from omr_scanner.evaluation.smb_qualification.run import (
        MODE_REHEARSAL,
        MODE_SMB,
        rehearsal_sources,
    )

    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            sources = _pairs(arguments.source, "--source")
            logs = _pairs(arguments.log, "--log")
            targets = _pairs(arguments.writer_target, "--writer-target")
            local_logs = _pairs(arguments.writer_log_local, "--writer-log-local")
            if set(logs) != set(sources):
                raise ValueError("every --source needs a --log with the same label")
            specs = [package.SourceSpec(label, sources[label], logs[label],
                                        targets.get(label, ""), local_logs.get(label, ""))
                     for label in sorted(sources)]
            root = package.prepare(arguments.output, specs, template_path=arguments.template,
                                   files_per_source=arguments.files_per_source,
                                   seed=arguments.seed, duration_seconds=arguments.duration,
                                   workers=arguments.workers)
            print(f"Prepared {root}\nRead {root / 'OPERATOR_STEPS.md'} next.")
            return 0
        if arguments.command == "run":
            verdict, folder = execute(arguments.campaign, mode=MODE_SMB, arguments=arguments)
            print(f"\nVerdict: {verdict}\nReport: {folder / 'report.md'}")
            return {judge.VERDICT_PASS: 0, judge.VERDICT_NOT_SMB: 3}.get(verdict, 1)
        # rehearse: prepare with local folders, then run in rehearsal mode.
        from omr_scanner.evaluation.intake_qualification.config import StabilitySettings

        staging = arguments.output.resolve() / f"rehearsal-{time.strftime('%Y%m%dT%H%M%S')}"
        specs = rehearsal_sources(staging, ["A", "B"])
        stability = (StabilitySettings(min_observations=2, quiet_seconds=1.5,
                                       max_decode_attempts=3, retry_backoff_seconds=1.0,
                                       poll_interval_seconds=1.0)
                     if arguments.fast_stability else None)
        root = package.prepare(staging, specs, template_path=arguments.template,
                               files_per_source=arguments.files_per_source, seed=arguments.seed,
                               duration_seconds=arguments.duration, workers=arguments.workers,
                               stability=stability)
        try:
            verdict, folder = execute(root, mode=MODE_REHEARSAL, arguments=arguments)
        finally:
            from contextlib import suppress

            from omr_scanner.evaluation.intake_qualification.supervisor import remove_link

            for link in (staging / "rehearsal" / "share").glob("*"):
                with suppress(OSError):
                    remove_link(link, attempts=5)
        print(f"\nVerdict: {verdict}\nReport: {folder / 'report.md'}")
        return 0 if verdict == judge.VERDICT_NOT_SMB else 1
    except KeyboardInterrupt:
        return 130
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - command line
    sys.exit(main())
