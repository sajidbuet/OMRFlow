"""Measure what reopening a crashed project costs at scale (0.1.1 phase 3, ADR-0006).

Builds a project holding one batch of ``--sheets`` rows left exactly as a
killed Scan run leaves it - ``--committed`` of them committed (result and
conflicts), the rest ``queued``, the batch ``running`` - by recording copies
of a few real recognition results under distinct source paths, then times:

* ``recover_on_open`` (stale rows back, every stored result re-derived from
  ``result_json``, batch-scope passes, status) - and a second, no-op run;
* ``restore_targets`` and ``scan_progress`` (what Scan shows before Resume);
* ``count_conflicts`` and the first Resolve page of ``list_conflicts``;
* ``completed_results`` + ``scan_paths``, which ``ScanPage.adopt_batch``
  loads to fill the scan list (the one unbounded read on reopen).

Usage::

    python scripts/benchmark_recovery.py --sheets 10000 --committed 6000
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT):
    if str(entry) not in sys.path:  # pragma: no cover - script bootstrap
        sys.path.insert(0, str(entry))

from omr_scanner.database.models import BatchStatus  # noqa: E402
from omr_scanner.evaluation import stress_dataset  # noqa: E402
from omr_scanner.services import (  # noqa: E402
    RecognitionOptions,
    batch_store,
    create_project,
    review_store,
    scan_recovery,
)
from omr_scanner.services.batch_processor import (  # noqa: E402
    BatchOptions,
    ProcessedScan,
    process_batch,
)


def _timed(
    label: str, results: dict[str, Any], function: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    started = time.perf_counter()
    value = function(*args, **kwargs)
    results[label] = round(time.perf_counter() - started, 3)
    return value


def main(argv: list[str] | None = None) -> int:
    """Build the crashed project, time each reopen step, print JSON."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sheets", type=int, default=10_000)
    parser.add_argument("--committed", type=int, default=6_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--writer",
        choices=("scan", "legacy"),
        default="scan",
        help="'scan': this build's Scan stage; 'legacy': results without conflicts",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    arguments = parser.parse_args(argv)

    from tests.conftest import build_answer_sheet_template

    template = build_answer_sheet_template()
    with tempfile.TemporaryDirectory(prefix="omrflow_recovery_") as scratch:
        root = Path(scratch)
        spec = stress_dataset.StressDatasetSpec(seed=arguments.seed, sheet_count=40)
        samples = []
        for index in range(40):
            sheet = stress_dataset.render_sheet_for_index(spec, template, index)
            path = root / f"sample_{index:02d}.png"
            path.write_bytes(sheet.malformed_bytes or sheet.png_bytes or b"")
            samples.append(path)
        # The Scan stage's own engine options: per-bubble measurements are not
        # kept (ScanPage._engine_options), so stored results are realistic.
        options = BatchOptions(recognition=RecognitionOptions(keep_bubble_measurements=False))
        read = list(process_batch(samples, template, options=options, workers=4).processed)

        session = create_project(root, "Recovery Benchmark")
        database = session.database
        paths = [root / "virtual" / f"scan_{index:06d}.png" for index in range(arguments.sheets)]
        results: dict = {"sheets": arguments.sheets, "committed": arguments.committed}
        legacy = arguments.writer == "legacy"
        results["writer"] = arguments.writer
        batch_id = _timed(
            "register_s",
            results,
            batch_store.create_batch,
            database,
            paths,
            identity=batch_store.BatchIdentity.of(template),
            settings={} if legacy else {scan_recovery.WORK_UNIT_SETTING: True},
        )
        batch_store.mark_queued(database, batch_id, paths)
        batch_store.set_batch_status(database, batch_id, BatchStatus.RUNNING)
        # 'legacy' writes what a build before phase 3 wrote: results only.
        recorder = batch_store.BatchRecorder(
            database=database,
            batch_id=batch_id,
            template=None if legacy else template,
            flush_every=200,
        )
        started = time.perf_counter()
        for index in range(arguments.committed):
            source = read[index % len(read)]
            recorder.record(
                ProcessedScan(result=replace(source.result, source_path=paths[index]))
            )
        recorder.flush()
        results["record_committed_s"] = round(time.perf_counter() - started, 3)

        templates = [template]
        report = _timed(
            "recover_on_open_s", results, scan_recovery.recover_on_open, database,
            templates=templates,
        )
        results["resynced"] = sum(item.sheets_resynced for item in report.batches)
        results["returned_to_pending"] = report.scans_returned
        _timed(
            "recover_on_open_again_s", results, scan_recovery.recover_on_open, database,
            templates=templates,
        )
        _timed("restore_targets_s", results, scan_recovery.restore_targets, database)
        _timed("scan_progress_s", results, scan_recovery.scan_progress, database, batch_id)
        counts = _timed(
            "count_conflicts_s", results, review_store.count_conflicts, database, batch_id
        )
        results["conflicts"] = counts.total
        _timed(
            "resolve_first_page_s", results, review_store.list_conflicts, database, batch_id,
            limit=500,
        )
        _timed(
            "adopt_completed_results_s", results, batch_store.completed_results, database, batch_id
        )
        _timed("adopt_scan_paths_s", results, batch_store.scan_paths, database, batch_id)
        results["database_mib"] = round(database.path.stat().st_size / 1024 / 1024, 1)
        session.close()

    print(json.dumps(results, indent=2))
    if arguments.json_out is not None:
        arguments.json_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
