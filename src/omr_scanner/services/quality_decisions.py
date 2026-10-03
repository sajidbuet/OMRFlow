"""Scan-quality decisions and suggested rescans: the persistence side (0.1.1 revised phase 7).

Purpose:
    Apply a scan session's **pinned** quality policy
    (:class:`~omr_scanner.domain.quality_decision.QualityPolicy`) to the
    evidence recognition stored about each sheet, keep the decision with the
    fingerprint that produced it, and expose ``RESCAN_REQUIRED`` decisions as
    **suggested** rejections an operator either confirms - through the existing
    :func:`~omr_scanner.services.scan_lifecycle.reject_scan` - or dismisses.
    ``ARCHITECTURE_NOTES.md`` §12; ``docs/scan_quality.md``.

Responsibilities:
    * :func:`evidence_from_result` - one stored :class:`ScanResult` to the pure
      layer's :class:`~omr_scanner.domain.quality_decision.QualityEvidence`.
    * :func:`session_policy` / :func:`pinned_policy` - the session's policy,
      pinned (audited) the first time it is needed.
    * :func:`record_decisions_in_session` - called by
      :func:`~omr_scanner.services.batch_store.record_results` **inside the
      sheet's work-unit transaction**, so a committed sheet always has its
      decision (no separate step, nothing for a kill to split).
    * :func:`evaluate_stored` - derive missing decisions from stored results
      (sheets read before the upgrade, or by a recognition-only tool).
      Idempotent; reads no image.
    * :func:`outstanding_suggestions` / :func:`count_outstanding` - the
      suggestions still awaiting an operator, with the one predicate
      (:func:`outstanding_clause`) every count shares.
    * :func:`confirm_suggestion` / :func:`dismiss_suggestion` - the operator's
      two answers, each one transaction with its audit event.

The rule that makes a suggestion a suggestion:
    A ``scan_quality_decision`` row never changes whether a sheet counts. The
    effective set is decided by :mod:`omr_scanner.services.session_population`
    alone, and nothing here writes ``scan_rejection``. A suggestion stops being
    *outstanding* when an operator has answered it: the sheet was rejected (in
    any way), the suggestion was dismissed, or the operator resolved the
    sheet's own quality-evidence conflict in Resolve (accepting the sheet as it
    is) - each an explicit, named human decision.

What does NOT belong here:
    Qt; measuring evidence (recognition and :mod:`omr_scanner.services.scan_quality`
    do that); thresholds of any kind.
"""

from __future__ import annotations

import json
import logging
import weakref
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, exists, func, not_, select
from sqlalchemy.orm import aliased

