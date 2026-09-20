r"""Headless CLI for the mandatory production-hardening stress benchmark.

Purpose:
    Run (or resume) a deterministic synthetic stress batch - up to and
    including the mandatory 100,000-sheet acceptance run (Phase 10, §19,
    §31, §52) - from the command line, with no GUI involved, so the
    kill-and-resume acceptance matrix can be scripted and reproduced exactly.

Usage::

    # First run: creates the project and registers a 100,000-sheet batch.
    python -m omr_scanner.tools.benchmark_stress out/stress_100k --create \\
        --template examples/templates/100_question_4_choice_example.omrt \\
        --sheets 100000 --seed 20260920 --workers 8

    # Resume after an interruption (including a forced kill): same command,
    # without --create. It finds the existing batch and processes only what
    # `resumable_scans` says is left.
    python -m omr_scanner.tools.benchmark_stress out/stress_100k \\
        --template examples/templates/100_question_4_choice_example.omrt \\
        --sheets 100000 --seed 20260920 --workers 8

    # Smaller presets for development, CI and smoke testing (§22):
    python -m omr_scanner.tools.benchmark_stress out/stress_100 --create \\
        --template sheet.omrt --sheets 100 --seed 1 --workers 4

Output:
    ``<project>/benchmarks/stress_<batch_id>.json`` - the machine-readable
    report (§50: environment, configuration, throughput, resource use,
    reliability counts). ``<project>/benchmarks/stress_<batch_id>.jsonl`` -
    the raw telemetry samples (:mod:`omr_scanner.services.telemetry`).

Exit codes:
    ``0`` finished (every sheet reached a terminal state), ``1`` the project
    or template could not be used, ``2`` bad arguments, ``3`` cancelled
    (Ctrl+C) with sheets still pending - re-run the same command to resume.

What this tool deliberately does not do:
    Kill itself. The mandatory kill-and-resume acceptance matrix (§30/§31)
    requires an *abrupt*, ungraceful termination - this process must be
    killed from outside (a process manager, ``taskkill /F``, ``kill -9``, or
    an orchestration script watching the printed progress or the project's
    database) at the desired completion percentage, then this exact command
    run again to resume. See ``development/PHASE_10_HANDOFF.md`` for the
    full kill-matrix procedure and exact commands.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from omr_scanner import __version__
from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.database.models import ScanBatch
from omr_scanner.errors import OMRScannerError
from omr_scanner.evaluation import stress_dataset, stress_runner
from omr_scanner.services import (
    ProjectSession,
    batch_store,
    create_project,
    open_project,
    project_health,
    telemetry,
)
from omr_scanner.services.parallel_batch import describe_environment
from omr_scanner.services.project_lock import ProjectLockHeldError
from omr_scanner.services.template_service import load_template

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_ARGS = 2
EXIT_CANCELLED = 3

STRESS_SOURCE_LABEL = "cli-benchmark"


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the tool."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.benchmark_stress",
        description=(
            "Run or resume a deterministic synthetic stress batch through the "
            "real production pipeline."
        ),
    )
    parser.add_argument("project", type=Path, help="Project directory to create or open.")
    parser.add_argument(
        "--create", action="store_true", help="Create the project if it does not exist."
    )
    parser.add_argument("--template", type=Path, required=True, help="The .omrt template to use.")
    parser.add_argument(
        "--sheets", type=int, required=True, help="Total sheets in the stress dataset."
    )
    parser.add_argument("--seed", type=int, required=True, help="Master seed for the dataset.")
    parser.add_argument(
        "--workers", type=int, default=1, help="Worker processes (1 = single-threaded, no pool)."
    )
    parser.add_argument(
        "--opencv-threads", type=int, default=1, help="OpenCV threads per worker process."
    )
    parser.add_argument(
        "--worker-recycle-after",
        type=int,
        default=0,
        help="Sheets processed, pool-wide, before every worker is replaced. 0 disables.",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=None,
        help="Where to write the JSON benchmark report. Defaults to "
        "<project>/benchmarks/stress_<batch_id>.json.",
    )
    parser.add_argument("--quiet", action="store_true", help="Do not print progress lines.")
    parser.add_argument(
        "--force-lock",
        action="store_true",
        help=(
            "Remove an existing project lock before opening (Phase 10, §3). "
            "This is the explicit operator decision the lock model requires: "
            "pass it only when you are certain no other OMRFlow process - in "
            "particular no orphaned worker process from a previous forced "
            "kill - is still using this project. Resuming after a genuine "
            "abrupt termination of this same tool is exactly the case this "
            "flag exists for; never pass it to silently steal a lock another "
            "still-running instance legitimately holds."
        ),
    )
    return parser


def _open_or_create_project(arguments: argparse.Namespace) -> ProjectSession:
    if arguments.create and not arguments.project.exists():
        return create_project(arguments.project.parent, arguments.project.name)
    return open_project(arguments.project, force_lock=arguments.force_lock)


def _find_existing_stress_batch(
    database: ProjectDatabase, seed: int, sheet_count: int
) -> str | None:
    """Return a previously registered stress batch matching (seed, sheet_count)."""
    with database.session() as db_session:
        rows = db_session.execute(
            select(ScanBatch.batch_id, ScanBatch.settings_json)
        ).all()
    for batch_id, settings_json in rows:
        try:
            settings = json.loads(settings_json)
        except (json.JSONDecodeError, TypeError):
            continue
        matches_seed = settings.get("stress_seed") == seed
        matches_count = settings.get("stress_sheet_count") == sheet_count
        if matches_seed and matches_count:
            return str(batch_id)
    return None


def _progress_counts(database: ProjectDatabase, batch_id: str) -> tuple[int, int, int]:
    summary = batch_store.load_summary(database, batch_id)
    if summary is None:
        return 0, 0, 0
    return summary.completed + summary.warning, summary.pending, summary.failed


def main(argv: list[str] | None = None) -> int:
    """Run the tool and return a shell exit code."""
    logging.basicConfig(level=logging.WARNING)
    arguments = build_parser().parse_args(argv)
    if arguments.sheets <= 0:
        print("--sheets must be positive", file=sys.stderr)
        return EXIT_BAD_ARGS

    try:
        template = load_template(arguments.template)
    except OMRScannerError as exc:
        print(f"Could not load the template: {exc.user_message}", file=sys.stderr)
        return EXIT_FAILED

    try:
        session = _open_or_create_project(arguments)
    except ProjectLockHeldError as exc:
        print(f"Project is locked: {exc.holder.describe()}", file=sys.stderr)
        if exc.holder.likely_stale:
            print(
                "This lock looks stale (no such process on this machine). "
                "If you are certain nothing else is using this project, "
                "re-run with --force-lock.",
                file=sys.stderr,
            )
        return EXIT_FAILED
    except OMRScannerError as exc:
        print(f"Could not open the project: {exc.user_message}", file=sys.stderr)
        return EXIT_FAILED

    try:
        return _run(arguments, session.database, template)
    finally:
        # Closing the *session*, not only the database, is what releases the
        # project lock (Phase 10, §3) - a bug caught in this tool's own
        # manual testing: closing only `.database` left the lock file behind
        # forever, permanently refusing every later run or resume.
        session.close()


def _run(
    arguments: argparse.Namespace, database: ProjectDatabase, template: OmrTemplate
) -> int:
    health = project_health.quick_check(database)
    if not health.is_ok:
        print("Project failed a quick health check before starting:", file=sys.stderr)
        for issue in health.issues:
            print(f"  [{issue.level.value}] {issue.code}: {issue.message}", file=sys.stderr)
        return EXIT_FAILED

    spec = stress_dataset.StressDatasetSpec(seed=arguments.seed, sheet_count=arguments.sheets)
    batch_id = _find_existing_stress_batch(database, spec.seed, spec.sheet_count)
    resumed = batch_id is not None
    if batch_id is None:
        batch_id = stress_runner.create_stress_batch(
            database, spec, template, source_label=STRESS_SOURCE_LABEL
        )
        if not arguments.quiet:
            print(f"Registered new stress batch {batch_id} ({spec.sheet_count} sheets)")
    elif not arguments.quiet:
        summary = batch_store.load_summary(database, batch_id)
        print(
            f"Resuming stress batch {batch_id}: {summary.resume_label if summary else 'unknown'}"
        )

    benchmarks_dir = database.path.parent / "benchmarks"
    benchmarks_dir.mkdir(parents=True, exist_ok=True)
    report_path = arguments.report_out or benchmarks_dir / f"stress_{batch_id}.json"
    telemetry_path = benchmarks_dir / f"stress_{batch_id}.jsonl"

    recorder_db = batch_store.BatchRecorder(database=database, batch_id=batch_id)
    recorder_tel = telemetry.TelemetryRecorder(
        telemetry_path,
        lambda: _progress_counts(database, batch_id),
        database_path=database.path,
        worker_count=arguments.workers,
    )

    cancelled = {"flag": False}

    def _should_cancel() -> bool:
        return cancelled["flag"]

    def _on_progress(_progress: object) -> None:
        if arguments.quiet:
            return
        completed, pending, failed = _progress_counts(database, batch_id)
        eta = telemetry.estimate_completion(recorder_tel.samples)
        eta_text = f"{eta:.0f}s" if eta is not None else "calculating..."
        print(
            f"\rcompleted={completed} pending={pending} failed={failed} eta={eta_text}",
            end="",
            file=sys.stdout,
        )

    started = time.monotonic()
    recorder_tel.start()
    try:
        stress_runner.run_stress_batch(
            database,
            batch_id,
            template,
            spec,
            workers=arguments.workers,
            opencv_threads=arguments.opencv_threads,
            worker_recycle_after=arguments.worker_recycle_after,
            should_cancel=_should_cancel,
            on_result=recorder_db.record,
            on_progress=_on_progress,
        )
    except KeyboardInterrupt:
        cancelled["flag"] = True
    finally:
        recorder_db.flush()
        recorder_tel.stop()
        if not arguments.quiet:
            print()

    elapsed = time.monotonic() - started
    summary = batch_store.finalise_batch(database, batch_id)
    _write_report(
        report_path,
        arguments=arguments,
        database=database,
        batch_id=batch_id,
        spec=spec,
        summary=summary,
        elapsed_seconds=elapsed,
        resumed=resumed,
        telemetry_path=telemetry_path,
    )

    if summary is None or summary.pending > 0:
        if not arguments.quiet:
            print(
                f"Run ended with {summary.pending if summary else '?'} sheet(s) still "
                "pending. Re-run the same command to resume."
            )
        return EXIT_CANCELLED
    if not arguments.quiet:
        print(f"Finished: {summary.total} sheet(s) processed in {elapsed:.1f}s")
    return EXIT_OK


def _write_report(
    report_path: Path,
    *,
    arguments: argparse.Namespace,
    database: ProjectDatabase,
    batch_id: str,
    spec: stress_dataset.StressDatasetSpec,
    summary: batch_store.BatchSummary | None,
    elapsed_seconds: float,
    resumed: bool,
    telemetry_path: Path,
) -> None:
    """Write the §50 benchmark report, as much of it as this run measured.

    Deliberately partial where a metric this CLI does not itself produce
    (report-generation time, conflict throughput) would otherwise have to be
    fabricated - see ``development/PHASE_10_HANDOFF.md`` for exactly which
    §50 fields this tool populates and which remain for a future extension.
    """
    samples = telemetry.read_samples(telemetry_path)
    peak_memory_mb = max((s.process_memory_mb for s in samples), default=0.0)
    average_rate = (
        sum(s.sheets_per_second for s in samples) / len(samples) if samples else 0.0
    )
    payload = {
        "application_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "environment": describe_environment(),
        "batch_id": batch_id,
        "resumed_existing_batch": resumed,
        "configuration": {
            "sheet_count": spec.sheet_count,
            "seed": spec.seed,
            "workers": arguments.workers,
            "opencv_threads": arguments.opencv_threads,
            "worker_recycle_after": arguments.worker_recycle_after,
        },
        "this_run": {
            "elapsed_seconds": elapsed_seconds,
            "average_sheets_per_second": average_rate,
            "peak_process_memory_mb": peak_memory_mb,
            "telemetry_sample_count": len(samples),
        },
        "cumulative_batch_state": {
            "total": summary.total if summary else 0,
            "completed": summary.completed if summary else 0,
            "warning": summary.warning if summary else 0,
            "failed": summary.failed if summary else 0,
            "pending": summary.pending if summary else 0,
            "status": summary.status if summary else "unknown",
        },
        "database_size_mb": database.path.stat().st_size / (1024 * 1024)
        if database.path.is_file()
        else 0.0,
        "telemetry_file": str(telemetry_path),
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
