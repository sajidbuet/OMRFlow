"""Conflicts, human decisions and the provenance ledger (Phase 6).

Purpose:
    Own every write that can change what a value *means* - and make sure each
    one is atomic, named, reasoned and recorded.

Responsibilities:
    * :func:`sync_conflicts` / :func:`sync_duplicate_identifiers` - idempotent
      detection into storage.
    * :func:`accept_machine_value`, :func:`correct_value`, :func:`defer`,
      :func:`reopen` - the four human actions, each one transaction.
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
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from omr_scanner.database.models import AuditEvent, BatchScan, ReviewConflict
from omr_scanner.domain.review import (
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
from omr_scanner.domain.template import (
    GridFieldDefinition,
    QuestionBlockFieldDefinition,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.services.conflict_policy import (
    detect_conflicts,
    detect_duplicate_identifiers,
)
from omr_scanner.services.scan_export import SheetResolution

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.conflict_policy import ConflictPolicy, DetectedConflict
    from omr_scanner.services.recognition_models import ScanResult

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

    @property
    def allows_value_correction(self) -> bool:
        """Whether a reviewer may supply a replacement value here."""
        return self.conflict_type.allows_value_correction


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


def _to_record(row: ReviewConflict, scan: BatchScan | None) -> ConflictRecord:
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
    observation = found.observation
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
    """
    moment = _now()
    with database.session() as session:
        scans = session.scalars(
            select(BatchScan).where(BatchScan.batch_id == batch_id)
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

        for scan_id, row in existing.items():
            if scan_id in detected or row.state == ConflictState.WITHDRAWN.value:
                continue
            if ConflictState(row.state).is_human_touched:
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
        conflict_type = ConflictType(row.conflict_type)
        if not conflict_type.allows_value_correction:
            raise ReviewError(
                f"Conflict {conflict_id} ({conflict_type.value}) carries no field value",
                user_message=(
                    f"'{conflict_type.label}' is not a value that can be corrected. "
                    "Acknowledge it or defer it instead."
                ),
            )

        previous = _project_provenance(session, row).value
        _append_event(
            session,
            conflict=row,
            action=ReviewAction.CORRECTED,
            reviewer=name,
            previous_value=previous,
            new_value=value,
            reason_code=reason.value,
            reason_text=text_value,
        )
        row.state = ConflictState.RESOLVED.value
        row.updated_at = _now()
        session.flush()
        _LOGGER.info(
            "Conflict %d corrected by %s: %r -> %r (machine value %r preserved)",
            conflict_id,
            name,
            previous,
            value,
            row.machine_value,
        )
        return _project_provenance(session, row)


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
            detail="The earlier decision was withdrawn; the conflict is open again.",
        )
        row.state = ConflictState.OPEN.value
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
# Provenance: the projection
# ----------------------------------------------------------------------
def _project_provenance(session: Session, row: ReviewConflict) -> Provenance:
    """Fold a conflict's ordered events into its current provenance.

    A left-fold over the ledger, not a lookup of a cached column. ``REOPENED``
    clears the decision because it does not
    :attr:`~omr_scanner.domain.review.ReviewAction.sets_effective_value`, which
    is what makes "reopen then decide again" behave correctly without a special
    case anywhere.
    """
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.conflict_id == row.conflict_id)
        .order_by(AuditEvent.event_id)
    ).all()
    return _fold_events(row, [_to_audit(item) for item in events])


def _fold_events(row: ReviewConflict, events: Sequence[AuditRecord]) -> Provenance:
    """Reduce an ordered history to one :class:`Provenance`."""
    decided: AuditRecord | None = None
    for event in events:
        if event.action.sets_effective_value:
            decided = event
        elif event.action is ReviewAction.REOPENED:
            decided = None

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


def recompute_state(events: Sequence[AuditRecord]) -> ConflictState:
    """Derive a conflict's state from its history alone.

    Exists so a test can prove the cached
    :attr:`~omr_scanner.database.models.ReviewConflict.state` never drifts from
    the ledger that justifies it. Nothing in the application reads this at
    runtime - the cached column is there to make a ten-thousand-row queue fast.
    """
    state = ConflictState.OPEN
    for event in events:
        if event.action in (ReviewAction.ACCEPTED, ReviewAction.CORRECTED):
            state = ConflictState.RESOLVED
        elif event.action is ReviewAction.DEFERRED:
            state = ConflictState.DEFERRED
        elif event.action is ReviewAction.REOPENED:
            state = ConflictState.OPEN
        elif event.action is ReviewAction.WITHDRAWN:
            state = ConflictState.WITHDRAWN
    return state


def provenance_for(database: ProjectDatabase, conflict_id: int) -> Provenance:
    """Return where one conflict's current value came from."""
    with database.session() as session:
        return _project_provenance(session, _require_conflict(session, conflict_id))


