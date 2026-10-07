"""What one intake qualification campaign is (revised phase 9).

Purpose:
    One frozen, JSON-round-trippable description of a campaign - the cohort
    seed, the timing seed, the scale, the engine and intake configuration, the
    planned failures - so a report names exactly what was run and a failure
    can be rerun from it.

Modes:
    * ``self_test`` - minutes, every mechanism once (three sources, partial
      writes, a duplicate, a duplicate ID, a rescan, real process kills). Its
      best verdict is "ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION".
    * ``release`` - the ``ACCEPTANCE_CRITERIA.md`` §5.1 workload: >= 3
      sources, >= 10,000 arrivals. Only this mode can be ``QUALIFIED``, and
      only after :data:`RELEASE_REQUIREMENTS` are re-checked against what was
      actually *measured* (never inferred from the mode name).
    * ``custom`` - anything else; never ``QUALIFIED`` unless it meets the
      release requirements by measurement, and it is labelled custom.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from enum import StrEnum
from typing import Any


class Mode(StrEnum):
    """Which campaign shape was requested."""

    SELF_TEST = "self_test"
    RELEASE = "release"
    CUSTOM = "custom"


RELEASE_MIN_SOURCES = 3
RELEASE_MIN_ARRIVALS = 10_000
RELEASE_MIN_SETS = 2
REQUIRED_KILL_PERCENTS: tuple[int, ...] = (1, 25, 50, 75, 99)
"""``ACCEPTANCE_CRITERIA.md`` §5.4 case 11: resume after interruption at about
these fractions of a large run."""

VERDICT_QUALIFIED = "QUALIFIED"
VERDICT_SMALL = "ALL RUNS PASSED — NOT THE RELEASE QUALIFICATION"
VERDICT_FAILED = "FAILED"
VERDICTS = (VERDICT_QUALIFIED, VERDICT_SMALL, VERDICT_FAILED)

OPERATOR = "Qualification Operator"
"""The scripted operator's name, recorded on every decision it makes."""

SOURCE_LABELS = "ABCDEFGH"


