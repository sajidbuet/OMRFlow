"""The session-authoritative downstream entry points (0.1.1 phase 4).

Purpose:
    Attendance, scoring, Results and Reports select **a scan session**, by
    its id, and every authoritative operation here takes that id. The
    session's effective sheet set is
    :mod:`omr_scanner.services.session_population`'s; the rows these
    operations read and write are kept in the session's **recorded
    downstream store** (``scan_session.downstream_batch_id``, ADR-0007) -
    an internal storage key, resolved here and nowhere else from the
    session id, never chosen by a caller and never "the latest batch".

    A batch that belongs to no scan session (a project not yet upgraded, or
    rows a tool wrote directly) is addressed by the pseudo-session id
    ``batch:<batch id>`` and is its own one-batch population.

Compatibility:
    The batch-keyed functions of ``reconciliation_store``, ``scoring_store``
    and ``report_store`` remain: they take any batch of a session, normalise
    it to the session's store and behave identically. They are kept for the
    per-entry operations (assigning a script, overriding attendance, one
    candidate's result) and for existing callers; new authoritative call
    sites use this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omr_scanner.errors import OMRScannerError
from omr_scanner.services import (
    reconciliation_store,
    report_store,
    scoring_store,
    session_population,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from omr_scanner.database.engine import ProjectDatabase
    from omr_scanner.domain.reconciliation import ReconciliationCounts, ReconciliationEntry
    from omr_scanner.domain.reporting import ReadinessReport
    from omr_scanner.domain.scoring import ResultCounts
    from omr_scanner.domain.template import OmrTemplate


class SessionScopeError(OMRScannerError):
    """The named scan session has no batch to report on."""


def store(database: ProjectDatabase, scan_session_id: str) -> str:
    """The downstream store of ``scan_session_id`` - its recorded storage key.

    Raises:
        SessionScopeError: The session has no batch at all.
    """
    if scan_session_id.startswith(session_population.LONE_BATCH_PREFIX):
        return scan_session_id.removeprefix(session_population.LONE_BATCH_PREFIX)
    found = session_population.session_key(database, scan_session_id)
    if found is None:
        raise SessionScopeError(
            f"Scan session {scan_session_id} has no batch",
            user_message="This scan session has no scanned batch yet.",
        )
    return found


def session_of(database: ProjectDatabase, batch_id: str) -> str:
    """The session id (or ``batch:<id>``) a batch belongs to - for compatibility callers."""
    return session_population.session_of_batch(database, batch_id)


def population(
    database: ProjectDatabase, scan_session_id: str
) -> session_population.SessionPopulation:
    """The session's effective sheet set."""
    return session_population.session_population(database, scan_session_id)


def reconcile(
    database: ProjectDatabase, roster_id: int, scan_session_id: str
) -> ReconciliationCounts:
    """Reconcile the session's effective sheet set against one roster."""
    return reconciliation_store.reconcile_batch(
        database, roster_id, store(database, scan_session_id)
    )


def entries(
    database: ProjectDatabase, roster_id: int, scan_session_id: str, **kwargs: Any
) -> tuple[ReconciliationEntry, ...]:
    """The session's stored reconciliation entries for one roster."""
    return reconciliation_store.list_entries(
        database, roster_id, store(database, scan_session_id), **kwargs
    )


def stored_counts(
    database: ProjectDatabase, roster_id: int, scan_session_id: str
) -> ReconciliationCounts | None:
    """The counts the session's last reconciliation against one roster recorded."""
    return reconciliation_store.stored_counts(
        database, roster_id, store(database, scan_session_id)
    )


def score(
    database: ProjectDatabase,
    roster_id: int,
    scan_session_id: str,
    template: OmrTemplate,
    **kwargs: Any,
) -> ResultCounts:
    """Score the session's candidates for one roster."""
    return scoring_store.score_batch(
        database, roster_id, store(database, scan_session_id), template, **kwargs
    )


def results(
    database: ProjectDatabase,
    roster_id: int,
    scan_session_id: str,
    template: OmrTemplate | None = None,
    **kwargs: Any,
) -> tuple[scoring_store.StoredResult, ...]:
    """The session's stored results for one roster."""
    return scoring_store.list_results(
        database, roster_id, store(database, scan_session_id), template, **kwargs
    )


def overview(
    database: ProjectDatabase, roster_id: int, scan_session_id: str
) -> tuple[report_store.SetOverview, ...]:
    """The Result Management set list for the session."""
    return report_store.set_overview(database, roster_id, store(database, scan_session_id))


def readiness(
    database: ProjectDatabase,
    roster_id: int,
    scan_session_id: str,
    template: OmrTemplate,
    set_code: str,
    *,
    for_final_export: bool,
) -> ReadinessReport:
    """One set's readiness for the session (Final Export requires it CLOSED)."""
    return report_store.check_readiness(
        database, roster_id, store(database, scan_session_id), template, set_code,
        for_final_export=for_final_export,
    )


def generate_for_set(
    database: ProjectDatabase,
    set_id: str,
    scan_session_id: str,
    template: OmrTemplate,
    **kwargs: Any,
) -> report_store.GenerationOutcome:
    """Generate one defined set's report from the session."""
    return report_store.generate_for_set(
        database, set_id, store(database, scan_session_id), template, **kwargs
    )


def final_export_status(
    database: ProjectDatabase, scan_session_id: str, set_code: str
) -> report_store.FinalExportStatus:
    """Whether the set's latest Final Export from the session still stands."""
    return report_store.final_export_status(
        database, store(database, scan_session_id), set_code
    )


__all__ = [
    "SessionScopeError",
    "entries",
    "final_export_status",
    "generate_for_set",
    "overview",
    "population",
    "readiness",
    "reconcile",
    "results",
    "score",
    "session_of",
    "store",
    "stored_counts",
]
