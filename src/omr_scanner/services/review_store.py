"""Conflicts, human decisions and the provenance ledger (Phase 6).

Purpose:
    Own every write that can change what a value *means* - and make sure each
    one is atomic, named, reasoned and recorded.

Responsibilities:
    * :func:`sync_conflicts` / :func:`sync_duplicate_identifiers` - idempotent
      detection into storage.
    * :func:`accept_machine_value`, :func:`correct_value`, :func:`defer`,
      :func:`reopen` - the four human actions, each one transaction.
    * :func:`undo_decision` / :func:`undo_resolved_sheet` - taking a decision,
      or a whole sheet's worth of them, back again. Also one transaction each,
      and also append-only.
    * :func:`provenance_for` / :func:`effective_values_for_scan` - the single
      answer to "what is this value, and where did it come from".
    * :func:`history_for` - the ledger, oldest first.
    * :func:`list_conflicts` / :func:`count_conflicts` - what the queue reads.

What does NOT belong here:
    * Qt, images, or recognition. Detection *policy* is
      :mod:`omr_scanner.services.conflict_policy`; this module stores what that
      module decided.

The three rules this module enforces, and how:

    **The machine observation is never lost.** A human action writes an
    :class:`~omr_scanner.database.models.AuditEvent` and updates the conflict's
    ``state``. It never writes ``machine_value``. The only code that touches
    those columns is :func:`sync_conflicts`, and when a re-read changes them it
    appends a ``RE_RECOGNISED`` event first, so even the machine cannot revise
    itself silently.

    **A decision is atomic.** Each action runs inside one
    :meth:`~omr_scanner.database.engine.ProjectDatabase.session`, which commits
    on success and rolls back on any failure. An event without its state change
    - or a state change without its event - cannot be committed.

    **The ledger is append-only.** This module exposes :func:`append_event` and
    nothing that updates or deletes one, and migration 3 installs SQLite
    triggers that abort any attempt. See
    :data:`~omr_scanner.database.migrations.AUDIT_IMMUTABILITY_TRIGGERS`.

Why the effective value is a projection:
    :func:`provenance_for` folds a conflict's ordered events rather than
    reading a cached "final value" column. ``REOPENED`` therefore *unsets* the
    decision by construction, a second correction supersedes the first without
    editing it, and there is no way for a cached value to drift from the
    history that justifies it. ``ReviewConflict.state`` is cached for queue
    speed, and :func:`recompute_state` exists so a test can prove the two agree.

How undo works without rewriting anything:
    :func:`standing_commands` folds the ledger into the stack of human commands
    currently *in effect*: a decision pushes, an ``UNDONE`` event pops. Undo is
    therefore a new event like any other - the reversed decision keeps its own
    reviewer, reason and timestamp - and both the effective value and the
    cached state are still derived from the same single fold. Redo has no event
    of its own on purpose: re-deciding is a decision, and recording it as one
    keeps the ledger a record of what people chose rather than of which buttons
    they pressed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import func, select

from omr_scanner.database.models import AuditEvent, BatchScan, ReviewConflict, ScanRejection
from omr_scanner.domain.review import (
    REOPENED_LABEL,
    REOPENED_MARKER,
    RESOLUTION_TYPES,
    WHOLE_FIELD,
    Candidate,
    ConflictState,
    ConflictType,
    FieldKind,
    FieldRef,
    MachineObservation,
    Provenance,
    ReasonCode,
    ReviewAction,
    ReviewCounts,
    ValueSource,
)
from omr_scanner.domain.scan_lifecycle import LifecycleState
from omr_scanner.domain.template import (
    GridFieldDefinition,
    QuestionBlockFieldDefinition,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.recognition.models import UNRESOLVED_CHARACTER
from omr_scanner.services.conflict_policy import (
    DetectedConflict,
    detect_conflicts,
    detect_duplicate_identifiers,
    join_field_value,
    machine_field_symbols,
)
from omr_scanner.services.recognition_models import ScanResult
from omr_scanner.services.scan_export import SheetResolution

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.conflict_policy import ConflictPolicy

_LOGGER = logging.getLogger(__name__)


class ReviewError(OMRScannerError):
    """A review action was refused: no reviewer, no reason, or a bad transition.

    Deliberately an :class:`~omr_scanner.errors.OMRScannerError` so the GUI's
    existing :func:`~omr_scanner.gui.error_reporting.report_error` presents it
    like any other refusal, and so nothing has to catch a bare ``ValueError``
    and guess what it meant.
    """


def _now() -> datetime:
    """Current UTC time. One place, so every timestamp agrees."""
    return datetime.now(UTC)


# ----------------------------------------------------------------------
# Which stored rows are still conflicts
# ----------------------------------------------------------------------
_RESOLUTION_TYPE_VALUES: tuple[str, ...] = tuple(item.value for item in RESOLUTION_TYPES)
"""The stored ``conflict_type`` values that belong in the resolution queue.

Derived from :data:`~omr_scanner.domain.review.RESOLUTION_TYPES`, so the SQL
and the domain cannot disagree about what a conflict is."""


def _resolution_only(statement: Any) -> Any:
    """Restrict a select over conflicts to the ones needing resolution.

    **Applied to every read that counts, lists or blocks on conflicts**, which
    is what makes an older project behave like a new one the moment it is
    opened - without rewriting a single row of it.

    A project scanned before answer ambiguity stopped being a conflict may hold
    thousands of ``answer_*`` rows. They are evidence, and a decision somebody
    recorded on one is still theirs, so nothing here deletes them; they simply
    do not appear in the working queue, do not inflate any count, and do not
    stop a batch proceeding. A re-read of the sheet withdraws the untouched
    ones in the ordinary way, because detection no longer produces them.

    Their *decisions* are still honoured - see :func:`sheet_resolutions` and
    :func:`effective_answers`, which deliberately do not use this filter when
    applying what a reviewer already decided. Dropping a correction a person
    made would be the one genuinely destructive reading of this change.
    """
    return statement.where(ReviewConflict.conflict_type.in_(_RESOLUTION_TYPE_VALUES))


def _ineligible_scans_select() -> Any:
    """Select every scan that must not contribute to a result.

    Rejected, superseded and re-imported scans - see
    :mod:`omr_scanner.services.scan_lifecycle`, which owns the table. Read
    here directly, as a SQL predicate, so the queue stays one query.
    """
    return select(ScanRejection.scan_id).where(
        ScanRejection.state != LifecycleState.ACTIVE.value
    )


def _ineligible_scans() -> Any:
    """:func:`_ineligible_scans_select` as a scalar subquery, for ``NOT IN``."""
    return _ineligible_scans_select().scalar_subquery()


def _active_scans_only(statement: Any) -> Any:
    """Restrict a select over conflicts to scans that are still in play.

    A rejected scan's conflicts are **not** withdrawn, resolved or rewritten -
    its evidence and every decision on it are needed for audit and for *Undo
    Reject*, which must restore the sheet exactly as it was. They simply leave
    the working queue and every count while the scan is rejected, the way a
    legacy answer conflict does (:func:`_resolution_only`).
    """
    return statement.where(ReviewConflict.scan_id.not_in(_ineligible_scans()))


# ----------------------------------------------------------------------
# Serialising the machine's candidate list
# ----------------------------------------------------------------------
def _dump_candidates(candidates: Sequence[Candidate]) -> str:
    """Serialise ranked candidates for storage. ``""`` when there are none."""
    if not candidates:
        return ""
    return json.dumps(
        [
            {"label": item.label, "fill": round(item.fill_ratio, 6), "sel": item.selected}
            for item in candidates
        ],
        separators=(",", ":"),
    )


def _load_candidates(payload: str) -> tuple[Candidate, ...]:
    """Rebuild candidates from storage, tolerating a legacy or damaged value."""
    if not payload:
        return ()
    try:
        raw = json.loads(payload)
    except (ValueError, TypeError):
        _LOGGER.warning("Stored candidate evidence could not be decoded; ignoring it")
        return ()
    return tuple(
        Candidate(
            label=str(item.get("label", "")),
            fill_ratio=float(item.get("fill", 0.0)),
            selected=bool(item.get("sel", False)),
        )
        for item in raw
        if isinstance(item, dict)
    )


def _dump_related(scan_ids: Sequence[int]) -> str:
    """Serialise the other scans a batch-scope conflict concerns."""
    return ",".join(str(item) for item in scan_ids)


def _load_related(payload: str) -> tuple[int, ...]:
    """Rebuild those scan ids, skipping anything unparseable."""
    if not payload:
        return ()
    found: list[int] = []
    for chunk in payload.split(","):
        try:
            found.append(int(chunk))
        except ValueError:  # pragma: no cover - defensive
            continue
    return tuple(found)


# ----------------------------------------------------------------------
# The read model
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """One stored conflict, as everything above this layer sees it.

    A plain value object rather than the ORM row, so the GUI can hold a queue
    of ten thousand of these without holding ten thousand attached SQLAlchemy
    instances - and so nothing outside this module has an object it could
    accidentally mutate and flush.

    Attributes:
        conflict_id: Stable identity.
        batch_id: The batch it belongs to.
        scan_id: The sheet it belongs to.
        scan_name: That sheet's file name, for a queue row.
        identifier_value: The sheet's recognised identifier, for a queue row.
        conflict_type: Why a human is needed.
        state: Where it is in its review life.
        severity: Queue ordering only.
        field: Which response group.
        observation: What the machine saw, verbatim.
        related_scan_ids: Other sheets a batch-scope conflict concerns.
        created_at / updated_at: ISO-8601 UTC.
        reversed_before: Whether anybody has ever reopened or undone a decision
            on this conflict. Presentation only - it lets the queue distinguish
            "nobody has looked at this" from "somebody decided and then took it
            back", which :class:`~omr_scanner.domain.review.ConflictState`
            cannot, because both are ``OPEN``.
    """

    conflict_id: int
    batch_id: str
    scan_id: int
    scan_name: str
    identifier_value: str
    conflict_type: ConflictType
    state: ConflictState
    severity: int
    field: FieldRef
    observation: MachineObservation
    related_scan_ids: tuple[int, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    reversed_before: bool = False

    @property
    def allows_value_correction(self) -> bool:
        """Whether a reviewer may supply a replacement value here."""
        return self.conflict_type.allows_value_correction

    @property
    def state_label(self) -> str:
        """The state as a queue cell should name it.

        ``"Reopened"`` for a conflict that is open *because somebody put it
        back*. It is still :attr:`~omr_scanner.domain.review.ConflictState.OPEN`
        in storage and in every count - reopening does not invent a sixth
        state - but a reviewer working a queue needs to see that this one has
        already been through somebody's hands.
        """
        if self.state is ConflictState.OPEN and self.reversed_before:
            return REOPENED_LABEL
        return self.state.label

    @property
    def state_marker(self) -> str:
        """The glyph that carries :attr:`state_label` without relying on colour."""
        if self.state is ConflictState.OPEN and self.reversed_before:
            return REOPENED_MARKER
        return self.state.marker


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One ledger entry, as the history view reads it.

    Attributes:
        event_id: Stable identity, ascending with time.
        occurred_at: ISO-8601 UTC.
        action: What happened.
        reviewer: Who did it; empty for machine-authored events.
        previous_value / new_value: The effective value before and after.
        machine_value: What recognition read, repeated on every event so one
            row is self-describing in an exported ledger.
        reason_code / reason_text: Why.
        detail: A sentence for machine-authored events.
    """

    event_id: int
    occurred_at: str
    action: ReviewAction
    reviewer: str = ""
    previous_value: str = ""
    new_value: str = ""
    machine_value: str = ""
    reason_code: str = ""
    reason_text: str = ""
    detail: str = ""

    @property
    def reason_label(self) -> str:
        """The reason's display wording, or free text, or ``""``."""
        if not self.reason_code:
            return self.reason_text
        try:
            label = ReasonCode(self.reason_code).label
        except ValueError:  # pragma: no cover - a code from a newer build
            return self.reason_text or self.reason_code
        return f"{label} - {self.reason_text}" if self.reason_text else label


def _to_record(
    row: ReviewConflict, scan: BatchScan | None, *, reversed_before: bool = False
) -> ConflictRecord:
    """Project one ORM row (and its sheet) into a detached value object."""
    return ConflictRecord(
        conflict_id=row.conflict_id,
        batch_id=row.batch_id,
        scan_id=row.scan_id,
        scan_name=scan.filename if scan is not None else "",
        identifier_value=scan.identifier_value if scan is not None else "",
        conflict_type=ConflictType(row.conflict_type),
        state=ConflictState(row.state),
        severity=row.severity,
        field=FieldRef(
            zone_id=row.zone_id,
            group_key=row.group_key,
            kind=FieldKind(row.field_kind),
            label=row.field_label,
            question_number=row.question_number,
        ),
        observation=MachineObservation(
            value=row.machine_value,
            status=row.machine_status,
            confidence=row.machine_confidence,
            top_fill=row.machine_top_fill,
            margin=row.machine_margin,
            candidates=_load_candidates(row.machine_candidates),
            detail=row.machine_detail,
        ),
        related_scan_ids=_load_related(row.related_scan_ids),
        created_at=row.created_at.isoformat(timespec="seconds"),
        updated_at=row.updated_at.isoformat(timespec="seconds"),
        reversed_before=reversed_before,
    )


