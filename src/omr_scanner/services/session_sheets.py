"""A scan session's sheets, one page at a time, filtered and sorted in SQL (0.1.1 revised phase 8).

Purpose:
    The operational Scan stage lists every sheet registered into a scan
    session - tens of thousands in a continuous session - with the provenance
    an operator needs (scanner source, batch, the **original** file name) and
    the state each sheet is in (read / pending, quality decision, conflict,
    rescan). This module answers that list one bounded page at a time: every
    filter, the search and the sort run in SQL, and a page never holds more
    rows than it shows. The GUI's paged table model renders what it returns.

    It also names a session's batches and sources for filter menus, and one
    sheet's provenance for the Resolve workspace.

What does NOT belong here:
    * Classifying sheets into the effective set or the snapshot's partition -
      that is :mod:`omr_scanner.services.session_population` and
      :mod:`omr_scanner.services.session_snapshot`. This is a **listing**: it
      shows the stored facts of each row and decides nothing.
    * Qt.

Original file names:
    A watched source's sheet is read from a content-addressed copy inside the
    project (ADR-0008), so ``batch_scan.filename`` is that copy's name. The
    operator's own name for the file is the intake ledger's
    ``intake_file.file_name``; it is shown first, and the stored copy's name
    only as diagnostic detail.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import exists, func, literal, or_, select
from sqlalchemy.orm import aliased

from omr_scanner.database.models import (
    BatchScan,
    BatchSupersession,
    IntakeFile,
    IntakeSource,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
    ScanQualityDecision,
    ScanRejection,
)
from omr_scanner.domain.quality_decision import QualityDecision
from omr_scanner.domain.review import RESOLUTION_TYPES, ConflictState
from omr_scanner.domain.scan_lifecycle import LifecycleState
from omr_scanner.services import intake as intake_service
from omr_scanner.services import quality_decisions

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase

PAGE_SIZE = 200
"""Rows per page in the operational list. Enough to fill a tall table twice."""

_RESOLUTION_VALUES = tuple(item.value for item in RESOLUTION_TYPES)
_UNRESOLVED = (ConflictState.OPEN.value, ConflictState.DEFERRED.value)


class StatusFilter(StrEnum):
    """Which processing statuses to list."""

    ALL = "all"
    NOT_READ = "not_read"
    """Pending, cancelled, queued or being read."""
    PROCESSING = "processing"
    """Queued for or inside a worker right now."""
    READ = "read"
    """Completed cleanly."""
    REVIEW = "review"
    """Read, with something for a human (``warning``)."""
    FAILED = "failed"
    DUPLICATE = "duplicate"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return _STATUS_FILTER_LABELS[self]


_STATUS_FILTER_LABELS = {
    StatusFilter.ALL: "All statuses",
    StatusFilter.NOT_READ: "Not read yet",
    StatusFilter.PROCESSING: "Being read",
    StatusFilter.READ: "Read",
    StatusFilter.REVIEW: "Needs review",
    StatusFilter.FAILED: "Failed",
    StatusFilter.DUPLICATE: "Duplicate image",
}

_STATUSES: dict[StatusFilter, tuple[str, ...]] = {
    StatusFilter.NOT_READ: (
        ScanJobStatus.PENDING.value,
        ScanJobStatus.CANCELLED.value,
        ScanJobStatus.QUEUED.value,
        ScanJobStatus.PROCESSING.value,
    ),
    StatusFilter.PROCESSING: (ScanJobStatus.QUEUED.value, ScanJobStatus.PROCESSING.value),
    StatusFilter.READ: (ScanJobStatus.COMPLETED.value,),
    StatusFilter.REVIEW: (ScanJobStatus.WARNING.value,),
    StatusFilter.FAILED: (ScanJobStatus.FAILED.value,),
    StatusFilter.DUPLICATE: (ScanJobStatus.DUPLICATE.value,),
}


class QualityFilter(StrEnum):
    """Which scan-quality decisions to list (revised phase 7's decision layer)."""

    ALL = "all"
    SUGGESTED = "suggested"
    """An unanswered suggested rescan (the one definition: ``outstanding_clause``)."""
    RESCAN_REQUIRED = "rescan_required"
    """Decided *rescan required*, answered or not."""
    WARNING = "warning"
    RETRY = "retry"
    ACCEPT = "accept"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return _QUALITY_FILTER_LABELS[self]


_QUALITY_FILTER_LABELS = {
    QualityFilter.ALL: "Any quality",
    QualityFilter.SUGGESTED: "Suggested rescan (unanswered)",
    QualityFilter.RESCAN_REQUIRED: "Rescan required (any)",
    QualityFilter.WARNING: "Accepted with warning",
    QualityFilter.RETRY: "Read again (software)",
    QualityFilter.ACCEPT: "Accepted",
}

_QUALITY_DECISION: dict[QualityFilter, str] = {
    QualityFilter.RESCAN_REQUIRED: QualityDecision.RESCAN_REQUIRED.value,
    QualityFilter.WARNING: QualityDecision.ACCEPT_WITH_WARNING.value,
    QualityFilter.RETRY: QualityDecision.RETRY_PROCESSING.value,
    QualityFilter.ACCEPT: QualityDecision.ACCEPT.value,
}


class ConflictStateFilter(StrEnum):
    """Whether a sheet still has a required conflict for Resolve."""

    ALL = "all"
    UNRESOLVED = "unresolved"
    NONE = "none"

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            ConflictStateFilter.ALL: "Any conflicts",
            ConflictStateFilter.UNRESOLVED: "Unresolved conflict",
            ConflictStateFilter.NONE: "No unresolved conflict",
        }[self]


