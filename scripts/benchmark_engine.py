"""Measure the continuous engine against the finite Scan path (0.1.1 revised phase 6).

Purpose:
    Evidence for ADR-0009 and the phase 6 handoff - not a qualification: how
    fast the engine processes the same real sheets as the existing finite path,
    what a warm pool saves over a pool per unit, and what the coordinator
    itself costs per sheet.

What it runs, on the same deterministic synthetic sheets (stress dataset,
seed 42, decodable sheets only, the suite's synthetic answer sheet, the Scan
stage's engine options):

* **finite** - one batch: register, hash and duplicate-link as *Process All*
  does, recognise with ``process_batch`` on ``--workers`` processes, record
  through the adaptive ``BatchRecorder`` (the work unit), batch-scope pass,
  finalise;
* **engine, pre-registered units** - the same sheets as finite sealed batches
  of ``--unit`` sheets in one session, processed by the engine with a warm
  :class:`~omr_scanner.services.recognition_pool.ProcessRecogniser`;
* **engine, watched intake** - the same sheets written to a real folder,
  stabilised, copy-ingested and registered by phase 5 while the engine runs;
* **pool start-up** - seconds from a fresh pool's first submission to its
  first result, against the steady per-sheet time.

Every run prints sheets/s; repeat with ``--repeat`` (end-to-end throughput on
a desktop varies run to run - phase 3 saw up to 6x - so report ranges).

Usage::

    python scripts/benchmark_engine.py --sheets 400 --workers 8 --unit 50 --repeat 2
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT):
    if str(entry) not in sys.path:  # pragma: no cover - script bootstrap
        sys.path.insert(0, str(entry))


def _sheets(count: int) -> list[bytes]:
    import cv2
    import numpy as np
    from tests.crash.harness import template

    from omr_scanner.evaluation import stress_dataset

    spec = stress_dataset.StressDatasetSpec(seed=42, sheet_count=count * 2)
    out: list[bytes] = []
    index = 0
    while len(out) < count:
        sheet = stress_dataset.render_sheet_for_index(spec, template(), index)
        index += 1
        data = sheet.png_bytes
        if data and cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED) is not None:
            out.append(data)
    return out


def _project(workspace: Path, name: str) -> Any:
    from tests.crash.harness import create_project_with_template

    from omr_scanner.services import open_project

    return open_project(create_project_with_template(workspace, name))


def _options() -> Any:
    from omr_scanner.services.recognition_settings import RecognitionOptions

    return RecognitionOptions(with_preview=False, keep_bubble_measurements=False)


def finite(workspace: Path, sheets: list[bytes], workers: int) -> float:
    """*Process All* on one batch, as the Scan stage runs it; returns sheets/s."""
    from omr_scanner.services import (
        BatchOptions,
        BatchRecorder,
        batch_store,
        intake,
        load_template,
        process_batch,
        resolve_active_template,
        scan_lifecycle,
        scan_recovery,
        scan_sessions,
    )

    project = _project(workspace, "finite")
    try:
        template = load_template(resolve_active_template(project.project))  # type: ignore[arg-type]
        folder = project.root / "in"
        folder.mkdir()
        paths = []
        for index, data in enumerate(sheets):
            path = folder / f"{index:06d}.png"
            path.write_bytes(data)
            paths.append(path)
        database = project.database
        started = time.perf_counter()
        batch_id = scan_sessions.start_batch(
            database, paths, identity=batch_store.BatchIdentity.of(template),
            settings={scan_recovery.WORK_UNIT_SETTING: True},
        )
        batch_store.mark_queued(database, batch_id, paths)
        batch_store.set_batch_status(database, batch_id, batch_store.BatchStatus.RUNNING)
        recorder = BatchRecorder(database=database, batch_id=batch_id, template=template,
                                 commit_when_idle=True)
        intake.record_manual_batch(database, batch_id)
        linked = {
            str(item.path)
            for item in scan_lifecycle.link_exact_duplicates(database, batch_id, paths)
        }
        process_batch([p for p in paths if str(p) not in linked], template,
                      options=BatchOptions(recognition=_options()), on_result=recorder.record,
                      workers=workers)
        recorder.flush()
        scan_recovery.complete_batch_review_state(database, batch_id)
        batch_store.finalise_batch(database, batch_id)
        return len(sheets) / (time.perf_counter() - started)
    finally:
        project.close()


def engine_units(
    workspace: Path, sheets: list[bytes], workers: int, unit: int
) -> tuple[float, int]:
    """The engine over pre-registered sealed units; returns (sheets/s, pools started)."""
    from omr_scanner.domain.processing import EngineLimits
    from omr_scanner.services import (
        batch_store,
        load_template,
        resolve_active_template,
        scan_recovery,
        scan_sessions,
    )
    from omr_scanner.services.continuous_engine import ContinuousEngine
    from omr_scanner.services.recognition_pool import ProcessRecogniser

    project = _project(workspace, "units")
    try:
        template = load_template(resolve_active_template(project.project))  # type: ignore[arg-type]
        database = project.database
        session_id = scan_sessions.create_scan_session(database, name="Bench").scan_session_id
        folder = project.root / "in"
        folder.mkdir()
        started = time.perf_counter()
        for start in range(0, len(sheets), unit):
            paths = []
            for index in range(start, min(len(sheets), start + unit)):
                path = folder / f"{index:06d}.png"
                path.write_bytes(sheets[index])
                paths.append(path)
            batch_id = scan_sessions.start_batch(
                database, paths, identity=batch_store.BatchIdentity.of(template),
                scan_session_id=session_id, settings={scan_recovery.WORK_UNIT_SETTING: True},
            )
            scan_sessions.seal_batch(database, batch_id)
        recogniser = ProcessRecogniser(template, workers=workers, options=_options())
        engine = ContinuousEngine(database, scan_session_id=session_id, template=template,
                                  recogniser=recogniser, limits=EngineLimits.for_workers(workers))
        engine.start()
        while True:
            report = engine.step(wait=0.05)
            if report.idle and not engine.in_flight and engine.status().caught_up:
                break
        engine.shutdown()
        return len(sheets) / (time.perf_counter() - started), recogniser.pools_started
    finally:
        project.close()


def engine_intake(workspace: Path, sheets: list[bytes], workers: int, unit: int) -> float:
    """The engine with a real watched folder: stabilise, copy, register, read."""
    from omr_scanner.domain.intake import StabilityPolicy
    from omr_scanner.domain.processing import EngineLimits, UnitPolicy
    from omr_scanner.services import intake, load_template, resolve_active_template, scan_sessions
    from omr_scanner.services.continuous_engine import ContinuousEngine
    from omr_scanner.services.intake import IntakeService
    from omr_scanner.services.recognition_pool import ProcessRecogniser

    project = _project(workspace, "intake")
    try:
        template = load_template(resolve_active_template(project.project))  # type: ignore[arg-type]
        database = project.database
        session_id = scan_sessions.create_scan_session(database, name="Bench").scan_session_id
        folder = workspace / "scanner"
        folder.mkdir()
        source = intake.create_source(
            database, label="scanner", root_path=str(folder),
            policy=StabilityPolicy(min_observations=2, quiet_seconds=0.2,
                                   poll_interval_seconds=0.5),
        )
        intake.attach_source(database, source.source_id, session_id, actor="bench")
        for index, data in enumerate(sheets):
            (folder / f"{index:06d}.png").write_bytes(data)
        started = time.perf_counter()
        engine = ContinuousEngine(
            database, scan_session_id=session_id, template=template,
            recogniser=ProcessRecogniser(template, workers=workers, options=_options()),
            intake_factory=lambda: IntakeService(database, project.root),
            limits=EngineLimits.for_workers(workers),
            unit_policy=UnitPolicy(max_unit_size=unit, trickle_seconds=1.0),
        )
        engine.start()
        while True:
            engine.poll_intake()
            engine.form_units()
            report = engine.step(wait=0.05)
            status = engine.status()
            if report.idle and not engine.in_flight and status.caught_up and \
                    status.completed + status.failed + status.duplicates >= len(sheets):
                break
            if report.idle:
                time.sleep(0.05)
        engine.shutdown()
        return len(sheets) / (time.perf_counter() - started)
    finally:
        project.close()


def pool_startup(workspace: Path, sheets: list[bytes], workers: int) -> tuple[float, float]:
    """Seconds to a fresh pool's first result, and steady seconds per sheet after it."""
    from tests.crash.harness import template

    from omr_scanner.services.recognition_pool import ProcessRecogniser

    folder = workspace / "pool"
    folder.mkdir()
    paths = []
    for index, data in enumerate(sheets[: workers * 4]):
        path = folder / f"{index:06d}.png"
        path.write_bytes(data)
        paths.append(path)
    recogniser = ProcessRecogniser(template(), workers=workers, options=_options())
    started = time.perf_counter()
    for ticket, path in enumerate(paths):
        recogniser.submit(ticket, path)
    first = None
    done = 0
    while done < len(paths):
        got = recogniser.poll(0.05)
        if got and first is None:
            first = time.perf_counter() - started
        done += len(got)
    total = time.perf_counter() - started
    recogniser.close()
    assert first is not None
    steady = (total - first) / max(1, len(paths) - workers)
    return first, steady


