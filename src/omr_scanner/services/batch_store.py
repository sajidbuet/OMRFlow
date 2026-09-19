"""Durable batch state: what survives closing the window (Phase 5).

Purpose:
    Give every scan in a batch a record that outlives the process that read it,
    so a run interrupted after nine thousand of ten thousand sheets resumes
    rather than restarts.

Responsibilities:
    * :func:`create_batch` / :func:`register_scans` - enumerate the work once.
    * :func:`record_results` - commit finished sheets incrementally.
    * :func:`recover_interrupted` - repair state left behind by a crash.
    * :func:`resumable_scans` / :func:`failed_scans` - what is left to do.
    * :func:`check_compatibility` - refuse to mix results produced under
      different rules.
    * :func:`load_summary` / :func:`list_batches` - what the GUI reads.

What does NOT belong here:
    * Recognition, naming, copying or progress semantics. Those are
      :mod:`omr_scanner.services.batch_processor`, which knows nothing about
      this module - persistence is attached to it through its existing
      ``on_result`` hook, so a caller with no project (a test, a command line
      tool, the benchmark) runs exactly the code path it always did.
    * Qt. Everything here runs on whichever thread the coordinator is on.

Who writes, and why that matters:
    Only the coordinating process ever writes these tables, one thread at a
    time. Worker processes return recognition results and nothing else - they
    never open the database, never assign an output name and never touch shared
    state. SQLite is a single-writer store and the architecture keeps it that
    way by construction rather than by locking discipline.

Why results are committed in groups:
    One transaction per sheet is one fsync per sheet. On a spinning disk or a
    synchronised folder that dominates the run, and the work it protects is a
    few hundred milliseconds of recognition. Results are therefore buffered by
    :class:`BatchRecorder` and flushed on whichever of
    :data:`FLUSH_EVERY_SCANS` or :data:`FLUSH_INTERVAL_SECONDS` comes first, so
    an abrupt termination costs at most a second or two of finished work
    instead of the whole run. The bound is a documented trade, not an accident.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, delete, func, select, update

from omr_scanner.database.models import (
    BatchScan,
    BatchStatus,
    ScanBatch,
    ScanJobStatus,
)
from omr_scanner.services.recognition_models import (
    ENGINE_VERSION,
    RecognitionOutcome,
    ScanResult,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.batch_processor import ProcessedScan

_LOGGER = logging.getLogger(__name__)

FLUSH_EVERY_SCANS = 25
"""How many finished sheets a recorder buffers before committing.

Twenty-five sheets is a few seconds of work on any machine this runs on, and
one transaction per twenty-five sheets keeps the cost of durability under a
percent of the run. See the module docstring."""

FLUSH_INTERVAL_SECONDS = 2.0
"""Longest a finished sheet may sit unpersisted, however slow the batch.

Without this, a batch of very slow sheets would hold results in memory for
minutes - the count-based rule alone is not enough when the count rises slowly."""

OUTCOME_TO_STATUS: dict[str, ScanJobStatus] = {
    RecognitionOutcome.COMPLETE.value: ScanJobStatus.COMPLETED,
    RecognitionOutcome.REVIEW.value: ScanJobStatus.WARNING,
    RecognitionOutcome.REGISTRATION_FAILED.value: ScanJobStatus.FAILED,
    RecognitionOutcome.ERROR.value: ScanJobStatus.FAILED,
}
"""Recognition outcome to durable job status.

