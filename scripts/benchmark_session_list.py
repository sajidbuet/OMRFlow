"""Measure the operational GUI's paged session sheet list (0.1.1 revised phase 8).

Purpose:
    Evidence for ``PHASE_H_HANDOFF.md`` §8/§15 - not a qualification: how long
    one page and one count of :mod:`omr_scanner.services.session_sheets` take
    on a metadata population of 10,000 and 100,000 sheets
    (``tests/snapshot_population.py``: rows only, no images), for the default
    newest-first page, a deep page, each filter, a sort and a search.

Usage::

    python scripts/benchmark_session_list.py --sheets 10000 100000 --repeat 5
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
    """Build each population in a fresh project and time the list operations."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sheets", type=int, nargs="+", default=[10_000, 100_000])
    parser.add_argument("--repeat", type=int, default=5)
    arguments = parser.parse_args()

    from tests.snapshot_population import build

    from omr_scanner.services import create_project
    from omr_scanner.services.session_sheets import (
        ConflictStateFilter,
        QualityFilter,
        RescanFilter,
        SheetQuery,
        SheetSort,
        StatusFilter,
        count_sheets,
        list_sheets,
    )

    print(
        f"Hardware: {platform.processor() or platform.machine()}, {os.cpu_count()} logical "
        f"CPUs; {platform.platform()}; Python {platform.python_version()}"
    )
    queries = {
        "default (newest first)": SheetQuery(),
        "status: failed": SheetQuery(status=StatusFilter.FAILED),
        "quality: suggested": SheetQuery(quality=QualityFilter.SUGGESTED),
        "conflict: unresolved": SheetQuery(conflict=ConflictStateFilter.UNRESOLVED),
        "rescan: rejected": SheetQuery(rescan=RescanFilter.REJECTED),
        "sort: student id": SheetQuery(sort=SheetSort.STUDENT_ID, descending=False),
        "sort: file name": SheetQuery(sort=SheetSort.FILE_NAME, descending=False),
        "search: 00123": SheetQuery(search="00123"),
    }
    for sheets in arguments.sheets:
        with tempfile.TemporaryDirectory() as folder:
            project = create_project(Path(folder), f"List {sheets}")
            try:
                population = build(project.database, sheets=sheets)
                session_id = population.scan_session_id
                print(f"\n{sheets:,} sheets ({population.units} units):")
                for name, query in queries.items():
                    counts, pages = [], []
                    for _ in range(arguments.repeat):
                        started = time.perf_counter()
                        total = count_sheets(project.database, session_id, query)
                        counts.append(time.perf_counter() - started)
                        started = time.perf_counter()
                        rows = list_sheets(project.database, session_id, query)
                        pages.append(time.perf_counter() - started)
                    print(
                        f"  {name:<24} count {statistics.median(counts) * 1000:6.1f} ms  "
                        f"page {statistics.median(pages) * 1000:6.1f} ms  "
                        f"(max {max(pages) * 1000:6.1f}; {total:,} match, {len(rows)} shown)"
                    )
                deep = []
                for _ in range(arguments.repeat):
                    started = time.perf_counter()
                    list_sheets(project.database, session_id, offset=sheets // 2)
                    deep.append(time.perf_counter() - started)
                print(f"  {'middle page':<24} page {statistics.median(deep) * 1000:6.1f} ms")
            finally:
                project.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