def _to_audit(row: AuditEvent) -> AuditRecord:
    """Project one ledger row into a detached value object."""
    return AuditRecord(
        event_id=row.event_id,
        occurred_at=row.occurred_at.isoformat(timespec="seconds"),
        action=ReviewAction(row.action),
        reviewer=row.reviewer,
        previous_value=row.previous_value,
        new_value=row.new_value,
        machine_value=row.machine_value,
        reason_code=row.reason_code,
        reason_text=row.reason_text,
        detail=row.detail,
    )


# ----------------------------------------------------------------------
# Appending to the ledger
# ----------------------------------------------------------------------
def _append_event(
    session: Session,
    *,
    conflict: ReviewConflict,
    action: ReviewAction,
    reviewer: str = "",
    previous_value: str = "",
    new_value: str = "",
    reason_code: str = "",
    reason_text: str = "",
    detail: str = "",
) -> AuditEvent:
    """Append one ledger entry inside an open transaction.

    The only way anything in this application writes an
    :class:`~omr_scanner.database.models.AuditEvent`. There is deliberately no
    ``update_event`` or ``delete_event`` beside it; see the module docstring.
    """
    event = AuditEvent(
        occurred_at=_now(),
        batch_id=conflict.batch_id,
        scan_id=conflict.scan_id,
        conflict_id=conflict.conflict_id,
        action=action.value,
        reviewer=reviewer,
        previous_value=previous_value,
        new_value=new_value,
        machine_value=conflict.machine_value,
        reason_code=reason_code,
        reason_text=reason_text,
        detail=detail,
    )
    session.add(event)
    # Flushed so the caller sees `event_id`, and so a failure surfaces here -
    # inside the transaction - rather than at commit time when the state change
    # has already been made.
    session.flush()
    return event


# ----------------------------------------------------------------------
# Detection into storage
# ----------------------------------------------------------------------
def sync_conflicts(
    database: ProjectDatabase,
    *,
    batch_id: str,
    scan_id: int,
    result: ScanResult,
    template: OmrTemplate,
    policy: ConflictPolicy | None = None,
) -> int:
    """Store the conflicts one finished sheet implies. Idempotent.

    Args:
        database: The open project database.
        batch_id: The batch the sheet belongs to.
        scan_id: The sheet's durable id.
        result: What recognition produced.
        template: The template it was read with.
        policy: What deserves review; defaults apply when omitted.

    Returns:
        How many conflicts this sheet now has in a non-withdrawn state.
        Legacy answer rows are not counted; see :func:`_resolution_only`.

    Called once per finished sheet, from the coordinating process. Running it
    again on the same sheet - a Phase 5 retry, a resumed batch, a re-review -
    updates the existing rows rather than creating a second set, because a
    conflict's identity is ``(batch, scan, type, zone, group)``.

    Three cases, and the rule that distinguishes them:

    * **Still detected, machine observation unchanged** - nothing is written.
    * **Still detected, observation changed** (a retry read the sheet
      differently) - the observation is updated *and* a ``RE_RECOGNISED`` event
      records what it was before. The machine does not get to revise itself
      silently either.
    * **No longer detected** - the conflict is withdrawn, **unless a human has
      already acted on it**. A machine may withdraw its own complaint; it may
      not erase a person's decision, so a resolved or deferred conflict keeps
      its state and its history whatever a later read says.
    """
    detected = detect_conflicts(result, template, policy=policy)
    by_key = {item.key: item for item in detected}
    moment = _now()

    with database.session() as session:
        existing = session.scalars(
            select(ReviewConflict)
            .where(ReviewConflict.batch_id == batch_id)
            .where(ReviewConflict.scan_id == scan_id)
            # A batch-scope duplicate conflict belongs to the batch pass, not to
            # this sheet's own re-read, and must not be withdrawn by it.
            .where(ReviewConflict.conflict_type != ConflictType.IDENTIFIER_DUPLICATE.value)
        ).all()
        existing_by_key = {
            (row.conflict_type, row.zone_id, row.group_key): row for row in existing
        }

        for key, found in by_key.items():
            row = existing_by_key.get(key)
            if row is None:
                _insert_conflict(session, batch_id, scan_id, found, moment)
            else:
                _refresh_conflict(session, row, found, moment)

        for key, row in existing_by_key.items():
            if key in by_key or row.state is ConflictState.WITHDRAWN.value:
                continue
            if ConflictState(row.state).is_human_touched:
                continue
            _withdraw_conflict(session, row, moment)

        session.flush()
        return int(
            session.scalar(
                _resolution_only(
                    select(func.count())
                    .select_from(ReviewConflict)
                    .where(ReviewConflict.batch_id == batch_id)
                    .where(ReviewConflict.scan_id == scan_id)
                    .where(ReviewConflict.state != ConflictState.WITHDRAWN.value)
                )
            )
            or 0
        )


def _insert_conflict(
    session: Session,
    batch_id: str,
    scan_id: int,
    found: DetectedConflict,
    moment: datetime,
) -> ReviewConflict:
    """Create a conflict and open its history with a ``DETECTED`` event."""
    observation = found.observation
    row = ReviewConflict(
        batch_id=batch_id,
        scan_id=scan_id,
        conflict_type=found.conflict_type.value,
        scope=found.conflict_type.scope.value,
        severity=found.severity,
        state=ConflictState.OPEN.value,
        zone_id=found.field.zone_id,
        group_key=found.field.group_key,
        field_kind=found.field.kind.value,
        field_label=found.field.label,
        question_number=found.field.question_number,
        machine_value=observation.value,
        machine_status=observation.status,
        machine_confidence=observation.confidence,
        machine_top_fill=observation.top_fill,
        machine_margin=observation.margin,
        machine_candidates=_dump_candidates(observation.candidates),
        machine_detail=observation.detail,
        related_scan_ids=_dump_related(found.related_scan_ids),
        created_at=moment,
        updated_at=moment,
    )
    session.add(row)
    # Flushed to obtain `conflict_id` before the event that references it.
    session.flush()
    _append_event(
        session,
        conflict=row,
        action=ReviewAction.DETECTED,
        new_value=observation.value,
        detail=observation.detail or found.conflict_type.label,
    )
    return row


def _refresh_conflict(
    session: Session, row: ReviewConflict, found: DetectedConflict, moment: datetime
) -> None:
    """Update a conflict whose sheet has been read again.

    Writes nothing when the observation is identical, so an idempotent re-sync
    leaves no trace in the ledger - which is what lets a resumed batch call this
    for every sheet without filling the history with noise.
    """
    _refresh_observation(session, row, found.observation, moment)


def _refresh_observation(
    session: Session, row: ReviewConflict, observation: MachineObservation, moment: datetime
) -> None:
    """Bring a row's machine observation up to date, recording any change.

    Shared by detection and by :func:`_override_row`, so a reused override
    record whose sheet has since been read differently says so in its history
    exactly as a detected conflict would.
    """
    unchanged = (
        row.machine_value == observation.value
        and row.machine_status == observation.status
        and row.machine_confidence == observation.confidence
        and row.machine_candidates == _dump_candidates(observation.candidates)
    )
    if unchanged:
        return

    previous = row.machine_value
    _append_event(
        session,
        conflict=row,
        action=ReviewAction.RE_RECOGNISED,
        previous_value=previous,
        new_value=observation.value,
        detail=(
            f"The sheet was read again and the machine value changed from "
            f"'{previous or '(blank)'}' to '{observation.value or '(blank)'}'."
        ),
    )
    row.machine_value = observation.value
    row.machine_status = observation.status
    row.machine_confidence = observation.confidence
    row.machine_top_fill = observation.top_fill
    row.machine_margin = observation.margin
    row.machine_candidates = _dump_candidates(observation.candidates)
    row.machine_detail = observation.detail
    row.updated_at = moment


def _withdraw_conflict(session: Session, row: ReviewConflict, moment: datetime) -> None:
    """Retire a conflict the machine no longer reports. Never deletes it."""
    _append_event(
        session,
        conflict=row,
        action=ReviewAction.WITHDRAWN,
        previous_value=row.machine_value,
        detail="Re-reading this sheet no longer produces this conflict.",
    )
    row.state = ConflictState.WITHDRAWN.value
    row.updated_at = moment


def _redetect_conflict(session: Session, row: ReviewConflict, moment: datetime) -> None:
    """Raise again a conflict the machine had withdrawn, now that it applies again.

    Used by duplicate detection, whose facts change without any sheet being
    re-read: two sheets stop being duplicates when one is rejected, and are
    duplicates again when the rejection is undone or a replacement link is
    removed. Leaving the withdrawn record withdrawn would under-report the
    batch's actionable state - Resolve would show one side, or neither.

    **Only a machine withdrawal is reversed.** A record whose standing command
    is a person's decision is left exactly as it is: the machine may take back
    its own complaint and raise it again, never override somebody's decision.
    The event is machine-authored (no reviewer), so the undo stack, which
    replays people's commands, never sees it.
    """
    stack = standing_commands(
        [_to_audit(item) for item in _ordered_events(session, row.conflict_id)]
    )
    if not stack or stack[-1].action is not ReviewAction.WITHDRAWN:
        return
    _append_event(
        session,
        conflict=row,
        action=ReviewAction.REDETECTED,
        new_value=row.machine_value,
        detail="Detected again: the sheets concerned are both in play once more.",
    )
    row.state = ConflictState.OPEN.value
    row.updated_at = moment


UNREAD_SET_CODE_MARKERS = frozenset({"?", "_"})
"""Characters recognition writes where a set-code position was not read."""


def sync_undefined_set_codes(database: ProjectDatabase, batch_id: str) -> int:
    """Raise a conflict for every script whose set code is not a defined set.

    Args:
        database: The open project database.
        batch_id: The batch to examine.

    Returns:
        How many scripts in the batch now carry such a conflict.

    Run in the coordinator after a batch, and again before reconciling, for the
    same reason :func:`sync_duplicate_identifiers` is: whether ``"D"`` is a
    valid set is a fact about the *project*, not about the sheet, and the
    project's set list can change after the batch was read.

    Only a set code that was **read** and is not in the list takes part. A code
    still in dispute already has its own open conflict, and a project that has
    defined no sets has nothing to compare against. A conflict nobody acted on
    is withdrawn when it no longer applies (the set was added, or another
    correction made the code valid); one a person decided is left alone.
    """
    from omr_scanner.services import project_sets

    defined = {item.code for item in project_sets.list_sets(database)}
    effective = effective_set_codes(database, batch_id)
    moment = _now()
    with database.session() as session:
        # "Still in dispute" is judged on every *other* set-code conflict. The
        # one this function raises marks the code unknown too, and counting it
        # would make a second run withdraw what the first one raised.
        disputed = {
            row.scan_id
            for row in session.scalars(
                select(ReviewConflict)
                .where(ReviewConflict.batch_id == batch_id)
                .where(
                    ReviewConflict.state.in_(
                        [ConflictState.OPEN.value, ConflictState.DEFERRED.value]
                    )
                )
            ).all()
            if ConflictType(row.conflict_type) is not ConflictType.SET_CODE_UNDEFINED
            and (
                ConflictType(row.conflict_type) in _SET_CODE_IS_UNKNOWN
                or ConflictType(row.conflict_type).is_processing_failure
            )
        }
        scans = {
            row.scan_id: row
            for row in session.scalars(
                select(BatchScan).where(BatchScan.batch_id == batch_id)
            ).all()
        }
        existing = {
            row.scan_id: row
            for row in session.scalars(
                select(ReviewConflict)
                .where(ReviewConflict.batch_id == batch_id)
                .where(
                    ReviewConflict.conflict_type == ConflictType.SET_CODE_UNDEFINED.value
                )
            ).all()
        }
        flagged: set[int] = set()
        for scan_id, found in effective.items():
            value = found.value
            if (
                not defined
                or scan_id in disputed
                or not value
                or any(marker in value for marker in UNREAD_SET_CODE_MARKERS)
                or value in defined
            ):
                continue
            row = existing.get(scan_id)
            if row is not None and ConflictState(row.state).is_human_touched:
                # Somebody has already decided about this sheet's set code.
                flagged.add(scan_id)
                continue
            zone_id, label = _set_code_zone(scans.get(scan_id))
            detected = DetectedConflict(
                conflict_type=ConflictType.SET_CODE_UNDEFINED,
                field=FieldRef(
                    zone_id=zone_id,
                    group_key=WHOLE_FIELD,
                    kind=FieldKind.SET_CODE,
                    label=label,
                ),
                observation=MachineObservation(
                    value=value,
                    status="resolved",
                    detail=(
                        f"Read as '{value}', which is not one of this project's sets "
                        f"({', '.join(sorted(defined))})."
                    ),
                ),
                severity=1,
            )
            if row is None:
                _insert_conflict(session, batch_id, scan_id, detected, moment)
                flagged.add(scan_id)
                continue
            # An existing record is refreshed exactly as `sync_conflicts`
            # refreshes one. A withdrawn record stays withdrawn - detection
            # never reopens a conflict - and the script is still kept out of
            # every set's reconciliation, because its code is still undefined.
            _refresh_conflict(session, row, detected, moment)
            if row.state != ConflictState.WITHDRAWN.value:
                flagged.add(scan_id)

        for scan_id, row in existing.items():
            if scan_id in flagged or row.state == ConflictState.WITHDRAWN.value:
                continue
            if ConflictState(row.state).is_human_touched:
                continue
            _withdraw_conflict(session, row, moment)
        session.flush()
    return len(flagged)


