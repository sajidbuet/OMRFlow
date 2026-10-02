"""Renamed copies of a scan session's effective sheets (0.1.1 phase 4).

Purpose:
    The Scan stage can copy each sheet under its roll number *while a run reads
    it* - per run, and before any review. For a session of several batches the
    authoritative set is the session's **effective** sheet set, after review:
    rescans replace their originals, rejected, excluded, deferred, duplicate
    and superseded sheets are left out, and a corrected Student ID names the
    file. This module writes that set, once, into a folder.

Rules:
    * The same naming rules as the per-run copy
      (:class:`~omr_scanner.services.filename_manager.FilenameAllocator`): the
      effective Student ID, ``_a``, ``_b`` for a later sheet claiming the same
      one, and ``UNRESOLVED_###`` for a sheet whose ID is not trusted (an open
      identifier conflict, a ``?`` or ``_``, or a sheet that could not be read).
    * Session order: batch by batch (oldest first), then batch position - so
      re-exporting the same session gives the same names.
    * Nothing is ever overwritten, and nothing in the project changes: this
      writes files, not records. A source image no longer on disk is listed,
      not silently skipped.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from omr_scanner.database.models import BatchScan
from omr_scanner.services import session_population
from omr_scanner.services.filename_manager import FilenameAllocator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase


@dataclass(frozen=True, slots=True)
class RenamedCopy:
    """One sheet written under its new name."""

    scan_id: int
    source: Path
    name: str


@dataclass(frozen=True, slots=True)
class RenamedExport:
    """What one export wrote.

    Attributes:
        output_dir: Where the copies are.
        copies: Every sheet written, in session order.
        missing_sources: File names of effective sheets whose image is no
            longer on disk - not copied, and said so.
    """

    output_dir: Path
    copies: tuple[RenamedCopy, ...]
    missing_sources: tuple[str, ...] = ()

    @property
    def unresolved(self) -> int:
        """How many copies are named ``UNRESOLVED_###``."""
        return sum(1 for item in self.copies if item.name.upper().startswith("UNRESOLVED"))


def _trusted(value: str, *, unresolved: bool) -> bool:
    return bool(value) and not unresolved and "?" not in value and "_" not in value


def export_renamed_copies(
    database: ProjectDatabase, scan_session_id: str, output_dir: Path
) -> RenamedExport:
    """Copy every effective sheet of ``scan_session_id`` into ``output_dir``, renamed."""
    population = session_population.session_population(database, scan_session_id)
    effective = population.effective
    identifiers = session_population.effective_identifiers(database, population, effective)
    order = {batch: index for index, batch in enumerate(population.batch_ids)}
    with database.session() as session:
        rows = [
            (int(scan_id), str(batch), int(index), str(path))
            for scan_id, batch, index, path in session.execute(
                select(
                    BatchScan.scan_id, BatchScan.batch_id, BatchScan.batch_index,
                    BatchScan.source_path,
                ).where(BatchScan.scan_id.in_(sorted(effective)))
            ).all()
        ] if effective else []
    rows.sort(key=lambda item: (order.get(item[1], len(order)), item[2], item[0]))
    output_dir.mkdir(parents=True, exist_ok=True)
    allocator = FilenameAllocator(output_dir)
    copies: list[RenamedCopy] = []
    missing: list[str] = []
    for scan_id, _batch, _index, source_path in rows:
        source = Path(source_path)
        if not source.is_file():
            missing.append(source.name)
            continue
        found = identifiers.get(scan_id)
        stem = (
            found.value
            if found is not None and _trusted(found.value, unresolved=found.unresolved)
            else None
        )
        name = allocator.allocate(stem, source.suffix)
        destination = output_dir / name
        if destination.exists():  # the allocator saw the folder; never replace
            raise FileExistsError(f"Refusing to overwrite existing file '{destination}'")
        shutil.copy2(source, destination)
        copies.append(RenamedCopy(scan_id=scan_id, source=source, name=name))
    return RenamedExport(
        output_dir=output_dir, copies=tuple(copies), missing_sources=tuple(missing)
    )


__all__ = ["RenamedCopy", "RenamedExport", "export_renamed_copies"]
