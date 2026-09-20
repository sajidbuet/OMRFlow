"""Content-hash provenance for imported scans (Phase 10, §9/§10).

Purpose:
    Answer three questions a filename can never answer honestly: is this the
    same file it was when it was imported, has this exact file already been
    imported once, and where did a moved or relinked source file go.

Responsibilities:
    * :func:`hash_file` / :func:`hash_bytes` - the one hashing routine every
      other function here uses, so "the same algorithm everywhere" is true by
      construction rather than by convention.
    * :func:`compute_hashes_for_batch` - hash every real source file in a
      batch and persist the digest, in bounded-size batched writes.
    * :func:`duplicate_groups` - which scans share identical content, across
      one batch or the whole project.
    * :func:`check_availability` - present / missing / changed / moved, for
      the health check and for reopening a project before reprocessing.
    * :func:`relink_scan` - accept a replacement path for a scan, but only
      after its content hash is verified to match.

What does NOT belong here:
    * Deciding what to *do* about a duplicate or a changed file - that is an
      operator decision, surfaced by the health check
      (:mod:`omr_scanner.services.project_health`) and the GUI. Detecting is
      this module's job; resolving is not.
    * Duplicate *candidate ID* detection. Two different scripts can
      legitimately share a candidate ID exception (Phase 7's territory,
      :mod:`omr_scanner.services.reconciliation`) without their *image bytes*
      being identical, and vice versa - a re-scanned duplicate photocopy has
      the same candidate but different bytes. Conflating the two exception
      classes was explicitly ruled out by the phase brief.

Why hashing runs once, at import, and not on every open:
    A hash is proof about a moment: "this is what the file contained when it
    was registered." Re-hashing on every project open would cost the same I/O
    as import itself, every single time, for scans nobody has touched - the
    health check instead compares size and modification time first
    (:func:`check_availability`) and only re-hashes when one of those has
    actually changed, which is the situation the hash exists to adjudicate.
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from omr_scanner.database.models import BatchScan

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase

_LOGGER = logging.getLogger(__name__)

HASH_ALGORITHM = "sha256"
"""The one algorithm this module ever writes. See :class:`BatchScan.content_hash_algorithm`
for why it is still recorded per row rather than assumed."""

STRESS_SOURCE_PREFIX = "stress:"
"""How a virtual, lazily-rendered stress-test source is identified (see
:mod:`omr_scanner.evaluation.stress_dataset`) - never hashed as a file,
because it is not one.

Deliberately free of ``/`` or ``\\``: every virtual source path is
round-tripped through :class:`pathlib.Path` (batch registration and storage
both take ``Path`` values), and ``PureWindowsPath`` rewrites
``stress://1/000001`` into ``stress:\\1\\000001`` - silently destroying the
``stress://`` prefix this module would otherwise check for. A prefix with no
separator characters at all survives that round trip unchanged on every
platform, which is why :func:`~omr_scanner.evaluation.stress_dataset.virtual_source_path`
joins the seed and index with ``-``, never ``/``."""

_READ_CHUNK_BYTES = 1 << 20
"""1 MiB. Bounded, constant memory whether the file is 50 KB or 500 MB - what
makes hashing scale to a 100,000-sheet batch without the peak memory of the
run depending on its largest single scan."""

_HASH_BATCH_SIZE = 500
"""How many computed digests accumulate before one bulk `UPDATE` commits them.

The same trade Phase 5's :class:`~omr_scanner.services.batch_store.BatchRecorder`
makes for recognition results: one transaction per file would be one fsync
per file, and hashing a 100,000-sheet batch would spend more time committing
than hashing."""

_HASH_WORKERS = 8
"""Threads used to hash files concurrently.

Hashing is I/O-bound - the GIL is released for the duration of every
``file.read()`` - so a small thread pool speeds up a large batch on any
storage faster than a single spinning disk without competing with the CPU
cost of recognition, which runs in separate *processes* entirely
(:mod:`omr_scanner.services.parallel_batch`)."""


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes.

    Args:
        path: File to hash.

    Returns:
        A 64-character lowercase hex digest.

    Raises:
        OSError: The file could not be opened or read.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of an in-memory buffer.

    For content that is never written to a permanent file at all - a
    lazily-rendered stress-test sheet (:mod:`omr_scanner.evaluation.stress_dataset`)
    still needs a content identity for its own exact-duplicate test case.
    """
    return hashlib.sha256(data).hexdigest()


def is_virtual_source(source_path: str) -> bool:
    """Whether ``source_path`` names a synthetic source rather than a real file."""
    return source_path.startswith(STRESS_SOURCE_PREFIX)


