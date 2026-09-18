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

from omr_scanner import APPLICATION_NAME, __version__
from omr_scanner.config import app_log_file, load_app_config
from omr_scanner.utils.logging_setup import configure_logging

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1


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

    arguments = build_argument_parser().parse_args(argv)

    config = load_app_config()
    level = (
        getattr(logging, arguments.log_level)
        if arguments.log_level is not None
        else config.log_level_value()
    )
    configure_logging(level=level, log_file=app_log_file())
    _install_exception_hook()

    logger.info("%s %s starting (Python %s)", APPLICATION_NAME, __version__, sys.version.split()[0])

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