def _set_code_zone(scan: BatchScan | None) -> tuple[str, str]:
    """The set-code zone and its label, from a sheet's stored result."""
    if scan is None or not scan.result_json:
        return "", "Set code"
    try:
        result = ScanResult.from_dict(json.loads(scan.result_json))
    except (ValueError, KeyError, TypeError):  # pragma: no cover - defensive
        return "", "Set code"
    zone_id = result.set_code_zone_id or ""
    field_view = next((item for item in result.fields if item.zone_id == zone_id), None)
    return zone_id, (field_view.label if field_view is not None else "") or "Set code"


def sync_duplicate_identifiers(database: ProjectDatabase, batch_id: str) -> int:
    """Detect and store duplicate-identifier conflicts across a whole batch.

    Args:
        database: The open project database.
        batch_id: The batch to examine.

    Returns:
        How many duplicate conflicts the batch now has.

    Run **after** a batch finishes, in the coordinator. A duplicate is not a
    property of one sheet - both are perfectly legible - so it cannot be seen
    while reading one, and making a worker process aware of its siblings would
    mean shared mutable state across processes, which the Phase 5 architecture
    exists to avoid.

    Only identifiers the engine itself considered reliable take part: two sheets
    both read as ``"21?312"`` are evidence of a recognition problem, which they
    already have their own conflicts for, not of a duplicate candidate.

    **Only result-eligible scans take part.** A rejected original shares its
    Student ID with its rescan by definition; counting it would keep the
    replacement in a duplicate conflict with a sheet that no longer counts.
    Two *active* sheets with one ID are still detected exactly as before, and
    a duplicate conflict somebody has already decided is kept, as always.
    """
    moment = _now()
    with database.session() as session:
        scans = session.scalars(
            select(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.scan_id.not_in(_ineligible_scans()))
        ).all()
        # `identifier_is_reliable` is a property of the result, and the durable
        # row stores the value only - so reliability is inferred the same way
        # the naming layer does: a value with no unresolved placeholder in it.
        reliable = [
            (row.scan_id, row.identifier_value)
            for row in scans
            if row.identifier_value and "?" not in row.identifier_value
            and "_" not in row.identifier_value
        ]
        detected = detect_duplicate_identifiers(reliable)

        existing = {
            row.scan_id: row
            for row in session.scalars(
                select(ReviewConflict)
                .where(ReviewConflict.batch_id == batch_id)
                .where(
                    ReviewConflict.conflict_type == ConflictType.IDENTIFIER_DUPLICATE.value
                )
            ).all()
        }

        for scan_id, found in detected.items():
            row = existing.get(scan_id)
            if row is None:
                _insert_conflict(session, batch_id, scan_id, found, moment)
            else:
                row.related_scan_ids = _dump_related(found.related_scan_ids)
                _refresh_conflict(session, row, found, moment)
                if row.state == ConflictState.WITHDRAWN.value:
                    _redetect_conflict(session, row, moment)

        ineligible = set(session.scalars(_ineligible_scans_select()).all())
        for scan_id, row in existing.items():
            if scan_id in detected or row.state == ConflictState.WITHDRAWN.value:
                continue
            if ConflictState(row.state).is_human_touched:
                continue
            if scan_id in ineligible:
                # A rejected scan's own record is left exactly as it was -
                # hidden from the queue, not withdrawn - so that undoing the
                # rejection brings it back rather than losing it.
                continue
            _withdraw_conflict(session, row, moment)

        session.flush()
        return len(detected)


# ----------------------------------------------------------------------
# Human actions
# ----------------------------------------------------------------------
def validate_reviewer(reviewer: str) -> str:
    """Return a cleaned reviewer name, or refuse.

    Raises:
        ReviewError: The name is empty or only whitespace.

    The exit criterion says a final value must be traceable to a *named*
    correction, so an unnamed one cannot be allowed to exist. This is the one
    place that is enforced, and every human action calls it.
    """
    cleaned = reviewer.strip()
    if not cleaned:
        raise ReviewError(
            "A review action was attempted with no reviewer name",
            user_message=(
                "Enter your name before resolving a conflict. Every correction "
                "is recorded against the person who made it."
            ),
        )
    return cleaned


def validate_reason(reason: ReasonCode, reason_text: str) -> str:
    """Return cleaned reason text, or refuse.

    Raises:
        ReviewError: ``reason`` is :attr:`~omr_scanner.domain.review.ReasonCode.OTHER`
            and no text was supplied. "Other" with nothing beside it records
            that a value was changed and nothing about why, which is the one
            thing a reason field exists to prevent.
    """
    cleaned = reason_text.strip()
    if reason.requires_text and not cleaned:
        raise ReviewError(
            "A correction used reason 'other' with no explanation",
            user_message="Describe the reason when choosing 'Other'.",
        )
    return cleaned


def accept_machine_value(
    database: ProjectDatabase,
    conflict_id: int,
    *,
    reviewer: str,
    reason: ReasonCode = ReasonCode.MACHINE_CONFIRMED,
    reason_text: str = "",
) -> Provenance:
    """Record that a named reviewer inspected this conflict and agreed.

    Returns:
        The conflict's provenance afterwards.

    This is **not** the same as leaving the conflict alone. The value does not
    change, but its source becomes ``HUMAN``: somebody looked at the sheet and
    confirmed what the machine read, which is exactly the distinction an
    examination office needs between "checked" and "not yet checked".
    """
    name = validate_reviewer(reviewer)
    text_value = validate_reason(reason, reason_text)

    with database.session() as session:
        row = _require_conflict(session, conflict_id)
        previous = _project_provenance(session, row).value
        _append_event(
            session,
            conflict=row,
            action=ReviewAction.ACCEPTED,
            reviewer=name,
            previous_value=previous,
            new_value=row.machine_value,
            reason_code=reason.value,
            reason_text=text_value,
        )
        row.state = ConflictState.RESOLVED.value
        row.updated_at = _now()
        session.flush()
        _LOGGER.info(
            "Conflict %d accepted by %s (machine value %r kept)",
            conflict_id,
            name,
            row.machine_value,
        )
        return _project_provenance(session, row)


def correct_value(
    database: ProjectDatabase,
    conflict_id: int,
    *,
    value: str,
    reviewer: str,
    reason: ReasonCode,
    reason_text: str = "",
) -> Provenance:
    """Record a named reviewer's replacement value.

    Args:
        database: The open project database.
        conflict_id: The conflict being decided.
        value: The effective value from now on. May be ``""`` for "blank".
        reviewer: Who decided. Required.
        reason: Why. Required.
        reason_text: Free text; required when ``reason`` is ``OTHER``.

    Returns:
        The conflict's provenance afterwards.

    Raises:
        ReviewError: No reviewer, a missing explanation, or a conflict that
            does not accept a value at all (a corrupt image is not a value a
            reviewer can choose between).

    **The machine's value is not touched.** ``machine_value`` keeps whatever
    recognition read - including a double mark such as ``"B-D"``, which remains
    true about the paper after a reviewer decides the candidate meant ``"B"``.
    """
    name = validate_reviewer(reviewer)
    text_value = validate_reason(reason, reason_text)

    with database.session() as session:
        row = _require_conflict(session, conflict_id)
        _correct_one(
            session,
            row,
            value=value,
            reviewer=name,
            reason=reason,
            reason_text=text_value,
        )
        session.flush()
        return _project_provenance(session, row)


def _correct_one(
    session: Session,
    row: ReviewConflict,
    *,
    value: str,
    reviewer: str,
    reason: ReasonCode,
    reason_text: str,
    detail: str = "",
) -> str:
    """Record one replacement value inside an open transaction.

    Returns:
        The effective value this decision replaced.

    Factored out of :func:`correct_value` so that a field-level edit can write
    several positions in **one** transaction without a second implementation of
    what a correction is. Both paths append the same event, set the same state
    and leave ``machine_value`` alone.
    """
    conflict_type = ConflictType(row.conflict_type)
    if not conflict_type.allows_value_correction:
        raise ReviewError(
            f"Conflict {row.conflict_id} ({conflict_type.value}) carries no field value",
            user_message=(
                f"'{conflict_type.label}' is not a value that can be corrected. "
                "Acknowledge it or defer it instead."
            ),
        )

    previous = _project_provenance(session, row).value
    if _is_override_row(row) and not is_override(detail):
        # A redo, or a single-value correction made on the override record
        # itself: still an override of a confident reading, and the ledger row
        # has to say so on its own.
        detail = (f"{detail} " if detail else "") + (
            f"Explicit manual override of a confident machine reading. {OVERRIDE_MARKER}"
        )
    _append_event(
        session,
        conflict=row,
        action=ReviewAction.CORRECTED,
        reviewer=reviewer,
        previous_value=previous,
        new_value=value,
        reason_code=reason.value,
        reason_text=reason_text,
        detail=detail,
    )
    row.state = ConflictState.RESOLVED.value
    row.updated_at = _now()
    _LOGGER.info(
        "Conflict %d corrected by %s: %r -> %r (machine value %r preserved)",
        row.conflict_id,
        reviewer,
        previous,
        value,
        row.machine_value,
    )
    return previous


# ----------------------------------------------------------------------
# Correcting a whole field in one action
# ----------------------------------------------------------------------
GROUP_MARKER = "[edit "
"""How a grouped operator action is marked in an event's ``detail``.

A **context identifier, carried in free text** because the ledger has no column
for one. ``audit_event`` is the one table in the schema that cannot be altered
casually - it is under immutability triggers and holds every decision any
project has ever recorded - and adding a column to it so that a convenience
action can group its own events would be a migration of the whole ledger for a
presentation concern.

The marker is human-readable on purpose: it appears in the history dialog as
part of a sentence, so a reader sees *why* three positions changed at once
rather than a bare identifier. :func:`group_of` is the only thing that parses
it back."""


def group_of(detail: str) -> str:
    """Return the grouped-action token an event's detail carries, or ``""``."""
    start = detail.find(GROUP_MARKER)
    if start < 0:
        return ""
    start += len(GROUP_MARKER)
    end = detail.find("]", start)
    return detail[start:end] if end > start else ""


OVERRIDE_MARKER = "[override]"
"""How an explicit override of a confident machine reading is marked in an
event's ``detail``.

Carried in free text for the same reason as :data:`GROUP_MARKER`: the ledger
is under immutability triggers and gains no column for this. The record the
event belongs to already says so structurally - its type is
:attr:`~omr_scanner.domain.review.ConflictType.MANUAL_OVERRIDE` - but an
exported ledger row is read on its own, and must say by itself that nobody
detected a problem here and a person overruled the machine anyway.
:func:`is_override` is the only thing that reads it back."""


def is_override(detail: str) -> bool:
    """Whether an event records an explicit override of a confident reading."""
    return OVERRIDE_MARKER in detail


def _is_override_row(row: ReviewConflict) -> bool:
    """Whether a stored record was opened by an operator rather than detected."""
    return row.conflict_type == ConflictType.MANUAL_OVERRIDE.value


def _resting_state(row: ReviewConflict) -> ConflictState:
    """Where a record stands when no decision stands on it.

    ``OPEN`` for a detected conflict: the question it raised is unanswered
    again. ``WITHDRAWN`` for an operator override: there never was a question,
    so the machine's reading simply stands again - see
    :attr:`~omr_scanner.domain.review.ConflictType.MANUAL_OVERRIDE`.
    """
    return ConflictState.WITHDRAWN if _is_override_row(row) else ConflictState.OPEN


