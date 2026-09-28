"""Reject & Rescan: the lifecycle of an unusable scan, and its replacement.

Purpose:
    Own every write that changes whether a scan may contribute to a result -
    rejecting it, undoing that, confirming (or withdrawing) its rescan, linking
    an exact re-import back to it, and eventually quarantining or deleting the
    superseded image - and make each one atomic, named, reasoned and recorded.

Responsibilities:
    * :func:`reject_scan`, :func:`undo_reject`, :func:`confirm_replacement`,
      :func:`remove_replacement` - the operator's actions, one transaction
      each, each appending to the shared audit ledger.
    * :func:`lifecycle_states` / :func:`ineligible_scan_ids` - **the single
      answer to "may this scan contribute to a result"**, read by
      reconciliation, scoring, duplicate detection, the review queue and the
      exports.
    * :func:`replacement_candidates` / :func:`possible_rescans` - conservative
      suggestions, by effective Student ID only. Never acted on here.
    * :func:`sync_reimports` - exact-hash re-imports of rejected content.
    * :func:`plan_purge` / :func:`execute_purge` / :func:`owned_path` - *Purge
      Rejects*, constrained to files the project owns.
    * :func:`record_incomplete_export` - the audited acknowledgement that a
      final export was produced while rescans were outstanding.

What does NOT belong here:
    * Qt. Deciding the effective Student ID or set code (the review ledger's
      job, :mod:`omr_scanner.services.review_store`). Deciding which scripts a
      set reconciles (:mod:`omr_scanner.services.reconciliation_store`, which
      asks here for lifecycle states and nothing else).

The invariants this module enforces - in the service, not in the GUI:
    1. A rejected scan is never result-eligible while rejected.
    2. A superseded scan is never result-eligible.
    3. Confirming a replacement does not reactivate the original.
    4. Re-importing the original's bytes does not reactivate it
       (:func:`sync_reimports`).
    5. Purging or quarantining the original never touches the replacement, or
       any file another scan still references, or any file outside the
       project's own storage.
    6. Undoing a rejection is refused while a replacement is linked: the link
       must be withdrawn explicitly first (:func:`remove_replacement`), after
       which two active scans with one Student ID are exactly what the
       existing duplicate detection is for.

Who writes:
    The GUI thread, through these functions, as for every other review
    decision. Worker processes never call anything here.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    CandidateRoster,
    ReconciliationRun,
    ScanJobStatus,
    ScanRejection,
)
from omr_scanner.domain.project import ProjectLayout
from omr_scanner.domain.scan_lifecycle import (
    EXPORT_ENTITY,
    LIFECYCLE_ENTITY,
    FileState,
    LifecycleAction,
    LifecycleState,
    PurgeFile,
    PurgeItem,
    PurgeMode,
    PurgeOutcome,
    PurgePlan,
    RejectionReason,
    ReplacementCandidate,
    RescanCase,
    RescanCounts,
)
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import review_store
from omr_scanner.services.scan_provenance import hash_file, is_virtual_source

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Mapping, Sequence

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.services.review_store import EffectiveIdentifier

_LOGGER = logging.getLogger(__name__)

QUARANTINE_DIR_NAME = "quarantine"
"""The project folder *Purge Rejects* moves images into. Created on first use,
beside the managed folders, so an ordinary project never gains an empty one."""

QUARANTINE_SUBDIR = "rejected_scans"

_PROCESSED = frozenset({ScanJobStatus.COMPLETED.value, ScanJobStatus.WARNING.value})
"""Processing states a replacement must have reached: it must have been read."""


class LifecycleError(OMRScannerError):
    """A Reject & Rescan action was refused.

    Always carries a ``user_message`` saying what to do instead: the operator
    is mid-decision.
    """


def _now() -> datetime:
    """Current UTC time. One place, so every timestamp agrees."""
    return datetime.now(UTC)


def _state(row: ScanRejection | None) -> LifecycleState:
    """The lifecycle state one stored row stands for; no row is active."""
    if row is None:
        return LifecycleState.ACTIVE
    try:
        return LifecycleState(row.state)
    except ValueError:  # pragma: no cover - a state from a newer build
        # Unknown means "not known to be eligible" - the safe reading.
        return LifecycleState.REJECTED_PENDING_RESCAN


# ----------------------------------------------------------------------
# Reads: the single answer to "may this scan contribute"
# ----------------------------------------------------------------------
def lifecycle_states(database: ProjectDatabase, batch_id: str) -> dict[int, LifecycleState]:
    """Return every scan in a batch that is **not** active, with its state.

    Args:
        database: The open project database.
        batch_id: The batch.

    Returns:
        ``scan_id -> state`` for rejected, superseded and re-imported scans. A
        scan absent from the mapping is active. One query, whatever the size
        of the batch.
    """
    with database.session() as session:
        rows = session.execute(
            select(ScanRejection.scan_id, ScanRejection.state)
            .where(ScanRejection.batch_id == batch_id)
            .where(ScanRejection.state != LifecycleState.ACTIVE.value)
        ).all()
    found: dict[int, LifecycleState] = {}
    for scan_id, state in rows:
        try:
            found[int(scan_id)] = LifecycleState(state)
        except ValueError:  # pragma: no cover - see _state
            found[int(scan_id)] = LifecycleState.REJECTED_PENDING_RESCAN
    return found


def ineligible_scan_ids(database: ProjectDatabase, batch_id: str) -> frozenset[int]:
    """The scans of a batch that must not contribute to any result."""
    return frozenset(lifecycle_states(database, batch_id))


def removed_images(database: ProjectDatabase, batch_id: str) -> frozenset[int]:
    """Scans whose image *Purge Rejects* quarantined or deleted, on purpose."""
    with database.session() as session:
        return frozenset(
            int(item)
            for item in session.scalars(
                select(ScanRejection.scan_id)
                .where(ScanRejection.batch_id == batch_id)
                .where(ScanRejection.file_state != FileState.PRESENT.value)
            ).all()
        )


def state_of(database: ProjectDatabase, scan_id: int) -> LifecycleState:
    """One scan's lifecycle state."""
    with database.session() as session:
        return _state(_row_for(session, scan_id))


def _row_for(session: Session, scan_id: int) -> ScanRejection | None:
    return session.scalars(
        select(ScanRejection).where(ScanRejection.scan_id == scan_id).limit(1)
    ).first()


