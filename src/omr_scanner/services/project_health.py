"""Project database and file-system health checks (Phase 10, §6/§7).

Purpose:
    Answer "is this project safe to keep working in?" at two costs: a cheap
    check suitable for every ordinary project open, and a comprehensive one
    for an operator who has asked for it or who has a reason to be worried.

Responsibilities:
    * :func:`quick_check` - SQLite's own ``PRAGMA quick_check``. Cheap enough
      to run every time a project opens.
    * :func:`full_check` - structural integrity, foreign keys, schema
      version, source-scan availability, stale jobs, unresolved review and
      reconciliation exceptions, missing verified answer keys, backup
      presence, and free disk space. Expensive; run on demand
      (*Tools -> Project Health / Recovery*), never automatically.
    * :class:`HealthReport` - the result, always with a worst-first
      :attr:`~HealthReport.level` an operator or a test can check without
      reading every issue.

What does NOT belong here:
    * Repairing anything. This module only ever detects and describes - see
      the phase brief §7's explicit rule against a "one-click repair" that
      silently rewrites examination data. Recovery guidance is surfaced by
      the GUI from what this module reports; fixing it (relinking a scan,
      restoring a backup, resolving a conflict) is the operator's decision,
      exercised through the module that actually owns that data.
    * Deciding whether a *result* is stale. Phase 8's own staleness rule
      (:func:`omr_scanner.services.scoring_store.stale_reasons_for`) already
      answers that per result; recomputing it for every result in a large
      project on every health check would make the "comprehensive" check
      cost as much as scoring the whole batch again. This module instead
      reports how many *sets* currently lack a verified key, which is the
      structural precondition for staleness, not staleness itself - see
      :attr:`HealthReport` for the honest boundary this draws.

Why quick and full are different functions, not one function with a flag:
    So a call site's own name says which one it invoked. A `quick_check`
    call at every project open should never accidentally become a full
    integrity scan because a default argument changed.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import exists, func, select, text
from sqlalchemy.exc import DBAPIError

from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.database.models import (
    AnswerKeyRevision,
    AuditEvent,
    BatchScan,
    BatchStatus,
    ReconciliationEntryRow,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
)
from omr_scanner.domain.reconciliation import ReconciliationStatus
from omr_scanner.domain.review import RESOLUTION_TYPES, ConflictState, ReviewAction
from omr_scanner.services import (
    project_backup,
    scan_lifecycle,
    scan_provenance,
    scan_sessions,
    set_identity,
)
from omr_scanner.services.set_identity import SetCodeMap, distinct_codes

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase

_LOGGER = logging.getLogger(__name__)

LOW_DISK_SPACE_BYTES = 500 * 1024 * 1024
"""Free space below which a health check warns (Phase 10, §34). 500 MiB is
comfortably more than one more report or one more autosave needs, and small
enough that it does not nag an operator with a merely modest amount of
headroom."""


class HealthLevel(StrEnum):
    """How serious a single finding, or a whole report, is."""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        """Ordering for "worst wins" aggregation."""
        return {HealthLevel.OK: 0, HealthLevel.WARNING: 1, HealthLevel.ERROR: 2}[self]


@dataclass(frozen=True, slots=True)
class HealthIssue:
    """One finding.

    Attributes:
        level: How serious.
        code: Stable, machine-readable identifier (never derived from the
            message text, so a later reword cannot silently change what a
            test or a script matches against).
        message: Plain-language description, safe to show an operator
            directly - never candidate data (§39/§40's privacy rule applies
            here too: a health check must not become a new place candidate
            information leaks into a diagnostic bundle).
    """

    level: HealthLevel
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The result of one health check.

    Attributes:
        issues: Every finding, worst first. Empty means nothing to report -
            not necessarily "perfect", since some things (real-scan
            recognition accuracy) this application never claims to check at
            all.
        kind: ``"quick"`` or ``"full"``, naming which check produced this.
    """

    issues: tuple[HealthIssue, ...]
    kind: str

    @property
    def level(self) -> HealthLevel:
        """The worst level among every issue, or :attr:`HealthLevel.OK` when empty."""
        if not self.issues:
            return HealthLevel.OK
        return max((issue.level for issue in self.issues), key=lambda level: level.rank)

    @property
    def is_ok(self) -> bool:
        """Whether the worst finding is still :attr:`HealthLevel.OK`."""
        return self.level == HealthLevel.OK

    def by_level(self, level: HealthLevel) -> tuple[HealthIssue, ...]:
        """Every issue at exactly ``level``."""
        return tuple(issue for issue in self.issues if issue.level == level)