def compute_hashes_for_batch(
    database: ProjectDatabase, batch_id: str, *, only_missing: bool = True
) -> int:
    """Hash every real source file in a batch and persist the digest.

    Args:
        database: The open project database.
        batch_id: The batch to hash.
        only_missing: Skip scans that already carry a hash. A batch reopened
            after an earlier hashing pass, or resumed after an interruption,
            does not pay to re-read files it has already fingerprinted.

    Returns:
        How many scans were newly hashed.

    A source file that cannot be read (missing, permission denied) is
    skipped rather than raising: hashing is provenance, not a precondition
    for processing, and a missing file is already reported, with a readable
    message, by recognition itself when the batch actually runs.
    """
    with database.session() as session:
        query = select(BatchScan.scan_id, BatchScan.source_path).where(
            BatchScan.batch_id == batch_id
        )
        if only_missing:
            query = query.where(BatchScan.content_sha256 == "")
        rows = session.execute(query).all()

    targets = [
        (int(scan_id), str(source_path))
        for scan_id, source_path in rows
        if not is_virtual_source(str(source_path))
    ]
    if not targets:
        return 0

    digests: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=_HASH_WORKERS) as pool:
        futures = {
            pool.submit(_safe_hash_file, path): scan_id for scan_id, path in targets
        }
        for future in as_completed(futures):
            scan_id = futures[future]
            digest = future.result()
            if digest is not None:
                digests[scan_id] = digest

    _write_hashes(database, digests)
    return len(digests)


def _safe_hash_file(source_path: str) -> str | None:
    """Hash one file, returning ``None`` (and logging) on any read failure."""
    try:
        return hash_file(Path(source_path))
    except OSError:
        _LOGGER.warning("Could not hash %s for provenance", Path(source_path).name)
        return None


def _write_hashes(database: ProjectDatabase, digests: dict[int, str]) -> None:
    """Persist computed digests in bounded-size batches, one transaction each.

    Uses SQLAlchemy's ORM bulk-update-by-primary-key form
    (``session.execute(update(Model), [{"pk": ..., "col": ...}, ...])``), which
    is why every parameter dict names the primary key column literally.
    """
    items = list(digests.items())
    statement = update(BatchScan)
    for start in range(0, len(items), _HASH_BATCH_SIZE):
        chunk = items[start : start + _HASH_BATCH_SIZE]
        with database.session() as session:
            session.execute(
                statement,
                [
                    {
                        "scan_id": scan_id,
                        "content_sha256": digest,
                        "content_hash_algorithm": HASH_ALGORITHM,
                    }
                    for scan_id, digest in chunk
                ],
            )


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """Scans across a batch whose content is byte-for-byte identical.

    Attributes:
        content_sha256: The shared digest.
        scan_ids: Every scan carrying it, in ``batch_index`` order.
        filenames: Their file names, parallel to :attr:`scan_ids`, for a
            message a human reads without a second query.
    """

    content_sha256: str
    scan_ids: tuple[int, ...]
    filenames: tuple[str, ...]

    @property
    def is_exact_duplicate_import(self) -> bool:
        """Whether more than one scan in the batch shares this content."""
        return len(self.scan_ids) > 1


def duplicate_groups(database: ProjectDatabase, batch_id: str) -> tuple[DuplicateGroup, ...]:
    """Return every group of scans in ``batch_id`` sharing identical content.

    Args:
        database: The open project database.
        batch_id: The batch to inspect. Hashes must already be computed
            (:func:`compute_hashes_for_batch`); a scan with no hash yet is
            excluded rather than treated as a false match against other
            unhashed scans.

    Returns:
        Groups with more than one member, worst (largest) first. Empty when
        every scan's content is unique - the ordinary case.

    This is **exact-duplicate-file** detection, deliberately distinct from
    Phase 7's duplicate-*candidate-ID* exceptions
    (:mod:`omr_scanner.services.reconciliation`): two different scripts can
    share a roll number without sharing a single byte, and one script
    imported twice by accident shares every byte while (usually) reading the
    same roll number too. Conflating the two would hide the second, much more
    innocent, mistake.
    """
    with database.session() as session:
        rows = session.execute(
            select(BatchScan.content_sha256, BatchScan.scan_id, BatchScan.filename)
            .where(BatchScan.batch_id == batch_id)
            .where(BatchScan.content_sha256 != "")
            .order_by(BatchScan.batch_index)
        ).all()

    by_hash: dict[str, list[tuple[int, str]]] = {}
    for content_hash, scan_id, filename in rows:
        by_hash.setdefault(str(content_hash), []).append((int(scan_id), str(filename)))

    groups = [
        DuplicateGroup(
            content_sha256=digest,
            scan_ids=tuple(scan_id for scan_id, _ in members),
            filenames=tuple(filename for _, filename in members),
        )
        for digest, members in by_hash.items()
        if len(members) > 1
    ]
    groups.sort(key=lambda group: len(group.scan_ids), reverse=True)
    return tuple(groups)


