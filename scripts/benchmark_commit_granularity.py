"""Measure what each Scan commit granularity costs (0.1.1 phase 3, ADR-0006).

Purpose:
    Decide - from numbers, not assumption - whether the Scan stage can commit
    every sheet's durable work unit (recognition result + its own conflicts)
    in a transaction of its own, or whether results must keep being grouped.

What it measures, for each granularity (``--modes``, sheets per commit):

* **Writer-only cost.** Real recognition results (read once, up front, from
  deterministic synthetic sheets with the stress dataset's mix of blanks,
  double marks, duplicates and malformed files) are replayed through
  :class:`~omr_scanner.services.batch_store.BatchRecorder` with the template
  set, exactly as the Scan stage records them. Per-commit latency (mean, p95,
  max), total writer seconds and the writer-limited ceiling in sheets/s.
* **End to end.** The same sheets recognised again by the real pool with the
  recorder attached, as the Scan stage runs them: sheets/s.
* **Visible latency.** Seconds from a result reaching the coordinator to its
  commit returning - how long a finished sheet is shown as in flight.
* **Lock behaviour.** A reader thread runs the Scan page's own count query
  every 50 ms throughout, as a second connection would; its worst wait and
  any ``database is locked`` errors are recorded.
* **Size.** Database growth per mode.

Why a script and not a test:
    Throughput depends on the disk and on whatever else the machine is doing.
    This prints what it measured; the decision is recorded in
    ``development/releases/0.1.1-alpha.0/PHASE_C_HANDOFF.md``.

Usage::

    python scripts/benchmark_commit_granularity.py
    python scripts/benchmark_commit_granularity.py --sheets 600 --workers 8 --end-to-end-workers 4,8
    python scripts/benchmark_commit_granularity.py --project-root D:/share
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT / "src") not in sys.path:  # pragma: no cover - script bootstrap
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from omr_scanner.evaluation import stress_dataset  # noqa: E402
from omr_scanner.services import (  # noqa: E402
    RecognitionOptions,
    batch_store,
    create_project,
)
from omr_scanner.services.batch_processor import (  # noqa: E402
    BatchOptions,
    ProcessedScan,
    process_batch,
)
from omr_scanner.services.template_service import load_template  # noqa: E402

ADAPTIVE = "adaptive"
SCAN_OPTIONS = BatchOptions(recognition=RecognitionOptions(keep_bubble_measurements=False))
"""The Scan stage's engine options: per-bubble measurements are not kept
(``ScanPage._engine_options``), so stored results have their real size."""


def _default_template() -> object:
    """The suite's synthetic answer sheet.

    Not ``examples/templates/100_question_4_choice_example.omrt``: the stress
    generator's sheets fail registration on every page against that template,
    so the measurement would be of failures only.
    """
    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))
    from tests.conftest import build_answer_sheet_template

    return build_answer_sheet_template()


def _render(directory: Path, template: object, count: int, seed: int) -> list[Path]:
    spec = stress_dataset.StressDatasetSpec(seed=seed, sheet_count=count)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        sheet = stress_dataset.render_sheet_for_index(spec, template, index)  # type: ignore[arg-type]
        path = directory / f"sheet_{index:05d}.png"
        path.write_bytes(sheet.malformed_bytes or sheet.png_bytes or b"")
        paths.append(path)
    return paths


class _Reader(threading.Thread):
    """Polls the batch's status counts on its own connection, like a second reader."""

    def __init__(self, database: object, batch_id: str) -> None:
        super().__init__(daemon=True)
        self.database = database
        self.batch_id = batch_id
        self.stop = threading.Event()
        self.waits: list[float] = []
        self.locked = 0

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.perf_counter()
            try:
                batch_store.load_summary(self.database, self.batch_id)  # type: ignore[arg-type]
            except Exception as exc:  # pragma: no cover - measured, not expected
                if "locked" in str(exc):
                    self.locked += 1
            self.waits.append(time.perf_counter() - started)
            time.sleep(0.05)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def _replay(
    project_root: Path, template: object, outcomes: list[ProcessedScan], every: int
) -> dict:
    """Writer-only: replay finished sheets through the recorder, timing each commit."""
    session = create_project(project_root, f"commit-{every}")
    database = session.database
    try:
        size_before = database.path.stat().st_size
        batch_id = batch_store.create_batch(
            database,
            [item.source_path for item in outcomes],
            identity=batch_store.BatchIdentity.of(template),  # type: ignore[arg-type]
        )
        latencies: list[float] = []
        recorder = batch_store.BatchRecorder(
            database=database,
            batch_id=batch_id,
            flush_every=every,
            flush_interval=3600.0,
            template=template,  # type: ignore[arg-type]
        )
        original = recorder.flush

        def timed_flush() -> bool:
            started = time.perf_counter()
            ok = original()
            if recorder.persisted:
                latencies.append(time.perf_counter() - started)
            return ok

        recorder.flush = timed_flush  # type: ignore[method-assign]
        reader = _Reader(database, batch_id)
        reader.start()
        started = time.perf_counter()
        for item in outcomes:
            recorder.record(item)
        recorder.flush()
        writer_seconds = time.perf_counter() - started
        reader.stop.set()
        reader.join()
        size_after = database.path.stat().st_size
        return {
            "sheets_per_commit": every,
            "commits": len(latencies),
            "writer_seconds": round(writer_seconds, 3),
            "writer_ceiling_sheets_per_s": round(len(outcomes) / writer_seconds, 1),
            "commit_ms_mean": round(statistics.mean(latencies) * 1000, 2) if latencies else 0,
            "commit_ms_p95": round(_percentile(latencies, 0.95) * 1000, 2),
            "commit_ms_max": round(max(latencies, default=0) * 1000, 2),
            "per_sheet_write_ms": round(writer_seconds / len(outcomes) * 1000, 2),
            "reader_wait_ms_max": round(max(reader.waits, default=0) * 1000, 2),
            "reader_locked_errors": reader.locked,
            "db_growth_kib": round((size_after - size_before) / 1024, 1),
        }
    finally:
        session.close()