def _to_case(
    row: ScanRejection,
    *,
    replacement_name: str = "",
    live_id: EffectiveIdentifier | None = None,
    live_set: EffectiveIdentifier | None = None,
) -> RescanCase:
    """Project a stored row into a detached case.

    ``live_id`` / ``live_set`` are the *current* effective values from the
    review ledger. When given they are what the case is known by, so a
    correction made on the Resolve stage after rejection is honoured; the
    stored snapshot is what history shows once the scan is gone.
    """
    try:
        reason: RejectionReason | None = RejectionReason(row.reason_code)
    except ValueError:
        reason = None
    try:
        file_state = FileState(row.file_state)
    except ValueError:  # pragma: no cover - a value from a newer build
        file_state = FileState.PRESENT
    recognised_id = row.recognised_candidate_id
    if live_id is not None:
        recognised_id = "" if live_id.unresolved else live_id.value
    recognised_set = row.recognised_set_code
    if live_set is not None:
        recognised_set = "" if live_set.unresolved else live_set.value
    return RescanCase(
        scan_id=row.scan_id,
        batch_id=row.batch_id,
        state=_state(row),
        reason=reason,
        note=row.note,
        declared_candidate_id=row.declared_candidate_id,
        declared_set_code=row.declared_set_code,
        recognised_candidate_id=recognised_id,
        recognised_set_code=recognised_set,
        source_name=row.source_name,
        source_path=row.source_path,
        content_sha256=row.content_sha256,
        rejected_by=row.rejected_by,
        rejected_at=row.rejected_at,
        replacement_scan_id=row.replacement_scan_id,
        replacement_name=replacement_name,
        replaced_by=row.replaced_by,
        replaced_at=row.replaced_at,
        reimport_of_scan_id=row.reimport_of_scan_id,
        file_state=file_state,
        file_action_at=row.file_action_at,
    )


def cases_by_scan(
    database: ProjectDatabase, batch_id: str, *, live: bool = True
) -> dict[int, RescanCase]:
    """Every non-active record of a batch, keyed by scan id.

    Args:
        database: The open project database.
        batch_id: The batch.
        live: Read the scans' current effective Student ID and set code from
            the review ledger (the default). ``False`` returns the snapshots
            taken at rejection time, which is cheaper and is what history
            wants.
    """
    identifiers = review_store.effective_identifiers(database, batch_id) if live else {}
    set_codes = review_store.effective_set_codes(database, batch_id) if live else {}
    with database.session() as session:
        rows = session.scalars(
            select(ScanRejection)
            .where(ScanRejection.batch_id == batch_id)
            .where(ScanRejection.state != LifecycleState.ACTIVE.value)
            .order_by(ScanRejection.rejected_at, ScanRejection.scan_id)
        ).all()
        names = _names(session, [row.replacement_scan_id for row in rows])
        return {
            row.scan_id: _to_case(
                row,
                replacement_name=names.get(row.replacement_scan_id or -1, ""),
                live_id=identifiers.get(row.scan_id),
                live_set=set_codes.get(row.scan_id),
            )
            for row in rows
        }


def _names(session: Session, scan_ids: Iterable[int | None]) -> dict[int, str]:
    wanted = [item for item in scan_ids if item is not None]
    if not wanted:
        return {}
    return {
        int(scan_id): str(name)
        for scan_id, name in session.execute(
            select(BatchScan.scan_id, BatchScan.filename).where(
                BatchScan.scan_id.in_(wanted)
            )
        ).all()
    }


def list_cases(
    database: ProjectDatabase,
    batch_id: str,
    *,
    include_completed: bool = True,
    include_reimports: bool = False,
) -> tuple[RescanCase, ...]:
    """The batch's rescan cases, for the *Rejected / Rescan* queue.

    Args:
        database: The open project database.
        batch_id: The batch.
        include_completed: Include cases whose rescan has been confirmed.
        include_reimports: Include exact re-imports of rejected content. They
            are not rescan cases - nothing is awaited - so they are listed
            only when asked for.

    Returns:
        Outstanding cases first, then completed ones, each oldest first.
    """
    cases = list(cases_by_scan(database, batch_id).values())
    kept = [
        item
        for item in cases
        if item.state is LifecycleState.REJECTED_PENDING_RESCAN
        or (include_completed and item.state is LifecycleState.SUPERSEDED_BY_REPLACEMENT)
        or (include_reimports and item.state is LifecycleState.REIMPORT_OF_REJECTED)
    ]
    kept.sort(key=lambda item: (not item.is_outstanding, item.rejected_at or _now(), item.scan_id))
    return tuple(kept)


def get_case(database: ProjectDatabase, scan_id: int) -> RescanCase | None:
    """One scan's rejection record, or ``None``.

    ``None`` when the scan was never rejected, or its rejection was undone.
    """
    with database.session() as session:
        row = _row_for(session, scan_id)
        if row is None or _state(row) is LifecycleState.ACTIVE:
            return None
        batch_id = row.batch_id
    return cases_by_scan(database, batch_id).get(scan_id)


def count_cases(database: ProjectDatabase, batch_id: str) -> RescanCounts:
    """How many rejected scans the batch has, by state. Counted in SQL."""
    states = lifecycle_states(database, batch_id)
    return RescanCounts(
        outstanding=sum(
            1 for item in states.values() if item is LifecycleState.REJECTED_PENDING_RESCAN
        ),
        superseded=sum(
            1 for item in states.values() if item is LifecycleState.SUPERSEDED_BY_REPLACEMENT
        ),
        reimports=sum(
            1 for item in states.values() if item is LifecycleState.REIMPORT_OF_REJECTED
        ),
    )


def outstanding_for_set(
    database: ProjectDatabase, batch_id: str, set_code: str
) -> tuple[RescanCase, ...]:
    """The outstanding rescans that may belong to one set.

    A case whose set code (declared, or the scan's current effective code) is
    this set's belongs to it. So does one whose set code is **unknown**, or
    names no defined set: such a sheet could be anybody's, and a report that
    ignored it would look complete when it might not be.
    """
    from omr_scanner.services import project_sets

    defined = {item.code for item in project_sets.list_sets(database)}
    return tuple(
        case
        for case in cases_by_scan(database, batch_id).values()
        if case.is_outstanding
        and (
            case.set_code == set_code
            or not case.set_code
            or (bool(defined) and case.set_code not in defined)
        )
    )


