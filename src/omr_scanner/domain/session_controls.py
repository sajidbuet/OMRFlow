"""Operator controls for continuous operation: the vocabulary (0.1.1 revised phase 7).

Purpose:
    Name the operator's **intent** for a scan session's intake and recognition
    (``ARCHITECTURE_NOTES.md`` §14.3) as plain values that are persisted, so a
    restart, a crash or a project reopen restores exactly what the operator
    last asked for - never a default.

The intents:
    * Intake ``on`` / ``paused`` for the whole session, and ``paused`` per
      source. Paused means *not reconciled and not registered from*; nothing on
      disk is lost, and a paused source is not unreachable.
    * Recognition :class:`ProcessingIntent`:

      ``running``  - claim and read work as it becomes ready.
      ``paused``   - claim nothing new; sheets already handed to a worker
                     finish **and are recorded**.
      ``stopped``  - the result of *Finish current and stop* or *Cancel queued
                     work*: like paused, and the engine shuts down once what is
                     current has settled.

      A restart resumes recognition **only** when the persisted intent is
      ``running``. ``paused`` and ``stopped`` survive a restart unchanged until
      an operator resumes.

What does NOT belong here:
    Database, Qt, threads. :mod:`omr_scanner.services.session_controls`
    stores these; :mod:`omr_scanner.services.continuous_engine` obeys them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

CONTROL_ENTITY = "session_control"
"""``audit_event.entity_type`` of control changes (``entity_id``: the session,
or the source for a per-source pause)."""


class ProcessingIntent(StrEnum):
    """What the operator wants recognition to do (stored in ``scan_session.processing_intent``)."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"

    @property
    def claims_work(self) -> bool:
        """Whether new sheets may be claimed under this intent."""
        return self is ProcessingIntent.RUNNING

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            ProcessingIntent.RUNNING: "Processing",
            ProcessingIntent.PAUSED: "Processing paused",
            ProcessingIntent.STOPPED: "Processing stopped",
        }[self]


class ControlAction(StrEnum):
    """``audit_event.action`` values for control changes (20 characters at most)."""

    INTAKE_PAUSED = "intake_paused"
    INTAKE_RESUMED = "intake_resumed"
    SOURCE_PAUSED = "source_paused"
    SOURCE_RESUMED = "source_resumed"
    PROCESSING_PAUSED = "processing_paused"
    PROCESSING_RESUMED = "processing_resumed"
    FINISH_REQUESTED = "finish_requested"
    QUEUE_CANCELLED = "queue_cancelled"


@dataclass(frozen=True, slots=True)
class SessionControls:
    """A session's persisted operator intent, as one immutable value.

    Attributes:
        scan_session_id: The session.
        intake_paused: Intake is paused for every source of the session.
        processing: The recognition intent.
        paused_sources: Sources paused individually (project-level sources;
            only those attached to the session matter to it).
    """

    scan_session_id: str
    intake_paused: bool = False
    processing: ProcessingIntent = ProcessingIntent.RUNNING
    paused_sources: frozenset[str] = frozenset()

    def intake_allowed(self, source_id: str) -> bool:
        """Whether ``source_id`` may be reconciled and registered from now."""
        return not self.intake_paused and source_id not in self.paused_sources

    @property
    def processing_allowed(self) -> bool:
        """Whether new sheets may be claimed."""
        return self.processing.claims_work


__all__ = ["CONTROL_ENTITY", "ControlAction", "ProcessingIntent", "SessionControls"]
