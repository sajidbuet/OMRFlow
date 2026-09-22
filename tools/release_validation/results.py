"""The result model every stage reports into.

Purpose:
    Give the whole framework one vocabulary for "what happened". A stage
    returns a :class:`StageResult` holding :class:`CheckResult` rows; the
    reporter renders them and the exit code is derived from them. Nothing
    prints its own verdict, so the console summary, the Markdown report and the
    JSON can never disagree.

Why WARNING is not a failure by default:
    A qualification run that treats every imperfection as release-blocking gets
    ignored, and an ignored gate is worse than no gate. A warning is something
    a person should read; ``--fail-on-warning`` is there for the run where that
    distinction is not wanted.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """How one check or stage ended.

    A :class:`~enum.StrEnum` so that it serialises to JSON as its own name
    without a custom encoder.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"
    SKIPPED = "SKIPPED"

    @property
    def is_blocking(self) -> bool:
        """Whether this status alone should stop a release."""
        return self is Status.FAIL


@dataclass
class CheckResult:
    """One thing that was checked, and what came of it.

    Attributes:
        name: What was checked, phrased so a reader who has not seen the code
            can tell what failed.
        status: The verdict.
        duration_seconds: How long it took. Useful for spotting a check that
            passed only because it timed out into a default.
        detail: The observed value - a version string, a path, a count. Present
            on a pass as well, because "passed, and here is what it saw" is far
            more useful than a bare tick.
        reason: Why it failed or was skipped. Required for anything that is not
            a pass; a skip with no reason is indistinguishable from a check
            nobody wrote.
        exception: Formatted traceback, when one was raised.
        artifacts: Paths, relative to the run's output directory, that a reader
            needs in order to investigate - a screenshot, a log, a JUnit file.
    """

    name: str
    status: Status
    duration_seconds: float = 0.0
    detail: str = ""
    reason: str = ""
    exception: str = ""
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """This check as plain data, for the JSON report."""
        return {
            "name": self.name,
            "status": self.status.value,
            "duration_seconds": round(self.duration_seconds, 3),
            "detail": self.detail,
            "reason": self.reason,
            "exception": self.exception,
            "artifacts": list(self.artifacts),
        }