class RescanFilter(StrEnum):
    """Where a sheet stands in Reject & Rescan."""

    ALL = "all"
    REJECTED = "rejected"
    """Rejected, awaiting its rescan."""
    REPLACED = "replaced"
    """Rejected and replaced by a confirmed rescan."""
    REPLACEMENT = "replacement"
    """Confirmed as another sheet's rescan."""
    ACTIVE = "active"
    """Not rejected (or the rejection was undone)."""

    @property
    def label(self) -> str:
        """Operator-facing wording."""
        return {
            RescanFilter.ALL: "Any rescan state",
            RescanFilter.REJECTED: "Rejected - awaiting rescan",
            RescanFilter.REPLACED: "Replaced by a rescan",
            RescanFilter.REPLACEMENT: "Is a confirmed rescan",
            RescanFilter.ACTIVE: "Not rejected",
        }[self]


class SheetSort(StrEnum):
    """Orders the list can be sorted by (every one ends with the sheet id)."""

    ARRIVAL = "arrival"
    STATUS = "status"
    STUDENT_ID = "student_id"
    SET_CODE = "set_code"
    SOURCE = "source"
    BATCH = "batch"
    FILE_NAME = "file_name"
    QUALITY = "quality"


@dataclass(frozen=True, slots=True)
class SheetQuery:
    """What the operator asked the list to show. Every field is applied in SQL.

    Attributes:
        status / quality / conflict / rescan: The filters.
        source_id: Only sheets that arrived through this intake source.
        batch_ids: Only sheets of these batches (empty: every batch).
        search: A Student ID or (original or stored) file name fragment.
        sort / descending: The order; ties broken by the sheet id.
    """

    status: StatusFilter = StatusFilter.ALL
    quality: QualityFilter = QualityFilter.ALL
    conflict: ConflictStateFilter = ConflictStateFilter.ALL
    rescan: RescanFilter = RescanFilter.ALL
    source_id: str = ""
    batch_ids: tuple[str, ...] = ()
    search: str = ""
    sort: SheetSort = SheetSort.ARRIVAL
    descending: bool = True


@dataclass(frozen=True, slots=True)
class BatchOption:
    """One batch of a session, as a filter menu and a list row name it.

    Attributes:
        batch_id: The batch.
        position: 1-based, in creation order within the session.
        label: ``Batch 3 · Scanner A`` (``· superseded`` when it is).
        source_id / source_label: The intake source it came from, if any.
        role: ``scan``, ``rescan``, ``reprocess`` or ``legacy``.
        superseded: A live batch supersession replaces it.
        sheets: Rows it holds.
        created_at: When it was registered.
    """

    batch_id: str
    position: int
    label: str
    source_id: str | None
    source_label: str
    role: str
    superseded: bool
    sheets: int
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class SourceOption:
    """An intake source that contributed to the session (or is attached to it)."""

    source_id: str
    label: str
    kind: str