def _override_row(
    session: Session,
    *,
    batch_id: str,
    scan_id: int,
    zone_id: str,
    position: int,
    observation: MachineObservation,
    kind: FieldKind,
    field_label: str,
    moment: datetime,
) -> ReviewConflict:
    """Return the record an override of one confident position is stored on.

    Reuses the record an earlier, since-undone override left behind - the
    identity ``(batch, scan, type, zone, group)`` is unique, and one position
    overridden twice is one record with a longer history rather than two -
    and otherwise creates one.

    **No DETECTED event is written.** The machine raised nothing here, and a
    history that opened with "machine recognition flagged this" would record
    something that did not happen. The first event on a new record is the
    operator's own correction.
    """
    row = session.scalars(
        select(ReviewConflict)
        .where(ReviewConflict.batch_id == batch_id)
        .where(ReviewConflict.scan_id == scan_id)
        .where(ReviewConflict.conflict_type == ConflictType.MANUAL_OVERRIDE.value)
        .where(ReviewConflict.zone_id == zone_id)
        .where(ReviewConflict.group_key == position)
    ).first()
    if row is not None:
        _refresh_observation(session, row, observation, moment)
        return row
    row = ReviewConflict(
        batch_id=batch_id,
        scan_id=scan_id,
        conflict_type=ConflictType.MANUAL_OVERRIDE.value,
        scope=ConflictType.MANUAL_OVERRIDE.scope.value,
        severity=0,
        state=ConflictState.WITHDRAWN.value,
        zone_id=zone_id,
        group_key=position,
        field_kind=kind.value,
        field_label=field_label,
        question_number=None,
        machine_value=observation.value,
        machine_status=observation.status,
        machine_confidence=observation.confidence,
        machine_top_fill=observation.top_fill,
        machine_margin=observation.margin,
        machine_candidates=_dump_candidates(observation.candidates),
        machine_detail=observation.detail,
        related_scan_ids="",
        created_at=moment,
        updated_at=moment,
    )
    session.add(row)
    # Flushed to obtain `conflict_id` before the correction that references it.
    session.flush()
    return row


@dataclass(frozen=True, slots=True)
class FieldEdit:
    """What one whole-field correction did.

    Attributes:
        group: The token every event of this action carries, so the history can
            show that they were one operator action and
            :func:`undo_field_edit` can take all of them back together.
        zone_id: The field that was edited.
        value: What the operator entered, as one string.
        changed: ``group_key -> new value`` for the positions that were
            actually written.
        unchanged: Positions whose conflict already carried the entered value
            and needed no second decision.
        missing: Positions the entered value disagrees with that have **no
            conflict to correct** and no override was requested for. Always
            empty when the caller checked first; reported rather than silently
            ignored, because a value the interface accepted and did not store
            is the worst outcome here.
        overridden: The subset of :attr:`changed` that overrode a *confident*
            machine reading - positions nobody had disputed.
    """

    group: str
    zone_id: str
    value: str
    changed: dict[int, str] = field(default_factory=dict)
    unchanged: tuple[int, ...] = ()
    missing: tuple[int, ...] = ()
    overridden: tuple[int, ...] = ()

    @property
    def conflict_count(self) -> int:
        """How many position conflicts this action decided."""
        return len(self.changed)


def correct_field(
    database: ProjectDatabase,
    *,
    batch_id: str,
    scan_id: int,
    zone_id: str,
    values: Mapping[int, str],
    display_value: str,
    field_label: str,
    reviewer: str,
    reason: ReasonCode,
    reason_text: str = "",
    overrides: Mapping[int, MachineObservation] | None = None,
    field_kind: FieldKind | None = None,
    previous_value: str = "",
    context: str = "",
) -> FieldEdit:
    """Decide several positions of one field as a single operator action.

    Args:
        database: The open project database.
        batch_id: The batch being reviewed.
        scan_id: The sheet.
        zone_id: The field's template zone.
        values: ``group_key -> value`` for every position the entered field
            value implies. ``WHOLE_FIELD`` for a field that is disputed as a
            whole rather than per position.
        display_value: The field value as the operator typed it, for the
            history.
        field_label: What to call the field in the history ("Student ID").
        reviewer: Who decided. Required.
        reason: Why. Required, and shared by every position.
        reason_text: Free text; required when ``reason`` is ``OTHER``.
        overrides: ``group_key -> machine observation`` for positions the
            operator is **explicitly overriding** although the machine read
            them confidently and no conflict exists. Only these may be written
            without a conflict; any other conflict-less position is reported
            in :attr:`FieldEdit.missing`, as before.
        field_kind: What kind of field this is, for a new override record.
            Taken from the field's existing conflicts when omitted.
        previous_value: The field as it read before the edit, for the history.
        context: Where the edit was made and why, appended to every event's
            detail so the ledger records the circumstances as well as the change.

    Returns:
        What was written, and what was deliberately not.

    Raises:
        ReviewError: No reviewer, a missing explanation, or nothing to decide.

    **Overriding a confident reading is allowed, and is never disguised.** A
    digit read cleanly as ``9`` that the paper shows is an ``8`` must be
    correctable, or the one tool meant for "I can see the whole number" cannot
    fix the error that matters most. Such a position is stored on an
    :attr:`~omr_scanner.domain.review.ConflictType.MANUAL_OVERRIDE` record
    (:func:`_override_row`), never on an invented detection, and its event
    carries :data:`OVERRIDE_MARKER` as well as the edit's group token - so it
    is undone with the rest of the edit and reads as what it was.

    **A convenience over the position model, not a replacement for it.** A
    student who left four digits of their roll number blank produces four
    position conflicts, and making the operator visit each one to type one
    digit is the workflow this exists to remove. What it writes is still four
    ordinary corrections, each against its own conflict, each with the
    reviewer, the reason and the audit event a single-digit correction would
    have had - so every guarantee the position model gives (per-bubble
    provenance, granular reopen, the effective-value fold) is untouched.

    **One transaction.** A field half-applied would leave an identifier that is
    neither what the machine read nor what the operator typed.

    A position whose conflict already carries the entered value is left alone:
    re-deciding it would put a second identical correction in its history and
    say nothing.
    """
    name = validate_reviewer(reviewer)
    text_value = validate_reason(reason, reason_text)
    group = uuid4().hex[:8]
    requested = dict(overrides or {})
    was = f" (was '{previous_value}')" if previous_value else ""
    moment = _now()

    with database.session() as session:
        rows = {
            row.group_key: row
            for row in session.scalars(
                _resolution_only(
                    select(ReviewConflict)
                    .where(ReviewConflict.batch_id == batch_id)
                    .where(ReviewConflict.scan_id == scan_id)
                    .where(ReviewConflict.zone_id == zone_id)
                    .where(ReviewConflict.state != ConflictState.WITHDRAWN.value)
                )
            ).all()
        }

        kind = field_kind or next(
            (FieldKind(row.field_kind) for row in rows.values()), FieldKind.OTHER
        )

        changed: dict[int, str] = {}
        unchanged: list[int] = []
        missing: list[int] = []
        overridden: list[int] = []
        for position in sorted(values):
            wanted = values[position]
            row = rows.get(position)
            detail = (
                f"{field_label} set to '{display_value or '(blank)'}'{was} in one "
                f"edit; this position took '{wanted or '(blank)'}'."
            )
            if row is None:
                observation = requested.get(position)
                if observation is None:
                    missing.append(position)
                    continue
                if observation.value == wanted:
                    # Agreeing with a confident reading overrides nothing.
                    unchanged.append(position)
                    continue
                row = _override_row(
                    session,
                    batch_id=batch_id,
                    scan_id=scan_id,
                    zone_id=zone_id,
                    position=position,
                    observation=observation,
                    kind=kind,
                    field_label=field_label,
                    moment=moment,
                )
                detail = (
                    "Explicit manual override of a confidently read machine value: "
                    f"recognition read '{observation.value or '(blank)'}' here and "
                    f"raised no conflict. {detail} {OVERRIDE_MARKER}"
                )
                overridden.append(position)
            else:
                current = _project_provenance(session, row)
                # Already decided, and decided this way: a second identical
                # correction would add a line to the history saying nothing.
                if current.is_human_decided and current.value == wanted:
                    unchanged.append(position)
                    continue
            _correct_one(
                session,
                row,
                value=wanted,
                reviewer=name,
                reason=reason,
                reason_text=text_value,
                detail=(
                    f"{detail} {context.strip()} {GROUP_MARKER}{group}]"
                    if context.strip()
                    else f"{detail} {GROUP_MARKER}{group}]"
                ),
            )
            changed[position] = wanted

        if not changed:
            raise ReviewError(
                f"Field edit on scan {scan_id} zone {zone_id} changed nothing",
                user_message=(
                    "That value is already recorded for every position this "
                    "edit can reach. Nothing was changed."
                ),
            )
        session.flush()

    _LOGGER.info(
        "Scan %d %s set to %r by %s in one edit (%d position(s), %d override(s) "
        "of a confident reading, group %s)",
        scan_id,
        zone_id,
        display_value,
        name,
        len(changed),
        len(overridden),
        group,
    )
    return FieldEdit(
        group=group,
        zone_id=zone_id,
        value=display_value,
        changed=changed,
        unchanged=tuple(unchanged),
        missing=tuple(missing),
        overridden=tuple(overridden),
    )


def undo_field_edit(
    database: ProjectDatabase,
    *,
    batch_id: str,
    group: str,
    reviewer: str,
    reason_text: str = "",
) -> tuple[UndoTarget, ...]:
    """Take back every decision one whole-field edit made.

    Args:
        database: The open project database.
        batch_id: The batch being reviewed.
        group: The token :class:`FieldEdit` reported.
        reviewer: Who is undoing. Required.
        reason_text: Optional free text, recorded against every reversal.

    Returns:
        What was reversed, oldest first. Empty when none of the edit's
        decisions still stands.

    **One press, one action.** An operator who typed a roll number once and
    corrected four positions with it should not have to press undo four times
    to take that back - the four were never four decisions from where they were
    sitting. Only the decisions still standing are reversed, so an edit whose
    positions have since been decided again is not quietly rolled over the top
    of that later work.
    """
    name = validate_reviewer(reviewer)

    with database.session() as session:
        events = _human_events(session, batch_id, limit=UNDO_SEARCH_LIMIT)
        # `_walk_back_to_standing` keeps every command an undo has not
        # cancelled, which for one conflict includes the decisions a later
        # decision superseded. Only the **top** of each conflict's stack may be
        # popped, so a position somebody has since decided again is left alone
        # rather than rolled back over their work.
        topmost: dict[int, AuditEvent] = {}
        for event in _walk_back_to_standing(events):
            topmost.setdefault(event.conflict_id, event)
        standing = [
            event
            for event in topmost.values()
            if group_of(event.detail) == group
        ]
        if not standing:
            return ()
        moment = _now()
        reversed_commands: list[UndoTarget] = []
        for event in standing:
            row = _require_conflict(session, event.conflict_id)
            reversed_commands.append(
                _undo_one(session, row, reviewer=name, reason_text=reason_text)
            )
            row.updated_at = moment
        session.flush()
    _LOGGER.info(
        "Field edit %s: %d decision(s) undone by %s", group, len(standing), name
    )
    return tuple(reversed(reversed_commands))


def defer(
    database: ProjectDatabase,
    conflict_id: int,
    *,
    reviewer: str,
    reason_text: str = "",
) -> Provenance:
    """Record that a named reviewer deliberately postponed this conflict.

    Deferring decides nothing, so the effective value stays the machine's and
    the conflict still counts as unresolved. It exists so that a queue can tell
    "looked at and put aside" from "never looked at".
    """
    name = validate_reviewer(reviewer)

    with database.session() as session:
        row = _require_conflict(session, conflict_id)
        if _is_override_row(row):
            # Deferring means "still unresolved"; an override was never a
            # question, so there is nothing to postpone. Undo or reopen it.
            raise ReviewError(
                f"Conflict {conflict_id} is an operator override and cannot be deferred",
                user_message=(
                    "An operator override is not an open question and cannot be "
                    "deferred. Undo or reopen it to restore the machine reading."
                ),
            )
        previous = _project_provenance(session, row).value
        _append_event(
            session,
            conflict=row,
            action=ReviewAction.DEFERRED,
            reviewer=name,
            previous_value=previous,
            reason_text=reason_text.strip(),
            detail="Postponed for a later decision.",
        )
        row.state = ConflictState.DEFERRED.value
        row.updated_at = _now()
        session.flush()
        return _project_provenance(session, row)


def reopen(
    database: ProjectDatabase,
    conflict_id: int,
    *,
    reviewer: str,
    reason_text: str = "",
) -> Provenance:
    """Reopen a decided conflict so it can be decided again.

    The earlier decision is **not** removed. Reopening appends an event that
    withdraws the decision's authority; the effective value falls back to the
    machine's until somebody decides again, and the original correction stays
    in the ledger with its original reviewer, reason and timestamp.

    That is the difference between this and editing: a reviewer who later
    changes ``C`` to ``D`` produces a history saying "machine read B, X made it
    C, Y reopened it, Y made it D" - never one that reads as though X had
    chosen D all along.
    """
    name = validate_reviewer(reviewer)

    with database.session() as session:
        row = _require_conflict(session, conflict_id)
        previous = _project_provenance(session, row).value
        _append_event(
            session,
            conflict=row,
            action=ReviewAction.REOPENED,
            reviewer=name,
            previous_value=previous,
            new_value=row.machine_value,
            reason_text=reason_text.strip(),
            detail=(
                "The operator override was withdrawn; the machine reading stands "
                "again."
                if _is_override_row(row)
                else "The earlier decision was withdrawn; the conflict is open again."
            ),
        )
        row.state = _resting_state(row).value
        row.updated_at = _now()
        session.flush()
        _LOGGER.info("Conflict %d reopened by %s", conflict_id, name)
        return _project_provenance(session, row)


