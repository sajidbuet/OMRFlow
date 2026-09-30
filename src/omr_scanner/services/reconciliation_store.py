"""Store the candidate roster, the reconciliation and every human decision.

Purpose:
    The repository layer for Phase 7. Imports a validated roster, runs
    reconciliation, persists its result so the interface can filter and count a
    large cohort in SQL, and records every operator decision in the **same
    append-only ledger** Phase 6 established.

Scope:
    Database access only. The classification rules live in
    :mod:`omr_scanner.services.reconciliation` (pure), the file reading in
    :mod:`omr_scanner.services.candidate_import` (pure), and nothing here
    imports Qt.

Three arrangements worth understanding before changing anything:

**The roster is written once and never updated.** What the file said about a
candidate is what it said. An operator who establishes otherwise creates a
decision beside it; :func:`entries_for` then reports the imported value and the
effective one together. Re-importing makes a *new* roster and deactivates the
old, so the decisions taken against the old one remain interpretable.

**Reconciliation output is a cache, decisions are the input.** The entry and
script tables are rewritten wholesale on every :func:`reconcile_batch`, because
a stale classification that looks current is worse than none. Decisions survive
that, which is what makes re-running safe.

**Every human action appends to** :class:`~omr_scanner.database.models.AuditEvent`
in the same transaction as the decision it records, under ``entity_type`` of
``candidate`` or ``script``. There is no update or delete for events here, for
the same reason there is none in :mod:`omr_scanner.services.review_store`.

Privacy:
    No function in this module logs a candidate ID, a name or a marks value.
    Counts, scan ids and roster ids only. See ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, and_, delete, func, select

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    CandidateRoster,
    ReconciliationDecision,
    ReconciliationEntryRow,
    ReconciliationRun,
    ReconciliationScript,
    RegisteredCandidate,
    ScanRejection,
)
from omr_scanner.domain.reconciliation import (
    AttendanceSource,
    AttendanceState,
    CandidateDecision,
    CandidateRecord,
    ReconciliationAction,
    ReconciliationCounts,
    ReconciliationEntry,
    ReconciliationIssue,
    ReconciliationReason,
    ReconciliationStatus,
    ResolutionState,
    ScriptAssignment,
    ScriptDecision,
    ScriptRecord,
    ScriptView,
)
from omr_scanner.domain.scan_lifecycle import LifecycleState
from omr_scanner.errors import OMRScannerError
from omr_scanner.services import scan_lifecycle
from omr_scanner.services.reconciliation import (
    ReconciliationInput,
    count_entries,
    reconcile,
)
from omr_scanner.services.reconciliation_leads import OutOfSetScript
from omr_scanner.services.review_store import effective_identifiers, effective_set_codes

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.scan_lifecycle import RescanCase
    from omr_scanner.services.candidate_import import RosterValidation
    from omr_scanner.services.review_store import EffectiveIdentifier

_LOGGER = logging.getLogger(__name__)

_ANY_SET: Any = object()
"""Sentinel for "every set" in :func:`list_rosters`.

``None`` already means something specific there - the unscoped rosters a
project had before attendance was per-set - so "no filter at all" needs a
value of its own rather than reusing it."""

ENTITY_CANDIDATE = "candidate"
ENTITY_SCRIPT = "script"
TARGET_CANDIDATE = "candidate"
TARGET_SCRIPT = "script"


class ReconciliationError(OMRScannerError):
    """A reconciliation action was refused.

    Always carries a ``user_message``: an operator is mid-decision and needs to
    know what to do differently, not that something went wrong.
    """


def _now() -> datetime:
    """The current instant, timezone-aware."""
    return datetime.now(UTC)


# ----------------------------------------------------------------------
# Value objects returned to callers
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RosterSummary:
    """One imported roster, as the interface lists it."""

    roster_id: int
    created_at: datetime
    source_name: str
    source_sheet: str
    candidate_count: int
    expected_present: int
    expected_absent: int
    attendance_unknown: int
    has_attendance_column: bool
    is_active: bool
    imported_by: str = ""
    set_id: str | None = None
    """The examination set this roster belongs to, or ``None`` for a roster
    imported before attendance was per-set."""
    source_path: str = ""
    """The full path it was imported from, or ``""`` when that was not recorded
    (a roster imported before migration 10, or through a path with no file)."""

    @property
    def describe(self) -> str:
        """A one-line description for a list or a status label."""
        where = f" ({self.source_sheet})" if self.source_sheet else ""
        return f"{self.source_name}{where} - {self.candidate_count} candidate(s)"


@dataclass(frozen=True, slots=True)
class ReconciliationAuditRecord:
    """One entry from the shared ledger, about a candidate or a script."""

    event_id: int
    occurred_at: datetime
    entity_type: str
    entity_id: str
    scan_id: int
    action: ReconciliationAction
    reviewer: str
    previous_value: str
    new_value: str
    machine_value: str
    reason_code: str
    reason_text: str
    detail: str

    @property
    def reason(self) -> str:
        """The reason as text, preferring the operator's own words."""
        if self.reason_text:
            return self.reason_text
        if not self.reason_code:
            return ""
        try:
            return ReconciliationReason(self.reason_code).label
        except ValueError:
            return self.reason_code


