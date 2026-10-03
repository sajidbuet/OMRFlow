"""A large, realistic *metadata* population for snapshot measurements (revised phase 7).

Writes rows only - no image, no recognition - straight into a real project
database through the real models: finite sealed units per source in one open
session, their sheets in every processing state, review conflicts, quality
decisions (some suggested rescans), rejections with confirmed replacements,
and the intake ledger behind them (registered rows plus files still
stabilising, ready or held). The proportions are illustrative, not a claim
about a real examination.

Used by ``tests/integration/test_snapshot_scale.py`` and
``scripts/benchmark_session_snapshot.py``.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import insert, select

from omr_scanner.database.models import (
    BatchScan,
    IntakeFile,
    ReviewConflict,
    ScanBatch,
    ScanQualityDecision,
    ScanRejection,
)
from omr_scanner.domain.intake import IntakeState
from omr_scanner.services import intake, scan_sessions


@dataclass(frozen=True, slots=True)
class Population:
    """What was written."""

    scan_session_id: str
    sheets: int
    units: int
    sources: int
    unregistered: int


def _sha(*parts: object) -> str:
    return hashlib.sha256(":".join(str(part) for part in parts).encode()).hexdigest()


def build(
    database: Any,
    *,
    sheets: int,
    sources: int = 3,
    unit_size: int = 200,
    now: datetime | None = None,
) -> Population:
    """Write ``sheets`` registered sheets (plus ~2 % unregistered files) into a new session."""
    moment = now or datetime.now(UTC)
    session_id = scan_sessions.create_scan_session(
        database, name="Snapshot scale", created_by="bench"
    ).scan_session_id
    source_ids = []
    for index in range(sources):
        info = intake.create_source(
            database, label=f"Scanner {index}", root_path=rf"\\bench-{index}\scans",
            created_by="bench",
        )
        intake.attach_source(database, info.source_id, session_id, actor="bench")
        source_ids.append(info.source_id)

    batches: list[dict[str, Any]] = []
    scans: list[dict[str, Any]] = []
    per_source = max(1, sheets // sources)
    for source_index, source_id in enumerate(source_ids):
        remaining = per_source if source_index < sources - 1 else sheets - per_source * (
            sources - 1
        )
        unit = 0
        while remaining > 0:
            size = min(unit_size, remaining)
            batch_id = uuid.uuid4().hex
            created = moment - timedelta(minutes=600 - len(batches))
            batches.append({
                "batch_id": batch_id, "created_at": created, "updated_at": created,
                "source_folder": "", "template_id": "bench", "template_name": "bench",
                "template_path": "", "geometry_fingerprint": "g", "recognition_fingerprint": "r",
                "engine_version": "bench", "settings_json": "{}", "status": "completed",
                "total_scans": size, "scan_session_id": session_id, "sealed_at": created,
                "sealed_by": "bench", "role": "scan", "source_id": source_id,
            })
            for index in range(size):
                serial = len(scans)
                kind = serial % 100
                status = (
                    "pending" if kind < 2 else "failed" if kind < 4 else
                    "warning" if kind < 20 else "completed"
                )
                read = status in ("completed", "warning", "failed")
                scans.append({
                    "batch_id": batch_id, "batch_index": index,
                    "source_path": rf"\\bench-{source_index}\scans\{serial:07d}.png",
                    "filename": f"{serial:07d}.png", "status": status,
                    "attempt_count": 1 if read else 0,
                    "outcome": ("registration_failed" if status == "failed" else
                             "review" if status == "warning" else
                             "complete" if read else ""),
                    "identifier_value": f"{17_000_000 + serial % (sheets - sheets // 50):08d}",
                    "content_sha256": _sha("bench", serial),
                    "finished_at": moment - timedelta(seconds=serial % 900) if read else None,
                })
            remaining -= size
            unit += 1

    with database.session() as session:
        session.execute(insert(ScanBatch), batches)
        session.execute(insert(BatchScan), scans)
        rows = session.execute(
            select(BatchScan.scan_id, BatchScan.batch_id, BatchScan.status, ScanBatch.source_id)
            .join(ScanBatch, ScanBatch.batch_id == BatchScan.batch_id)
            .where(ScanBatch.scan_session_id == session_id)
            .order_by(BatchScan.scan_id)
        ).all()
        conflicts: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        ledger: list[dict[str, Any]] = []
        read_ids = [row for row in rows if row[2] in ("completed", "warning", "failed")]
        for position, (scan_id, batch_id, status, source_id) in enumerate(rows):
            ledger.append({
                "source_id": source_id, "scan_session_id": session_id,
                "relative_path": f"{scan_id:07d}.png", "absolute_path": "", "file_name": "",
                "file_size": 1, "mtime_ns": 1, "content_sha256": _sha("bench", scan_id - 1),
                "state": IntakeState.REGISTERED.value, "state_reason": "", "detail": "",
                "state_changed_at": moment, "first_seen_at": moment, "last_seen_at": moment,
                "observations": 2, "attempts": 0, "present": True, "is_current": True,
                "path_reused": False, "reverify_required": False, "batch_scan_id": scan_id,
                "registered_at": moment,
            })
            if status not in ("completed", "warning", "failed"):
                continue
            if position % 10 == 0:
                conflicts.append({
                    "batch_id": batch_id, "scan_id": scan_id,
                    "conflict_type": "identifier_ambiguous",
                    "state": "resolved" if position % 20 == 0 else "open",
                    "zone_id": "roll_number", "group_key": 0,
                    "created_at": moment, "updated_at": moment,
                })
            suggested = status == "failed"
            decisions.append({
                "scan_id": scan_id, "batch_id": batch_id,
                "decision": "rescan_required" if suggested else "accept",
                "reasons": "registration_failed" if suggested else "",
                "suggested_reason": "registration" if suggested else "",
                "policy_version": 1, "policy_fingerprint": "bench", "evaluated_at": moment,
            })
        for index in range(0, min(len(read_ids), 400), 8):
            original, replacement = read_ids[index], read_ids[index + 1]
            rejections.append({
                "scan_id": original[0], "batch_id": original[1],
                "state": (
                    "superseded_by_replacement" if index % 16 == 0
                    else "rejected_pending_rescan"
                ),
                "reason_code": "folded", "rejected_at": moment, "updated_at": moment,
                "replacement_scan_id": replacement[0] if index % 16 == 0 else None,
            })
        unregistered = max(1, sheets // 50)
        states = (IntakeState.STABILIZING, IntakeState.READY, IntakeState.HELD,
                  IntakeState.UNREADABLE)
        for index in range(unregistered):
            ledger.append({
                "source_id": source_ids[index % sources], "scan_session_id": session_id,
                "relative_path": f"new/{index:06d}.png", "absolute_path": "", "file_name": "",
                "file_size": 1, "mtime_ns": 1, "content_sha256": None,
                "state": states[index % len(states)].value, "state_reason": "", "detail": "",
                "state_changed_at": moment, "first_seen_at": moment, "last_seen_at": moment,
                "observations": 1, "attempts": 0, "present": True, "is_current": True,
                "path_reused": False, "reverify_required": False, "batch_scan_id": None,
            })
        session.execute(insert(ReviewConflict), conflicts)
        session.execute(insert(ScanQualityDecision), decisions)
        if rejections:
            session.execute(insert(ScanRejection), rejections)
        session.execute(insert(IntakeFile), ledger)
    return Population(
        scan_session_id=session_id,
        sheets=sheets,
        units=len(batches),
        sources=sources,
        unregistered=unregistered,
    )


__all__ = ["Population", "build"]