from omr_scanner.database.models import (
    AuditEvent,
    BatchScan,
    BatchSupersession,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
    ScanQualityDecision,
    ScanRejection,
    ScanSession,
)
from omr_scanner.domain.quality_decision import (
    DEFAULT_POLICY,
    QualityDecision,
    QualityEvidence,
    QualityPolicy,
    QualityReason,
    QualityVerdict,
)
from omr_scanner.domain.review import ConflictState, ConflictType
from omr_scanner.domain.scan_lifecycle import LifecycleState, RejectionReason
from omr_scanner.domain.scan_quality import ScanQualityStatus
from omr_scanner.errors import OMRScannerError
from omr_scanner.services.recognition_models import (
    RecognitionOutcome,
    RegistrationStatus,
    ScanResult,
    StatusCode,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.scan_lifecycle import RescanCase

_LOGGER = logging.getLogger(__name__)

QUALITY_SCHEMA_VERSION = 17
"""The first schema with ``scan_quality_decision`` and the session controls."""

QUALITY_ENTITY = "scan_quality"
"""``audit_event.entity_type`` for an operator's answer to a suggested rescan
(``entity_id`` = scan id)."""

EVALUATION_WINDOW = 500
"""Stored results decoded per transaction by :func:`evaluate_stored`."""

READ_STATUSES = (
    ScanJobStatus.COMPLETED.value,
    ScanJobStatus.WARNING.value,
    ScanJobStatus.FAILED.value,
)

EVIDENCE_CONFLICTS = (
    ConflictType.REGISTRATION_FAILED.value,
    ConflictType.IMAGE_UNREADABLE.value,
    ConflictType.SCAN_QUALITY.value,
)
"""Sheet-level conflicts that carry the same evidence a quality decision rests on.

Resolving one of them in Resolve is the operator saying "keep this sheet as it
is" - an answer to the suggested rescan, not a second question."""


class QualityAction(StrEnum):
    """``audit_event.action`` values (20 characters at most)."""

    DISMISSED = "quality_dismissed"


class QualityError(OMRScannerError):
    """A quality-suggestion action was refused. Always carries a ``user_message``."""


def _now() -> datetime:
    return datetime.now(UTC)


_HAS_SCHEMA: weakref.WeakKeyDictionary[ProjectDatabase, bool] = weakref.WeakKeyDictionary()


def has_quality_schema(database: ProjectDatabase) -> bool:
    """Whether the database stores quality decisions and controls (schema 17+).

    Cached per open database: a database's schema only changes when it is
    opened (migrations run before the object exists), and the work unit and
    the engine's control check ask on every commit and step.
    """
    known = _HAS_SCHEMA.get(database)
    if known is None:
        known = database.schema_version >= QUALITY_SCHEMA_VERSION
        _HAS_SCHEMA[database] = known
    return known


def stores_decisions(database: ProjectDatabase) -> bool:
    """Whether the work unit should record decisions here: writable and schema 17+."""
    return not database.read_only and has_quality_schema(database)


# ----------------------------------------------------------------------
# Evidence
# ----------------------------------------------------------------------
def evidence_from_result(result: ScanResult) -> QualityEvidence:
    """Reduce one stored recognition result to the facts the policy decides on.

    The distinctions that matter, from the result's own vocabulary:

    * **decode failure** - ``IMAGE_LOAD_ERROR`` (the file is not an image);
    * **registration failure** - outcome ``registration_failed`` (the page was
      read but could not be rectified - a paper or template fault);
    * **processing error** - outcome ``error`` without a decode failure: an
      unexpected exception, or the result standing in for a worker process
      that died (:func:`~omr_scanner.services.recognition_pool.error_result`).
      A software fault, never evidence about the paper;
    * **template error** - ``INVALID_TEMPLATE``;
    * the page-geometry assessment's verdict and issues, when one exists.
    """
    codes = set(result.status_codes)
    decode = StatusCode.IMAGE_LOAD_ERROR.value in codes
    processing = result.outcome is RecognitionOutcome.ERROR and not decode
    registration = result.outcome is RecognitionOutcome.REGISTRATION_FAILED
    assessment = result.scan_quality
    status = ""
    evaluated = False
    issue_codes: tuple[str, ...] = ()
    unusable: tuple[str, ...] = ()
    areas: tuple[str, ...] = ()
    if assessment is not None:
        status = assessment.status.value
        evaluated = assessment.evaluated
        issue_codes = assessment.codes
        unusable = tuple(
            issue.code.value
            for issue in assessment.issues
            if issue.status is ScanQualityStatus.UNUSABLE
        )
        areas = tuple(
            dict.fromkeys(area.value for issue in assessment.issues for area in issue.areas)
        )
    return QualityEvidence(
        decode_failed=decode,
        registration_failed=registration,
        processing_error=processing,
        template_error=StatusCode.INVALID_TEMPLATE.value in codes,
        quality_status=status,
        quality_evaluated=evaluated,
        issue_codes=issue_codes,
        unusable_issue_codes=unusable,
        areas=areas,
        alignment_warning=result.registration is RegistrationStatus.REGISTERED_WITH_WARNING,
    )


def decode_failure_verdict(policy: QualityPolicy = DEFAULT_POLICY) -> QualityVerdict:
    """The decision for an intake file that never decoded (it has no scan row)."""
    return policy.decide(QualityEvidence(decode_failed=True))


# ----------------------------------------------------------------------
# The session's pinned policy
# ----------------------------------------------------------------------
def _pin_in(row: ScanSession) -> QualityPolicy:
    """The session's policy; pins the default when none is pinned yet.

    The pin is the session row itself (``quality_policy_json`` and its
    fingerprint): written once, never replaced, so it is its own record. It is
    deliberately not an ``audit_event`` - pinning is machine bookkeeping that
    happens inside a sheet's work unit, and the finite workflow's audit ledger
    stays exactly as it was.
    """
    if row.quality_policy_json:
        try:
            return QualityPolicy.from_json(row.quality_policy_json)
        except (ValueError, KeyError, TypeError) as exc:
            # A policy this build cannot read must not be silently replaced:
            # that would change the session's interpretation without anyone
            # deciding it.
            raise QualityError(
                f"Scan session {row.scan_session_id} has an unreadable quality policy",
                user_message=(
                    "This scan session's scan-quality policy was written by a newer "
                    "OMRFlow and cannot be interpreted by this version."
                ),
            ) from exc
    policy = DEFAULT_POLICY
    row.quality_policy_json = policy.to_json()
    row.quality_policy_fingerprint = policy.fingerprint
    _LOGGER.info(
        "Scan session %s pinned scan-quality policy v%d (%s)",
        row.scan_session_id[:8],
        policy.version,
        policy.fingerprint[:12],
    )
    return policy


def session_policy(database: ProjectDatabase, scan_session_id: str) -> QualityPolicy:
    """The policy ``scan_session_id`` decides with, pinning the default on first use.

    Read-only or pre-17 databases are never written: they get the default.
    """
    if database.read_only or not has_quality_schema(database):
        return DEFAULT_POLICY
    with database.session() as session:
        row = session.get(ScanSession, scan_session_id)
        if row is None:
            return DEFAULT_POLICY
        return _pin_in(row)


def pinned_policy(database: ProjectDatabase, scan_session_id: str) -> QualityPolicy | None:
    """The session's pinned policy, or ``None`` when it has not pinned one yet. Read-only."""
    if not has_quality_schema(database):
        return None
    with database.session() as session:
        text = session.scalar(
            select(ScanSession.quality_policy_json).where(
                ScanSession.scan_session_id == scan_session_id
            )
        )
    return QualityPolicy.from_json(text) if text else None


def _policy_for_batch(session: Session, batch_id: str) -> QualityPolicy:
    owner = session.scalar(select(ScanBatch.scan_session_id).where(ScanBatch.batch_id == batch_id))
    if not owner:
        return DEFAULT_POLICY
    row = session.get(ScanSession, owner)
    return _pin_in(row) if row is not None else DEFAULT_POLICY


# ----------------------------------------------------------------------
# Recording
# ----------------------------------------------------------------------
def _write(
    session: Session,
    *,
    scan_id: int,
    batch_id: str,
    verdict: QualityVerdict,
    moment: datetime,
) -> None:
    reasons = ",".join(item.value for item in verdict.reasons)
    suggested = verdict.suggested_rejection.value if verdict.suggested_rejection else ""
    row = session.get(ScanQualityDecision, scan_id)
    if row is None:
        session.add(
            ScanQualityDecision(
                scan_id=scan_id,
                batch_id=batch_id,
                decision=verdict.decision.value,
                reasons=reasons,
                issue_codes=",".join(verdict.issue_codes),
                areas=",".join(verdict.areas),
                suggested_reason=suggested,
                policy_version=verdict.policy_version,
                policy_fingerprint=verdict.policy_fingerprint,
                evaluated_at=moment,
            )
        )
        return
    unchanged = (
        row.decision == verdict.decision.value
        and row.reasons == reasons
        and row.policy_fingerprint == verdict.policy_fingerprint
    )
    if unchanged:
        return
    # New evidence (the sheet was read again) or another policy: a new
    # decision. A dismissal answered the old one, so it does not carry over.
    row.batch_id = batch_id
    row.decision = verdict.decision.value
    row.reasons = reasons
    row.issue_codes = ",".join(verdict.issue_codes)
    row.areas = ",".join(verdict.areas)
    row.suggested_reason = suggested
    row.policy_version = verdict.policy_version
    row.policy_fingerprint = verdict.policy_fingerprint
    row.evaluated_at = moment
    row.dismissed_at = None
    row.dismissed_by = ""
    row.dismiss_note = ""


def record_decisions_in_session(
    session: Session, batch_id: str, results: Mapping[int, ScanResult]
) -> None:
    """Decide and store each read sheet's quality, inside the caller's transaction.

    The second half of the durable work unit's review state: called by
    :func:`~omr_scanner.services.batch_store.record_results` after the sheet's
    own conflicts, so result, conflicts and decision commit together. The
    session's policy is pinned here the first time a sheet of the session is
    decided.
    """
    if not results:
        return
    policy = _policy_for_batch(session, batch_id)
    moment = _now()
    for scan_id, result in results.items():
        _write(
            session,
            scan_id=int(scan_id),
            batch_id=batch_id,
            verdict=policy.decide(evidence_from_result(result)),
            moment=moment,
        )
    session.flush()


def _session_batches(session: Session, scan_session_id: str) -> list[str]:
    return [
        str(item)
        for item in session.scalars(
            select(ScanBatch.batch_id)
            .where(ScanBatch.scan_session_id == scan_session_id)
            .order_by(ScanBatch.created_at, ScanBatch.batch_id)
        ).all()
    ]


def evaluate_stored(database: ProjectDatabase, scan_session_id: str) -> int:
    """Decide every read sheet of the session that has no decision yet. Idempotent.

    From stored ``result_json`` only - no image is read - one window at a time.
    Covers sheets read before migration 17 and sheets a recognition-only caller
    stored. Returns how many decisions were written. Run by the continuous
    engine's restart sequence and before a session is finished.
    """
    if database.read_only or not has_quality_schema(database):
        return 0
    with database.session() as session:
        batches = _session_batches(session, scan_session_id)
    if not batches:
        return 0
    written = 0
    cursor = -1
    while True:
        with database.session() as session:
            window = session.execute(
                select(BatchScan.scan_id, BatchScan.batch_id, BatchScan.result_json)
                .where(BatchScan.batch_id.in_(batches))
                .where(BatchScan.status.in_(READ_STATUSES))
                .where(BatchScan.result_json != "")
                .where(BatchScan.scan_id > cursor)
                .where(
                    ~exists().where(ScanQualityDecision.scan_id == BatchScan.scan_id)
                )
                .order_by(BatchScan.scan_id)
                .limit(EVALUATION_WINDOW)
            ).all()
            if not window:
                return written
            by_batch: dict[str, dict[int, ScanResult]] = {}
            for scan_id, batch_id, payload in window:
                cursor = int(scan_id)
                try:
                    result = ScanResult.from_dict(json.loads(payload))
                except (ValueError, TypeError, KeyError):
                    _LOGGER.exception("Stored result of scan %s could not be decoded", scan_id)
                    continue
                by_batch.setdefault(str(batch_id), {})[int(scan_id)] = result
            for batch_id, results in by_batch.items():
                record_decisions_in_session(session, batch_id, results)
                written += len(results)


# ----------------------------------------------------------------------
# Suggestions
# ----------------------------------------------------------------------
def outstanding_clause(scan_id_column: Any) -> Any:
    """SQL: the sheet's quality decision is a suggested rescan nobody has answered yet.

    **The single definition** every list and count of outstanding suggestions
    uses: a ``rescan_required`` decision, not dismissed, on a sheet that is
    still ``active`` (any rejection, exclusion or replacement link is an
    answer), whose quality-evidence conflict no operator has resolved.
    Superseded batches and other dispositions are the effective-set service's
    to decide; callers combine this clause with it.
    """
    # Aliases, so the clause can sit inside a query that already selects from
    # these tables without being correlated to that query's rows.
    conflict = aliased(ReviewConflict)
    rejection = aliased(ScanRejection)
    decision = aliased(ScanQualityDecision)
    answered_in_resolve = (
        select(conflict.conflict_id)
        .where(conflict.scan_id == scan_id_column)
        .where(conflict.conflict_type.in_(EVIDENCE_CONFLICTS))
        .where(conflict.state == ConflictState.RESOLVED.value)
        .exists()
    )
    not_active = (
        select(rejection.scan_id)
        .where(rejection.scan_id == scan_id_column)
        .where(rejection.state != LifecycleState.ACTIVE.value)
        .exists()
    )
    suggested = (
        select(decision.scan_id)
        .where(decision.scan_id == scan_id_column)
        .where(decision.decision == QualityDecision.RESCAN_REQUIRED.value)
        .where(decision.dismissed_at.is_(None))
        .exists()
    )
    return and_(suggested, not_(not_active), not_(answered_in_resolve))


def _live_batches(session: Session, scan_session_id: str) -> list[str]:
    superseded = set(
        session.scalars(
            select(BatchSupersession.superseded_batch_id).where(
                BatchSupersession.reversed_at.is_(None)
            )
        ).all()
    )
    return [item for item in _session_batches(session, scan_session_id) if item not in superseded]


@dataclass(frozen=True, slots=True)
class QualitySuggestion:
    """One suggested rescan awaiting an operator (detached; for a paged view).

    Attributes:
        scan_id / batch_id: The sheet.
        source_name: Its file name.
        decision: Always ``RESCAN_REQUIRED`` for an outstanding suggestion.
        reasons: Why, as reason codes.
        issue_codes: The geometry issues behind it.
        suggested_reason: The existing rejection reason offered.
        policy_version / policy_fingerprint: Which policy decided.
        evaluated_at: When.
        source_id: The intake source the sheet came from, when known.
    """

    scan_id: int
    batch_id: str
    source_name: str
    decision: QualityDecision
    reasons: tuple[QualityReason, ...]
    issue_codes: tuple[str, ...]
    suggested_reason: RejectionReason | None
    policy_version: int
    policy_fingerprint: str
    evaluated_at: datetime | None
    source_id: str | None = None


def _reasons(text: str) -> tuple[QualityReason, ...]:
    found: list[QualityReason] = []
    for item in text.split(","):
        try:
            found.append(QualityReason(item))
        except ValueError:
            continue
    return tuple(found)


def count_outstanding(database: ProjectDatabase, scan_session_id: str) -> int:
    """How many suggested rescans of the session await an answer. One count query."""
    if not has_quality_schema(database):
        return 0
    with database.session() as session:
        live = _live_batches(session, scan_session_id)
        if not live:
            return 0
        return int(
            session.scalar(
                select(func.count())
                .select_from(BatchScan)
                .where(BatchScan.batch_id.in_(live))
                .where(outstanding_clause(BatchScan.scan_id))
            )
            or 0
        )


def outstanding_suggestions(
    database: ProjectDatabase,
    scan_session_id: str,
    *,
    offset: int = 0,
    limit: int = 100,
) -> tuple[QualitySuggestion, ...]:
    """The session's unanswered suggested rescans, oldest decision first, one page at a time."""
    if not has_quality_schema(database):
        return ()
    with database.session() as session:
        live = _live_batches(session, scan_session_id)
        if not live:
            return ()
        rows = session.execute(
            select(
                BatchScan.scan_id,
                BatchScan.batch_id,
                BatchScan.filename,
                ScanQualityDecision,
                ScanBatch.source_id,
            )
            .join(ScanQualityDecision, ScanQualityDecision.scan_id == BatchScan.scan_id)
            .join(ScanBatch, ScanBatch.batch_id == BatchScan.batch_id)
            .where(BatchScan.batch_id.in_(live))
            .where(outstanding_clause(BatchScan.scan_id))
            .order_by(ScanQualityDecision.evaluated_at, BatchScan.scan_id)
            .offset(max(0, offset))
            .limit(max(0, limit))
        ).all()
        return tuple(
            QualitySuggestion(
                scan_id=int(scan_id),
                batch_id=str(batch_id),
                source_name=str(name or ""),
                decision=QualityDecision(decision.decision),
                reasons=_reasons(decision.reasons),
                issue_codes=tuple(item for item in decision.issue_codes.split(",") if item),
                suggested_reason=(
                    RejectionReason(decision.suggested_reason)
                    if decision.suggested_reason
                    else None
                ),
                policy_version=decision.policy_version,
                policy_fingerprint=decision.policy_fingerprint,
                evaluated_at=decision.evaluated_at,
                source_id=str(source) if source else None,
            )
            for scan_id, batch_id, name, decision, source in rows
        )


@dataclass(frozen=True, slots=True)
class StoredDecision:
    """One sheet's stored quality decision, as an inspector reads it."""

    scan_id: int
    decision: QualityDecision
    reasons: tuple[QualityReason, ...]
    suggested_reason: RejectionReason | None
    policy_version: int
    policy_fingerprint: str
    dismissed_by: str = ""
    dismissed_at: datetime | None = None


def decision_of(database: ProjectDatabase, scan_id: int) -> StoredDecision | None:
    """The stored decision for one sheet, or ``None`` (not read, or pre-17)."""
    if not has_quality_schema(database):
        return None
    with database.session() as session:
        row = session.get(ScanQualityDecision, scan_id)
        if row is None:
            return None
        return StoredDecision(
            scan_id=row.scan_id,
            decision=QualityDecision(row.decision),
            reasons=_reasons(row.reasons),
            suggested_reason=RejectionReason(row.suggested_reason)
            if row.suggested_reason
            else None,
            policy_version=row.policy_version,
            policy_fingerprint=row.policy_fingerprint,
            dismissed_by=row.dismissed_by,
            dismissed_at=row.dismissed_at,
        )


def _require_outstanding(session: Session, scan_id: int) -> ScanQualityDecision:
    row = session.get(ScanQualityDecision, scan_id)
    if row is None or row.decision != QualityDecision.RESCAN_REQUIRED.value:
        raise QualityError(
            f"Scan {scan_id} has no suggested rescan",
            user_message="That sheet has no suggested rescan to answer.",
        )
    outstanding = session.scalar(
        select(func.count())
        .select_from(BatchScan)
        .where(BatchScan.scan_id == scan_id)
        .where(outstanding_clause(BatchScan.scan_id))
    )
    if not outstanding:
        raise QualityError(
            f"Scan {scan_id}'s suggested rescan was already answered",
            user_message="That suggested rescan has already been answered.",
        )
    return row


def dismiss_suggestion(
    database: ProjectDatabase, scan_id: int, *, reviewer: str, note: str = ""
) -> None:
    """Decline a suggested rescan: the sheet is kept exactly as it is. Audited.

    One transaction: the dismissal and its ``audit_event``. Nothing about the
    sheet's results changes - a suggestion never changed them.
    """
    from omr_scanner.services import review_store

    name = review_store.validate_reviewer(reviewer)
    moment = _now()
    with database.session() as session:
        row = _require_outstanding(session, scan_id)
        row.dismissed_at = moment
        row.dismissed_by = name
        row.dismiss_note = note.strip()
        session.add(
            AuditEvent(
                occurred_at=moment,
                batch_id=row.batch_id,
                scan_id=scan_id,
                conflict_id=0,
                entity_type=QUALITY_ENTITY,
                entity_id=str(scan_id),
                action=QualityAction.DISMISSED.value,
                reviewer=name,
                previous_value=QualityDecision.RESCAN_REQUIRED.value,
                new_value="kept",
                reason_code=row.suggested_reason,
                reason_text=note.strip(),
                detail=(
                    f"Suggested rescan declined ({row.reasons}); the sheet is kept as read. "
                    f"Quality policy v{row.policy_version} ({row.policy_fingerprint[:12]})."
                ),
            )
        )
    _LOGGER.info("Suggested rescan of scan %d dismissed by %s", scan_id, name)


def confirm_suggestion(
    database: ProjectDatabase,
    scan_id: int,
    *,
    reviewer: str,
    reason: RejectionReason | None = None,
    note: str = "",
    declared_candidate_id: str = "",
    declared_set_code: str = "",
) -> RescanCase:
    """Confirm a suggested rescan: reject the sheet through the existing Reject & Rescan flow.

    Exactly :func:`~omr_scanner.services.scan_lifecycle.reject_scan` - the same
    single transaction, the same audit event, the same effect on the effective
    set - with the suggested reason unless the operator chose another
    (``reason``), and a note naming the suggestion and policy it confirms.
    """
    from omr_scanner.services import scan_lifecycle

    with database.session() as session:
        row = _require_outstanding(session, scan_id)
        suggested = RejectionReason(row.suggested_reason) if row.suggested_reason else None
        provenance = (
            f"Confirmed suggested rescan ({row.reasons}; quality policy "
            f"v{row.policy_version} {row.policy_fingerprint[:12]})."
        )
    chosen = reason or suggested or RejectionReason.POOR_QUALITY
    text = f"{note.strip()} {provenance}".strip()
    return scan_lifecycle.reject_scan(
        database,
        scan_id,
        reviewer=reviewer,
        reason=chosen,
        note=text,
        declared_candidate_id=declared_candidate_id,
        declared_set_code=declared_set_code,
    )


def decisions_by_scan(
    database: ProjectDatabase, scan_ids: Iterable[int]
) -> dict[int, StoredDecision]:
    """Stored decisions for the given sheets (for tests and inspectors)."""
    wanted = sorted({int(item) for item in scan_ids})
    if not wanted or not has_quality_schema(database):
        return {}
    found: dict[int, StoredDecision] = {}
    with database.session() as session:
        for row in session.scalars(
            select(ScanQualityDecision).where(ScanQualityDecision.scan_id.in_(wanted))
        ).all():
            found[row.scan_id] = StoredDecision(
                scan_id=row.scan_id,
                decision=QualityDecision(row.decision),
                reasons=_reasons(row.reasons),
                suggested_reason=RejectionReason(row.suggested_reason)
                if row.suggested_reason
                else None,
                policy_version=row.policy_version,
                policy_fingerprint=row.policy_fingerprint,
                dismissed_by=row.dismissed_by,
                dismissed_at=row.dismissed_at,
            )
    return found


def decision_counts(
    database: ProjectDatabase, batch_ids: Sequence[str]
) -> dict[QualityDecision, int]:
    """``decision -> sheets`` over the given batches (one grouped query)."""
    if not batch_ids or not has_quality_schema(database):
        return {}
    with database.session() as session:
        return {
            QualityDecision(decision): int(count)
            for decision, count in session.execute(
                select(ScanQualityDecision.decision, func.count())
                .where(ScanQualityDecision.batch_id.in_(list(batch_ids)))
                .group_by(ScanQualityDecision.decision)
            ).all()
        }


__all__ = [
    "EVIDENCE_CONFLICTS",
    "QUALITY_ENTITY",
    "QUALITY_SCHEMA_VERSION",
    "QualityAction",
    "QualityError",
    "QualitySuggestion",
    "StoredDecision",
    "confirm_suggestion",
    "count_outstanding",
    "decision_counts",
    "decision_of",
    "decisions_by_scan",
    "decode_failure_verdict",
    "dismiss_suggestion",
    "evaluate_stored",
    "evidence_from_result",
    "has_quality_schema",
    "outstanding_clause",
    "outstanding_suggestions",
    "pinned_policy",
    "record_decisions_in_session",
    "session_policy",
    "stores_decisions",
]