# ----------------------------------------------------------------------
# Validation of a human action
# ----------------------------------------------------------------------
def validate_operator(name: str) -> str:
    """Return a usable operator name, or refuse the action.

    The same rule Phase 6 applies to a reviewer, for the same reason: the exit
    criterion is that every decision traces to a *named* person, so an unnamed
    one is not allowed to exist.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ReconciliationError(
            "No operator name configured",
            user_message=(
                "Set your name in File > Settings > Reviewer before recording "
                "a reconciliation decision. Every decision is recorded against "
                "the person who made it."
            ),
        )
    return cleaned


def validate_reason(reason: ReconciliationReason, reason_text: str) -> str:
    """Return the explanation for a decision, or refuse the action."""
    cleaned = reason_text.strip()
    if reason.requires_text and not cleaned:
        raise ReconciliationError(
            "Reason 'Other' requires an explanation",
            user_message=(
                "Choosing 'Other' needs a short explanation, so that somebody "
                "reading this record later can tell what was decided and why."
            ),
        )
    return cleaned


# ----------------------------------------------------------------------
# The ledger
# ----------------------------------------------------------------------
def _append_event(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    action: ReconciliationAction,
    batch_id: str = "",
    scan_id: int = 0,
    reviewer: str = "",
    previous_value: str = "",
    new_value: str = "",
    machine_value: str = "",
    reason_code: str = "",
    reason_text: str = "",
    detail: str = "",
) -> AuditEvent:
    """Append one entry to the shared provenance ledger.

    The only way this module writes an
    :class:`~omr_scanner.database.models.AuditEvent`, and - as in
    :mod:`omr_scanner.services.review_store` - there is deliberately no update
    or delete beside it. ``entity_id`` carries a candidate ID or a scan id;
    both are candidate-identifying, so they live in the database, never in a
    log line.
    """
    event = AuditEvent(
        occurred_at=_now(),
        batch_id=batch_id,
        scan_id=scan_id,
        conflict_id=0,
        entity_type=entity_type,
        entity_id=entity_id,
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
    # Flushed inside the transaction so a failure surfaces here rather than at
    # commit, when the decision row has already been written.
    session.flush()
    return event


# ----------------------------------------------------------------------
# Importing a roster
# ----------------------------------------------------------------------
def import_roster(
    database: ProjectDatabase,
    validation: RosterValidation,
    *,
    imported_by: str = "",
    activate: bool = True,
    set_id: str | None = None,
    source_path: Path | None = None,
) -> int:
    """Store a validated roster and, by default, make it the active one.

    Args:
        database: The open project database.
        validation: The result of
            :func:`omr_scanner.services.candidate_import.read_roster`.
        imported_by: Who imported it, if known.
        activate: Whether this becomes the roster reconciliation reads.
        set_id: The examination set this attendance list belongs to. Activating
            supersedes the previous active roster **for that set only** -
            importing Set 11's list must never deactivate Set 10's. ``None``
            keeps the pre-Part-2 behaviour of one unscoped roster per project,
            which is what a project with no defined sets still uses.
        source_path: The file it was read from, recorded in full so that two
            files of the same name in different folders stay distinguishable.

    Returns:
        The new roster's id.

    Raises:
        ReconciliationError: The validation did not pass. A roster with a
            repeated candidate ID or no candidates at all is never stored -
            "partially imported" is the one state this must not leave behind.

    Everything is written in **one transaction**: the roster row, every
    candidate, and the deactivation of the previous roster. A failure leaves
    the project exactly as it was.
    """
    if not validation.can_import:
        blocking = validation.blocking_issues
        raise ReconciliationError(
            f"Roster rejected: {len(blocking)} blocking issue(s)",
            user_message=(
                blocking[0].message
                if blocking
                else "The candidate list contains no candidates to import."
            ),
        )

    mapping = validation.mapping
    with database.session() as session:
        if activate:
            # Scoped to this set: another set's active roster is untouched,
            # because they are independent candidate lists that merely happen
            # to belong to the same examination.
            for row in session.scalars(
                select(CandidateRoster).where(_active_roster_filter(set_id))
            ).all():
                row.is_active = False

        roster = CandidateRoster(
            set_id=set_id,
            created_at=_now(),
            source_name=validation.source_name,
            source_path=str(source_path) if source_path is not None else "",
            source_sheet=validation.sheet,
            source_format=validation.source_name.rsplit(".", 1)[-1].casefold()[:10],
            column_map=json.dumps(
                {
                    "candidate_id": mapping.candidate_id,
                    "name": mapping.name,
                    "attendance": mapping.attendance,
                }
            ),
            rows_read=validation.rows_read,
            candidate_count=len(validation.candidates),
            expected_present=validation.expected_present,
            expected_absent=validation.expected_absent,
            attendance_unknown=validation.attendance_unknown,
            has_attendance_column=mapping.attendance is not None,
            is_active=activate,
            imported_by=imported_by.strip(),
        )
        session.add(roster)
        session.flush()

        session.add_all(
            RegisteredCandidate(
                roster_id=roster.roster_id,
                candidate_id=item.candidate_id,
                display_name=item.display_name,
                row_order=order,
                source_row=item.source_row,
                imported_attendance=item.imported_attendance.value,
                imported_value=item.imported_value,
            )
            for order, item in enumerate(validation.candidates)
        )
        roster_id = roster.roster_id

    _LOGGER.info(
        "Candidate roster imported: roster=%d candidates=%d present=%d absent=%d "
        "unknown=%d active=%s",
        roster_id,
        len(validation.candidates),
        validation.expected_present,
        validation.expected_absent,
        validation.attendance_unknown,
        activate,
    )
    return roster_id


def _roster_set_filter(set_id: str | None) -> ColumnElement[bool]:
    """Match the rosters belonging to one set, or to no set.

    ``set_id=None`` deliberately matches only rosters whose ``set_id`` *is*
    NULL rather than every roster: "the project's unscoped roster" and "Set
    10's roster" are different things, and a query that confused them would
    hand one set's candidate list to another - see §19.
    """
    if set_id is None:
        return CandidateRoster.set_id.is_(None)
    return CandidateRoster.set_id == set_id


def _active_roster_filter(set_id: str | None) -> ColumnElement[bool]:
    """Match the active roster(s) belonging to one set, or to no set.

    A predicate rather than a prebuilt statement, so that each caller owns the
    ordering and limit it needs - and so that nothing here has to name the
    element type of a SELECT, whose spelling changed between SQLAlchemy 2.0
    and 2.1. A column predicate is spelled the same in both.
    """
    return and_(CandidateRoster.is_active.is_(True), _roster_set_filter(set_id))


def list_rosters(
    database: ProjectDatabase, *, set_id: str | None = _ANY_SET
) -> tuple[RosterSummary, ...]:
    """Return imported rosters, newest first.

    Args:
        database: The open project database.
        set_id: Which set's rosters to list. Omit for *every* roster in the
            project, pass a set id for that set's, or pass ``None`` for the
            unscoped rosters a pre-Part-2 project has. The sentinel default
            exists because ``None`` is itself a meaningful value here.
    """
    with database.session() as session:
        query = select(CandidateRoster).order_by(CandidateRoster.roster_id.desc())
        if set_id is not _ANY_SET:
            query = query.where(_roster_set_filter(set_id))
        return tuple(_roster_summary(row) for row in session.scalars(query).all())


def active_roster(
    database: ProjectDatabase, set_id: str | None = None
) -> RosterSummary | None:
    """Return the roster reconciliation reads for one set, or ``None``.

    Args:
        database: The open project database.
        set_id: The set whose attendance list is wanted. ``None`` asks for the
            project's unscoped roster - the only kind that exists before sets
            are defined, and the only kind a project with no sets ever has.

    A set with no imported attendance returns ``None`` **rather than falling
    back** to another set's roster or to the unscoped one. That refusal is the
    point: §15 requires a missing attendance workbook to stop the work with a
    message, never to borrow a neighbour's.
    """
    with database.session() as session:
        row = session.scalars(
            select(CandidateRoster)
            .where(_active_roster_filter(set_id))
            .order_by(CandidateRoster.roster_id.desc())
            .limit(1)
        ).first()
        return _roster_summary(row) if row is not None else None


def unassigned_rosters(database: ProjectDatabase) -> tuple[RosterSummary, ...]:
    """Return rosters imported before attendance was per-set.

    What migration 9 deliberately refused to guess at. An interface can offer
    these to an operator to assign to a set explicitly (§29); nothing assigns
    one automatically.
    """
    return list_rosters(database, set_id=None)


def assign_roster_to_set(
    database: ProjectDatabase, roster_id: int, set_id: str
) -> None:
    """Attach an existing roster to a set, and make it that set's active one.

    The explicit resolution §29 asks for: an operator states which set a
    pre-Part-2 candidate list belongs to, rather than the application
    inferring it.

    Raises:
        ReconciliationError: No such roster, or it already belongs to a
            different set - re-pointing a roster that candidates, results and
            reconciliation entries already hang off would change which set
            those records belong to, which is not an edit this makes quietly.
    """
    with database.session() as session:
        target = session.get(CandidateRoster, roster_id)
        if target is None:
            raise ReconciliationError(
                f"Roster {roster_id} not found",
                user_message="That candidate list is no longer in this project.",
            )
        if target.set_id is not None and target.set_id != set_id:
            raise ReconciliationError(
                f"Roster {roster_id} already belongs to set {target.set_id}",
                user_message=(
                    "That candidate list already belongs to a different set. "
                    "Import it again for this set instead."
                ),
            )
        for row in session.scalars(
            select(CandidateRoster).where(_active_roster_filter(set_id))
        ).all():
            row.is_active = False
        target.set_id = set_id
        target.is_active = True
    _LOGGER.info("Roster %d assigned to set %s", roster_id, set_id)


def _roster_summary(row: CandidateRoster) -> RosterSummary:
    """Convert a roster row into a detached summary."""
    return RosterSummary(
        roster_id=row.roster_id,
        created_at=row.created_at,
        source_name=row.source_name,
        source_sheet=row.source_sheet,
        candidate_count=row.candidate_count,
        expected_present=row.expected_present,
        expected_absent=row.expected_absent,
        attendance_unknown=row.attendance_unknown,
        has_attendance_column=row.has_attendance_column,
        is_active=row.is_active,
        imported_by=row.imported_by,
        set_id=row.set_id,
        source_path=row.source_path or "",
    )


def set_active_roster(database: ProjectDatabase, roster_id: int) -> None:
    """Make one roster the active one.

    Reconciliation results for other rosters are left alone rather than
    deleted: they were computed against a different candidate list and remain a
    true record of what that list implied.
    """
    with database.session() as session:
        target = session.get(CandidateRoster, roster_id)
        if target is None:
            raise ReconciliationError(
                f"Roster {roster_id} not found",
                user_message="That candidate list is no longer in this project.",
            )
        # Only among this roster's own set: activating Set 11's older list
        # must not deactivate Set 10's current one.
        for row in session.scalars(
            select(CandidateRoster).where(_active_roster_filter(target.set_id))
        ).all():
            row.is_active = False
        target.is_active = True
    _LOGGER.info("Active candidate roster changed: roster=%d", roster_id)


def roster_candidates(
    database: ProjectDatabase, roster_id: int
) -> tuple[CandidateRecord, ...]:
    """Return one roster's candidates, in file order."""
    with database.session() as session:
        rows = session.scalars(
            select(RegisteredCandidate)
            .where(RegisteredCandidate.roster_id == roster_id)
            .order_by(RegisteredCandidate.row_order)
        ).all()
        return tuple(
            CandidateRecord(
                candidate_id=row.candidate_id,
                display_name=row.display_name,
                source_row=row.source_row,
                imported_attendance=AttendanceState(row.imported_attendance),
                imported_value=row.imported_value,
            )
            for row in rows
        )


def find_candidates(
    database: ProjectDatabase, roster_id: int, query: str, *, limit: int = 50
) -> tuple[CandidateRecord, ...]:
    """Search a roster by candidate ID or name, for an assignment dialog.

    Filtered in SQL and capped: an operator assigning one script must not pull
    a ten-thousand-candidate roster into memory to do it.
    """
    text = query.strip()
    with database.session() as session:
        statement = select(RegisteredCandidate).where(
            RegisteredCandidate.roster_id == roster_id
        )
        if text:
            pattern = f"%{text}%"
            statement = statement.where(
                RegisteredCandidate.candidate_id.ilike(pattern)
                | RegisteredCandidate.display_name.ilike(pattern)
            )
        rows = session.scalars(
            statement.order_by(RegisteredCandidate.row_order).limit(limit)
        ).all()
        return tuple(
            CandidateRecord(
                candidate_id=row.candidate_id,
                display_name=row.display_name,
                source_row=row.source_row,
                imported_attendance=AttendanceState(row.imported_attendance),
                imported_value=row.imported_value,
            )
            for row in rows
        )


