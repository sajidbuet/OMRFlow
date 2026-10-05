"""The operational GUI stays responsive on a large, growing session (revised phase 8).

ACCEPTANCE_CRITERIA F6: at 10,000 files with ongoing arrivals the GUI stays
responsive - **measured**, not "it did not hang".

Set-up: a 10,000-sheet metadata session (``tests/snapshot_population.py``)
open on the real Scan page in session mode - the real 1-second snapshot
poller, the paged session list - with the real Resolve page receiving every
snapshot and refreshing its queue live. A writer thread commits new sheets
(and a conflict every few) continuously, as the engine would, so counts
change on every poll. Meanwhile the GUI thread performs operator actions
(turning list pages, changing filters, moving through the Resolve queue) and
a 20 ms heartbeat timer measures how late the event loop delivers it.

Thresholds (recorded in ``PHASE_H_HANDOFF.md`` §15): median lateness under
30 ms and 95th percentile under 150 ms - human perception of "instant" is
around 100 ms - and the worst single stall under 2,000 ms. The worst case is
not the live updates (their reads run in worker threads) but an operator's
own action reading the database on the GUI thread while the writer is
committing about twenty groups a second: in SQLite's rollback-journal mode
(ADR-0002, unchanged - not WAL) a reader then waits for the writer, which
waits for the snapshot's read transaction. Measured, explained, bounded - not
hidden; the application's hang watchdog treats ten seconds as unresponsive.
"""

from __future__ import annotations

import statistics
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import QApplication
from sqlalchemy import insert, select
from tests.snapshot_population import build

from omr_scanner.database.models import BatchScan, ReviewConflict, ScanBatch
from omr_scanner.gui.pages import WORKFLOW_PAGES
from omr_scanner.gui.review.page import ResolvePage
from omr_scanner.gui.scan.page import ScanPage
from omr_scanner.services import create_project

pytestmark = pytest.mark.gui

SHEETS = 10_000
RUN_SECONDS = 12.0
HEARTBEAT_MS = 20
MEDIAN_LATE_MS = 30.0
P95_LATE_MS = 150.0
WORST_LATE_MS = 2_000.0


class Arrivals(threading.Thread):
    """Commits new sheets into the session in small groups, as the engine does."""

    def __init__(self, database, session_id: str, source_id: str) -> None:
        super().__init__(name="arrivals", daemon=True)
        self.database = database
        self.session_id = session_id
        self.source_id = source_id
        self.stop = threading.Event()
        self.committed = 0
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            serial = 0
            while not self.stop.is_set():
                batch_id = uuid.uuid4().hex
                moment = datetime.now(UTC)
                with self.database.session() as session:
                    session.execute(insert(ScanBatch), [{
                        "batch_id": batch_id, "created_at": moment, "updated_at": moment,
                        "source_folder": "", "template_id": "bench", "template_name": "bench",
                        "template_path": "", "geometry_fingerprint": "g",
                        "recognition_fingerprint": "r", "engine_version": "bench",
                        "settings_json": "{}", "status": "completed", "total_scans": 5,
                        "scan_session_id": self.session_id, "sealed_at": moment,
                        "sealed_by": "bench", "role": "scan", "source_id": self.source_id,
                    }])
                for _ in range(10):
                    if self.stop.is_set():
                        break
                    with self.database.session() as session:
                        session.execute(insert(BatchScan), [{
                            "batch_id": batch_id, "batch_index": serial % 1000,
                            "source_path": rf"\\live\scans\{serial:07d}.png",
                            "filename": f"live{serial:07d}.png", "status": "completed",
                            "attempt_count": 1, "outcome": "complete",
                            "identifier_value": f"{18_000_000 + serial:08d}",
                            "content_sha256": f"live{serial}", "finished_at": moment,
                        }])
                        if serial % 4 == 0:
                            scan_id = session.scalar(
                                select(BatchScan.scan_id).where(
                                    BatchScan.filename == f"live{serial:07d}.png"
                                )
                            )
                            session.execute(insert(ReviewConflict), [{
                                "batch_id": batch_id, "scan_id": scan_id,
                                "conflict_type": "identifier_multiple", "state": "open",
                                "zone_id": "roll_number", "group_key": 0,
                                "created_at": moment, "updated_at": moment,
                            }])
                    serial += 1
                    self.committed += 1
                    time.sleep(0.05)
        except BaseException as exc:  # reported by the test
            self.error = exc