A table rather than branching, and one that covers every outcome a *finished*
sheet can have. ``PENDING`` is deliberately absent: it is not an outcome, and a
result carrying it would mean the pipeline handed back something it had not
actually read."""


class ErrorCategory:
    """Coarse, stable groupings for why a sheet failed.

    Deliberately few and deliberately derived from the engine's own
    :class:`~omr_scanner.services.recognition_models.StatusCode` rather than
    from message text: an operator triaging four hundred failures needs to know
    whether they are looking at one bad scanner batch or four hundred different
    problems, and a category parsed out of English prose would stop being true
    the first time a sentence was reworded.
    """

    NONE = ""
    IMAGE = "image"
    """The file could not be decoded at all."""
    REGISTRATION = "registration"
    """The page could not be rectified - markers or orientation."""
    TEMPLATE = "template"
    """The template cannot describe this sheet."""
    PROCESSING = "processing"
    """An unexpected failure inside recognition."""


_STATUS_CODE_CATEGORY: tuple[tuple[str, str], ...] = (
    ("IMAGE_LOAD_ERROR", ErrorCategory.IMAGE),
    ("MARKER_NOT_FOUND", ErrorCategory.REGISTRATION),
    ("ORIENTATION_FAILED", ErrorCategory.REGISTRATION),
    ("ALIGNMENT_FAILED", ErrorCategory.REGISTRATION),
    ("INVALID_TEMPLATE", ErrorCategory.TEMPLATE),
    ("PROCESSING_ERROR", ErrorCategory.PROCESSING),
)
"""Checked in order; the first status code a result carries decides. Ordered
most-specific first, so a result that both failed to load *and* failed to
align is reported as the load failure that caused it."""


def categorise_error(result: ScanResult) -> str:
    """Return the :class:`ErrorCategory` for a failed result, or ``""``.

    Args:
        result: The recognition result to classify.

    Returns:
        The category, or an empty string for a result that did not fail.
    """
    if result.outcome not in (
        RecognitionOutcome.ERROR,
        RecognitionOutcome.REGISTRATION_FAILED,
    ):
        return ErrorCategory.NONE
    for code, category in _STATUS_CODE_CATEGORY:
        if code in result.status_codes:
            return category
    return ErrorCategory.PROCESSING


@dataclass(frozen=True, slots=True)
class BatchIdentity:
    """What a batch was run with, for the compatibility check on resume.

    Attributes:
        template_id: The template's stable id.
        template_name: Its display name, for a message a human reads.
        template_path: Where it was loaded from.
        geometry_fingerprint: Hash of the template's page, markers and zones.
        recognition_fingerprint: Hash of its recognition settings, template
            level and per zone.
        engine_version: The recognition engine's behaviour version.
    """

    template_id: str = ""
    template_name: str = ""
    template_path: str = ""
    geometry_fingerprint: str = ""
    recognition_fingerprint: str = ""
    engine_version: str = ENGINE_VERSION

    @classmethod
    def of(cls, template: OmrTemplate, template_path: Path | None = None) -> BatchIdentity:
        """Capture the identity of ``template`` as it stands right now."""
        return cls(
            template_id=template.template_id,
            template_name=template.name,
            template_path=str(template_path) if template_path is not None else "",
            geometry_fingerprint=template.geometry_fingerprint(),
            recognition_fingerprint=template.recognition_fingerprint(),
            engine_version=ENGINE_VERSION,
        )


@dataclass(frozen=True, slots=True)
class CompatibilityVerdict:
    """Whether a stored batch may be resumed with the current configuration.

    Attributes:
        compatible: ``True`` when nothing material has changed.
        differences: One plain sentence per difference found, worst first.
            Empty when ``compatible``.
    """

    compatible: bool
    differences: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """Every difference as one paragraph, for a dialog."""
        return " ".join(self.differences)


@dataclass(frozen=True, slots=True)
class BatchSummary:
    """Counts describing one stored batch, for a list or a header.

    Attributes:
        batch_id: The batch's stable id.
        created_at: When it was first registered.
        updated_at: When it last changed.
        source_folder: Where its scans came from.
        template_name: The template it was run with.
        status: Its :class:`~omr_scanner.database.models.BatchStatus` value.
        total: How many scans it contains.
        completed: Read cleanly.
        warning: Read, needing review.
        failed: Could not be read.
        pending: Never attempted, or left over from a cancelled run.
    """

    batch_id: str
    created_at: datetime
    updated_at: datetime
    source_folder: str
    template_name: str
    status: str
    total: int
    completed: int = 0
    warning: int = 0
    failed: int = 0
    pending: int = 0

    @property
    def processed(self) -> int:
        """Scans that reached a terminal state.

        The definition the progress bar uses (``completed + warning + failed``):
        a sheet that failed is still a sheet that finished.
        """
        return self.completed + self.warning + self.failed

    @property
    def is_finished(self) -> bool:
        """Whether nothing remains to process."""
        return self.pending == 0 and self.total > 0

    @property
    def resume_label(self) -> str:
        """One line describing what resuming this batch would do."""
        if self.is_finished:
            return f"{self.total} scan(s), all processed"
        return f"{self.processed} of {self.total} processed - {self.pending} remaining"


def _now() -> datetime:
    """Current UTC time. One place, so every timestamp agrees."""
    return datetime.now(UTC)


def new_batch_id() -> str:
    """Return a fresh batch identifier.

    A UUID4 hex string rather than an auto-increment integer: a batch id
    travels into log lines, file names and (later) report headers, and it must
    not collide when two projects are merged or a database is copied.
    """
    return uuid.uuid4().hex


def create_batch(
    database: ProjectDatabase,
    paths: Sequence[Path],
    *,
    identity: BatchIdentity,
    source_folder: Path | None = None,
    settings: dict[str, Any] | None = None,
    batch_id: str | None = None,
) -> str:
    """Register a batch and every scan in it, all in one transaction.

    Args:
        database: The open project database.
        paths: The scans, in batch order. That order is stored and is what a
            resumed run replays, so duplicate-identifier suffixes stay stable
            across an interruption.
        identity: The template and engine this batch is bound to.
        source_folder: The folder the operator chose, for the batch list.
        settings: Batch options as plain JSON-safe data, recorded so a resume
            can tell the operator what the original run was configured to do.
        batch_id: Override the generated id; for tests.

    Returns:
        The batch id.

    Every scan starts :attr:`~omr_scanner.database.models.ScanJobStatus.PENDING`.
    Nothing is marked processing until a run actually claims it.
    """
    identifier = batch_id if batch_id is not None else new_batch_id()
    moment = _now()
    with database.session() as session:
        session.add(
            ScanBatch(
                batch_id=identifier,
                created_at=moment,
                updated_at=moment,
                source_folder=str(source_folder) if source_folder is not None else "",
                template_id=identity.template_id,
                template_name=identity.template_name,
                template_path=identity.template_path,
                geometry_fingerprint=identity.geometry_fingerprint,
                recognition_fingerprint=identity.recognition_fingerprint,
                engine_version=identity.engine_version,
                settings_json=json.dumps(settings or {}, sort_keys=True),
                status=BatchStatus.NEW.value,
                total_scans=len(paths),
            )
        )
        # Flush the parent before adding the children. `batch_scan.batch_id`
        # is a real foreign key and SQLite enforces it immediately (the engine
        # turns `PRAGMA foreign_keys` on), so the batch row has to be on disk
        # inside this transaction before any scan row can reference it.
        session.flush()
        session.add_all(
            _scan_row(identifier, index, path) for index, path in enumerate(paths)
        )
    _LOGGER.info(
        "Batch %s registered: %d scan(s) from %s with template '%s'",
        identifier,
        len(paths),
        source_folder or "(files)",
        identity.template_name,
    )
    return identifier


def _scan_row(batch_id: str, index: int, path: Path) -> BatchScan:
    """Build one PENDING scan row, recording what the file looked like now.

    Size and modification time are captured rather than a content hash: they
    are what a file system already knows, so enumerating ten thousand scans
    stays instant, and they are enough to notice that a source file was
    replaced between the original run and a resume. Hashing every scan would
    read every byte of the batch twice for a check that almost never fires.
    """
    try:
        stat = path.stat()
        size, modified = int(stat.st_size), float(stat.st_mtime)
    except OSError:
        # A file that vanished between the folder listing and now is still a
        # legitimate batch member - it will fail when it is read, with a
        # message that says so, rather than being silently dropped here.
        size, modified = 0, 0.0
    return BatchScan(
        batch_id=batch_id,
        batch_index=index,
        source_path=str(path),
        filename=path.name,
        file_size=size,
        modified_at=modified,
        status=ScanJobStatus.PENDING.value,
    )


def set_batch_status(database: ProjectDatabase, batch_id: str, status: BatchStatus) -> None:
    """Record a batch's lifecycle state."""
    with database.session() as session:
        session.execute(
            update(ScanBatch)
            .where(ScanBatch.batch_id == batch_id)
            .values(status=status.value, updated_at=_now())
        )


