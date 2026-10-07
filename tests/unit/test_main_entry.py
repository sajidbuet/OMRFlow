"""Tests for the command line entry point.

The GUI itself is not started here; ``main()`` is covered up to the point where
it would hand control to Qt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omr_scanner import APPLICATION_NAME, __version__
from omr_scanner.main import (
    EXIT_FAILURE,
    QUALIFICATION_COORDINATOR_ARGUMENT,
    build_argument_parser,
    main,
)


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


class TestQualificationCoordinatorEntry:
    """``OMRFlow.exe --intake-qualification-coordinator``: the installed-build run's hook."""

    @pytest.fixture
    def coordinator(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        import omr_scanner.evaluation.intake_qualification.coordinator as module
        import omr_scanner.gui.application as application

        calls: list[list[str]] = []

        def fake_main(argv: list[str]) -> int:
            calls.append(argv)
            return 7

        def no_gui(*_args: object, **_kwargs: object) -> int:
            raise AssertionError("the GUI must not start in coordinator mode")

        monkeypatch.setattr(module, "main", fake_main)
        monkeypatch.setattr(application, "run_gui", no_gui)
        return calls

    def test_it_runs_the_coordinator_with_the_remaining_arguments(self, coordinator):
        code = main([QUALIFICATION_COORDINATOR_ARGUMENT, "--run", "r.json", "--incarnation", "2"])

        assert code == 7
        assert coordinator == [["--run", "r.json", "--incarnation", "2"]]

    def test_only_as_the_first_argument(self, coordinator, monkeypatch: pytest.MonkeyPatch):
        import omr_scanner.gui.application as application

        monkeypatch.setattr(application, "run_gui", lambda *_a, **_k: 0)
        with pytest.raises(SystemExit):  # an unknown option for the GUI's parser
            main(["--log-level", "INFO", QUALIFICATION_COORDINATOR_ARGUMENT])
        assert coordinator == []

    def test_it_is_not_offered_in_help(self):
        assert QUALIFICATION_COORDINATOR_ARGUMENT not in build_argument_parser().format_help()

    def test_a_missing_console_is_replaced(self, coordinator, monkeypatch: pytest.MonkeyPatch):
        """A windowed frozen build starts with no stdout / stderr."""
        import sys

        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        assert main([QUALIFICATION_COORDINATOR_ARGUMENT, "--run", "r.json"]) == 7
        assert sys.stdout is not None and sys.stderr is not None
        sys.stdout.close()
        sys.stderr.close()

    def test_a_coordinator_failure_is_an_exit_code(self, monkeypatch: pytest.MonkeyPatch):
        import omr_scanner.evaluation.intake_qualification.coordinator as module

        def explode(_argv: list[str]) -> int:
            raise RuntimeError("the project could not be opened")

        monkeypatch.setattr(module, "main", explode)
        assert main([QUALIFICATION_COORDINATOR_ARGUMENT, "--run", "r.json"]) == EXIT_FAILURE

    def test_the_harness_uses_the_same_argument(self):
        from omr_scanner.evaluation.intake_qualification import supervisor

        assert supervisor.PACKAGED_COORDINATOR_ARGUMENT == QUALIFICATION_COORDINATOR_ARGUMENT
