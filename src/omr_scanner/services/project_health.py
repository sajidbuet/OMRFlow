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

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from omr_scanner.database.migrations import SCHEMA_VERSION
from omr_scanner.database.models import (
    AnswerKeyRevision,
    BatchScan,
    ReconciliationEntryRow,
    ReviewConflict,
    ScanBatch,
    ScanJobStatus,
)
from omr_scanner.domain.reconciliation import ReconciliationStatus
from omr_scanner.domain.review import RESOLUTION_TYPES, ConflictState
from omr_scanner.services import project_backup, scan_provenance

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


def _source_scan_issues(database: ProjectDatabase) -> list[HealthIssue]:
    """Missing or changed source scans, across every batch in the project."""
    with database.session() as session:
        batch_ids = session.scalars(select(ScanBatch.batch_id)).all()

    missing = 0
    changed = 0
    for batch_id in batch_ids:
        for entry in scan_provenance.check_availability(database, batch_id):
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
    with database.session() as session:
        recognised_sets = {
            code
            for (code,) in session.execute(
                select(BatchScan.set_code_value).where(BatchScan.set_code_value != "")
            ).all()
        }
        verified_sets = {
            code
            for (code,) in session.execute(
                select(AnswerKeyRevision.set_code).where(
                    AnswerKeyRevision.status == "verified"
                )
            ).all()
        }
    missing = sorted(recognised_sets - verified_sets)
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
            _source_scan_issues,
            _unresolved_conflict_issue,
            _unresolved_reconciliation_issue,
            _missing_verified_key_issue,
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