def mark_queued(database: ProjectDatabase, batch_id: str, paths: Sequence[Path]) -> None:
    """Mark the scans a run is about to attempt as queued.

    Called once, before the pool starts. Anything left QUEUED or PROCESSING
    afterwards is what :func:`recover_interrupted` repairs.
    """
    if not paths:
        return
    wanted = {str(path) for path in paths}
    with database.session() as session:
        rows = session.scalars(
            select(BatchScan).where(BatchScan.batch_id == batch_id)
        ).all()
        for row in rows:
            if row.source_path in wanted:
                row.status = ScanJobStatus.QUEUED.value
                row.started_at = _now()


def record_results(
    database: ProjectDatabase, batch_id: str, outcomes: Sequence[ProcessedScan]
) -> None:
    """Persist a group of finished scans in one transaction.

    Args:
        database: The open project database.
        batch_id: The batch the results belong to.
        outcomes: Finished sheets, in any order.

    Raises:
        omr_scanner.errors.DatabaseError: The transaction failed. The caller
            must treat every scan in ``outcomes`` as **not** durably recorded -
            see :class:`BatchRecorder`, which is what surfaces that to the
            operator rather than letting a run report success it cannot back up.
    """
    if not outcomes:
        return
    moment = _now()
    by_path = {str(item.source_path): item for item in outcomes}
    with database.session() as session:
        rows = session.scalars(
            select(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.source_path.in_(list(by_path)))
        ).all()
        for row in rows:
            outcome = by_path.get(row.source_path)
            if outcome is not None:
                _apply_outcome(row, outcome, moment)
        session.execute(
            update(ScanBatch)
            .where(ScanBatch.batch_id == batch_id)
            .values(updated_at=moment)
        )