@dataclass(frozen=True, slots=True)
class SheetRow:
    """One sheet as the operational list shows it. Detached; stored facts only.

    Attributes:
        scan_id / batch_id: Identity (internal; never the operator's label).
        batch_label: ``Batch 3 · Scanner A``.
        source_id / source_label: The scanner source, when known.
        original_name: The operator's own file name (the intake ledger's), or
            the stored name when the sheet did not arrive through intake.
        stored_name: ``batch_scan.filename`` - for a watched source, the
            project's content-addressed copy. Diagnostic detail.
        source_path: The file recognition reads.
        status / outcome: Processing status and recognition outcome.
        student_id / set_code: As read (``batch_scan``'s stored columns).
        quality: The scan-quality decision, or ``""`` when not decided.
        suggestion_outstanding: An unanswered suggested rescan.
        unresolved_conflicts: Required conflicts still open or deferred.
        lifecycle: :class:`~omr_scanner.domain.scan_lifecycle.LifecycleState`
            value (``active`` when no rejection was ever recorded).
        is_replacement: Confirmed as another sheet's rescan.
        arrived_at: When intake registered it (or the file was first seen).
        read_at: When its result was committed.
    """

    scan_id: int
    batch_id: str
    batch_label: str
    source_id: str | None
    source_label: str
    original_name: str
    stored_name: str
    source_path: str
    status: str
    outcome: str
    student_id: str
    set_code: str
    quality: str
    suggestion_outstanding: bool
    unresolved_conflicts: int
    lifecycle: str
    is_replacement: bool
    arrived_at: datetime | None
    read_at: datetime | None


@dataclass(frozen=True, slots=True)
class SheetProvenance:
    """Where one sheet came from, in an operator's terms (the Resolve workspace).

    Attributes:
        scan_id: The sheet.
        source_label: Its scanner source (``""`` when it did not come through one).
        batch_label: ``Batch 3 · Scanner A``.
        batch_id: Internal; for a diagnostic tooltip only.
        original_name / stored_name: As :class:`SheetRow`.
        arrived_at / read_at: As :class:`SheetRow`.
        student_id / set_code: As read (``batch_scan``'s stored columns -
            not the effective value after review).
    """

    scan_id: int
    source_label: str
    batch_label: str
    batch_id: str
    original_name: str
    stored_name: str
    arrived_at: datetime | None
    read_at: datetime | None
    student_id: str = ""
    set_code: str = ""

    def describe(self) -> str:
        """One line: ``Scanner A · Batch 3 · original.png · arrived 10:42``."""
        # A session batch's label already names its scanner; say it once.
        batch = self.batch_label.replace(f" · {self.source_label}", "", 1)
        parts = [part for part in (self.source_label, batch) if part]
        parts.append(self.original_name or self.stored_name)
        if self.arrived_at is not None:
            parts.append(f"arrived {self.arrived_at.astimezone():%H:%M}")
        return " · ".join(parts)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


# ----------------------------------------------------------------------
# Batches and sources
# ----------------------------------------------------------------------
def session_batches(database: ProjectDatabase, scan_session_id: str) -> tuple[BatchOption, ...]:
    """The session's batches, oldest first, with their operator labels. Two grouped reads."""
    with database.session() as session:
        rows = session.execute(
            select(
                ScanBatch.batch_id,
                ScanBatch.source_id,
                ScanBatch.role,
                ScanBatch.created_at,
                ScanBatch.total_scans,
            )
            .where(ScanBatch.scan_session_id == scan_session_id)
            .order_by(ScanBatch.created_at, ScanBatch.batch_id)
        ).all()
        if not rows:
            return ()
        ids = [str(row[0]) for row in rows]
        superseded = {
            str(item)
            for item in session.scalars(
                select(BatchSupersession.superseded_batch_id)
                .where(BatchSupersession.reversed_at.is_(None))
                .where(BatchSupersession.superseded_batch_id.in_(ids))
            ).all()
        }
        labels = {
            str(source_id): str(label)
            for source_id, label in session.execute(
                select(IntakeSource.source_id, IntakeSource.label)
            ).all()
        } if intake_service.has_intake_schema(database) else {}
    found: list[BatchOption] = []
    for position, (batch_id, source_id, role, created_at, total) in enumerate(rows, start=1):
        source_label = labels.get(str(source_id), "") if source_id else ""
        label = f"Batch {position}"
        if source_label:
            label += f" · {source_label}"
        if str(role) not in ("scan", "legacy"):
            label += f" · {role}"
        if str(batch_id) in superseded:
            label += " · superseded"
        found.append(
            BatchOption(
                batch_id=str(batch_id),
                position=position,
                label=label,
                source_id=str(source_id) if source_id else None,
                source_label=source_label,
                role=str(role),
                superseded=str(batch_id) in superseded,
                sheets=int(total or 0),
                created_at=_aware(created_at),
            )
        )
    return tuple(found)


