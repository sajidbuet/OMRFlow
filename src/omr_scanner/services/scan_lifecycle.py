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
    * :func:`exclude_scan`, :func:`defer_scan`, :func:`restore_scan` and
      :func:`keep_script` - the Attendance stage's dispositions (*Reject /
      Exclude*, *Defer*, *Restore*, *Keep this script*). The same table, the
      same ledger and the same eligibility rule: an excluded or deferred scan
      is simply another state that is not
      :attr:`~omr_scanner.domain.scan_lifecycle.LifecycleState.ACTIVE`.
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
    7. A confirmed replacement cannot be excluded or deferred while its link
       stands - that would silently leave its original's candidate with no
       script. Withdraw the link first.
    8. Excluded and deferred scans are **never** a source for
       :func:`sync_reimports`: the kept copy of an accidentally duplicated
       file has the excluded copy's exact bytes, and must stay active.

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

from sqlalchemy import func, select

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    CandidateRoster,
    ReconciliationRun,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
    ScanRejection,
)
from omr_scanner.domain.project import ProjectLayout
from omr_scanner.domain.review import RESOLUTION_TYPES, ConflictState
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
from omr_scanner.domain.session_population import SheetDisposition
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import review_store, set_identity
from omr_scanner.services.scan_provenance import hash_file, is_virtual_source

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable, Sequence

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.services.review_store import EffectiveIdentifier
    from omr_scanner.services.session_population import SessionPopulation

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
    replacement_batch: str = "",
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
        replacement_batch_id=replacement_batch,
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
        batches = _batches(session, [row.replacement_scan_id for row in rows])
        return {
            row.scan_id: _to_case(
                row,
                replacement_name=names.get(row.replacement_scan_id or -1, ""),
                replacement_batch=batches.get(row.replacement_scan_id or -1, ""),
                live_id=identifiers.get(row.scan_id),
                live_set=set_codes.get(row.scan_id),
            )
            for row in rows
        }


def session_cases(
    database: ProjectDatabase, population: SessionPopulation, *, live: bool = True
) -> dict[int, RescanCase]:
    """Every lifecycle record of a scan session's population, keyed by scan id.

    The session-wide counterpart of :func:`cases_by_scan` (0.1.1 phase 4):
    merged over the batches holding the population's sheets, and limited to
    sheets whose disposition *is* their lifecycle state - a sheet in a batch
    read again as a whole, or a rescan that counts in another session, is
    not this session's case.
    """
    wanted = population.with_disposition(
        SheetDisposition.SUPERSEDED_BY_REPLACEMENT,
        SheetDisposition.REJECTED_PENDING_RESCAN,
        SheetDisposition.REIMPORT_OF_REJECTED,
        SheetDisposition.EXACT_DUPLICATE,
        SheetDisposition.EXCLUDED,
        SheetDisposition.DEFERRED,
    )
    found: dict[int, RescanCase] = {}
    for batch_id in population.batches_holding(wanted):
        for scan_id, case in cases_by_scan(database, batch_id, live=live).items():
            if scan_id in wanted:
                found[scan_id] = case
    return found


def _session_cases_of(database: ProjectDatabase, batch_id: str) -> dict[int, RescanCase]:
    """:func:`session_cases` for the session ``batch_id`` belongs to (live values)."""
    from omr_scanner.services import session_population

    return session_cases(database, session_population.population(database, batch_id))