def _apply_outcome(row: BatchScan, outcome: ProcessedScan, moment: datetime) -> None:
    """Copy one finished sheet's outcome onto its durable row."""
    result = outcome.result
    row.status = OUTCOME_TO_STATUS.get(
        result.outcome.value, ScanJobStatus.FAILED
    ).value
    row.attempt_count += 1
    row.outcome = result.outcome.value
    row.registration = result.registration.value
    row.identifier_value = result.identifier_value
    row.set_code_value = result.set_code_value
    row.output_name = outcome.output_name
    row.output_path = str(outcome.output_path) if outcome.output_path else ""
    row.copied = outcome.copied
    row.error_code = result.error_code
    row.error_category = categorise_error(result)
    row.error_message = result.registration_message or outcome.message
    # The engine's own serialisation, not a hand-rolled projection: it is
    # versioned, round-trips, and gains fields without a migration here.
    row.result_json = json.dumps(result.to_dict(), separators=(",", ":"))
    row.finished_at = moment
    row.duration_seconds = result.elapsed_seconds


def _rows_changed(result: object) -> int:
    """Return how many rows a bulk UPDATE touched.

    ``Session.execute`` is typed as returning the generic ``Result``, which does
    not declare ``rowcount``; every UPDATE actually returns a ``CursorResult``,
    which does. One narrowing here rather than a cast at each call site.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)


def mark_cancelled(database: ProjectDatabase, batch_id: str) -> int:
    """Return every unfinished scan to a resumable state after a cancel.

    Returns:
        How many rows were changed.

    A cancelled scan is ``CANCELLED`` rather than ``PENDING`` so the batch list
    can distinguish "never started" from "the operator stopped this"; both are
    resumable (:meth:`~omr_scanner.database.models.ScanJobStatus.is_resumable`).
    """
    with database.session() as session:
        result = session.execute(
            update(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .where(
                BatchScan.status.in_(
                    [
                        ScanJobStatus.QUEUED.value,
                        ScanJobStatus.PROCESSING.value,
                        ScanJobStatus.PENDING.value,
                    ]
                )
            )
            .values(status=ScanJobStatus.CANCELLED.value)
        )
        session.execute(
            update(ScanBatch)
            .where(ScanBatch.batch_id == batch_id)
            .values(status=BatchStatus.CANCELLED.value, updated_at=_now())
        )
        return _rows_changed(result)


def recover_interrupted(database: ProjectDatabase) -> tuple[int, int]:
    """Repair state left behind by a run that never finished.

    Called when a project is opened. A row can only be ``QUEUED`` or
    ``PROCESSING`` while some process owns it; if this application is starting
    up, no process does, so those rows are stale by definition and nothing else
    would ever move them on. They become ``PENDING`` - never ``FAILED``, because
    "we do not know what happened to this sheet" is not the same as "this sheet
    is bad", and marking it failed would quietly exclude it from a resume.

    Returns:
        ``(batches_repaired, scans_repaired)``.
    """
    stale_states = [ScanJobStatus.QUEUED.value, ScanJobStatus.PROCESSING.value]
    with database.session() as session:
        scans = session.execute(
            update(BatchScan)
            .where(BatchScan.status.in_(stale_states))
            .values(status=ScanJobStatus.PENDING.value, started_at=None)
        )
        batches = session.execute(
            update(ScanBatch)
            .where(ScanBatch.status == BatchStatus.RUNNING.value)
            .values(status=BatchStatus.INTERRUPTED.value, updated_at=_now())
        )
        repaired = (_rows_changed(batches), _rows_changed(scans))
    if repaired[1]:
        _LOGGER.info(
            "Recovered %d scan(s) in %d interrupted batch(es) left mid-processing",
            repaired[1],
            repaired[0],
        )
    return repaired


def resumable_scans(
    database: ProjectDatabase, batch_id: str, *, include_failed: bool = False
) -> tuple[Path, ...]:
    """Return the scans a resume should process, in batch order.

    Args:
        database: The open project database.
        batch_id: The batch to inspect.
        include_failed: Also return scans that failed, for an explicit retry.
            Off by default: a resume repeats what was never done, and silently
            re-reading a sheet that has already failed twice wastes the
            operator's time without telling them anything new.

    Returns:
        Source paths, ordered by :attr:`~omr_scanner.database.models.BatchScan.batch_index`
        so that duplicate-identifier suffixes come out the same way they would
        have in an uninterrupted run.
    """
    wanted = [
        ScanJobStatus.PENDING.value,
        ScanJobStatus.QUEUED.value,
        ScanJobStatus.PROCESSING.value,
        ScanJobStatus.CANCELLED.value,
    ]
    if include_failed:
        wanted.append(ScanJobStatus.FAILED.value)
    with database.session() as session:
        rows = session.scalars(
            select(BatchScan.source_path)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.status.in_(wanted))
            .order_by(BatchScan.batch_index)
        ).all()
    return tuple(Path(item) for item in rows)


def scan_paths(database: ProjectDatabase, batch_id: str) -> tuple[Path, ...]:
    """Return every scan in the batch, in batch order.

    The order matters as much as the contents: it is the order a resumed run
    replays, and therefore the order that decides which of two sheets sharing
    a roll number keeps the plain file name.
    """
    with database.session() as session:
        rows = session.scalars(
            select(BatchScan.source_path)
            .where(BatchScan.batch_id == batch_id)
            .order_by(BatchScan.batch_index)
        ).all()
    return tuple(Path(item) for item in rows)


def scan_ids_by_path(database: ProjectDatabase, batch_id: str) -> dict[Path, int]:
    """Map each of a batch's source paths to its durable scan id.

    What a caller holding :class:`ProcessedScan` results needs in order to
    attach anything to the right row - Phase 6's conflict detection, above all,
    which works from a finished :class:`BatchReport` and has only paths.
    """
    with database.session() as session:
        rows = session.execute(
            select(BatchScan.source_path, BatchScan.scan_id).where(
                BatchScan.batch_id == batch_id
            )
        ).all()
    return {Path(str(path)): int(scan_id) for path, scan_id in rows}


def failed_scans(database: ProjectDatabase, batch_id: str) -> tuple[Path, ...]:
    """Return every failed scan in the batch, in batch order."""
    with database.session() as session:
        rows = session.scalars(
            select(BatchScan.source_path)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.status == ScanJobStatus.FAILED.value)
            .order_by(BatchScan.batch_index)
        ).all()
    return tuple(Path(item) for item in rows)


def completed_results(
    database: ProjectDatabase, batch_id: str
) -> tuple[ScanResult, ...]:
    """Rebuild every finished scan's recognition result from the database.

    This is what makes "resume" honest rather than cosmetic: the results a
    previous run produced come back as real
    :class:`~omr_scanner.services.recognition_models.ScanResult` objects, so a
    CSV exported after resuming covers the whole batch and not just the part
    this session happened to read.

    A row whose stored JSON cannot be decoded is skipped with a log line rather
    than raising - one unreadable record must not cost the other 9,999.
    """
    with database.session() as session:
        rows = session.scalars(
            select(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.result_json != "")
            .order_by(BatchScan.batch_index)
        ).all()
        payloads = [(row.filename, row.result_json) for row in rows]

    results: list[ScanResult] = []
    for filename, payload in payloads:
        try:
            results.append(ScanResult.from_dict(json.loads(payload)))
        except (ValueError, TypeError, KeyError):
            _LOGGER.exception("Stored result for %s could not be decoded", filename)
    return tuple(results)


def results_by_scan(
    database: ProjectDatabase, batch_id: str
) -> dict[int, ScanResult]:
    """Rebuild every finished scan's result, keyed by its durable scan id.

    The sibling of :func:`completed_results`, which keys by nothing: scoring
    needs to go from a candidate's script to what was read off it, and pairing
    the two lists by position would be a silent mismatch waiting for the first
    batch with an undecodable row in it.

    A row whose stored JSON cannot be decoded is skipped with a log line rather
    than raising - one unreadable record must not cost the other 9,999.
    """
    with database.session() as session:
        rows = session.scalars(
            select(BatchScan)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.result_json != "")
            .order_by(BatchScan.batch_index)
        ).all()
        payloads = [(row.scan_id, row.filename, row.result_json) for row in rows]

    found: dict[int, ScanResult] = {}
    for scan_id, filename, payload in payloads:
        try:
            found[scan_id] = ScanResult.from_dict(json.loads(payload))
        except (ValueError, TypeError, KeyError):
            _LOGGER.exception("Stored result for %s could not be decoded", filename)
    return found


def check_compatibility(
    database: ProjectDatabase, batch_id: str, identity: BatchIdentity
) -> CompatibilityVerdict:
    """Say whether ``identity`` may be used to continue a stored batch.

    Args:
        database: The open project database.
        batch_id: The batch to check.
        identity: What the *current* configuration would run with.

    Returns:
        The verdict. Incompatibility is never resolved here - the operator is
        told what changed and decides, because "resume with the original
        settings" and "reprocess everything with the new ones" are both
        legitimate answers and only they know which one they meant.
    """
    with database.session() as session:
        batch = session.get(ScanBatch, batch_id)
        if batch is None:
            return CompatibilityVerdict(
                compatible=False, differences=("This batch is no longer in the project.",)
            )
        stored = BatchIdentity(
            template_id=batch.template_id,
            template_name=batch.template_name,
            template_path=batch.template_path,
            geometry_fingerprint=batch.geometry_fingerprint,
            recognition_fingerprint=batch.recognition_fingerprint,
            engine_version=batch.engine_version,
        )

    differences: list[str] = []
    if stored.template_id != identity.template_id:
        differences.append(
            f"The batch was processed with template '{stored.template_name}' and the "
            f"loaded template is '{identity.template_name}'."
        )
    elif stored.geometry_fingerprint != identity.geometry_fingerprint:
        differences.append(
            "The template's geometry has been edited since this batch was started, "
            "so bubbles would be sampled from different positions than the scans "
            "already processed."
        )
    if stored.recognition_fingerprint != identity.recognition_fingerprint:
        differences.append(
            "The recognition thresholds have changed since this batch was started, "
            "so the remaining scans would be judged by different rules than the "
            "ones already processed."
        )
    if stored.engine_version != identity.engine_version:
        differences.append(
            f"This batch was processed by recognition engine {stored.engine_version} "
            f"and this build is {identity.engine_version}."
        )
    return CompatibilityVerdict(
        compatible=not differences, differences=tuple(differences)
    )


def _status_counts(session: Session, batch_id: str) -> dict[str, int]:
    """Return ``{status: count}`` for one batch, in a single grouped query.

    Counting in SQL rather than loading every row: a ten-thousand-scan batch's
    summary is four integers, and fetching ten thousand objects to add them up
    would make opening the batch list slower than processing a sheet.
    """
    rows = session.execute(
        select(BatchScan.status, func.count())
        .where(BatchScan.batch_id == batch_id)
        .group_by(BatchScan.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def load_summary(database: ProjectDatabase, batch_id: str) -> BatchSummary | None:
    """Return one batch's counts, or ``None`` when it does not exist."""
    with database.session() as session:
        batch = session.get(ScanBatch, batch_id)
        if batch is None:
            return None
        return _summary_from(batch, _status_counts(session, batch_id))