def session_sources(database: ProjectDatabase, scan_session_id: str) -> tuple[SourceOption, ...]:
    """Sources attached to the session or that contributed a batch to it, oldest first."""
    if not intake_service.has_intake_schema(database):
        return ()
    contributed = {
        item.source_id for item in session_batches(database, scan_session_id) if item.source_id
    }
    return tuple(
        SourceOption(source_id=item.source_id, label=item.label, kind=item.kind.value)
        for item in intake_service.list_sources(database)
        if item.attached_session_id == scan_session_id or item.source_id in contributed
    )


def batches_of_source(
    database: ProjectDatabase, scan_session_id: str, source_id: str
) -> tuple[str, ...]:
    """The session's batch ids that came from one source - a filter, never a scope."""
    return tuple(
        item.batch_id
        for item in session_batches(database, scan_session_id)
        if item.source_id == source_id
    )


# ----------------------------------------------------------------------
# The paged list
# ----------------------------------------------------------------------
def _unresolved_exists() -> Any:
    return (
        select(ReviewConflict.conflict_id)
        .where(ReviewConflict.scan_id == BatchScan.scan_id)
        .where(ReviewConflict.conflict_type.in_(_RESOLUTION_VALUES))
        .where(ReviewConflict.state.in_(_UNRESOLVED))
        .exists()
    )


def _unresolved_count() -> Any:
    return (
        select(func.count())
        .select_from(ReviewConflict)
        .where(ReviewConflict.scan_id == BatchScan.scan_id)
        .where(ReviewConflict.conflict_type.in_(_RESOLUTION_VALUES))
        .where(ReviewConflict.state.in_(_UNRESOLVED))
        .scalar_subquery()
    )


def _is_replacement() -> Any:
    # Aliased: the list already outer-joins the sheet's *own* rejection row,
    # and an unaliased reference here would correlate with that one.
    replaced = aliased(ScanRejection)
    return exists().where(replaced.replacement_scan_id == BatchScan.scan_id)


def _original_name(with_intake: bool) -> Any:
    if not with_intake:
        return BatchScan.filename
    return func.coalesce(func.nullif(IntakeFile.file_name, ""), BatchScan.filename)


def _needs(query: SheetQuery, *, with_intake: bool, sorting: bool) -> frozenset[str]:
    """The tables a query must join - only those, so a plain page reads ``batch_scan`` alone."""
    needs: set[str] = set()
    if query.source_id:
        needs.add("batch")
    if query.rescan in (RescanFilter.REJECTED, RescanFilter.REPLACED, RescanFilter.ACTIVE):
        needs.add("rejection")
    if query.quality not in (QualityFilter.ALL, QualityFilter.SUGGESTED):
        needs.add("quality")
    if query.search.strip() and with_intake:
        needs.add("intake")
    if sorting:
        if query.sort is SheetSort.SOURCE:
            needs |= {"batch", "source"}
        elif query.sort is SheetSort.BATCH:
            needs.add("batch")
        elif query.sort is SheetSort.FILE_NAME and with_intake:
            needs.add("intake")
        elif query.sort is SheetSort.QUALITY:
            needs.add("quality")
    return frozenset(needs)


def _join(
    statement: Any, needs: frozenset[str], *, with_intake: bool, with_quality: bool
) -> Any:
    """Add exactly the joins ``needs`` names (every one on a key or unique column)."""
    if "batch" in needs or "source" in needs:
        statement = statement.join(ScanBatch, ScanBatch.batch_id == BatchScan.batch_id)
    if "rejection" in needs:
        statement = statement.outerjoin(ScanRejection, ScanRejection.scan_id == BatchScan.scan_id)
    if "intake" in needs and with_intake:
        statement = statement.outerjoin(
            IntakeFile, IntakeFile.intake_file_id == BatchScan.intake_file_id
        )
    if "source" in needs and with_intake:
        statement = statement.outerjoin(IntakeSource, IntakeSource.source_id == ScanBatch.source_id)
    if "quality" in needs and with_quality:
        statement = statement.outerjoin(
            ScanQualityDecision, ScanQualityDecision.scan_id == BatchScan.scan_id
        )
    return statement


