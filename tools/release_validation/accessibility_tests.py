"""Stage: accessibility checks, reported by severity.

Two things come back from ``suites/test_accessibility.py``: the pass/fail of
its structural assertions, and a JSON Lines file of graded findings. This stage
merges them, so one report row exists per finding as well as per test.

Why the stage is non-blocking by default:
    The project's own position, in ``docs/release/RELEASE_CHECKLIST.md``, is
    that an *initial* accessibility review is sufficient at Alpha and a full
    audit is required from Beta. Making every unnamed combo box release-
    blocking today would either stop every release or teach everyone to pass
    ``--fail-on-warning`` never - both worse than reporting the findings
    honestly and letting the maintainer decide. ``--strict-accessibility``
    makes it blocking for the run where that is wanted.
"""

from __future__ import annotations

import json
from pathlib import Path

from tools.release_validation import config as cfg
from tools.release_validation.pytest_runner import run_pytest, suite_path
from tools.release_validation.results import StageResult, Status

SEVERITY_TO_STATUS = {
    "ERROR": Status.FAIL,
    "WARNING": Status.WARNING,
    "INFO": Status.SKIPPED,
}
"""How a finding's severity appears in the report.

INFO maps to SKIPPED rather than PASS deliberately: an informational note is
not evidence that something was verified, and counting it as a pass would
inflate the totals with observations nobody acted on.
"""


def _findings_path(config: cfg.ValidationConfig) -> Path:
    return config.artifacts_dir / "accessibility-findings.jsonl"


def _load_findings(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    findings: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            findings.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return findings


def run(config: cfg.ValidationConfig, *, strict: bool = False) -> StageResult:
    """Run the accessibility suite and fold its findings into the report."""
    findings_file = _findings_path(config)
    environment = config.child_environment()
    environment["OMRFLOW_VALIDATION_A11Y"] = str(findings_file)
    environment["OMRFLOW_VALIDATION_SCREENSHOTS"] = str(config.screenshots_dir)

    stage = run_pytest(
        config,
        stage_name="Accessibility checks",
        selection=(suite_path("test_accessibility.py"),),
        junit_name="accessibility-validation.xml",
        environment=environment,
        blocking=strict,
    )

    findings = _load_findings(findings_file)
    counts = {"ERROR": 0, "WARNING": 0, "INFO": 0}
    for finding in findings:
        severity = finding.get("severity", "INFO").upper()
        counts[severity] = counts.get(severity, 0) + 1
        stage.record(
            f"[{severity}] {finding.get('category', 'finding')}: {finding.get('message', '')}",
            SEVERITY_TO_STATUS.get(severity, Status.SKIPPED),
            detail=finding.get("where", ""),
            reason="" if severity == "INFO" else finding.get("message", ""),
            artifacts=[f"artifacts/{findings_file.name}"],
        )

    stage.record(
        "accessibility findings recorded",
        Status.PASS,
        detail=(
            f"{counts['ERROR']} error, {counts['WARNING']} warning, "
            f"{counts['INFO']} informational"
        ),
        artifacts=[f"artifacts/{findings_file.name}"] if findings else [],
    )

    if not strict and counts["ERROR"]:
        stage.record(
            "severity policy",
            Status.WARNING,
            detail=f"{counts['ERROR']} accessibility error(s) reported",
            reason=(
                "this stage is non-blocking by default - an Alpha requires only "
                "an initial accessibility review. Re-run with "
                "--strict-accessibility to make these block a release."
            ),
        )

    return stage