def quick_check(database: ProjectDatabase) -> HealthReport:
    """Run SQLite's cheap ``PRAGMA quick_check``.

    Args:
        database: The open project database.

    Returns:
        A report with at most one issue: the check either passes, or it
        names the first problem SQLite found. Safe to run on every ordinary
        project open - it does not walk every index the way a full
        ``integrity_check`` does.
    """
    try:
        with database.engine.connect() as connection:
            rows = connection.execute(text("PRAGMA quick_check")).all()
        findings = [str(row[0]) for row in rows]
    except DBAPIError as exc:
        # Corruption severe enough that SQLite cannot even run the check
        # (a torn header, a destroyed schema page) raises here instead of
        # returning a row describing the problem. Either way it is the same
        # finding: this database is not safe to trust.
        findings = [str(exc.orig) if exc.orig is not None else str(exc)]
    if findings == ["ok"]:
        return HealthReport(issues=(), kind="quick")
    return HealthReport(
        issues=(
            HealthIssue(
                level=HealthLevel.ERROR,
                code="SQLITE_QUICK_CHECK_FAILED",
                message=(
                    "The project database failed a quick integrity check: "
                    + "; ".join(findings)
                ),
            ),
        ),
        kind="quick",
    )


def _integrity_check(database: ProjectDatabase) -> list[HealthIssue]:
    try:
        with database.engine.connect() as connection:
            rows = connection.execute(text("PRAGMA integrity_check")).all()
        findings = [str(row[0]) for row in rows]
    except DBAPIError as exc:
        findings = [str(exc.orig) if exc.orig is not None else str(exc)]
    if findings == ["ok"]:
        return []
    return [
        HealthIssue(
            level=HealthLevel.ERROR,
            code="SQLITE_INTEGRITY_CHECK_FAILED",
            message="The project database failed a full integrity check: " + "; ".join(findings),
        )
    ]


def _foreign_key_check(database: ProjectDatabase) -> list[HealthIssue]:
    try:
        with database.engine.connect() as connection:
            rows = connection.execute(text("PRAGMA foreign_key_check")).all()
    except DBAPIError:
        # Already reported by `_integrity_check`; nothing further to add.
        return []
    if not rows:
        return []
    return [
        HealthIssue(
            level=HealthLevel.ERROR,
            code="FOREIGN_KEY_VIOLATION",
            message=f"{len(rows)} foreign-key inconsistency(ies) were found in the database.",
        )
    ]


def _schema_version_issue(database: ProjectDatabase) -> list[HealthIssue]:
    version = database.schema_version
    if version == SCHEMA_VERSION:
        return []
    if version > SCHEMA_VERSION:
        return [
            HealthIssue(
                level=HealthLevel.ERROR,
                code="SCHEMA_NEWER_THAN_BUILD",
                message=(
                    f"This project's schema (version {version}) is newer than this "
                    f"build supports (version {SCHEMA_VERSION}). Update OMRFlow, or "
                    "open the project read-only."
                ),
            )
        ]
    return [
        HealthIssue(
            level=HealthLevel.WARNING,
            code="SCHEMA_OLDER_THAN_BUILD",
            message=(
                f"This project's schema (version {version}) is older than this build "
                f"(version {SCHEMA_VERSION}) and has not yet been migrated."
            ),
        )
    ]


def _stale_job_issues(database: ProjectDatabase) -> list[HealthIssue]:
    """Rows still ``queued``/``processing`` at rest.

    Normally impossible to observe: :func:`~omr_scanner.services.batch_store.recover_interrupted`
    repairs these the moment a project opens. Finding one here means either
    this check ran before that repair, or something bypassed it - worth a
    warning either way, not a silent pass.
    """
    with database.session() as session:
        stale = session.scalar(
            select(func.count())
            .select_from(BatchScan)
            .where(
                BatchScan.status.in_(
                    [ScanJobStatus.QUEUED.value, ScanJobStatus.PROCESSING.value]
                )
            )
        )
    if not stale:
        return []
    return [
        HealthIssue(
            level=HealthLevel.WARNING,
            code="STALE_PROCESSING_JOBS",
            message=(
                f"{stale} scan(s) are recorded as still being processed, with no "
                "process able to be doing so. They should return to pending the "
                "next time the project is reopened."
            ),
        )
    ]