# ----------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------
def _append_event(
    session: Session,
    *,
    scan_id: int,
    batch_id: str,
    action: LifecycleAction,
    reviewer: str = "",
    previous_value: str = "",
    new_value: str = "",
    machine_value: str = "",
    reason_code: str = "",
    reason_text: str = "",
    detail: str = "",
    entity_type: str = LIFECYCLE_ENTITY,
    entity_id: str | None = None,
) -> AuditEvent:
    """Append one lifecycle entry to the shared, append-only ledger.

    The only way this module writes an
    :class:`~omr_scanner.database.models.AuditEvent`; like every other writer
    of that table it offers no update or delete.
    """
    event = AuditEvent(
        occurred_at=_now(),
        batch_id=batch_id,
        scan_id=scan_id,
        conflict_id=0,
        entity_type=entity_type,
        entity_id=entity_id if entity_id is not None else str(scan_id),
        action=action.value,
        reviewer=reviewer,
        previous_value=previous_value,
        new_value=new_value,
        machine_value=machine_value,
        reason_code=reason_code,
        reason_text=reason_text,
        detail=detail,
    )
    session.add(event)
    session.flush()
    return event


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One lifecycle ledger entry, as a history view reads it."""

    event_id: int
    occurred_at: datetime
    scan_id: int
    action: LifecycleAction
    reviewer: str
    previous_value: str
    new_value: str
    reason_code: str
    reason_text: str
    detail: str

    @property
    def describe(self) -> str:
        """One line for a history list."""
        who = self.reviewer or "OMRFlow"
        when = self.occurred_at.isoformat(timespec="seconds")
        text = f"{when} - {self.action.label} ({who})"
        if self.reason_text:
            text += f": {self.reason_text}"
        return text


def lifecycle_history(database: ProjectDatabase, scan_id: int) -> tuple[LifecycleEvent, ...]:
    """Every lifecycle event recorded about one scan, oldest first."""
    with database.session() as session:
        rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_type == LIFECYCLE_ENTITY)
            .where(AuditEvent.entity_id == str(scan_id))
            .order_by(AuditEvent.event_id)
        ).all()
        found: list[LifecycleEvent] = []
        for row in rows:
            try:
                action = LifecycleAction(row.action)
            except ValueError:  # pragma: no cover - an action from a newer build
                continue
            found.append(
                LifecycleEvent(
                    event_id=row.event_id,
                    occurred_at=row.occurred_at,
                    scan_id=row.scan_id,
                    action=action,
                    reviewer=row.reviewer,
                    previous_value=row.previous_value,
                    new_value=row.new_value,
                    reason_code=row.reason_code,
                    reason_text=row.reason_text,
                    detail=row.detail,
                )
            )
        return tuple(found)


# ----------------------------------------------------------------------
# Rejecting, and taking it back
# ----------------------------------------------------------------------
def _require_scan(session: Session, scan_id: int) -> BatchScan:
    scan = session.get(BatchScan, scan_id)
    if scan is None:
        raise LifecycleError(
            f"Scan {scan_id} does not exist",
            user_message="That scan is no longer in this project.",
        )
    return scan


def reject_scan(
    database: ProjectDatabase,
    scan_id: int,
    *,
    reviewer: str,
    reason: RejectionReason,
    note: str = "",
    declared_candidate_id: str = "",
    declared_set_code: str = "",
) -> RescanCase:
    """Mark one scan unusable, pending a rescan.

    Args:
        database: The open project database.
        scan_id: The scan to reject.
        reviewer: Who decided. Required.
        reason: Why. Required; :attr:`RejectionReason.OTHER` needs a note.
        note: The operator's own words.
        declared_candidate_id: A Student ID the operator can read off the
            paper, **for identifying this case only** - it never becomes the
            scan's effective Student ID, and a recognised identity is not
            required to reject at all.
        declared_set_code: Likewise for the set code. Must be one of the
            project's sets when the project defines any.

    Returns:
        The new case.

    Raises:
        LifecycleError: The scan is already rejected, superseded or a
            re-import, the note is missing for *Other*, or the declared set
            code is not a defined set.
        omr_scanner.services.review_store.ReviewError: No reviewer.

    **Nothing is deleted or rewritten.** The scan row, its recognition result,
    its conflicts and their decisions stay exactly as they were - which is
    what makes :func:`undo_reject` a faithful restoration rather than a
    reconstruction. The image stays where it is.
    """
    name = review_store.validate_reviewer(reviewer)
    note_text = note.strip()
    if reason.requires_note and not note_text:
        raise LifecycleError(
            "Rejection reason 'other' needs a note",
            user_message="Describe why the scan is unusable when choosing 'Other'.",
        )
    declared_id = declared_candidate_id.strip()
    declared_set = declared_set_code.strip()
    if declared_set:
        from omr_scanner.services import project_sets

        defined = {item.code for item in project_sets.list_sets(database)}
        if defined and declared_set not in defined:
            raise LifecycleError(
                f"Declared set code {declared_set!r} is not a defined set",
                user_message=(
                    f"'{declared_set}' is not one of this project's sets "
                    f"({', '.join(sorted(defined))}). Leave it empty if the set "
                    "is not known."
                ),
            )

    with database.session() as session:
        scan = _require_scan(session, scan_id)
        batch_id = scan.batch_id
    identifiers = review_store.effective_identifiers(database, batch_id)
    set_codes = review_store.effective_set_codes(database, batch_id)
    live_id = identifiers.get(scan_id)
    live_set = set_codes.get(scan_id)

    moment = _now()
    with database.session() as session:
        scan = _require_scan(session, scan_id)
        row = _row_for(session, scan_id)
        previous = _state(row)
        if previous is not LifecycleState.ACTIVE:
            raise LifecycleError(
                f"Scan {scan_id} is already {previous.value}",
                user_message=(
                    f"{scan.filename or 'This scan'} is already "
                    f"{previous.label.lower()}."
                ),
            )
        if row is None:
            row = ScanRejection(scan_id=scan_id, batch_id=batch_id, rejected_at=moment)
            session.add(row)
        row.state = LifecycleState.REJECTED_PENDING_RESCAN.value
        row.reason_code = reason.value
        row.note = note_text
        row.declared_candidate_id = declared_id
        row.declared_set_code = declared_set
        row.recognised_candidate_id = live_id.value if live_id is not None else ""
        row.recognised_set_code = live_set.value if live_set is not None else ""
        row.source_name = scan.filename or ""
        row.source_path = scan.source_path or ""
        row.content_sha256 = scan.content_sha256 or ""
        row.rejected_by = name
        row.rejected_at = moment
        row.replacement_scan_id = None
        row.replaced_by = ""
        row.replaced_at = None
        row.reimport_of_scan_id = None
        row.file_state = FileState.PRESENT.value
        row.updated_at = moment
        session.flush()
        _append_event(
            session,
            scan_id=scan_id,
            batch_id=batch_id,
            action=LifecycleAction.REJECTED,
            reviewer=name,
            previous_value=previous.value,
            new_value=LifecycleState.REJECTED_PENDING_RESCAN.value,
            machine_value=row.recognised_candidate_id,
            reason_code=reason.value,
            reason_text=note_text,
            detail=_identity_detail(
                "Rejected; a rescan is required. No longer contributes to any result.",
                declared_id=declared_id,
                declared_set=declared_set,
                recognised_id=row.recognised_candidate_id,
                recognised_set=row.recognised_set_code,
            ),
        )
        case = _to_case(row, live_id=live_id, live_set=live_set)
    _LOGGER.info("Scan %d rejected by %s (%s); rescan required", scan_id, name, reason.value)
    _after_change(database, batch_id)
    return case


def _identity_detail(
    sentence: str,
    *,
    declared_id: str,
    declared_set: str,
    recognised_id: str,
    recognised_set: str,
) -> str:
    """A self-describing ledger sentence naming every identity involved."""
    parts = [sentence]
    parts.append(
        f"Recognised Student ID '{recognised_id or '(none)'}', set code "
        f"'{recognised_set or '(none)'}'."
    )
    if declared_id or declared_set:
        parts.append(
            f"Operator-supplied case identity: Student ID '{declared_id or '(none)'}', "
            f"set code '{declared_set or '(none)'}' - for this case only, not the "
            "scan's effective value."
        )
    return " ".join(parts)


def undo_reject(
    database: ProjectDatabase, scan_id: int, *, reviewer: str, note: str = ""
) -> None:
    """Return a rejected scan to active, exactly as it was.

    Raises:
        LifecycleError: The scan is not awaiting a rescan. In particular a
            **superseded** scan is refused: undoing it while its replacement
            is linked would leave two result-eligible scripts for one sheet.
            Withdraw the link with :func:`remove_replacement` first - then the
            two-scan situation is the ordinary duplicate one, and the existing
            duplicate detection raises it. An image already quarantined or
            purged is refused too: there is nothing left to reactivate.
        omr_scanner.services.review_store.ReviewError: No reviewer.
    """
    name = review_store.validate_reviewer(reviewer)
    moment = _now()
    with database.session() as session:
        row = _row_for(session, scan_id)
        state = _state(row)
        if row is None or state is LifecycleState.ACTIVE:
            raise LifecycleError(
                f"Scan {scan_id} is not rejected",
                user_message="That scan is not rejected, so there is nothing to undo.",
            )
        if state is LifecycleState.SUPERSEDED_BY_REPLACEMENT:
            raise LifecycleError(
                f"Scan {scan_id} has a confirmed replacement",
                user_message=(
                    "This rejected scan has a confirmed rescan. Undoing the "
                    "rejection now would leave two valid scripts for one sheet. "
                    "Remove the replacement link first; the two scans are then "
                    "reviewed as an ordinary duplicate."
                ),
            )
        if state is LifecycleState.REIMPORT_OF_REJECTED:
            raise LifecycleError(
                f"Scan {scan_id} is a re-import of rejected content",
                user_message=(
                    "This scan is byte-for-byte the same file as a rejected scan. "
                    "Importing it again does not make it usable."
                ),
            )
        if row.file_state != FileState.PRESENT.value:
            raise LifecycleError(
                f"Scan {scan_id} image is {row.file_state}",
                user_message="This scan's image has been removed; it cannot be reactivated.",
            )
        batch_id = row.batch_id
        row.state = LifecycleState.ACTIVE.value
        row.updated_at = moment
        _append_event(
            session,
            scan_id=scan_id,
            batch_id=batch_id,
            action=LifecycleAction.REJECT_UNDONE,
            reviewer=name,
            previous_value=state.value,
            new_value=LifecycleState.ACTIVE.value,
            reason_text=note.strip(),
            detail=(
                "Rejection undone; the scan is active again with every earlier "
                "recognition result, conflict and decision as it was."
            ),
        )
    _LOGGER.info("Scan %d: rejection undone by %s", scan_id, name)
    _after_change(database, batch_id)


# ----------------------------------------------------------------------
# Replacement
# ----------------------------------------------------------------------
def _replacement_ids(session: Session) -> set[int]:
    """Every scan that is currently a confirmed replacement."""
    return {
        int(item)
        for item in session.scalars(
            select(ScanRejection.replacement_scan_id).where(
                ScanRejection.replacement_scan_id.is_not(None)
            )
        ).all()
        if item is not None
    }


def _candidates_for(
    case: RescanCase,
    scans: Sequence[BatchScan],
    *,
    identifiers: Mapping[int, EffectiveIdentifier],
    set_codes: Mapping[int, EffectiveIdentifier],
    ineligible: frozenset[int],
    taken: set[int],
) -> tuple[ReplacementCandidate, ...]:
    """Scans that might be ``case``'s rescan. See :func:`replacement_candidates`."""
    identity = case.identity
    if not identity:
        return ()
    wanted_set = case.set_code
    found: list[ReplacementCandidate] = []
    for scan in scans:
        if (
            scan.scan_id == case.scan_id
            or scan.scan_id in ineligible
            or scan.scan_id in taken
            or scan.status not in _PROCESSED
        ):
            continue
        if case.content_sha256 and scan.content_sha256 == case.content_sha256:
            continue  # the same file again is not a rescan
        read = identifiers.get(scan.scan_id)
        if read is None or read.unresolved or read.value != identity:
            continue
        code = set_codes.get(scan.scan_id)
        code_value = "" if code is None or code.unresolved else code.value
        agrees = None if not (wanted_set and code_value) else code_value == wanted_set
        found.append(
            ReplacementCandidate(
                scan_id=scan.scan_id,
                source_name=scan.filename or "",
                candidate_id=read.value,
                set_code=code_value,
                set_code_agrees=agrees,
            )
        )
    # Agreeing set code first, then the newest scan - a rescan arrives after
    # the sheet it replaces.
    found.sort(key=lambda item: (item.set_code_agrees is not True, -item.scan_id))
    return tuple(found)