def main() -> int:
    """Run every measurement ``--repeat`` times and print the ranges."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sheets", type=int, default=400)
    parser.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--unit", type=int, default=50)
    parser.add_argument("--repeat", type=int, default=1)
    arguments = parser.parse_args()

    print(f"{platform.processor()} | {os.cpu_count()} logical CPUs | {platform.platform()} | "
          f"Python {platform.python_version()}")
    import cv2

    print(f"OpenCV {cv2.__version__}; {arguments.sheets} sheets, {arguments.workers} workers, "
          f"unit {arguments.unit}")
    sheets = _sheets(arguments.sheets)
    results: dict[str, list[float]] = {"finite": [], "engine_units": [], "engine_intake": []}
    for run in range(arguments.repeat):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "p").mkdir()
            first, steady = pool_startup(workspace / "p", sheets, arguments.workers)
            print(f"run {run + 1}: pool start-up to first result {first:.2f} s; "
                  f"steady {steady * 1000:.0f} ms/sheet across the pool")
            for name, call in (
                ("finite", lambda w: finite(w, sheets, arguments.workers)),
                ("engine_units", lambda w: engine_units(w, sheets, arguments.workers,
                                                        arguments.unit)[0]),
                ("engine_intake", lambda w: engine_intake(w, sheets, arguments.workers,
                                                          arguments.unit)),
            ):
                target = workspace / name
                target.mkdir()
                rate = call(target)
                results[name].append(rate)
                print(f"run {run + 1}: {name:14s} {rate:6.1f} sheets/s")
    for name, rates in results.items():
        print(f"{name:14s} sheets/s: min {min(rates):.1f}  median {statistics.median(rates):.1f}  "
              f"max {max(rates):.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
