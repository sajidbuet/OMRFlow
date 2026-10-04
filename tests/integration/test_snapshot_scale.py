"""The session snapshot stays bounded on a large metadata population (revised phase 7).

Ten thousand sheet rows (no images), written by ``tests/snapshot_population.py``:
the snapshot partitions, issues the same fixed set of statements as for a
session a tenth the size, and stays well inside a polling budget. Measured
numbers for larger populations are ``scripts/benchmark_session_snapshot.py``'s.
"""

from __future__ import annotations

import time

from tests.snapshot_population import build

from omr_scanner.services.session_snapshot import take_snapshot


def test_ten_thousand_rows_one_fixed_set_of_grouped_queries(project_session):
    database = project_session.database
    small = build(database, sheets=1_000)
    large = build(database, sheets=10_000)
    first = take_snapshot(database, small.scan_session_id)
    started = time.perf_counter()
    snapshot = take_snapshot(database, large.scan_session_id)
    elapsed = time.perf_counter() - started
    assert snapshot.partitions
    assert snapshot.discovered_excluding_ignored == 10_000 + large.unregistered
    assert snapshot.query_count == first.query_count
    assert len(snapshot.sources) == 3
    assert snapshot.partition.queued == 200  # 2 % pending
    assert snapshot.outstanding_suggestions > 0
    print(
        f"\nSnapshot of {snapshot.discovered_excluding_ignored} files/sheets: "
        f"{elapsed * 1000:.0f} ms, {snapshot.query_count} statements in its read transaction"
    )
    assert elapsed < 10.0, "a polled snapshot must not grow pathologically with the session"
