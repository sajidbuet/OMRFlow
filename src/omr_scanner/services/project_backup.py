"""Project database backups and recovery snapshots (Phase 10, §5).

Purpose:
    Give an operator, and the application itself before a risky operation, a
    verifiably complete copy of the project database to fall back to.

Responsibilities:
    * :func:`create_backup` - a consistent snapshot, taken with SQLite's own
      online backup API, never a raw file copy.
    * :func:`list_backups` - what backups exist, distinguishing a complete
      one from one a crash interrupted mid-copy.
    * :func:`verify_backup` - re-hash a backup file against its manifest.
    * :func:`restore_backup` - copy a backup out to a new location, never
      overwriting an existing file.

What does NOT belong here:
    * Deciding *when* a backup is mandatory (before a migration, before a
      recovery attempt) - that is each caller's job; this module only knows
      how to take one and account for it.
    * Repairing a damaged database. A backup exists so a repair attempt has
      something to fall back to if it goes wrong -
      :mod:`omr_scanner.services.project_health` is where damage is detected,
      and this phase deliberately builds no "one-click repair" (see the
      phase brief §7).

Why the online backup API, not ``shutil.copy``:
    SQLite is a live, being-written file. Copying its bytes with an ordinary
    file copy can capture a write half-finished - a page written but its
    journal not yet applied - producing a backup that looks present but is
    actually torn. ``sqlite3.Connection.backup()`` uses SQLite's own backup
    protocol, which takes a read lock for the duration and copies a
    transactionally consistent snapshot, whatever else might be writing to
    the source at the same time.

Why the manifest is written *last*:
    A backup that a crash interrupts mid-copy must never be mistaken for a
    valid one. The ``.sqlite3`` file is written first (and may therefore
    exist, incomplete, after a crash); the ``.sqlite3.json`` manifest is
    written only once the copy has finished and been hashed, so **the
    manifest's mere existence is the "this backup is complete" signal** -
    :func:`list_backups` treats a ``.sqlite3`` file with no manifest as an
    incomplete backup, not a corrupt one, and reports it separately.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from omr_scanner import __version__
from omr_scanner.errors import OMRScannerError
from omr_scanner.services.scan_provenance import hash_file

_LOGGER = logging.getLogger(__name__)

BACKUP_DIR_NAME = "backups"
"""Sub-directory of a project's root where backups live."""

BACKUP_SUFFIX = ".sqlite3"
MANIFEST_SUFFIX = ".sqlite3.json"


class BackupError(OMRScannerError):
    """A backup could not be created, verified or restored."""


def _sanitise_reason(reason: str) -> str:
    """Turn free text into a safe filename fragment; never empty."""
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in reason.strip())
    cleaned = cleaned.strip("_")
    return cleaned or "backup"


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return a path in ``directory`` guaranteed not to already exist.

    Mirrors :func:`omr_scanner.services.report_store.unique_output_path`'s
    "never overwrite" contract for the same reason: a backup silently
    replacing an earlier one defeats the entire point of taking it.
    """
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    index = 1
    while True:
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """What one backup is, and proof it finished.

    Attributes:
        backup_file: File name of the ``.sqlite3`` copy (relative, inside the
            backups directory).
        created_at: When the backup finished (UTC, ISO 8601).
        app_version: OMRFlow version that created it.
        schema_version: The source database's schema version at that moment.
        reason: Why it was taken (``"before-migration"``, ``"manual"``, an
            operator-supplied note, ...).
        source_size_bytes: Size of the source database at backup time.
        backup_sha256: Content hash of the finished backup file, checked by
            :func:`verify_backup`.
    """

    backup_file: str
    created_at: str
    app_version: str
    schema_version: int
    reason: str
    source_size_bytes: int
    backup_sha256: str

    def to_json(self) -> str:
        """Serialise for the sidecar manifest file."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, payload: str) -> BackupManifest:
        """Parse a sidecar manifest file's contents."""
        return cls(**json.loads(payload))