def _apply_filters(
    statement: Any, query: SheetQuery, batches: list[str], *, with_intake: bool,
    with_quality: bool,
) -> Any:
    """``statement`` (FROM ``batch_scan`` with :func:`_join`'s joins) narrowed by ``query``."""
    wanted = [item for item in batches if not query.batch_ids or item in query.batch_ids]
    statement = statement.where(BatchScan.batch_id.in_(wanted))
    if query.status is not StatusFilter.ALL:
        statement = statement.where(BatchScan.status.in_(_STATUSES[query.status]))
    if query.source_id:
        statement = statement.where(ScanBatch.source_id == query.source_id)
    if query.conflict is ConflictStateFilter.UNRESOLVED:
        statement = statement.where(_unresolved_exists())
    elif query.conflict is ConflictStateFilter.NONE:
        statement = statement.where(~_unresolved_exists())
    if query.rescan is RescanFilter.REJECTED:
        statement = statement.where(
            ScanRejection.state == LifecycleState.REJECTED_PENDING_RESCAN.value
        )
    elif query.rescan is RescanFilter.REPLACED:
        statement = statement.where(
            ScanRejection.state == LifecycleState.SUPERSEDED_BY_REPLACEMENT.value
        )
    elif query.rescan is RescanFilter.REPLACEMENT:
        statement = statement.where(_is_replacement())
    elif query.rescan is RescanFilter.ACTIVE:
        statement = statement.where(
            or_(
                ScanRejection.state.is_(None),
                ScanRejection.state == LifecycleState.ACTIVE.value,
            )
        )
    if query.quality is not QualityFilter.ALL:
        if not with_quality:
            statement = statement.where(literal(False))
        elif query.quality is QualityFilter.SUGGESTED:
            statement = statement.where(quality_decisions.outstanding_clause(BatchScan.scan_id))
        else:
            statement = statement.where(
                ScanQualityDecision.decision == _QUALITY_DECISION[query.quality]
            )
    text = query.search.strip()
    if text:
        # The operator's own file name (the stored copy's content-addressed
        # name would match hex fragments nobody typed) or the Student ID.
        pattern = f"%{text}%"
        statement = statement.where(
            or_(
                BatchScan.identifier_value.like(pattern),
                _original_name(with_intake).like(pattern),
            )
        )
    return statement


def _session_batch_ids(database: ProjectDatabase, scan_session_id: str) -> list[str]:
    with database.session() as session:
        return [
            str(item)
            for item in session.scalars(
                select(ScanBatch.batch_id).where(ScanBatch.scan_session_id == scan_session_id)
            ).all()
        ]


def count_sheets(
    database: ProjectDatabase, scan_session_id: str, query: SheetQuery | None = None
) -> int:
    """How many of the session's sheets ``query`` lists. One count statement.

    Joins only what a filter needs: an unfiltered count is an index-only scan
    of ``batch_scan``'s ``(batch_id, status)`` index.
    """
    wanted = query or SheetQuery()
    batches = _session_batch_ids(database, scan_session_id)
    if not batches:
        return 0
    with_intake = intake_service.has_intake_schema(database)
    with_quality = quality_decisions.has_quality_schema(database)
    statement = _join(
        select(func.count()).select_from(BatchScan),
        _needs(wanted, with_intake=with_intake, sorting=False),
        with_intake=with_intake,
        with_quality=with_quality,
    )
    statement = _apply_filters(
        statement, wanted, batches, with_intake=with_intake, with_quality=with_quality
    )
    with database.session() as session:
        return int(session.scalar(statement) or 0)


def _order(query: SheetQuery, *, with_intake: bool, with_quality: bool) -> list[Any]:
    column: Any
    if query.sort is SheetSort.STATUS:
        column = BatchScan.status
    elif query.sort is SheetSort.STUDENT_ID:
        column = BatchScan.identifier_value
    elif query.sort is SheetSort.SET_CODE:
        column = BatchScan.set_code_value
    elif query.sort is SheetSort.SOURCE:
        column = IntakeSource.label if with_intake else ScanBatch.source_id
    elif query.sort is SheetSort.BATCH:
        column = ScanBatch.created_at
    elif query.sort is SheetSort.FILE_NAME:
        column = _original_name(with_intake)
    elif query.sort is SheetSort.QUALITY:
        column = ScanQualityDecision.decision if with_quality else BatchScan.status
    else:
        column = None
    tail = BatchScan.scan_id.desc() if query.descending else BatchScan.scan_id.asc()
    if column is None:
        return [tail]
    return [column.desc() if query.descending else column.asc(), tail]