def possible_rescans(
    database: ProjectDatabase, batch_id: str
) -> dict[int, tuple[ReplacementCandidate, ...]]:
    """Every outstanding case's possible rescans, read once for the batch."""
    identifiers = review_store.effective_identifiers(database, batch_id)
    set_codes = review_store.effective_set_codes(database, batch_id)
    cases = cases_by_scan(database, batch_id)
    ineligible = frozenset(cases)
    with database.session() as session:
        scans = session.scalars(
            select(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .order_by(BatchScan.batch_index)
        ).all()
        taken = _replacement_ids(session)
        return {
            scan_id: _candidates_for(
                case,
                scans,
                identifiers=identifiers,
                set_codes=set_codes,
                ineligible=ineligible,
                taken=taken,
            )
            for scan_id, case in cases.items()
            if case.is_outstanding
        }


def replacement_candidates(
    database: ProjectDatabase, scan_id: int
) -> tuple[ReplacementCandidate, ...]:
    """Scans in the same batch that might be one rejected sheet's rescan.

    **Conservative, by construction.** A scan is suggested only when its
    *effective* Student ID - after every Resolve-stage correction - is fully
    read and equals the case's identity (the operator's declared ID, or the
    rejected scan's own). Set code is further evidence, reported but never
    required. **File names are never compared**: scanners, computers and
    watched folders name files however they like.

    Excluded: the rejected scan itself, scans not yet read, scans that are
    themselves rejected or superseded, scans already confirmed as another
    case's replacement, and a scan with the rejected scan's exact bytes (a
    re-import, not a rescan).

    An unknown-identity case has no candidates; :func:`association_choices`
    lists what an operator may link by hand.
    """
    with database.session() as session:
        row = _row_for(session, scan_id)
        if row is None or _state(row) is not LifecycleState.REJECTED_PENDING_RESCAN:
            return ()
        batch_id = row.batch_id
    return possible_rescans(database, batch_id).get(scan_id, ())


def association_choices(
    database: ProjectDatabase, scan_id: int
) -> tuple[ReplacementCandidate, ...]:
    """Every scan an operator could link to a rejected case by hand.

    For the case the matcher cannot help with - an unreadable Student ID - an
    operator holding the physical sheet can still say which new scan is its
    rescan. Every active, read scan of the batch that is not already a
    replacement is offered; suggestions come first. The choice is never made
    here.
    """
    with database.session() as session:
        row = _row_for(session, scan_id)
        if row is None or _state(row) is not LifecycleState.REJECTED_PENDING_RESCAN:
            return ()
        batch_id = row.batch_id
    suggested = {item.scan_id: item for item in replacement_candidates(database, scan_id)}
    identifiers = review_store.effective_identifiers(database, batch_id)
    set_codes = review_store.effective_set_codes(database, batch_id)
    ineligible = ineligible_scan_ids(database, batch_id)
    choices: list[ReplacementCandidate] = list(suggested.values())
    with database.session() as session:
        taken = _replacement_ids(session)
        original_hash = row.content_sha256
        for scan in session.scalars(
            select(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .order_by(BatchScan.batch_index.desc())
        ).all():
            if (
                scan.scan_id in suggested
                or scan.scan_id == scan_id
                or scan.scan_id in ineligible
                or scan.scan_id in taken
                or scan.status not in _PROCESSED
                or (original_hash and scan.content_sha256 == original_hash)
            ):
                continue
            read = identifiers.get(scan.scan_id)
            code = set_codes.get(scan.scan_id)
            choices.append(
                ReplacementCandidate(
                    scan_id=scan.scan_id,
                    source_name=scan.filename or "",
                    candidate_id=read.value if read is not None else "",
                    set_code=code.value if code is not None else "",
                )
            )
    return tuple(choices)


def confirm_replacement(
    database: ProjectDatabase,
    original_scan_id: int,
    replacement_scan_id: int,
    *,
    reviewer: str,
    note: str = "",
) -> RescanCase:
    """Record, explicitly, that one scan is a rejected sheet's rescan.

    Args:
        database: The open project database.
        original_scan_id: The rejected scan.
        replacement_scan_id: Its rescan. Need not be a suggested candidate -
            an unknown-identity case is linked by hand - but it must be an
            active, read scan in the same batch.
        reviewer: Who confirmed it. Required.
        note: Optional words.

    Returns:
        The case, now superseded.

    Raises:
        LifecycleError: The original is not awaiting a rescan; the replacement
            is the original, is in another batch, has not been read, is itself
            rejected or superseded, already replaces another sheet, or has the
            original's exact bytes.

    **The original is not reactivated**, now or by any later step: it becomes
    :attr:`~LifecycleState.SUPERSEDED_BY_REPLACEMENT`, which is never
    result-eligible. The replacement was already an ordinary active scan and
    stays one; what confirmation adds is the provenance link and the end of
    the outstanding case.
    """
    name = review_store.validate_reviewer(reviewer)
    moment = _now()
    with database.session() as session:
        row = _row_for(session, original_scan_id)
        if row is None or _state(row) is not LifecycleState.REJECTED_PENDING_RESCAN:
            raise LifecycleError(
                f"Scan {original_scan_id} is not awaiting a rescan",
                user_message="That scan is not a rejected sheet awaiting a rescan.",
            )
        if replacement_scan_id == original_scan_id:
            raise LifecycleError(
                "A scan cannot replace itself",
                user_message="Choose the new scan, not the rejected one.",
            )
        replacement = _require_scan(session, replacement_scan_id)
        if replacement.batch_id != row.batch_id:
            raise LifecycleError(
                "The replacement belongs to another batch",
                user_message=(
                    "The rescan must be read into the same batch as the rejected "
                    "sheet, so that attendance and results count it."
                ),
            )
        if _state(_row_for(session, replacement_scan_id)) is not LifecycleState.ACTIVE:
            raise LifecycleError(
                f"Scan {replacement_scan_id} is not active",
                user_message="That scan is itself rejected and cannot be a replacement.",
            )
        if replacement_scan_id in _replacement_ids(session):
            raise LifecycleError(
                f"Scan {replacement_scan_id} already replaces another sheet",
                user_message="That scan is already the confirmed rescan of another sheet.",
            )
        if replacement.status not in _PROCESSED:
            raise LifecycleError(
                f"Scan {replacement_scan_id} has not been read",
                user_message=(
                    "That scan has not been read successfully yet. Process it on "
                    "the Scan stage first."
                ),
            )
        original_hash = row.content_sha256
        if original_hash and replacement.content_sha256 == original_hash:
            raise LifecycleError(
                "Replacement has the original's exact content",
                user_message=(
                    "That file is byte-for-byte the rejected scan. A rescan is a "
                    "new scan of the paper."
                ),
            )
        batch_id = row.batch_id
        row.state = LifecycleState.SUPERSEDED_BY_REPLACEMENT.value
        row.replacement_scan_id = replacement_scan_id
        row.replaced_by = name
        row.replaced_at = moment
        row.updated_at = moment
        detail = (
            f"Scan {replacement_scan_id} ({replacement.filename}) confirmed as the "
            f"rescan of scan {original_scan_id} ({row.source_name}). The original "
            "stays ineligible and is kept for provenance."
        )
        _append_event(
            session,
            scan_id=original_scan_id,
            batch_id=batch_id,
            action=LifecycleAction.REPLACED,
            reviewer=name,
            previous_value=LifecycleState.REJECTED_PENDING_RESCAN.value,
            new_value=LifecycleState.SUPERSEDED_BY_REPLACEMENT.value,
            reason_text=note.strip(),
            detail=detail,
        )
        _append_event(
            session,
            scan_id=replacement_scan_id,
            batch_id=batch_id,
            action=LifecycleAction.LINKED_AS_REPLACEMENT,
            reviewer=name,
            previous_value=LifecycleState.ACTIVE.value,
            new_value=LifecycleState.ACTIVE.value,
            reason_text=note.strip(),
            detail=detail,
        )
        case = _to_case(row, replacement_name=replacement.filename or "")
    _LOGGER.info(
        "Scan %d confirmed as the replacement of scan %d by %s",
        replacement_scan_id,
        original_scan_id,
        name,
    )
    _after_change(database, batch_id)
    return case


def remove_replacement(
    database: ProjectDatabase, original_scan_id: int, *, reviewer: str, note: str = ""
) -> None:
    """Withdraw a mistaken replacement link.

    The original returns to *awaiting rescan* - never straight to active, so
    this cannot by itself create two valid scripts - and the former
    replacement remains an ordinary active scan. Both histories record it.

    Raises:
        LifecycleError: No link to remove, or the original's image has already
            been removed (the case could never be completed differently).
    """
    name = review_store.validate_reviewer(reviewer)
    moment = _now()
    with database.session() as session:
        row = _row_for(session, original_scan_id)
        if row is None or _state(row) is not LifecycleState.SUPERSEDED_BY_REPLACEMENT:
            raise LifecycleError(
                f"Scan {original_scan_id} has no replacement",
                user_message="That scan has no confirmed replacement to remove.",
            )
        if row.file_state != FileState.PRESENT.value:
            raise LifecycleError(
                f"Scan {original_scan_id} image is {row.file_state}",
                user_message=(
                    "The rejected scan's image has already been removed, so its "
                    "replacement link is kept."
                ),
            )
        batch_id = row.batch_id
        former = row.replacement_scan_id
        row.state = LifecycleState.REJECTED_PENDING_RESCAN.value
        row.replacement_scan_id = None
        row.replaced_by = ""
        row.replaced_at = None
        row.updated_at = moment
        detail = (
            f"Replacement link to scan {former} withdrawn; scan {original_scan_id} "
            "awaits a rescan again."
        )
        for target in (original_scan_id, former):
            if target is None:
                continue
            _append_event(
                session,
                scan_id=target,
                batch_id=batch_id,
                action=LifecycleAction.REPLACEMENT_REMOVED,
                reviewer=name,
                previous_value=LifecycleState.SUPERSEDED_BY_REPLACEMENT.value,
                new_value=LifecycleState.REJECTED_PENDING_RESCAN.value,
                reason_text=note.strip(),
                detail=detail,
            )
    _LOGGER.info("Scan %d: replacement link removed by %s", original_scan_id, name)
    _after_change(database, batch_id)


# ----------------------------------------------------------------------
# Re-imports of rejected content
# ----------------------------------------------------------------------
def sync_reimports(database: ProjectDatabase, batch_id: str) -> int:
    """Link active scans whose bytes repeat a rejected scan's. Idempotent.

    Args:
        database: The open project database.
        batch_id: The batch whose scans to check.

    Returns:
        How many scans were newly linked.

    Run in the coordinator after a batch, like the other batch-level passes.
    A file with a rejected (or superseded) scan's exact content hash - found
    anywhere in the project - is the same physical image imported again, not
    a rescan. It is recorded as
    :attr:`~LifecycleState.REIMPORT_OF_REJECTED`, pointing at the scan whose
    history it belongs to, so it neither counts as a new submission nor is
    offered as a replacement. Scans with no hash yet are left alone.
    """
    moment = _now()
    linked = 0
    with database.session() as session:
        sources: dict[str, int] = {}
        for row in session.scalars(
            select(ScanRejection).where(
                ScanRejection.state.in_(
                    [
                        LifecycleState.REJECTED_PENDING_RESCAN.value,
                        LifecycleState.SUPERSEDED_BY_REPLACEMENT.value,
                    ]
                )
            )
        ).all():
            digest = row.content_sha256 or _scan_hash(session, row.scan_id)
            if digest:
                sources.setdefault(digest, row.scan_id)
        if not sources:
            return 0
        taken = _replacement_ids(session)
        for scan in session.scalars(
            select(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.content_sha256.in_(list(sources)))
        ).all():
            original = sources.get(scan.content_sha256)
            if original is None or original == scan.scan_id or scan.scan_id in taken:
                continue
            existing = _row_for(session, scan.scan_id)
            if _state(existing) is not LifecycleState.ACTIVE:
                continue
            if existing is None:
                existing = ScanRejection(
                    scan_id=scan.scan_id, batch_id=batch_id, rejected_at=moment
                )
                session.add(existing)
            row = existing
            row.state = LifecycleState.REIMPORT_OF_REJECTED.value
            row.reason_code = ""
            row.note = ""
            row.source_name = scan.filename or ""
            row.source_path = scan.source_path or ""
            row.content_sha256 = scan.content_sha256
            row.reimport_of_scan_id = original
            row.rejected_by = ""
            row.rejected_at = moment
            row.file_state = FileState.PRESENT.value
            row.updated_at = moment
            session.flush()
            _append_event(
                session,
                scan_id=scan.scan_id,
                batch_id=batch_id,
                action=LifecycleAction.REIMPORT_LINKED,
                previous_value=LifecycleState.ACTIVE.value,
                new_value=LifecycleState.REIMPORT_OF_REJECTED.value,
                detail=(
                    f"Content hash identical to rejected scan {original}; this is the "
                    "same image imported again, not a rescan. Not counted."
                ),
            )
            linked += 1
    if linked:
        _LOGGER.info("Batch %s: %d re-import(s) of rejected content linked", batch_id, linked)
        _after_change(database, batch_id)
    return linked


def _scan_hash(session: Session, scan_id: int) -> str:
    scan = session.get(BatchScan, scan_id)
    return scan.content_sha256 if scan is not None else ""


# ----------------------------------------------------------------------
# Consequences of a transition
# ----------------------------------------------------------------------
def _after_change(database: ProjectDatabase, batch_id: str) -> None:
    """Re-derive everything a lifecycle change affects.

    Duplicate-ID conflicts are re-synced, because an ineligible scan no longer
    takes part in them. Every reconciliation already run for this batch
    against an active roster is re-run, so no screen or count goes on showing
    a rejected script as valid. Scores are not recomputed here - that is the
    Results stage's explicit action - but they are reported stale
    (:func:`~omr_scanner.services.scoring_store.stale_reasons_for`).
    """
    from omr_scanner.services import reconciliation_store

    try:
        review_store.sync_duplicate_identifiers(database, batch_id)
        with database.session() as session:
            rosters = [
                int(item)
                for item in session.scalars(
                    select(ReconciliationRun.roster_id)
                    .join(
                        CandidateRoster,
                        CandidateRoster.roster_id == ReconciliationRun.roster_id,
                    )
                    .where(ReconciliationRun.batch_id == batch_id)
                    .where(CandidateRoster.is_active.is_(True))
                ).all()
            ]
        for roster_id in rosters:
            reconciliation_store.reconcile_batch(database, roster_id, batch_id)
    except OMRScannerError:
        _LOGGER.exception("Could not refresh batch %s after a lifecycle change", batch_id)


# ----------------------------------------------------------------------
# The incomplete-export acknowledgement
# ----------------------------------------------------------------------
def record_incomplete_export(
    database: ProjectDatabase,
    *,
    batch_id: str,
    set_code: str,
    reviewer: str,
    outstanding: int,
    detail: str = "",
) -> None:
    """Record that a final export was made while rescans were outstanding.

    Raises:
        omr_scanner.services.review_store.ReviewError: No reviewer - an
            acknowledgement nobody made is not one.
    """
    name = review_store.validate_reviewer(reviewer)
    with database.session() as session:
        _append_event(
            session,
            scan_id=0,
            batch_id=batch_id,
            action=LifecycleAction.EXPORT_INCOMPLETE,
            reviewer=name,
            new_value=str(outstanding),
            reason_text="Export incomplete results",
            detail=detail
            or (
                f"Final export of set {set_code} produced with {outstanding} rejected "
                "sheet(s) still awaiting rescan; the results are incomplete."
            ),
            entity_type=EXPORT_ENTITY,
            entity_id=set_code,
        )
    _LOGGER.info(
        "Incomplete export acknowledged by %s: set %s, %d outstanding rescan(s)",
        name,
        set_code,
        outstanding,
    )


def incomplete_exports(database: ProjectDatabase) -> tuple[LifecycleEvent, ...]:
    """Every acknowledged incomplete export, oldest first."""
    with database.session() as session:
        rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_type == EXPORT_ENTITY)
            .order_by(AuditEvent.event_id)
        ).all()
        return tuple(
            LifecycleEvent(
                event_id=row.event_id,
                occurred_at=row.occurred_at,
                scan_id=row.scan_id,
                action=LifecycleAction.EXPORT_INCOMPLETE,
                reviewer=row.reviewer,
                previous_value=row.previous_value,
                new_value=row.new_value,
                reason_code=row.reason_code,
                reason_text=row.reason_text,
                detail=row.detail,
            )
            for row in rows
        )


