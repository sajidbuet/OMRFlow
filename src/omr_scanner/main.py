"""Application entry point.

Purpose:
    Parse the command line, configure logging, load application configuration
    and hand control to the GUI.

Responsibilities:
    * Be the only module that decides what happens before Qt exists.
    * Install the last-resort exception hook so an uncaught error is logged
      rather than printed and lost.

What does NOT belong here:
    * Widgets, services or domain logic.

Invariants:
    * Logging is configured before anything else can log.
    * :func:`main` returns a process exit code and never raises.
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import sys
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from omr_scanner import (
    APPLICATION_NAME,
    RELEASE_CHANNEL,
    __version__,
    build_identifier,
)
from omr_scanner.config import app_log_file, load_app_config
from omr_scanner.utils.logging_setup import configure_logging

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1

QUALIFICATION_COORDINATOR_ARGUMENT = "--intake-qualification-coordinator"
"""Hidden first argument: run the intake-qualification coordinator, not the GUI.

The intake qualification harness (``omr_scanner.evaluation.intake_qualification``)
kills and restarts a headless coordinator process - the production continuous
engine, recognition worker pool and intake service - while scanner writers keep
writing. From source that process is ``python -m ...coordinator``; a frozen
build has no ``-m``, so the release gate's installed-build run (revised phase
10) starts the *installed* executable with this argument instead, and the
packaged runtime - its bundled Python, SQLite, OpenCV and frozen worker
start-up - is what gets killed. Not listed in ``--help``: it is a qualification
hook, not an operator command, and it opens no window.
"""


def build_argument_parser() -> argparse.ArgumentParser:
    """Return the command line parser for the ``omrflow`` entry point."""
    parser = argparse.ArgumentParser(
        prog="omrflow",
        description=f"{APPLICATION_NAME} - template-driven OMR examination processing.",
    )
    parser.add_argument("--version", action="version", version=f"{APPLICATION_NAME} {__version__}")
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        help="Project folder to open on start-up.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override the configured log level for this run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Start OMRFlow.

    Args:
        argv: Command line arguments excluding the program name. Defaults to
            ``sys.argv[1:]``.

    Returns:
        ``0`` on a clean exit, ``1`` when start-up failed.
    """
    # Batch recognition reads sheets in worker processes, and a frozen Windows
    # build re-runs this executable to start each one. Without this call that
    # child would start a second copy of the application instead of a worker -
    # recursively. It does nothing at all when running from source.
    multiprocessing.freeze_support()

    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == [QUALIFICATION_COORDINATOR_ARGUMENT]:
        return _run_qualification_coordinator(raw[1:])

    arguments = build_argument_parser().parse_args(argv)

    config = load_app_config()
    level = (
        getattr(logging, arguments.log_level)
        if arguments.log_level is not None
        else config.log_level_value()
    )
    configure_logging(level=level, log_file=app_log_file())
    _install_exception_hook()

    # The build identifier rather than the bare version: it carries the
    # source commit, so a log attached to a prerelease bug report identifies
    # the exact revision that produced it rather than merely the release.
    logger.info(
        "%s %s (%s) starting (Python %s)",
        APPLICATION_NAME,
        build_identifier(),
        RELEASE_CHANNEL.value,
        sys.version.split()[0],
    )

    try:
        from omr_scanner.gui.application import run_gui

        exit_code = run_gui(config, initial_project=arguments.project)
    except Exception:
        # Qt failing to initialise (no display, missing platform plugin) must
        # produce a log entry rather than an unexplained crash.
        logger.exception("Application terminated with an unhandled error")
        return EXIT_FAILURE

    logger.info("%s exiting with code %d", APPLICATION_NAME, exit_code)
    return exit_code


def _run_qualification_coordinator(argv: Sequence[str]) -> int:
    """Run one intake-qualification coordinator incarnation; see the argument's note."""
    import os

    # A windowed frozen build has no console, so `sys.stdout` / `sys.stderr`
    # are None and anything that prints would raise. The coordinator's
    # evidence goes to its own log file; route stray output to the null device
    # (left open for the life of the process).
    if sys.stdout is None:
        sys.stdout = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115
    configure_logging(level=load_app_config().log_level_value(), log_file=app_log_file())
    _install_exception_hook()
    logger.info(
        "%s %s (%s) starting as an intake-qualification coordinator (Python %s)",
        APPLICATION_NAME,
        build_identifier(),
        RELEASE_CHANNEL.value,
        sys.version.split()[0],
    )
    from omr_scanner.evaluation.intake_qualification.coordinator import main as coordinator_main

    try:
        return coordinator_main(list(argv))
    except SystemExit as exit_request:
        code = exit_request.code
        return code if isinstance(code, int) else EXIT_FAILURE
    except BaseException:
        # Already written to the coordinator's own evidence log ("crashed").
        logger.exception("The intake-qualification coordinator failed")
        return EXIT_FAILURE


def _install_exception_hook() -> None:
    """Route uncaught exceptions to the log before the default handler runs."""
    previous_hook = sys.excepthook

    def hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical(
                "Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback)
            )
        previous_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = hook


if __name__ == "__main__":
    raise SystemExit(main())