def _batches(session: Session, scan_ids: Iterable[int | None]) -> dict[int, str]:
    wanted = [item for item in scan_ids if item is not None]
    if not wanted:
        return {}
    return {
        int(scan_id): str(batch_id)
        for scan_id, batch_id in session.execute(
            select(BatchScan.scan_id, BatchScan.batch_id).where(
                BatchScan.scan_id.in_(wanted)
            )
        ).all()
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
    session_wide: bool = False,
) -> tuple[RescanCase, ...]:
    """The batch's rescan cases, for the *Rejected / Rescan* queue.

    Args:
        database: The open project database.
        batch_id: The batch.
        include_completed: Include cases whose rescan has been confirmed.
        include_reimports: Include exact re-imports of rejected content. They
            are not rescan cases - nothing is awaited - so they are listed
            only when asked for.
        session_wide: Every case of the scan session ``batch_id`` belongs to
            (the Resolve stage, 0.1.1 phase 4).

    Returns:
        Outstanding cases first, then completed ones, each oldest first.
    """
    cases = list(
        (
            _session_cases_of(database, batch_id)
            if session_wide
            else cases_by_scan(database, batch_id)
        ).values()
    )
    kept = [
        item
        for item in cases
        if item.state is LifecycleState.REJECTED_PENDING_RESCAN
        or (include_completed and item.state is LifecycleState.SUPERSEDED_BY_REPLACEMENT)
        or (
            include_reimports
            and item.state
            in (LifecycleState.REIMPORT_OF_REJECTED, LifecycleState.DUPLICATE_CONTENT)
        )
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


def count_cases(
    database: ProjectDatabase, batch_id: str, *, session_wide: bool = False
) -> RescanCounts:
    """How many rejected scans the batch (or its whole session) has, by state.

    ``session_wide`` counts the scan session's population (0.1.1 phase 4):
    a case in a batch read again as a whole, or a rescan counting in another
    session, is not this session's case.
    """
    if session_wide:
        from omr_scanner.services import session_population

        counts = session_population.population(database, batch_id).counts()
        return RescanCounts(
            excluded=counts.get(SheetDisposition.EXCLUDED, 0),
            deferred=counts.get(SheetDisposition.DEFERRED, 0),
            outstanding=counts.get(SheetDisposition.REJECTED_PENDING_RESCAN, 0),
            superseded=counts.get(SheetDisposition.SUPERSEDED_BY_REPLACEMENT, 0),
            reimports=counts.get(SheetDisposition.REIMPORT_OF_REJECTED, 0),
            duplicates=counts.get(SheetDisposition.EXACT_DUPLICATE, 0),
        )
    states = lifecycle_states(database, batch_id)
    return RescanCounts(
        excluded=sum(1 for item in states.values() if item is LifecycleState.EXCLUDED),
        deferred=sum(1 for item in states.values() if item is LifecycleState.DEFERRED),
        outstanding=sum(
            1 for item in states.values() if item is LifecycleState.REJECTED_PENDING_RESCAN
        ),
        superseded=sum(
            1 for item in states.values() if item is LifecycleState.SUPERSEDED_BY_REPLACEMENT
        ),
        reimports=sum(
            1 for item in states.values() if item is LifecycleState.REIMPORT_OF_REJECTED
        ),
        duplicates=sum(
            1 for item in states.values() if item is LifecycleState.DUPLICATE_CONTENT
        ),
    )


def outstanding_for_set(
    database: ProjectDatabase, batch_id: str, set_code: str
) -> tuple[RescanCase, ...]:
    """The outstanding rescans that may belong to one set.

    A case whose set code (declared, or the scan's current effective code) is
    this set's belongs to it. So does one whose set code is **unknown**, or
    names no defined set: such a sheet could be anybody's, and a report that
    ignored it would look complete when it might not be. Set codes are
    compared through :mod:`omr_scanner.services.set_identity`.

    **Session-wide** (0.1.1 phase 4): ``batch_id`` names the scan session;
    every outstanding case of the session's population is considered.
    """
    identity = set_identity.load(database)
    return tuple(
        case
        for case in _session_cases_of(database, batch_id).values()
        if case.is_outstanding and _may_belong(identity, case.set_code, set_code)
    )


def _may_belong(identity: set_identity.SetIdentity, code: str, set_code: str) -> bool:
    """Whether a case with set ``code`` may be one of ``set_code``'s sheets.

    Its own set, an unknown set, or a set that names no defined set (or two
    colliding ones) - the last two could be anybody's.
    """
    if not code:
        return True
    if set_identity.same_set(code, set_code):
        return True
    return identity.has_sets and not identity.is_defined(code)


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
        # The declared set is logical - what the operator says the paper is -
        # and is stored in the defined set's own spelling ("a" -> "A").
        identity = set_identity.load(database)
        if identity.has_sets:
            found = identity.logical(declared_set)
            if found is None:
                raise LifecycleError(
                    f"Declared set code {declared_set!r} is not a defined set",
                    user_message=(
                        f"'{declared_set}' is not one of this project's sets "
                        f"({', '.join(sorted(identity.codes))}). Leave it empty if "
                        "the set is not known."
                    ),
                )
            declared_set = found.code

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
    _after_change(database, batch_id, scans=(scan_id,))
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
    if state_of(database, scan_id).is_disposition:
        # Excluded or deferred from the Attendance stage: the same reversal,
        # recorded under its own name.
        restore_scan(database, scan_id, reviewer=name, note=note)
        return
    moment = _now()
    with database.session() as session:
        row = _row_for(session, scan_id)
        state = _state(row)
        if row is None or state is LifecycleState.ACTIVE:
            raise LifecycleError(
                f"Scan {scan_id} is not rejected",
                user_message="That scan is not rejected, so there is nothing to undo.",
            )
        # Checked first: for an original whose image is gone, "remove the
        # replacement link first" would send the operator to an action that is
        # refused too.
        if row.file_state != FileState.PRESENT.value:
            raise LifecycleError(
                f"Scan {scan_id} image is {row.file_state}",
                user_message=FileState(row.file_state).unavailable_note,
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
    _after_change(database, batch_id, scans=(scan_id,))


# ----------------------------------------------------------------------
# Attendance dispositions: Reject / Exclude, Defer, Restore, Keep this script
# ----------------------------------------------------------------------
def _refuse_replacement(session: Session, scan: BatchScan, what: str) -> None:
    """Refuse to take a confirmed replacement out of play while it is linked."""
    original = session.scalars(
        select(ScanRejection).where(ScanRejection.replacement_scan_id == scan.scan_id).limit(1)
    ).first()
    if original is None:
        return
    raise LifecycleError(
        f"Scan {scan.scan_id} is the confirmed replacement of scan {original.scan_id}",
        user_message=(
            f"{scan.filename or 'This scan'} is the confirmed rescan of "
            f"{original.source_name or f'scan {original.scan_id}'}. It cannot be "
            f"{what} while that link stands, because the original's candidate "
            "would silently lose their script. Keep this copy instead, or remove "
            "the replacement link under Resolve > Rejected / Rescan first."
        ),
    )


def _write_disposition(
    session: Session,
    scan: BatchScan,
    *,
    target: LifecycleState,
    reviewer: str,
    reason: RejectionReason | None,
    note: str,
    detail: str,
    moment: datetime,
    readings: _ProjectReadings,
) -> LifecycleState:
    """Move one scan to ``target`` (excluded or deferred), with its ledger entry.

    Runs inside the caller's transaction, so :func:`keep_script` can dispose
    of several copies atomically. ``readings`` holds the effective identities,
    read *before* that transaction opened. Returns the state it left.
    """
    row = _row_for(session, scan.scan_id)
    previous = _state(row)
    allowed = (
        (LifecycleState.ACTIVE, LifecycleState.DEFERRED)
        if target is LifecycleState.EXCLUDED
        else (LifecycleState.ACTIVE,)
    )
    if previous not in allowed:
        raise LifecycleError(
            f"Scan {scan.scan_id} is {previous.value}; cannot become {target.value}",
            user_message=(
                f"{scan.filename or 'This scan'} is already "
                f"{previous.label.lower()}."
                + (
                    " Restore it first."
                    if previous.is_disposition
                    else " Its Reject & Rescan case is handled on the Resolve stage."
                )
            ),
        )
    _refuse_replacement(
        session, scan, "rejected" if target is LifecycleState.EXCLUDED else "deferred"
    )
    live_id = readings.identifiers.get(scan.scan_id)
    live_set = readings.set_codes.get(scan.scan_id)
    if row is None:
        row = ScanRejection(scan_id=scan.scan_id, batch_id=scan.batch_id, rejected_at=moment)
        session.add(row)
    row.state = target.value
    row.reason_code = reason.value if reason is not None else ""
    row.note = note
    row.declared_candidate_id = ""
    row.declared_set_code = ""
    row.recognised_candidate_id = live_id.value if live_id is not None else ""
    row.recognised_set_code = live_set.value if live_set is not None else ""
    row.source_name = scan.filename or ""
    row.source_path = scan.source_path or ""
    row.content_sha256 = scan.content_sha256 or ""
    row.rejected_by = reviewer
    row.rejected_at = moment
    row.replacement_scan_id = None
    row.replaced_by = ""
    row.replaced_at = None
    row.reimport_of_scan_id = None
    row.file_state = FileState.PRESENT.value
    row.updated_at = moment
    session.flush()
    if target is LifecycleState.DEFERRED:
        action = LifecycleAction.DEFERRED
    elif previous is LifecycleState.DEFERRED:
        action = LifecycleAction.DISPOSITION_CHANGED
    else:
        action = LifecycleAction.EXCLUDED
    _append_event(
        session,
        scan_id=scan.scan_id,
        batch_id=scan.batch_id,
        action=action,
        reviewer=reviewer,
        previous_value=previous.value,
        new_value=target.value,
        machine_value=row.recognised_candidate_id,
        reason_code=row.reason_code,
        reason_text=note,
        detail=_identity_detail(
            detail,
            declared_id="",
            declared_set="",
            recognised_id=row.recognised_candidate_id,
            recognised_set=row.recognised_set_code,
        )
        + f" Batch {scan.batch_id[:8]}, file '{scan.filename or scan.scan_id}'.",
    )
    return previous


def _readings_for(database: ProjectDatabase, scan_ids: Sequence[int]) -> _ProjectReadings:
    """Effective identities for the batches these scans belong to.

    Read before any write transaction opens, as :func:`reject_scan` does.
    """
    with database.session() as session:
        batch_ids = [
            str(item)
            for item in session.scalars(
                select(BatchScan.batch_id).where(BatchScan.scan_id.in_(list(scan_ids)))
            ).all()
        ]
    return _project_readings(database, batch_ids)


def _validated_note(reason: RejectionReason | None, note: str) -> str:
    text = note.strip()
    if reason is not None and reason.requires_note and not text:
        raise LifecycleError(
            "Exclusion reason 'other' needs a note",
            user_message="Describe why the sheet is excluded when choosing 'Other'.",
        )
    return text


_EXCLUDED_SENTENCE = (
    "Rejected / excluded: takes no part in reconciliation, duplicate detection, "
    "scoring, results or exports. No rescan is expected. Nothing was deleted."
)


def exclude_scan(
    database: ProjectDatabase,
    scan_id: int,
    *,
    reviewer: str,
    reason: RejectionReason,
    note: str = "",
) -> RescanCase:
    """*Reject / Exclude*: take a sheet out of this examination's results.

    Args:
        database: The open project database.
        scan_id: An active or deferred scan.
        reviewer: Who decided. Required.
        reason: Why - normally one of
            :data:`~omr_scanner.domain.scan_lifecycle.EXCLUSION_REASONS`.
            :attr:`RejectionReason.OTHER` needs a note.
        note: The operator's own words.

    Returns:
        The scan's record, now :attr:`LifecycleState.EXCLUDED`.

    Raises:
        LifecycleError: The scan is already excluded, is a Reject & Rescan
            case, or is a confirmed replacement; or *Other* has no note.
        omr_scanner.services.review_store.ReviewError: No reviewer.

    A logical exclusion: the scan row, its recognition result, its conflicts
    and decisions and its image are all kept, which is what makes
    :func:`restore_scan` exact. Reconciliation and duplicate detection are
    re-derived at once (:func:`_after_change`), in the same way as for a
    rejection.
    """
    name = review_store.validate_reviewer(reviewer)
    text = _validated_note(reason, note)
    readings = _readings_for(database, [scan_id])
    with database.session() as session:
        scan = _require_scan(session, scan_id)
        batch_id = scan.batch_id
        _write_disposition(
            session, scan,
            target=LifecycleState.EXCLUDED, reviewer=name, reason=reason,
            note=text, detail=_EXCLUDED_SENTENCE, moment=_now(), readings=readings,
        )
    _LOGGER.info("Scan %d excluded by %s (%s)", scan_id, name, reason.value)
    _after_change(database, batch_id, scans=(scan_id,))
    case = get_case(database, scan_id)
    assert case is not None  # just written
    return case


def defer_scan(
    database: ProjectDatabase, scan_id: int, *, reviewer: str, note: str = ""
) -> RescanCase:
    """*Defer*: postpone the decision about a sheet, without losing it.

    The sheet leaves reconciliation counts, duplicate detection, scoring and
    results while deferred, and is listed and counted as *Deferred* until it
    is restored (:func:`restore_scan`) or excluded (:func:`exclude_scan`).

    Raises:
        LifecycleError: The scan is not active, or is a confirmed replacement.
        omr_scanner.services.review_store.ReviewError: No reviewer.
    """
    name = review_store.validate_reviewer(reviewer)
    readings = _readings_for(database, [scan_id])
    with database.session() as session:
        scan = _require_scan(session, scan_id)
        batch_id = scan.batch_id
        _write_disposition(
            session, scan,
            target=LifecycleState.DEFERRED, reviewer=name, reason=None,
            note=note.strip(),
            detail=(
                "Deferred: the decision is postponed. Left out of reconciliation "
                "counts, scoring and results until restored or excluded. Nothing "
                "was deleted."
            ),
            moment=_now(),
            readings=readings,
        )
    _LOGGER.info("Scan %d deferred by %s", scan_id, name)
    _after_change(database, batch_id, scans=(scan_id,))
    case = get_case(database, scan_id)
    assert case is not None  # just written
    return case


def restore_scan(
    database: ProjectDatabase, scan_id: int, *, reviewer: str, note: str = ""
) -> None:
    """*Restore*: return an excluded or deferred sheet to active review.

    Exactly as it was - recognition result, conflicts and decisions were never
    touched. If that makes a duplicate real again, duplicate detection and
    reconciliation raise it again, as for any other pair of active scans.

    Raises:
        LifecycleError: The scan is not excluded or deferred (a Reject &
            Rescan case is undone on the Resolve stage), or its image is no
            longer available.
        omr_scanner.services.review_store.ReviewError: No reviewer.
    """
    name = review_store.validate_reviewer(reviewer)
    moment = _now()
    with database.session() as session:
        row = _row_for(session, scan_id)
        state = _state(row)
        if row is None or not state.is_disposition:
            raise LifecycleError(
                f"Scan {scan_id} is {state.value}; nothing to restore",
                user_message=(
                    "That sheet is not rejected / excluded or deferred, so there "
                    "is nothing to restore."
                    if state is LifecycleState.ACTIVE
                    else "That sheet is a Reject & Rescan case; undo it on the "
                    "Resolve stage under Rejected / Rescan."
                ),
            )
        if row.file_state != FileState.PRESENT.value:  # pragma: no cover - never purged
            raise LifecycleError(
                f"Scan {scan_id} image is {row.file_state}",
                user_message=FileState(row.file_state).unavailable_note,
            )
        batch_id = row.batch_id
        row.state = LifecycleState.ACTIVE.value
        row.updated_at = moment
        _append_event(
            session,
            scan_id=scan_id,
            batch_id=batch_id,
            action=LifecycleAction.RESTORED,
            reviewer=name,
            previous_value=state.value,
            new_value=LifecycleState.ACTIVE.value,
            reason_code=row.reason_code,
            reason_text=note.strip(),
            detail=(
                f"Restored from {state.label.lower()} to active review, with every "
                "earlier recognition result, conflict and decision as it was."
            ),
        )
    _LOGGER.info("Scan %d restored from %s by %s", scan_id, state.value, name)
    _after_change(database, batch_id, scans=(scan_id,))


def keep_script(
    database: ProjectDatabase,
    keep_scan_id: int,
    duplicate_scan_ids: Sequence[int],
    *,
    reviewer: str,
    candidate_id: str = "",
    note: str = "",
) -> tuple[RescanCase, ...]:
    """*Keep this script*: settle a duplicate group in one transaction.

    Args:
        database: The open project database.
        keep_scan_id: The copy the operator inspected and chose. Must be active.
        duplicate_scan_ids: Every other copy in the group - one or more - each
            to be excluded with reason
            :attr:`~omr_scanner.domain.scan_lifecycle.RejectionReason.DUPLICATE`.
        reviewer: Who decided. Required.
        candidate_id: The Student ID the group is filed under, for the ledger.
        note: The operator's own words.

    Returns:
        The excluded copies' records.

    Raises:
        LifecycleError: Nothing to exclude, the kept copy is among them or is
            not active, or any copy cannot be excluded. **All or nothing**:
            one refusal leaves every copy as it was.

    Nothing is chosen here. The canonical copy is whichever the operator
    named; file names, timestamps and scan order are never consulted.
    """
    name = review_store.validate_reviewer(reviewer)
    others = list(dict.fromkeys(int(item) for item in duplicate_scan_ids))
    if not others:
        raise LifecycleError(
            "keep_script needs at least one duplicate",
            user_message="There is no other copy to reject.",
        )
    if keep_scan_id in others:
        raise LifecycleError(
            "The kept script is also listed as a duplicate",
            user_message="The script you keep cannot also be rejected.",
        )
    text = note.strip()
    moment = _now()
    batches: set[str] = set()
    readings = _readings_for(database, [keep_scan_id, *others])
    with database.session() as session:
        kept = _require_scan(session, keep_scan_id)
        if _state(_row_for(session, keep_scan_id)) is not LifecycleState.ACTIVE:
            raise LifecycleError(
                f"Scan {keep_scan_id} is not active",
                user_message=(
                    f"{kept.filename or 'The chosen script'} is not active, so it "
                    "cannot be kept. Restore it first."
                ),
            )
        batches.add(kept.batch_id)
        names: list[str] = []
        for scan_id in others:
            scan = _require_scan(session, scan_id)
            batches.add(scan.batch_id)
            names.append(scan.filename or f"scan {scan_id}")
            _write_disposition(
                session, scan,
                target=LifecycleState.EXCLUDED, reviewer=name,
                reason=RejectionReason.DUPLICATE, note=text,
                detail=(
                    f"Duplicate of {kept.filename or f'scan {keep_scan_id}'} "
                    f"(scan {keep_scan_id}), which was kept"
                    + (f" for candidate {candidate_id}" if candidate_id else "")
                    + ". " + _EXCLUDED_SENTENCE
                ),
                moment=moment,
                readings=readings,
            )
        _append_event(
            session,
            scan_id=keep_scan_id,
            batch_id=kept.batch_id,
            action=LifecycleAction.KEPT_CANONICAL,
            reviewer=name,
            previous_value=LifecycleState.ACTIVE.value,
            new_value=LifecycleState.ACTIVE.value,
            reason_code=RejectionReason.DUPLICATE.value,
            reason_text=text,
            detail=(
                "Kept as the script"
                + (f" for candidate {candidate_id}" if candidate_id else "")
                + f"; {len(others)} duplicate cop{'y' if len(others) == 1 else 'ies'} "
                f"rejected / excluded: {', '.join(names)} "
                f"(scan {', '.join(str(item) for item in others)})."
            ),
        )
    _LOGGER.info(
        "Scan %d kept by %s; %d duplicate(s) excluded", keep_scan_id, name, len(others)
    )
    _after_change(database, *sorted(batches), scans=(keep_scan_id, *others))
    return tuple(
        case for case in (get_case(database, item) for item in others) if case is not None
    )


def list_dispositions(
    database: ProjectDatabase,
    batch_id: str,
    *,
    states: Iterable[LifecycleState] = (LifecycleState.EXCLUDED, LifecycleState.DEFERRED),
) -> tuple[RescanCase, ...]:
    """The session's excluded and/or deferred sheets, deferred first, oldest first.

    Session-wide since 0.1.1 phase 4: ``batch_id`` names the scan session.
    """
    wanted = set(states)
    cases = [
        case for case in _session_cases_of(database, batch_id).values() if case.state in wanted
    ]
    cases.sort(
        key=lambda item: (
            item.state is not LifecycleState.DEFERRED,
            item.rejected_at or _now(),
            item.scan_id,
        )
    )
    return tuple(cases)


def deferred_for_set(
    database: ProjectDatabase, batch_id: str, set_code: str
) -> tuple[RescanCase, ...]:
    """The deferred sheets that may belong to one set.

    Placed like :func:`outstanding_for_set`: a sheet whose set code is this
    set's, unknown, or no defined set's may be this set's, and a result that
    ignored it would look complete when it might not be.
    """
    identity = set_identity.load(database)
    return tuple(
        case
        for case in _session_cases_of(database, batch_id).values()
        if case.state is LifecycleState.DEFERRED
        and _may_belong(identity, case.set_code, set_code)
    )


@dataclass(frozen=True, slots=True)
class SheetFacts:
    """Everything an operator needs to tell one scan from another, detached.

    What the Attendance stage shows beside each copy of a duplicate before
    anything is kept or rejected - so the wrong copy is hard to pick.

    Attributes:
        scan_id: The scan.
        filename: Its file name.
        batch_id / batch_label: The batch it was read in.
        scan_number: Its 1-based position in that batch.
        machine_id / effective_id: The Student ID as read, and as it reads now.
        machine_set / effective_set: Likewise for the set code.
        read_at: When recognition finished, when known.
        state: Its lifecycle state.
        reason_label: Why it is not active, when it is not.
        decided_by / decided_at: Who put it in that state, and when.
        replacement_of: The file name of the rejected original this scan is
            the confirmed rescan of, or ``""``.
        replaced_by: For a superseded original, its replacement's file name.
    """

    scan_id: int
    filename: str
    batch_id: str
    batch_label: str
    scan_number: int
    machine_id: str
    effective_id: str
    machine_set: str
    effective_set: str
    read_at: datetime | None
    state: LifecycleState
    reason_label: str = ""
    decided_by: str = ""
    decided_at: datetime | None = None
    replacement_of: str = ""
    replaced_by: str = ""

    @property
    def is_rescan_linked(self) -> bool:
        """Whether this scan takes part in a confirmed Reject & Rescan link."""
        return bool(self.replacement_of or self.replaced_by)


def sheet_facts(database: ProjectDatabase, scan_ids: Iterable[int]) -> dict[int, SheetFacts]:
    """Identifying facts for each of these scans, in whatever batches they are."""
    wanted = list(dict.fromkeys(int(item) for item in scan_ids))
    if not wanted:
        return {}
    with database.session() as session:
        scans = session.scalars(select(BatchScan).where(BatchScan.scan_id.in_(wanted))).all()
        batch_ids = {scan.batch_id for scan in scans}
        rows = {
            row.scan_id: row
            for row in session.scalars(
                select(ScanRejection).where(ScanRejection.scan_id.in_(wanted))
            ).all()
        }
        originals = {
            int(row.replacement_scan_id): row.source_name or f"scan {row.scan_id}"
            for row in session.scalars(
                select(ScanRejection).where(ScanRejection.replacement_scan_id.in_(wanted))
            ).all()
            if row.replacement_scan_id is not None
        }
        replacement_names = _names(session, [row.replacement_scan_id for row in rows.values()])
        detached = [
            (
                scan.scan_id,
                scan.filename or "",
                scan.batch_id,
                scan.batch_index,
                scan.identifier_value or "",
                scan.set_code_value or "",
                scan.finished_at,
            )
            for scan in scans
        ]
        cases = {
            scan_id: _to_case(
                row,
                replacement_name=replacement_names.get(row.replacement_scan_id or -1, ""),
            )
            for scan_id, row in rows.items()
        }
    readings = _project_readings(database, batch_ids)
    found: dict[int, SheetFacts] = {}
    for scan_id, name, batch_id, index, machine_id, machine_set, read_at in detached:
        read = readings.identifiers.get(scan_id)
        code = readings.set_codes.get(scan_id)
        case = cases.get(scan_id)
        state = case.state if case is not None else LifecycleState.ACTIVE
        inactive = case is not None and state is not LifecycleState.ACTIVE
        found[scan_id] = SheetFacts(
            scan_id=scan_id,
            filename=name,
            batch_id=batch_id,
            batch_label=readings.batch_labels.get(batch_id, f"batch {batch_id[:8]}"),
            scan_number=index + 1,
            machine_id=read.machine_value if read is not None else machine_id,
            effective_id=("" if read.unresolved else read.value) if read else machine_id,
            machine_set=code.machine_value if code is not None else machine_set,
            effective_set=("" if code.unresolved else code.value) if code else machine_set,
            read_at=read_at,
            state=state,
            reason_label=case.reason_label if inactive and case is not None else "",
            decided_by=case.rejected_by if inactive and case is not None else "",
            decided_at=case.rejected_at if inactive and case is not None else None,
            replacement_of=originals.get(scan_id, ""),
            replaced_by=(
                case.replacement_name
                if case is not None and state is LifecycleState.SUPERSEDED_BY_REPLACEMENT
                else ""
            ),
        )
    return found


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


@dataclass(frozen=True, slots=True)
class _ProjectReadings:
    """Every scan's effective Student ID and set code, across the project.

    Read batch by batch through the review ledger's own projections - one
    pair of queries per batch - because the ledger is organised per batch and
    a rescan may arrive in any of them.
    """

    identifiers: dict[int, EffectiveIdentifier]
    set_codes: dict[int, EffectiveIdentifier]
    batch_labels: dict[str, str]


def _batch_label(batch: ScanBatch) -> str:
    """How a batch is named to an operator: short id and when it was made."""
    return f"batch {batch.batch_id[:8]} ({batch.created_at:%Y-%m-%d})"


def _project_readings(
    database: ProjectDatabase, batch_ids: Iterable[str] | None = None
) -> _ProjectReadings:
    """Effective identities for every scan of the given batches (default: all)."""
    with database.session() as session:
        batches = session.scalars(select(ScanBatch)).all()
        labels = {batch.batch_id: _batch_label(batch) for batch in batches}
    wanted = list(batch_ids) if batch_ids is not None else list(labels)
    identifiers: dict[int, EffectiveIdentifier] = {}
    set_codes: dict[int, EffectiveIdentifier] = {}
    for batch_id in dict.fromkeys(wanted):
        identifiers.update(review_store.effective_identifiers(database, batch_id))
        set_codes.update(review_store.effective_set_codes(database, batch_id))
    return _ProjectReadings(identifiers=identifiers, set_codes=set_codes, batch_labels=labels)


def _project_ineligible(session: Session) -> frozenset[int]:
    """Every scan in the project that is not active."""
    return frozenset(
        int(item)
        for item in session.scalars(
            select(ScanRejection.scan_id).where(
                ScanRejection.state != LifecycleState.ACTIVE.value
            )
        ).all()
    )


def _candidate(
    case: RescanCase,
    scan: BatchScan,
    readings: _ProjectReadings,
    *,
    wanted_set: str = "",
) -> ReplacementCandidate:
    """Describe one scan as a possible replacement for ``case``."""
    read = readings.identifiers.get(scan.scan_id)
    code = readings.set_codes.get(scan.scan_id)
    code_value = "" if code is None or code.unresolved else code.value
    agrees = (
        None
        if not (wanted_set and code_value)
        else set_identity.same_set(code_value, wanted_set)
    )
    return ReplacementCandidate(
        scan_id=scan.scan_id,
        source_name=scan.filename or "",
        candidate_id=read.value if read is not None else "",
        set_code=code_value,
        set_code_agrees=agrees,
        batch_id=scan.batch_id,
        batch_label=readings.batch_labels.get(scan.batch_id, scan.batch_id[:8]),
        other_batch=scan.batch_id != case.batch_id,
        read_at=scan.finished_at,
    )


def _refusable(
    case: RescanCase,
    scan: BatchScan,
    *,
    ineligible: frozenset[int],
    taken: set[int],
) -> bool:
    """Whether a scan can never be this case's replacement.

    The same rules :func:`confirm_replacement` enforces.
    """
    return (
        scan.scan_id == case.scan_id
        or scan.scan_id in ineligible
        or scan.scan_id in taken
        or scan.status not in _PROCESSED
        or bool(case.content_sha256 and scan.content_sha256 == case.content_sha256)
    )


def possible_rescans(
    database: ProjectDatabase, batch_id: str
) -> dict[int, tuple[ReplacementCandidate, ...]]:
    """Every outstanding case of a batch, and its possible rescans.

    **Project-wide.** A sheet rejected while one batch was being processed is
    often rescanned later - another batch, another scanner, another computer,
    a file name nothing like the original's - so candidates are looked for in
    every batch of the project. Batch numbers, like file names, are never
    evidence: only the effective Student ID is. Each candidate says which
    batch it came from.
    """
    cases = {
        scan_id: case
        for scan_id, case in cases_by_scan(database, batch_id).items()
        if case.is_outstanding
    }
    identities = {case.identity for case in cases.values() if case.identity}
    if not identities:
        return dict.fromkeys(cases, ())
    readings = _project_readings(database)
    wanted = {
        scan_id
        for scan_id, read in readings.identifiers.items()
        if not read.unresolved and read.value in identities
    }
    with database.session() as session:
        taken = _replacement_ids(session)
        ineligible = _project_ineligible(session)
        scans = (
            session.scalars(
                select(BatchScan)
                .where(BatchScan.scan_id.in_(list(wanted)))
                .order_by(BatchScan.scan_id)
            ).all()
            if wanted
            else []
        )
        found: dict[int, tuple[ReplacementCandidate, ...]] = {}
        for scan_id, case in cases.items():
            rows = [
                _candidate(case, scan, readings, wanted_set=case.set_code)
                for scan in scans
                if not _refusable(case, scan, ineligible=ineligible, taken=taken)
                and readings.identifiers[scan.scan_id].value == case.identity
            ]
            # Agreeing set code first, then the newest scan - a rescan arrives
            # after the sheet it replaces.
            rows.sort(key=lambda item: (item.set_code_agrees is not True, -item.scan_id))
            found[scan_id] = tuple(rows)
        return found


def replacement_candidates(
    database: ProjectDatabase, scan_id: int
) -> tuple[ReplacementCandidate, ...]:
    """Scans anywhere in the project that might be one rejected sheet's rescan.

    **Conservative, by construction.** A scan is suggested only when its
    *effective* Student ID - after every Resolve-stage correction - is fully
    read and equals the case's identity (the operator's declared ID, or the
    rejected scan's own). Set code is further evidence, reported but never
    required. **File names and batches are never compared**: scanners,
    computers and watched folders name files and batches however they like.

    Excluded: the rejected scan itself, scans not yet read, scans that are
    themselves rejected or superseded, scans already confirmed as another
    case's replacement, and a scan with the rejected scan's exact bytes (a
    re-import, not a rescan) - in whichever batch it arrived.

    An unknown-identity case has no candidates; :func:`association_choices`
    lists what an operator may link by hand.
    """
    with database.session() as session:
        row = _row_for(session, scan_id)
        if row is None or _state(row) is not LifecycleState.REJECTED_PENDING_RESCAN:
            return ()
        batch_id = row.batch_id
    return possible_rescans(database, batch_id).get(scan_id, ())


ASSOCIATION_LIMIT = 200
"""How many scans the manual association list holds at once.

A project may hold a hundred thousand scans; the list is filtered in SQL by
what the operator types and capped, so it never pulls the project into a
widget."""


def association_choices(
    database: ProjectDatabase,
    scan_id: int,
    *,
    search: str = "",
    limit: int = ASSOCIATION_LIMIT,
) -> tuple[ReplacementCandidate, ...]:
    """Scans an operator could link to a rejected case by hand, project-wide.

    Args:
        database: The open project database.
        scan_id: The rejected scan.
        search: Case-insensitive text matched against file names and machine
            Student IDs, in SQL. Empty lists the newest scans.
        limit: At most this many rows.

    For the case the matcher cannot help with - an unreadable Student ID - an
    operator holding the physical sheet can still say which new scan is its
    rescan. Every active, read scan of the **project** that is not already a
    replacement is eligible; suggestions come first. The choice is never made
    here, and every refusal rule of :func:`confirm_replacement` applies.
    """
    with database.session() as session:
        row = _row_for(session, scan_id)
        if row is None or _state(row) is not LifecycleState.REJECTED_PENDING_RESCAN:
            return ()
    case = get_case(database, scan_id)
    if case is None:  # pragma: no cover - checked above
        return ()
    suggested = list(replacement_candidates(database, scan_id))
    seen = {item.scan_id for item in suggested}
    text = search.strip()
    with database.session() as session:
        taken = _replacement_ids(session)
        ineligible = _project_ineligible(session)
        statement = select(BatchScan).where(
            BatchScan.status.in_(sorted(_PROCESSED))
        )
        if text:
            pattern = f"%{text}%"
            statement = statement.where(
                BatchScan.filename.ilike(pattern) | BatchScan.identifier_value.ilike(pattern)
            )
        rows = [
            scan
            for scan in session.scalars(
                statement.order_by(BatchScan.scan_id.desc()).limit(limit + len(seen) + 1)
            ).all()
            if scan.scan_id not in seen
            and not _refusable(case, scan, ineligible=ineligible, taken=taken)
        ][: max(0, limit - len(suggested))]
    readings = _project_readings(database, {scan.batch_id for scan in rows})
    return tuple(suggested) + tuple(_candidate(case, scan, readings) for scan in rows)


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
            active, read scan of this project. **Any batch**: a sheet is often
            rescanned later, elsewhere, under an unrelated file name.
        reviewer: Who confirmed it. Required.
        note: Optional words.

    Returns:
        The case, now superseded.

    Raises:
        LifecycleError: The original is not awaiting a rescan; the replacement
            is the original, is not in this project, has not been read, is itself
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
        # Project-scoped: any batch of this project will do. The project
        # boundary is the database itself - a scan of another project has no
        # row here, and `_require_scan` refuses it.
        replacement = _require_scan(session, replacement_scan_id)
        if session.get(ScanBatch, replacement.batch_id) is None:  # pragma: no cover
            raise LifecycleError(
                f"Scan {replacement_scan_id} has no batch in this project",
                user_message="That scan does not belong to this project.",
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
        mine = _session_of(session, row.batch_id)
        theirs = _session_of(session, replacement.batch_id)
        if mine and theirs and mine != theirs:
            # 0.1.1 phase 4: an examination's population is its scan
            # session's. A rescan read into another session would make one
            # session's result depend on another's sheet.
            raise LifecycleError(
                f"Scan {replacement_scan_id} is in another scan session",
                user_message=(
                    "That scan belongs to a different scan session. A rescan must be "
                    "read into the original's scan session - combine the two "
                    "sessions first (Scan stage, Session menu) if they are one "
                    "examination."
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
        replacement_batch = replacement.batch_id
        row.state = LifecycleState.SUPERSEDED_BY_REPLACEMENT.value
        row.replacement_scan_id = replacement_scan_id
        row.replaced_by = name
        row.replaced_at = moment
        row.updated_at = moment
        where = (
            f" from batch {replacement_batch[:8]}"
            if replacement_batch != batch_id
            else ""
        )
        detail = (
            f"Scan {replacement_scan_id} ({replacement.filename}){where} confirmed as "
            f"the rescan of scan {original_scan_id} ({row.source_name}, batch "
            f"{batch_id[:8]}). The original stays ineligible and is kept for "
            "provenance; the rescan counts in the original's place."
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
            batch_id=replacement_batch,
            action=LifecycleAction.LINKED_AS_REPLACEMENT,
            reviewer=name,
            previous_value=LifecycleState.ACTIVE.value,
            new_value=LifecycleState.ACTIVE.value,
            reason_text=note.strip(),
            detail=detail,
        )
        case = _to_case(
            row,
            replacement_name=replacement.filename or "",
            replacement_batch=replacement_batch,
        )
    _LOGGER.info(
        "Scan %d confirmed as the replacement of scan %d by %s",
        replacement_scan_id,
        original_scan_id,
        name,
    )
    _after_change(
        database, batch_id, replacement_batch, scans=(original_scan_id, replacement_scan_id)
    )
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
                user_message=FileState(row.file_state).unavailable_note,
            )
        batch_id = row.batch_id
        former = row.replacement_scan_id
        former_scan = session.get(BatchScan, former) if former is not None else None
        former_batch = former_scan.batch_id if former_scan is not None else batch_id
        row.state = LifecycleState.REJECTED_PENDING_RESCAN.value
        row.replacement_scan_id = None
        row.replaced_by = ""
        row.replaced_at = None
        row.updated_at = moment
        detail = (
            f"Replacement link to scan {former} withdrawn; scan {original_scan_id} "
            "awaits a rescan again."
        )
        for target, target_batch in ((original_scan_id, batch_id), (former, former_batch)):
            if target is None:
                continue
            _append_event(
                session,
                scan_id=target,
                batch_id=target_batch,
                action=LifecycleAction.REPLACEMENT_REMOVED,
                reviewer=name,
                previous_value=LifecycleState.SUPERSEDED_BY_REPLACEMENT.value,
                new_value=LifecycleState.REJECTED_PENDING_RESCAN.value,
                reason_text=note.strip(),
                detail=detail,
            )
    _LOGGER.info("Scan %d: replacement link removed by %s", original_scan_id, name)
    # The former replacement returns to counting in its own batch, so both
    # batches' duplicate state and reconciliation are re-derived.
    _after_change(
        database,
        batch_id,
        former_batch,
        scans=(original_scan_id, *((former,) if former is not None else ())),
    )


# ----------------------------------------------------------------------
# Re-imports of rejected content
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DuplicateImage:
    """A registered scan left unread because its bytes are already in the session.

    Attributes:
        scan_id: The copy.
        path: Its source path.
        original_scan_id: The sheet it repeats.
        original_name: That sheet's file name, for the operator.
        state: :attr:`LifecycleState.DUPLICATE_CONTENT`, or
            :attr:`LifecycleState.REIMPORT_OF_REJECTED` when the sheet it
            repeats was rejected (the existing re-import rule, applied here
            before recognition instead of after).
    """

    scan_id: int
    path: Path
    original_scan_id: int
    original_name: str
    state: LifecycleState


_UNREAD = tuple(status.value for status in ScanJobStatus if status.is_resumable)


def link_exact_duplicates(
    database: ProjectDatabase, batch_id: str, paths: Sequence[Path] | None = None
) -> tuple[DuplicateImage, ...]:
    """Leave unread every registered scan whose exact bytes the session already has.

    Run as the last step of registering a run's files, after their content
    hashes are recorded and **before any sheet is read** (0.1.1 phase 4,
    ARCHITECTURE_NOTES §9.1): byte-identical images added under another file
    name, folder or batch of the same scan session become one effective
    script, at zero recognition cost.

    * A copy of a **rejected or superseded** scan anywhere in the project is a
      re-import (:func:`sync_reimports`, the existing rule) - now linked
      before recognition.
    * A copy of an earlier sheet of a **live** batch of the same session -
      active, excluded or deferred - is ``duplicate_content``, linked to the
      earliest such sheet (session batch order, then batch position).
    * Another session's identical image is not this session's concern; a
      superseded batch's sheets are not originals (a *Reprocess All* re-reads
      the same files on purpose).

    Both kinds get ``batch_scan.status = duplicate`` (terminal: resume and
    retry leave them alone), a ``scan_rejection`` record pointing at the sheet
    they repeat, and an audit event. Only scans of ``batch_id`` not yet read
    are considered (only ``paths`` of them, when given). Idempotent.
    """
    from omr_scanner.services import scan_sessions, session_population

    sync_reimports(database, batch_id)
    owner = session_population.session_of_batch(database, batch_id)
    batches = list(session_population.session_batch_ids(database, owner)) or [batch_id]
    superseded = set(scan_sessions.live_supersessions(database)) if (
        scan_sessions.has_lifecycle_schema(database)
    ) else set()
    live_batches = [item for item in batches if item not in superseded]
    order = {item: index for index, item in enumerate(batches)}
    wanted = {str(item) for item in paths} if paths is not None else None
    moment = _now()
    linked: list[DuplicateImage] = []
    with database.session() as session:
        targets = [
            row
            for row in session.scalars(
                select(BatchScan)
                .where(BatchScan.batch_id == batch_id)
                .where(BatchScan.content_sha256 != "")
                .where(BatchScan.status.in_(_UNREAD))
                .order_by(BatchScan.batch_index)
            ).all()
            if wanted is None or str(row.source_path) in wanted
        ]
        if not targets:
            return ()
        hashes = sorted({row.content_sha256 for row in targets})
        holders = session.execute(
            select(
                BatchScan.scan_id, BatchScan.batch_id, BatchScan.batch_index,
                BatchScan.filename, BatchScan.content_sha256,
            )
            .where(BatchScan.batch_id.in_(live_batches))
            .where(BatchScan.content_sha256.in_(hashes))
        ).all()
        holder_ids = [int(item[0]) for item in holders]
        states = {
            int(scan_id): LifecycleState(str(state))
            for scan_id, state in session.execute(
                select(ScanRejection.scan_id, ScanRejection.state).where(
                    ScanRejection.scan_id.in_(holder_ids)
                )
            ).all()
        }
        names = {int(item[0]): str(item[3] or "") for item in holders}
        not_originals = {
            LifecycleState.DUPLICATE_CONTENT,
            LifecycleState.REIMPORT_OF_REJECTED,
            LifecycleState.REJECTED_PENDING_RESCAN,
            LifecycleState.SUPERSEDED_BY_REPLACEMENT,
        }
        by_hash: dict[str, list[tuple[tuple[int, int, int], int]]] = {}
        for scan_id, holder_batch, index, _name, digest in holders:
            by_hash.setdefault(str(digest), []).append(
                ((order.get(str(holder_batch), len(order)), int(index), int(scan_id)), int(scan_id))
            )
        for entries in by_hash.values():
            entries.sort()

        for row in targets:
            state = states.get(row.scan_id, LifecycleState.ACTIVE)
            record = _row_for(session, row.scan_id)
            if state is LifecycleState.REIMPORT_OF_REJECTED and record is not None:
                original = int(record.reimport_of_scan_id or 0)
                row.status = ScanJobStatus.DUPLICATE.value
                linked.append(
                    DuplicateImage(
                        scan_id=row.scan_id, path=Path(row.source_path),
                        original_scan_id=original,
                        original_name=names.get(original, "") or _names(
                            session, [original]
                        ).get(original, ""),
                        state=state,
                    )
                )
                continue
            if state is not LifecycleState.ACTIVE:
                continue
            key = (order.get(batch_id, len(order)), int(row.batch_index), int(row.scan_id))
            first = next(
                (
                    scan_id
                    for holder_key, scan_id in by_hash.get(row.content_sha256, [])
                    if holder_key < key
                    and scan_id != row.scan_id
                    and states.get(scan_id, LifecycleState.ACTIVE) not in not_originals
                ),
                None,
            )
            if first is None:
                continue
            original = first
            if record is None:
                record = ScanRejection(scan_id=row.scan_id, batch_id=batch_id, rejected_at=moment)
                session.add(record)
            record.state = LifecycleState.DUPLICATE_CONTENT.value
            record.reason_code = ""
            record.note = ""
            record.source_name = row.filename or ""
            record.source_path = row.source_path or ""
            record.content_sha256 = row.content_sha256
            record.reimport_of_scan_id = original
            record.rejected_by = ""
            record.rejected_at = moment
            record.file_state = FileState.PRESENT.value
            record.updated_at = moment
            row.status = ScanJobStatus.DUPLICATE.value
            states[row.scan_id] = LifecycleState.DUPLICATE_CONTENT
            session.flush()
            _append_event(
                session,
                scan_id=row.scan_id,
                batch_id=batch_id,
                action=LifecycleAction.DUPLICATE_CONTENT_LINKED,
                previous_value=LifecycleState.ACTIVE.value,
                new_value=LifecycleState.DUPLICATE_CONTENT.value,
                detail=(
                    f"Content hash identical to scan {original} "
                    f"('{names.get(original, '')}') of this scan session; the same image "
                    "registered again. Not read, not counted."
                ),
            )
            linked.append(
                DuplicateImage(
                    scan_id=row.scan_id, path=Path(row.source_path),
                    original_scan_id=original, original_name=names.get(original, ""),
                    state=LifecycleState.DUPLICATE_CONTENT,
                )
            )
    if linked:
        _LOGGER.info(
            "Batch %s: %d exact duplicate image(s) linked and left unread", batch_id, len(linked)
        )
    return tuple(linked)


def duplicate_images(database: ProjectDatabase, batch_id: str) -> dict[Path, str]:
    """``source path -> the file name it repeats`` for a batch's unread duplicate images."""
    with database.session() as session:
        rows = session.execute(
            select(BatchScan.source_path, ScanRejection.reimport_of_scan_id)
            .join(ScanRejection, ScanRejection.scan_id == BatchScan.scan_id)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.status == ScanJobStatus.DUPLICATE.value)
        ).all()
        names = _names(session, [int(item) for _path, item in rows if item is not None])
    return {
        Path(str(path)): names.get(int(original), f"scan {original}") if original else ""
        for path, original in rows
    }


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
        linked_scans: list[int] = []
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
            linked_scans.append(scan.scan_id)
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
        _after_change(database, batch_id, scans=linked_scans)
    return linked


def _session_of(session: Session, batch_id: str) -> str | None:
    """A batch's scan session, or ``None`` (no session, or a pre-session schema).

    Two batches that both have no session are not known to be one session,
    but neither are they known to be two; :func:`confirm_replacement` refuses
    only a link between two batches with *different known* sessions.
    """
    # Writes only happen on a migrated (schema >= 14) database, which has the
    # column; a read-only older one never reaches confirm_replacement.
    found = session.scalar(select(ScanBatch.scan_session_id).where(ScanBatch.batch_id == batch_id))
    return str(found) if found else None


def _scan_hash(session: Session, scan_id: int) -> str:
    scan = session.get(BatchScan, scan_id)
    return scan.content_sha256 if scan is not None else ""


# ----------------------------------------------------------------------
# Consequences of a transition
# ----------------------------------------------------------------------
def _after_change(
    database: ProjectDatabase, *batch_ids: str, scans: Sequence[int] | None = None
) -> None:
    """Re-derive everything a lifecycle change affects, in every batch it touches.

    Nothing here is decided afresh: each consequence is produced by the
    machinery that owns it, exactly as if the scans concerned had simply been
    in their new state when that machinery last ran.

    * Duplicate-ID conflicts are re-derived for the identifier groups the
      changed sheets (``scans``) are in or are leaving -
      :func:`~omr_scanner.services.review_store.sync_duplicate_identifiers_for`,
      the bounded pass (0.1.1 phase 4); without ``scans``, the full rebuild
      :func:`~omr_scanner.services.review_store.sync_duplicate_identifiers`.
      An ineligible scan takes no part, and a duplicate that becomes real
      again (after *Undo Reject*, say) is raised again by the same rules.
    * Every reconciliation already run for each batch against an active roster
      is re-run, so no screen or count goes on showing a rejected script as
      valid, or missing a replacement that now stands in for one.

    Scores are not recomputed here - that is the Results stage's explicit
    action - but they are reported stale
    (:func:`~omr_scanner.services.scoring_store.stale_reasons_for`). A cross-
    batch replacement touches two batches, which is why this takes several.
    """
    from omr_scanner.services import reconciliation_store, session_population

    # Once per scan session touched (0.1.1 phase 4): duplicates and
    # reconciliation are session-wide, so two batches of one session are one
    # refresh, and a link between two sessions refreshes both.
    keys = dict.fromkeys(
        session_population.population_key(database, item) for item in batch_ids if item
    )
    if scans:
        try:
            review_store.sync_duplicate_identifiers_for(database, scans)
        except OMRScannerError:
            _LOGGER.exception("Could not re-derive duplicates after a lifecycle change")
    for batch_id in keys:
        try:
            if not scans:
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
            _LOGGER.exception(
                "Could not refresh the session of batch %s after a lifecycle change", batch_id
            )


# ----------------------------------------------------------------------
# Where a cross-batch replacement counts
# ----------------------------------------------------------------------
def adopted_replacements(database: ProjectDatabase, batch_id: str) -> dict[int, int]:
    """Replacements from **other** batches that stand in for this batch's originals.

    Returns:
        ``replacement scan id -> original scan id``.

    A confirmed rescan counts in the batch of the sheet it replaces - that is
    where its candidate's attendance, marks and report live - wherever it was
    itself read. Reconciliation and scoring of ``batch_id`` include these.
    """
    with database.session() as session:
        rows = session.execute(
            select(ScanRejection.replacement_scan_id, ScanRejection.scan_id)
            .join(BatchScan, BatchScan.scan_id == ScanRejection.replacement_scan_id)
            .where(ScanRejection.batch_id == batch_id)
            .where(ScanRejection.state == LifecycleState.SUPERSEDED_BY_REPLACEMENT.value)
            .where(BatchScan.batch_id != batch_id)
        ).all()
    return {int(replacement): int(original) for replacement, original in rows}


def counted_elsewhere(database: ProjectDatabase, batch_id: str) -> dict[int, str]:
    """This batch's scans that count in another batch, as a replacement there.

    Returns:
        ``scan id -> the batch it counts in``.

    Their own batch's reconciliation leaves them out, so a rescan counts once.
    """
    with database.session() as session:
        rows = session.execute(
            select(ScanRejection.replacement_scan_id, ScanRejection.batch_id)
            .join(BatchScan, BatchScan.scan_id == ScanRejection.replacement_scan_id)
            .where(ScanRejection.state == LifecycleState.SUPERSEDED_BY_REPLACEMENT.value)
            .where(BatchScan.batch_id == batch_id)
            .where(ScanRejection.batch_id != batch_id)
        ).all()
    return {int(scan_id): str(original_batch) for scan_id, original_batch in rows}


@dataclass(frozen=True, slots=True)
class ProcessedSheet:
    """One read sheet, as the Resolve stage's *All processed sheets* view lists it.

    Deliberately what the batch row already holds - the machine's readings,
    not the review ledger's effective values - so that a page of the list
    costs one indexed query however large the batch is. The effective
    identity of the one sheet selected is read on selection
    (:func:`current_identity`).

    Attributes:
        scan_id: The scan.
        filename: Its file name.
        identifier: The Student ID as recognition read it.
        set_code: The set code as recognition read it.
        status: Its processing status (completed, warning, failed).
        state: Its lifecycle state.
        open_conflicts: How many of its Resolve records still need a decision.
    """

    scan_id: int
    filename: str
    identifier: str
    set_code: str
    status: str
    state: LifecycleState
    open_conflicts: int = 0


PROCESSED_STATUSES = frozenset(
    {ScanJobStatus.COMPLETED.value, ScanJobStatus.WARNING.value, ScanJobStatus.FAILED.value}
)
"""What *processed* means for the inspection view: recognition has run."""


def processed_sheets(
    database: ProjectDatabase,
    batch_id: str,
    *,
    search: str = "",
    limit: int | None = 500,
    offset: int = 0,
    session_wide: bool = False,
) -> tuple[ProcessedSheet, ...]:
    """Every read sheet of a batch, conflict or not, filtered and paged in SQL.

    ``session_wide`` lists the live batches of the whole scan session
    ``batch_id`` belongs to (the Resolve stage, 0.1.1 phase 4).

    What makes a **cleanly read** sheet reachable for Reject & Rescan: a scan
    that raised no conflict appears in no conflict queue, yet an operator
    holding the paper may know it is folded, clipped or the wrong page. This
    lists sheets, not problems, and counts nothing as unresolved.
    """
    text = search.strip()
    batches = [batch_id]
    if session_wide:
        from omr_scanner.services import session_population

        batches = list(session_population.population(database, batch_id).live_batch_ids)
    with database.session() as session:
        open_count = (
            select(func.count())
            .select_from(ReviewConflict)
            .where(ReviewConflict.scan_id == BatchScan.scan_id)
            .where(ReviewConflict.conflict_type.in_([item.value for item in RESOLUTION_TYPES]))
            .where(
                ReviewConflict.state.in_(
                    [ConflictState.OPEN.value, ConflictState.DEFERRED.value]
                )
            )
            .scalar_subquery()
        )
        statement = (
            select(BatchScan, ScanRejection.state, open_count)
            .join(ScanRejection, ScanRejection.scan_id == BatchScan.scan_id, isouter=True)
            .where(BatchScan.batch_id.in_(batches))
            .where(BatchScan.status.in_(sorted(PROCESSED_STATUSES)))
        )
        if text:
            pattern = f"%{text}%"
            statement = statement.where(
                BatchScan.filename.ilike(pattern) | BatchScan.identifier_value.ilike(pattern)
            )
        statement = statement.order_by(BatchScan.batch_id, BatchScan.batch_index)
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        found: list[ProcessedSheet] = []
        for scan, state, conflicts in session.execute(statement).all():
            try:
                lifecycle = LifecycleState(state) if state else LifecycleState.ACTIVE
            except ValueError:  # pragma: no cover - a state from a newer build
                lifecycle = LifecycleState.REJECTED_PENDING_RESCAN
            found.append(
                ProcessedSheet(
                    scan_id=scan.scan_id,
                    filename=scan.filename or "",
                    identifier=scan.identifier_value or "",
                    set_code=scan.set_code_value or "",
                    status=scan.status,
                    state=lifecycle,
                    open_conflicts=int(conflicts or 0),
                )
            )
        return tuple(found)


def batch_of(database: ProjectDatabase, scan_id: int) -> str | None:
    """The batch a scan was read into, or ``None`` when it is not in the project."""
    with database.session() as session:
        scan = session.get(BatchScan, scan_id)
        return scan.batch_id if scan is not None else None



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
    "ASSOCIATION_LIMIT",
    "PROCESSED_STATUSES",
    "QUARANTINE_DIR_NAME",
    "LifecycleError",
    "LifecycleEvent",
    "ProcessedSheet",
    "SheetFacts",
    "adopted_replacements",
    "association_choices",
    "batch_of",
    "cases_by_scan",
    "confirm_replacement",
    "count_cases",
    "counted_elsewhere",
    "current_identity",
    "defer_scan",
    "deferred_for_set",
    "exclude_scan",
    "execute_purge",
    "get_case",
    "incomplete_exports",
    "ineligible_scan_ids",
    "keep_script",
    "lifecycle_history",
    "lifecycle_states",
    "list_cases",
    "list_dispositions",
    "managed_roots",
    "outstanding_for_set",
    "owned_path",
    "plan_purge",
    "possible_rescans",
    "processed_sheets",
    "quarantine_root",
    "record_incomplete_export",
    "reject_scan",
    "remove_replacement",
    "removed_images",
    "replacement_candidates",
    "restore_scan",
    "sheet_facts",
    "state_of",
    "sync_reimports",
    "undo_reject",
    "with_live_identity",
]