def test_ten_thousand_sheets_with_arrivals_keep_the_event_loop_responsive(qtbot, tmp_path):
    project = create_project(tmp_path, "Responsiveness")
    page = resolve = None
    arrivals = None
    try:
        population = build(project.database, sheets=SHEETS)
        with project.database.session() as session:
            source_id = session.scalar(
                select(ScanBatch.source_id).where(
                    ScanBatch.scan_session_id == population.scan_session_id
                )
            )
        page = ScanPage(next(item for item in WORKFLOW_PAGES if item.key == "scan"))
        qtbot.addWidget(page)
        page.set_reviewer("Operator")
        resolve = ResolvePage(next(item for item in WORKFLOW_PAGES if item.key == "resolve"))
        qtbot.addWidget(resolve)
        resolve.set_reviewer("Operator")
        page.session_view_changed.connect(resolve.on_session_view)
        page.on_project_changed(project)
        resolve.on_project_changed(project)
        resolve.load_session(population.scan_session_id)
        page.resize(1366, 768)
        page.show()
        resolve.resize(1366, 768)
        resolve.show()
        assert page.session_mode.active
        qtbot.waitUntil(lambda: page.session_sheet_list.total >= SHEETS, timeout=60_000)

        arrivals = Arrivals(project.database, population.scan_session_id, str(source_id))
        late: list[float] = []
        clock = QElapsedTimer()
        last = [0]

        def beat() -> None:
            now = clock.elapsed()
            if last[0]:
                late.append(max(0.0, now - last[0] - HEARTBEAT_MS))
            last[0] = now

        actions: list[float] = []
        by_kind: dict[int, list[float]] = {0: [], 1: [], 2: [], 3: []}
        step = [0]

        def act() -> None:
            started = time.perf_counter()
            listing = page.session_sheet_list
            choice = step[0] % 4
            if choice == 0:
                listing.next_page() or listing.previous_page()
            elif choice == 1:
                listing.status_combo.setCurrentIndex(
                    (listing.status_combo.currentIndex() + 1) % listing.status_combo.count()
                )
            elif choice == 2:
                # Working through the queue while scanning continues. (Switching
                # Resolve's *view* re-reads the session population on the GUI
                # thread - about a second at 10,000 sheets; measured separately
                # and recorded as a limitation in the handoff.)
                resolve.select_next()
            else:
                page.session_mode.poller.refresh()
            step[0] += 1
            elapsed = (time.perf_counter() - started) * 1000.0
            actions.append(elapsed)
            by_kind[choice].append(elapsed)

        heartbeat = QTimer()
        heartbeat.setInterval(HEARTBEAT_MS)
        heartbeat.timeout.connect(beat)
        operator = QTimer()
        operator.setInterval(400)
        operator.timeout.connect(act)
        start_requests = page.session_mode.poller.requests
        arrivals.start()
        clock.start()
        heartbeat.start()
        operator.start()
        deadline = time.monotonic() + RUN_SECONDS
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.001)
        heartbeat.stop()
        operator.stop()
        arrivals.stop.set()
        arrivals.join(timeout=30)
        assert arrivals.error is None, arrivals.error
        polls = page.session_mode.poller.requests - start_requests

        ordered = sorted(late)
        median = statistics.median(ordered)
        p95 = ordered[int(len(ordered) * 0.95) - 1]
        worst = ordered[-1]
        print(
            f"\n10k responsiveness: {SHEETS:,} sheets + {arrivals.committed} arrivals in "
            f"{RUN_SECONDS:.0f} s; snapshot polls {polls} (interval 1,000 ms), list page "
            f"{page.session_sheet_list.page_size}; heartbeat {HEARTBEAT_MS} ms x {len(late)}: "
            f"lateness median {median:.1f} ms, p95 {p95:.1f} ms, worst {worst:.1f} ms; "
            f"operator actions {len(actions)}: median {statistics.median(actions):.1f} ms, "
            f"worst {max(actions):.1f} ms; last list read "
            f"{page.session_sheet_list.last_query_ms:.0f} ms, last live Resolve read "
            f"{resolve.last_live_read_ms:.0f} ms (both in worker threads); worst per action: "
            + ", ".join(
                f"{name} {max(by_kind[kind] or [0.0]):.0f} ms"
                for kind, name in enumerate(
                    ("list page", "list filter", "Resolve next", "snapshot refresh")
                )
            )
        )
        assert arrivals.committed > 50
        assert polls <= RUN_SECONDS * 2 + 4  # ~1/s; never a storm
        assert median < MEDIAN_LATE_MS, median
        assert p95 < P95_LATE_MS, p95
        assert worst < WORST_LATE_MS, worst
        # The live counts followed the arrivals (the snapshot, not the GUI, counted).
        qtbot.waitUntil(
            lambda: page.session_mode.view is not None
            and page.session_mode.view.snapshot.partition.total >= SHEETS + arrivals.committed,
            timeout=30_000,
        )
    finally:
        if arrivals is not None:
            arrivals.stop.set()
            arrivals.join(timeout=30)
        if page is not None:
            page.shutdown_background_work()
        if resolve is not None:
            resolve.shutdown()
        project.close()