def _crash_consistency_issues(database: ProjectDatabase) -> list[HealthIssue]:
    """States an abnormal termination could leave and recovery must not (0.1.1 phase 3).

    Bounded: five aggregate queries, none of which loads a row. See
    ``docs/decisions/ADR-0006-crash-safe-scan-work-units.md``.

    * a batch still ``running`` at rest - repaired by the next writable open;
    * a committed sheet status with no stored result - impossible for any
      writer of this build;
    * a failed sheet with a stored result and no conflict, in a batch that
      has review state - a failed read always raises exactly one conflict, so
      this is review state a crash left incomplete;
    * a conflict whose sheet has no stored result - review state with no
      recognition behind it;
    * a conflict whose history opens more than once - duplicated detection.
    """
    terminal = [
        ScanJobStatus.COMPLETED.value,
        ScanJobStatus.WARNING.value,
        ScanJobStatus.FAILED.value,
    ]
    with database.session() as session:
        running = session.scalar(
            select(func.count())
            .select_from(ScanBatch)
            .where(ScanBatch.status == BatchStatus.RUNNING.value)
        )
        without_result = session.scalar(
            select(func.count())
            .select_from(BatchScan)
            .where(BatchScan.status.in_(terminal))
            .where(BatchScan.result_json == "")
        )
        reviewed_batches = select(ReviewConflict.batch_id).distinct()
        failed_unreviewed = session.scalar(
            select(func.count())
            .select_from(BatchScan)
            .where(BatchScan.status == ScanJobStatus.FAILED.value)
            .where(BatchScan.result_json != "")
            .where(BatchScan.batch_id.in_(reviewed_batches))
            .where(~exists().where(ReviewConflict.scan_id == BatchScan.scan_id))
        )
        orphaned = session.scalar(
            select(func.count())
            .select_from(ReviewConflict)
            .join(BatchScan, BatchScan.scan_id == ReviewConflict.scan_id)
            .where(BatchScan.result_json == "")
            .where(ReviewConflict.state != ConflictState.WITHDRAWN.value)
        )
        detections = (
            select(AuditEvent.conflict_id)
            .where(AuditEvent.action == ReviewAction.DETECTED.value)
            .where(AuditEvent.conflict_id != 0)
            .group_by(AuditEvent.conflict_id)
            .having(func.count() > 1)
            .subquery()
        )
        duplicated = session.scalar(select(func.count()).select_from(detections))

    issues: list[HealthIssue] = []
    if running:
        issues.append(
            HealthIssue(
                level=HealthLevel.WARNING,
                code="BATCH_LEFT_RUNNING",
                message=(
                    f"{running} batch(es) are recorded as running. Unless a batch is being "
                    "processed right now, a run was interrupted; reopening the project "
                    "for editing recovers it."
                ),
            )
        )
    if without_result:
        issues.append(
            HealthIssue(
                level=HealthLevel.ERROR,
                code="SCAN_COMPLETED_WITHOUT_RESULT",
                message=(
                    f"{without_result} scan(s) are recorded as read but have no stored "
                    "recognition result."
                ),
            )
        )
    if failed_unreviewed:
        issues.append(
            HealthIssue(
                level=HealthLevel.ERROR,
                code="SCAN_REVIEW_STATE_MISSING",
                message=(
                    f"{failed_unreviewed} unreadable scan(s) have no review conflict, so "
                    "Resolve would not list them. Reopening the project for editing "
                    "re-derives them from the stored results."
                ),
            )
        )
    if orphaned:
        issues.append(
            HealthIssue(
                level=HealthLevel.ERROR,
                code="REVIEW_STATE_WITHOUT_RECOGNITION",
                message=f"{orphaned} conflict(s) belong to scans that have no stored result.",
            )
        )
    if duplicated:
        issues.append(
            HealthIssue(
                level=HealthLevel.ERROR,
                code="CONFLICT_DETECTED_TWICE",
                message=f"{duplicated} conflict(s) record their detection more than once.",
            )
        )
    return issues