def candidate_exists(database: ProjectDatabase, roster_id: int, candidate_id: str) -> bool:
    """Whether a candidate ID is on a roster."""
    with database.session() as session:
        return (
            session.scalars(
                select(RegisteredCandidate.candidate_row_id)
                .where(RegisteredCandidate.roster_id == roster_id)
                .where(RegisteredCandidate.candidate_id == candidate_id)
                .limit(1)
            ).first()
            is not None
        )


# ----------------------------------------------------------------------
# Reading the scripts a batch produced
# ----------------------------------------------------------------------
class ScriptSetPlacement(StrEnum):
    """Where one script stands relative to a set's reconciliation.

    What a set's reconciliation is allowed to see. **Only**
    :attr:`IN_SET` is reconciled; every other script is counted and reported
    rather than silently dropped, so an operator can see what is outside the
    set and why.

    Reject & Rescan adds three placements. A script rejected pending a rescan
    in this set is :attr:`REJECTED` - filed under its candidate, never
    counted; a superseded original or an exact re-import of rejected content
    is :attr:`SUPERSEDED` - outside every set's reconciliation; and a confirmed
    replacement is simply :attr:`IN_SET`, like any other active script.
    """

    IN_SET = "in_set"
    """The effective set code is this set's code."""

    OTHER_SET = "other_set"
    """The effective set code is another defined set's code. Outside this
    set's reconciliation universe entirely: never an unknown ID here."""

    UNRESOLVED = "unresolved"
    """The set code is still in dispute (blank, double-marked, too faint, not
    a defined set, or the sheet could not be read). Reviewed on the Resolve
    stage; reconciled once it names a set."""

    UNDEFINED = "undefined"
    """The set code was read and settled, but names no defined set - a
    decision somebody made on the Resolve stage that still names no set."""

    REJECTED = "rejected"
    """Rejected pending a rescan, and belonging to this set - by the
    operator's declared set code, else its effective one. Filed under its
    candidate as *rescan required*; never counted."""

    REJECTED_UNPLACED = "rejected_unplaced"
    """Rejected pending a rescan, with no known set. Reconciled by no set -
    and reported by every set, because it could be anybody's."""

    SUPERSEDED = "superseded"
    """A rejected original whose rescan has been confirmed, or an exact
    re-import of rejected content. Outside every set's reconciliation."""

    COUNTED_ELSEWHERE = "counted_elsewhere"
    """A confirmed rescan of a sheet from **another** batch. It counts there,
    in its original's place, and so is left out here - a rescan counts once."""

    EXCLUDED = "excluded"
    """Rejected / excluded on the Attendance stage. Outside every set's
    reconciliation, duplicate detection and results; counted in the scope so
    it never disappears from the arithmetic."""

    DEFERRED = "deferred"
    """Deferred on the Attendance stage. Filed under its candidate when it
    reads as one of this set's registered candidates - never counted - and
    otherwise listed only as deferred."""


_LIFECYCLE_PLACEMENTS = frozenset(
    {
        ScriptSetPlacement.REJECTED,
        ScriptSetPlacement.REJECTED_UNPLACED,
        ScriptSetPlacement.SUPERSEDED,
        ScriptSetPlacement.COUNTED_ELSEWHERE,
        ScriptSetPlacement.EXCLUDED,
        ScriptSetPlacement.DEFERRED,
    }
)


@dataclass(frozen=True, slots=True)
class _BatchReadings:
    """A batch's scripts' effective values, including adopted replacements.

    Attributes:
        identifiers / set_codes: The review ledger's effective values for
            every scan of the batch **and** for every replacement from another
            batch that stands in for one of its originals (read from that
            replacement's own batch, where its review records live).
        adopted: ``replacement scan id -> original scan id`` for those.
        elsewhere: This batch's scans that count in another batch instead.
    """

    identifiers: dict[int, EffectiveIdentifier]
    set_codes: dict[int, EffectiveIdentifier]
    adopted: dict[int, int]
    elsewhere: dict[int, str]


def _batch_readings(database: ProjectDatabase, batch_id: str) -> _BatchReadings:
    """Read a batch's effective values, adopting cross-batch replacements.

    Reconciliation, scoring and reporting all work per batch, and a sheet's
    candidate lives in the batch the sheet was first read in. A rescan read
    into a later batch therefore **stands in for its original in the
    original's batch** - that is where it is reconciled and marked - and is
    left out of its own batch's reconciliation so it counts exactly once.
    """
    identifiers = dict(effective_identifiers(database, batch_id))
    set_codes = dict(effective_set_codes(database, batch_id))
    adopted = scan_lifecycle.adopted_replacements(database, batch_id)
    by_batch: dict[str, list[int]] = {}
    for scan_id in adopted:
        other = scan_lifecycle.batch_of(database, scan_id)
        if other is not None:
            by_batch.setdefault(other, []).append(scan_id)
    for other, scan_ids in by_batch.items():
        theirs = effective_identifiers(database, other)
        codes = effective_set_codes(database, other)
        for scan_id in scan_ids:
            if scan_id in theirs:
                identifiers[scan_id] = theirs[scan_id]
            if scan_id in codes:
                set_codes[scan_id] = codes[scan_id]
    return _BatchReadings(
        identifiers=identifiers,
        set_codes=set_codes,
        adopted=adopted,
        elsewhere=scan_lifecycle.counted_elsewhere(database, batch_id),
    )


@dataclass(frozen=True, slots=True)
class SetScriptScope:
    """How a batch's scripts divide around one set.

    Attributes:
        set_code: The set, or ``""`` when reconciliation is unscoped.
        in_set: Active scripts reconciled for this set.
        other_sets: Scripts belonging to another defined set, by that set's code.
        unresolved: Scripts whose set code is still in dispute.
        undefined: Scripts whose settled set code is no defined set.
        rescan_required: Rejected scripts awaiting a rescan that belong to this
            set or to no known set.
        superseded: Superseded originals and re-imports of rejected content
            belonging to this set (or, unscoped, to the batch).
        counted_elsewhere: This batch's scans that are confirmed rescans of
            sheets in another batch, and are counted there instead.
        excluded: Sheets rejected / excluded on the Attendance stage that
            belong to this set (or to no known set). Not counted.
        deferred: Sheets deferred on the Attendance stage that belong to this
            set (or to no known set). Not counted while deferred.
    """

    set_code: str
    in_set: int = 0
    other_sets: dict[str, int] = field(default_factory=dict)
    unresolved: int = 0
    undefined: int = 0
    rescan_required: int = 0
    superseded: int = 0
    counted_elsewhere: int = 0
    excluded: int = 0
    deferred: int = 0

    @property
    def other_set_total(self) -> int:
        """How many scripts belong to other sets."""
        return sum(self.other_sets.values())

    @property
    def needs_attention(self) -> int:
        """Scripts no set can reconcile until their set code is settled."""
        return self.unresolved + self.undefined


def _placements(
    database: ProjectDatabase, batch_id: str, set_code: str
) -> dict[int, tuple[ScriptSetPlacement, str]]:
    """Place every script of a batch relative to one set's code.

    The single definition of "which scripts belong to a set", used both to
    choose what is reconciled and to report what was left out. Decided on the
    **effective** set code - after any Resolve-stage correction - never on
    what the machine read.
    """
    from omr_scanner.services import project_sets

    defined = {item.code for item in project_sets.list_sets(database)}
    cases = scan_lifecycle.cases_by_scan(database, batch_id, live=False)
    readings = _batch_readings(database, batch_id)
    placements: dict[int, tuple[ScriptSetPlacement, str]] = {}
    for scan_id, found in readings.set_codes.items():
        code = found.value
        if scan_id in readings.elsewhere:
            placements[scan_id] = (ScriptSetPlacement.COUNTED_ELSEWHERE, code)
            continue
        case = cases.get(scan_id)
        if case is not None:
            # A lifecycle decision outranks the set code: a rejected or
            # superseded scan is never IN_SET, whatever its code says.
            placements[scan_id] = _lifecycle_placement(case, found, set_code, defined)
            continue
        if found.unresolved or not code or "?" in code or "_" in code:
            placements[scan_id] = (ScriptSetPlacement.UNRESOLVED, code)
        elif code == set_code:
            placements[scan_id] = (ScriptSetPlacement.IN_SET, code)
        elif code in defined:
            placements[scan_id] = (ScriptSetPlacement.OTHER_SET, code)
        else:
            placements[scan_id] = (ScriptSetPlacement.UNDEFINED, code)
    return placements


def _case_set_code(case: RescanCase, found: EffectiveIdentifier | None) -> str:
    """The set a rejected scan belongs to: declared, else effective, else ``""``."""
    if case.declared_set_code:
        return case.declared_set_code
    if found is None or found.unresolved:
        return ""
    value = found.value
    return "" if not value or "?" in value or "_" in value else value


