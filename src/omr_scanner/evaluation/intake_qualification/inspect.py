"""Read-only facts from a campaign project's database (revised phase 9).

The evaluator's view of what OMRFlow durably holds: every ledger row, sheet,
batch (with a digest of its membership), conflict, audit event, rejection,
quality decision, session and supersession - read in **one** read
transaction so the rows describe one instant - plus, separately, what the
production read services say about the same state (the session snapshot and
the effective population), so that the two can be compared.

Opened read-only, from outside the process under test. A hot rollback journal
left by a kill inside a commit is first rolled back by a plain writable open -
exactly what the application's own next open does (see :func:`settle_journal`).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

READ_STATUSES = ("completed", "warning", "failed")
QUEUED_STATUSES = ("pending", "cancelled", "queued")


def settle_journal(project: Path) -> bool:
    """Roll back a transaction a kill interrupted; return whether there was one.

    A kill inside a commit leaves ``database.sqlite-journal``; SQLite rolls it
    back on the next open that can write, but a read-only connection cannot
    and fails. The committed state *is* the state after that rollback, so the
    supervisor performs it with one plain read on a writable connection and
    records that it did (the crash harness's technique).
    """
    journal = project / "database.sqlite-journal"
    if not journal.is_file() or journal.stat().st_size == 0:
        return False
    connection = sqlite3.connect(project / "database.sqlite", timeout=30)
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
    finally:
        connection.close()
    return True


def open_read_only(project: Path) -> Any:
    """The project database, read-only (after settling a hot journal)."""
    from omr_scanner.database.engine import open_project_database

    settle_journal(project)
    return open_project_database(project / "database.sqlite", read_only=True)


@dataclass(frozen=True, slots=True)
class SheetRow:
    """One batch scan row."""

    scan_id: int
    batch_id: str
    status: str
    sha: str
    intake_file_id: int | None
    attempts: int
    identifier: str
    set_code: str
    filename: str
    source_path: str
    outcome: str


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One intake-ledger file row."""

    intake_file_id: int
    source_id: str
    source_label: str
    session_id: str | None
    relative_path: str
    absolute_path: str
    file_name: str
    size: int
    sha: str
    state: str
    reason: str
    batch_scan_id: int | None
    duplicate_of_scan_id: int | None
    first_seen_at: datetime | None
    ready_at: datetime | None
    registered_at: datetime | None
    is_current: bool


@dataclass(frozen=True, slots=True)
class BatchRow:
    """One scan batch row, with its member scan ids."""

    batch_id: str
    session_id: str | None
    role: str
    status: str
    sealed: bool
    source_id: str | None
    total: int
    members: tuple[int, ...]
    created_at: datetime | None

    @property
    def digest(self) -> str:
        """SHA-256 of the sorted member scan ids - recorded at seal, re-checked later."""
        text = ",".join(str(item) for item in self.members)
        return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ConflictRow:
    """One review conflict row."""

    conflict_id: int
    scan_id: int
    batch_id: str
    kind: str
    state: str
    zone: str
    group: int


@dataclass(frozen=True, slots=True)
class AuditRow:
    """One audit event row."""

    event_id: int
    action: str
    reviewer: str
    scan_id: int | None
    conflict_id: int | None
    entity_type: str
    entity_id: str
    batch_id: str


@dataclass
class Facts:
    """Everything durable, at one instant."""

    session_id: str
    sessions: list[tuple[str, str, int]] = field(default_factory=list)
    sheets: dict[int, SheetRow] = field(default_factory=dict)
    ledger: list[LedgerRow] = field(default_factory=list)
    batches: dict[str, BatchRow] = field(default_factory=dict)
    conflicts: list[ConflictRow] = field(default_factory=list)
    audit: list[AuditRow] = field(default_factory=list)
    rejections: dict[int, tuple[str, int | None]] = field(default_factory=dict)
    quality: dict[int, tuple[str, bool]] = field(default_factory=dict)
    superseded_batches: set[str] = field(default_factory=set)
    supersessions: int = 0
    taken_at: datetime | None = None

    # --- derived -------------------------------------------------------
    def session_batches(self) -> list[BatchRow]:
        """The batches of this session."""
        return [item for item in self.batches.values() if item.session_id == self.session_id]

    def session_sheets(self) -> list[SheetRow]:
        """The sheets of this session's batches."""
        ids = {item.batch_id for item in self.session_batches()}
        return [item for item in self.sheets.values() if item.batch_id in ids]

    def live_sheets(self) -> list[SheetRow]:
        """The session's sheets outside superseded batches."""
        return [item for item in self.session_sheets()
                if item.batch_id not in self.superseded_batches]

    def committed(self) -> list[SheetRow]:
        """The live sheets whose status is a read status."""
        return [item for item in self.live_sheets() if item.status in READ_STATUSES]

    def counts(self) -> dict[str, int]:
        """The session's sheet count per status."""
        return dict(Counter(item.status for item in self.session_sheets()))


def read_facts(database: Any, session_id: str) -> Facts:
    """Every durable fact the evaluator needs, from one read transaction."""
    from sqlalchemy import select

    from omr_scanner.database.models import (
        AuditEvent,
        BatchScan,
        BatchSupersession,
        IntakeFile,
        IntakeSource,
        ReviewConflict,
        ScanBatch,
        ScanQualityDecision,
        ScanRejection,
        ScanSession,
    )

    facts = Facts(session_id=session_id, taken_at=datetime.now(UTC))
    with database.session() as session:
        session.connection().exec_driver_sql("BEGIN")
        facts.sessions = [
            (str(a), str(b), int(c or 0)) for a, b, c in session.execute(
                select(ScanSession.scan_session_id, ScanSession.state, ScanSession.reopen_count)
            ).all()
        ]
        members: dict[str, list[int]] = {}
        for row in session.execute(
            select(BatchScan.scan_id, BatchScan.batch_id, BatchScan.status,
                   BatchScan.content_sha256, BatchScan.intake_file_id, BatchScan.attempt_count,
                   BatchScan.identifier_value, BatchScan.set_code_value, BatchScan.filename,
                   BatchScan.source_path, BatchScan.outcome)
        ).all():
            sheet = SheetRow(
                scan_id=int(row[0]), batch_id=str(row[1]), status=str(row[2]),
                sha=str(row[3] or ""), intake_file_id=int(row[4]) if row[4] is not None else None,
                attempts=int(row[5] or 0), identifier=str(row[6] or ""),
                set_code=str(row[7] or ""), filename=str(row[8] or ""),
                source_path=str(row[9] or ""), outcome=str(row[10] or ""),
            )
            facts.sheets[sheet.scan_id] = sheet
            members.setdefault(sheet.batch_id, []).append(sheet.scan_id)
        for row in session.execute(
            select(ScanBatch.batch_id, ScanBatch.scan_session_id, ScanBatch.role,
                   ScanBatch.status, ScanBatch.sealed_at, ScanBatch.source_id,
                   ScanBatch.total_scans, ScanBatch.created_at)
        ).all():
            batch_id = str(row[0])
            facts.batches[batch_id] = BatchRow(
                batch_id=batch_id, session_id=str(row[1]) if row[1] else None,
                role=str(row[2] or ""), status=str(row[3] or ""), sealed=row[4] is not None,
                source_id=str(row[5]) if row[5] else None, total=int(row[6] or 0),
                members=tuple(sorted(members.get(batch_id, ()))), created_at=row[7],
            )
        labels = {
            str(a): str(b)
            for a, b in session.execute(select(IntakeSource.source_id, IntakeSource.label)).all()
        }
        for item in session.scalars(select(IntakeFile)).all():
            facts.ledger.append(LedgerRow(
                intake_file_id=int(item.intake_file_id), source_id=str(item.source_id),
                source_label=labels.get(str(item.source_id), ""),
                session_id=item.scan_session_id, relative_path=str(item.relative_path),
                absolute_path=str(item.absolute_path), file_name=str(item.file_name),
                size=int(item.file_size or 0), sha=str(item.content_sha256 or ""),
                state=str(item.state), reason=str(item.state_reason or ""),
                batch_scan_id=item.batch_scan_id, duplicate_of_scan_id=item.duplicate_of_scan_id,
                first_seen_at=item.first_seen_at, ready_at=item.ready_at,
                registered_at=item.registered_at, is_current=bool(item.is_current),
            ))
        facts.conflicts = [
            ConflictRow(int(a), int(b), str(c), str(d), str(e), str(f or ""), int(g))
            for a, b, c, d, e, f, g in session.execute(
                select(ReviewConflict.conflict_id, ReviewConflict.scan_id, ReviewConflict.batch_id,
                       ReviewConflict.conflict_type, ReviewConflict.state, ReviewConflict.zone_id,
                       ReviewConflict.group_key)
            ).all()
        ]
        facts.audit = [
            AuditRow(int(a), str(b), str(c or ""), int(d) if d else None,
                     int(e) if e else None, str(f or ""), str(g or ""), str(h or ""))
            for a, b, c, d, e, f, g, h in session.execute(
                select(AuditEvent.event_id, AuditEvent.action, AuditEvent.reviewer,
                       AuditEvent.scan_id, AuditEvent.conflict_id, AuditEvent.entity_type,
                       AuditEvent.entity_id, AuditEvent.batch_id).order_by(AuditEvent.event_id)
            ).all()
        ]
        facts.rejections = {
            int(a): (str(b), int(c) if c is not None else None)
            for a, b, c in session.execute(
                select(ScanRejection.scan_id, ScanRejection.state,
                       ScanRejection.replacement_scan_id)
            ).all()
        }
        facts.quality = {
            int(a): (str(b), c is not None)
            for a, b, c in session.execute(
                select(ScanQualityDecision.scan_id, ScanQualityDecision.decision,
                       ScanQualityDecision.dismissed_at)
            ).all()
        }
        sups = session.execute(
            select(BatchSupersession.superseded_batch_id, BatchSupersession.reversed_at)
        ).all()
        facts.supersessions = len(sups)
        facts.superseded_batches = {str(a) for a, b in sups if b is None}
    return facts


# ----------------------------------------------------------------------
# The independent recount of the session snapshot's partition
# ----------------------------------------------------------------------
_LEDGER = {
    "discovered": "stabilizing", "stabilizing": "stabilizing", "ready": "ready",
    "held": "held", "vanished": "vanished", "unreadable": "unreadable_pending_decision",
    "unsupported": "unreadable_pending_decision", "duplicate_content": "duplicate",
}


def recount_partition(facts: Facts) -> dict[str, int]:
    """Partition buckets recounted row by row from the raw tables.

    The rule of ``ARCHITECTURE_NOTES.md`` §14.1, written out again here rather
    than calling the snapshot: an unregistered ledger row by its state
    (``ignored`` outside the partition); a sheet of a superseded batch is
    *superseded*; a rejected sheet *rescan_required* (awaiting) or
    *superseded* (replaced); an exact-duplicate row *duplicate*; an unread
    sheet *queued* or *processing*; a read sheet *rescan_required* while its
    suggested rescan is unanswered, *conflict* while a required conflict is
    open or deferred, otherwise *accepted*.
    """
    from omr_scanner.domain.review import RESOLUTION_TYPES
    from omr_scanner.services.quality_decisions import EVIDENCE_CONFLICTS

    required = {item.value for item in RESOLUTION_TYPES}
    evidence = {item.value if hasattr(item, "value") else str(item) for item in EVIDENCE_CONFLICTS}
    buckets: Counter[str] = Counter()
    for row in facts.ledger:
        if row.session_id != facts.session_id or row.batch_scan_id is not None:
            continue
        bucket = _LEDGER.get(row.state)
        if bucket is not None:
            buckets[bucket] += 1
    unresolved: Counter[int] = Counter()
    evidence_resolved: set[int] = set()
    for conflict in facts.conflicts:
        if conflict.kind in required and conflict.state in ("open", "deferred"):
            unresolved[conflict.scan_id] += 1
        if conflict.kind in evidence and conflict.state == "resolved":
            evidence_resolved.add(conflict.scan_id)
    for sheet in facts.session_sheets():
        lifecycle = facts.rejections.get(sheet.scan_id, ("active", None))[0]
        if sheet.batch_id in facts.superseded_batches:
            buckets["superseded"] += 1
        elif lifecycle == "rejected_pending_rescan":
            buckets["rescan_required"] += 1
        elif lifecycle == "superseded_by_replacement":
            buckets["superseded"] += 1
        elif lifecycle != "active":
            buckets["duplicate"] += 1  # a re-import of a rejected sheet
        elif sheet.status == "duplicate":
            buckets["duplicate"] += 1
        elif sheet.status == "processing":
            buckets["processing"] += 1
        elif sheet.status not in READ_STATUSES:
            buckets["queued"] += 1
        else:
            decision, dismissed = facts.quality.get(sheet.scan_id, ("accept", False))
            if (decision == "rescan_required" and not dismissed
                    and sheet.scan_id not in evidence_resolved):
                buckets["rescan_required"] += 1
            elif unresolved.get(sheet.scan_id):
                buckets["conflict"] += 1
            else:
                buckets["accepted"] += 1
    return dict(buckets)


__all__ = [
    "READ_STATUSES",
    "AuditRow",
    "BatchRow",
    "ConflictRow",
    "Facts",
    "LedgerRow",
    "SheetRow",
    "open_read_only",
    "read_facts",
    "recount_partition",
    "settle_journal",
]