def _source_scan_issues(database: ProjectDatabase) -> list[HealthIssue]:
    """Missing or changed source scans, across every batch in the project."""
    with database.session() as session:
        batch_ids = session.scalars(select(ScanBatch.batch_id)).all()

    missing = 0
    changed = 0
    for batch_id in batch_ids:
        # A superseded original whose image *Purge Rejects* quarantined or
        # deleted is gone on purpose, with the record to prove it - not a
        # source scan that went missing.
        removed = scan_lifecycle.removed_images(database, batch_id)
        for entry in scan_provenance.check_availability(database, batch_id):
            if entry.scan_id in removed:
                continue
            if entry.availability == scan_provenance.ScanAvailability.MISSING:
                missing += 1
            elif entry.availability == scan_provenance.ScanAvailability.CHANGED:
                changed += 1

    issues: list[HealthIssue] = []
    if missing:
        issues.append(
            HealthIssue(
                level=HealthLevel.WARNING,
                code="SOURCE_SCANS_MISSING",
                message=(
                    f"{missing} source scan(s) could not be found at their recorded "
                    "location. Relink them before reprocessing."
                ),
            )
        )
    if changed:
        issues.append(
            HealthIssue(
                level=HealthLevel.ERROR,
                code="SOURCE_SCANS_CHANGED",
                message=(
                    f"{changed} source scan(s) no longer match the content hash "
                    "recorded when they were imported - the file at that path has "
                    "changed since."
                ),
            )
        )
    return issues


def _unresolved_conflict_issue(database: ProjectDatabase) -> list[HealthIssue]:
    """Open conflicts that still need a person.

    Counts only the types that
    :attr:`~omr_scanner.domain.review.ConflictType.requires_resolution` - the
    student ID, the set code, and sheets that could not be read. A project
    scanned by an earlier build may also hold ``answer_*`` rows; reporting
    those would tell an operator to go and resolve something the Resolve stage
    no longer offers, which is worse than saying nothing.
    """
    with database.session() as session:
        open_count = session.scalar(
            select(func.count())
            .select_from(ReviewConflict)
            .where(ReviewConflict.state == ConflictState.OPEN.value)
            .where(
                ReviewConflict.conflict_type.in_(
                    [item.value for item in RESOLUTION_TYPES]
                )
            )
        )
    if not open_count:
        return []
    return [
        HealthIssue(
            level=HealthLevel.WARNING,
            code="UNRESOLVED_CONFLICTS",
            message=(
                f"{open_count} identification conflict(s) are still awaiting review."
            ),
        )
    ]


def _unresolved_reconciliation_issue(database: ProjectDatabase) -> list[HealthIssue]:
    with database.session() as session:
        rows = session.scalars(select(ReconciliationEntryRow.status)).all()
    exceptions = 0
    for status in rows:
        try:
            if ReconciliationStatus(status).is_exception:
                exceptions += 1
        except ValueError:
            # An unrecognised status string is itself worth flagging as an
            # exception (something a future or foreign build wrote), not a
            # reason to crash the whole health check.
            exceptions += 1
    if not exceptions:
        return []
    return [
        HealthIssue(
            level=HealthLevel.WARNING,
            code="UNRESOLVED_RECONCILIATION_EXCEPTIONS",
            message=(
                f"{exceptions} candidate/script reconciliation exception(s) are "
                "still unresolved."
            ),
        )
    ]