def _require_conflict(session: Session, conflict_id: int) -> ReviewConflict:
    """Load a conflict or refuse."""
    row = session.get(ReviewConflict, conflict_id)
    if row is None:
        raise ReviewError(
            f"Conflict {conflict_id} does not exist",
            user_message="That conflict is no longer in this project.",
        )
    return row


# ----------------------------------------------------------------------
# Undo
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class UndoTarget:
    """The decision an undo would take back, before it is taken back.

    Attributes:
        conflict_id: The conflict it was made on.
        scan_id: That conflict's sheet, so a reviewer can be shown what they
            are about to change rather than having it happen off screen.
        action: Which command it was.
        reviewer: Who made it.
        value: The effective value it established, ``""`` for blank.
        reason: Its reason code's stored value.
        reason_text: The free text that accompanied it.
        group: The whole-field edit it was part of, or ``""``. Lets undo take
            back one *operator action* rather than one of the several positions
            that action happened to write.
        describe: One short phrase naming it, for a tooltip or a menu item.

    Carries everything needed to *re-issue* the command, which is how redo is
    built: repeating a decision is a decision, recorded as one, rather than a
    third kind of ledger event that means "put the earlier one back".
    """

    conflict_id: int
    scan_id: int
    action: ReviewAction
    reviewer: str
    value: str
    reason: str = ""
    reason_text: str = ""
    group: str = ""
    describe: str = ""


@dataclass(frozen=True, slots=True)
class SheetUndo:
    """One sheet's completed resolution session, as undo sees it.

    Attributes:
        scan_id: The sheet.
        scan_name: Its file name, for a message.
        conflict_ids: Every conflict the session decided, in the order the
            decisions were made.
        decisions: How many standing decisions the session left behind, which
            is how many reversals undoing it appends. Never fewer than
            ``len(conflict_ids)`` and more when one conflict was decided twice.
        reversed_commands: What was taken back, oldest first. Empty until the
            undo has actually happened - :func:`last_resolved_sheet` describes
            what *would* be undone, and filling this in would mean doing the
            work twice to answer a tooltip.
    """

    scan_id: int
    scan_name: str
    conflict_ids: tuple[int, ...]
    decisions: int
    reversed_commands: tuple[UndoTarget, ...] = ()


_HUMAN_ACTIONS: tuple[str, ...] = tuple(
    item.value for item in ReviewAction if item.is_human
)
"""The stored ``action`` values a person can author. Derived from the enum, so
a new human action cannot be added and forgotten here."""

UNDO_SEARCH_LIMIT = 500
"""How far back through a batch's ledger :func:`last_decision` looks.

A bound, not a policy: the answer is almost always the newest event or the one
behind it, and a batch that has been reviewed for a week should not read its
entire history to answer a keystroke. Beyond this, undo reports that there is
nothing to take back rather than pretending to have searched."""

SHEET_UNDO_RUN_LIMIT = 20
"""How many sheet-resolution sessions :func:`last_resolved_sheet` looks back
through.

Each candidate run costs a query to ask whether its sheet is finished, and
"the last sheet you finished" is by construction one of the last few. Without
a bound, a reviewer who had worked five hundred single-conflict sheets would
pay five hundred queries for a toolbar's tooltip."""


def _human_events(session: Session, batch_id: str, *, limit: int) -> list[AuditEvent]:
    """Return a batch's most recent human events, newest first."""
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.batch_id == batch_id)
            .where(AuditEvent.action.in_(_HUMAN_ACTIONS))
            .order_by(AuditEvent.event_id.desc())
            .limit(limit)
        ).all()
    )


def _walk_back_to_standing(events: Sequence[AuditEvent]) -> list[AuditEvent]:
    """Filter a newest-first run of human events down to the ones still standing.

    Walking *backwards* is what makes this cheap. An ``UNDONE`` event cancels
    exactly one earlier command on the same conflict, so counting undos as they
    are passed and spending them against the commands below is the same answer
    :func:`standing_commands` gives, without having to load every conflict's
    full history to get it.
    """
    pending: dict[int, int] = {}
    standing: list[AuditEvent] = []
    for event in events:
        if event.action == ReviewAction.UNDONE.value:
            pending[event.conflict_id] = pending.get(event.conflict_id, 0) + 1
            continue
        if pending.get(event.conflict_id):
            pending[event.conflict_id] -= 1
            continue
        standing.append(event)
    return standing


def last_decision(database: ProjectDatabase, batch_id: str) -> UndoTarget | None:
    """Return the most recent human command in a batch that still stands.

    Args:
        database: The open project database.
        batch_id: The batch being reviewed.

    Returns:
        What an undo would take back, or ``None`` when nothing in the last
        :data:`UNDO_SEARCH_LIMIT` events is still standing.

    Deliberately batch-wide rather than "whatever is selected". With
    auto-advance on, the conflict a reviewer has just decided is no longer the
    one on screen, and an undo that acted on the *new* selection would either
    do nothing or - worse - take back a decision the reviewer was not thinking
    about.
    """
    with database.session() as session:
        standing = _walk_back_to_standing(
            _human_events(session, batch_id, limit=UNDO_SEARCH_LIMIT)
        )
        if not standing:
            return None
        event = standing[0]
        conflict = session.get(ReviewConflict, event.conflict_id)
        return _to_target(event, conflict)


def _to_target(event: AuditEvent, conflict: ReviewConflict | None) -> UndoTarget:
    """Project one human command into the value an undo reports."""
    action = ReviewAction(event.action)
    return UndoTarget(
        conflict_id=event.conflict_id,
        scan_id=event.scan_id,
        action=action,
        reviewer=event.reviewer,
        value=event.new_value,
        reason=event.reason_code,
        reason_text=event.reason_text,
        group=group_of(event.detail),
        describe=_describe_command(action, event.new_value, conflict),
    )


def _describe_command(
    action: ReviewAction, value: str, conflict: ReviewConflict | None
) -> str:
    """One short phrase naming a command, for a tooltip."""
    where = ""
    if conflict is not None:
        where = FieldRef(
            zone_id=conflict.zone_id,
            group_key=conflict.group_key,
            kind=FieldKind(conflict.field_kind),
            label=conflict.field_label,
            question_number=conflict.question_number,
        ).describe()
    if (
        conflict is not None
        and action is ReviewAction.CORRECTED
        and _is_override_row(conflict)
    ):
        what = f"overriding the machine reading with '{value or '(blank)'}'"
        return f"{what} on {where}" if where else what
    what = {
        ReviewAction.ACCEPTED: f"accepting '{value or '(blank)'}'",
        ReviewAction.CORRECTED: f"correcting to '{value or '(blank)'}'",
        ReviewAction.DEFERRED: "deferring",
        ReviewAction.REOPENED: "reopening",
    }.get(action, action.value)
    return f"{what} on {where}" if where else what


def undo_decision(
    database: ProjectDatabase,
    conflict_id: int,
    *,
    reviewer: str,
    reason_text: str = "",
) -> UndoTarget:
    """Take back the most recent human command standing on one conflict.

    Args:
        database: The open project database.
        conflict_id: The conflict to step back on.
        reviewer: Who is undoing. Required, and recorded - an undo changes what
            a script is worth just as a correction does.
        reason_text: Optional free text.

    Returns:
        What was taken back - enough to put it back again, which is what makes
        redo a re-issued decision rather than a third kind of ledger event.

    Raises:
        ReviewError: No reviewer, or nothing standing to take back.

    **One step, not a reset.** A conflict corrected by X and then corrected
    again by Y returns to X's value and stays resolved; only when the last
    standing command is gone does it return to the machine's value and to
    ``OPEN``. That is the difference between this and :func:`reopen`, and it is
    why the ledger is folded as a stack rather than scanned for the latest
    decision.
    """
    name = validate_reviewer(reviewer)

    with database.session() as session:
        row = _require_conflict(session, conflict_id)
        undone = _undo_one(session, row, reviewer=name, reason_text=reason_text)
        row.updated_at = _now()
        session.flush()
        _LOGGER.info("Conflict %d: decision undone by %s", conflict_id, name)
        return undone


def _undo_one(
    session: Session, row: ReviewConflict, *, reviewer: str, reason_text: str
) -> UndoTarget:
    """Append one reversal to a conflict, inside an open transaction."""
    events = [_to_audit(item) for item in _ordered_events(session, row.conflict_id)]
    stack = standing_commands(events)
    if not stack or not stack[-1].action.is_human:
        raise ReviewError(
            f"Conflict {row.conflict_id} has no decision to undo",
            user_message="There is nothing to undo on this conflict.",
        )

    undone = stack[-1]
    before = _fold_events(row, events)
    remaining = stack[:-1]
    restored = _standing_decision(remaining)
    restored_value = restored.new_value if restored is not None else row.machine_value

    _append_event(
        session,
        conflict=row,
        action=ReviewAction.UNDONE,
        reviewer=reviewer,
        previous_value=before.value,
        new_value=restored_value,
        reason_text=reason_text.strip(),
        detail=(
            f"The {undone.action.value} decision recorded by "
            f"{undone.reviewer or 'an unnamed reviewer'} at {undone.occurred_at} "
            "was undone. It remains in this history."
        ),
    )
    row.state = _state_of(remaining, override=_is_override_row(row)).value
    session.flush()
    return UndoTarget(
        conflict_id=row.conflict_id,
        scan_id=row.scan_id,
        action=undone.action,
        reviewer=undone.reviewer,
        value=undone.new_value,
        reason=undone.reason_code,
        reason_text=undone.reason_text,
        group=group_of(undone.detail),
        describe=_describe_command(undone.action, undone.new_value, row),
    )


def last_resolved_sheet(database: ProjectDatabase, batch_id: str) -> SheetUndo | None:
    """Return the sheet whose resolution session finished most recently.

    Args:
        database: The open project database.
        batch_id: The batch being reviewed.

    Returns:
        What :func:`undo_resolved_sheet` would take back, or ``None``.

    **A session is a run, and the ledger already records it.** A reviewer works
    one sheet until it is finished and then moves to the next, so a sheet's
    session is the maximal run of consecutive human events in this batch that
    belong to it - the run ends exactly where the reviewer moved on. Nothing
    extra is stored to know this, so it survives closing the project, and a
    second operator on the same project sees the same answer.

    Only a run whose sheet is *finished* qualifies: at least one conflict
    resolved and none left unresolved. A half-worked sheet has not been
    completed, so there is no completion to undo.
    """
    with database.session() as session:
        return _find_resolved_sheet(session, batch_id)


def _find_resolved_sheet(session: Session, batch_id: str) -> SheetUndo | None:
    """Locate the most recent completed sheet-resolution run."""
    events = _human_events(session, batch_id, limit=UNDO_SEARCH_LIMIT)
    for run in _runs_by_scan(events)[:SHEET_UNDO_RUN_LIMIT]:
        scan_id = run[0].scan_id
        if not _sheet_is_resolved(session, batch_id, scan_id):
            continue
        standing = _walk_back_to_standing(run)
        if not standing:
            continue
        scan = session.get(BatchScan, scan_id)
        # `standing` is newest-first inside the run; the session reads forwards.
        ordered = list(reversed(standing))
        return SheetUndo(
            scan_id=scan_id,
            scan_name=scan.filename if scan is not None else "",
            conflict_ids=tuple(dict.fromkeys(item.conflict_id for item in ordered)),
            decisions=len(ordered),
        )
    return None


def _runs_by_scan(events: Sequence[AuditEvent]) -> list[list[AuditEvent]]:
    """Split a newest-first event list into maximal same-sheet runs."""
    runs: list[list[AuditEvent]] = []
    for event in events:
        if runs and runs[-1][0].scan_id == event.scan_id:
            runs[-1].append(event)
        else:
            runs.append([event])
    return runs


def _sheet_is_resolved(session: Session, batch_id: str, scan_id: int) -> bool:
    """Whether every conflict on one sheet has been decided, and some were."""
    by_state = {
        str(state): int(count)
        for state, count in session.execute(
            _resolution_only(
                select(ReviewConflict.state, func.count())
                .where(ReviewConflict.batch_id == batch_id)
                .where(ReviewConflict.scan_id == scan_id)
                .group_by(ReviewConflict.state)
            )
        ).all()
    }
    unresolved = by_state.get(ConflictState.OPEN.value, 0) + by_state.get(
        ConflictState.DEFERRED.value, 0
    )
    return unresolved == 0 and by_state.get(ConflictState.RESOLVED.value, 0) > 0


