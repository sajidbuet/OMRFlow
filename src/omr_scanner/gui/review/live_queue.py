"""Resolve's live refresh, read off the GUI thread (0.1.1 revised phase 8).

Purpose:
    While a continuous session scans, Resolve's queue must follow new
    conflicts - without visiting Scan and without freezing the operator who
    is deciding one. The queue's reads are the session-wide ones the stage
    already uses (``list_conflicts`` / ``count_conflicts`` /
    ``count_cases``, each over the session's effective population); at
    10,000 sheets one refresh is around a second of database and Python work
    - acceptable for a click, not for every snapshot. So the **live** refresh
    reads everything it needs here, in a worker thread, and the page only
    applies the result. The operator's own actions keep the stage's ordinary
    synchronous refresh.

Bounded:
    One read at a time; asking while one runs owes exactly one more, made
    with whatever the queue's controls say when the first returns. Results
    carry a generation, and the page drops one that no longer matches what is
    on screen.

What does NOT belong here:
    Widgets, and any rule - these are the same service calls the stage makes,
    only on another thread.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal, Slot

from omr_scanner.services import (
    count_conflicts,
    intake_decisions,
    list_conflicts,
    load_summary,
    quality_decisions,
    scan_lifecycle,
    scan_sessions,
    session_population,
    session_sheets,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.domain.review import ReviewCounts
    from omr_scanner.domain.scan_lifecycle import RescanCounts
    from omr_scanner.services import (
        BatchSummary,
        ConflictFilter,
        ConflictRecord,
        ProjectDatabase,
    )
    from omr_scanner.services.scan_sessions import ScanSessionInfo
    from omr_scanner.services.session_sheets import SourceOption

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QueueSummary:
    """The counts under Resolve's queue - conflicts, rescans, suggestions, waiting files."""

    counts: ReviewCounts
    rescans: RescanCounts
    suggested: int = 0
    waiting: int = 0


def summary_counts(
    database: ProjectDatabase, batch_id: str, scan_session_id: str
) -> QueueSummary:
    """Read the summary counts (the session's, whichever batch anchors the queue)."""
    counts = count_conflicts(database, batch_id, session_wide=True)
    rescans = scan_lifecycle.count_cases(database, batch_id, session_wide=True)
    suggested = waiting = 0
    if scan_session_id:
        suggested = quality_decisions.count_outstanding(database, scan_session_id)
        waiting = intake_decisions.count_pending(database, scan_session_id)
    return QueueSummary(counts=counts, rescans=rescans, suggested=suggested, waiting=waiting)


@dataclass(frozen=True, slots=True)
class LiveQueueData:
    """One live read of the queue, for the page to apply.

    Attributes:
        generation: Which request this answers.
        view: The queue view it was read for (the state filter's text).
        conflicts: The conflict view's rows, or ``None`` for another view.
        summary: The counts under the queue.
        batch_options: ``(label, batch id)`` of every batch of the session.
        population_text: The session's description for the heading.
        multi_batch: Whether the session has more than one batch.
        elapsed_ms: How long the read took (in the worker).
        sources: The session's sources, for the source filter.
        session: The session's identity and lifecycle, for the heading.
        batch_summary: The anchoring batch's stored summary, for a one-batch
            heading.
    """

    generation: int
    view: str
    conflicts: tuple[ConflictRecord, ...] | None
    summary: QueueSummary
    batch_options: tuple[tuple[str, str], ...]
    population_text: str
    multi_batch: bool
    elapsed_ms: float
    sources: tuple[SourceOption, ...] = ()
    session: ScanSessionInfo | None = None
    batch_summary: BatchSummary | None = None


@dataclass(frozen=True, slots=True)
class LiveQueueRequest:
    """What to read."""

    database: ProjectDatabase
    batch_id: str
    scan_session_id: str
    view: str
    conflict_filter: ConflictFilter | None
    limit: int
    generation: int