def _missing_verified_key_issue(database: ProjectDatabase) -> list[HealthIssue]:
    """Recognised sets with no verified key, compared through set identity.

    A sheet's reading is what the *paper* says, so it is translated to its
    logical set first (``A`` -> ``10`` for a Set 10 printed as ``A``), and
    keys are matched canonically (a key stored as ``a`` is Set ``A``'s).
    """
    identity = set_identity.load(database)
    with database.session() as session:
        recognised_sets = {
            identity.logical_for_physical(code)
            for (code,) in session.execute(
                select(BatchScan.set_code_value).where(BatchScan.set_code_value != "")
            ).all()
        }
        verified_sets = SetCodeMap(
            (code, True)
            for (code,) in session.execute(
                select(AnswerKeyRevision.set_code).where(
                    AnswerKeyRevision.status == "verified"
                )
            ).all()
        )
    missing = sorted(
        code for code in distinct_codes(sorted(recognised_sets)) if code not in verified_sets
    )
    if not missing:
        return []
    return [
        HealthIssue(
            level=HealthLevel.WARNING,
            code="SETS_WITHOUT_A_VERIFIED_KEY",
            message=(
                f"{len(missing)} recognised set(s) have no verified answer key: "
                + ", ".join(missing)
            ),
        )
    ]


def _set_identity_issues(database: ProjectDatabase) -> list[HealthIssue]:
    """Defined sets whose codes now canonicalise alike (``A`` and ``a``).

    Created by builds before phase 0.1.1-A, which compared set codes
    exactly. Never merged automatically: reported here by name, and the
    set-dependent stages refuse to score, verify or generate for them until an
    operator renames or removes one in Project Configuration.
    """
    found = set_identity.collisions(database)
    return [
        HealthIssue(
            level=HealthLevel.WARNING,
            code="SET_CODE_COLLISION",
            message=item.describe()
            + " Rename or remove one in Project Configuration -> Sets; until then "
            "reconciliation, answer-key verification, scoring and reports for it "
            "are refused.",
        )
        for item in found
    ]