def create_backup(
    database_path: Path, backups_dir: Path, *, reason: str, schema_version: int
) -> BackupManifest:
    """Take a consistent, verified backup of a project database.

    Args:
        database_path: The live SQLite file to copy.
        backups_dir: Where backups for this project live. Created if absent.
        reason: Free text naming why ("before-migration", "manual", ...);
            becomes part of the backup's file name.
        schema_version: The schema version to record, from the caller's own
            already-open connection - this function opens its own read-only
            connection to the source only for the copy itself.

    Returns:
        The manifest, already written beside the backup.

    Raises:
        BackupError: The source could not be read or the copy could not be
            written.
    """
    if not database_path.is_file():
        raise BackupError(
            f"Cannot back up: {database_path} does not exist",
            user_message="The project database could not be found to back up.",
        )
    backups_dir.mkdir(parents=True, exist_ok=True)
    moment = datetime.now(UTC)
    stem = f"backup_{moment.strftime('%Y%m%dT%H%M%SZ')}_{_sanitise_reason(reason)}"
    destination = _unique_path(backups_dir, stem, BACKUP_SUFFIX)

    try:
        source_conn = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
        try:
            dest_conn = sqlite3.connect(str(destination))
            try:
                source_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            source_conn.close()
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(
            f"Backup of {database_path} failed: {exc}",
            user_message="The project database could not be backed up.",
        ) from exc

    manifest = BackupManifest(
        backup_file=destination.name,
        created_at=moment.isoformat(),
        app_version=__version__,
        schema_version=schema_version,
        reason=reason,
        source_size_bytes=database_path.stat().st_size,
        backup_sha256=hash_file(destination),
    )
    # Written last, deliberately - see the module docstring.
    manifest_path = destination.with_name(destination.name + ".json")
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    _LOGGER.info("Created project backup %s (%s)", destination.name, reason)
    return manifest


@dataclass(frozen=True, slots=True)
class BackupEntry:
    """One backup found on disk, complete or not.

    Attributes:
        manifest: Present only for a complete backup.
        backup_path: Where the ``.sqlite3`` file is.
        is_complete: Whether a manifest was found beside it.
    """

    backup_path: Path
    manifest: BackupManifest | None
    is_complete: bool


def list_backups(backups_dir: Path) -> tuple[BackupEntry, ...]:
    """Return every backup found, newest first, complete or not.

    An incomplete entry (``manifest is None``) is a ``.sqlite3`` file with no
    ``.sqlite3.json`` beside it - almost always the result of a crash during
    :func:`create_backup` itself, since the manifest is the last thing that
    function writes. It is reported, not hidden, so an operator can see (and
    delete) a half-finished backup rather than mistake it for a working one.
    """
    if not backups_dir.is_dir():
        return ()
    entries: list[BackupEntry] = []
    for candidate in sorted(backups_dir.glob(f"*{BACKUP_SUFFIX}"), reverse=True):
        manifest_path = candidate.with_name(candidate.name + ".json")
        manifest: BackupManifest | None = None
        if manifest_path.is_file():
            try:
                manifest = BackupManifest.from_json(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, KeyError):
                manifest = None
        entries.append(
            BackupEntry(backup_path=candidate, manifest=manifest, is_complete=manifest is not None)
        )
    return tuple(entries)


def verify_backup(entry: BackupEntry) -> bool:
    """Re-hash a backup file and compare it against its own manifest.

    Returns:
        ``True`` when the file's current content hash matches the hash
        recorded when it was created, or when the entry has no manifest to
        verify against at all is reported as ``False`` (never "assume OK").
    """
    if entry.manifest is None:
        return False
    try:
        current = hash_file(entry.backup_path)
    except OSError:
        return False
    return current == entry.manifest.backup_sha256


def restore_backup(entry: BackupEntry, destination: Path) -> Path:
    """Copy a backup out to ``destination``, never overwriting an existing file.

    Args:
        entry: The backup to restore. Must be complete
            (:attr:`BackupEntry.is_complete`).
        destination: Where to copy it. If this exact path exists, a numeric
            suffix is used instead - restoring a backup must never silently
            destroy whatever is already there.

    Returns:
        The path actually written.

    Raises:
        BackupError: The entry is incomplete, or its content no longer
            matches its manifest (§5: "ensure an incomplete backup cannot
            masquerade as a valid completed backup" extends to a backup that
            has since been damaged, too).
    """
    if not entry.is_complete:
        raise BackupError(
            f"{entry.backup_path.name} has no manifest and cannot be trusted as a "
            "complete backup",
            user_message="This backup is incomplete and cannot be restored.",
        )
    if not verify_backup(entry):
        raise BackupError(
            f"{entry.backup_path.name} failed hash verification",
            user_message="This backup's content no longer matches its recorded hash "
            "and was not restored.",
        )
    target = _unique_path(destination.parent, destination.stem, destination.suffix)
    shutil.copy2(entry.backup_path, target)
    return target


__all__ = [
    "BACKUP_DIR_NAME",
    "BackupEntry",
    "BackupError",
    "BackupManifest",
    "create_backup",
    "list_backups",
    "restore_backup",
    "verify_backup",
]
