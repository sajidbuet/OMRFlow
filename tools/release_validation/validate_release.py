r"""OMRFlow release qualification - the entry point.

Run the whole thing and walk away::

    python tools\\release_validation\\validate_release.py --all

Nothing in this framework asks a question while it runs. Every decision is made
by the stage that made the observation, recorded with a reason, and folded into
an exit code: ``0`` when every release-blocking stage passed, non-zero when one
did not. That is the point - release qualification should not need anybody
watching it, least of all a language model.

Stage selection:
    With no flags, ``--safe`` is assumed: everything that does not install or
    uninstall software. Naming any stage explicitly runs only those.

Exit codes:
    ``0``   qualified
    ``1``   a release-blocking stage failed
    ``2``   the run could not start (bad arguments, unusable environment)
    ``130`` interrupted
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

# The repository root must be importable before anything else: this file is run
# as a script, so `sys.path[0]` is its own directory and `tools.release_validation`
# would not resolve.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from tools.release_validation import (  # noqa: E402
    accessibility_tests,
    build_tests,
    cleanup,
    environment_checks,
    gui_tests,
    installer_tests,
    packaged_app_tests,
    reporting,
    source_tests,
    visual_tests,
)
from tools.release_validation import (  # noqa: E402
    config as cfg,
)
from tools.release_validation.process_utils import ProcessRegistry  # noqa: E402
from tools.release_validation.results import RunResult  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tools.release_validation.results import StageResult

STAGE_FLAGS = (
    "source",
    "gui",
    "workflow",
    "accessibility",
    "visual",
    "build",
    "packaged",
    "installer",
)


def build_parser() -> argparse.ArgumentParser:
    """The command-line interface, documented in this directory's README."""
    parser = argparse.ArgumentParser(
        prog="validate_release.py",
        description="Qualify an OMRFlow release without supervision.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  validate_release.py --all              everything, including install/uninstall\n"
            "  validate_release.py --safe             everything except install/uninstall\n"
            "  validate_release.py --gui --accessibility\n"
            "  validate_release.py --installer dist\\installer\\OMRFlow-...-Setup-x64.exe\n"
        ),
    )

    selection = parser.add_argument_group("what to run")
    selection.add_argument(
        "--all",
        action="store_true",
        help="every stage, including the destructive installer round trip",
    )
    selection.add_argument(
        "--safe",
        action="store_true",
        help="every stage except the installer (the default when nothing is named)",
    )
    selection.add_argument("--source", action="store_true", help="the repository's pytest suite")
    selection.add_argument("--gui", action="store_true", help="Qt GUI functional checks")
    selection.add_argument(
        "--workflow", action="store_true", help="the end-to-end workflow smoke test"
    )
    selection.add_argument(
        "--accessibility", action="store_true", help="accessibility checks against the Qt tree"
    )
    selection.add_argument(
        "--visual", action="store_true", help="compare screenshots with the committed baselines"
    )
    selection.add_argument(
        "--build", action="store_true", help="verify the built bundle (see also --rebuild)"
    )
    selection.add_argument(
        "--packaged", action="store_true", help="launch and drive dist/OMRFlow/OMRFlow.exe"
    )
    selection.add_argument(
        "--installer",
        nargs="?",
        const="",
        metavar="PATH",
        help="install/launch/uninstall/reinstall. Optionally name the installer; "
        "otherwise the newest in dist/installer is used",
    )
    selection.add_argument(
        "--installed-app-only",
        action="store_true",
        help="only check an OMRFlow that is already installed; installs nothing",
    )
    selection.add_argument(
        "--stress",
        action="store_true",
        help="also run the stress-marked suite (excluded from --all; slow)",
    )

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument(
        "--rebuild",
        action="store_true",
        help="run Build-App.ps1 -Clean before verifying the bundle",
    )
    behaviour.add_argument(
        "--quick",
        action="store_true",
        help="source stage runs tests/unit only - for iterating, not for a release",
    )
    behaviour.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="exit non-zero when anything is a warning",
    )
    behaviour.add_argument(
        "--strict-accessibility",
        action="store_true",
        help="make accessibility errors release-blocking",
    )
    behaviour.add_argument(
        "--update-visual-baselines",
        action="store_true",
        help="overwrite the committed screenshots with this run's; review before committing",
    )
    behaviour.add_argument(
        "--replace-installation",
        action="store_true",
        help="allow the installer stage to remove an OMRFlow that is already installed",
    )
    behaviour.add_argument(
        "--kill-stray-processes",
        action="store_true",
        help="during cleanup, also stop OMRFlow processes this run did not start",
    )
    behaviour.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="keep the temporary workspace (projects, rendered sheets) after the run",
    )
    behaviour.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"where to write reports (default: {cfg.RESULTS_ROOT.name}/<timestamp>)",
    )
    behaviour.add_argument("--verbose", action="store_true", help="more console output")
    return parser