def _scan_session_issues(database: ProjectDatabase) -> list[HealthIssue]:
    """Lifecycle integrity of scan sessions and finite batches (0.1.1 phase 2).

    Every batch belongs to a session; a sealed batch's ``total_scans`` equals
    its rows; no supersession is cyclic, crosses sessions or replaces an
    unsealed batch; a closed session holds no open batch; the active-session
    pointer names a session of this project; a batch whose template identity
    differs from its session's pin was acknowledged; ambiguous upgrade cases
    are listed. Crash-recovery state (revised phase 3) is not checked here.
    """
    from omr_scanner.database.models import (
        AuditEvent,
        BatchSupersession,
        ProjectSetting,
        ScanSession,
        SettingKey,
    )
    from omr_scanner.domain.scan_sessions import SessionAction, supersession_cycles

    if not scan_sessions.has_lifecycle_schema(database):
        return []
    issues: list[HealthIssue] = []
    with database.session() as session:
        orphans = session.scalar(
            select(func.count()).select_from(ScanBatch).where(ScanBatch.scan_session_id.is_(None))
        )
        if orphans:
            issues.append(
                HealthIssue(
                    HealthLevel.ERROR,
                    "BATCH_WITHOUT_SCAN_SESSION",
                    f"{orphans} batch(es) belong to no scan session. Reopen the project "
                    "read-write to run the upgrade backfill.",
                )
            )
        counts: dict[str, int] = {
            str(batch_id): int(count)
            for batch_id, count in session.execute(
                select(BatchScan.batch_id, func.count()).group_by(BatchScan.batch_id)
            ).all()
        }
        for batch_id, total in session.execute(
            select(ScanBatch.batch_id, ScanBatch.total_scans).where(
                ScanBatch.sealed_at.is_not(None)
            )
        ).all():
            if int(total) != int(counts.get(batch_id, 0)):
                issues.append(
                    HealthIssue(
                        HealthLevel.ERROR,
                        "SEALED_BATCH_MEMBERSHIP_CHANGED",
                        f"Sealed batch {batch_id[:8]} records {total} scan(s) but holds "
                        f"{counts.get(batch_id, 0)}.",
                    )
                )
        facts = {
            str(batch_id): (scan_session_id, sealed_at)
            for batch_id, scan_session_id, sealed_at in session.execute(
                select(ScanBatch.batch_id, ScanBatch.scan_session_id, ScanBatch.sealed_at)
            ).all()
        }
        live = {
            str(old): str(new)
            for old, new in session.execute(
                select(
                    BatchSupersession.superseded_batch_id,
                    BatchSupersession.superseding_batch_id,
                ).where(BatchSupersession.reversed_at.is_(None))
            ).all()
        }
        for cycle in supersession_cycles(live):
            issues.append(
                HealthIssue(
                    HealthLevel.ERROR,
                    "SUPERSESSION_CYCLE",
                    "Batch supersession forms a cycle: "
                    + " -> ".join(item[:8] for item in cycle),
                )
            )
        for old, new in live.items():
            old_facts, new_facts = facts.get(old), facts.get(new)
            if old_facts is None or new_facts is None or old_facts[0] != new_facts[0]:
                issues.append(
                    HealthIssue(
                        HealthLevel.ERROR,
                        "CROSS_SESSION_SUPERSESSION",
                        f"Batch {old[:8]} is superseded by {new[:8]} of another scan session.",
                    )
                )
            elif old_facts[1] is None:
                issues.append(
                    HealthIssue(
                        HealthLevel.WARNING,
                        "UNSEALED_BATCH_SUPERSEDED",
                        f"Batch {old[:8]} is superseded but was never sealed.",
                    )
                )
        sessions = session.scalars(select(ScanSession)).all()
        for row in sessions:
            if row.state == "closed":
                still_open = [
                    batch_id
                    for batch_id, (owner, sealed_at) in facts.items()
                    if owner == row.scan_session_id and sealed_at is None
                ]
                if still_open:
                    issues.append(
                        HealthIssue(
                            HealthLevel.ERROR,
                            "CLOSED_SESSION_HAS_OPEN_BATCH",
                            f"Closed scan session '{row.name}' holds {len(still_open)} "
                            "unsealed batch(es).",
                        )
                    )
            if bool(row.template_id) != bool(row.geometry_fingerprint):
                issues.append(
                    HealthIssue(
                        HealthLevel.WARNING,
                        "SESSION_TEMPLATE_PIN_INCOMPLETE",
                        f"Scan session '{row.name}' has an incomplete pinned template identity.",
                    )
                )
        acknowledged = set(
            session.scalars(
                select(AuditEvent.batch_id).where(
                    AuditEvent.action == SessionAction.TEMPLATE_ACKNOWLEDGED.value
                )
            ).all()
        )
        pins = {row.scan_session_id: row for row in sessions}
        for batch_id, owner, template_id, geometry, recognition, engine in session.execute(
            select(
                ScanBatch.batch_id, ScanBatch.scan_session_id, ScanBatch.template_id,
                ScanBatch.geometry_fingerprint, ScanBatch.recognition_fingerprint,
                ScanBatch.engine_version,
            )
        ).all():
            pin = pins.get(owner) if owner else None
            if pin is None or not pin.template_id or batch_id in acknowledged:
                continue
            if (template_id, geometry, recognition, engine) != (
                pin.template_id, pin.geometry_fingerprint,
                pin.recognition_fingerprint, pin.engine_version,
            ):
                issues.append(
                    HealthIssue(
                        HealthLevel.WARNING,
                        "BATCH_DIFFERS_FROM_SESSION_TEMPLATE",
                        f"Batch {batch_id[:8]} was read with a template identity that "
                        f"differs from scan session '{pin.name}' without an acknowledgement.",
                    )
                )
        pointer = session.get(ProjectSetting, SettingKey.ACTIVE_SCAN_SESSION)
        if pointer is not None and pointer.value and pointer.value not in pins:
            issues.append(
                HealthIssue(
                    HealthLevel.WARNING,
                    "ACTIVE_SCAN_SESSION_MISSING",
                    "The active scan session no longer exists; the next Process All "
                    "starts a new one.",
                )
            )
        project = session.get(ProjectSetting, SettingKey.PROJECT_ID)
        foreign = [
            row.name
            for row in sessions
            if row.project_id and project is not None and row.project_id != project.value
        ]
        if foreign:
            issues.append(
                HealthIssue(
                    HealthLevel.WARNING,
                    "SCAN_SESSION_OF_ANOTHER_PROJECT",
                    f"{len(foreign)} scan session(s) record a different project id.",
                )
            )
    report = scan_sessions.backfill_report(database)
    if report and report.get("ambiguous"):
        issues.append(
            HealthIssue(
                HealthLevel.WARNING,
                "SCAN_SESSION_BACKFILL_AMBIGUOUS",
                f"The upgrade kept {len(report['ambiguous'])} rescan relationship(s) in "
                "separate scan sessions because grouping them was ambiguous: "
                + " ".join(report["ambiguous"]),
            )
        )
    return issues