def list_sheets(
    database: ProjectDatabase,
    scan_session_id: str,
    query: SheetQuery | None = None,
    *,
    offset: int = 0,
    limit: int = PAGE_SIZE,
) -> tuple[SheetRow, ...]:
    """One page of the session's sheets, filtered and sorted in SQL. Bounded by ``limit``.

    Two statements: the page's sheet ids (joining only what the filters and
    the sort need), then those few sheets' details - so the per-row columns
    (conflicts, suggestion, replacement) are computed for one page, never for
    the session.
    """
    wanted = query or SheetQuery()
    batches = session_batches(database, scan_session_id)
    if not batches:
        return ()
    label_of = {item.batch_id: item.label for item in batches}
    with_intake = intake_service.has_intake_schema(database)
    with_quality = quality_decisions.has_quality_schema(database)
    page = _join(
        select(BatchScan.scan_id).select_from(BatchScan),
        _needs(wanted, with_intake=with_intake, sorting=True),
        with_intake=with_intake,
        with_quality=with_quality,
    )
    page = _apply_filters(
        page,
        wanted,
        [item.batch_id for item in batches],
        with_intake=with_intake,
        with_quality=with_quality,
    )
    page = (
        page.order_by(*_order(wanted, with_intake=with_intake, with_quality=with_quality))
        .offset(max(0, offset))
        .limit(max(0, limit))
    )
    with database.session() as session:
        ids = [int(item) for item in session.scalars(page).all()]
        if not ids:
            return ()
        everything = frozenset({"batch", "rejection", "intake", "source", "quality"})
        columns: list[Any] = [
            BatchScan.scan_id,
            BatchScan.batch_id,
            ScanBatch.source_id,
            IntakeSource.label if with_intake else literal(""),
            _original_name(with_intake),
            BatchScan.filename,
            BatchScan.source_path,
            BatchScan.status,
            BatchScan.outcome,
            BatchScan.identifier_value,
            BatchScan.set_code_value,
            ScanQualityDecision.decision if with_quality else literal(""),
            (
                quality_decisions.outstanding_clause(BatchScan.scan_id)
                if with_quality
                else literal(False)
            ),
            _unresolved_count(),
            func.coalesce(ScanRejection.state, literal(LifecycleState.ACTIVE.value)),
            _is_replacement(),
            func.coalesce(BatchScan.registered_at, IntakeFile.first_seen_at)
            if with_intake
            else BatchScan.registered_at,
            BatchScan.finished_at,
        ]
        details = _join(
            select(*columns).select_from(BatchScan),
            everything,
            with_intake=with_intake,
            with_quality=with_quality,
        ).where(BatchScan.scan_id.in_(ids))
        found = {int(row[0]): row for row in session.execute(details).all()}
    return tuple(_row(found[scan_id], label_of) for scan_id in ids if scan_id in found)


def _row(row: Any, label_of: dict[str, str]) -> SheetRow:
    return SheetRow(
        scan_id=int(row[0]),
        batch_id=str(row[1]),
        batch_label=label_of.get(str(row[1]), str(row[1])[:8]),
        source_id=str(row[2]) if row[2] else None,
        source_label=str(row[3] or ""),
        original_name=str(row[4] or ""),
        stored_name=str(row[5] or ""),
        source_path=str(row[6] or ""),
        status=str(row[7] or ""),
        outcome=str(row[8] or ""),
        student_id=str(row[9] or ""),
        set_code=str(row[10] or ""),
        quality=str(row[11] or ""),
        suggestion_outstanding=bool(row[12]),
        unresolved_conflicts=int(row[13] or 0),
        lifecycle=str(row[14] or LifecycleState.ACTIVE.value),
        is_replacement=bool(row[15]),
        arrived_at=_aware(row[16]),
        read_at=_aware(row[17]),
    )