def _case_identity(case: RescanCase, found: EffectiveIdentifier | None) -> str:
    """The Student ID a rejected scan is filed under: declared, else effective."""
    if case.declared_candidate_id:
        return case.declared_candidate_id
    if found is None or found.unresolved:
        return ""
    value = found.value
    return "" if not value or "?" in value or "_" in value else value


def _lifecycle_placement(
    case: RescanCase,
    found: EffectiveIdentifier | None,
    set_code: str,
    defined: set[str],
) -> tuple[ScriptSetPlacement, str]:
    """Place a rejected, superseded, re-imported, excluded or deferred scan."""
    code = _case_set_code(case, found)
    if case.state is LifecycleState.EXCLUDED:
        return ScriptSetPlacement.EXCLUDED, code
    if case.state is LifecycleState.DEFERRED:
        return ScriptSetPlacement.DEFERRED, code
    if case.state is not LifecycleState.REJECTED_PENDING_RESCAN:
        return ScriptSetPlacement.SUPERSEDED, code
    if code and code == set_code:
        return ScriptSetPlacement.REJECTED, code
    if code and code in defined:
        return ScriptSetPlacement.OTHER_SET, code
    return ScriptSetPlacement.REJECTED_UNPLACED, code


def _set_code_of_roster(database: ProjectDatabase, roster_id: int) -> str | None:
    """The code of the set a roster belongs to, or ``None`` when unscoped."""
    from omr_scanner.services import project_sets

    with database.session() as session:
        row = session.get(CandidateRoster, roster_id)
        set_id = row.set_id if row is not None else None
    if set_id is None:
        return None
    exam_set = project_sets.get_set(database, set_id)
    return exam_set.code if exam_set is not None else None


def script_scope(database: ProjectDatabase, roster_id: int, batch_id: str) -> SetScriptScope:
    """Count how a batch's scripts divide around one roster's set.

    For an unscoped roster (a project with no sets, or a list from before
    attendance was per-set) every script is in scope, as it always was.
    """
    set_code = _set_code_of_roster(database, roster_id)
    if set_code is None:
        states = scan_lifecycle.lifecycle_states(database, batch_id)
        scripts = batch_scripts(database, batch_id)
        return SetScriptScope(
            set_code="",
            in_set=sum(1 for item in scripts if not item.rejected and not item.deferred),
            rescan_required=sum(
                1
                for item in states.values()
                if item is LifecycleState.REJECTED_PENDING_RESCAN
            ),
            superseded=sum(
                1
                for item in states.values()
                if item
                in (LifecycleState.SUPERSEDED_BY_REPLACEMENT, LifecycleState.REIMPORT_OF_REJECTED)
            ),
            counted_elsewhere=len(scan_lifecycle.counted_elsewhere(database, batch_id)),
            excluded=sum(1 for item in states.values() if item is LifecycleState.EXCLUDED),
            deferred=sum(1 for item in states.values() if item is LifecycleState.DEFERRED),
        )
    in_set = unresolved = undefined = rescan = superseded = elsewhere = 0
    excluded = deferred = 0
    others: dict[str, int] = {}
    for placement, code in _placements(database, batch_id, set_code).values():
        if placement is ScriptSetPlacement.IN_SET:
            in_set += 1
        elif placement is ScriptSetPlacement.OTHER_SET:
            others[code] = others.get(code, 0) + 1
        elif placement is ScriptSetPlacement.UNRESOLVED:
            unresolved += 1
        elif placement in (ScriptSetPlacement.REJECTED, ScriptSetPlacement.REJECTED_UNPLACED):
            rescan += 1
        elif placement is ScriptSetPlacement.SUPERSEDED:
            if code == set_code or not code:
                superseded += 1
        elif placement is ScriptSetPlacement.COUNTED_ELSEWHERE:
            elsewhere += 1
        elif placement is ScriptSetPlacement.EXCLUDED:
            if code == set_code or not code:
                excluded += 1
        elif placement is ScriptSetPlacement.DEFERRED:
            if code == set_code or not code:
                deferred += 1
        else:
            undefined += 1
    return SetScriptScope(
        set_code=set_code,
        in_set=in_set,
        other_sets=dict(sorted(others.items())),
        unresolved=unresolved,
        undefined=undefined,
        rescan_required=rescan,
        superseded=superseded,
        counted_elsewhere=elsewhere,
        excluded=excluded,
        deferred=deferred,
    )


def out_of_set_scripts(
    database: ProjectDatabase, roster_id: int, batch_id: str
) -> tuple[OutOfSetScript, ...]:
    """The scripts a set's reconciliation left out, as sources of leads.

    A candidate reported *Missing script* may well have one: its set code is
    unresolved, names no defined set, or was read as another set. These are
    offered as leads (see
    :func:`omr_scanner.services.reconciliation_leads.script_leads`) - another
    set's script only for an identical ID, because the same roll number in two
    sets is normally two different people. Empty for an unscoped roster.
    """
    set_code = _set_code_of_roster(database, roster_id)
    if set_code is None:
        return ()
    placements = _placements(database, batch_id, set_code)
    found: list[OutOfSetScript] = []
    for script in batch_scripts(database, batch_id):
        placement, code = placements.get(script.scan_id, (ScriptSetPlacement.IN_SET, ""))
        if placement is ScriptSetPlacement.IN_SET or placement in _LIFECYCLE_PLACEMENTS:
            # A rejected or superseded scan is no lead for anybody: it is not
            # a script that counts, and its rescan is found on Resolve.
            continue
        if placement is ScriptSetPlacement.OTHER_SET:
            reason, exact = f"Read as set {code} - check its set code", True
        elif placement is ScriptSetPlacement.UNDEFINED:
            reason, exact = f"Set code '{code}' is not a defined set", False
        else:
            reason, exact = "Set code not yet resolved", False
        found.append(
            OutOfSetScript(
                scan_id=script.scan_id,
                source_name=script.source_name,
                recognised_id=script.effective_candidate_id,
                reason=reason,
                exact_only=exact,
            )
        )
    return tuple(found)