# ----------------------------------------------------------------------
# Purge Rejects
# ----------------------------------------------------------------------
def quarantine_root(project_root: Path) -> Path:
    """Where *Purge Rejects* moves images."""
    return ProjectLayout(project_root).root / QUARANTINE_DIR_NAME


def managed_roots(project_root: Path) -> tuple[Path, ...]:
    """The only folders *Purge Rejects* may ever remove a file from.

    Project-owned image storage: the original-scans folder, the aligned-scans
    folder and the quarantine folder. The project root itself is not one - it
    holds the database - and nothing outside the project ever is.
    """
    layout = ProjectLayout(project_root)
    return (
        layout.scans_original_dir,
        layout.scans_aligned_dir,
        quarantine_root(project_root),
    )


def _within(child: str, parent: str) -> bool:
    """Whether ``child`` lies strictly inside ``parent``.

    Case-insensitive on Windows; paths on different drives are never nested.
    """
    child_n = os.path.normcase(os.path.normpath(child))
    parent_n = os.path.normcase(os.path.normpath(parent))
    try:
        return child_n != parent_n and os.path.commonpath([child_n, parent_n]) == parent_n
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


def owned_path(project_root: Path, candidate: str) -> Path | None:
    """Validate that a stored path is a file this project owns, or refuse.

    Args:
        project_root: The open project's root.
        candidate: A path as the database recorded it.

    Returns:
        The file's resolved path when - and only when - it is a regular file
        inside one of :func:`managed_roots`. ``None`` otherwise.

    **A database path is never trusted.** It is refused when it is empty,
    virtual, relative, contains a ``..`` component, passes through a symbolic
    link or junction below the managed folder, resolves outside that folder
    after links are followed, is the folder itself, or is not a regular file.
    A scan read in place from a scanner's folder, a network share or another
    computer is therefore never removed: OMRFlow did not create it.
    """
    if not candidate or is_virtual_source(candidate):
        return None
    raw = Path(candidate)
    if not raw.is_absolute() or any(part == ".." for part in raw.parts):
        return None
    lexical = str(Path(candidate).absolute())
    for managed in managed_roots(project_root):
        managed_text = str(managed.absolute())
        if not _within(lexical, managed_text):
            continue
        # Every component from the file up to and including the managed
        # folder must be a plain directory entry: a link anywhere on that
        # chain could point the "project" path at somebody else's file.
        probe = Path(lexical)
        while True:
            if _is_link(probe):
                return None
            if os.path.normcase(str(probe)) == os.path.normcase(managed_text):
                break
            parent = probe.parent
            if parent == probe:  # pragma: no cover - reached a root
                return None
            probe = parent
        try:
            resolved = Path(lexical).resolve(strict=True)
            managed_resolved = Path(managed_text).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not _within(str(resolved), str(managed_resolved)):
            return None
        if not resolved.is_file() or _is_link(resolved):
            return None
        return resolved
    return None