@dataclass
class StageResult:
    """Every check belonging to one qualification stage.

    Attributes:
        name: The stage's name as it appears in the console summary.
        blocking: Whether a failure here should stop a release. A stage that is
            informational - an optional extra check - can be non-blocking, and
            the exit code will ignore its failures while the report still shows
            them.
        skipped_reason: Set when the stage did not run at all, which is
            different from running and finding nothing to do.
    """

    name: str
    checks: list[CheckResult] = field(default_factory=list)
    blocking: bool = True
    skipped_reason: str = ""
    duration_seconds: float = 0.0

    # ------------------------------------------------------------- recording
    def record(
        self,
        name: str,
        status: Status,
        *,
        detail: str = "",
        reason: str = "",
        duration_seconds: float = 0.0,
        artifacts: list[str] | None = None,
    ) -> CheckResult:
        """Record one check with an explicit status."""
        check = CheckResult(
            name=name,
            status=status,
            detail=detail,
            reason=reason,
            duration_seconds=duration_seconds,
            artifacts=artifacts or [],
        )
        self.checks.append(check)
        return check

    def ok(
        self,
        name: str,
        passed: bool,
        *,
        detail: str = "",
        reason: str = "",
        artifacts: list[str] | None = None,
    ) -> CheckResult:
        """Record a boolean check.

        ``reason`` is what a reader is told when ``passed`` is false; when it is
        empty the detail is used, because a failure with neither is useless.
        """
        return self.record(
            name,
            Status.PASS if passed else Status.FAIL,
            detail=detail,
            reason="" if passed else (reason or detail or "condition not met"),
            artifacts=artifacts,
        )

    def warn(self, name: str, *, detail: str = "", reason: str = "") -> CheckResult:
        """Record something a person should read but that does not block."""
        return self.record(name, Status.WARNING, detail=detail, reason=reason)

    def skip(self, name: str, reason: str) -> CheckResult:
        """Record something deliberately not done, and why."""
        return self.record(name, Status.SKIPPED, reason=reason)

    def fail(
        self, name: str, reason: str, *, exception: BaseException | None = None
    ) -> CheckResult:
        """Record a failure, optionally carrying the exception that caused it."""
        check = self.record(name, Status.FAIL, reason=reason)
        if exception is not None:
            check.exception = "".join(
                traceback.format_exception(type(exception), exception, exception.__traceback__)
            )
        return check

    # --------------------------------------------------------------- summary
    def count(self, status: Status) -> int:
        """How many of this stage's checks ended with ``status``."""
        return sum(1 for check in self.checks if check.status is status)

    @property
    def status(self) -> Status:
        """The stage's own verdict, derived from its checks.

        A stage that was skipped outright reports SKIPPED even if it holds no
        checks; a stage with no checks and no reason is a programming mistake
        and reports SKIPPED with that said plainly by the reporter.
        """
        if self.skipped_reason:
            return Status.SKIPPED
        if any(check.status is Status.FAIL for check in self.checks):
            return Status.FAIL
        if not self.checks:
            return Status.SKIPPED
        if any(check.status is Status.WARNING for check in self.checks):
            return Status.WARNING
        if all(check.status is Status.SKIPPED for check in self.checks):
            return Status.SKIPPED
        return Status.PASS

    @property
    def is_release_blocking(self) -> bool:
        """Whether this stage alone should stop a release."""
        return self.blocking and self.status is Status.FAIL

    def to_dict(self) -> dict[str, Any]:
        """This stage and its checks as plain data."""
        return {
            "name": self.name,
            "status": self.status.value,
            "blocking": self.blocking,
            "skipped_reason": self.skipped_reason,
            "duration_seconds": round(self.duration_seconds, 3),
            "counts": {
                status.value: self.count(status)
                for status in (Status.PASS, Status.FAIL, Status.WARNING, Status.SKIPPED)
            },
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass
class RunResult:
    """Every stage of one qualification run."""

    run_id: str
    stages: list[StageResult] = field(default_factory=list)
    started: str = ""
    finished: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    command_line: str = ""

    def add(self, stage: StageResult) -> StageResult:
        """Append a finished stage, and return it for convenience."""
        self.stages.append(stage)
        return stage

    def count(self, status: Status) -> int:
        """How many checks across the whole run ended with ``status``."""
        return sum(stage.count(status) for stage in self.stages)

    @property
    def blocking_failures(self) -> list[StageResult]:
        """The stages whose failure stops a release."""
        return [stage for stage in self.stages if stage.is_release_blocking]

    HOUSEKEEPING = frozenset({"Environment", "Cleanup"})
    """Stages that run every time and verify nothing about the release itself."""

    @property
    def substantive_stages(self) -> list[StageResult]:
        """The stages that actually qualify something."""
        return [stage for stage in self.stages if stage.name not in self.HOUSEKEEPING]

    @property
    def verified_anything(self) -> bool:
        """Whether at least one qualifying stage actually ran.

        A run whose only requested stage was skipped - no installer present, no
        desktop, nothing installed - has verified nothing, and must not be
        reported as qualified. A step that was not run is not a pass, and an
        exit code of 0 would say it was.
        """
        return any(
            stage.status is not Status.SKIPPED for stage in self.substantive_stages
        )

    def qualified(self, *, fail_on_warning: bool) -> bool:
        """Whether this run permits a release.

        Warnings count only when the operator asked them to; a non-blocking
        stage's failure never does, which is what makes it non-blocking. A run
        that verified nothing is never qualified - see :attr:`verified_anything`.
        """
        if not self.verified_anything:
            return False
        if self.blocking_failures:
            return False
        return not (fail_on_warning and self.count(Status.WARNING) > 0)

    def to_dict(self) -> dict[str, Any]:
        """The whole run as plain data, for ``validation-results.json``."""
        return {
            "schema": "omrflow.release-validation/1",
            "run_id": self.run_id,
            "started": self.started,
            "finished": self.finished,
            "command_line": self.command_line,
            "environment": self.environment,
            "totals": {
                status.value: self.count(status)
                for status in (Status.PASS, Status.FAIL, Status.WARNING, Status.SKIPPED)
            },
            "blocking_failures": [stage.name for stage in self.blocking_failures],
            "verified_anything": self.verified_anything,
            "stages": [stage.to_dict() for stage in self.stages],
        }