def list_batches(database: ProjectDatabase, *, limit: int = 50) -> tuple[BatchSummary, ...]:
    """Return stored batches, most recently updated first."""
    with database.session() as session:
        batches = session.scalars(
            select(ScanBatch).order_by(ScanBatch.updated_at.desc()).limit(limit)
        ).all()
        summaries = [
            _summary_from(batch, _status_counts(session, batch.batch_id))
            for batch in batches
        ]
    return tuple(summaries)


def _summary_from(batch: ScanBatch, counts: dict[str, int]) -> BatchSummary:
    """Build a summary from a batch row and its status histogram."""
    pending = sum(
        counts.get(status.value, 0)
        for status in ScanJobStatus
        if status.is_resumable
    )
    return BatchSummary(
        batch_id=batch.batch_id,
        created_at=batch.created_at,
        updated_at=batch.updated_at,
        source_folder=batch.source_folder,
        template_name=batch.template_name,
        status=batch.status,
        total=batch.total_scans,
        completed=counts.get(ScanJobStatus.COMPLETED.value, 0),
        warning=counts.get(ScanJobStatus.WARNING.value, 0),
        failed=counts.get(ScanJobStatus.FAILED.value, 0),
        pending=pending,
    )


def finalise_batch(database: ProjectDatabase, batch_id: str) -> BatchSummary | None:
    """Record the batch's terminal status from what its scans actually say.

    Derived from the rows rather than from "the loop ended": a run that
    finished with thirty-six unreadable sheets is
    :attr:`~omr_scanner.database.models.BatchStatus.COMPLETED_WITH_ERRORS`, and
    reporting it as a plain success is exactly the overclaim this phase exists
    to prevent.
    """
    summary = load_summary(database, batch_id)
    if summary is None:
        return None
    if summary.pending:
        status = BatchStatus.INTERRUPTED
    elif summary.failed:
        status = BatchStatus.COMPLETED_WITH_ERRORS
    else:
        status = BatchStatus.COMPLETED
    set_batch_status(database, batch_id, status)
    return load_summary(database, batch_id)