def _referenced_elsewhere(session: Session, scan_id: int) -> set[str]:
    """Normalised paths every *other* scan in the project still refers to."""
    found: set[str] = set()
    for other_id, source, output in session.execute(
        select(BatchScan.scan_id, BatchScan.source_path, BatchScan.output_path)
    ).all():
        if int(other_id) == scan_id:
            continue
        for text in (source, output):
            if text:
                found.add(os.path.normcase(str(Path(str(text)).absolute())))
    return found


def _planned_files(
    session: Session, project_root: Path, row: ScanRejection
) -> tuple[list[PurgeFile], list[str]]:
    """Which of one case's files may be removed, and why the rest may not."""
    files: list[PurgeFile] = []
    skipped: list[str] = []
    elsewhere = _referenced_elsewhere(session, row.scan_id)
    scan = session.get(BatchScan, row.scan_id)
    wanted: list[tuple[str, str]] = []
    if row.file_state == FileState.QUARANTINED.value:
        wanted.extend(
            (str(item.get("to", "")), "quarantined")
            for item in _load_detail(row.file_detail).get("files", [])
            if item.get("to")
        )
    else:
        wanted.append((row.source_path or (scan.source_path if scan else ""), "source"))
        if scan is not None and scan.copied and scan.output_path:
            wanted.append((scan.output_path, "copy"))
    for text, role in wanted:
        label = Path(text).name or "(no file)"
        if not text:
            continue
        if not Path(text).exists():
            skipped.append(f"{label}: no longer on disk")
            continue
        path = owned_path(project_root, text)
        if path is None:
            skipped.append(
                f"{label}: outside the project's own scan storage - OMRFlow did not "
                "create it and will not remove it"
            )
            continue
        if os.path.normcase(str(path)) in elsewhere or os.path.normcase(
            str(Path(text).absolute())
        ) in elsewhere:
            skipped.append(f"{label}: still used by another scan")
            continue
        expected = row.content_sha256 or (scan.content_sha256 if scan else "")
        if role == "source" and expected:
            try:
                if hash_file(path) != expected:
                    skipped.append(f"{label}: changed since it was imported")
                    continue
            except OSError:
                skipped.append(f"{label}: could not be read")
                continue
        files.append(PurgeFile(path=str(path), size_bytes=path.stat().st_size, role=role))
    return files, skipped