def history_for(database: ProjectDatabase, conflict_id: int) -> tuple[AuditRecord, ...]:
    """Return one conflict's complete history, oldest first."""
    with database.session() as session:
        events = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.conflict_id == conflict_id)
            .order_by(AuditEvent.event_id)
        ).all()
        return tuple(_to_audit(item) for item in events)


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
    """

    states: tuple[ConflictState, ...] = ()
    conflict_types: tuple[ConflictType, ...] = ()
    scan_id: int | None = None
    search: str = ""
    include_withdrawn: bool = False


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
        Detached value objects, ordered by severity, then sheet, then field -
        deterministic, so paging is stable and a reopened queue looks the same.

    Ordering and filtering happen in SQL. A batch of ten thousand sheets can
    carry thousands of conflicts, and loading them all to sort them in Python
    is the difference between a queue that opens instantly and one that does
    not.
    """
    rules = filters if filters is not None else ConflictFilter()
    with database.session() as session:
        statement = (
            select(ReviewConflict, BatchScan)
            .join(BatchScan, BatchScan.scan_id == ReviewConflict.scan_id, isouter=True)
            .where(ReviewConflict.batch_id == batch_id)
        )
        statement = _apply_filters(statement, rules)
        statement = statement.order_by(
            ReviewConflict.severity.desc(),
            ReviewConflict.scan_id,
            ReviewConflict.zone_id,
            ReviewConflict.group_key,
            ReviewConflict.conflict_id,
        )
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        return tuple(
            _to_record(conflict, scan) for conflict, scan in session.execute(statement).all()
        )


def _apply_filters(statement: Any, rules: ConflictFilter) -> Any:
    """Apply a :class:`ConflictFilter` to a select over conflicts."""
    statement = _resolution_only(statement)
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
    """
    with database.session() as session:
        by_state = {
            str(state): int(count)
            for state, count in session.execute(
                _resolution_only(
                    select(ReviewConflict.state, func.count())
                    .where(ReviewConflict.batch_id == batch_id)
                    .group_by(ReviewConflict.state)
                )
            ).all()
        }
        by_type = {
            str(kind): int(count)
            for kind, count in session.execute(
                _resolution_only(
                    select(ReviewConflict.conflict_type, func.count())
                    .where(ReviewConflict.batch_id == batch_id)
                    .where(ReviewConflict.state != ConflictState.WITHDRAWN.value)
                    .group_by(ReviewConflict.conflict_type)
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
        return _to_record(row, session.get(BatchScan, row.scan_id))


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
        for conflict, scan in rows:
            path = Path(scan.source_path)
            entry = decided.setdefault(
                path,
                {"identifier": "", "set_code": "", "answers": {}, "unresolved": 0,
                 "reviewed": False},
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
    nothing about the other five - so a positional correction is applied by
    substitution into the machine's own string. A whole-field decision
    replaces it outright.
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
    if conflict.group_key == WHOLE_FIELD or conflict.zone_id not in identifier_zones:
        entry[slot] = found.value
        return
    current = entry[slot] or conflict.machine_value
    entry[slot] = _substitute_position(current, conflict.group_key, found.value)


def _substitute_position(value: str, position: int, replacement: str) -> str:
    """Replace one printed position of a field value.

    Positions are *printed columns*, and a column's symbol may be more than one
    character (a set code whose options are ``"10"``, ``"11"``, ``"12"``), so
    this cannot index into the string. It rebuilds from the machine's own
    characters where it can and falls back to appending, which is the honest
    behaviour for a value whose length the correction disagrees with.
    """
    characters = list(value)
    if 0 <= position < len(characters):
        characters[position] = replacement
        return "".join(characters)
    return value


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

        conflicts = session.scalars(
            select(ReviewConflict).where(ReviewConflict.batch_id == batch_id)
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
            found[conflict.scan_id] = replace(
                current,
                value=_identifier_after(current.value, conflict, decided.value),
                source=ValueSource.HUMAN,
                reviewer=decided.reviewer,
                reason=decided.reason,
            )
    return found


_SET_CODE_IS_UNKNOWN: frozenset[ConflictType] = frozenset(
    {
        ConflictType.SET_CODE_BLANK,
        ConflictType.SET_CODE_MULTIPLE,
        ConflictType.SET_CODE_UNCERTAIN,
        ConflictType.SET_CODE_UNREADABLE,
        ConflictType.SET_CODE_LOW_CONFIDENCE,
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
    unchanged: a positional correction substitutes one printed position, which
    is why it cannot simply index into the string.
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

        conflicts = session.scalars(
            select(ReviewConflict).where(ReviewConflict.batch_id == batch_id)
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
            found[conflict.scan_id] = replace(
                current,
                value=_identifier_after(current.value, conflict, decided.value),
                source=ValueSource.HUMAN,
                reviewer=decided.reviewer,
                reason=decided.reason,
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


def _identifier_after(current: str, conflict: ReviewConflict, decided: str) -> str:
    """Apply one resolved identifier decision to a sheet's ID.

    The same rule :func:`_apply_decision` uses for the CSV export, and for the
    same reason: correcting the third digit of a roll number says nothing about
    the other five, so a positional decision substitutes rather than replaces.
    """
    if conflict.group_key == WHOLE_FIELD:
        return decided
    return _substitute_position(current or conflict.machine_value, conflict.group_key, decided)


def scan_source_path(database: ProjectDatabase, scan_id: int) -> str:
    """Return one scan's source path, for the review workspace to load."""
    with database.session() as session:
        row = session.get(BatchScan, scan_id)
        return row.source_path if row is not None else ""


__all__ = [
    "AuditRecord",
    "ConflictFilter",
    "ConflictRecord",
    "ReviewError",
    "accept_machine_value",
    "correct_value",
    "count_conflicts",
    "count_conflicts_for_scan",
    "defer",
    "effective_values_for_scan",
    "get_conflict",
    "history_for",
    "list_conflicts",
    "provenance_for",
    "recompute_state",
    "reopen",
    "scan_source_path",
    "sheet_resolutions",
    "sync_conflicts",
    "sync_duplicate_identifiers",
    "validate_reason",
    "validate_reviewer",
]
