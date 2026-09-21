r"""The Phase 10 qualification CLI - run the campaign unattended.

This is the *only* supported way to run the 100,000-sheet release
qualification. It is a headless, unattended, resumable command that needs no
GUI, no interactive input and no supervision: start it, leave the machine
alone, and read ``qualification_summary.md`` afterwards.

Nothing here decides anything. Every judgement lives in
:mod:`omr_scanner.evaluation.qualification`, which the GUI launcher calls
through *this* CLI rather than in-process, so that closing the GUI cannot
stop a campaign and the GUI cannot possibly reach a different verdict from
the command line.

Commands
--------
``preflight``
    Check whether a campaign can finish - disk, memory, template, generator
    determinism - and print the runtime and disk estimates. Writes
    ``preflight.md``/``preflight.json`` and changes nothing else.
``run``
    Start a campaign. Refuses to start over an existing one unless
    ``--restart`` is given.
``resume``
    Continue a campaign that was interrupted - including one whose
    orchestrator itself died. Runs already verified are skipped.
``status``
    Print where a campaign has got to, read-only. Safe to run against a
    campaign that is in progress.
``report``
    Rebuild ``qualification_summary.json``/``.md`` from the stored state.

Exit codes
----------
``0``
    Everything asked for succeeded: preflight passed, or the campaign
    qualified.
``1``
    A release-blocking failure. **Never** downgraded to a warning to let a
    phase finish - see the module docstring of
    :mod:`omr_scanner.evaluation.qualification`.
``2``
    Bad arguments, or a campaign that could not be started at all.
``130``
    Interrupted by the operator - either Ctrl-C, or a ``stop_requested``
    sentinel file in the output directory, which stops the campaign cleanly
    between runs. Neither is a failure, and both leave a resumable campaign.

Examples:
--------
Validate the harness itself, in a few minutes::

    .\.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification run `
        --output-dir C:\OMRflow-qualification\smoke `
        --template examples\templates\100_question_4_choice_example.omrt `
        --sheets 1000 --checkpoints 25,75 --warmup-sheets 50

The real thing - expect many hours, and leave it alone::

    .\.venv\Scripts\python.exe -m omr_scanner.tools.phase10_qualification run `
        --output-dir D:\OMRflow-qualification `
        --template examples\templates\100_question_4_choice_example.omrt
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from omr_scanner.evaluation import qualification

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_ARGS = 2
EXIT_INTERRUPTED = 130

DEFAULT_TEMPLATE = Path("examples/templates/100_question_4_choice_example.omrt")


def _percent_list(text: str) -> tuple[int, ...]:
    """Parse ``"1,25,50,75,99"`` into checkpoints, rejecting nonsense early."""
    values: list[int] = []
    for piece in text.split(","):
        stripped = piece.strip()
        if not stripped:
            continue
        try:
            percent = int(stripped)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{stripped!r} is not a whole percentage"
            ) from None
        if not 1 <= percent <= 99:
            raise argparse.ArgumentTypeError(
                f"checkpoint {percent} must be between 1 and 99 - a kill at 0% "
                "would have nothing committed to protect, and one at 100% "
                "would not be a kill"
            )
        values.append(percent)
    if not values:
        raise argparse.ArgumentTypeError("at least one checkpoint is required")
    return tuple(sorted(set(values)))


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the tool."""
    parser = argparse.ArgumentParser(
        prog="python -m omr_scanner.tools.phase10_qualification",
        description=(
            "Run the Phase 10 100,000-sheet release qualification, unattended."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_campaign_arguments(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--output-dir",
            type=Path,
            required=True,
            help=(
                "The one directory this campaign owns. Every project, log, "
                "digest and report is written here, and nothing outside it is "
                "ever created or removed. Put it on a drive with tens of GB "
                "free."
            ),
        )
        sub.add_argument(
            "--template",
            type=Path,
            default=DEFAULT_TEMPLATE,
            help=f"The .omrt template every run reads with (default: {DEFAULT_TEMPLATE}).",
        )
        sub.add_argument(
            "--sheets",
            type=int,
            default=qualification.DEFAULT_SHEETS,
            help=(
                f"Logical sheets per run (default: {qualification.DEFAULT_SHEETS:,}). "
                "Lower it only to validate the harness - a campaign at 1,000 "
                "sheets is not the 100,000-sheet qualification and its report "
                "says so."
            ),
        )
        sub.add_argument(
            "--seed",
            type=int,
            default=qualification.QUALIFICATION_SEED,
            help=f"Dataset seed (default: {qualification.QUALIFICATION_SEED}).",
        )
        sub.add_argument(
            "--checkpoints",
            type=_percent_list,
            default=qualification.DEFAULT_CHECKPOINTS,
            help=(
                "Durably-committed percentages at which to force a kill, as a "
                "comma-separated list (default: "
                f"{','.join(str(p) for p in qualification.DEFAULT_CHECKPOINTS)}). "
                "Each gets its own independent project."
            ),
        )
        sub.add_argument(
            "--workers",
            type=int,
            default=0,
            help="Worker processes per run. 0 (default) uses half the logical CPUs.",
        )
        sub.add_argument(
            "--opencv-threads",
            type=int,
            default=1,
            help="OpenCV threads per worker process (default: 1).",
        )
        sub.add_argument(
            "--worker-recycle-after",
            type=int,
            default=500,
            help="Sheets processed pool-wide before every worker is replaced (default: 500).",
        )
        sub.add_argument(
            "--mode",
            choices=("full", "reference", "progressive"),
            default="full",
            help=(
                "'full' (default): the release qualification - an "
                "uninterrupted reference run plus one independent "
                "forced-kill run per checkpoint. 'reference': the reference "
                "run alone, with no kills, which is the single "
                "100,000-sheet stress run and NOT the qualification. "
                "'progressive': one project killed at each checkpoint in "
                "turn - faster, weaker, an engineering aid only."
            ),
        )
        sub.add_argument(
            "--warmup-sheets",
            type=int,
            default=qualification.DEFAULT_WARMUP_SHEETS,
            help=(
                "Sheets in the short warm-up run that precedes the campaign "
                f"(default: {qualification.DEFAULT_WARMUP_SHEETS}; 0 skips it). "
                "It exists so a broken template or unwritable path fails in "
                "twenty seconds rather than forty minutes."
            ),
        )
        sub.add_argument(
            "--retain-passed-projects",
            action="store_true",
            help=(
                "Keep every run's full project directory, including the "
                "multi-GB database, after it has passed. Off by default "
                "because six 100,000-sheet projects is well over 10 GB of "
                "databases whose evidence has already been extracted. A "
                "FAILED run's project is always kept regardless."
            ),
        )

    preflight_parser = subparsers.add_parser(
        "preflight", help="Check whether a campaign can finish. Changes nothing."
    )
    add_campaign_arguments(preflight_parser)

    run_parser = subparsers.add_parser("run", help="Start a campaign.")
    add_campaign_arguments(run_parser)
    run_parser.add_argument(
        "--restart",
        action="store_true",
        help=(
            "Start over even though this output directory already holds a "
            "campaign. Previous state is overwritten; previous project "
            "directories and evidence are left on disk for you to inspect or "
            "delete yourself."
        ),
    )

    resume_parser = subparsers.add_parser(
        "resume", help="Continue an interrupted campaign, skipping verified runs."
    )
    resume_parser.add_argument("--output-dir", type=Path, required=True)

    status_parser = subparsers.add_parser(
        "status", help="Print a campaign's progress, read-only."
    )
    status_parser.add_argument("--output-dir", type=Path, required=True)
    status_parser.add_argument(
        "--json", action="store_true", help="Print the raw state as JSON."
    )

    report_parser = subparsers.add_parser(
        "report", help="Rebuild the summary report from stored state."
    )
    report_parser.add_argument("--output-dir", type=Path, required=True)

    return parser


def _config_from(arguments: argparse.Namespace) -> qualification.QualificationConfig:
    return qualification.QualificationConfig(
        output_dir=arguments.output_dir.expanduser().resolve(),
        template_path=arguments.template.expanduser().resolve(),
        sheets=arguments.sheets,
        seed=arguments.seed,
        checkpoints=tuple(arguments.checkpoints),
        workers=arguments.workers,
        opencv_threads=arguments.opencv_threads,
        worker_recycle_after=arguments.worker_recycle_after,
        mode=arguments.mode,
        warmup_sheets=arguments.warmup_sheets,
        retain_passed_projects=arguments.retain_passed_projects,
    )


def _print_preflight(report: qualification.PreflightReport) -> None:
    print(f"Runs:                 {', '.join(report.runs)}")
    print(
        f"Estimated runtime:    {qualification.format_duration(report.estimated_runtime_seconds)}"
        "  (estimate, from previously measured throughput on this project)"
    )
    print(
        "Estimated peak disk:  "
        f"{qualification.format_bytes(report.estimated_peak_disk_bytes)}"
    )
    print(
        "Available disk:       "
        f"{qualification.format_bytes(report.available_disk_bytes)}"
    )
    print()
    for check in report.checks:
        mark = "PASS" if check.passed else ("FAIL" if check.blocking else "WARN")
        print(f"  [{mark}] {check.name}: {check.detail}")
    print()
    print(f"Preflight: {'PASS' if report.passed else 'FAIL'}")


def _command_preflight(arguments: argparse.Namespace) -> int:
    config = _config_from(arguments)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    report = qualification.preflight(config)
    (config.output_dir / qualification.PREFLIGHT_JSON_NAME).write_text(
        json.dumps(report.to_json(), indent=2), encoding="utf-8"
    )
    (config.output_dir / qualification.PREFLIGHT_FILE_NAME).write_text(
        qualification.render_preflight_markdown(report, config), encoding="utf-8"
    )
    _print_preflight(report)
    print(f"\nWritten: {config.output_dir / qualification.PREFLIGHT_FILE_NAME}")
    return EXIT_OK if report.passed else EXIT_FAILED


def _command_run(arguments: argparse.Namespace) -> int:
    config = _config_from(arguments)
    state_path = config.output_dir / qualification.STATE_FILE_NAME
    if state_path.is_file() and not arguments.restart:
        print(
            f"A campaign already exists at {state_path}.\n"
            "Use `resume` to continue it, or `run --restart` to start over.",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS

    lock_path = config.output_dir / qualification.LOCK_NAME
    if lock_path.is_file():
        status = qualification.load_status(config.output_dir)
        if status.get("orchestrator_running"):
            print(
                "A campaign is already running in this output directory "
                f"(orchestrator PID {status.get('orchestrator_pid')}). "
                "Two campaigns sharing one directory would overwrite each "
                "other's evidence.",
                file=sys.stderr,
            )
            return EXIT_BAD_ARGS
        print(
            f"Removing a stale campaign lock ({lock_path}) - the process that "
            "held it is gone."
        )
        lock_path.unlink()

    state = qualification.QualificationState(state_path, config)
    state.save()
    print(
        f"Starting the Phase 10 qualification: {', '.join(config.run_ids())}, "
        f"{config.sheets:,} sheets each."
    )
    print(f"Output: {config.output_dir}")
    print(
        "This is unattended. Leave the machine alone - the forced process "
        "terminations are deliberate. Watch progress with:\n"
        f"  python -m omr_scanner.tools.phase10_qualification status "
        f"--output-dir {config.output_dir}"
    )
    return _drive(config, state)


def _command_resume(arguments: argparse.Namespace) -> int:
    output_dir = arguments.output_dir.expanduser().resolve()
    state_path = output_dir / qualification.STATE_FILE_NAME
    if not state_path.is_file():
        print(f"No campaign to resume at {state_path}.", file=sys.stderr)
        return EXIT_BAD_ARGS
    status = qualification.load_status(output_dir)
    if status.get("orchestrator_running"):
        print(
            "That campaign is still running "
            f"(orchestrator PID {status.get('orchestrator_pid')}). Nothing to resume.",
            file=sys.stderr,
        )
        return EXIT_BAD_ARGS

    state = qualification.QualificationState.load(state_path)
    pending = state.next_pending_run()
    if pending is None:
        print("Every run has already passed. Rebuilding the report only.")
        _json_path, md_path = qualification.build_reports(state)
        print(f"  {md_path}")
        return _exit_code_for(state.overall_status)

    done = [run for run in state.config.run_ids() if state.is_passed(run)]
    print(
        f"Resuming at {pending}. Already verified: {', '.join(done) or 'nothing'}."
    )
    state.overall_status = "running"
    state.notes.append(
        f"Resumed at {datetime.now(UTC).isoformat()} from {pending}."
    )
    state.save()
    return _drive(state.config, state)


def _drive(
    config: qualification.QualificationConfig, state: qualification.QualificationState
) -> int:
    """Run the campaign, translating its outcome into an exit code."""
    try:
        qualified = qualification.execute_campaign(config, state)
    except KeyboardInterrupt:
        print(
            "\nInterrupted. The campaign's state has been saved - continue with:\n"
            f"  python -m omr_scanner.tools.phase10_qualification resume "
            f"--output-dir {config.output_dir}",
            file=sys.stderr,
        )
        return EXIT_INTERRUPTED
    summary = config.output_dir / qualification.SUMMARY_MD_NAME
    print()
    if state.overall_status == "stopped":
        # Asked for, between runs, with every verified run kept. Reported as
        # an operator interruption (130) rather than success or failure,
        # because neither of those happened.
        print(
            "STOPPED at your request, between runs. Nothing failed. Continue "
            "with:\n"
            f"  python -m omr_scanner.tools.phase10_qualification resume "
            f"--output-dir {config.output_dir}\n"
            f"Report: {summary}"
        )
        return EXIT_INTERRUPTED
    if qualified and qualification.is_release_qualification(config):
        print(f"QUALIFIED. Report: {summary}")
        return EXIT_OK
    if qualified:
        # Passing a smaller or reference-only campaign is a success, and it
        # is not the release qualification. Exit 0 - nothing failed - while
        # saying plainly what it was.
        print(f"All runs passed. {qualification.qualification_scope_caveat(config)}")
        print(f"Report: {summary}")
        return EXIT_OK
    print(f"NOT QUALIFIED. Report: {summary}", file=sys.stderr)
    for note in state.notes:
        print(f"  {note}", file=sys.stderr)
    print(
        "\nFailed runs' project directories have been kept deliberately - "
        "they are the evidence. Nothing has been cleaned up for you.",
        file=sys.stderr,
    )
    return EXIT_FAILED


def _command_status(arguments: argparse.Namespace) -> int:
    output_dir = arguments.output_dir.expanduser().resolve()
    status = qualification.load_status(output_dir)
    if arguments.json:
        print(json.dumps(status, indent=2))
        return EXIT_OK if status.get("exists") else EXIT_BAD_ARGS
    if not status.get("exists"):
        print(f"No campaign at {output_dir}.", file=sys.stderr)
        return EXIT_BAD_ARGS
    if not status.get("readable"):
        print(f"Campaign state unreadable: {status.get('error')}", file=sys.stderr)
        return EXIT_FAILED

    print(f"Campaign:     {output_dir}")
    print(f"Started:      {status.get('started_at')}")
    print(f"Updated:      {status.get('updated_at')}")
    print(f"Status:       {status.get('overall_status')}")
    print(
        "Orchestrator: "
        + (
            f"running (PID {status.get('orchestrator_pid')})"
            if status.get("orchestrator_running")
            else "not running"
        )
    )
    print()
    for name, record in status.get("stages", {}).items():
        detail = record.get("detail", "")
        print(f"  {record.get('status', '?'):<9} {name:<10} {detail}")
    notes = status.get("notes") or ()
    if notes:
        print()
        for note in notes:
            print(f"  ! {note}")
    return EXIT_OK


def _exit_code_for(overall_status: str) -> int:
    """Translate a campaign's stored status into a shell exit code.

    In one place, because three commands report the same statuses and an
    earlier version of this file disagreed with itself: a campaign stopped
    deliberately between runs, and one that passed at reduced scale, were
    both reported as release-blocking failures by ``report`` while ``run``
    reported them correctly. Nothing failed in either case.
    """
    if overall_status in {"qualified", "passed_not_qualification"}:
        return EXIT_OK
    if overall_status in {"stopped", "interrupted", "running"}:
        return EXIT_INTERRUPTED
    return EXIT_FAILED


def _command_report(arguments: argparse.Namespace) -> int:
    output_dir = arguments.output_dir.expanduser().resolve()
    state_path = output_dir / qualification.STATE_FILE_NAME
    if not state_path.is_file():
        print(f"No campaign state at {state_path}.", file=sys.stderr)
        return EXIT_BAD_ARGS
    state = qualification.QualificationState.load(state_path)
    json_path, md_path = qualification.build_reports(state)
    print(f"Written:\n  {json_path}\n  {md_path}")
    return _exit_code_for(state.overall_status)


def main(argv: list[str] | None = None) -> int:
    """Run the tool and return a shell exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    arguments = build_parser().parse_args(argv)
    handlers = {
        "preflight": _command_preflight,
        "run": _command_run,
        "resume": _command_resume,
        "status": _command_status,
        "report": _command_report,
    }
    return handlers[arguments.command](arguments)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
