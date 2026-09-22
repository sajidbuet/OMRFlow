"""Level A - the repository's own test suite, run the canonical way.

Purpose:
    Prove the source is sound before anything is built from it. This stage
    deliberately adds no tests of its own: the repository already has a little
    over four thousand, and a release harness that ran a different, smaller set
    would be qualifying something other than what the project tests.

Why it invokes pytest with no path by default:
    ``pyproject.toml`` sets ``testpaths``, ``addopts`` (including
    ``-m "not stress"``) and ``pythonpath``. Passing an explicit path would
    bypass nothing, but passing explicit *options* would - so the canonical
    invocation is the bare one, exactly as CONTRIBUTING.md, docs/TESTING.md and
    the CI workflow all use it.
"""

from __future__ import annotations

from tools.release_validation import config as cfg
from tools.release_validation.pytest_runner import run_pytest
from tools.release_validation.results import StageResult, Status

FAST_SELECTION = ("tests/unit",)
"""What ``--quick`` runs: the unit suite, no Qt, a couple of minutes."""


def run(config: cfg.ValidationConfig, *, quick: bool = False) -> StageResult:
    """Run the repository's suite.

    Args:
        config: The run this stage belongs to.
        quick: Run only ``tests/unit``. For iterating on the framework itself;
            a release must not be qualified on it, and the report says which
            was used.
    """
    selection = FAST_SELECTION if quick else ()
    stage = run_pytest(
        config,
        stage_name="Source tests",
        selection=selection,
        junit_name="pytest-results.xml",
    )
    if quick:
        stage.record(
            "selection",
            Status.WARNING,
            detail="tests/unit only",
            reason="--quick was given; this is not a full source qualification",
        )
    return stage


def run_stress_smoke(config: cfg.ValidationConfig) -> StageResult:
    """The stress-marked suite, which the default run excludes.

    Not part of ``--all``: ``pyproject.toml`` excludes ``-m stress`` from the
    default run because those tests process thousands of sheets, and §5 of the
    framework's brief is explicit that release qualification must not become
    the 100,000-sheet run. Offered here so that ``--stress`` reaches it without
    anyone having to remember the marker.
    """
    return run_pytest(
        config,
        stage_name="Stress suite",
        selection=(),
        junit_name="pytest-stress-results.xml",
        extra_args=("-m", "stress"),
        blocking=False,
    )
