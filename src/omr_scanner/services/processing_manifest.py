"""Processing manifests: what a batch was run with, at its lifecycle boundaries.

Purpose:
    Write :class:`~omr_scanner.database.models.ProcessingManifest` rows - the
    reproducibility snapshot Phase 10 designed (migration 7) and that nothing
    wrote until 0.1.1 phase 2 (defect 6 of ``0.1.0-alpha.2``).

When a manifest is written:
    * ``run_finished`` - by :func:`omr_scanner.services.batch_store.finalise_batch`,
      at the end of every processing run (including a resume or a retry);
    * ``sealed`` - by :func:`omr_scanner.services.scan_sessions.seal_batch`,
      when the batch's membership becomes final.

    One row per boundary, never rewritten: a later manifest for the same batch
    is a new row, so "what produced this?" still has the answer it had then.

What a manifest holds:
    Assembled from tables that already exist rather than duplicating them: the
    batch's template identity and fingerprints, engine version, settings,
    membership and processing counts, its scan session and role, and the
    answer-key and scoring-policy revisions in force at that moment (by id and
    revision only). No candidate data.

What does NOT belong here:
    Qt; deciding *when* a boundary happens (the callers do).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from omr_scanner import __version__
from omr_scanner.database.models import (
    AnswerKeyRevision,
    BatchScan,
    ProcessingManifest,
    ScanBatch,
    ScoringPolicyRevision,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase

_LOGGER = logging.getLogger(__name__)

MANIFEST_FORMAT = 1
"""Version of :func:`build_payload`'s shape."""


def build_payload(session: Session, batch_id: str, *, trigger: str) -> dict[str, Any] | None:
    """The manifest content for one batch at this moment, or ``None`` if it is gone."""
    from omr_scanner.database.migrations import SCHEMA_VERSION

    batch = session.get(ScanBatch, batch_id)
    if batch is None:
        return None
    lifecycle = session.execute(
        select(ScanBatch.scan_session_id, ScanBatch.role, ScanBatch.sealed_at).where(
            ScanBatch.batch_id == batch_id
        )
    ).one()
    counts = {
        str(status): int(count)
        for status, count in session.execute(
            select(BatchScan.status, func.count())
            .where(BatchScan.batch_id == batch_id)
            .group_by(BatchScan.status)
        ).all()
    }
    keys = [
        {"set_code": code, "revision": int(revision), "key_id": int(key_id)}
        for key_id, code, revision in session.execute(
            select(AnswerKeyRevision.key_id, AnswerKeyRevision.set_code, AnswerKeyRevision.revision)
            .where(AnswerKeyRevision.status == "verified")
            .order_by(AnswerKeyRevision.set_code, AnswerKeyRevision.revision)
        ).all()
    ]
    policy = session.execute(
        select(ScoringPolicyRevision.policy_id, ScoringPolicyRevision.revision)
        .where(ScoringPolicyRevision.is_active.is_(True))
        .limit(1)
    ).first()
    return {
        "manifest_format": MANIFEST_FORMAT,
        "trigger": trigger,
        "schema_version": SCHEMA_VERSION,
        "batch": {
            "batch_id": batch.batch_id,
            "scan_session_id": lifecycle.scan_session_id,
            "role": lifecycle.role,
            "sealed_at": lifecycle.sealed_at.isoformat() if lifecycle.sealed_at else None,
            "status": batch.status,
            "total_scans": batch.total_scans,
            "status_counts": counts,
            "source_folder": batch.source_folder,
            "created_at": batch.created_at.isoformat(),
        },
        "template": {
            "template_id": batch.template_id,
            "template_name": batch.template_name,
            "geometry_fingerprint": batch.geometry_fingerprint,
            "recognition_fingerprint": batch.recognition_fingerprint,
        },
        "engine_version": batch.engine_version,
        "settings": json.loads(batch.settings_json or "{}"),
        "verified_answer_keys": keys,
        "scoring_policy": (
            {"policy_id": int(policy.policy_id), "revision": int(policy.revision)}
            if policy is not None
            else None
        ),
    }


def add_manifest(session: Session, batch_id: str, *, trigger: str) -> ProcessingManifest | None:
    """Add a manifest row inside an open transaction (the caller's)."""
    from omr_scanner.database.migrations import SCHEMA_VERSION

    payload = build_payload(session, batch_id, trigger=trigger)
    if payload is None:
        return None
    row = ProcessingManifest(
        batch_id=batch_id,
        generated_at=datetime.now(UTC),
        application_version=__version__,
        schema_version=SCHEMA_VERSION,
        payload_json=json.dumps(payload, sort_keys=True),
    )
    session.add(row)
    session.flush()
    return row


def record(database: ProjectDatabase, batch_id: str, *, trigger: str) -> bool:
    """Write one manifest for ``batch_id`` in its own transaction.

    Returns whether a row was written. Never fatal to the caller's work: a
    manifest that cannot be written is logged, because losing the run over its
    receipt would be the wrong trade.
    """
    if database.read_only:
        return False
    try:
        with database.session() as session:
            written = add_manifest(session, batch_id, trigger=trigger) is not None
    except Exception:  # pragma: no cover - logged, deliberately not fatal
        _LOGGER.exception("Processing manifest for batch %s could not be written", batch_id)
        return False
    if written:
        _LOGGER.info("Processing manifest written for batch %s (%s)", batch_id, trigger)
    return written


def manifests_for(database: ProjectDatabase, batch_id: str) -> tuple[dict[str, Any], ...]:
    """Every manifest payload for a batch, oldest first."""
    with database.session() as session:
        rows = session.scalars(
            select(ProcessingManifest)
            .where(ProcessingManifest.batch_id == batch_id)
            .order_by(ProcessingManifest.generated_at, ProcessingManifest.manifest_id)
        ).all()
        return tuple(json.loads(row.payload_json) for row in rows)


__all__ = ["MANIFEST_FORMAT", "add_manifest", "build_payload", "manifests_for", "record"]
