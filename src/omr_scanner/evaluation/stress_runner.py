"""Run a stress dataset through the real production pipeline (Phase 10, §19-§25).

Purpose:
    Turn a :class:`~omr_scanner.evaluation.stress_dataset.StressDatasetSpec`
    into an ordinary, durable batch and process it with the exact same
    :mod:`omr_scanner.services.batch_processor` /
    :mod:`omr_scanner.services.parallel_batch` /
    :mod:`omr_scanner.services.batch_store` machinery every real examination
    batch uses - unchanged, not a parallel code path built only for testing.

Responsibilities:
    * :func:`create_stress_batch` - register the batch with virtual source
      paths; nothing is rendered yet.
    * :func:`run_stress_batch` - process whatever remains
      (:func:`~omr_scanner.services.batch_store.resumable_scans`), in
      disk-bounded chunks.

What does NOT belong here:
    * Any change to the real pipeline. This module is a caller of
      :mod:`omr_scanner.services.batch_processor`, exactly like the Scan
      page is - see the module docstring's "Why this lives above services,
      not inside it" for why that boundary is deliberate.
    * Telemetry sampling (:mod:`omr_scanner.services.telemetry`) or the
      command-line entry point (:mod:`omr_scanner.tools.benchmark_stress`),
      which both build on this module rather than living inside it.

Why this lives in ``evaluation``, not ``services``:
    ``docs/ARCHITECTURE.md`` places the QA/benchmark layer
    (:mod:`omr_scanner.evaluation`) *above* :mod:`omr_scanner.services`: it
    may call any service, but no service may call it back - the same
    direction :mod:`omr_scanner.evaluation.benchmark` already depends on
    :mod:`omr_scanner.services.recognition_service` in. This module needs
    both :mod:`omr_scanner.evaluation.stress_dataset` (the sheet generator)
    and several ``services`` modules (batch orchestration and persistence);
    putting it in ``services`` would mean a core service module importing
    the QA layer, which
    ``tests/unit/test_architecture.py``'s own commentary calls out as
    exactly backwards.

How the "no 100,000 physical files" requirement and "reuse the unmodified
pipeline" requirement are both satisfied at once:
    Neither :mod:`~omr_scanner.services.batch_processor` nor
    :mod:`~omr_scanner.services.parallel_batch` is taught anything about
    virtual sources - modifying either to special-case a synthetic path
    would be exactly the kind of change §19 warns against risking (a
    "stress test" that quietly exercises a different code path than a real
    batch does). Instead, :func:`run_stress_batch` renders one bounded
    *chunk* of sheets (:data:`MATERIALIZE_CHUNK_SIZE`) to real temporary
    PNG files immediately before handing that chunk to the unmodified
    :func:`~omr_scanner.services.batch_processor.process_batch`, and deletes
    them immediately after. Peak disk usage is bounded by the chunk size,
    never by the run's total sheet count, while every sheet that reaches
    recognition is a real file read from real bytes, exactly as a scanned
    page would be.

    The one seam this does require: a chunk's temporary paths are not the
    identity a resumed run addresses a sheet by (a temp file is deleted
    before the next chunk even starts). :func:`run_stress_batch` therefore
    wraps the caller's ``on_result`` to rewrite each finished
    :class:`~omr_scanner.services.batch_processor.ProcessedScan`'s
    ``result.source_path`` from the temporary path back to the sheet's
    permanent virtual identity
    (:func:`~omr_scanner.evaluation.stress_dataset.virtual_source_path`)
    before it reaches :mod:`omr_scanner.services.batch_store` - which is the
    one place that identity has to be stable, since it is the primary key
    :func:`~omr_scanner.services.batch_store.record_results` matches
    against.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from omr_scanner.evaluation.stress_dataset import (
    StressDatasetSpec,
    parse_virtual_source_path,
    render_sheet_for_index,
    virtual_source_path,
)
from omr_scanner.services import batch_processor, batch_store
from omr_scanner.services.batch_processor import BatchOptions, BatchReport, ProcessedScan
from omr_scanner.services.filename_manager import FilenameAllocator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services.batch_processor import BatchProgress

_LOGGER = logging.getLogger(__name__)

MATERIALIZE_CHUNK_SIZE = 2000
"""Sheets rendered to real temporary files at once (§19: "avoid unnecessarily
consuming hundreds of gigabytes"). Bounds peak disk usage for a stress run to
this many synthetic images, whatever the run's total sheet count - a
100,000-sheet run never has more than this many physical files on disk at
any single moment."""


def create_stress_batch(
    database: ProjectDatabase,
    spec: StressDatasetSpec,
    template: OmrTemplate,
    *,
    source_label: str = "stress-test",
    batch_id: str | None = None,
) -> str:
    """Register a stress run as an ordinary batch, with virtual source paths.

    Args:
        database: The open project database.
        spec: The stress dataset to register.
        template: The template every sheet will be read with.
        source_label: Recorded in the batch's settings, for the benchmark
            report and the batch list.
        batch_id: Override the generated id; for tests and for resuming a
            specific known batch.

    Returns:
        The batch id, exactly as
        :func:`~omr_scanner.services.batch_store.create_batch` returns for
        any other batch.

    Nothing is rendered here. Every one of ``spec.sheet_count`` rows is
    registered ``pending`` with a virtual, never-yet-a-file
    :func:`~omr_scanner.evaluation.stress_dataset.virtual_source_path` -
    :func:`~omr_scanner.services.batch_store.create_batch`'s own per-file
    ``stat()`` call already tolerates a path that does not exist (its
    documented behaviour, unrelated to this phase), which is exactly what
    every one of these paths is until :func:`run_stress_batch` renders it.
    """
    paths = [Path(virtual_source_path(spec, index)) for index in range(spec.sheet_count)]
    identity = batch_store.BatchIdentity.of(template)
    return batch_store.create_batch(
        database,
        paths,
        identity=identity,
        settings={
            "stress_source": source_label,
            "stress_seed": spec.seed,
            "stress_sheet_count": spec.sheet_count,
        },
        batch_id=batch_id,
    )


@dataclass(slots=True)
class _MaterializedChunk:
    """One chunk of stress sheets, rendered to real files on disk."""

    temp_dir: Path
    virtual_by_real: dict[Path, Path]

    def cleanup(self) -> None:
        """Remove every file this chunk wrote."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)


def _materialize_chunk(
    spec: StressDatasetSpec,
    template: OmrTemplate,
    virtual_paths: list[Path],
    scratch_root: Path,
) -> _MaterializedChunk:
    """Render one bounded chunk of virtual sheets to real temporary files."""
    temp_dir = Path(tempfile.mkdtemp(prefix="omrflow_stress_", dir=str(scratch_root)))
    virtual_by_real: dict[Path, Path] = {}
    for virtual_path in virtual_paths:
        _seed, index = parse_virtual_source_path(str(virtual_path))
        rendered = render_sheet_for_index(spec, template, index)
        real_path = temp_dir / f"stress_{index:07d}.png"
        payload = rendered.malformed_bytes if rendered.is_malformed else rendered.png_bytes
        if payload is None:  # pragma: no cover - RenderedStressSheet always sets one
            raise RuntimeError(f"Stress sheet {index} rendered no bytes at all")
        real_path.write_bytes(payload)
        virtual_by_real[real_path] = virtual_path
    return _MaterializedChunk(temp_dir=temp_dir, virtual_by_real=virtual_by_real)


def run_stress_batch(
    database: ProjectDatabase,
    batch_id: str,
    template: OmrTemplate,
    spec: StressDatasetSpec,
    *,
    workers: int,
    opencv_threads: int = 1,
    worker_recycle_after: int = 0,
    scratch_root: Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_result: Callable[[ProcessedScan], None] | None = None,
    on_progress: Callable[[BatchProgress], None] | None = None,
    on_submit: Callable[[tuple[int, ...]], None] | None = None,
) -> list[BatchReport]:
    """Process every sheet of a stress batch not already durably completed.

    Args:
        database: The open project database.
        batch_id: The batch to process, from :func:`create_stress_batch`.
        template: The template to read every sheet with.
        spec: The same spec the batch was created with - sheet ``N``
            regenerates identically only when this matches.
        workers: How many worker processes to use, unchanged semantics from
            every other batch (``1`` runs in this thread with no pool).
        opencv_threads: Forwarded to
            :class:`~omr_scanner.services.batch_processor.BatchOptions`.
        worker_recycle_after: Forwarded to
            :class:`~omr_scanner.services.batch_processor.BatchOptions`.
        scratch_root: Parent directory for the per-chunk temporary folders.
            Defaults to the system temp directory.
        should_cancel: Polled between chunks *and* forwarded into
            :func:`~omr_scanner.services.batch_processor.process_batch` for
            polling within one - cancellation remains as responsive as any
            other batch.
        on_result: Called for each finished sheet, with its
            ``result.source_path`` already rewritten to the sheet's
            permanent virtual identity. Pass a
            :class:`~omr_scanner.services.batch_store.BatchRecorder`'s
            ``.record`` to persist durably, exactly as any other batch does.
        on_progress: Forwarded as-is; progress events during a chunk report
            the chunk's real temporary paths, which is cosmetic only (they
            exist for the whole duration of that chunk's processing) and is
            reconciled to the permanent identity for every completion event
            by ``on_result``, which is what any consumer keeping durable or
            authoritative state should read from.
        on_submit: Called with each chunk's ``batch_index`` values immediately
            before that chunk is handed to recognition. This is the *only*
            way to know which sheets a run actually submitted, as opposed to
            which ones it finished: the Phase 10 qualification's
            ``previously_completed_jobs_rescheduled_for_recognition``
            assertion has to be measured, not inferred from final row counts,
            and a resumed run that wrongly re-read an already-committed sheet
            would still produce a correct-looking final database.

    Returns:
        One :class:`~omr_scanner.services.batch_processor.BatchReport` per
        chunk processed - callers that want one aggregate view should sum
        the counts across the list, since a genuinely 100,000-sheet run
        processes far more chunks than fit usefully in one report.

    This is what makes the mandatory kill-and-resume acceptance test
    (§30/§31) work by construction rather than by a special case: calling
    this function again after an interruption asks the database what is
    left, exactly as resuming a real batch does, and only ever
    (re-)materializes and (re-)processes those sheets.

    **The database, not a Python list, is the authoritative queue.** Each
    chunk's worth of work is fetched from
    :func:`~omr_scanner.services.batch_store.resumable_scans_window` -
    bounded to :data:`MATERIALIZE_CHUNK_SIZE` rows - immediately before it
    is processed, rather than the whole remaining-work list being pulled
    into memory once at the start. A 100,000-sheet run therefore never
    holds more than one chunk's worth of paths (2,000, by default) in
    Python at any moment, matching the same bound
    :func:`_materialize_chunk` already applies to the *files* it renders.
    """
    root = scratch_root if scratch_root is not None else Path(tempfile.gettempdir())
    allocator = FilenameAllocator()
    options = BatchOptions(opencv_threads=opencv_threads, worker_recycle_after=worker_recycle_after)
    reports: list[BatchReport] = []
    cursor = -1

    while True:
        window = batch_store.resumable_scans_window(
            database, batch_id, after_batch_index=cursor, limit=MATERIALIZE_CHUNK_SIZE
        )
        if not window:
            break
        cursor = window[-1][0]
        chunk_virtual = [path for _index, path in window]
        if on_submit is not None:
            on_submit(tuple(index for index, _path in window))

        materialized = _materialize_chunk(spec, template, chunk_virtual, root)
        try:
            real_paths = list(materialized.virtual_by_real.keys())
            wrapped_result = _wrap_on_result(on_result, materialized.virtual_by_real)
            report = batch_processor.process_batch(
                real_paths,
                template,
                options=options,
                allocator=allocator,
                on_progress=on_progress,
                on_result=wrapped_result,
                should_cancel=should_cancel,
                workers=workers,
            )
            reports.append(report)
        finally:
            materialized.cleanup()

        if should_cancel is not None and should_cancel():
            _LOGGER.info("Stress run %s cancelled after %d chunk(s)", batch_id, len(reports))
            break

    return reports


def _wrap_on_result(
    on_result: Callable[[ProcessedScan], None] | None,
    virtual_by_real: dict[Path, Path],
) -> Callable[[ProcessedScan], None] | None:
    """Rewrite a finished sheet's temp path back to its permanent identity."""
    if on_result is None:
        return None

    def _forward(processed: ProcessedScan) -> None:
        virtual_path = virtual_by_real[processed.result.source_path]
        corrected = replace(
            processed, result=replace(processed.result, source_path=virtual_path)
        )
        on_result(corrected)

    return _forward


__all__ = [
    "MATERIALIZE_CHUNK_SIZE",
    "create_stress_batch",
    "run_stress_batch",
]
