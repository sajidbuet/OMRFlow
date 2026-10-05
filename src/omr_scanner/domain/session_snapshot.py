"""A scan session's progress at one instant: the vocabulary (0.1.1 revised phase 7).

Purpose:
    The immutable values :func:`omr_scanner.services.session_snapshot.take_snapshot`
    returns and phase 8's operational GUI polls (``ARCHITECTURE_NOTES.md``
    §14.1-14.2): counts that **partition** everything discovered, three
    separate progress lines, the session's activity ("Caught up - watching for
    new scans" only under its definition), per-source state and a
    registration-failure rate alarm. Pure: no database.

The partition (every file or sheet in exactly one bucket):

    discovered (excluding ignored)
      = stabilizing + ready + held + vanished + unreadable_pending_decision
                                                    (intake, not registered)
      + queued + processing                         (registered, not read yet)
      + accepted + conflict + rescan_required       (read, effective / listed)
      + superseded + duplicate + excluded + deferred + counted_elsewhere

    ``held`` (arrived for a closed session), ``excluded``, ``deferred`` and
    ``counted_elsewhere`` are buckets the roadmap's illustration did not name;
    they are states that exist, so they are counted, never folded into another
    bucket (which would double-count or hide them).

Three progress lines, never one percentage:
    * Recognition - processed / (discovered - duplicate - vanished): may
      **decrease** when files arrive.
    * Conflict resolution - resolved / required conflicts on effective sheets.
    * Rescan - confirmed replacements / sheets that currently need or needed a
      rescan (rejected awaiting rescan + replaced + unanswered suggestions).

Heuristics here are **operational, not calibrated**: :class:`CaughtUpPolicy`
(how recently a source must have been reconciled) and
:class:`RegistrationAlarmPolicy` (when a source's registration failures look
like a wrong template rather than bad paper). Neither is a recognition
threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum

from omr_scanner.domain.session_controls import SessionControls


class SessionActivity(StrEnum):
    """What a session is doing, in the order the snapshot decides it (first match)."""

    CLOSED = "closed"
    PROCESSING_STOPPED = "processing_stopped"
    PROCESSING_PAUSED = "processing_paused"
    WAITING_FOR_SOURCE = "waiting_for_source"
    """An enabled, unpaused source is unreachable (or was never reachable)."""
    PROCESSING = "processing"
    """Files are stabilising or ready, or sheets are queued or being read."""
    INTAKE_PAUSED = "intake_paused"
    """Nothing left to process, but intake is paused for the session or a source."""
    CHECKING_SOURCES = "checking_sources"
    """Nothing left to process, but an enabled source has not been reconciled
    recently enough to say nothing new has arrived."""
    CAUGHT_UP = "caught_up"
    """Caught up - watching for new scans. **Never** "the examination is complete"."""

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return _ACTIVITY_LABELS[self]


_ACTIVITY_LABELS: dict[SessionActivity, str] = {
    SessionActivity.CLOSED: "Closed",
    SessionActivity.PROCESSING_STOPPED: "Processing stopped",
    SessionActivity.PROCESSING_PAUSED: "Processing paused",
    SessionActivity.WAITING_FOR_SOURCE: "Waiting for a scanner source",
    SessionActivity.PROCESSING: "Processing",
    SessionActivity.INTAKE_PAUSED: "Intake paused",
    SessionActivity.CHECKING_SOURCES: "Checking scanner sources",
    SessionActivity.CAUGHT_UP: "Caught up - watching for new scans",
}


@dataclass(frozen=True, slots=True)
class Partition:
    """Every discovered (non-ignored) file or sheet of the session, counted once."""

    stabilizing: int = 0
    ready: int = 0
    held: int = 0
    vanished: int = 0
    unreadable_pending_decision: int = 0
    queued: int = 0
    processing: int = 0
    accepted: int = 0
    conflict: int = 0
    rescan_required: int = 0
    superseded: int = 0
    duplicate: int = 0
    excluded: int = 0
    deferred: int = 0
    counted_elsewhere: int = 0

    @property
    def total(self) -> int:
        """The sum of every bucket - must equal ``discovered_excluding_ignored``."""
        return sum(int(getattr(self, item.name)) for item in fields(self))

    def as_dict(self) -> dict[str, int]:
        """``bucket -> count``, in declaration order."""
        return {item.name: int(getattr(self, item.name)) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class Progress:
    """One progress line: ``done`` of ``total``. ``fraction`` is ``None`` with nothing to do."""

    done: int
    total: int

    @property
    def fraction(self) -> float | None:
        """``done / total``, or ``None`` when ``total`` is zero."""
        return None if self.total <= 0 else self.done / self.total


@dataclass(frozen=True, slots=True)
class CaughtUpPolicy:
    """How recently an enabled source must have been reconciled for "caught up".

    A source counts as checked when its last **successful** reconciliation is
    no older than ``factor x poll interval + slack_seconds``. Defaults: twice
    the source's own poll interval plus 5 s - an **operational heuristic, not
    calibrated** on a real share.
    """

    factor: float = 2.0
    slack_seconds: float = 5.0

    def allowance_seconds(self, poll_interval_seconds: float) -> float:
        """How old a successful reconciliation may be."""
        return self.factor * max(0.0, poll_interval_seconds) + self.slack_seconds


DEFAULT_CAUGHT_UP_POLICY = CaughtUpPolicy()


@dataclass(frozen=True, slots=True)
class RegistrationAlarmPolicy:
    """When a source's registration failures suggest a systematic cause.

    ``ARCHITECTURE_NOTES.md`` §12: a wrong template fails *every* sheet, so a
    source's registration failures deserve a **rate** alarm - never a
    single-sheet one. Over the source's most recent ``window`` read sheets, the
    alarm is raised when at least ``min_samples`` were read and at least
    ``alarm_fraction`` of them failed registration.

    Defaults (20 / 5 / 0.5) are an **operational heuristic, not calibrated**
    against real scanning rooms; they are not recognition thresholds and change
    no decision about any sheet.
    """

    window: int = 20
    min_samples: int = 5
    alarm_fraction: float = 0.5

    def __post_init__(self) -> None:
        """Refuse a policy that could alarm on nothing or never."""
        if self.window < 1 or self.min_samples < 1 or self.min_samples > self.window:
            raise ValueError("RegistrationAlarmPolicy needs 1 <= min_samples <= window")
        if not 0.0 < self.alarm_fraction <= 1.0:
            raise ValueError("RegistrationAlarmPolicy.alarm_fraction must lie in (0, 1]")

    def evaluate(self, samples: int, failures: int) -> RegistrationAlarm:
        """The alarm state for ``failures`` of the latest ``samples`` (at most ``window``)."""
        fraction = failures / samples if samples else 0.0
        return RegistrationAlarm(
            window=self.window,
            samples=samples,
            failures=failures,
            fraction=fraction,
            raised=samples >= self.min_samples and fraction >= self.alarm_fraction,
        )


DEFAULT_ALARM_POLICY = RegistrationAlarmPolicy()


@dataclass(frozen=True, slots=True)
class RegistrationAlarm:
    """A source's recent registration-failure rate and whether it alarms."""

    window: int
    samples: int
    failures: int
    fraction: float
    raised: bool


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """One intake source's state within the session (bounded: counts, never file lists).

    Attributes:
        source_id / label / kind: Identity.
        enabled: Configured on.
        intake_paused: Paused - by itself, or because the session's intake is.
        reachability: The last listing's outcome (``online``, ``unreachable``...).
        reachable: ``reachability`` is ``online``.
        last_reconciled_at: The last **successful** reconciliation.
        reconciled_recently: Within the :class:`CaughtUpPolicy` allowance.
        stabilizing / ready / held / unreadable / vanished: Its unregistered files.
        registered: Its sheets registered into the session.
        processed: Of those, read (completed, warning or failed).
        accepted / conflict / rescan_required: Its registered sheets in those
            partition buckets (the session's own classification, grouped by
            source - revised phase 8).
        duplicate: Its exact-duplicate files - registered duplicates plus
            ledger rows linked as duplicate content.
        recent_processed: Read within the rate window.
        rate_per_minute: ``recent_processed`` per minute of the window, or
            ``None`` before anything was read.
        alarm: The registration-failure rate alarm.
    """

    source_id: str
    label: str
    kind: str
    enabled: bool
    intake_paused: bool
    reachability: str
    reachable: bool
    last_reconciled_at: datetime | None
    reconciled_recently: bool
    stabilizing: int = 0
    ready: int = 0
    held: int = 0
    unreadable: int = 0
    vanished: int = 0
    registered: int = 0
    processed: int = 0
    accepted: int = 0
    conflict: int = 0
    rescan_required: int = 0
    duplicate: int = 0
    recent_processed: int = 0
    rate_per_minute: float | None = None
    alarm: RegistrationAlarm | None = None

    @property
    def problems(self) -> int:
        """Files awaiting an operator decision (held or unreadable/unsupported)."""
        return self.held + self.unreadable


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """One scan session at one instant. Immutable; from bounded grouped queries.

    Attributes:
        scan_session_id / session_state: The session and ``open`` / ``closed``.
        taken_at: When.
        activity: What it is doing (:class:`SessionActivity`).
        caught_up: ``activity is CAUGHT_UP`` - open, nothing stabilising, ready,
            queued or processing, every enabled unpaused source reachable and
            reconciled recently, processing running, intake on.
        partition: Every discovered non-ignored file or sheet, once.
        discovered_excluding_ignored: Counted independently of the partition;
            the two must agree (asserted by the tests and the campaign).
        ignored: Ledger rows ignored (temporary names, exclusions, unchanged
            content, operator-dismissed files).
        recognition / conflicts / rescans: The three progress lines.
        outstanding_suggestions: Suggested rescans no operator has answered.
        retry_processing: Read sheets whose reading failed in software (the
            remedy is to read them again, not to rescan the paper).
        pending_decisions: Held + unreadable/unsupported files (operator queue).
        controls: The persisted operator intent.
        sources: Per-source state, oldest source first.
        disabled_sources / unreachable_sources: Labels, for the operator.
        running_batches: Units still ``running`` (in flight, or a coordinator
            died before finishing their cross-sheet pass).
        query_count: How many SQL statements produced this snapshot.
    """

    scan_session_id: str
    session_state: str
    taken_at: datetime
    activity: SessionActivity
    caught_up: bool
    partition: Partition
    discovered_excluding_ignored: int
    ignored: int
    recognition: Progress
    conflicts: Progress
    rescans: Progress
    outstanding_suggestions: int
    retry_processing: int
    pending_decisions: int
    controls: SessionControls
    sources: tuple[SourceSnapshot, ...]
    disabled_sources: tuple[str, ...]
    unreachable_sources: tuple[str, ...]
    running_batches: int
    query_count: int = 0

    @property
    def partitions(self) -> bool:
        """Whether the buckets add up to what was discovered (they must)."""
        return self.partition.total == self.discovered_excluding_ignored


__all__ = [
    "DEFAULT_ALARM_POLICY",
    "DEFAULT_CAUGHT_UP_POLICY",
    "CaughtUpPolicy",
    "Partition",
    "Progress",
    "RegistrationAlarm",
    "RegistrationAlarmPolicy",
    "SessionActivity",
    "SessionSnapshot",
    "SourceSnapshot",
]