def undo_resolved_sheet(
    database: ProjectDatabase,
    batch_id: str,
    *,
    reviewer: str,
    reason_text: str = "",
) -> SheetUndo | None:
    """Take back every decision of the most recently completed sheet.

    Args:
        database: The open project database.
        batch_id: The batch being reviewed.
        reviewer: Who is undoing. Required.
        reason_text: Optional free text, recorded against every reversal.

    Returns:
        What was undone, or ``None`` when no completed sheet was found.

    Raises:
        ReviewError: No reviewer name.

    **One transaction for the whole sheet.** A sheet half-restored is worse
    than one not restored at all: the counters would be right, the queue would
    look finished, and one conflict would silently still carry a decision
    nobody meant to keep. Either every reversal in the session commits or none
    of them does.

    Decisions made *before* that session are left alone. Undoing a sheet the
    operator finished this morning must not also discard what somebody decided
    on it last week, and because a session is a run in the ledger rather than
    "everything on this sheet", it does not.
    """
    name = validate_reviewer(reviewer)

    with database.session() as session:
        found = _find_resolved_sheet(session, batch_id)
        if found is None:
            return None
        moment = _now()
        reversed_commands: list[UndoTarget] = []
        # Newest first: each reversal pops the top of its conflict's stack, so
        # a conflict decided twice in the session is stepped back twice, in the
        # order those decisions were made.
        for conflict_id in _reversal_order(session, batch_id, found):
            row = _require_conflict(session, conflict_id)
            reversed_commands.append(
                _undo_one(session, row, reviewer=name, reason_text=reason_text)
            )
            row.updated_at = moment
        session.flush()
        found = replace(found, reversed_commands=tuple(reversed(reversed_commands)))
    _LOGGER.info(
        "Sheet %d: %d decision(s) undone by %s", found.scan_id, found.decisions, name
    )
    return found


def _reversal_order(
    session: Session, batch_id: str, found: SheetUndo
) -> tuple[int, ...]:
    """Return the conflict ids to reverse, newest decision first, with repeats.

    One entry per standing decision rather than per conflict: a position
    corrected and then corrected again during the same session needs two
    reversals, and returning the conflict twice is what makes
    :func:`_undo_one`'s single pop enough.
    """
    events = _human_events(session, batch_id, limit=UNDO_SEARCH_LIMIT)
    for run in _runs_by_scan(events):
        if run[0].scan_id != found.scan_id:
            continue
        return tuple(item.conflict_id for item in _walk_back_to_standing(run))
    return ()


# ----------------------------------------------------------------------
# Provenance: the projection
# ----------------------------------------------------------------------
def _ordered_events(session: Session, conflict_id: int) -> Sequence[AuditEvent]:
    """Return one conflict's events, oldest first.

    Ordered by ``event_id`` rather than by ``occurred_at``: two events written
    in the same transaction can share a timestamp to the second, and the fold
    that derives the effective value depends on knowing which came first.
    """
    return session.scalars(
        select(AuditEvent)
        .where(AuditEvent.conflict_id == conflict_id)
        .order_by(AuditEvent.event_id)
    ).all()


def _project_provenance(session: Session, row: ReviewConflict) -> Provenance:
    """Fold a conflict's ordered events into its current provenance.

    A left-fold over the ledger, not a lookup of a cached column. ``REOPENED``
    clears the decision because it does not
    :attr:`~omr_scanner.domain.review.ReviewAction.sets_effective_value`, which
    is what makes "reopen then decide again" behave correctly without a special
    case anywhere.
    """
    events = _ordered_events(session, row.conflict_id)
    return _fold_events(row, [_to_audit(item) for item in events])


def standing_commands(events: Sequence[AuditRecord]) -> tuple[AuditRecord, ...]:
    """Fold an ordered history into the commands still in effect, oldest first.

    Args:
        events: One conflict's events, oldest first.

    Returns:
        The stack of commands that have not been taken back, oldest at index
        ``0``.

    The single definition of "what has actually happened to this conflict", and
    the reason undo needs neither a mutable column nor a second history table.
    A command (:attr:`~omr_scanner.domain.review.ReviewAction.is_command`) is
    pushed; an ``UNDONE`` event pops the top, and only when the top is a human
    command - a machine withdrawal is not somebody's decision to take back.

    Everything else about a conflict is derived from this one function:
    :func:`_fold_events` reads the effective value off the stack and
    :func:`recompute_state` reads the state off its top, so the two can no more
    disagree with each other than either can with the ledger.
    """
    stack: list[AuditRecord] = []
    for event in events:
        if event.action is ReviewAction.UNDONE:
            if stack and stack[-1].action.is_human:
                stack.pop()
            continue
        if event.action.is_command:
            stack.append(event)
    return tuple(stack)


def _standing_decision(stack: Sequence[AuditRecord]) -> AuditRecord | None:
    """Return the command that currently decides the value, if any.

    Read from the top down. ``DEFERRED`` and ``WITHDRAWN`` are transparent -
    postponing a conflict, or the machine retracting it, does not undo a
    correction somebody already made - while ``REOPENED`` stops the search,
    because reopening is precisely the statement that no decision stands.
    """
    for event in reversed(stack):
        if event.action.sets_effective_value:
            return event
        if event.action in (ReviewAction.REOPENED, ReviewAction.REDETECTED):
            # A re-detected conflict is an open question again, as a
            # reopened one is.
            return None
    return None


def _fold_events(row: ReviewConflict, events: Sequence[AuditRecord]) -> Provenance:
    """Reduce an ordered history to one :class:`Provenance`."""
    decided = _standing_decision(standing_commands(events))

    if decided is None:
        return Provenance(
            value=row.machine_value,
            source=ValueSource.MACHINE,
            machine_value=row.machine_value,
            machine_confidence=row.machine_confidence,
            conflict_id=row.conflict_id,
            state=ConflictState(row.state),
        )
    return Provenance(
        value=decided.new_value,
        source=ValueSource.HUMAN,
        machine_value=row.machine_value,
        machine_confidence=row.machine_confidence,
        reviewer=decided.reviewer,
        reason=decided.reason_code,
        reason_text=decided.reason_text,
        decided_at=decided.occurred_at,
        audit_event_id=decided.event_id,
        conflict_id=row.conflict_id,
        state=ConflictState(row.state),
    )


_STATE_AFTER: dict[ReviewAction, ConflictState] = {
    ReviewAction.ACCEPTED: ConflictState.RESOLVED,
    ReviewAction.CORRECTED: ConflictState.RESOLVED,
    ReviewAction.DEFERRED: ConflictState.DEFERRED,
    ReviewAction.REOPENED: ConflictState.OPEN,
    ReviewAction.WITHDRAWN: ConflictState.WITHDRAWN,
    ReviewAction.REDETECTED: ConflictState.OPEN,
}
"""Where each command leaves a conflict. One entry per
:attr:`~omr_scanner.domain.review.ReviewAction.is_command` action, so a new
command cannot be added without deciding what it means for the state."""


def recompute_state(
    events: Sequence[AuditRecord], *, override: bool = False
) -> ConflictState:
    """Derive a conflict's state from its history alone.

    Exists so a test can prove the cached
    :attr:`~omr_scanner.database.models.ReviewConflict.state` never drifts from
    the ledger that justifies it - and, since undo is an event rather than a
    rewrite, so that an undone decision's state falls out of the same fold as
    everything else. Nothing in the application reads this on the hot path; the
    cached column is there to make a ten-thousand-row queue fast.
    """
    return _state_of(standing_commands(events), override=override)


def _state_of(
    stack: Sequence[AuditRecord], *, override: bool = False
) -> ConflictState:
    """Return the state the topmost standing command leaves a conflict in.

    ``override`` is for an :attr:`~omr_scanner.domain.review.ConflictType.MANUAL_OVERRIDE`
    record, which has no open state to return to: with nothing standing it
    rests as withdrawn, because nobody is being asked anything.
    """
    rest = ConflictState.WITHDRAWN if override else ConflictState.OPEN
    if not stack:
        return rest
    state = _STATE_AFTER.get(stack[-1].action, ConflictState.OPEN)
    return rest if state is ConflictState.OPEN else state


def provenance_for(database: ProjectDatabase, conflict_id: int) -> Provenance:
    """Return where one conflict's current value came from."""
    with database.session() as session:
        return _project_provenance(session, _require_conflict(session, conflict_id))


def provenance_for_scan(
    database: ProjectDatabase, batch_id: str, scan_id: int
) -> dict[int, Provenance]:
    """Return where every conflict on one sheet gets its value from.

    Args:
        database: The open project database.
        batch_id: The batch the sheet belongs to.
        scan_id: The sheet.

    Returns:
        One :class:`~omr_scanner.domain.review.Provenance` per conflict on that
        sheet, keyed by conflict id - including the ones nobody has decided,
        which resolve to the machine's own reading.

    Unlike :func:`effective_values_for_scan`, which answers "what did a person
    change here" for a downstream consumer, this answers "where does each of
    these values stand" for the reviewer looking at the sheet. The review
    overlay needs the undecided ones too: a position nobody has touched is
    exactly what it has to draw in amber.

    One transaction for the sheet rather than one per conflict, so selecting a
    conflict on a sheet with a dozen of them opens one.
    """
    with database.session() as session:
        rows = session.scalars(
            select(ReviewConflict)
            .where(ReviewConflict.batch_id == batch_id)
            .where(ReviewConflict.scan_id == scan_id)
        ).all()
        return {row.conflict_id: _project_provenance(session, row) for row in rows}


def history_for(database: ProjectDatabase, conflict_id: int) -> tuple[AuditRecord, ...]:
    """Return one conflict's complete history, oldest first."""
    with database.session() as session:
        return tuple(_to_audit(item) for item in _ordered_events(session, conflict_id))


def effective_values_for_scan(
    database: ProjectDatabase, batch_id: str, scan_id: int
) -> dict[tuple[str, int], Provenance]:
    """Return every human-decided value on one sheet, keyed by field reference.

    The single place a downstream consumer - the CSV export, a future report -
    asks "did a person change anything on this sheet". Keyed by
    ``(zone_id, group_key)``, which is the same identity
    :func:`~omr_scanner.services.conflict_policy.group_labels` uses, so a caller
    can map a value straight onto the group it belongs to.

    Only conflicts a human actually decided appear. A conflict nobody has
    touched resolves to the machine's own value, which the caller already has,
    and including it would invite a consumer to believe it had been reviewed.
    """
    with database.session() as session:
        rows = session.scalars(
            select(ReviewConflict)
            .where(ReviewConflict.batch_id == batch_id)
            .where(ReviewConflict.scan_id == scan_id)
            .where(ReviewConflict.state == ConflictState.RESOLVED.value)
        ).all()
        decided: dict[tuple[str, int], Provenance] = {}
        for row in rows:
            if row.group_key == WHOLE_FIELD and not row.zone_id:
                continue
            found = _project_provenance(session, row)
            if found.is_human_decided:
                decided[(row.zone_id, row.group_key)] = found
        return decided


# ----------------------------------------------------------------------
# Queue reads
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConflictFilter:
    """What subset of a batch's conflicts a queue should show.

    Attributes:
        states: Keep only these states; empty means every state except
            withdrawn.
        conflict_types: Keep only these types; empty means all.
        scan_id: Keep only this sheet's conflicts.
        search: Case-insensitive substring matched against the sheet's file
            name and its recognised identifier.
        include_withdrawn: Show conflicts the machine has retracted. Off by
            default - they are kept for the record, not for the working queue.
        include_rejected: Show conflicts on rejected, superseded or re-imported
            scans. Off by default - such a scan is out of the working queue
            until its rejection is undone - and on only for inspecting one.
    """

    states: tuple[ConflictState, ...] = ()
    conflict_types: tuple[ConflictType, ...] = ()
    scan_id: int | None = None
    search: str = ""
    include_withdrawn: bool = False
    include_rejected: bool = False


def list_conflicts(
    database: ProjectDatabase,
    batch_id: str,
    *,
    filters: ConflictFilter | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[ConflictRecord, ...]:
    """Return a batch's conflicts, most serious first.

    Args:
        database: The open project database.
        batch_id: The batch to read.
        filters: What to include; everything non-withdrawn by default.
        limit: Page size. ``None`` returns everything, which is fine for a
            test and for a few hundred conflicts; the queue passes a page.
        offset: Where the page starts.

    Returns:
        Detached value objects, **sheet by sheet**: the sheets in most trouble
        first, and within each sheet its worst conflict first, then by field
        and printed position. Deterministic, so paging is stable and a reopened
        queue looks the same.

    Ordering and filtering happen in SQL. A batch of ten thousand sheets can
    carry thousands of conflicts, and loading them all to sort them in Python
    is the difference between a queue that opens instantly and one that does
    not.
    """
    rules = filters if filters is not None else ConflictFilter()
    with database.session() as session:
        statement = (
            select(ReviewConflict, BatchScan, _reversed_before_column())
            .join(BatchScan, BatchScan.scan_id == ReviewConflict.scan_id, isouter=True)
            .where(ReviewConflict.batch_id == batch_id)
        )
        statement = _apply_filters(statement, rules)
        # **Sheet-major, worst sheet first.** Ordering by severity across the
        # whole batch - which this did - scatters one sheet's conflicts through
        # every other sheet's: a roll-number column with two marks is severity
        # 1 and an uncertain one is severity 0, so two problems on the same
        # paper ended up hundreds of rows apart. An operator holding one sheet
        # could not work it, and resolving a conflict moved the selection to a
        # different sheet, which is the jumping this ordering caused.
        #
        # The window keeps the intent of the old ordering - the sheets in most
        # trouble are still first - while making each sheet one contiguous run.
        sheet_severity = func.max(ReviewConflict.severity).over(
            partition_by=ReviewConflict.scan_id
        )
        statement = statement.order_by(
            sheet_severity.desc(),
            ReviewConflict.scan_id,
            ReviewConflict.severity.desc(),
            ReviewConflict.zone_id,
            ReviewConflict.group_key,
            ReviewConflict.conflict_id,
        )
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        return tuple(
            _to_record(conflict, scan, reversed_before=bool(reversed_before))
            for conflict, scan, reversed_before in session.execute(statement).all()
        )


_REVERSAL_ACTIONS: tuple[str, ...] = tuple(
    item.value for item in ReviewAction if item.is_reversal
)


def _reversed_before_column() -> Any:
    """A correlated count of this conflict's reversals, for the queue.

    A subquery rather than a second round trip: the queue reads five hundred
    rows at a time, and asking the ledger once per row is how a page that opens
    instantly becomes one that does not. ``ix_audit_event_conflict`` is what
    makes it cheap.
    """
    return (
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.conflict_id == ReviewConflict.conflict_id)
        .where(AuditEvent.action.in_(_REVERSAL_ACTIONS))
        .scalar_subquery()
    )