def original_names(
    database: ProjectDatabase, scan_ids: Iterable[int], *, intake_only: bool = False
) -> dict[int, str]:
    """``scan_id -> the operator's original file name`` for the given sheets.

    For a watched source's sheet this is the intake ledger's name, not the
    project copy's content-addressed one; a sheet that did not come through
    intake keeps its stored name - or, with ``intake_only``, is left out.
    Unknown ids are left out. One query per 900 ids (SQLite's bound-parameter
    limit).
    """
    wanted = sorted({int(item) for item in scan_ids})
    if not wanted:
        return {}
    with_intake = intake_service.has_intake_schema(database)
    if intake_only and not with_intake:
        return {}
    statement = select(BatchScan.scan_id, _original_name(with_intake)).select_from(BatchScan)
    if intake_only:
        statement = statement.join(
            IntakeFile, IntakeFile.intake_file_id == BatchScan.intake_file_id
        ).where(IntakeFile.file_name != "")
    elif with_intake:
        statement = statement.outerjoin(
            IntakeFile, IntakeFile.intake_file_id == BatchScan.intake_file_id
        )
    found: dict[int, str] = {}
    with database.session() as session:
        for start in range(0, len(wanted), 900):
            chunk = wanted[start:start + 900]
            for scan_id, name in session.execute(
                statement.where(BatchScan.scan_id.in_(chunk))
            ).all():
                found[int(scan_id)] = str(name or "")
    return found


def with_original_names[Named](
    database: ProjectDatabase, items: Sequence[Named], *fields: tuple[str, str]
) -> list[Named]:
    """``items`` with each watched-source sheet's name shown as it arrived.

    Each ``(id_attribute, name_attribute)`` pair names a scan id and the
    display name beside it on the (frozen dataclass) items; a name is
    replaced only for a sheet that came through intake, so a finite batch's
    rows are returned unchanged. One batched lookup for all the items.
    """
    ids = {
        scan_id
        for item in items
        for id_attribute, _ in fields
        if isinstance(scan_id := getattr(item, id_attribute), int) and scan_id > 0
    }
    names = original_names(database, ids, intake_only=True)
    if not names:
        return list(items)
    shown: list[Named] = []
    for item in items:
        changes = {
            name_attribute: names[scan_id]
            for id_attribute, name_attribute in fields
            if (scan_id := getattr(item, id_attribute)) in names
        }
        shown.append(dataclasses.replace(item, **changes) if changes else item)  # type: ignore[type-var]
    return shown


# ----------------------------------------------------------------------
# One sheet's provenance
# ----------------------------------------------------------------------
def sheet_provenance(database: ProjectDatabase, scan_id: int) -> SheetProvenance | None:
    """Where one sheet came from: source, batch, original file name, arrival. Or ``None``."""
    with_intake = intake_service.has_intake_schema(database)
    with database.session() as session:
        statement = select(
            BatchScan.batch_id,
            BatchScan.filename,
            ScanBatch.scan_session_id,
            IntakeSource.label if with_intake else literal(""),
            _original_name(with_intake),
            func.coalesce(BatchScan.registered_at, IntakeFile.first_seen_at)
            if with_intake
            else BatchScan.registered_at,
            BatchScan.finished_at,
            BatchScan.identifier_value,
            BatchScan.set_code_value,
        ).select_from(BatchScan).join(ScanBatch, ScanBatch.batch_id == BatchScan.batch_id)
        if with_intake:
            statement = statement.outerjoin(
                IntakeFile, IntakeFile.intake_file_id == BatchScan.intake_file_id
            ).outerjoin(IntakeSource, IntakeSource.source_id == ScanBatch.source_id)
        row = session.execute(statement.where(BatchScan.scan_id == scan_id)).first()
    if row is None:
        return None
    batch_id, stored, owner, source_label, original, arrived, read, student, code = row
    label = str(batch_id)[:8]
    if owner:
        for item in session_batches(database, str(owner)):
            if item.batch_id == str(batch_id):
                label = item.label
                break
    return SheetProvenance(
        scan_id=scan_id,
        source_label=str(source_label or ""),
        batch_label=label,
        batch_id=str(batch_id),
        original_name=str(original or ""),
        stored_name=str(stored or ""),
        arrived_at=_aware(arrived),
        read_at=_aware(read),
        student_id=str(student or ""),
        set_code=str(code or ""),
    )


__all__ = [
    "PAGE_SIZE",
    "BatchOption",
    "ConflictStateFilter",
    "QualityFilter",
    "RescanFilter",
    "SheetProvenance",
    "SheetQuery",
    "SheetRow",
    "SheetSort",
    "SourceOption",
    "StatusFilter",
    "batches_of_source",
    "count_sheets",
    "list_sheets",
    "original_names",
    "session_batches",
    "session_sources",
    "sheet_provenance",
    "with_original_names",
]