def _end_to_end(
    project_root: Path, template: object, paths: list[Path], mode: str, workers: int
) -> dict:
    """Recognise and record, as the Scan stage does: sheets/s and visible latency."""
    session = create_project(project_root, f"e2e-{mode}-{workers}")
    adaptive = mode == ADAPTIVE
    database = session.database
    try:
        batch_id = batch_store.create_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template)  # type: ignore[arg-type]
        )
        arrived: dict[Path, float] = {}
        visible: list[float] = []

        def committed(items: object) -> None:
            now = time.perf_counter()
            for item in items:  # type: ignore[attr-defined]
                visible.append(now - arrived.pop(item.source_path, now))

        commits: list[int] = []

        def counted(items: object) -> None:
            commits.append(len(items))  # type: ignore[arg-type]
            committed(items)

        recorder = batch_store.BatchRecorder(
            database=database,
            batch_id=batch_id,
            flush_every=batch_store.FLUSH_EVERY_SCANS if adaptive else int(mode),
            template=template,  # type: ignore[arg-type]
            commit_when_idle=adaptive,
            on_commit=counted,
        )

        def on_result(item: ProcessedScan) -> None:
            arrived[item.source_path] = time.perf_counter()
            recorder.record(item)

        started = time.perf_counter()
        process_batch(
            paths, template, options=SCAN_OPTIONS, on_result=on_result, workers=workers  # type: ignore[arg-type]
        )
        recorder.flush()
        elapsed = time.perf_counter() - started
        return {
            "mode": mode,
            "workers": workers,
            "commits": len(commits),
            "mean_sheets_per_commit": round(statistics.mean(commits), 2) if commits else 0,
            "sheets_per_s": round(len(paths) / elapsed, 2),
            "elapsed_s": round(elapsed, 2),
            "visible_latency_ms_mean": round(statistics.mean(visible) * 1000, 1) if visible else 0,
            "visible_latency_ms_max": round(max(visible, default=0) * 1000, 1),
        }
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    """Run the measurements and print them as JSON."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sheets", type=int, default=400)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--modes",
        default="1,5,25,adaptive",
        help="sheets per commit, comma separated; 'adaptive' is the Scan stage's policy",
    )
    parser.add_argument(
        "--end-to-end-workers",
        type=lambda text: [int(item) for item in text.split(",")],
        default=None,
        help="worker counts for the end-to-end runs (default: --workers)",
    )
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--template", type=Path, default=None, help="an .omrt file")
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--skip-end-to-end", action="store_true")
    parser.add_argument("--json-out", type=Path, default=None)
    arguments = parser.parse_args(argv)

    template = (
        load_template(arguments.template) if arguments.template else _default_template()
    )
    modes = [item.strip() for item in arguments.modes.split(",") if item.strip()]
    if arguments.end_to_end_workers is None:
        arguments.end_to_end_workers = [arguments.workers]
    with tempfile.TemporaryDirectory(prefix="omrflow_commit_") as scratch:
        scratch_dir = Path(scratch)
        root = arguments.project_root or scratch_dir / "projects"
        root.mkdir(parents=True, exist_ok=True)
        print(f"Rendering {arguments.sheets} synthetic sheets ...", flush=True)
        paths = _render(scratch_dir / "sheets", template, arguments.sheets, arguments.seed)

        print(f"Recognising once with {arguments.workers} worker(s) ...", flush=True)
        started = time.perf_counter()
        report = process_batch(
            paths, template, options=SCAN_OPTIONS, workers=arguments.workers
        )
        recognition_s = time.perf_counter() - started
        outcomes = list(report.processed)
        mix: dict[str, int] = {}
        for item in outcomes:
            mix[item.outcome.value] = mix.get(item.outcome.value, 0) + 1

        results: dict = {
            "outcome_mix": mix,
            "sheets": arguments.sheets,
            "workers": arguments.workers,
            "recognition_only_sheets_per_s": round(len(paths) / recognition_s, 2),
            "project_root": str(root),
            "writer_only": [],
            "end_to_end": [],
        }
        for mode in modes:
            if mode == ADAPTIVE:
                continue  # a replay is all backlog; only end to end says anything
            print(f"Writer-only replay, {mode} sheet(s) per commit ...", flush=True)
            results["writer_only"].append(_replay(root, template, outcomes, int(mode)))
        if not arguments.skip_end_to_end:
            for workers in arguments.end_to_end_workers:
                for mode in modes:
                    print(f"End to end, {mode}, {workers} worker(s) ...", flush=True)
                    results["end_to_end"].append(
                        _end_to_end(root, template, paths, mode, workers)
                    )

    print(json.dumps(results, indent=2))
    if arguments.json_out is not None:
        arguments.json_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
