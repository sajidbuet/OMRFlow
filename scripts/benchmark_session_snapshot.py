"""Measure the session snapshot on a large metadata population (0.1.1 revised phase 7).

Purpose:
    Evidence for ``PHASE_G_HANDOFF.md`` §13 - not a qualification: how long
    :func:`omr_scanner.services.session_snapshot.take_snapshot` takes, and how
    many SQL statements it issues, for sessions of tens of thousands of sheet
    rows (no images; rows written by ``tests/snapshot_population.py``), so that
    phase 8 can choose a polling interval from numbers.

Usage::

    python scripts/benchmark_session_snapshot.py --sheets 10000 50000 --repeat 5
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

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT):
    if str(entry) not in sys.path:  # pragma: no cover - script bootstrap
        sys.path.insert(0, str(entry))


def main() -> int:
    """Build each population in a fresh project and time repeated snapshots."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sheets", type=int, nargs="+", default=[10_000, 50_000])
    parser.add_argument("--repeat", type=int, default=5)
    arguments = parser.parse_args()

    from tests.snapshot_population import build

    from omr_scanner.services import create_project
    from omr_scanner.services.session_snapshot import take_snapshot

    try:
        import psutil

        memory = f"{psutil.virtual_memory().total / 2**30:.0f} GB RAM"
    except ImportError:  # pragma: no cover - optional
        memory = "RAM unknown"
    print(
        f"Hardware: {platform.processor() or platform.machine()}, {os.cpu_count()} logical "
        f"CPUs, {memory}; {platform.platform()}; Python {platform.python_version()}"
    )
    for sheets in arguments.sheets:
        with tempfile.TemporaryDirectory() as folder:
            project = create_project(Path(folder), f"Snapshot {sheets}")
            try:
                started = time.perf_counter()
                population = build(project.database, sheets=sheets)
                built = time.perf_counter() - started
                started = time.perf_counter()
                first = take_snapshot(project.database, population.scan_session_id)
                cold = time.perf_counter() - started
                timings = []
                for _ in range(arguments.repeat):
                    started = time.perf_counter()
                    snapshot = take_snapshot(project.database, population.scan_session_id)
                    timings.append(time.perf_counter() - started)
                assert snapshot.partitions and first.partitions
                size = (project.root / "database.sqlite").stat().st_size
                print(
                    f"population {snapshot.discovered_excluding_ignored} files/sheets "
                    f"({population.units} units, {population.sources} sources): "
                    f"database {size / 2**20:.1f} MiB (rows written in {built:.1f} s); "
                    f"snapshot statements {snapshot.query_count}; "
                    f"first {cold * 1000:.0f} ms, then median "
                    f"{statistics.median(timings) * 1000:.0f} ms, "
                    f"max {max(timings) * 1000:.0f} ms over {len(timings)}"
                )
            finally:
                project.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
