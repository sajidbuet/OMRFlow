"""Background workers for roster import and reconciliation.

Purpose:
    Keep two operations off the GUI thread: parsing a workbook, which is
    seconds for a large cohort, and reconciling a batch, which writes a row per
    candidate and a row per script.

Why threads rather than processes:
    Both operations are one pass over data the main process already owns, and
    neither is CPU-bound in the way recognition is. A process would mean
    pickling the roster back, and - for reconciliation - a second connection to
    a single-writer SQLite database, which Phase 5 deliberately avoids. Phase
    5's worker *pool* is untouched by any of this; reconciliation happens after
    recognition, never inside it.

The pattern is the one Phase 3's ``PreviewWorker`` and Phase 6's
``SheetWorker`` established: a ``QThread`` that carries its inputs, does the
work in :meth:`run`, and emits one signal carrying a plain result object.
Failures arrive as a message on that same object rather than as an exception
crossing a thread boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from omr_scanner.errors import OMRScannerError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from PySide6.QtCore import QObject

    from omr_scanner.domain.reconciliation import ReconciliationCounts
    from omr_scanner.services import ProjectDatabase
    from omr_scanner.services.candidate_import import ColumnMapping, RosterValidation

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RosterReadResult:
    """What reading a roster file produced.

    Exactly one of :attr:`validation` and :attr:`error` is set. ``error`` is
    already an operator-facing sentence - the services layer produces those -
    so the page shows it without rewording.
    """

    validation: RosterValidation | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether the file was read."""
        return self.validation is not None


class RosterReadWorker(QThread):
    """Read and validate a roster file without blocking the interface."""

    ready = Signal(object)
    """Emitted with a :class:`RosterReadResult` when reading finishes."""

    def __init__(
        self,
        path: Path,
        mapping: ColumnMapping | None,
        sheet: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._mapping = mapping
        self._sheet = sheet

    def run(self) -> None:
        """Read the file. Runs on the worker thread."""
        from omr_scanner.services.candidate_import import read_roster

        try:
            validation = read_roster(self._path, self._mapping, sheet=self._sheet)
        except OMRScannerError as exc:
            # The *kind* of failure, never its message. A duplicate-ID refusal
            # names the candidate ID in its user message precisely so the
            # operator can find the row - which is exactly why that message
            # must not reach a log file.
            _LOGGER.warning(
                "Candidate list could not be read: %s", type(exc).__name__
            )
            self.ready.emit(RosterReadResult(error=exc.user_message or str(exc)))
            return
        except Exception as exc:  # pragma: no cover - defensive
            # Type only, and no traceback. Everything this code path touches
            # came out of a file full of candidate data, and a third-party
            # library's message can quote the cell it choked on. The operator
            # still sees the detail, in the dialog, where it is allowed.
            _LOGGER.error(
                "Unexpected failure reading a candidate list: %s",
                type(exc).__name__,
            )
            self.ready.emit(
                RosterReadResult(
                    error=(
                        "The candidate list could not be read. It may be "
                        f"damaged or in an unexpected format. ({exc})"
                    )
                )
            )
            return
        self.ready.emit(RosterReadResult(validation=validation))


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What a reconciliation run produced."""

    counts: ReconciliationCounts | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        """Whether reconciliation completed."""
        return self.counts is not None


class ReconcileWorker(QThread):
    """Run reconciliation without blocking the interface."""

    ready = Signal(object)
    """Emitted with a :class:`ReconcileResult` when reconciliation finishes."""

    def __init__(
        self,
        database: ProjectDatabase,
        roster_id: int,
        batch_id: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._database = database
        self._roster_id = roster_id
        self._batch_id = batch_id

    def run(self) -> None:
        """Reconcile. Runs on the worker thread."""
        from omr_scanner.services import reconciliation_store

        try:
            counts = reconciliation_store.reconcile_batch(
                self._database, self._roster_id, self._batch_id
            )
        except OMRScannerError as exc:
            _LOGGER.warning("Reconciliation failed: %s", type(exc).__name__)
            self.ready.emit(ReconcileResult(error=exc.user_message or str(exc)))
            return
        except Exception as exc:  # pragma: no cover - defensive
            # Type only, for the reason given in `RosterReadWorker.run`: a
            # database error here can quote the candidate ID it tripped over.
            _LOGGER.error(
                "Unexpected failure during reconciliation: %s", type(exc).__name__
            )
            self.ready.emit(
                ReconcileResult(error=f"Reconciliation could not complete. ({exc})")
            )
            return
        self.ready.emit(ReconcileResult(counts=counts))