def _apply_filters(statement: Any, rules: ConflictFilter) -> Any:
    """Apply a :class:`ConflictFilter` to a select over conflicts."""
    statement = _resolution_only(statement)
    if not rules.include_rejected:
        statement = _active_scans_only(statement)
    if rules.states:
        statement = statement.where(
            ReviewConflict.state.in_([item.value for item in rules.states])
        )
    elif not rules.include_withdrawn:
        statement = statement.where(
            ReviewConflict.state != ConflictState.WITHDRAWN.value
        )
    if rules.conflict_types:
        statement = statement.where(
            ReviewConflict.conflict_type.in_([item.value for item in rules.conflict_types])
        )
    if rules.scan_id is not None:
        statement = statement.where(ReviewConflict.scan_id == rules.scan_id)
    if rules.search:
        pattern = f"%{rules.search.strip().lower()}%"
        statement = statement.where(
            func.lower(BatchScan.filename).like(pattern)
            | func.lower(BatchScan.identifier_value).like(pattern)
        )
    return statement


def count_conflicts(database: ProjectDatabase, batch_id: str) -> ReviewCounts:
    """Return how many conflicts a batch has in each state, and by type.

    Counted in SQL, grouped, in two queries - never by loading the rows. A
    summary panel that walked ten thousand objects on every repaint is exactly
    the kind of thing that makes a large batch unusable.

    **Answer ambiguity is not counted here.** A batch with a hundred
    double-marked questions, two disputed student IDs and one disputed set code
    reports three, not a hundred and three - which is the number an operator has
    to act on. How many answers were ambiguous is a recognition statistic and is
    reported with the recognition results.

    Nor are the conflicts of a **rejected** scan: it awaits a rescan, not a
    decision, and is counted by
    :func:`~omr_scanner.services.scan_lifecycle.count_cases` instead.
    """
    with database.session() as session:
        by_state = {
            str(state): int(count)
            for state, count in session.execute(
                _active_scans_only(
                    _resolution_only(
                        select(ReviewConflict.state, func.count())
                        .where(ReviewConflict.batch_id == batch_id)
                        .group_by(ReviewConflict.state)
                    )
                )
            ).all()
        }
        by_type = {
            str(kind): int(count)
            for kind, count in session.execute(
                _active_scans_only(
                    _resolution_only(
                        select(ReviewConflict.conflict_type, func.count())
                        .where(ReviewConflict.batch_id == batch_id)
                        .where(ReviewConflict.state != ConflictState.WITHDRAWN.value)
                        .group_by(ReviewConflict.conflict_type)
                    )
                )
            ).all()
        }
    return ReviewCounts(
        total=sum(by_state.values()),
        open_count=by_state.get(ConflictState.OPEN.value, 0),
        resolved=by_state.get(ConflictState.RESOLVED.value, 0),
        deferred=by_state.get(ConflictState.DEFERRED.value, 0),
        withdrawn=by_state.get(ConflictState.WITHDRAWN.value, 0),
        by_type=by_type,
    )


def count_conflicts_for_scan(
    database: ProjectDatabase, batch_id: str, scan_id: int
) -> ReviewCounts:
    """Return one sheet's conflict counts, for the review workspace header."""
    with database.session() as session:
        by_state = {
            str(state): int(count)
            for state, count in session.execute(
                _resolution_only(
                    select(ReviewConflict.state, func.count())
                    .where(ReviewConflict.batch_id == batch_id)
                    .where(ReviewConflict.scan_id == scan_id)
                    .group_by(ReviewConflict.state)
                )
            ).all()
        }
    return ReviewCounts(
        total=sum(by_state.values()),
        open_count=by_state.get(ConflictState.OPEN.value, 0),
        resolved=by_state.get(ConflictState.RESOLVED.value, 0),
        deferred=by_state.get(ConflictState.DEFERRED.value, 0),
        withdrawn=by_state.get(ConflictState.WITHDRAWN.value, 0),
    )


def get_conflict(database: ProjectDatabase, conflict_id: int) -> ConflictRecord | None:
    """Return one conflict, or ``None`` when it does not exist."""
    with database.session() as session:
        row = session.get(ReviewConflict, conflict_id)
        if row is None:
            return None
        reversals = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.conflict_id == conflict_id)
            .where(AuditEvent.action.in_(_REVERSAL_ACTIONS))
        )
        return _to_record(
            row,
            session.get(BatchScan, row.scan_id),
            reversed_before=bool(reversals),
        )


def sheet_resolutions(
    database: ProjectDatabase, batch_id: str, template: OmrTemplate
) -> dict[Path, SheetResolution]:
    """Return every sheet's human decisions, ready for export.

    Args:
        database: The open project database.
        batch_id: The batch to read.
        template: The template it was read with, to turn a group key back into
            a printed question number.

    Returns:
        One entry per sheet that has any conflict, keyed by source path.

    **The one place a consumer learns that a value was corrected.** The CSV
    export takes this mapping and nothing else; a later report should take the
    same one. Scattering "is there a resolution for this field" through every
    consumer is how an export eventually ships that quietly ignores
    corrections, which is exactly what
    :class:`~omr_scanner.services.scan_export.SheetResolution` exists to
    prevent.

    Counted and folded in two queries over the whole batch rather than one per
    sheet: a ten-thousand-sheet batch would otherwise open ten thousand
    transactions to export one file.
    """
    numbers = _question_numbers_by_group(template)
    identifier_zones = {
        zone.id for zone in template.zones if isinstance(zone.field, GridFieldDefinition)
    }

    with database.session() as session:
        rows = session.execute(
            select(ReviewConflict, BatchScan)
            .join(BatchScan, BatchScan.scan_id == ReviewConflict.scan_id)
            .where(ReviewConflict.batch_id == batch_id)
        ).all()

        decided: dict[Path, dict[str, Any]] = {}
        scans: dict[Path, BatchScan] = {}
        for conflict, scan in rows:
            path = Path(scan.source_path)
            scans[path] = scan
            entry = decided.setdefault(
                path,
                {"identifier": _FieldDecisions(), "set_code": _FieldDecisions(),
                 "answers": {}, "unresolved": 0, "reviewed": False},
            )
            # Counted only for conflicts that still require resolution: the
            # exported `unresolved_conflicts` column says how much of this row
            # is waiting for a person, and an ambiguous answer is not.
            if (
                ConflictState(conflict.state).needs_attention
                and ConflictType(conflict.conflict_type).requires_resolution
            ):
                entry["unresolved"] += 1
            if conflict.state != ConflictState.RESOLVED.value:
                continue
            found = _project_provenance(session, conflict)
            if not found.is_human_decided:
                continue
            entry["reviewed"] = True
            _apply_decision(entry, conflict, found, numbers, identifier_zones)

        # Assembled once per sheet, after every decision is known, so each
        # field is rebuilt from whole position symbols rather than patched one
        # character at a time.
        for path, entry in decided.items():
            scan = scans[path]
            entry["identifier"] = entry["identifier"].assemble(
                scan, scan.identifier_value or ""
            )
            entry["set_code"] = entry["set_code"].assemble(scan, scan.set_code_value or "")

    return {
        path: SheetResolution(
            identifier=entry["identifier"],
            set_code=entry["set_code"],
            answers=entry["answers"],
            unresolved=entry["unresolved"],
            reviewed=entry["reviewed"],
        )
        for path, entry in decided.items()
    }


def _apply_decision(
    entry: dict[str, Any],
    conflict: ReviewConflict,
    found: Provenance,
    numbers: dict[tuple[str, int], int],
    identifier_zones: set[str],
) -> None:
    """Fold one resolved conflict into a sheet's export overrides.

    A per-position decision on an identifier or set code cannot simply replace
    the whole field's value - correcting the third digit of a roll number says
    nothing about the other five - so it is recorded against its position and
    the field is assembled from position symbols afterwards; see
    :class:`_FieldDecisions`. A whole-field decision replaces it outright.
    """
    kind = FieldKind(conflict.field_kind)
    if kind is FieldKind.QUESTION:
        number = numbers.get((conflict.zone_id, conflict.group_key))
        if number is not None:
            entry["answers"][number] = found.value
        return

    if kind not in (FieldKind.IDENTIFIER, FieldKind.SET_CODE):
        return
    slot = "identifier" if kind is FieldKind.IDENTIFIER else "set_code"
    whole = conflict.group_key == WHOLE_FIELD or conflict.zone_id not in identifier_zones
    entry[slot].add(conflict, found.value, whole=whole)


@dataclass(slots=True)
class _FieldDecisions:
    """Every human decision on one sheet's identifier, or its set code.

    Collected first and assembled once (:meth:`assemble`), because a field is
    a sequence of **position symbols** and a symbol may be several characters
    long - a set code printed ``10``, ``11``, ``12``. Patching the assembled
    string one decision at a time, by character index, is what turned a
    corrected ``["10", "2"]`` into something other than ``"102"``.

    Attributes:
        whole: A decision on the field as a whole, or ``None``.
        zone_id: The field's zone, from its positional decisions.
        positions: ``position -> decided symbol``.
        ordered: Every decision in the order it was folded, for the fallback
            path in :meth:`assemble`.
    """

    whole: str | None = None
    zone_id: str = ""
    positions: dict[int, str] = field(default_factory=dict)
    ordered: list[tuple[int, str]] = field(default_factory=list)

    def add(self, conflict: ReviewConflict, value: str, *, whole: bool) -> None:
        """Record one resolved decision."""
        if whole:
            self.whole = value
            self.ordered.append((WHOLE_FIELD, value))
            return
        self.zone_id = conflict.zone_id
        self.positions[conflict.group_key] = value
        self.ordered.append((conflict.group_key, value))

    @property
    def is_empty(self) -> bool:
        """Whether nobody decided anything about this field."""
        return not self.ordered

    def assemble(self, scan: BatchScan | None, machine_value: str) -> str:
        """Return the field's effective value, or ``""`` when nothing was decided.

        Positional decisions are laid over the **machine's per-position
        symbols**, read from the stored recognition result
        (:func:`~omr_scanner.services.conflict_policy.machine_field_symbols`),
        and joined - never substituted into a string by character index.

        Falls back to character substitution over ``machine_value`` only where
        no per-position reading exists (a row whose stored result is missing or
        will not decode), or when a whole-field decision and positional ones
        meet on the same field. Both are recorded as limits in ``README.md``.
        """
        if self.is_empty:
            return ""
        if not self.positions:
            return self.whole or ""
        if self.whole is None:
            symbols = _stored_field_symbols(scan, self.zone_id)
            if symbols is not None:
                for position, value in self.positions.items():
                    if 0 <= position < len(symbols):
                        symbols[position] = value
                return join_field_value(symbols)
        value = machine_value
        for position, decided in self.ordered:
            if position == WHOLE_FIELD:
                value = decided
            else:
                value = _substitute_position(value, position, decided)
        return value


def _stored_field_symbols(scan: BatchScan | None, zone_id: str) -> list[str] | None:
    """The machine's per-position symbols for one field, from the stored result."""
    if scan is None or not scan.result_json or not zone_id:
        return None
    try:
        result = ScanResult.from_dict(json.loads(scan.result_json))
    except (ValueError, KeyError, TypeError):  # pragma: no cover - defensive
        _LOGGER.warning("Scan %d: stored result could not be decoded", scan.scan_id)
        return None
    return machine_field_symbols(result, zone_id)


def _substitute_position(value: str, position: int, replacement: str) -> str:
    """Replace one printed position of a field value, by character. Fallback only.

    Correct only when every symbol is one character, which is why nothing calls
    it except :meth:`_FieldDecisions.assemble` when no per-position machine
    reading is available.

    A position beyond the known value is padded with the unresolved marker
    rather than dropped: a person's decision must never vanish because the
    machine's string was shorter than the field.
    """
    if position < 0:
        return value
    characters = list(value)
    characters.extend(UNRESOLVED_CHARACTER for _ in range(position + 1 - len(characters)))
    characters[position] = replacement
    return "".join(characters)