def read_live_queue(request: LiveQueueRequest) -> LiveQueueData:
    """Do the live refresh's reads - the stage's own service calls - in one go."""
    started = time.perf_counter()
    database, batch_id = request.database, request.batch_id
    conflicts = (
        tuple(
            session_sheets.with_original_names(
                database,
                list_conflicts(
                    database,
                    batch_id,
                    filters=request.conflict_filter,
                    limit=request.limit,
                    session_wide=True,
                ),
                ("scan_id", "scan_name"),
            )
        )
        if request.conflict_filter is not None
        else None
    )
    population = session_population.population(database, batch_id)
    options = tuple(
        (
            f"Batch {position} · {item[:8]}"
            + ("" if item in population.live_batch_ids else " · superseded"),
            item,
        )
        for position, item in enumerate(population.batch_ids, start=1)
    )
    session_id = request.scan_session_id
    return LiveQueueData(
        generation=request.generation,
        view=request.view,
        conflicts=conflicts,
        summary=summary_counts(database, batch_id, session_id),
        batch_options=options,
        population_text=session_population.describe(population),
        multi_batch=len(population.batch_ids) > 1,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        sources=session_sheets.session_sources(database, session_id) if session_id else (),
        session=scan_sessions.get_scan_session(database, session_id) if session_id else None,
        batch_summary=load_summary(database, batch_id),
    )


class _LiveWorker(QObject):
    done = Signal(object)
    failed = Signal(int, str)

    @Slot(object)
    def read(self, request: object) -> None:
        try:
            data = read_live_queue(request)  # type: ignore[arg-type]
        except Exception as exc:  # a live refresh must never kill the thread
            _LOGGER.warning("Live queue refresh failed: %s", exc)
            self.failed.emit(getattr(request, "generation", 0), str(exc))
            return
        self.done.emit(data)


class LiveQueueReader(QObject):
    """Runs :func:`read_live_queue` in its own thread, one read at a time.

    Signals:
        ready: a :class:`LiveQueueData` on the GUI thread.
    """

    ready = Signal(object)
    _request = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _LiveWorker | None = None
        self._busy = False
        self._owed: object | None = None
        self.reads = 0

    @property
    def busy(self) -> bool:
        """Whether a read is in progress."""
        return self._busy

    def request(self, request: LiveQueueRequest) -> None:
        """Read now, or - while a read runs - owe this one (replacing an older owed one)."""
        if self._busy:
            self._owed = request
            return
        self._ensure_thread()
        self._busy = True
        self.reads += 1
        self._request.emit(request)

    def shutdown(self) -> None:
        """Stop the thread, waiting for a read in progress. Safe to call twice."""
        self._owed = None
        thread, worker = self._thread, self._worker
        if worker is not None:
            with contextlib.suppress(RuntimeError, TypeError):  # already disconnected
                self._request.disconnect(worker.read)
        if thread is not None:
            thread.quit()
            thread.wait()
            thread.deleteLater()
        self._thread = None
        self._worker = None
        self._busy = False

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        thread = QThread(self)
        thread.setObjectName("resolveLiveQueueThread")
        worker = _LiveWorker()
        worker.moveToThread(thread)
        self._request.connect(worker.read)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        thread.finished.connect(worker.deleteLater)
        thread.start()
        self._thread = thread
        self._worker = worker

    def _on_done(self, data: object) -> None:
        self._busy = False
        self.ready.emit(data)
        self._settle()

    def _on_failed(self, _generation: int, _message: str) -> None:
        self._busy = False
        self._settle()

    def _settle(self) -> None:
        owed, self._owed = self._owed, None
        if owed is not None:
            self.request(owed)  # type: ignore[arg-type]


__all__ = [
    "LiveQueueData",
    "LiveQueueReader",
    "LiveQueueRequest",
    "QueueSummary",
    "read_live_queue",
    "summary_counts",
]