def batch_scripts(
    database: ProjectDatabase, batch_id: str, *, set_code: str | None = None
) -> tuple[ScriptRecord, ...]:
    """Return a batch's scripts, carrying their post-review candidate IDs.

    Args:
        database: The open project database.
        batch_id: The batch.
        set_code: Keep only scripts whose **effective** set code is this set's
            (see :class:`ScriptSetPlacement`). ``None`` returns every script -
            what an unscoped roster reconciles against.

    The identifier comes from
    :func:`omr_scanner.services.review_store.effective_identifiers`, never from
    ``BatchScan.identifier_value`` directly - that column holds what the machine
    read, and reconciling against it would ignore every correction a reviewer
    made on the Resolve stage.

    **Lifecycle first.** A superseded original, a re-import of rejected
    content and a sheet rejected / excluded on the Attendance stage are never
    returned. A scan rejected pending a rescan is returned with
    :attr:`~omr_scanner.domain.reconciliation.ScriptRecord.rejected` set - so
    its candidate can be told *rescan required* - carrying the case's
    identity (the operator's declared Student ID, else its effective one); a
    deferred scan likewise with
    :attr:`~omr_scanner.domain.reconciliation.ScriptRecord.deferred`. Neither
    ever counts. See :mod:`omr_scanner.services.scan_lifecycle`.
    """
    readings = _batch_readings(database, batch_id)
    identifiers = readings.identifiers
    cases = scan_lifecycle.cases_by_scan(database, batch_id, live=False)
    keep: set[int] | None = None
    if set_code is not None:
        keep = {
            scan_id
            for scan_id, (placement, _code) in _placements(
                database, batch_id, set_code
            ).items()
            if placement in (ScriptSetPlacement.IN_SET, ScriptSetPlacement.REJECTED)
            or (placement is ScriptSetPlacement.DEFERRED and _code == set_code)
        }
    with database.session() as session:
        rows = session.scalars(
            select(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .order_by(BatchScan.batch_index)
        ).all()
        # Replacements read into other batches stand in for this batch's
        # originals, after its own scans; this batch's scans that stand in
        # for another's originals are counted there, not here.
        if readings.adopted:
            rows = [
                *rows,
                *session.scalars(
                    select(BatchScan)
                    .where(BatchScan.scan_id.in_(list(readings.adopted)))
                    .order_by(BatchScan.scan_id)
                ).all(),
            ]
        scripts = []
        for row in rows:
            if row.scan_id in readings.elsewhere:
                continue
            if keep is not None and row.scan_id not in keep:
                continue
            found = identifiers.get(row.scan_id)
            case = cases.get(row.scan_id)
            if case is not None and case.state not in (
                LifecycleState.REJECTED_PENDING_RESCAN,
                LifecycleState.DEFERRED,
            ):
                continue
            machine = found.machine_value if found else (row.identifier_value or "")
            if case is not None:
                identity = _case_identity(case, found)
                deferred = case.state is LifecycleState.DEFERRED
                scripts.append(
                    ScriptRecord(
                        scan_id=row.scan_id,
                        source_name=row.filename or "",
                        machine_candidate_id=machine,
                        effective_candidate_id=identity,
                        identifier_unresolved=not identity,
                        corrected_by_human=bool(found and found.was_corrected),
                        rejected=not deferred,
                        deferred=deferred,
                    )
                )
                continue
            scripts.append(
                ScriptRecord(
                    scan_id=row.scan_id,
                    source_name=row.filename or "",
                    machine_candidate_id=machine,
                    effective_candidate_id=(
                        found.value if found else (row.identifier_value or "")
                    ),
                    identifier_unresolved=bool(found and found.unresolved),
                    corrected_by_human=bool(found and found.was_corrected),
                )
            )
        return tuple(scripts)


def _set_unresolved_ids(
    database: ProjectDatabase, batch_id: str, set_code: str
) -> frozenset[str]:
    """Student IDs of active scripts left out of a set because their set is unsettled.

    What lets a candidate read *Script found - set unresolved* rather than
    *Missing script*. Only fully read IDs take part - an ID with an unread
    position identifies nobody.
    """
    placements = _placements(database, batch_id, set_code)
    unsettled = {
        scan_id
        for scan_id, (placement, _code) in placements.items()
        if placement in (ScriptSetPlacement.UNRESOLVED, ScriptSetPlacement.UNDEFINED)
    }
    if not unsettled:
        return frozenset()
    found: set[str] = set()
    for scan_id, item in _batch_readings(database, batch_id).identifiers.items():
        value = item.value
        if (
            scan_id in unsettled
            and not item.unresolved
            and value
            and "?" not in value
            and "_" not in value
        ):
            found.add(value)
    return frozenset(found)


# ----------------------------------------------------------------------
# Decisions - the input reconciliation honours
# ----------------------------------------------------------------------
def _decisions(
    session: Session, roster_id: int, batch_id: str
) -> tuple[dict[int, ScriptDecision], dict[str, CandidateDecision]]:
    """Load every standing decision for one roster/batch pair."""
    rows = session.scalars(
        select(ReconciliationDecision)
        .where(ReconciliationDecision.roster_id == roster_id)
        .where(ReconciliationDecision.batch_id == batch_id)
    ).all()
    scripts: dict[int, ScriptDecision] = {}
    candidates: dict[str, CandidateDecision] = {}
    for row in rows:
        if row.target_kind == TARGET_SCRIPT:
            scripts[int(row.target_key)] = ScriptDecision(
                assigned_candidate_id=row.assigned_candidate_id,
                excluded=row.excluded,
                primary=row.is_primary,
                reason_code=row.reason_code,
                reason_text=row.reason_text,
                reviewer=row.reviewer,
            )
        else:
            candidates[row.target_key] = CandidateDecision(
                attendance_override=AttendanceState(row.attendance_override),
                dismissed=row.dismissed,
                reason_code=row.reason_code,
                reason_text=row.reason_text,
                reviewer=row.reviewer,
            )
    return scripts, candidates


def _upsert_decision(
    session: Session,
    *,
    roster_id: int,
    batch_id: str,
    target_kind: str,
    target_key: str,
) -> ReconciliationDecision:
    """Return the decision row for one target, creating it if new."""
    row = session.scalars(
        select(ReconciliationDecision)
        .where(ReconciliationDecision.roster_id == roster_id)
        .where(ReconciliationDecision.batch_id == batch_id)
        .where(ReconciliationDecision.target_kind == target_kind)
        .where(ReconciliationDecision.target_key == target_key)
        .limit(1)
    ).first()
    if row is not None:
        row.updated_at = _now()
        return row
    moment = _now()
    row = ReconciliationDecision(
        roster_id=roster_id,
        batch_id=batch_id,
        target_kind=target_kind,
        target_key=target_key,
        decided_at=moment,
        updated_at=moment,
    )
    session.add(row)
    session.flush()
    return row


# ----------------------------------------------------------------------
# Running reconciliation
# ----------------------------------------------------------------------
def reconcile_batch(
    database: ProjectDatabase, roster_id: int, batch_id: str
) -> ReconciliationCounts:
    """Reconcile a batch against a roster, and store the result.

    Args:
        database: The open project database.
        roster_id: The candidate list to match against.
        batch_id: The batch of scripts.

    Returns:
        The summary counts.

    **Idempotent.** The entry and script tables for this roster/batch pair are
    rewritten wholesale, so running it twice with unchanged inputs leaves
    exactly the state one run leaves - no second set of exceptions, and no
    classification left behind from a roster that is no longer active.
    Operator decisions are *not* touched: they are the input, not the output.
    """
    candidates = roster_candidates(database, roster_id)
    # A set's roster is reconciled against that set's scripts only. A script
    # of another set is outside this universe - not an unknown candidate - and
    # one whose set code is unsettled waits on the Resolve stage.
    set_code = _set_code_of_roster(database, roster_id)
    scripts = batch_scripts(database, batch_id, set_code=set_code)

    with database.session() as session:
        script_decisions, candidate_decisions = _decisions(session, roster_id, batch_id)

    entries = reconcile(
        ReconciliationInput(
            candidates=candidates,
            scripts=scripts,
            script_decisions=script_decisions,
            candidate_decisions=candidate_decisions,
            set_unresolved_ids=(
                _set_unresolved_ids(database, batch_id, set_code)
                if set_code is not None
                else frozenset()
            ),
        )
    )
    # A rejected script is listed under its candidate, but it is not one of
    # the scripts this reconciliation counts.
    counts = count_entries(
        entries,
        scripts=sum(1 for item in scripts if not item.rejected and not item.deferred),
    )

    moment = _now()
    with database.session() as session:
        # Delete-then-insert rather than a merge: a candidate who was an
        # exception and no longer is must not leave a row behind, and matching
        # up two sets of rows by hand is how that happens.
        session.execute(
            delete(ReconciliationScript)
            .where(ReconciliationScript.roster_id == roster_id)
            .where(ReconciliationScript.batch_id == batch_id)
        )
        session.execute(
            delete(ReconciliationEntryRow)
            .where(ReconciliationEntryRow.roster_id == roster_id)
            .where(ReconciliationEntryRow.batch_id == batch_id)
        )
        session.flush()

        row_ids = {
            row.candidate_id: row.candidate_row_id
            for row in session.scalars(
                select(RegisteredCandidate).where(
                    RegisteredCandidate.roster_id == roster_id
                )
            ).all()
        }

        for order, entry in enumerate(entries):
            stored = ReconciliationEntryRow(
                roster_id=roster_id,
                batch_id=batch_id,
                entry_key=_entry_key(entry, order),
                entry_order=order,
                candidate_row_id=row_ids.get(entry.candidate_id)
                if entry.is_registered
                else None,
                candidate_id=entry.candidate_id,
                display_name=entry.display_name,
                is_registered=entry.is_registered,
                status=entry.status.value,
                issues=",".join(sorted(item.value for item in entry.issues)),
                script_count=entry.script_count,
                excluded_count=sum(1 for item in entry.scripts if item.excluded),
                imported_attendance=(
                    entry.candidate.imported_attendance.value
                    if entry.candidate
                    else AttendanceState.UNKNOWN.value
                ),
                effective_attendance=entry.effective_attendance.value,
                attendance_source=entry.attendance_source.value,
                resolution=entry.resolution.value,
                reason_code=entry.reason_code,
                reason_text=entry.reason_text,
                reviewer=entry.reviewer,
                updated_at=moment,
            )
            session.add(stored)
            session.flush()
            for view in entry.scripts:
                session.add(
                    ReconciliationScript(
                        roster_id=roster_id,
                        batch_id=batch_id,
                        entry_id=stored.entry_id,
                        scan_id=view.script.scan_id,
                        source_name=view.script.source_name,
                        machine_candidate_id=view.script.machine_candidate_id,
                        effective_candidate_id=view.script.effective_candidate_id,
                        assigned_candidate_id=(
                            entry.candidate_id
                            if view.assignment is ScriptAssignment.HUMAN
                            else ""
                        ),
                        assignment=view.assignment.value,
                        identifier_unresolved=view.script.identifier_unresolved,
                        excluded=view.excluded,
                        is_primary=view.primary,
                        reason_code=view.reason_code,
                        reason_text=view.reason_text,
                        reviewer=view.reviewer,
                        updated_at=moment,
                    )
                )

        run = session.scalars(
            select(ReconciliationRun)
            .where(ReconciliationRun.roster_id == roster_id)
            .where(ReconciliationRun.batch_id == batch_id)
            .limit(1)
        ).first()
        payload = json.dumps(_counts_payload(counts))
        if run is None:
            session.add(
                ReconciliationRun(
                    roster_id=roster_id,
                    batch_id=batch_id,
                    created_at=moment,
                    updated_at=moment,
                    counts=payload,
                )
            )
        else:
            run.updated_at = moment
            run.counts = payload

    _LOGGER.info(
        "Reconciled: roster=%d entries=%d scripts=%d matched=%d absent_confirmed=%d "
        "unknown_id=%d duplicate=%d present_without=%d absent_with=%d unresolved=%d",
        roster_id,
        len(entries),
        counts.scripts,
        counts.matched,
        counts.absent_confirmed,
        counts.unknown_id,
        counts.duplicate_script,
        counts.present_without_script,
        counts.absent_with_script,
        counts.unresolved_candidate_id,
    )
    return counts


def _entry_key(entry: ReconciliationEntry, order: int) -> str:
    """A stable storage key for one entry.

    A registered candidate is keyed by their ID. An entry that exists only
    because a script arrived is keyed by its first scan id, so two scripts read
    as the same unknown number share one entry while two genuinely separate
    problems stay separate.
    """
    if entry.is_registered:
        return f"c:{entry.candidate_id}"
    if entry.candidate_id:
        return f"u:{entry.candidate_id}"
    return f"s:{entry.scripts[0].script.scan_id}" if entry.scripts else f"x:{order}"


def _counts_payload(counts: ReconciliationCounts) -> dict[str, Any]:
    """Serialise counts for storage, without the derived properties."""
    return {
        "registered": counts.registered,
        "expected_present": counts.expected_present,
        "expected_absent": counts.expected_absent,
        "attendance_unknown": counts.attendance_unknown,
        "scripts": counts.scripts,
        "scripts_excluded": counts.scripts_excluded,
        "matched": counts.matched,
        "absent_confirmed": counts.absent_confirmed,
        "unknown_id": counts.unknown_id,
        "duplicate_script": counts.duplicate_script,
        "present_without_script": counts.present_without_script,
        "absent_with_script": counts.absent_with_script,
        "unresolved_candidate_id": counts.unresolved_candidate_id,
        "rescan_required": counts.rescan_required,
        "script_set_unresolved": counts.script_set_unresolved,
        "script_deferred": counts.script_deferred,
        "resolved": counts.resolved,
        "dismissed": counts.dismissed,
        "deferred": counts.deferred,
        "outstanding_count": counts.outstanding_count,
    }


def stored_counts(
    database: ProjectDatabase, roster_id: int, batch_id: str
) -> ReconciliationCounts | None:
    """Return the counts recorded by the last reconciliation, or ``None``."""
    with database.session() as session:
        run = session.scalars(
            select(ReconciliationRun)
            .where(ReconciliationRun.roster_id == roster_id)
            .where(ReconciliationRun.batch_id == batch_id)
            .limit(1)
        ).first()
        if run is None or not run.counts:
            return None
        payload = json.loads(run.counts)
    return ReconciliationCounts(**payload)


def last_reconciled_at(
    database: ProjectDatabase, roster_id: int, batch_id: str
) -> datetime | None:
    """When this roster/batch pair was last reconciled."""
    with database.session() as session:
        return session.scalars(
            select(ReconciliationRun.updated_at)
            .where(ReconciliationRun.roster_id == roster_id)
            .where(ReconciliationRun.batch_id == batch_id)
            .limit(1)
        ).first()


# ----------------------------------------------------------------------
# Reading the reconciliation back
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EntryFilter:
    """What subset of a reconciliation the table should show."""

    statuses: tuple[ReconciliationStatus, ...] = ()
    resolutions: tuple[ResolutionState, ...] = ()
    exceptions_only: bool = False
    search: str = ""


def list_entries(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    *,
    filters: EntryFilter | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[ReconciliationEntry, ...]:
    """Return stored reconciliation entries, filtered and ordered in SQL.

    Paged in the database rather than in Python for the same reason Phase 6's
    conflict queue is: a ten-thousand-candidate cohort must cost a page, not a
    walk.
    """
    rules = filters or EntryFilter()
    with database.session() as session:
        statement = (
            select(ReconciliationEntryRow)
            .where(ReconciliationEntryRow.roster_id == roster_id)
            .where(ReconciliationEntryRow.batch_id == batch_id)
        )
        statement = _apply_filters(statement, rules)
        statement = statement.order_by(ReconciliationEntryRow.entry_order)
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        rows = session.scalars(statement).all()
        if not rows:
            return ()

        entry_ids = [row.entry_id for row in rows]
        links = session.scalars(
            select(ReconciliationScript)
            .where(ReconciliationScript.entry_id.in_(entry_ids))
            .order_by(ReconciliationScript.scan_id)
        ).all()
        by_entry: dict[int, list[ReconciliationScript]] = {}
        for link in links:
            by_entry.setdefault(link.entry_id, []).append(link)

        candidates = {
            row.candidate_row_id: row
            for row in session.scalars(
                select(RegisteredCandidate).where(
                    RegisteredCandidate.roster_id == roster_id
                )
            ).all()
        }
        ineligible = _ineligible_among(session, [link.scan_id for link in links])
        return tuple(
            _to_entry(row, by_entry.get(row.entry_id, ()), candidates, ineligible)
            for row in rows
        )


def _ineligible_among(
    session: Session, scan_ids: Sequence[int]
) -> dict[int, LifecycleState]:
    """Which of these scans are not active **now**, and in which state.

    Read at the moment an entry is read rather than stored with it, so that a
    reconciliation computed before a scan was rejected, excluded or deferred -
    a cache - can never hand scoring or a report such a script as though it
    counted.
    """
    if not scan_ids:
        return {}
    found: dict[int, LifecycleState] = {}
    for scan_id, state in session.execute(
        select(ScanRejection.scan_id, ScanRejection.state)
        .where(ScanRejection.scan_id.in_(list(set(scan_ids))))
        .where(ScanRejection.state != LifecycleState.ACTIVE.value)
    ).all():
        try:
            found[int(scan_id)] = LifecycleState(state)
        except ValueError:  # pragma: no cover - a state from a newer build
            found[int(scan_id)] = LifecycleState.REJECTED_PENDING_RESCAN
    return found


def _apply_filters(statement: Any, rules: EntryFilter) -> Any:
    """Narrow an entry query by the interface's current filters."""
    if rules.statuses:
        statement = statement.where(
            ReconciliationEntryRow.status.in_([item.value for item in rules.statuses])
        )
    elif rules.exceptions_only:
        statement = statement.where(
            ReconciliationEntryRow.status.notin_(
                [
                    ReconciliationStatus.MATCHED.value,
                    ReconciliationStatus.ABSENT_CONFIRMED.value,
                ]
            )
        )
    if rules.resolutions:
        statement = statement.where(
            ReconciliationEntryRow.resolution.in_(
                [item.value for item in rules.resolutions]
            )
        )
    text = rules.search.strip()
    if text:
        pattern = f"%{text}%"
        # A recognised ID is searchable too: an operator holding a sheet that
        # reads "1805017" needs to find it whichever candidate it is filed
        # under - and under a correction, whether they type what the machine
        # read or what it reads as now.
        recognised = (
            select(ReconciliationScript.entry_id)
            .where(
                ReconciliationScript.machine_candidate_id.ilike(pattern)
                | ReconciliationScript.effective_candidate_id.ilike(pattern)
            )
            .scalar_subquery()
        )
        statement = statement.where(
            ReconciliationEntryRow.candidate_id.ilike(pattern)
            | ReconciliationEntryRow.display_name.ilike(pattern)
            | ReconciliationEntryRow.entry_id.in_(recognised)
        )
    return statement


def count_entries_stored(
    database: ProjectDatabase, roster_id: int, batch_id: str
) -> dict[ReconciliationStatus, int]:
    """Return how many entries hold each status, as one grouped query."""
    with database.session() as session:
        rows = session.execute(
            select(ReconciliationEntryRow.status, func.count())
            .where(ReconciliationEntryRow.roster_id == roster_id)
            .where(ReconciliationEntryRow.batch_id == batch_id)
            .group_by(ReconciliationEntryRow.status)
        ).all()
    found: dict[ReconciliationStatus, int] = {}
    for value, total in rows:
        try:
            found[ReconciliationStatus(value)] = int(total)
        except ValueError:  # pragma: no cover - a status this build cannot read
            continue
    return found


def get_entry(
    database: ProjectDatabase, entry_id: int
) -> ReconciliationEntry | None:
    """Return one stored entry with its scripts."""
    with database.session() as session:
        row = session.get(ReconciliationEntryRow, entry_id)
        if row is None:
            return None
        links = session.scalars(
            select(ReconciliationScript)
            .where(ReconciliationScript.entry_id == entry_id)
            .order_by(ReconciliationScript.scan_id)
        ).all()
        candidates = {
            item.candidate_row_id: item
            for item in session.scalars(
                select(RegisteredCandidate).where(
                    RegisteredCandidate.roster_id == row.roster_id
                )
            ).all()
        }
        ineligible = _ineligible_among(session, [link.scan_id for link in links])
        return _to_entry(row, links, candidates, ineligible)


def _to_entry(
    row: ReconciliationEntryRow,
    links: Sequence[ReconciliationScript],
    candidates: dict[int, RegisteredCandidate],
    ineligible: dict[int, LifecycleState] | None = None,
) -> ReconciliationEntry:
    """Convert stored rows into a detached domain entry."""
    candidate = None
    source = candidates.get(row.candidate_row_id) if row.candidate_row_id else None
    if source is not None:
        candidate = CandidateRecord(
            candidate_id=source.candidate_id,
            display_name=source.display_name,
            source_row=source.source_row,
            imported_attendance=AttendanceState(source.imported_attendance),
            imported_value=source.imported_value,
        )
    issues = frozenset(
        ReconciliationIssue(item) for item in row.issues.split(",") if item
    )
    return ReconciliationEntry(
        candidate_id=row.candidate_id,
        candidate=candidate,
        scripts=tuple(
            _to_script_view(link, state=(ineligible or {}).get(link.scan_id))
            for link in links
        ),
        issues=issues,
        status=ReconciliationStatus(row.status),
        effective_attendance=AttendanceState(row.effective_attendance),
        attendance_source=AttendanceSource(row.attendance_source),
        resolution=ResolutionState(row.resolution),
        reason_code=row.reason_code,
        reason_text=row.reason_text,
        reviewer=row.reviewer,
    )


def _to_script_view(
    link: ReconciliationScript, *, state: LifecycleState | None = None
) -> ScriptView:
    """Convert one stored script link into a detached view.

    ``state`` is the scan's current lifecycle state when it is not active: a
    deferred scan is flagged deferred, any other ineligible one rejected -
    either way it does not count.
    """
    deferred = state is LifecycleState.DEFERRED
    return ScriptView(
        script=ScriptRecord(
            scan_id=link.scan_id,
            source_name=link.source_name,
            machine_candidate_id=link.machine_candidate_id,
            effective_candidate_id=link.effective_candidate_id,
            identifier_unresolved=link.identifier_unresolved,
            corrected_by_human=(
                link.effective_candidate_id != link.machine_candidate_id
            ),
            rejected=state is not None and not deferred,
            deferred=deferred,
        ),
        assignment=ScriptAssignment(link.assignment),
        excluded=link.excluded,
        primary=link.is_primary,
        reason_code=link.reason_code,
        reason_text=link.reason_text,
        reviewer=link.reviewer,
    )


def entry_id_for(
    database: ProjectDatabase, roster_id: int, batch_id: str, candidate_id: str
) -> int | None:
    """Return the stored entry id filed under a candidate ID."""
    with database.session() as session:
        return session.scalars(
            select(ReconciliationEntryRow.entry_id)
            .where(ReconciliationEntryRow.roster_id == roster_id)
            .where(ReconciliationEntryRow.batch_id == batch_id)
            .where(ReconciliationEntryRow.candidate_id == candidate_id)
            .limit(1)
        ).first()


# ----------------------------------------------------------------------
# Human resolution
# ----------------------------------------------------------------------
# Each action below does exactly three things, in one transaction:
#
#   1. records the operator's standing decision,
#   2. appends one audit event describing it,
#   3. re-runs reconciliation so every consequence becomes visible.
#
# Step 3 is what makes a cascading exception impossible to hide: assigning a
# script to a candidate who already has one produces a duplicate, and the
# operator sees it immediately rather than discovering it at export time.


def _script_context(
    database: ProjectDatabase, roster_id: int, batch_id: str, scan_id: int
) -> tuple[str, str]:
    """Return one script's (machine ID, currently effective ID)."""
    with database.session() as session:
        link = session.scalars(
            select(ReconciliationScript)
            .where(ReconciliationScript.roster_id == roster_id)
            .where(ReconciliationScript.batch_id == batch_id)
            .where(ReconciliationScript.scan_id == scan_id)
            .limit(1)
        ).first()
        if link is None:
            raise ReconciliationError(
                f"Script {scan_id} is not part of this reconciliation",
                user_message=(
                    "That script is not part of the reconciled batch. Run "
                    "reconciliation again and try once more."
                ),
            )
        return link.machine_candidate_id, (
            link.assigned_candidate_id or link.effective_candidate_id
        )


def assign_script(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    scan_id: int,
    *,
    candidate_id: str,
    operator: str,
    reason: ReconciliationReason,
    reason_text: str = "",
) -> ReconciliationCounts:
    """Attribute one script to a registered candidate by hand.

    Args:
        database: The open project database.
        roster_id: The active candidate list.
        batch_id: The batch being reconciled.
        scan_id: The script to attribute.
        candidate_id: The candidate the script really belongs to. Must be on
            the active roster - assigning a script to somebody who is not
            registered would recreate the unknown-ID problem under a new name.
        operator: Who decided. Required.
        reason: Why, as a closed-list code.
        reason_text: The operator's own words; required when ``reason`` is
            ``OTHER``.

    Returns:
        The counts after re-reconciling, so a caller can show the consequences
        at once.

    **The machine's reading is untouched.** ``machine_candidate_id`` keeps what
    recognition read, the assignment is recorded beside it, and the audit event
    carries both.
    """
    named = validate_operator(operator)
    explanation = validate_reason(reason, reason_text)
    target = candidate_id.strip()
    if not target:
        raise ReconciliationError(
            "No candidate chosen",
            user_message="Choose the candidate this script belongs to.",
        )
    if not candidate_exists(database, roster_id, target):
        raise ReconciliationError(
            "Assignment target is not on the roster",
            user_message=(
                "That candidate is not on the imported candidate list. Check "
                "the ID, or import the correct list first."
            ),
        )

    machine_value, previous = _script_context(database, roster_id, batch_id, scan_id)
    with database.session() as session:
        decision = _upsert_decision(
            session,
            roster_id=roster_id,
            batch_id=batch_id,
            target_kind=TARGET_SCRIPT,
            target_key=str(scan_id),
        )
        decision.assigned_candidate_id = target
        decision.reason_code = reason.value
        decision.reason_text = explanation
        decision.reviewer = named
        _append_event(
            session,
            entity_type=ENTITY_SCRIPT,
            entity_id=str(scan_id),
            action=ReconciliationAction.ASSIGNED,
            batch_id=batch_id,
            scan_id=scan_id,
            reviewer=named,
            previous_value=previous,
            new_value=target,
            machine_value=machine_value,
            reason_code=reason.value,
            reason_text=explanation,
        )
    _LOGGER.info("Script assigned by operator: scan=%d roster=%d", scan_id, roster_id)
    return reconcile_batch(database, roster_id, batch_id)


def clear_script_assignment(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    scan_id: int,
    *,
    operator: str,
) -> ReconciliationCounts:
    """Withdraw a manual assignment, returning the script to recognition's answer.

    The withdrawn assignment stays in the ledger under the name of whoever made
    it. Undoing a decision is itself a decision.
    """
    named = validate_operator(operator)
    machine_value, previous = _script_context(database, roster_id, batch_id, scan_id)
    with database.session() as session:
        decision = _upsert_decision(
            session,
            roster_id=roster_id,
            batch_id=batch_id,
            target_kind=TARGET_SCRIPT,
            target_key=str(scan_id),
        )
        decision.assigned_candidate_id = ""
        decision.reviewer = named
        _append_event(
            session,
            entity_type=ENTITY_SCRIPT,
            entity_id=str(scan_id),
            action=ReconciliationAction.UNASSIGNED,
            batch_id=batch_id,
            scan_id=scan_id,
            reviewer=named,
            previous_value=previous,
            new_value=machine_value,
            machine_value=machine_value,
        )
    return reconcile_batch(database, roster_id, batch_id)


def set_script_excluded(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    scan_id: int,
    *,
    excluded: bool,
    operator: str,
    reason: ReconciliationReason = ReconciliationReason.ACCIDENTAL_RESCAN,
    reason_text: str = "",
) -> ReconciliationCounts:
    """Set a script aside as an accidental re-scan, or bring it back.

    **Nothing is deleted.** The scan row, its recognition result, its overlay
    and its audit trail all remain; the script simply stops counting towards
    its candidate and is shown as set aside. That is the difference between
    resolving a duplicate and destroying evidence.
    """
    named = validate_operator(operator)
    explanation = validate_reason(reason, reason_text) if excluded else ""
    machine_value, current = _script_context(database, roster_id, batch_id, scan_id)
    with database.session() as session:
        decision = _upsert_decision(
            session,
            roster_id=roster_id,
            batch_id=batch_id,
            target_kind=TARGET_SCRIPT,
            target_key=str(scan_id),
        )
        decision.excluded = excluded
        if excluded:
            decision.reason_code = reason.value
            decision.reason_text = explanation
        decision.reviewer = named
        _append_event(
            session,
            entity_type=ENTITY_SCRIPT,
            entity_id=str(scan_id),
            action=(
                ReconciliationAction.EXCLUDED
                if excluded
                else ReconciliationAction.INCLUDED
            ),
            batch_id=batch_id,
            scan_id=scan_id,
            reviewer=named,
            previous_value=current,
            new_value=current,
            machine_value=machine_value,
            reason_code=reason.value if excluded else "",
            reason_text=explanation,
            detail="set aside from downstream processing" if excluded else "",
        )
    _LOGGER.info(
        "Script exclusion changed: scan=%d roster=%d excluded=%s",
        scan_id,
        roster_id,
        excluded,
    )
    return reconcile_batch(database, roster_id, batch_id)


def set_primary_script(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    scan_id: int,
    *,
    operator: str,
) -> ReconciliationCounts:
    """Nominate which of a candidate's scripts is the working one.

    The others are neither deleted nor excluded - they remain attributed to the
    candidate and remain visible. This records a preference for a later phase
    to honour, not a judgement that the rest are invalid.
    """
    named = validate_operator(operator)
    machine_value, current = _script_context(database, roster_id, batch_id, scan_id)
    with database.session() as session:
        link = session.scalars(
            select(ReconciliationScript)
            .where(ReconciliationScript.roster_id == roster_id)
            .where(ReconciliationScript.batch_id == batch_id)
            .where(ReconciliationScript.scan_id == scan_id)
            .limit(1)
        ).first()
        siblings: list[int] = []
        if link is not None:
            siblings = [
                row.scan_id
                for row in session.scalars(
                    select(ReconciliationScript)
                    .where(ReconciliationScript.entry_id == link.entry_id)
                    .where(ReconciliationScript.scan_id != scan_id)
                ).all()
            ]
        # Exactly one primary per candidate, so nominating a second clears the
        # first rather than leaving two rows both claiming to be the working one.
        for other in siblings:
            existing = _upsert_decision(
                session,
                roster_id=roster_id,
                batch_id=batch_id,
                target_kind=TARGET_SCRIPT,
                target_key=str(other),
            )
            existing.is_primary = False

        decision = _upsert_decision(
            session,
            roster_id=roster_id,
            batch_id=batch_id,
            target_kind=TARGET_SCRIPT,
            target_key=str(scan_id),
        )
        decision.is_primary = True
        decision.reviewer = named
        _append_event(
            session,
            entity_type=ENTITY_SCRIPT,
            entity_id=str(scan_id),
            action=ReconciliationAction.MARKED_PRIMARY,
            batch_id=batch_id,
            scan_id=scan_id,
            reviewer=named,
            previous_value=current,
            new_value=current,
            machine_value=machine_value,
            detail="nominated as the working script",
        )
    return reconcile_batch(database, roster_id, batch_id)


def override_attendance(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    candidate_id: str,
    *,
    attendance: AttendanceState,
    operator: str,
    reason: ReconciliationReason,
    reason_text: str = "",
) -> ReconciliationCounts:
    """Record that a candidate's real attendance differs from the roster's.

    **The imported value is never touched.** ``RegisteredCandidate.imported_attendance``
    still says what the file said; this records what an operator established,
    and the entry then reports both with
    :attr:`~omr_scanner.domain.reconciliation.ReconciliationEntry.attendance_was_overridden`
    set.

    Passing :attr:`~omr_scanner.domain.reconciliation.AttendanceState.UNKNOWN`
    withdraws a previous override and restores the roster's own value.
    """
    named = validate_operator(operator)
    explanation = validate_reason(reason, reason_text)
    with database.session() as session:
        candidate = session.scalars(
            select(RegisteredCandidate)
            .where(RegisteredCandidate.roster_id == roster_id)
            .where(RegisteredCandidate.candidate_id == candidate_id)
            .limit(1)
        ).first()
        if candidate is None:
            raise ReconciliationError(
                "Attendance override target is not on the roster",
                user_message=(
                    "That candidate is not on the imported candidate list, so "
                    "their attendance cannot be overridden."
                ),
            )
        imported = candidate.imported_attendance

        decision = _upsert_decision(
            session,
            roster_id=roster_id,
            batch_id=batch_id,
            target_kind=TARGET_CANDIDATE,
            target_key=candidate_id,
        )
        previous = decision.attendance_override
        decision.attendance_override = attendance.value
        decision.reason_code = reason.value
        decision.reason_text = explanation
        decision.reviewer = named
        _append_event(
            session,
            entity_type=ENTITY_CANDIDATE,
            entity_id=candidate_id,
            action=ReconciliationAction.ATTENDANCE_OVERRIDDEN,
            batch_id=batch_id,
            reviewer=named,
            previous_value=previous if previous != "unknown" else imported,
            new_value=attendance.value,
            machine_value=imported,
            reason_code=reason.value,
            reason_text=explanation,
            detail="imported value retained",
        )
    _LOGGER.info(
        "Attendance overridden by operator: roster=%d new_state=%s",
        roster_id,
        attendance.value,
    )
    return reconcile_batch(database, roster_id, batch_id)


def dismiss_entry(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    candidate_id: str,
    *,
    operator: str,
    reason: ReconciliationReason,
    reason_text: str = "",
) -> ReconciliationCounts:
    """Accept an exception as-is, without changing anything about it.

    For the cases with no action to take: a script genuinely missing, a roster
    error nobody can correct today. The exception stops counting as outstanding
    work but keeps its classification, so it is still visible under the
    *Accepted as-is* filter rather than looking resolved.
    """
    named = validate_operator(operator)
    explanation = validate_reason(reason, reason_text)
    with database.session() as session:
        decision = _upsert_decision(
            session,
            roster_id=roster_id,
            batch_id=batch_id,
            target_kind=TARGET_CANDIDATE,
            target_key=candidate_id,
        )
        decision.dismissed = True
        decision.reason_code = reason.value
        decision.reason_text = explanation
        decision.reviewer = named
        _append_event(
            session,
            entity_type=ENTITY_CANDIDATE,
            entity_id=candidate_id,
            action=ReconciliationAction.DISMISSED,
            batch_id=batch_id,
            reviewer=named,
            reason_code=reason.value,
            reason_text=explanation,
        )
    return reconcile_batch(database, roster_id, batch_id)


def reopen_entry(
    database: ProjectDatabase,
    roster_id: int,
    batch_id: str,
    candidate_id: str,
    *,
    operator: str,
) -> ReconciliationCounts:
    """Put a dismissed exception back on the outstanding list.

    The dismissal stays in the ledger under the name of whoever made it.
    """
    named = validate_operator(operator)
    with database.session() as session:
        decision = _upsert_decision(
            session,
            roster_id=roster_id,
            batch_id=batch_id,
            target_kind=TARGET_CANDIDATE,
            target_key=candidate_id,
        )
        decision.dismissed = False
        decision.reviewer = named
        _append_event(
            session,
            entity_type=ENTITY_CANDIDATE,
            entity_id=candidate_id,
            action=ReconciliationAction.REOPENED,
            batch_id=batch_id,
            reviewer=named,
        )
    return reconcile_batch(database, roster_id, batch_id)


# ----------------------------------------------------------------------
# History
# ----------------------------------------------------------------------
def history_for(
    database: ProjectDatabase, entity_type: str, entity_id: str
) -> tuple[ReconciliationAuditRecord, ...]:
    """Return every recorded decision about one candidate or script, oldest first."""
    with database.session() as session:
        rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.entity_type == entity_type)
            .where(AuditEvent.entity_id == entity_id)
            .order_by(AuditEvent.event_id)
        ).all()
        return tuple(_to_audit(row) for row in rows)


