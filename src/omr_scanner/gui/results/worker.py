"""Background scoring, so a large cohort does not freeze the interface.

Scoring is arithmetic and is fast - ten thousand candidates is well under a
second of calculation - but the *writes* are not, and the recognition results
have to be decoded first. Both happen off the GUI thread, following the pattern
Phase 3's ``PreviewWorker``, Phase 6's ``SheetWorker`` and Phase 7's
``ReconcileWorker`` established.

Cancellation stops between candidates and **does not commit a partial run**:
the results a cancelled run computed are discarded rather than written, so a
batch never ends up half-marked under two different policies while claiming to
be current. That is stricter than "leave it coherent" and is the only behaviour
that keeps the summary honest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PySide6.QtCore import QObject

    from omr_scanner.domain.scoring import ResultCounts
    from omr_scanner.domain.template import OmrTemplate
    from omr_scanner.services import ProjectDatabase

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScoringResult:
    """What a scoring run produced."""

    counts: ResultCounts | None = None
    error: str = ""
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        """Whether the run completed."""
        return self.counts is not None and not self.cancelled


class ScoringWorker(QThread):
    """Score a batch without blocking the interface."""

    ready = Signal(object)
    """Emitted with a :class:`ScoringResult` when scoring finishes."""

    progressed = Signal(int, int)
    """Emitted with ``(done, total)`` as candidates are marked."""

    def __init__(
        self,
        database: ProjectDatabase,
        roster_id: int,
        batch_id: str,
        template: OmrTemplate,
        *,
        computed_by: str = "",
        candidates: tuple[str, ...] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._roster_id = roster_id
        self._batch_id = batch_id
        self._template = template
        self._computed_by = computed_by
        self._candidates = candidates
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the run to stop after the candidate in progress."""
        self._cancelled = True

    def run(self) -> None:
        """Score. Runs on the worker thread."""
        from omr_scanner.services import scoring_store

        try:
            counts = scoring_store.score_batch(
                self._database,
                self._roster_id,
                self._batch_id,
                self._template,
                computed_by=self._computed_by,
                candidates=self._candidates,
                should_cancel=lambda: self._cancelled,
                on_progress=self.progressed.emit,
            )
        except OMRScannerError as exc:
            # Type and message only when the message is the services layer's
            # own operator-facing text; nothing from a candidate's record.
            _LOGGER.warning("Scoring failed: %s", type(exc).__name__)
            self.ready.emit(ScoringResult(error=exc.user_message or str(exc)))
            return
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.error("Unexpected failure during scoring: %s", type(exc).__name__)
            self.ready.emit(
                ScoringResult(error=f"Scoring could not complete. ({exc})")
            )
            return
        self.ready.emit(ScoringResult(counts=counts, cancelled=self._cancelled))