def _session_population_issues(database: ProjectDatabase) -> list[HealthIssue]:
    """Integrity of the rescan links the effective sheet set is built from (0.1.1 phase 4).

    :mod:`omr_scanner.services.session_population` resolves every sheet's
    disposition from ``scan_rejection``; it never repairs what it reads, so a
    malformed lineage is reported here instead: a cycle of replacements, a
    superseded sheet whose replacement is missing, a link on a row that is not
    superseded, a lifecycle row filed under another batch than its scan, a
    replacement in another scan session (honoured through its lineage root,
    but no longer creatable), and a session whose downstream state is split
    across batches. Linear in the number of lifecycle rows.
    """
    from omr_scanner.database.models import ReconciliationRun, ScanRejection
    from omr_scanner.domain.scan_lifecycle import LifecycleState
    from omr_scanner.domain.session_population import lineage_roots

    if not scan_sessions.has_lifecycle_schema(database):
        return []
    issues: list[HealthIssue] = []
    superseded = LifecycleState.SUPERSEDED_BY_REPLACEMENT.value
    with database.session() as session:
        rows = session.execute(
            select(
                ScanRejection.scan_id,
                ScanRejection.batch_id,
                ScanRejection.state,
                ScanRejection.replacement_scan_id,
                BatchScan.batch_id,
            ).outerjoin(BatchScan, BatchScan.scan_id == ScanRejection.scan_id)
        ).all()
        batch_session: dict[str, str | None] = {
            str(batch_id): owner
            for batch_id, owner in session.execute(
                select(ScanBatch.batch_id, ScanBatch.scan_session_id)
            ).all()
        }
        replacement_batch: dict[int, str] = {
            int(scan_id): str(batch_id)
            for scan_id, batch_id in session.execute(
                select(BatchScan.scan_id, BatchScan.batch_id).where(
                    BatchScan.scan_id.in_(
                        select(ScanRejection.replacement_scan_id).where(
                            ScanRejection.replacement_scan_id.is_not(None)
                        )
                    )
                )
            ).all()
        }
        holders = session.execute(
            select(ReconciliationRun.batch_id).distinct()
        ).scalars().all()
    replacement_of: dict[int, int] = {}
    for scan_id, filed_batch, state, replacement, scan_batch in rows:
        if scan_batch is not None and filed_batch != scan_batch:
            issues.append(
                HealthIssue(
                    HealthLevel.ERROR,
                    "LIFECYCLE_BATCH_MISMATCH",
                    f"Scan {scan_id}'s lifecycle record is filed under batch "
                    f"{str(filed_batch)[:8]}, but the scan was read into {str(scan_batch)[:8]}.",
                )
            )
        if state == superseded and (replacement is None or replacement not in replacement_batch):
            issues.append(
                HealthIssue(
                    HealthLevel.ERROR,
                    "DANGLING_REPLACEMENT",
                    f"Scan {scan_id} is marked replaced, but its replacement "
                    + ("is not recorded." if replacement is None else "is not in the project."),
                )
            )
        if replacement is None:
            continue
        if state != superseded:
            issues.append(
                HealthIssue(
                    HealthLevel.ERROR,
                    "CONTRADICTORY_REPLACEMENT_LINK",
                    f"Scan {scan_id} names replacement {replacement} but its state is "
                    f"'{state}', not replaced.",
                )
            )
            continue
        replacement_of[int(replacement)] = int(scan_id)
        mine = batch_session.get(str(filed_batch))
        theirs = batch_session.get(replacement_batch.get(int(replacement), ""))
        if mine and theirs and mine != theirs:
            issues.append(
                HealthIssue(
                    HealthLevel.WARNING,
                    "CROSS_SESSION_REPLACEMENT",
                    f"Scan {scan_id} is replaced by scan {replacement} of another scan "
                    "session; the replacement counts in the original's session.",
                )
            )
    cycles = lineage_roots(replacement_of).cycles
    if cycles:
        issues.append(
            HealthIssue(
                HealthLevel.ERROR,
                "RESCAN_LINEAGE_CYCLE",
                f"Rescan replacements form a cycle through {len(cycles)} scan(s): "
                + ", ".join(str(item) for item in sorted(cycles)),
            )
        )
    per_session: dict[str, list[str]] = {}
    for batch_id in holders:
        owner = batch_session.get(str(batch_id))
        if owner:
            per_session.setdefault(owner, []).append(str(batch_id))
    for _owner, batches in sorted(per_session.items()):
        if len(batches) > 1:
            issues.append(
                HealthIssue(
                    HealthLevel.WARNING,
                    "SESSION_DOWNSTREAM_SPLIT",
                    f"{len(batches)} batches of one scan session hold reconciliation "
                    "state; Attendance, Results and Reports read only the oldest "
                    "(" + ", ".join(sorted(item[:8] for item in batches)) + ").",
                )
            )
    return issues