def _question_numbers_by_group(template: OmrTemplate) -> dict[tuple[str, int], int]:
    """Map ``(zone_id, group_key)`` to the printed question number."""
    numbers: dict[tuple[str, int], int] = {}
    for zone in template.zones:
        field_def = zone.field
        if not isinstance(field_def, QuestionBlockFieldDefinition):
            continue
        for offset in range(field_def.question_count):
            numbers[(zone.id, offset)] = field_def.first_question + offset
    return numbers


@dataclass(frozen=True, slots=True)
class EffectiveIdentifier:
    """What a sheet's candidate ID reads as, after review.

    The bridge Phase 7 crosses. Reconciliation matches on :attr:`value`, shows
    :attr:`machine_value` beside it, and treats :attr:`unresolved` as a state of
    its own rather than pretending an unread ID is an unknown candidate.

    Attributes:
        scan_id: The sheet.
        machine_value: What recognition read. **Never overwritten.**
        value: What it reads as now - the machine's, unless a named reviewer
            decided otherwise on the Resolve stage.
        source: Which of the two :attr:`value` came from.
        unresolved: Whether the identifier is still *unknown* - an open or
            deferred conflict says nobody has established what it is. A
            duplicate-identifier conflict does **not** set this: the ID was read
            perfectly well, and it is Phase 7's job to say two sheets share it.
        reviewer / reason: Who decided, and why, when a human did.
    """

    scan_id: int
    machine_value: str
    value: str
    source: ValueSource = ValueSource.MACHINE
    unresolved: bool = False
    reviewer: str = ""
    reason: str = ""

    @property
    def was_corrected(self) -> bool:
        """Whether a person changed what the machine read."""
        return self.source is ValueSource.HUMAN and self.value != self.machine_value


_IDENTIFIER_IS_UNKNOWN: frozenset[ConflictType] = frozenset(
    {
        ConflictType.IDENTIFIER_BLANK,
        ConflictType.IDENTIFIER_INCOMPLETE,
        ConflictType.IDENTIFIER_MULTIPLE,
        ConflictType.IDENTIFIER_UNCERTAIN,
        ConflictType.IDENTIFIER_UNREADABLE,
        ConflictType.IDENTIFIER_LOW_CONFIDENCE,
    }
)
"""Conflicts that mean nobody yet knows which candidate a sheet belongs to.

Deliberately excludes ``IDENTIFIER_DUPLICATE`` - a duplicate ID is a perfectly
legible ID that two sheets share, which is a reconciliation problem rather than
a recognition one. Including it would file every duplicate under "candidate ID
not yet resolved" and hide the duplication itself.
"""


def effective_identifiers(
    database: ProjectDatabase, batch_id: str
) -> dict[int, EffectiveIdentifier]:
    """Return every sheet's candidate ID after Phase 6 review, keyed by scan id.

    Args:
        database: The open project database.
        batch_id: The batch to read.

    Returns:
        One entry per scan in the batch, whether or not anything was corrected.

    **The one place outside this module learns what a sheet's identifier now
    is.** Reconciliation asks here rather than reading
    ``BatchScan.identifier_value`` directly, because that column holds the
    machine's reading and using it would silently ignore every correction a
    reviewer made - the Phase 6 defect in a new costume.

    Two queries over the whole batch, not one per sheet: a ten-thousand-sheet
    cohort must not open ten thousand transactions to be reconciled.
    """
    with database.session() as session:
        scans = session.scalars(
            select(BatchScan).where(BatchScan.batch_id == batch_id)
        ).all()
        found = {
            row.scan_id: EffectiveIdentifier(
                scan_id=row.scan_id,
                machine_value=row.identifier_value or "",
                value=row.identifier_value or "",
            )
            for row in scans
        }

        fields: dict[int, _FieldDecisions] = {}
        conflicts = session.scalars(
            select(ReviewConflict)
            .where(ReviewConflict.batch_id == batch_id)
            .order_by(ReviewConflict.conflict_id)
        ).all()
        for conflict in conflicts:
            current = found.get(conflict.scan_id)
            if current is None:
                continue
            kind = FieldKind(conflict.field_kind)
            conflict_type = ConflictType(conflict.conflict_type)
            state = ConflictState(conflict.state)

            if state.needs_attention and (
                conflict_type in _IDENTIFIER_IS_UNKNOWN
                or conflict_type.is_processing_failure
            ):
                found[conflict.scan_id] = replace(current, unresolved=True)
                continue

            if kind is not FieldKind.IDENTIFIER or state is not ConflictState.RESOLVED:
                continue
            decided = _project_provenance(session, conflict)
            if not decided.is_human_decided:
                continue
            fields.setdefault(conflict.scan_id, _FieldDecisions()).add(
                conflict, decided.value, whole=conflict.group_key == WHOLE_FIELD
            )
            found[conflict.scan_id] = replace(
                current,
                source=ValueSource.HUMAN,
                reviewer=decided.reviewer,
                reason=decided.reason,
            )
        by_scan = {row.scan_id: row for row in scans}
        for scan_id, decisions in fields.items():
            current = found[scan_id]
            found[scan_id] = replace(
                current,
                value=decisions.assemble(by_scan.get(scan_id), current.machine_value),
            )
    return found


_SET_CODE_IS_UNKNOWN: frozenset[ConflictType] = frozenset(
    {
        ConflictType.SET_CODE_BLANK,
        ConflictType.SET_CODE_MULTIPLE,
        ConflictType.SET_CODE_UNCERTAIN,
        ConflictType.SET_CODE_UNREADABLE,
        ConflictType.SET_CODE_LOW_CONFIDENCE,
        ConflictType.SET_CODE_UNDEFINED,
    }
)
"""Conflicts that mean nobody yet knows which paper a sheet answers.

Phase 8 refuses to mark such a sheet. Falling back to another set's key would
produce a mark that looks ordinary and is against the wrong paper, which is the
worst available outcome - worse than no mark at all.
"""


def effective_set_codes(
    database: ProjectDatabase, batch_id: str
) -> dict[int, EffectiveIdentifier]:
    """Return every sheet's question-paper set after Phase 6 review.

    The sibling of :func:`effective_identifiers`, and the one place scoring
    learns which paper a candidate sat. Reading ``BatchScan.set_code_value``
    directly would ignore every correction a reviewer made - and a sheet marked
    against the wrong set's key is the defect this phase most has to avoid.

    Multi-character set codes (``"10"``, ``"X1"``) are carried through
    unchanged: a positional correction replaces one position's whole symbol and
    the code is reassembled from symbols - see :class:`_FieldDecisions`.
    """
    with database.session() as session:
        scans = session.scalars(
            select(BatchScan).where(BatchScan.batch_id == batch_id)
        ).all()
        found = {
            row.scan_id: EffectiveIdentifier(
                scan_id=row.scan_id,
                machine_value=row.set_code_value or "",
                value=row.set_code_value or "",
            )
            for row in scans
        }

        fields: dict[int, _FieldDecisions] = {}
        conflicts = session.scalars(
            select(ReviewConflict)
            .where(ReviewConflict.batch_id == batch_id)
            .order_by(ReviewConflict.conflict_id)
        ).all()
        for conflict in conflicts:
            current = found.get(conflict.scan_id)
            if current is None:
                continue
            kind = FieldKind(conflict.field_kind)
            conflict_type = ConflictType(conflict.conflict_type)
            state = ConflictState(conflict.state)

            if state.needs_attention and (
                conflict_type in _SET_CODE_IS_UNKNOWN
                or conflict_type.is_processing_failure
            ):
                found[conflict.scan_id] = replace(current, unresolved=True)
                continue

            if kind is not FieldKind.SET_CODE or state is not ConflictState.RESOLVED:
                continue
            decided = _project_provenance(session, conflict)
            if not decided.is_human_decided:
                continue
            fields.setdefault(conflict.scan_id, _FieldDecisions()).add(
                conflict, decided.value, whole=conflict.group_key == WHOLE_FIELD
            )
            found[conflict.scan_id] = replace(
                current,
                source=ValueSource.HUMAN,
                reviewer=decided.reviewer,
                reason=decided.reason,
            )
        by_scan = {row.scan_id: row for row in scans}
        for scan_id, decisions in fields.items():
            current = found[scan_id]
            found[scan_id] = replace(
                current,
                value=decisions.assemble(by_scan.get(scan_id), current.machine_value),
            )
    return found


@dataclass(frozen=True, slots=True)
class EffectiveAnswers:
    """What one sheet's questions read as, after Phase 6 review.

    Attributes:
        scan_id: The sheet.
        decided: Answers a named reviewer decided, keyed by printed question
            number. Only these - a question nobody touched keeps the machine's
            reading, which the caller already has.
        unresolved_questions: Printed question numbers whose conflict is still
            open or deferred, among conflicts that
            :attr:`~omr_scanner.domain.review.ConflictType.requires_resolution`.
            **Empty in practice**, because an answer no longer produces a
            conflict of any kind - and a legacy ``answer_*`` row must not block
            a batch that this build would never have flagged. Kept, with
            :attr:`~omr_scanner.domain.scoring.BlockReason.UNRESOLVED_ANSWERS`,
            so that the guarantee it expresses - scoring never invents an
            answer nobody has settled - stays wired up rather than being
            deleted and having to be remembered.

            What stops an ambiguous answer being *marked as though it were a
            clean one* is now
            :func:`~omr_scanner.services.scoring.build_candidate_answers`,
            which renders an answer the engine could not decide as the
            canonical "not a single answer" symbol.
    """

    scan_id: int
    decided: dict[int, str]
    unresolved_questions: tuple[int, ...] = ()

    @property
    def has_unresolved(self) -> bool:
        """Whether any question on this sheet still awaits review."""
        return bool(self.unresolved_questions)


def effective_answers(
    database: ProjectDatabase, batch_id: str, template: OmrTemplate
) -> dict[int, EffectiveAnswers]:
    """Return every sheet's reviewed answers, keyed by scan id.

    Args:
        database: The open project database.
        batch_id: The batch to read.
        template: The template it was read with, to turn a conflict's group key
            back into a printed question number.

    Returns:
        One entry per sheet that has any question conflict. A sheet with none
        is absent from the mapping, because there is nothing to say about it
        that the recognition result does not already say.

    Two queries over the whole batch, for the same reason its siblings are: a
    ten-thousand-sheet cohort must not open ten thousand transactions to be
    marked.
    """
    numbers = _question_numbers_by_group(template)
    found: dict[int, EffectiveAnswers] = {}

    with database.session() as session:
        conflicts = session.scalars(
            select(ReviewConflict)
            .where(ReviewConflict.batch_id == batch_id)
            .where(ReviewConflict.field_kind == FieldKind.QUESTION.value)
        ).all()
        for conflict in conflicts:
            number = numbers.get((conflict.zone_id, conflict.group_key))
            if number is None:
                continue
            entry = found.setdefault(
                conflict.scan_id, EffectiveAnswers(scan_id=conflict.scan_id, decided={})
            )
            state = ConflictState(conflict.state)
            if state.needs_attention:
                # A legacy answer conflict nobody decided is not a reason to
                # refuse to mark the script: this build would never have raised
                # it, and there is now nowhere to go and resolve it.
                if ConflictType(conflict.conflict_type).requires_resolution:
                    found[conflict.scan_id] = replace(
                        entry,
                        unresolved_questions=(*entry.unresolved_questions, number),
                    )
                continue
            if state is not ConflictState.RESOLVED:
                continue
            decided = _project_provenance(session, conflict)
            if decided.is_human_decided:
                entry.decided[number] = decided.value

    return {
        scan_id: replace(
            entry, unresolved_questions=tuple(sorted(entry.unresolved_questions))
        )
        for scan_id, entry in found.items()
    }


def scan_source_path(database: ProjectDatabase, scan_id: int) -> str:
    """Return one scan's source path, for the review workspace to load."""
    with database.session() as session:
        row = session.get(BatchScan, scan_id)
        return row.source_path if row is not None else ""


__all__ = [
    "OVERRIDE_MARKER",
    "UNDO_SEARCH_LIMIT",
    "AuditRecord",
    "ConflictFilter",
    "ConflictRecord",
    "FieldEdit",
    "ReviewError",
    "SheetUndo",
    "UndoTarget",
    "accept_machine_value",
    "correct_field",
    "correct_value",
    "count_conflicts",
    "count_conflicts_for_scan",
    "defer",
    "effective_values_for_scan",
    "get_conflict",
    "group_of",
    "history_for",
    "is_override",
    "last_decision",
    "last_resolved_sheet",
    "list_conflicts",
    "provenance_for",
    "provenance_for_scan",
    "recompute_state",
    "reopen",
    "scan_source_path",
    "sheet_resolutions",
    "standing_commands",
    "sync_conflicts",
    "sync_duplicate_identifiers",
    "sync_undefined_set_codes",
    "undo_decision",
    "undo_field_edit",
    "undo_resolved_sheet",
    "validate_reason",
    "validate_reviewer",
]