@dataclass(frozen=True, slots=True)
class StabilitySettings:
    """The intake stability policy every simulated source is created with.

    Configuration (``ACCEPTANCE_CRITERIA.md`` D8), recorded in the report;
    the release default is the production local-folder default
    (:data:`omr_scanner.domain.intake.LOCAL_POLICY`).
    """

    min_observations: int = 2
    quiet_seconds: float = 5.0
    max_decode_attempts: int = 3
    retry_backoff_seconds: float = 5.0
    poll_interval_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    """Everything needed to plan, run and reproduce one campaign.

    Attributes:
        mode: The requested shape (:class:`Mode`).
        seed: Cohort seed - candidates, answers, keys, contents, file names,
            source assignment, defects and operator plan. Deterministic.
        timing_seed: Arrival timing and write patterns, separately seeded.
        sources: How many simulated scanner sources (writer processes).
        sets: Logical examination sets.
        candidates_per_set: Roster size per set.
        duration_seconds: Length of the interrupted run's arrival timeline.
        control_duration_seconds: The uninterrupted control run's timeline
            (arrival timing need not match; the logical cohort does).
        workers: Recognition worker processes of the coordinator.
        unit_size / trickle_seconds: The engine's finite-unit policy.
        max_in_flight / claim_window / max_commit_group: Engine limits.
        stability: The sources' stability policy.
        kill_percents: Forced kills when this percent of the run's expected
            sheets are committed (and at least one is in flight).
        after_commit_kill_percent: A kill paused exactly after a work-unit
            commit (case 4); ``0`` disables.
        duplicate_sync_kill_percent: A kill paused after a commit whose
            duplicate-ID pass is still owed (case 13); ``0`` disables.
        operator_kill_percent: A kill while the operator is in the middle of
            a series of Resolve decisions (case 6); ``0`` disables.
        clean_close_percent: A clean application close (cases 1 and 5).
        outage_source / outage_start / outage_end: A source's folder link is
            removed from the filesystem between these fractions of the
            arrival timeline (local source-loss simulation, never SMB).
        checkpoint_percents: Consistent checkpoints (the engine and operator
            hold at a step boundary) at these committed percentages.
        run_control / run_finite: The uninterrupted control campaign and the
            one-batch finite run of the same cohort.
        reprocess: Exercise *Reprocess All* on one plain unit mid-session.
        restart_offline_files: Files the writers must complete while the
            coordinator is down before it is restarted.
        stage_timeout_seconds: Longest any one waited-for state may take.
    """

    mode: Mode = Mode.SELF_TEST
    seed: int = 20261006
    timing_seed: int = 6102026
    sources: int = 3
    sets: tuple[str, ...] = ("A", "B", "C", "D")
    candidates_per_set: int = 30
    duration_seconds: float = 100.0
    control_duration_seconds: float = 40.0
    workers: int = 2
    unit_size: int = 25
    trickle_seconds: float = 4.0
    max_in_flight: int = 16
    claim_window: int = 32
    max_commit_group: int = 25
    stability: StabilitySettings = field(
        default_factory=lambda: StabilitySettings(
            min_observations=2, quiet_seconds=1.2, max_decode_attempts=3,
            retry_backoff_seconds=1.0, poll_interval_seconds=0.5,
        )
    )
    kill_percents: tuple[int, ...] = (25, 75)
    after_commit_kill_percent: int = 35
    duplicate_sync_kill_percent: int = 10
    operator_kill_percent: int = 60
    clean_close_percent: int = 85
    outage_source: str = "B"
    outage_start: float = 0.35
    outage_end: float = 0.55
    checkpoint_percents: tuple[int, ...] = (10, 50, 90, 100)
    run_control: bool = True
    run_finite: bool = True
    reprocess: bool = True
    restart_offline_files: int = 3
    stage_timeout_seconds: float = 900.0

    # ------------------------------------------------------------------
    @property
    def source_labels(self) -> tuple[str, ...]:
        """``("A", "B", "C", ...)``, one per simulated scanner."""
        return tuple(SOURCE_LABELS[: self.sources])

    def to_json(self) -> dict[str, Any]:
        """Plain data, for the manifest and the report."""
        data = asdict(self)
        data["mode"] = self.mode.value
        data["sets"] = list(self.sets)
        data["kill_percents"] = list(self.kill_percents)
        data["checkpoint_percents"] = list(self.checkpoint_percents)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CampaignConfig:
        """The inverse of :meth:`to_json`; unknown keys are refused."""
        known = {item.name for item in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown campaign configuration key(s): {unknown}")
        values = dict(data)
        values["mode"] = Mode(values.get("mode", Mode.CUSTOM.value))
        if "stability" in values:
            values["stability"] = StabilitySettings(**values["stability"])
        for key in ("sets", "kill_percents", "checkpoint_percents"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)

    def dumps(self) -> str:
        """Canonical JSON text."""
        return json.dumps(self.to_json(), sort_keys=True, indent=1)


def self_test_config(**overrides: Any) -> CampaignConfig:
    """The small, CI-suitable harness self-test: every mechanism once, in minutes."""
    return replace(CampaignConfig(mode=Mode.SELF_TEST), **overrides)


def release_config(**overrides: Any) -> CampaignConfig:
    """The ``ACCEPTANCE_CRITERIA.md`` §5.1 release-scale campaign.

    2,560 candidates per set over four sets gives a little over 10,000
    arrivals once byte copies, rescans and the other planted cases are added
    (the planner refuses a release plan below :data:`RELEASE_MIN_ARRIVALS`).
    The stability policy is the production local-folder default.
    """
    base = CampaignConfig(
        mode=Mode.RELEASE,
        candidates_per_set=2_560,
        duration_seconds=2_400.0,
        control_duration_seconds=900.0,
        workers=6,
        unit_size=200,
        trickle_seconds=30.0,
        stability=StabilitySettings(),
        kill_percents=REQUIRED_KILL_PERCENTS,
        after_commit_kill_percent=35,
        duplicate_sync_kill_percent=15,
        operator_kill_percent=60,
        clean_close_percent=85,
        outage_start=0.40,
        outage_end=0.52,
        checkpoint_percents=(1, 10, 25, 50, 75, 90, 100),
        restart_offline_files=10,
        stage_timeout_seconds=3_600.0,
    )
    return replace(base, **overrides)


__all__ = [
    "OPERATOR",
    "RELEASE_MIN_ARRIVALS",
    "RELEASE_MIN_SETS",
    "RELEASE_MIN_SOURCES",
    "REQUIRED_KILL_PERCENTS",
    "VERDICTS",
    "VERDICT_FAILED",
    "VERDICT_QUALIFIED",
    "VERDICT_SMALL",
    "CampaignConfig",
    "Mode",
    "StabilitySettings",
    "release_config",
    "self_test_config",
]