class ScanAvailability(StrEnum):
    """What became of a source scan since it was imported."""

    PRESENT_UNCHANGED = "present_unchanged"
    """The file is where it was, and its content hash still matches."""

    MISSING = "missing"
    """No file exists at the recorded path."""

    CHANGED = "changed"
    """A file exists at the recorded path, but its content hash does not
    match what was recorded at import - it is not the same script."""

    UNVERIFIABLE = "unverifiable"
    """No hash was ever recorded for this scan (an older project, or a
    virtual stress-test source), so nothing can be said beyond "a file
    exists here right now"."""


@dataclass(frozen=True, slots=True)
class SourceAvailability:
    """One scan's availability as of right now.

    Attributes:
        scan_id: The scan checked.
        source_path: Its recorded path.
        availability: The verdict.
    """

    scan_id: int
    source_path: str
    availability: ScanAvailability


def check_availability(
    database: ProjectDatabase, batch_id: str
) -> tuple[SourceAvailability, ...]:
    """Classify every real source file in a batch as present, missing or changed.

    Args:
        database: The open project database.
        batch_id: The batch to check.

    Returns:
        One :class:`SourceAvailability` per real (non-virtual) scan, in
        ``batch_index`` order. Missing and changed files are what
        :mod:`omr_scanner.services.project_health` surfaces before a
        reprocessing run is allowed to start.
    """
    with database.session() as session:
        rows = session.execute(
            select(BatchScan.scan_id, BatchScan.source_path, BatchScan.content_sha256)
            .where(BatchScan.batch_id == batch_id)
            .order_by(BatchScan.batch_index)
        ).all()

    results: list[SourceAvailability] = []
    for scan_id, source_path, stored_hash in rows:
        source_path = str(source_path)
        if is_virtual_source(source_path):
            continue
        path = Path(source_path)
        if not path.is_file():
            verdict = ScanAvailability.MISSING
        elif not stored_hash:
            verdict = ScanAvailability.UNVERIFIABLE
        else:
            current = _safe_hash_file(source_path)
            verdict = (
                ScanAvailability.PRESENT_UNCHANGED
                if current == stored_hash
                else ScanAvailability.CHANGED
            )
        results.append(
            SourceAvailability(
                scan_id=int(scan_id), source_path=source_path, availability=verdict
            )
        )
    return tuple(results)


class RelinkError(Exception):
    """A proposed replacement path could not be accepted for a scan."""


def relink_scan(database: ProjectDatabase, scan_id: int, new_path: Path) -> None:
    """Point a scan at a replacement file, once its identity is verified.

    Args:
        database: The open project database.
        scan_id: The scan being relinked (its source file was moved).
        new_path: The candidate replacement location.

    Raises:
        RelinkError: ``new_path`` does not exist, or its content hash does
            not match the hash recorded when the scan was originally
            imported - accepting it anyway would silently attach a different
            script's identity to this candidate's record, which is exactly
            the mistake content hashing exists to prevent.

    A scan that was never hashed (an older project, migrated forward) cannot
    be relinked by identity at all; relinking it is refused until it has been
    hashed once against its current, presumably-still-correct path.
    """
    with database.session() as session:
        row = session.execute(
            select(BatchScan.content_sha256).where(BatchScan.scan_id == scan_id)
        ).first()
    if row is None:
        raise RelinkError(f"No scan with id {scan_id} exists in this project.")
    stored_hash = str(row[0])
    if not stored_hash:
        raise RelinkError(
            "This scan has no recorded content hash to verify against. "
            "Its original file must be re-hashed before it can be relinked."
        )
    if not new_path.is_file():
        raise RelinkError(f"'{new_path}' does not exist.")
    try:
        candidate_hash = hash_file(new_path)
    except OSError as exc:
        raise RelinkError(f"'{new_path}' could not be read: {exc}") from exc
    if candidate_hash != stored_hash:
        raise RelinkError(
            f"'{new_path.name}' is not the same file that was originally imported "
            "for this scan (its content does not match). Relinking was refused."
        )
    with database.session() as session:
        session.execute(
            update(BatchScan)
            .where(BatchScan.scan_id == scan_id)
            .values(source_path=str(new_path), filename=new_path.name)
        )


__all__ = [
    "HASH_ALGORITHM",
    "STRESS_SOURCE_PREFIX",
    "DuplicateGroup",
    "RelinkError",
    "ScanAvailability",
    "SourceAvailability",
    "check_availability",
    "compute_hashes_for_batch",
    "duplicate_groups",
    "hash_bytes",
    "hash_file",
    "is_virtual_source",
    "relink_scan",
]