def _load_detail(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def plan_purge(database: ProjectDatabase, project_root: Path) -> PurgePlan:
    """Say what *Purge Rejects* would do, without doing any of it.

    Only a **superseded** original whose replacement is still active is
    eligible - a rejected sheet still awaiting its rescan never is, whatever
    it looks like - and of its files only those :func:`owned_path` accepts.
    Across every batch in the project.
    """
    eligible: list[PurgeItem] = []
    blocked: list[tuple[RescanCase, str]] = []
    with database.session() as session:
        pending = len(
            session.scalars(
                select(ScanRejection.scan_id).where(
                    ScanRejection.state == LifecycleState.REJECTED_PENDING_RESCAN.value
                )
            ).all()
        )
        rows = session.scalars(
            select(ScanRejection)
            .where(ScanRejection.state == LifecycleState.SUPERSEDED_BY_REPLACEMENT.value)
            .where(ScanRejection.file_state != FileState.PURGED.value)
            .order_by(ScanRejection.scan_id)
        ).all()
        names = _names(session, [row.replacement_scan_id for row in rows])
        for row in rows:
            case = _to_case(row, replacement_name=names.get(row.replacement_scan_id or -1, ""))
            replacement = (
                session.get(BatchScan, row.replacement_scan_id)
                if row.replacement_scan_id is not None
                else None
            )
            if replacement is None:
                blocked.append((case, "its replacement is no longer in the project"))
                continue
            if _state(_row_for(session, replacement.scan_id)) is not LifecycleState.ACTIVE:
                blocked.append((case, "its replacement is itself rejected"))
                continue
            files, skipped = _planned_files(session, project_root, row)
            eligible.append(PurgeItem(case=case, files=tuple(files), skipped=tuple(skipped)))
    return PurgePlan(eligible=tuple(eligible), pending=pending, blocked=tuple(blocked))


def _quarantine_destination(project_root: Path, scan_id: int, name: str) -> Path:
    folder = quarantine_root(project_root) / QUARANTINE_SUBDIR / str(scan_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    counter = 1
    while target.exists():
        target = folder / f"{Path(name).stem}_{counter}{Path(name).suffix}"
        counter += 1
    return target


def execute_purge(
    database: ProjectDatabase,
    project_root: Path,
    *,
    mode: PurgeMode,
    reviewer: str,
    scan_ids: Iterable[int] | None = None,
) -> PurgeOutcome:
    """Quarantine or delete superseded originals' images.

    Args:
        database: The open project database.
        project_root: The open project's root.
        mode: Move into quarantine (the default recommendation) or delete.
        reviewer: Who ran it. Required.
        scan_ids: Restrict to these originals; every eligible one otherwise.

    Returns:
        What was actually done, including every file left in place and why.

    **Re-plans rather than trusting a plan shown earlier**, so a case whose
    replacement was rejected in the meantime, or a file that moved, is caught
    at the moment of acting. Each case is one transaction after its files are
    handled; a quarantine move is put back if that transaction fails. The
    database row, the rejection record and the audit history are never
    removed - only :attr:`~ScanRejection.file_state` changes.
    """
    name = review_store.validate_reviewer(reviewer)
    plan = plan_purge(database, project_root)
    wanted = set(scan_ids) if scan_ids is not None else None
    processed = moved = freed = 0
    skipped: list[str] = []
    for item in plan.eligible:
        if wanted is not None and item.case.scan_id not in wanted:
            continue
        skipped.extend(f"{item.case.source_name}: {text}" for text in item.skipped)
        targets = [
            file
            for file in item.files
            if mode is PurgeMode.DELETE or file.role != "quarantined"
        ]
        if not targets:
            continue
        done: list[dict[str, Any]] = []
        undo: list[tuple[Path, Path]] = []
        for file in targets:
            source = owned_path(project_root, file.path)  # validated again, now
            if source is None:
                skipped.append(f"{Path(file.path).name}: failed validation at purge time")
                continue
            try:
                if mode is PurgeMode.QUARANTINE:
                    destination = _quarantine_destination(
                        project_root, item.case.scan_id, source.name
                    )
                    shutil.move(str(source), str(destination))
                    undo.append((destination, source))
                    done.append(
                        {"from": str(source), "to": str(destination), "role": file.role,
                         "bytes": file.size_bytes, "action": "quarantined"}
                    )
                else:
                    source.unlink()
                    done.append(
                        {"from": str(source), "role": file.role,
                         "bytes": file.size_bytes, "action": "deleted"}
                    )
            except OSError as exc:
                skipped.append(f"{source.name}: could not be removed ({exc})")
        if not done:
            continue
        try:
            _record_purge(database, item.case.scan_id, mode=mode, reviewer=name, done=done,
                          skipped=item.skipped)
        except Exception:
            for destination, source in reversed(undo):
                try:
                    shutil.move(str(destination), str(source))
                except OSError:  # pragma: no cover - reported below
                    _LOGGER.exception("Could not return %s from quarantine", destination)
            raise
        processed += 1
        moved += len(done)
        freed += sum(int(entry["bytes"]) for entry in done)
    _LOGGER.info(
        "Purge Rejects (%s) by %s: %d original(s), %d file(s), %d byte(s)",
        mode.value,
        name,
        processed,
        moved,
        freed,
    )
    return PurgeOutcome(
        mode=mode, processed=processed, files_moved=moved, bytes_freed=freed,
        skipped=tuple(skipped),
    )


def _record_purge(
    database: ProjectDatabase,
    scan_id: int,
    *,
    mode: PurgeMode,
    reviewer: str,
    done: Sequence[dict[str, Any]],
    skipped: Sequence[str],
) -> None:
    moment = _now()
    with database.session() as session:
        row = _row_for(session, scan_id)
        if row is None:  # pragma: no cover - planned a moment ago
            raise LifecycleError(f"Scan {scan_id} lost its rejection record")
        previous_file = row.file_state
        history = _load_detail(row.file_detail)
        history.setdefault("actions", []).append(
            {"at": moment.isoformat(timespec="seconds"), "mode": mode.value,
             "by": reviewer, "files": list(done), "skipped": list(skipped)}
        )
        if mode is PurgeMode.QUARANTINE:
            history["files"] = [entry for entry in done if entry.get("to")]
        new_state = FileState.QUARANTINED if mode is PurgeMode.QUARANTINE else FileState.PURGED
        row.file_state = new_state.value
        row.file_detail = json.dumps(history, ensure_ascii=False)
        row.file_action_at = moment
        row.file_action_by = reviewer
        row.updated_at = moment
        listed = ", ".join(
            f"{Path(str(entry['from'])).name} -> {entry.get('to', 'deleted')}" for entry in done
        )
        _append_event(
            session,
            scan_id=scan_id,
            batch_id=row.batch_id,
            action=(
                LifecycleAction.IMAGE_QUARANTINED
                if mode is PurgeMode.QUARANTINE
                else LifecycleAction.IMAGE_PURGED
            ),
            reviewer=reviewer,
            previous_value=previous_file,
            new_value=new_state.value,
            detail=(
                f"Superseded original's image {new_state.label.lower()}: {listed}. "
                "The scan record, its rejection and its replacement link are kept."
            ),
        )


def current_identity(database: ProjectDatabase, scan_id: int) -> tuple[str, str]:
    """One scan's current effective ``(Student ID, set code)``, for display.

    Read from the review ledger - after every Resolve-stage correction - and
    shown to an operator deciding whether to reject the scan. Unread positions
    are shown as they are; nothing here claims an identity is reliable.
    """
    with database.session() as session:
        scan = _require_scan(session, scan_id)
        batch_id = scan.batch_id
    identifier = review_store.effective_identifiers(database, batch_id).get(scan_id)
    set_code = review_store.effective_set_codes(database, batch_id).get(scan_id)
    return (
        identifier.value if identifier is not None else "",
        set_code.value if set_code is not None else "",
    )


def with_live_identity(case: RescanCase, database: ProjectDatabase) -> RescanCase:
    """Return ``case`` with its current effective Student ID and set code."""
    identifiers = review_store.effective_identifiers(database, case.batch_id)
    set_codes = review_store.effective_set_codes(database, case.batch_id)
    live_id = identifiers.get(case.scan_id)
    live_set = set_codes.get(case.scan_id)
    return replace(
        case,
        recognised_candidate_id=(
            "" if live_id is None or live_id.unresolved else live_id.value
        ),
        recognised_set_code=(
            "" if live_set is None or live_set.unresolved else live_set.value
        ),
    )


__all__ = [
    "QUARANTINE_DIR_NAME",
    "LifecycleError",
    "LifecycleEvent",
    "association_choices",
    "cases_by_scan",
    "confirm_replacement",
    "count_cases",
    "current_identity",
    "execute_purge",
    "get_case",
    "incomplete_exports",
    "ineligible_scan_ids",
    "lifecycle_history",
    "lifecycle_states",
    "list_cases",
    "managed_roots",
    "outstanding_for_set",
    "owned_path",
    "plan_purge",
    "possible_rescans",
    "quarantine_root",
    "record_incomplete_export",
    "reject_scan",
    "remove_replacement",
    "removed_images",
    "replacement_candidates",
    "state_of",
    "sync_reimports",
    "undo_reject",
    "with_live_identity",
]