def _backup_issue(project_root: Path) -> list[HealthIssue]:
    backups_dir = project_root / project_backup.BACKUP_DIR_NAME
    entries = project_backup.list_backups(backups_dir)
    if not entries:
        return [
            HealthIssue(
                level=HealthLevel.WARNING,
                code="NO_BACKUPS",
                message="No project backup has ever been created.",
            )
        ]
    incomplete = sum(1 for entry in entries if not entry.is_complete)
    if incomplete:
        return [
            HealthIssue(
                level=HealthLevel.WARNING,
                code="INCOMPLETE_BACKUPS_PRESENT",
                message=(
                    f"{incomplete} backup attempt(s) in {backups_dir} never finished "
                    "(no manifest) and are not usable."
                ),
            )
        ]
    return []


def _disk_space_issue(project_root: Path) -> list[HealthIssue]:
    try:
        usage = shutil.disk_usage(project_root)
    except OSError:
        return []
    if usage.free >= LOW_DISK_SPACE_BYTES:
        return []
    free_mb = usage.free // (1024 * 1024)
    return [
        HealthIssue(
            level=HealthLevel.WARNING,
            code="LOW_DISK_SPACE",
            message=(
                f"Only {free_mb} MB of free disk space remains where this project "
                "is stored."
            ),
        )
    ]


def full_check(database: ProjectDatabase, project_root: Path) -> HealthReport:
    """Run every comprehensive check this module knows how to run.

    Args:
        database: The open project database.
        project_root: The project's root directory (for backups and free
            disk space, which are file-system facts the database cannot
            answer on its own).

    Returns:
        The combined report. Expensive relative to :func:`quick_check` -
        ``integrity_check`` alone walks every page of the database - and
        intended for *Tools -> Project Health / Recovery*, not for every
        ordinary open.
    """
    issues: list[HealthIssue] = []
    issues += _integrity_check(database)
    structurally_sound = not issues
    if structurally_sound:
        issues += _foreign_key_check(database)

    if structurally_sound:
        # Every remaining check runs ordinary SQL against tables that
        # `_integrity_check` has just confirmed are readable. If the database
        # is already known to be damaged, running more queries against it
        # would only raise the same corruption again for no new information -
        # the operator already has the one finding that matters most.
        for check in (
            _schema_version_issue,
            _stale_job_issues,
            _crash_consistency_issues,
            _source_scan_issues,
            _unresolved_conflict_issue,
            _unresolved_reconciliation_issue,
            _missing_verified_key_issue,
            _set_identity_issues,
            _scan_session_issues,
            _session_population_issues,
        ):
            try:
                issues += check(database)
            except DBAPIError as exc:
                _LOGGER.exception("Health check %s failed unexpectedly", check.__name__)
                issues.append(
                    HealthIssue(
                        level=HealthLevel.ERROR,
                        code="HEALTH_CHECK_QUERY_FAILED",
                        message=f"A health check query failed: {exc}",
                    )
                )
    issues += _backup_issue(project_root)
    issues += _disk_space_issue(project_root)
    issues.sort(key=lambda issue: issue.level.rank, reverse=True)
    return HealthReport(issues=tuple(issues), kind="full")


__all__ = [
    "LOW_DISK_SPACE_BYTES",
    "HealthIssue",
    "HealthLevel",
    "HealthReport",
    "full_check",
    "quick_check",
]