def delete_batch(database: ProjectDatabase, batch_id: str) -> None:
    """Remove a batch and its scans. Used by tests and housekeeping."""
    with database.session() as session:
        session.execute(delete(BatchScan).where(BatchScan.batch_id == batch_id))
        session.execute(delete(ScanBatch).where(ScanBatch.batch_id == batch_id))


@dataclass
class BatchRecorder:
    """Buffers finished sheets and commits them in groups.

    Attach to a run through
    :func:`~omr_scanner.services.batch_processor.process_batch`'s existing
    ``on_result`` hook - the batch processor itself stays unaware that a
    database exists, which is what keeps it usable with no project open.

    Attributes:
        database: Where to write.
        batch_id: The batch being recorded.
        flush_every: Buffered sheets that trigger a commit.
        flush_interval: Seconds after which a partial buffer is committed.
        persisted: How many sheets have been committed successfully.
        failure: The first persistence failure's message, or ``""``. Non-empty
            means results have been produced that are **not** durably stored,
            which the caller must report rather than quietly finishing (see
            "Persistence failure" in ``docs/scan_workflow.md``).
    """

    database: ProjectDatabase
    batch_id: str
    flush_every: int = FLUSH_EVERY_SCANS
    flush_interval: float = FLUSH_INTERVAL_SECONDS
    persisted: int = 0
    failure: str = ""
    _buffer: list[ProcessedScan] = field(default_factory=list, repr=False)
    _last_flush: float = field(default_factory=time.monotonic, repr=False)

    @property
    def healthy(self) -> bool:
        """Whether every result so far has been durably recorded."""
        return not self.failure

    @property
    def unsaved(self) -> int:
        """How many finished sheets are buffered but not yet committed."""
        return len(self._buffer)

    def record(self, outcome: ProcessedScan) -> None:
        """Buffer one finished sheet, flushing when the buffer is due."""
        self._buffer.append(outcome)
        due = (
            len(self._buffer) >= self.flush_every
            or (time.monotonic() - self._last_flush) >= self.flush_interval
        )
        if due:
            self.flush()

    def flush(self) -> bool:
        """Commit whatever is buffered.

        Returns:
            ``True`` when the buffer is now empty and durable. ``False`` when
            the write failed; the buffer is kept so a later flush can retry it,
            and :attr:`failure` records why.
        """
        if not self._buffer:
            self._last_flush = time.monotonic()
            return True
        try:
            record_results(self.database, self.batch_id, self._buffer)
        except Exception as exc:
            # Deliberately broad: any storage failure - a full disk, a revoked
            # network share, a locked file - must become a reported condition
            # rather than an exception that ends the run and loses the results
            # still held in memory.
            _LOGGER.exception("Could not persist %d batch result(s)", len(self._buffer))
            if not self.failure:
                self.failure = str(exc)
            return False
        self.persisted += len(self._buffer)
        self._buffer.clear()
        self._last_flush = time.monotonic()
        return True


__all__ = [
    "FLUSH_EVERY_SCANS",
    "FLUSH_INTERVAL_SECONDS",
    "OUTCOME_TO_STATUS",
    "BatchIdentity",
    "BatchRecorder",
    "BatchStatus",
    "BatchSummary",
    "CompatibilityVerdict",
    "ErrorCategory",
    "ScanJobStatus",
    "categorise_error",
    "check_compatibility",
    "completed_results",
    "create_batch",
    "delete_batch",
    "failed_scans",
    "finalise_batch",
    "list_batches",
    "load_summary",
    "mark_cancelled",
    "mark_queued",
    "new_batch_id",
    "record_results",
    "recover_interrupted",
    "resumable_scans",
    "scan_ids_by_path",
    "scan_paths",
    "set_batch_status",
]