def history_for_entry(
    database: ProjectDatabase, entry: ReconciliationEntry
) -> tuple[ReconciliationAuditRecord, ...]:
    """Return the decisions about one entry: the candidate's and every script's.

    Merged and ordered by when they happened, because an operator reading the
    history of a duplicate wants one story, not one per sheet.
    """
    records: list[ReconciliationAuditRecord] = []
    if entry.is_registered:
        records.extend(history_for(database, ENTITY_CANDIDATE, entry.candidate_id))
    for view in entry.scripts:
        records.extend(
            history_for(database, ENTITY_SCRIPT, str(view.script.scan_id))
        )
    return tuple(sorted(records, key=lambda item: item.event_id))


def _to_audit(row: AuditEvent) -> ReconciliationAuditRecord:
    """Convert a ledger row into a detached record."""
    try:
        action = ReconciliationAction(row.action)
    except ValueError:  # pragma: no cover - a Phase 6 action on a shared row
        action = ReconciliationAction.RECONCILED
    return ReconciliationAuditRecord(
        event_id=row.event_id,
        occurred_at=row.occurred_at,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        scan_id=row.scan_id,
        action=action,
        reviewer=row.reviewer,
        previous_value=row.previous_value,
        new_value=row.new_value,
        machine_value=row.machine_value,
        reason_code=row.reason_code,
        reason_text=row.reason_text,
        detail=row.detail,
    )