def _selected(arguments: argparse.Namespace) -> set[str]:
    """Which stages this invocation should run."""
    explicit = {name for name in STAGE_FLAGS if getattr(arguments, name, False)}
    if arguments.installer is not None:
        explicit.add("installer")
    if arguments.installed_app_only:
        return {"installed"}

    if arguments.all:
        return set(STAGE_FLAGS)
    if arguments.safe or not explicit:
        # The default. Everything that does not install or uninstall anything.
        return set(STAGE_FLAGS) - {"installer"}
    return explicit


def main(argv: list[str] | None = None) -> int:
    """Run the selected stages and return the process exit code."""
    arguments = build_parser().parse_args(argv)
    stages = _selected(arguments)

    config = cfg.new_run(results_root=arguments.results_dir)
    config.verbose = arguments.verbose
    config.keep_artifacts = arguments.keep_artifacts
    config.fail_on_warning = arguments.fail_on_warning
    config.update_visual_baselines = arguments.update_visual_baselines
    config.allow_replace_installation = arguments.replace_installation
    if arguments.installer:
        config.installer_path = Path(arguments.installer).resolve()
    config.prepare()

    run = RunResult(
        run_id=config.run_id,
        started=datetime.now().isoformat(timespec="seconds"),
        command_line=" ".join([Path(sys.argv[0]).name, *(argv or sys.argv[1:])]),
        environment=environment_checks.describe_environment(),
    )
    registry = ProcessRegistry()

    print(f"OMRFlow release qualification - run {config.run_id}")
    print(f"  stages : {', '.join(sorted(stages))}")
    print(f"  results: {config.output_dir}")
    print()

    interrupted = False
    try:
        environment = run.add(environment_checks.run(config))
        if environment.status.is_blocking:
            print("The environment is not usable; stopping before anything else runs.")
        else:
            _run_stages(run, config, registry, stages, arguments)
    except KeyboardInterrupt:  # pragma: no cover - operator pressed Ctrl-C
        interrupted = True
        print("\nInterrupted; cleaning up.")
    finally:
        run.add(
            cleanup.run(config, registry, kill_stray=arguments.kill_stray_processes)
        )
        run.finished = datetime.now().isoformat(timespec="seconds")
        reporting.write_json(run, config)
        reporting.write_markdown(run, config, fail_on_warning=arguments.fail_on_warning)
        reporting.print_summary(run, config, fail_on_warning=arguments.fail_on_warning)

    if interrupted:
        return 130
    return 0 if run.qualified(fail_on_warning=arguments.fail_on_warning) else 1


def _guarded(run: RunResult, name: str, call: Callable[[], StageResult]) -> None:
    """Run one stage, turning any escaping exception into a recorded failure.

    A bug in a stage must not destroy the run and the report with it. An
    unattended qualification that dies with a traceback has told nobody
    anything; one that records "this stage raised TypeError, here is the
    traceback" and carries on has told them exactly what to fix.
    """
    from tools.release_validation.results import StageResult

    try:
        run.add(call())
    except Exception as error:
        stage = StageResult(name=name)
        stage.fail(
            "the stage ran to completion",
            f"{type(error).__name__}: {error}",
            exception=error,
        )
        run.add(stage)
        print(f"  [{name}] raised {type(error).__name__}: {error}")


def _run_stages(
    run: RunResult,
    config: cfg.ValidationConfig,
    registry: ProcessRegistry,
    stages: set[str],
    arguments: argparse.Namespace,
) -> None:
    """Run the selected stages, in the order a failure is cheapest to find."""
    if "installed" in stages:
        _guarded(
            run,
            "Installed application launch",
            lambda: packaged_app_tests.run_installed(config, registry),
        )
        return

    if "source" in stages:
        _guarded(run, "Source tests", lambda: source_tests.run(config, quick=arguments.quick))

    if arguments.stress:
        _guarded(run, "Stress suite", lambda: source_tests.run_stress_smoke(config))

    if "gui" in stages:
        _guarded(run, "Qt GUI tests", lambda: gui_tests.run(config))

    if "workflow" in stages:
        _guarded(run, "Workflow smoke test", lambda: gui_tests.run_workflow(config))

    if "accessibility" in stages:
        _guarded(
            run,
            "Accessibility checks",
            lambda: accessibility_tests.run(config, strict=arguments.strict_accessibility),
        )

    # After the GUI stages, because it compares the screenshots they took.
    if "visual" in stages:
        _guarded(run, "Visual regression", lambda: visual_tests.run(config))

    if "build" in stages:
        if arguments.rebuild:
            _guarded(run, "Application build", lambda: build_tests.build_bundle(config))
        _guarded(run, "Build/package verification", lambda: build_tests.run(config))

    if "packaged" in stages:
        _guarded(
            run,
            "Packaged application launch",
            lambda: packaged_app_tests.run(config, registry),
        )

    if "installer" in stages:
        _guarded(run, "Installer qualification", lambda: installer_tests.run(config, registry))


if __name__ == "__main__":
    raise SystemExit(main())
