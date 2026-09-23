"""Stage: Qt GUI functional qualification, and the workflow smoke test.

Both stages here are thin: they choose a pytest selection, point the suite at
the run's screenshot directory, and hand the JUnit XML to the reporter. The
substance is in ``suites/test_gui_functional.py`` and
``suites/test_workflow_smoke.py``.

Why the GUI stage insists on a real desktop:
    The checks that matter most here - the ribbon's layout at five widths,
    focus moving on Tab, a window that maximises and restores - are the ones
    Qt's ``offscreen`` platform answers differently or not at all. A run on a
    machine with no desktop is recorded as SKIPPED with that said, never as a
    pass.
"""

from __future__ import annotations

import os

from tools.release_validation import config as cfg
from tools.release_validation.pytest_runner import run_pytest, suite_path
from tools.release_validation.results import StageResult


def _gui_environment(config: cfg.ValidationConfig) -> dict[str, str]:
    """The child environment for a GUI suite.

    Adds the screenshot directory the ``checkpoint`` fixture writes into. The
    Qt platform is deliberately *not* forced: whatever the operator's desktop
    provides is what a user would get, and forcing ``offscreen`` here would
    quietly downgrade the stage to something weaker while still reporting a
    pass.
    """
    environment = config.child_environment()
    environment["OMRFLOW_VALIDATION_SCREENSHOTS"] = str(config.screenshots_dir)
    return environment


def _headless() -> bool:
    """Whether this process has no usable desktop.

    On Windows a desktop is present unless the operator has explicitly asked
    for the offscreen platform, which is how a CI runner asks for it.
    """
    return os.environ.get("QT_QPA_PLATFORM", "").lower() in {"offscreen", "minimal"}


def run(config: cfg.ValidationConfig) -> StageResult:
    """Run the Qt GUI functional suite."""
    if _headless():
        stage = StageResult(name="Qt GUI tests")
        stage.skipped_reason = (
            "QT_QPA_PLATFORM is set to an offscreen platform; the layout, focus "
            "and window-state checks need a real desktop and would pass "
            "vacuously. Unset it and re-run on a Windows desktop session."
        )
        return stage

    return run_pytest(
        config,
        stage_name="Qt GUI tests",
        selection=(suite_path("test_gui_functional.py"),),
        junit_name="gui-validation.xml",
        environment=_gui_environment(config),
    )


def run_workflow(config: cfg.ValidationConfig) -> StageResult:
    """Run the end-to-end workflow smoke test.

    Not headless-gated: it drives the service layer rather than widgets, so it
    is as meaningful on a CI runner as on a desktop. That also makes it the one
    stage here that a future GitHub Actions job could adopt unchanged.
    """
    return run_pytest(
        config,
        stage_name="Workflow smoke test",
        selection=(suite_path("test_workflow_smoke.py"),),
        junit_name="workflow-validation.xml",
        environment=_gui_environment(config),
    )
