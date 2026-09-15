"""Tests for the command line entry point.

The GUI itself is not started here; ``main()`` is covered up to the point where
it would hand control to Qt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner import APPLICATION_NAME, __version__
from omr_scanner.main import EXIT_FAILURE, build_argument_parser, main


def test_parser_accepts_an_optional_project_path():
    arguments = build_argument_parser().parse_args(["C:/exams/midterm"])

    assert arguments.project == Path("C:/exams/midterm")
    assert arguments.log_level is None


def test_parser_rejects_an_unknown_log_level():
    with pytest.raises(SystemExit):
        build_argument_parser().parse_args(["--log-level", "SHOUT"])


def test_version_flag_reports_the_package_version(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exit_info:
        build_argument_parser().parse_args(["--version"])

    assert exit_info.value.code == 0
    assert f"{APPLICATION_NAME} {__version__}" in capsys.readouterr().out


def test_main_reports_failure_when_the_gui_cannot_start(monkeypatch: pytest.MonkeyPatch):
    """A Qt start-up failure must be logged and turned into an exit code."""
    import omr_scanner.gui.application as application

    def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("no Qt platform plugin")

    monkeypatch.setattr(application, "run_gui", explode)

    assert main([]) == EXIT_FAILURE
